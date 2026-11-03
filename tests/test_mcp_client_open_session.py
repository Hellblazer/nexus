# SPDX-License-Identifier: AGPL-3.0-or-later
"""``open_session`` against a real streamable-HTTP MCP server (nexus-sgmyl).

mcp 2.x changed three things this client touches, and none showed in a unit test
that fakes the session: the HTTP client the transport takes is built on the SDK's
own ``httpx2`` (an ``httpx.Timeout`` is not accepted), ``streamable_http_client``
yields a pair of streams instead of a triple, and a ``CallToolResult``'s
attributes are snake_case (``structured_content``, ``is_error``). This drives the
real transport and the real result type end to end.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator

import pytest

from nexus.mcp_client import core

_SERVER = textwrap.dedent(
    """
    import sys
    from mcp.server.mcpserver import MCPServer

    m = MCPServer("open-session-probe")

    @m.tool()
    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    @m.tool()
    def boom() -> str:
        raise ValueError("expected failure")

    m.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
    """
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_url() -> Iterator[str]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"the probe server exited early with {proc.returncode}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the probe server never listened")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_a_call_through_open_session_returns_the_structured_payload(server_url: str) -> None:
    endpoint = core.MCPEndpoint(url=server_url, headers={"X-Probe": "1"}, timeout_s=15.0)
    async with core.open_session(endpoint) as session:
        assert await core.call_tool(session, "add", {"a": 2, "b": 3}) == {"sum": 5}


@pytest.mark.asyncio
async def test_a_failing_tool_is_none_because_the_real_result_says_is_error(server_url: str) -> None:
    """The fail-soft contract on the SDK's own result type: a camelCase read of
    ``isError`` would see ``False`` here and hand back the error text as a payload."""
    endpoint = core.MCPEndpoint(url=server_url, timeout_s=15.0)
    async with core.open_session(endpoint) as session:
        assert await core.call_tool(session, "boom", {}) is None
