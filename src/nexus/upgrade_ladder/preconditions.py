# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P3.1 + P4.0b: the non-data axes as STATELESS preconditions.

Package/engine ACQUISITION, service-stack PROVISIONING (P4.0b,
nexus-6nmrc — the guided-upgrade stage-2 job: a legacy footprint needs a
target to migrate INTO), and process freshness are NOT ladder rungs
(RDR-185 Decision): they are converged by the same trigger
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
engine → provisioning → process, and the process verdict is RE-DERIVED
after the engine step — ``converge_engine``'s own install/restart brings up a current
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


def _default_plugin_version() -> str | None:
    """Installed conexus plugin version — the identity a session's
    ``CLAUDE_PLUGIN_ROOT`` ultimately resolves to. ``None`` when no conexus
    plugin install is detectable (no Claude Code on this box, or the plugin
    is not installed). On-disk only, per the module contract.

    Delegates to :func:`nexus.health._installed_conexus_plugin_versions`
    (reviewer Medium, nexus-2a5ij fold): that reader already tolerates both
    ``installed_plugins.json`` schema variants and multi-entry plugins — a
    third reimplementation here is the nexus-1si7z "held together by
    cross-referencing" drift class. Multiple installed versions resolve to
    the highest (best-effort numeric ordering)."""
    from nexus.health import _installed_conexus_plugin_versions  # noqa: PLC0415 — deferred to avoid import cycle

    versions = _installed_conexus_plugin_versions()
    if not versions:
        return None

    def _key(v: str) -> tuple:
        try:
            return tuple(int(p) for p in v.split("."))
        except ValueError:
            return (-1,)

    return max(versions, key=_key)


def _default_lockstep_marker() -> str | None:
    """Last CLI version the RDR-143 lockstep action CONFIRMED, read from the
    SAME marker file the SessionStart hook reads
    (``~/.config/nexus/cli_lockstep_marker``; ``NX_LOCKSTEP_MARKER``
    overrides for tests, mirroring the hook). ``None`` = never confirmed or
    unreadable — a comparison INPUT, never an authority."""
    import os  # noqa: PLC0415 — stdlib, deferred with the module's on-disk-read convention

    override = os.environ.get("NX_LOCKSTEP_MARKER")
    marker = (
        Path(override) if override
        else Path.home() / ".config" / "nexus" / "cli_lockstep_marker"
    )
    try:
        text = marker.read_text().strip()
    except OSError:
        return None
    return text or None


def _lockstep_report(
    plugin_version: str | None, marker_version: str | None
) -> PreconditionReport:
    """RDR-185 P3.0 / nexus-2a5ij: hooks/plugin lockstep freshness as a named
    STATELESS precondition — the comparison INPUTS are the installed plugin
    version and the RDR-143 lockstep marker, the same on-disk pair the
    SessionStart hook reads. Report-only by design: NO new mechanism, no
    converge action ever — acquisition is the lockstep hook/action's job,
    fired at the next SessionStart."""
    name = "plugin-lockstep"
    if plugin_version is None:
        return PreconditionReport(
            name=name, applicable=False, current=True,
            detail="no conexus plugin install detected (Claude Code absent "
                   "or plugin not installed)",
        )
    if marker_version is None:
        return PreconditionReport(
            name=name, applicable=True, current=False,
            detail=(
                f"plugin {plugin_version} installed but no lockstep marker — "
                "CLI lockstep unconfirmed (the SessionStart hook confirms it "
                "on the next Claude session)"
            ),
        )
    if marker_version != plugin_version:
        return PreconditionReport(
            name=name, applicable=True, current=False,
            detail=(
                f"plugin {plugin_version} vs last-confirmed CLI "
                f"{marker_version} — upgrade in flight (the RDR-143 lockstep "
                "action converges it at the next Claude session)"
            ),
        )
    return PreconditionReport(
        name=name, applicable=True, current=True,
        detail=f"plugin and CLI lockstep confirmed at {plugin_version}",
    )


def _converge_provisioning(
    provisioned_fn: Callable[[], bool],
    footprint_fn: Callable[[], bool],
    establish_fn: Callable[[], Any],
    *,
    allow: bool,
) -> PreconditionReport:
    provisioned = provisioned_fn()
    footprint = footprint_fn()
    if provisioned or not footprint or not allow:
        return _provisioning_report(provisioned, footprint)
    try:
        readiness = establish_fn()
    except Exception as exc:  # noqa: BLE001 — a provisioning failure is REPORTED (the walk then finds no verified target and the substrate rung stays pending), never a crash of the trigger
        _log.warning("precondition_provisioning_failed", error=str(exc))
        return _provisioning_report(
            False, footprint, detail=f"provisioning failed: {exc}"
        )
    if not getattr(readiness, "ready", False):
        reason = getattr(readiness, "reason", "") or "service not ready"
        return _provisioning_report(
            False, footprint, detail=f"service not ready — NOT migrating: {reason}"
        )
    url = getattr(readiness, "service_url", "")
    return _provisioning_report(
        True, footprint,
        detail=f"service stack provisioned and verified at {url}",
        actions=(f"provisioned + verified the service stack at {url}",),
    )


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


def _default_provisioned(config_dir: Path) -> bool:
    """ON-DISK evidence that a service stack exists for this install: the
    pg_credentials provisioning artifact (written once by nx init /
    guided-upgrade) or a configured service_url credential. File-level
    only — never a probe of a possibly-down service."""
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid import cycle
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred, circular-dep avoidance

    if (get_credential("service_url") or "").strip():
        return True
    return (config_dir / CREDENTIALS_FILENAME).exists()


def _default_footprint() -> bool:
    from nexus.upgrade_ladder.census import _chroma_footprint_present  # noqa: PLC0415 — deferred to avoid import cost

    return _chroma_footprint_present()


def _default_establish() -> Any:
    from nexus.upgrade_ladder.provisioning import establish_verified_service  # noqa: PLC0415 — deferred to avoid import cost (P0e rehome: the surviving home)

    return establish_verified_service()


def _provisioning_report(
    provisioned: bool, footprint: bool, *, actions: tuple[str, ...] = (), detail: str = ""
) -> PreconditionReport:
    """The ACQUISITION verdict for the service stack (P4.0b, nexus-6nmrc).

    N/A when there is no legacy footprint to migrate: a fresh user must
    NEVER be provisioned by the upgrade path (nexus-ltix8's no-op).
    Current when the on-disk provisioning artifact already exists.
    """
    if provisioned:
        return PreconditionReport(
            name="provisioning",
            applicable=True,
            current=True,
            detail=detail or "service stack provisioned",
            actions=actions,
        )
    if not footprint:
        return PreconditionReport(
            name="provisioning",
            applicable=False,
            current=True,
            detail="no legacy footprint to migrate — nothing to provision (fresh install)",
        )
    return PreconditionReport(
        name="provisioning",
        applicable=True,
        current=False,
        detail=detail or (
            "a legacy Chroma footprint needs a service stack to migrate into "
            "— none provisioned yet"
        ),
        actions=actions,
    )


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
    _provisioned_fn: Callable[[], bool] | None = None,
    _footprint_fn: Callable[[], bool] | None = None,
    _plugin_version_fn: Callable[[], str | None] | None = None,
    _lockstep_marker_fn: Callable[[], str | None] | None = None,
) -> list[PreconditionReport]:
    """READ-ONLY live verdicts for every precondition axis.

    Stateless by construction: every call re-reads the sidecar, the lease
    file, and package metadata. Never probes a process.

    NOT YET WIRED to a production surface (P3 validator gap 4 — the
    docstring used to overclaim "the doctor/dry-run shape"): the
    doctor-visible precondition line is the .22 follow-on bead's scope.
    ``converge_preconditions`` is what the trigger calls today.
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

    provisioned_fn = (
        _provisioned_fn if _provisioned_fn is not None else (lambda: _default_provisioned(config_dir))
    )
    footprint_fn = _footprint_fn if _footprint_fn is not None else _default_footprint

    plugin_fn = (
        _plugin_version_fn if _plugin_version_fn is not None else _default_plugin_version
    )
    marker_fn = (
        _lockstep_marker_fn if _lockstep_marker_fn is not None else _default_lockstep_marker
    )

    installed = installed_fn() or ""
    return [
        _package_report(installed),
        _engine_report(engine_detect()),
        _provisioning_report(provisioned_fn(), footprint_fn()),
        _process_report(lease_fn(), installed),
        _lockstep_report(plugin_fn(), marker_fn()),
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
    _provisioned_fn: Callable[[], bool] | None = None,
    _footprint_fn: Callable[[], bool] | None = None,
    _establish_fn: Callable[[], Any] | None = None,
    _plugin_version_fn: Callable[[], str | None] | None = None,
    _lockstep_marker_fn: Callable[[], str | None] | None = None,
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

    provisioned_fn = (
        _provisioned_fn if _provisioned_fn is not None else (lambda: _default_provisioned(config_dir))
    )
    footprint_fn = _footprint_fn if _footprint_fn is not None else _default_footprint
    establish_fn = _establish_fn if _establish_fn is not None else _default_establish

    installed = installed_fn() or ""
    reports = [_package_report(installed)]

    engine_status = engine_detect()
    engine_actions: tuple[str, ...] = ()
    if engine_status.applicable and not engine_status.converged and allow_engine_install:
        engine_actions = tuple(engine_converge())
        for line in engine_actions:
            _log.info("precondition_engine_action", action=line)
    reports.append(_engine_report(engine_status, actions=engine_actions))

    # Provisioning (ACQUISITION — P4.0b): a legacy footprint with no service
    # stack has nothing to migrate INTO. Reuses establish_verified_service
    # verbatim (provision -> health-gate -> version-pin -> discoverability).
    # allow_process_cycle=False (--skip-t3) suppresses it: standing up the
    # whole stack is exactly what "fast T2-only" opts out of.
    reports.append(
        _converge_provisioning(
            provisioned_fn, footprint_fn, establish_fn, allow=allow_process_cycle
        )
    )

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

    # Plugin/CLI lockstep (nexus-2a5ij): REPORT-ONLY, deliberately last and
    # never converged here — the RDR-143 SessionStart hook/action owns
    # acquisition; this surface only makes the skew visible alongside the
    # other axes instead of leaving it a hook-log-only fact.
    plugin_fn = (
        _plugin_version_fn if _plugin_version_fn is not None else _default_plugin_version
    )
    marker_fn = (
        _lockstep_marker_fn if _lockstep_marker_fn is not None else _default_lockstep_marker
    )
    reports.append(_lockstep_report(plugin_fn(), marker_fn()))
    return reports
