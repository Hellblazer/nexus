# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nexus.session.resolve_active_session_id (issue #594, nexus-9e9a;
extended nexus-36q84 for the CLAUDE_CODE_SESSION_ID tier).

The session_id resolution chain (NX_SESSION_ID env -> CLAUDE_CODE_SESSION_ID env ->
read_claude_session_id -> fallback) must live in exactly one place and be reused
by all three callsites:

  1. T1Database._init_new_discovery       (every Path A/B/C/client branch)
  2. _record_tier_write (mcp/core.py)     (telemetry insert)
  3. _print_tier_status_summary (_session_end_launcher.py)
                                          (session-end summary)

Pre-PR these were three open-coded copies with three divergent fallbacks
(uuid4(), "unknown", and no fallback respectively). The drift class that
produced PR #590 (nexus-h8ge) was a divergence between two of these copies.
This file's tests exist so any future change to the chain has to be made
in exactly one place or it surfaces here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helper: write the on-disk current_session pointer.
# ─────────────────────────────────────────────────────────────────────────────


def _write_current_session(tmp_path: Path, sid: str) -> None:
    target = tmp_path / "current_session"
    target.write_text(sid)
    target.chmod(0o600)


# ─────────────────────────────────────────────────────────────────────────────
# resolve_active_session_id chain semantics
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveActiveSessionIdChain:
    """The chain: arg > NX_SESSION_ID env > CLAUDE_CODE_SESSION_ID env >
    read_claude_session_id() > None."""

    def test_explicit_arg_wins_over_env_and_file(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-claude-code-env")
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id("from-arg") == "from-arg"

    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id() == "from-env"

    def test_nx_session_id_wins_over_claude_code_session_id(
        self, tmp_path, monkeypatch
    ):
        """NX_SESSION_ID must stay the highest env-based priority — nested
        claude -p dispatch and tests that force a specific session id rely
        on being able to override CLAUDE_CODE_SESSION_ID (which is
        inherited identically by nested subprocesses/subagents)."""
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "from-nx-env")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-claude-code-env")
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id() == "from-nx-env"

    def test_claude_code_session_id_wins_over_file_when_nx_unset(
        self, tmp_path, monkeypatch
    ):
        """nexus-36q84: the regression this bead exists to fix. With
        NX_SESSION_ID unset, CLAUDE_CODE_SESSION_ID must win over the
        machine-wide current_session flat file — even when the file holds
        a DIFFERENT (stale/clobbered-by-a-sibling-session) value. This is
        the assertion that actually proves the collision is closed, not
        just described."""
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-A-harness-id")
        _write_current_session(tmp_path, "session-B-clobbered-the-file")

        assert resolve_active_session_id() == "session-A-harness-id"

    def test_file_wins_when_env_empty(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        _write_current_session(tmp_path, "canonical-uuid")

        assert resolve_active_session_id() == "canonical-uuid"

    def test_returns_none_when_nothing_resolves(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        # No current_session file written.

        assert resolve_active_session_id() is None

    def test_blank_env_treated_as_unset(self, tmp_path, monkeypatch):
        """Whitespace-only NX_SESSION_ID falls through, matching the
        pre-PR behaviour every callsite implemented via .strip()."""
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "   ")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id() == "from-file"

    def test_blank_claude_code_session_id_treated_as_unset(
        self, tmp_path, monkeypatch
    ):
        """Whitespace-only CLAUDE_CODE_SESSION_ID falls through to the
        file, matching the .strip() convention every tier uses."""
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "   ")
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id() == "from-file"

    def test_blank_arg_falls_through(self, tmp_path, monkeypatch):
        """Empty string arg behaves the same as None: chain continues."""
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id("") == "from-env"


class TestSessionCollisionRegression:
    """nexus-36q84: the actual documented failure scenario.

    hooks.session_start() writes current_session unconditionally whenever
    NX_SESSION_ID is not already inherited. A SECOND top-level Claude Code
    session starting on the same machine clobbers the file with its own
    id. Any Bash-tool subprocess or hook in the FIRST session, invoked
    after that point, must still resolve to the FIRST session's id via
    CLAUDE_CODE_SESSION_ID — not silently pick up the second session's id
    from the now-clobbered file.
    """

    @pytest.mark.t2_tenant
    def test_two_session_starts_race_the_flat_file_first_session_still_wins(
        self, tmp_path, monkeypatch
    ):
        from nexus import hooks
        from nexus.session import read_claude_session_id, resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        # Session A starts first (its own SessionStart hook invocation).
        hooks.session_start("session-A-uuid")

        # Session B starts second on the SAME machine/config dir and
        # clobbers the shared flat file (the documented bug).
        hooks.session_start("session-B-uuid")
        assert read_claude_session_id() == "session-B-uuid"

        # A Bash-tool subprocess belonging to session A (CLAUDE_CODE_SESSION_ID
        # set to A's id by the harness) resolves to A's id, NOT B's —
        # proving the fix, not just describing it.
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-A-uuid")
        assert resolve_active_session_id() == "session-A-uuid"


# ─────────────────────────────────────────────────────────────────────────────
# Callsite unification: each of the three sites routes through the helper.
# ─────────────────────────────────────────────────────────────────────────────


class TestT1DatabaseRoutesThroughHelper:
    """T1Database._resolve_session_id MUST delegate to
    nexus.session.resolve_active_session_id with the explicit ctor arg."""

    def test_t1_propagates_helper_value(self, tmp_path, monkeypatch):
        from nexus.db.t1 import T1Database

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        # Patch the helper to return a recognisable sentinel.
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id",
            lambda arg=None: "HELPER-SENTINEL-1234",
        )
        # Also patch the alias in nexus.db.t1's import namespace, since
        # `from nexus.session import resolve_active_session_id` would
        # bind the symbol at import time.
        monkeypatch.setattr(
            "nexus.db.t1.resolve_active_session_id",
            lambda arg=None: "HELPER-SENTINEL-1234",
            raising=False,
        )
        assert T1Database._resolve_session_id(None) == "HELPER-SENTINEL-1234"

    def test_t1_uses_unknown_fallback_when_helper_returns_none(
        self, tmp_path, monkeypatch
    ):
        """Per-entry T1 metadata can never be empty (it's the filter
        key). When the helper returns None, T1 must substitute
        ``"unknown"`` so the audit log and chunk store agree on
        attribution."""
        from nexus.db.t1 import T1Database

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # No current_session file -> helper returns None -> T1 uses "unknown".
        assert T1Database._resolve_session_id(None) == "unknown"


class TestRecordTierWriteRoutesThroughHelper:
    """_record_tier_write MUST delegate to resolve_active_session_id and
    use ``"unknown"`` as the per-row last-resort sentinel."""

    def test_tier_write_uses_helper(self, tmp_path, monkeypatch):
        """Patch the helper, invoke the function, assert the row landed
        with the helper's value as session_id."""
        from nexus.mcp.core import _record_tier_write

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id",
            lambda arg=None: "HELPER-SENTINEL-9876",
        )

        observed: dict[str, str] = {}

        # nexus-pyzk7: _record_tier_write routes through the telemetry STORE
        # (backend-blind), not a raw taxonomy.conn. Capture the store call.
        class _FakeTelemetry:
            def record_tier_write(self, *, session_id, **kwargs):
                observed["session_id"] = session_id

        class _FakeT2:
            def __init__(self):
                self.telemetry = _FakeTelemetry()

        from contextlib import contextmanager

        @contextmanager
        def _fake_t2_ctx():
            yield _FakeT2()

        monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)

        _record_tier_write(tool="t", tier="T1")
        assert observed.get("session_id") == "HELPER-SENTINEL-9876"

    def test_tier_write_unknown_fallback_when_helper_returns_none(
        self, tmp_path, monkeypatch
    ):
        from nexus.mcp.core import _record_tier_write

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # No file -> helper returns None -> tier-write uses "unknown".

        observed: dict[str, str] = {}

        class _FakeTelemetry:
            def record_tier_write(self, *, session_id, **kwargs):
                observed["session_id"] = session_id

        class _FakeT2:
            def __init__(self):
                self.telemetry = _FakeTelemetry()

        from contextlib import contextmanager

        @contextmanager
        def _fake_t2_ctx():
            yield _FakeT2()

        monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)

        _record_tier_write(tool="t", tier="T1")
        assert observed.get("session_id") == "unknown"




# ─────────────────────────────────────────────────────────────────────────────
# nexus-xs7fp: the flat-file tier belongs to Claude Code hosts, for an MCP server
# ─────────────────────────────────────────────────────────────────────────────

#: What a process started by Claude Code carries (measured on live nx-mcp
#: children of the CLI, nexus-xs7fp): every Claude Code subprocess has these.
_CLAUDE_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")


def _foreign_host_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The environment a Cursor / Codex / Gemini host gives nx-mcp: no
    session id of any kind and no Claude Code marker, with a Claude
    session's pointer file sitting in the machine-wide config dir."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    for name in _CLAUDE_MARKERS:
        monkeypatch.delenv(name, raising=False)
    _write_current_session(tmp_path, "last-claude-session")


def _as_mcp_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.session as session

    monkeypatch.setattr(session, "_mcp_server_process", True)


class TestFlatFileFallbackIsClaudeOnlyForTheMcpServer:
    def test_a_non_claude_host_does_not_get_the_files_session(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        _foreign_host_env(monkeypatch, tmp_path)
        _as_mcp_server(monkeypatch)

        assert resolve_active_session_id() is None

    def test_a_claude_code_child_still_gets_the_files_session(self, tmp_path, monkeypatch):
        """A Claude Code child with neither session env var (an older Claude
        Code, or a launch path that drops it) still falls back to the file."""
        from nexus.session import resolve_active_session_id

        _foreign_host_env(monkeypatch, tmp_path)
        _as_mcp_server(monkeypatch)
        monkeypatch.setenv("CLAUDECODE", "1")

        assert resolve_active_session_id() == "last-claude-session"

    def test_the_entrypoint_marker_alone_also_counts(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        _foreign_host_env(monkeypatch, tmp_path)
        _as_mcp_server(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")

        assert resolve_active_session_id() == "last-claude-session"

    def test_a_blank_marker_is_not_a_marker(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        _foreign_host_env(monkeypatch, tmp_path)
        _as_mcp_server(monkeypatch)
        monkeypatch.setenv("CLAUDECODE", "  ")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "")

        assert resolve_active_session_id() is None

    def test_a_named_session_still_wins_on_a_non_claude_host(self, tmp_path, monkeypatch):
        """The workaround the docs used to prescribe keeps working: tiers 1-3
        are untouched, only tier 4 is gated."""
        from nexus.session import resolve_active_session_id

        _foreign_host_env(monkeypatch, tmp_path)
        _as_mcp_server(monkeypatch)
        monkeypatch.setenv("NX_SESSION_ID", "named-by-the-host")
        assert resolve_active_session_id() == "named-by-the-host"

        monkeypatch.delenv("NX_SESSION_ID")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "harness-id")
        assert resolve_active_session_id() == "harness-id"

    def test_a_bare_cli_with_no_marker_keeps_the_file(self, tmp_path, monkeypatch):
        """Only the MCP server process is gated. `nx scratch` run from a plain
        terminal beside a Claude session (and a hook, a doctor run) has no
        marker and still resolves through the file, as documented."""
        from nexus.session import resolve_active_session_id

        _foreign_host_env(monkeypatch, tmp_path)

        assert resolve_active_session_id() == "last-claude-session"

    def test_the_t1_routing_of_a_non_claude_host_has_no_session_to_mint(self, tmp_path, monkeypatch):
        """What the MCP lifespan consumes: no session id, so it takes the
        existing fail-loud 'T1 unavailable this process' branch instead of
        minting or borrowing the Claude session's scope."""
        from nexus.db.t1 import T1RoutingAction, resolve_t1_routing_tiers

        _foreign_host_env(monkeypatch, tmp_path)
        _as_mcp_server(monkeypatch)
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)

        decision = resolve_t1_routing_tiers(tmp_path)

        assert decision.action == T1RoutingAction.MINT
        assert decision.session_id is None

    def test_marking_the_process_turns_the_gate_on(self, monkeypatch):
        import nexus.session as session

        monkeypatch.setattr(session, "_mcp_server_process", False)
        session.mark_mcp_server_process()

        assert session._mcp_server_process is True

    @pytest.mark.parametrize("module_name", ["nexus.mcp.core", "nexus.mcp.catalog"])
    def test_both_server_entry_points_mark_the_process(self, module_name):
        """Source-level pin, as the spawn-tripwire test does: nothing but
        ``main()`` runs only in a real server process, and a gate nobody
        switches on protects nothing."""
        import ast
        import importlib
        import inspect

        main = importlib.import_module(module_name).main
        tree = ast.parse(inspect.getsource(main).lstrip())
        called = {
            c.func.id for c in ast.walk(tree)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        assert "mark_mcp_server_process" in called, f"{module_name}.main() does not mark the process"


# ─────────────────────────────────────────────────────────────────────────────
# nexus-xs7fp review: order, not presence, and the operator-facing message
# ─────────────────────────────────────────────────────────────────────────────

#: Calls main() may make before it marks the process. isolate_stdin only moves
#: the stdio protocol off fd 0 and never resolves a session.
_ALLOWED_BEFORE_MARK = {"isolate_stdin"}


def _call_name(call: "ast.Call") -> str:
    import ast

    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "?"


def _main_marking_problems(main_source: str) -> list[str]:
    """Problems with where main() marks the process: not an unconditional
    top-level statement of main(), or any call ahead of it other than the
    allowed ones (so a session resolution could run unmarked)."""
    import ast

    fn = ast.parse(main_source).body[0]
    mark_index = next(
        (
            i
            for i, stmt in enumerate(fn.body)
            if isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and _call_name(stmt.value) == "mark_mcp_server_process"
        ),
        None,
    )
    if mark_index is None:
        return ["mark_mcp_server_process() is not an unconditional top-level statement of main()"]
    problems = []
    for stmt in fn.body[:mark_index]:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _call_name(node) not in _ALLOWED_BEFORE_MARK:
                problems.append(f"{_call_name(node)}() runs before the process is marked")
    return problems


def _module_level_resolver_calls(module_source: str) -> list[str]:
    """Calls to a session reader at import time (outside any def): they would
    run before main() can mark the process."""
    import ast

    readers = {"resolve_active_session_id", "read_claude_session_id"}
    found = []
    for stmt in ast.parse(module_source).body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _call_name(node) in readers:
                found.append(_call_name(node))
    return found


class TestTheProcessIsMarkedBeforeAnySessionIsResolved:
    @pytest.mark.parametrize("module_name", ["nexus.mcp.core", "nexus.mcp.catalog"])
    def test_main_marks_the_process_before_anything_else_runs(self, module_name):
        import importlib
        import inspect

        main = importlib.import_module(module_name).main
        assert _main_marking_problems(inspect.getsource(main).lstrip()) == []

    @pytest.mark.parametrize("module_name", ["nexus.mcp.core", "nexus.mcp.catalog"])
    def test_no_session_is_resolved_at_import_time(self, module_name):
        import importlib
        import inspect

        module = importlib.import_module(module_name)
        assert _module_level_resolver_calls(inspect.getsource(module)) == []

    def test_the_ordering_check_can_fail(self):
        """The check itself, against sources that are wrong in each way."""
        late = (
            "def main():\n"
            "    resolve_active_session_id()\n"
            "    mark_mcp_server_process()\n"
        )
        conditional = (
            "def main():\n"
            "    if x:\n"
            "        mark_mcp_server_process()\n"
        )
        missing = "def main():\n    isolate_stdin()\n"
        fine = (
            "def main():\n"
            "    isolate_stdin()\n"
            "    mark_mcp_server_process()\n"
            "    resolve_active_session_id()\n"
        )
        assert _main_marking_problems(late) == ["resolve_active_session_id() runs before the process is marked"]
        assert _main_marking_problems(conditional) != []
        assert _main_marking_problems(missing) != []
        assert _main_marking_problems(fine) == []
        assert _module_level_resolver_calls("x = resolve_active_session_id()\n") == ["resolve_active_session_id"]
        assert _module_level_resolver_calls("def f():\n    resolve_active_session_id()\n") == []


class TestTheUnavailableMessageNamesTheRemedy:
    def _message(self) -> str:
        from nexus.mcp.core import T1UnavailableThisProcessError, _raise_t1_unavailable_this_process

        with pytest.raises(T1UnavailableThisProcessError) as exc:
            _raise_t1_unavailable_this_process()
        return str(exc.value)

    def test_it_names_nx_session_id_as_the_remedy(self):
        msg = self._message()
        remedy = msg.split("Remedy:", 1)[1]
        assert "NX_SESSION_ID" in remedy

    def test_it_says_the_flat_file_is_deliberately_skipped(self):
        assert "current_session" in self._message().split("Remedy:", 1)[0]

    def test_it_no_longer_sends_a_desktop_user_to_a_claude_code_session(self):
        assert "Claude Code session" not in self._message().split("Remedy:", 1)[1]
