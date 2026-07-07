# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the SQLite->Postgres catalog ETL (bead nexus-bdaxz, RDR-152).

Two test levels:

Unit tests (fast, no service):
  - Row transform logic: owner, document, link, chunk, collection field mapping
  - id NOT copied for links (SQLite AUTOINCREMENT PK; PG uses BIGSERIAL)
  - metadata JSON parsed from string to dict
  - FK insertion order invariant via mock call sequence
  - copy-not-move: SQLite source read-only
  - count_source_rows() returns per-table counts

Integration tests (@pytest.mark.integration):
  - Full ETL against a real Java service + hermetic Postgres 16
  - EXACT row counts: SQLite owners/documents/links == PG after ETL
  - Spot-check: a document's fields round-trip correctly
  - Spot-check: a link's fields round-trip correctly
  - Tenant stamping (all rows have tenant_id='default')
  - Idempotency: second run produces no additional rows
  - FK ordering proof: link referencing a doc lands correctly
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL

# ── Prerequisite paths ─────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")
_INITDB    = _PG_BIN / "initdb"
_PG_CTL    = _PG_BIN / "pg_ctl"
_PSQL      = _PG_BIN / "psql"
_CREATEDB  = _PG_BIN / "createdb"
_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = Path(_JAVA_HOME) / "bin" / "java" if _JAVA_HOME else Path(shutil.which("java") or "java")

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

_SKIP_INTEGRATION = pytest.mark.skipif(
    not _ALL_PREREQS,
    reason=(
        "skipped: missing jar or pg16 binaries "
        f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
    ),
)

# Note: NO _BOOTSTRAP_SQL here.  The Java service runs Liquibase (SchemaMigrator)
# at startup and self-migrates the full catalog schema before binding the HTTP port.
# Pre-applying DDL causes "relation already exists" → MigrationException →
# System.exit(1) → service never binds (same bug fixed in net63 / qnp5s).
# The only pre-start SQL is SERVICE_ROLES_SQL (creates nexus_svc role needed by
# grants-nexus-svc.xml runAlways changeset) applied in cat_etl_pg_instance.

# ── SQLite schema (minimal, mirrors CatalogStore._SCHEMA_SQL) ─────────────────

_CATALOG_SCHEMA = """\
CREATE TABLE IF NOT EXISTS owners (
    tumbler_prefix TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    owner_type     TEXT NOT NULL,
    repo_hash      TEXT,
    description    TEXT,
    repo_root      TEXT DEFAULT '',
    head_hash      TEXT
);
-- Note: the real SQLite .catalog.db owners table has NO next_seq column
-- (next_seq is JSONL-only state in the SQLite-backed catalog). The ETL derives
-- the post-migration next_seq from the migrated document tumblers, not from here.

CREATE TABLE IF NOT EXISTS documents (
    tumbler              TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    author               TEXT,
    year                 INTEGER,
    content_type         TEXT,
    file_path            TEXT,
    corpus               TEXT,
    physical_collection  TEXT,
    chunk_count          INTEGER,
    head_hash            TEXT,
    indexed_at           TEXT,
    metadata             TEXT,
    source_mtime         REAL DEFAULT 0.0,
    alias_of             TEXT DEFAULT '',
    source_uri           TEXT DEFAULT '',
    bib_year             INTEGER DEFAULT 0,
    bib_authors          TEXT DEFAULT '',
    bib_venue            TEXT DEFAULT '',
    bib_citation_count   INTEGER DEFAULT 0,
    bib_semantic_scholar_id TEXT DEFAULT '',
    bib_openalex_id      TEXT DEFAULT '',
    bib_doi              TEXT DEFAULT '',
    bib_enriched_at      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_tumbler TEXT NOT NULL,
    to_tumbler   TEXT NOT NULL,
    link_type    TEXT NOT NULL,
    from_span    TEXT DEFAULT '',
    to_span      TEXT DEFAULT '',
    created_by   TEXT DEFAULT 'user',
    created_at   TEXT DEFAULT '',
    metadata     TEXT,
    UNIQUE(from_tumbler, to_tumbler, link_type)
);

CREATE TABLE IF NOT EXISTS collections (
    name                 TEXT PRIMARY KEY,
    content_type         TEXT DEFAULT '',
    owner_id             TEXT DEFAULT '',
    embedding_model      TEXT DEFAULT '',
    model_version        TEXT DEFAULT '',
    display_name         TEXT DEFAULT '',
    legacy_grandfathered INTEGER DEFAULT 0,
    superseded_by        TEXT DEFAULT '',
    superseded_at        TEXT DEFAULT '',
    created_at           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS document_chunks (
    doc_id      TEXT NOT NULL,
    position    INTEGER NOT NULL,
    chash       TEXT NOT NULL,
    chunk_index INTEGER,
    line_start  INTEGER,
    line_end    INTEGER,
    char_start  INTEGER,
    char_end    INTEGER,
    PRIMARY KEY (doc_id, position)
);

CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ── Bootstrap SQL for hermetic Postgres (from test_http_catalog_integration.py) ─

# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.15)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _psql(pg: dict, sql: str, dbname: str | None = None) -> None:
    db = dbname or pg["dbname"]
    proc = subprocess.run(
        [
            str(_PSQL),
            "-h", "127.0.0.1",
            "-p", str(pg["port"]),
            "-U", pg["user"],
            "-d", db,
            "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql failed (rc={proc.returncode}):\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )


def _make_source_catalog(
    *,
    owners: list[dict] | None = None,
    documents: list[dict] | None = None,
    links: list[dict] | None = None,
    collections: list[dict] | None = None,
    chunks: list[dict] | None = None,
    meta: list[dict] | None = None,
    owner_high_water: dict[str, int] | None = None,
) -> Path:
    """Build a hermetic SQLite catalog DB for use as an ETL source.

    Returns the path to the .catalog.db file (in a temp directory).
    All tables are created even if no rows are provided.

    ``owner_high_water`` writes a sibling ``owners.jsonl`` mapping owner prefix to
    ``next_seq`` (the high-water mark the real catalog keeps there, NOT in the DB).
    The ETL reads it to floor next_seq, defending against tumbler reuse on a source
    catalog that has had documents deleted/compacted. When omitted, no owners.jsonl
    is written and the ETL falls back to max(surviving doc seq).
    """
    tmp = tempfile.mkdtemp(prefix="nexus_cat_etl_src_")
    db_path = Path(tmp) / ".catalog.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CATALOG_SCHEMA)
    conn.commit()

    for row in (owners or []):
        conn.execute(
            "INSERT INTO owners (tumbler_prefix, name, owner_type, repo_hash, "
            "description, repo_root, head_hash) VALUES (?,?,?,?,?,?,?)",
            (
                row["tumbler_prefix"],
                row["name"],
                row.get("owner_type", "repo"),
                row.get("repo_hash"),
                row.get("description"),
                row.get("repo_root", ""),
                row.get("head_hash"),
            ),
        )

    for row in (documents or []):
        meta_json = row.get("metadata")
        if isinstance(meta_json, dict):
            meta_json = json.dumps(meta_json)
        conn.execute(
            "INSERT INTO documents (tumbler, title, author, year, content_type, "
            "file_path, corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata, source_mtime, alias_of, source_uri, "
            "bib_year, bib_authors, bib_venue, bib_citation_count, "
            "bib_semantic_scholar_id, bib_openalex_id, bib_doi, bib_enriched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["tumbler"],
                row["title"],
                row.get("author", ""),
                row.get("year", 0),
                row.get("content_type", ""),
                row.get("file_path", ""),
                row.get("corpus", ""),
                row.get("physical_collection", ""),
                row.get("chunk_count", 0),
                row.get("head_hash", ""),
                row.get("indexed_at", ""),
                meta_json,
                row.get("source_mtime", 0.0),
                row.get("alias_of", ""),
                row.get("source_uri", ""),
                row.get("bib_year", 0),
                row.get("bib_authors", ""),
                row.get("bib_venue", ""),
                row.get("bib_citation_count", 0),
                row.get("bib_semantic_scholar_id", ""),
                row.get("bib_openalex_id", ""),
                row.get("bib_doi", ""),
                row.get("bib_enriched_at", ""),
            ),
        )

    for row in (links or []):
        link_meta = row.get("metadata")
        if isinstance(link_meta, dict):
            link_meta = json.dumps(link_meta)
        conn.execute(
            "INSERT INTO links (from_tumbler, to_tumbler, link_type, "
            "from_span, to_span, created_by, created_at, metadata) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                row["from_tumbler"],
                row["to_tumbler"],
                row["link_type"],
                row.get("from_span", ""),
                row.get("to_span", ""),
                row.get("created_by", "user"),
                row.get("created_at", ""),
                link_meta,
            ),
        )

    for row in (collections or []):
        conn.execute(
            "INSERT INTO collections (name, content_type, owner_id, "
            "embedding_model, model_version, display_name, "
            "legacy_grandfathered, superseded_by, superseded_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["name"],
                row.get("content_type", ""),
                row.get("owner_id", ""),
                row.get("embedding_model", ""),
                row.get("model_version", ""),
                row.get("display_name", ""),
                row.get("legacy_grandfathered", 0),
                row.get("superseded_by", ""),
                row.get("superseded_at", ""),
                row.get("created_at", ""),
            ),
        )

    for row in (chunks or []):
        conn.execute(
            "INSERT INTO document_chunks (doc_id, position, chash, "
            "chunk_index, line_start, line_end, char_start, char_end) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                row["doc_id"],
                row["position"],
                row["chash"],
                row.get("chunk_index"),
                row.get("line_start"),
                row.get("line_end"),
                row.get("char_start"),
                row.get("char_end"),
            ),
        )

    for row in (meta or []):
        conn.execute(
            "INSERT INTO _meta (key, value) VALUES (?,?)",
            (row["key"], row.get("value")),
        )

    conn.commit()
    conn.close()

    if owner_high_water:
        jsonl_path = db_path.parent / "owners.jsonl"
        with jsonl_path.open("w") as f:
            for prefix, next_seq in owner_high_water.items():
                f.write(json.dumps({
                    "owner": prefix,
                    "name": f"owner-{prefix}",
                    "owner_type": "repo",
                    "repo_hash": "",
                    "description": "",
                    "next_seq": next_seq,
                }) + "\n")

    return db_path


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — no service, no SQLite DB required
# ══════════════════════════════════════════════════════════════════════════════

class TestOwnerTransform:
    """_transform_owner field mapping."""

    def test_required_fields_present(self):
        from nexus.db.t2.catalog_etl import _transform_owner

        row = {
            "tumbler_prefix": "1.42",
            "name": "myrepo",
            "owner_type": "repo",
            "repo_hash": "abc123",
            "description": "desc",
            "repo_root": "/home/user/repo",
            "head_hash": "deadbeef",
        }
        result = _transform_owner(row)
        assert result["tumbler_prefix"] == "1.42"
        assert result["name"] == "myrepo"
        assert result["owner_type"] == "repo"
        assert result["repo_hash"] == "abc123"
        assert result["description"] == "desc"
        assert result["repo_root"] == "/home/user/repo"
        assert result["head_hash"] == "deadbeef"

    def test_none_fields_become_empty_string(self):
        from nexus.db.t2.catalog_etl import _transform_owner

        row = {
            "tumbler_prefix": "1.1",
            "name": "n",
            "owner_type": "repo",
            "repo_hash": None,
            "description": None,
            "repo_root": None,
            "head_hash": None,
        }
        result = _transform_owner(row)
        assert result["repo_hash"] == ""
        assert result["description"] == ""
        assert result["repo_root"] == ""
        assert result["head_hash"] == ""


class TestDocumentTransform:
    """_transform_document field mapping."""

    def test_metadata_string_parsed_to_dict(self):
        from nexus.db.t2.catalog_etl import _transform_document

        raw = {"key": "value", "n": 42}
        row = {
            "tumbler": "1.1.1",
            "title": "T",
            "metadata": json.dumps(raw),
        }
        result = _transform_document(row)
        assert result["metadata"] == raw, (
            f"metadata string must be parsed to dict, got: {result['metadata']!r}"
        )

    def test_metadata_none_stays_none(self):
        from nexus.db.t2.catalog_etl import _transform_document

        row = {"tumbler": "1.1.1", "title": "T", "metadata": None}
        result = _transform_document(row)
        assert result["metadata"] is None

    def test_metadata_already_dict_preserved(self):
        from nexus.db.t2.catalog_etl import _transform_document

        d = {"x": 1}
        row = {"tumbler": "1.1.2", "title": "T", "metadata": d}
        result = _transform_document(row)
        assert result["metadata"] == d

    def test_metadata_invalid_json_becomes_none(self):
        from nexus.db.t2.catalog_etl import _transform_document

        row = {"tumbler": "1.1.3", "title": "T", "metadata": "{not:valid json"}
        result = _transform_document(row)
        assert result["metadata"] is None

    def test_tumbler_and_title_required(self):
        from nexus.db.t2.catalog_etl import _transform_document

        row = {"tumbler": "1.2.3", "title": "My Title"}
        result = _transform_document(row)
        assert result["tumbler"] == "1.2.3"
        assert result["title"] == "My Title"

    def test_bib_fields_default_to_zero_or_empty(self):
        from nexus.db.t2.catalog_etl import _transform_document

        row = {"tumbler": "1.1.4", "title": "T"}
        result = _transform_document(row)
        assert result["bib_year"] == 0
        assert result["bib_authors"] == ""
        assert result["bib_citation_count"] == 0


class TestLinkTransform:
    """_transform_link field mapping."""

    def test_id_not_in_payload(self):
        """SQLite AUTOINCREMENT id must NOT appear in the ETL payload."""
        from nexus.db.t2.catalog_etl import _transform_link

        row = {
            "id": 17,
            "from_tumbler": "1.1.1",
            "to_tumbler": "1.1.2",
            "link_type": "cites",
            "from_span": "",
            "to_span": "",
            "created_by": "user",
            "created_at": "2026-01-01T00:00:00Z",
            "metadata": None,
        }
        result = _transform_link(row)
        assert "id" not in result, "SQLite id must not be in the import payload"

    def test_link_fields_round_trip(self):
        from nexus.db.t2.catalog_etl import _transform_link

        row = {
            "id": 1,
            "from_tumbler": "1.2.3",
            "to_tumbler": "1.2.4",
            "link_type": "implements",
            "from_span": "chash:aabbcc",
            "to_span": "",
            "created_by": "developer",
            "created_at": "2026-03-15T09:00:00Z",
            "metadata": None,
        }
        result = _transform_link(row)
        assert result["from_tumbler"] == "1.2.3"
        assert result["to_tumbler"] == "1.2.4"
        assert result["link_type"] == "implements"
        assert result["from_span"] == "chash:aabbcc"
        assert result["created_by"] == "developer"

    def test_link_metadata_string_parsed(self):
        from nexus.db.t2.catalog_etl import _transform_link

        meta = {"co_discovered_by": ["agent1"]}
        row = {
            "id": 2,
            "from_tumbler": "1.1.1",
            "to_tumbler": "1.1.2",
            "link_type": "cites",
            "metadata": json.dumps(meta),
        }
        result = _transform_link(row)
        assert result["metadata"] == meta


class TestChunkTransform:
    """_transform_chunk_row field mapping."""

    def test_doc_id_not_in_row_payload(self):
        """doc_id must be excluded from the per-row payload (it's in the envelope)."""
        from nexus.db.t2.catalog_etl import _transform_chunk_row

        row = {
            "doc_id": "1.2.3",
            "position": 0,
            "chash": "abcdef0123456789abcdef0123456789",
            "chunk_index": 0,
            "line_start": 1,
            "line_end": 10,
            "char_start": 0,
            "char_end": 500,
        }
        result = _transform_chunk_row(row)
        assert "doc_id" not in result, "doc_id must be in the envelope, not per-row payload"

    def test_position_and_chash_present(self):
        from nexus.db.t2.catalog_etl import _transform_chunk_row

        row = {
            "doc_id": "1.1.1",
            "position": 3,
            "chash": "cafebabe0000000000000000cafebabe",
        }
        result = _transform_chunk_row(row)
        assert result["position"] == 3
        assert result["chash"] == "cafebabe0000000000000000cafebabe"


class TestCollectionTransform:
    """_transform_collection field mapping."""

    def test_name_required(self):
        from nexus.db.t2.catalog_etl import _transform_collection

        row = {"name": "code__myrepo__voyage-code-3__v1"}
        result = _transform_collection(row)
        assert result["name"] == "code__myrepo__voyage-code-3__v1"

    def test_all_fields_present(self):
        from nexus.db.t2.catalog_etl import _transform_collection

        row = {
            "name": "docs__nexus__voyage-context-3__v1",
            "content_type": "docs",
            "owner_id": "1.5",
            "embedding_model": "voyage-context-3",
            "model_version": "v1",
            "display_name": "Nexus docs",
            "legacy_grandfathered": 1,
            "superseded_by": "",
            "superseded_at": "",
            "created_at": "2026-01-01T00:00:00Z",
        }
        result = _transform_collection(row)
        assert result["content_type"] == "docs"
        assert result["owner_id"] == "1.5"
        assert result["legacy_grandfathered"] == 1


class TestMaxSeqForOwner:
    """_max_seq_for_owner helper."""

    def test_returns_max_doc_sequence(self):
        from nexus.db.t2.catalog_etl import _max_seq_for_owner

        tumblers = ["1.2.1", "1.2.3", "1.2.10", "1.3.5"]
        assert _max_seq_for_owner("1.2", tumblers) == 10

    def test_returns_zero_when_no_docs(self):
        from nexus.db.t2.catalog_etl import _max_seq_for_owner

        assert _max_seq_for_owner("1.99", ["1.2.1"]) == 0

    def test_ignores_nested_tumblers(self):
        from nexus.db.t2.catalog_etl import _max_seq_for_owner

        # "1.2.3.4" is a sub-document; should not count for prefix "1.2"
        tumblers = ["1.2.5", "1.2.3.4"]
        assert _max_seq_for_owner("1.2", tumblers) == 5


class TestCountSourceRows:
    """count_source_rows returns per-table counts."""

    def test_empty_tables_return_zeros(self):
        from nexus.db.t2.catalog_etl import count_source_rows

        db_path = _make_source_catalog()
        counts = count_source_rows(db_path)
        assert counts["owners"] == 0
        assert counts["documents"] == 0
        assert counts["links"] == 0
        assert counts["collections"] == 0
        assert counts["document_chunks"] == 0
        assert counts["_meta"] == 0

    def test_counts_rows_correctly(self):
        from nexus.db.t2.catalog_etl import count_source_rows

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.1", "name": "o1", "owner_type": "repo"},
                {"tumbler_prefix": "1.2", "name": "o2", "owner_type": "curator"},
            ],
            documents=[
                {"tumbler": "1.1.1", "title": "doc1"},
                {"tumbler": "1.1.2", "title": "doc2"},
                {"tumbler": "1.2.1", "title": "doc3"},
            ],
            links=[
                {"from_tumbler": "1.1.1", "to_tumbler": "1.2.1", "link_type": "cites"},
            ],
            meta=[
                {"key": "last_applied_event_offset", "value": "42"},
            ],
        )
        counts = count_source_rows(db_path)
        assert counts["owners"] == 2
        assert counts["documents"] == 3
        assert counts["links"] == 1
        assert counts["_meta"] == 1


class TestMigrateCatalogMocked:
    """FK insertion order + mock call accounting."""

    def _make_mock_client(self):
        client = MagicMock()
        client._post.return_value = None
        return client

    def test_fk_insertion_order_owners_before_documents(self):
        """owners are imported (via _post /import/owner) before documents."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[{"tumbler_prefix": "1.1", "name": "owner1", "owner_type": "repo"}],
            documents=[{"tumbler": "1.1.1", "title": "doc1"}],
        )
        client = self._make_mock_client()
        call_paths: list[str] = []

        def track_post(path: str, payload: dict | None = None) -> None:
            call_paths.append(path)
            return None

        client._post.side_effect = track_post
        migrate_catalog(db_path, client)

        # owner import must precede document import
        owner_idx = next(
            (i for i, p in enumerate(call_paths) if p == "/import/owner"), None
        )
        doc_idx = next(
            (i for i, p in enumerate(call_paths) if p == "/import/document"), None
        )
        assert owner_idx is not None, "/import/owner call not found"
        assert doc_idx is not None, "/import/document call not found"
        assert owner_idx < doc_idx, (
            f"owners must be imported before documents; "
            f"owner_idx={owner_idx}, doc_idx={doc_idx}"
        )

    def test_fk_insertion_order_documents_before_chunks(self):
        """documents are imported before document_chunks (cross-store FK)."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[{"tumbler_prefix": "1.1", "name": "r", "owner_type": "repo"}],
            documents=[{"tumbler": "1.1.1", "title": "d"}],
            chunks=[
                {"doc_id": "1.1.1", "position": 0, "chash": "a" * 32},
            ],
        )
        client = self._make_mock_client()
        call_paths: list[str] = []

        def track_post(path: str, payload: dict | None = None) -> None:
            call_paths.append(path)
            return None

        client._post.side_effect = track_post
        migrate_catalog(db_path, client)

        doc_idx = next(
            (i for i, p in enumerate(call_paths) if p == "/import/document"), None
        )
        chunk_idx = next(
            (i for i, p in enumerate(call_paths) if p == "/import/chunk"), None
        )
        assert doc_idx is not None
        assert chunk_idx is not None
        assert doc_idx < chunk_idx, (
            "documents must be imported before chunks (FK constraint)"
        )

    def test_meta_table_skipped(self):
        """The _meta table is skipped; no /import/meta call is made."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            meta=[
                {"key": "last_applied_event_offset", "value": "999"},
                {"key": "header_hash", "value": "deadbeef"},
            ],
        )
        client = self._make_mock_client()
        migrate_catalog(db_path, client)

        # No call to /import/meta
        for c in client._post.call_args_list:
            path = c.args[0] if c.args else c.kwargs.get("path", "")
            assert path != "/import/meta", (
                f"_meta table must be skipped; got /import/meta call with {c}"
            )

    def test_returns_expected_keys(self):
        """migrate_catalog returns dict with per-table keys."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog()
        client = self._make_mock_client()
        results = migrate_catalog(db_path, client)

        for key in ("owners", "documents", "collections", "document_chunks", "links", "catalog_meta"):
            assert key in results, f"missing key in results: {key!r}"
            assert "read" in results[key]
            assert "written" in results[key]

    def test_empty_catalog_returns_zeros(self):
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog()
        client = self._make_mock_client()
        results = migrate_catalog(db_path, client)

        for table in ("owners", "documents", "collections", "links"):
            assert results[table]["read"] == 0
            assert results[table]["written"] == 0

    def test_chunks_grouped_by_doc_id(self):
        """Multiple chunks for the same doc_id are posted in one /import/chunk call."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[{"tumbler_prefix": "1.1", "name": "r", "owner_type": "repo"}],
            documents=[{"tumbler": "1.1.1", "title": "d"}],
            chunks=[
                {"doc_id": "1.1.1", "position": 0, "chash": "a" * 32},
                {"doc_id": "1.1.1", "position": 1, "chash": "b" * 32},
                {"doc_id": "1.1.1", "position": 2, "chash": "c" * 32},
            ],
        )
        client = self._make_mock_client()
        chunk_calls: list[dict] = []

        def track_post(path: str, payload: dict | None = None) -> None:
            if path == "/import/chunk":
                chunk_calls.append(payload or {})
            return None

        client._post.side_effect = track_post
        migrate_catalog(db_path, client)

        # Should be exactly 1 chunk call (all 3 rows for doc_id "1.1.1")
        assert len(chunk_calls) == 1, (
            f"Expected 1 /import/chunk call for 3 chunks of one doc, got {len(chunk_calls)}"
        )
        assert chunk_calls[0]["doc_id"] == "1.1.1"
        assert len(chunk_calls[0]["rows"]) == 3

    def test_document_chunks_import_retries_transient_502(self, monkeypatch):
        """RDR-178 Gap 3 (nexus-ob4vc): the document_chunks manifest write used
        to call client._post(...) directly with NO retry wrapper at all — the
        genuine bypassed call site behind the 270-row catalog manifest loss on
        2026-07-01. It must now survive a transient 502 exactly like the other
        table imports."""
        import httpx

        import nexus.retry as retry
        from nexus.db.t2.catalog_etl import migrate_catalog

        monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

        db_path = _make_source_catalog(
            owners=[{"tumbler_prefix": "1.1", "name": "r", "owner_type": "repo"}],
            documents=[{"tumbler": "1.1.1", "title": "d"}],
            chunks=[{"doc_id": "1.1.1", "position": 0, "chash": "a" * 32}],
        )

        attempts = {"n": 0}

        def flaky_post(path: str, payload: dict | None = None):
            if path != "/import/chunk":
                return None
            attempts["n"] += 1
            if attempts["n"] < 3:
                req = httpx.Request("POST", "http://svc/import/chunk")
                resp = httpx.Response(502, request=req)
                raise httpx.HTTPStatusError("HTTP 502", request=req, response=resp)
            return None

        client = self._make_mock_client()
        client._post.side_effect = flaky_post
        migrate_catalog(db_path, client)

        assert attempts["n"] == 3  # retried twice, succeeded on the 3rd

    def test_source_sqlite_unchanged_after_etl(self):
        """SQLite source must not be modified (copy-not-move)."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[{"tumbler_prefix": "1.1", "name": "r", "owner_type": "repo"}],
            documents=[
                {"tumbler": "1.1.1", "title": "T1"},
                {"tumbler": "1.1.2", "title": "T2"},
            ],
            links=[
                {"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"},
            ],
        )
        client = self._make_mock_client()
        migrate_catalog(db_path, client)

        # Verify source is unchanged
        conn = sqlite3.connect(str(db_path))
        owner_count = conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        link_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        conn.close()

        assert owner_count == 1, f"owners count changed: expected 1, got {owner_count}"
        assert doc_count == 2, f"docs count changed: expected 2, got {doc_count}"
        assert link_count == 1, f"links count changed: expected 1, got {link_count}"


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests — real Java service + hermetic Postgres 16
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def cat_etl_pg_instance():
    """Hermetic PostgreSQL 16 instance for catalog ETL integration tests.

    net63 pattern (same as test_http_catalog_integration.py / pg_instance):
    - NO schema pre-application: Liquibase owns the full DDL lifecycle.
      The JAR runs SchemaMigrator at startup and applies the full catalog-*
      changelog before binding the HTTP port.
    - nexus_svc role MUST exist before the JAR starts because
      grants-nexus-svc.xml (runAlways=true) issues GRANT ... TO nexus_svc.
      SERVICE_ROLES_SQL creates it as a NOSUPERUSER NOBYPASSRLS login role.
    """
    if not _ALL_PREREQS:
        pytest.skip("missing jar or pg16 binaries")

    pgdata = tempfile.mkdtemp(prefix="nexus_cat_etl_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}",
             "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexus_cat_etltest"],
            check=True, capture_output=True,
        )

        pg = {"port": pg_port, "dbname": "nexus_cat_etltest", "user": pg_user, "pgdata": pgdata}

        # Create nexus_svc BEFORE starting the JAR (grants-nexus-svc.xml requires it).
        # No _BOOTSTRAP_SQL: Liquibase creates the full catalog_* schema at JAR startup.
        _psql(pg, SERVICE_ROLES_SQL)

        yield pg
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def cat_etl_service(cat_etl_pg_instance):
    """Java service against the hermetic Postgres for catalog ETL tests.

    Two-role DB configuration (same as test_http_catalog_integration.py / service):
    - NX_DB_ADMIN_* = OS superuser (trust auth): Liquibase runs DDL as this role.
    - NX_DB_USER/PASS = nexus_svc (NOSUPERUSER NOBYPASSRLS): HikariCP app pool
      connects as this role so FORCE ROW LEVEL SECURITY actually applies.
      nexus_svc is granted DML by grants-nexus-svc.xml (runAlways changeset).

    NX_CHROMA_PATH: isolated temp dir to avoid opening the dev Chroma instance
    at ~/.config/nexus/chroma (may have incompatible SQLite state).
    """
    svc_port = _free_port()
    token = "cat-etl-bearer-token-abc123"
    pg = cat_etl_pg_instance
    pg_user = pg["user"]
    pg_jdbc = f"jdbc:postgresql://127.0.0.1:{pg['port']}/{pg['dbname']}"
    chroma_data = tempfile.mkdtemp(prefix="nexus-cat-etl-chroma-")

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        # App pool: nexus_svc (NOSUPERUSER NOBYPASSRLS) — FORCE RLS applies.
        "NX_DB_URL":  pg_jdbc,
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "3",
        # Migration pool: OS superuser — has DDL rights for full Liquibase run.
        "NX_DB_ADMIN_URL":  pg_jdbc,
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
        # Isolated Chroma: avoids version-mismatch panics against dev instance.
        "NX_CHROMA_PATH": chroma_data,
    }
    env.pop("NX_STORAGE_BACKEND", None)
    env.pop("NX_STORAGE_BACKEND_CATALOG", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=40.0)
        yield f"http://127.0.0.1:{svc_port}", token, proc
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(chroma_data, ignore_errors=True)


@pytest.fixture(scope="module")
def cat_etl_client(cat_etl_service):
    """HttpCatalogClient connected to the real ETL test service."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    base_url, token, _ = cat_etl_service
    _saved_token = os.environ.get("NX_SERVICE_TOKEN")
    os.environ["NX_SERVICE_TOKEN"] = token
    c = HttpCatalogClient(base_url=base_url, tenant="default", _token=token)
    yield c
    c.close()
    # Restore: a leaked module token poisons later env-resolving modules (nexus-edwlp).
    if _saved_token is None:
        os.environ.pop("NX_SERVICE_TOKEN", None)
    else:
        os.environ["NX_SERVICE_TOKEN"] = _saved_token


@pytest.mark.integration
@_SKIP_INTEGRATION
class TestCatalogEtlIntegration:
    """Full ETL tests: real Java service + hermetic Postgres 16."""

    def test_exact_owner_document_link_counts(self, cat_etl_client):
        """EXACT row counts: owners/documents/links in PG == SQLite source."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        N_OWNERS = 2
        N_DOCS   = 3
        N_LINKS  = 2

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.1", "name": "owner-cnt-1", "owner_type": "repo",
                 "repo_root": "/repo1"},
                {"tumbler_prefix": "1.2", "name": "owner-cnt-2", "owner_type": "curator"},
            ],
            documents=[
                {"tumbler": "1.1.1", "title": "doc-cnt-A", "content_type": "paper",
                 "source_uri": "file:///repo1/doc_a.pdf", "source_mtime": 1717700000.0},
                {"tumbler": "1.1.2", "title": "doc-cnt-B", "content_type": "code"},
                {"tumbler": "1.2.1", "title": "doc-cnt-C", "content_type": "rdr"},
            ],
            links=[
                {"from_tumbler": "1.1.1", "to_tumbler": "1.2.1",
                 "link_type": "cites", "created_by": "user"},
                {"from_tumbler": "1.1.2", "to_tumbler": "1.1.1",
                 "link_type": "implements", "created_by": "developer"},
            ],
        )

        results = migrate_catalog(db_path, cat_etl_client)

        assert results["owners"]["read"]    == N_OWNERS
        assert results["owners"]["written"] == N_OWNERS
        assert results["documents"]["read"]    == N_DOCS
        assert results["documents"]["written"] == N_DOCS
        assert results["links"]["read"]    == N_LINKS
        assert results["links"]["written"] == N_LINKS

        # Verify against PG owner-scoped state, NOT global stats() — the module-scoped
        # client accumulates rows across tests, so a global doc_count assertion would be
        # order-dependent and pass vacuously. by_owner() is scoped to these owners.
        owner1_docs = cat_etl_client.by_owner("1.1")
        owner2_docs = cat_etl_client.by_owner("1.2")
        assert len(owner1_docs) == 2, f"owner 1.1 must have 2 docs, got {len(owner1_docs)}"
        assert len(owner2_docs) == 1, f"owner 1.2 must have 1 doc, got {len(owner2_docs)}"
        assert {str(e.tumbler) for e in owner1_docs} == {"1.1.1", "1.1.2"}
        assert {str(e.tumbler) for e in owner2_docs} == {"1.2.1"}

        # Links round-tripped exactly through the real service (not just ETL's own count).
        cites = cat_etl_client.links_from("1.1.1", link_type="cites")
        impls = cat_etl_client.links_from("1.1.2", link_type="implements")
        assert [str(l.to_tumbler) for l in cites] == ["1.2.1"]
        assert [str(l.to_tumbler) for l in impls] == ["1.1.1"]

    def test_document_field_round_trip(self, cat_etl_client):
        """Spot-check: a document's fields come back correctly from PG after ETL."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        metadata = {"source": "test", "tags": ["etl", "catalog"]}
        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.3", "name": "owner-rt", "owner_type": "repo"},
            ],
            documents=[
                {
                    "tumbler": "1.3.1",
                    "title": "ETL Round-Trip Doc",
                    "author": "Test Author",
                    "year": 2026,
                    "content_type": "paper",
                    "file_path": "docs/roundtrip.pdf",
                    "corpus": "test-corpus",
                    "physical_collection": "knowledge__test__voyage-context-3__v1",
                    "chunk_count": 5,
                    "head_hash": "cafebabe1234",
                    "source_mtime": 1717700001.0,
                    "source_uri": "file:///docs/roundtrip.pdf",
                    "bib_year": 2025,
                    "bib_authors": "Hildebrand, H.",
                    "bib_venue": "ICML 2025",
                    "bib_citation_count": 42,
                    "bib_doi": "10.1234/test.doi",
                    "metadata": metadata,
                },
            ],
        )
        migrate_catalog(db_path, cat_etl_client)

        entry = cat_etl_client.resolve("1.3.1")
        assert entry is not None, "document 1.3.1 not found in PG after ETL"
        assert entry.title == "ETL Round-Trip Doc"
        assert entry.author == "Test Author"
        assert entry.year == 2026
        assert entry.content_type == "paper"
        assert entry.file_path == "docs/roundtrip.pdf"
        assert entry.corpus == "test-corpus"
        assert entry.physical_collection == "knowledge__test__voyage-context-3__v1"
        assert entry.chunk_count == 5
        assert entry.source_mtime == 1717700001.0
        assert entry.source_uri == "file:///docs/roundtrip.pdf"

    def test_link_round_trip(self, cat_etl_client):
        """Spot-check: a link's fields come back correctly from PG after ETL."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.4", "name": "owner-lrt", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.4.1", "title": "from-doc"},
                {"tumbler": "1.4.2", "title": "to-doc"},
            ],
            links=[
                {
                    "from_tumbler": "1.4.1",
                    "to_tumbler":   "1.4.2",
                    "link_type":    "cites",
                    "from_span":    "chash:deadbeef00000000deadbeef00000000",
                    "to_span":      "",
                    "created_by":   "developer",
                    "created_at":   "2026-05-01T10:00:00Z",
                },
            ],
        )
        migrate_catalog(db_path, cat_etl_client)

        links = cat_etl_client.links_from("1.4.1", link_type="cites")
        assert len(links) == 1, (
            f"Expected 1 cites link from 1.4.1, got {len(links)}"
        )
        lnk = links[0]
        assert str(lnk.from_tumbler) == "1.4.1"
        assert str(lnk.to_tumbler) == "1.4.2"
        assert lnk.link_type == "cites"
        assert lnk.from_span == "chash:deadbeef00000000deadbeef00000000"
        assert lnk.created_by == "developer"

    def test_tenant_stamping(self, cat_etl_client):
        """All migrated rows use tenant_id='default' (RLS enforced)."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.5", "name": "owner-ts", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.5.1", "title": "tenant-stamp-doc"},
            ],
        )
        migrate_catalog(db_path, cat_etl_client)

        # cat_etl_client is configured with tenant="default"
        # If tenant stamping was broken, resolve() would return None
        entry = cat_etl_client.resolve("1.5.1")
        assert entry is not None, (
            "Document not visible under tenant='default' — tenant stamping failed"
        )

    def test_idempotency_second_run_no_extra_rows(self, cat_etl_client):
        """Running ETL twice produces no duplicate rows."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.6", "name": "owner-idem", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.6.1", "title": "idem-doc-1", "chunk_count": 2},
                {"tumbler": "1.6.2", "title": "idem-doc-2"},
            ],
            chunks=[
                {"doc_id": "1.6.1", "position": 0, "chash": "c" * 32,
                 "chunk_index": 0, "line_start": 1, "line_end": 40},
                {"doc_id": "1.6.1", "position": 1, "chash": "d" * 32,
                 "chunk_index": 1, "line_start": 41, "line_end": 80},
            ],
            links=[
                {"from_tumbler": "1.6.1", "to_tumbler": "1.6.2",
                 "link_type": "relates"},
            ],
        )

        r1 = migrate_catalog(db_path, cat_etl_client)
        r2 = migrate_catalog(db_path, cat_etl_client)

        # Both runs must report the same read counts
        assert r1["owners"]["read"] == r2["owners"]["read"]
        assert r1["documents"]["read"] == r2["documents"]["read"]
        assert r1["document_chunks"]["read"] == r2["document_chunks"]["read"]
        assert r1["links"]["read"] == r2["links"]["read"]

        # Documents visible once (not duplicated)
        docs = cat_etl_client.by_owner("1.6")
        idem_docs = [d for d in docs if str(d.tumbler).startswith("1.6.")]
        assert len(idem_docs) == 2, (
            f"Expected 2 docs for owner 1.6 after 2 ETL runs, got {len(idem_docs)} — "
            "idempotency broken"
        )

        # Chunk manifest visible once after 2 runs (ON CONFLICT DO NOTHING is
        # idempotent — re-import must not duplicate nor truncate the manifest).
        manifest = cat_etl_client.get_manifest("1.6.1")
        assert len(manifest) == 2, (
            f"Expected 2 manifest rows for 1.6.1 after 2 ETL runs, got {len(manifest)} — "
            "chunk idempotency broken"
        )
        assert {r.chash for r in manifest} == {"c" * 32, "d" * 32}

        # Links visible once
        links = cat_etl_client.links_from("1.6.1", link_type="relates")
        assert len(links) == 1, (
            f"Expected 1 link after 2 ETL runs, got {len(links)} — link idempotency broken"
        )

    def test_fk_ordering_link_refs_doc(self, cat_etl_client):
        """FK ordering: a link referencing a doc succeeds because doc is inserted first.

        This is the critical FK-ordering proof: if we inserted links before
        documents the catalog service would reject the import (FK violation).
        """
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.7", "name": "owner-fk", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.7.1", "title": "fk-doc-from"},
                {"tumbler": "1.7.2", "title": "fk-doc-to"},
            ],
            links=[
                # This link references two documents; it must land AFTER both docs
                {
                    "from_tumbler": "1.7.1",
                    "to_tumbler":   "1.7.2",
                    "link_type":    "cites",
                    "created_by":   "user",
                },
            ],
        )

        results = migrate_catalog(db_path, cat_etl_client)

        # Both docs and the link must be written (not rejected)
        assert results["documents"]["written"] == 2
        assert results["links"]["written"] == 1, (
            f"Link referencing a doc must succeed after FK-ordered ETL; "
            f"got links.written={results['links']['written']}"
        )

        # Verify link is queryable
        links = cat_etl_client.links_from("1.7.1")
        fk_link = next(
            (l for l in links if str(l.to_tumbler) == "1.7.2"), None
        )
        assert fk_link is not None, "FK-ordered link not found in PG after ETL"

    def test_collections_migrated(self, cat_etl_client):
        """Collections land in catalog_collections after ETL."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.8", "name": "owner-coll", "owner_type": "repo"},
            ],
            collections=[
                {
                    "name": "code__owner-coll__voyage-code-3__v1",
                    "content_type": "code",
                    "owner_id": "1.8",
                    "embedding_model": "voyage-code-3",
                    "model_version": "v1",
                    "created_at": "2026-04-01T00:00:00Z",
                },
            ],
        )
        results = migrate_catalog(db_path, cat_etl_client)

        assert results["collections"]["read"] == 1
        assert results["collections"]["written"] == 1

        coll = cat_etl_client.get_collection("code__owner-coll__voyage-code-3__v1")
        assert coll is not None, "collection not found in PG after ETL"
        assert coll["content_type"] == "code"
        assert coll["owner_id"] == "1.8"

    def test_chunks_migrated_with_correct_doc_id(self, cat_etl_client):
        """document_chunks land with the correct doc_id in PG."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.9", "name": "owner-chk", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.9.1", "title": "chunked-doc", "chunk_count": 2},
            ],
            chunks=[
                {"doc_id": "1.9.1", "position": 0, "chash": "a" * 32,
                 "chunk_index": 0, "line_start": 1, "line_end": 50},
                {"doc_id": "1.9.1", "position": 1, "chash": "b" * 32,
                 "chunk_index": 1, "line_start": 51, "line_end": 100},
            ],
        )
        results = migrate_catalog(db_path, cat_etl_client)

        assert results["document_chunks"]["read"] == 2
        assert results["document_chunks"]["written"] == 2

        manifest = cat_etl_client.get_manifest("1.9.1")
        assert len(manifest) == 2, (
            f"Expected 2 manifest rows for doc 1.9.1, got {len(manifest)}"
        )
        chashes = {r.chash for r in manifest}
        assert "a" * 32 in chashes
        assert "b" * 32 in chashes

    def test_meta_table_skipped_in_catalog_meta(self, cat_etl_client):
        """The _meta table is skipped; catalog_meta stays empty."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            meta=[
                {"key": "last_applied_event_offset", "value": "100"},
                {"key": "header_hash", "value": "abc123"},
            ],
        )
        results = migrate_catalog(db_path, cat_etl_client)

        assert results["catalog_meta"]["read"] == 0
        assert results["catalog_meta"]["written"] == 0
        assert results["catalog_meta"]["skipped"] == 2

    def test_copy_not_move_sqlite_unchanged(self, cat_etl_client):
        """SQLite source is unchanged after ETL (copy-not-move)."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.10", "name": "owner-cnm", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.10.1", "title": "cnm-doc-1"},
                {"tumbler": "1.10.2", "title": "cnm-doc-2"},
            ],
        )
        migrate_catalog(db_path, cat_etl_client)

        conn = sqlite3.connect(str(db_path))
        owner_count = conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
        doc_count   = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        conn.close()

        assert owner_count == 1, f"owners count changed post-ETL: {owner_count}"
        assert doc_count   == 2, f"docs count changed post-ETL: {doc_count}"

    def test_next_seq_reconciled_no_tumbler_collision_post_cutover(self, cat_etl_client):
        """REGRESSION (substantive-critic Critical): after ETL, registering a NEW
        document must NOT collide with a migrated tumbler.

        Without next_seq reconciliation every imported owner lands with next_seq=0,
        so the first registerDocument allocates ``prefix.1`` — which already exists —
        and the bare INSERT throws a unique violation (DataAccessException). The fix
        derives next_seq = max(migrated doc sequence) and GREATEST-merges it on the
        owner, so the next allocation is ``prefix.{max+1}``.
        """
        from nexus.db.t2.catalog_etl import migrate_catalog

        # Owner 1.20 (unused elsewhere — module-scoped client) with docs at seq 1, 2.
        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.20", "name": "owner-seq", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.20.1", "title": "seq-doc-1"},
                {"tumbler": "1.20.2", "title": "seq-doc-2"},
            ],
        )
        results = migrate_catalog(db_path, cat_etl_client)

        # The reconcile pass ran for exactly this owner (1 owner with docs).
        assert results["next_seq_reconcile"]["written"] == 1

        # Register a genuinely new document for the migrated owner. With next_seq
        # left at 0 this would raise (collision on 1.20.1); reconciled it gets 1.20.3.
        new_tumbler = cat_etl_client.register(
            "1.20",
            "post-cutover new doc",
            content_type="paper",
            source_uri="file:///repo20/brand_new.pdf",
        )
        assert str(new_tumbler) == "1.20.3", (
            f"new doc must allocate 1.20.3 (max migrated seq + 1), got {new_tumbler}"
        )

        # And the migrated documents are untouched.
        docs = {str(e.tumbler) for e in cat_etl_client.by_owner("1.20")}
        assert docs == {"1.20.1", "1.20.2", "1.20.3"}

    def test_next_seq_uses_jsonl_high_water_no_tumbler_reuse(self, cat_etl_client):
        """REGRESSION (substantive-critic Significant): a source catalog with deleted
        documents must NOT reuse a deleted tumbler slot after migration.

        The high-water mark lives in owners.jsonl and is never decremented on delete.
        Deriving next_seq from surviving documents alone would allocate into the gap
        left by deletions, reusing an address that links / T3 chunks / external tools
        may still reference. The ETL must floor next_seq at jsonl_next_seq - 1.
        """
        from nexus.db.t2.catalog_etl import migrate_catalog

        # Owner 1.21: docs 1..5 were assigned; 2, 4, 5 later deleted+compacted, so
        # only 1.21.1 and 1.21.3 survive in .catalog.db. owners.jsonl high-water
        # next_seq=6 (next-to-assign) records that 5 was the last allocated.
        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.21", "name": "owner-gap", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.21.1", "title": "gap-doc-1"},
                {"tumbler": "1.21.3", "title": "gap-doc-3"},
            ],
            owner_high_water={"1.21": 6},
        )
        migrate_catalog(db_path, cat_etl_client)

        # Next allocation must be 1.21.6 (high-water), NOT 1.21.2/1.21.4 (reused gap).
        new_tumbler = cat_etl_client.register(
            "1.21",
            "post-cutover after deletions",
            source_uri="file:///repo21/new.pdf",
        )
        assert str(new_tumbler) == "1.21.6", (
            f"must allocate 1.21.6 from the high-water mark, not reuse a deleted "
            f"slot — got {new_tumbler}"
        )

    def test_next_seq_greatest_merge_no_downgrade_on_rerun(self, cat_etl_client):
        """REGRESSION (substantive-critic Significant): a second ETL run after the live
        service has advanced next_seq must NOT downgrade the counter (GREATEST merge).

        Migration rehearsal-then-cutover is operationally common: run ETL, the service
        takes live writes, then re-run ETL. The re-import sends a lower floor; the
        service GREATEST keeps the higher live value.
        """
        from nexus.db.t2.catalog_etl import migrate_catalog

        db_path = _make_source_catalog(
            owners=[
                {"tumbler_prefix": "1.22", "name": "owner-rerun", "owner_type": "repo"},
            ],
            documents=[
                {"tumbler": "1.22.1", "title": "rr-doc-1"},
                {"tumbler": "1.22.2", "title": "rr-doc-2"},
            ],
        )
        migrate_catalog(db_path, cat_etl_client)

        # Live registration advances next_seq past the ETL floor (2 -> 3 assigned).
        first = cat_etl_client.register("1.22", "live doc", source_uri="file:///r22/a.pdf")
        assert str(first) == "1.22.3"

        # Re-run ETL: reconcile sends next_seq=2; GREATEST(current=3, 2) keeps 3.
        migrate_catalog(db_path, cat_etl_client)

        # The live-advanced counter is preserved — next alloc is 1.22.4, not a re-1.22.3.
        second = cat_etl_client.register("1.22", "after rerun", source_uri="file:///r22/b.pdf")
        assert str(second) == "1.22.4", (
            f"GREATEST merge must not downgrade a live-advanced next_seq — got {second}"
        )
