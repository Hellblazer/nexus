# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx tuplespace`` -- consumer CLI for the RDR-110 tuple space.

Provides read, write, and introspection access to the registered
coordination subspaces (tasks, mailbox, locks, events, barriers, hooks).
The CLI mirrors the MCP tool surface in ``nexus.mcp.core`` so agents and
humans use the same vocabulary.

Daemon-mode (``NX_STORAGE_MODE=daemon``) is honoured: when the daemon owns
``tuples.db``, this command refuses mutating subcommands rather than
opening a competing SQLite connection. Read-only introspection
(``list-subspaces``, ``show-schema``) is always safe because it touches
the registry only.

The ``out`` / ``read`` / ``take`` / ``ack`` / ``nack`` subcommands are a
smoke-test surface intended for interactive debugging and shell scripts.
Production agents use the MCP tools (``tuplespace_out`` etc.) so claim
ownership and CAS atomicity remain process-local to the MCP server.
"""
from __future__ import annotations

import json as _json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import click
import structlog

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _resolve_tuples_db_path() -> Path:
    """Return the canonical tuples.db path, honouring ``NX_TUPLES_DB``."""
    override = os.environ.get("NX_TUPLES_DB")
    if override:
        return Path(override).expanduser()
    from nexus.config import load_config

    cfg = load_config()
    nexus_dir = cfg.get("nexus_dir", "~/.config/nexus")
    return Path(os.path.expanduser(f"{nexus_dir}/tuples.db"))


def _is_daemon_mode() -> bool:
    from nexus.db import is_daemon_mode
    return is_daemon_mode()


def _load_registry():
    """Return a loaded ``Registry``. Honours ``NX_TUPLESPACE_BUILTIN_DIR``."""
    from nexus.tuplespace.registry import Registry, default_builtin_dir

    override = os.environ.get("NX_TUPLESPACE_BUILTIN_DIR")
    builtin_dir = Path(override).expanduser() if override else default_builtin_dir()
    return Registry.load(builtin_dir)


def _open_conn(db_path: Path) -> sqlite3.Connection:
    """Open ``tuples.db`` for direct-mode operations."""
    from nexus.tuplespace.store import open_tuples_db

    conn = open_tuples_db(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _build_index(registry):
    """Construct a ``TupleIndex`` rooted at the local chroma directory."""
    import chromadb

    from nexus.config import load_config
    from nexus.tuplespace.index import TupleIndex

    cfg = load_config()
    nexus_dir = cfg.get("nexus_dir", "~/.config/nexus")
    chroma_dir = os.path.expanduser(f"{nexus_dir}/chroma")
    # storage-boundary-allow: tuplespace CLI subcommand for direct-mode
    # ops; daemon-mode callers go through TuplespaceService via T2Client.
    client = chromadb.PersistentClient(path=chroma_dir)
    return TupleIndex.from_registry(registry, client)


def _refuse_in_daemon_mode(action: str) -> None:
    """Exit cleanly when a mutating subcommand is invoked in daemon mode."""
    if _is_daemon_mode():
        click.echo(
            f"refusing to {action} in NX_STORAGE_MODE=daemon (daemon owns tuples.db). "
            "Use the MCP tool surface instead.",
            err=True,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# Banner support (used by session_start_hook and `nx tuplespace stats`)
# ---------------------------------------------------------------------------


def banner_summary() -> dict[str, int]:
    """Return a compact dict: ``{subspaces, tuples, active_claims}``.

    Side effect free in daemon mode: returns counts of zero for ``tuples``
    and ``active_claims`` because opening the SQLite file would race the
    daemon's single writer. ``subspaces`` always reflects the registry,
    which is loaded from YAML files only.
    """
    summary = {"subspaces": 0, "tuples": 0, "active_claims": 0}
    try:
        registry = _load_registry()
        summary["subspaces"] = len(list(registry.schemas()))
    except Exception as exc:
        _log.warning("tuplespace_banner_registry_failed", error=str(exc))
        return summary

    if _is_daemon_mode():
        return summary

    db_path = _resolve_tuples_db_path()
    if not db_path.exists():
        return summary

    try:
        conn = _open_conn(db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM tuples").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM tuples "
                "WHERE claim_state='claimed' AND claim_expires_at >= unixepoch()"
            ).fetchone()[0]
            summary["tuples"] = int(total or 0)
            summary["active_claims"] = int(active or 0)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.warning("tuplespace_banner_sqlite_failed", error=str(exc))
    return summary


def banner_line() -> str:
    """Return the one-line session-start banner string."""
    s = banner_summary()
    return (
        f"tuplespace: {s['subspaces']} subspaces, "
        f"{s['tuples']} tuples, {s['active_claims']} active claims"
    )


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


@click.group("tuplespace")
def tuplespace_group() -> None:
    """Inspect and exercise the RDR-110 tuple space.

    Subcommands provide read-only introspection (``list-subspaces``,
    ``show-schema``, ``stats``) and a smoke-test surface for
    ``out`` / ``read`` / ``take`` / ``ack`` / ``nack``. Production agents
    use the MCP tools; this CLI is for shell scripts and interactive
    debugging.
    """


@tuplespace_group.command("list-subspaces")
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit JSON instead of a plain list.",
)
def list_subspaces_cmd(json_out: bool) -> None:
    """List every registered subspace template (e.g. ``tasks/<project>``)."""
    registry = _load_registry()
    names = sorted(s.name for s in registry.schemas())
    if json_out:
        click.echo(_json.dumps({"subspaces": names}, indent=2))
        return
    if not names:
        click.echo("(no subspaces registered)")
        return
    for name in names:
        click.echo(name)


@tuplespace_group.command("show-schema")
@click.argument("subspace")
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit JSON instead of a human summary.",
)
def show_schema_cmd(subspace: str, json_out: bool) -> None:
    """Print the resolved schema for a concrete or template subspace."""
    from nexus.tuplespace.api import subspace_schema
    from nexus.tuplespace.registry import UnknownSubspaceError

    registry = _load_registry()
    try:
        schema = subspace_schema(registry=registry, subspace=subspace)
    except UnknownSubspaceError as exc:
        click.echo(f"unknown subspace: {exc}", err=True)
        sys.exit(1)

    if json_out:
        click.echo(_json.dumps(schema, indent=2, sort_keys=True))
        return

    click.echo(f"name:             {schema['name']}")
    click.echo(f"tier:             {schema['tier']}")
    click.echo(f"content_type:     {schema['content_type']}")
    click.echo(f"embed_from:       {schema['embed_from']}")
    click.echo(f"retention_seconds: {schema['retention_seconds']}")
    click.echo("dimensions:")
    for k, v in (schema.get("dimensions") or {}).items():
        click.echo(f"  {k}: {v}")
    click.echo(f"take: {schema['take']}")
    click.echo(f"read: {schema['read']}")


@tuplespace_group.command("stats")
@click.argument("subspace", required=False)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit JSON instead of a human table.",
)
def stats_cmd(subspace: str | None, json_out: bool) -> None:
    """Show tuple counts (total, available, claimed, consumed).

    With no SUBSPACE: prints the banner summary plus a per-subspace
    breakdown across every concrete subspace observed in ``tuples.db``.
    With a SUBSPACE argument: prints counts for that subspace only.
    """
    db_path = _resolve_tuples_db_path()
    if _is_daemon_mode():
        click.echo(
            "stats unavailable in NX_STORAGE_MODE=daemon "
            "(daemon owns tuples.db; query via MCP tools).",
            err=True,
        )
        sys.exit(2)

    if not db_path.exists():
        if json_out:
            click.echo(_json.dumps({"db_path": str(db_path), "exists": False, "summary": banner_summary()}))
            return
        click.echo(f"tuples.db not found at {db_path} (no tuples written yet)")
        click.echo(banner_line())
        return

    conn = _open_conn(db_path)
    try:
        if subspace:
            from nexus.tuplespace.api import subspace_stats

            stats = subspace_stats(conn=conn, subspace=subspace)
            if json_out:
                click.echo(_json.dumps({"subspace": subspace, **stats}, indent=2))
                return
            click.echo(f"subspace:  {subspace}")
            click.echo(f"total:     {stats['total']}")
            click.echo(f"available: {stats['available']}")
            click.echo(f"claimed:   {stats['claimed']}")
            click.echo(f"consumed:  {stats['consumed']}")
            return

        rows = conn.execute(
            "SELECT subspace, "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN consumed_at IS NULL "
            "           AND (claim_state IS NULL OR claim_expires_at < unixepoch()) "
            "           THEN 1 ELSE 0 END) AS available, "
            "  SUM(CASE WHEN claim_state='claimed' "
            "           AND claim_expires_at >= unixepoch() THEN 1 ELSE 0 END) AS claimed, "
            "  SUM(CASE WHEN consumed_at IS NOT NULL THEN 1 ELSE 0 END) AS consumed "
            "FROM tuples GROUP BY subspace ORDER BY subspace"
        ).fetchall()
        summary = banner_summary()
        per_subspace = [
            {
                "subspace": r[0],
                "total": int(r[1] or 0),
                "available": int(r[2] or 0),
                "claimed": int(r[3] or 0),
                "consumed": int(r[4] or 0),
            }
            for r in rows
        ]
        if json_out:
            click.echo(_json.dumps({"summary": summary, "per_subspace": per_subspace}, indent=2))
            return
        click.echo(banner_line())
        if not per_subspace:
            click.echo("(no concrete subspaces have tuples yet)")
            return
        click.echo("")
        click.echo(f"{'SUBSPACE':<40} {'TOTAL':>6} {'AVAIL':>6} {'CLAIM':>6} {'CONSU':>6}")
        for row in per_subspace:
            click.echo(
                f"{row['subspace']:<40} {row['total']:>6} {row['available']:>6} "
                f"{row['claimed']:>6} {row['consumed']:>6}"
            )
    finally:
        conn.close()


def _parse_json_arg(value: str, label: str) -> Any:
    try:
        return _json.loads(value)
    except _json.JSONDecodeError as exc:
        click.echo(f"invalid JSON for {label}: {exc}", err=True)
        sys.exit(1)


@tuplespace_group.command("out")
@click.argument("subspace")
@click.argument("dimensions_json")
@click.option("--content", default="", help="Tuple body text.")
@click.option("--match-text", default=None, help="Override for the embedding source.")
@click.option("--ttl-seconds", type=float, default=None, help="TTL in seconds (default: no expiry).")
def out_cmd(
    subspace: str,
    dimensions_json: str,
    content: str,
    match_text: str | None,
    ttl_seconds: float | None,
) -> None:
    """Post a tuple to SUBSPACE. DIMENSIONS_JSON is a JSON object."""
    _refuse_in_daemon_mode("write tuples")
    from nexus.tuplespace.api import out

    dims = _parse_json_arg(dimensions_json, "DIMENSIONS_JSON")
    if not isinstance(dims, dict):
        click.echo("DIMENSIONS_JSON must be a JSON object", err=True)
        sys.exit(1)

    registry = _load_registry()
    db_path = _resolve_tuples_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open_conn(db_path)
    index = _build_index(registry)
    try:
        tid = out(
            conn=conn,
            index=index,
            registry=registry,
            subspace=subspace,
            content=content,
            dimensions=dims,
            match_text=match_text,
            ttl_seconds=ttl_seconds,
        )
    finally:
        conn.close()
    click.echo(tid)


@tuplespace_group.command("read")
@click.argument("subspace")
@click.option("--query", default="", help="Semantic query text.")
@click.option("--where", "where_json", default=None, help="JSON filter dict.")
@click.option("--floor", type=float, default=None, help="Minimum similarity.")
@click.option("-n", "max_results", type=int, default=None, help="Max results.")
def read_cmd(
    subspace: str,
    query: str,
    where_json: str | None,
    floor: float | None,
    max_results: int | None,
) -> None:
    """Read tuples matching QUERY from SUBSPACE (non-destructive)."""
    _refuse_in_daemon_mode("read tuples")
    from nexus.tuplespace.api import read

    where = _parse_json_arg(where_json, "--where") if where_json else None
    registry = _load_registry()
    db_path = _resolve_tuples_db_path()
    if not db_path.exists():
        click.echo(_json.dumps({"results": []}))
        return
    conn = _open_conn(db_path)
    index = _build_index(registry)
    try:
        results = read(
            conn=conn,
            index=index,
            registry=registry,
            subspace=subspace,
            query=query,
            where=where,
            floor=floor,
            n=max_results,
        )
    finally:
        conn.close()
    click.echo(_json.dumps({"results": results}, indent=2, default=str))


@tuplespace_group.command("take")
@click.argument("subspace")
@click.option("--query", default="", help="Semantic query text.")
@click.option("--claimant", required=True, help="Identifier for the claiming agent.")
@click.option("--where", "where_json", default=None, help="JSON filter dict.")
@click.option("--floor", type=float, default=None, help="Minimum similarity.")
@click.option("--lease-seconds", type=float, default=None, help="Claim lease seconds.")
def take_cmd(
    subspace: str,
    query: str,
    claimant: str,
    where_json: str | None,
    floor: float | None,
    lease_seconds: float | None,
) -> None:
    """Atomically claim a tuple from SUBSPACE (destructive)."""
    _refuse_in_daemon_mode("take tuples")
    from nexus.tuplespace.api import take

    where = _parse_json_arg(where_json, "--where") if where_json else None
    registry = _load_registry()
    db_path = _resolve_tuples_db_path()
    if not db_path.exists():
        click.echo(_json.dumps({"claimed": False, "reason": "tuples.db missing"}))
        return
    conn = _open_conn(db_path)
    index = _build_index(registry)
    try:
        result = take(
            conn=conn,
            index=index,
            registry=registry,
            subspace=subspace,
            query=query,
            claimant=claimant,
            where=where,
            floor=floor,
            lease_seconds=lease_seconds,
        )
    finally:
        conn.close()
    if result is None:
        click.echo(_json.dumps({"claimed": False}))
        return
    tup, cid = result
    click.echo(_json.dumps({"claimed": True, "claim_id": cid, "tuple": tup}, indent=2, default=str))


@tuplespace_group.command("ack")
@click.argument("claim_id")
@click.option("--claimant", required=True, help="The claiming agent (must match take).")
def ack_cmd(claim_id: str, claimant: str) -> None:
    """Acknowledge a claim: mark the tuple consumed."""
    _refuse_in_daemon_mode("ack claims")
    from nexus.tuplespace.api import ack, ClaimNotFoundError, ClaimOwnershipError

    db_path = _resolve_tuples_db_path()
    if not db_path.exists():
        click.echo("tuples.db missing", err=True)
        sys.exit(1)
    conn = _open_conn(db_path)
    try:
        try:
            ack(conn=conn, claim_id=claim_id, claimant=claimant)
        except (ClaimNotFoundError, ClaimOwnershipError) as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
    finally:
        conn.close()
    click.echo("ok")


@tuplespace_group.command("nack")
@click.argument("claim_id")
@click.option("--claimant", required=True, help="The claiming agent (must match take).")
def nack_cmd(claim_id: str, claimant: str) -> None:
    """Negative-ack a claim: release it back to available."""
    _refuse_in_daemon_mode("nack claims")
    from nexus.tuplespace.api import nack, ClaimNotFoundError, ClaimOwnershipError

    db_path = _resolve_tuples_db_path()
    if not db_path.exists():
        click.echo("tuples.db missing", err=True)
        sys.exit(1)
    conn = _open_conn(db_path)
    try:
        try:
            nack(conn=conn, claim_id=claim_id, claimant=claimant)
        except (ClaimNotFoundError, ClaimOwnershipError) as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
    finally:
        conn.close()
    click.echo("ok")
