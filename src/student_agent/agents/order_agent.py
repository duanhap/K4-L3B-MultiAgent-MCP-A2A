"""Order and Item specialist agent."""
from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import OrderResult
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


async def run_order_agent(
    case_id: str,
    resolved_order_ids: list[str],
    include_product_context: bool,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    cached_orders: dict[str, Any] | None = None,
) -> OrderResult:
    """Investigate orders, line items, and product details.
    
    Budget:
    - Reuse cached order data from entity_agent if available.
    - get_order_items for each resolved_order_id.
    - get_product_context for unique product_ids if include_product_context=True (capped to max 2 products to save budget).
    """
    cached_orders = cached_orders or {}
    item_ids: list[str] = []
    seller_ids: list[str] = []
    order_status: dict[str, str] = {}
    purchase_timestamp: dict[str, str | None] = {}
    approved_at: dict[str, str | None] = {}
    delivered_at: dict[str, str | None] = {}
    estimated_delivery: dict[str, str | None] = {}
    shipped_at: dict[str, str | None] = {}
    evidence_refs: list[str] = []
    items_data: dict[str, Any] = {}
    product_ids: list[str] = []

    for order_id in resolved_order_ids:
        # 1. Order details (cached or fetch)
        order = cached_orders.get(order_id)
        if not order:
            try:
                res = await gateway.call("get_order", case_id=case_id, order_id=order_id)
                order = res.get("data")
                ev_ref = res.get("evidence_ref")
                if ev_ref:
                    evidence_refs.append(ev_ref)
                    trace.emit(
                        case_id=case_id,
                        event_type="tool_result_consumed",
                        actor="order-agent",
                        tool_name="get_order",
                        evidence_refs=[ev_ref],
                        attributes={"order_id": order_id},
                    )
            except Exception as exc:
                logger.warning("Order agent failed get_order for %s: %s", order_id, exc)

        if isinstance(order, dict):
            order_status[order_id] = order.get("order_status") or order.get("status") or "unknown"
            purchase_timestamp[order_id] = order.get("order_purchase_timestamp") or order.get("purchase_timestamp")
            approved_at[order_id] = order.get("order_approved_at") or order.get("approved_at")
            delivered_at[order_id] = order.get("order_delivered_customer_date") or order.get("delivered_at")
            estimated_delivery[order_id] = order.get("order_estimated_delivery_date") or order.get("estimated_delivery")
            shipped_at[order_id] = order.get("order_delivered_carrier_date") or order.get("shipped_at")

        # 2. Order items
        try:
            items_res = await gateway.call("get_order_items", case_id=case_id, order_id=order_id)
            ev_ref = items_res.get("evidence_ref")
            if ev_ref:
                evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order-agent",
                    tool_name="get_order_items",
                    evidence_refs=[ev_ref],
                    attributes={"order_id": order_id},
                )
            raw_items = items_res.get("data", [])
            items_data[order_id] = raw_items
            
            item_list = raw_items if isinstance(raw_items, list) else [raw_items] if isinstance(raw_items, dict) else []
            for item in item_list:
                if isinstance(item, dict):
                    iid = item.get("order_item_id") or item.get("item_id")
                    if iid is not None:
                        item_ids.append(str(iid))
                    sid = item.get("seller_id")
                    if sid:
                        seller_ids.append(str(sid))
                    pid = item.get("product_id")
                    if pid:
                        product_ids.append(str(pid))
        except Exception as exc:
            logger.warning("Order agent failed get_order_items for %s: %s", order_id, exc)

    # 3. Product context if requested (get_product_context requires order_id)
    if include_product_context and resolved_order_ids:
        for oid in resolved_order_ids[:1]:
            try:
                prod_res = await gateway.call("get_product_context", case_id=case_id, order_id=oid)
                ev_ref = prod_res.get("evidence_ref")
                if ev_ref:
                    evidence_refs.append(ev_ref)
                    trace.emit(
                        case_id=case_id,
                        event_type="tool_result_consumed",
                        actor="order-agent",
                        tool_name="get_product_context",
                        evidence_refs=[ev_ref],
                        attributes={"order_id": oid},
                    )
            except Exception as exc:
                logger.warning("Order agent failed get_product_context for %s: %s", oid, exc)

    return OrderResult(
        order_ids=list(resolved_order_ids),
        item_ids=list(dict.fromkeys(item_ids)),
        seller_ids=list(dict.fromkeys(seller_ids)),
        order_status=order_status,
        purchase_timestamp=purchase_timestamp,
        approved_at=approved_at,
        delivered_at=delivered_at,
        estimated_delivery=estimated_delivery,
        shipped_at=shipped_at,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        items_data=items_data,
    )
