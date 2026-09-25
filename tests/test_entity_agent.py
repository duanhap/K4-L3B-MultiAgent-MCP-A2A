import asyncio
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
import pytest

from student_agent.agents.entity_agent import run_entity_agent
from student_agent.models import EntityResult
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


@pytest.mark.asyncio
async def test_entity_agent_candidate_rejection_and_resolution(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    # Mock gateway
    gateway = AsyncMock()

    # Simulate get_order: valid for af0bbb..., fail for candidate-001
    async def mock_call(tool_name: str, *, case_id: str, **kwargs):
        if tool_name == "get_order":
            order_id = kwargs.get("order_id")
            if order_id == "af0bbb47f125381ce9f3597dc70ef07b":
                return {
                    "schema_version": "day09-mcp-evidence-v1",
                    "evidence_ref": "ev_0123456789abcdef0123456789",
                    "result_hash": f"sha256:{'a'*64}",
                    "domain": "order",
                    "data": {
                        "order_id": order_id,
                        "customer_unique_id": "cust-001",
                        "order_status": "delivered",
                    },
                }
            else:
                raise RuntimeError(f"Order {order_id} not found")
        elif tool_name == "get_customer_history":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_customer_hist_evidence_12345",
                "result_hash": f"sha256:{'b'*64}",
                "domain": "customer",
                "data": {
                    "customer_unique_id": kwargs.get("customer_unique_id"),
                    "orders": [{"order_id": "af0bbb47f125381ce9f3597dc70ef07b"}, {"order_id": "past_order_999"}],
                },
            }
        raise NotImplementedError(f"Unexpected tool {tool_name}")

    gateway.call.side_effect = mock_call

    case = {
        "case_id": "L3B_CASE_001",
        "candidate_order_ids": [
            "af0bbb47f125381ce9f3597dc70ef07b",
            "candidate-001",
        ],
        "customer_request": {
            "claimed_order_id": "af0bbb47f125381ce9f3597dc70ef07b",
        },
        "investigation_scope": {
            "include_customer_history": True,
        },
        "customer_unique_id_hint": "customer-597dc70ef07b",
    }

    result = await run_entity_agent(case, gateway, trace)

    assert result.status == "resolved"
    assert result.resolved_order_ids == ["af0bbb47f125381ce9f3597dc70ef07b"]
    assert result.rejected_candidates == ["candidate-001"]
    assert result.confidence == 1.0
    assert "past_order_999" in result.related_order_ids
    assert len(result.evidence_refs) == 2
    assert "ev_0123456789abcdef0123456789" in result.evidence_refs
    assert "ev_customer_hist_evidence_12345" in result.evidence_refs
