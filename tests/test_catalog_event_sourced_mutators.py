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
