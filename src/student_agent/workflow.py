"""Coordinator and multi-agent workflow entrypoint for Day09 L3B."""
from __future__ import annotations

import asyncio
from typing import Any

from .agents.conflict_resolver import resolve_conflicts
from .agents.entity_agent import run_entity_agent
from .agents.order_agent import run_order_agent
from .agents.payment_agent import run_payment_agent
from .agents.policy_agent import run_policy_agent
from .agents.shipment_agent import run_shipment_agent
from .agents.verifier import verify_and_calibrate
from .mcp_gateway import EvidenceGateway
from .models import (
    ConflictResult,
    EntityResult,
    OrderResult,
    PaymentResult,
    PolicyResult,
    ShipmentResult,
)
from .trace import TraceWriter


def build_output(
    case_id: str,
    entity_result: EntityResult,
    order_result: OrderResult,
    shipment_result: ShipmentResult,
    payment_result: PaymentResult,
    policy_result: PolicyResult,
    conflict_result: ConflictResult,
    calibrated_confidence: float,
    evidence_refs: list[str],
) -> dict[str, Any]:
    """Assemble final dictionary strictly conforming to day09-l3b-output-v2 schema."""
    # Format data conflicts
    formatted_conflicts = [
        {
            "field": c.field,
            "sources": c.sources,
            "selected_source": c.selected_source,
            "resolution_code": c.resolution_code,
        }
        for c in conflict_result.conflicts
    ][:5]

    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": policy_result.primary_issue,
            "secondary_issues": policy_result.secondary_issues[:10],
            "case_status": policy_result.case_status,
            "confidence": calibrated_confidence,
        },
        "affected_entities": {
            "order_ids": list(dict.fromkeys(entity_result.resolved_order_ids))[:20],
            "item_ids": list(dict.fromkeys(order_result.item_ids))[:20],
            "seller_ids": list(dict.fromkeys(order_result.seller_ids + shipment_result.late_seller_ids))[:20],
            "payment_references": list(dict.fromkeys(payment_result.payment_references))[:20],
            "shipment_ids": list(dict.fromkeys(shipment_result.shipment_ids))[:20],
        },
        "entity_resolution": {
            "status": entity_result.status,
            "resolved_order_ids": list(dict.fromkeys(entity_result.resolved_order_ids))[:20],
            "rejected_candidates": list(dict.fromkeys(entity_result.rejected_candidates))[:20],
            "confidence": entity_result.confidence,
        },
        "customer_context": {
            "customer_unique_id": entity_result.customer_unique_id,
            "related_order_ids": list(dict.fromkeys(entity_result.related_order_ids))[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_result.verdict,
            "late_seller_ids": list(dict.fromkeys(shipment_result.late_seller_ids))[:20],
            "timeline_complete": shipment_result.timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_result.verdict,
            "captured_total_brl": payment_result.captured_total_brl,
            "refunded_total_brl": payment_result.refunded_total_brl,
            "refundable_total_brl": payment_result.refundable_total_brl,
        },
        "root_cause_analysis": {
            "ranked_causes": policy_result.ranked_causes[:5],
            "responsible_parties": policy_result.responsible_parties[:5],
        },
        "evidence_refs": evidence_refs[:30],
        "data_conflicts": formatted_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": policy_result.recommended_refund_brl,
            "refund_lines": policy_result.refund_lines[:10],
        },
        "resolution_actions": list(dict.fromkeys(policy_result.resolution_actions))[:8],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for a single dispute case."""
    case_id = case["case_id"]
    scope = case.get("investigation_scope", {})
    include_product_context = scope.get("include_product_context", False)

    # 1. Coordinator assigns task to Entity Agent
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"step": "entity_resolution"},
    )
    entity_result = await run_entity_agent(case, gateway, trace)

    # 2. Coordinator assigns tasks to Specialists (Order, Shipment, Payment)
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        attributes={"step": "order_investigation"},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
        attributes={"step": "shipment_investigation"},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
        attributes={"step": "payment_reconciliation"},
    )

    # Run order agent first (or in parallel) to supply item/timestamps to shipment agent
    order_result = await run_order_agent(
        case_id=case_id,
        resolved_order_ids=entity_result.resolved_order_ids,
        include_product_context=include_product_context,
        gateway=gateway,
        trace=trace,
        cached_orders=entity_result.order_data,
    )

    shipment_task = run_shipment_agent(
        case_id=case_id,
        resolved_order_ids=entity_result.resolved_order_ids,
        order_result=order_result,
        gateway=gateway,
        trace=trace,
    )
    payment_task = run_payment_agent(
        case_id=case_id,
        resolved_order_ids=entity_result.resolved_order_ids,
        gateway=gateway,
        trace=trace,
    )

    shipment_result, payment_result = await asyncio.gather(shipment_task, payment_task)

    # 3. Handoff to Policy Agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        attributes={"step": "policy_evaluation"},
    )
    policy_result = await run_policy_agent(
        case=case,
        entity_result=entity_result,
        order_result=order_result,
        shipment_result=shipment_result,
        payment_result=payment_result,
        gateway=gateway,
        trace=trace,
    )

    # 4. Conflict Resolution
    conflict_result = resolve_conflicts(
        entity_result=entity_result,
        order_result=order_result,
        shipment_result=shipment_result,
        payment_result=payment_result,
    )

    # 5. Handoff to Verifier
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        attributes={"step": "verification_and_calibration"},
    )
    calibrated_confidence, final_evidence_refs = verify_and_calibrate(
        case=case,
        entity_result=entity_result,
        order_result=order_result,
        shipment_result=shipment_result,
        payment_result=payment_result,
        policy_result=policy_result,
        conflict_result=conflict_result,
        trace=trace,
    )

    # 6. Build final schema-compliant output
    return build_output(
        case_id=case_id,
        entity_result=entity_result,
        order_result=order_result,
        shipment_result=shipment_result,
        payment_result=payment_result,
        policy_result=policy_result,
        conflict_result=conflict_result,
        calibrated_confidence=calibrated_confidence,
        evidence_refs=final_evidence_refs,
    )
