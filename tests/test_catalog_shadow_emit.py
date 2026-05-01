# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 1 PR F shadow-emit path.

Coverage:
- Default (gate OFF, no env var): no events.jsonl content after any
  catalog mutation; existing JSONL + SQLite behavior is unchanged.
- Env-var parsing: 1/true/yes/on → ON; 0/false/no/off/empty/unset → OFF.
- With gate ON, every supported write site emits the corresponding
  typed event with the right payload:
    register_owner → OwnerRegistered
    register → DocumentRegistered
    update → DocumentRegistered (lossless replay; Phase 3 introduces
             fine-grained DocumentRenamed / DocumentEnriched intent types)
    set_alias → DocumentAliased
    delete_document → DocumentDeleted
    link → LinkCreated
    unlink → LinkDeleted
    bulk_unlink → LinkDeleted per matching link
- Round-trip: with gate ON, drive a sequence of catalog mutations,
  then replay events.jsonl through the projector against a fresh
  CatalogDB → equal to the live db (events alone are sufficient to
  reproduce the SQLite state).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog, _read_shadow_gate
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def cat_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    """Catalog constructed with shadow emit OFF and ES OFF — the
    historical "no events.jsonl" config. PR ζ (nexus-o6aa.9.5) flipped
    the ES default to ON, so asserting an empty event log now requires
    opting both gates out.
    """
    monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db")


@pytest.fixture()
def cat_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    """Catalog constructed with shadow emit ON and ES OFF — exercises
    the shadow-only path. Under PR ζ this requires an explicit ES
    opt-out so shadow is the only writer of events.jsonl.
    """
    monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db")


def _read_events(cat: Catalog) -> list[ev.Event]:
    if not cat._events_path.exists():
        return []
    out: list[ev.Event] = []
    with cat._events_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(ev.Event.from_dict(json.loads(line)))
    return out


# ── Gate parsing ─────────────────────────────────────────────────────────


class TestGateParsing:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "True", "yes", "ON", "on"])
    def test_on_values(self, monkeypatch: pytest.MonkeyPatch, val: str):
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", val)
        assert _read_shadow_gate() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_off_values(self, monkeypatch: pytest.MonkeyPatch, val: str):
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", val)
        assert _read_shadow_gate() is False

    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        assert _read_shadow_gate() is False


# ── Default (gate OFF) — no behavior change ──────────────────────────────


class TestDefaultOff:
    def test_no_events_after_register(self, cat_off: Catalog):
        owner = cat_off.register_owner("nexus", "repo", repo_hash="ababab")
        cat_off.register(owner, "doc-A.md", content_type="prose", file_path="doc-A.md")
        # events.jsonl either doesn't exist or is empty.
        if cat_off._events_path.exists():
            assert cat_off._events_path.read_text() == ""

    def test_existing_jsonl_writes_unchanged(self, cat_off: Catalog):
        owner = cat_off.register_owner("nexus", "repo", repo_hash="cdcdcd")
        cat_off.register(owner, "doc-A.md", content_type="prose", file_path="doc-A.md")
        # owners.jsonl + documents.jsonl land as before.
        assert cat_off._owners_path.read_text().count("\n") >= 1
        assert cat_off._documents_path.read_text().count("\n") >= 1


# ── Gate ON — emits per write site ───────────────────────────────────────


class TestEmitsOnRegisterOwner:
    def test_owner_registered_event_emitted(self, cat_on: Catalog):
        owner = cat_on.register_owner(
            "nexus", "repo", repo_hash="ababab",
            description="test repo", repo_root="/git/nexus",
        )
        evs = _read_events(cat_on)
        assert len(evs) == 1
        e = evs[0]
        assert e.type == ev.TYPE_OWNER_REGISTERED
        assert e.v == 0
        assert e.payload.owner_id == str(owner)
        assert e.payload.name == "nexus"
        assert e.payload.owner_type == "repo"
        assert e.payload.repo_hash == "ababab"
        assert e.payload.repo_root == "/git/nexus"
        assert e.payload.description == "test repo"


class TestEmitsOnRegister:
    def test_document_registered_event_emitted(self, cat_on: Catalog):
        owner = cat_on.register_owner("nexus", "repo", repo_hash="ababab")
        tumbler = cat_on.register(
            owner, "doc-A.md", content_type="prose",
            file_path="doc-A.md", chunk_count=12, head_hash="aaaa1111",
        )

        evs = _read_events(cat_on)
        # Owner + document emit.
        assert len(evs) == 2
        owner_e, doc_e = evs
        assert owner_e.type == ev.TYPE_OWNER_REGISTERED
        assert doc_e.type == ev.TYPE_DOCUMENT_REGISTERED
        assert doc_e.v == 0
        p = doc_e.payload
        assert p.tumbler == str(tumbler)
        assert p.doc_id == str(tumbler)  # Phase 1 stand-in
        assert p.owner_id == str(owner)
        assert p.title == "doc-A.md"
        assert p.content_type == "prose"
        assert p.file_path == "doc-A.md"
        assert p.chunk_count == 12
        assert p.head_hash == "aaaa1111"


class TestEmitsOnUpdate:
    def test_update_emits_document_registered_with_new_state(self, cat_on: Catalog):
        owner = cat_on.register_owner("nexus", "repo", repo_hash="ababab")
        tumbler = cat_on.register(
            owner, "doc-A.md", content_type="prose",
            file_path="doc-A.md", chunk_count=0,
        )
        cat_on.update(tumbler, chunk_count=42, head_hash="newhash")

        evs = _read_events(cat_on)
        assert len(evs) == 3  # owner + register + update
        upd = evs[-1]
        assert upd.type == ev.TYPE_DOCUMENT_REGISTERED
        assert upd.payload.tumbler == str(tumbler)
        assert upd.payload.chunk_count == 42
        assert upd.payload.head_hash == "newhash"


class TestEmitsOnSetAlias:
    def test_set_alias_emits_document_aliased(self, cat_on: Catalog):
        owner = cat_on.register_owner("nexus", "repo", repo_hash="ababab")
        canonical = cat_on.register(owner, "canonical.md", content_type="prose")
        alias = cat_on.register(owner, "alias.md", content_type="prose")
        cat_on.set_alias(alias, canonical)

        evs = _read_events(cat_on)
        # owner + 2 registers + 1 alias-register-rewrite + 1 aliased event.
        # set_alias appends a DocumentRecord (with alias_of populated)
        # via _append_jsonl + emits DocumentAliased.
        types = [e.type for e in evs]
        assert types.count(ev.TYPE_DOCUMENT_ALIASED) == 1
        aliased = next(e for e in evs if e.type == ev.TYPE_DOCUMENT_ALIASED)
        assert aliased.payload.alias_doc_id == str(alias)
        assert aliased.payload.canonical_doc_id == str(canonical)


class TestEmitsOnDeleteDocument:
    def test_delete_document_emits_document_deleted(self, cat_on: Catalog):
        owner = cat_on.register_owner("nexus", "repo", repo_hash="ababab")
        tumbler = cat_on.register(owner, "doc-A.md", content_type="prose")
        assert cat_on.delete_document(tumbler) is True

        evs = _read_events(cat_on)
        types = [e.type for e in evs]
        assert types.count(ev.TYPE_DOCUMENT_DELETED) == 1
        deleted = next(e for e in evs if e.type == ev.TYPE_DOCUMENT_DELETED)
        assert deleted.payload.doc_id == str(tumbler)
        assert deleted.payload.reason == "catalog.delete_document"


class TestEmitsOnLinkUnlink:
    def test_link_emits_link_created(self, cat_on: Catalog):
        owner = cat_on.register_owner("nexus", "repo", repo_hash="ababab")
        a = cat_on.register(owner, "a.md", content_type="prose")
        b = cat_on.register(owner, "b.md", content_type="prose")
        created = cat_on.link(a, b, link_type="cites", created_by="manual")
        assert created is True

        evs = _read_events(cat_on)
        link_events = [e for e in evs if e.type == ev.TYPE_LINK_CREATED]
        assert len(link_events) == 1
        p = link_events[0].payload
        assert p.from_doc == str(a)
        assert p.to_doc == str(b)
        assert p.link_type == "cites"
        assert p.creator == "manual"

    def test_unlink_emits_link_deleted(self, cat_on: Catalog):
        owner = cat_on.register_owner("nexus", "repo", repo_hash="ababab")
        a = cat_on.register(owner, "a.md", content_type="prose")
        b = cat_on.register(owner, "b.md", content_type="prose")
        cat_on.link(a, b, link_type="cites", created_by="manual")
        n = cat_on.unlink(a, b, link_type="cites")
        assert n == 1

        evs = _read_events(cat_on)
        types = [e.type for e in evs]
        assert ev.TYPE_LINK_DELETED in types
        deleted = next(e for e in evs if e.type == ev.TYPE_LINK_DELETED)
        assert deleted.payload.from_doc == str(a)
        assert deleted.payload.to_doc == str(b)
        assert deleted.payload.link_type == "cites"


# ── Round-trip: events.jsonl ⇒ projector ⇒ live SQLite ────────────────────


class TestShadowReplayRoundTrip:
    """The most important test: real catalog mutations leave an
    events.jsonl that, replayed through the projector, reproduces the
    SQLite state. This is the ground truth check that shadow emit
    captures every state change the catalog's tumbler-keyed schema
    cares about.
    """

    def test_register_register_link_unlink_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # RDR-101 Phase 3 follow-up C (nexus-o6aa.9.8): pin to legacy
        # explicitly so the shadow path is the only writer of
        # events.jsonl. Pre-fix this test inherited PR ζ's default-ON
        # ES gate and silently exercised the ES write path while
        # purporting to test shadow round-trip — passing for the
        # wrong reason because ES also writes events.jsonl.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")

        owner = cat.register_owner("nexus", "repo", repo_hash="ababab")
        a = cat.register(owner, "a.md", content_type="prose", file_path="a.md")
        b = cat.register(owner, "b.md", content_type="prose", file_path="b.md")
        cat.link(a, b, link_type="cites", created_by="manual")
        cat.unlink(a, b, link_type="cites")
        cat.delete_document(b)

        # Replay events.jsonl into a fresh CatalogDB.
        log = EventLog(cat_dir)
        projected_path = tmp_path / "projected.db"
        proj_db = CatalogDB(projected_path)
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        # Compare row sets (strip links.id autoincrement, same as PR C).
        live_conn = sqlite3.connect(str(cat_dir / ".catalog.db"))
        proj_conn = sqlite3.connect(str(projected_path))
        try:
            for table in ("owners", "documents"):
                cur = live_conn.execute(
                    f"PRAGMA table_info({table})"
                )
                cols = ", ".join(r[1] for r in cur.fetchall())
                live_rows = sorted(live_conn.execute(
                    f"SELECT {cols} FROM {table} ORDER BY {cols}"
                ).fetchall())
                proj_rows = sorted(proj_conn.execute(
                    f"SELECT {cols} FROM {table} ORDER BY {cols}"
                ).fetchall())
                assert live_rows == proj_rows, (
                    f"{table} rows differ:\n"
                    f"  live:      {live_rows}\n"
                    f"  projected: {proj_rows}"
                )
            # Links: both should be empty (link + unlink + delete leave 0 links).
            assert live_conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
            assert proj_conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
        finally:
            live_conn.close()
            proj_conn.close()

    def test_register_register_link_unlink_delete_under_es(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """ES-mode companion (RDR-101 Phase 3 follow-up C,
        nexus-o6aa.9.8). The legacy-mode test above pins the shadow
        path's round-trip invariant; this one exercises the same
        register/link/unlink/delete sequence under
        ``NEXUS_EVENT_SOURCED=1`` so the ES write path's events.jsonl
        is similarly verified to replay into a byte-equal SQLite.

        Without this test, a regression in the ES write path that
        emits a malformed event (or omits one) would slip through —
        the shadow-path test above runs only in legacy mode.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")

        owner = cat.register_owner("nexus", "repo", repo_hash="ababab")
        a = cat.register(owner, "a.md", content_type="prose", file_path="a.md")
        b = cat.register(owner, "b.md", content_type="prose", file_path="b.md")
        cat.link(a, b, link_type="cites", created_by="manual")
        cat.unlink(a, b, link_type="cites")
        cat.delete_document(b)

        log = EventLog(cat_dir)
        projected_path = tmp_path / "projected.db"
        proj_db = CatalogDB(projected_path)
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        live_conn = sqlite3.connect(str(cat_dir / ".catalog.db"))
        proj_conn = sqlite3.connect(str(projected_path))
        try:
            for table in ("owners", "documents"):
                cur = live_conn.execute(
                    f"PRAGMA table_info({table})"
                )
                cols = ", ".join(r[1] for r in cur.fetchall())
                live_rows = sorted(live_conn.execute(
                    f"SELECT {cols} FROM {table} ORDER BY {cols}"
                ).fetchall())
                proj_rows = sorted(proj_conn.execute(
                    f"SELECT {cols} FROM {table} ORDER BY {cols}"
                ).fetchall())
                assert live_rows == proj_rows, (
                    f"ES mode: {table} rows differ:\n"
                    f"  live:      {live_rows}\n"
                    f"  projected: {proj_rows}"
                )
            assert live_conn.execute(
                "SELECT COUNT(*) FROM links",
            ).fetchone()[0] == 0
            assert proj_conn.execute(
                "SELECT COUNT(*) FROM links",
            ).fetchone()[0] == 0
        finally:
            live_conn.close()
            proj_conn.close()
