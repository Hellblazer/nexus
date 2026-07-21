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

from nexus.engine_version import REQUIRED_ENGINE_VERSION
from nexus.upgrade_finish import (
    PoisonProbe,
    SkewReport,
    StaleProcess,
    _parse_etime,
    check_version_transition,
    converge_engine,
    detect_engine_convergence,
    detect_stale_processes,
    enumerate_processes,
    heal_diag_view,
    restart_stale,
    unload_stale_t2_launchagent,
)

_REQUIRED_STR = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
_PINNED_TAG = "engine-service-v" + _REQUIRED_STR


def _older_version_str() -> str:
    major, minor, patch = REQUIRED_ENGINE_VERSION
    if patch > 0:
        return f"{major}.{minor}.{patch - 1}"
    if minor > 0:
        return f"{major}.{minor - 1}.999"
    return f"{max(major - 1, 0)}.999.999"

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
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
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

    def test_finish_failure_degrades_one_leg_and_continues(self, tmp_path):
        """nexus-p78a0 rehearsal catch (run 2): a broken process probe used
        to abort the WHOLE finish pass with None — on a ps-less box engine
        convergence and the pending-rung callout silently never ran. The
        probe leg must degrade alone; the later legs (and their callout)
        still fire."""
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            side_effect=RuntimeError("ps exploded"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout",
            return_value=["chash-rekey PENDING — test marker"],
        ):
            line = check_version_transition(tmp_path)
        assert line is not None and "6.7.0 -> 6.7.1" in line
        assert "process-skew detection unavailable" in line
        # The later legs really ran: the callout line made it into the
        # summary despite the first leg's failure.
        assert "chash-rekey PENDING" in line
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

    def test_ps_binary_missing_raises_actionable_runtimeerror(self):
        """nexus-cfgo9: a minimal-container box with no `ps` at all (no
        procps) must raise a CLEAR, actionable RuntimeError -- not let the
        bare FileNotFoundError escape as an unhandled traceback. Found via
        the real --package-upgrade rehearsal (debian:trixie-slim has no
        procps): `nx daemon restart-stale` crashed entirely before engine
        convergence ever ran. Still fail-loud (never silently zero
        processes) -- every caller already degrades this ONE leg gracefully
        on any Exception and continues (restart_stale_cmd,
        check_version_transition, nx doctor's _check_process_skew)."""
        import pytest as _pytest  # noqa: PLC0415 — file pattern: deferred imports

        with patch(
            "nexus.upgrade_finish.subprocess.run",
            side_effect=FileNotFoundError(2, "No such file or directory", "ps"),
        ), _pytest.raises(RuntimeError, match="'ps' command is not available"):
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


class TestDetectEngineConvergence:
    """nexus-cfgo9: the ONE-engine model — an existing local install
    converges its engine binary to REQUIRED_ENGINE_VERSION rather than
    merely refusing a stale one."""

    def _creds(self, tmp_path):
        (tmp_path / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n")

    def test_not_applicable_in_cloud_mode(self, tmp_path):
        self._creds(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=False):
            status = detect_engine_convergence(tmp_path)
        assert status.applicable is False
        assert status.converged is True

    def test_not_applicable_when_service_not_configured(self, tmp_path):
        # No pg_credentials written -- not a service-mode install.
        with patch("nexus.config.is_local_mode", return_value=True):
            status = detect_engine_convergence(tmp_path)
        assert status.applicable is False

    def test_converged_when_installed_matches_required(self, tmp_path):
        self._creds(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value={"version": _REQUIRED_STR},
        ):
            status = detect_engine_convergence(tmp_path)
        assert status.applicable is True
        assert status.converged is True
        assert status.installed_version == REQUIRED_ENGINE_VERSION

    def test_mismatch_when_installed_is_older(self, tmp_path):
        self._creds(tmp_path)
        older = _older_version_str()
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value={"version": older},
        ):
            status = detect_engine_convergence(tmp_path)
        assert status.applicable is True
        assert status.converged is False
        assert status.installed_version == tuple(
            int(p) for p in older.split(".")
        )
        assert older in status.reason
        assert _REQUIRED_STR in status.reason

    def test_mismatch_when_no_provenance_recorded(self, tmp_path):
        self._creds(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value=None,
        ):
            status = detect_engine_convergence(tmp_path)
        assert status.applicable is True
        assert status.converged is False
        assert status.installed_version is None


class TestConvergeEngine:
    """converge_engine: the actual install+cycle action (EngineConvergence)."""

    def _creds(self, tmp_path):
        (tmp_path / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n")

    def _mismatch(self, tmp_path):
        self._creds(tmp_path)
        return patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value={"version": _older_version_str()},
        )

    def test_skips_cleanly_on_match(self, tmp_path):
        self._creds(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value={"version": _REQUIRED_STR},
        ), patch("nexus.daemon.binary_install.install_binary") as install:
            actions = converge_engine(tmp_path)
        assert actions == []
        install.assert_not_called()

    def test_not_applicable_returns_empty(self, tmp_path):
        # No pg_credentials -- not service mode; must not act or report.
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.daemon.binary_install.install_binary"
        ) as install:
            actions = converge_engine(tmp_path)
        assert actions == []
        install.assert_not_called()

    def test_dry_run_reports_without_acting(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_engine(tmp_path, dry_run=True)
        install.assert_not_called()
        sp.assert_not_called()
        assert len(actions) == 1
        assert "would converge" in actions[0]
        assert _REQUIRED_STR in actions[0]

    def test_dry_run_reports_poison_block_not_would_converge(self, tmp_path):
        """code-review LOW: the poison gate must be checked BEFORE the
        dry-run early-return -- a dry-run preview must never promise a
        convergence a real run would actually block. Previously the poison
        check ran only on the non-dry-run path, so `--dry-run` against a
        poisoned store reported 'would converge' when a real run would
        immediately hit NEEDS-HUMAN instead."""
        class _StubPlaybook:
            def terminal_block(self) -> str:
                return "UNBLOCK: remediate chash poison first, see runbook §8.1"

        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe",
                    return_value=PoisonProbe(playbook=_StubPlaybook()),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install:
            actions = converge_engine(tmp_path, dry_run=True)

        install.assert_not_called()
        assert len(actions) == 1
        assert "would converge" not in actions[0]
        assert "would be BLOCKED by chash-poison gate" in actions[0]
        assert "UNBLOCK: remediate chash poison first" in actions[0]

    def test_fires_on_mismatch_installs_pinned_tag_and_cycles_service(self, tmp_path):
        from unittest.mock import MagicMock

        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(tmp_path / "service" / "nexus-service", {"version": _REQUIRED_STR}),
                ) as install, \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ) as sp:
            actions = converge_engine(tmp_path)

        install.assert_called_once()
        called_tag = install.call_args[0][0]
        assert called_tag == _PINNED_TAG
        assert sp.call_count == 2  # stop && start
        assert any("converged engine" in a and _PINNED_TAG in a for a in actions)
        assert any("restarted the storage service" in a for a in actions)

    def test_poison_gate_blocks_and_surfaces_unblock_text(self, tmp_path):
        class _StubPlaybook:
            def terminal_block(self) -> str:
                return "UNBLOCK: remediate chash poison first, see runbook §8.1"

        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe",
                    return_value=PoisonProbe(playbook=_StubPlaybook()),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install:
            actions = converge_engine(tmp_path)

        install.assert_not_called()
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "UNBLOCK: remediate chash poison first" in actions[0]

    def test_install_failure_reports_needs_human_never_raises(self, tmp_path):
        from nexus.daemon.binary_install import BinaryVerificationError

        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    side_effect=BinaryVerificationError("sha256 mismatch"),
                ), \
                patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_engine(tmp_path)

        sp.assert_not_called()
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "sha256 mismatch" in actions[0]

    def test_install_bare_oserror_reports_needs_human_never_raises(self, tmp_path):
        """code-review HIGH: install_binary can raise more than
        BinaryVerificationError -- _atomic_copy (binary_install.py) re-raises
        bare OSError/etc UNWRAPPED on disk-full, permission-denied, or mkdir
        failure. converge_engine's 'never raises' contract must hold for
        EVERY exception, not just the expected one (a narrower catch let
        these escape uncaught -- the exact GH #1402 silent-failure shape in
        the auto path, since the caller's own try/except would swallow the
        propagated exception with zero action line)."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    side_effect=OSError("disk full"),
                ), \
                patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_engine(tmp_path)

        sp.assert_not_called()
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "disk full" in actions[0]

    def test_restart_failure_reports_needs_human_but_install_stands(self, tmp_path):
        from unittest.mock import MagicMock

        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(tmp_path / "service" / "nexus-service", {"version": _REQUIRED_STR}),
                ), \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=1),
                ):
            actions = converge_engine(tmp_path)

        assert any("converged engine" in a for a in actions)
        assert any("NEEDS HUMAN" in a for a in actions)

    # ── nexus-pgdcv: the gate DEFERS when it cannot verify, never blind ──

    def test_unknown_probe_defers_and_never_installs(self, tmp_path):
        """nexus-pgdcv (GH #1414): 'probe cannot run because the service/PG
        is not up yet' is the ORDINARY ordering on a box being converged —
        the old fail-open converged the engine blind exactly then. An
        UNKNOWN verdict now defers with a loud line instead."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe",
                    return_value=PoisonProbe(
                        unknown_reason="Cannot query databasechangelog (psql exit 2)",
                    ),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_engine(tmp_path)

        install.assert_not_called()
        sp.assert_not_called()
        assert len(actions) == 1
        assert "DEFERRED" in actions[0]
        assert "could not verify" in actions[0]
        assert "Cannot query databasechangelog" in actions[0]
        # Round-2 critique HIGH-2: the VERIFIED path (doctor/restart-stale,
        # which re-run this same gate) leads; install-binary is named only
        # for the will-not-boot class. And MEDIUM-2: no passive-retry
        # promise (check_version_transition stamps seen unconditionally).
        assert "nx doctor" in actions[0]
        assert "nx daemon service install-binary" in actions[0]
        assert actions[0].index("nx doctor") < actions[0].index(
            "nx daemon service install-binary"
        )
        assert "re-attempts on the next pass" not in actions[0]
        assert "NEEDS HUMAN" not in actions[0]  # a hold, not a human gate

    def test_unknown_probe_dry_run_reports_would_defer(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe",
                    return_value=PoisonProbe(unknown_reason="service not up"),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install:
            actions = converge_engine(tmp_path, dry_run=True)

        install.assert_not_called()
        assert len(actions) == 1
        assert "would DEFER" in actions[0]
        assert "would converge" not in actions[0]
        assert "service not up" in actions[0]


class TestPoisonProbe:
    """nexus-pgdcv: _poison_probe's tri-state classification of
    _check_migration_state's results — poisoned / clean / unknown must be
    told apart; unknown must NEVER read as clean."""

    def _result(self, label, detail, *, ok=False, fatal=False, warn=False):
        from nexus.health import HealthResult
        return HealthResult(label=label, ok=ok, detail=detail, fatal=fatal, warn=warn)

    def _probe(self, tmp_path, results=None, raises=None):
        from nexus.upgrade_finish import _poison_probe
        if raises is not None:
            cm = patch("nexus.health._check_migration_state", side_effect=raises)
        else:
            cm = patch("nexus.health._check_migration_state", return_value=results)
        with cm:
            return _poison_probe(tmp_path)

    def test_poison_result_yields_playbook(self, tmp_path):
        from nexus.db.chash_tables import POISON_DETAIL_TOKEN
        probe = self._probe(tmp_path, results=[
            self._result(
                "Chunk chash conformance",
                f"12 chunk row(s) have a {POISON_DETAIL_TOKEN} (…)",
                warn=True,
            ),
            self._result("Schema migrations", "ok", ok=True),
        ])
        assert probe.playbook is not None
        assert probe.unknown_reason is None

    def test_probe_could_not_run_warn_is_unknown(self, tmp_path):
        # The token-less conformance WARN is health.py's explicit "the
        # pre-upgrade poison check could NOT run" marker.
        probe = self._probe(tmp_path, results=[
            self._result(
                "Chunk chash conformance",
                "no nexus_diag diagnostic credentials (pre-P2.1 install) — "
                "the pre-upgrade poison check could NOT run.",
                warn=True,
            ),
            self._result("Schema migrations", "ok", ok=True),
        ])
        assert probe.playbook is None
        assert probe.unknown_reason is not None
        assert "nexus_diag" in probe.unknown_reason

    def test_fatal_early_return_is_unknown_not_clean(self, tmp_path):
        # PG unreachable → _check_migration_state early-returns ONE fatal
        # result and the chash leg never runs. Absence of a conformance
        # result must NOT read as clean (the GH #1414 blind spot).
        probe = self._probe(tmp_path, results=[
            self._result(
                "Schema migrations",
                "Cannot query databasechangelog (psql exit 2): connection refused",
                fatal=True,
            ),
        ])
        assert probe.playbook is None
        assert probe.unknown_reason is not None
        assert "Cannot query databasechangelog" in probe.unknown_reason

    def test_exception_is_unknown_never_raises(self, tmp_path):
        probe = self._probe(tmp_path, raises=RuntimeError("probe exploded"))
        assert probe.playbook is None
        assert probe.unknown_reason is not None
        assert "probe exploded" in probe.unknown_reason

    def test_clean_run_is_clean(self, tmp_path):
        probe = self._probe(tmp_path, results=[
            self._result("Schema migrations", "42 applied", ok=True),
        ])
        assert probe.playbook is None
        assert probe.unknown_reason is None

    def test_nongating_debt_warn_does_not_defer(self, tmp_path):
        # "Chash legacy debt" is a DIFFERENT label — non-gating by design
        # (no CHECK constraint exists there); it must not hold convergence.
        probe = self._probe(tmp_path, results=[
            self._result(
                "Chash legacy debt", "7 dangling reference(s)", warn=True,
            ),
            self._result("Schema migrations", "42 applied", ok=True),
        ])
        assert probe.playbook is None
        assert probe.unknown_reason is None

    def test_creds_absent_warn_is_unknown_not_clean(self, tmp_path):
        # Round-2 code-review Low: the creds-absent early return is a
        # not-ok, NON-fatal "Schema migrations" warn — a second caller
        # without detect_engine_convergence's pre-gate must not read it
        # as clean (the chash leg never ran).
        probe = self._probe(tmp_path, results=[
            self._result(
                "Schema migrations",
                "service mode not configured (pg_credentials absent)",
                warn=True,
            ),
        ])
        assert probe.playbook is None
        assert probe.unknown_reason is not None
        assert "service mode not configured" in probe.unknown_reason

    def test_unknown_reason_is_truncated(self, tmp_path):
        # Round-2 critique LOW-1: reasons longer than 200 chars are capped
        # (they render inline in a single action line).
        probe = self._probe(tmp_path, results=[
            self._result("Schema migrations", "x" * 500, fatal=True),
        ])
        assert probe.unknown_reason is not None
        assert len(probe.unknown_reason) <= 200

    def test_label_constant_pins_the_health_wire_format(self):
        # Round-2 critique MEDIUM-1: the label is now a shared constant;
        # pin its VALUE so a rename cannot silently change the doctor's
        # user-facing label (and so both gates and health.py stay coupled
        # through chash_tables, the same home as POISON_DETAIL_TOKEN).
        from nexus.db.chash_tables import CHASH_CONFORMANCE_LABEL
        assert CHASH_CONFORMANCE_LABEL == "Chunk chash conformance"


class TestCheckVersionTransitionEngineConvergence:
    """check_version_transition's automatic post-upgrade pass also runs
    engine convergence (nexus-cfgo9) alongside stale-process restart."""

    def test_transition_includes_convergence_actions(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine",
            return_value=["converged engine: installed engine-service-v9.9.9 (was 1.0.0)"],
        ):
            line = check_version_transition(tmp_path)
        assert "converged engine" in line

    def test_install_oserror_needs_human_line_is_not_silently_absorbed(self, tmp_path):
        """code-review HIGH, end-to-end: drives the REAL converge_engine
        (not mocked) with install_binary raising a bare OSError. Before the
        widened catch, this exception would propagate out of converge_engine,
        be swallowed by check_version_transition's own outer try/except (a
        structlog warning only, no user-visible trace), and the finish
        summary would read as if nothing needed converging -- the exact
        GH #1402 silent-failure shape. After the fix, converge_engine
        catches it internally and returns a NEEDS HUMAN line, which flows
        through into the summary normally."""
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        (tmp_path / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ), patch(
            "nexus.upgrade_finish.heal_diag_view", return_value=[],
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ), patch(
            "nexus.config.is_local_mode", return_value=True,
        ), patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value={"version": _older_version_str()},
        ), patch(
            "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
        ), patch(
            "nexus.daemon.binary_install.install_binary",
            side_effect=OSError("disk full"),
        ):
            line = check_version_transition(tmp_path)
        assert line is not None
        assert "NEEDS HUMAN" in line
        assert "disk full" in line

    def test_convergence_failure_never_blocks_the_finish_summary(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine",
            side_effect=RuntimeError("boom"),
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ):
            line = check_version_transition(tmp_path)
        assert line == "upgraded 6.7.0 -> 6.7.1; no stale processes"


class TestHealDiagView:
    """nexus-cfgo9 (GH #1402 second symptom): the thin
    ``upgrade_finish.heal_diag_view`` wiring around
    ``nexus.db.pg_provision.heal_diag_view_grants_and_ownership`` — grant/
    ownership repair only, unconditional (not gated on engine mismatch)."""

    def _creds(self, tmp_path, port: str = "54321"):
        (tmp_path / "pg_credentials").write_text(f"PG_PORT={port}\n")

    def test_not_applicable_in_cloud_mode(self, tmp_path):
        self._creds(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=False), patch(
            "nexus.db.pg_provision.heal_diag_view_grants_and_ownership"
        ) as heal:
            actions = heal_diag_view(tmp_path)
        assert actions == []
        heal.assert_not_called()

    def test_not_applicable_when_service_not_configured(self, tmp_path):
        # No pg_credentials written -- not a service-mode install.
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.pg_provision.heal_diag_view_grants_and_ownership"
        ) as heal:
            actions = heal_diag_view(tmp_path)
        assert actions == []
        heal.assert_not_called()

    def test_delegates_with_port_and_bootstrap_superuser(self, tmp_path):
        from unittest.mock import MagicMock

        self._creds(tmp_path, port="54321")
        fake_bins = MagicMock()
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.pg_provision.discover_pg_binaries", return_value=fake_bins,
        ), patch(
            "nexus.db.pg_provision.bootstrap_superuser", return_value="hal.hildebrand",
        ), patch(
            "nexus.db.pg_provision.heal_diag_view_grants_and_ownership",
            return_value=["healed: nexus_diag lacked SELECT ..."],
        ) as heal:
            actions = heal_diag_view(tmp_path)

        heal.assert_called_once_with(fake_bins, 54321, "hal.hildebrand")
        assert actions == ["healed: nexus_diag lacked SELECT ..."]

    def test_probe_failure_degrades_to_empty_never_raises(self, tmp_path):
        self._creds(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.pg_provision.discover_pg_binaries",
            side_effect=RuntimeError("no pg binaries"),
        ):
            actions = heal_diag_view(tmp_path)
        assert actions == []

    def test_missing_or_zero_port_is_a_noop(self, tmp_path):
        self._creds(tmp_path, port="0")
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.pg_provision.heal_diag_view_grants_and_ownership"
        ) as heal:
            actions = heal_diag_view(tmp_path)
        assert actions == []
        heal.assert_not_called()


class TestCheckVersionTransitionDiagViewHeal:
    """check_version_transition's finish pass also runs the diag-view heal
    (nexus-cfgo9), independently try/excepted from engine convergence."""

    def test_transition_includes_heal_actions(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine", return_value=[],
        ), patch(
            "nexus.upgrade_finish.heal_diag_view",
            return_value=["healed: nexus.diag_chash_conformance was owned by ..."],
        ):
            line = check_version_transition(tmp_path)
        assert "healed: nexus.diag_chash_conformance" in line

    def test_heal_failure_never_blocks_the_finish_summary(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine", return_value=[],
        ), patch(
            "nexus.upgrade_finish.heal_diag_view",
            side_effect=RuntimeError("boom"),
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ):
            line = check_version_transition(tmp_path)
        assert line == "upgraded 6.7.0 -> 6.7.1; no stale processes"

    def test_engine_convergence_failure_does_not_block_heal_actions(self, tmp_path):
        """The two new legs (converge_engine, heal_diag_view) are
        independently try/excepted -- one failing must not swallow the
        other's actions."""
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.7.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine",
            side_effect=RuntimeError("boom"),
        ), patch(
            "nexus.upgrade_finish.heal_diag_view",
            return_value=["healed: nexus_diag lacked SELECT ..."],
        ):
            line = check_version_transition(tmp_path)
        assert "healed: nexus_diag lacked SELECT" in line


class TestUnloadStaleT2Launchagent:
    """nexus-c0vby (GH #1405 defect 2): service mode must never leave a
    respawning com.nexus.t2 LaunchAgent behind."""

    def test_local_mode_untouched(self, tmp_path):
        """Local mode (or the default env) is not service-backed for
        'memory' -- the T2 daemon IS the live substrate there, so this
        leg must never touch the agent, regardless of whether one is
        installed."""
        from nexus.db.storage_mode import StorageBackend

        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SQLITE,
        ), patch(
            "nexus.commands.daemon._autostart_unit_installed"
        ) as probe, patch(
            "nexus.daemon.installer.uninstall_autostart"
        ) as uninstall:
            actions = unload_stale_t2_launchagent(tmp_path)
        assert actions == []
        probe.assert_not_called()
        uninstall.assert_not_called()

    def test_service_mode_no_agent_installed_is_noop(self, tmp_path):
        from nexus.db.storage_mode import StorageBackend

        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SERVICE,
        ), patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=None,
        ), patch(
            "nexus.daemon.installer.uninstall_autostart"
        ) as uninstall:
            actions = unload_stale_t2_launchagent(tmp_path)
        assert actions == []
        uninstall.assert_not_called()

    def test_service_mode_with_agent_removes_it_and_reports(self, tmp_path):
        from pathlib import Path

        from nexus.daemon.installer import UninstallResult, UninstallStatus
        from nexus.db.storage_mode import StorageBackend

        dest = tmp_path / "com.nexus.t2.plist"
        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SERVICE,
        ), patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=Path(dest),
        ), patch(
            "nexus.daemon.installer.uninstall_autostart",
            return_value=UninstallResult(status=UninstallStatus.REMOVED, dest=dest),
        ) as uninstall:
            actions = unload_stale_t2_launchagent(tmp_path)
        uninstall.assert_called_once_with(tier="t2")
        assert len(actions) == 1
        assert "removed" in actions[0]
        assert "com.nexus.t2" in actions[0]
        assert str(dest) in actions[0]

    def test_service_mode_with_agent_surfaces_warnings(self, tmp_path):
        from pathlib import Path

        from nexus.daemon.installer import UninstallResult, UninstallStatus
        from nexus.db.storage_mode import StorageBackend

        dest = tmp_path / "com.nexus.t2.plist"
        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SERVICE,
        ), patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=Path(dest),
        ), patch(
            "nexus.daemon.installer.uninstall_autostart",
            return_value=UninstallResult(
                status=UninstallStatus.REMOVED, dest=dest,
                warnings=("launchctl bootout gui/501/com.nexus.t2 exited 1: no such process",),
            ),
        ):
            actions = unload_stale_t2_launchagent(tmp_path)
        assert any("removed" in a for a in actions)
        assert any("no such process" in a for a in actions)

    def test_removal_failure_is_needs_human_never_raises(self, tmp_path):
        from nexus.db.storage_mode import StorageBackend

        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SERVICE,
        ), patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=tmp_path / "x.plist",
        ), patch(
            "nexus.daemon.installer.uninstall_autostart",
            side_effect=OSError("permission denied"),
        ):
            actions = unload_stale_t2_launchagent(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "permission denied" in actions[0]

    def test_storage_backend_probe_failure_is_silent_never_raises(self, tmp_path):
        """A malformed NX_STORAGE_BACKEND env var raises
        StorageModeFlagError inside storage_backend_for -- this leg must
        degrade to a no-op, not crash the finish pass (mirrors
        heal_diag_view's probe-failure discipline)."""
        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            side_effect=RuntimeError("bad flag"),
        ):
            actions = unload_stale_t2_launchagent(tmp_path)
        assert actions == []


class TestPendingDataRungCallout:
    """critic-180-cohort finding 2: the auto-converge summary must surface a
    pending chash-rekey rung with its user-facing consequence, not leave it
    to nx doctor alone."""

    def test_chash_rekey_pending_names_the_citation_consequence(self):
        from nexus.upgrade_finish import pending_data_rung_callout

        class _Pending:
            name = "chash-rekey"

            def detect(self):
                from nexus.upgrade_ladder.protocol import RungStatus
                return RungStatus(applicable=True, converged=False,
                                  pending_detail="pending")

        class _NA:
            name = "t2-schema"

            def detect(self):
                from nexus.upgrade_ladder.protocol import RungStatus
                return RungStatus(applicable=False, converged=False)

        with patch("nexus.upgrade_ladder.registry.default_registry",
                   return_value=[_NA(), _Pending()]):
            lines = pending_data_rung_callout()
        assert len(lines) == 1
        assert "chash-rekey PENDING" in lines[0]
        assert "citations" in lines[0]
        assert "nx upgrade" in lines[0]

    def test_detect_crash_degrades_to_no_callout(self):
        from nexus.upgrade_finish import pending_data_rung_callout

        class _Boom:
            name = "chash-rekey"

            def detect(self):
                raise RuntimeError("probe exploded")

        with patch("nexus.upgrade_ladder.registry.default_registry",
                   return_value=[_Boom()]):
            assert pending_data_rung_callout() == []


class TestCheckVersionTransitionLaunchagentUnload:
    """check_version_transition's finish pass also runs the T2 LaunchAgent
    unload (nexus-c0vby), independently try/excepted from the other legs."""

    def test_transition_includes_unload_actions(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.10.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.10.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.10.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine", return_value=[],
        ), patch(
            "nexus.upgrade_finish.heal_diag_view", return_value=[],
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ), patch(
            "nexus.upgrade_finish.unload_stale_t2_launchagent",
            return_value=["removed the stray com.nexus.t2 LaunchAgent: /x.plist"],
        ):
            line = check_version_transition(tmp_path)
        assert "removed the stray com.nexus.t2 LaunchAgent" in line

    def test_unload_failure_never_blocks_the_finish_summary(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.10.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.10.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.10.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine", return_value=[],
        ), patch(
            "nexus.upgrade_finish.heal_diag_view", return_value=[],
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ), patch(
            "nexus.upgrade_finish.unload_stale_t2_launchagent",
            side_effect=RuntimeError("boom"),
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ):
            line = check_version_transition(tmp_path)
        assert line == "upgraded 6.10.0 -> 6.10.1; no stale processes"

    def test_unload_failure_does_not_block_other_legs_actions(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.10.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.10.1"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="6.10.1"),
        ), patch(
            "nexus.upgrade_finish.converge_engine", return_value=[],
        ), patch(
            "nexus.upgrade_finish.heal_diag_view",
            return_value=["healed: nexus_diag lacked SELECT ..."],
        ), patch(
            "nexus.upgrade_finish.unload_stale_t2_launchagent",
            side_effect=RuntimeError("boom"),
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ):
            line = check_version_transition(tmp_path)
        assert "healed: nexus_diag lacked SELECT" in line
