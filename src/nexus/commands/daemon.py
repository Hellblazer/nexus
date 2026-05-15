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
    loopback TCP port. Constructs ``T2Database`` + ``RegistryStore`` so
    domain-store RPCs (``memory.*``, ``plans.*`` etc.), introspection
    RPCs (``exec_raw``, ``schema``, ``peek``, ``stats``, ``export``),
    and the ``subspace_add`` admin RPC are all served. Writes a
    discovery file so clients can find it.

    With --foreground the process blocks until SIGTERM or SIGINT.
    Without --foreground (default) the process daemonises and returns
    immediately with the discovery JSON printed to stdout.
    """
    from nexus.daemon.subspace_registry import RegistryStore
    from nexus.daemon.t2_daemon import T2Daemon
    from nexus.db.t2 import T2Database

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
        daemon = T2Daemon(
            config_dir=config_dir,
            t2db=t2db,
            tuples_db_path=tuples_db_path,
            registry_store=registry_store,
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
        with client._get_pool().acquire() as conn:
            result = conn.call("exec_raw", {"sql": sql})
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
        with client._get_pool().acquire() as conn:
            result = conn.call("schema", {"filters": filters if filters else None})
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
        with client._get_pool().acquire() as conn:
            result = conn.call("peek", {"table": table, "offset": offset, "limit": limit})
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
        with client._get_pool().acquire() as conn:
            result = conn.call("stats", {})
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
        with client._get_pool().acquire() as conn:
            result = conn.call("export", {"table": table, "format": fmt, "dest_path": dest})
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

