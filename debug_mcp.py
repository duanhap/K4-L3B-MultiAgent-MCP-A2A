"""Debug script to test MCP tool calls directly."""
import asyncio
from student_agent.config import Settings
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx2
import json


async def test_all_tools():
    root = Path(".")
    settings = Settings.load(root)
    headers = {"Authorization": f"Bearer {settings.team_api_key}"}
    timeout = httpx2.Timeout(60.0, connect=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http,
        streamable_http_client(settings.mcp_endpoint, http_client=http) as (r, w),
        ClientSession(r, w) as session,
    ):
        await session.initialize()
        test_order_id = "af0bbb47f125381ce9f3597dc70ef07b"
        case_id = "L3B_CASE_001"
        tests = [
            ("get_order", {"case_id": case_id, "order_id": test_order_id}),
            ("get_order_items", {"case_id": case_id, "order_id": test_order_id}),
            ("get_order_payments", {"case_id": case_id, "order_id": test_order_id}),
            ("get_shipment_summary", {"case_id": case_id, "order_id": test_order_id}),
            ("get_refund_timeline", {"case_id": case_id, "order_id": test_order_id}),
            ("get_customer_history", {"case_id": case_id, "customer_unique_id": "customer-597dc70ef07b"}),
            ("get_policy", {"case_id": case_id, "policy_version": "EC_POLICY_V2"}),
        ]
        for tool_name, args in tests:
            try:
                res = await session.call_tool(tool_name, arguments=args)
                if res.is_error:
                    msg = res.content[0].text[:200] if res.content else "no content"
                    print(f"FAIL {tool_name}: {msg}")
                else:
                    # Try to parse as JSON to see structure
                    text = res.content[0].text if res.content else ""
                    try:
                        data = json.loads(text)
                        print(f"OK   {tool_name}: keys={list(data.keys())}, domain={data.get('domain')}")
                        inner = data.get("data")
                        if isinstance(inner, dict):
                            print(f"       data keys: {list(inner.keys())[:8]}")
                        elif isinstance(inner, list):
                            print(f"       data list len={len(inner)}, first_keys={list(inner[0].keys())[:8] if inner else []}")
                    except Exception:
                        print(f"OK   {tool_name}: raw={text[:200]}")
            except Exception as e:
                print(f"EXCEPTION {tool_name}: {e}")


asyncio.run(test_all_tools())
