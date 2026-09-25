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
            
        # Timestamps from get_shipment_summary
        shipped_at = _parse_iso(s_data.get("delivered_carrier_at") or s_data.get("shipped_at") or order_result.shipped_at.get(order_id))
        delivered_at = _parse_iso(s_data.get("delivered_customer_at") or s_data.get("delivered_at") or order_result.delivered_at.get(order_id))
        estimated_delivery = _parse_iso(s_data.get("estimated_delivery_at") or s_data.get("estimated_delivery") or order_result.estimated_delivery.get(order_id))

        # Check explicit events in shipment summary
        events = s_data.get("events", [])
        if isinstance(events, list):
            for ev in events:
                if isinstance(ev, dict):
                    etype = ev.get("event_type")
                    actor = ev.get("actor")
                    if etype == "delivered_late":
                        if actor == "logistics_provider":
                            logistics_delay_detected = True
                        elif actor == "seller":
                            seller_delay_detected = True

        # Check shipping_limits from shipment summary or order items
        shipping_limits = s_data.get("shipping_limits", [])
        if isinstance(shipping_limits, list):
            for lim in shipping_limits:
                if isinstance(lim, dict):
                    lim_date = _parse_iso(lim.get("shipping_limit_at"))
                    if lim_date and shipped_at and shipped_at > lim_date:
                        seller_delay_detected = True
                        sid_seller = lim.get("seller_id")
                        if sid_seller:
                            late_seller_ids.append(str(sid_seller))

        status_str = str(s_data.get("order_status") or s_data.get("status") or order_result.order_status.get(order_id, "")).lower()
        if "return" in status_str:
            returned_detected = True

        if delivered_at and estimated_delivery:
            if delivered_at > estimated_delivery:
                if not seller_delay_detected:
                    logistics_delay_detected = True
            else:
                on_time_detected = True
        elif not delivered_at:
            timeline_complete = False
            if status_str in ("canceled", "unavailable"):
                lost_detected = True

    # Determine final verdict
    if returned_detected:
        verdict = "returned"
    elif logistics_delay_detected and not seller_delay_detected:
        verdict = "logistics_delay"
    elif seller_delay_detected:
        verdict = "seller_delay"
    elif lost_detected:
        verdict = "lost"
    elif on_time_detected:
        verdict = "on_time"
    elif not evidence_refs:
        verdict = "insufficient_evidence"
    else:
        verdict = "on_time"

    return ShipmentResult(
        verdict=verdict,
        late_seller_ids=list(dict.fromkeys(late_seller_ids)),
        shipment_ids=list(dict.fromkeys(shipment_ids)),
        timeline_complete=timeline_complete,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        raw_data=raw_data,
    )
