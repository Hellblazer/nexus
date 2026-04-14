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
    lines: list[str] = []
    all_ok = True

    # Check expected tables
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for tbl in ("memory", "plans", "topics", "topic_assignments", "relevance_log"):
        ok = tbl in tables
        lines.append(_check_line(f"Table {tbl}", ok))
        if not ok:
            all_ok = False

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
def doctor_cmd(clean_checkpoints: bool, clean_pipelines: bool, fix: bool,
               fix_paths: bool, dry_run: bool, check_schema: bool) -> None:
    """Verify that all required services and credentials are available."""
    if check_schema:
        _run_check_schema()
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
        registry_path = Path.home() / ".config" / "nexus" / "repos.json"
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
