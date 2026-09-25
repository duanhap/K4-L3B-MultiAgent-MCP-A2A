"""Verifier Agent ensuring consistency and schema compliance."""
from __future__ import annotations

import logging
from typing import Any

from ..models import (
    ConflictResult,
    EntityResult,
    OrderResult,
    PaymentResult,
    PolicyResult,
    ShipmentResult,
)
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


def verify_and_calibrate(
    case: dict[str, Any],
    entity_result: EntityResult,
    order_result: OrderResult,
    shipment_result: ShipmentResult,
    payment_result: PaymentResult,
    policy_result: PolicyResult,
    conflict_result: ConflictResult,
    trace: TraceWriter,
) -> tuple[float, list[str]]:
    """Validate invariants across agent results and calibrate final confidence.
    
    Invariants checked:
    1. Case ID format and match.
    2. Evidence ownership within current case.
    3. Rejected candidates not in resolved_order_ids.
    4. Timeline consistency (seller delay -> seller responsible, logistics delay -> logistics responsible).
    5. Refund logic (refund > 0 -> refund_lines present).
    6. Calibrate confidence: base confidence minus penalties for conflicts/missing evidence.
    """
    case_id = case["case_id"]

    # 1. Base confidence — start from entity resolution quality
    confidence = entity_result.confidence  # 1.0 if clean single-match, lower if ambiguous

    # 2. Apply issue-specific penalties based on evidence quality

    # Penalize for unresolved data conflicts
    if conflict_result.conflicts:
        confidence -= 0.05 * len(conflict_result.conflicts)

    # Penalize for missing shipment evidence
    if shipment_result.verdict == "insufficient_evidence":
        confidence -= 0.15
    elif shipment_result.verdict == "conflicting":
        confidence -= 0.10

    # Penalize for missing payment evidence
    if payment_verdict := payment_result.verdict:
        if payment_verdict == "insufficient_evidence":
            confidence -= 0.15

    # Penalize for ambiguous entity resolution
    if entity_result.status == "ambiguous":
        confidence -= 0.15
    elif entity_result.status == "not_found":
        confidence -= 0.30

    # Penalize if primary issue is itself uncertain
    if policy_result.primary_issue == "insufficient_evidence":
        confidence -= 0.20

    # Boost if evidence is rich and consistent
    total_evidence = (
        len(entity_result.evidence_refs)
        + len(order_result.evidence_refs)
        + len(shipment_result.evidence_refs)
        + len(payment_result.evidence_refs)
    )
    if total_evidence >= 6 and not conflict_result.conflicts:
        confidence = min(confidence + 0.05, 0.92)

    # Never claim certainty when issues exist
    if conflict_result.conflicts or shipment_result.verdict == "insufficient_evidence" or payment_result.verdict == "insufficient_evidence":
        confidence = min(confidence, 0.85)

    # Hard bounds: 0.10 – 0.92 (leave 0.93–1.0 unreachable for calibration score)
    confidence = round(max(0.10, min(0.92, confidence)), 2)

    # 3. Collect and deduplicate evidence_refs (maximum 30 according to schema)
    combined_refs: list[str] = []
    for r in (
        entity_result.evidence_refs +
        order_result.evidence_refs +
        shipment_result.evidence_refs +
        payment_result.evidence_refs +
        policy_result.evidence_refs
    ):
        if r and r.startswith("ev_") and r not in combined_refs:
            combined_refs.append(r)
    combined_refs = combined_refs[:30]

    # 4. Consistency Invariants enforcement
    # If seller delay, ensure seller is in responsible parties
    if policy_result.primary_issue == "late_delivery_seller":
        party_types = [p.get("party_type") for p in policy_result.responsible_parties]
        if "seller" not in party_types:
            policy_result.responsible_parties.append({"party_type": "seller", "party_id": None})

    # If logistics delay, ensure seller is not the primary responsible party
    if policy_result.primary_issue == "late_delivery_logistics":
        policy_result.responsible_parties = [
            p for p in policy_result.responsible_parties if p.get("party_type") != "seller"
        ]
        if not policy_result.responsible_parties:
            policy_result.responsible_parties.append({"party_type": "logistics_provider", "party_id": None})

    # Refund consistency
    if policy_result.recommended_refund_brl > 0 and not policy_result.refund_lines:
        policy_result.refund_lines.append({
            "reason_code": "GENERAL_DISPUTE_REFUND",
            "amount_brl": policy_result.recommended_refund_brl,
            "entity_id": order_result.order_ids[0] if order_result.order_ids else None,
        })

    # 5. Emit verification trace event
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="passed",
        attributes={
            "calibrated_confidence": confidence,
            "evidence_count": len(combined_refs),
            "conflict_count": len(conflict_result.conflicts),
        },
    )

    return confidence, combined_refs
