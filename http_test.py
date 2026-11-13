"""Live MCP smoke test; run the HTTP server in another terminal first."""
import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from run import configure_console, load_settings


async def main() -> None:
    load_settings()
    host = os.environ["NOVEL_MEMORY_HOST"]
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    url = f"http://{host}:{os.environ['NOVEL_MEMORY_PORT']}/mcp"
    headers = {"Authorization": f"Bearer {os.environ['NOVEL_MEMORY_TOKEN']}"}
    async with (
        httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=None), trust_env=False) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        names = {tool.name for tool in listing.tools}
        assert names == {"list_memory_tags", "search_memory", "get_memory", "get_memories"}
        result = await session.call_tool("list_memory_tags", {})
        assert not result.isError
        data = json.loads(result.content[0].text)
        print("MCP HTTP OK; four read-only tools discovered.")
        print(f"Visible stages: {len(data['items'])}")
        if data["items"]:
            memory_id = data["items"][0]["memory_id"]
            readback = await session.call_tool("get_memory", {"memory_id": memory_id})
            assert not readback.isError
            print(f"Read OK: {memory_id}")


if __name__ == "__main__":
    configure_console()
    asyncio.run(main())
