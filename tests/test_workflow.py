import asyncio
from unittest.mock import AsyncMock
from pathlib import Path

from student_agent.workflow import solve_case
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


def test_solve_case_end_to_end(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    gateway = AsyncMock()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs):
        if tool_name == "get_order":
            order_id = kwargs.get("order_id")
            if order_id == "af0bbb47f125381ce9f3597dc70ef07b":
                return {
                    "schema_version": "day09-mcp-evidence-v1",
                    "evidence_ref": "ev_order_mock_0000000000001",
                    "result_hash": f"sha256:{'a'*64}",
                    "domain": "order",
                    "data": {
                        "order_id": order_id,
                        "customer_unique_id": "cust-001",
                        "order_status": "delivered",
                    },
                }
            raise RuntimeError("Order not found")
        elif tool_name == "get_customer_history":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_customer_mock_0000000002",
                "result_hash": f"sha256:{'b'*64}",
                "domain": "customer",
                "data": {"orders": [{"order_id": "af0bbb47f125381ce9f3597dc70ef07b"}]},
            }
        elif tool_name == "get_order_items":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_items_mock_0000000000003",
                "result_hash": f"sha256:{'c'*64}",
                "domain": "item",
                "data": [{"order_item_id": "1", "seller_id": "seller_1", "product_id": "prod_1"}],
            }
        elif tool_name == "get_product_context":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_product_mock_0000000004",
                "result_hash": f"sha256:{'d'*64}",
                "domain": "product",
                "data": {"product_id": kwargs.get("product_id"), "product_category_name": "perfumaria"},
            }
        elif tool_name == "get_shipment_summary":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_shipment_mock_000000005",
                "result_hash": f"sha256:{'e'*64}",
                "domain": "shipment",
                "data": {
                    "shipment_id": "ship_001",
                    "shipped_at": "2018-01-02T10:00:00Z",
                    "delivered_at": "2018-01-18T10:00:00Z",
                    "estimated_delivery": "2018-01-12T10:00:00Z",
                },
            }
        elif tool_name == "get_order_payments":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_payment_mock_0000000006",
                "result_hash": f"sha256:{'f'*64}",
                "domain": "payment",
                "data": [{"payment_sequential": 1, "payment_value": 85.50, "payment_type": "credit_card"}],
            }
        elif tool_name == "get_refund_timeline":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_refund_mock_00000000007",
                "result_hash": f"sha256:{'0'*64}",
                "domain": "refund",
                "data": [],
            }
        elif tool_name == "get_policy":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_policy_mock_00000000008",
                "result_hash": f"sha256:{'1'*64}",
                "domain": "policy",
                "data": {"version": "v2"},
            }
        raise NotImplementedError(f"Unexpected tool: {tool_name}")

    gateway.call.side_effect = mock_call

    case = {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "af0bbb47f125381ce9f3597dc70ef07b",
            "claims": [
                {"claim_id": "claim-001-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": [
            "af0bbb47f125381ce9f3597dc70ef07b",
            "candidate-001",
        ],
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
        "customer_unique_id_hint": "customer-597dc70ef07b",
    }

    # Simulate CLI trace start
    trace.emit(case_id="L3B_CASE_001", event_type="case_received", actor="coordinator")

    output = asyncio.run(solve_case(case, gateway, trace))

    # Validate output schema
    contracts.validate_output(output, "outputs/L3B_CASE_001.json")
    assert output["schema_version"] == "day09-l3b-output-v2"
    assert output["case_id"] == "L3B_CASE_001"
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["entity_resolution"]["resolved_order_ids"] == ["af0bbb47f125381ce9f3597dc70ef07b"]
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-001"]
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["financial_resolution"]["currency"] == "BRL"
    assert len(output["evidence_refs"]) > 0

    trace.emit(case_id="L3B_CASE_001", event_type="case_finalized", actor="coordinator")

    # Verify trace file events
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 8
