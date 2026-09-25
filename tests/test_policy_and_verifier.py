import asyncio
from unittest.mock import AsyncMock
from pathlib import Path
import pytest

from student_agent.agents.policy_agent import run_policy_agent
from student_agent.agents.conflict_resolver import resolve_conflicts
from student_agent.agents.verifier import verify_and_calibrate
from student_agent.models import (
    EntityResult,
    OrderResult,
    ShipmentResult,
    PaymentResult,
)
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


@pytest.mark.asyncio
async def test_policy_conflict_and_verifier(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    gateway = AsyncMock()

    case = {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "claims": [{"claim_id": "c1", "topic": "late_delivery_seller"}, {"claim_id": "c2", "topic": "refund"}]
        }
    }

    entity_res = EntityResult(
        status="resolved",
        resolved_order_ids=["order_001"],
        confidence=1.0,
        evidence_refs=["ev_entity_evidence_000000000001"]
    )

    order_res = OrderResult(
        order_ids=["order_001"],
        item_ids=["item_1"],
        seller_ids=["seller_1"],
        order_status={"order_001": "delivered"},
        delivered_at={"order_001": "2018-01-20T00:00:00Z"},
        evidence_refs=["ev_order_evidence_000000000002"]
    )

    shipment_res = ShipmentResult(
        verdict="seller_delay",
        late_seller_ids=["seller_1"],
        shipment_ids=["ship_1"],
        raw_data={"order_001": {"delivered_at": "2018-01-22T00:00:00Z"}},
        evidence_refs=["ev_shipment_evidence_000000003"]
    )

    payment_res = PaymentResult(
        verdict="reconciled",
        captured_total_brl=200.0,
        refundable_total_brl=200.0,
        evidence_refs=["ev_payment_evidence_000000004"]
    )

    # 1. Policy Agent
    policy_res = await run_policy_agent(
        case=case,
        entity_result=entity_res,
        order_result=order_res,
        shipment_result=shipment_res,
        payment_result=payment_res,
        gateway=gateway,
        trace=trace,
    )
    assert policy_res.primary_issue == "late_delivery_seller"
    assert policy_res.recommended_refund_brl == 200.0
    assert len(policy_res.refund_lines) == 1

    # 2. Conflict Resolver
    conflict_res = resolve_conflicts(entity_res, order_res, shipment_res, payment_res)
    assert len(conflict_res.conflicts) == 1
    assert conflict_res.conflicts[0].field == "delivered_at"
    assert conflict_res.conflicts[0].resolution_code == "prefer_shipment_record"

    # 3. Verifier
    confidence, all_refs = verify_and_calibrate(
        case=case,
        entity_result=entity_res,
        order_result=order_res,
        shipment_result=shipment_res,
        payment_result=payment_res,
        policy_result=policy_res,
        conflict_result=conflict_res,
        trace=trace,
    )
    assert confidence < 1.0  # due to conflict penalty
    assert len(all_refs) == 4
    for ref in all_refs:
        assert ref.startswith("ev_")
