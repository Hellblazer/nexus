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


class TestEnumerate:
    def test_filters_to_conexus_processes(self):
        procs = enumerate_processes(_PS)
        pids = [p[0] for p in procs]
        assert pids == [100, 101, 200, 300]  # vim + the ps probe excluded

    def test_classification_via_detect(self):
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(1_000_000.0, "6.7.1"),
        ):
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
        ):
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
        with patch("nexus.upgrade_finish.os.kill") as k:
            actions = restart_stale(self._report(), dry_run=True)
        k.assert_not_called()
        assert any("would restart aspect-worker" in a for a in actions)
        assert any("NEEDS HUMAN: mcp-host" in a for a in actions)

    def test_kills_worker_reports_session_bound(self):
        with patch("nexus.upgrade_finish.os.kill") as k:
            actions = restart_stale(self._report())
        k.assert_called_once_with(200, 15)
        assert any("restarted aspect-worker" in a for a in actions)
        # MCP hosts are NEVER killed — a live Claude session owns them.
        assert any("pid 100" in a and "NEEDS HUMAN" in a for a in actions)


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
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ):
            line = check_version_transition(tmp_path)
        assert line == "upgraded 6.7.0 -> 6.7.1; no stale processes"
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.7.1"

    def test_transition_reports_actions(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        r = SkewReport(installed_version="6.7.1")
        r.stale = [StaleProcess(pid=7, kind="mcp-host", command="m", age_s=9)]
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes", return_value=r,
        ):
            line = check_version_transition(tmp_path)
        assert "NEEDS HUMAN" in line and "6.7.0 -> 6.7.1" in line

    def test_finish_failure_never_blocks_startup(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            side_effect=RuntimeError("ps exploded"),
        ):
            assert check_version_transition(tmp_path) is None
        # Stamp still advanced: the transition is consumed, not retried
        # forever against a broken probe.
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.7.1"
