"""Debug: check if server error message has more detail, and test different cases."""
import asyncio
from student_agent.config import Settings
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx2
import json


async def test_verbose():
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

        # Print ALL content blocks from error response
        res = await session.call_tool(
            "get_order",
            arguments={"case_id": "L3B_CASE_001", "order_id": "af0bbb47f125381ce9f3597dc70ef07b"},
        )
        print("=== get_order L3B_CASE_001 ===")
        print("is_error:", res.is_error)
        for i, blk in enumerate(res.content):
            print(f"  block[{i}] type={type(blk).__name__}")
            if hasattr(blk, "text"):
                print(f"  text: {blk.text}")
        sc = getattr(res, "structuredContent", None) or getattr(res, "structured_content", None)
        print("structuredContent:", sc)

        # Try get_sellers (different param schema)
        tools_resp = await session.list_tools()
        for t in tools_resp.tools:
            if t.name == "get_sellers":
                print("\nget_sellers input_schema:", json.dumps(t.input_schema, indent=2))
                break

        res2 = await session.call_tool(
            "get_sellers",
            arguments={"case_id": "L3B_CASE_001", "order_id": "af0bbb47f125381ce9f3597dc70ef07b"},
        )
        print("\n=== get_sellers ===")
        print("is_error:", res2.is_error)
        for blk in res2.content:
            if hasattr(blk, "text"):
                print("text:", blk.text[:300])


asyncio.run(test_verbose())
