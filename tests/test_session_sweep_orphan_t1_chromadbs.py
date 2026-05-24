# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nexus.session.sweep_orphan_t1_chromadbs (issue nexus-aigkb).

Each ungraceful Claude Code session exit (SIGKILL, crash, lost
SessionEnd hook) leaves the per-session T1 chromadb server
re-parented to launchd / init (pid 1). The chromadb keeps holding a
TCP port, file descriptors, and a tmpdir indefinitely.

Live observation 2026-05-24: 5 orphan chromadbs accumulated over 3
days of normal Claude Code use, parented to launchd. Each had been
running 1-3 days post-session-exit.

The parallel sweep ``sweep_orphan_resource_trackers`` reaps
multiprocessing tracker children; this one reaps the chromadb
parent itself. Both run at MCP top-level startup so the next
start_t1_server has a clean slate.
"""

from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Parser tests (pure function, no subprocess).
# ─────────────────────────────────────────────────────────────────────────────


class TestParseOrphanT1ChromadbCandidates:
    """Validate _parse_orphan_t1_chromadb_candidates."""

    def test_parses_orphan_chromadb_line(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       12:34 /path/to/python /path/to/chroma run "
            "--host 127.0.0.1 --port 49994 --path /tmp/nx_t1_abcd1234\n"
        )
        assert _parse_orphan_t1_chromadb_candidates(ps_output) == [211]

    def test_skips_chromadb_with_live_parent(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211   555       12:34 /path/to/chroma run --port 49994 "
            "--path /tmp/nx_t1_abcd1234\n"
        )
        # PPID != 1 → still owned by a live Claude Code session
        assert _parse_orphan_t1_chromadb_candidates(ps_output) == []

    def test_skips_chromadb_without_nx_t1_marker(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       12:34 /path/to/chroma run --port 49994 "
            "--path /home/user/my-project/.chroma\n"
        )
        # No nx_t1_ marker → user's own chromadb, off-limits
        assert _parse_orphan_t1_chromadb_candidates(ps_output) == []

    def test_skips_non_chromadb_orphans(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       12:34 /path/to/python -m my_other_service "
            "--data /tmp/nx_t1_abcd\n"
        )
        # Has nx_t1_ marker but not "chroma run" → not ours
        assert _parse_orphan_t1_chromadb_candidates(ps_output) == []

    def test_age_threshold_excludes_in_flight_spawn(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       00:05 /path/to/chroma run --port 49994 "
            "--path /tmp/nx_t1_abcd1234\n"
        )
        # 5s old; default min_age 60s → skip (could be a racing spawn)
        assert _parse_orphan_t1_chromadb_candidates(ps_output) == []

    def test_protected_pids_honored(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       12:34 /path/to/chroma run --port 49994 "
            "--path /tmp/nx_t1_abcd1234\n"
        )
        assert _parse_orphan_t1_chromadb_candidates(
            ps_output, protected_pids={211}
        ) == []

    def test_multi_day_etime_parses(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1   03-21:36:36 /path/to/chroma run --port 49994 "
            "--path /tmp/nx_t1_abcd1234\n"
        )
        # 3d 21h+ old; well past the 60s threshold
        assert _parse_orphan_t1_chromadb_candidates(ps_output) == [211]

    def test_handles_empty_input(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        assert _parse_orphan_t1_chromadb_candidates("") == []
        assert _parse_orphan_t1_chromadb_candidates(
            "  PID  PPID     ELAPSED COMMAND\n"
        ) == []

    def test_skips_malformed_lines(self):
        from nexus.session import _parse_orphan_t1_chromadb_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "garbage line with no pid\n"
            "  211     1       12:34 /path/to/chroma run --port 49994 --path /tmp/nx_t1_abcd1234\n"
            "  not-a-pid     1       12:34 chroma run nx_t1_other\n"
        )
        # Only the well-formed orphan line is returned
        assert _parse_orphan_t1_chromadb_candidates(ps_output) == [211]


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-level tests (mocks the ps subprocess and _kill_orphan_tracker_pids).
# ─────────────────────────────────────────────────────────────────────────────


class TestSweepOrphanT1Chromadbs:
    """Validate sweep_orphan_t1_chromadbs end-to-end with mocked subprocess."""

    def test_sweep_signals_orphans(self, monkeypatch):
        import nexus.session as session

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       12:34 chroma run --port 49994 --path /tmp/nx_t1_abcd1234\n"
            "  212     1       12:34 chroma run --port 49995 --path /tmp/nx_t1_efgh5678\n"
        )

        monkeypatch.setattr(
            session.subprocess,
            "check_output",
            lambda *a, **kw: ps_output,
        )

        signalled_pids: list[int] = []

        def fake_kill(pids, **kwargs):
            signalled_pids.extend(pids)
            return len(pids)

        monkeypatch.setattr(session, "_kill_orphan_tracker_pids", fake_kill)

        result = session.sweep_orphan_t1_chromadbs()
        assert result == 2
        assert signalled_pids == [211, 212]

    def test_sweep_skips_when_no_orphans(self, monkeypatch):
        import nexus.session as session

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211   555       12:34 chroma run --port 49994 --path /tmp/nx_t1_abcd1234\n"
        )
        monkeypatch.setattr(
            session.subprocess, "check_output", lambda *a, **kw: ps_output,
        )

        def must_not_be_called(*a, **kw):
            pytest.fail("_kill_orphan_tracker_pids should not be called when no orphans")

        monkeypatch.setattr(
            session, "_kill_orphan_tracker_pids", must_not_be_called
        )

        assert session.sweep_orphan_t1_chromadbs() == 0

    def test_sweep_swallows_ps_failure(self, monkeypatch):
        import nexus.session as session
        import subprocess

        def failing_ps(*a, **kw):
            raise subprocess.CalledProcessError(1, "ps")

        monkeypatch.setattr(session.subprocess, "check_output", failing_ps)
        # Should return 0, not raise — failures must never block startup
        assert session.sweep_orphan_t1_chromadbs() == 0
