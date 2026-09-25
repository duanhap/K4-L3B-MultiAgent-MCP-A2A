"""Entity and customer resolution agent."""
from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import EntityResult
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


async def run_entity_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> EntityResult:
    """Resolve candidate order IDs and fetch customer context.
    
    Logic:
    1. Iterate over candidate_order_ids.
    2. Call get_order for each candidate.
    3. Categorize into resolved_order_ids or rejected_candidates based on response.
    4. Match against claimed_order_id and calculate resolution confidence.
    5. If include_customer_history is requested, call get_customer_history.
    6. Emit tool_result_consumed trace events for each valid MCP call.
    """
    case_id = case["case_id"]
    candidate_order_ids: list[str] = case.get("candidate_order_ids", [])
    customer_request = case.get("customer_request", {})
    claimed_order_id = customer_request.get("claimed_order_id")
    scope = case.get("investigation_scope", {})
    include_customer_history = scope.get("include_customer_history", False)
    customer_unique_id_hint = case.get("customer_unique_id_hint")

    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []
    order_data: dict[str, Any] = {}
    evidence_refs: list[str] = []
    found_customer_unique_id: str | None = customer_unique_id_hint

    # 1. Candidate loop
    for candidate in candidate_order_ids:
        try:
            order_res = await gateway.call("get_order", case_id=case_id, order_id=candidate)
            data = order_res.get("data")
            ev_ref = order_res.get("evidence_ref")

            if ev_ref:
                evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="entity-agent",
                    tool_name="get_order",
                    evidence_refs=[ev_ref],
                    attributes={"order_id": candidate, "status": "resolved"},
                )

            # Check if order data is valid and not empty / error indicator
            if data and isinstance(data, dict) and data.get("order_id"):
                resolved_order_ids.append(candidate)
                order_data[candidate] = data
                # Extract customer_unique_id or customer_id if present
                if not found_customer_unique_id:
                    found_customer_unique_id = data.get("customer_unique_id") or data.get("customer_id")
            elif data and isinstance(data, list) and len(data) > 0:
                resolved_order_ids.append(candidate)
                order_data[candidate] = data[0] if isinstance(data[0], dict) else data
            else:
                rejected_candidates.append(candidate)
        except Exception as exc:
            # Failed to fetch order -> candidate is rejected
            logger.warning("Order candidate %s rejected: %s", candidate, exc)
            rejected_candidates.append(candidate)

    # 2. Determine resolution status & confidence
    if len(resolved_order_ids) == 1:
        status = "resolved"
        if claimed_order_id and claimed_order_id in resolved_order_ids:
            confidence = 1.0
        else:
            confidence = 0.85
    elif len(resolved_order_ids) > 1:
        status = "ambiguous"
        # If one matches claimed_order_id directly, bias towards higher confidence
        if claimed_order_id and claimed_order_id in resolved_order_ids:
            confidence = 0.7
        else:
            confidence = 0.4
    else:
        status = "not_found"
        confidence = 0.0

    # 3. Customer History if requested
    related_order_ids: list[str] = []
    if include_customer_history and (found_customer_unique_id or customer_unique_id_hint):
        cust_id = found_customer_unique_id or customer_unique_id_hint
        try:
            cust_res = await gateway.call(
                "get_customer_history",
                case_id=case_id,
                customer_unique_id=cust_id,
            )
            ev_ref = cust_res.get("evidence_ref")
            if ev_ref:
                evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="entity-agent",
                    tool_name="get_customer_history",
                    evidence_refs=[ev_ref],
                    attributes={"customer_unique_id": cust_id},
                )
            cust_data = cust_res.get("data")
            if isinstance(cust_data, dict):
                orders = cust_data.get("orders") or cust_data.get("order_ids") or []
                if isinstance(orders, list):
                    for o in orders:
                        oid = o.get("order_id") if isinstance(o, dict) else o
                        if oid and isinstance(oid, str):
                            related_order_ids.append(oid)
            elif isinstance(cust_data, list):
                for item in cust_data:
                    oid = item.get("order_id") if isinstance(item, dict) else item
                    if oid and isinstance(oid, str):
                        related_order_ids.append(oid)
        except Exception as exc:
            logger.warning("Failed to fetch customer history for %s: %s", cust_id, exc)

    # Ensure unique lists preserving order
    resolved_order_ids = list(dict.fromkeys(resolved_order_ids))
    rejected_candidates = list(dict.fromkeys(rejected_candidates))
    related_order_ids = list(dict.fromkeys(related_order_ids))
    evidence_refs = list(dict.fromkeys(evidence_refs))

    return EntityResult(
        status=status,
        resolved_order_ids=resolved_order_ids,
        rejected_candidates=rejected_candidates,
        customer_unique_id=found_customer_unique_id,
        related_order_ids=related_order_ids,
        confidence=confidence,
        order_data=order_data,
        evidence_refs=evidence_refs,
    )
