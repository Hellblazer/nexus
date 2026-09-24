# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-126 §2 (nexus-3t1jg): library-level T2 autostart install/uninstall.

The OS-unit install/uninstall logic previously lived inside the Click
command bodies ``t2_install_cmd`` / ``t2_uninstall_cmd`` in
``nexus.commands.daemon``. RDR-126 lifts it here so it can be called
**in-process** with a structured return value by:

- ``nexus.mcp._first_run.ensure_installed_and_running`` — first-run on
  MCP startup, which needed to know whether it installed fresh
  (``NEWLY_INSTALLED``) or found an existing unit (``ALREADY_PRESENT``)
  to drive the first-run banner's two text variants and surface the
  unit path. RETIRED with the T2 daemon (nexus-i711w Stage 2 sub-stage
  B); see the tombstone at ``mcp/_first_run.py``. The structured return
  value survives it, for the callers below; and
- the ``daemon_uninstall`` MCP tool (RDR-126 §4); and
- the ``nx daemon t2 install/uninstall`` CLI, which becomes a thin
  wrapper that translates these results into ``click.echo`` / exit codes.

Design rules:

- **Pure library code.** No ``click``, no ``sys.exit``, no ``print``.
  Outcomes are returned (:class:`InstallResult` / :class:`UninstallResult`)
  or raised as typed :class:`InstallerError` subclasses.
- **Generic autostart helpers stay in ``nexus.commands.daemon``** (they
  are shared with the T3 install paths). This module delegates to them
  via a lazy import so there is no import cycle and so the existing test
  indirection points (``daemon._autostart_*``) keep working.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import structlog

from nexus.bounded_subprocess import run_bounded

_log = structlog.get_logger(__name__)


class InstallStatus(Enum):
    """Outcome of :func:`install_autostart`."""

    NEWLY_INSTALLED = "newly_installed"
    ALREADY_PRESENT = "already_present"
    FAILED = "failed"


class UninstallStatus(Enum):
    """Outcome of :func:`uninstall_autostart`."""

    REMOVED = "removed"
    NOT_INSTALLED = "not_installed"


@dataclass(frozen=True)
class InstallResult:
    """Structured result of an autostart install attempt."""

    status: InstallStatus
    dest: Path
    detail: str = ""
    activated_cmd: list[str] | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallResult:
    """Structured result of an autostart uninstall attempt.

    ``survivors`` (nexus-dmgvx, GH #1419 Issue 2) names processes still
    running AFTER the unit is gone. Removing the autostart entry stops the
    thing from coming BACK; it does not stop what is running NOW, and
    Postgres is left up deliberately on the stop path too ("independently
    managed"). Reporting a bare "Removed <path>" while a supervisor and a
    cluster are both live is how Steve Harris ended up hunting a postgres
    process by hand. Empty on the happy path.
    """

    status: UninstallStatus
    dest: Path
    warnings: tuple[str, ...] = field(default_factory=tuple)
    survivors: tuple[str, ...] = field(default_factory=tuple)
    #: Whether the OS DEACTIVATION (``launchctl bootout`` /
    #: ``systemctl --user disable --now``) actually succeeded — as distinct
    #: from ``status``, which reports only that the unit FILE is gone. The
    #: two diverge on exactly the case that matters: a failed or missing
    #: deactivator is downgraded to a warning and the file is removed
    #: anyway, so ``REMOVED`` alone cannot tell a caller whether a running
    #: process was terminated. ``uninstall_daemon`` derives
    #: ``daemon_stopped`` from THIS, not from ``status`` (nexus-i711w Stage
    #: 2 sub-stage B review, High-2): reporting "daemon stopped" in the one
    #: case where the daemon demonstrably survived is worse than the
    #: over-pessimism it replaced. ``True`` on the NOT_INSTALLED path —
    #: there was nothing to deactivate — which is why callers must AND it
    #: with ``status``.
    deactivated: bool = True


class InstallerError(Exception):
    """Base class for install failures the CLI translates to exit 1."""


class SymlinkRefusedError(InstallerError):
    """The destination unit path is a symlink; refuse to write through it."""


class ContentDiffersError(InstallerError):
    """The destination exists with differing content and ``force`` is off."""


class ActivationError(InstallerError):
    """``launchctl`` / ``systemctl`` activation failed and ``force`` is off."""




def _render_for_service() -> tuple[Path, str]:
    """Resolve the destination path and rendered unit body for the storage
    SERVICE tier (RDR-174 P2.1). It was written as a mirror of the since-deleted
    ``_render_for_t2`` (nexus-i711w Stage 2 sub-stage B), swapping the template
    filename; it is now the only renderer. The unit execs
    ``nx daemon service start --foreground``.
    """
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    install_dir = _daemon._autostart_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    log_dir = _daemon._autostart_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    template_name = _daemon._autostart_filename_service()
    nx_bin = _daemon._resolve_nx_bin()
    # nexus-cd1k0.19 review round 2, finding 4: bake in the RESOLVED
    # ABSOLUTE config dir THIS install used (whatever combination of
    # NEXUS_CONFIG_DIR / default resolved it) so the generated unit's
    # ProgramArguments/ExecStart carry an explicit --config-dir, never a
    # flagless one — see _render_template's own docstring for why.
    config_dir = str(nexus_config_dir().resolve())
    rendered = _daemon._render_template(
        template_name,
        nx_bin=nx_bin,
        log_dir=str(log_dir),
        path_env=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        config_dir=config_dir,
    )
    return install_dir / template_name, rendered


def _render_for(tier: str) -> tuple[Path, str]:
    """Dispatch the per-tier render. ``install_autostart`` is tier-generic; the
    render path is the only tier-specific seam on the INSTALL side (activation
    is dest-based and tier-agnostic)."""
    # NO t2 RENDER: the T2 daemon is retired (nexus-i711w Stage 2 sub-stage B),
    # so a T2 unit can no longer be INSTALLED. Removal of a unit left by an
    # older install is still supported — see _autostart_filename_for /
    # _deactivate_cmd, which keep their t2 arms for exactly that.
    if tier == "service":
        return _render_for_service()
    raise ValueError(f"unknown autostart tier {tier!r}")


def rendered_unit_content(tier: str) -> tuple[Path, str]:
    """Public wrapper over :func:`_render_for` for cross-module drift
    detection (nexus-rlp0v).

    ``_render_for`` is otherwise an install-path implementation detail,
    private to this module. :func:`nexus.upgrade_finish.converge_service_autostart_unit`
    needs the SAME render :func:`install_autostart` uses internally to
    decide whether an ALREADY-INSTALLED unit has drifted from the current
    template — duplicating the per-tier render logic there would be the
    real mistake (two renderers that can silently diverge). This is the
    one sanctioned cross-module entry point for reading that render
    without reaching into a private name.
    """
    return _render_for(tier)


def _is_darwin() -> bool:
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    return _daemon._autostart_platform() == "darwin"


def _activate_cmd(dest: Path) -> list[str]:
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    if _daemon._autostart_platform() == "darwin":
        uid = os.getuid()
        return ["launchctl", "bootstrap", f"gui/{uid}", str(dest)]
    return ["systemctl", "--user", "enable", "--now", dest.name]


def _deactivate_cmd(dest: Path, *, tier: str = "t2") -> list[str]:
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    if _daemon._autostart_platform() == "darwin":
        uid = os.getuid()
        # The launchd bootout target is the unit LABEL, which is tier-specific
        # (the Linux disable path is dest.name and tier-agnostic). RDR-174 P2.1:
        # a hardcoded T2 label here would no-op or boot out the wrong unit for
        # the service tier.
        label = (
            _daemon._SERVICE_LAUNCHD_LABEL
            if tier == "service"
            else _daemon._T2_LAUNCHD_LABEL
        )
        return ["launchctl", "bootout", f"gui/{uid}/{label}"]
    return ["systemctl", "--user", "disable", "--now", dest.name]


class ActivationState(Enum):
    """What the OS service manager says about an installed autostart unit
    (nexus-mac7t). Distinct from the unit FILE being present: the file is
    written before activation, and a later ``launchctl disable`` /
    ``systemctl disable`` leaves it in place, so the file alone cannot
    say whether the service starts at login."""

    #: Registered for login: the unit is not in launchd's disabled
    #: overrides (``launchctl print-disabled gui/<uid>``) / ``systemctl
    #: --user is-enabled`` exits 0.
    ACTIVE = "active"
    #: A manager answered and positively reports the unit disabled or
    #: unknown. Only this state is a defect; the remedy rides in
    #: :attr:`ActivationProbe.remedy`.
    NOT_ACTIVE = "not_active"
    #: No ``launchctl`` / ``systemctl`` on this box: nothing here can
    #: register the unit, so its state is not a defect to fix here.
    NO_MANAGER = "no_manager"
    #: A manager exists but could not be asked from this process: no user
    #: bus over ssh, no GUI domain on a headless Mac, a hung or broken
    #: binary. Never read as a defect (code-review-expert and critic on
    #: 9ffaa462f: reading this as NOT_ACTIVE sent ``nx daemon
    #: restart-stale`` over ssh into a bounce that deleted a working unit).
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class ActivationProbe:
    """Result of :func:`autostart_activation_state`."""

    state: ActivationState
    #: The manager's own words (first line of stderr/stdout), the missing
    #: command, or the timeout, for the caller's report line. Empty on
    #: ``ACTIVE``.
    detail: str = ""
    #: The command that re-registers the unit, filled only on
    #: ``NOT_ACTIVE`` (platform-specific: a launchd label disabled by
    #: ``launchctl disable`` has to be enabled first, or ``bootstrap``
    #: refuses).
    remedy: str = ""


#: Ceiling on the activation query. A hung manager must not wedge
#: ``nx doctor`` or the finish pass; a timeout reads as ``UNREACHABLE``.
_ACTIVATION_QUERY_TIMEOUT: float = 10.0

#: Ceiling on an OS-manager INSTALL/UNINSTALL action -- ``launchctl
#: bootstrap``/``bootout`` on macOS, ``systemctl --user enable --now``/
#: ``disable --now`` on Linux (nexus-k9i56). MEASURED on this box (macOS
#: 25.6.0, 2026-09-23) against a throwaway LaunchAgent
#: (``com.nexus.k9i56-throwaway-test``, never a live nexus unit --
#: ``launchctl list | grep -i nexus`` was checked first and left alone),
#: three runs each, idle and under a 17-process ``yes`` CPU load (all
#: cores pinned, load average ~6-16 during the run):
#:
#:     launchctl bootstrap   idle 4.4-5.7ms   loaded 6.1-9.5ms
#:     launchctl bootout     idle 4.2-5.1ms   loaded 5.4-6.8ms
#:
#: NO LINUX BOX EXISTS HERE, so the Linux half is REASONED from systemd's
#: own published defaults, not measured -- that gap is real and stays
#: open: the bead asked to time the verbs "on macOS AND on Linux", and
#: only macOS timings above are actual measurements. ``systemctl --user
#: enable/disable --now`` runs the unit's start/stop JOB inline (this is
#: the PER-USER manager, spawned by ``--user``, not the system one), and
#: that job is itself bounded by ``TimeoutStartSec=``/``TimeoutStopSec=``,
#: which default (``DefaultTimeoutStartSec=``/``DefaultTimeoutStopSec=``
#: in systemd-user.conf(5) -- the user-manager config file, not
#: systemd-system.conf(5)) to 90s each; the shipped value is the same 90s
#: either way, so this is a citation fix, not a changed number.
#:
#: ONE LOAD SHAPE IS NOT ADDRESSED AT ALL: manager CONTENTION -- launchd
#: or the systemd --user instance busy with a queue of OTHER jobs.
#: ``TimeoutStartSec=``/``TimeoutStopSec=`` bound the job's own EXECUTION
#: once dispatched; they say nothing about how long a job can sit
#: QUEUED before the manager gets to it, and that wait is not measured
#: (the CPU-loaded run above loaded the BOX, not the manager's own job
#: queue) or reasoned about here. This bound is judged adequate anyway
#: for what actually gets shelled out: the shipped ``com.nexus.service.plist``/
#: ``nexus-service.service`` unit runs ``nx daemon service start
#: --foreground``, and that supervisor's own architected shutdown budget
#: (``storage_service_daemon._SUPERVISOR_STOP_GRACE``, currently 12.0s --
#: 2x its election budget plus a graceful-SIGTERM window plus a
#: post-SIGKILL reap, with a 1s margin) is the slow half of what a
#: bootout/disable --now actually waits on, and it is an order of
#: magnitude below both systemd's 90s default and this 120s bound. So the
#: JOB itself is fast even under the load shapes examined; a genuinely
#: contended manager queue is the one shape left honestly unmeasured, not
#: reasoned to be safe.
#:
#: THE MULTIPLIER IS A JUDGEMENT, NOT A DERIVATION -- same posture as
#: ``pg_provision._INITDB_TIMEOUT_S`` above (in ``db/pg_provision.py``).
#: The measured numbers say what an idle-to-moderately-loaded Mac does in
#: single-digit milliseconds; they say nothing about a slow box, a wedged
#: launchd/systemd bus, or a unit whose own ExecStart genuinely takes a
#: while. This bound must stay ABOVE systemd's own 90s default for the
#: same reason ``pg_provision._PG_CTL_WAIT_TIMEOUT_S`` stays above
#: ``PGCTLTIMEOUT``: killing the outer call before the inner job can
#: report its own, better-worded failure loses information. 120s clears
#: that with margin and is several orders of magnitude above anything
#: measured here -- the cost of being generous is a longer wait on a box
#: that is already broken, the cost of being tight is breaking a
#: slow-but-working install/uninstall.
_MANAGER_ACTION_TIMEOUT_S: float = 120.0

#: Where the manager binaries live when the calling process's PATH is
#: trimmed (an MCP server, cron). Consulted after PATH by every manager
#: spawn in this module, probe and actuator alike (critic on 6867dbe4d:
#: resolving it in the probe only left the activator raising "not found"
#: on the same box the probe had just answered for). When neither
#: resolves, the bare name is spawned and the OS's own FileNotFoundError
#: is the "no manager" signal.
_MANAGER_ABSOLUTE_PATHS: dict[str, tuple[str, ...]] = {
    "launchctl": ("/bin/launchctl",),
    "systemctl": ("/usr/bin/systemctl", "/bin/systemctl"),
}

REINSTALL_REMEDY = "nx daemon service uninstall --autostart && nx daemon service install --autostart"

#: ``systemctl is-enabled`` words that mean "the unit will not start at
#: login" (a positive answer, as opposed to a bus failure).
_SYSTEMD_NOT_ENABLED_WORDS = frozenset({"disabled", "not-found", "masked", "masked-runtime"})

#: ``launchctl print-disabled`` value tokens. macOS 26 prints ``=> disabled``
#: / ``=> enabled``; older dialects printed ``=> true`` / ``=> false``. A
#: listed label with any other token is UNREACHABLE, never ACTIVE: the
#: parser must not read what it did not understand as good news.
_LAUNCHD_DISABLED_TOKENS = frozenset({"disabled", "true"})
_LAUNCHD_ENABLED_TOKENS = frozenset({"enabled", "false"})


def _manager_executable(name: str) -> str:
    """The argv[0] to spawn for a manager command: the bare name when PATH
    resolves it (the OS does the lookup, argv stays as every log and test
    has always seen it), a known absolute location when PATH is trimmed,
    and the bare name again when neither holds so the spawn raises the
    OS's own FileNotFoundError."""
    if shutil.which(name):
        return name
    for candidate in _MANAGER_ABSOLUTE_PATHS.get(name, ()):
        if os.access(candidate, os.X_OK):
            return candidate
    return name


def _run_manager(
    cmd: list[str], *, timeout: float, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    """A launchctl/systemctl command with the binary resolved through PATH
    then :data:`_MANAGER_ABSOLUTE_PATHS`, routed through
    :func:`~nexus.bounded_subprocess.run_bounded` so a hung manager is
    killed -- process group and all -- at ``timeout`` rather than left to
    block the caller forever. Raises ``FileNotFoundError`` (filename = the
    bare command) when no manager exists, exactly as a bare spawn would;
    ``subprocess.TimeoutExpired`` when the manager does not answer within
    ``timeout``.

    ``timeout`` is REQUIRED and keyword-only (nexus-k9i56). Originally a
    ``**kwargs`` funnel (nexus-t10nc) with a branch that fell through to a
    stock, unbounded ``subprocess.run`` whenever a caller omitted
    ``timeout`` -- three of the five call sites did, deliberately left
    unbounded because nobody had measured a bound for ``launchctl
    bootout``/``systemctl`` yet. All five now pass one: the two activation
    probes use :data:`_ACTIVATION_QUERY_TIMEOUT`, the three install/
    uninstall actuators use :data:`_MANAGER_ACTION_TIMEOUT_S`. Making
    ``timeout`` an explicit parameter (rather than reading it out of
    ``kwargs``) means a call site that omits it fails loudly at the call,
    with Python's own ``TypeError``, instead of silently falling through
    to the removed unbounded branch.

    ``capture_output`` is accepted, for callers written against the
    ``subprocess.run`` shape, and dropped before reaching
    :func:`run_bounded` -- which always captures via its own
    ``stdout``/``stderr`` defaults.
    """
    argv = [_manager_executable(cmd[0]), *cmd[1:]]
    rest = {k: v for k, v in kwargs.items() if k != "capture_output"}
    return run_bounded(argv, timeout=timeout, **rest)  # type: ignore[arg-type]


def _launchd_label_for(tier: str) -> str:
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    return _daemon._SERVICE_LAUNCHD_LABEL if tier == "service" else _daemon._T2_LAUNCHD_LABEL


def _activation_query_cmd(dest: Path, *, tier: str) -> list[str]:
    """The read-only registration query. macOS: ``launchctl print-disabled
    gui/<uid>`` -- the disabled overrides are the only persistent
    de-registration of a plist that sits in ``~/Library/LaunchAgents``
    (``launchctl print gui/<uid>/<label>`` answers "loaded right now", a
    different fact: ``bootout`` is session-only and a booted-out job loads
    again at the next login; measured 2026-09-18 on three enabled but
    unloaded LaunchAgents). Linux: ``systemctl --user is-enabled <unit>``,
    which is the enable state directly."""
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    if _daemon._autostart_platform() == "darwin":
        return ["launchctl", "print-disabled", f"gui/{os.getuid()}"]
    return ["systemctl", "--user", "is-enabled", dest.name]


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    raw = (result.stderr or "").strip() or (result.stdout or "").strip()
    return raw.splitlines()[0] if raw else ""


def autostart_activation_state(dest: Path, *, tier: str) -> ActivationProbe:
    """Ask the OS service manager whether the installed unit at ``dest`` is
    registered for login (nexus-mac7t).

    :func:`install_autostart` writes the unit file before it activates, and
    every drift check compared file content only, so a unit the manager
    did not have read as "already up to date" on every later pass. This is
    the second half of that check, and the answer :func:`install_autostart`
    itself consults before it short-circuits on identical content.

    Never raises. ``NOT_ACTIVE`` only on a POSITIVE answer from the manager
    (the label listed as disabled by launchd; ``disabled`` / ``not-found``
    / ``masked`` from systemd). Every other failure to get an answer is
    ``UNREACHABLE``: a non-zero exit with any other text (``Failed to
    connect to bus``, ``Could not find domain``), a timeout, a binary that
    would not run, a listing token the parser does not know. ``NO_MANAGER``
    when neither PATH nor the known absolute locations hold the binary.
    """
    cmd = _activation_query_cmd(dest, tier=tier)
    shown = " ".join(cmd)
    try:
        result = _run_manager(
            cmd, capture_output=True, text=True, check=False,
            timeout=_ACTIVATION_QUERY_TIMEOUT,
        )
    except FileNotFoundError:
        known = ", ".join(_MANAGER_ABSOLUTE_PATHS.get(cmd[0], ()))
        return ActivationProbe(
            ActivationState.NO_MANAGER,
            f"{cmd[0]} not found on PATH" + (f" or at {known}" if known else ""),
        )
    except subprocess.TimeoutExpired:
        return ActivationProbe(
            ActivationState.UNREACHABLE,
            f"`{shown}` did not answer within {_ACTIVATION_QUERY_TIMEOUT:g}s",
        )
    except OSError as exc:
        return ActivationProbe(ActivationState.UNREACHABLE, f"`{shown}` could not run ({exc})")

    if cmd[0] == "launchctl":
        if result.returncode != 0:
            return ActivationProbe(
                ActivationState.UNREACHABLE,
                f"`{shown}` exited {result.returncode}: {_first_line(result)}",
            )
        label = _launchd_label_for(tier)
        listed = re.search(rf'"{re.escape(label)}"\s*=>\s*(\S+)', result.stdout or "")
        if listed is None:
            return ActivationProbe(ActivationState.ACTIVE)  # unlisted labels are enabled
        token = listed.group(1)
        if token in _LAUNCHD_DISABLED_TOKENS:
            return ActivationProbe(
                ActivationState.NOT_ACTIVE,
                f"`{shown}` reports {label} disabled",
                remedy=f"launchctl enable gui/{os.getuid()}/{label} && {REINSTALL_REMEDY}",
            )
        if token in _LAUNCHD_ENABLED_TOKENS:
            return ActivationProbe(ActivationState.ACTIVE)
        return ActivationProbe(
            ActivationState.UNREACHABLE,
            f"`{shown}` lists {label} as `{token}`, a value this client does not know",
        )

    if result.returncode == 0:
        return ActivationProbe(ActivationState.ACTIVE)
    word = (result.stdout or "").strip().split("\n", 1)[0].strip()
    if word in _SYSTEMD_NOT_ENABLED_WORDS:
        return ActivationProbe(
            ActivationState.NOT_ACTIVE,
            f"`{shown}` reports {word}",
            remedy=REINSTALL_REMEDY,
        )
    return ActivationProbe(
        ActivationState.UNREACHABLE,
        f"`{shown}` exited {result.returncode}: {_first_line(result)}",
    )


def _launchd_loaded_now(tier: str) -> bool | None:
    """macOS only: is the job bootstrapped in this login session right now?
    ``launchctl print gui/<uid>/<label>`` exits 0 when it is; "Could not
    find service" when it is not. Anything else (no gui domain over ssh, a
    timeout) is ``None``: could not tell. This is the fact a FAILED
    ``bootstrap`` leaves behind (it writes no disabled override, so
    ``print-disabled`` still reads enabled); the install short-circuit asks
    it so a retry re-activates instead of answering ALREADY_PRESENT
    (critic on 6867dbe4d, measured)."""
    cmd = ["launchctl", "print", f"gui/{os.getuid()}/{_launchd_label_for(tier)}"]
    try:
        result = _run_manager(
            cmd, capture_output=True, text=True, check=False,
            timeout=_ACTIVATION_QUERY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if "Could not find service" in (result.stderr or "") + (result.stdout or ""):
        return False
    return None


def _autostart_filename_for(tier: str) -> str:
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    if tier == "t2":
        return _daemon._autostart_filename_t2()
    if tier == "service":
        return _daemon._autostart_filename_service()
    raise ValueError(f"unknown autostart tier {tier!r}")


def install_autostart(*, tier: str, force: bool = False) -> InstallResult:
    """Install a daemon OS autostart unit for the current user.

    ``tier`` selects which unit is rendered. ``"service"`` (RDR-174 P2.1 — the
    storage service that serves every tier) is the only installable tier; the
    activation path is tier-agnostic (dest-based), only the rendered unit
    differs.

    ``tier`` is REQUIRED and deliberately has no default (nexus-i711w Stage 2
    sub-stage B). It defaulted to ``"t2"`` while the T2 daemon existed; with
    that daemon retired, ``_render_for`` has no t2 arm, so a default would be a
    ``ValueError`` trap for any unqualified caller. Note the asymmetry with
    :func:`uninstall_autostart`, which KEEPS its ``"t2"`` default: you can no
    longer INSTALL a T2 unit, but an upgraded box must still be able to REMOVE
    one left behind by a pre-retirement install.

    The OS unit is the source of truth. If the destination already holds
    the freshly-rendered content AND the service manager reports it
    registered (or cannot be asked -- :func:`autostart_activation_state`),
    returns ``ALREADY_PRESENT`` without re-activating. Otherwise the unit
    is written and activated via
    ``launchctl bootstrap`` (macOS) / ``systemctl --user enable --now``
    (Linux), returning ``NEWLY_INSTALLED``.

    Raises:
        SymlinkRefusedError: ``dest`` is a symlink.
        ContentDiffersError: ``dest`` exists with differing content and
            ``force`` is False.
        ActivationError: activation shelled out non-zero / not found and
            ``force`` is False.

    Under ``force`` an activation failure is downgraded to a warning on
    the returned :class:`InstallResult` rather than raised.
    """
    dest, rendered = _render_for(tier)
    probe: ActivationProbe | None = None

    if dest.is_symlink():
        raise SymlinkRefusedError(
            f"{dest} is a symlink; refusing to install autostart through it. "
            "Remove the symlink first and re-run."
        )
    if dest.exists():
        try:
            existing: str | None = dest.read_text()
        except OSError:
            existing = None
        if existing == rendered and not force:
            # nexus-mac7t: identical content alone used to answer
            # ALREADY_PRESENT, so a unit the manager did not have (an
            # install whose activation failed, a later `launchctl disable`,
            # a manager that appeared after a no-manager install) was never
            # activated by a retry (cd1k0.4's defect, held here now rather
            # than by deleting the file on failure). Two questions on
            # macOS: registered for login (print-disabled) AND loaded now
            # (launchctl print), because a failed bootstrap leaves the
            # first true and the second false. ACTIVE and loaded: nothing
            # to do. UNREACHABLE: activation would fail for the same
            # environmental reason, so say what could not be confirmed
            # instead of churning. Anything else falls through to a
            # genuine activation attempt. --force always falls through.
            probe = autostart_activation_state(dest, tier=tier)
            unconfirmed = ""
            if probe.state is ActivationState.ACTIVE:
                loaded = _launchd_loaded_now(tier) if _is_darwin() else True
                if loaded is True:
                    return InstallResult(
                        status=InstallStatus.ALREADY_PRESENT,
                        dest=dest,
                        detail=f"{dest} already up to date and registered; no changes",
                    )
                if loaded is None:
                    unconfirmed = "whether it is loaded in this login session could not be checked"
            elif probe.state is ActivationState.UNREACHABLE:
                unconfirmed = probe.detail
            if unconfirmed:
                return InstallResult(
                    status=InstallStatus.ALREADY_PRESENT,
                    dest=dest,
                    detail=(
                        f"{dest} already up to date; could not confirm it is "
                        f"registered with the service manager ({unconfirmed}) -- "
                        "run `nx doctor` from a login session to confirm"
                    ),
                )
        if not force and existing is not None and existing != rendered:
            raise ContentDiffersError(
                f"{dest} exists and its content differs from the rendered "
                "template; refusing to overwrite. Re-run with --force to "
                "replace the existing file (your customisations will be "
                "lost), or remove the file first."
            )

    # nexus-cd1k0.5: --force over a unit the OS already loaded must unload
    # it first, or launchd keeps running the OLD definition (bootstrap of an
    # already-loaded label is a no-op) and systemd keeps the old ExecStart
    # until a daemon-reload. converge_service_autostart_unit avoids this by
    # uninstalling first; the CLI's --force path did not. A deactivation
    # that fails (nothing loaded) is harmless and ignored.
    # --force over identical content is a deliberate re-activation
    # (nexus-mac7t: the one way past a short-circuit that reads registered),
    # so the unload runs for any previous content, not only differing.
    previous: str | None = existing if dest.exists() else None
    if force and previous is not None:
        predeactivate_cmd = _deactivate_cmd(dest, tier=tier)
        try:
            _run_manager(
                predeactivate_cmd, capture_output=True, text=True, check=False,
                timeout=_MANAGER_ACTION_TIMEOUT_S,
            )
        except (FileNotFoundError, OSError):
            pass
        except subprocess.TimeoutExpired:
            # Best-effort unload before the overwrite below (same as the
            # FileNotFoundError/OSError branch above): a hung manager here
            # must not block the reinstall, but "never a silent pass"
            # (nexus-k9i56) means naming the verb and the bound rather than
            # swallowing it outright.
            _log.warning(
                f"{tier}_install_predeactivate_timeout",
                cmd=" ".join(predeactivate_cmd),
                timeout_s=_MANAGER_ACTION_TIMEOUT_S,
            )

    dest.write_text(rendered)
    dest.chmod(0o644)

    # The unit file STAYS on every activation failure. cd1k0.4 restored the
    # tree here so a retry would not read file == render and answer
    # ALREADY_PRESENT; that invariant now lives at the short-circuit above,
    # which asks the manager. Keeping the file is what lets `nx doctor`'s
    # activation row and converge_service_autostart_unit's no-manager NOTE
    # report a unit that was wanted and could not be registered, and what
    # the ActivationError message says ("file installed but not
    # activated").
    cmd = _activate_cmd(dest)
    warnings: tuple[str, ...] = ()
    try:
        result = _run_manager(
            cmd, capture_output=True, text=True, check=False,
            timeout=_MANAGER_ACTION_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        msg = (
            f"{cmd[0]} not found on PATH; file installed but not activated ({exc}). "
            f"Once a service manager is available, run `{REINSTALL_REMEDY}` to register it."
        )
        if not force:
            raise ActivationError(msg) from exc
        _log.warning(f"{tier}_install_activation_not_found", dest=str(dest), error=str(exc))
        return InstallResult(
            status=InstallStatus.NEWLY_INSTALLED, dest=dest, detail=msg, warnings=(msg,)
        )
    except subprocess.TimeoutExpired as exc:
        # nexus-k9i56: activation is the load-bearing half of install, so a
        # timeout here is treated exactly like FileNotFoundError above --
        # raised unless --force, and either way the message names the verb
        # (the shown command) and the bound, never a silent pass.
        msg = (
            f"`{' '.join(cmd)}` did not answer within {_MANAGER_ACTION_TIMEOUT_S:g}s; "
            f"file installed but not activated. Once the service manager responds, run "
            f"`{REINSTALL_REMEDY}` to register it."
        )
        if not force:
            raise ActivationError(msg) from exc
        _log.warning(
            f"{tier}_install_activation_timeout",
            dest=str(dest),
            timeout_s=_MANAGER_ACTION_TIMEOUT_S,
        )
        return InstallResult(
            status=InstallStatus.NEWLY_INSTALLED, dest=dest, detail=msg, warnings=(msg,)
        )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        msg = f"{' '.join(cmd)} exited {result.returncode}: {detail}"
        if probe is not None and probe.remedy:
            # The manager had already said why (a disabled launchd label
            # refuses bootstrap); the failing path names the same remedy
            # the doctor row and converge do.
            msg += f" -- {probe.detail}; run `{probe.remedy}`"
        if not force:
            raise ActivationError(msg)
        _log.warning(f"{tier}_install_activation_failed", dest=str(dest), returncode=result.returncode)
        warnings = (msg,)
        return InstallResult(
            status=InstallStatus.NEWLY_INSTALLED, dest=dest, detail=msg, warnings=warnings
        )

    return InstallResult(
        status=InstallStatus.NEWLY_INSTALLED,
        dest=dest,
        detail=f"Activated via: {' '.join(cmd)}",
        activated_cmd=cmd,
    )


@dataclass(frozen=True)
class DaemonUninstallReport:
    """Result of the higher-level ``daemon_uninstall`` orchestration."""

    confirmed: bool
    unit_status: UninstallStatus
    unit_dest: Path
    marker_removed: bool
    data_removed: bool
    data_dir: Path
    daemon_stopped: bool
    warnings: tuple[str, ...]
    message: str
    #: RDR-165 eu4u4: whether the engine-service/PG stack was stopped
    #: (`nx daemon service stop --with-pg`). Best-effort, like ``daemon_stopped``.
    #: Defaulted so the MCP daemon_uninstall dry-run path needn't set it.
    service_stopped: bool = False
    #: Status/dest of the SERVICE-tier autostart unit removal. uninstall_daemon
    #: stops the engine-service + PG stack, so it must also remove the SERVICE
    #: autostart unit (else the OS watchdog restarts the just-stopped service).
    #: ``unit_status``/``unit_dest`` remain the T2 unit for back-compat.
    service_unit_status: UninstallStatus = UninstallStatus.NOT_INSTALLED
    service_unit_dest: Path | None = None


# NO _stop_daemon_best_effort: it shelled out to ``nx daemon t2 stop``, a verb
# retired with the T2 daemon (nexus-i711w Stage 2 sub-stage B). Deactivating the
# unit already terminates a surviving process on both platforms — ``launchctl
# bootout`` kills the running job, ``systemctl --user disable --now`` stops it —
# so the separate shell-out was belt-and-braces that no longer has a belt.
# ``daemon_stopped`` is now derived from that deactivation (see uninstall_daemon
# step 2).


def _stop_service_stack_best_effort() -> tuple[bool, str | None]:
    """Best-effort ``nx daemon service stop --with-pg``. Returns (stopped, warning).

    RDR-165 eu4u4: the complete teardown must stop the engine-service + embedded
    Postgres, not only the T2 daemon (the installer.py gap where uninstall only
    ran ``nx daemon t2 stop``). Same shell-out rationale as the since-deleted
    ``_stop_daemon_best_effort`` (see its tombstone above): lifecycle is
    daemon-command territory, not installer logic (RDR-126 §2). Routes through
    the existing service-stop
    command, which relinquishes the storage_service lease via the shared
    ``service_registry.py`` primitive (RDR-149 — no duplicated lifecycle here).
    A no-running-service exit is reported as a warning, never raised.
    """
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    cmd = [*_daemon._resolve_nx_bin(), "daemon", "service", "stop", "--with-pg"]
    try:
        result = run_bounded(cmd, timeout=30)
    except Exception as exc:  # noqa: BLE001 — stop is best-effort
        return False, f"service stop failed: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        return False, f"service stop exited {result.returncode}: {detail}"
    return True, None


def uninstall_daemon(*, confirm: bool = False, remove_data: bool = False) -> DaemonUninstallReport:
    """Orchestrate full daemon removal for the ``daemon_uninstall`` MCP tool.

    With ``confirm=False`` this is a dry run: it reports what WOULD be
    removed and touches nothing. With ``confirm=True`` it removes BOTH OS
    autostart units (service and the legacy T2 one), stops the engine-service +
    Postgres stack (best-effort), and removes the first-run marker. With
    ``remove_data=True`` it additionally wipes the nexus config / data
    directory (``nexus_config_dir()``).
    """
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
    from nexus.mcp._first_run import _first_run_marker_path  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    install_dir = _daemon._autostart_install_dir()
    unit_dest = install_dir / _daemon._autostart_filename_t2()
    service_unit_dest = install_dir / _daemon._autostart_filename_service()
    data_dir = nexus_config_dir()
    marker = _first_run_marker_path()

    if not confirm:
        parts = [
            f"the service autostart unit at {service_unit_dest}",
            f"the T2 autostart unit at {unit_dest}",
            "stop the engine-service + Postgres stack (service stop --with-pg)",
        ]
        if marker.exists():
            parts.append(f"the first-run marker at {marker}")
        if remove_data:
            parts.append(f"ALL nexus data under {data_dir}")
        plan = "; ".join(parts)
        return DaemonUninstallReport(
            confirmed=False,
            unit_status=(
                UninstallStatus.REMOVED if unit_dest.exists() else UninstallStatus.NOT_INSTALLED
            ),
            unit_dest=unit_dest,
            service_unit_status=(
                UninstallStatus.REMOVED if service_unit_dest.exists() else UninstallStatus.NOT_INSTALLED
            ),
            service_unit_dest=service_unit_dest,
            marker_removed=False,
            data_removed=False,
            data_dir=data_dir,
            daemon_stopped=False,
            warnings=(),
            message=(
                f"This would remove: {plan}. Re-run with confirm=true to proceed"
                + (" (remove_data=true is set: this DELETES your notes and search index)." if remove_data else ".")
            ),
        )

    warnings: list[str] = []

    # 1. Remove BOTH OS autostart units. The SERVICE unit is the one that
    #    actually restarts the stack (it is the OS watchdog), so removing it is
    #    load-bearing — leaving it would re-start the service stopped in step 2.
    #    The legacy T2 unit is removed too. Both are best-effort (NOT_INSTALLED
    #    is graceful).
    service_unit_result = uninstall_autostart(tier="service")
    warnings.extend(service_unit_result.warnings)
    unit_result = uninstall_autostart()
    warnings.extend(unit_result.warnings)

    #    Deactivating the legacy T2 unit is ALSO how a surviving T2 daemon gets
    #    stopped now that ``nx daemon t2 stop`` is gone (nexus-i711w Stage 2
    #    sub-stage B): launchctl bootout kills the running job, systemctl
    #    disable --now stops it. NOT_INSTALLED means there was nothing to stop.
    #
    #    ANDed with ``deactivated``, NOT derived from ``status`` alone: a
    #    failed/missing deactivator is downgraded to a warning and the unit
    #    file removed anyway, so ``REMOVED`` comes back in the one case where
    #    the daemon demonstrably SURVIVED. Claiming "daemon stopped" there is
    #    wrong in the dangerous direction — worse than the old code's
    #    permanent "stop not confirmed", which was merely pessimistic.
    daemon_stopped = (
        unit_result.status is UninstallStatus.REMOVED and unit_result.deactivated
    )

    # 2. Stop the engine-service + Postgres stack (best-effort) — RDR-165 eu4u4.
    #    A complete teardown must leave no running storage backend.
    service_stopped, service_warning = _stop_service_stack_best_effort()
    if service_warning:
        warnings.append(service_warning)

    # 3. Remove the first-run marker so a reinstall re-shows the banner.
    marker_removed = False
    if marker.exists():
        try:
            marker.unlink()
            marker_removed = True
        except OSError as exc:
            warnings.append(f"could not remove first-run marker: {exc}")

    # 4. Optionally wipe all nexus data.
    data_removed = False
    if remove_data and data_dir.exists():
        import shutil  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

        # Path-safety guard (review H2): a misconfigured NEXUS_CONFIG_DIR
        # (e.g. "/", "/Users", or a bare home dir) must never let rmtree
        # wipe a broad tree. confirm=True gates accidents; this gates a
        # confused caller with a bad env. Refuse the home dir itself and
        # any shallow path (<=3 components covers "/", "/Users",
        # "/Users/<user>", "/home/<user>", "/etc", "/var/lib"); a real
        # config dir (~/.config/nexus) and test tmp dirs are deeper.
        resolved = data_dir.resolve()
        home = Path.home().resolve()
        if resolved == home or len(resolved.parts) <= 3:
            warnings.append(
                f"refusing to remove data dir {data_dir}: path is too shallow "
                "to be a nexus config dir; skipping data removal "
                "(check NEXUS_CONFIG_DIR)."
            )
        else:
            try:
                shutil.rmtree(data_dir)
                data_removed = True
            except OSError as exc:
                warnings.append(f"could not remove data dir {data_dir}: {exc}")

    summary = [
        f"service autostart unit: {service_unit_result.status.value}",
        f"T2 autostart unit: {unit_result.status.value}",
    ]
    summary.append("service stack stopped" if service_stopped else "service stop not confirmed")
    summary.append("daemon stopped" if daemon_stopped else "daemon stop not confirmed")
    if marker_removed:
        summary.append("first-run marker removed")
    if data_removed:
        summary.append(f"data dir {data_dir} wiped")
    return DaemonUninstallReport(
        confirmed=True,
        unit_status=unit_result.status,
        unit_dest=unit_result.dest,
        service_unit_status=service_unit_result.status,
        service_unit_dest=service_unit_result.dest,
        marker_removed=marker_removed,
        data_removed=data_removed,
        data_dir=data_dir,
        daemon_stopped=daemon_stopped,
        service_stopped=service_stopped,
        warnings=tuple(warnings),
        message="Daemon uninstall complete: " + "; ".join(summary) + ".",
    )


def uninstall_autostart(*, tier: str = "t2") -> UninstallResult:
    """Remove a daemon OS autostart unit for the current user.

    ``tier`` selects the unit: ``"t2"`` (default — ``daemon_uninstall`` and
    ``upgrade_finish`` rely on it) or ``"service"`` (RDR-174 P2.1).

    The ``"t2"`` default SURVIVES the T2 daemon's retirement on purpose
    (nexus-i711w Stage 2 sub-stage B). Removal machinery outlives what it
    removes: a box upgraded from a pre-retirement install still carries a
    launchd/systemd unit firing ``nx daemon t2 start``, and without this arm it
    would keep firing that now-nonexistent command on every boot forever.
    Retiring it is gated on "no supported upgrade path still carries such a
    unit", not on the daemon going away. See :func:`install_autostart` for the
    other half of the asymmetry — INSTALL dies, REMOVE survives.

    A non-zero / missing
    ``launchctl bootout`` / ``systemctl disable`` is downgraded to a warning and
    the file is removed anyway (the unit file is the durable artifact). Returns
    ``NOT_INSTALLED`` when nothing is present.
    """
    from nexus.commands import daemon as _daemon  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    install_dir = _daemon._autostart_install_dir()
    dest = install_dir / _autostart_filename_for(tier)

    if not dest.exists():
        return UninstallResult(status=UninstallStatus.NOT_INSTALLED, dest=dest)

    warnings: list[str] = []
    deactivated = True
    cmd = _deactivate_cmd(dest, tier=tier)
    try:
        result = _run_manager(
            cmd, capture_output=True, text=True, check=False,
            timeout=_MANAGER_ACTION_TIMEOUT_S,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip()
            warnings.append(f"{' '.join(cmd)} exited {result.returncode}: {detail}")
            deactivated = False
    except FileNotFoundError as exc:
        warnings.append(f"{cmd[0]} not found ({exc}); removing file anyway.")
        deactivated = False
    except subprocess.TimeoutExpired:
        # nexus-k9i56: same posture as the FileNotFoundError branch above --
        # a hung manager must not block removal of the unit file (the
        # durable artifact), but the warning names the verb and the bound
        # rather than passing silently.
        warnings.append(
            f"`{' '.join(cmd)}` did not answer within {_MANAGER_ACTION_TIMEOUT_S:g}s; "
            "removing file anyway."
        )
        deactivated = False

    dest.unlink()

    # nexus-dmgvx: the unit is gone, so nothing will come BACK — but say what
    # is still running NOW. Probed after the unlink so the report describes
    # the post-uninstall world, and wrapped because a probe failure must
    # never strand a unit that has already been removed.
    survivors: tuple[str, ...] = ()
    try:
        survivors = _probe_survivors(tier=tier)
    except Exception as exc:  # noqa: BLE001 — the removal already happened; never fail it for a probe
        warnings.append(
            f"could not check for surviving processes ({exc}) — verify with "
            "`nx daemon service status` yourself"
        )

    return UninstallResult(
        status=UninstallStatus.REMOVED,
        dest=dest,
        warnings=tuple(warnings),
        survivors=survivors,
        deactivated=deactivated,
    )


def _discover_service_lease() -> object | None:
    """Fresh storage-service lease, or ``None``. Never raises.

    Liveness is LEASE FRESHNESS via the RDR-149 primitive, deliberately not a
    bespoke ``ps`` sweep — reinventing per-tier liveness is exactly what the
    lifecycle gate (``tests/daemon/test_lifecycle_gate.py``) exists to stop.
    """
    try:
        import os  # noqa: PLC0415 — deferred, branch-local

        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred

        # Same construction service_endpoint.discover_lease uses (tier
        # "storage_service", scope = uid) — going through the registry rather
        # than through discover_lease() because that helper returns only
        # (base_url, token) and the operator line wants supervisor_pid.
        registry = ServiceRegistry(dir=nexus_config_dir(), tier="storage_service")
        return registry.discover(str(os.getuid()))
    except Exception:  # noqa: BLE001 — a probe is never a verdict
        return None


def _probe_live_postgres() -> int | None:
    """Port of a reachable local Postgres from pg_credentials, else ``None``.

    Never raises. A refused connection means nothing survived, which is the
    common and desired case.
    """
    try:
        import socket  # noqa: PLC0415 — deferred, branch-local

        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred
        from nexus.db.pg_provision import CREDENTIALS_FILENAME, _read_credentials  # noqa: PLC0415 — deferred

        creds_path = nexus_config_dir() / CREDENTIALS_FILENAME
        if not creds_path.exists():
            return None
        port = int((_read_credentials(creds_path) or {}).get("PG_PORT", 0) or 0)
        if port <= 0:
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return port if s.connect_ex(("127.0.0.1", port)) == 0 else None
    except Exception:  # noqa: BLE001 — a probe is never a verdict
        return None


def _probe_survivors(*, tier: str) -> tuple[str, ...]:
    """Processes still alive after the unit was removed, as operator lines.

    Scoped BY TIER: uninstalling the t2 agent must not report the storage
    service as its survivor — they are different units with different owners,
    and a misattributed survivor sends the operator after the wrong process.
    """
    if tier != "service":
        return ()

    out: list[str] = []
    lease = _discover_service_lease()
    if lease is not None:
        # nexus-cd1k0.6 finding (7): LeaseRecord carries no top-level
        # `supervisor_pid` attribute -- the supervisor stamps it into
        # `payload` (storage_service_daemon.py's publish call), so the old
        # `getattr(lease, "supervisor_pid", None)` always fell through to
        # its default and the survivor line never showed a pid.
        pid = lease.payload.get("supervisor_pid")
        where = f" (pid {pid})" if pid else ""
        out.append(
            f"storage service{where} is still running — the autostart entry is "
            "gone so it will not restart, but nothing stopped it: "
            "`nx daemon service stop`"
        )

    pg_port = _probe_live_postgres()
    if pg_port is not None:
        out.append(
            f"Postgres is still accepting connections on 127.0.0.1:{pg_port} — "
            "it is independently managed and is NOT stopped by uninstall: "
            "`nx daemon service stop --with-pg` (or pg_ctl -D <PG_DATA> stop)"
        )
    return tuple(out)
