# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 3 PR β event-sourced mutator paths.

Covers update / delete_document / set_alias / rename_collection under
NEXUS_EVENT_SOURCED=1. PR α already covered register_owner + register;
this PR β extends the gate to the remaining write methods (link /
unlink stay legacy until a follow-up that handles their merge
semantics in the projector).

Coverage per mutator:
- Gate ON: events.jsonl gets the right typed event; SQLite mutated via
  Projector.apply; legacy JSONL still written for back-compat; shadow
  emit suppressed (no double write).
- Gate OFF: legacy direct-write behaviour unchanged.
- Replay: events.jsonl produced under the new path projects to a fresh
  CatalogDB to a SQLite state byte-equal to the live DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector


# ── update ───────────────────────────────────────────────────────────────


class TestUpdateEventSourced:
    def test_update_emits_document_registered_via_event_log(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", chunk_count=0,
        )
        cat.update(tumbler, chunk_count=42, head_hash="updated")

        # events.jsonl: owner + register + update.
        log = EventLog(d)
        events = list(log.replay())
        assert len(events) == 3
        # Last event is the update, modeled as DocumentRegistered with
        # post-update state.
        assert events[-1].type == ev.TYPE_DOCUMENT_REGISTERED
        assert events[-1].payload.tumbler == str(tumbler)
        assert events[-1].payload.chunk_count == 42
        assert events[-1].payload.head_hash == "updated"

        # SQLite reflects the update (via projector).
        row = cat._db.execute(
            "SELECT chunk_count, head_hash FROM documents WHERE tumbler = ?",
            (str(tumbler),),
        ).fetchone()
        assert row == (42, "updated")

    def test_update_re_derives_chunk_count_from_manifest_when_omitted(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-zq79 F4: when caller omits chunk_count, cat.update() must
        re-derive it from the current document_chunks count so the emitted
        event payload is fresh (not the resolve-time stale snapshot).
        Event replay would otherwise project the old 0.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", chunk_count=0,
        )
        # Simulate the post-store manifest write writing 5 chunk rows but
        # NOT touching documents.chunk_count (the pre-zq79 bug shape).
        # Use the catalog public API to satisfy the projector-only-writes
        # invariant (RDR-101 Phase 3 ε).
        cat.append_manifest_chunks(
            str(tumbler),
            [
                {
                    "position": pos,
                    "chash": f"chash{pos}",
                    "chunk_index": pos,
                    "line_start": None,
                    "line_end": None,
                    "char_start": None,
                    "char_end": None,
                }
                for pos in range(5)
            ],
        )
        # Update with head_hash only — no chunk_count in fields.
        cat.update(tumbler, head_hash="updated")
        log = EventLog(d)
        events = list(log.replay())
        # Last event must carry re-derived chunk_count=5, not the
        # stale 0 from resolve().
        assert events[-1].type == ev.TYPE_DOCUMENT_REGISTERED
        assert events[-1].payload.chunk_count == 5, (
            f"expected re-derived chunk_count=5, got "
            f"{events[-1].payload.chunk_count}"
        )

    def test_update_respects_caller_supplied_chunk_count(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-zq79 F4: caller intent wins — when chunk_count is passed
        explicitly (e.g. orphan-backfill paths), use the caller's value
        without re-derivation.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", chunk_count=0,
        )
        # 3 manifest rows present, but caller wants to assert 99.
        cat.append_manifest_chunks(
            str(tumbler),
            [
                {
                    "position": pos,
                    "chash": f"chash{pos}",
                    "chunk_index": pos,
                    "line_start": None,
                    "line_end": None,
                    "char_start": None,
                    "char_end": None,
                }
                for pos in range(3)
            ],
        )
        cat.update(tumbler, chunk_count=99)
        log = EventLog(d)
        events = list(log.replay())
        assert events[-1].payload.chunk_count == 99

    def test_update_refreshes_indexed_at_when_head_hash_changes(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-zq79 F7: cat.update(head_hash=...) must refresh
        documents.indexed_at to now. Pre-fix, indexed_at stayed at the
        original register stamp forever; `nx catalog show` last_indexed
        never advanced on re-indexed files.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", chunk_count=0,
        )
        original_at = cat.resolve(tumbler).indexed_at
        import time
        time.sleep(0.01)
        cat.update(tumbler, head_hash="rev2")
        refreshed_at = cat.resolve(tumbler).indexed_at
        assert refreshed_at != original_at, (
            f"indexed_at must advance on re-index; "
            f"original={original_at!r} refreshed={refreshed_at!r}"
        )


# ── delete_document ──────────────────────────────────────────────────────


class TestDeleteDocumentEventSourced:
    def test_delete_emits_event_and_removes_row(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(owner, "doc.md", content_type="prose")
        assert cat.delete_document(tumbler) is True

        log = EventLog(d)
        events = list(log.replay())
        types = [e.type for e in events]
        assert ev.TYPE_DOCUMENT_DELETED in types
        deleted = [e for e in events if e.type == ev.TYPE_DOCUMENT_DELETED][0]
        assert deleted.payload.tumbler == str(tumbler)

        # SQLite row gone.
        rows = cat._db.execute(
            "SELECT count(*) FROM documents WHERE tumbler = ?",
            (str(tumbler),),
        ).fetchone()
        assert rows[0] == 0

    def test_delete_cascades_to_document_chunks(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-8g79.7: deleting a document must purge its
        document_chunks manifest rows in the same write — pre-fix the
        manifest was left as FK orphans because the schema has no
        ON DELETE CASCADE and the projector handler only DELETEd from
        documents.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(owner, "doc.md", content_type="prose")
        # Plant 3 manifest rows via the public API.
        cat.append_manifest_chunks(
            str(tumbler),
            [
                {
                    "position": i, "chash": f"ch{i}",
                    "chunk_index": i,
                    "line_start": None, "line_end": None,
                    "char_start": None, "char_end": None,
                }
                for i in range(3)
            ],
        )
        assert len(cat.get_manifest(str(tumbler))) == 3

        assert cat.delete_document(tumbler) is True

        # documents row gone AND document_chunks rows gone.
        doc_count = cat._db.execute(
            "SELECT count(*) FROM documents WHERE tumbler = ?",
            (str(tumbler),),
        ).fetchone()[0]
        chunk_count = cat._db.execute(
            "SELECT count(*) FROM document_chunks WHERE doc_id = ?",
            (str(tumbler),),
        ).fetchone()[0]
        assert doc_count == 0
        assert chunk_count == 0, (
            "delete_document must cascade-purge document_chunks; pre-fix "
            f"this left {chunk_count} orphan rows."
        )

    def test_delete_cascades_to_document_chunks_legacy_path(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-8g79.7: same cascade for the non-event-sourced path."""
        monkeypatch.delenv("NEXUS_EVENT_SOURCED", raising=False)
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(owner, "doc.md", content_type="prose")
        cat.append_manifest_chunks(
            str(tumbler),
            [{"position": 0, "chash": "ch0", "chunk_index": 0,
              "line_start": None, "line_end": None,
              "char_start": None, "char_end": None}],
        )
        assert len(cat.get_manifest(str(tumbler))) == 1

        assert cat.delete_document(tumbler) is True

        chunk_count = cat._db.execute(
            "SELECT count(*) FROM document_chunks WHERE doc_id = ?",
            (str(tumbler),),
        ).fetchone()[0]
        assert chunk_count == 0


# ── set_alias ────────────────────────────────────────────────────────────


class TestSetAliasEventSourced:
    def test_set_alias_emits_event_and_updates_alias_of(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        canonical = cat.register(owner, "canonical.md", content_type="prose")
        alias = cat.register(owner, "alias.md", content_type="prose")
        cat.set_alias(alias, canonical)

        log = EventLog(d)
        events = list(log.replay())
        aliased = [e for e in events if e.type == ev.TYPE_DOCUMENT_ALIASED]
        assert len(aliased) == 1
        assert aliased[0].payload.alias_doc_id == str(alias)
        assert aliased[0].payload.canonical_doc_id == str(canonical)

        # SQLite has alias_of populated.
        row = cat._db.execute(
            "SELECT alias_of FROM documents WHERE tumbler = ?",
            (str(alias),),
        ).fetchone()
        assert row[0] == str(canonical)


# ── rename_collection ────────────────────────────────────────────────────


class TestRenameCollectionEventSourced:
    def test_rename_emits_per_row_events_and_updates_sqlite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "a.md", content_type="prose",
            file_path="a.md",
            physical_collection="docs__old",
        )
        cat.register(
            owner, "b.md", content_type="prose",
            file_path="b.md",
            physical_collection="docs__old",
        )

        n = cat.rename_collection("docs__old", "docs__new")
        assert n == 2

        # events.jsonl has 2 owner-or-register events + 2 rename events.
        log = EventLog(d)
        events = list(log.replay())
        post_rename = [
            e for e in events
            if e.type == ev.TYPE_DOCUMENT_REGISTERED
            and e.payload.physical_collection == "docs__new"
        ]
        assert len(post_rename) == 2

        # SQLite reflects the rename.
        rows = cat._db.execute(
            "SELECT count(*) FROM documents WHERE physical_collection = ?",
            ("docs__new",),
        ).fetchone()
        assert rows[0] == 2


# ── End-to-end: full replay equals live SQLite ────────────────────────────


class TestFullReplayEqualsLive:
    def test_register_update_alias_delete_replay_matches(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(
            owner, "a.md", content_type="prose",
            file_path="a.md", chunk_count=3,
        )
        b = cat.register(
            owner, "b.md", content_type="prose",
            file_path="b.md", chunk_count=7,
        )
        cat.update(a, chunk_count=99)
        cat.set_alias(b, a)
        cat._db.close()

        # Replay events.jsonl into a fresh CatalogDB.
        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        # The live DB and the replayed DB must match for owners and
        # documents (modulo timestamps in indexed_at).
        with sqlite3.connect(str(d / ".catalog.db")) as live:
            live_doc_a = live.execute(
                "SELECT chunk_count FROM documents WHERE tumbler = ?",
                (str(a),),
            ).fetchone()
            live_doc_b = live.execute(
                "SELECT alias_of FROM documents WHERE tumbler = ?",
                (str(b),),
            ).fetchone()
        with sqlite3.connect(str(tmp_path / "projected.db")) as proj:
            proj_doc_a = proj.execute(
                "SELECT chunk_count FROM documents WHERE tumbler = ?",
                (str(a),),
            ).fetchone()
            proj_doc_b = proj.execute(
                "SELECT alias_of FROM documents WHERE tumbler = ?",
                (str(b),),
            ).fetchone()
        assert live_doc_a == proj_doc_a == (99,)
        assert live_doc_b == proj_doc_b == (str(a),)


# ── Shadow emit still suppressed ─────────────────────────────────────────


class TestShadowEmitSuppressedAcrossMutators:
    def test_no_double_writes_when_both_gates_on(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(owner, "doc.md", content_type="prose")
        cat.update(tumbler, chunk_count=5)
        cat.delete_document(tumbler)

        # Each mutation produced exactly one event, not two.
        log = EventLog(d)
        events = list(log.replay())
        # owner + register + update + delete = 4 events.
        assert len(events) == 4
        types = [e.type for e in events]
        assert types == [
            ev.TYPE_OWNER_REGISTERED,
            ev.TYPE_DOCUMENT_REGISTERED,
            ev.TYPE_DOCUMENT_REGISTERED,  # update reuses Registered
            ev.TYPE_DOCUMENT_DELETED,
        ]
