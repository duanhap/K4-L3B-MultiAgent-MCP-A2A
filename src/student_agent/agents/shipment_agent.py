"""Shipment and delivery timeline specialist agent."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import OrderResult, ShipmentResult
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        clean = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None


async def run_shipment_agent(
    case_id: str,
    resolved_order_ids: list[str],
    order_result: OrderResult,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> ShipmentResult:
    """Analyze shipments, shipping limits, and delivery timestamps.
    
    Verdicts:
    - on_time: delivered_at <= estimated_delivery
    - seller_delay: shipped_at > shipping_limit_date or delayed dispatch
    - logistics_delay: seller shipped on time, but delivered_at > estimated_delivery
    - lost: not delivered and well past estimated delivery / status canceled/unavailable
    - returned: shipment marked returned
    - conflicting: mâu thuẫn status hoặc timestamp
    - insufficient_evidence: không đủ dữ liệu kết luận
    """
    if not resolved_order_ids:
        return ShipmentResult(verdict="insufficient_evidence")

    shipment_ids: list[str] = []
    late_seller_ids: list[str] = []
    evidence_refs: list[str] = []
    raw_data: dict[str, Any] = {}
    
    seller_delay_detected = False
    logistics_delay_detected = False
    on_time_detected = False
    lost_detected = False
    returned_detected = False
    timeline_complete = True

    for order_id in resolved_order_ids:
        shipment_info = None
        try:
            res = await gateway.call("get_shipment_summary", case_id=case_id, order_id=order_id)
            ev_ref = res.get("evidence_ref")
            if ev_ref:
                evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="shipment-agent",
                    tool_name="get_shipment_summary",
                    evidence_refs=[ev_ref],
                    attributes={"order_id": order_id},
                )
            shipment_info = res.get("data")
            raw_data[order_id] = shipment_info
        except Exception as exc:
            logger.warning("Shipment agent failed get_shipment_summary for %s: %s", order_id, exc)

        # Parse timestamps from shipment_info or fallback to order_result
        s_data = shipment_info if isinstance(shipment_info, dict) else {}
        
        # Check shipment_id
        sid = s_data.get("shipment_id") or s_data.get("tracking_number") or s_data.get("id")
        if sid:
            shipment_ids.append(str(sid))
            
        # Timestamps
        shipped_at = _parse_iso(s_data.get("shipped_at") or order_result.shipped_at.get(order_id))
        delivered_at = _parse_iso(s_data.get("delivered_at") or order_result.delivered_at.get(order_id))
        estimated_delivery = _parse_iso(s_data.get("estimated_delivery") or order_result.estimated_delivery.get(order_id))
        shipping_limit = _parse_iso(s_data.get("shipping_limit_date"))

        # Also inspect seller SLA from order items if available
        items = order_result.items_data.get(order_id, [])
        if isinstance(items, list):
            for itm in items:
                if isinstance(itm, dict):
                    itm_seller = itm.get("seller_id")
                    itm_limit = _parse_iso(itm.get("shipping_limit_date"))
                    if itm_limit and shipped_at and shipped_at > itm_limit:
                        seller_delay_detected = True
                        if itm_seller:
                            late_seller_ids.append(str(itm_seller))

        status_str = str(s_data.get("status") or order_result.order_status.get(order_id, "")).lower()
        if "return" in status_str:
            returned_detected = True

        # Check delays
        if shipping_limit and shipped_at and shipped_at > shipping_limit:
            seller_delay_detected = True
            seller_id = s_data.get("seller_id")
            if seller_id:
                late_seller_ids.append(str(seller_id))

        if delivered_at and estimated_delivery:
            if delivered_at > estimated_delivery:
                if seller_delay_detected:
                    pass  # already marked seller delay
                else:
                    logistics_delay_detected = True
            else:
                on_time_detected = True
        elif not delivered_at:
            timeline_complete = False
            if status_str in ("canceled", "unavailable"):
                lost_detected = True
            elif estimated_delivery and datetime.now(estimated_delivery.tzinfo) > estimated_delivery:
                lost_detected = True

    # Determine final verdict
    if returned_detected:
        verdict = "returned"
    elif seller_delay_detected:
        verdict = "seller_delay"
    elif logistics_delay_detected:
        verdict = "logistics_delay"
    elif lost_detected:
        verdict = "lost"
    elif on_time_detected:
        verdict = "on_time"
    elif not evidence_refs:
        verdict = "insufficient_evidence"
    else:
        verdict = "insufficient_evidence"

    return ShipmentResult(
        verdict=verdict,
        late_seller_ids=list(dict.fromkeys(late_seller_ids)),
        shipment_ids=list(dict.fromkeys(shipment_ids)),
        timeline_complete=timeline_complete,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        raw_data=raw_data,
    )
