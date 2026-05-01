# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the RDR-101 Phase 1+2 correctness review.

One test per finding so a future regression points at the exact
contract that was broken. Findings:

- C1: ``EventLog.replay`` must skip lines whose payload shape is
  invalid (e.g. ``payload: null``, ``payload: [1,2,3]``, ``v: "abc"``)
  rather than aborting the iterator.
- C2: ``Catalog.set_alias`` must hold the catalog directory flock for
  the duration of the JSONL append + shadow emit.
- C3: ``Catalog.unlink`` and ``bulk_unlink`` must commit SQLite before
  appending shadow events.
- C4: ``nx catalog doctor --replay-equality`` must exclude the links
  ``id`` autoincrement column by NAME, not by positional slice.
- I-events-1: ``EventLog.append`` must not silently coerce
  non-JSON-native payload values (raise TypeError instead).
- I-events-2: ``Event.from_dict`` must not crash on a non-dict
  payload (list, int, etc.).
- I-events-3 (covered by C1): ``v: "abc"`` does not crash replay.
- I-projector: ``_v0_document_deleted`` must use ``payload.tumbler`` to
  find the SQLite row when ``mint_doc_id=True`` (Phase 2) emits a
  UUID7 ``doc_id``.
- I-catalog-rename: ``Catalog.rename_collection`` must shadow-emit
  events so a replay reproduces the post-rename collection name.
- I-catalog-alias: ``Projector._v0_document_aliased`` must UPDATE the
  alias_of column so a replay of shadow-emitted ``set_alias`` events
  reproduces the alias graph.
- I-Phase2-force: ``synthesize-log --force`` must preserve doc_ids for
  tumblers that already appeared in the prior log (covered by the
  updated test in ``test_catalog_synthesize_log.py``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector


# ── C1 + I-events-2 + I-events-3: EventLog.replay broad exception ────────


class TestReplayResilientToBadPayloadShape:
    def test_replay_skips_null_payload_for_known_type(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir()
        log = EventLog(d)
        # Hand-craft a line with payload: null for a known type whose
        # payload class has required fields.
        log.path.write_text(
            json.dumps({
                "type": ev.TYPE_DOCUMENT_DELETED,
                "v": 1,
                "payload": None,
                "ts": "2026-04-30T00:00:00Z",
            }) + "\n"
            + json.dumps({
                "type": ev.TYPE_DOCUMENT_DELETED,
                "v": 1,
                "payload": {"doc_id": "x", "reason": "y"},
                "ts": "2026-04-30T00:01:00Z",
            }) + "\n"
        )
        # Pre-fix this aborted the iterator on the bad line; post-fix it
        # logs and skips the bad line then yields the good one.
        events = list(log.replay())
        assert len(events) == 1
        assert events[0].type == ev.TYPE_DOCUMENT_DELETED
        assert events[0].payload.doc_id == "x"

    def test_replay_skips_list_payload(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir()
        log = EventLog(d)
        log.path.write_text(
            json.dumps({
                "type": ev.TYPE_DOCUMENT_DELETED,
                "v": 1,
                "payload": [1, 2, 3],
                "ts": "t",
            }) + "\n"
            + json.dumps({
                "type": ev.TYPE_DOCUMENT_DELETED,
                "v": 1,
                "payload": {"doc_id": "good", "reason": "y"},
                "ts": "t",
            }) + "\n"
        )
        events = list(log.replay())
        # Bad line is logged + skipped; good line yields normally.
        assert len(events) == 1
        assert events[0].payload.doc_id == "good"

    def test_replay_does_not_crash_on_non_int_v(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir()
        log = EventLog(d)
        log.path.write_text(
            json.dumps({
                "type": ev.TYPE_DOCUMENT_DELETED,
                "v": "abc",
                "payload": {"doc_id": "x", "reason": "y"},
                "ts": "t",
            }) + "\n"
        )
        events = list(log.replay())
        assert len(events) == 1
        # Non-int v is coerced to 0 (synthesized) per the defensive
        # guard in from_dict.
        assert events[0].v == 0


# ── I-events-1: append raises on non-JSON-serializable payload ───────────


class TestAppendRaisesOnNonJsonPayload:
    def test_datetime_in_meta_raises_typeerror(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir()
        log = EventLog(d)
        bad_event = ev.make_event(ev.DocumentRegisteredPayload(
            doc_id="x", owner_id="1.1", content_type="prose",
            source_uri="file:///x.md", coll_id="c1",
            meta={"when": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        ))
        with pytest.raises(TypeError):
            log.append(bad_event)

    def test_path_in_meta_raises_typeerror(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir()
        log = EventLog(d)
        bad_event = ev.make_event(ev.DocumentRegisteredPayload(
            doc_id="x", owner_id="1.1", content_type="prose",
            source_uri="file:///x.md", coll_id="c1",
            meta={"path": Path("/foo/bar")},
        ))
        with pytest.raises(TypeError):
            log.append(bad_event)


# ── C2: set_alias holds the catalog flock ────────────────────────────────


class TestSetAliasHoldsLock:
    def test_set_alias_acquires_release_pattern(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Track lock acquisition / release calls.
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        canonical = cat.register(owner, "canon.md", content_type="prose")
        alias = cat.register(owner, "alias.md", content_type="prose")

        acquired: list[int] = []
        released: list[int] = []
        original_acquire = cat._acquire_lock
        original_release = cat._release_lock

        def tracked_acquire():
            fd = original_acquire()
            acquired.append(fd)
            return fd

        def tracked_release(fd):
            released.append(fd)
            return original_release(fd)

        monkeypatch.setattr(cat, "_acquire_lock", tracked_acquire)
        monkeypatch.setattr(cat, "_release_lock", tracked_release)

        # set_alias before this PR did NOT call _acquire_lock at all;
        # post-fix it calls acquire+release exactly once.
        before = len(acquired)
        cat.set_alias(alias, canonical)
        assert len(acquired) == before + 1, (
            "set_alias must acquire the catalog flock exactly once"
        )
        assert len(released) == before + 1, (
            "set_alias must release the catalog flock exactly once"
        )


# ── C3: unlink emits LinkDeleted AFTER db.commit ─────────────────────────


class TestUnlinkEventOrderingAfterCommit:
    def test_unlink_does_not_emit_before_commit(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Track the order of db.commit() and _emit_shadow_event() calls.
        Pre-fix, emits happened inside the per-row loop, BEFORE the
        loop's terminating commit. Post-fix, emits happen after the
        commit so a process crash leaves SQLite + events.jsonl
        consistent (or both reflecting the pre-delete state).

        PR ζ (nexus-o6aa.9.5) flipped NEXUS_EVENT_SOURCED default to
        ON; shadow emit is a no-op under ES so this test pins to the
        legacy path explicitly to exercise the ordering invariant.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        cat.link(a, b, link_type="cites", created_by="manual")

        sequence: list[str] = []
        original_commit = cat._db.commit
        original_emit = cat._emit_shadow_event

        def tracked_commit():
            sequence.append("commit")
            return original_commit()

        def tracked_emit(event):
            sequence.append(f"emit:{event.type}")
            return original_emit(event)

        monkeypatch.setattr(cat._db, "commit", tracked_commit)
        monkeypatch.setattr(cat, "_emit_shadow_event", tracked_emit)

        n = cat.unlink(a, b, link_type="cites")
        assert n == 1

        # Find the unlink-related sequence: the LinkDeleted emit must
        # land AFTER the commit that follows the DELETE.
        assert "commit" in sequence
        commit_idx = sequence.index("commit")
        link_deleted_idx = next(
            i for i, s in enumerate(sequence) if s == f"emit:{ev.TYPE_LINK_DELETED}"
        )
        assert link_deleted_idx > commit_idx, (
            f"LinkDeleted emitted before db.commit; sequence: {sequence}"
        )


# ── C4: doctor links snapshot strips id by NAME, not position ───────────


class TestDoctorLinksSnapshotByName:
    def test_snapshot_table_excludes_named_columns(self, tmp_path):
        from nexus.commands.catalog import _snapshot_table

        # Build a CatalogDB and seed one link.
        db = CatalogDB(tmp_path / ".catalog.db")
        db.execute(
            "INSERT OR REPLACE INTO owners "
            "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("1.1", "x", "repo", "h", "d", ""),
        )
        db.execute(
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata, source_mtime, source_uri) "
            "VALUES (?, ?, '', 0, '', '', '', '', 0, '', '', '{}', 0, '')",
            ("1.1.1", "doc1"),
        )
        db.execute(
            "INSERT INTO links "
            "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata) "
            "VALUES (?, ?, ?, '', '', 'manual', '', '{}')",
            ("1.1.1", "1.1.1", "self"),
        )
        db.commit()

        rows_with_id = _snapshot_table(db._conn, "links")
        rows_without_id = _snapshot_table(
            db._conn, "links", exclude_cols=["id"],
        )
        assert len(rows_with_id[0]) == len(rows_without_id[0]) + 1
        # The excluded column must be id (not a positional slice).
        cur = db._conn.execute("PRAGMA table_info(links)")
        cols = [r[1] for r in cur.fetchall()]
        id_index = cols.index("id")
        # rows_with_id[0][id_index] is the autoincrement; without_id
        # must have all columns except that one.
        with_minus_id = (
            rows_with_id[0][:id_index] + rows_with_id[0][id_index + 1:]
        )
        assert rows_without_id[0] == with_minus_id
        db.close()


# ── I-projector: _v0_document_deleted prefers tumbler ────────────────────


class TestV0DocumentDeletedUsesTumbler:
    def test_delete_with_uuid7_doc_id_falls_back_to_tumbler(self, tmp_path):
        """When mint_doc_id=True synthesizes a DocumentDeleted with
        doc_id=UUID7, the projector must find the row by tumbler. Pre-
        fix it issued WHERE tumbler=UUID7 and silently no-oped the
        deletion; tombstoned documents resurrected on replay.
        """
        db = CatalogDB(tmp_path / ".catalog.db")
        # Seed a document.
        db.execute(
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata, source_mtime, source_uri) "
            "VALUES ('1.7.42', 'doc', '', 0, '', '', '', '', 0, '', '', "
            "'{}', 0, '')"
        )
        db.commit()

        # Apply DocumentDeleted with UUID7 doc_id but tumbler populated.
        proj = Projector(db)
        proj.apply(ev.Event(
            type=ev.TYPE_DOCUMENT_DELETED, v=0,
            payload=ev.DocumentDeletedPayload(
                doc_id="019de2fc-2b2a-7bc4-9d84-6d0c17d2357e",
                reason="tombstone",
                tumbler="1.7.42",
            ),
            ts="t",
        ))
        db.commit()

        rows = db.execute(
            "SELECT tumbler FROM documents"
        ).fetchall()
        assert rows == [], (
            "Document should be deleted via tumbler join; pre-fix the "
            "WHERE tumbler=<UUID7> would not match and the row would "
            "survive."
        )
        db.close()


# ── I-catalog-rename: rename_collection emits events ─────────────────────


class TestRenameCollectionEmitsEvents:
    def test_rename_collection_with_shadow_emit_replays_correctly(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", physical_collection="docs__old",
        )
        n = cat.rename_collection("docs__old", "docs__new")
        assert n == 1

        # Replay events.jsonl into a fresh CatalogDB. The replayed row
        # must carry the NEW physical_collection.
        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        with sqlite3.connect(str(tmp_path / "projected.db")) as pc:
            row = pc.execute(
                "SELECT physical_collection FROM documents"
            ).fetchone()
        assert row is not None
        assert row[0] == "docs__new", (
            f"Pre-fix rename_collection emitted no events; replay would "
            f"see the OLD collection name. Got: {row[0]!r}"
        )


# ── I-catalog-alias: _v0_document_aliased UPDATEs alias_of ───────────────


class TestV0DocumentAliasedUpdatesColumn:
    def test_replayed_set_alias_event_populates_alias_of(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        # Seed two documents.
        for tumbler in ("1.1.1", "1.1.2"):
            db.execute(
                "INSERT INTO documents "
                "(tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, "
                "indexed_at, metadata, source_mtime, source_uri) "
                f"VALUES ('{tumbler}', 'doc', '', 0, '', '', '', '', 0, "
                "'', '', '{}', 0, '')"
            )
        db.commit()

        proj = Projector(db)
        proj.apply(ev.Event(
            type=ev.TYPE_DOCUMENT_ALIASED, v=0,
            payload=ev.DocumentAliasedPayload(
                alias_doc_id="1.1.2",
                canonical_doc_id="1.1.1",
            ),
            ts="t",
        ))
        db.commit()

        row = db.execute(
            "SELECT alias_of FROM documents WHERE tumbler = ?",
            ("1.1.2",),
        ).fetchone()
        assert row is not None
        assert row[0] == "1.1.1", (
            "Pre-fix the v0 DocumentAliased handler was a no-op; "
            "replaying a shadow-emitted set_alias event would leave "
            "alias_of empty in the projected SQLite."
        )
        db.close()


# ── End-to-end: shadow-emit set_alias replays correctly ──────────────────


class TestSetAliasShadowEmitReplay:
    """The combination of C2 (set_alias holds flock) and the alias
    projector fix means replaying a shadow-emit log of a set_alias
    sequence reproduces the alias graph in the projected SQLite."""

    def test_round_trip(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        canonical = cat.register(owner, "canon.md", content_type="prose")
        alias = cat.register(owner, "alias.md", content_type="prose")
        cat.set_alias(alias, canonical)

        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        with sqlite3.connect(str(tmp_path / "projected.db")) as pc:
            alias_row = pc.execute(
                "SELECT alias_of FROM documents WHERE tumbler = ?",
                (str(alias),),
            ).fetchone()
        assert alias_row is not None
        assert alias_row[0] == str(canonical)
