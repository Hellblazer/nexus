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
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

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
@click.option(
    "--announce-stdout",
    "announce_stdout",
    is_flag=True,
    default=False,
    help=(
        "Emit the discovery JSON (UDS path, PID, ports, registry digest) "
        "on stdout at startup. Required when launched under an "
        "orchestrator that captures stdout for service discovery (e.g. a "
        "container init). Default off (nexus-l712 RDR-113): the "
        "discovery file at ~/.config/nexus/t2_addr.<uid> is the primary "
        "channel, and a shared stdout sink would otherwise leak PID + "
        "UDS path + registry digest."
    ),
)
def start_cmd(
    config_dir_str: str | None, foreground: bool, announce_stdout: bool
) -> None:
    """Start the T2 daemon.

    Binds both a UDS socket (mode 0600, peer-cred checked) and a
    loopback TCP port. Constructs ``T2Database`` + ``RegistryStore`` so
    domain-store RPCs (``memory.*``, ``plans.*`` etc.), introspection
    RPCs (``exec_raw``, ``schema``, ``peek``, ``stats``, ``export``),
    and the ``subspace_add`` admin RPC are all served. Writes a
    discovery file so clients can find it.

    With --foreground the process blocks until SIGTERM or SIGINT.
    Without --foreground (default) the process daemonises and returns
    immediately with the discovery JSON printed to stdout.

    nexus-uuuh: configures a RotatingFileHandler at
    ``~/.config/nexus/logs/daemon.log`` (10 MB, 5 backups). The
    launchd/systemd-captured stderr stream becomes a crash-diagnostics
    log only; steady-state telemetry is bounded by the rotation here.
    """
    from nexus.daemon.subspace_registry import RegistryStore
    from nexus.daemon.t2_daemon import T2Daemon
    from nexus.daemon.tuplespace_service import TuplespaceService
    from nexus.db.t2 import T2Database
    from nexus.logging_setup import configure_logging

    configure_logging("daemon")

    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    memory_db_path = config_dir / "memory.db"
    tuples_db_path = config_dir / "tuples.db"

    async def _run() -> None:
        # T2Daemon.start() runs migrations against memory.db AND tuples.db
        # BEFORE binding sockets. Construct T2Database + RegistryStore here
        # so the same migration path produces a coherent schema; T2Database
        # opens its own connections lazily so there is no race with the
        # daemon's writer.
        t2db = T2Database(memory_db_path)
        registry_store = RegistryStore(tuples_db_path=tuples_db_path)

        # nexus-6s8v (RDR-112): construct TuplespaceService so the daemon
        # serves the tuplespace.* RPC suite. The service opens its own
        # SQLite connection to tuples.db (single-writer per RDR-112 §9)
        # and a TupleIndex backed by the local persistent chroma.
        import chromadb
        chroma_dir = config_dir / "chroma"
        # storage-boundary-allow: this IS the daemon-startup CLI; the
        # chroma client it creates becomes the daemon's owned writer.
        chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
        tuplespace_service = TuplespaceService(
            tuples_db_path=tuples_db_path,
            chroma_client=chroma_client,
        )

        daemon = T2Daemon(
            config_dir=config_dir,
            t2db=t2db,
            tuples_db_path=tuples_db_path,
            registry_store=registry_store,
            tuplespace_service=tuplespace_service,
            announce_stdout=announce_stdout,
        )
        try:
            await daemon.start()
        except RuntimeError as exc:
            click.echo(f"Error: {exc}", err=True)
            t2db.close()
            sys.exit(1)

        try:
            if foreground:
                await daemon.run_until_signal()
            # In non-foreground mode: start() already announced on stdout;
            # the process exits, leaving the event loop's background servers.
            # Full background-daemonise (double-fork) is a follow-on bead;
            # --foreground is the reliable path for now.
        finally:
            t2db.close()

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


# ---------------------------------------------------------------------------
# nx daemon t2 doctor (nexus-6m9i, third 360° OBS C-3 + RECOV S-2)
# ---------------------------------------------------------------------------


@t2_group.command("doctor")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Output raw JSON.",
)
def doctor_cmd(config_dir_str: str | None, as_json: bool) -> None:
    """Probe the running T2 daemon's health + load.

    Hits the daemon's ``ping`` RPC and reports active-handler count,
    blocking_take in-flight slots, wake-thread liveness, registry
    digest, schema version, and process identity.

    Pre-third-360°, ``nx doctor`` and ``nx health`` referenced this
    command in error hints but it did not exist (nexus-6m9i OBS C-3 +
    RECOV S-2).
    """
    config_dir = Path(config_dir_str) if config_dir_str else nexus_config_dir()
    disc = _discovery_path(config_dir)
    if not disc.exists():
        click.echo("No T2 daemon discovery file found.", err=True)
        click.echo("  Fix: run `nx daemon t2 start` first.", err=True)
        sys.exit(1)

    try:
        data = json.loads(disc.read_text())
        uds_path = data.get("uds_path")
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read discovery file: {exc}", err=True)
        sys.exit(1)

    from nexus.daemon.t2_client import T2Client

    if uds_path and Path(uds_path).exists():
        client = T2Client(uds_path=Path(uds_path), rpc_timeout_seconds=2.0)
    else:
        tcp_host = data.get("tcp_host", "127.0.0.1")
        tcp_port = data.get("tcp_port", 0)
        if not tcp_port:
            click.echo("Daemon has neither UDS nor TCP transport.", err=True)
            sys.exit(2)
        client = T2Client(
            tcp_addr=(tcp_host, int(tcp_port)), rpc_timeout_seconds=2.0
        )

    try:
        pong = client.call("ping")
    except Exception as exc:
        click.echo(f"Daemon ping failed: {exc}", err=True)
        click.echo(
            "  Fix: the daemon may be wedged. `nx daemon t2 stop && "
            "nx daemon t2 start` (or kill -9 the pid in the discovery "
            "file if stop hangs, then `rm <uds_path>`).",
            err=True,
        )
        sys.exit(2)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if as_json:
        click.echo(json.dumps(pong, indent=2, default=str))
        return

    click.echo("T2 Daemon Doctor")
    click.echo("-" * 40)
    click.echo(f"  pid              : {data.get('pid')}")
    click.echo(f"  daemon_version   : {pong.get('version')}")
    click.echo(f"  schema_version   : {pong.get('schema_version')}")
    click.echo(f"  protocol_version : {pong.get('daemon_protocol_version')}")
    click.echo(f"  start_time       : {pong.get('start_time')}")
    click.echo(f"  uds_path         : {uds_path}")
    click.echo(
        f"  tcp              : {data.get('tcp_host')}:{data.get('tcp_port')}"
    )
    # Load metrics (nexus-6m9i OBS C-1): enriched ping returns these.
    if "active_handlers" in pong:
        click.echo(f"  active_handlers  : {pong['active_handlers']}")
    if "blocking_take_in_flight" in pong:
        click.echo(
            f"  blocking_take    : {pong['blocking_take_in_flight']}"
        )
    if "wake_thread_alive" in pong:
        click.echo(f"  wake_thread      : {pong['wake_thread_alive']}")


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
# Helpers shared by introspection subcommands
# ---------------------------------------------------------------------------


def _nexus_config_dir() -> Path:
    from nexus.config import nexus_config_dir as _ncd
    return _ncd()


def _client_from_disc(disc_data: dict) -> "T2Client":  # noqa: F821
    from nexus.daemon.t2_client import T2Client
    uds = disc_data.get("uds_path")
    tcp = disc_data.get("tcp_addr")
    if uds:
        return T2Client(uds_path=Path(uds))
    if tcp:
        host, port_str = tcp.split(":")
        return T2Client(tcp_addr=(host, int(port_str)))
    raise RuntimeError("Discovery file missing both uds_path and tcp_addr.")


def _uds_client_from_disc(disc_data: dict) -> "T2Client":  # noqa: F821
    """Return a UDS-only client; exit if not available (admin ops require UDS)."""
    from nexus.daemon.t2_client import T2Client
    uds = disc_data.get("uds_path")
    if not uds:
        click.echo("Discovery file missing uds_path. Admin ops require UDS.", err=True)
        sys.exit(1)
    return T2Client(uds_path=Path(uds))


def _load_disc(config_dir_str: str | None) -> dict:
    config_dir = Path(config_dir_str) if config_dir_str else _nexus_config_dir()
    disc = _discovery_path(config_dir)
    if not disc.exists():
        click.echo("No T2 daemon discovery file found.", err=True)
        sys.exit(1)
    try:
        return json.loads(disc.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read discovery file: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# nx daemon t2 exec  (admin -- UDS only)
# ---------------------------------------------------------------------------


@t2_group.command("exec")
@click.option("--config-dir", "config_dir_str", default=None, help="Config directory override.")
@click.option(
    "--raw",
    "sql",
    required=True,
    metavar="SQL",
    help="SQL string to execute (read-only; mode=ro enforced by daemon).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def exec_cmd(config_dir_str: str | None, sql: str, as_json: bool) -> None:
    """Execute read-only SQL on the T2 database via the daemon (admin, UDS only).

    Writes are rejected by SQLite mode=ro on the daemon side.

    Example:
      nx daemon t2 exec --raw "SELECT COUNT(*) FROM memory"
    """
    from nexus.daemon.t2_client import T2Client, T2DaemonError  # noqa: F401

    disc = _load_disc(config_dir_str)
    client = _uds_client_from_disc(disc)
    try:
        result = client.call("exec_raw", {"sql": sql})
    except T2DaemonError as exc:
        click.echo(f"Daemon error: {exc}", err=True)
        sys.exit(1)
    except (ConnectionError, OSError) as exc:
        click.echo(f"Connection error: {exc}", err=True)
        sys.exit(1)
    finally:
        client.close()

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    if not result:
        click.echo("(no rows)")
        return
    headers = list(result[0].keys())
    widths = [
        max(len(h), max((len(str(row.get(h, ""))) for row in result), default=0))
        for h in headers
    ]
    click.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    click.echo("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in result:
        click.echo("  ".join(str(row.get(h, "")).ljust(w) for h, w in zip(headers, widths)))


# ---------------------------------------------------------------------------
# nx daemon t2 schema
# ---------------------------------------------------------------------------


@t2_group.command("schema")
@click.option("--config-dir", "config_dir_str", default=None, help="Config directory override.")
@click.option("--tables", "tables_filter", default=None, help="Filter to a specific table name.")
@click.option("--indexes", "indexes", is_flag=True, default=False, help="Include indexes.")
@click.option("--fts", "fts", is_flag=True, default=False, help="Include FTS5 virtual tables.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def schema_cmd(
    config_dir_str: str | None,
    tables_filter: str | None,
    indexes: bool,
    fts: bool,
    as_json: bool,
) -> None:
    """Print T2 database schema.

    With no flags, returns all tables. Use --tables NAME, --indexes, --fts
    to restrict output. Safe over TCP.
    """
    from nexus.daemon.t2_client import T2Client, T2DaemonError  # noqa: F401

    disc = _load_disc(config_dir_str)
    client = _client_from_disc(disc)

    filters: dict = {}
    if tables_filter is not None:
        filters["tables"] = tables_filter
    if indexes:
        filters["indexes"] = True
    if fts:
        filters["fts"] = True

    try:
        result = client.call("schema", {"filters": filters if filters else None})
    except T2DaemonError as exc:
        click.echo(f"Daemon error: {exc}", err=True)
        sys.exit(1)
    finally:
        client.close()

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    if "tables" in result:
        click.echo(f"Tables ({len(result['tables'])}):")
        for t in result["tables"]:
            click.echo(f"  {t['name']}")
            for col in t.get("columns", []):
                click.echo(f"    {col['name']} {col['type']}")
    if "indexes" in result:
        click.echo(f"Indexes ({len(result['indexes'])}):")
        for idx in result["indexes"]:
            click.echo(f"  {idx['name']} on {idx['table']}")
    if "fts" in result:
        click.echo(f"FTS5 tables ({len(result['fts'])}):")
        for t in result["fts"]:
            click.echo(f"  {t['name']}")


# ---------------------------------------------------------------------------
# nx daemon t2 peek
# ---------------------------------------------------------------------------


@t2_group.command("peek")
@click.option("--config-dir", "config_dir_str", default=None, help="Config directory override.")
@click.argument("table")
@click.option("--offset", default=0, show_default=True, help="Row offset.")
@click.option("--limit", default=20, show_default=True, help="Rows to return (max 300).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def peek_cmd(
    config_dir_str: str | None,
    table: str,
    offset: int,
    limit: int,
    as_json: bool,
) -> None:
    """Show rows from a T2 table (paged, max 300 per call). Safe over TCP.

    Example:
      nx daemon t2 peek memory --limit 10
    """
    from nexus.daemon.t2_client import T2Client, T2DaemonError  # noqa: F401

    disc = _load_disc(config_dir_str)
    client = _client_from_disc(disc)
    try:
        result = client.call("peek", {"table": table, "offset": offset, "limit": limit})
    except T2DaemonError as exc:
        click.echo(f"Daemon error: {exc}", err=True)
        sys.exit(1)
    finally:
        client.close()

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    rows = result.get("rows", [])
    total = result.get("total", 0)
    eff_limit = result.get("limit", limit)
    click.echo(f"Table: {table}  total={total}  offset={offset}  limit={eff_limit}")
    if not rows:
        click.echo("(no rows)")
        return
    headers = list(rows[0].keys())
    widths = [
        max(len(h), max((len(str(row.get(h, ""))) for row in rows), default=0))
        for h in headers
    ]
    click.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    click.echo("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        click.echo("  ".join(str(row.get(h, "")).ljust(w) for h, w in zip(headers, widths)))


# ---------------------------------------------------------------------------
# nx daemon t2 stats
# ---------------------------------------------------------------------------


@t2_group.command("stats")
@click.option("--config-dir", "config_dir_str", default=None, help="Config directory override.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def stats_cmd(config_dir_str: str | None, as_json: bool) -> None:
    """Print T2 database statistics (row counts, file sizes). Safe over TCP."""
    from nexus.daemon.t2_client import T2Client, T2DaemonError  # noqa: F401

    disc = _load_disc(config_dir_str)
    client = _client_from_disc(disc)
    try:
        result = client.call("stats", {})
    except T2DaemonError as exc:
        click.echo(f"Daemon error: {exc}", err=True)
        sys.exit(1)
    finally:
        client.close()

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo("T2 Stats")
    click.echo("-" * 40)
    tables = result.get("tables", {})
    if tables:
        click.echo("Row counts:")
        for tname, count in sorted(tables.items()):
            click.echo(f"  {tname}: {count}")
    click.echo(f"memory_db: {result.get('memory_db_bytes', 0):,} bytes")
    click.echo(f"memory_db WAL: {result.get('memory_db_wal_bytes', 0):,} bytes")
    click.echo(f"tuples_db: {result.get('tuples_db_bytes', 0):,} bytes")
    click.echo(f"tuples_db WAL: {result.get('tuples_db_wal_bytes', 0):,} bytes")


# ---------------------------------------------------------------------------
# nx daemon t2 export  (admin -- UDS only)
# ---------------------------------------------------------------------------


@t2_group.command("export")
@click.option("--config-dir", "config_dir_str", default=None, help="Config directory override.")
@click.option("--table", "table", default=None, help="Table to export (omit for all tables).")
@click.option(
    "--format",
    "fmt",
    default="jsonl",
    show_default=True,
    type=click.Choice(["jsonl", "csv", "sqlite"]),
    help="Output format.",
)
@click.argument("dest")
def export_cmd(config_dir_str: str | None, table: str | None, fmt: str, dest: str) -> None:
    """Export a T2 table (or all tables) to DEST on the daemon host (admin, UDS only).

    Examples:
      nx daemon t2 export --table memory --format jsonl /tmp/memory.jsonl
      nx daemon t2 export --format sqlite /tmp/backup.db
    """
    from nexus.daemon.t2_client import T2Client, T2DaemonError  # noqa: F401

    disc = _load_disc(config_dir_str)
    client = _uds_client_from_disc(disc)
    try:
        result = client.call("export", {"table": table, "format": fmt, "dest_path": dest})
    except T2DaemonError as exc:
        click.echo(f"Daemon error: {exc}", err=True)
        sys.exit(1)
    except (ConnectionError, OSError) as exc:
        click.echo(f"Connection error: {exc}", err=True)
        sys.exit(1)
    finally:
        client.close()

    click.echo(
        f"Export complete: {result.get('rows', '?')} rows, "
        f"{result.get('bytes_written', 0):,} bytes -> {result.get('path', dest)}"
    )


# ---------------------------------------------------------------------------
# nx daemon t2 install / uninstall --autostart  (RDR-112, nexus-6w0c)
# ---------------------------------------------------------------------------
#
# After OS reboot the user should not have to manually run
# ``nx daemon t2 start``. We ship a launchd plist (macOS) and a systemd
# user-unit (Linux) under ``src/nexus/_resources/daemon/``, force-included
# into the wheel. ``install --autostart`` renders the template, drops it
# into the per-OS location, and bootstraps it; ``uninstall --autostart``
# is the symmetric remove.

_PLIST_NAME = "com.nexus.t2.plist"
_SERVICE_NAME = "nexus-t2.service"
_LAUNCHD_LABEL = "com.nexus.t2"


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
    """Expand the ``<string>__NX_BIN__</string>`` line into one entry per token.

    The plist's ``ProgramArguments`` array gives launchd one ``<string>``
    per argv element. A multi-token fallback (``["/path/python", "-m",
    "nexus.cli"]``) must therefore render as multiple ``<string>``
    siblings, not a single space-joined string — launchd treats one
    ``<string>`` slot as a single executable name, so a single
    ``"<string>python -m nexus.cli</string>"`` makes ``posix_spawn``
    fail with ENOENT.
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

    ``nx_bin`` is a token list. The plist substitutes the
    ``<string>__NX_BIN__</string>`` line into one ``<string>`` per
    token; the systemd unit's ``ExecStart=__NX_BIN__ ...`` line uses
    ``shlex.join`` so multi-token argvs survive systemd's
    whitespace-split parser.
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

    Returns a single-element list when ``nx`` is on ``$PATH`` (the
    common case for ``uv tool install``). Falls back to a three-element
    list ``[python, "-m", "nexus.cli"]`` when ``shutil.which("nx")``
    returns ``None``. Callers must respect the token boundaries when
    rendering into platform autostart formats: a multi-element list
    cannot be flattened into a single ``<string>`` plist slot, and the
    systemd ``ExecStart=`` line must shlex-quote each token so paths
    with spaces stay one argument.
    """
    found = shutil.which("nx")
    if found:
        return [found]
    return [sys.executable, "-m", "nexus.cli"]


def _autostart_filename() -> str:
    return _PLIST_NAME if _autostart_platform() == "darwin" else _SERVICE_NAME


def _read_installed_autostart_nx_bin() -> str | None:
    """Return the nx_bin path from the installed autostart file.

    nexus-2wvl: ``_resolve_nx_bin`` is called at install time; after a
    later ``pip install --upgrade`` (or a ``uv tool`` update that
    relocates the entry point), the path baked into the autostart file
    can become stale. ``nx doctor --check-bridge`` reads this value and
    verifies the binary still exists on disk; if not, it tells the
    operator to re-render via ``install --autostart --force``.

    Returns:
        The first argument of the autostart command line as a string,
        or ``None`` when no autostart is installed for the current
        platform OR when the file format is unrecognised.

        - macOS: ``ProgramArguments[0]`` from the launchd plist.
        - Linux: the first whitespace-separated token of the
          ``ExecStart=`` line in the systemd user unit.
    """
    try:
        install_dir = _autostart_install_dir()
    except click.ClickException:
        return None  # platform unsupported
    dest = install_dir / _autostart_filename()
    if not dest.is_file():
        return None

    platform = _autostart_platform()
    try:
        if platform == "darwin":
            import plistlib
            data = plistlib.loads(dest.read_bytes())
            args = data.get("ProgramArguments")
            if isinstance(args, list) and args:
                return str(args[0])
            return None
        # Linux / systemd
        for line in dest.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("ExecStart="):
                tokens = shlex.split(stripped[len("ExecStart="):])
                if tokens:
                    return tokens[0]
                return None
        return None
    except Exception:  # noqa: BLE001 -- diagnostic helper, never raises
        return None


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
    help=(
        "Two effects, both intentional: (1) overwrite an existing "
        "plist/unit file even when its content differs from the "
        "freshly rendered template (nexus-31cr); (2) treat supervisor "
        "activation failures (launchctl/systemctl exit non-zero, or "
        "binary missing on PATH) as warnings instead of errors. "
        "Without ``--force`` an operator-customised file is preserved "
        "and the command refuses with a diagnostic naming this flag."
    ),
)
def install_cmd(autostart: bool, force: bool) -> None:
    """Install the T2 daemon autostart entry for the current user.

    macOS: writes ~/Library/LaunchAgents/com.nexus.t2.plist and bootstraps
    it via ``launchctl bootstrap gui/$UID``.

    Linux: writes ~/.config/systemd/user/nexus-t2.service and enables it
    via ``systemctl --user enable --now nexus-t2.service``.

    Exit codes:
      0 - install + activation both succeeded, or activation failed under
          ``--force`` (warning emitted, file still on disk), or the file
          on disk is already byte-identical to the rendered template
          (no-op).
      1 - file installed but supervisor activation failed (no ``--force``),
          OR existing file content differs from the rendered template and
          ``--force`` was not supplied (overwrite refusal). Operators or
          CI scripts that check ``$?`` see the failure and can react.
    """
    if not autostart:  # pragma: no cover -- click enforces required=True
        raise click.UsageError("--autostart is required")

    install_dir = _autostart_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    log_dir = _autostart_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    template_name = _autostart_filename()
    nx_bin = _resolve_nx_bin()
    rendered = _render_template(
        template_name,
        nx_bin=nx_bin,
        log_dir=str(log_dir),
        path_env=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    )
    dest = install_dir / template_name

    # nexus-31cr: overwrite guard. Operator may have customised the file
    # (added EnvironmentVariables, changed log dir, etc.). Preserve it
    # unless --force is given.
    #
    # nexus-26b7 (notable, dim-8 FS-7): refuse to read/write through a
    # symlink. Without this, the read_text + write_text pair below
    # follows the symlink and our overwrite guard inspects (and then
    # rewrites) the symlink target instead of the autostart file the
    # operator believes they are installing.
    if dest.is_symlink():
        click.echo(
            f"Error: {dest} is a symlink; refusing to install autostart "
            "through it (FS-7 TOCTOU mitigation). Remove the symlink "
            "first and re-run.",
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


@t2_group.command("uninstall")
@click.option(
    "--autostart",
    is_flag=True,
    required=True,
    help="Remove OS autostart entry installed by ``install --autostart``.",
)
def uninstall_cmd(autostart: bool) -> None:
    """Remove the T2 daemon autostart entry for the current user."""
    if not autostart:  # pragma: no cover
        raise click.UsageError("--autostart is required")

    install_dir = _autostart_install_dir()
    template_name = _autostart_filename()
    dest = install_dir / template_name

    if not dest.exists():
        click.echo(f"Autostart not installed (nothing at {dest}).")
        return

    platform = _autostart_platform()
    if platform == "darwin":
        uid = os.getuid()
        cmd = ["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"]
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
# t3 sub-group  (RDR-112 P1.5.1, nexus-s3dm)
# ---------------------------------------------------------------------------
#
# The T3 daemon is a managed ``chroma run`` subprocess. chromadb's bundled
# HTTP server is the production-quality RPC layer (RDR-112 §A1); this group
# owns the subprocess lifecycle + discovery file. T3Client (P1.5.3 /
# nexus-7yd2) and ``discovery_resolve('t3')`` (P1.5.2 / nexus-n8xg) ship in
# follow-on beads.


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
        "Emit the discovery JSON on stdout at startup. Default off "
        "(mirrors T2 nexus-l712): the discovery file at "
        "~/.config/nexus/t3_addr.<uid> is the primary channel."
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

    Idempotent on a live daemon: if a discovery file exists and its PID is
    still alive, prints the existing discovery payload without spawning a
    duplicate. Cloud mode (NX_LOCAL=0) fails loud — chromadb's CloudClient
    is already HTTP-served.

    Without ``--foreground`` the CLI exits as soon as the chroma
    subprocess is listening on its TCP port. ``--foreground`` blocks
    until SIGTERM/SIGINT arrives (or chroma exits on its own); used by
    the launchd/systemd autostart templates so the supervisor observes
    a long-running foreground process and can react to crashes via
    ``KeepAlive.Crashed`` / ``Restart=on-failure``. (nexus-6j2f
    review C1.)
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

    # ── --foreground: supervisor-friendly blocking loop ──────────────────
    # The chroma subprocess is in its OWN session (start_new_session=True
    # in start_t3_daemon), so SIGTERM delivered to this CLI process does
    # NOT propagate to chroma automatically. The signal handler below
    # explicitly calls stop_t3_daemon to clean up.
    import threading
    import time
    stop_requested = threading.Event()

    def _on_signal(_signum, _frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    pid = payload["pid"]
    while not stop_requested.is_set():
        if not _pid_is_alive(pid):
            # Chroma exited on its own — return non-zero so the
            # supervisor's crash-handler (launchd KeepAlive.Crashed
            # / systemd Restart=on-failure) fires.
            click.echo(
                f"T3 chroma subprocess (pid={pid}) exited unexpectedly; "
                "the supervisor will restart it.",
                err=True,
            )
            sys.exit(3)
        time.sleep(0.5)

    # Graceful shutdown path: stop_t3_daemon signals the chroma session
    # group and unlinks the discovery file. Exit 0 so the supervisor
    # treats this as an operator-requested stop, not a crash.
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


@t3_group.command("info")
@click.option(
    "--config-dir",
    "config_dir_str",
    default=None,
    help="Config directory override.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Output raw JSON."
)
def t3_info_cmd(config_dir_str: str | None, as_json: bool) -> None:
    """Print the T3 daemon discovery JSON (or formatted summary)."""
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
    click.echo("T3 Daemon Info")
    click.echo("-" * 40)
    for key, value in data.items():
        click.echo(f"  {key}: {value}")


# ---------------------------------------------------------------------------
# T3 autostart install / uninstall  (RDR-112 P1.5.4, nexus-v5hb)
# ---------------------------------------------------------------------------
#
# Mirrors the T2 install/uninstall flow at daemon.py:896-1064. Templates
# ship under src/nexus/_resources/daemon/com.nexus.t3.plist (macOS) and
# nexus-t3.service (Linux). The launchd plist + systemd unit shape is
# the battle-tested T2 template adapted for the T3 subcommand: the
# supervisor invokes ``nx daemon t3 start`` instead of ``nx daemon t2
# start --foreground``, because the T3 daemon's ``start`` already
# blocks until SIGTERM (it foregrounds the chroma run subprocess).

_T3_PLIST_NAME = "com.nexus.t3.plist"
_T3_SERVICE_NAME = "nexus-t3.service"
_T3_LAUNCHD_LABEL = "com.nexus.t3"


def _autostart_filename_t3() -> str:
    return _T3_PLIST_NAME if _autostart_platform() == "darwin" else _T3_SERVICE_NAME


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

    macOS: writes ~/Library/LaunchAgents/com.nexus.t3.plist and
    bootstraps it via ``launchctl bootstrap gui/$UID``.

    Linux: writes ~/.config/systemd/user/nexus-t3.service and enables
    it via ``systemctl --user enable --now nexus-t3.service``.

    The shipped templates point the supervisor at ``nx daemon t3 start``;
    the T3 ``start`` command spawns the managed chroma run subprocess
    and the daemon-start helper exits once chroma is listening (process
    group survives via ``start_new_session=True``).
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

    # Symlink + overwrite-guard semantics mirror T2's install_cmd
    # (daemon.py:957-990) exactly; the operator-customisation
    # preservation contract is identical for T3.
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

