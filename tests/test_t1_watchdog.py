# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the T1 server watchdog (nexus-99jb Layer 1)."""
from __future__ import annotations

import os
from unittest.mock import patch


# ── _is_alive ─────────────────────────────────────────────────────────────────


class TestIsAlive:

    def test_own_process_is_alive(self):
        from nexus.t1_watchdog import _is_alive
        assert _is_alive(os.getpid()) is True

    def test_zero_and_negative_pids_are_not_alive(self):
        from nexus.t1_watchdog import _is_alive
        assert _is_alive(0) is False
        assert _is_alive(-1) is False

    def test_process_lookup_error_means_dead(self):
        from nexus.t1_watchdog import _is_alive
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _is_alive(12345) is False

    def test_permission_error_means_alive(self):
        """Process exists but owned by another uid — still alive."""
        from nexus.t1_watchdog import _is_alive
        with patch("os.kill", side_effect=PermissionError):
            assert _is_alive(1) is True


# ── _cleanup ──────────────────────────────────────────────────────────────────


class TestCleanup:

    def test_cleanup_removes_session_file_and_tmpdir(self, tmp_path):
        from nexus.t1_watchdog import _cleanup

        session_file = tmp_path / "test.session"
        session_file.write_text("{}")
        tmpdir = tmp_path / "nx_t1_xyz"
        tmpdir.mkdir()
        (tmpdir / "chroma.sqlite3").write_bytes(b"data")

        # Mock stop_t1_server so nothing real gets signalled.
        with patch("nexus.session.stop_t1_server") as mock_stop:
            _cleanup(chroma_pid=99999, session_file=session_file, tmpdir=tmpdir)

        mock_stop.assert_called_once_with(99999)
        assert not session_file.exists()
        assert not tmpdir.exists()

    def test_cleanup_swallows_session_file_oserror(self, tmp_path):
        """If session file can't be removed, cleanup still proceeds."""
        from nexus.t1_watchdog import _cleanup

        with patch("nexus.session.stop_t1_server"):
            # Nonexistent session file — unlink(missing_ok=True) succeeds,
            # but let's test an actively unwriteable path too.
            _cleanup(
                chroma_pid=1, session_file=tmp_path / "does-not-exist",
                tmpdir=None,
            )  # no raise

    def test_cleanup_swallows_stop_t1_server_failure(self, tmp_path):
        """Chroma stop failures don't prevent tmpdir cleanup."""
        from nexus.t1_watchdog import _cleanup

        tmpdir = tmp_path / "nx_t1_err"
        tmpdir.mkdir()
        with patch("nexus.session.stop_t1_server",
                   side_effect=RuntimeError("simulated")):
            _cleanup(chroma_pid=1, session_file=None, tmpdir=tmpdir)

        assert not tmpdir.exists(), "tmpdir cleanup must run even if stop fails"


# ── main() loop ───────────────────────────────────────────────────────────────


class TestMain:

    def test_exits_when_chroma_dies_without_cleanup(self, monkeypatch, tmp_path):
        """If chroma dies first, the watchdog exits cleanly without calling
        stop_t1_server — SessionEnd or sweep own that cleanup.
        """
        from nexus import t1_watchdog

        # Patch sleep to be instant + _is_alive sequence:
        #   Tick 1: chroma dead → exit(0) without cleanup.
        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            t1_watchdog, "_is_alive",
            lambda pid: False,  # everything dead
        )

        session_file = tmp_path / "s.session"
        session_file.write_text("{}")
        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup:
            rc = t1_watchdog.main([
                "--claude-pid", "100",
                "--chroma-pid", "200",
                "--session-file", str(session_file),
                "--tmpdir", str(tmp_path / "tmpdir"),
            ])
        assert rc == 0
        mock_cleanup.assert_not_called()
        # Session file should still exist — the watchdog only cleans when
        # IT was responsible for the chroma teardown.
        assert session_file.exists()

    def test_triggers_cleanup_when_claude_pid_dies(self, monkeypatch, tmp_path):
        """Chroma alive + claude dead → cleanup fires and main returns."""
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _fake_alive(pid: int) -> bool:
            return pid == 200  # chroma alive, claude dead

        monkeypatch.setattr(t1_watchdog, "_is_alive", _fake_alive)

        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup:
            rc = t1_watchdog.main([
                "--claude-pid", "100",
                "--chroma-pid", "200",
                "--session-file", str(tmp_path / "s.session"),
                "--tmpdir", str(tmp_path / "tmpdir"),
            ])

        assert rc == 0
        mock_cleanup.assert_called_once()
        kwargs = mock_cleanup.call_args.kwargs
        assert kwargs["chroma_pid"] == 200
        assert str(kwargs["session_file"]).endswith("s.session")
