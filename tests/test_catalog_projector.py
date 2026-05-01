# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nexus.catalog.projector + nexus.catalog.synthesizer.

Replay-equality is the binding test: ``Catalog.rebuild()`` reading JSONL
into SQLite and ``Projector.apply_all(synthesize_from_jsonl())`` driving
events into a fresh SQLite must produce identical row sets. If they
disagree, the synthesizer or the projector is wrong (RF-101-2).

Coverage:
- Replay-equality on a non-trivial fixture catalog
- Tombstones don't resurrect (RF-101-2 sub-case)
- Aliases preserve their alias_of column AND emit a paired
  ``DocumentAliased`` event (RF-101-2 sub-case)
- Idempotency: applying the same events twice is a no-op
- Unknown (type, v) pairs skip with the unknown-dispatch warning
- ``apply_all`` reports the number of events applied
- ``DocumentRenamed`` v: 0 updates source_uri without recreating the row
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.projector import Projector
from nexus.catalog.synthesizer import synthesize_from_jsonl


# ── Helpers ──────────────────────────────────────────────────────────────


def _dump_table(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Snapshot a table's rows in stable order for comparison."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if not cols:
        return []
    sort_cols = ", ".join(cols)
    rows = conn.execute(
        f"SELECT {sort_cols} FROM {table} ORDER BY {sort_cols}"
    ).fetchall()
    return rows


def _snapshot(db_path: Path) -> dict[str, list[tuple]]:
    """Snapshot owners + documents + links from a SQLite catalog db."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            "owners": _dump_table(conn, "owners"),
            "documents": _dump_table(conn, "documents"),
            "links": _dump_table(conn, "links"),
        }
    finally:
        conn.close()


def _appendl(path: Path, obj: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(obj) + "\n")


def _build_fixture_catalog(catalog_dir: Path) -> Path:
    """Build a JSONL-only catalog with several non-trivial cases.

    Returns the catalog directory so the caller can drive
    ``Catalog.rebuild()`` against it.
    """
    catalog_dir.mkdir(parents=True, exist_ok=True)
    owners = catalog_dir / "owners.jsonl"
    documents = catalog_dir / "documents.jsonl"
    links = catalog_dir / "links.jsonl"

    # Two owners
    _appendl(owners, {
        "owner": "1.1", "name": "nexus", "owner_type": "repo",
        "repo_hash": "571b8edd", "description": "Git repo: nexus",
        "repo_root": "/git/nexus", "next_seq": 99,
    })
    _appendl(owners, {
        "owner": "1.2", "name": "ART", "owner_type": "repo",
        "repo_hash": "8c2e74c0", "description": "Git repo: ART",
        "repo_root": "/git/ART", "next_seq": 4181,
    })

    # Document A: live
    _appendl(documents, {
        "tumbler": "1.1.1", "title": "doc-A.md", "author": "alice",
        "year": 2024, "content_type": "prose", "file_path": "doc-A.md",
        "corpus": "knowledge", "physical_collection": "docs__nexus-571b8edd",
        "chunk_count": 12, "head_hash": "aaaa1111",
        "indexed_at": "2026-04-01T00:00:00Z",
        "meta": {"author_email": "alice@example.org"},
        "source_mtime": 1714000000.0, "alias_of": "",
        "source_uri": "file:///git/nexus/doc-A.md",
    })

    # Document B: live with non-empty alias_of (alias of 1.1.1)
    _appendl(documents, {
        "tumbler": "1.1.2", "title": "doc-A.md", "author": "alice",
        "year": 2024, "content_type": "prose", "file_path": "doc-A.md",
        "corpus": "knowledge", "physical_collection": "docs__nexus-571b8edd",
        "chunk_count": 12, "head_hash": "aaaa1111",
        "indexed_at": "2026-04-01T01:00:00Z",
        "meta": {}, "source_mtime": 1714000010.0,
        "alias_of": "1.1.1",
        "source_uri": "file:///git/nexus/doc-A.md",
    })

    # Document C: registered then tombstoned (last write is _deleted=true)
    _appendl(documents, {
        "tumbler": "1.2.7", "title": "deleted.txt", "author": "",
        "year": 0, "content_type": "code", "file_path": "deleted.txt",
        "corpus": "", "physical_collection": "code__ART-8c2e74c0",
        "chunk_count": 3, "head_hash": "ccccccc",
        "indexed_at": "2026-04-02T00:00:00Z",
        "meta": {"reason": "still-here"}, "source_mtime": 1714100000.0,
        "alias_of": "",
        "source_uri": "file:///git/ART/deleted.txt",
    })
    _appendl(documents, {
        "tumbler": "1.2.7", "_deleted": True,
        "title": "deleted.txt", "author": "", "year": 0,
        "content_type": "code", "file_path": "deleted.txt",
        "corpus": "", "physical_collection": "code__ART-8c2e74c0",
        "chunk_count": 3, "head_hash": "ccccccc",
        "indexed_at": "2026-04-02T01:00:00Z",
        "meta": {"reason": "still-here"}, "source_mtime": 1714100000.0,
        "alias_of": "",
        "source_uri": "file:///git/ART/deleted.txt",
    })

    # Document D: empty source_uri (legacy paper row)
    _appendl(documents, {
        "tumbler": "1.2.42", "title": "Some Paper", "author": "Smith, J.",
        "year": 2020, "content_type": "paper", "file_path": "",
        "corpus": "papers", "physical_collection": "papers__art-curator",
        "chunk_count": 50, "head_hash": "ddddeeee",
        "indexed_at": "2026-04-03T00:00:00Z",
        "meta": {}, "source_mtime": 0.0, "alias_of": "",
        "source_uri": "",
    })

    # Two links: one live, one tombstoned
    _appendl(links, {
        "from_t": "1.1.1", "to_t": "1.2.42", "link_type": "cites",
        "from_span": "", "to_span": "", "created_by": "bib_enricher",
        "created_at": "2026-04-04T00:00:00Z", "meta": {},
        "_deleted": False,
    })
    _appendl(links, {
        "from_t": "1.1.1", "to_t": "1.1.2", "link_type": "implements",
        "from_span": "", "to_span": "", "created_by": "manual",
        "created_at": "2026-04-04T01:00:00Z", "meta": {},
        "_deleted": False,
    })
    _appendl(links, {
        "from_t": "1.1.1", "to_t": "1.1.2", "link_type": "implements",
        "from_span": "", "to_span": "", "created_by": "manual",
        "created_at": "2026-04-04T02:00:00Z", "meta": {},
        "_deleted": True,
    })

    return catalog_dir


def _project_via_log(
    catalog_dir: Path, fresh_db_path: Path,
) -> CatalogDB:
    """Drive synthesizer → projector against a fresh CatalogDB."""
    db = CatalogDB(fresh_db_path)
    Projector(db).apply_all(synthesize_from_jsonl(catalog_dir))
    return db


# ── Replay equality ──────────────────────────────────────────────────────


class TestReplayEquality:
    """The binding test for Phase 1.

    ``Catalog.rebuild()`` is the established JSONL → SQLite path; the
    projector is the new path. Both must produce the same SQLite for
    the same JSONL inputs.
    """

    def test_replay_equality_full_fixture(self, tmp_path):
        catalog_dir = _build_fixture_catalog(tmp_path / "catalog")

        # Path A: existing rebuild() via Catalog construction.
        cat_a = Catalog(catalog_dir, tmp_path / "rebuild.db")
        cat_a._db.commit()
        snap_a = _snapshot(tmp_path / "rebuild.db")

        # Path B: fresh CatalogDB driven by synthesizer + projector.
        db_b = _project_via_log(catalog_dir, tmp_path / "projected.db")
        db_b.close()
        snap_b = _snapshot(tmp_path / "projected.db")

        assert snap_a["owners"] == snap_b["owners"], (
            f"owners differ:\n  rebuild: {snap_a['owners']}\n  projected: {snap_b['owners']}"
        )
        assert snap_a["documents"] == snap_b["documents"], (
            f"documents differ:\n  rebuild: {snap_a['documents']}\n  projected: {snap_b['documents']}"
        )
        # ``links.id`` is an autoincrement PK; the projector's INSERT
        # order matches the synthesizer's collapse order, which matches
        # rebuild()'s iteration order, so ids should align. If this
        # assertion ever flakes we will need to compare links sans id.
        assert snap_a["links"] == snap_b["links"], (
            f"links differ:\n  rebuild: {snap_a['links']}\n  projected: {snap_b['links']}"
        )


# ── Sub-cases per RF-101-2 ───────────────────────────────────────────────


class TestTombstones:
    """Tombstoned documents must NOT resurrect via the projector."""

    def test_tombstoned_doc_not_in_projected_state(self, tmp_path):
        catalog_dir = _build_fixture_catalog(tmp_path / "catalog")
        db = _project_via_log(catalog_dir, tmp_path / "projected.db")

        rows = db.execute(
            "SELECT tumbler FROM documents WHERE tumbler = ?",
            ("1.2.7",),
        ).fetchall()
        assert rows == [], (
            "Tombstoned document 1.2.7 must not appear in the SQLite "
            "projection. RF-101-2 sub-case: synthesized tombstones produce "
            "DocumentRegistered + DocumentDeleted; the projector must apply "
            "the deletion."
        )
        db.close()


class TestAliases:
    """Alias rows project with alias_of populated AND emit DocumentAliased."""

    def test_alias_column_populated(self, tmp_path):
        catalog_dir = _build_fixture_catalog(tmp_path / "catalog")
        db = _project_via_log(catalog_dir, tmp_path / "projected.db")

        row = db.execute(
            "SELECT alias_of FROM documents WHERE tumbler = ?",
            ("1.1.2",),
        ).fetchone()
        assert row is not None, "alias row must exist in projection"
        assert row[0] == "1.1.1"
        db.close()

    def test_aliased_event_emitted_for_alias_rows(self, tmp_path):
        catalog_dir = _build_fixture_catalog(tmp_path / "catalog")
        events = list(synthesize_from_jsonl(catalog_dir))

        aliased = [
            e for e in events if e.type == ev.TYPE_DOCUMENT_ALIASED
        ]
        assert len(aliased) == 1, (
            f"Expected 1 DocumentAliased event for the alias row in the "
            f"fixture; got {len(aliased)}: {aliased!r}"
        )
        assert aliased[0].payload.alias_doc_id == "1.1.2"
        assert aliased[0].payload.canonical_doc_id == "1.1.1"

    def test_tombstoned_rows_emit_delete_event(self, tmp_path):
        catalog_dir = _build_fixture_catalog(tmp_path / "catalog")
        events = list(synthesize_from_jsonl(catalog_dir))

        deleted = [e for e in events if e.type == ev.TYPE_DOCUMENT_DELETED]
        assert len(deleted) == 1
        assert deleted[0].payload.doc_id == "1.2.7"
        assert deleted[0].payload.reason == "synthesized_from_tombstone"


# ── Idempotency ──────────────────────────────────────────────────────────


class TestIdempotency:
    """Applying the same event sequence twice is a no-op past the first run."""

    def test_double_apply_yields_same_state(self, tmp_path):
        catalog_dir = _build_fixture_catalog(tmp_path / "catalog")

        db = CatalogDB(tmp_path / "projected.db")
        proj = Projector(db)
        # First application
        proj.apply_all(synthesize_from_jsonl(catalog_dir))
        first = _snapshot(tmp_path / "projected.db")
        # Second application against the same DB
        proj.apply_all(synthesize_from_jsonl(catalog_dir))
        second = _snapshot(tmp_path / "projected.db")
        db.close()

        # Documents and owners use INSERT OR REPLACE → identical.
        assert first["owners"] == second["owners"]
        assert first["documents"] == second["documents"]
        # Links use INSERT OR IGNORE on the (from, to, type) UNIQUE
        # index → identical row count.
        assert len(first["links"]) == len(second["links"])


# ── Forward compat ───────────────────────────────────────────────────────


class TestUnknownDispatch:
    def test_unknown_type_is_skipped(self, tmp_path, caplog):
        db = CatalogDB(tmp_path / "x.db")
        proj = Projector(db)

        unknown = ev.Event(
            type="FutureEventType", v=1,
            payload={"a": 1}, ts="2026-04-30T12:00:00+00:00",
        )
        # No raise; no-op on SQLite.
        proj.apply(unknown)
        rows = db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 0
        db.close()

    def test_v1_known_type_raises(self, tmp_path):
        # Round-3 review: pre-fix _v1_unsupported logged a warning and
        # returned, which combined with the v=1 default in make_event
        # produced a silent-drop trap. Now it raises NotImplementedError
        # so dispatch on (type, 1) lands LOUDLY before any write commits
        # — the writer holds the flock and has not yet committed at the
        # point this raises, so SQLite + JSONL stay un-touched.
        import pytest as _pytest
        db = CatalogDB(tmp_path / "x.db")
        proj = Projector(db)

        e = ev.make_event(
            ev.DocumentDeletedPayload(doc_id="1.7.42", reason="x"),
            v=1,
        )
        with _pytest.raises(NotImplementedError, match="v: 1"):
            proj.apply(e)
        rows = db.execute(
            "SELECT count(*) FROM documents WHERE tumbler = ?",
            ("1.7.42",),
        ).fetchone()
        assert rows[0] == 0
        db.close()


# ── apply_all return value + DocumentRenamed ─────────────────────────────


class TestApplyAll:
    def test_apply_all_returns_count(self, tmp_path):
        catalog_dir = _build_fixture_catalog(tmp_path / "catalog")
        db = CatalogDB(tmp_path / "projected.db")
        events = list(synthesize_from_jsonl(catalog_dir))
        n = Projector(db).apply_all(iter(events))
        assert n == len(events)
        db.close()


class TestDocumentRenamed:
    def test_renamed_event_updates_source_uri(self, tmp_path):
        # Set up a registered doc, then apply a rename event.
        db = CatalogDB(tmp_path / "x.db")
        proj = Projector(db)
        proj.apply(ev.Event(
            type=ev.TYPE_DOCUMENT_REGISTERED, v=0,
            payload=ev.DocumentRegisteredPayload(
                doc_id="1.7.42", owner_id="1.7",
                content_type="code",
                source_uri="file:///old.py",
                coll_id="code__test",
                tumbler="1.7.42",
            ),
            ts="2026-04-30T00:00:00Z",
        ))
        proj.apply(ev.Event(
            type=ev.TYPE_DOCUMENT_RENAMED, v=0,
            payload=ev.DocumentRenamedPayload(
                doc_id="1.7.42",
                new_source_uri="file:///new.py",
            ),
            ts="2026-04-30T01:00:00Z",
        ))
        db.commit()

        row = db.execute(
            "SELECT source_uri FROM documents WHERE tumbler = ?",
            ("1.7.42",),
        ).fetchone()
        assert row is not None
        assert row[0] == "file:///new.py"
        db.close()
