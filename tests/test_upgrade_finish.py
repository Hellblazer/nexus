# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4xgfy: process-skew detection + finish-the-upgrade choreography.

Motivated by the 6.7.0/6.7.1 live upgrades: doctor said 'latest' while
every running process executed the old code from memory; the aspect-worker
orphaned to ppid 1 twice in two days; MinerU sat dead in the OOM-risk
fallback until a human noticed. All fixture-driven: `ps` output and dist
metadata are injectable, no real processes are touched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.engine_version import REQUIRED_ENGINE_VERSION
from nexus.upgrade_finish import (
    PoisonProbe,
    RunningEngine as _RunningEngine,
    SkewReport,
    StaleProcess,
    _parse_etime,
    check_version_transition,
    converge_engine,
    converge_service_autostart_unit,
    detect_engine_convergence,
    detect_stale_processes,
    enumerate_processes,
    heal_diag_view,
    invocation_is_preview,
    restart_stale,
    unload_stale_t2_launchagent,
    unload_stale_service_launchagent,
)

_REQUIRED_STR = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
_PINNED_TAG = "engine-service-v" + _REQUIRED_STR


def _assert_service_cycled(sp) -> None:
    """Assert the stop AND start verbs were actually invoked, by ARGV.

    Replaces a `sp.call_count == 2` count. The count was coupled to how many
    subprocesses the cycle happens to spawn, so adding the nexus-cfgo9
    pre-stop process-table snapshot (a `ps` probe) broke three tests that
    were not about probe counts at all. Argv is what these tests mean, and
    it keeps asserting when the choreography around it changes.
    """
    argvs = [
        c.args[0] for c in sp.call_args_list
        if c.args and isinstance(c.args[0], list)
    ]
    assert ["nx", "daemon", "service", "stop"] in argvs, argvs
    assert ["nx", "daemon", "service", "start"] in argvs, argvs


def _converged_provenance(tmp_path, version: str = _REQUIRED_STR) -> dict:
    """Write a REAL installed binary + return the receipt that backs it.

    nexus-8eaeg: "converged" now means the receipt AND the bytes agree, so a
    fixture that claims convergence has to put a binary on disk. A bare
    ``{"version": ...}`` dict no longer describes a converged box — it
    describes one whose receipt nothing backs, which is a re-acquisition
    trigger (see ``TestNoAcquisitionOnPreviewOrConvergedBox``).
    """
    import hashlib

    svc = tmp_path / "service"
    svc.mkdir(parents=True, exist_ok=True)
    payload = b"native-engine-image"
    (svc / "nexus-service").write_bytes(payload)
    return {
        "version": version,
        "tag": "engine-service-v" + version,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


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
        #
        # THE ROOT IS PINNED AND THE PATH IS REAL-SHAPED (nexus-mjhwk). This
        # check used to consult the hardcoded _PROC_MARKERS while
        # enumerate_processes consulted the layout-derived ones, so this
        # fixture passed on the legacy vocabulary alone -- by coincidence
        # rather than because the marker matched anything the enumerate side
        # would have selected. Both call sites now share _process_markers(),
        # so the command has to sit under the pinned install root the way a
        # real one does. Do not "simplify" this back to a bare /u/ path.
        probe = MagicMock(returncode=0, stdout=(
            "/Users/u/.local/share/uv/tools/conexus/bin/python3 "
            "/Users/u/.local/bin/nx daemon aspect-worker start\n"
        ))

        calls = []

        def _kill(pid, sig):
            calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # drained on first poll
        # process_command reads /proc directly on Linux — a subprocess.run
        # mock never reaches it there (CI-only IndexError, 2026-08-01). The
        # test's subject is the KILL choreography; pin the command-read at
        # its own seam (the transport has its own tests).
        with _pin_tool_root(), \
                patch("nexus.upgrade_finish.os.kill", side_effect=_kill), \
                patch("nexus.upgrade_finish.time.sleep"), \
                patch("nexus.upgrade_finish.process_command",
                      return_value=probe.stdout.strip()), \
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


class TestCheckVersionTransitionBackfillsInstallMode:
    """nexus-g7ijj: the real (non-preview) path best-effort backfills a
    missing ``install.mode`` record, gated on the one-shot stamp claim
    having already won the race and NEVER on the preview path. Verified
    via the call itself (patched at its ``nexus.config`` home, the
    deferred-import call site's resolution point) rather than end-to-end
    file assertions — the surrounding finish-pass legs (engine convergence,
    diag-view heal, launchagent unloads) are exercised for real against a
    bare tmp_path elsewhere in this file and are orthogonal to this hook.
    """

    def test_real_run_calls_the_backfill(self, tmp_path):
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
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ), patch(
            "nexus.config.backfill_install_mode_record",
        ) as backfill:
            line = check_version_transition(tmp_path)
        backfill.assert_called_once_with()
        assert line == "upgraded 6.7.0 -> 6.7.1; no stale processes"

    def test_first_ever_run_also_calls_the_backfill(self, tmp_path):
        """No prior stamp at all: ``check_version_transition`` returns
        early ("nothing stale to finish") right after the stamp claim —
        but an ancient install that upgraded through several releases
        before this stamp mechanism ever existed hits exactly this
        branch on its first post-upgrade invocation, which is precisely
        the never-recorded install.mode case nexus-g7ijj closes. The
        backfill must fire here too, not only on a real version
        transition."""
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.config.backfill_install_mode_record",
        ) as backfill:
            line = check_version_transition(tmp_path)
        assert line is None
        backfill.assert_called_once_with()

    def test_preview_never_calls_the_backfill(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.7.0\n")
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "6.7.1"),
        ), patch(
            "nexus.config.backfill_install_mode_record",
        ) as backfill:
            check_version_transition(tmp_path, preview=True)
        backfill.assert_not_called()

    def test_backfill_failure_never_breaks_the_finish_pass(self, tmp_path):
        """Best-effort posture: a raising backfill degrades quietly and the
        rest of the finish pass still completes and reports normally."""
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
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ), patch(
            "nexus.config.backfill_install_mode_record",
            side_effect=RuntimeError("boom"),
        ):
            line = check_version_transition(tmp_path)
        assert line == "upgraded 6.7.0 -> 6.7.1; no stale processes"
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

    def test_ps_binary_missing_falls_back_to_procfs(self):
        """nexus-cfgo9 follow-up: no `ps` binary is NOT the end of the leg.

        The predecessor raised here, and every caller degrades one leg
        gracefully -- so on a minimal container (debian:trixie-slim, the
        --package-upgrade rehearsal box) process-skew detection simply
        never ran. That is the silent-fallback class: the detection
        written for post-upgrade skew stops running exactly where skew is
        most likely. Linux always mounts /proc, so the ps DEPENDENCY is
        removable rather than merely tolerable."""
        rows = [(4242, 99, "/opt/uv/tools/conexus/bin/python -m nexus.mcp")]
        with patch(
            "nexus.upgrade_finish.subprocess.run",
            side_effect=FileNotFoundError(2, "No such file or directory", "ps"),
        ), patch(
            "nexus.daemon.service_registry._procfs_available", return_value=True,
        ), \
                patch(
                    "nexus.daemon.service_registry._procfs_enumerate",
                    return_value=rows,
                ), patch(
                    "nexus.upgrade_finish._install_root",
                    side_effect=RuntimeError("no dist"),
                ):
            assert enumerate_processes(None) == rows

    def test_no_ps_and_no_procfs_raises_actionable_runtimeerror(self):
        """Fail-loud survives the fallback: a box with NEITHER source must
        still raise, never read as zero processes (review 38b7db3d M5).
        Every caller degrades this ONE leg on any Exception and continues
        (restart_stale_cmd, check_version_transition, nx doctor's
        _check_process_skew)."""
        import pytest as _pytest  # noqa: PLC0415 — file pattern: deferred imports

        with patch(
            "nexus.upgrade_finish.subprocess.run",
            side_effect=FileNotFoundError(2, "No such file or directory", "ps"),
        ), patch(
            "nexus.daemon.service_registry._procfs_available", return_value=False,
        ), _pytest.raises(RuntimeError, match="neither a 'ps' command nor"):
            enumerate_processes(None)

    def test_service_stack_pids_finds_supervisor_and_engine(self, tmp_path):
        """nexus-cfgo9: the convergence restart needs the stack's ground
        truth from the PROCESS TABLE, because the lease -- which is what
        `nx daemon service stop` decides from -- goes invisible on a TTL
        while the processes are still alive and serving."""
        cfg = tmp_path / "nexus"
        rows = [
            (196, 60, f"/v/bin/python3 /v/bin/nx daemon service start "
                      f"--foreground --config-dir {cfg}"),
            (214, 60, f"{cfg}/service/nexus-service -Xmx1g"),
            (153, 60, "/x/pg-bundle/bundle/bin/postgres -D /x/postgres"),
            (900, 5, "/v/bin/python3 /v/bin/nx daemon restart-stale"),
            (901, 5, "/other/config/service/nexus-service"),
        ]
        with patch("nexus.upgrade_finish.all_process_rows", return_value=rows):
            from nexus.upgrade_finish import service_stack_pids  # noqa: PLC0415 — file pattern: deferred imports

            found = service_stack_pids(cfg)
        assert sorted(p for p, _ in found) == [196, 214], (
            "must match exactly the supervisor + engine for THIS config_dir "
            "— never Postgres, never restart-stale itself, never another "
            f"install's engine. Got: {found}"
        )

    def test_prefix_colliding_sibling_profile_is_never_matched(self, tmp_path):
        """Review Critical (2026-08-01): a bare `str(config_dir) in command`
        substring match folded a HEALTHY sibling profile into the kill set
        whenever one profile's path was a string-prefix of another's
        (.config/nexus vs .config/nexus-staging — and --config-dir is the
        DOCUMENTED multi-profile mechanism). The supervisor match must be
        token-exact on the --config-dir argument, both flag spellings."""
        cfg = tmp_path / "nexus"
        sibling = tmp_path / "nexus-staging"
        rows = [
            (196, 60, f"/v/bin/python3 /v/bin/nx daemon service start "
                      f"--foreground --config-dir {cfg}"),
            (214, 60, f"{cfg}/service/nexus-service -Xmx1g"),
            # The prefix-colliding SIBLING profile: alive, healthy, NOT ours.
            (300, 60, f"/v/bin/python3 /v/bin/nx daemon service start "
                      f"--foreground --config-dir {sibling}"),
            (301, 60, f"{sibling}/service/nexus-service -Xmx1g"),
            # --config-dir=<path> spelling, also a sibling.
            (400, 60, f"/v/bin/python3 /v/bin/nx daemon service start "
                      f"--foreground --config-dir={sibling}"),
        ]
        with patch("nexus.upgrade_finish.all_process_rows", return_value=rows):
            from nexus.upgrade_finish import service_stack_pids  # noqa: PLC0415 — file pattern: deferred imports

            found = service_stack_pids(cfg)
            sibling_found = service_stack_pids(sibling)
        assert sorted(p for p, _ in found) == [196, 214], (
            f"prefix-colliding sibling must NEVER enter the kill set: {found}"
        )
        assert sorted(p for p, _ in sibling_found) == [300, 301, 400], (
            "the sibling's own sweep must still find its own stack (incl. "
            f"the --config-dir= spelling): {sibling_found}"
        )

    def test_innocent_lookalike_commands_are_never_matched(self, tmp_path):
        """Critique 21345: substring matching swept innocent processes whose
        command lines merely REFERENCE the paths — an operator tailing the
        engine log or grepping the supervisor spawn line during the exact
        incident this fix targets. Engine = argv[0] exact; supervisor =
        token-exact --config-dir."""
        cfg = tmp_path / "nexus"
        rows = [
            (196, 60, f"/v/bin/python3 /v/bin/nx daemon service start "
                      f"--foreground --config-dir {cfg}"),
            (214, 60, f"{cfg}/service/nexus-service -Xmx1g"),
            (500, 3, f"tail -f {cfg}/service/nexus-service.log"),
            (501, 3, f"cat {cfg}/service/nexus-service"),
            (502, 3, f'grep "daemon service start" {cfg}/logs/storage_service.log'),
        ]
        with patch("nexus.upgrade_finish.all_process_rows", return_value=rows):
            from nexus.upgrade_finish import service_stack_pids  # noqa: PLC0415 — file pattern: deferred imports

            found = service_stack_pids(cfg)
        assert sorted(p for p, _ in found) == [196, 214], (
            f"diagnostic lookalikes must never be swept: {found}"
        )

    def test_restart_and_verify_wiring_reports_the_sweep(self, tmp_path):
        """Critique 21345: the sweep's standalone units were tested but the
        WIRING through _restart_and_verify was not — every converge-level
        test's MagicMock made the before-snapshot silently empty. This pins
        that survivors found by the snapshot surface as [stop-sweep] in the
        action line, one layer up."""
        from nexus import upgrade_finish as uf

        survivors = [(196, "nx daemon service start --foreground "
                           f"--config-dir {tmp_path}")]
        stop_ok = MagicMock(returncode=0, stdout="", stderr="")
        start_ok = MagicMock(returncode=0, stdout="", stderr="")
        running = MagicMock(version="v0.1.60", pid=214)
        # process_state is pinned "S" (running, not a zombie) alongside
        # _pid_alive: this test asserts the SURVIVOR path, and on a
        # /proc-less box process_state would otherwise shell out to `ps`
        # and consume one of subprocess.run's side_effect items below
        # (nexus-o8dil.21).
        with patch.object(uf, "service_stack_pids", return_value=survivors), \
             patch.object(uf, "_pid_alive", return_value=True), \
             patch.object(uf, "process_state", return_value="S"), \
             patch.object(uf, "process_command",
                          return_value=survivors[0][1]), \
             patch.object(uf, "terminate_pids") as term, \
             patch.object(uf.subprocess, "run",
                          side_effect=[stop_ok, start_ok]), \
             patch.object(uf, "_running_engine", return_value=running):
            actions: list[str] = []
            uf._restart_and_verify(tmp_path, actions, "v0.1.60")
        term.assert_called()
        joined = " ".join(actions)
        assert "stop-sweep" in joined, (
            f"the sweep must be visible in the reported actions: {actions}"
        )

    def test_sweep_kills_a_stack_that_survived_stop(self, tmp_path):
        """THE FIX: `nx daemon service stop` reports success having signalled
        nothing when it cannot discover a live lease, and `nx daemon service
        start` is a no-op when it CAN — so the composed restart is a race.
        Verifying the stop against the process table removes it."""
        cfg = tmp_path / "nexus"
        before = [
            (196, f"nx daemon service start --foreground --config-dir {cfg}"),
            (214, f"{cfg}/service/nexus-service -Xmx1g"),
        ]
        killed: list[list[int]] = []
        with patch("nexus.upgrade_finish._pid_alive", return_value=True), \
                patch(
                    "nexus.upgrade_finish.process_command",
                    side_effect=lambda pid: dict(before)[pid],
                ), \
                patch(
                    "nexus.upgrade_finish.terminate_pids",
                    side_effect=lambda pids, **_: killed.append(pids) or [],
                ):
            from nexus.upgrade_finish import _sweep_surviving_stack  # noqa: PLC0415 — file pattern: deferred imports

            note = _sweep_surviving_stack(cfg, before)
        assert killed == [[196], [214]], (
            "supervisor must be signalled BEFORE the engine (PDEATHSIG rides "
            f"the supervisor); got {killed}"
        )
        assert "196" in note and "214" in note and "stop-sweep" in note, note

    def test_sweep_never_signals_a_recycled_pid(self, tmp_path):
        """Pid-recycle TOCTOU (the review 38b7db3d High-3 convention): a pid
        that died in the stop and was instantly reused by an unrelated
        process must NOT be signalled. An unreadable argv is not evidence of
        a recycle and must not skip a genuine survivor."""
        cfg = tmp_path / "nexus"
        before = [
            (196, f"nx daemon service start --foreground --config-dir {cfg}"),
            (214, f"{cfg}/service/nexus-service -Xmx1g"),
        ]
        killed: list[list[int]] = []
        # 196 was recycled into someone's editor; 214's argv is unreadable.
        current = {196: "vim /etc/hosts", 214: ""}
        with patch("nexus.upgrade_finish._pid_alive", return_value=True), \
                patch(
                    "nexus.upgrade_finish.process_command",
                    side_effect=lambda pid: current[pid],
                ), \
                patch(
                    "nexus.upgrade_finish.terminate_pids",
                    side_effect=lambda pids, **_: killed.append(pids) or [],
                ):
            from nexus.upgrade_finish import _sweep_surviving_stack  # noqa: PLC0415 — file pattern: deferred imports

            note = _sweep_surviving_stack(cfg, before)
        assert killed == [[], [214]], (
            "the recycled pid 196 must never be signalled; 214 (unreadable "
            f"argv, still alive) must still be swept. Got {killed}"
        )
        assert "196" not in note, note

    def test_sweep_is_silent_when_stop_actually_stopped(self, tmp_path):
        """No survivors => no note and no signals. The sweep must not narrate
        a healthy cycle, or the line stops meaning anything."""
        cfg = tmp_path / "nexus"
        with patch("nexus.upgrade_finish._pid_alive", return_value=False), \
                patch("nexus.upgrade_finish.terminate_pids") as term:
            from nexus.upgrade_finish import _sweep_surviving_stack  # noqa: PLC0415 — file pattern: deferred imports

            note = _sweep_surviving_stack(cfg, [(196, "x"), (214, "y")])
        assert note == ""
        term.assert_not_called()

    def test_sweep_treats_a_zombie_as_stopped_not_as_a_survivor(self, tmp_path):
        """nexus-o8dil.21. ``_pid_alive`` is ``os.kill(pid, 0)``, which
        SUCCEEDS for a terminated-but-unreaped process. Orphans of a PID 1
        that is not a real init (the package-upgrade MVV container's PID 1
        is a shell script) stay zombies indefinitely, so this sweep
        re-signalled corpses and then narrated the successful stop as
        having "left pid(s) running".

        RED-FIRST: against the pre-fix body (``_pid_alive`` alone) both
        pids are re-terminated and the note is non-empty.
        """
        cfg = tmp_path / "nexus"
        before = [
            (196, f"nx daemon service start --foreground --config-dir {cfg}"),
            (214, f"{cfg}/service/nexus-service -Xmx1g"),
        ]
        killed: list[list[int]] = []
        with patch("nexus.upgrade_finish._pid_alive", return_value=True), \
                patch("nexus.upgrade_finish.process_state", return_value="Z"), \
                patch(
                    "nexus.upgrade_finish.terminate_pids",
                    side_effect=lambda pids, **_: killed.append(pids) or [],
                ):
            from nexus.upgrade_finish import _sweep_surviving_stack  # noqa: PLC0415 — file pattern: deferred imports

            note = _sweep_surviving_stack(cfg, before)
        assert note == "", (
            "a stop that killed the stack and left only unreaped zombies "
            f"succeeded — it must not be narrated as incomplete: {note!r}"
        )
        assert killed == [], (
            f"zombies must not be re-signalled: {killed}"
        )

    def test_sweep_note_never_asserts_an_unobserved_lease_cause(self, tmp_path):
        """The note must report what this function OBSERVED (pids alive
        after the stop returned), never a cause it cannot see.

        The pre-fix strings hardcoded "(its lease-based check saw no live
        lease)" into every note. In the 2026-08-14 package-upgrade MVV that
        invented diagnosis sent the investigation after a cross-version
        lease-discovery gap that did not exist — the same run's stop
        printed "Storage service stopped (pid(s)=...)", which the CLI emits
        ONLY when the lease WAS discovered.
        """
        cfg = tmp_path / "nexus"
        before = [(196, f"nx daemon service start --foreground --config-dir {cfg}")]
        with patch("nexus.upgrade_finish._pid_alive", return_value=True), \
                patch("nexus.upgrade_finish.process_state", return_value="S"), \
                patch(
                    "nexus.upgrade_finish.process_command",
                    return_value=before[0][1],
                ), \
                patch("nexus.upgrade_finish.terminate_pids", return_value=[]):
            from nexus.upgrade_finish import _sweep_surviving_stack  # noqa: PLC0415 — file pattern: deferred imports

            note = _sweep_surviving_stack(cfg, before)
        assert "stop-sweep" in note and "196" in note, note
        assert "lease" not in note.lower(), (
            "the sweep has no visibility into the stop's lease outcome and "
            f"must not claim one: {note!r}"
        )

    def test_procfs_enumerate_parses_a_synthetic_proc_tree(self, tmp_path):
        """The /proc reader itself: NUL-separated cmdline, and an age
        derived from uptime minus starttime (field 22 of stat, indexed
        from the LAST ')' because comm can contain spaces and parens)."""
        import os as _os  # noqa: PLC0415 — file pattern: deferred imports

        hz = _os.sysconf("SC_CLK_TCK") or 100
        (tmp_path / "uptime").write_text("500.00 1000.00\n")
        proc = tmp_path / "1234"
        proc.mkdir()
        (proc / "cmdline").write_bytes(b"/venv/bin/python\x00-m\x00nexus.mcp\x00")
        # comm deliberately contains a space and a ')' — the naive split(3)
        # parse gets the wrong field for this shape.
        after = " ".join(["S"] + ["0"] * 18 + [str(200 * hz)] + ["0"] * 30)
        (proc / "stat").write_text(f"1234 (py (x) thing) {after}\n")
        # A kernel thread: empty cmdline, must be skipped rather than named.
        kt = tmp_path / "2"
        kt.mkdir()
        (kt / "cmdline").write_bytes(b"")
        (kt / "stat").write_text(f"2 (kthreadd) {after}\n")

        with patch("nexus.daemon.service_registry.PROCFS_ROOT", tmp_path):
            from nexus.upgrade_finish import _procfs_enumerate  # noqa: PLC0415 — file pattern: deferred imports

            rows = _procfs_enumerate()
        assert rows == [(1234, 300, "/venv/bin/python -m nexus.mcp")]


class TestPidAliveAmbiguousOSErrorSemantics:
    """nexus-oyo2g round 3 (critique T2 [21510], Significant finding 1).

    The pid_alive consolidation (round 2, finding 3) deleted this module's
    OWN ``_pid_alive`` — which treated any non-ESRCH ``OSError`` as DEAD
    (``except OSError: return False``) — and replaced it with
    ``from nexus.daemon.service_registry import pid_alive as _pid_alive``.
    ``service_registry.pid_alive`` treats an ambiguous (non-ESRCH)
    ``OSError`` as ALIVE instead
    (``except OSError as exc: return exc.errno != errno.ESRCH``). That
    flip silently propagated into THIS module's ``_sweep_surviving_stack``
    -> ``terminate_pids`` path too, on the exact liveness check that
    decides whether ``service_stack_pids``' nexus-cfgo9 convergence sweep
    considers a stack member still running. No prior test in this file
    exercised the ``OSError`` branch in either direction (every patch used
    a blanket ``return_value``), so this pins the semantics explicitly at
    THIS call site — not just the ``service_registry`` primitive's own
    test (``tests/daemon/test_storage_service_daemon.py::
    test_pid_is_alive_is_the_shared_primitive_not_a_local_copy``, which
    only proves identity, not behavior).

    Alive-on-ambiguity is the correct direction for BOTH consumers this
    function serves: ``stop_storage_service``'s termination-verification
    (declaring "dead" on an ambiguous errno risks the exact false-all-clear
    this whole bead exists to close) and this module's ``_sweep_surviving_
    stack``/``restart_stale`` convergence retry (erring toward "still
    alive" makes the sweep retry/escalate rather than falsely declaring an
    early clean exit — the two failure directions are NOT symmetric: a
    missed live process is a data-integrity risk, a redundant kill attempt
    on an already-dead pid is a no-op ``ProcessLookupError``/
    ``PermissionError`` swallow).
    """

    def test_ambiguous_oserror_is_treated_as_alive(self) -> None:
        import errno as _errno
        from nexus.upgrade_finish import _pid_alive
        # errno must be one PEP 3151 maps to NO OSError subclass: EPERM
        # auto-promotes to PermissionError, which the OLD dead-on-ambiguous
        # local copy also special-cased, making the probe vacuous (critique
        # T2 [21510] round 3 falsified exactly that). EIO stays a plain
        # OSError, so only the alive-on-ambiguity semantics pass this.
        with patch("os.kill", side_effect=OSError(_errno.EIO, "ambiguous")):
            assert _pid_alive(12345) is True, (
                "an OSError other than ESRCH must be treated as ALIVE, not "
                "dead — the safe direction for both this module's "
                "convergence-retry use and the primitive's stop-side use"
            )

    def test_esrch_still_means_dead(self) -> None:
        """Contrast case: the fix must not have gone TOO far — a genuine
        'no such process' must still read as dead."""
        import errno as _errno
        from nexus.upgrade_finish import _pid_alive
        with patch("os.kill", side_effect=OSError(_errno.ESRCH, "no such process")):
            assert _pid_alive(12345) is False

    def test_process_lookup_error_still_means_dead(self) -> None:
        from nexus.upgrade_finish import _pid_alive
        with patch("os.kill", side_effect=ProcessLookupError()):
            assert _pid_alive(12345) is False


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
            return_value=_converged_provenance(tmp_path),
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
                ) as sp, \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=_RunningEngine(
                        up=True, version=REQUIRED_ENGINE_VERSION,
                    ),
                ):
            actions = converge_engine(tmp_path)

        install.assert_called_once()
        called_tag = install.call_args[0][0]
        assert called_tag == _PINNED_TAG
        _assert_service_cycled(sp)
        assert any("converged engine" in a and _PINNED_TAG in a for a in actions)
        # nexus-4yf4u: this assertion used to read "restarted the storage
        # service", which the predecessor emitted purely from stop/start both
        # exiting 0. A returncode proves the commands ran, not that the
        # service came up on the new engine, so the success line now carries
        # the OBSERVED running version and the probe is stubbed accordingly.
        assert any("verified running v" + _REQUIRED_STR in a for a in actions)

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


class TestConvergeEngineLiveVerification:
    """nexus-4yf4u (GH #1419 Issue 1): restart-stale must never claim or imply
    progress it did not OBSERVE in the running world.

    Steve Harris ran ``nx daemon restart-stale`` twice against an engine stuck
    at v0.1.49 under conexus 6.16.0 (requires v0.1.51). No error surfaced, no
    forward progress, and ``nx doctor`` kept reporting the same mismatch. By
    elimination over converge_engine's branches — POISONED, install-failure and
    no-pinned-tag all emit loud NEEDS HUMAN lines, and a successful install
    would have moved the provenance sidecar that doctor reads — the only branch
    that yields "no error + no progress + sidecar unmoved" is UNKNOWN/DEFERRED.

    That arm is unbounded and unescalating: ANY store-unverifiability parks
    convergence forever. His service was pegged at 100-290% CPU with PG under
    it, so the store could not be verified, and the deferral text told him to
    wait until "the service is up" — a condition already true on his box.

    The discriminator these tests pin: an unverifiable store means something
    DIFFERENT depending on whether the service is actually up. Down is the
    ordinary ordering (defer). Up-but-not-answering is abnormal (say so loudly).
    Per Hal 2026-07-24 the loud arm does NOT auto-escalate to install-binary —
    the product does not install under an unverifiable store on its own
    initiative; it states the situation and names the escape.
    """

    _OLDER = tuple(int(p) for p in _older_version_str().split("."))

    def _creds(self, tmp_path):
        (tmp_path / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n")

    def _mismatch(self, tmp_path):
        self._creds(tmp_path)
        return patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value={"version": _older_version_str()},
        )

    def _disk_current(self, tmp_path):
        self._creds(tmp_path)
        return patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value=_converged_provenance(tmp_path),
        )

    def _running(self, **kw):
        return _RunningEngine(**kw)

    # ── C1: classify UNKNOWN by whether the service is actually up ──

    def test_unknown_probe_with_service_up_but_unanswerable_is_needs_human(
        self, tmp_path,
    ):
        """THE Steve case. Store unverifiable AND the service is up but not
        answering /version is not an ordinary ordering — it is a wedged box.
        Deferring here is what looped him forever."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe",
                    return_value=PoisonProbe(unknown_reason="psql timeout after 30s"),
                ), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(
                        up=True, version=None, reason="/version timed out",
                    ),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_engine(tmp_path)

        # Loud, and still hands-off: no blind install under an unverifiable store.
        install.assert_not_called()
        sp.assert_not_called()
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "DEFERRED" not in actions[0]
        # It must name the ACTUAL situation, not the already-true "not up yet".
        assert "up" in actions[0]
        assert "psql timeout after 30s" in actions[0]
        assert "/version timed out" in actions[0]
        # The escape is named (not auto-taken).
        assert "nx daemon service install-binary" in actions[0]

    def test_unknown_probe_with_service_down_still_defers(self, tmp_path):
        """Guard against over-correcting: a DOWN service with an unverifiable
        store is the genuinely ordinary ordering (nexus-pgdcv / GH #1414) and
        must stay a soft deferral, not become a false alarm on every box that
        simply has not started its service yet."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe",
                    return_value=PoisonProbe(unknown_reason="service not up"),
                ), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(up=False, version=None),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install:
            actions = converge_engine(tmp_path)

        install.assert_not_called()
        assert len(actions) == 1
        assert "DEFERRED" in actions[0]
        assert "NEEDS HUMAN" not in actions[0]

    def test_unknown_probe_with_service_up_reports_the_running_version(
        self, tmp_path,
    ):
        """When the service answers but the store is unverifiable, the deferral
        stands — but it must REPORT what is actually running. The old text
        described a condition ("once the service is up") that was already true,
        which is what made the loop unreadable."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe",
                    return_value=PoisonProbe(unknown_reason="changelog unreadable"),
                ), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(up=True, version=self._OLDER),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install:
            actions = converge_engine(tmp_path)

        install.assert_not_called()
        assert len(actions) == 1
        assert "DEFERRED" in actions[0]
        assert _older_version_str() in actions[0]
        assert "running" in actions[0].lower()

    # ── C2: success is OBSERVED, never inferred from a returncode ──

    def test_restart_that_does_not_converge_is_needs_human_not_success(
        self, tmp_path,
    ):
        """`stop.returncode == 0 and start.returncode == 0` proves the commands
        exited cleanly, NOT that the restarted service came up on the new
        engine. Claiming convergence from a returncode is the same fail-quiet
        class one layer down."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(
                        tmp_path / "service" / "nexus-service",
                        {"version": _REQUIRED_STR},
                    ),
                ), \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(up=True, version=self._OLDER),
                ):
            actions = converge_engine(tmp_path)

        # The install genuinely happened and is reported.
        assert any("converged engine" in a for a in actions)
        # But convergence was NOT observed, so it must not be claimed.
        assert any("NEEDS HUMAN" in a for a in actions)
        assert not any("restarted the storage service to pick up" in a for a in actions)
        assert any(_older_version_str() in a for a in actions)

    def test_restart_verified_at_required_version_reports_converged(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(
                        tmp_path / "service" / "nexus-service",
                        {"version": _REQUIRED_STR},
                    ),
                ), \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(
                        up=True, version=REQUIRED_ENGINE_VERSION,
                    ),
                ):
            actions = converge_engine(tmp_path)

        assert any("converged engine" in a for a in actions)
        assert not any("NEEDS HUMAN" in a for a in actions)
        assert any(_REQUIRED_STR in a and "verified" in a for a in actions)

    # ── C1b: disk is right but the PROCESS is stale ──

    def test_disk_current_but_running_stale_restarts_without_installing(
        self, tmp_path,
    ):
        """The sidecar answers "what is on disk", which is the right question
        for the crash-loop case it was chosen for — but it is the WRONG sole
        answer to "is the running system converged". A correct on-disk binary
        with a stale live process needs a restart, not a reinstall, and today
        returns [] as "already converged"."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._disk_current(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ) as sp, \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    side_effect=[
                        self._running(up=True, version=self._OLDER),
                        self._running(up=True, version=REQUIRED_ENGINE_VERSION),
                    ],
                ):
            actions = converge_engine(tmp_path)

        install.assert_not_called()          # disk is already correct
        _assert_service_cycled(sp)
        assert actions                       # never a silent "already converged"
        assert any(_older_version_str() in a for a in actions)
        assert not any("NEEDS HUMAN" in a for a in actions)

    # ── review CRE-A finding 1 (High): the OTHER converged sub-cases ──
    #
    # C1b originally acted only on strictly-older and let every other
    # sub-case fall into a bare `return []`, which the CLI renders as
    # "converged". Three of those four sub-cases had NOT observed the
    # running engine at all, so the reassurance was unearned — the same
    # defect class as the bug under repair, one layer down. None of them
    # was covered by a test, which is why it took a reviewer to find.

    def test_disk_current_and_service_down_stays_silent(self, tmp_path):
        """A STOPPED service is an ordinary state, not an alarm: the on-disk
        binary governs the next start, so there is nothing to converge and
        nothing to say. This pins the one sub-case that may stay silent, so a
        future fix for the noisy ones cannot over-correct into false alarms."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._disk_current(tmp_path), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.upgrade_finish.subprocess.run") as sp, \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(up=False, version=None),
                ):
            actions = converge_engine(tmp_path)

        assert actions == []
        install.assert_not_called()
        sp.assert_not_called()

    def test_disk_current_but_service_up_and_unanswerable_is_not_silent(
        self, tmp_path,
    ):
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._disk_current(tmp_path), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.upgrade_finish.subprocess.run") as sp, \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(
                        up=True, version=None, reason="/version timed out",
                    ),
                ):
            actions = converge_engine(tmp_path)

        # Must NOT read as converged, and must not act on its own either.
        assert actions
        assert any("UNVERIFIED" in a for a in actions)
        assert any("/version timed out" in a for a in actions)
        install.assert_not_called()
        sp.assert_not_called()

    def test_running_newer_than_required_is_reported_never_downgraded(
        self, tmp_path,
    ):
        """A running engine NEWER than the release dependency is not converged
        either — but restarting into the on-disk binary would silently
        DOWNGRADE it, so this reports and stops."""
        major, minor, patchv = REQUIRED_ENGINE_VERSION
        newer = (major, minor, patchv + 1)

        with patch("nexus.config.is_local_mode", return_value=True), \
                self._disk_current(tmp_path), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.upgrade_finish.subprocess.run") as sp, \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(up=True, version=newer),
                ):
            actions = converge_engine(tmp_path)

        assert actions
        assert any("NEWER" in a for a in actions)
        assert any(".".join(str(p) for p in newer) in a for a in actions)
        install.assert_not_called()
        sp.assert_not_called()      # never an automatic downgrade

    def test_c1b_restart_is_manual_only_never_from_the_auto_trigger(
        self, tmp_path,
    ):
        """Critic CRITIC-C Significant 3 + Hal decision 2026-07-24.

        C1b bounces a live storage service. It is reachable from BOTH the
        manual `nx daemon restart-stale` AND `check_version_transition`, the
        unattended pass that runs on the first `nx <anything>` after a version
        change — a sub-case that used to be a hard no-op, so the set of
        conditions producing an autonomous restart had grown without ever
        being weighed against D2 ("the product does not take disruptive
        action on its own initiative").

        Steve's OWN GH #1419 Issue 3b is that cycling the service under
        legitimate load severs a client mid-batch. An unattended bounce
        landing concurrent with a long index IS that failure. So the
        unattended path REPORTS and points at the manual verb; it does not
        act.
        """
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._disk_current(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.upgrade_finish.subprocess.run") as sp, \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(up=True, version=self._OLDER),
                ):
            actions = converge_engine(tmp_path, unattended=True)

        sp.assert_not_called()               # THE point: no autonomous bounce
        install.assert_not_called()
        assert len(actions) == 1
        assert "NEEDS HUMAN" not in actions[0]          # nothing is broken
        assert _older_version_str() in actions[0]       # names what is running
        assert "nx daemon restart-stale" in actions[0]  # names the manual verb

    def test_c1b_restart_still_fires_on_the_manual_path(self, tmp_path):
        """The other side of the same contract — the guard must not disarm
        C1b for the human who explicitly asked for convergence."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._disk_current(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ) as sp, \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    side_effect=[
                        self._running(up=True, version=self._OLDER),
                        self._running(up=True, version=REQUIRED_ENGINE_VERSION),
                    ],
                ):
            actions = converge_engine(tmp_path)     # default: attended

        _assert_service_cycled(sp)
        assert any("verified running" in a for a in actions)

    # ── review CRE-A finding 2 (Medium): the settle-poll loop itself ──

    def test_settle_poll_retries_until_the_service_publishes_its_version(
        self, tmp_path,
    ):
        """The bounded poll exists because a freshly started service does not
        publish its lease instantly. Every other test resolves on the FIRST
        post-restart probe, so the loop body never ran under test."""
        probes = [
            self._running(up=True, version=None, reason="lease not published yet"),
            self._running(up=True, version=None, reason="lease not published yet"),
            self._running(up=True, version=REQUIRED_ENGINE_VERSION),
        ]
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(
                        tmp_path / "service" / "nexus-service",
                        {"version": _REQUIRED_STR},
                    ),
                ), \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ), \
                patch("nexus.upgrade_finish.time.sleep") as slept, \
                patch(
                    "nexus.upgrade_finish._running_engine", side_effect=probes,
                ) as probe:
            actions = converge_engine(tmp_path)

        assert probe.call_count == 3          # the loop actually iterated
        assert slept.call_count >= 1          # and it waited between probes
        assert any("verified running v" + _REQUIRED_STR in a for a in actions)
        assert not any("NEEDS HUMAN" in a for a in actions)

    def test_settle_poll_exhausted_reports_unverified_never_success(
        self, tmp_path,
    ):
        """When the budget runs out with the version still unknown, the
        catch-all must say UNVERIFIED — never claim the convergence."""
        with patch("nexus.config.is_local_mode", return_value=True), \
                self._mismatch(tmp_path), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(
                        tmp_path / "service" / "nexus-service",
                        {"version": _REQUIRED_STR},
                    ),
                ), \
                patch(
                    "nexus.upgrade_finish.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ), \
                patch("nexus.upgrade_finish.time.sleep"), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=self._running(
                        up=True, version=None, reason="never came back",
                    ),
                ):
            actions = converge_engine(tmp_path)

        assert any("converged engine" in a for a in actions)   # the install is a fact
        assert any("UNVERIFIED" in a for a in actions)
        assert any("NEEDS HUMAN" in a for a in actions)
        assert not any("verified running" in a for a in actions)


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


class TestUnloadStaleServiceLaunchagent:
    """nexus-6bmph (RDR-183 residual): a managed/cloud box must never keep a
    respawning com.nexus.service unit (the t2-leg sibling)."""

    def test_local_mode_untouched(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed") as probe, \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall:
            actions = unload_stale_service_launchagent(tmp_path)
        assert actions == []
        probe.assert_not_called()
        uninstall.assert_not_called()

    def test_nonlocal_no_unit_is_noop(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=False), \
             patch("nexus.commands.daemon._service_autostart_unit_installed",
                   return_value=None), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall:
            actions = unload_stale_service_launchagent(tmp_path)
        assert actions == []
        uninstall.assert_not_called()

    def test_nonlocal_with_unit_removes_and_reports(self, tmp_path):
        from pathlib import Path as _P

        from nexus.daemon.installer import UninstallResult, UninstallStatus

        dest = tmp_path / "com.nexus.service.plist"
        with patch("nexus.config.is_local_mode", return_value=False), \
             patch("nexus.commands.daemon._service_autostart_unit_installed",
                   return_value=_P(dest)), \
             patch("nexus.daemon.installer.uninstall_autostart",
                   return_value=UninstallResult(status=UninstallStatus.REMOVED, dest=dest)) as uninstall:
            actions = unload_stale_service_launchagent(tmp_path)
        uninstall.assert_called_once_with(tier="service")
        assert len(actions) == 1
        assert "removed" in actions[0]
        assert str(dest) in actions[0]

    def test_removal_failure_is_needs_human_never_raises(self, tmp_path):
        from pathlib import Path as _P

        dest = tmp_path / "com.nexus.service.plist"
        with patch("nexus.config.is_local_mode", return_value=False), \
             patch("nexus.commands.daemon._service_autostart_unit_installed",
                   return_value=_P(dest)), \
             patch("nexus.daemon.installer.uninstall_autostart",
                   side_effect=RuntimeError("bootout exploded")):
            actions = unload_stale_service_launchagent(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "nx daemon service uninstall --autostart" in actions[0]


class TestConvergeServiceAutostartUnit:
    """nexus-rlp0v: a drifted local-mode service-tier autostart unit (e.g. a
    stale ProcessType=Background) must converge on `nx daemon restart-stale`
    without a human hand-editing ~/Library/LaunchAgents, and must never
    bounce the service from the unattended/dry-run paths."""

    def _dest(self, tmp_path):
        from pathlib import Path as _P  # noqa: PLC0415 — local import, test-only convenience
        return _P(tmp_path) / "com.nexus.service.plist"

    def test_nonlocal_mode_untouched(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=False), \
             patch("nexus.commands.daemon._service_autostart_unit_installed") as probe, \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall, \
             patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_service_autostart_unit(tmp_path)
        assert actions == []
        probe.assert_not_called()
        uninstall.assert_not_called()
        sp.assert_not_called()

    def test_no_unit_installed_is_noop(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=None), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall:
            actions = converge_service_autostart_unit(tmp_path)
        assert actions == []
        uninstall.assert_not_called()

    # ── code-review round 1, Critical: a genuine probe FAILURE must never
    # collapse into the same [] a benign not-applicable result returns. Each
    # probe step gets its own case so a regression back to one blanket
    # except-and-swallow is caught regardless of which step it happens on.

    def test_is_local_mode_raises_is_needs_human_never_silent(self, tmp_path):
        with patch("nexus.config.is_local_mode", side_effect=RuntimeError("config unreadable")), \
             patch("nexus.commands.daemon._service_autostart_unit_installed") as probe, \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall:
            actions = converge_service_autostart_unit(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "config unreadable" in actions[0]
        probe.assert_not_called()
        uninstall.assert_not_called()

    def test_unit_lookup_raises_is_needs_human_never_silent(self, tmp_path):
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed",
                   side_effect=RuntimeError("platform detection exploded")), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall:
            actions = converge_service_autostart_unit(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "platform detection exploded" in actions[0]
        uninstall.assert_not_called()

    def test_render_raises_is_needs_human_never_silent(self, tmp_path):
        """The reviewer's reproduction case: installer.rendered_unit_content
        (formerly the private _render_for) raising used to be swallowed by
        one blanket except and reported as [] — indistinguishable from
        'already up to date'."""
        dest = self._dest(tmp_path)
        dest.write_text("some content\n")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content",
                   side_effect=RuntimeError("render blew up")), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall:
            actions = converge_service_autostart_unit(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "render blew up" in actions[0]
        uninstall.assert_not_called()

    def test_dest_read_raises_is_needs_human_never_silent(self, tmp_path):
        """dest exists per the probe but a read failure (permissions, TOCTOU
        unlink) must ALSO surface loudly, not read as 'no unit installed'."""
        from pathlib import Path as _P  # noqa: PLC0415 — local import, test-only convenience

        dest = _P("/nonexistent/does/not/exist/com.nexus.service.plist")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "new content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall:
            actions = converge_service_autostart_unit(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        uninstall.assert_not_called()

    def test_content_matches_is_noop(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.write_text("same content\n")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "same content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall, \
             patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_service_autostart_unit(tmp_path)
        assert actions == []
        uninstall.assert_not_called()
        sp.assert_not_called()

    def _drifted(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.write_text("old content with ProcessType Background\n")
        return dest

    def test_drift_unattended_reports_note_never_mutates(self, tmp_path):
        dest = self._drifted(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "new content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall, \
             patch("nexus.daemon.installer.install_autostart") as install, \
             patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_service_autostart_unit(tmp_path, unattended=True)
        assert len(actions) == 1
        assert "NOTE" in actions[0] and "restart-stale" in actions[0]
        uninstall.assert_not_called()
        install.assert_not_called()
        sp.assert_not_called()

    def test_drift_dry_run_reports_note_never_mutates(self, tmp_path):
        dest = self._drifted(tmp_path)
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "new content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall, \
             patch("nexus.daemon.installer.install_autostart") as install, \
             patch("nexus.upgrade_finish.subprocess.run") as sp:
            actions = converge_service_autostart_unit(tmp_path, dry_run=True)
        assert len(actions) == 1
        assert "NOTE" in actions[0]
        uninstall.assert_not_called()
        install.assert_not_called()
        sp.assert_not_called()

    def test_drift_attended_stops_reinstalls_and_verifies(self, tmp_path):
        from nexus.daemon.installer import (  # noqa: PLC0415 — local import, test-only convenience
            InstallResult, InstallStatus, UninstallResult, UninstallStatus,
        )

        dest = self._drifted(tmp_path)
        stop_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "new content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart",
                   return_value=UninstallResult(status=UninstallStatus.REMOVED, dest=dest)) as uninstall, \
             patch("nexus.daemon.installer.install_autostart",
                   return_value=InstallResult(
                       status=InstallStatus.NEWLY_INSTALLED, dest=dest,
                       detail="Activated via: launchctl bootstrap ...",
                   )) as install, \
             patch("nexus.upgrade_finish.subprocess.run", return_value=stop_result) as sp, \
             patch("nexus.upgrade_finish._running_engine",
                   return_value=_RunningEngine(up=True, version=(1, 2, 3))):
            actions = converge_service_autostart_unit(tmp_path)

        sp.assert_called_once_with(
            ["nx", "daemon", "service", "stop"],
            capture_output=True, text=True, timeout=60,
        )
        uninstall.assert_called_once_with(tier="service")
        install.assert_called_once_with(tier="service")
        assert len(actions) == 1
        assert "converged" in actions[0]
        assert str(dest) in actions[0]
        assert "NEEDS HUMAN" not in actions[0]

    def test_stop_failure_is_needs_human_never_mutates_unit(self, tmp_path):
        dest = self._drifted(tmp_path)
        stop_result = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "new content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart") as uninstall, \
             patch("nexus.daemon.installer.install_autostart") as install, \
             patch("nexus.upgrade_finish.subprocess.run", return_value=stop_result):
            actions = converge_service_autostart_unit(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        uninstall.assert_not_called()
        install.assert_not_called()

    def test_install_raises_is_needs_human_never_raises(self, tmp_path):
        dest = self._drifted(tmp_path)
        stop_result = MagicMock(returncode=0, stdout="", stderr="")
        from nexus.daemon.installer import UninstallResult, UninstallStatus  # noqa: PLC0415 — local import, test-only convenience
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "new content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart",
                   return_value=UninstallResult(status=UninstallStatus.REMOVED, dest=dest)), \
             patch("nexus.daemon.installer.install_autostart",
                   side_effect=RuntimeError("bootstrap exploded")), \
             patch("nexus.upgrade_finish.subprocess.run", return_value=stop_result):
            actions = converge_service_autostart_unit(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "bootstrap exploded" in actions[0]

    def test_service_unanswerable_after_restart_is_needs_human(self, tmp_path):
        from nexus.daemon.installer import (  # noqa: PLC0415 — local import, test-only convenience
            InstallResult, InstallStatus, UninstallResult, UninstallStatus,
        )

        dest = self._drifted(tmp_path)
        stop_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content", return_value=(dest, "new content\n")), \
             patch("nexus.daemon.installer.uninstall_autostart",
                   return_value=UninstallResult(status=UninstallStatus.REMOVED, dest=dest)), \
             patch("nexus.daemon.installer.install_autostart",
                   return_value=InstallResult(status=InstallStatus.NEWLY_INSTALLED, dest=dest, detail="ok")), \
             patch("nexus.upgrade_finish.subprocess.run", return_value=stop_result), \
             patch("nexus.upgrade_finish._running_engine",
                   return_value=_RunningEngine(up=False, version=None, reason="no lease")), \
             patch("nexus.upgrade_finish.time.sleep"):
            actions = converge_service_autostart_unit(tmp_path)
        assert len(actions) == 1
        assert "NEEDS HUMAN" in actions[0]
        assert "converged the storage-service autostart unit" in actions[0]


class TestUnloadStaleT2Launchagent:
    """nexus-c0vby (GH #1405 defect 2): no box may be left with a respawning
    com.nexus.t2 LaunchAgent behind."""

    def test_local_mode_ALSO_removed_after_the_daemon_retired(self, tmp_path):
        """CONTRACT FLIPPED by nexus-i711w Stage 2 sub-stage B.

        This test used to assert the OPPOSITE — that local mode was left
        untouched — and it was right to: the T2 daemon was the live substrate
        on a SQLite-mode box, so its LaunchAgent was legitimate there, and a
        local `nx daemon t2 install --autostart` round-trip was expected to
        keep recreating it.

        The daemon is retired. No box of any storage mode can start one, and
        none can reinstall the unit, so a surviving unit is stale EVERYWHERE —
        it fires `nx daemon t2 start`, a command that no longer exists, on
        every boot forever. Keeping the service-mode gate would have left
        exactly the SQLite-mode boxes — the ones most likely to be carrying a
        unit — unfixed.
        """
        from pathlib import Path

        from nexus.daemon.installer import UninstallResult, UninstallStatus

        dest = tmp_path / "com.nexus.t2.plist"
        # The =sqlite backend no longer exists to pin (RDR-158 P3), so the
        # "ALSO in local mode" claim is expressed as its non-vacuity guard:
        # a re-introduced storage-mode gate would have to CONSULT the
        # resolver, so assert nothing does.
        with patch(
            "nexus.db.storage_mode.storage_backend_for",
        ) as backend, patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=Path(dest),
        ), patch(
            "nexus.daemon.installer.uninstall_autostart",
            return_value=UninstallResult(status=UninstallStatus.REMOVED, dest=dest),
        ) as uninstall:
            actions = unload_stale_t2_launchagent(tmp_path)
        backend.assert_not_called()
        uninstall.assert_called_once_with(tier="t2")
        assert len(actions) == 1
        assert "com.nexus.t2" in actions[0]

    def test_no_agent_installed_is_noop(self, tmp_path):
        """The probe, not the storage mode, is what gates the removal now."""
        with patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=None,
        ), patch(
            "nexus.daemon.installer.uninstall_autostart"
        ) as uninstall:
            actions = unload_stale_t2_launchagent(tmp_path)
        assert actions == []
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

    def test_detect_crash_surfaces_as_unavailable_not_silently_dropped(self):
        """nexus-v2mdd: a raising detect() used to be indistinguishable from
        'not pending' (`except Exception: continue`), silently deleting the
        chash-rekey PENDING warning whose own docstring says citations for
        pre-existing content silently break without it. It must instead
        surface as an explicit unknown/unavailable line naming the rung
        and the failure — never a bare []."""
        from nexus.upgrade_finish import pending_data_rung_callout

        class _Boom:
            name = "chash-rekey"

            def detect(self):
                raise RuntimeError("probe exploded")

        with patch("nexus.upgrade_ladder.registry.default_registry",
                   return_value=[_Boom()]):
            lines = pending_data_rung_callout()
        assert len(lines) == 1
        assert "chash-rekey" in lines[0]
        assert "unknown" in lines[0].lower() or "unavailable" in lines[0].lower()
        assert "probe exploded" in lines[0]

    def test_registry_construction_failure_surfaces_as_unavailable_not_silently_dropped(self):
        """nexus-jgac3: v2mdd fixed the INNER per-rung `except Exception:
        continue` (a raising detect()) but left this function's OWN
        default_registry() call -- the thing that FEEDS the for-loop --
        completely unguarded. A raising default_registry() (deferred
        imports, registry construction) must degrade to an explicit
        unavailable line, the same shape v2mdd already established for a
        raising detect(), not propagate out of this function at all."""
        from nexus.upgrade_finish import pending_data_rung_callout

        with patch(
            "nexus.upgrade_ladder.registry.default_registry",
            side_effect=RuntimeError("registry construction exploded"),
        ):
            lines = pending_data_rung_callout()
        assert len(lines) == 1
        assert "unknown" in lines[0].lower() or "unavailable" in lines[0].lower()
        assert "registry construction exploded" in lines[0]


class TestCheckVersionTransitionPendingDataRungRegistryFailure:
    """nexus-jgac3: drives the REAL pending_data_rung_callout (not mocked)
    with default_registry() raising. Before the fix, this exception would
    propagate out of pending_data_rung_callout, be swallowed by
    check_version_transition's own outer try/except (a structlog warning
    only, no user-visible trace) -- the finish summary would read as if
    nothing were pending, exactly the GH #1402-shaped silent-failure this
    bead is a live instance of. After the fix, pending_data_rung_callout
    catches it internally and returns an explicit unavailable line, which
    flows through into the summary normally, same as every other leg."""

    def test_registry_construction_failure_surfaces_in_finish_summary_not_silently_absorbed(
        self, tmp_path,
    ):
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
            "nexus.upgrade_finish.heal_diag_view", return_value=[],
        ), patch(
            "nexus.upgrade_ladder.registry.default_registry",
            side_effect=RuntimeError("registry construction exploded"),
        ):
            line = check_version_transition(tmp_path)
        assert line is not None
        assert "unknown" in line.lower() or "unavailable" in line.lower()
        assert "registry construction exploded" in line


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


class TestPoisonProbeSelfBackfill:
    """GH #1414 era-hop regression, part 2 (2026-07-21): a pre-P2.1 install
    has no nexus_diag credentials, so the tri-state probe read UNKNOWN and
    converge_engine deferred FOREVER on exactly the unattended-upgrade boxes
    RDR-185 exists for — with `nx doctor` (the advertised re-attempt)
    failing identically (no operator step in the walk ever backfills the
    role). The probe now self-heals first: when the creds file carries no
    NX_DB_DIAG_* keys, it invokes the idempotent RDR-182 P2.1 backfill
    (best-effort, resolution-gated on the LIVE local cluster facts) before
    classifying. Defer semantics are untouched — a store that STILL cannot
    be verified after the backfill attempt remains UNKNOWN."""

    def test_creds_absent_triggers_backfill_then_probes(self, tmp_path, monkeypatch):
        from nexus import upgrade_finish as uf

        calls: list[str] = []
        (tmp_path / "pg_credentials").write_text("PG_PORT=5432\n")  # no NX_DB_DIAG_*

        monkeypatch.setattr(
            "nexus.db.diag_connection.resolve_diag_credentials",
            lambda creds_path=None: None,
        )
        monkeypatch.setattr(
            "nexus.db.pg_provision.backfill_diag_role_best_effort",
            lambda: calls.append("backfill") or True,
        )
        monkeypatch.setattr(
            "nexus.health._check_migration_state",
            lambda creds_path=None: calls.append("probe") or [],
        )

        probe = uf._poison_probe(tmp_path)

        assert calls == ["backfill", "probe"], (
            "creds-absent must attempt the idempotent diag backfill BEFORE "
            "the probe runs (unattended-convergence contract, RDR-185)"
        )
        assert probe.playbook is None and probe.unknown_reason is None  # CLEAN

    def test_creds_present_skips_backfill(self, tmp_path, monkeypatch):
        from nexus import upgrade_finish as uf

        calls: list[str] = []

        monkeypatch.setattr(
            "nexus.db.diag_connection.resolve_diag_credentials",
            lambda creds_path=None: object(),  # creds resolve fine
        )
        monkeypatch.setattr(
            "nexus.db.pg_provision.backfill_diag_role_best_effort",
            lambda: calls.append("backfill") or True,
        )
        monkeypatch.setattr(
            "nexus.health._check_migration_state",
            lambda creds_path=None: calls.append("probe") or [],
        )

        uf._poison_probe(tmp_path)
        assert calls == ["probe"], "no backfill when diag creds already resolve"

    def test_backfill_failure_still_probes_and_stays_unknown(self, tmp_path, monkeypatch):
        """The backfill is best-effort: a failed attempt must not crash the
        probe, and the creds-still-absent WARN keeps reading UNKNOWN (the
        defer semantics stay intact when self-heal cannot help)."""
        from nexus import upgrade_finish as uf
        from nexus.db.chash_tables import CHASH_CONFORMANCE_LABEL
        from nexus.health import HealthResult

        monkeypatch.setattr(
            "nexus.db.diag_connection.resolve_diag_credentials",
            lambda creds_path=None: None,
        )
        monkeypatch.setattr(
            "nexus.db.pg_provision.backfill_diag_role_best_effort",
            lambda: False,
        )
        monkeypatch.setattr(
            "nexus.health._check_migration_state",
            lambda creds_path=None: [HealthResult(
                label=CHASH_CONFORMANCE_LABEL, ok=False,
                detail="no nexus_diag diagnostic credentials (pre-P2.1 install) — could NOT run",
                warn=True,
            )],
        )

        probe = uf._poison_probe(tmp_path)
        assert probe.unknown_reason is not None
        assert "nexus_diag" in probe.unknown_reason


class TestBackfillDiagRoleBestEffort:
    """Resolution-first guards (f2c07c58 lesson): gate on the LIVE local
    cluster facts (creds file present + PG_PORT), never on a mode guess."""

    def test_no_creds_file_returns_false(self, tmp_path, monkeypatch):
        from nexus.db import pg_provision as pp

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        assert pp.backfill_diag_role_best_effort() is False

    def test_no_port_returns_false(self, tmp_path, monkeypatch):
        from nexus.db import pg_provision as pp

        (tmp_path / "pg_credentials").write_text("NX_DB_ADMIN_USER=x\n")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        assert pp.backfill_diag_role_best_effort() is False

    def test_happy_path_calls_backfill_and_returns_true(self, tmp_path, monkeypatch):
        from nexus.db import pg_provision as pp

        (tmp_path / "pg_credentials").write_text("PG_PORT=54321\n")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(pp, "discover_pg_binaries", lambda: "BINS")
        monkeypatch.setattr(pp, "bootstrap_superuser", lambda: "os_user")
        seen: dict = {}

        def _fake_backfill(bins, port, os_user, creds_path):
            seen.update(bins=bins, port=port, os_user=os_user, creds_path=creds_path)

        monkeypatch.setattr(pp, "_backfill_diag_role", _fake_backfill)
        assert pp.backfill_diag_role_best_effort() is True
        assert seen["port"] == 54321
        assert seen["bins"] == "BINS"

    def test_exception_is_swallowed_returns_false(self, tmp_path, monkeypatch):
        from nexus.db import pg_provision as pp

        (tmp_path / "pg_credentials").write_text("PG_PORT=54321\n")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            pp, "discover_pg_binaries",
            lambda: (_ for _ in ()).throw(RuntimeError("no bins")),
        )
        assert pp.backfill_diag_role_best_effort() is False


class TestNoAcquisitionOnPreviewOrConvergedBox:
    """nexus-8eaeg: the ACQUISITION seam pins.

    Observed live on the first 7.0.0 run: `nx upgrade --dry-run` held a 4-5
    minute HTTPS pull of the ~190 MB engine release asset and persisted
    nothing. Two independent properties are pinned here, both stated as
    "the downloader is never invoked" rather than "the summary looks right",
    because the summary was fine in the incident — it was the socket that
    was not:

    1. a DRY RUN plans and never acquires (any box, converged or not);
    2. a WET run on a converged box whose receipt is BACKED BY THE BYTES
       does not re-acquire either — and one whose receipt is NOT backed
       (missing/corrupt binary) does, loudly.

    The seam asserted is ``binary_install.install_binary``: everything below
    it opens the network, nothing above it does.
    """

    def _creds(self, tmp_path):
        (tmp_path / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n")

    def _receipt(self, tmp_path, version: str, *, payload: bytes = b"engine",
                 sha: str | None = None, place_binary: bool = True):
        """Write a real sidecar + (optionally) a real binary on disk."""
        import hashlib
        import json

        svc = tmp_path / "service"
        svc.mkdir(parents=True, exist_ok=True)
        if place_binary:
            (svc / "nexus-service").write_bytes(payload)
        digest = sha if sha is not None else hashlib.sha256(payload).hexdigest()
        (svc / "nexus-service.meta.json").write_text(
            json.dumps({
                "version": version,
                "tag": "engine-service-v" + version,
                "sha256": digest,
            })
        )

    def test_dry_run_on_a_converged_box_acquires_nothing(self, tmp_path):
        self._creds(tmp_path)
        self._receipt(tmp_path, _REQUIRED_STR)
        with patch("nexus.config.is_local_mode", return_value=True), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=_RunningEngine(up=False, version=None),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.daemon.binary_install._download") as dl:
            actions = converge_engine(tmp_path, dry_run=True)
        install.assert_not_called()
        dl.assert_not_called()
        assert actions == []

    def test_dry_run_on_a_stale_box_plans_the_acquisition_without_doing_it(
        self, tmp_path,
    ):
        """The --dry-run promise, stated positively: the pending acquisition
        is REPORTED as an action line, and the downloader is untouched."""
        self._creds(tmp_path)
        self._receipt(tmp_path, _older_version_str())
        with patch("nexus.config.is_local_mode", return_value=True), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.daemon.binary_install._download") as dl:
            actions = converge_engine(tmp_path, dry_run=True)
        install.assert_not_called()
        dl.assert_not_called()
        assert len(actions) == 1
        assert actions[0].startswith("would converge engine")
        assert _older_version_str() in actions[0]
        assert _REQUIRED_STR in actions[0]

    def test_wet_run_on_a_converged_verified_box_does_not_reacquire(self, tmp_path):
        """The severity question: does a plain `nx upgrade` re-pull 190 MB on
        a box that is already right? It must not — and the verification that
        earns that skip is LOCAL (receipt digest vs the installed file)."""
        self._creds(tmp_path)
        self._receipt(tmp_path, _REQUIRED_STR)
        with patch("nexus.config.is_local_mode", return_value=True), \
                patch(
                    "nexus.upgrade_finish._running_engine",
                    return_value=_RunningEngine(up=False, version=None),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.daemon.binary_install._download") as dl:
            actions = converge_engine(tmp_path)
        install.assert_not_called()
        dl.assert_not_called()
        assert actions == []

    def test_wet_run_on_a_version_mismatch_does_acquire(self, tmp_path):
        """The inverse pin — the skip above must be earned, not universal."""
        self._creds(tmp_path)
        self._receipt(tmp_path, _older_version_str())
        with patch("nexus.config.is_local_mode", return_value=True), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(tmp_path / "service" / "nexus-service",
                                  {"version": _REQUIRED_STR}),
                ) as install, \
                patch(
                    "nexus.upgrade_finish._restart_and_verify",
                    return_value=["<restart stubbed>"],
                ):
            actions = converge_engine(tmp_path)
        install.assert_called_once()
        assert install.call_args.args[0] == _PINNED_TAG
        assert actions == ["<restart stubbed>"]

    def test_wet_run_reacquires_when_the_receipt_is_not_backed_by_the_bytes(
        self, tmp_path,
    ):
        """FAIL LOUD, not fail silent: a receipt claiming the required version
        with NO binary behind it used to read as converged forever. It is a
        re-acquisition trigger, and the reason is named."""
        self._creds(tmp_path)
        self._receipt(tmp_path, _REQUIRED_STR, place_binary=False)
        with patch("nexus.config.is_local_mode", return_value=True), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(tmp_path / "service" / "nexus-service",
                                  {"version": _REQUIRED_STR}),
                ) as install, \
                patch(
                    "nexus.upgrade_finish._restart_and_verify",
                    return_value=["<restart stubbed>"],
                ):
            actions = converge_engine(tmp_path)
        install.assert_called_once()
        assert actions == ["<restart stubbed>"]

    def test_dry_run_reports_an_unbacked_receipt_instead_of_reacquiring(
        self, tmp_path,
    ):
        self._creds(tmp_path)
        self._receipt(tmp_path, _REQUIRED_STR, place_binary=False)
        with patch("nexus.config.is_local_mode", return_value=True), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch("nexus.daemon.binary_install.install_binary") as install, \
                patch("nexus.daemon.binary_install._download") as dl:
            actions = converge_engine(tmp_path, dry_run=True)
        install.assert_not_called()
        dl.assert_not_called()
        assert len(actions) == 1
        assert "would converge engine" in actions[0]
        assert "unverified" in actions[0]
        assert "missing" in actions[0]

    def test_corrupt_installed_binary_is_reacquired_on_a_wet_run(self, tmp_path):
        self._creds(tmp_path)
        self._receipt(tmp_path, _REQUIRED_STR, payload=b"engine",
                      sha="0" * 64)
        with patch("nexus.config.is_local_mode", return_value=True), \
                patch(
                    "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
                ), \
                patch(
                    "nexus.daemon.binary_install.install_binary",
                    return_value=(tmp_path / "service" / "nexus-service",
                                  {"version": _REQUIRED_STR}),
                ) as install, \
                patch(
                    "nexus.upgrade_finish._restart_and_verify",
                    return_value=["<restart stubbed>"],
                ):
            actions = converge_engine(tmp_path)
        install.assert_called_once()


class TestInvocationIsPreview:
    """nexus-8eaeg: the root-group finish trigger has no other way to learn
    that the invocation promised to change nothing — ``--dry-run`` is a
    SUBCOMMAND flag and Click's group callback runs before it is parsed
    (verified against click 8.3: ``ctx.args``/``ctx.protected_args`` are both
    empty at group-callback time)."""

    def test_dry_run_token_is_a_preview(self):
        assert invocation_is_preview(["upgrade", "--dry-run"]) is True

    def test_ordinary_invocation_is_not_a_preview(self):
        assert invocation_is_preview(["upgrade"]) is False
        assert invocation_is_preview(["search", "dry run of the release"]) is False

    def test_mcp_style_argv_is_not_a_preview(self):
        assert invocation_is_preview([]) is False


class TestCheckVersionTransitionPreview:
    """The end-to-end shape of the incident: a version transition + a
    ``--dry-run`` invocation. Nothing may be acquired, nothing restarted, and
    — the part that makes the deferral safe — the one-shot version stamp must
    NOT be consumed, so the next ordinary invocation still finishes for real.
    """

    def _transitioning(self, tmp_path):
        (tmp_path / "last_seen_version").write_text("6.18.1\n")
        (tmp_path / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n")

    def test_preview_plans_acquires_nothing_and_leaves_the_stamp(self, tmp_path):
        self._transitioning(tmp_path)
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "7.0.0"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="7.0.0"),
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
        ) as install, patch(
            "nexus.daemon.binary_install._download",
        ) as dl, patch(
            "nexus.upgrade_finish.heal_diag_view",
        ) as heal, patch(
            "nexus.upgrade_finish.unload_stale_service_launchagent",
        ) as unload:
            line = check_version_transition(tmp_path, preview=True)

        install.assert_not_called()
        dl.assert_not_called()
        heal.assert_not_called()
        unload.assert_not_called()
        assert line is not None
        assert "PREVIEW ONLY" in line
        assert "would converge engine" in line
        # The transition is still owed — the stamp was not consumed.
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.18.1"

    def test_the_wet_pass_still_converges_after_a_preview(self, tmp_path):
        """The deferral must be a deferral, not a cancellation."""
        self._transitioning(tmp_path)
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "7.0.0"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="7.0.0"),
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ), patch(
            "nexus.upgrade_finish.heal_diag_view", return_value=[],
        ), patch(
            "nexus.upgrade_finish.unload_stale_t2_launchagent", return_value=[],
        ), patch(
            "nexus.upgrade_finish.unload_stale_service_launchagent", return_value=[],
        ), patch(
            "nexus.config.is_local_mode", return_value=True,
        ), patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value={"version": _older_version_str()},
        ), patch(
            "nexus.upgrade_finish._poison_probe", return_value=PoisonProbe(),
        ), patch(
            "nexus.daemon.binary_install.install_binary",
            return_value=(tmp_path / "service" / "nexus-service",
                          {"version": _REQUIRED_STR}),
        ) as install, patch(
            "nexus.upgrade_finish._restart_and_verify", return_value=["cycled"],
        ):
            check_version_transition(tmp_path, preview=True)
            install.assert_not_called()
            line = check_version_transition(tmp_path, preview=False)

        install.assert_called_once()
        assert "converged" not in (line or "") or "cycled" in (line or "")
        assert (tmp_path / "last_seen_version").read_text().strip() == "7.0.0"

    def test_preview_defaults_from_argv(self, tmp_path, monkeypatch):
        """No explicit ``preview=`` at the ``nexus/cli.py`` call site — the
        default must come from the process's own argv, or the incident
        returns."""
        self._transitioning(tmp_path)
        monkeypatch.setattr("sys.argv", ["nx", "upgrade", "--dry-run"])
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "7.0.0"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="7.0.0"),
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
        ) as install, patch(
            "nexus.daemon.binary_install._download",
        ) as dl:
            line = check_version_transition(tmp_path)

        install.assert_not_called()
        dl.assert_not_called()
        assert "PREVIEW ONLY" in line
        assert (tmp_path / "last_seen_version").read_text().strip() == "6.18.1"

    def test_stale_processes_are_only_named_in_preview_never_restarted(
        self, tmp_path,
    ):
        """The /proc-vs-subprocess mock seam gotcha this file documents makes
        a real restart hard to observe; assert on ``restart_stale``'s own
        dry_run contract instead — the finish pass must hand it through."""
        self._transitioning(tmp_path)
        with patch(
            "nexus.upgrade_finish.install_mtime_and_version",
            return_value=(0.0, "7.0.0"),
        ), patch(
            "nexus.upgrade_finish.running_from_tool_install", return_value=True,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(
                installed_version="7.0.0",
                stale=[
                    StaleProcess(
                        pid=200, kind="aspect-worker", command="x", age_s=99999,
                    )
                ],
            ),
        ), patch(
            "nexus.upgrade_finish.converge_engine", return_value=[],
        ), patch(
            "nexus.upgrade_finish.pending_data_rung_callout", return_value=[],
        ), patch(
            "nexus.upgrade_finish.os.kill",
        ) as kill:
            line = check_version_transition(tmp_path, preview=True)

        kill.assert_not_called()
        assert "would restart aspect-worker (pid 200)" in line
