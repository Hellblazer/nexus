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

import pytest

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


def test_upgrade_trigger_threads_skip_t3_into_preconditions() -> None:
    """Wiring pin for the --skip-t3 contract."""
    source = inspect.getsource(_converge_preconditions)
    assert "not skip_t3" in source  # both gates derive from the flag


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
