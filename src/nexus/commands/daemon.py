# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon`` command group — manage T2 (and future T3) daemons.

RDR-112 P1.1 (nexus-61x6): transport scaffold with dual-bind UDS+TCP.

Subcommands:
  nx daemon t2 start   Start the T2 daemon (foreground or background)
  nx daemon t2 stop    Send SIGTERM to the running T2 daemon
  nx daemon t2 info    Print the discovery file contents (JSON)

Phase 1.1 scope: start / stop / info.
Later beads add domain-store RPCs, EventStream, migration runner, etc.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import click

from nexus.config import nexus_config_dir


def _discovery_path(config_dir: Path) -> Path:
    uid = os.getuid()
    return config_dir / f"t2_addr.{uid}"


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group("daemon")
def daemon_group() -> None:
    """Manage storage daemons (T2, T3)."""


# ---------------------------------------------------------------------------
# t2 sub-group
# ---------------------------------------------------------------------------


@daemon_group.group("t2")
def t2_group() -> None:
    """T2 daemon — persistent memory and plan stores."""


# ---------------------------------------------------------------------------
# nx daemon t2 start
# ---------------------------------------------------------------------------


@t2_group.command("start")
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
    help="Run in foreground (block until SIGTERM/SIGINT). Default: background.",
)
def start_cmd(config_dir_str: str | None, foreground: bool) -> None:
    """Start the T2 daemon.

    Binds both a UDS socket (mode 0600, peer-cred checked) and a
    loopback TCP port. Writes a discovery file so clients can find it.

    With --foreground the process blocks until SIGTERM or SIGINT.
    Without --foreground (default) the process daemonises and returns
    immediately with the discovery JSON printed to stdout.
    """
    from nexus.daemon.t2_daemon import T2Daemon

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()

    async def _run() -> None:
        daemon = T2Daemon(config_dir=config_dir)
        try:
            await daemon.start()
        except RuntimeError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)

        if foreground:
            await daemon.run_until_signal()
        # In non-foreground mode: start() already announced on stdout;
        # the process exits, leaving the event loop's background servers.
        # NOTE: full background-daemonise (double-fork) is a follow-on
        # bead; for now --foreground is the reliable path and the default
        # exits immediately after printing the discovery JSON.

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# nx daemon t2 stop
# ---------------------------------------------------------------------------


@t2_group.command("stop")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
def stop_cmd(config_dir_str: str | None) -> None:
    """Send SIGTERM to the running T2 daemon.

    Reads the discovery file to find the daemon PID, then sends SIGTERM.
    The daemon performs a graceful drain before exiting.
    """
    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = _discovery_path(config_dir)
    if not disc.exists():
        click.echo("No T2 daemon discovery file found — is the daemon running?", err=True)
        sys.exit(1)

    try:
        data = json.loads(disc.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read discovery file: {exc}", err=True)
        sys.exit(1)

    pid = data.get("pid")
    if not isinstance(pid, int):
        click.echo("Discovery file missing or invalid 'pid' field.", err=True)
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"No process with pid={pid}. Discovery file may be stale.", err=True)
        disc.unlink(missing_ok=True)
        sys.exit(1)
    except PermissionError:
        click.echo(f"Permission denied sending SIGTERM to pid={pid}.", err=True)
        sys.exit(1)

    click.echo(f"SIGTERM sent to T2 daemon (pid={pid}).")


# ---------------------------------------------------------------------------
# nx daemon t2 info
# ---------------------------------------------------------------------------


@t2_group.command("info")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def info_cmd(config_dir_str: str | None, as_json: bool) -> None:
    """Print discovery information for the running T2 daemon.

    Reads the discovery file at ``~/.config/nexus/t2_addr.<uid>``.
    """
    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = _discovery_path(config_dir)
    if not disc.exists():
        click.echo("No T2 daemon discovery file found — is the daemon running?", err=True)
        sys.exit(1)

    try:
        data = json.loads(disc.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read discovery file: {exc}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    click.echo("T2 Daemon Info")
    click.echo("-" * 40)
    for key, value in data.items():
        click.echo(f"  {key}: {value}")


# ---------------------------------------------------------------------------
# nx daemon t2 subspace  (P1.5 nexus-x98k)
# ---------------------------------------------------------------------------


@t2_group.group("subspace")
def subspace_group() -> None:
    """Manage registered subspace schemas (admin; UDS only)."""


@subspace_group.command("add")
@click.argument("yaml_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
def subspace_add_cmd(yaml_path: Path, config_dir_str: str | None) -> None:
    """Register a subspace schema from a YAML file.

    Reads YAML_PATH, sends it to the running T2 daemon via the UDS-only
    ``subspace_add`` admin RPC, and prints the registered name + digest.

    The daemon validates the schema (JSON Schema + reserved-prefix check)
    and persists it to tuples.db. Duplicate names are rejected.

    Requires a running daemon (``nx daemon t2 start``) and must be invoked
    as the same user that owns the daemon (UDS peer-cred gate).
    """
    from nexus.daemon.t2_client import T2Client, T2DaemonError

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = _discovery_path(config_dir)
    if not disc.exists():
        click.echo("No T2 daemon discovery file found — is the daemon running?", err=True)
        sys.exit(1)

    try:
        data = json.loads(disc.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read discovery file: {exc}", err=True)
        sys.exit(1)

    uds_path_str = data.get("uds_path")
    if not uds_path_str:
        click.echo("Discovery file missing 'uds_path'. Cannot use admin RPC over TCP.", err=True)
        sys.exit(1)

    try:
        yaml_text = yaml_path.read_text()
    except OSError as exc:
        click.echo(f"Failed to read YAML file: {exc}", err=True)
        sys.exit(1)

    try:
        with T2Client(uds_path=Path(uds_path_str)) as client:
            result = client.call("subspace_add", {"yaml": yaml_text})
    except T2DaemonError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except OSError as exc:
        click.echo(f"Connection error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Registered: {result.get('name')}")
    click.echo(f"Digest:     {result.get('digest')}")
