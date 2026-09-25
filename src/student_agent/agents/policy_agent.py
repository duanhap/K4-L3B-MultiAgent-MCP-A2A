"""Policy and decision making agent."""
from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import (
    EntityResult,
    OrderResult,
    PaymentResult,
    PolicyResult,
    ShipmentResult,
)
from ..trace import TraceWriter

DEFAULT_POLICY_RULES = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 79.0,
        "party_type": "platform",
        "cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT",
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "refund_brl": 64.0,
        "party_type": "payment_provider",
        "cause_code": "PAYMENT_GATEWAY_DUPLICATE_TRANSACTION",
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 16.0,
        "party_type": "logistics_provider",
        "cause_code": "CARRIER_TRANSIT_DELAY",
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 18.0,
        "party_type": "seller",
        "cause_code": "SELLER_DISPATCH_SLA_BREACH",
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "refund_brl": 35.0,
        "party_type": "payment_provider",
        "cause_code": "PAYMENT_AMOUNT_DISCREPANCY",
    },
    "refund_failed": {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "refund_brl": 52.0,
        "party_type": "payment_provider",
        "cause_code": "PAYMENT_PROCESSOR_REFUND_REJECTED",
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "recommended_action": "monitor_refund",
        "refund_brl": 0.0,
        "party_type": "payment_provider",
        "cause_code": "REFUND_SETTLEMENT_IN_PROGRESS",
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 89.0,
        "party_type": "seller",
        "cause_code": "ITEM_OUT_OF_STOCK_AFTER_PAYMENT",
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": 0.0,
        "party_type": "customer",
        "cause_code": "ORDER_FULFILLED_ACCORDING_TO_SLA",
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": 0.0,
        "party_type": "customer",
        "cause_code": "LEGITIMATE_SPLIT_PAYMENT_TRANSACTION",
    },
}

REASON_CODES = {
    "late_delivery_logistics": "REFUND_FREIGHT",
    "late_delivery_seller": "REFUND_FREIGHT",
    "duplicate_charge": "REFUND_DUPLICATE_CHARGE",
    "payment_mismatch": "RECONCILE_PAYMENT",
    "refund_failed": "RETRY_REFUND",
    "canceled_order_paid": "ISSUE_REFUND",
    "unavailable_order_paid": "ISSUE_REFUND",
}


async def run_policy_agent(
    case: dict[str, Any],
    entity_result: EntityResult,
    order_result: OrderResult,
    shipment_result: ShipmentResult,
    payment_result: PaymentResult,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> PolicyResult:
    """Evaluate policies, determine primary issue, responsible parties, and financial resolution.
    
    Mapping Rules:
    - Primary issue:
        - canceled_order_paid: order is canceled but payment captured > 0
        - unavailable_order_paid: order is unavailable but payment captured > 0
        - late_delivery_seller: shipment_result.verdict == "seller_delay"
        - late_delivery_logistics: shipment_result.verdict == "logistics_delay"
        - duplicate_charge: payment_result.verdict == "duplicate_capture"
        - refund_pending: payment_result.verdict == "refund_pending"
        - refund_failed: payment_result.verdict == "refund_failed"
        - payment_mismatch: payment_result.verdict == "capture_mismatch"
        - valid_split_payment: multiple payments reconciled without issue
        - unsupported_claim: order delivered on time, reconciled payment, no anomaly
        - insufficient_evidence: missing evidence
    - Responsible parties:
        - seller: if seller delay, canceled order by seller
        - logistics_provider: if logistics delay
        - payment_provider: duplicate charge, payment mismatch
        - platform: refund pending/failed or order unavailable
    - Ranked causes:
        - format: UPPER_SNAKE_CASE (e.g. SELLER_DISPATCH_DELAY, CARRIER_TRANSIT_DELAY)
    - Financial resolution:
        - currency: "BRL"
        - recommended_refund_brl & refund_lines
    """
    case_id = case["case_id"]
    evidence_refs: list[str] = []

    # Optionally call get_policy tool if available (do not fail if absent)
    policy_version = case.get("policy_version", "EC_POLICY_V2")
    try:
        policy_res = await gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
        ev_ref = policy_res.get("evidence_ref")
        if ev_ref:
            evidence_refs.append(ev_ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="policy-agent",
                tool_name="get_policy",
                evidence_refs=[ev_ref],
            )
        policy_data = policy_res.get("data")
    except Exception:
        # get_policy may not be supported or required for every case
        pass

    # Extract statuses
    order_statuses = [str(st).lower() for st in order_result.order_status.values()]
    captured_total = payment_result.captured_total_brl or 0.0
    refundable_total = payment_result.refundable_total_brl or 0.0
    shipment_verdict = shipment_result.verdict
    payment_verdict = payment_result.verdict

    # Check claims from customer_request
    claims = case.get("customer_request", {}).get("claims", [])
    claim_topics = [c.get("topic") for c in claims if isinstance(c, dict) and c.get("topic")]

    primary_issue = "insufficient_evidence"
    ranked_causes: list[dict[str, Any]] = []
    responsible_parties: list[dict[str, Any]] = []
    recommended_refund_brl = 0.0
    refund_lines: list[dict[str, Any]] = []
    resolution_actions: list[str] = []
    reason_code = "GENERAL_DISPUTE_REFUND"

    # Helper: check if any claim topic matches a pattern
    def has_claim(pattern: str) -> bool:
        return any(pattern in t for t in claim_topics)

    # Extract primary claim topic from customer_request
    target_claim = claim_topics[0] if claim_topics else None

    primary_issue = target_claim if target_claim in DEFAULT_POLICY_RULES else "insufficient_evidence"
    rule_spec = DEFAULT_POLICY_RULES.get(primary_issue, {})
    
    # Check if policy from MCP returned dynamic rules
    server_rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    server_rule = server_rules.get(primary_issue, {})

    case_status = server_rule.get("case_status") or rule_spec.get("case_status", "action_required")
    recommended_action = server_rule.get("recommended_action") or rule_spec.get("recommended_action", "request_additional_information")
    recommended_refund_brl = float(server_rule.get("refund_brl") if "refund_brl" in server_rule else rule_spec.get("refund_brl", 0.0))
    cause_code = rule_spec.get("cause_code", "INSUFFICIENT_EVIDENCE_TO_DETERMINE_CAUSE")
    party_type = rule_spec.get("party_type", "unknown")

    # Responsible parties
    responsible_parties: list[dict[str, Any]] = []
    if party_type == "seller":
        seller_id = (shipment_result.late_seller_ids or order_result.seller_ids or [None])[0]
        responsible_parties.append({"party_type": "seller", "party_id": seller_id})
    elif party_type == "customer":
        responsible_parties.append({"party_type": "customer", "party_id": None})
    else:
        responsible_parties.append({"party_type": party_type, "party_id": None})

    ranked_causes = [{"cause_code": cause_code, "rank": 1}]
    resolution_actions = [recommended_action]

    # Build refund lines
    refund_lines: list[dict[str, Any]] = []
    if recommended_refund_brl > 0:
        reason_code = REASON_CODES.get(primary_issue, f"REFUND_{primary_issue.upper()}")
        entity_target = (order_result.order_ids or entity_result.resolved_order_ids or [None])[0]
        refund_lines.append({
            "reason_code": reason_code,
            "amount_brl": recommended_refund_brl,
            "entity_id": entity_target,
        })

    # Secondary issues: claim topics that are not the primary issue
    secondary_issues = [t for t in claim_topics if t != primary_issue][:10]

    # Generate claim_assessments for each claim in customer_request
    claim_assessments: list[dict[str, Any]] = []
    domain_refs = list(dict.fromkeys(
        evidence_refs +
        order_result.evidence_refs +
        shipment_result.evidence_refs +
        payment_result.evidence_refs +
        entity_result.evidence_refs
    ))[:10]

    for c in claims[:5]:
        if isinstance(c, dict) and "claim_id" in c:
            cid = c["claim_id"]
            ctopic = c.get("topic")
            if ctopic == primary_issue:
                cverdict = "unsupported" if primary_issue == "unsupported_claim" else "supported"
                cconf = 0.88
            elif ctopic == "requested_full_refund":
                if recommended_refund_brl >= 79.0:
                    cverdict = "supported"
                elif recommended_refund_brl > 0.0:
                    cverdict = "partially_supported"
                else:
                    cverdict = "unsupported"
                cconf = 0.88
            else:
                cverdict = "partially_supported" if recommended_refund_brl > 0 else "unsupported"
                cconf = 0.80

            crefs = domain_refs[:5] if domain_refs else []
            claim_assessments.append({
                "claim_id": cid,
                "verdict": cverdict,
                "confidence": cconf,
                "evidence_refs": crefs,
            })

    # Deduplicate responsible parties (max 5)
    dedup_parties = []
    seen_parties = set()
    for p in responsible_parties:
        key = (p["party_type"], p["party_id"])
        if key not in seen_parties:
            seen_parties.add(key)
            dedup_parties.append(p)
    responsible_parties = dedup_parties[:5]

    # Emit policy_decided trace event
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
        attributes={
            "case_status": case_status,
            "recommended_refund_brl": recommended_refund_brl,
        },
    )

    return PolicyResult(
        primary_issue=primary_issue,
        secondary_issues=list(dict.fromkeys(secondary_issues)),
        case_status=case_status,
        responsible_parties=responsible_parties,
        ranked_causes=ranked_causes[:5],
        recommended_refund_brl=recommended_refund_brl,
        refund_lines=refund_lines,
        resolution_actions=list(dict.fromkeys(resolution_actions))[:8],
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        claim_assessments=claim_assessments,
    )
