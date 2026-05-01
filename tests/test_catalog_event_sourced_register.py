# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 3 PR α event-sourced register path.

Coverage:
- Gate parsing (PR ζ semantics, nexus-o6aa.9.5): 0/false/no/off → OFF;
  1/true/yes/on/unset/empty → ON. The default flipped to ON in PR ζ.
- Legacy path (gate explicitly OFF): legacy direct-write path runs
  unchanged.
- Gate ON: register_owner / register write events.jsonl FIRST, then
  project to SQLite via Projector.apply, then append to legacy JSONL
  for back-compat.
- Equivalence: a sequence of register() calls under the new path
  produces a SQLite state byte-equal to the same sequence under the
  legacy path.
- Replay: events.jsonl produced by the new path replays through a
  fresh CatalogDB to a state byte-equal to the live DB.
- Shadow emit suppression: when event-sourced is ON, shadow emit does
  NOT double-write (would otherwise produce duplicate events.jsonl
  lines).
- Idempotency: register() under the new path keeps the same idempotency
  guards (file_path dedup, head_hash+title dedup).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.catalog import (
    Catalog,
    _read_event_sourced_gate,
)
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector


# ── Gate parsing ─────────────────────────────────────────────────────────


class TestEventSourcedGate:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "ON", ""])
    def test_on_values(self, monkeypatch: pytest.MonkeyPatch, val: str):
        # PR ζ (nexus-o6aa.9.5): empty string is ON (the default-on
        # branch); only explicit falsy tokens flip it OFF.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", val)
        assert _read_event_sourced_gate() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off"])
    def test_off_values(self, monkeypatch: pytest.MonkeyPatch, val: str):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", val)
        assert _read_event_sourced_gate() is False

    def test_unset_is_on(self, monkeypatch: pytest.MonkeyPatch):
        # PR ζ: default flipped to ON. The irreversibility window
        # opens here; the bootstrap guardrail in _ensure_consistent
        # falls back to legacy when events.jsonl is empty / absent.
        monkeypatch.delenv("NEXUS_EVENT_SOURCED", raising=False)
        assert _read_event_sourced_gate() is True


# ── Legacy path (gate explicitly OFF) — pre-ζ behaviour unchanged ────────


class TestLegacyPathStillRuns:
    def test_register_does_not_write_events_jsonl_by_default(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # PR ζ flipped the default to ES; opt back into legacy for
        # this assertion that no events.jsonl is produced.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(owner, "doc.md", content_type="prose", file_path="doc.md")

        # events.jsonl either doesn't exist or is empty.
        events_path = d / "events.jsonl"
        if events_path.exists():
            assert events_path.read_text() == ""

        # SQLite + JSONL still written.
        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 1


# ── Gate ON — event-sourced path ─────────────────────────────────────────


class TestEventSourcedPathWrites:
    def test_register_owner_writes_event_log_first(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        cat.register_owner("nexus", "repo", repo_hash="abab")

        # events.jsonl has one OwnerRegistered event.
        log = EventLog(d)
        events = list(log.replay())
        assert len(events) == 1
        assert events[0].type == ev.TYPE_OWNER_REGISTERED
        assert events[0].payload.name == "nexus"

        # SQLite has the row (via projector).
        row = cat._db.execute(
            "SELECT name, owner_type, repo_hash FROM owners "
            "WHERE tumbler_prefix = ?", ("1.1",),
        ).fetchone()
        assert row == ("nexus", "repo", "abab")

        # Legacy JSONL also written for back-compat.
        owners_jsonl = (d / "owners.jsonl").read_text()
        assert "nexus" in owners_jsonl

    def test_register_writes_event_log_first(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "doc.md",
            content_type="prose",
            file_path="doc.md",
            chunk_count=12,
            head_hash="aaaa1111",
        )

        log = EventLog(d)
        events = list(log.replay())
        assert len(events) == 2
        assert events[0].type == ev.TYPE_OWNER_REGISTERED
        assert events[1].type == ev.TYPE_DOCUMENT_REGISTERED
        p = events[1].payload
        assert p.tumbler == str(tumbler)
        assert p.title == "doc.md"
        assert p.chunk_count == 12

        # SQLite has the row.
        row = cat._db.execute(
            "SELECT title, chunk_count, head_hash FROM documents "
            "WHERE tumbler = ?", (str(tumbler),),
        ).fetchone()
        assert row == ("doc.md", 12, "aaaa1111")


# ── Equivalence: new path ≡ legacy path ──────────────────────────────────


class TestEquivalence:
    """A sequence of mutations under the new path produces a SQLite
    state byte-equal to the same sequence under the legacy path."""

    def _build_catalog(
        self, tmp_path: Path, name: str, event_sourced: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        if event_sourced:
            monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        else:
            # PR ζ flipped default to ON; explicit OFF for legacy path.
            monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        d = tmp_path / name
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner(
            "nexus", "repo", repo_hash="571b8edd",
            description="Test repo",
        )
        cat.register(
            owner, "a.md", content_type="prose", file_path="a.md",
            chunk_count=3, head_hash="a1",
        )
        cat.register(
            owner, "b.md", content_type="prose", file_path="b.md",
            chunk_count=7, head_hash="b1",
        )
        cat._db.close()
        return d / ".catalog.db"

    def _snap(self, db_path: Path) -> dict[str, list[tuple]]:
        conn = sqlite3.connect(str(db_path))
        try:
            return {
                "owners": sorted(conn.execute(
                    "SELECT tumbler_prefix, name, owner_type, repo_hash, "
                    "description, repo_root FROM owners"
                ).fetchall()),
                "documents": sorted(conn.execute(
                    "SELECT tumbler, title, author, year, content_type, "
                    "file_path, corpus, physical_collection, chunk_count, "
                    "head_hash, indexed_at, metadata, source_mtime, "
                    "alias_of, source_uri FROM documents"
                ).fetchall()),
            }
        finally:
            conn.close()

    def test_legacy_and_event_sourced_produce_same_sqlite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        legacy_db = self._build_catalog(
            tmp_path, "legacy", event_sourced=False, monkeypatch=monkeypatch,
        )
        es_db = self._build_catalog(
            tmp_path, "event_sourced", event_sourced=True, monkeypatch=monkeypatch,
        )

        legacy_snap = self._snap(legacy_db)
        es_snap = self._snap(es_db)

        # owners are byte-equal
        assert legacy_snap["owners"] == es_snap["owners"]

        # documents are byte-equal modulo indexed_at (ISO timestamps).
        # Strip the timestamp column for comparison.
        def _strip_ts(rows):
            # indexed_at is column index 10
            return [r[:10] + r[11:] for r in rows]

        assert _strip_ts(legacy_snap["documents"]) == _strip_ts(
            es_snap["documents"]
        )


# ── Replay: events.jsonl produced by new path projects to same SQLite ────


class TestNewPathReplays:
    def test_events_jsonl_replay_matches_live_sqlite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(owner, "a.md", content_type="prose", file_path="a.md")
        cat.register(owner, "b.md", content_type="prose", file_path="b.md")

        # Replay events.jsonl into a fresh CatalogDB.
        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        # Both DBs must have the same owners + documents rows.
        with sqlite3.connect(str(d / ".catalog.db")) as live:
            live_owners = sorted(live.execute(
                "SELECT tumbler_prefix, name FROM owners"
            ).fetchall())
            live_docs = sorted(live.execute(
                "SELECT tumbler, title FROM documents"
            ).fetchall())
        with sqlite3.connect(str(tmp_path / "projected.db")) as proj:
            proj_owners = sorted(proj.execute(
                "SELECT tumbler_prefix, name FROM owners"
            ).fetchall())
            proj_docs = sorted(proj.execute(
                "SELECT tumbler, title FROM documents"
            ).fetchall())
        assert live_owners == proj_owners
        assert live_docs == proj_docs


# ── Shadow emit suppression when event-sourced is ON ─────────────────────


class TestShadowEmitSuppressedWhenEventSourced:
    def test_no_double_write_when_both_gates_on(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Both gates on. The event-sourced path should write the event
        # once via _write_to_event_log; shadow emit should NOT write a
        # second copy after the SQLite commit.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        cat.register_owner("nexus", "repo", repo_hash="abab")

        log = EventLog(d)
        events = list(log.replay())
        # Exactly ONE OwnerRegistered (not two).
        assert len(events) == 1
        assert events[0].type == ev.TYPE_OWNER_REGISTERED


# ── Idempotency under the new path ───────────────────────────────────────


class TestIdempotencyUnderEventSourced:
    def test_register_same_file_path_twice_returns_same_tumbler(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        first = cat.register(
            owner, "doc.md", content_type="prose", file_path="doc.md",
        )
        second = cat.register(
            owner, "doc.md", content_type="prose", file_path="doc.md",
        )
        assert first == second
        # Only one DocumentRegistered in the log.
        log = EventLog(d)
        doc_events = [
            e for e in log.replay()
            if e.type == ev.TYPE_DOCUMENT_REGISTERED
        ]
        assert len(doc_events) == 1
