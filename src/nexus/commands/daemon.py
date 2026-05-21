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
