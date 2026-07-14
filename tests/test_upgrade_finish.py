# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4xgfy: process-skew detection + finish-the-upgrade choreography.

Motivated by the 6.7.0/6.7.1 live upgrades: doctor said 'latest' while
every running process executed the old code from memory; the aspect-worker
orphaned to ppid 1 twice in two days; MinerU sat dead in the OOM-risk
fallback until a human noticed. All fixture-driven: `ps` output and dist
metadata are injectable, no real processes are touched.
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.upgrade_finish import (
    SkewReport,
    StaleProcess,
    _parse_etime,
    check_version_transition,
    detect_stale_processes,
    enumerate_processes,
    restart_stale,
)

_PS = """\
  PID ELAPSED COMMAND
  100 01:00:00 /Users/u/.local/share/uv/tools/conexus/bin/python3 /Users/u/.local/bin/nx-mcp
  101 01:00:00 /Users/u/.local/share/uv/tools/conexus/bin/python3 /Users/u/.local/bin/nx-mcp-catalog
  200 2-03:00:00 /Users/u/.local/share/uv/tools/conexus/bin/python3 /Users/u/.local/bin/nx daemon aspect-worker start --config-dir /x
  300 8-00:00:05 /Users/u/.local/share/uv/tools/conexus/bin/mineru-api --host 127.0.0.1
  400 00:05 /usr/bin/vim unrelated.txt
  500 03:00 ps -eo pid,etime,command
"""


class TestEtimeParse:
    def test_forms(self):
        assert _parse_etime("00:05") == 5
        assert _parse_etime("03:00") == 180
        assert _parse_etime("01:00:00") == 3600
        assert _parse_etime("2-03:00:00") == 2 * 86400 + 3 * 3600
        assert _parse_etime("8-00:00:05") == 8 * 86400 + 5


_TOOL_ROOT = "/Users/u/.local/share/uv/tools/conexus/lib/python3.12/site-packages"


def _pin_tool_root():
    from pathlib import Path as _P  # noqa: PLC0415 — file pattern: deferred imports

    return patch("nexus.upgrade_finish._install_root", return_value=_P(_TOOL_ROOT))


class TestEnumerate:
    def test_filters_to_conexus_processes(self):
        with _pin_tool_root():
            procs = enumerate_processes(_PS)
        pids = [p[0] for p in procs]
        assert pids == [100, 101, 200, 300]  # vim + the ps probe excluded

    def test_classification_via_detect(self):
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(1_000_000.0, "6.7.1"),
        ), _pin_tool_root():
            # now = install + 30min: the 1h-old MCP pair and the multi-day
            # daemons all predate the install -> all stale.
            report = detect_stale_processes(_PS, now=1_000_000.0 + 1800)
        kinds = {p.pid: p.kind for p in report.stale}
        assert kinds == {
            100: "mcp-host", 101: "mcp-host",
            200: "aspect-worker", 300: "mineru",
        }
        assert {p.pid for p in report.restartable} == {200, 300}
        assert {p.pid for p in report.session_bound} == {100, 101}

    def test_fresh_processes_are_not_stale(self):
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(1_000_000.0, "6.7.1"),
        ), _pin_tool_root():
            # now = install + 10 days: everything in the fixture STARTED
            # after the install (ages < 10 days except the 8-day mineru...
            # 8d < 10d so started 2 days AFTER install -> fresh).
            report = detect_stale_processes(_PS, now=1_000_000.0 + 10 * 86400)
        assert report.stale == []


class TestRestartStale:
    @staticmethod
    def _report() -> SkewReport:
        r = SkewReport(installed_version="6.7.1")
        r.stale = [
            StaleProcess(pid=200, kind="aspect-worker", command="w", age_s=99),
            StaleProcess(pid=100, kind="mcp-host", command="m", age_s=99),
        ]
        return r

    def test_dry_run_touches_nothing(self):
        with patch("nexus.upgrade_finish.os.kill") as k, \
                patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = restart_stale(self._report(), dry_run=True)
        sp.assert_not_called()
        k.assert_not_called()
        assert any("would restart aspect-worker" in a for a in actions)
        assert any("NEEDS HUMAN: mcp-host" in a for a in actions)

    def test_kills_worker_reports_session_bound(self):
        import signal  # noqa: PLC0415 — file pattern: deferred imports
        from unittest.mock import MagicMock  # noqa: PLC0415 — file pattern: deferred imports

        # Pre-kill re-verification (review 38b7db3d High-3): the probe must
        # see OUR command at that pid, else the kill is skipped.
        probe = MagicMock(returncode=0, stdout=(
            "/u/.local/share/uv/tools/conexus/bin/python3 "
            "/u/.local/bin/nx daemon aspect-worker start\n"
        ))

        calls = []

        def _kill(pid, sig):
            calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # drained on first poll
        with patch("nexus.upgrade_finish.os.kill", side_effect=_kill), \
                patch("nexus.upgrade_finish.time.sleep"), \
                patch("nexus.upgrade_finish.subprocess.run", return_value=probe):
            actions = restart_stale(self._report())
        assert calls[0] == (200, signal.SIGTERM)
        assert any("restarted aspect-worker" in a and "drained" in a for a in actions)
        # MCP hosts are NEVER killed — a live Claude session owns them.
        assert any("pid 100" in a and "NEEDS HUMAN" in a for a in actions)

    def test_mineru_cycle_honors_spawn_policy(self):
        """nexus-c7odl (critique 60ed904e): the AUTOMATED upgrade-finish
        cycle honors mineru_autostart=off — an operator managing the
        server out-of-band owns its staleness too. The explicit verbs
        stay available (the action line says exactly which)."""
        from unittest.mock import MagicMock  # noqa: PLC0415 — file pattern: deferred imports

        r = SkewReport(installed_version="6.7.1")
        r.stale = [StaleProcess(pid=300, kind="mineru", command="mineru-api", age_s=99)]

        with patch("nexus.daemon.mineru_lifecycle.spawn_policy_allows",
                   return_value=False), \
                patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = restart_stale(r)
        sp.assert_not_called()
        assert any("autostart policy is off" in a for a in actions)

        with patch("nexus.daemon.mineru_lifecycle.spawn_policy_allows",
                   return_value=True), \
                patch("nexus.upgrade_finish.subprocess.run",
                      return_value=MagicMock(returncode=0)) as sp:
            actions = restart_stale(r)
        assert sp.call_count == 2  # stop && start
        assert any("cycled MinerU" in a for a in actions)


class TestVersionTransition:
    def test_first_run_stamps_quietly(self, tmp_path):
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ):
            assert check_version_transition(tmp_path) is None
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.7.1"

    def test_same_version_is_silent_noop(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.1\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ):
            assert check_version_transition(tmp_path) is None

    def test_transition_runs_finish_and_summarizes(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        self._tool = patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        )
        self._tool.start()
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ):
            line = check_version_transition(tmp_path)
        self._tool.stop()
        assert line == "upgraded 6.7.0 -> 6.7.1; no stale processes"
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.7.1"

    def test_transition_reports_actions(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        self._tool = patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        )
        self._tool.start()
        r = SkewReport(installed_version="6.7.1")
        r.stale = [StaleProcess(pid=7, kind="mcp-host", command="m", age_s=9)]
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes", return_value=r,
        ):
            line = check_version_transition(tmp_path)
        self._tool.stop()
        assert "NEEDS HUMAN" in line and "6.7.0 -> 6.7.1" in line

    def test_finish_failure_never_blocks_startup(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            side_effect=RuntimeError("ps exploded"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ):
            assert check_version_transition(tmp_path) is None
        # Stamp still advanced: the transition is consumed, not retried
        # forever against a broken probe.
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.7.1"


class TestRecycledPid:
    def test_recycled_pid_is_never_signaled(self):
        """High-3: the pid re-verification sees a DIFFERENT command at the
        snapshot's pid (recycled) — the kill must be skipped."""
        from unittest.mock import MagicMock  # noqa: PLC0415 — file pattern: deferred imports

        r = SkewReport(installed_version="6.8.0")
        r.stale = [StaleProcess(pid=200, kind="aspect-worker", command="w", age_s=9)]
        probe = MagicMock(returncode=0, stdout="/usr/bin/vim innocent.txt\n")
        with patch("nexus.upgrade_finish.os.kill") as k, \
                patch("nexus.upgrade_finish.subprocess.run", return_value=probe):
            actions = restart_stale(r)
        k.assert_not_called()
        assert any("gone or recycled" in a for a in actions)


class TestFailLoud:
    def test_missing_dist_info_raises(self, tmp_path):
        """Critical-1: an unlocatable dist-info must RAISE, never degrade to
        mtime=0.0 (which silently disabled ALL skew detection)."""
        import pytest as _pytest  # noqa: PLC0415 — file pattern: deferred imports
        from unittest.mock import MagicMock  # noqa: PLC0415 — file pattern: deferred imports

        from nexus.upgrade_finish import install_mtime_and_version  # noqa: PLC0415 — file pattern: deferred imports

        dist = MagicMock()
        dist.version = "6.8.0"
        dist.locate_file.return_value = tmp_path  # no dist-info inside
        with patch("importlib.metadata.distribution", return_value=dist), \
                _pytest.raises(RuntimeError, match="dist-info"):
            install_mtime_and_version()

    def test_ps_failure_raises(self):
        """M5: a failed/empty ps must RAISE, never read as zero processes."""
        import pytest as _pytest  # noqa: PLC0415 — file pattern: deferred imports
        from unittest.mock import MagicMock  # noqa: PLC0415 — file pattern: deferred imports

        bad = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("nexus.upgrade_finish.subprocess.run", return_value=bad), \
                _pytest.raises(RuntimeError, match="ps failed"):
            enumerate_processes(None)


class TestCrossVenvGuard:
    def test_dev_venv_never_runs_the_finish_pass(self, tmp_path):
        """Critique 38b7db3d C2: a dev checkout's venv mtime says nothing
        about production processes — the transition consumes the stamp but
        the restart pass never runs from a non-tool interpreter."""
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install",
            return_value=False,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
        ) as detect:
            assert check_version_transition(tmp_path) is None
        detect.assert_not_called()
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.7.1"
