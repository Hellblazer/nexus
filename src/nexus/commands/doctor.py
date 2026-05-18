# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import click
import structlog

from nexus.registry import RepoRegistry

_log = structlog.get_logger(__name__)

_CHECK = "✓"
_WARN = "✗"


def _check_line(label: str, ok: bool, detail: str = "") -> str:
    status = _CHECK if ok else _WARN
    msg = f"  {status} {label}"
    if detail:
        msg += f": {detail}"
    return msg


def _fix(lines: list[str], *fix_lines: str) -> None:
    """Append indented Fix: lines after a failure entry."""
    first = True
    for fix_line in fix_lines:
        if first:
            lines.append(f"    Fix: {fix_line}")
            first = False
        else:
            lines.append(f"         {fix_line}")


# Keep old name so existing tests importing `_check` still work.
def _check(label: str, ok: bool, detail: str = "") -> str:
    return _check_line(label, ok, detail)


class _T2Inspector:
    """Daemon-aware T2 introspection adapter for doctor checks.

    Doctor checks have historically opened a direct ``sqlite3``
    connection against ``memory.db`` to query ``sqlite_master`` /
    ``PRAGMA`` / scalar counts. Under ``NX_STORAGE_MODE=daemon`` that
    races the daemon writer; the prior code called
    ``reject_under_daemon_mode`` and refused to run, leaving operators
    with no diagnostic surface in the daemon's default mode.

    nexus-pac1 (RDR-112 P4.5): this adapter exposes a small,
    uniform read interface that the check functions consume. Under
    daemon mode it routes through the daemon's introspection RPCs
    (``schema`` for ``sqlite_master`` lists; ``exec_raw`` for arbitrary
    SELECT). Under direct mode it opens a local ``sqlite3.Connection``
    and runs the same queries directly. The check bodies stay
    identical.

    Usage::

        with _T2Inspector(db_path) as t2:
            tables = t2.tables()
            rows = t2.execute("SELECT COUNT(*) FROM plans WHERE ...")

    Read-only by contract. The daemon's ``exec_raw`` opens its
    connection with ``mode=ro``; direct mode is non-mutating by
    convention (doctor checks never write to ``memory.db``).
    """

    def __init__(self, db_path: "Path") -> None:
        from nexus.db import is_daemon_mode
        self._mode = "daemon" if is_daemon_mode() else "direct"
        self._db_path = db_path
        self._conn = None
        self._client_ctx = None
        self._client = None
        if self._mode == "daemon":
            from nexus.mcp_infra import t2_ctx
            self._client_ctx = t2_ctx()
            # ``t2_ctx`` under daemon mode returns a T2Client (no
            # ``__enter__`` needed in practice — the facade is reusable).
            # Under direct mode it would return a T2Database context
            # manager which the daemon path does not exercise.
            self._client = self._client_ctx
        else:
            import sqlite3
            # storage-boundary-allow: pac1 doctor T2 introspection
            #   adapter; direct-mode branch only — daemon-mode goes
            #   through the introspection RPCs.
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")

    def __enter__(self) -> "_T2Inspector":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @property
    def mode(self) -> str:
        return self._mode

    def _schema_section(self, section: str) -> set[str]:
        """Return names from ``sqlite_master`` for the given section.

        Section is one of ``tables`` / ``indexes`` / ``fts``.
        """
        if self._mode == "daemon":
            assert self._client is not None
            # IntrospectionService.schema signature: schema(filters=None).
            # The daemon expands args as kwargs, so pass the ``filters``
            # arg explicitly. Asking for db=memory restricts the result
            # to the memory.db side (no tuples.db touch).
            schema = self._client.call(
                "schema", {"filters": {"db": "memory"}}
            )["memory"]
            return {item["name"] for item in schema.get(section, [])}
        assert self._conn is not None
        if section == "tables":
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        elif section == "indexes":
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        elif section == "fts":
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND sql LIKE '%USING fts5%'"
            ).fetchall()
        else:
            raise ValueError(f"unknown schema section {section!r}")
        return {r[0] for r in rows}

    def tables(self) -> set[str]:
        return self._schema_section("tables")

    def indexes(self) -> set[str]:
        return self._schema_section("indexes")

    def fts_tables(self) -> set[str]:
        return self._schema_section("fts")

    def execute(self, sql: str) -> list[tuple]:
        """Execute a read-only SELECT and return rows as tuples.

        Daemon mode uses ``exec_raw`` (returns list[dict]); we drop
        the keys to match the sqlite3 ``.fetchall()`` shape so
        existing check-function bodies stay drop-in compatible.

        Direct mode runs against the local connection. The daemon's
        ``exec_raw`` enforces read-only via ``mode=ro``; direct mode
        relies on doctor's non-mutating convention.

        Parameter binding is NOT supported: the daemon's exec_raw
        accepts only a bare SQL string. Doctor checks never carry
        user-supplied parameters, so interpolating static values into
        the SQL string is acceptable and matches the historical
        callers (which also embedded the values inline).
        """
        if self._mode == "daemon":
            assert self._client is not None
            # exec_raw signature: exec_raw(sql).
            rows = self._client.call("exec_raw", {"sql": sql})
            return [tuple(row.values()) for row in rows]
        assert self._conn is not None
        return self._conn.execute(sql).fetchall()


def _run_check_schema() -> None:
    """Validate T2 database schema and report pending migrations (RDR-076)."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.migrations import MIGRATIONS, _parse_version

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("T2 database not found — nothing to check.")
        return

    # nexus-pac1 (RDR-112 P4.5): the previous code refused to run
    # under daemon mode (reject_under_daemon_mode). The new
    # ``_T2Inspector`` adapter routes through the daemon's
    # introspection RPCs under daemon mode and opens a local
    # sqlite3.Connection under direct mode, so the same check body
    # works in both modes.
    lines: list[str] = []
    all_ok = True
    with _T2Inspector(db_path) as t2:
        tables = t2.tables()
        for tbl in ("memory", "plans", "topics", "topic_assignments", "taxonomy_meta", "topic_links", "relevance_log", "search_telemetry", "chash_index", "hook_failures"):
            ok = tbl in tables
            lines.append(_check_line(f"Table {tbl}", ok))
            if not ok:
                all_ok = False

        # CLI review: the FTS5 virtual tables are load-bearing for memory
        # search + plan match. A schema without them passes the table
        # check but fails at query time. Include them + critical indexes.
        fts_names = t2.fts_tables()
        for fts in ("memory_fts",):
            ok = fts in fts_names or fts in tables
            lines.append(_check_line(f"FTS5 table {fts}", ok))
            if not ok:
                all_ok = False

        index_names = t2.indexes()
        expected_indexes = {
            "idx_chash_index_collection",
            "idx_topic_assignments_topic_id",
        }
        for idx in sorted(expected_indexes):
            ok = idx in index_names
            # Fail loud only for chash_index — the taxonomy index may not
            # exist on pre-4.2 schemas that ran ad-hoc migrations; note it
            # as a warning rather than a failure.
            if idx == "idx_chash_index_collection":
                lines.append(_check_line(f"Index {idx}", ok))
                if not ok:
                    all_ok = False
            else:
                if not ok:
                    lines.append(f"  note: optional index {idx} missing")

        # Check _nexus_version
        has_ver = "_nexus_version" in tables
        lines.append(_check_line("Version tracking table", has_ver))

        if has_ver:
            ver_rows = t2.execute(
                "SELECT value FROM _nexus_version WHERE key='cli_version'"
            )
            row = ver_rows[0] if ver_rows else None
            if row:
                stored = row[0]
                try:
                    from importlib.metadata import version as _pkg_version

                    cli_ver = _pkg_version("conexus")
                except Exception:
                    cli_ver = "0.0.0"
                stored_t = _parse_version(stored)
                cli_t = _parse_version(cli_ver)
                pending = [
                    m
                    for m in MIGRATIONS
                    if _parse_version(m.introduced) > stored_t
                    and _parse_version(m.introduced) <= cli_t
                ]
                if pending:
                    all_ok = False
                    lines.append(
                        _check_line(
                            "Pending migrations",
                            False,
                            f"{len(pending)} pending (stored: v{stored}, CLI: v{cli_ver})",
                        )
                    )
                    # nexus-6m9i (third 360° UPGRADE U-CRIT-1): under
                    # daemon mode `nx upgrade` is rejected; the daemon
                    # applies migrations at its own startup. Surface
                    # both recovery paths.
                    from nexus.db import is_daemon_mode as _idm
                    if _idm():
                        lines.append(
                            "    Fix (daemon mode): `nx daemon t2 stop && "
                            "nx daemon t2 start` (migrations apply at "
                            "daemon startup)."
                        )
                    else:
                        lines.append("    Fix: run 'nx upgrade'")
                else:
                    lines.append(_check_line("Schema version", True, f"v{stored}"))
            else:
                all_ok = False
                lines.append(_check_line("Version row", False, "missing"))
        else:
            all_ok = False
            lines.append("    Fix: run 'nx upgrade'")

    click.echo("T2 Schema Check:")
    for line in lines:
        click.echo(line)
    if all_ok:
        click.echo("\nAll checks passed.")


#: Minimum number of global-tier builtin plan rows expected after
#: ``nx catalog setup`` has run on a fresh install. RDR-078 shipped 9;
#: RDR-092 Phase 0a brought that to 12; RDR-097 Phase 1 added two
#: more (hybrid-factual-lookup, traverse-then-generate) for 14 total.
#: The check only fails below 9 so a partial install on an older
#: plugin is still tolerated.
_MIN_GLOBAL_BUILTIN_COUNT: int = 9


def _resolve_claude_cache_dir(cwd: Path | None = None) -> Path:
    """Return the Claude Code per-project MCP-log cache directory.

    Slug rule (observed on macOS 2026-04-25): cwd with both ``/`` and
    ``.`` replaced by ``-``. Example for cwd
    ``/Users/hal.hildebrand/git/nexus``:
    ``-Users-hal-hildebrand-git-nexus``.

    Empty slug means cwd was the filesystem root (very unusual);
    returns the cache parent so callers can detect the platform.
    """
    if cwd is None:
        cwd = Path.cwd()
    slug = str(cwd).replace("/", "-").replace(".", "-")
    if not slug or slug == "-":
        # Edge case: cwd is the root path. Return the cache parent so
        # caller's exists() check still does the right thing.
        return Path.home() / "Library" / "Caches" / "claude-cli-nodejs"
    return Path.home() / "Library" / "Caches" / "claude-cli-nodejs" / slug


#: Silent-death signatures from RDR-094 §Day 2 Operations §Diagnosing
#: nx-mcp silent death. Each signature is a substring matched against
#: the cache JSONL line's "debug" or "error" field.
_MCP_SILENT_DEATH_SIGNATURES: tuple[str, ...] = (
    "STDIO connection dropped after",
    "stdio transport error",
)

#: Tool-failure signatures (less severe; surfaced as info, not warning).
_MCP_TOOL_FAILURE_SIGNATURES: tuple[str, ...] = (
    "MCP error -32001: AbortError",
)


def _scan_mcp_log_jsonl(
    path: Path,
    cutoff_epoch: float,
) -> tuple[list[dict], list[dict]]:
    """Return (silent_deaths, tool_failures) found in *path*.

    Each match dict carries ``timestamp``, ``signature``, ``message``,
    ``session_id``, and ``log_file`` for cross-referencing against
    mcp.log + watchdog.log.
    """
    import json as _json
    import datetime as _dt

    silent_deaths: list[dict] = []
    tool_failures: list[dict] = []

    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                ts_raw = rec.get("timestamp", "")
                try:
                    ts_epoch = _dt.datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00"),
                    ).timestamp()
                except (ValueError, AttributeError):
                    continue
                if ts_epoch < cutoff_epoch:
                    continue
                msg = rec.get("debug", "") or rec.get("error", "")
                if not isinstance(msg, str):
                    continue
                hit = {
                    "timestamp": ts_raw,
                    "session_id": rec.get("sessionId", ""),
                    "message": msg[:200],
                    "log_file": str(path.name),
                }
                for sig in _MCP_SILENT_DEATH_SIGNATURES:
                    if sig in msg:
                        hit["signature"] = sig
                        silent_deaths.append(hit)
                        break
                else:
                    for sig in _MCP_TOOL_FAILURE_SIGNATURES:
                        if sig in msg:
                            hit["signature"] = sig
                            tool_failures.append(hit)
                            break
    except OSError:
        pass
    return silent_deaths, tool_failures


def _run_check_mcp_logs(*, json_out: bool, hours: int = 24) -> None:
    """Surface nx-mcp silent-death evidence from Claude Code's MCP cache.

    Per RDR-094 §Day 2 Operations §Diagnosing nx-mcp silent death
    (nexus-3f95 + nexus-50u5).

    Walks Claude Code's per-server log cache at
    ``~/Library/Caches/claude-cli-nodejs/<cwd-slug>/mcp-logs-*`` for
    files modified within the last *hours* window and greps for the
    silent-death signatures Claude Code emits when nx-mcp's stdio
    transport breaks before structlog can flush:

      * "STDIO connection dropped after Ns uptime"
      * "stdio transport error"

    Tool-failure events ("AbortError" client-side aborts) are surfaced
    as info entries; they may indicate user-cancelled tool calls
    rather than crashes.

    On non-macOS platforms (no ``~/Library/Caches/claude-cli-nodejs``)
    the check exits cleanly with "not present on this platform" --
    the cache is a Claude Code CLI implementation detail, not part
    of the MCP protocol.
    """
    import json as _json
    import time

    cache_dir = _resolve_claude_cache_dir()
    cutoff_epoch = time.time() - hours * 3600.0

    payload: dict[str, Any] = {
        "cache_dir": str(cache_dir),
        "hours_window": hours,
        "platform_supported": False,
        "silent_deaths": [],
        "tool_failures": [],
        "log_dirs_scanned": 0,
        "log_files_scanned": 0,
    }

    if not cache_dir.exists():
        if json_out:
            click.echo(_json.dumps(payload, indent=2))
        else:
            click.echo(
                f"MCP log surface not present at {cache_dir} "
                f"(macOS-only path; nothing to check on this platform)."
            )
        return

    payload["platform_supported"] = True

    log_dirs = sorted(cache_dir.glob("mcp-logs-*"))
    payload["log_dirs_scanned"] = len(log_dirs)

    for log_dir in log_dirs:
        if not log_dir.is_dir():
            continue
        for jsonl in log_dir.glob("*.jsonl"):
            try:
                if jsonl.stat().st_mtime < cutoff_epoch:
                    continue
            except OSError:
                continue
            payload["log_files_scanned"] += 1
            sd, tf = _scan_mcp_log_jsonl(jsonl, cutoff_epoch)
            for hit in sd:
                hit["server"] = log_dir.name
                payload["silent_deaths"].append(hit)
            for hit in tf:
                hit["server"] = log_dir.name
                payload["tool_failures"].append(hit)

    if json_out:
        click.echo(_json.dumps(payload, indent=2))
        return

    click.echo(
        f"Scanned {payload['log_files_scanned']} JSONL files across "
        f"{payload['log_dirs_scanned']} mcp-logs-* dirs under "
        f"{cache_dir} (last {hours}h)."
    )
    if not payload["silent_deaths"] and not payload["tool_failures"]:
        click.echo("No silent-death or tool-failure signatures found.")
        return

    if payload["silent_deaths"]:
        click.echo(
            f"\n[WARNING] Silent-death signatures: "
            f"{len(payload['silent_deaths'])}"
        )
        click.echo(
            "  Cross-reference these timestamps against "
            "~/.config/nexus/logs/mcp.log + ~/.config/nexus/logs/watchdog.log"
        )
        click.echo(
            "  to identify the gap. See RDR-094 §Day 2 Operations."
        )
        for hit in payload["silent_deaths"]:
            click.echo(
                f"  {hit['timestamp']}  {hit['signature']}  "
                f"server={hit['server']}  session={hit['session_id'][:8]}..."
            )

    if payload["tool_failures"]:
        click.echo(
            f"\n[INFO] Tool-failure signatures: "
            f"{len(payload['tool_failures'])} "
            f"(may be user-cancelled aborts, not crashes)"
        )
        for hit in payload["tool_failures"][:5]:
            click.echo(
                f"  {hit['timestamp']}  {hit['signature']}  "
                f"server={hit['server']}"
            )
        if len(payload["tool_failures"]) > 5:
            click.echo(
                f"  ... and {len(payload['tool_failures']) - 5} more "
                "(use --json for full list)"
            )


def _run_check_tmpdirs(*, reap: bool, json_out: bool) -> None:
    """List or reap orphan ``nx_t1_*`` tmpdirs (RDR-094 Phase 3).

    Read-only by default: enumerates candidates that
    :func:`nexus.session.sweep_orphan_tmpdirs` would reap (no session
    record reference, mtime > 24h). With ``--reap-tmpdirs``, calls
    the sweep function and reports the count actually deleted.

    Exits non-zero when reap reports zero candidates and ``--reap-
    tmpdirs`` was passed (so the operator can spot a no-op run in
    automation).
    """
    import json
    import tempfile
    import time
    from pathlib import Path

    from nexus.session import sweep_orphan_tmpdirs

    tmpdir_root = Path(tempfile.gettempdir())
    cutoff_hours = 24.0
    cutoff = time.time() - cutoff_hours * 3600.0

    candidates: list[dict] = []
    if tmpdir_root.exists():
        for d in sorted(tmpdir_root.glob("nx_t1_*")):
            if not d.is_dir():
                continue
            try:
                mtime = d.stat().st_mtime
            except OSError:
                continue
            age_h = (time.time() - mtime) / 3600.0
            if mtime >= cutoff:
                continue
            try:
                size_kb = sum(
                    p.stat().st_size for p in d.rglob("*") if p.is_file()
                ) / 1024.0
            except OSError:
                size_kb = 0.0
            candidates.append({
                "path": str(d),
                "age_hours": round(age_h, 2),
                "size_kb": round(size_kb, 1),
            })

    payload: dict = {
        "tmpdir_root": str(tmpdir_root),
        "cutoff_hours": cutoff_hours,
        "candidates": candidates,
        "reaped": 0,
    }

    if reap:
        payload["reaped"] = sweep_orphan_tmpdirs(
            tmpdir_root=tmpdir_root,
            max_age_hours=cutoff_hours,
        )

    if json_out:
        click.echo(json.dumps(payload, indent=2))
    else:
        if not candidates:
            click.echo(
                f"No orphan nx_t1_* tmpdirs older than {cutoff_hours}h "
                f"under {tmpdir_root}."
            )
        else:
            click.echo(
                f"Orphan nx_t1_* candidates under {tmpdir_root}: "
                f"{len(candidates)}"
            )
            for c in candidates:
                click.echo(
                    f"  {c['path']}  age={c['age_hours']}h  "
                    f"size={c['size_kb']}KB"
                )
        if reap:
            click.echo(f"Reaped: {payload['reaped']}")

    if reap and payload["reaped"] == 0 and candidates:
        # All candidates failed to delete; surface as non-zero.
        raise click.exceptions.Exit(1)


def _run_check_plan_library() -> None:
    """Report plan-library dimensional health. RDR-092 Phase 0c.2.

    Categories counted:

      * **authored**: rows whose ``dimensions`` column is populated
        AND whose ``tags`` do not include ``backfill`` (shipped YAML
        seeds or grown plans with full identity).
      * **backfilled**: rows whose ``tags`` contain ``backfill`` /
        ``backfill-low-conf`` (Phase 0d heuristic migration output).
      * **non-dimensional**: rows with ``dimensions IS NULL``
        (legacy / pre-RDR-078 seeds that need ``nx plan repair``).

    Exits non-zero when the global-tier builtin count
    (``project='' AND tags LIKE '%builtin-template%'``) falls below
    :data:`_MIN_GLOBAL_BUILTIN_COUNT`; that state signals the scoped
    loader never seeded (typically ``nx catalog setup`` was never
    re-run after the RDR-078 loader landed).
    """
    from nexus.commands._helpers import default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("T2 database not found; nothing to check.")
        click.echo("Fix: run 'nx catalog setup' to initialise the library.")
        raise click.exceptions.Exit(1)

    # nexus-pac1 (RDR-112 P4.5): replaces the prior
    # ``reject_under_daemon_mode`` + direct sqlite3.connect path with
    # the daemon-aware ``_T2Inspector`` adapter.
    with _T2Inspector(db_path) as t2:
        def _count(where: str) -> int:
            rows = t2.execute(
                f"SELECT COUNT(*) FROM plans WHERE {where}"
            )
            row = rows[0] if rows else (0,)
            return int(row[0] or 0)

        total = _count("1=1")
        non_dimensional = _count("dimensions IS NULL")
        backfilled = _count(
            "dimensions IS NOT NULL AND "
            "(tags LIKE '%backfill%' OR tags LIKE '%backfill-low-conf%')"
        )
        authored = _count(
            "dimensions IS NOT NULL AND "
            "NOT (tags LIKE '%backfill%' OR tags LIKE '%backfill-low-conf%')"
        )
        global_builtin = _count(
            "project = '' AND tags LIKE '%builtin-template%'"
        )

    click.echo("Plan library check:")
    click.echo(f"  total rows:         {total}")
    click.echo(f"  authored:           {authored}")
    click.echo(f"  backfilled:         {backfilled}")
    click.echo(f"  non-dimensional:    {non_dimensional}")
    click.echo(f"  global-tier builtin count: {global_builtin}")
    click.echo("")

    failed = False
    if global_builtin < _MIN_GLOBAL_BUILTIN_COUNT:
        click.echo(
            f"  FAIL: global-tier builtin count {global_builtin} "
            f"< expected {_MIN_GLOBAL_BUILTIN_COUNT}",
            err=True,
        )
        click.echo("    Fix: run 'nx catalog setup'.", err=True)
        failed = True
    if non_dimensional:
        click.echo(
            f"  WARN: {non_dimensional} non-dimensional row(s) "
            "(legacy / pre-RDR-078 seeds).",
            err=True,
        )
        click.echo(
            "    Fix: run 'nx plan repair' to backfill dimensions "
            "heuristically.",
            err=True,
        )

    if not failed:
        click.echo("All checks passed.")
    else:
        raise click.exceptions.Exit(1)


def _run_trim_telemetry(days: int) -> None:
    """Delete search_telemetry rows older than *days* (RDR-087 Phase 2.4)."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2.telemetry import Telemetry

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("T2 database not found — nothing to trim.")
        return
    telemetry = Telemetry(db_path)
    try:
        deleted = telemetry.trim_search_telemetry(days=days)
    finally:
        telemetry.close()
    noun = "row" if deleted == 1 else "rows"
    click.echo(
        f"Trimmed {deleted} search_telemetry {noun} older than {days} days."
    )


# ── --check-aspect-queue (nexus-1pfq) ────────────────────────────────────────


def _run_check_aspect_queue() -> None:
    """Report aspect_extraction_queue depth + per-status breakdown.

    RDR-089 follow-up nexus-qeo8 introduced an async worker
    (``aspect_worker.py``) that drains this table on a daemon thread.
    Without observability, a backlog grows silently. This check
    surfaces (a) total rows, (b) per-status counts, (c) oldest
    enqueued_at as a lag indicator, (d) failed rows with their last
    error so a stuck worker is visible.
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("aspect_extraction_queue: T2 database not found.")
        return

    # nexus-pac1 (RDR-112 P4.5): daemon-aware introspection.
    with _T2Inspector(db_path) as t2:
        # Confirm the table exists (pre-RDR-089 dbs won't have it).
        if "aspect_extraction_queue" not in t2.tables():
            click.echo(
                "aspect_extraction_queue: table not present "
                "(no aspect-extraction work has been queued)."
            )
            return

        total_rows = t2.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue"
        )
        total = total_rows[0][0] if total_rows else 0
        click.echo(f"aspect_extraction_queue: {total} row(s) total")

        if total == 0:
            return

        # Per-status breakdown.
        click.echo("\nBy status:")
        rows = t2.execute(
            "SELECT status, COUNT(*) FROM aspect_extraction_queue "
            "GROUP BY status ORDER BY status"
        )
        for status, count in rows:
            click.echo(f"  {status:<12} {count:>6}")

        # Oldest enqueued_at across all non-completed rows — the lag
        # indicator. ``processing`` and ``pending`` both contribute
        # to the worker's open-work view; we report MIN across them.
        oldest_rows = t2.execute(
            "SELECT MIN(enqueued_at), source_path "
            "FROM aspect_extraction_queue "
            "WHERE status IN ('pending', 'processing')"
        )
        oldest = oldest_rows[0] if oldest_rows else None
        if oldest and oldest[0]:
            click.echo(
                f"\nOldest pending/processing: {oldest[0]} "
                f"({oldest[1] or '?'})"
            )

        # Surface failed rows with their last_error so the operator
        # sees stuck work without needing SQL.
        failed = t2.execute(
            "SELECT collection, source_path, retry_count, last_error "
            "FROM aspect_extraction_queue "
            "WHERE status = 'failed' "
            "ORDER BY enqueued_at DESC LIMIT 20"
        )
        if failed:
            click.echo(f"\nFailed rows (showing top {len(failed)}):")
            for collection, source_path, retry_count, last_error in failed:
                click.echo(
                    f"  [{collection}] {source_path}  "
                    f"(retries={retry_count})"
                )
                if last_error:
                    # Truncate long errors but always show enough to
                    # diagnose the failure class.
                    err = (last_error or "").replace("\n", " ").strip()
                    click.echo(f"    last_error: {err[:200]}")


# ── --check-tier-discipline (nexus-a52i) ─────────────────────────────────────


def _run_check_tier_discipline() -> None:
    """Audit tier-write activity for the current session.

    Reads ``tier_writes`` from T2 and prints the same summary as
    ``nx tier-status`` for the current session, plus a structured
    warning when a session has zero tier writes (a soft signal that
    the session may have produced findings without persisting them).

    Heuristic only — does NOT exit non-zero. Visibility, not
    enforcement.
    """
    import os as _os
    from pathlib import Path as _Path

    from nexus.commands._helpers import default_db_path as _default_db_path
    from nexus.session import read_claude_session_id as _read_claude_session_id

    session_id = (
        _os.environ.get("NX_SESSION_ID", "").strip()
        or _read_claude_session_id()
    )
    if not session_id:
        click.echo("Tier-discipline check:")
        click.echo("  No current session resolvable (skip).")
        return

    db_path = _default_db_path()
    if not _Path(db_path).exists():
        click.echo("Tier-discipline check:")
        click.echo(f"  T2 database not found at {db_path} (skip).")
        return

    # nexus-pac1 (RDR-112 P4.5): daemon-aware introspection.
    # The daemon's exec_raw does not accept parameter binding, so we
    # quote the session_id inline. session_id is read from
    # NX_SESSION_ID or read_claude_session_id; both produce
    # UUID-shaped strings that contain no single quotes in practice.
    # Quote-escape defensively in case a future session source
    # contains apostrophes.
    quoted_session = "'" + session_id.replace("'", "''") + "'"
    with _T2Inspector(db_path) as t2:
        if "tier_writes" not in t2.tables():
            click.echo("Tier-discipline check:")
            click.echo("  tier_writes table not yet initialised (no writes seen).")
            return
        rows = t2.execute(
            "SELECT tier, COUNT(*) FROM tier_writes "
            f"WHERE session_id = {quoted_session} GROUP BY tier"
        )

    by_tier = {tier: n for tier, n in rows}
    total = sum(by_tier.values())

    click.echo(f"Tier-discipline check (session {session_id}):")
    if total == 0:
        click.echo(
            "  WARNING: zero tier writes recorded for this session. "
            "Findings produced (if any) have not been persisted."
        )
        click.echo(
            "  Run with `nx tier-status --session " + session_id +
            "` for the structured view."
        )
        click.echo(
            "  Pass --json for downstream tooling. Use `nx memory put`, "
            "`nx scratch put`, or the MCP equivalents to write back."
        )
        return

    click.echo(f"  total writes: {total}")
    for tier in ("T1", "T2", "T3", "plan"):
        n = by_tier.get(tier, 0)
        if n:
            click.echo(f"    {tier:<6} {n}")
    if all(by_tier.get(t, 0) == 0 for t in ("T2", "T3")):
        click.echo(
            "  NOTE: writes are T1/plan only. No persistent (T2/T3) "
            "write-back yet — durable findings are not surfaced."
        )


# ── --check-post-store-hooks (nexus-b0ka) ────────────────────────────────────


def _run_check_post_store_hooks() -> None:
    """Enumerate post-store hooks registered on each of the three chains.

    Importing :mod:`nexus.mcp.core` triggers the static
    ``register_post_store_*_hook`` calls; this function then reads the
    chain lists from :mod:`nexus.mcp_infra` and prints the
    ``__name__`` attributes per chain.

    Use cases (from nexus-b0ka):

      * Confirm RDR-089 aspect_extraction_enqueue_hook registered
        after install.
      * Detect drift if a hook silently fails to register due to
        import-order bugs.
      * Smoke after upgrade: are the expected hooks still registered?
    """
    # Import for side-effects: registers the chash_dual_write +
    # taxonomy_assign batch hooks and the aspect-extraction document
    # hook (mcp/core.py:387-388 + the nexus-qeo8 follow-up).
    import nexus.mcp.core  # noqa: F401, PLC0415
    from nexus.mcp_infra import (  # noqa: PLC0415
        _post_document_hooks,
        _post_store_batch_hooks,
        _post_store_hooks,
    )

    chains: list[tuple[str, list]] = [
        ("Single-doc chain (RDR-070)", _post_store_hooks),
        ("Batch chain (RDR-095)", _post_store_batch_hooks),
        ("Document-grain chain (RDR-089)", _post_document_hooks),
    ]
    total = 0
    for label, hooks in chains:
        click.echo(f"\n{label}:")
        if not hooks:
            click.echo("  (none)")
            continue
        for hook in hooks:
            name = getattr(hook, "__name__", repr(hook))
            module = getattr(hook, "__module__", "?")
            click.echo(f"  - {name}  [{module}]")
            total += 1

    click.echo(f"\nTotal: {total} hook(s) registered across 3 chains.")


# ── --check-storage-boundary (RDR-112 §5; nexus-b7o1) ────────────────────


def _run_check_storage_boundary(*, fail_on_violation: bool = False) -> int:
    """AST-scan ``src/nexus/**/*.py`` for direct storage-substrate callers.

    RDR-112 §5: when ``NX_STORAGE_MODE=daemon``, the daemon owns
    ``memory.db``, ``tuples.db``, and the local chromadb directory as
    the single writer of each. Any module outside the storage substrate
    that opens a competing connection violates the boundary. Static
    lint instead of a runtime probe so violators surface in CI /
    pre-merge even when no daemon is running.

    Detected calls:
      - ``sqlite3.connect(...)`` (also matches ``import sqlite3 as _x``)
      - ``chromadb.PersistentClient(...)`` (CR-6 / nexus-nphw)

    Allowlist roots (CR-4 / nexus-e8ao):
      - ``src/nexus/daemon/`` (the storage process itself)
      - ``src/nexus/db/`` (daemon-internal domain stores)

    Per-line allowlist: any call whose source line — or any contiguous
    comment line immediately above it — contains
    ``# storage-boundary-allow:`` is treated as an intentional
    exception. The comment SHOULD include a short reason after the
    colon.

    Severity:
      - advisory (exit 0) when ``NX_STORAGE_MODE`` is unset / not
        ``daemon`` AND ``fail_on_violation`` is False.
      - hard fail (exit 2) when ``NX_STORAGE_MODE=daemon`` OR
        ``fail_on_violation`` is True. The flag (CR-5 / nexus-b43y) is
        wired into CI so the lint gates merges regardless of the local
        ``NX_STORAGE_MODE`` setting.

    Returns the chosen exit code so callers can propagate it.
    """
    import ast
    import os as _os
    from pathlib import Path as _Path

    pkg_root = _Path(__file__).resolve().parent.parent
    if pkg_root.name != "nexus":  # pragma: no cover — defensive
        click.echo(
            f"check-storage-boundary: unable to locate src/nexus "
            f"(got {pkg_root}); aborting.",
            err=True,
        )
        return 2

    # CR-4 (nexus-e8ao): allowlist matches RDR-112 §5 — daemon/ holds the
    # storage process and db/ holds the daemon-internal domain stores.
    allowed_roots = (pkg_root / "daemon", pkg_root / "db")

    def _is_sqlite_connect_call(node: ast.Call) -> bool:
        # Match `sqlite3.connect(...)` and `*.connect(...)` where the
        # attribute lookup is on a module imported as sqlite3.
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            value = func.value
            if isinstance(value, ast.Name) and value.id in {"sqlite3", "_sqlite3"}:
                return True
        return False

    def _is_chroma_persistent_client_call(node: ast.Call) -> bool:
        # CR-6 (nexus-nphw): match `chromadb.PersistentClient(...)`.
        # Match the import alias name explicitly (refuses lookalikes).
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "PersistentClient"
        ):
            value = func.value
            if isinstance(value, ast.Name) and value.id in {
                "chromadb",
                "_chromadb",
            }:
                return True
        return False

    def _is_under_any(path: _Path, roots: tuple[_Path, ...]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _has_allow_marker(source_lines: list[str], lineno: int) -> bool:
        # CR-6 (nexus-nphw): same-line OR contiguous comment preamble.
        marker = "# storage-boundary-allow:"
        if marker in source_lines[lineno - 1]:
            return True
        i = lineno - 2  # 0-indexed line above the call
        while i >= 0:
            stripped = source_lines[i].strip()
            if not stripped:
                i -= 1
                continue
            if stripped.startswith("#"):
                if marker in stripped:
                    return True
                i -= 1
                continue
            break
        return False

    violations: list[tuple[str, int, str, str]] = []
    for py_path in sorted(pkg_root.rglob("*.py")):
        if _is_under_any(py_path, allowed_roots):
            continue
        try:
            source = py_path.read_text()
            tree = ast.parse(source, filename=str(py_path))
        except (OSError, SyntaxError) as exc:
            click.echo(
                f"check-storage-boundary: failed to parse {py_path}: {exc}",
                err=True,
            )
            continue
        source_lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_sqlite_connect_call(node):
                kind = "sqlite3.connect"
            elif _is_chroma_persistent_client_call(node):
                kind = "chromadb.PersistentClient"
            else:
                continue
            if _has_allow_marker(source_lines, node.lineno):
                continue
            snippet = source_lines[node.lineno - 1].strip()
            rel = py_path.relative_to(pkg_root.parent.parent)
            violations.append((str(rel), node.lineno, snippet, kind))

    from nexus.db import is_daemon_mode as _is_daemon_mode
    daemon_mode = _is_daemon_mode()
    click.echo("Storage-boundary check (RDR-112 §5):")
    if not violations:
        click.echo(
            _check(
                "no direct sqlite3.connect / chromadb.PersistentClient "
                "outside daemon/ + db/",
                True,
            )
        )
        return 0

    label = (
        "direct storage-substrate calls outside src/nexus/daemon/ + "
        "src/nexus/db/ (without `# storage-boundary-allow:` marker)"
    )
    click.echo(_check(label, False, f"{len(violations)} violation(s)"))
    for path, line, snippet, kind in violations:
        click.echo(f"  {path}:{line} [{kind}]: {snippet}")

    if daemon_mode or fail_on_violation:
        click.echo("")
        reason = (
            "NX_STORAGE_MODE=daemon is set"
            if daemon_mode
            else "--fail-on-violation passed"
        )
        click.echo(
            f"{reason}; the violations above bypass the daemon's "
            "single-writer guarantee. Exiting 2."
        )
        return 2
    click.echo("")
    click.echo(
        "Advisory only (NX_STORAGE_MODE not 'daemon'). RDR-112 §5 calls "
        "for staged remediation; this list scopes the remaining work. "
        "Pass --fail-on-violation to make this a hard gate."
    )
    return 0


# ── --check-autostart (RDR-112; nexus-mf91) ──────────────────────────────


def _run_check_autostart(*, json_out: bool = False) -> None:
    """Report whether the T2 daemon autostart unit is installed.

    Complements the first-run TTY nudge in
    ``nexus.commands._autostart_prompt``: tells the operator the same
    thing the nudge says, on demand, with no TTY / marker gating. Use
    when the nudge has already been silenced or the operator wants the
    state without rerunning ``nx daemon t2 install --autostart``.
    """
    import json as _json

    from nexus.commands._autostart_prompt import autostart_status

    status = autostart_status()

    if json_out:
        click.echo(_json.dumps(status))
        return

    click.echo("Autostart status (RDR-112):")
    if not status["platform_supported"]:
        click.echo(
            _check(
                "platform supported",
                False,
                f"sys.platform={sys.platform!r} (only darwin and linux are supported)",
            )
        )
        return
    click.echo(_check("platform supported", True))
    click.echo(f"  unit path: {status['unit_path']}")
    click.echo(_check("autostart installed", bool(status["installed"])))
    click.echo(
        _check(
            "nudge marker written",
            bool(status["marker_present"]),
            "operator has been nudged once"
            if status["marker_present"]
            else "no nudge fired yet on this machine",
        )
    )
    if status["storage_mode"]:
        click.echo(f"  NX_STORAGE_MODE: {status['storage_mode']}")
    if not status["installed"]:
        click.echo("")
        click.echo(
            "Hint: `nx daemon t2 install --autostart` to install the "
            "launchd plist (macOS) or systemd user unit (Linux) so the "
            "daemon comes up at login."
        )


# ── --check-bridge (RDR-111 deep-review pass — bridge installability) ─────


def _run_check_bridge() -> None:
    """Diagnose ORB hook-bridge installability.

    Per the 2026-05-15 deep-review pass: a wheel-only install delivers no
    bridge silently — the seven ``orb_bridge_*.py`` scripts live under
    ``$CLAUDE_PLUGIN_ROOT`` and require both the Python wheel AND the nx
    Claude Code plugin. Without ``nx doctor`` checking, users get a
    silently-broken bridge.

    Checks:
      1. ``CLAUDE_PLUGIN_ROOT`` is set (otherwise bridge scripts can't be located).
      2. All seven ``orb_bridge_*.py`` exist under it.
      3. ``~/.config/nexus/tuples.db`` exists and is readable.
      4. The seven hook-event subspace YAMLs resolve via the registry.
      5. At least one tuple has been written in the last 24h (sanity that
         the bridge has actually fired).
    """
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    expected_scripts = [
        "orb_bridge_pretooluse.py",
        "orb_bridge_posttooluse.py",
        "orb_bridge_stop.py",
        "orb_bridge_subagent_stop.py",
        "orb_bridge_user_prompt_submit.py",
        "orb_bridge_session.py",
        "orb_bridge_notification.py",
    ]

    click.echo("ORB hook-bridge diagnostics (RDR-111):")
    click.echo("")

    # 1. CLAUDE_PLUGIN_ROOT
    if plugin_root_env:
        click.echo(_check("CLAUDE_PLUGIN_ROOT", True, plugin_root_env))
        plugin_root = Path(plugin_root_env)
    else:
        click.echo(
            _check(
                "CLAUDE_PLUGIN_ROOT",
                False,
                "unset — bridge scripts cannot be located; install the nx Claude Code plugin",
            )
        )
        plugin_root = None

    # 2. Bridge scripts
    if plugin_root is not None:
        scripts_dir = plugin_root / "hooks" / "scripts"
        missing = [s for s in expected_scripts if not (scripts_dir / s).is_file()]
        if missing:
            click.echo(
                _check(
                    "bridge scripts",
                    False,
                    f"missing under {scripts_dir}: {missing}",
                )
            )
        else:
            click.echo(
                _check(
                    "bridge scripts",
                    True,
                    f"7/7 present under {scripts_dir}",
                )
            )

    # 3. tuples.db
    # nexus-bkvg (FS-4): honour NEXUS_CONFIG_DIR via nexus_config_dir
    # so sandbox runs / multi-profile installs probe the right file.
    from nexus.config import nexus_config_dir as _nexus_config_dir
    tuples_db = _nexus_config_dir() / "tuples.db"
    if tuples_db.exists():
        size = tuples_db.stat().st_size
        click.echo(
            _check("tuples.db", True, f"{tuples_db} ({size} bytes)")
        )
    else:
        click.echo(
            _check(
                "tuples.db",
                False,
                f"{tuples_db} not present — bridge has not run yet",
            )
        )

    # 4. Registry resolves hook subspaces
    try:
        from nexus.tuplespace.registry import Registry, default_builtin_dir
        reg = Registry.load(default_builtin_dir(), subdirs=("hooks",))
        hook_subspaces = [
            s for s in reg._by_template.keys() if s.startswith("hook_events/")
        ]
        if len(hook_subspaces) == 7:
            click.echo(
                _check(
                    "hook-event subspaces",
                    True,
                    f"7/7 resolve via {default_builtin_dir()}",
                )
            )
        else:
            click.echo(
                _check(
                    "hook-event subspaces",
                    False,
                    f"only {len(hook_subspaces)}/7 resolve",
                )
            )
    except Exception as exc:  # noqa: BLE001
        click.echo(_check("hook-event subspaces", False, f"registry load failed: {exc}"))

    # 5. Plugin/wheel version skew (nexus-y1xc)
    #
    # The bridge has a BRIDGE_API_VERSION protocol gate, but plugin/wheel
    # versions can still drift on otherwise-compatible protocols (e.g. a
    # newer wheel ships docs/CLI changes the plugin scripts depend on).
    # Soft-warn on mismatch; do not fail loud.
    plugin_version: str | None = None
    if plugin_root is not None:
        manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
        if manifest_path.is_file():
            try:
                import json as _json
                plugin_version = _json.loads(manifest_path.read_text()).get("version")
            except Exception:  # noqa: BLE001
                plugin_version = None
    wheel_version: str | None
    try:
        from importlib.metadata import version as _pkg_version
        wheel_version = _pkg_version("conexus")
    except Exception:  # noqa: BLE001
        wheel_version = None
    if plugin_version and wheel_version:
        if plugin_version == wheel_version:
            click.echo(
                _check(
                    "plugin/wheel version",
                    True,
                    f"nx={plugin_version} conexus={wheel_version}",
                )
            )
        else:
            click.echo(
                _check(
                    "plugin/wheel version",
                    False,
                    f"skew: nx plugin={plugin_version} != conexus wheel={wheel_version} — "
                    f"reinstall the matching pair (uv tool install conexus and update the "
                    f"nx plugin) or expect drift bugs beyond the BRIDGE_API_VERSION gate",
                )
            )
    elif plugin_version is None and plugin_root is not None:
        click.echo(
            _check(
                "plugin/wheel version",
                False,
                "nx plugin manifest not found under CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json",
            )
        )

    # nexus-2wvl: detect autostart-binary drift.  ``_resolve_nx_bin`` is
    # captured at install time; later ``pip install --upgrade conexus``
    # can move the nx executable, leaving the autostart entry pointing
    # at a vanished path.  Silent only until launchd / systemd tries to
    # spawn the daemon, so surface it loudly in doctor.
    try:
        from nexus.commands.daemon import _read_installed_autostart_nx_bin
        autostart_nx_bin = _read_installed_autostart_nx_bin()
    except Exception:  # noqa: BLE001 -- defensive
        autostart_nx_bin = None
    if autostart_nx_bin is not None:
        if Path(autostart_nx_bin).exists():
            click.echo(
                _check(
                    "autostart binary",
                    True,
                    f"{autostart_nx_bin} exists",
                )
            )
        else:
            click.echo(
                _check(
                    "autostart binary",
                    False,
                    f"stale path: {autostart_nx_bin} does not exist "
                    f"(likely after `pip install --upgrade conexus` or a "
                    f"`uv tool` relocation); re-run "
                    f"`nx daemon t2 install --autostart --force` to "
                    f"refresh the entry",
                )
            )
    else:
        # nexus-mlmu.6 (DR-6, 2026-05-17): emit an explicit
        # 'not installed' signal so operators can distinguish a
        # healthy autostart from a never-installed one.
        click.echo(
            _check(
                "autostart binary",
                False,
                "not installed (run `nx daemon t2 install --autostart` "
                "to enable launch-at-login)",
            )
        )

    # 6. Recent tuple sanity (nexus-1xip: refuse the direct tuples.db open
    # under daemon mode — the daemon owns the WAL writer and a parallel
    # connection from this process is a race risk. Surface the skip with
    # a hint so the operator knows where the live signal lives.)
    if tuples_db.exists():
        from nexus.db import DaemonModeDiagnosticError, reject_under_daemon_mode
        try:
            reject_under_daemon_mode("nx doctor --check-bridge (tuples.db readback)")
        except DaemonModeDiagnosticError as exc:
            click.echo(
                _check(
                    "recent hook events",
                    True,
                    f"skipped under NX_STORAGE_MODE=daemon — use `nx daemon t2 peek tuples` "
                    f"or watch event_stream for live signal ({exc.__class__.__name__})",
                )
            )
            return
        import sqlite3 as _sqlite3
        try:
            # storage-boundary-allow: read-only hook-events probe;
            # daemon-mode is short-circuited via the except branch above.
            conn = _sqlite3.connect(f"file:{tuples_db}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT COUNT(*) FROM tuples "
                "WHERE subspace LIKE 'hook_events/%' "
                "AND created_at > unixepoch() - 86400"
            ).fetchone()
            conn.close()
            recent = row[0] if row else 0
            if recent > 0:
                click.echo(
                    _check(
                        "recent hook events",
                        True,
                        f"{recent} tuple(s) in the last 24h",
                    )
                )
            else:
                click.echo(
                    _check(
                        "recent hook events",
                        False,
                        "0 tuples in last 24h — bridge may not be firing (set CLAUDECODE, unset NX_BRIDGE_DISABLE)",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            click.echo(_check("recent hook events", False, f"query failed: {exc}"))

    # 7. Bridge fail-closed operator-override surfacing (RDR-114 Step 3, nexus-6bad)
    #
    # Report whether the operator has opted into the legacy fail-open
    # path via NX_BRIDGE_ALLOW_DIRECT_FALLBACK, warn on the
    # conflicting-env combination, and surface any recent
    # hook_bridge_emit_drop_rpc_failed events from the daemon's
    # rotated log (RotatingFileHandler at ~/.config/nexus/logs/daemon.log
    # via nexus.logging_setup.configure_logging("daemon")).
    bridge_disable = os.environ.get("NX_BRIDGE_DISABLE", "").strip()
    direct_fallback_env = os.environ.get("NX_BRIDGE_ALLOW_DIRECT_FALLBACK", "").strip()
    _falsy = ("", "0", "false", "False")
    direct_fallback_set = direct_fallback_env not in _falsy
    bridge_disable_set = bridge_disable not in _falsy
    if direct_fallback_set:
        # Operator override visible.
        click.echo(
            _check(
                "bridge fail-closed policy",
                False,
                f"OPERATOR OVERRIDE: NX_BRIDGE_ALLOW_DIRECT_FALLBACK={direct_fallback_env!r} — "
                "legacy fail-open direct-mode fallback enabled; "
                "WAL-contention race with daemon writer is accepted",
            )
        )
        if bridge_disable_set:
            click.echo(
                _check(
                    "bridge env conflict",
                    False,
                    "WARNING: NX_BRIDGE_DISABLE and NX_BRIDGE_ALLOW_DIRECT_FALLBACK are both set; "
                    "NX_BRIDGE_DISABLE exits first in emit() and NX_BRIDGE_ALLOW_DIRECT_FALLBACK "
                    "has no effect (conflict; unset NX_BRIDGE_DISABLE if the fallback should fire)",
                )
            )
    else:
        click.echo(
            _check(
                "bridge fail-closed policy",
                True,
                "default fail-closed under daemon routing (RDR-114); "
                "drops surface as hook_bridge_emit_drop_rpc_failed",
            )
        )

    # Read recent drop events from the daemon's rotated log. Best-effort:
    # if the log is missing, unreadable, or the daemon mode never ran,
    # report a clean state.
    # nexus-bkvg (FS-4): honour NEXUS_CONFIG_DIR via nexus_config_dir.
    from nexus.config import nexus_config_dir as _nexus_config_dir
    daemon_log = _nexus_config_dir() / "logs" / "daemon.log"
    recent_drops = 0
    if daemon_log.is_file():
        try:
            import datetime as _dt
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)
            cutoff_iso = cutoff.isoformat()
            # Each drop event line includes "timestamp=<isoformat>" because
            # nexus.logging_setup wires the structlog TimeStamper with
            # utc=True; pair the event-name match with a string
            # comparison against the cutoff ISO so the parse is robust
            # to small RotatingFileHandler formatting variations.
            for line in daemon_log.read_text(errors="replace").splitlines():
                if "hook_bridge_emit_drop_rpc_failed" not in line:
                    continue
                idx = line.find("timestamp=")
                if idx == -1:
                    # No timestamp field: count as recent (defensive).
                    recent_drops += 1
                    continue
                ts_str = line[idx + len("timestamp="):].split()[0]
                if ts_str >= cutoff_iso:
                    recent_drops += 1
        except Exception as exc:  # noqa: BLE001
            click.echo(
                _check(
                    "recent bridge drops",
                    False,
                    f"failed to scan {daemon_log}: {exc}",
                )
            )
            return
    if recent_drops > 0:
        click.echo(
            _check(
                "recent bridge drops",
                False,
                f"{recent_drops} hook_bridge_emit_drop_rpc_failed event(s) in last 24h; "
                f"daemon may have been unavailable (check `nx daemon t2 info`)",
            )
        )
    else:
        click.echo(
            _check(
                "recent bridge drops",
                True,
                f"no recent drops in {daemon_log}",
            )
        )


# ── --check-mineru (nexus-2fyb code-review R3-3) ────────────────────────────


def _run_check_mineru() -> None:
    """Verify MinerU is importable and the formula-aware extractor entry
    point is reachable.

    nexus-2fyb promoted ``mineru[all]`` from an optional extra to a default
    dependency. Before this check, a corrupt install (missing wheel,
    broken import chain) was silent until ``nx index pdf`` ran on a
    formula-bearing PDF. Surfacing it at doctor-time gives the user an
    actionable error before they try to use the feature.
    """
    try:
        from mineru.cli.common import do_parse  # noqa: PLC0415
    except Exception as exc:
        click.echo(_check("MinerU import", False, f"{type(exc).__name__}: {exc}"))
        click.echo(
            "  ↳ MinerU is required since nexus-2fyb. Reinstall with "
            "`uv tool install --reinstall conexus`."
        )
        return

    if do_parse is None:
        click.echo(_check("MinerU import", False, "do_parse is None"))
        click.echo(
            "  ↳ mineru.cli.common imported but do_parse is None — "
            "the import shim is broken. Reinstall conexus."
        )
        return

    click.echo(_check("MinerU import", True, "mineru.cli.common.do_parse OK"))

    # Optional: surface server-side state. The mineru-api server is opt-in;
    # not running is fine. Just report status.
    try:
        from nexus.config import get_mineru_server_url  # noqa: PLC0415
        url = get_mineru_server_url()
    except Exception:
        url = None
    if url:
        try:
            import httpx  # noqa: PLC0415
            with httpx.Client(timeout=2.0) as client:
                r = client.get(f"{url}/health")
            if r.status_code == 200:
                click.echo(_check("MinerU server", True, f"reachable at {url}"))
            else:
                click.echo(_check(
                    "MinerU server", False,
                    f"{url} returned HTTP {r.status_code}",
                ))
        except Exception as exc:
            click.echo(_check(
                "MinerU server", False,
                f"{url} unreachable: {type(exc).__name__}",
            ))
    else:
        click.echo("  (no mineru-api server configured; subprocess mode in use)")


@click.command("doctor")
@click.option(
    "--clean-checkpoints",
    is_flag=True,
    default=False,
    help="Delete orphaned PDF checkpoint files (where the source PDF no longer exists).",
)
@click.option(
    "--clean-pipelines",
    is_flag=True,
    default=False,
    help="Delete orphaned PDF pipeline buffer entries (stale or missing source PDF).",
)
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help="Apply HNSW ef tuning to all local collections (local mode only).",
)
@click.option(
    "--fix-paths",
    is_flag=True,
    default=False,
    help="Migrate absolute file_path entries to relative paths (catalog + T3).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report affected entries without writing changes (use with --fix-paths).",
)
@click.option(
    "--check-schema",
    is_flag=True,
    default=False,
    help="Validate T2 database schema and report pending migrations.",
)
@click.option(
    "--check-search",
    "check_search",
    is_flag=True,
    default=False,
    help="Run probe 3a — the name-resolution canary from "
         "tests/fixtures/name_canaries.py. Exits 2 when any surface "
         "raises an unexpected exception. RDR-087 Phase 3.2.",
)
@click.option(
    "--check-resources",
    "check_resources",
    is_flag=True,
    default=False,
    help="Probe POSIX semaphore headroom and report orphan "
         "multiprocessing-tracker pressure. Exits 2 with 'Errno 28' "
         "when the namespace is exhausted (known sources: MinerU "
         "workers / orphan chroma children leaking via multiprocessing "
         "/ trackers re-parented to init after ungraceful MCP "
         "shutdowns). Beads nexus-dc57 + nexus-ze2a + nexus-9h1s.",
)
@click.option(
    "--check-quotas",
    "check_quotas",
    is_flag=True,
    default=False,
    help="Report ChromaDB Cloud free-tier quotas, Voyage AI model "
         "caps, and any transient-error retries observed this process. "
         "Exits 1 when the cloud tenant is unreachable in cloud mode "
         "(nexus-c590).",
)
@click.option(
    "--check-taxonomy",
    "check_taxonomy",
    is_flag=True,
    default=False,
    help="Verify the topic_links ≡ projection-assignment invariant "
         "(GH #252). Exits 1 on drift.",
)
@click.option(
    "--check-plan-library",
    "check_plan_library",
    is_flag=True,
    default=False,
    help="Report plan-library dimensional health: authored vs "
         "backfilled vs non-dimensional row counts, plus global-tier "
         "builtin count. Exits 1 when builtin count < 9. RDR-092 "
         "Phase 0c.2.",
)
@click.option(
    "--check-tmpdirs",
    "check_tmpdirs",
    is_flag=True,
    default=False,
    help="List orphan nx_t1_* tmpdirs that no session record points "
         "at AND are older than 24h. Reap candidates from RDR-094 "
         "Phase 3 sweep_orphan_tmpdirs. Read-only; pair with "
         "--reap-tmpdirs to actually delete them.",
)
@click.option(
    "--check-mcp-logs",
    "check_mcp_logs",
    is_flag=True,
    default=False,
    help="Scan Claude Code's per-server MCP cache for nx-mcp "
         "silent-death signatures ('STDIO connection dropped', "
         "'stdio transport error'). macOS only; skips cleanly on "
         "Linux/Windows. RDR-094 Phase H (nexus-50u5).",
)
@click.option(
    "--check-tier-discipline",
    "check_tier_discipline",
    is_flag=True,
    default=False,
    help="Audit tier-write activity for the current session: prints "
         "the tier-write summary from the tier_writes table and "
         "warns when a substantive session has no write-back. "
         "Phase 1B nexus-a52i.",
)
@click.option(
    "--mcp-log-hours",
    "mcp_log_hours",
    default=24,
    type=click.IntRange(min=1),
    show_default=True,
    help="Lookback window in hours for --check-mcp-logs.",
)
@click.option(
    "--reap-tmpdirs",
    "reap_tmpdirs",
    is_flag=True,
    default=False,
    help="With --check-tmpdirs, run sweep_orphan_tmpdirs and report "
         "the count reaped. Without --check-tmpdirs this flag is "
         "ignored.",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit machine-parseable JSON (used with --check-search, --check-quotas).",
)
@click.option(
    "--trim-telemetry",
    "trim_telemetry",
    is_flag=True,
    default=False,
    help="Delete search_telemetry rows older than --days (default 30) to "
         "cap T2 disk use. RDR-087 Phase 2.4.",
)
@click.option(
    "--check-post-store-hooks",
    "check_post_store_hooks",
    is_flag=True,
    default=False,
    help="Enumerate post-store hooks registered on each of the three "
         "chains (single-doc / batch / document-grain). nexus-b0ka.",
)
@click.option(
    "--check-mineru",
    "check_mineru",
    is_flag=True,
    default=False,
    help="Verify MinerU is importable. nexus-2fyb promoted mineru[all] "
         "from optional extra to default dep; this surfaces a corrupt "
         "install at doctor-time instead of waiting for the first "
         "math-PDF index to fail.",
)
@click.option(
    "--check-aspect-queue",
    "check_aspect_queue",
    is_flag=True,
    default=False,
    help="Report aspect_extraction_queue depth, per-status counts, "
         "oldest pending row, and any failed rows with their last "
         "error. nexus-1pfq.",
)
@click.option(
    "--check-t1",
    "check_t1",
    is_flag=True,
    default=False,
    help="Diagnose T1 addr-file presence + reachability. Detects "
         "the 'no t1_addr.<claude_pid> when Claude Code is parent' "
         "case and the exec -a / wrapper-rename residual. RDR-105 "
         "P5 / nexus-ssdg. Exits 1 when Claude is in the chain "
         "but the addr file is missing or unreachable.",
)
@click.option(
    "--check-bridge",
    "check_bridge",
    is_flag=True,
    default=False,
    help="Diagnose ORB hook-bridge installability (RDR-111): "
         "CLAUDE_PLUGIN_ROOT, bridge scripts present, tuples.db, "
         "registry resolves the 7 hook-event subspaces, and at "
         "least one tuple landed in the last 24h.",
)
@click.option(
    "--check-storage-boundary",
    "check_storage_boundary",
    is_flag=True,
    default=False,
    help="AST-lint src/nexus for direct sqlite3.connect / "
         "chromadb.PersistentClient calls outside src/nexus/daemon/ "
         "and src/nexus/db/ (RDR-112 §5). Advisory unless "
         "NX_STORAGE_MODE=daemon or --fail-on-violation is set; "
         "either elevates the result to exit 2. nexus-b7o1.",
)
@click.option(
    "--fail-on-violation",
    "fail_on_violation",
    is_flag=True,
    default=False,
    help="Make --check-storage-boundary exit 2 on any violation, "
         "regardless of NX_STORAGE_MODE. Intended for CI gating "
         "(RDR-112 §5, nexus-b43y).",
)
@click.option(
    "--check-autostart",
    "check_autostart",
    is_flag=True,
    default=False,
    help="Report T2 daemon autostart unit status (RDR-112 nexus-mf91): "
         "platform support, unit path, whether the launchd plist or "
         "systemd user unit is installed, and whether the once-per-"
         "machine nudge marker has fired.",
)
@click.option(
    "--days",
    "days",
    default=30,
    type=click.IntRange(min=1),
    show_default=True,
    help="Retention window for --trim-telemetry (days; minimum 1).",
)
def doctor_cmd(clean_checkpoints: bool, clean_pipelines: bool, fix: bool,
               fix_paths: bool, dry_run: bool, check_schema: bool,
               check_search: bool, check_resources: bool,
               check_quotas: bool, check_taxonomy: bool,
               check_plan_library: bool,
               check_tmpdirs: bool, reap_tmpdirs: bool,
               check_mcp_logs: bool, mcp_log_hours: int,
               check_mineru: bool,
               json_out: bool,
               trim_telemetry: bool, days: int,
               check_post_store_hooks: bool,
               check_aspect_queue: bool,
               check_t1: bool,
               check_bridge: bool,
               check_storage_boundary: bool,
               fail_on_violation: bool,
               check_autostart: bool,
               check_tier_discipline: bool) -> None:
    """Verify that all required services and credentials are available."""
    if check_schema:
        _run_check_schema()
        return

    if check_search:
        from nexus.doctor_search import run_check_search
        run_check_search(json_out=json_out)
        return

    if check_resources:
        _run_check_resources()
        return

    if check_quotas:
        _run_check_quotas(json_out=json_out)
        return

    if check_tmpdirs:
        _run_check_tmpdirs(reap=reap_tmpdirs, json_out=json_out)
        return

    if check_mcp_logs:
        _run_check_mcp_logs(json_out=json_out, hours=mcp_log_hours)
        return

    if check_taxonomy:
        _run_check_taxonomy()
        return

    if check_plan_library:
        _run_check_plan_library()
        return

    if trim_telemetry:
        _run_trim_telemetry(days=days)
        return

    if check_post_store_hooks:
        _run_check_post_store_hooks()
        return

    if check_mineru:
        _run_check_mineru()
        return

    if check_aspect_queue:
        _run_check_aspect_queue()
        return

    if check_t1:
        _run_check_t1()
        return

    if check_bridge:
        _run_check_bridge()
        return

    if check_storage_boundary:
        exit_code = _run_check_storage_boundary(
            fail_on_violation=fail_on_violation
        )
        if exit_code != 0:
            raise SystemExit(exit_code)
        return

    if check_autostart:
        _run_check_autostart(json_out=json_out)
        return

    if check_tier_discipline:
        _run_check_tier_discipline()
        return

    if fix:
        from nexus.config import is_local_mode, _default_local_path
        from nexus.db.t3 import T3Database, apply_hnsw_ef
        if not is_local_mode():
            click.echo("SPANN defaults adequate — no HNSW tuning needed (cloud mode)")
            return
        local_path = _default_local_path()
        db = T3Database(local_mode=True, local_path=str(local_path))
        count = apply_hnsw_ef(db)
        click.echo(f"Updated HNSW search_ef on {count} collection(s).")
        return

    if clean_checkpoints:
        from nexus.checkpoint import scan_orphaned_checkpoints
        deleted = scan_orphaned_checkpoints(delete=True)
        if deleted:
            click.echo(f"Deleted {len(deleted)} orphaned checkpoint(s).")
        else:
            click.echo("No orphaned checkpoints found.")
        return

    if clean_pipelines:
        from nexus.pipeline_buffer import PIPELINE_DB_PATH, PipelineDB
        if not PIPELINE_DB_PATH.exists():
            click.echo("No pipeline database found.")
            return
        db = PipelineDB(PIPELINE_DB_PATH)
        deleted = db.scan_orphaned_pipelines(delete=True)
        if deleted:
            click.echo(f"Deleted {len(deleted)} orphaned pipeline entry/entries.")
        else:
            click.echo("No orphaned pipeline entries found.")
        return

    if fix_paths:
        from nexus.catalog import Catalog
        from nexus.catalog.catalog import make_relative
        from nexus.catalog.tumbler import Tumbler, read_owners
        from nexus.config import catalog_path
        from nexus.mcp_infra import get_t3

        cat_p = catalog_path()
        if not Catalog.is_initialized(cat_p):
            click.echo("Catalog not initialized — run: nx catalog setup")
            return

        cat = Catalog(cat_p, cat_p / ".catalog.db")

        # Find all entries with absolute file_path
        rows = cat._db.execute(
            "SELECT tumbler, file_path, physical_collection FROM documents WHERE file_path LIKE '/%'"
        ).fetchall()

        if not rows:
            click.echo("No absolute file_path entries found.")
            return

        click.echo(f"Found {len(rows)} entries with absolute paths.")

        # Load owners for repo_root lookup
        owners_path = cat._owners_path
        owners = read_owners(owners_path) if owners_path.exists() else {}

        # Get registry for fallback
        from nexus.config import nexus_config_dir

        registry_path = nexus_config_dir() / "repos.json"
        registry = RepoRegistry(registry_path) if registry_path.exists() else None

        t3_db = None
        if not dry_run:
            # nexus-pac1 (RDR-112 P4.5): daemon-aware T3 factory.
            try:
                t3_db = get_t3()
            except RuntimeError as exc:
                raise click.ClickException(str(exc)) from exc

        fixed = 0
        chunks_updated = 0
        for tumbler_str, file_path, physical_collection in rows:
            tumbler = Tumbler.parse(tumbler_str)
            owner_prefix = str(tumbler.owner_address())
            owner_rec = owners.get(owner_prefix)

            if not owner_rec:
                continue
            if owner_rec.owner_type == "curator":
                continue

            # Determine repo_root
            repo_root = None
            if owner_rec.repo_root:
                repo_root = Path(owner_rec.repo_root)
            elif owner_rec.repo_hash and registry:
                for rp in registry.all_info():
                    h = hashlib.sha256(rp.encode()).hexdigest()[:8]
                    if h == owner_rec.repo_hash:
                        repo_root = Path(rp)
                        break

            if repo_root is None:
                _log.warning("fix_paths_no_root", tumbler=tumbler_str, file_path=file_path)
                continue

            new_rel = make_relative(file_path, repo_root)
            if new_rel == file_path:
                # Not under repo_root — skip
                _log.warning("fix_paths_not_under_root", tumbler=tumbler_str,
                             file_path=file_path, repo_root=str(repo_root))
                continue

            if dry_run:
                click.echo(f"  [dry-run] {tumbler_str}: {file_path} -> {new_rel}")
            else:
                # Update T3 source_path
                n = 0
                if physical_collection:
                    n = t3_db.update_source_path(physical_collection, file_path, new_rel)
                chunks_updated += n
                # Update catalog entry
                cat.update(tumbler, file_path=new_rel)
                click.echo(f"  fixed: {tumbler_str}: {file_path} -> {new_rel} ({n} chunks)")

            fixed += 1

        if dry_run:
            click.echo(f"\n{fixed} entries would be fixed. Use --fix-paths without --dry-run to apply.")
        else:
            click.echo(f"\nFixed {fixed} entries ({chunks_updated} T3 chunks updated).")
        return

    # ── Health check path — delegates to nexus.health ─────────────────────────
    from nexus.health import run_health_checks, format_health_for_cli

    results, is_local = run_health_checks()
    output, failed = format_health_for_cli(results, local_mode=is_local)
    click.echo(output)

    if failed:
        raise click.exceptions.Exit(1)


def _probe_semaphore_namespace() -> tuple[bool, str]:
    """Probe POSIX named-semaphore availability.

    Attempts to allocate and immediately unlink one throwaway named
    semaphore. Returns ``(True, info_msg)`` when the kernel namespace
    has headroom; ``(False, error_repr)`` when allocation fails —
    typically ``[Errno 28] No such space left on device`` under
    exhaustion (beads nexus-dc57 + nexus-ze2a).

    Separated from the CLI handler so tests can monkeypatch it.
    """
    import os as _os
    try:
        from _multiprocessing import SemLock  # type: ignore[attr-defined]
    except ImportError:
        return True, "SemLock probe unavailable on this platform"
    probe_name = f"/nx-doctor-probe-{_os.getpid()}"
    try:
        lock = SemLock(0, 0, 1, name=probe_name, unlink=True)
        # SemLock ctor created and owns the semaphore; unlink happens
        # via the ``unlink=True`` flag on close.
        del lock
        return True, "POSIX named-semaphore namespace has headroom"
    except OSError as exc:
        return False, f"{exc!r}"


def _count_orphan_trackers() -> int | None:
    """Return the number of PPID=1 multiprocessing tracker orphans
    visible to this user, or ``None`` if the count cannot be
    obtained. Pure read; no side effects.

    Bead nexus-9h1s. Each orphan tracker holds POSIX semaphores
    until killed; the namespace is bounded
    (``kern.posix.sem.max=10000`` on macOS). A high count predicts
    imminent SemLock failure even when the live probe still passes.
    """
    try:
        import subprocess

        from nexus.session import _parse_orphan_tracker_candidates

        ps_output = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,etime,command"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return len(_parse_orphan_tracker_candidates(ps_output))
    except Exception:
        return None


def _run_check_resources() -> None:
    """Emit a resource-pressure report to stdout; exit 2 on failure.

    Two signals:

    * ``_probe_semaphore_namespace`` -- direct SemLock allocation;
      fails with Errno 28 when the namespace is exhausted.
    * Orphan multiprocessing tracker count via
      :func:`_count_orphan_trackers`. Warns above 100 (advisory)
      and 1000 (urgent). Bead nexus-9h1s.
    """
    ok, msg = _probe_semaphore_namespace()
    orphan_count = _count_orphan_trackers()
    if ok:
        click.echo(f"[\u2713] resources: {msg}")
        if orphan_count is None:
            return
        if orphan_count >= 1000:
            click.echo(
                f"[!] orphan multiprocessing trackers: {orphan_count} "
                f"(URGENT - reap soon to avoid Errno 28; each leaks "
                f"POSIX semaphores until killed)",
                err=True,
            )
            click.echo(
                "    Reap inline: python -c 'from nexus.session import "
                "sweep_orphan_resource_trackers; "
                "print(sweep_orphan_resource_trackers())'\n"
                "    Or: ps -eo pid,ppid,command | "
                "awk '$2==1 && /multiprocessing/ {print $1}' | "
                "xargs kill -TERM",
                err=True,
            )
        elif orphan_count >= 100:
            click.echo(
                f"[!] orphan multiprocessing trackers: {orphan_count} "
                f"(advisory - accumulating)"
            )
        else:
            click.echo(
                f"[\u2713] orphan multiprocessing trackers: {orphan_count}"
            )
        return
    click.echo(f"[\u2717] resources: SemLock probe FAILED — {msg}", err=True)
    if orphan_count is not None:
        click.echo(
            f"    orphan multiprocessing trackers: {orphan_count}",
            err=True,
        )
    click.echo(
        "Known sources of POSIX semaphore exhaustion on this project:\n"
        "  - nexus-ze2a: MinerU workers leak semaphores.\n"
        "    Workaround: `nx mineru stop` (kills the whole process group).\n"
        "  - nexus-dc57: orphan chroma children from earlier nexus sessions.\n"
        "    Workaround: kill orphan chromas (`ps aux | grep 'chroma run'`).\n"
        "  - nexus-9h1s: multiprocessing.resource_tracker subprocesses\n"
        "    re-parented to init (PPID=1) after ungraceful MCP shutdowns.\n"
        "    Reap with: python -c 'from nexus.session import "
        "sweep_orphan_resource_trackers; "
        "print(sweep_orphan_resource_trackers())'\n"
        "If the count does not recover, reboot — macOS does not unlink\n"
        "leaked named semaphores until the next boot.",
        err=True,
    )
    raise click.exceptions.Exit(2)


# ── --check-t1 (RDR-105 P5 / nexus-ssdg) ─────────────────────────────────────


def _run_check_t1() -> None:
    """Diagnostic: T1 addr-file presence + reachability.

    .. note::
       Imports ``_tcp_probe_alive`` lazily from ``nexus.mcp.core`` to
       avoid pulling FastMCP / chromadb / corpus into the doctor's
       cold-start path.

    Three outcomes:

    * **Healthy.** A live ``claude*`` ancestor is reachable via the
      PPID walk and ``~/.config/nexus/t1_addr.<claude_pid>`` exists
      AND its host:port responds to a TCP probe.
    * **Missing addr file under live Claude.** A ``claude*`` ancestor
      is reachable but the addr file is absent. Two common causes:
      (a) the MCP server crashed before the lifespan wrote the file,
      (b) the operator launched Claude Code via ``exec -a`` or a
      custom wrapper whose process name does not start with
      ``claude``, defeating the PPID walk's match.
    * **No Claude in chain.** The current process has no ``claude*``
      ancestor; ``nx scratch`` from this shell will fail-loud unless
      the operator opts in via ``NX_T1_ISOLATED=1``.

    Exit code:
      * 0: healthy or "no Claude in chain" (informational).
      * 1: Claude in chain but addr file absent or unreachable.
    """
    import os as _os

    from nexus.mcp.core import _tcp_probe_alive
    from nexus.session import (
        _command_name_of,
        find_immediate_claude_pid,
        read_t1_addr_for,
        t1_addr_path,
    )

    own_pid = _os.getpid()
    claude_pid = find_immediate_claude_pid(start_pid=own_pid)

    if claude_pid <= 0:
        click.echo("[ ] T1: no claude ancestor in PPID chain")
        click.echo(
            "    This is informational. ``nx scratch`` from this shell "
            "will fail-loud unless you opt into per-process ephemeral "
            "T1 via ``NX_T1_ISOLATED=1``."
        )
        return

    comm = _command_name_of(claude_pid)
    is_claude = comm.lower().startswith("claude")
    if not is_claude:
        # find_immediate_claude_pid returned the immediate-PPID
        # fallback because no ancestor's comm starts with "claude".
        # Likely an exec -a / wrapper rename.
        click.echo(
            f"[!] T1: ancestor PID {claude_pid} (comm={comm!r}) is not "
            "named 'claude*'; likely launched via exec -a or a "
            "wrapper. Falling back to the immediate-PPID."
        )
        click.echo(
            "    If you launched Claude Code via ``exec -a`` or a "
            "custom wrapper, ensure the process name starts with "
            "``claude`` so the PPID walk can find it."
        )
        raise click.exceptions.Exit(1)

    addr_path = t1_addr_path(claude_pid)
    addr = read_t1_addr_for(claude_pid)
    if addr is None:
        click.echo(
            f"[✗] T1: claude ancestor PID {claude_pid} ({comm!r}) "
            f"is alive but {addr_path} is missing or unreadable."
        )
        click.echo(
            "    The MCP server's lifespan should have written this "
            "file at session start. Causes:\n"
            "      - The MCP server crashed before the lifespan "
            "completed (check ~/.config/nexus/logs/mcp.log).\n"
            "      - The MCP server is still booting; retry shortly.\n"
            "      - The Claude Code binary was launched via "
            "``exec -a`` with a different process name (the PPID walk "
            "found the right PID but the comm-prefix check missed)."
        )
        raise click.exceptions.Exit(1)

    host, port = addr
    if _tcp_probe_alive(host, port, timeout=1.0):
        click.echo(
            f"[✓] T1: addr file {addr_path.name} -> "
            f"{host}:{port} (chroma reachable)"
        )
        return

    click.echo(
        f"[✗] T1: addr file {addr_path.name} -> {host}:{port} "
        "but TCP probe failed."
    )
    click.echo(
        "    The addr file points at a chroma that is not listening. "
        "The MCP server may have died ungracefully. Restart Claude "
        "Code; the next MCP startup will sweep this stale addr file "
        "and spawn a fresh chroma."
    )
    raise click.exceptions.Exit(1)


# ── --check-quotas (nexus-c590) ──────────────────────────────────────────────


def _collect_quota_report() -> dict:
    """Build the structured quota-headroom report (nexus-c590).

    Returns a dict with three sections: ``chromadb`` (free-tier cloud
    limits + T3 reachability), ``voyage`` (per-model token + dimension
    caps), and ``retry`` (cumulative backoff observed in this process
    so far via :func:`nexus.retry.get_retry_stats`).

    Pure data-shape; both the human-readable and ``--json`` renderers
    consume this same dict so they never drift.

    *Why static*: live "requests/min" probing would require a running
    counter at every outgoing HTTP call; not shipped here. The retry
    counters give operators the most actionable signal — "backed off N
    times, slept Xs total" — without new plumbing.
    """
    from nexus.db.chroma_quotas import QUOTAS
    from nexus.retry import get_retry_stats

    chromadb_limits = {
        "max_embedding_dimensions": QUOTAS.MAX_EMBEDDING_DIMENSIONS,
        "max_document_bytes": QUOTAS.MAX_DOCUMENT_BYTES,
        "safe_chunk_bytes": QUOTAS.SAFE_CHUNK_BYTES,
        "max_query_results": QUOTAS.MAX_QUERY_RESULTS,
        "max_query_string_chars": QUOTAS.MAX_QUERY_STRING_CHARS,
        "max_where_predicates": QUOTAS.MAX_WHERE_PREDICATES,
        "max_concurrent_reads": QUOTAS.MAX_CONCURRENT_READS,
        "max_concurrent_writes": QUOTAS.MAX_CONCURRENT_WRITES,
        "max_records_per_write": QUOTAS.MAX_RECORDS_PER_WRITE,
        "max_records_per_collection": QUOTAS.MAX_RECORDS_PER_COLLECTION,
        "max_collections_per_account": QUOTAS.MAX_COLLECTIONS_PER_ACCOUNT,
    }

    # T3 reachability probe: is the configured cloud tenant reachable
    # right now? A quota report is only actionable if the client can
    # actually connect.
    t3_reachable = False
    t3_detail = ""
    try:
        from nexus.config import is_local_mode
        from nexus.mcp_infra import get_t3

        if is_local_mode():
            t3_reachable = True
            t3_detail = "local mode — cloud quotas are reference-only"
        else:
            # nexus-pac1 (RDR-112 P4.5): daemon-aware reachability probe.
            get_t3()
            t3_reachable = True
            t3_detail = "cloud tenant reachable"
    except Exception as exc:
        t3_detail = f"unreachable: {type(exc).__name__}: {str(exc)[:80]}"

    # Embedder limits. In cloud mode the three Voyage models we use
    # have a fixed 1024-dim space and 32k-token cap; in local mode the
    # ONNX MiniLM (384-dim) or fastembed bge (768-dim) is active. RDR-109
    # Phase 2: report what's actually embedding, not what the canonical
    # cloud schema would suggest.
    if is_local_mode():
        from nexus.db.local_ef import (  # noqa: PLC0415
            LocalEmbeddingFunction,
            local_model_token,
        )
        _ef = LocalEmbeddingFunction()
        voyage_limits = {
            "mode": "local",
            "models": {
                local_model_token(): {
                    "max_tokens": 512,
                    "embedding_dims": _ef.dimensions,
                },
            },
            "target_rpm": 0,
            "api_key_set": False,
        }
    else:
        voyage_limits = {
            "mode": "cloud",
            "models": {
                # nexus-8g79.22: voyage-3 is the LEGACY base model name;
                # Voyage AI retired it in early 2025. Kept here as a
                # detection label so doctor reports operators with
                # leftover voyage-3 configs see the retired tag rather
                # than a "healthy" line. New code paths use
                # voyage-code-3 / voyage-context-3 exclusively (see
                # corpus.py:effective_embedding_model_for_writes).
                "voyage-3": {
                    "max_tokens": 32_000,
                    "embedding_dims": 1024,
                    "status": "retired",
                },
                "voyage-code-3": {"max_tokens": 32_000, "embedding_dims": 1024},
                "voyage-context-3": {"max_tokens": 32_000, "embedding_dims": 1024},
            },
            "target_rpm": 250,  # matches ``doc_indexer._RATE_LIMIT_RPM``
            "api_key_set": False,
        }
    try:
        from nexus.config import get_credential

        voyage_limits["api_key_set"] = bool(get_credential("voyage_api_key"))
    except Exception:
        pass

    # Observed retry load — cumulative this process. Zero on fresh
    # sessions; non-zero after any `nx index` run that hit a transient
    # error.
    retry = dict(get_retry_stats())

    # RDR-109 Phase 3: cross-encoder substrate availability + active backend.
    from nexus.cross_encoder import cross_encoder_available  # noqa: PLC0415
    cross_encoder_info = {
        "available": cross_encoder_available(),
        "backend": "voyage-rerank-2.5" if not is_local_mode() else "onnx-local",
        "default_local_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    }

    return {
        "chromadb": {
            "limits": chromadb_limits,
            "reachable": t3_reachable,
            "detail": t3_detail,
        },
        "voyage": voyage_limits,
        "cross_encoder": cross_encoder_info,
        "retry": retry,
    }


def _format_quota_report(report: dict) -> str:
    """Human-readable form of :func:`_collect_quota_report` output."""
    lines: list[str] = []
    lines.append("Quota headroom report (nexus-c590)")
    lines.append("")

    # ── ChromaDB ─────────────────────────────────────────────────────────
    cdb = report["chromadb"]
    status = _CHECK if cdb["reachable"] else _WARN
    lines.append(f"  {status} ChromaDB Cloud: {cdb['detail']}")
    lines.append("    free-tier limits (from nexus.db.chroma_quotas.QUOTAS):")
    for k, v in cdb["limits"].items():
        lines.append(f"      {k:32} {v:,}")
    lines.append("")

    # ── Voyage ───────────────────────────────────────────────────────────
    v = report["voyage"]
    status = _CHECK if v["api_key_set"] else _WARN
    key_label = "VOYAGE_API_KEY: set" if v["api_key_set"] else "VOYAGE_API_KEY: absent"
    lines.append(f"  {status} Voyage AI: {key_label}")
    lines.append(f"    target rpm (indexer rate limiter):        {v['target_rpm']}")
    for model, caps in v["models"].items():
        lines.append(
            f"    {model:20} tokens={caps['max_tokens']:>6,}  "
            f"dims={caps['embedding_dims']}"
        )
    lines.append("")

    # ── Cross-encoder (RDR-109 Phase 3) ──────────────────────────────────
    ce = report.get("cross_encoder", {})
    if ce:
        ce_status = _CHECK if ce.get("available") else _WARN
        lines.append(
            f"  {ce_status} Cross-encoder backend: {ce.get('backend', 'unknown')}"
        )
        if ce.get("backend") == "onnx-local":
            lines.append(
                f"    default local model: {ce.get('default_local_model', '')}"
            )
        lines.append("")

    # ── Retry accumulator ────────────────────────────────────────────────
    r = report["retry"]
    if r.get("total_count", 0) > 0:
        lines.append(f"  {_WARN} Observed transient-error retries this process:")
        if r.get("voyage_count", 0) > 0:
            lines.append(
                f"    voyage:  {r['voyage_seconds']:>6.1f}s over "
                f"{r['voyage_count']} retries"
            )
        if r.get("chroma_count", 0) > 0:
            lines.append(
                f"    chroma:  {r['chroma_seconds']:>6.1f}s over "
                f"{r['chroma_count']} retries"
            )
        lines.append(
            f"    total:   {r['total_seconds']:>6.1f}s over "
            f"{r['total_count']} retries"
        )
    else:
        lines.append(f"  {_CHECK} Retry accumulator: no transient backoffs observed")

    return "\n".join(lines)


def _run_check_taxonomy() -> None:
    """Verify the topic_links ≡ projection-assignment invariant (GH #252).

    ``topic_links`` is the materialized aggregate of ``topic_assignments``
    rows with ``assigned_by='projection'``. Today a single caller
    (``_persist_assignments``) maintains it via ``refresh_projection_links``.
    Any future caller that writes projection assignments through
    ``assign_topic`` directly — or a test fixture that seeds rows — will
    silently re-break the invariant. This check detects the drift.
    """
    from nexus.commands._helpers import default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("T2 database not found — nothing to check.")
        return

    # nexus-pac1 (RDR-112 P4.5): daemon-aware introspection.
    with _T2Inspector(db_path) as t2:
        tables = t2.tables()
        required = {"topic_assignments", "topic_links", "topics"}
        missing = required - tables
        if missing:
            click.echo(
                "Taxonomy tables missing: "
                f"{', '.join(sorted(missing))} — run `nx catalog setup` to initialise."
            )
            return

        # Topics that have projection assignments but no row in topic_links
        # (neither as source nor target) are drift — but only when a
        # topic_links pair is structurally possible. A doc_id with exactly
        # one projection assignment cannot produce a link (a link requires
        # from + to), so flagging it as drift is a false positive. Same
        # logic if the co-occurring topic was assigned via a non-projection
        # path (centroid, bertopic) — refresh_projection_links only
        # aggregates ``assigned_by='projection'`` rows, so a centroid
        # partner does not contribute to topic_links. nexus-346q: require
        # a co-occurring projection assignment on the same doc before
        # flagging drift. Shakeout on live data: 15 of 20 residual drift
        # rows after a backfill were isolated topics that could never
        # produce a link.
        # The NOT EXISTS form (``tl.from_topic_id = ta.topic_id OR
        # tl.to_topic_id = ta.topic_id``) defeats SQLite's index planner —
        # the OR forces a covering scan of topic_links per outer row, which
        # multiplies with the topic_assignments scan into billions of row
        # touches on real-size catalogs (~526k × 13k = ~7B comparisons in
        # one production database, hanging the check past 30s). Pre-build
        # the linked-topic set with a UNION (uses the topic_links primary
        # key for both halves) and reduce to a single fast NOT IN.
        drift_rows = t2.execute(
            "SELECT DISTINCT ta.topic_id, t.label, t.collection "
            "  FROM topic_assignments ta "
            "  LEFT JOIN topics t ON t.id = ta.topic_id "
            " WHERE ta.assigned_by = 'projection' "
            "   AND ta.topic_id NOT IN ( "
            "       SELECT from_topic_id FROM topic_links "
            "       UNION "
            "       SELECT to_topic_id   FROM topic_links "
            "   ) "
            "   AND EXISTS ( "
            "       SELECT 1 FROM topic_assignments ta2 "
            "        WHERE ta2.doc_id      = ta.doc_id "
            "          AND ta2.topic_id    != ta.topic_id "
            "          AND ta2.assigned_by = 'projection' "
            "   )"
        )

        proj_rows = t2.execute(
            "SELECT COUNT(DISTINCT topic_id) FROM topic_assignments "
            "WHERE assigned_by = 'projection'"
        )
        projection_total = proj_rows[0][0] if proj_rows else 0

    if not drift_rows:
        click.echo(
            f"✓ topic_links invariant holds ({projection_total} topic(s) "
            "with projection assignments)."
        )
        return

    click.echo(
        f"✗ topic_links drift: {len(drift_rows)}/{projection_total} topic(s) "
        "have projection assignments but no topic_links row."
    )
    for topic_id, label, coll in drift_rows[:10]:
        pretty = label or f"(unlabelled id={topic_id})"
        scope = f" [{coll}]" if coll else ""
        click.echo(f"  - topic {topic_id}: {pretty}{scope}")
    if len(drift_rows) > 10:
        click.echo(f"  … {len(drift_rows) - 10} more")
    click.echo(
        "Fix: re-run `nx taxonomy project --backfill --persist` to rebuild "
        "the materialized view."
    )
    raise click.exceptions.Exit(1)


def _run_check_quotas(*, json_out: bool = False) -> None:
    """Emit the quota-headroom report (nexus-c590).

    Exits 1 when ChromaDB is unreachable in cloud mode — a quota
    report without a client connection is not actionable. Local mode
    and a reachable cloud tenant both exit 0.
    """
    import json as _json

    report = _collect_quota_report()
    if json_out:
        click.echo(_json.dumps(report, indent=2))
    else:
        click.echo(_format_quota_report(report))

    if not report["chromadb"]["reachable"]:
        raise click.exceptions.Exit(1)
