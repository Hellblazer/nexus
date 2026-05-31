# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon`` command group — manage the T3 storage daemon.

RDR-120 P1.A (nexus-41unl): introduces the T3 daemon (managed
``chroma run`` subprocess). The T2 sub-group ships in RDR-120 P3a.

Subcommands:
    nx daemon t3 start [--foreground]   Start the managed chroma subprocess
    nx daemon t3 stop                    Send SIGTERM to the running daemon
    nx daemon t3 status                  Print discovery file (PID + address)
    nx daemon t3 install --autostart     Install launchd/systemd unit
    nx daemon t3 uninstall --autostart   Remove launchd/systemd unit
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
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import click

from nexus.config import nexus_config_dir


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group("daemon")
def daemon_group() -> None:
    """Manage storage daemons (T3; T2 ships in RDR-120 P3a)."""


# ---------------------------------------------------------------------------
# Autostart helpers (shared with future T2 install/uninstall)
# ---------------------------------------------------------------------------


_T3_PLIST_NAME = "com.nexus.t3.plist"
_T3_SERVICE_NAME = "nexus-t3.service"
_T3_LAUNCHD_LABEL = "com.nexus.t3"

_T2_PLIST_NAME = "com.nexus.t2.plist"
_T2_SERVICE_NAME = "nexus-t2.service"
_T2_LAUNCHD_LABEL = "com.nexus.t2"


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
    from importlib.resources import as_file, files

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


def _autostart_filename_t3() -> str:
    return _T3_PLIST_NAME if _autostart_platform() == "darwin" else _T3_SERVICE_NAME


def _autostart_filename_t2() -> str:
    return _T2_PLIST_NAME if _autostart_platform() == "darwin" else _T2_SERVICE_NAME


# ---------------------------------------------------------------------------
# t3 sub-group
# ---------------------------------------------------------------------------


@daemon_group.group("t3")
def t3_group() -> None:
    """T3 daemon — managed chroma run subprocess (local mode only)."""


@t3_group.command("start")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override (default: ~/.config/nexus/).",
)
@click.option(
    "--local-path",
    "local_path_str",
    default=None,
    help=(
        "Override the chroma persistent path. Default: "
        "``nexus.config._default_local_path()`` (XDG-aware, "
        "~/.local/share/nexus/chroma)."
    ),
)
@click.option(
    "--announce-stdout",
    "announce_stdout",
    is_flag=True,
    default=False,
    help=(
        "Emit the discovery JSON on stdout at startup. Default off: "
        "the discovery file at ~/.config/nexus/t3_addr.<uid> is the "
        "primary channel."
    ),
)
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help=(
        "Block until SIGTERM/SIGINT or chroma exits. Required when "
        "launched under a supervisor (launchd, systemd) so the "
        "supervisor sees the daemon stay up. Without this flag the "
        "CLI exits after writing the discovery file, leaving chroma "
        "as a session-detached subprocess (the supervisor sees a "
        "zero-exit and never triggers KeepAlive / Restart=on-failure)."
    ),
)
def t3_start_cmd(
    config_dir_str: str | None,
    local_path_str: str | None,
    announce_stdout: bool,
    foreground: bool,
) -> None:
    """Start the T3 chroma daemon (local mode only).

    Idempotent on a live daemon: if a discovery file exists and its PID
    is still alive, prints the existing discovery payload without
    spawning a duplicate. Cloud mode (NX_LOCAL=0) fails loud — chromadb
    CloudClient is already HTTP-served.

    Without ``--foreground`` the CLI exits as soon as the chroma
    subprocess is listening on its TCP port. ``--foreground`` blocks
    until SIGTERM/SIGINT (or chroma exits on its own); used by the
    launchd/systemd autostart templates so the supervisor observes a
    long-running foreground process and can react to crashes via
    ``KeepAlive.Crashed`` / ``Restart=on-failure``.
    """
    from nexus.config import _default_local_path
    from nexus.daemon.t3_daemon import (
        T3CloudModeError,
        T3StartError,
        _pid_is_alive,
        start_t3_daemon,
        stop_t3_daemon,
    )

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    local_path = Path(local_path_str) if local_path_str else _default_local_path()
    try:
        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    except T3CloudModeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except T3StartError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    if announce_stdout:
        click.echo(json.dumps(payload))
    else:
        click.echo(
            f"T3 daemon running on {payload['tcp_host']}:{payload['tcp_port']} "
            f"(pid={payload['pid']}, local_path={payload['local_path']})."
        )

    if not foreground:
        return

    # --foreground: supervisor-friendly blocking loop. The chroma
    # subprocess is in its own session (start_new_session=True), so
    # SIGTERM to this CLI does not propagate automatically — the signal
    # handler below explicitly calls stop_t3_daemon to clean up.
    stop_requested = threading.Event()

    def _on_signal(_signum, _frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    pid = payload["pid"]
    while not stop_requested.is_set():
        if not _pid_is_alive(pid):
            click.echo(
                f"T3 chroma subprocess (pid={pid}) exited unexpectedly; "
                "the supervisor will restart it.",
                err=True,
            )
            sys.exit(3)
        time.sleep(0.5)

    stop_t3_daemon(config_dir=config_dir)
    sys.exit(0)


@t3_group.command("stop")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
def t3_stop_cmd(config_dir_str: str | None) -> None:
    """Stop the running T3 daemon (graceful SIGTERM → SIGKILL escalation)."""
    from nexus.daemon.t3_daemon import stop_t3_daemon

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    pid = stop_t3_daemon(config_dir=config_dir)
    if pid is None:
        click.echo("No T3 daemon discovery file found — already stopped.")
        return
    click.echo(f"T3 daemon stopped (pid={pid}).")


@t3_group.command("status")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Output raw JSON."
)
def t3_status_cmd(config_dir_str: str | None, as_json: bool) -> None:
    """Print the T3 daemon discovery JSON (PID, bound address, paths).

    RDR-120 bead nexus-41unl acceptance: reports PID + bound address.
    Exits non-zero when no discovery file exists.
    """
    from nexus.daemon.t3_daemon import t3_discovery_path

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = t3_discovery_path(config_dir)
    if not disc.exists():
        click.echo(
            "No T3 daemon discovery file found — is the daemon running?",
            err=True,
        )
        sys.exit(1)
    try:
        data = json.loads(disc.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read discovery file: {exc}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    click.echo("T3 Daemon Status")
    click.echo("-" * 40)
    for key, value in data.items():
        click.echo(f"  {key}: {value}")


# ---------------------------------------------------------------------------
# nx daemon t3 install / uninstall  (launchd plist / systemd user unit)
# ---------------------------------------------------------------------------


@t3_group.command("install")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help=(
        "Install OS autostart entry (launchd on macOS, systemd user "
        "unit on Linux) so the T3 daemon starts at login / boot."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Overwrite an existing plist/unit file even when its content "
        "differs from the freshly rendered template; treat supervisor "
        "activation failures as warnings instead of errors."
    ),
)
def t3_install_cmd(autostart: bool, force: bool) -> None:
    """Install the T3 daemon autostart entry for the current user.

    macOS: writes ``~/Library/LaunchAgents/com.nexus.t3.plist`` and
    bootstraps it via ``launchctl bootstrap gui/$UID``.

    Linux: writes ``~/.config/systemd/user/nexus-t3.service`` and
    enables it via ``systemctl --user enable --now nexus-t3.service``.

    The shipped templates point the supervisor at
    ``nx daemon t3 start --foreground``.
    """
    if not autostart:  # pragma: no cover -- click enforces required=True
        raise click.UsageError("--autostart is required")

    install_dir = _autostart_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    log_dir = _autostart_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    template_name = _autostart_filename_t3()
    nx_bin = _resolve_nx_bin()
    rendered = _render_template(
        template_name,
        nx_bin=nx_bin,
        log_dir=str(log_dir),
        path_env=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    )
    dest = install_dir / template_name

    if dest.is_symlink():
        click.echo(
            f"Error: {dest} is a symlink; refusing to install autostart "
            "through it. Remove the symlink first and re-run.",
            err=True,
        )
        sys.exit(1)
    if dest.exists():
        try:
            existing = dest.read_text()
        except OSError:
            existing = None
        if existing == rendered:
            click.echo(f"{dest} already up to date; no changes")
            return
        if not force and existing is not None:
            click.echo(
                f"Error: {dest} exists and its content differs from the "
                "rendered template; refusing to overwrite. Re-run with "
                "--force to replace the existing file (your customisations "
                "will be lost), or remove the file first.",
                err=True,
            )
            sys.exit(1)

    dest.write_text(rendered)
    dest.chmod(0o644)
    click.echo(f"Wrote {dest}")

    platform = _autostart_platform()
    if platform == "darwin":
        uid = os.getuid()
        cmd = ["launchctl", "bootstrap", f"gui/{uid}", str(dest)]
    else:
        cmd = ["systemctl", "--user", "enable", "--now", template_name]
    label = "Warning" if force else "Error"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        click.echo(
            f"{label}: {cmd[0]} not found on PATH; file installed but not activated ({exc}).",
            err=True,
        )
        if not force:
            sys.exit(1)
        return
    if result.returncode != 0:
        click.echo(
            f"{label}: {' '.join(cmd)} exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}",
            err=True,
        )
        if not force:
            sys.exit(1)
        return
    click.echo(f"Activated via: {' '.join(cmd)}")


@t3_group.command("uninstall")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help="Remove OS autostart entry installed by ``install --autostart``.",
)
def t3_uninstall_cmd(autostart: bool) -> None:
    """Remove the T3 daemon autostart entry for the current user."""
    if not autostart:  # pragma: no cover
        raise click.UsageError("--autostart is required")

    install_dir = _autostart_install_dir()
    template_name = _autostart_filename_t3()
    dest = install_dir / template_name

    if not dest.exists():
        click.echo(f"Autostart not installed (nothing at {dest}).")
        return

    platform = _autostart_platform()
    if platform == "darwin":
        uid = os.getuid()
        cmd = ["launchctl", "bootout", f"gui/{uid}/{_T3_LAUNCHD_LABEL}"]
    else:
        cmd = ["systemctl", "--user", "disable", "--now", template_name]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            click.echo(
                f"Warning: {' '.join(cmd)} exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}",
                err=True,
            )
    except FileNotFoundError as exc:
        click.echo(f"Warning: {cmd[0]} not found ({exc}); removing file anyway.", err=True)

    dest.unlink()
    click.echo(f"Removed {dest}")


# ---------------------------------------------------------------------------
# t2 sub-group (RDR-120 P3a.A, nexus-7aayk)
# ---------------------------------------------------------------------------


@daemon_group.group("t2")
def t2_group() -> None:
    """T2 daemon: single-writer process owning the seven domain-store SQLite handles."""


@t2_group.command("start")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override (default: ~/.config/nexus/).",
)
@click.option(
    "--db-path",
    "db_path_str",
    default=None,
    help=(
        "Override the memory.db path. Default: ``nexus.config.default_db_path()``."
    ),
)
def t2_start_cmd(config_dir_str: str | None, db_path_str: str | None) -> None:
    """Start the T2 daemon (always foreground; supervisor blocks on this process).

    Unlike ``nx daemon t3 start`` which spawns a managed ``chroma run``
    subprocess and may exit early, the T2 daemon IS this Python process.
    ``start`` runs the asyncio event loop until SIGTERM/SIGINT and
    cleans up sockets + discovery file on exit. Run under launchd /
    systemd via ``nx daemon t2 install --autostart`` for production
    use; the foreground requirement is what the supervisor watches.

    If another T2 daemon already holds the spawn lock (a live winner
    already owns the data file), this process quiet-attaches instead of
    crashing (RDR-140 P1.3): ``run_t2_daemon`` logs ``t2_daemon_spawn_lost``
    at info and returns, so the command exits 0 with no traceback. The
    launchd/systemd template treats a zero exit as "do not restart"
    (see the install command), so a loser does not trigger a respawn loop.
    A genuine lifecycle error (bind failed, etc.) still raises
    ``T2DaemonError`` and exits 2.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.daemon.t2_daemon import T2DaemonError, run_t2_daemon

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    db_path = Path(db_path_str) if db_path_str else default_db_path()

    click.echo(
        f"T2 daemon starting (config_dir={config_dir}, db_path={db_path})..."
    )
    try:
        run_t2_daemon(config_dir=config_dir, db_path=db_path)
    except T2DaemonError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)


@t2_group.command("stop")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
def t2_stop_cmd(config_dir_str: str | None) -> None:
    """Stop the running T2 daemon by reading the discovery file's PID
    and sending SIGTERM.

    Returns 0 on success or when no daemon is running. SIGTERM is the
    canonical stop signal; the daemon's asyncio loop catches it,
    closes sockets, unlinks the discovery file, and exits 0
    (rendered as code 143 to launchd / systemd; both supervisor
    templates list 143 as a non-failure exit).
    """
    import json as _json

    from nexus.daemon.t2_daemon import t2_discovery_path

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = t2_discovery_path(config_dir)
    if not disc.exists():
        click.echo("No T2 daemon discovery file found; already stopped.")
        return
    try:
        payload = _json.loads(disc.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        click.echo(
            f"Failed to read discovery file: {exc}. Removing stale file.",
            err=True,
        )
        disc.unlink(missing_ok=True)
        return
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        click.echo(f"Invalid pid in discovery file: {pid!r}", err=True)
        disc.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"T2 daemon (pid={pid}) already gone; cleaning discovery file.")
        disc.unlink(missing_ok=True)
        return
    except OSError as exc:
        click.echo(f"Failed to signal pid {pid}: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Sent SIGTERM to T2 daemon (pid={pid}).")


@t2_group.command("status")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Output raw JSON.",
)
def t2_status_cmd(config_dir_str: str | None, as_json: bool) -> None:
    """Print the T2 daemon discovery JSON (PID + UDS path + TCP address)."""
    import json as _json
    from nexus.daemon.t2_daemon import t2_discovery_path

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = t2_discovery_path(config_dir)
    if not disc.exists():
        click.echo(
            "No T2 daemon discovery file found; is the daemon running?",
            err=True,
        )
        sys.exit(1)
    try:
        data = _json.loads(disc.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        click.echo(f"Failed to read discovery file: {exc}", err=True)
        sys.exit(1)
    # Probe the recorded pid for liveness. A discovery file can outlive
    # its daemon (e.g. an interrupted graceful stop left the file while
    # the process died, or a crash). Reporting such a file as "running"
    # masks a dead daemon (nexus-n8sbw).
    pid = data.get("pid")
    alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False

    if as_json:
        click.echo(_json.dumps({**data, "alive": alive}, indent=2))
        if not alive:
            sys.exit(1)
        return

    click.echo("T2 Daemon Status")
    click.echo("-" * 40)
    for key, value in data.items():
        click.echo(f"  {key}: {value}")
    if not alive:
        click.echo(
            f"  status: STALE (recorded pid {pid} is not running). "
            f"Run 'nx daemon t2 start' to respawn."
        )
        sys.exit(1)
    click.echo("  status: running")


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

# RDR-140 P2.2 (nexus-fkhe2): how long ``ensure-running`` blocks on the
# single-flight election lock before giving up and proceeding to spawn anyway.
# The election lock makes only one of K racing stacks cold-spawn; on timeout we
# degrade to the pre-P2 behaviour (the daemon's own spawn lock is still the hard
# backstop, so a redundant spawn merely quiet-attaches — never a deadlock).
# Module constant so tests can shrink it.
_T2_ELECTION_WAIT_TIMEOUT: float = 15.0


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
    import errno
    import fcntl

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
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _predecessor_alive(pid: int) -> bool:
    """True iff *pid* is alive AND still looks like a t2 daemon.

    The cmdline guard (``_is_t2_daemon_process``) defends the version-cycle
    wait against PID reuse: if the predecessor's pid is recycled to an
    unrelated process during the wait, treat the predecessor as gone rather
    than waiting on (or aborting for) a stranger. Used by ``ensure-running``
    to poll the predecessor's exit (RDR-129 A2).
    """
    from nexus.daemon.t2_daemon import _is_t2_daemon_process, _pid_is_alive

    return _pid_is_alive(pid) and _is_t2_daemon_process(pid)


def _t2_db_write_lock_acquirable(db_path: Path, timeout_ms: int) -> bool:
    """Probe whether memory.db's single WAL writer lock is acquirable
    within ``timeout_ms``, without holding it destructively (RDR-128 P0b).

    Opens a throwaway connection and attempts ``BEGIN IMMEDIATE`` — the same
    write lock the daemon's startup migration needs — then immediately rolls
    back. Returns:

    * ``True``  — the lock was acquired within the bounded busy_timeout (or
      the file does not exist yet, so there is nothing to contend with);
    * ``False`` — it stayed locked/busy for the whole timeout (another
      process, typically ``nx index repo``, is holding it).

    Bounded by construction: ``busy_timeout`` caps the ``BEGIN IMMEDIATE``
    wait, so this never hangs. Non-lock ``OperationalError``\\s propagate.
    """
    import sqlite3

    if not db_path.exists():
        return True
    # A raw connection is the point of this probe: routing through
    # T2Database would open eight connections and defeat a single-lock test.
    conn = sqlite3.connect(str(db_path), timeout=timeout_ms / 1000.0)  # epsilon-allow: RDR-128 P0b raw lock probe
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return True
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return False
        raise
    finally:
        conn.close()


@t2_group.command("ensure-running")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option(
    "--timeout",
    default=15.0,
    type=float,
    help=(
        "Seconds to wait for the daemon to become reachable after a "
        "cold spawn. Default: 15.0 — a cold-start daemon runs its "
        "one-time startup migration (which can take several seconds and "
        "holds the write lock) BEFORE it binds, so a tighter budget "
        "spuriously warns on a healthy boot (nexus-u3mfr). The wait "
        "fails fast if the spawned process dies, so a larger budget only "
        "affects a genuinely slow migration, not a failed spawn."
    ),
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress 'already running' / 'spawned' messages; print only errors.",
)
def t2_ensure_running_cmd(
    config_dir_str: str | None, timeout: float, quiet: bool,
) -> None:
    """Ensure the T2 daemon is running; spawn it in the background if not.

    Idempotent — safe to invoke from session-start hooks, post-install
    scripts, or as a defensive prelude to any operation that needs the
    daemon. The daemon's own spawn-lock arbitrates concurrent invocations
    so a race between the plugin hook and a manual ``nx daemon t2 start``
    can't double-start.

    Exit codes:
      0 — daemon is reachable (already running, or successfully spawned).
      1 — daemon was spawned but did not become reachable within ``--timeout``.
    """
    import time as _time
    from nexus.daemon.t2_daemon import t2_discovery_path

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = t2_discovery_path(config_dir)

    def _running_daemon() -> tuple[int, str] | None:
        """Return (pid, daemon_version) of the live daemon, or None.

        Probes the recorded PID via ``os.kill(pid, 0)``; stale discovery
        files outlive crashed daemons, so a readable file is not proof of
        life.
        """
        if not disc.exists():
            return None
        try:
            data = json.loads(disc.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        pid = data.get("pid")
        if not isinstance(pid, int):
            return None
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return None
        return pid, str(data.get("daemon_version") or "")

    def _daemon_is_alive() -> bool:
        return _running_daemon() is not None

    def _installed_version() -> str:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            return _pkg_version("conexus")
        except PackageNotFoundError:
            return ""

    # Fast pre-discovery (lock-free common case): a live matching-version
    # daemon means we are done without ever touching the election lock —
    # bounded boot latency is paid only on the actual spawn path.
    running = _running_daemon()
    if running is not None:
        _running_pid, running_ver = running
        installed = _installed_version()
        if not installed or running_ver == installed:
            if not quiet:
                click.echo("T2 daemon already running.")
            return

    # RDR-140 P2.2 (nexus-fkhe2): single-flight election around the
    # discover→spawn decision. K racing stacks block on this lock; only the
    # holder cold-spawns. We RE-DISCOVER after acquiring it so a stack that
    # finished spawning while we waited is attached, not duplicated. The lock
    # is anchored on the data file and is DISTINCT from the daemon's lifetime
    # spawn lock; fcntl auto-releases it on the holder's death, so a holder
    # that dies mid-spawn never deadlocks the waiters (they win the lock,
    # re-discover no daemon, and exactly one spawns).
    db_path = config_dir / "memory.db"
    election_fd = _acquire_election_lock(db_path, _T2_ELECTION_WAIT_TIMEOUT)
    if election_fd is None and not quiet:
        click.echo(
            "T2 election-lock wait timed out; proceeding to spawn unguarded "
            "(the daemon spawn lock remains the backstop).",
            err=True,
        )
    try:
        # Re-discover under the lock — the winner may have come up already.
        running = _running_daemon()
        if running is not None:
            running_pid, running_ver = running
            installed = _installed_version()
            # A live daemon whose version matches the installed tool (or whose
            # installed version can't be determined) is left alone (idempotent).
            if not installed or running_ver == installed:
                if not quiet:
                    click.echo("T2 daemon already running.")
                return
            # Version skew: the daemon froze its code at start and predates the
            # last upgrade (nexus-5ldk1). "Ensure running" means ensure a
            # CURRENT daemon; gracefully cycle the stale one and respawn below.
            # SIGTERM lets the daemon drain in-flight RPC (stop() awaits
            # wait_closed) and remove its discovery file before we respawn.
            #
            # RDR-128 P0b (RF-4): but do NOT SIGTERM a healthy (if stale)
            # daemon while memory.db's WAL writer lock is held — the respawn's
            # startup migration would race the holder (typically `nx index
            # repo`) and could crash-loop, and because ensure-running is
            # one-shot we'd be left with NO daemon. Probe the lock with a
            # bounded timeout; on timeout, ABORT the cycle: leave the
            # stale-but-working daemon up and defer to the next ensure-running.
            # Never hang; never trade a working daemon for none.
            if not _t2_db_write_lock_acquirable(
                db_path, timeout_ms=_T2_CYCLE_DB_PROBE_TIMEOUT_MS
            ):
                click.echo(
                    f"T2 daemon v{running_ver} stale but memory.db write lock "
                    f"held; cycle deferred (will retry on next ensure-running).",
                    err=True,
                )
                return  # leave the stale daemon up — best-effort, retried later
            if not quiet:
                click.echo(
                    f"T2 daemon is stale (running {running_ver}, installed "
                    f"{installed}); cycling to current."
                )
            try:
                os.kill(running_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            # RDR-129 A2 (nexus-kwqhd): poll the predecessor's PID liveness,
            # NOT the discovery file. stop() now holds the spawn lock until the
            # process exits (defer-release-to-exit) while unlinking the
            # discovery file early; a discovery-file poll would see "gone"
            # while the pid is still alive and holding the lock, so the cold
            # spawn below would hit EAGAIN on the spawn lock and leave ZERO
            # daemons. Wait for the pid to actually exit (lock dropped by the
            # OS); if it outlives the window, abort and keep the
            # stale-but-working daemon (RF-4: never trade a working daemon for
            # none).
            cycle_deadline = _time.monotonic() + _T2_CYCLE_EXIT_TIMEOUT
            while _time.monotonic() < cycle_deadline:
                if not _predecessor_alive(running_pid):
                    break
                _time.sleep(0.1)
            else:
                click.echo(
                    f"T2 daemon v{running_ver} (pid {running_pid}) did not "
                    f"exit within {_T2_CYCLE_EXIT_TIMEOUT}s of SIGTERM; cycle "
                    "aborted, leaving it up (will retry on next "
                    "ensure-running).",
                    err=True,
                )
                return  # never trade a working daemon for none
            # predecessor fully exited; its spawn lock is released — cold spawn.

        # Cold spawn. Use the same nx binary the operator invoked (preserves
        # PATH/virtualenv assumptions). start_new_session detaches the child
        # so this command can exit while the daemon keeps running.
        nx_bin = _resolve_nx_bin()
        argv = [*nx_bin, "daemon", "t2", "start"]
        if config_dir_str is not None:
            argv.extend(["--config-dir", config_dir_str])
        if not quiet:
            click.echo(f"Spawning T2 daemon: {' '.join(argv)}")
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        # nexus-u3mfr: migration-aware wait. A cold-start daemon runs its
        # one-time startup migration (multi-second, holds the write lock)
        # BEFORE it binds and writes the discovery file, so reachability
        # legitimately lags the spawn by several seconds. The fix is two-fold:
        # the default budget is generous enough to cover the migration
        # (--timeout default raised to 15s), AND we distinguish "still alive,
        # migrating" from "the process died" — if the spawned child exits
        # without becoming reachable we fail fast with its exit code rather
        # than waiting out the whole budget on a corpse. The warning therefore
        # only fires on a genuinely slow/stuck migration, not a healthy boot.
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if _daemon_is_alive():
                if not quiet:
                    click.echo("T2 daemon is reachable.")
                return
            if proc.poll() is not None:
                click.echo(
                    f"Error: T2 daemon process exited (code {proc.returncode}) "
                    "before becoming reachable. Check "
                    "~/Library/Logs/nexus-t2.err (macOS) or `journalctl "
                    "--user -u nexus-t2.service` (Linux) for the failure.",
                    err=True,
                )
                sys.exit(1)
            _time.sleep(0.1)

        click.echo(
            f"Warning: T2 daemon did not become reachable within {timeout}s "
            "(process still alive — likely a slow or stalled startup "
            "migration). Check ~/Library/Logs/nexus-t2.err (macOS) or "
            "`journalctl --user -u nexus-t2.service` (Linux) for the failure.",
            err=True,
        )
        sys.exit(1)
    finally:
        _release_election_lock(election_fd)


@t2_group.command("install")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help=(
        "Install OS autostart entry (launchd on macOS, systemd user "
        "unit on Linux) so the T2 daemon starts at login / boot."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing plist/unit file even when its content "
    "differs from the freshly rendered template.",
)
def t2_install_cmd(autostart: bool, force: bool) -> None:
    """Install the T2 daemon autostart entry for the current user."""
    if not autostart:  # pragma: no cover
        raise click.UsageError("--autostart is required")

    install_dir = _autostart_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    log_dir = _autostart_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    template_name = _autostart_filename_t2()
    nx_bin = _resolve_nx_bin()
    rendered = _render_template(
        template_name,
        nx_bin=nx_bin,
        log_dir=str(log_dir),
        path_env=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    )
    dest = install_dir / template_name

    if dest.is_symlink():
        click.echo(
            f"Error: {dest} is a symlink; refusing to install autostart "
            "through it. Remove the symlink first and re-run.",
            err=True,
        )
        sys.exit(1)
    if dest.exists():
        try:
            existing = dest.read_text()
        except OSError:
            existing = None
        if existing == rendered:
            click.echo(f"{dest} already up to date; no changes")
            return
        if not force and existing is not None:
            click.echo(
                f"Error: {dest} exists and its content differs from the "
                "rendered template; refusing to overwrite. Re-run with "
                "--force to replace.",
                err=True,
            )
            sys.exit(1)

    dest.write_text(rendered)
    dest.chmod(0o644)
    click.echo(f"Wrote {dest}")

    platform = _autostart_platform()
    if platform == "darwin":
        uid = os.getuid()
        cmd = ["launchctl", "bootstrap", f"gui/{uid}", str(dest)]
    else:
        cmd = ["systemctl", "--user", "enable", "--now", template_name]
    label = "Warning" if force else "Error"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        click.echo(
            f"{label}: {cmd[0]} not found on PATH; file installed but not activated ({exc}).",
            err=True,
        )
        if not force:
            sys.exit(1)
        return
    if result.returncode != 0:
        click.echo(
            f"{label}: {' '.join(cmd)} exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}",
            err=True,
        )
        if not force:
            sys.exit(1)
        return
    click.echo(f"Activated via: {' '.join(cmd)}")


@t2_group.command("uninstall")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help="Remove OS autostart entry installed by ``install --autostart``.",
)
def t2_uninstall_cmd(autostart: bool) -> None:
    """Remove the T2 daemon autostart entry for the current user."""
    if not autostart:  # pragma: no cover
        raise click.UsageError("--autostart is required")

    install_dir = _autostart_install_dir()
    template_name = _autostart_filename_t2()
    dest = install_dir / template_name

    if not dest.exists():
        click.echo(f"Autostart not installed (nothing at {dest}).")
        return

    platform = _autostart_platform()
    if platform == "darwin":
        uid = os.getuid()
        cmd = ["launchctl", "bootout", f"gui/{uid}/{_T2_LAUNCHD_LABEL}"]
    else:
        cmd = ["systemctl", "--user", "disable", "--now", template_name]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            click.echo(
                f"Warning: {' '.join(cmd)} exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}",
                err=True,
            )
    except FileNotFoundError as exc:
        click.echo(f"Warning: {cmd[0]} not found ({exc}); removing file anyway.", err=True)

    dest.unlink()
    click.echo(f"Removed {dest}")
