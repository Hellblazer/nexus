# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
from __future__ import annotations

import hashlib
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


def _run_check_schema() -> None:
    """Validate T2 database schema and report pending migrations (RDR-076)."""
    import sqlite3

    from nexus.commands._helpers import default_db_path
    from nexus.db.migrations import MIGRATIONS, _parse_version

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("T2 database not found — nothing to check.")
        return

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    # CLI review: match the other T2 connection defaults. Opening without
    # WAL here caused immediate lock errors when a concurrent MCP tool
    # was writing during the check.
    conn.execute("PRAGMA journal_mode=WAL")
    lines: list[str] = []
    all_ok = True

    # Check expected tables (base tables and every domain store).
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for tbl in ("memory", "plans", "topics", "topic_assignments", "taxonomy_meta", "topic_links", "relevance_log", "search_telemetry", "chash_index", "hook_failures"):
        ok = tbl in tables
        lines.append(_check_line(f"Table {tbl}", ok))
        if not ok:
            all_ok = False

    # CLI review: the FTS5 virtual tables are load-bearing for memory
    # search + plan match. A schema without them passes the table
    # check but fails at query time. Include them + critical indexes.
    fts_names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND sql LIKE '%USING fts5%'"
        ).fetchall()
    }
    for fts in ("memory_fts",):
        ok = fts in fts_names or fts in tables
        lines.append(_check_line(f"FTS5 table {fts}", ok))
        if not ok:
            all_ok = False

    index_names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
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
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
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
                lines.append("    Fix: run 'nx upgrade'")
            else:
                lines.append(_check_line("Schema version", True, f"v{stored}"))
        else:
            all_ok = False
            lines.append(_check_line("Version row", False, "missing"))
    else:
        all_ok = False
        lines.append("    Fix: run 'nx upgrade'")

    conn.close()

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

    from nexus.db.t1 import SESSIONS_DIR
    from nexus.session import sweep_orphan_tmpdirs

    tmpdir_root = Path(tempfile.gettempdir())
    cutoff_hours = 24.0
    cutoff = time.time() - cutoff_hours * 3600.0

    referenced: set[str] = set()
    if SESSIONS_DIR.exists():
        for f in SESSIONS_DIR.glob("*.session"):
            try:
                rec = json.loads(f.read_text())
                if isinstance(rec, dict):
                    td = rec.get("tmpdir", "")
                    if td:
                        referenced.add(str(Path(td).resolve()))
            except (json.JSONDecodeError, OSError):
                continue

    candidates: list[dict] = []
    if tmpdir_root.exists():
        for d in sorted(tmpdir_root.glob("nx_t1_*")):
            if not d.is_dir():
                continue
            try:
                resolved = str(d.resolve())
            except OSError:
                continue
            if resolved in referenced:
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
            sessions_dir=SESSIONS_DIR, tmpdir_root=tmpdir_root,
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
    import sqlite3

    from nexus.commands._helpers import default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("T2 database not found; nothing to check.")
        click.echo("Fix: run 'nx catalog setup' to initialise the library.")
        raise click.exceptions.Exit(1)

    # Context manager guards against a raise inside the count loop
    # leaking the connection (RDR-092 code-review S-3).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")

        def _count(where: str) -> int:
            row = conn.execute(
                f"SELECT COUNT(*) FROM plans WHERE {where}"
            ).fetchone()
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
    finally:
        conn.close()

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


# ── --check-hooks (nexus-ntbg) ───────────────────────────────────────────────


def _run_check_hooks(*, threshold_ms: int, days: int, json_out: bool) -> None:
    """Report slow PostToolUse hook firings recorded in T2 hook_telemetry."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2.telemetry import Telemetry

    db_path = default_db_path()
    if not db_path.exists():
        if json_out:
            click.echo('{"rows": [], "count": 0, "note": "T2 db not found"}')
        else:
            click.echo("T2 database not found — no hook telemetry available.")
        return

    telemetry = Telemetry(db_path)
    try:
        rows = telemetry.query_slow_hooks(
            threshold_ms=threshold_ms, days=days, limit=200,
        )
    finally:
        telemetry.close()

    if json_out:
        import json as _json
        click.echo(_json.dumps({"rows": rows, "count": len(rows)}))
        return

    if not rows:
        click.echo(
            f"No slow-hook records in last {days} days "
            f"(threshold ≥{threshold_ms}ms)."
        )
        click.echo(
            "  Tune writer threshold via env "
            "NX_HOOK_TELEMETRY_THRESHOLD_MS (default 2000ms)."
        )
        return

    click.echo(
        f"Slow hook firings (last {days}d, ≥{threshold_ms}ms, "
        f"top {len(rows)}):"
    )
    click.echo("")
    # Brief table
    click.echo(f"  {'TIMESTAMP':27} {'DURATION':>10}  {'EVENT':18} TOOL")
    click.echo(f"  {'-'*27} {'-'*10}  {'-'*18} {'-'*30}")
    for r in rows:
        ts = (r.get("ts") or "")[:26]
        dur = f"{r.get('duration_ms', 0)}ms"
        evt = (r.get("hook_event_name") or "")[:18]
        tool = (r.get("tool_name") or "")[:48]
        click.echo(f"  {ts:27} {dur:>10}  {evt:18} {tool}")


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
    import sqlite3 as _sqlite3
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("aspect_extraction_queue: T2 database not found.")
        return

    conn = _sqlite3.connect(str(db_path))
    try:
        # Confirm the table exists (pre-RDR-089 dbs won't have it).
        has_table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='aspect_extraction_queue'"
        ).fetchone()
        if not has_table:
            click.echo(
                "aspect_extraction_queue: table not present "
                "(no aspect-extraction work has been queued)."
            )
            return

        total = conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue"
        ).fetchone()[0]
        click.echo(f"aspect_extraction_queue: {total} row(s) total")

        if total == 0:
            return

        # Per-status breakdown.
        click.echo("\nBy status:")
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM aspect_extraction_queue "
            "GROUP BY status ORDER BY status"
        ).fetchall()
        for status, count in rows:
            click.echo(f"  {status:<12} {count:>6}")

        # Oldest enqueued_at across all non-completed rows — the lag
        # indicator. ``processing`` and ``pending`` both contribute
        # to the worker's open-work view; we report MIN across them.
        oldest = conn.execute(
            "SELECT MIN(enqueued_at), source_path "
            "FROM aspect_extraction_queue "
            "WHERE status IN ('pending', 'processing')"
        ).fetchone()
        if oldest and oldest[0]:
            click.echo(
                f"\nOldest pending/processing: {oldest[0]} "
                f"({oldest[1] or '?'})"
            )

        # Surface failed rows with their last_error so the operator
        # sees stuck work without needing SQL.
        failed = conn.execute(
            "SELECT collection, source_path, retry_count, last_error "
            "FROM aspect_extraction_queue "
            "WHERE status = 'failed' "
            "ORDER BY enqueued_at DESC LIMIT 20"
        ).fetchall()
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
    finally:
        conn.close()


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
    help="Probe POSIX semaphore headroom. Exits 2 with 'Errno 28' when "
         "the namespace is exhausted (known sources: MinerU workers / "
         "orphan chroma children leaking via multiprocessing). Beads "
         "nexus-dc57 + nexus-ze2a.",
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
    "--check-hooks",
    "check_hooks",
    is_flag=True,
    default=False,
    help="Report slow PostToolUse hook firings captured into "
         "T2 hook_telemetry. Tunable via NX_HOOK_TELEMETRY_THRESHOLD_MS "
         "(default 2000ms) on the hook side. nexus-ntbg.",
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
    "--check-aspect-queue",
    "check_aspect_queue",
    is_flag=True,
    default=False,
    help="Report aspect_extraction_queue depth, per-status counts, "
         "oldest pending row, and any failed rows with their last "
         "error. nexus-1pfq.",
)
@click.option(
    "--hook-threshold",
    "hook_threshold",
    default=0,
    type=click.IntRange(min=0),
    show_default=True,
    help="Additional duration_ms filter for --check-hooks (0 = include all stored).",
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
               json_out: bool,
               trim_telemetry: bool, days: int,
               check_hooks: bool, hook_threshold: int,
               check_post_store_hooks: bool,
               check_aspect_queue: bool) -> None:
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

    if check_hooks:
        _run_check_hooks(threshold_ms=hook_threshold, days=days, json_out=json_out)
        return

    if check_post_store_hooks:
        _run_check_post_store_hooks()
        return

    if check_aspect_queue:
        _run_check_aspect_queue()
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
        from nexus.db import make_t3

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
            t3_db = make_t3()

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


def _run_check_resources() -> None:
    """Emit a resource-pressure report to stdout; exit 2 on failure."""
    ok, msg = _probe_semaphore_namespace()
    if ok:
        click.echo(f"[\u2713] resources: {msg}")
        return
    click.echo(f"[\u2717] resources: SemLock probe FAILED — {msg}", err=True)
    click.echo(
        "Known sources of POSIX semaphore exhaustion on this project:\n"
        "  - nexus-ze2a: MinerU workers leak semaphores.\n"
        "    Workaround: `nx mineru stop` (kills the whole process group).\n"
        "  - nexus-dc57: orphan chroma children from earlier nexus sessions.\n"
        "    Workaround: kill orphan chromas (`ps aux | grep 'chroma run'`).\n"
        "If the count does not recover, reboot — macOS does not unlink\n"
        "leaked named semaphores until the next boot.",
        err=True,
    )
    raise click.exceptions.Exit(2)


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
        from nexus.db import make_t3

        if is_local_mode():
            t3_reachable = True
            t3_detail = "local mode — cloud quotas are reference-only"
        else:
            make_t3()
            t3_reachable = True
            t3_detail = "cloud tenant reachable"
    except Exception as exc:
        t3_detail = f"unreachable: {type(exc).__name__}: {str(exc)[:80]}"

    # Voyage AI limits. Model-specific token caps come from the Voyage
    # published specs (documented alongside ``nexus.corpus``); embedding
    # dimension is fixed across the three models we use.
    voyage_limits = {
        "models": {
            "voyage-3": {"max_tokens": 32_000, "embedding_dims": 1024},
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

    return {
        "chromadb": {
            "limits": chromadb_limits,
            "reachable": t3_reachable,
            "detail": t3_detail,
        },
        "voyage": voyage_limits,
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
    import sqlite3

    from nexus.commands._helpers import default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        click.echo("T2 database not found — nothing to check.")
        return

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
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
    drift_rows = conn.execute(
        """
        SELECT DISTINCT ta.topic_id, t.label, t.collection
          FROM topic_assignments ta
          LEFT JOIN topics t ON t.id = ta.topic_id
         WHERE ta.assigned_by = 'projection'
           AND EXISTS (
               SELECT 1 FROM topic_assignments ta2
                WHERE ta2.doc_id      = ta.doc_id
                  AND ta2.topic_id    != ta.topic_id
                  AND ta2.assigned_by = 'projection'
           )
           AND NOT EXISTS (
               SELECT 1 FROM topic_links tl
                WHERE tl.from_topic_id = ta.topic_id
                   OR tl.to_topic_id   = ta.topic_id
           )
        """
    ).fetchall()

    projection_total = conn.execute(
        "SELECT COUNT(DISTINCT topic_id) FROM topic_assignments "
        "WHERE assigned_by = 'projection'"
    ).fetchone()[0]

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
