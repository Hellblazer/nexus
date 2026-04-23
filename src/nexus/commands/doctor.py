# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
import hashlib
from pathlib import Path

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
#: RDR-092 Phase 0a brings that to 12, but the check only fails below 9
#: so a partial install on an older plugin is still tolerated.
_MIN_GLOBAL_BUILTIN_COUNT: int = 9


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
               check_plan_library: bool, json_out: bool,
               trim_telemetry: bool, days: int) -> None:
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

    if check_taxonomy:
        _run_check_taxonomy()
        return

    if check_plan_library:
        _run_check_plan_library()
        return

    if trim_telemetry:
        _run_trim_telemetry(days=days)
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
