# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P3.1 (nexus-n7u38.23): the two non-data axes as stateless
preconditions.

Package/engine acquisition and process freshness are converged by the
trigger BEFORE the ladder walks — STATELESS: re-derived live each
invocation from ON-DISK state only (provenance sidecar, package metadata,
lease file), never a network/IPC probe of a possibly-crash-looping
process. The lease/marker version fields survive as comparison INPUTS,
never authorities. Duplicate service-cycle triggers coalesce by ordering
+ re-derivation: the engine converge's own cycle brings up a current
supervisor, so the process check (re-run after) skips naturally.
"""
from __future__ import annotations

import inspect
import pathlib
from dataclasses import dataclass
from unittest.mock import patch

from click.testing import CliRunner

import pytest

import nexus.db.migrations as migrations
import nexus.upgrade_ladder.preconditions as pre_mod
from nexus.cli import main
from nexus.commands.upgrade import _converge_preconditions, upgrade
from nexus.upgrade_ladder.preconditions import (
    PreconditionReport,
    check_preconditions,
    converge_preconditions,
)


@dataclass
class _EngineStatus:
    applicable: bool = True
    converged: bool = False
    reason: str | None = "installed engine v0.1.40 != required v0.1.44"


@dataclass
class _Lease:
    version: str = ""


def _names(reports: list[PreconditionReport]) -> list[str]:
    return [r.name for r in reports]


def test_check_covers_the_three_axes(tmp_path: pathlib.Path) -> None:
    reports = check_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
        _lease_fn=lambda: None,
        _installed_version_fn=lambda: "6.12.0",
    )
    assert _names(reports) == ["package", "engine", "process"]
    assert all(r.current for r in reports)  # nothing running, engine converged


def test_check_is_read_only_and_offline(tmp_path: pathlib.Path) -> None:
    """Crash-loop safety: the verdicts derive from injected ON-DISK reads —
    a check can NEVER hang on a dead service (no probe fn is even
    injectable; the seams are a sidecar read, a lease-file read, and
    package metadata)."""
    calls = {"engine": 0, "lease": 0}

    def _engine():
        calls["engine"] += 1
        return _EngineStatus()  # crash-looping engine: sidecar still answers

    def _lease():
        calls["lease"] += 1
        return _Lease(version="6.10.0")

    reports = check_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=_engine,
        _lease_fn=_lease,
        _installed_version_fn=lambda: "6.12.0",
    )
    by = {r.name: r for r in reports}
    assert by["engine"].current is False
    assert "v0.1.44" in by["engine"].detail
    assert by["process"].current is False  # lease 6.10.0 != installed 6.12.0
    assert "6.10.0" in by["process"].detail
    assert calls == {"engine": 1, "lease": 1}  # exactly one on-disk read each


def test_lease_version_is_input_not_authority(tmp_path: pathlib.Path) -> None:
    """The comparison re-derives freshness from lease-vs-installed each
    call — a current lease yields current; the SAME code path with a stale
    lease yields stale. No recorded verdict anywhere."""
    def _report(lease_version: str) -> PreconditionReport:
        reports = check_preconditions(
            config_dir=tmp_path,
            _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
            _lease_fn=lambda: _Lease(version=lease_version),
            _installed_version_fn=lambda: "6.12.0",
        )
        return {r.name: r for r in reports}["process"]

    assert _report("6.12.0").current is True
    assert _report("6.11.0").current is False  # same invocation shape, fresh derivation


def test_unknown_lease_version_fails_toward_stale(tmp_path: pathlib.Path) -> None:
    """The f0pmd divergence, preserved: an empty/legacy lease version cannot
    prove currency — process reads stale (a bounded cycle beats a stale
    supervisor, the #1112/RDR-149 bug class)."""
    reports = check_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
        _lease_fn=lambda: _Lease(version=""),
        _installed_version_fn=lambda: "6.12.0",
    )
    assert {r.name: r for r in reports}["process"].current is False


def test_converge_coalesces_service_cycles(tmp_path: pathlib.Path) -> None:
    """THE coalescing pin: engine converge installs + cycles the service;
    the process check RE-DERIVES afterward (fresh lease read) and must NOT
    cycle again — one stop/start pair per invocation, not two."""
    cycles = {"n": 0}
    lease_state = {"version": "6.10.0"}

    def _engine_converge():
        # converge_engine's install cycles the service; the new supervisor
        # publishes a current lease.
        lease_state["version"] = "6.12.0"
        return ["converged engine (v0.1.40 -> v0.1.44): installed + restarted"]

    def _cycle():
        cycles["n"] += 1

    reports = converge_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(),
        _engine_converge_fn=_engine_converge,
        _lease_fn=lambda: _Lease(version=lease_state["version"]),
        _installed_version_fn=lambda: "6.12.0",
        _cycle_fn=_cycle,
    )
    by = {r.name: r for r in reports}
    assert by["engine"].actions  # engine converged with actions
    assert by["process"].current is True  # re-derived AFTER the engine cycle
    assert cycles["n"] == 0  # coalesced: no second stop/start


def test_converge_cycles_process_when_engine_already_current(
    tmp_path: pathlib.Path,
) -> None:
    """Non-vacuity companion: with a current engine but a stale supervisor,
    the process converge DOES cycle (the coalescing test isn't passing
    because cycling is unreachable)."""
    cycles = {"n": 0}
    lease_state = {"version": "6.10.0"}

    def _cycle():
        cycles["n"] += 1
        lease_state["version"] = "6.12.0"

    reports = converge_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
        _engine_converge_fn=lambda: [],
        _lease_fn=lambda: _Lease(version=lease_state["version"]),
        _installed_version_fn=lambda: "6.12.0",
        _cycle_fn=_cycle,
    )
    assert cycles["n"] == 1
    assert {r.name: r for r in reports}["process"].current is True


def test_failed_engine_cycle_degrades_to_bounded_second_cycle(
    tmp_path: pathlib.Path,
) -> None:
    """P3 critique Medium: the coalescing claim is happy-path. When the
    engine converge's restart FAILS to publish a current lease (stale
    still-alive supervisor), the process step fires its own cycle — a
    BOUNDED second pair, and if THAT also fails to freshen the lease, the
    run reports stale honestly and stops (no in-invocation retry loop)."""
    cycles = {"n": 0}
    lease_state = {"version": "6.10.0"}  # engine converge does NOT freshen it

    reports = converge_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(),
        _engine_converge_fn=lambda: ["attempted engine install; restart failed"],
        _lease_fn=lambda: _Lease(version=lease_state["version"]),
        _installed_version_fn=lambda: "6.12.0",
        _cycle_fn=lambda: cycles.__setitem__("n", cycles["n"] + 1),
    )
    assert cycles["n"] == 1  # exactly one process-step cycle — bounded, no loop
    process = {r.name: r for r in reports}["process"]
    assert process.current is False  # still stale: reported honestly, not retried
    assert "6.10.0" in process.detail


def test_no_running_process_is_current_not_cycled(tmp_path: pathlib.Path) -> None:
    """No lease = nothing running to cycle (upgrade must not auto-spawn) —
    process reads current-by-absence."""
    cycles = {"n": 0}
    reports = converge_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
        _engine_converge_fn=lambda: [],
        _lease_fn=lambda: None,
        _installed_version_fn=lambda: "6.12.0",
        _cycle_fn=lambda: cycles.__setitem__("n", cycles["n"] + 1),
    )
    assert {r.name: r for r in reports}["process"].current is True
    assert cycles["n"] == 0


def test_skip_t3_suppresses_converge_actions_but_still_reports(
    tmp_path: pathlib.Path,
) -> None:
    """P3 review Medium: --skip-t3's fast-T2-only contract gates this
    stage's engine install AND process cycle (verdicts still computed —
    they are sub-ms on-disk reads — only the actions are suppressed)."""
    cycles = {"n": 0}
    installs = {"n": 0}

    def _engine_converge():
        installs["n"] += 1
        return ["installed"]

    reports = converge_preconditions(
        config_dir=tmp_path,
        allow_engine_install=False,   # what --skip-t3 (and --auto) passes
        allow_process_cycle=False,    # what --skip-t3 passes
        _engine_detect_fn=lambda: _EngineStatus(),
        _engine_converge_fn=_engine_converge,
        _lease_fn=lambda: _Lease(version="6.10.0"),
        _installed_version_fn=lambda: "6.12.0",
        _cycle_fn=lambda: cycles.__setitem__("n", cycles["n"] + 1),
    )
    assert installs["n"] == 0
    assert cycles["n"] == 0
    by = {r.name: r for r in reports}
    assert by["engine"].current is False  # still REPORTED honestly
    assert by["process"].current is False


def test_upgrade_trigger_threads_skip_t3_into_both_gates() -> None:
    """Wiring pin, per-gate (P3 validator gap 2): a loose 'not skip_t3' in
    source would still pass if the PROCESS gate alone regressed — the exact
    P3-review Medium. Each gate's own clause must reference the flag."""
    source = inspect.getsource(_converge_preconditions)
    engine_line = next(
        line for line in source.splitlines() if "allow_engine_install=" in line
    )
    process_line = next(
        line for line in source.splitlines() if "allow_process_cycle=" in line
    )
    assert "skip_t3" in engine_line, engine_line
    assert "skip_t3" in process_line, process_line
    assert "auto_mode" in engine_line  # --auto still gates the install


def test_upgrade_command_threads_flags_into_the_precondition_stage(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3 validator gap 1: nothing verified upgrade() actually FORWARDS the
    click flags — a hardcoded skip_t3=False at the call site passed every
    test. Drive the CLI and unpack the call kwargs."""
    migrations._upgrade_done.clear()
    monkeypatch.setenv("NX_MIGRATION_NOTICE", "0")
    with (
        patch("nexus.commands.upgrade._db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.commands.upgrade.T3_UPGRADES", []),
        patch("nexus.commands.upgrade._quiesce_daemon"),
        patch("nexus.commands.upgrade._cycle_supervised_daemons_to_current"),
        patch("nexus.commands.upgrade._stdin_isatty", return_value=False),
        patch("nexus.commands.upgrade._converge_preconditions") as stage,
    ):
        result = CliRunner().invoke(main, ["upgrade", "--skip-t3"])
        assert result.exit_code == 0, result.output
        assert stage.call_args.kwargs == {"auto_mode": False, "skip_t3": True}

        stage.reset_mock()
        migrations._upgrade_done.clear()
        result = CliRunner().invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0, result.output
        assert stage.call_args.kwargs == {"auto_mode": True, "skip_t3": False}


def test_engine_converge_is_skipped_when_already_converged(
    tmp_path: pathlib.Path,
) -> None:
    """P3 validator carry-item (a): the dual-trigger no-op, as BEHAVIOR not
    just census+memo — when the root-level transition finisher already
    converged the engine on this invocation, the precondition stage's
    detect reports converged and the shared converge is never re-invoked."""
    installs = {"n": 0}

    def _engine_converge():
        installs["n"] += 1
        return ["should not happen"]

    reports = converge_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
        _engine_converge_fn=_engine_converge,
        _lease_fn=lambda: None,
        _installed_version_fn=lambda: "6.12.0",
        _cycle_fn=lambda: None,
    )
    assert installs["n"] == 0  # the finisher's work is not redone
    assert {r.name: r for r in reports}["engine"].current is True


def test_trigger_wrapper_never_blocks_on_precondition_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """P3 validator gap 3: the CLI wrapper's own best-effort body was
    untested — a precondition explosion must NOT block the upgrade (the T2
    migration already ran) and must not crash the command."""
    def _boom(**_kwargs):
        raise RuntimeError("sidecar unreadable")

    monkeypatch.setattr(pre_mod, "converge_preconditions", _boom)
    _converge_preconditions(auto_mode=False, skip_t3=False)  # must not raise
    assert "Traceback" not in capsys.readouterr().out


def test_trigger_wrapper_echoes_actions_and_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wrapper's reporting body (P3 validator gap 3): actions echo, a
    non-current axis echoes its pending detail — and --auto stays silent."""
    reports = [
        PreconditionReport("engine", True, True, "converged", ("installed v0.1.44",)),
        PreconditionReport("process", True, False, "supervisor lease 6.10.0 != installed 6.12.0"),
    ]
    monkeypatch.setattr(pre_mod, "converge_preconditions", lambda **_k: reports)

    _converge_preconditions(auto_mode=False, skip_t3=False)
    out = capsys.readouterr().out
    assert "installed v0.1.44" in out
    assert "pending" in out and "6.10.0" in out

    _converge_preconditions(auto_mode=True, skip_t3=False)
    assert capsys.readouterr().out == ""  # --auto: log-only, hook stays quiet


def test_package_reports_installed_version(tmp_path: pathlib.Path) -> None:
    reports = check_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
        _lease_fn=lambda: None,
        _installed_version_fn=lambda: "6.12.0",
    )
    package = {r.name: r for r in reports}["package"]
    assert package.current is True
    assert "6.12.0" in package.detail


def test_unreadable_package_metadata_is_loud(tmp_path: pathlib.Path) -> None:
    reports = check_preconditions(
        config_dir=tmp_path,
        _engine_detect_fn=lambda: _EngineStatus(converged=True, reason=None),
        _lease_fn=lambda: None,
        _installed_version_fn=lambda: "",
    )
    package = {r.name: r for r in reports}["package"]
    assert package.current is False
    assert "unreadable" in package.detail.lower() or "unknown" in package.detail.lower()


def test_upgrade_trigger_wires_preconditions_before_the_walk() -> None:
    """Wiring pin: nx upgrade converges preconditions BEFORE _run_ladder
    (source-order inspection, matching the P0 wiring-pin style)."""
    source = inspect.getsource(upgrade.callback)
    assert "_converge_preconditions(" in source
    assert source.index("_converge_preconditions(") < source.index("_run_ladder(")
