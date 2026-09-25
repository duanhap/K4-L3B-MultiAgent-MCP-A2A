"""Conflict Resolver detecting and resolving data discrepancies."""
from __future__ import annotations

from typing import Any

from ..models import (
    ConflictResult,
    DataConflict,
    EntityResult,
    OrderResult,
    PaymentResult,
    ShipmentResult,
)


def resolve_conflicts(
    entity_result: EntityResult,
    order_result: OrderResult,
    shipment_result: ShipmentResult,
    payment_result: PaymentResult,
) -> ConflictResult:
    """Compare fields across specialist results and identify data conflicts.
    
    Resolution precedence:
    - Shipment date conflict: prefer_shipment_record
    - Payment amount conflict: prefer_payment_record
    - Order status conflict: prefer_order_record
    """
    conflicts: list[DataConflict] = []

    # 1. Compare delivered_at / shipment dates between Order data and Shipment data
    for order_id in order_result.order_ids:
        order_delivered = order_result.delivered_at.get(order_id)
        raw_shipment = shipment_result.raw_data.get(order_id, {})
        shipment_delivered = raw_shipment.get("delivered_at") if isinstance(raw_shipment, dict) else None

        if order_delivered and shipment_delivered and order_delivered != shipment_delivered:
            conflicts.append(DataConflict(
                field="delivered_at",
                sources=["order_record", "shipment_record"],
                selected_source="shipment_record",
                resolution_code="prefer_shipment_record",
            ))

        # 2. Compare status between Order and Shipment
        order_status = order_result.order_status.get(order_id)
        shipment_status = raw_shipment.get("status") if isinstance(raw_shipment, dict) else None
        if order_status and shipment_status and order_status.lower() != shipment_status.lower():
            conflicts.append(DataConflict(
                field="order_status",
                sources=["order_record", "shipment_record"],
                selected_source="order_record",
                resolution_code="prefer_order_record",
            ))

    # 3. Check payment capture consistency
    if payment_result.verdict == "capture_mismatch":
        conflicts.append(DataConflict(
            field="payment_value",
            sources=["order_total", "payment_capture"],
            selected_source="payment_capture",
            resolution_code="prefer_payment_record",
        ))

    # Schema allows max 5 conflicts
    return ConflictResult(conflicts=conflicts[:5])
