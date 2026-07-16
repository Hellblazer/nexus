# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P3.1: the two non-data axes as STATELESS preconditions.

Package/engine acquisition and process freshness/provisioning are NOT
ladder rungs (RDR-185 Decision): they are converged by the same trigger
BEFORE the ladder walks, and their freshness is re-derived LIVE at every
invocation as an installed-vs-running/required comparison — never
recorded as independent version state (Gap-4: the second and FINAL
answer mechanism; data rungs' derived ladder position is the first).

Sourcing is ON-DISK ONLY (the Constraints contract): the engine verdict
reads the install-time provenance sidecar (``detect_engine_convergence``
— crash-loop-safe: the incident it fixes is an engine that never answers
``/version``), the process verdict reads the supervisor LEASE FILE
(version field as a comparison INPUT, not an authority), and the package
verdict reads installed package metadata. No network, no IPC.

Coalescing (the f0pmd duplicate-cycle class): converge order is package →
engine → process, and the process verdict is RE-DERIVED after the engine
step — ``converge_engine``'s own install/restart brings up a current
supervisor whose fresh lease already compares equal, so the process step
skips its cycle naturally. One stop/start pair per invocation.

Keyword-only ``_*_fn`` parameters are injectable seams (the
``_cycle_storage_service_to_current`` test convention); defaults
reproduce production behavior.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PreconditionReport:
    """One axis's live verdict. NEVER persisted — that is the point."""

    name: str
    applicable: bool
    current: bool
    detail: str = ""
    actions: tuple[str, ...] = ()


def _default_installed_version() -> str:
    from importlib.metadata import version  # noqa: PLC0415 — deferred; only needed on this path

    try:
        return version("conexus")
    except Exception:  # noqa: BLE001 — an unreadable install must yield a LOUD not-current verdict, not a crash
        return ""


def _default_lease(config_dir: Path) -> Any | None:
    import os  # noqa: PLC0415 — stdlib, branch-local

    from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    registry = ServiceRegistry(dir=config_dir, tier="storage_service")
    return registry.discover(str(os.getuid()))


def _package_report(installed: str) -> PreconditionReport:
    if installed:
        return PreconditionReport(
            name="package",
            applicable=True,
            current=True,
            detail=f"conexus {installed} installed",
        )
    return PreconditionReport(
        name="package",
        applicable=True,
        current=False,
        detail=(
            "installed conexus version unreadable (package metadata probe "
            "failed) — acquisition is the lockstep action's / installer's job"
        ),
    )


def _engine_report(status: Any, *, actions: tuple[str, ...] = ()) -> PreconditionReport:
    current = (not status.applicable) or bool(status.converged)
    return PreconditionReport(
        name="engine",
        applicable=bool(status.applicable),
        current=current,
        detail=getattr(status, "reason", None) or "engine converged",
        actions=actions,
    )


def _process_report(lease: Any | None, installed: str) -> PreconditionReport:
    if lease is None:
        return PreconditionReport(
            name="process",
            applicable=True,
            current=True,
            detail="no supervised service running (nothing to cycle; upgrade never auto-spawns)",
        )
    lease_version = str(getattr(lease, "version", "") or "")
    if lease_version and installed and lease_version == installed:
        return PreconditionReport(
            name="process",
            applicable=True,
            current=True,
            detail=f"supervisor runs installed {installed}",
        )
    # f0pmd divergence preserved: an empty/legacy lease version cannot prove
    # currency — fail TOWARD stale (a bounded cycle beats a stale supervisor).
    return PreconditionReport(
        name="process",
        applicable=True,
        current=False,
        detail=(
            f"supervisor lease version {lease_version or 'unknown'!s} != "
            f"installed {installed or 'unknown'} — stale process"
        ),
    )


def check_preconditions(
    *,
    config_dir: Path,
    _engine_detect_fn: Callable[[], Any] | None = None,
    _lease_fn: Callable[[], Any | None] | None = None,
    _installed_version_fn: Callable[[], str] | None = None,
) -> list[PreconditionReport]:
    """READ-ONLY live verdicts for all three axes (the doctor/dry-run shape).

    Stateless by construction: every call re-reads the sidecar, the lease
    file, and package metadata. Never probes a process.
    """
    from nexus.upgrade_finish import detect_engine_convergence  # noqa: PLC0415 — deferred to avoid import cost

    engine_detect = (
        _engine_detect_fn
        if _engine_detect_fn is not None
        else (lambda: detect_engine_convergence(config_dir))
    )
    lease_fn = _lease_fn if _lease_fn is not None else (lambda: _default_lease(config_dir))
    installed_fn = (
        _installed_version_fn if _installed_version_fn is not None else _default_installed_version
    )

    installed = installed_fn() or ""
    return [
        _package_report(installed),
        _engine_report(engine_detect()),
        _process_report(lease_fn(), installed),
    ]


def converge_preconditions(
    *,
    config_dir: Path,
    allow_engine_install: bool = True,
    allow_process_cycle: bool = True,
    _engine_detect_fn: Callable[[], Any] | None = None,
    _engine_converge_fn: Callable[[], list[str]] | None = None,
    _lease_fn: Callable[[], Any | None] | None = None,
    _installed_version_fn: Callable[[], str] | None = None,
    _cycle_fn: Callable[[], None] | None = None,
) -> list[PreconditionReport]:
    """Converge stale axes in order: package (report-only) → engine →
    process (re-derived AFTER the engine step; see module docstring for
    the coalescing argument).

    ``allow_engine_install=False`` (the ``--auto`` form) prevents a
    REDUNDANT same-invocation engine install: on a version transition the
    root-level finisher (``check_version_transition``, which fires before
    this stage on the same invocation) has already run the single shared
    converge inline. The SessionStart timeout is protected by that path's
    TRANSITION-GATING (at most one install per version bump, idempotent
    skip every other session), not by this flag — see the P3 decision
    addendum (nexus_rdr/185-p3-engine-trigger-duality-decision): one
    mechanism, temporarily two triggers, the second P4-scoped for demotion.

    Coalescing is the HAPPY-PATH property: a failed engine restart (or a
    start that returns a stale still-alive lease) leaves the process
    verdict stale and the process step fires its own BOUNDED second cycle
    — idempotent, never a loop (every step re-derives; nothing retries
    in-invocation).
    """
    from nexus.upgrade_finish import converge_engine, detect_engine_convergence  # noqa: PLC0415 — deferred to avoid import cost

    engine_detect = (
        _engine_detect_fn
        if _engine_detect_fn is not None
        else (lambda: detect_engine_convergence(config_dir))
    )
    engine_converge = (
        _engine_converge_fn
        if _engine_converge_fn is not None
        else (lambda: converge_engine(config_dir))
    )
    lease_fn = _lease_fn if _lease_fn is not None else (lambda: _default_lease(config_dir))
    installed_fn = (
        _installed_version_fn if _installed_version_fn is not None else _default_installed_version
    )

    def _default_cycle() -> None:
        from nexus.commands.upgrade import _cycle_storage_service_to_current  # noqa: PLC0415 — deferred to avoid import cycle

        _cycle_storage_service_to_current()

    cycle_fn = _cycle_fn if _cycle_fn is not None else _default_cycle

    installed = installed_fn() or ""
    reports = [_package_report(installed)]

    engine_status = engine_detect()
    engine_actions: tuple[str, ...] = ()
    if engine_status.applicable and not engine_status.converged and allow_engine_install:
        engine_actions = tuple(engine_converge())
        for line in engine_actions:
            _log.info("precondition_engine_action", action=line)
    reports.append(_engine_report(engine_status, actions=engine_actions))

    # Process: RE-DERIVE after the engine step — a fresh lease read sees the
    # supervisor converge_engine just restarted, coalescing the cycles.
    # allow_process_cycle=False (--skip-t3): verdicts still computed and
    # reported; the cycle action is suppressed per the flag's fast-T2-only
    # contract (P3 review Medium).
    process = _process_report(lease_fn(), installed)
    if not process.current and allow_process_cycle:
        cycle_fn()
        process = _process_report(lease_fn(), installed)  # re-derive post-cycle
        process = PreconditionReport(
            name=process.name,
            applicable=process.applicable,
            current=process.current,
            detail=process.detail,
            actions=("cycled supervised service to the installed version",),
        )
    reports.append(process)
    return reports
