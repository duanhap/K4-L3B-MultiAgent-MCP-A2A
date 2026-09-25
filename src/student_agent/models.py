"""Internal state models for the L3B multi-agent workflow.

These dataclasses represent the typed output of each specialist agent.
They are INTERNAL — never serialised directly to output JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EntityResult:
    """Output of the Entity/Customer agent."""
    status: str  # "resolved" | "ambiguous" | "not_found"
    resolved_order_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[str] = field(default_factory=list)
    customer_unique_id: str | None = None
    related_order_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    # raw order data keyed by order_id for downstream reuse
    order_data: dict[str, Any] = field(default_factory=dict)
    # evidence refs collected during resolution
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class OrderResult:
    """Output of the Order/Item agent."""
    order_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    # keyed by order_id
    order_status: dict[str, str] = field(default_factory=dict)
    # ISO timestamps keyed by order_id
    purchase_timestamp: dict[str, str | None] = field(default_factory=dict)
    approved_at: dict[str, str | None] = field(default_factory=dict)
    delivered_at: dict[str, str | None] = field(default_factory=dict)
    estimated_delivery: dict[str, str | None] = field(default_factory=dict)
    shipped_at: dict[str, str | None] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    # raw items data
    items_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ShipmentResult:
    """Output of the Shipment agent."""
    verdict: str = "insufficient_evidence"
    # "on_time"|"seller_delay"|"logistics_delay"|"lost"|"returned"|"conflicting"|"insufficient_evidence"
    late_seller_ids: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)
    timeline_complete: bool = False
    evidence_refs: list[str] = field(default_factory=list)
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentResult:
    """Output of the Payment/Refund agent."""
    verdict: str = "insufficient_evidence"
    # "reconciled"|"capture_mismatch"|"duplicate_capture"|"refund_pending"|
    # "refund_failed"|"refunded"|"insufficient_evidence"
    captured_total_brl: float | None = None
    refunded_total_brl: float | None = None
    refundable_total_brl: float | None = None
    payment_references: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    raw_payments: list[dict[str, Any]] = field(default_factory=list)
    raw_refunds: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PolicyResult:
    """Output of the Policy agent."""
    primary_issue: str = "insufficient_evidence"
    secondary_issues: list[str] = field(default_factory=list)
    case_status: str = "needs_investigation"
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)
    ranked_causes: list[dict[str, Any]] = field(default_factory=list)
    recommended_refund_brl: float = 0.0
    refund_lines: list[dict[str, Any]] = field(default_factory=list)
    resolution_actions: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class DataConflict:
    """A detected conflict between two data sources."""
    field: str
    sources: list[str]
    selected_source: str | None
    resolution_code: str


@dataclass
class ConflictResult:
    """Output of the Conflict Resolver."""
    conflicts: list[DataConflict] = field(default_factory=list)
