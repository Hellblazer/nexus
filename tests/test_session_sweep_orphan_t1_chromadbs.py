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
# Liveness-probe parser tests (pure function, no subprocess) -- nexus-oj1hn /
# GH #1151: sweep_orphan_tmpdirs must never reap a tmpdir backing a chromadb
# that is still running, even with a live (non-init) parent.
# ─────────────────────────────────────────────────────────────────────────────


class TestParseLiveT1ChromadbPaths:
    """Validate _parse_live_t1_chromadb_paths."""

    def test_extracts_path_basic(self):
        """A row carrying both markers has its --path value extracted.

        NOTE on ppid: this parser's input is ``ps -eo pid,command`` --
        TWO columns, no ppid field at all (contrast the sibling KILL
        gate's ``_parse_orphan_t1_chromadb_candidates``, which parses
        FOUR columns -- ``pid,ppid,etime,command`` -- specifically to
        filter on ppid==1). There is no ppid value in this function's
        input shape for a synthetic-row test to vary, so "ANY ppid
        matches" is a property of the CALLER's `ps` invocation (`ps -e`
        lists every process regardless of parent) and this function's
        total absence of ppid-based filtering logic, not something a
        per-row unit test can independently prove one way or the
        other. Verify that absence by reading the parser body, not by
        looking for a ppid assertion in a test."""
        from nexus.session import _parse_live_t1_chromadb_paths

        ps_output = (
            "  PID COMMAND\n"
            "  211 /path/to/chroma run --host 127.0.0.1 --port 49994 "
            "--path /Users/hal/.config/nexus/t1/nx_t1_abcd1234\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == {
            "/Users/hal/.config/nexus/t1/nx_t1_abcd1234"
        }

    def test_extracts_path_second_row_independently(self):
        """A second, differently-shaped row (no full binary path prefix,
        different port/path) is also extracted correctly -- proves the
        parser isn't accidentally coupled to the exact formatting of the
        first test's fixture."""
        from nexus.session import _parse_live_t1_chromadb_paths

        ps_output = (
            "  PID COMMAND\n"
            "  211 chroma run --port 49994 --path /tmp/nx_t1_abcd1234\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == {"/tmp/nx_t1_abcd1234"}

    def test_skips_row_without_chroma_run_marker(self):
        from nexus.session import _parse_live_t1_chromadb_paths

        ps_output = (
            "  PID COMMAND\n"
            "  211 /path/to/python -m my_other_service --data /tmp/nx_t1_abcd\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == set()

    def test_skips_row_without_nx_t1_marker(self):
        from nexus.session import _parse_live_t1_chromadb_paths

        ps_output = (
            "  PID COMMAND\n"
            "  211 chroma run --port 49994 --path /home/user/my-project/.chroma\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == set()

    def test_handles_empty_input(self):
        from nexus.session import _parse_live_t1_chromadb_paths

        assert _parse_live_t1_chromadb_paths("") == set()
        assert _parse_live_t1_chromadb_paths("  PID COMMAND\n") == set()

    def test_skips_malformed_lines(self):
        from nexus.session import _parse_live_t1_chromadb_paths

        ps_output = (
            "  PID COMMAND\n"
            "garbage line with no pid\n"
            "  211 chroma run --port 49994 --path /tmp/nx_t1_abcd1234\n"
            "not-a-pid chroma run nx_t1_other\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == {"/tmp/nx_t1_abcd1234"}

    def test_multiple_live_paths(self):
        from nexus.session import _parse_live_t1_chromadb_paths

        ps_output = (
            "  PID COMMAND\n"
            "  211 chroma run --port 49994 --path /tmp/nx_t1_aaaa\n"
            "  212 chroma run --port 49995 --path /tmp/nx_t1_bbbb\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == {
            "/tmp/nx_t1_aaaa",
            "/tmp/nx_t1_bbbb",
        }

    def test_row_missing_path_flag_skipped(self):
        """Defensive: a chroma row with both markers present in the
        command text but no extractable --path token is dropped
        rather than raising."""
        from nexus.session import _parse_live_t1_chromadb_paths

        ps_output = (
            "  PID COMMAND\n"
            "  211 chroma run --port 49994 nx_t1_no_path_flag_here\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == set()

    def test_long_path_survives_full_width(self):
        """PLAN-AUDIT HIGH FINDING: ps -eo command TRUNCATES to column
        width without -ww. A live nx_t1_ path long enough to exceed
        that width must still be extracted whole by the parser's own
        regex -- this proves the parser itself doesn't impose a
        second, silent truncation on top of requiring -ww from the
        caller."""
        from nexus.session import _parse_live_t1_chromadb_paths

        long_suffix = "x" * 220
        long_path = f"/Users/hal/.config/nexus/t1/nx_t1_{long_suffix}"
        ps_output = (
            "  PID COMMAND\n"
            f"  211 /path/to/chroma run --host 127.0.0.1 --port 49994 "
            f"--path {long_path}\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == {long_path}

    def test_path_with_embedded_space_survives(self):
        """CODE-REVIEW-EXPERT IMPORTANT FINDING (review round 1): a naive
        ``(\\S+)`` capture truncates at the first embedded space. A store
        path CAN legitimately contain one -- an unvalidated
        NEXUS_CONFIG_DIR override, or a space-containing $HOME on macOS
        (e.g. "/Users/Jane Doe/.config/nexus/t1/nx_t1_..."). Without the
        end-of-line capture fix, this would silently truncate the
        extracted path, the exact-string match against str(d) would
        fail, and the live dir would be reaped anyway -- reproducing
        GH #1151 under this specific precondition."""
        from nexus.session import _parse_live_t1_chromadb_paths

        space_path = "/Users/Jane Doe/.config/nexus/t1/nx_t1_abcd1234"
        ps_output = (
            "  PID COMMAND\n"
            f"  211 /path/to/chroma run --host 127.0.0.1 --port 49994 "
            f"--path {space_path}\n"
        )
        assert _parse_live_t1_chromadb_paths(ps_output) == {space_path}


# ─────────────────────────────────────────────────────────────────────────────
# _live_t1_chromadb_paths wrapper tests (mocks the ps subprocess).
# ─────────────────────────────────────────────────────────────────────────────


class TestLiveT1ChromadbPaths:
    """Validate _live_t1_chromadb_paths end-to-end with a mocked subprocess."""

    def test_uses_unlimited_width_flag(self, monkeypatch):
        """-ww is load-bearing (PLAN-AUDIT HIGH FINDING) -- assert the
        wrapper actually asks ps for it, not just that some flags are
        passed."""
        import nexus.session as session

        captured_args: list[list[str]] = []

        def fake_check_output(args, **kwargs):
            captured_args.append(args)
            return "  PID COMMAND\n"

        monkeypatch.setattr(session.subprocess, "check_output", fake_check_output)

        session._live_t1_chromadb_paths()
        assert captured_args
        assert "-ww" in captured_args[0]

    def test_returns_parsed_paths(self, monkeypatch):
        import nexus.session as session

        ps_output = (
            "  PID COMMAND\n"
            "  211 chroma run --port 49994 --path /tmp/nx_t1_abcd1234\n"
        )
        monkeypatch.setattr(
            session.subprocess, "check_output", lambda *a, **kw: ps_output
        )
        assert session._live_t1_chromadb_paths() == {"/tmp/nx_t1_abcd1234"}

    def test_fails_open_on_ps_error(self, monkeypatch):
        import subprocess

        import nexus.session as session

        def failing_ps(*a, **kw):
            raise subprocess.CalledProcessError(1, "ps")

        monkeypatch.setattr(session.subprocess, "check_output", failing_ps)
        assert session._live_t1_chromadb_paths() == set()

    def test_fails_open_on_missing_ps_binary(self, monkeypatch):
        import nexus.session as session

        def missing_ps(*a, **kw):
            raise FileNotFoundError("ps")

        monkeypatch.setattr(session.subprocess, "check_output", missing_ps)
        assert session._live_t1_chromadb_paths() == set()


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
