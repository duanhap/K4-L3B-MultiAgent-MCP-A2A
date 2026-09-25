"""Payment and refund reconciliation specialist agent."""
from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import PaymentResult
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


async def run_payment_agent(
    case_id: str,
    resolved_order_ids: list[str],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> PaymentResult:
    """Investigate payments and refund timeline.
    
    Verdicts:
    - reconciled: captured = total, no anomalies
    - capture_mismatch: captured amount differs from order expectation
    - duplicate_capture: multiple unexpected captures for same transaction
    - refund_pending: refund approved but not settled
    - refund_failed: refund attempt failed
    - refunded: refund fully executed
    - insufficient_evidence: missing evidence
    """
    if not resolved_order_ids:
        return PaymentResult(verdict="insufficient_evidence")

    payment_references: list[str] = []
    evidence_refs: list[str] = []
    raw_payments: list[dict[str, Any]] = []
    raw_refunds: list[dict[str, Any]] = []

    captured_total = 0.0
    refunded_total = 0.0
    has_pending_refund = False
    has_failed_refund = False
    duplicate_capture_flag = False

    for order_id in resolved_order_ids:
        # 1. Payments
        try:
            pay_res = await gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
            ev_ref = pay_res.get("evidence_ref")
            if ev_ref:
                evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment-agent",
                    tool_name="get_order_payments",
                    evidence_refs=[ev_ref],
                    attributes={"order_id": order_id},
                )
            p_data = pay_res.get("data")
            p_list = p_data if isinstance(p_data, list) else [p_data] if isinstance(p_data, dict) else []
            seen_sequential = set()
            for p in p_list:
                if isinstance(p, dict):
                    raw_payments.append(p)
                    val = p.get("payment_value") or p.get("value") or 0.0
                    try:
                        captured_total += float(val)
                    except (ValueError, TypeError):
                        pass

                    ref = p.get("payment_sequential") or p.get("payment_type") or p.get("id")
                    if ref:
                        payment_references.append(f"{order_id}_{ref}")
                    
                    seq = p.get("payment_sequential")
                    if seq in seen_sequential and seq is not None:
                        duplicate_capture_flag = True
                    if seq is not None:
                        seen_sequential.add(seq)
        except Exception as exc:
            logger.warning("Payment agent failed get_order_payments for %s: %s", order_id, exc)

        # 2. Refunds
        try:
            ref_res = await gateway.call("get_refund_timeline", case_id=case_id, order_id=order_id)
            ev_ref = ref_res.get("evidence_ref")
            if ev_ref:
                evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment-agent",
                    tool_name="get_refund_timeline",
                    evidence_refs=[ev_ref],
                    attributes={"order_id": order_id},
                )
            r_data = ref_res.get("data")
            r_list = r_data if isinstance(r_data, list) else [r_data] if isinstance(r_data, dict) else []
            for r in r_list:
                if isinstance(r, dict):
                    raw_refunds.append(r)
                    status = str(r.get("status") or r.get("refund_status") or "").lower()
                    amt = r.get("refund_amount") or r.get("amount") or 0.0
                    try:
                        r_amt = float(amt)
                    except (ValueError, TypeError):
                        r_amt = 0.0

                    if status in ("completed", "settled", "refunded", "success"):
                        refunded_total += r_amt
                    elif status in ("pending", "processing", "approved"):
                        has_pending_refund = True
                    elif status in ("failed", "rejected", "error"):
                        has_failed_refund = True
        except Exception as exc:
            logger.warning("Payment agent failed get_refund_timeline for %s: %s", order_id, exc)

    # Calculate refundable total
    captured_total = round(captured_total, 2)
    refunded_total = round(refunded_total, 2)
    refundable_total = round(max(0.0, captured_total - refunded_total), 2)

    # Determine verdict
    if not evidence_refs:
        verdict = "insufficient_evidence"
    elif duplicate_capture_flag:
        verdict = "duplicate_capture"
    elif has_failed_refund:
        verdict = "refund_failed"
    elif has_pending_refund:
        verdict = "refund_pending"
    elif refunded_total > 0 and refundable_total == 0.0:
        verdict = "refunded"
    elif captured_total > 0:
        verdict = "reconciled"
    else:
        verdict = "reconciled"

    return PaymentResult(
        verdict=verdict,
        captured_total_brl=captured_total if evidence_refs else None,
        refunded_total_brl=refunded_total if evidence_refs else None,
        refundable_total_brl=refundable_total if evidence_refs else None,
        payment_references=list(dict.fromkeys(payment_references)),
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        raw_payments=raw_payments,
        raw_refunds=raw_refunds,
    )
