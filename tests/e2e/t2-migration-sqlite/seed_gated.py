#!/usr/bin/env python3
"""Seed an isolated T2 memory.db (+ optional catalog) into a known migration
state for the SQLite-T2 migration E2E (RDR-170 / RDR-142 / nexus-3lbhb).

Runs INSIDE the container against a clean config dir — never touches a real
install. Pure sqlite3 + stdlib so it needs no nexus import.

Usage:  seed_gated.py <config_dir> <mode>
  mode = gated-orphan   : legacy-PK document_aspects + 2 unmapped rows + present
                          catalog (maps nothing) + version below 4.30.0
                          -> the RDR-108 PK migration GATES (MigrationError).
  mode = deferred       : legacy-PK document_aspects, NO catalog, version below
                          4.30.0 -> the PK migration DEFERS (MigrationRetry).
"""
import sqlite3
import sys
from pathlib import Path


def _bootstrap_version_row(conn: sqlite3.Connection, value: str) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _nexus_version (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO _nexus_version (key, value) VALUES ('cli_version', ?)",
        (value,),
    )


def _legacy_aspects(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE document_aspects ("
        "  collection TEXT NOT NULL, source_path TEXT NOT NULL,"
        "  doc_id TEXT NOT NULL DEFAULT '', source_uri TEXT, extracted_at TEXT,"
        "  model_version TEXT, extractor_name TEXT,"
        "  PRIMARY KEY (collection, source_path))"
    )
    conn.executemany(
        "INSERT INTO document_aspects (collection, source_path, source_uri) VALUES (?,?,?)",
        [("knowledge__orphan", "/a.pdf", "uri://a"), ("knowledge__orphan", "/b.pdf", "uri://b")],
    )


def _empty_catalog(cat: Path) -> None:
    cat.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(cat))
    c.executescript("""
        CREATE TABLE documents (tumbler TEXT PRIMARY KEY, title TEXT DEFAULT 'd',
            file_path TEXT, physical_collection TEXT);
        CREATE TABLE collections (name TEXT PRIMARY KEY, superseded_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '');
    """)
    c.commit()
    c.close()


def main() -> int:
    config_dir = Path(sys.argv[1])
    mode = sys.argv[2]
    config_dir.mkdir(parents=True, exist_ok=True)
    mem = config_dir / "memory.db"
    if mem.exists():
        mem.unlink()

    if mode == "catalog-only":
        # No legacy memory.db: a truly fresh install WITH a catalog present, so
        # `nx upgrade` runs apply_pending to COMPLETION (the je0b PK steps find a
        # catalog + an empty table -> no orphans -> succeed) and stamps the
        # registry-max version. The clean/registry-aware-stamp scenario.
        _empty_catalog(config_dir / "catalog" / ".catalog.db")
        print(f"seeded {mode} at {config_dir}")
        return 0

    conn = sqlite3.connect(str(mem))
    conn.execute("PRAGMA journal_mode=WAL")
    _bootstrap_version_row(conn, "4.1.2")  # below the 4.30.0 PK migration
    _legacy_aspects(conn)
    conn.commit()
    conn.close()

    if mode == "gated-orphan":
        _empty_catalog(config_dir / "catalog" / ".catalog.db")  # present, maps nothing
    elif mode == "deferred":
        pass  # no catalog -> MigrationRetry (defer)
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    print(f"seeded {mode} at {config_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
