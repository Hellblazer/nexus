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

# RDR-174 P2.1 (nexus-y2yj6): the storage SERVICE tier (engine-service binary +
# local Postgres; serves T2 + T3). Mirrors the T2/T3 autostart-unit identity.
_SERVICE_PLIST_NAME = "com.nexus.service.plist"
_SERVICE_SERVICE_NAME = "nexus-service.service"
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


def _autostart_filename_t3() -> str:
    return _T3_PLIST_NAME if _autostart_platform() == "darwin" else _T3_SERVICE_NAME


def _autostart_filename_t2() -> str:
    return _T2_PLIST_NAME if _autostart_platform() == "darwin" else _T2_SERVICE_NAME


def _autostart_filename_service() -> str:
    return (
        _SERVICE_PLIST_NAME
        if _autostart_platform() == "darwin"
        else _SERVICE_SERVICE_NAME
    )


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


#: Hard ceiling on each launchctl/systemctl invocation. A hung supervisor
#: command would otherwise block ensure-running forever and the Popen
#: fallback could never fire (RF-4: never trade a working spawn path for
#: zero daemons). TimeoutExpired is caught by the except below → False →
#: fallback.
_SUPERVISOR_CMD_TIMEOUT: float = 10.0


def _t2_supervisor_spawn(unit_path: Path) -> bool:
    """Route a T2 cold-spawn through the OS supervisor.

    darwin: try ``launchctl kickstart gui/<uid>/com.nexus.t2``; if it returns
    non-zero (unit not loaded), run ``launchctl bootstrap gui/<uid> <plist>``
    first, then kickstart again.  On any non-zero final exit, return False.
    linux: run ``systemctl --user start nexus-t2.service``.  On non-zero, return False.

    On ANY exception (binary absent, command timeout, permission error)
    returns False. The caller logs a warning and falls back to
    subprocess.Popen.
    """
    import os as _os  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    try:
        platform = _autostart_platform()
        if platform == "darwin":
            uid = _os.getuid()
            target = f"gui/{uid}/{_T2_LAUNCHD_LABEL}"
            res = subprocess.run(
                ["launchctl", "kickstart", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_SUPERVISOR_CMD_TIMEOUT,
            )
            if res.returncode != 0:
                # Unit may not be bootstrapped yet; bootstrap then retry.
                br = subprocess.run(
                    ["launchctl", "bootstrap", f"gui/{uid}", str(unit_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_SUPERVISOR_CMD_TIMEOUT,
                )
                if br.returncode != 0:
                    return False
                res = subprocess.run(
                    ["launchctl", "kickstart", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_SUPERVISOR_CMD_TIMEOUT,
                )
                if res.returncode != 0:
                    return False
            return True
        if platform.startswith("linux"):
            res = subprocess.run(
                ["systemctl", "--user", "start", _T2_SERVICE_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_SUPERVISOR_CMD_TIMEOUT,
            )
            return res.returncode == 0
    except Exception as exc:  # noqa: BLE001 — supervisor spawn boundary; failure logged via log.warning, caller degrades
        _log.warning(
            "t2_supervisor_spawn_exception",
            exc=str(exc),
            exc_type=type(exc).__name__,
        )
        return False
    return False


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
    import subprocess  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    from nexus.config import _default_local_path, is_local_mode  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    from nexus.daemon.discovery import find_t3_daemon  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    from nexus.daemon.t3_daemon import (  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        T3CloudModeError,
        T3StartError,
        run_t3_supervisor,
    )

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    local_path = Path(local_path_str) if local_path_str else _default_local_path()

    if foreground:
        # RDR-149 P3: the blocking long-lived supervisor — spawns chroma,
        # publishes the lease, and HEARTBEATS it every interval while chroma
        # is alive. This is the path the launchd/systemd templates run.
        try:
            code = run_t3_supervisor(config_dir=config_dir, local_path=local_path)
        except T3CloudModeError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        except T3StartError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        sys.exit(code)

    # Non-foreground: ensure a detached supervisor is running so the lease
    # is continuously heartbeated, then return. Idempotent on a live lease.
    if not is_local_mode():
        click.echo(
            "Error: T3 daemon is a no-op in cloud mode. Set NX_LOCAL=1 to "
            "opt into local mode.",
            err=True,
        )
        sys.exit(1)

    existing = find_t3_daemon(config_dir)
    if existing is None:
        argv = [
            *_resolve_nx_bin(), "daemon", "t3", "start", "--foreground",
            "--config-dir", str(config_dir), "--local-path", str(local_path),
        ]
        # nexus-ovbr7: crash-channel capture (see the storage-service spawn
        # for the rationale).
        from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

        spawn_log = open_child_log_or_devnull("t3_daemon.crash", config_dir)
        try:
            subprocess.Popen(
                argv,
                stdout=spawn_log,
                stderr=spawn_log,
                start_new_session=True,
            )
        finally:
            if not isinstance(spawn_log, int):
                spawn_log.close()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            existing = find_t3_daemon(config_dir)
            if existing is not None:
                break
            time.sleep(0.2)
        if existing is None:
            click.echo(
                "Error: T3 supervisor did not become ready within 15s.",
                err=True,
            )
            sys.exit(2)

    if announce_stdout:
        click.echo(json.dumps(existing))
    else:
        click.echo(
            f"T3 daemon running on {existing['tcp_host']}:{existing['tcp_port']} "
            f"(pid={existing['pid']}, local_path={existing.get('local_path')})."
        )


@t3_group.command("stop")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
def t3_stop_cmd(config_dir_str: str | None) -> None:
    """Stop the running T3 daemon (graceful SIGTERM → SIGKILL escalation)."""
    from nexus.daemon.t3_daemon import stop_t3_daemon  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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
    from nexus.daemon.t3_daemon import t3_discovery_path  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    from nexus.daemon.t2_daemon import T2DaemonError, run_t2_daemon  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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
    import json as _json  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    from nexus.daemon.t2_daemon import t2_discovery_path  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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
    pid = _discovery_record_pid(payload)
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
    import json as _json  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
    from nexus.daemon.t2_daemon import t2_discovery_path  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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
    pid = _discovery_record_pid(data)
    # RDR-149 P2: liveness is what a CLIENT would resolve. For a lease
    # record that is freshness (a daemon whose heartbeat loop wedged reads
    # as down even though its pid is alive); for a legacy payload it is
    # pid-liveness. ``find_t2_daemon`` applies the right rule per format.
    from nexus.daemon.discovery import find_t2_daemon  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    alive = find_t2_daemon(config_dir) is not None

    # RDR-140 P4.2 (Gap 5): surface how many restarts the crash-loop guard has
    # recorded in the current window — a rising count is the crash-loop signal.
    restarts = _restart_count(config_dir, now=time.time())

    if as_json:
        click.echo(_json.dumps(
            {**data, "alive": alive, "restarts_in_window": restarts}, indent=2,
        ))
        if not alive:
            sys.exit(1)
        return

    click.echo("T2 Daemon Status")
    click.echo("-" * 40)
    for key, value in data.items():
        click.echo(f"  {key}: {value}")
    click.echo(f"  restarts_in_window: {restarts}")
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


def _predecessor_alive(pid: int) -> bool:
    """True iff *pid* is alive AND still looks like a t2 daemon.

    The cmdline guard (``_is_t2_daemon_process``) defends the version-cycle
    wait against PID reuse: if the predecessor's pid is recycled to an
    unrelated process during the wait, treat the predecessor as gone rather
    than waiting on (or aborting for) a stranger. Used by ``ensure-running``
    to poll the predecessor's exit (RDR-129 A2).
    """
    from nexus.daemon.t2_daemon import _is_t2_daemon_process, _pid_is_alive  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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
    import sqlite3  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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


class T2EnsureOutcome(Enum):
    """Terminal state of an ensure-running attempt (RDR-141 P0, nexus-cvaip).

    Richer than a bare bool so a programmatic caller (the RDR-141
    self-healing re-assert in ``mcp_infra.t2_index_write``) can pick the
    correct WARNING event and decide whether a direct-write fallback is a
    true single-writer down-arm (D_old dead) or a cycle-deferred residual
    (D_old still alive).
    """
    REACHABLE = "reachable"                      # a current-version daemon is up and serving
    DEFERRED_WRITE_LOCK = "deferred_write_lock"  # stale daemon ALIVE; cycle deferred (WAL write-lock held)
    DEFERRED_SIGTERM = "deferred_sigterm"        # stale daemon ALIVE; SIGTERM'd but did not exit in window
    CRASHLOOP_SUPPRESSED = "crashloop_suppressed"  # no live incumbent daemon (never existed, or was fully reaped); crash-loop guard refused respawn
    SPAWN_FAILED = "spawn_failed"                # no daemon: cold-spawned process died or never became reachable
    SERVICE_MODE_SKIP = "service_mode_skip"      # memory store is in SERVICE mode; the SQLite daemon has no role (RDR-176)


def _t2_ensure_running_inner(
    config_dir_str: str | None, timeout: float, quiet: bool,
) -> T2EnsureOutcome:
    """Core logic of ``ensure-running``; returns a rich outcome enum.

    Extracted from the Click command (RDR-141 P0, nexus-cvaip) so that
    programmatic callers (``mcp_infra.t2_index_write``) can invoke the
    self-healing logic without triggering ``sys.exit``.  The Click
    command ``t2_ensure_running_cmd`` is a thin wrapper that maps the
    enum to CLI exit codes.
    """
    import time as _time  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()

    # RDR-176 Phase 1 (Gap 2): in SERVICE mode the SQLite T2 tier is a frozen
    # migration source and the Java service is the live substrate — no client
    # ever connects to this daemon (``run_t2_daemon`` already no-ops for the
    # same reason). Without this check every caller (nx-mcp's first-run hook,
    # ``nx upgrade``) cold-spawns a process that exits immediately by design,
    # and repeated calls across concurrent MCP sessions trip the crash-loop
    # guard below with a misleading "crash-loop suppressed" error even though
    # nothing is actually broken (nexus-daemon-6.6.0-service-mode-skip).
    from nexus.db.storage_mode import (  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        StorageBackend,
        storage_backend_for,
    )

    if storage_backend_for("memory") == StorageBackend.SERVICE:
        if not quiet:
            click.echo(
                "T2 daemon not needed: memory store is in service mode "
                "(Java service is the live substrate)."
            )
        return T2EnsureOutcome.SERVICE_MODE_SKIP

    def _running_daemon() -> tuple[int, str] | None:
        """Return (pid, daemon_version) of the live daemon, or None.

        RDR-149 P2: liveness is now lease freshness, resolved through
        ``find_t2_daemon`` (which TTL-checks the lease and falls back to
        pid-liveness for a legacy payload mid-upgrade). The external
        contract is unchanged: callers still receive ``(pid, version)`` of
        a live daemon, with the pid (lifted from the lease endpoint) used
        for the SIGTERM in the version-skew cycle below.
        """
        from nexus.daemon.discovery import find_t2_daemon  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

        payload = find_t2_daemon(config_dir)
        if payload is None:
            return None
        pid = payload.get("pid")
        if not isinstance(pid, int):
            return None
        # New lease records carry ``version``; a legacy payload carries
        # ``daemon_version`` (upgrade-window compatibility).
        version = payload.get("version") or payload.get("daemon_version") or ""
        return pid, str(version)

    def _daemon_is_alive() -> bool:
        return _running_daemon() is not None

    def _installed_version() -> str:
        from importlib.metadata import PackageNotFoundError  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

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
            return T2EnsureOutcome.REACHABLE

    # RDR-140 P2.2 (nexus-fkhe2): single-flight election around the
    # discover→spawn decision. K racing stacks block on this lock; only the
    # holder cold-spawns. We RE-DISCOVER after acquiring it so a stack that
    # finished spawning while we waited is attached, not duplicated. The lock
    # is anchored on the data file and is DISTINCT from the daemon's lifetime
    # spawn lock; fcntl auto-releases it on the holder's death, so a holder
    # that dies mid-spawn never deadlocks the waiters (they win the lock,
    # re-discover no daemon, and exactly one spawns).
    db_path = config_dir / "memory.db"
    election_fd = _acquire_election_lock(db_path, _election_wait_for(timeout))
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
                return T2EnsureOutcome.REACHABLE
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
                return T2EnsureOutcome.DEFERRED_WRITE_LOCK
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
                return T2EnsureOutcome.DEFERRED_SIGTERM
            # predecessor fully exited; its spawn lock is released — cold spawn.

        # RDR-140 P4.2 (Gap 5): crash-loop guard. If we have already respawned
        # _CRASHLOOP_MAX_RESTARTS times within the window without converging,
        # stop — an endless crash-loop with a traceback per attempt helps no
        # one. Log ONCE at error (the sentinel's tripped_logged flag prevents
        # re-logging on every suppressed call) and refuse to respawn. A healthy
        # convergence below clears the counter.
        _cl_now = _time.time()
        if _crashloop_tripped(config_dir, now=_cl_now):
            _data = _read_crashloop(config_dir)
            if not _data.get("tripped_logged"):
                _log.error(
                    "t2_daemon_crash_loop_suppressed",
                    restarts=_restart_count(config_dir, now=_cl_now),
                    window_s=_CRASHLOOP_WINDOW_S,
                    max_restarts=_CRASHLOOP_MAX_RESTARTS,
                )
                _data["tripped_logged"] = True
                _write_crashloop_atomic(config_dir, _data)
            click.echo(
                f"Error: T2 daemon crash-loop suppressed "
                f"({_CRASHLOOP_MAX_RESTARTS}+ restarts in "
                f"{_CRASHLOOP_WINDOW_S:.0f}s); refusing to respawn. Investigate "
                "the daemon log, then 'nx daemon t2 status'.",
                err=True,
            )
            return T2EnsureOutcome.CRASHLOOP_SUPPRESSED
        _record_restart(config_dir, now=_cl_now)

        # Cold spawn.
        #
        # nexus-uybp6: single-owner routing. When the OS autostart unit is
        # installed, route the respawn through the supervisor (launchd/systemd)
        # so the unit keeps exclusive ownership — an ad-hoc Popen spawn would
        # race launchd's KeepAlive and make the unit dormant again. On ANY
        # supervisor failure (non-zero exit, missing binary, exception) we log
        # a warning and fall through to the existing Popen path: never trade a
        # working spawn path for zero daemons (RF-4).
        #
        # If no unit is installed, skip directly to Popen (unchanged path).
        #
        # Supervisor routing is UNQUALIFIED-DEFAULT ONLY: the installed unit's
        # daemon resolves its config dir in LAUNCHD'S/SYSTEMD'S environment
        # (bare `nx daemon t2 start`, no --config-dir, no NEXUS_CONFIG_DIR),
        # i.e. the hard default. Kicking it on behalf of a caller that
        # overrode the config dir — via the flag OR the env var — would start
        # a daemon against the wrong data directory and never satisfy this
        # caller's reachability wait (and repeatedly kick the user's real
        # daemon: the multistack race harness found exactly that). Any
        # override therefore Popen-spawns with explicit isolation.
        proc = None
        _unit_path = (
            _autostart_unit_installed()
            if config_dir_str is None
            and not os.environ.get("NEXUS_CONFIG_DIR", "").strip()
            else None
        )
        if _unit_path is not None:
            if not quiet:
                click.echo(f"Respawning T2 daemon via OS supervisor: {_unit_path.name}")
            if _t2_supervisor_spawn(_unit_path):
                # Supervisor accepted the start command — no Popen proc to poll.
                # The reachability wait below handles convergence; proc stays None.
                pass
            else:
                _log.warning(
                    "t2_supervisor_spawn_failed",
                    unit=str(_unit_path),
                    fallback="popen",
                )
                if not quiet:
                    click.echo(
                        "Warning: OS supervisor spawn failed; falling back to direct spawn.",
                        err=True,
                    )
                _unit_path = None  # signal: use Popen path

        if _unit_path is None:
            # Popen path: use the same nx binary the operator invoked (preserves
            # PATH/virtualenv assumptions). start_new_session detaches the child
            # so this command can exit while the daemon keeps running.
            nx_bin = _resolve_nx_bin()
            argv = [*nx_bin, "daemon", "t2", "start"]
            if config_dir_str is not None:
                argv.extend(["--config-dir", config_dir_str])
            if not quiet:
                click.echo(f"Spawning T2 daemon: {' '.join(argv)}")
            # nexus-ovbr7: crash-channel capture (see the storage-service
            # spawn for the rationale).
            from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

            spawn_log = open_child_log_or_devnull("t2_daemon.crash", config_dir)
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=spawn_log,
                    stderr=spawn_log,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            finally:
                if not isinstance(spawn_log, int):
                    spawn_log.close()

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
        # NOTE: the proc.poll() death-check only applies to the Popen path —
        # on the supervisor path proc is None, so the check is skipped.
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if _daemon_is_alive():
                # Healthy convergence: clear the crash-loop counter.
                _reset_crashloop(config_dir)
                if not quiet:
                    click.echo("T2 daemon is reachable.")
                return T2EnsureOutcome.REACHABLE
            if proc is not None and proc.poll() is not None:
                click.echo(
                    f"Error: T2 daemon process exited (code {proc.returncode}) "
                    "before becoming reachable. Check "
                    "~/Library/Logs/nexus-t2.err (macOS) or `journalctl "
                    "--user -u nexus-t2.service` (Linux) for the failure.",
                    err=True,
                )
                return T2EnsureOutcome.SPAWN_FAILED
            _time.sleep(0.1)

        click.echo(
            f"Warning: T2 daemon did not become reachable within {timeout}s "
            "(process still alive — likely a slow or stalled startup "
            "migration). Check ~/Library/Logs/nexus-t2.err (macOS) or "
            "`journalctl --user -u nexus-t2.service` (Linux) for the failure.",
            err=True,
        )
        return T2EnsureOutcome.SPAWN_FAILED
    finally:
        _release_election_lock(election_fd)


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
      0 — daemon is reachable (already running or successfully spawned), or
          a stale-daemon cycle was deferred (a working daemon is still up).
      1 — daemon could not be made reachable: crash-loop suppressed, or the
          spawned process died / did not become reachable within ``--timeout``.
    """
    outcome = _t2_ensure_running_inner(config_dir_str, timeout, quiet)
    if outcome in (T2EnsureOutcome.CRASHLOOP_SUPPRESSED, T2EnsureOutcome.SPAWN_FAILED):
        sys.exit(1)


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
    """Install the T2 daemon autostart entry for the current user.

    Thin wrapper over :func:`nexus.daemon.installer.install_autostart`
    (RDR-126 §2 lift): the library function owns the file placement /
    activation logic; this command translates its structured result into
    ``click.echo`` lines and exit codes.
    """
    if not autostart:  # pragma: no cover
        raise click.UsageError("--autostart is required")

    from nexus.daemon import installer  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    try:
        result = installer.install_autostart(force=force)
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


@t2_group.command("uninstall")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help="Remove OS autostart entry installed by ``install --autostart``.",
)
def t2_uninstall_cmd(autostart: bool) -> None:
    """Remove the T2 daemon autostart entry for the current user.

    Thin wrapper over :func:`nexus.daemon.installer.uninstall_autostart`
    (RDR-126 §2 lift).
    """
    if not autostart:  # pragma: no cover
        raise click.UsageError("--autostart is required")

    from nexus.daemon import installer  # noqa: PLC0415 — deferred import — CLI startup cost, only needed in this subcommand path

    result = installer.uninstall_autostart()
    if result.status is installer.UninstallStatus.NOT_INSTALLED:
        click.echo(f"Autostart not installed (nothing at {result.dest}).")
        return
    for warning in result.warnings:
        click.echo(f"Warning: {warning}", err=True)
    click.echo(f"Removed {result.dest}")


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


# Full, clickable URL to the recovery playbook — surfaced verbatim in the
# gate below (and worth keeping in one place, nexus-pnwu0). Most terminals
# autolink an https:// URL; releases promote develop -> main, so an operator
# on a released build finds §8.1 on main.
_MIGRATION_RUNBOOK_URL = (
    "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"
)


def _emit_chash_poison_gate(config_dir: Path, *, force: bool) -> None:
    """nexus-pnwu0 / GH #1390 upgrade gate (see call site).

    Detects non-32-char chash rows in the pgvector store via the SAME probe
    `nx doctor` uses (:func:`nexus.health._check_migration_state`), and refuses
    the install unless *force*. Emits a full clickable runbook URL AND a
    ready-to-paste prompt the operator can hand to their own Claude — the
    agent-runnable remediation pattern, in-product. A probe that cannot run
    (PG down, not service mode) never blocks a legitimate install.
    """
    try:
        from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred, circular-dep
        from nexus.health import _check_migration_state  # noqa: PLC0415 — deferred, CLI startup cost

        creds_path = config_dir / CREDENTIALS_FILENAME
        poison = [
            r for r in _check_migration_state(creds_path=creds_path)
            if r.label == "Chunk chash conformance"
            and not r.ok and "non-32-char chash" in r.detail
        ]
    except Exception as exc:  # noqa: BLE001 — the gate must never block a valid install on an unrelated error
        click.echo(f"(chash-conformance pre-check skipped: {exc})", err=True)
        return

    if not poison:
        return

    detail = poison[0].detail
    prompt = (
        "My conexus/nexus store has non-32-char chash rows in pgvector "
        "(GH #1390 / nexus-pnwu0) and a new engine would crash-loop on boot. "
        "Walk me through the recovery in "
        f"{_MIGRATION_RUNBOOK_URL} section 8.1: roll back the poisoned "
        "pgvector target (nx storage migrate vectors --rollback), re-index the "
        "affected legacy-id collections from source, re-run nx guided-upgrade, "
        "and only then let me upgrade the engine. Do NOT drop the chash "
        "length constraints."
    )
    if force:
        click.echo(
            "WARNING (nexus-pnwu0): --force overrides the chash-poison gate. "
            f"{detail} The new engine may crash-loop on boot unless you have "
            "already remediated. Recovery: " + _MIGRATION_RUNBOOK_URL
            + " §8.1.",
            err=True,
        )
        return

    click.echo(
        "\nRefusing to install (nexus-pnwu0 / GH #1390): booting a new engine "
        "on this store would crash-loop.\n"
        f"  {detail}\n\n"
        "Remediate first — full recovery playbook (clickable):\n"
        f"  {_MIGRATION_RUNBOOK_URL} §8.1\n\n"
        "Or paste this to your Claude to be walked through it:\n"
        "  ----------------------------------------------------------------\n"
        f"  {prompt}\n"
        "  ----------------------------------------------------------------\n\n"
        "Do NOT drop the chash length constraints to force it through — that "
        "is the exact action that caused GH #1390. Re-run with --force ONLY "
        "after you have remediated.",
        err=True,
    )
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
    help="Override the chash-poison pre-check (nexus-pnwu0). Use ONLY after "
         "remediating per docs/migration-runbook.md §8.1 — a new engine will "
         "crash-loop on boot if the store still holds non-32-char chash rows.",
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

    # nexus-pnwu0 / GH #1390 upgrade gate: refuse to install a new engine onto a
    # store whose pgvector target already holds non-32-char chash rows. Booting
    # the new binary would crash-loop Liquibase's VALIDATE CONSTRAINT
    # (catalog-013-3 has no guard against present-but-violating constraints, and
    # the changelog cannot cleanly add one — the count query runs under FORCE RLS
    # as the NOBYPASSRLS migration role and sees zero of the very rows VALIDATE
    # then trips on). This is the actual gate the passive `nx doctor` probe could
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
