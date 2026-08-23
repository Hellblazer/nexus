"""Session hook tests: session_start and session_end lifecycle."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.hooks import session_end, session_end_flush, session_start


# NO _no_daemon stub: it forced `t2_index_write`'s direct-fallback path by
# making the daemon reachability probe fail (RDR-128 P3). That router — probe
# and all — was removed in nexus-i711w Stage 2 sub-stage B, so the SessionEnd
# flush already writes directly to the autouse-isolated tmp `memory.db`.


# ── session_start ────────────────────────────────────────────────────────────
#
# RDR-094 Phase F (4.13.0 / nexus-2lm0) deleted the hook-side chroma
# spawn block: ``start_t1_server``, ``write_session_record_by_id``,
# ``find_ancestor_session``, the session.lock acquisition, and the
# watchdog spawn all moved to nx-mcp's FastMCP lifespan. The hook now
# does sweep + UUID resolution + (optionally) current_session write.
# These tests pin that minimal contract.


@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
def test_session_start_returns_session_id(_mock_sid, tmp_path: Path) -> None:
    """session_start returns the session ID line."""
    with (
        patch("nexus.hooks.write_claude_session_id"),
    ):
        output = session_start()

    assert "test-uuid" in output
    assert "Nexus ready" in output


# ── #435 legacy session.lock cleanup ─────────────────────────────────────────





def test_session_start_does_not_overwrite_current_session_when_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested subprocess (NX_SESSION_ID set in env) must NOT stomp the
    parent's ``current_session`` flat file. Without this guard, every
    operator ``claude -p`` call would overwrite the parent's UUID with
    its own transient one, and the parent's shell-side ``nx scratch``
    would fall back to EphemeralClient for the rest of the conversation.
    """
    monkeypatch.setenv("NX_SESSION_ID", "parent-uuid-keep-me")
    mock_write = MagicMock()

    with (
        patch("nexus.hooks.write_claude_session_id", mock_write),
    ):
        output = session_start(claude_session_id="my-own-transient-uuid")

    assert "parent-uuid-keep-me" in output
    mock_write.assert_not_called()


def test_session_start_writes_current_session_when_top_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level Claude session (no NX_SESSION_ID inherited) writes
    ``current_session``, populating the cross-tree pointer that shell
    tools and subagents rely on."""
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    mock_write = MagicMock()

    with (
        patch("nexus.hooks.write_claude_session_id", mock_write),
    ):
        session_start(claude_session_id="top-level-uuid")

    mock_write.assert_called_once_with("top-level-uuid")


def test_session_start_uses_inherited_session_id(tmp_path: Path, monkeypatch) -> None:
    """When ``NX_SESSION_ID`` is set in env, the hook uses it verbatim
    rather than generating a new UUID or honouring the stdin payload.
    Subagents inherit the parent's ID this way."""
    monkeypatch.setenv("NX_SESSION_ID", "inherited-uuid")
    with (
        patch("nexus.hooks.write_claude_session_id"),
    ):
        output = session_start(claude_session_id="ignored-stdin-uuid")
    assert "inherited-uuid" in output
    assert "ignored-stdin-uuid" not in output


def test_session_start_falls_back_to_generated_uuid(tmp_path, monkeypatch) -> None:
    """No NX_SESSION_ID env and no stdin payload: generate a fresh UUID
    so invocations outside Claude Code (e.g. ``nx hook session-start``
    from a script) still produce a usable session pointer."""
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    with (
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.generate_session_id", return_value="fresh-uuid"),
    ):
        output = session_start()
    assert "fresh-uuid" in output


# ── T1 handoff marker writer (nexus-d76vc) ───────────────────────────────────
#
# On source=clear/resume, session_start() writes a T1 handoff marker for
# every live nx-mcp/nx-mcp-catalog sibling of the hook's own claude
# ancestor, so the MCP lifespan's watcher (nexus.mcp.core) can re-lease
# onto the new session id. startup/compact write nothing. Ancestry
# authentication happens via find_mcp_sibling_pids (unit-tested directly
# in tests/test_t1_discovery.py); these tests pin session_start's own
# source-gating and confirm it writes real, correctly-keyed marker files.


class TestT1HandoffMarkerWriter:
    def _session_start(self, monkeypatch, tmp_path, *, source, claude_pid=4242,
                        sibling_pids=(5001, 5002)):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        with (
            patch("nexus.hooks.write_claude_session_id"),
            patch("nexus.session.find_immediate_claude_pid", return_value=claude_pid),
            patch("nexus.session.find_mcp_sibling_pids", return_value=list(sibling_pids)) as mock_siblings,
        ):
            output = session_start(claude_session_id="new-sess-id", source=source)
        return output, mock_siblings

    def test_clear_writes_markers_for_every_sibling(self, tmp_path, monkeypatch) -> None:
        from nexus.daemon.t1_handoff import read_handoff_marker

        self._session_start(monkeypatch, tmp_path, source="clear")
        for pid in (5001, 5002):
            marker = read_handoff_marker(pid, tmp_path)
            assert marker is not None
            assert marker.new_session_id == "new-sess-id"
            assert marker.claude_pid == 4242

    def test_resume_writes_markers_for_every_sibling(self, tmp_path, monkeypatch) -> None:
        from nexus.daemon.t1_handoff import read_handoff_marker

        self._session_start(monkeypatch, tmp_path, source="resume")
        for pid in (5001, 5002):
            marker = read_handoff_marker(pid, tmp_path)
            assert marker is not None
            assert marker.new_session_id == "new-sess-id"

    def test_startup_writes_no_marker(self, tmp_path, monkeypatch) -> None:
        from nexus.daemon.t1_handoff import read_handoff_marker

        _, mock_siblings = self._session_start(monkeypatch, tmp_path, source="startup")
        mock_siblings.assert_not_called()
        for pid in (5001, 5002):
            assert read_handoff_marker(pid, tmp_path) is None

    def test_compact_writes_no_marker(self, tmp_path, monkeypatch) -> None:
        from nexus.daemon.t1_handoff import read_handoff_marker

        _, mock_siblings = self._session_start(monkeypatch, tmp_path, source="compact")
        mock_siblings.assert_not_called()
        for pid in (5001, 5002):
            assert read_handoff_marker(pid, tmp_path) is None

    def test_no_source_writes_no_marker(self, tmp_path, monkeypatch) -> None:
        """Bare ``nx hook session-start`` invocation (no stdin payload /
        no source) must not write handoff markers."""
        from nexus.daemon.t1_handoff import read_handoff_marker

        _, mock_siblings = self._session_start(monkeypatch, tmp_path, source=None)
        mock_siblings.assert_not_called()
        assert read_handoff_marker(5001, tmp_path) is None

    def test_unresolvable_claude_pid_writes_no_marker(self, tmp_path, monkeypatch) -> None:
        """find_immediate_claude_pid() falling back to 0 (no ancestry at
        all) must not write anything -- there is no verified ancestor to
        key the marker to."""
        from nexus.daemon.t1_handoff import read_handoff_marker

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        with (
            patch("nexus.hooks.write_claude_session_id"),
            patch("nexus.session.find_immediate_claude_pid", return_value=0),
            patch("nexus.session.find_mcp_sibling_pids") as mock_siblings,
        ):
            session_start(claude_session_id="new-sess-id", source="clear")
        mock_siblings.assert_not_called()
        assert read_handoff_marker(5001, tmp_path) is None

    def test_foreign_pid_never_gets_a_marker(self, tmp_path, monkeypatch) -> None:
        """find_mcp_sibling_pids is the sole ancestry gate: a pid it does
        NOT return (a different session's MCP server) never gets a
        marker written for it, even though it may share a config dir."""
        from nexus.daemon.t1_handoff import read_handoff_marker

        self._session_start(
            monkeypatch, tmp_path, source="clear", sibling_pids=(5001,),
        )
        # 5001 (returned) got a marker; 9999 (a foreign/different-session
        # pid, never returned by find_mcp_sibling_pids) got none.
        assert read_handoff_marker(5001, tmp_path) is not None
        assert read_handoff_marker(9999, tmp_path) is None

    def test_no_siblings_found_writes_nothing_and_does_not_raise(
        self, tmp_path, monkeypatch,
    ) -> None:
        output, _ = self._session_start(
            monkeypatch, tmp_path, source="clear", sibling_pids=(),
        )
        assert "Nexus ready" in output

    def test_marker_write_failure_does_not_crash_session_start(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A SessionStart hook must never fail the session over a T1-scope
        convenience feature (best-effort, logged at debug)."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        with (
            patch("nexus.hooks.write_claude_session_id"),
            patch("nexus.session.find_immediate_claude_pid", side_effect=RuntimeError("boom")),
        ):
            output = session_start(claude_session_id="new-sess-id", source="clear")
        assert "Nexus ready" in output


# ── session_end ──────────────────────────────────────────────────────────────
















def test_session_end_db_error_doesnt_crash(tmp_path: Path) -> None:
    """Storage errors during flush are caught gracefully.

    RDR-128 P3: session_end_flush now routes its writes through
    ``mcp_infra.t2_index_write``; force a storage error out of that path
    and assert the hook still returns its summary rather than crashing."""
    import sqlite3

    sessions = tmp_path / "sessions"
    sessions.mkdir()

    def _boom(_write_fn):
        raise sqlite3.OperationalError("disk I/O error")

    with patch("nexus.mcp_infra.t2_index_write", _boom):
        output = session_end()

    assert "Session ended" in output


# ── session_end_flush (RDR-094 Phase B / nexus-2b9r) ────────────────────────


class TestSessionEndFlush:
    """The split-out flush function does T1 flush + T2 expire and never
    touches chroma. Phase 4's hooks.json swap (Phase C / nexus-l828)
    points at this function so the SessionEnd path cannot race the
    MCP-owned chroma teardown."""

    def test_flush_returns_summary_when_no_record(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        monkeypatch.delenv("NX_SESSION_ID", raising=False)

        output = session_end_flush()

        assert "Flushed 0" in output
        assert "Expired 0" in output









# ── nx hook session-end-flush CLI subcommand ────────────────────────────────


def test_session_end_flush_cli_subcommand(tmp_path, monkeypatch):
    """The new CLI subcommand routes to session_end_flush, not session_end."""
    from click.testing import CliRunner

    from nexus.commands.hook import hook_group

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.delenv("NX_SESSION_ID", raising=False)

    runner = CliRunner()
    result = runner.invoke(hook_group, ["session-end-flush"])

    assert result.exit_code == 0
    assert "Flushed 0" in result.output
    assert "Expired 0" in result.output


# ── session lock stale cleanup ───────────────────────────────────────────────


# Phase F (RDR-094 / nexus-2lm0) deleted the hook-side chroma-spawn
# block, including the session.lock acquisition. The lock guarded
# concurrent siblings from each calling start_t1_server. Now nx-mcp's
# lifespan owns spawn, the hook does no T1 work, and there is no
# lock to test. The pre-Phase-F tests
# (test_session_start_writes_pid_to_lock,
# test_session_start_clears_stale_lock) were removed with the code
# they covered.


# ── _infer_repo ──────────────────────────────────────────────────────────────

def test_infer_repo_git_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When not in a git repo, falls back to cwd name."""
    from nexus.hooks import _infer_repo

    monkeypatch.chdir(tmp_path)
    name = _infer_repo()
    assert name == tmp_path.name


# ── RDR-155 P4b: the nexus-0rwwv SessionStart migration notice is retired ───


def test_session_start_carries_no_migration_notice(monkeypatch):
    """The bridge died with the migration machinery: SessionStart is the
    plain ready line — stranded pre-PG installs are redirected by the
    stranded-install detector at CLI/MCP startup instead."""
    from unittest.mock import patch as _patch

    with _patch("nexus.hooks.write_claude_session_id"):
        output = session_start(claude_session_id="s-0rwwv")
    assert "Nexus ready" in output
    assert "storage migration" not in output
    assert "guided-upgrade" not in output


# ── nexus-otnvr item 5: proactive stale-mcp-host SessionStart nudge ─────────
#
# substantive-critic 2026-08-08: doctor's Process freshness check
# (nexus-4xgfy) only fires when an operator manually runs `nx doctor` —
# every OTHER live Claude session stays blind to a background upgrade.
# `nx hook session-start` is the one hook-surface invocation that runs the
# full installed nx (package imports available, unlike the bare-interpreter
# hook scripts), so it's the cheapest proactive close: every NEW session
# announces machine-wide nx-mcp staleness via the identical primitive
# doctor uses (nexus.upgrade_finish.detect_stale_processes), so the two
# surfaces can never diverge on what "stale" means.


class _FakeStaleProcess:
    def __init__(self, kind: str) -> None:
        self.kind = kind


class _FakeSkewReport:
    def __init__(self, *, stale: list, installed_version: str = "9.9.9") -> None:
        self.stale = stale
        self.installed_version = installed_version


class TestStaleMcpHostSessionStartNudge:
    def test_no_stale_processes_appends_nothing(self, monkeypatch) -> None:
        from unittest.mock import patch as _patch

        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch(
                "nexus.upgrade_finish.detect_stale_processes",
                return_value=_FakeSkewReport(stale=[]),
            ),
        ):
            output = session_start(claude_session_id="s-otnvr-clean")
        assert "Nexus ready" in output
        assert "NOTE" not in output
        assert "predate" not in output

    def test_stale_non_mcp_process_appends_nothing(self, monkeypatch) -> None:
        """Only mcp-host staleness is this session's business — a stale
        aspect-worker/mineru/service process is doctor's/restart-stale's
        job, not a SessionStart nudge."""
        from unittest.mock import patch as _patch

        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch(
                "nexus.upgrade_finish.detect_stale_processes",
                return_value=_FakeSkewReport(stale=[_FakeStaleProcess("aspect-worker")]),
            ),
        ):
            output = session_start(claude_session_id="s-otnvr-other-kind")
        assert "NOTE" not in output

    def test_stale_mcp_host_appends_warning_with_version_and_mcp_hint(
        self, monkeypatch
    ) -> None:
        from unittest.mock import patch as _patch

        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch(
                "nexus.upgrade_finish.detect_stale_processes",
                return_value=_FakeSkewReport(
                    stale=[_FakeStaleProcess("mcp-host"), _FakeStaleProcess("mcp-host")],
                    installed_version="7.5.0",
                ),
            ),
        ):
            output = session_start(claude_session_id="s-otnvr-stale")
        assert "Nexus ready" in output
        assert "NOTE" in output
        assert "2 nx-mcp process(es)" in output
        assert "7.5.0" in output
        assert "/mcp" in output

    def test_probe_failure_never_breaks_session_start(self, monkeypatch) -> None:
        from unittest.mock import patch as _patch

        def boom():
            raise RuntimeError("ps unavailable")

        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch("nexus.upgrade_finish.detect_stale_processes", side_effect=boom),
        ):
            output = session_start(claude_session_id="s-otnvr-probe-fail")
        assert "Nexus ready" in output
        assert "NOTE" not in output


# ── nexus-h33x8.4: SessionStart guidance imperative, re-plumbed Tier B ──────
#
# The guidance imperative (formerly delivered by the pinned plugin's
# `cat .../using-nx-skills/SKILL.md` hooks.json entry) is now appended to
# `session_start()`'s own output, gated by the interim double-emission
# guard in nexus.session_start_guidance (unit-tested directly in
# tests/test_session_start_guidance.py — these pin the INTEGRATION into
# session_start() specifically).


class TestGuidanceImperativeIntegration:
    def test_guidance_text_appended_when_channel_open(self, monkeypatch) -> None:
        from unittest.mock import patch as _patch

        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch(
                "nexus.session_start_guidance.guidance_block",
                return_value="GUIDANCE-MARKER-TEXT",
            ),
        ):
            output = session_start(claude_session_id="s-h33x8-4-open")
        assert "Nexus ready" in output
        assert "GUIDANCE-MARKER-TEXT" in output

    def test_no_guidance_text_when_channel_suppressed(self, monkeypatch) -> None:
        from unittest.mock import patch as _patch

        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch("nexus.session_start_guidance.guidance_block", return_value=""),
        ):
            output = session_start(claude_session_id="s-h33x8-4-suppressed")
        assert "Nexus ready" in output
        assert "GUIDANCE-MARKER-TEXT" not in output

    def test_guidance_probe_failure_never_breaks_session_start(self, monkeypatch) -> None:
        from unittest.mock import patch as _patch

        def boom():
            raise RuntimeError("guidance module import failed")

        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch("nexus.session_start_guidance.guidance_block", side_effect=boom),
        ):
            output = session_start(claude_session_id="s-h33x8-4-boom")
        assert "Nexus ready" in output

    def test_real_guidance_text_reaches_output_end_to_end(self, monkeypatch) -> None:
        """No mocking of the guidance module itself: with no
        CLAUDE_PLUGIN_ROOT set (the test-harness default), the legacy-
        channel gate fails open and the real imperative text appears."""
        from unittest.mock import patch as _patch

        from nexus.session_start_guidance import GUIDANCE_IMPERATIVE

        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        with _patch("nexus.hooks.write_claude_session_id"):
            output = session_start(claude_session_id="s-h33x8-4-e2e")
        assert "Nexus ready" in output
        assert GUIDANCE_IMPERATIVE in output


# ── nexus-h33x8.5 VERIFICATION 1: the `nx hook session-start` emitter's own
# byte budget, end-to-end through session_start(). Mirrors the mocking
# pattern from TestStaleMcpHostSessionStartNudge so the stale-process
# best-effort probe (real filesystem/process introspection otherwise)
# can't make this flaky.


class TestGuidanceByteBudgetIntegration:
    #: session_start()'s own "Nexus ready (session: ...)." prefix plus the
    #: (mocked-absent) stale-process NOTE plus the short guidance block.
    #: 2,000 leaves headroom over the measured 2026-08-20 baseline (~1,182
    #: bytes for a typical session id) while remaining far below the
    #: routing-menu-era baseline this replaces (~8,800 bytes) and well
    #: under this emitter's share of the bead's 6,000-byte SessionStart
    #: total (the other unconditional emitter, session_start_hook.py, is
    #: out of this bead's scope — see nexus-h33x8.5 dev notes).
    _TOTAL_BUDGET_BYTES = 2000

    def test_session_start_output_under_byte_budget_with_imperative_first(
        self, monkeypatch
    ) -> None:
        from unittest.mock import patch as _patch

        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        with (
            _patch("nexus.hooks.write_claude_session_id"),
            _patch(
                "nexus.upgrade_finish.detect_stale_processes",
                return_value=_FakeSkewReport(stale=[]),
            ),
        ):
            output = session_start(claude_session_id="s-h33x8-5-budget")
        n = len(output.encode("utf-8"))
        assert n < self._TOTAL_BUDGET_BYTES, (
            f"nx hook session-start emitted {n} bytes, budget is "
            f"{self._TOTAL_BUDGET_BYTES}"
        )
        # TONE CHANGED 2026-08-23 (nexus-bc292). This asserted the
        # literal "You MUST invoke `Skill`". That sentence was measured,
        # not assumed, and it did not work: across 6 sandboxed runs of
        # the eval corpus case that exercises exactly this rule, the
        # SessionStart hook fired, this text reached the model verbatim,
        # and a conexus skill was invoked 1 time in 6. Three of those
        # runs made ZERO Skill calls. The instruction was already
        # maximal -- "hard rule, not a hint", "skipping is a defect" --
        # so writing it harder was the one remedy known to fail, because
        # that IS what shipped. What this test protects is unchanged:
        # the byte budget above, and that the routing statement leads
        # the output rather than sitting behind a preamble. Only the
        # sentence it pins changed.
        head = output.encode("utf-8")[:500].decode("utf-8", errors="ignore")
        assert "Conexus skills carry this project's accumulated practice" in head
