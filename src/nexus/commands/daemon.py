# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon`` command group — manage the storage daemons.

Sub-groups: ``service``
(engine-service binary + local Postgres; serves T2 + T3), and
``aspect-worker``. RDR-155 P4b: the ``t3`` sub-group (managed
``chroma run`` subprocess) is retired — the Java storage service serves
T3 in every mode.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import click
import structlog

from nexus.config import nexus_config_dir

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group("daemon")
def daemon_group() -> None:
    """Manage storage daemons (t2, service, aspect-worker)."""


# ---------------------------------------------------------------------------
# Autostart helpers (shared with future T2 install/uninstall)
# ---------------------------------------------------------------------------



# RDR-174 P2.1 (nexus-y2yj6): the storage SERVICE tier (engine-service binary +
# local Postgres; serves T2 + T3). Mirrors the T2/T3 autostart-unit identity.
_SERVICE_PLIST_NAME = "com.nexus.service.plist"
_SERVICE_SERVICE_NAME = "nexus-service.service"
# T2 autostart-unit IDENTITY. The T2 DAEMON is gone (nexus-i711w Stage 2
# sub-stage B), but these OUTLIVE it deliberately: an install upgraded from a
# version that ran `nx daemon t2 install --autostart` still has a launchd/
# systemd unit on disk, and upgrade_finish's stray-unit leg needs this identity
# to FIND and REMOVE it. Delete these only once no supported upgrade path can
# still be carrying such a unit — otherwise the stale unit lingers forever,
# firing a `nx daemon t2 start` that no longer exists on every boot.
_T2_PLIST_NAME = "com.nexus.t2.plist"
_T2_SERVICE_NAME = "nexus-t2.service"
_T2_LAUNCHD_LABEL = "com.nexus.t2"


_SERVICE_LAUNCHD_LABEL = "com.nexus.service"


def _autostart_platform() -> str:
    """Indirection point so tests can stub the platform."""
    return sys.platform


def _autostart_install_dir() -> Path:
    platform = _autostart_platform()
    if platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents"
    if platform.startswith("linux"):
        return Path.home() / ".config" / "systemd" / "user"
    raise click.ClickException(
        f"Autostart is not supported on platform {platform!r}; "
        "supported platforms are macOS (launchd) and Linux (systemd user units)."
    )


def _autostart_log_dir() -> Path:
    platform = _autostart_platform()
    if platform == "darwin":
        return Path.home() / "Library" / "Logs"
    return Path.home() / ".local" / "state" / "nexus"


def _read_template(name: str) -> str:
    from importlib.resources import as_file, files  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    resource = files("nexus") / "_resources" / "daemon" / name
    with as_file(resource) as resolved:
        return Path(resolved).read_text()


_PLIST_NX_BIN_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)<string>__NX_BIN__</string>\s*$")


def _substitute_plist_argv(body: str, nx_bin: list[str]) -> str:
    """Expand ``<string>__NX_BIN__</string>`` into one entry per argv
    token. The plist's ProgramArguments array gives launchd one
    ``<string>`` per element; a multi-token fallback
    (``[python, "-m", "nexus.cli"]``) must render as multiple siblings,
    not a single space-joined string, or posix_spawn fails with ENOENT.
    """
    out_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        match = _PLIST_NX_BIN_LINE_RE.match(line.rstrip("\n"))
        if match is None:
            out_lines.append(line)
            continue
        indent = match.group("indent")
        trailing_nl = "\n" if line.endswith("\n") else ""
        for token in nx_bin:
            out_lines.append(f"{indent}<string>{_xml_escape(token)}</string>{trailing_nl}")
    return "".join(out_lines)


def _render_template(name: str, *, nx_bin: list[str], log_dir: str, path_env: str) -> str:
    """Substitute placeholders in a shipped autostart template.

    The plist substitutes ``<string>__NX_BIN__</string>`` into one
    ``<string>`` per argv token; the systemd unit's
    ``ExecStart=__NX_BIN__ ...`` line uses ``shlex.join`` so multi-token
    argvs survive systemd's whitespace-split parser.
    """
    body = _read_template(name)
    if name.endswith(".plist"):
        body = _substitute_plist_argv(body, nx_bin)
    else:
        body = body.replace("__NX_BIN__", shlex.join(nx_bin))
    return (
        body
        .replace("__LOG_DIR__", log_dir)
        .replace("__PATH_ENV__", path_env)
    )


def _resolve_nx_bin() -> list[str]:
    """Resolve the argv prefix for invoking ``nx``.

    Returns a single-element list when ``nx`` is on ``$PATH``; falls
    back to ``[python, "-m", "nexus.cli"]`` when ``shutil.which("nx")``
    returns None. Callers must respect the token boundaries when
    rendering into platform autostart formats.
    """
    found = shutil.which("nx")
    if found:
        return [found]
    return [sys.executable, "-m", "nexus.cli"]




def _autostart_filename_service() -> str:
    return (
        _SERVICE_PLIST_NAME
        if _autostart_platform() == "darwin"
        else _SERVICE_SERVICE_NAME
    )


def _autostart_filename_t2() -> str:
    """Unit filename for the RETIRED T2 daemon — kept for stale-unit REMOVAL.

    See the _T2_* constants above: this is the identity upgrade_finish needs to
    detect and bootout a unit left behind by a pre-retirement install.
    """
    return _T2_PLIST_NAME if _autostart_platform() == "darwin" else _T2_SERVICE_NAME


def _autostart_unit_installed() -> Path | None:
    """Return the unit-file Path if the T2 autostart unit is installed, else None.

    Reuses the existing _autostart_install_dir() / _autostart_filename_t2()
    helpers as an indirection point so tests can stub them independently.
    Platform-guarded: returns None on unsupported platforms without raising.
    """
    try:
        unit_path = _autostart_install_dir() / _autostart_filename_t2()
    except click.ClickException:
        return None
    return unit_path if unit_path.exists() else None


def _service_autostart_unit_installed() -> Path | None:
    """Return the unit-file Path if the storage-SERVICE autostart unit is
    installed, else None. The service-tier sibling of
    :func:`_autostart_unit_installed` (nexus-6bmph); same stubbing seams.
    Platform-guarded: returns None on unsupported platforms without raising.
    """
    try:
        unit_path = _autostart_install_dir() / _autostart_filename_service()
    except click.ClickException:
        return None
    return unit_path if unit_path.exists() else None


#: Hard ceiling on each launchctl/systemctl invocation. A hung supervisor
#: command would otherwise block ensure-running forever and the Popen
#: fallback could never fire (RF-4: never trade a working spawn path for
#: zero daemons). TimeoutExpired is caught by the except below → False →
#: fallback.
_SUPERVISOR_CMD_TIMEOUT: float = 10.0




# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------






def _discovery_record_pid(data: dict) -> int | None:
    """Extract the owner pid from a T2 discovery record.

    RDR-149 P2: a lease record carries the pid under ``endpoint``; a
    legacy payload carries it at the top level. Read both so ``stop`` /
    ``status`` keep working across an in-flight upgrade window.
    """
    pid = data.get("pid")
    if isinstance(pid, int):
        return pid
    endpoint = data.get("endpoint")
    if isinstance(endpoint, dict):
        ep_pid = endpoint.get("pid")
        if isinstance(ep_pid, int):
            return ep_pid
    return None




#: nexus-c0vby (GH #1405 defect 2): the honest, non-error-shaped message a
#: service-mode box sees instead of "No T2 daemon discovery file found" --
#: that message implies something is WRONG, but in service mode the T2
#: daemon never starts by design (t2_daemon.py's own
#: t2_daemon_not_started_service_mode early-return). Module-level so the
#: doctor/finish-pass action lines and this CLI message stay wordable
#: identically without copy-paste drift.
_T2_SERVICE_MODE_STATUS_MESSAGE = (
    "service mode — T2 daemon intentionally not running (storage is the "
    "engine service)"
)




# RDR-128 P0b (RF-4): bounded timeout for the pre-cycle DB-acquirability
# probe. Matches the startup-migration busy_timeout (db/t2/__init__.py
# _BOOTSTRAP_BUSY_TIMEOUT_MS) — there is no point cycling to a daemon whose
# first act (the startup migration) would block longer than this. Module
# constant so tests can shrink it without waiting the full 30s.
_T2_CYCLE_DB_PROBE_TIMEOUT_MS: int = 30000

# RDR-129 A2 (nexus-kwqhd): how long ``ensure-running`` waits for a SIGTERM'd
# stale daemon to FULLY EXIT before cold-spawning its replacement. The wait
# polls the predecessor's PID liveness, not the discovery file: stop() now
# holds the spawn lock until process exit (defer-release-to-exit) but unlinks
# the discovery file early, so a discovery-file poll would see "gone" while the
# lock is still held and cold-spawn into an EAGAIN -> zero daemons. If the
# predecessor outlives this window the cycle aborts and leaves it up (RDR-128
# RF-4: never trade a working daemon for none). Module constant so tests can
# shrink it.
_T2_CYCLE_EXIT_TIMEOUT: float = 10.0

# RDR-140 P2.2 (nexus-fkhe2): safety margin added on top of the holder's
# worst-case hold time to derive how long a waiter blocks on the single-flight
# election lock. The wait is computed DYNAMICALLY (see
# ``_election_wait_for``) rather than fixed: the holder keeps the lock across
# its whole discover→spawn→reachability path, whose worst case is
# ``_T2_CYCLE_DB_PROBE_TIMEOUT_MS/1000`` (stale-version write-lock probe) +
# ``_T2_CYCLE_EXIT_TIMEOUT`` (predecessor exit poll) + ``timeout`` (reachability
# poll). A fixed wait shorter than that hold reproduces the pre-P2 thundering
# herd on timeout (code-review H-1 / critic S-1): every waiter times out at
# once, re-discovers the stale/absent daemon unguarded, and all cold-spawn.
# Deriving the wait from the same budgets guarantees a waiter never gives up
# before the holder releases, on any ``--timeout``. Releasing the lock earlier
# (before the reachability poll) is NOT an option: a waiter acquiring it during
# the winner's migration window would re-discover no live daemon and spawn too,
# defeating single-flight. Margin is a module constant so tests can shrink it.
_T2_ELECTION_WAIT_MARGIN: float = 5.0


def _election_wait_for(timeout: float) -> float:
    """Waiter election-lock budget: must exceed the holder's worst-case hold so
    waiters block until the winner is reachable, then attach rather than
    redundantly spawn (RDR-140 P2.2)."""
    return (
        _T2_CYCLE_DB_PROBE_TIMEOUT_MS / 1000.0
        + _T2_CYCLE_EXIT_TIMEOUT
        + timeout
        + _T2_ELECTION_WAIT_MARGIN
    )


def _election_lock_path_for_db(db_path: Path) -> Path:
    """Election-coordination lock path for *db_path*.

    RDR-140 P2.2: a sibling of the data file (``<db>.election_lock``) so stacks
    started from different ``config_dir``s against the same data file contend
    on one election. DISTINCT from the daemon's lifetime spawn lock
    (``<db>.spawn_lock`` / ``t2_spawn.lock``): if ``ensure-running`` held the
    daemon's own spawn lock, the spawned ``t2 start`` child would hit EAGAIN on
    its ``_acquire_spawn_lock`` and exit, leaving zero daemons.
    """
    return db_path.parent / f"{db_path.name}.election_lock"


def _acquire_election_lock(db_path: Path, timeout: float) -> int | None:
    """Blocking-with-timeout ``LOCK_EX`` on the election lock. Returns the held
    fd, or ``None`` if the timeout elapsed (caller proceeds unguarded).

    Blocking (not ``LOCK_NB``-fail-fast) so waiters queue then re-discover; the
    daemon's ``_acquire_spawn_lock`` uses ``LOCK_NB`` and must not, hence the
    distinct lock file. Auto-releases on holder death (the OS drops the fd's
    lock), so a holder that crashes mid-spawn never deadlocks the waiters.
    """
    import errno  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    import fcntl  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    path = _election_lock_path_for_db(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _release_election_lock(fd: int | None) -> None:
    if fd is None:
        return
    import fcntl  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# RDR-140 P4.2 (nexus-hrrpz) Gap 5: bounded crash-loop guard. Cold respawns are
# one-shot (``t2 start`` per ``ensure-running`` / launchd KeepAlive), so the
# guard is a persistent counter in a sentinel file beside the discovery file:
# restart timestamps within a rolling window. After _CRASHLOOP_MAX_RESTARTS in
# the window, ``ensure-running`` stops respawning and logs ONCE at error
# (instead of an endless crash-loop with a traceback per attempt). A daemon
# that converges (becomes reachable) clears the counter. This is suppression of
# respawn attempts, NOT a writer-lock change — RDR-128/129 single-writer is
# untouched. Module constants so tests can shrink the window/cap.
_CRASHLOOP_WINDOW_S: float = 300.0
_CRASHLOOP_MAX_RESTARTS: int = 5


def _crashloop_sentinel_path(config_dir: Path) -> Path:
    """Sentinel file path for the crash-loop guard, a sibling of the discovery
    file under *config_dir*."""
    return config_dir / "t2_crashloop.json"


def _read_crashloop(config_dir: Path) -> dict:
    try:
        data = json.loads(_crashloop_sentinel_path(config_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {"timestamps": [], "tripped_logged": False}
    if not isinstance(data, dict):
        return {"timestamps": [], "tripped_logged": False}
    ts = data.get("timestamps")
    data["timestamps"] = [t for t in ts if isinstance(t, (int, float))] if isinstance(ts, list) else []
    data["tripped_logged"] = bool(data.get("tripped_logged"))
    return data


def _write_crashloop_atomic(config_dir: Path, data: dict) -> None:
    """Atomic 0o600 write (mirrors ``_write_discovery_atomic``) so a concurrent
    reader never sees a partial sentinel."""
    path = _crashloop_sentinel_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(data).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _restart_count(config_dir: Path, *, now: float) -> int:
    """Restarts recorded within the window ending at *now* (read-only)."""
    cutoff = now - _CRASHLOOP_WINDOW_S
    data = _read_crashloop(config_dir)
    return sum(1 for t in data["timestamps"] if t >= cutoff)


def _record_restart(config_dir: Path, *, now: float) -> int:
    """Append a restart at *now*, prune entries older than the window, persist,
    and return the in-window count."""
    cutoff = now - _CRASHLOOP_WINDOW_S
    data = _read_crashloop(config_dir)
    kept = [t for t in data["timestamps"] if t >= cutoff]
    if not kept:
        # Fresh window (all prior restarts aged out): re-arm the one-shot
        # error log so a NEW crash loop is reported, not silently swallowed
        # by a stale tripped_logged flag from a previous window (code-review
        # HIGH-1).
        data["tripped_logged"] = False
    kept.append(now)
    data["timestamps"] = kept
    _write_crashloop_atomic(config_dir, data)
    return len(kept)


def _crashloop_tripped(config_dir: Path, *, now: float) -> bool:
    """True when the in-window restart count has reached the cap."""
    return _restart_count(config_dir, now=now) >= _CRASHLOOP_MAX_RESTARTS


def _reset_crashloop(config_dir: Path) -> None:
    """Clear the guard on healthy convergence (best-effort; never raises)."""
    try:
        _crashloop_sentinel_path(config_dir).unlink(missing_ok=True)
    except OSError:
        pass
















# ---------------------------------------------------------------------------
# service sub-group (RDR-152 P5.1, nexus-gmiaf.30)
# ---------------------------------------------------------------------------


@daemon_group.group("service")
def service_group() -> None:
    """Storage-service daemon: managed native service binary + local Postgres."""


@service_group.command("install")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help=(
        "Install OS autostart entry (launchd on macOS, systemd user unit on "
        "Linux) so the storage service starts at login / boot."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing plist/unit file even when its content "
    "differs from the freshly rendered template.",
)
def service_install_cmd(autostart: bool, force: bool) -> None:
    """Install the storage-service autostart entry for the current user.

    RDR-174 P2.1 (nexus-y2yj6): the service that serves every tier (engine
    binary + local Postgres) previously had no reboot-persistence. Thin wrapper
    over :func:`nexus.daemon.installer.install_autostart` with ``tier="service"``
    (mirrors ``nx daemon t2 install`` — same structured-result translation).
    The installed unit execs ``nx daemon service start --foreground``.
    """
    if not autostart:  # pragma: no cover — click enforces required=True
        raise click.UsageError("--autostart is required")

    from nexus.daemon import installer  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    try:
        result = installer.install_autostart(tier="service", force=force)
    except installer.SymlinkRefusedError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except installer.ContentDiffersError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except installer.ActivationError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if result.status is installer.InstallStatus.ALREADY_PRESENT:
        click.echo(result.detail)
        return

    click.echo(f"Wrote {result.dest}")
    for warning in result.warnings:
        click.echo(f"Warning: {warning}", err=True)
    if result.activated_cmd is not None:
        click.echo(f"Activated via: {' '.join(result.activated_cmd)}")


@service_group.command("uninstall")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help="Remove the OS autostart entry installed by ``install --autostart``.",
)
def service_uninstall_cmd(autostart: bool) -> None:
    """Remove the storage-service autostart entry for the current user.

    RDR-174 P2.1: completes the install/uninstall pair so the unit a user
    installs can be cleanly removed via ``nx`` (not stranded). Thin wrapper over
    :func:`nexus.daemon.installer.uninstall_autostart` with ``tier="service"``;
    mirrors ``nx daemon t2 uninstall``.
    """
    if not autostart:  # pragma: no cover — click enforces required=True
        raise click.UsageError("--autostart is required")

    from nexus.daemon import installer  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    result = installer.uninstall_autostart(tier="service")
    if result.status is installer.UninstallStatus.NOT_INSTALLED:
        click.echo(f"Autostart not installed (nothing at {result.dest}).")
        return
    for warning in result.warnings:
        click.echo(f"Warning: {warning}", err=True)
    click.echo(f"Removed {result.dest}")
    # nexus-dmgvx (GH #1419 Issue 2): removing the unit stops it coming BACK,
    # not what is running NOW. Printing a bare "Removed <path>" over a live
    # supervisor and a live Postgres is what sent Steve Harris hunting a
    # postgres process by hand. Exit stays 0 — the uninstall did succeed at
    # what it claims to do; the defect was silence, not the outcome.
    for survivor in result.survivors:
        click.echo(f"Still running: {survivor}", err=True)


def ensure_storage_supervisor(config_dir: Path):
    """Ensure a persistent (heartbeated) storage-service supervisor owns the lease.

    Returns the live :class:`LeaseRecord`. If a FRESH lease already exists this
    is a no-op (idempotent — re-running ``nx init --service`` / ``nx daemon
    service start`` is safe). Otherwise it detached-spawns the ``--foreground``
    supervisor (``start_new_session=True``) and waits up to 60s for it to publish
    a lease.

    Liveness is TTL-FRESHNESS, not process-aliveness: the short-circuit returns
    any lease whose heartbeat is within the ServiceRegistry TTL (a supervisor
    that crashed within the last TTL window still passes the freshness check and
    its lease expires shortly after). It also does not distinguish a supervised
    lease (``payload.supervisor_pid`` set) from a legacy transient one. In the
    current code paths this is sound because BOTH ``nx init --service`` and ``nx
    daemon service start`` route through here and the old transient
    ``start_storage_service`` init path is retired — so a fresh lease is a
    supervised one. A caller needing process-level liveness should poll the
    service ``/health`` endpoint directly (or ``nx service probe``).

    This is the SINGLE persistent-start path (nexus-qke1e): routing both surfaces
    through it means neither leaves a transient unsupervised lease that ages out
    by TTL because nothing heartbeats it. The bug it closes: ``start_storage_service``
    (the old init path) published a lease without a heartbeating supervisor, so
    the service looked 'serving' at init time but the lease aged out before the
    next client (e.g. ``nx migrate-to-service``) could discover it.

    Raises :class:`StorageServiceStartError` on a spawn that never becomes ready.
    """
    from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    from nexus.daemon import storage_service_daemon as _ssd  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    StorageServiceStartError = _ssd.StorageServiceStartError

    registry = ServiceRegistry(dir=config_dir, tier="storage_service")
    scope = str(os.getuid())
    existing = registry.discover(scope)
    if existing is not None:
        # RDR-175 heal-on-next-use hardening: a fresh (TTL-live) lease whose
        # ``supervisor_pid`` points at a DEAD process is a hard-crashed
        # supervisor (OOM-kill / SIGKILL with no relinquish). Without the OS
        # watchdog (no-autostart mode) nothing restarts it, and the lease would
        # otherwise be returned as a live endpoint for up to the TTL window.
        # Relinquish it and fall through to re-spawn. Reuses the exact guard from
        # ``stop_storage_service`` — an ABSENT ``supervisor_pid`` (legacy /
        # non-supervised lease) is left to the existing TTL-freshness
        # short-circuit, never re-spawned spuriously. (RDR-149-gate-safe: this is
        # in the storage-specific caller, not service_registry.discover.)
        supervisor_pid = existing.payload.get("supervisor_pid")
        if (
            isinstance(supervisor_pid, int)
            and supervisor_pid > 0
            and not _ssd._pid_is_alive(supervisor_pid)
        ):
            _log.warning(
                "storage_service_dead_lease_reclaim",
                supervisor_pid=supervisor_pid,
                msg="fresh lease held by a dead supervisor; relinquishing + re-spawning",
            )
            try:
                registry.relinquish(existing)
            except Exception as exc:  # noqa: BLE001 — best-effort reclaim; generation fencing still protects ownership
                # Don't fail the spawn: the new supervisor's publish bumps the
                # generation (fencing prevents double-ownership) and the 60s
                # discover-wait resolves once it lands. But log it — a silent
                # reclaim failure leaves no evidence for an operator.
                _log.warning(
                    "storage_service_dead_lease_relinquish_failed",
                    supervisor_pid=supervisor_pid,
                    error=str(exc),
                )
        else:
            return existing

    argv = [
        *_resolve_nx_bin(), "daemon", "service", "start", "--foreground",
        "--config-dir", str(config_dir),
    ]
    # nexus-ovbr7: route the child's streams to a crash-channel file so a failure
    # BEFORE run_storage_supervisor's configure_logging runs (import error, bad
    # argv) and interpreter-fatal tracebacks are captured. Post-configure, the
    # daemon drops its stderr handler (non-tty), so this file stays quiet healthy.
    from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    spawn_log = open_child_log_or_devnull("storage_service.crash", config_dir)
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,  # detached daemon: never inherit a TTY stdin (avoids read-block / dangling fd)
            stdout=spawn_log,
            stderr=spawn_log,
            start_new_session=True,
        )
    finally:
        if not isinstance(spawn_log, int):
            spawn_log.close()
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        existing = registry.discover(scope)
        if existing is not None:
            return existing
        time.sleep(0.5)
    raise StorageServiceStartError(
        "Storage service supervisor did not become ready within 60s. "
        f"Check {config_dir / 'logs' / 'storage_service.log'} "
        "or run 'nx daemon service start --foreground' to see the error."
    )


@service_group.command("start")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override (default: ~/.config/nexus/).",
)
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help=(
        "Block until SIGTERM/SIGINT or the service exits. Required when "
        "launched under a supervisor (launchd, systemd)."
    ),
)
@click.option(
    "--announce-stdout",
    "announce_stdout",
    is_flag=True,
    default=False,
    help="Emit the discovery JSON on stdout at startup.",
)
def service_start_cmd(
    config_dir_str: str | None,
    foreground: bool,
    announce_stdout: bool,
) -> None:
    """Start the native storage-service + Postgres supervisor (RDR-152 P5.1).

    Reads pg_credentials from the config directory (written by 'nx init
    --service'), starts the nx-managed Postgres cluster if it is not
    running, spawns the native nexus-service binary (RDR-161: the sole launch
    artifact; acquire it via 'nx daemon service install-binary'), waits for
    /health to return 200, then publishes the service endpoint to the
    ServiceRegistry under the 'storage_service' scope key.

    Without ``--foreground`` the command ensures the supervisor is running
    (spawning one in the background if needed) and exits. With
    ``--foreground`` the supervisor blocks until SIGTERM/SIGINT.

    A service/PG outage is always fatal — there is no direct-mode
    fallback (per RDR-152 §Approach).
    """
    from nexus.daemon.storage_service_daemon import (  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        StorageServiceStartError,
        run_storage_supervisor,
    )

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()

    if foreground:
        try:
            code = run_storage_supervisor(config_dir=config_dir)
        except StorageServiceStartError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        sys.exit(code)

    # Non-foreground: ensure a persistent (heartbeated) supervisor owns the lease.
    try:
        existing = ensure_storage_supervisor(config_dir)
    except StorageServiceStartError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    ep = existing.endpoint
    if announce_stdout:
        import json as _json  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        click.echo(_json.dumps({
            "host": ep.get("host"),
            "port": ep.get("port"),
            "pid": ep.get("pid"),
            "generation": existing.generation,
        }))
    else:
        click.echo(
            f"Storage service running on {ep.get('host')}:{ep.get('port')} "
            f"(pid={ep.get('pid')}, generation={existing.generation})."
        )


# The recovery-playbook URL moved to nexus.remediation.MIGRATION_RUNBOOK_URL
# with the RDR-182 P1.3 hoist (nexus-ykzbj.7) — the shared Playbook emitter
# is now the single source of truth for the gate's guidance text.


def _emit_chash_poison_gate(config_dir: Path, *, force: bool) -> None:
    """nexus-pnwu0 / GH #1414 upgrade gate (see call site).

    Classifies the store via the shared tri-state probe
    (:func:`nexus.upgrade_finish._poison_probe`, the same classification
    ``converge_engine``'s gate uses — nexus-pgdcv round-2 HIGH-1: the two
    gates must not diverge on the unknown state):

    - POISONED: refuse the install unless *force*. Emits the full clickable
      runbook URL AND a ready-to-paste prompt the operator can hand to
      their own Claude — the agent-runnable remediation pattern.
    - UNKNOWN (probe could not verify — PG down, missing nexus_diag creds):
      proceed WITH A LOUD WARNING, never silently. install-binary is the
      designated recovery tool for the will-not-boot class where the store
      is by definition unreachable, so unknown must not hard-block (the
      load-bearing never-brick rule) — but the old silent fail-open was
      the same bug class converge_engine's gate had.
    - CLEAN: proceed quietly.
    """
    from nexus.upgrade_finish import _poison_probe  # noqa: PLC0415 — deferred, CLI startup cost

    probe = _poison_probe(config_dir)
    if probe.unknown_reason is not None:
        click.echo(
            "WARNING: the chash-conformance pre-check could not verify this "
            f"store ({probe.unknown_reason}) — proceeding, because "
            "install-binary is the designated recovery path when the "
            "service cannot boot. The store's conformance is UNVERIFIED: "
            "run `nx doctor` after the service is up.",
            err=True,
        )
        return
    if probe.playbook is None:
        return

    # RDR-182 P1.3 (nexus-ykzbj.7): guidance text lives in the shared
    # Playbook emitter — one source of truth for this gate AND the Phase-3
    # MCP forensics/remediate tools. Locked to the nexus-o513u ladder-first
    # contract by tests/remediation/test_playbook.py; this function keeps
    # only the probe + flow control (force branch, exit code).
    playbook = probe.playbook
    if force:
        click.echo(playbook.force_override_warning(), err=True)
        return

    click.echo(playbook.terminal_block(), err=True)
    sys.exit(3)


@service_group.command("install-binary")
@click.argument("tag", required=True)
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option(
    "--pg-bundle/--no-pg-bundle",
    "want_pg_bundle",
    default=True,
    help="Also acquire+verify the relocatable PostgreSQL bundle from the same "
         "release (default). --no-pg-bundle installs only the service binary "
         "(e.g. cloud habitat with a managed Postgres).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Override the chash-poison pre-check (nexus-pnwu0 / GH #1414). Use "
         "ONLY after healing per docs/migration-runbook.md §8.1 (ladder-first: "
         "re-index legacy collections, nx upgrade, doctor clears). The rows "
         "stay unhealed debt if you force past them; a pre-v0.1.48 char-era "
         "engine can still crash-loop on boot.",
)
def service_install_binary_cmd(
    tag: str, config_dir_str: str | None, want_pg_bundle: bool, force: bool,
) -> None:
    """Download, verify, and install the signed native nexus-service binary
    (and, by default, the PostgreSQL bundle) from a release.

    TAG is an EXPLICIT engine-service-v* release tag (e.g. engine-service-v0.1.3);
    there is no "latest" resolution. Each per-platform asset, its .sha256, and its
    .sigstore.json bundle are fetched from the GitHub release, verified
    (sha256 + keyless Sigstore signature, pinned to this repo's release workflow
    identity), then placed under <config-dir>/service/. Verification fails closed:
    nothing is installed unless BOTH gates pass. One verified seam covers the
    binary and the PG bundle (RDR-161).
    """
    from importlib.metadata import PackageNotFoundError, version as _pkg_version  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    from nexus.daemon.binary_install import (  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        BinaryVerificationError,
        asset_name,
        install_binary,
        install_pg_bundle,
        pg_bundle_asset_name,
    )

    try:
        _nx_version = _pkg_version("conexus")
    except PackageNotFoundError:
        _nx_version = "unknown"

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    installed_by = f"conexus {_nx_version}"

    # nexus-pnwu0 / GH #1414 upgrade gate: refuse to install a new engine onto a
    # store whose pgvector target holds width-non-conformant chash rows. The
    # rows are unhealed upgrade-ladder debt the operator should converge FIRST
    # (nexus-o513u ladder-first: re-index legacy collections -> nx upgrade ->
    # doctor clears); swapping engine binaries mid-debt just churns the boot
    # path while the heal is pending. Note the original crash-loop premise was
    # narrowed by nexus-joima (2026-07-21): v0.1.48+ engines tolerate the rows
    # at boot (octet checks NOT VALID until the rekey rung); only a pre-v0.1.48
    # char-era engine can still crash-loop on catalog-013-3's first VALIDATE
    # (no guard against present-but-violating constraints, and the changelog
    # cannot cleanly add one — the count query runs under FORCE RLS as the
    # NOBYPASSRLS migration role and sees zero of the very rows VALIDATE then
    # trips on). This is the actual gate the passive `nx doctor` probe could
    # not enforce. The probe reuses _check_migration_state's chash query so there
    # is one source of truth; a probe failure never blocks a legitimate install.
    _emit_chash_poison_gate(config_dir, force=force)

    click.echo(f"Resolving {asset_name()} from release {tag}…")
    try:
        dest, prov = install_binary(tag, config_dir, installed_by=installed_by)
    except BinaryVerificationError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    click.echo(f"Installed {prov['asset']} ({tag})")
    click.echo(f"  -> {dest}")
    click.echo(f"  version: {prov['version']}")
    click.echo(f"  sha256:  {prov['sha256'][:16]}…")
    click.echo(f"  signature: verified (keyless Sigstore, {prov['source_url']})")

    if want_pg_bundle:
        click.echo(f"\nResolving {pg_bundle_asset_name()} from release {tag}…")
        try:
            pg_dest, pg_prov = install_pg_bundle(
                tag, config_dir, installed_by=installed_by,
            )
        except BinaryVerificationError as exc:
            # The binary already installed and verified; only the PG bundle
            # failed. Say so, and don't print the "restart the service" hint
            # below (the service can't start without the bundle in local mode).
            click.echo(
                f"Error: the native binary installed OK, but the PostgreSQL "
                f"bundle failed: {exc}\n"
                "Re-run `nx daemon service install-binary <tag>` to retry the "
                "bundle (the binary step is idempotent), or pass --no-pg-bundle "
                "if you run against a managed Postgres.",
                err=True,
            )
            sys.exit(2)
        click.echo(f"Installed {pg_prov['asset']} ({tag})")
        click.echo(f"  -> {pg_dest}")
        click.echo(f"  sha256:  {pg_prov['sha256'][:16]}…")
        click.echo("  signature: verified (keyless Sigstore)")

    click.echo("\nRestart the service to pick it up: nx daemon service stop && "
               "nx daemon service start")


@service_group.command("stop")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option(
    "--with-pg",
    "with_pg",
    is_flag=True,
    default=False,
    help="Also stop the nx-managed Postgres cluster via pg_ctl -m fast "
         "(terminates open connections immediately; left running by default).",
)
def service_stop_cmd(config_dir_str: str | None, with_pg: bool) -> None:
    """Stop the running storage-service supervisor (SIGTERM -> SIGKILL).

    Postgres is INTENTIONALLY left running (it is independently managed and
    may serve other clients) — nexus-pebfx.5 makes that visible instead of
    surprising: the command says so and offers --with-pg.
    """
    from nexus.daemon.storage_service_daemon import (  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        _port_accepting,
        _read_pg_credentials,
        stop_storage_service,
    )

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    pid = stop_storage_service(config_dir=config_dir)
    if pid is None:
        click.echo("No storage service lease found — already stopped.")
    else:
        click.echo(f"Storage service stopped (pid={pid}).")

    creds_path = config_dir / "pg_credentials"
    if not creds_path.exists():
        return
    try:
        creds = _read_pg_credentials(creds_path)
    except OSError:
        return
    port_str = creds.get("PG_PORT", "")
    if not port_str.isdigit() or not _port_accepting("127.0.0.1", int(port_str)):
        return

    if not with_pg:
        if pid is None:
            # Nothing was stopped — phrase as a state report, not an effect
            # of this command (critic S4: the causal phrasing misled when
            # the supervisor was already gone).
            click.echo(
                f"Postgres is still running on 127.0.0.1:{port_str} — use "
                "'nx daemon service stop --with-pg' to stop it."
            )
        else:
            click.echo(
                f"Postgres left running on 127.0.0.1:{port_str} (by design — "
                "it is independently managed; use 'nx daemon service stop "
                "--with-pg' to stop it too)."
            )
        return

    pg_data = creds.get("PG_DATA", "")
    if not pg_data:
        click.echo(
            "--with-pg: PG_DATA missing from pg_credentials — cannot stop "
            "Postgres. Stop it manually with pg_ctl.",
            err=True,
        )
        sys.exit(2)
    import subprocess  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    from nexus.db.pg_provision import discover_pg_binaries  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    try:
        bins = discover_pg_binaries()
        subprocess.run(
            [str(bins.pg_ctl), "-D", pg_data, "-m", "fast", "stop"],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 — boundary catch around PG stop; surfaced via click.echo + exit(2)
        click.echo(f"--with-pg: failed to stop Postgres: {exc}", err=True)
        sys.exit(2)
    click.echo(f"Postgres stopped (port {port_str}).")


def _probe_health(host: str, port: int, timeout: float = 3.0) -> str:
    """GET /health → "ok" | "db-down" | "unreachable" (nexus-pebfx.5)."""
    import urllib.request  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/health", timeout=timeout,
        ) as resp:
            return "ok" if resp.status == 200 else f"http-{resp.status}"
    except Exception as exc:  # noqa: BLE001 — boundary catch of urllib/transport errors; mapped to db-down status
        import urllib.error  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

        if isinstance(exc, urllib.error.HTTPError) and exc.code == 503:
            return "db-down"
        return "unreachable"


def _probe_pg(creds_path: Path) -> dict:
    """PG cluster facts for the status surface (nexus-pebfx.5).

    Best-effort: every field degrades to a readable placeholder rather than
    failing the status command — status must work BEST when the stack is
    broken.
    """
    out: dict = {}
    if not creds_path.exists():
        out["pg"] = "not provisioned (run: nx init --service)"
        return out
    from nexus.daemon.storage_service_daemon import (  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        _port_accepting,
        _read_pg_credentials,
    )

    try:
        creds = _read_pg_credentials(creds_path)
    except OSError:
        out["pg"] = f"credentials unreadable: {creds_path}"
        return out
    port_str = creds.get("PG_PORT", "")
    out["pg_port"] = port_str or "(missing from pg_credentials)"
    out["pg_data"] = creds.get("PG_DATA", "(missing from pg_credentials)")
    pg_up = bool(port_str.isdigit()) and _port_accepting("127.0.0.1", int(port_str))
    out["pg"] = "up" if pg_up else "DOWN"
    if pg_up:
        out["pgvector"] = _pgvector_version(creds) or "(query failed)"
    return out


def _pgvector_version(creds: dict) -> str | None:
    """Installed pgvector extension version via psql (admin creds)."""
    import subprocess  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    from nexus.daemon.binary_lifecycle import _db_name_from_creds, _psql_bin  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    psql = _psql_bin()
    if psql is None:
        return None
    user = creds.get("NX_DB_ADMIN_USER", "") or creds.get("NX_DB_USER", "")
    password = (
        creds.get("NX_DB_ADMIN_PASS", "")
        if creds.get("NX_DB_ADMIN_USER", "")
        else creds.get("NX_DB_PASS", "")
    )
    if not user:
        return None
    import os as _os  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    env = dict(_os.environ)
    env["PGPASSWORD"] = password
    try:
        result = subprocess.run(
            [
                psql, "-h", "127.0.0.1", "-p", str(creds.get("PG_PORT", "")),
                "-U", user, "-d", _db_name_from_creds(creds),
                "-t", "-A", "-X",
                "-c", "SELECT extversion FROM pg_extension WHERE extname='vector'",
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    version = result.stdout.strip()
    return version or "NOT INSTALLED"


def _nx_major_gap_note(installed_by: str) -> str | None:
    """Note when the well-known binary was installed by an older nx MAJOR.

    ``installed_by`` is the sidecar's ``"conexus X.Y.Z"`` stamp. Returns
    ``None`` when versions are unparseable or majors match.
    """
    import re as _re  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    from importlib.metadata import PackageNotFoundError, version as _pkg_version  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    m = _re.match(r"conexus (\d+)\.", installed_by or "")
    if not m:
        return None
    installed_major = int(m.group(1))
    try:
        current_major = int(_pkg_version("conexus").split(".")[0])
    except (PackageNotFoundError, ValueError):
        return None
    if installed_major < current_major:
        return (
            f"installed service binary was installed by {installed_by} but this "
            f"nx is major version {current_major} — reinstall it from a current "
            "build: nx daemon service install-binary <tag>"
        )
    return None


@service_group.command("status")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def service_status_cmd(config_dir_str: str | None, as_json: bool) -> None:
    """Print the storage-service endpoint (host, port, pid, generation).

    Exits non-zero when no live lease is found.
    """
    import json as _json  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    import os as _os  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    registry = ServiceRegistry(dir=config_dir, tier="storage_service")
    scope = str(_os.getuid())
    record = registry.discover(scope)

    if record is None:
        click.echo(
            "No storage service lease found — is the service running?",
            err=True,
        )
        sys.exit(1)

    ep = record.endpoint
    data = {
        "host": ep.get("host"),
        "port": ep.get("port"),
        "pid": ep.get("pid"),
        "generation": record.generation,
        "version": record.version,
        "heartbeat_epoch": record.heartbeat_epoch,
        "status": record.status,
    }

    # nexus-pebfx.5: one surface answering "is the stack healthy and how is
    # it configured" — supervisor, native service (/health + /version), PG cluster,
    # embedding mode, pgvector version, and the paths an operator would
    # otherwise assemble from ps aux + psql + curl + the addr file by hand.
    import os as _os  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    data["supervisor_pid"] = record.payload.get("supervisor_pid")
    data["addr_file"] = str(config_dir / f"storage_service_addr.{_os.getuid()}")
    host = ep.get("host", "127.0.0.1")
    port = int(ep.get("port") or 0)
    data["health"] = _probe_health(host, port)

    creds_path = config_dir / "pg_credentials"
    data["pg_credentials"] = str(creds_path) if creds_path.exists() else "(absent)"
    pg_info = _probe_pg(creds_path)
    data.update(pg_info)

    # nexus-ovbr7: surface where the evidence lives. Every component of the
    # stack writes a log file; an operator triaging a death should not have
    # to know the layout by heart.
    data["supervisor_log"] = str(config_dir / "logs" / "storage_service.log")
    data["service_log"] = str(config_dir / "logs" / "storage_service_native.log")
    data["crash_log"] = str(config_dir / "logs" / "storage_service.crash.log")
    if pg_info.get("pg_data"):
        data["pg_log"] = str(Path(pg_info["pg_data"]) / "pg.log")

    # nexus-pebfx.4 version handshake: report the RUNNING service's app +
    # schema versions, and warn when they drift from the binary installed at
    # the well-known location (a stale service that needs a restart).
    from nexus.daemon.binary_lifecycle import (  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        fetch_service_version,
        read_installed_provenance,
    )
    # Probe-latency guard (pebfx.5 critic S1): both HTTP probes hit the same
    # host/port — when /health is unreachable, /version cannot succeed, and
    # status is invoked MOST when the stack is broken. Skip the second 3s
    # timeout.
    svc_version = (
        fetch_service_version(host, port)
        if data["health"] != "unreachable"
        else None
    )
    stale_warning: str | None = None
    if svc_version is not None:
        data["service_app_version"] = svc_version.get("app_version")
        # RDR-002: release_version is the release identity (app_version is now the
        # frozen dev coordinate 1.0-SNAPSHOT and can no longer be compared against
        # the installed binary's tag-derived version).
        data["service_release_version"] = svc_version.get("release_version")
        data["embedding_mode"] = svc_version.get("embedding_mode", "unknown")
        if svc_version.get("embedding_models"):
            # Kept as a list: --json consumers get the same array shape the
            # /version endpoint emits (CRE round-trip-fidelity finding).
            data["embedding_models"] = svc_version["embedding_models"]
        data["schema_latest_id"] = svc_version.get("schema_latest_id")
        data["schema_changeset_count"] = svc_version.get("schema_changeset_count")
        installed = read_installed_provenance(config_dir)
        # RDR-002: compare the installed binary's tag-derived version against the
        # running service's release_version (both are the e.g. "0.1.6" release
        # semver), NOT app_version (permanently "1.0-SNAPSHOT" by contract, which
        # would false-positive stale on every call). A dev/unstamped service
        # reports release_version=null → no comparison → no spurious warning.
        svc_release = svc_version.get("release_version")
        if (
            installed is not None
            and installed.get("version")
            and svc_release
            and installed["version"] != svc_release
        ):
            stale_warning = (
                f"running service is release_version={svc_release} "
                f"but the installed binary is {installed['version']} — restart to "
                "pick it up: nx daemon service stop && nx daemon service start"
            )
            data["stale"] = True
        # Bead pebfx.4(b): warn when the installed binary predates the current
        # nx by a major version — the proactive "this binary was installed by a
        # much older nx" signal (binary and nx version schemes are otherwise
        # incomparable).
        if installed is not None and not stale_warning:
            note = _nx_major_gap_note(installed.get("installed_by", ""))
            if note:
                stale_warning = note
                data["installed_by_outdated"] = True

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo("Storage Service Status")
    click.echo("-" * 40)
    for key, value in data.items():
        # Lists (embedding_models) stay arrays in --json; join for humans.
        display = ", ".join(value) if isinstance(value, list) else value
        click.echo(f"  {key}: {display}")
    if stale_warning:
        click.echo(f"warning: {stale_warning}", err=True)


# ── Aspect-worker daemon (RDR-173): leased, per-tenant host for aspect extraction ──


@daemon_group.group("aspect-worker")
def aspect_worker_group() -> None:
    """Aspect-worker daemon: a leased, per-tenant host for the aspect-extraction
    loop (claim → claude -p → upsert document_aspects → mark_done) and the
    reclaim_stale loop. One more leased tier on the RDR-149 substrate."""


@aspect_worker_group.command("start")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override (default: ~/.config/nexus/).",
)
@click.option(
    "--tenant",
    "tenant",
    default="default",
    help="Tenant scope for the lease (per-tenant; per-host needs BYPASSRLS, forbidden by RDR-152).",
)
@click.option(
    "--stale-timeout-seconds",
    "stale_timeout_seconds",
    type=int,
    default=300,
    show_default=True,
    help="Reclaim staleness threshold; MUST exceed the claude -p extraction budget (180s) "
    "or an in-flight row could be false-reclaimed.",
)
def aspect_worker_start_cmd(
    config_dir_str: str | None, tenant: str, stale_timeout_seconds: int,
) -> None:
    """Start the aspect-worker daemon (foreground; runs until SIGTERM/SIGINT).

    CREDENTIAL MODEL (RDR-173): this MUST be spawned as a CHILD of a process
    that already has ``claude -p`` credentials so it inherits the ``claude``
    binary on ``PATH``, ``~/.claude``, and the Anthropic credential context.
    The enqueue-hook spawn (Phase 2) Popens this command from the storing
    process precisely so that inheritance happens; a credential-bare invocation
    will fail extraction. The daemon rides the registry's per-tenant lease, so a
    second start for the same tenant fences the predecessor (one owner survives).
    """
    from nexus.daemon.aspect_worker_daemon import run_aspect_worker_daemon  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    click.echo(
        f"Aspect-worker daemon starting (config_dir={config_dir}, tenant={tenant})..."
    )
    run_aspect_worker_daemon(
        config_dir=config_dir, tenant=tenant,
        stale_timeout_seconds=stale_timeout_seconds,
    )


@daemon_group.command("restart-stale")
@click.option("--dry-run", is_flag=True, default=False,
              help="Report what would be restarted without touching anything.")
def restart_stale_cmd(dry_run: bool) -> None:
    """Finish an upgrade: restart processes still running old code, converge
    the local engine to the release dependency, and heal diag-view drift.

    After ``uv tool upgrade conexus`` the disk is new but every long-lived
    process (MCP hosts, aspect-worker, MinerU) keeps executing the old
    code from memory (nexus-4xgfy). This verb restarts the classes that
    are safe to cycle and names the ones only you can close (MCP hosts
    belong to live Claude sessions). It also converges a service-mode
    install's engine binary to the release's required version (nexus-cfgo9:
    the ONE-engine model — a version mismatch is fixed here, not merely
    refused), and repairs ``nexus.diag_chash_conformance`` grant/ownership
    drift (GH #1402's second symptom — GRANT/ALTER OWNER only, no view
    DDL). It also removes a stray ``com.nexus.t2`` LaunchAgent on a
    service-mode box (nexus-c0vby, GH #1405 defect 2 — a service-mode
    box's T2 daemon never starts, so a leftover agent's KeepAlive would
    otherwise respawn an immediately-exiting process forever). Runs
    automatically on the first ``nx`` invocation after a version change;
    this is the manual form (also the re-run path for convergence outside
    a version transition, e.g. after remediating a chash-poison block).
    """
    from nexus.upgrade_finish import (  # noqa: PLC0415 — deferred import
        converge_engine,
        detect_engine_convergence,
        detect_stale_processes,
        heal_diag_view,
        install_source,
        restart_stale,
        unload_stale_t2_launchagent,
    )

    # nexus-cfgo9: process-skew detection is independent of engine
    # convergence and the diag-view heal below — a `ps`-less environment
    # (e.g. a minimal container with no procps) must not prevent the other
    # two legs from running. Previously this was unguarded and a
    # FileNotFoundError here aborted the whole command before convergence
    # ever got a chance to fire.
    try:
        report = detect_stale_processes()
        click.echo(f"installed: conexus {report.installed_version}  "
                   f"(source: {install_source()})")
        if not report.stale:
            click.echo("no stale processes — the machine matches the disk.")
        else:
            for line in restart_stale(report, dry_run=dry_run):
                click.echo(f"  {line}")
    except Exception as exc:  # noqa: BLE001 — one leg's failure must not block the others
        click.echo(f"process-skew detection failed ({exc}) — skipping this leg.", err=True)

    config_dir = nexus_config_dir()

    # nexus-cfgo9 code-review HIGH: converge_engine documents a "never
    # raises" contract, but that contract lives in one function's docstring
    # -- defense-in-depth wraps the call site too, the same independent-leg
    # pattern as the process-skew try/except above. Without this, a gap in
    # converge_engine's own exception handling (or a future regression of
    # it) would abort the command before the diag-view heal leg below ever
    # runs -- the identical asymmetry the process-skew fix closed.
    try:
        engine_actions = converge_engine(config_dir, dry_run=dry_run)
        if not engine_actions:
            # nexus-4yf4u (GH #1419 Issue 1): the predecessor printed one line
            # for an empty action list — "no convergence action needed
            # (already converged, or not on the local service stack)" —
            # conflating CONVERGED with NOT-APPLICABLE. On a box where
            # convergence was silently doing nothing, that line read as
            # reassurance. EngineConvergence already carries the distinction;
            # print it rather than the disjunction.
            status = detect_engine_convergence(config_dir)
            if not status.applicable:
                click.echo(
                    f"engine: not applicable — {status.reason or 'not on the local service stack'}"
                )
            else:
                # Review CRE-A finding 1 (High): this line must claim ONLY what
                # detect_engine_convergence actually knows — the ON-DISK fact.
                # It re-derives status from the provenance sidecar and never
                # probes the running service, so an earlier "on-disk and
                # running engine are vX" asserted an unobserved fact. Anything
                # true of the RUNNING engine arrives as an action line from
                # converge_engine, which is what does the live probe.
                req_s = ".".join(str(p) for p in status.required_version)
                click.echo(
                    f"engine: converged — installed engine is v{req_s} "
                    "(the release dependency)"
                )
        else:
            for line in engine_actions:
                click.echo(f"  {line}")
    except Exception as exc:  # noqa: BLE001 — one leg's failure must not block the others
        click.echo(f"engine convergence failed ({exc}) — skipping this leg.", err=True)

    if dry_run:
        click.echo("diag-view heal: skipped (--dry-run — GRANT/ALTER OWNER "
                    "are not previewed).")
    else:
        try:
            heal_actions = heal_diag_view(config_dir)
            if not heal_actions:
                click.echo(
                    "diag-view heal: no action needed (grants/ownership already "
                    "healthy, or not on the local service stack)."
                )
            else:
                for line in heal_actions:
                    click.echo(f"  {line}")
        except Exception as exc:  # noqa: BLE001 — one leg's failure must not block the others
            click.echo(f"diag-view heal failed ({exc}) — skipping this leg.", err=True)

    # nexus-c0vby: independent leg, same defense-in-depth pattern as the two
    # above — a gap in unload_stale_t2_launchagent's own handling must not
    # abort the command or hide the other legs' output.
    if dry_run:
        click.echo("T2 LaunchAgent unload: skipped (--dry-run).")
    else:
        try:
            unload_actions = unload_stale_t2_launchagent(config_dir)
            if not unload_actions:
                click.echo(
                    "T2 LaunchAgent: no action needed (not service mode, or "
                    "no stray agent installed)."
                )
            else:
                for line in unload_actions:
                    click.echo(f"  {line}")
        except Exception as exc:  # noqa: BLE001 — one leg's failure must not block the others
            click.echo(f"T2 LaunchAgent unload failed ({exc}) — skipping this leg.", err=True)
