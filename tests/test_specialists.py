import asyncio
from unittest.mock import AsyncMock
from pathlib import Path
import pytest

from student_agent.agents.order_agent import run_order_agent
from student_agent.agents.shipment_agent import run_shipment_agent
from student_agent.agents.payment_agent import run_payment_agent
from student_agent.models import OrderResult
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


@pytest.mark.asyncio
async def test_specialists_execution(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    gateway = AsyncMock()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs):
        if tool_name == "get_order_items":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_items_evidence_1234567890",
                "result_hash": f"sha256:{'a'*64}",
                "domain": "item",
                "data": [
                    {"order_item_id": "1", "seller_id": "seller_abc", "product_id": "prod_1", "shipping_limit_date": "2018-01-05T00:00:00Z"}
                ],
            }
        elif tool_name == "get_shipment_summary":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_shipment_evidence_12345678",
                "result_hash": f"sha256:{'b'*64}",
                "domain": "shipment",
                "data": {
                    "shipment_id": "ship_001",
                    "shipped_at": "2018-01-03T00:00:00Z",
                    "delivered_at": "2018-01-15T00:00:00Z",
                    "estimated_delivery": "2018-01-10T00:00:00Z",
                },
            }
        elif tool_name == "get_order_payments":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_payments_evidence_12345678",
                "result_hash": f"sha256:{'c'*64}",
                "domain": "payment",
                "data": [
                    {"payment_sequential": 1, "payment_value": 150.0, "payment_type": "credit_card"}
                ],
            }
        elif tool_name == "get_refund_timeline":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_refunds_evidence_123456789",
                "result_hash": f"sha256:{'d'*64}",
                "domain": "refund",
                "data": [],
            }
        raise NotImplementedError(f"Unexpected tool {tool_name}")

    gateway.call.side_effect = mock_call

    resolved_order_ids = ["order_123"]
    
    # 1. Run Order Agent
    order_res = await run_order_agent(
        case_id="L3B_CASE_001",
        resolved_order_ids=resolved_order_ids,
        include_product_context=False,
        gateway=gateway,
        trace=trace,
        cached_orders={"order_123": {"order_status": "delivered"}},
    )
    assert order_res.item_ids == ["1"]
    assert order_res.seller_ids == ["seller_abc"]
    assert len(order_res.evidence_refs) == 1

    # 2. Run Shipment Agent
    shipment_res = await run_shipment_agent(
        case_id="L3B_CASE_001",
        resolved_order_ids=resolved_order_ids,
        order_result=order_res,
        gateway=gateway,
        trace=trace,
    )
    # Delivered Jan 15 > Estimated Jan 10 with shipped on time -> logistics_delay
    assert shipment_res.verdict == "logistics_delay"
    assert shipment_res.shipment_ids == ["ship_001"]

    # 3. Run Payment Agent
    payment_res = await run_payment_agent(
        case_id="L3B_CASE_001",
        resolved_order_ids=resolved_order_ids,
        gateway=gateway,
        trace=trace,
    )
    assert payment_res.captured_total_brl == 150.0
    assert payment_res.refundable_total_brl == 150.0
    assert payment_res.verdict == "reconciled"
