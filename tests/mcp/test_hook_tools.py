# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool-tier hook registration mechanism (RDR-215 bead nexus-q02nx.3).

No hook module is ported yet -- bead nexus-q02nx.4 is the first. Every test
here drives the registration MECHANISM (``nexus.mcp.hooks``) through
trivial in-test fixture specs, never a real hook, via the server's own
in-process dispatch (``FastMCP.call_tool`` / ``FastMCP.list_tools``) -- the
same path a real ``tools/call``/``tools/list`` request takes.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from nexus._hook_runtime._io import HookResult, permission_decision, stop_decision
from nexus.mcp.hooks import (
    HOOK_TOOLS,
    HookToolSpec,
    _NEVER_TOOL_TIER,
    flatten_field_name,
    nest_payload,
    register_hook_tools,
)


def _run(coro):
    return asyncio.run(coro)


def _fresh_mcp() -> FastMCP:
    return FastMCP("hook-tools-test")


# ── field flattening / nesting ────────────────────────────────────────────

class TestFlattenAndNest:
    def test_flatten_field_name_joins_dots_with_double_underscore(self):
        assert flatten_field_name("session_id") == "session_id"
        assert flatten_field_name("tool_input.command") == "tool_input__command"

    def test_nest_payload_reassembles_a_dotted_field(self):
        flat = {"session_id": "abc", "tool_input__command": "ls -la"}
        assert nest_payload(flat) == {
            "session_id": "abc",
            "tool_input": {"command": "ls -la"},
        }

    def test_nest_payload_drops_none_values(self):
        flat = {"session_id": "abc", "tool_input__command": None}
        assert nest_payload(flat) == {"session_id": "abc"}

    def test_nest_payload_of_all_none_is_empty_dict(self):
        assert nest_payload({"session_id": None}) == {}

    def test_nest_payload_merges_two_fields_under_the_same_parent(self):
        flat = {"tool_input__command": "ls", "tool_input__file_path": "/tmp/x"}
        assert nest_payload(flat) == {
            "tool_input": {"command": "ls", "file_path": "/tmp/x"},
        }


# ── registration + in-process dispatch ────────────────────────────────────

class TestRegisterHookTools:
    def test_registers_one_tool_named_hook_prefixed(self):
        mcp = _fresh_mcp()
        spec = HookToolSpec(
            name="probe",
            run=lambda payload: HookResult(stdout='{"ok": true}'),
            fields=("session_id",),
            summary="a trivial in-test fixture, never a real hook",
        )
        register_hook_tools(mcp, (spec,))

        tools = _run(mcp.list_tools())
        assert [t.name for t in tools] == ["hook_probe"]

    def test_registered_tool_name_and_description_shape(self):
        """Verification bullet 2: asserted, not eyeballed."""
        mcp = _fresh_mcp()
        specs = (
            HookToolSpec(name="probe_a", run=lambda p: HookResult(), summary="fixture A"),
            HookToolSpec(name="probe_b", run=lambda p: HookResult(), fields=("tool_input.command",), summary="fixture B"),
        )
        register_hook_tools(mcp, specs)

        tools = _run(mcp.list_tools())
        assert len(tools) == 2
        for tool in tools:
            assert tool.name.startswith("hook_")
            assert tool.description is not None
            assert tool.description.startswith("Hook entry point (RDR-215):")

    def test_input_schema_mirrors_flattened_payload_fields(self):
        mcp = _fresh_mcp()
        spec = HookToolSpec(
            name="probe",
            run=lambda p: HookResult(),
            fields=("session_id", "tool_input.command"),
        )
        register_hook_tools(mcp, (spec,))

        tools = _run(mcp.list_tools())
        properties = tools[0].inputSchema["properties"]
        assert set(properties) == {"session_id", "tool_input__command"}

    def test_dispatch_calls_run_with_the_nested_payload(self):
        received: list[dict | None] = []

        def _capture(payload):
            received.append(payload)
            return HookResult(stdout="{}")

        mcp = _fresh_mcp()
        spec = HookToolSpec(name="probe", run=_capture, fields=("session_id", "tool_input.command"))
        register_hook_tools(mcp, (spec,))

        _run(mcp.call_tool("hook_probe", {"session_id": "abc", "tool_input__command": "ls"}))

        assert received == [{"session_id": "abc", "tool_input": {"command": "ls"}}]

    def test_dispatch_returns_run_stdout_as_text_iserror_false(self):
        mcp = _fresh_mcp()
        spec = HookToolSpec(name="probe", run=lambda p: HookResult(stdout='{"decision": "allow"}'))
        register_hook_tools(mcp, (spec,))

        result = _run(mcp.call_tool("hook_probe", {}))
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert len(result.content) == 1
        assert result.content[0].text == '{"decision": "allow"}'

    def test_dispatch_of_a_silent_hook_returns_empty_text(self):
        """HookResult(stdout=None) -- the common silent-hook shape -- renders
        as an empty text block, not the literal string "None"."""
        mcp = _fresh_mcp()
        spec = HookToolSpec(name="probe", run=lambda p: HookResult())
        register_hook_tools(mcp, (spec,))

        result = _run(mcp.call_tool("hook_probe", {}))
        assert result.isError is False
        assert result.content[0].text == ""

    def test_a_raised_exception_is_swallowed_as_empty_text_iserror_false(self):
        """The tool boundary: a hook module bug never surfaces as
        isError=True -- the event proceeds exactly as an un-set_e bash
        script would let it."""

        def _boom(payload):
            raise RuntimeError("boom")

        mcp = _fresh_mcp()
        spec = HookToolSpec(name="probe", run=_boom)
        register_hook_tools(mcp, (spec,))

        result = _run(mcp.call_tool("hook_probe", {}))
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert result.content[0].text == ""

    def test_a_raised_exception_is_logged(self, monkeypatch):
        """Patches ``nexus._hook_runtime._io``'s own emitter rather than
        ``structlog.testing.capture_logs()`` -- this repo's
        ``configure_logging`` installs a level-filtering wrapper_class that
        ``capture_logs()`` does not override (see
        ``tests/test_upgrade_finish.py::test_restart_helper_emits_a_
        structured_log_line`` for the full explanation), so patching the
        emitter directly is the reliable pattern here.

        The target is ``_emit``, not a module-level ``_log``: ``_io`` no
        longer holds an ambient structlog logger, because an unconfigured one
        writes to stdout, which is the hook's decision channel. ``_emit``
        chooses the sink at call time and imports structlog only if it has one.
        """
        import nexus._hook_runtime._io as io_mod

        emitted = []
        monkeypatch.setattr(
            io_mod,
            "_emit",
            lambda level, event, **fields: emitted.append((level, event, fields)),
        )

        def _boom(payload):
            raise RuntimeError("boom")

        mcp = _fresh_mcp()
        spec = HookToolSpec(name="probe", run=_boom)
        register_hook_tools(mcp, (spec,))

        _run(mcp.call_tool("hook_probe", {}))

        assert emitted == [
            ("warning", "hook_boundary_swallowed_exception", {"hook": "hook_probe", "error": "boom"})
        ]

    def test_multiple_hooks_reach_only_their_own_run(self):
        calls: dict[str, int] = {"a": 0, "b": 0}

        def _run_a(payload):
            calls["a"] += 1
            return HookResult(stdout="A")

        def _run_b(payload):
            calls["b"] += 1
            return HookResult(stdout="B")

        mcp = _fresh_mcp()
        register_hook_tools(
            mcp,
            (
                HookToolSpec(name="a", run=_run_a),
                HookToolSpec(name="b", run=_run_b),
            ),
        )

        result_a = _run(mcp.call_tool("hook_a", {}))
        assert result_a.content[0].text == "A"
        assert calls == {"a": 1, "b": 0}

        result_b = _run(mcp.call_tool("hook_b", {}))
        assert result_b.content[0].text == "B"
        assert calls == {"a": 1, "b": 1}

    def test_default_specs_argument_reads_the_module_global_dynamically(self, monkeypatch):
        """register_hook_tools(mcp) with no explicit specs must read
        nexus.mcp.hooks.HOOK_TOOLS at CALL time, not at this function's
        definition time -- a plain mutable default would bind the empty
        tuple once and never see a later assignment, which is exactly the
        seam bead nexus-q02nx.4 (and this bead's own stdio integration
        test) plug a real/fixture spec in through."""
        import nexus.mcp.hooks as hooks_mod

        fixture = HookToolSpec(name="probe", run=lambda p: HookResult(stdout="X"))
        monkeypatch.setattr(hooks_mod, "HOOK_TOOLS", (fixture,))

        mcp = _fresh_mcp()
        hooks_mod.register_hook_tools(mcp)

        tools = _run(mcp.list_tools())
        assert [t.name for t in tools] == ["hook_probe"]


# ── phase_review_close_requires_gate carve-out ────────────────────────────

class TestPhaseReviewCloseNeverOnThisTier:
    def test_the_name_is_in_the_forbidden_set(self):
        assert "phase_review_close_requires_gate" in _NEVER_TOOL_TIER

    def test_registering_it_raises(self):
        mcp = _fresh_mcp()
        spec = HookToolSpec(name="phase_review_close_requires_gate", run=lambda p: HookResult())
        with pytest.raises(ValueError, match="fail_closed"):
            register_hook_tools(mcp, (spec,))

    def test_it_is_absent_from_the_live_registration_table(self):
        assert "phase_review_close_requires_gate" not in {spec.name for spec in HOOK_TOOLS}


# ── the live server registers exactly the ported hooks, no more ──────────

def test_the_live_nx_mcp_server_registers_exactly_the_ported_hook_tools():
    """HOOK_TOOLS carries one entry per ported hook module (bead
    nexus-q02nx.4 is the first: hook_auto_approve). nexus.mcp.core's
    unconditional registration call must add exactly those hook_<name>
    tools to the live server, no more and no fewer -- the tool-count/
    description-lint docs (docs/mcp-servers.md, this module's own module
    docstring) must track that count, which is why this test asserts the
    exact set rather than merely "not empty"."""
    from nexus.mcp.core import mcp as core_mcp
    from nexus.mcp.hooks import HOOK_TOOLS

    hook_names = {t.name for t in core_mcp._tool_manager.list_tools() if t.name.startswith("hook_")}
    assert hook_names == {f"hook_{spec.name}" for spec in HOOK_TOOLS}


# ── the deny path ─────────────────────────────────────────────────────────

class TestToolTierCarriesADeny:
    """The tool tier's DENY path, which neither MVV port exercises.

    ``hook_auto_approve`` (bead .4) only ever allows or stays silent, and
    ``session_start_verb`` (bead .5) emits no decision at all, so the two ports
    that exist prove the allow and silent paths and the fail-open-on-crash
    path — and nothing about a hook that deliberately refuses.

    That gap matters because bead nexus-q02nx.17 ports
    ``pre_close_verification_hook.sh``, an active allow|deny gate whose refusal
    text 19 files quote, onto this same mechanism. It would be the first hook
    to find out whether a deny survives the tool boundary intact. These tests
    establish it now, on the mechanism, rather than discovering it there.
    """

    def test_a_deny_envelope_reaches_the_caller_byte_for_byte(self):
        deny = permission_decision(
            "PreToolUse",
            "deny",
            permission_decision_reason="blocked by the gate",
            reason="blocked by the gate",
            system_message="run the gate first",
        )

        mcp = _fresh_mcp()
        register_hook_tools(mcp, (HookToolSpec(name="gate", run=lambda p: HookResult(stdout=deny)),))

        result = _run(mcp.call_tool("hook_gate", {}))

        # isError stays False: a deny is a DECISION the event carries, not a
        # tool failure. Signalling it as an error would make a refusal
        # indistinguishable from a crash, which on this tier means allow.
        assert result.isError is False
        assert result.content[0].text == deny

    def test_a_stop_tier_block_reaches_the_caller_byte_for_byte(self):
        """The other refusal shape: the top-level ``decision`` form the Stop
        and SubagentStop hooks use, which is not a hookSpecificOutput envelope.
        """
        block = stop_decision("block", reason="owes a report")

        mcp = _fresh_mcp()
        register_hook_tools(mcp, (HookToolSpec(name="stopper", run=lambda p: HookResult(stdout=block)),))

        result = _run(mcp.call_tool("hook_stopper", {}))

        assert result.isError is False
        assert result.content[0].text == block

    def test_a_deny_is_distinguishable_from_a_crash(self):
        """The property bead .17 actually depends on.

        A crash returns empty text, which the event reads as "no opinion" and
        therefore proceeds — the deliberate fail-open this tier is built on. A
        deny returns its envelope. If those two were ever the same bytes, a
        crashing gate would look exactly like a passing one.
        """
        deny = permission_decision("PreToolUse", "deny", reason="refused")

        def _boom(payload):
            raise RuntimeError("gate exploded")

        mcp = _fresh_mcp()
        register_hook_tools(
            mcp,
            (
                HookToolSpec(name="denier", run=lambda p: HookResult(stdout=deny)),
                HookToolSpec(name="crasher", run=_boom),
            ),
        )

        denied = _run(mcp.call_tool("hook_denier", {}))
        crashed = _run(mcp.call_tool("hook_crasher", {}))

        assert denied.content[0].text == deny
        assert crashed.content[0].text == ""
        assert denied.content[0].text != crashed.content[0].text


# ── non-scalar payload fields (bead nexus-9ifls) ──────────────────────────

class TestNonScalarPayloadFields:
    """A hook-tool field must accept what ``${path}`` substitution delivers.

    Bead nexus-q02nx.6 measured, against a real Claude Code 2.1.278, that
    substitution hands an ``mcp_tool`` hook REAL STRUCTURES rather than
    JSON-encoded strings: ``${tool_input}`` arrives as a ``dict`` and
    ``${background_tasks}`` as a ``list`` of ``dict``s carrying a mixed
    ``type: shell`` / ``type: subagent`` population with different key sets.

    These drive FastMCP's ACTUAL argument validation through the server's own
    in-process dispatch, so they fail against the ``str | None`` annotation
    the tier shipped with -- which is the point. Reverting
    ``_make_tool_function``'s annotation to ``str | None`` must turn both
    ``test_a_dict_...`` and ``test_a_list_...`` red; if it does not, they are
    not testing validation.
    """

    def test_a_dict_valued_field_reaches_run_intact(self):
        received: list[dict | None] = []
        mcp = _fresh_mcp()
        register_hook_tools(
            mcp,
            (
                HookToolSpec(
                    name="probe",
                    run=lambda p: (received.append(p), HookResult(stdout="{}"))[1],
                    fields=("tool_input",),
                ),
            ),
        )

        tool_input = {"command": "echo hi", "description": "Echo a test string"}
        _run(mcp.call_tool("hook_probe", {"tool_input": tool_input}))

        assert received == [{"tool_input": tool_input}]

    def test_a_list_valued_field_survives_a_mixed_population(self):
        """The shape nexus-q02nx.13 cross-checks: heterogeneous key sets.

        An EMPTY list would pass even a stringly schema's coercion in some
        pydantic configurations and proves nothing about structure, so this
        deliberately carries two entries whose keys DIFFER -- ``shell`` has
        ``command`` where ``subagent`` has ``agent_type`` -- exactly as
        measured from a real ``Stop`` event.
        """
        received: list[dict | None] = []
        mcp = _fresh_mcp()
        register_hook_tools(
            mcp,
            (
                HookToolSpec(
                    name="probe",
                    run=lambda p: (received.append(p), HookResult(stdout="{}"))[1],
                    fields=("background_tasks",),
                ),
            ),
        )

        background_tasks = [
            {
                "id": "bm72q9d6v",
                "type": "shell",
                "status": "running",
                "description": "Sleep for 45 seconds in background",
                "command": "sleep 45",
            },
            {
                "id": "a1ea45d8d324ca24a",
                "type": "subagent",
                "status": "running",
                "description": "Count to ten slowly",
                "agent_type": "general-purpose",
            },
        ]
        _run(mcp.call_tool("hook_probe", {"background_tasks": background_tasks}))

        assert received == [{"background_tasks": background_tasks}]
        assert received[0]["background_tasks"][0]["command"] == "sleep 45"
        assert received[0]["background_tasks"][1]["agent_type"] == "general-purpose"

    def test_a_scalar_field_still_works(self):
        """Widening to ``Any`` must not regress the ordinary scalar case."""
        received: list[dict | None] = []
        mcp = _fresh_mcp()
        register_hook_tools(
            mcp,
            (
                HookToolSpec(
                    name="probe",
                    run=lambda p: (received.append(p), HookResult(stdout="{}"))[1],
                    fields=("tool_name",),
                ),
            ),
        )

        _run(mcp.call_tool("hook_probe", {"tool_name": "Bash"}))

        assert received == [{"tool_name": "Bash"}]
