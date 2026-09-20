# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a hook_<name> tool is reachable over a REAL nx-mcp stdio
transport (RDR-215 bead nexus-q02nx.3 -- "one integration test driving a
real nx-mcp over stdio for one tool").

TWO tests live here and the difference between them is the point.

``test_hook_tool_reachable_over_real_stdio`` drives a synthetic
``HookToolSpec`` whose payload the test fully controls, which is what
makes it a clean proof of the REGISTRATION AND TRANSPORT path,
independent of any hook's behaviour. It was written at bead
nexus-q02nx.3, when no hook module was ported yet and there was no real
hook to drive.

That premise died at bead nexus-q02nx.4 and this file did not notice for
the rest of the epic. Twelve real ``hook_*`` tools now ship and none was
driven here, while the RDR's Test Plan line -- "one stdio integration
test drives a real ``nx-mcp`` for ONE HOOK TOOL" -- read as satisfied
because a file of the right shape existed. The check's text still
matched the promise; its SUBJECT was never swapped from placeholder to
production. Caught at bead nexus-q02nx.31 by comparing this file's last
commit against the range it was supposed to cover, and recorded as the
fourteenth instance in T2 ``nexus_rdr/215-gates-that-lost-their-domain-tally``.

``test_a_real_shipped_hook_tool_is_reachable_over_stdio`` is the answer:
it registers the REAL ``HOOK_TOOLS`` tuple -- the exact call production
makes, not a spec built here that resembles one -- and asserts bytes
that could only have come from ``nexus.hooks.auto_approve.run``.

Rather than adding test-only fixture code to
the shipped ``nexus.mcp.hooks``/``nexus.mcp.core`` modules to make one
reachable, the first test spawns a tiny bootstrap script that runs the exact
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

#: The production sequence with NOTHING synthetic in it. Registering a
#: hand-built spec that merely resembles a real one would reproduce the
#: very gap this test exists to close, one level down: it would prove a
#: spec written here is reachable, not that the shipped ones are.
_REAL_BOOTSTRAP = textwrap.dedent(
    """
    from nexus.mcp import core
    from nexus.mcp.hooks import HOOK_TOOLS, register_hook_tools

    register_hook_tools(core.mcp, HOOK_TOOLS)
    core.main()
    """
)


def _server_params(tmp_path, source: str, name: str):
    """A stdio server running *source*, with this process's NX_ env forwarded."""
    from mcp.client.stdio import StdioServerParameters, get_default_environment

    bootstrap = tmp_path / name
    bootstrap.write_text(source)

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

    return StdioServerParameters(command=sys.executable, args=[str(bootstrap)], env=env)


async def test_hook_tool_reachable_over_real_stdio(t2_service_env, tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    server_params = _server_params(tmp_path, _BOOTSTRAP, "hook_tool_bootstrap.py")
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


async def test_a_real_shipped_hook_tool_is_reachable_over_stdio(t2_service_env, tmp_path):
    """A tool from the REAL ``HOOK_TOOLS`` answers over a real transport.

    The test above proves the mechanism with a spec written here. This one
    proves the shipped registry, which is what the RDR's Test Plan asks
    for and what nothing asserted for the whole epic (bead
    nexus-q02nx.31).

    ``hook_auto_approve`` is the subject because its ``run()`` is a pure
    allowlist membership test followed by an envelope render -- no T1, no
    T2, no ``bd``, no subprocess, no clock -- so the response is
    deterministic and can be asserted BYTE FOR BYTE. That matters: a test
    that booted ``nx-mcp``, called a real tool and asserted only that
    something came back would satisfy the Test Plan's sentence exactly as
    hollowly as the fixture did. These bytes can only have come from
    ``nexus.hooks.auto_approve`` through ``register_hook_tools`` and back
    over stdio.
    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from nexus.hooks.auto_approve import _ALLOWED_TOOLS
    from nexus.mcp.hooks import HOOK_TOOLS

    # Read the allowlist rather than hardcoding a member of it, so this
    # test fails loudly if the allowlist is emptied instead of quietly
    # asserting the silent non-match envelope.
    assert _ALLOWED_TOOLS, "the auto-approve allowlist is empty; nothing to drive"
    allowed = sorted(_ALLOWED_TOOLS)[0]

    server_params = _server_params(tmp_path, _REAL_BOOTSTRAP, "real_hook_bootstrap.py")
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            names = {t.name for t in (await session.list_tools()).tools}

            # THE COUNT THAT WAS ZERO. Instance 14 in the tally was that no
            # real hook tool was exposed over stdio by any test; asserting
            # the whole shipped set is present is the check that would have
            # caught it, rather than a check for the one tool this test
            # happens to call.
            expected = {f"hook_{spec.name}" for spec in HOOK_TOOLS}
            assert expected, "HOOK_TOOLS is empty; this test would prove nothing"
            missing = expected - names
            assert not missing, (
                f"{len(missing)} shipped hook tool(s) never reached the wire: "
                f"{sorted(missing)}. Registered over stdio: {sorted(names)}"
            )

            result = await session.call_tool(
                "hook_auto_approve",
                {"tool_name": allowed, "hook_event_name": "PreToolUse"},
            )
            assert result.isError is False
            assert len(result.content) == 1
            assert result.content[0].text == (
                '{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
                '"permissionDecision": "allow", '
                '"permissionDecisionReason": "plugin allowlist (auto-approve)"}}'
            )
