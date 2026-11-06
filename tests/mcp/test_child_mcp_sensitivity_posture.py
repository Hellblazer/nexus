# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 Step 2.5 (bead nexus-rfvyy.7.10): the posture follows a dispatched child into its nested nx MCP server.

``nx_enrich_beads`` and ``nx_plan_audit`` run ``claude -p`` with a tool grant that launches ``nx-mcp``. The child's
tools must never hand a sensitive chunk to the model, so:

1. ``_build_dispatch_env`` always exports ``NX_SENSITIVITY_POSTURE=llm_bound`` (every mode, whatever the parent had);
2. the nested ``nx-mcp`` reads the variable at startup (``main`` installs
   ``nexus.mcp._sensitivity_posture.install_sensitivity_posture_hook``) and runs every ``tools/call`` inside
   ``llm_bound``; the reads themselves already follow ``nexus.sensitivity.read_sensitivity``, which falls back to the
   variable, so a child that never entered a scope still excludes;
3. a server started without the variable behaves as before.

A5 (T2 nexus_rdr/243-research-11) says Claude Code hands the dispatch environment to the stdio server it launches.
That is a property of Claude Code, spiked once by hand. This file pins the two halves nexus owns: the export, and
the server's reading of it. It does not launch ``claude``.

The conftest sets ``NX_SENSITIVITY_POSTURE=llm_bound`` for every test, so each test here states the variable it means.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from typing import Any
from uuid import uuid4

import pytest

from nexus.sensitivity import POSTURE_ENV

# ── 1. the export ────────────────────────────────────────────────────────────


@pytest.fixture
def _no_t1_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.db.t1.mint_t1_session_token", lambda session_id, *, context: {"session_token": "own-minted-token"})


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="owned-default"),
        pytest.param({"ephemeral": True}, id="ephemeral"),
        pytest.param({"ephemeral": True, "grants_tool_access": True, "parent_session_id": "p"}, id="ephemeral-tool-grant"),
    ],
)
def test_dispatch_env_exports_llm_bound_in_every_mode(monkeypatch: pytest.MonkeyPatch, _no_t1_mint: None, kwargs: dict[str, Any]) -> None:
    """The parent had no posture variable: the export is deliberate, not inherited."""
    from nexus.operators.dispatch import _build_dispatch_env

    monkeypatch.delenv(POSTURE_ENV, raising=False)

    env = _build_dispatch_env(**kwargs)

    assert env[POSTURE_ENV] == "llm_bound"


def test_dispatch_env_overwrites_a_parent_value_that_is_not_llm_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.operators.dispatch import _build_dispatch_env

    monkeypatch.setenv(POSTURE_ENV, "default")

    assert _build_dispatch_env()[POSTURE_ENV] == "llm_bound"


# ── 2. the hook, in process ──────────────────────────────────────────────────


def _probe_server() -> Any:
    """A purpose-built MCPServer with a tool that reports what a tool body sees."""
    from mcp.server.mcpserver import MCPServer

    from nexus import sensitivity

    server = MCPServer("test-child-posture")

    @server.tool(description="posture probe")
    def probe(value: str) -> str:
        try:
            sensitivity.assert_llm_bound("probe")
            asserted = True
        except sensitivity.DispatchOutsideSensitivityScope:
            asserted = False
        return json.dumps({
            "posture": sensitivity.current_posture(), "surface": sensitivity.current_surface(),
            "read": sensitivity.read_sensitivity(), "asserted": asserted,
        })

    return server


def _probe(server: Any) -> dict[str, Any]:
    from tests._mcp_inproc import call_tool

    result = call_tool(server, "probe", {"value": "x"})
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.sensitivity_reads
def test_a_server_with_the_variable_runs_every_tool_call_inside_the_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scope is entered by the middleware, not just read off the environment: the variable is removed AFTER the
    hook was installed and the tool still sees the scope, its surface name, and an ``exclude`` read."""
    from nexus.mcp._sensitivity_posture import CHILD_SURFACE, install_sensitivity_posture_hook

    server = _probe_server()
    monkeypatch.setenv(POSTURE_ENV, "llm_bound")
    assert install_sensitivity_posture_hook(server) is True
    monkeypatch.delenv(POSTURE_ENV)

    assert _probe(server) == {"posture": "llm_bound", "surface": CHILD_SURFACE, "read": "exclude", "asserted": True}


@pytest.mark.sensitivity_reads
def test_a_server_without_the_variable_installs_nothing_and_reads_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp._sensitivity_posture import install_sensitivity_posture_hook

    server = _probe_server()
    monkeypatch.delenv(POSTURE_ENV, raising=False)
    before = list(server.middleware)

    assert install_sensitivity_posture_hook(server) is False
    assert list(server.middleware) == before
    assert _probe(server) == {"posture": "default", "surface": None, "read": "include", "asserted": False}


@pytest.mark.parametrize("value", ["default", "", "LLM_BOUND", "1"])
def test_only_the_exact_value_llm_bound_installs_the_hook(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from nexus.mcp._sensitivity_posture import install_sensitivity_posture_hook

    server = _probe_server()
    monkeypatch.setenv(POSTURE_ENV, value)

    assert install_sensitivity_posture_hook(server) is False


def test_installing_twice_adds_one_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp._sensitivity_posture import install_sensitivity_posture_hook

    server = _probe_server()
    monkeypatch.setenv(POSTURE_ENV, "llm_bound")
    n = len(server.middleware)

    assert install_sensitivity_posture_hook(server) is True
    assert install_sensitivity_posture_hook(server) is True
    assert len(server.middleware) == n + 1


# ── 3. the real nx-mcp over stdio ────────────────────────────────────────────

#: The production sequence (``nexus.mcp.core.mcp`` then ``main()``) with one extra tool registered first, exactly as
#: ``tests/mcp/test_hook_tools_stdio.py`` does. ``main()`` is what must install the hook, so the probe sees the scope
#: only if the real startup wired it. The probe reads the environment variable AFTER startup, to show it is the
#: middleware, not the variable, that answers ``surface``.
_BOOTSTRAP = textwrap.dedent(
    """
    import json
    import os

    from nexus import sensitivity
    from nexus.mcp import core


    @core.mcp.tool(description="posture probe (test fixture)")
    def posture_probe() -> str:
        return json.dumps({
            "posture": sensitivity.current_posture(), "surface": sensitivity.current_surface(),
            "read": sensitivity.read_sensitivity(),
        })


    core.main()
    """
)

_CANARY = "rfvyy-canary-zephyr-quokka-lantern"
_ORDINARY = "rfvyy-ordinary-zephyr-quokka-lantern"
_BODY = "The zephyr quokka carries a lantern across the harbour at dusk, and the lantern keeps the ferry on course. "


def _child_params(tmp_path: Any, *, posture: str | None, source: str = _BOOTSTRAP):
    """A stdio ``nx-mcp`` whose environment is built here, so the posture variable is exactly *posture*."""
    from mcp.client.stdio import StdioServerParameters, get_default_environment

    bootstrap = tmp_path / "child_bootstrap.py"
    bootstrap.write_text(source)
    env = get_default_environment()
    env.update({k: v for k, v in os.environ.items() if k.startswith(("NX_", "NEXUS_", "VOYAGE_"))})
    for var in ("NX_T1_SESSION", "NX_T1_SESSION_ID", "NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED", POSTURE_ENV):
        env.pop(var, None)
    if posture is not None:
        env[POSTURE_ENV] = posture
    env["NX_SESSION_ID"] = str(uuid4())
    env["NEXUS_CONFIG_DIR"] = str(tmp_path / "config")
    return StdioServerParameters(command=sys.executable, args=[str(bootstrap)], env=env)


async def _call(params: Any, tool: str, arguments: dict[str, Any]) -> str:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            assert result.is_error is False, result.content
            return result.content[0].text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_real_nx_mcp_started_with_the_variable_runs_its_tools_in_the_scope(t2_service_env, tmp_path) -> None:
    probe = json.loads(await _call(_child_params(tmp_path, posture="llm_bound"), "posture_probe", {}))

    from nexus.mcp._sensitivity_posture import CHILD_SURFACE

    assert probe == {"posture": "llm_bound", "surface": CHILD_SURFACE, "read": "exclude"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_real_nx_mcp_started_without_the_variable_is_unscoped(t2_service_env, tmp_path) -> None:
    probe = json.loads(await _call(_child_params(tmp_path, posture=None), "posture_probe", {}))

    assert probe == {"posture": "default", "surface": None, "read": "include"}


def _seed(core: Any) -> None:
    """One sensitive and one ordinary note with near-identical text, through the real MCP write path."""
    for title, text, sensitive in ((_CANARY, _BODY + _CANARY, True), (_ORDINARY, _BODY + _ORDINARY, False)):
        out = str(core.store_put(content=text * 4, collection="rfvyy-child-posture", title=title, sensitive=sensitive))
        assert out.startswith("Stored"), out


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.sensitivity_reads
async def test_search_in_a_child_returns_no_sensitive_hit_and_still_returns_the_ordinary_one(t2_service_env, tmp_path) -> None:
    from nexus.mcp import core

    _seed(core)
    query = {"query": "zephyr quokka lantern harbour ferry", "corpus": "knowledge", "limit": 20}

    scoped = await _call(_child_params(tmp_path, posture="llm_bound"), "search", query)
    unscoped = await _call(_child_params(tmp_path, posture=None), "search", query)

    # Non-vacuity: the ordinary note comes back in both, and the unscoped server (today's behaviour) also returns the canary.
    assert _ORDINARY in scoped, scoped
    assert _CANARY not in scoped, scoped
    assert _ORDINARY in unscoped and _CANARY in unscoped, unscoped
