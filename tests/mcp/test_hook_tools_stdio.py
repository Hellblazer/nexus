# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a hook_<name> tool is reachable over a REAL nx-mcp stdio
transport (RDR-215 bead nexus-q02nx.3 -- "one integration test driving a
real nx-mcp over stdio for one tool").

No hook module is ported yet (bead nexus-q02nx.4 is the first), so there is
no real hook to drive here. Rather than adding test-only fixture code to
the shipped ``nexus.mcp.hooks``/``nexus.mcp.core`` modules to make one
reachable, this spawns a tiny bootstrap script that runs the exact
production sequence -- ``nexus.mcp.core.mcp``, ``register_hook_tools``,
``nexus.mcp.core.main()`` (the same three names ``nx-mcp``'s own console
script entry point resolves to) -- and registers ONE trivial fixture
``HookToolSpec`` before calling ``main()``, exactly the one line a real
port adds to ``HOOK_TOOLS``. Nothing under ``src/nexus/`` carries any
test-only branch to make this possible.

Mirrors ``tests/test_mcp_server.py::test_mcp_server_round_trip``'s
stdio-client/env-forwarding shape (nexus-f4wcg) rather than reinventing it.
"""
from __future__ import annotations

import os
import sys
import textwrap
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_BOOTSTRAP = textwrap.dedent(
    """
    import json

    from nexus._hook_runtime._io import HookResult
    from nexus.mcp import core
    from nexus.mcp.hooks import HookToolSpec, register_hook_tools


    def _run(payload):
        return HookResult(stdout=json.dumps({"session_id": (payload or {}).get("session_id")}))


    register_hook_tools(
        core.mcp,
        (
            HookToolSpec(
                name="stdio_probe",
                run=_run,
                fields=("session_id",),
                summary="stdio-integration-test fixture, never a real hook",
            ),
        ),
    )
    core.main()
    """
)


async def test_hook_tool_reachable_over_real_stdio(t2_service_env, tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

    bootstrap = tmp_path / "hook_tool_bootstrap.py"
    bootstrap.write_text(_BOOTSTRAP)

    # Same env-forwarding shape as test_mcp_server_round_trip (nexus-f4wcg):
    # StdioServerParameters(env=...) does NOT inherit os.environ, so the
    # child must be handed everything it needs explicitly, including what
    # t2_service_env just set on THIS process (NX_STORAGE_BACKEND,
    # NX_SERVICE_URL, NX_SERVICE_TOKEN, NX_LOCAL).
    env = get_default_environment()
    env.update(
        {k: v for k, v in os.environ.items() if k.startswith(("NX_", "NEXUS_", "VOYAGE_"))}
    )
    # Drop any inherited T1 session/lease so the child MINTS its own against
    # the pinned endpoint above, rather than presenting a token/lease this
    # test process's own session holds (same reasoning as nexus-f4wcg's
    # sibling test).
    for t1_var in ("NX_T1_SESSION", "NX_T1_SESSION_ID", "NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED"):
        env.pop(t1_var, None)
    env["NX_SESSION_ID"] = str(uuid4())
    env["NEXUS_CONFIG_DIR"] = str(tmp_path / "config")

    server_params = StdioServerParameters(command=sys.executable, args=[str(bootstrap)], env=env)
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            tool = next((t for t in tools if t.name == "hook_stdio_probe"), None)
            assert tool is not None, [t.name for t in tools]
            assert tool.description is not None
            assert tool.description.startswith("Hook entry point (RDR-215):")

            result = await session.call_tool("hook_stdio_probe", {"session_id": "abc123"})
            assert result.isError is False
            assert len(result.content) == 1
            assert result.content[0].text == '{"session_id": "abc123"}'
