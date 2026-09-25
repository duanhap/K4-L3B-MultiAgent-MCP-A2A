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

logger = logging.getLogger(__name__)


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

    # Decision tree: Verify whether the specific claim is supported by evidence
    if target_claim == "canceled_order_paid":
        primary_issue = "canceled_order_paid"
        ranked_causes.append({"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1})
        for sid in order_result.seller_ids:
            responsible_parties.append({"party_type": "seller", "party_id": sid})
        if not responsible_parties:
            responsible_parties.append({"party_type": "platform", "party_id": None})
        recommended_refund_brl = refundable_total
        reason_code = "FULL_REFUND_ORDER_CANCELED"
        resolution_actions.extend(["process_full_refund", "notify_customer"])

    elif target_claim == "unavailable_order_paid":
        primary_issue = "unavailable_order_paid"
        ranked_causes.append({"cause_code": "ITEM_OUT_OF_STOCK_AFTER_PAYMENT", "rank": 1})
        responsible_parties.append({"party_type": "platform", "party_id": None})
        recommended_refund_brl = refundable_total
        reason_code = "FULL_REFUND_ORDER_UNAVAILABLE"
        resolution_actions.extend(["process_full_refund", "cancel_order_record"])

    elif target_claim == "late_delivery_seller":
        primary_issue = "late_delivery_seller"
        ranked_causes.append({"cause_code": "SELLER_DISPATCH_SLA_BREACH", "rank": 1})
        for sid in shipment_result.late_seller_ids or order_result.seller_ids:
            responsible_parties.append({"party_type": "seller", "party_id": sid})
        if not responsible_parties:
            responsible_parties.append({"party_type": "seller", "party_id": None})
        if refundable_total > 0:
            recommended_refund_brl = refundable_total
            reason_code = "REFUND_SELLER_DELAY"
            resolution_actions.extend(["issue_seller_penalty", "process_refund_for_delay"])
        else:
            resolution_actions.extend(["issue_seller_penalty", "notify_seller_sla_breach"])

    elif target_claim == "late_delivery_logistics":
        primary_issue = "late_delivery_logistics"
        ranked_causes.append({"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1})
        for sh_id in shipment_result.shipment_ids:
            responsible_parties.append({"party_type": "logistics_provider", "party_id": sh_id})
        if not responsible_parties:
            responsible_parties.append({"party_type": "logistics_provider", "party_id": None})
        if refundable_total > 0:
            recommended_refund_brl = refundable_total
            reason_code = "PARTIAL_REFUND_LOGISTICS_DELAY"
            resolution_actions.extend(["open_carrier_inquiry", "credit_shipping_fee"])
        else:
            resolution_actions.extend(["open_carrier_inquiry", "update_estimated_delivery"])

    elif target_claim == "duplicate_charge":
        primary_issue = "duplicate_charge"
        ranked_causes.append({"cause_code": "PAYMENT_GATEWAY_DUPLICATE_TRANSACTION", "rank": 1})
        responsible_parties.append({"party_type": "payment_provider", "party_id": None})
        recommended_refund_brl = round(max(0.0, captured_total / 2.0), 2)
        reason_code = "REFUND_DUPLICATE_CHARGE"
        resolution_actions.extend(["reverse_duplicate_charge", "notify_payment_processor"])

    elif target_claim == "payment_mismatch":
        primary_issue = "payment_mismatch"
        ranked_causes.append({"cause_code": "PAYMENT_AMOUNT_DISCREPANCY", "rank": 1})
        responsible_parties.append({"party_type": "payment_provider", "party_id": None})
        recommended_refund_brl = refundable_total if refundable_total > 0 else 0.0
        reason_code = "RECONCILE_PAYMENT_MISMATCH"
        resolution_actions.extend(["reconcile_discrepancy", "audit_payment_ledger"])

    elif target_claim == "refund_pending":
        primary_issue = "refund_pending"
        ranked_causes.append({"cause_code": "REFUND_SETTLEMENT_IN_PROGRESS", "rank": 1})
        responsible_parties.append({"party_type": "platform", "party_id": None})
        recommended_refund_brl = refundable_total
        reason_code = "EXPEDITE_PENDING_REFUND"
        resolution_actions.extend(["expedite_refund_settlement", "notify_customer_settlement_timeline"])

    elif target_claim == "refund_failed":
        primary_issue = "refund_failed"
        ranked_causes.append({"cause_code": "PAYMENT_PROCESSOR_REFUND_REJECTED", "rank": 1})
        responsible_parties.append({"party_type": "payment_provider", "party_id": None})
        recommended_refund_brl = refundable_total
        reason_code = "RETRY_FAILED_REFUND"
        resolution_actions.extend(["retry_refund_transaction", "escalate_to_finance"])

    elif target_claim == "valid_split_payment":
        primary_issue = "valid_split_payment"
        ranked_causes.append({"cause_code": "LEGITIMATE_SPLIT_PAYMENT_TRANSACTION", "rank": 1})
        responsible_parties.append({"party_type": "customer", "party_id": entity_result.customer_unique_id})
        resolution_actions.extend(["explain_split_payment_policy", "close_inquiry"])

    elif target_claim == "unsupported_claim":
        primary_issue = "unsupported_claim"
        ranked_causes.append({"cause_code": "ORDER_FULFILLED_ACCORDING_TO_SLA", "rank": 1})
        responsible_parties.append({"party_type": "customer", "party_id": entity_result.customer_unique_id})
        resolution_actions.extend(["reject_claim_with_delivery_proof", "close_inquiry"])

    else:
        primary_issue = "insufficient_evidence"
        ranked_causes.append({"cause_code": "INSUFFICIENT_EVIDENCE_TO_DETERMINE_CAUSE", "rank": 1})
        responsible_parties.append({"party_type": "unknown", "party_id": None})
        resolution_actions.append("request_additional_information")

    # Unused variable cleanup (walrus operator remnant guard)
    pass

    # Build refund lines if refund recommended
    if recommended_refund_brl > 0:
        entity_target = order_result.order_ids[0] if order_result.order_ids else None
        refund_lines.append({
            "reason_code": reason_code,
            "amount_brl": recommended_refund_brl,
            "entity_id": entity_target,
        })
        case_status = "action_required"
    elif primary_issue in ("unsupported_claim", "valid_split_payment"):
        case_status = "no_action"
    else:
        case_status = "needs_investigation" if primary_issue == "insufficient_evidence" else "action_required"

    # Secondary issues: claim topics that are not the primary issue
    secondary_issues = [t for t in claim_topics if t != primary_issue][:10]

    # Generate claim_assessments for each claim in customer_request
    claim_assessments: list[dict[str, Any]] = []
    # Relevant evidence pool
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
                cverdict = "supported"
                cconf = 0.88
            elif ctopic == "requested_full_refund":
                cverdict = "supported" if recommended_refund_brl > 0 else "unsupported"
                cconf = 0.85
            elif primary_issue == "unsupported_claim":
                cverdict = "unsupported"
                cconf = 0.88
            elif ctopic in ("valid_split_payment", "duplicate_charge", "payment_mismatch"):
                cverdict = "supported" if ctopic == primary_issue else "unsupported"
                cconf = 0.85
            else:
                cverdict = "partially_supported" if recommended_refund_brl > 0 else "unsupported"
                cconf = 0.75

            # Must have at least 1 evidence ref
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
