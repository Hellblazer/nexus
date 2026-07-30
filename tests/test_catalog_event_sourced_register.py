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

CATALOG SUBSTRATE (nexus-i711w Stage 2). Measured disposition: 2 PORT,
7 DIE, 1 GAP.

  - PORT (2): ``register_owner`` and ``register`` are implemented on BOTH
    substrates, so the halves of those two tests that assert the resulting
    ROW seed and read through :class:`tests._catalog_fixture_ops.
    ActiveCatalog`. Their ``events.jsonl`` / legacy-``owners.jsonl`` halves
    have no service-mode observable and are dropped; the event-emission
    contract for ``register_owner`` is still pinned by
    ``TestShadowEmitSuppressedWhenEventSourced`` and for ``register`` by
    ``TestNewPathReplays``, both of which stay.
  - DIE (7): ``_read_event_sourced_gate`` is a function ON the dying module;
    the rest assert ``events.jsonl`` presence/absence, dual-path SQLite
    equivalence, or replay.
  - GAP (1): ``register`` file_path idempotency — see its own annotation.

Everything pinned carries ``local_catalog_backend`` so the pin is EXPLICIT
rather than incidental on a not-yet-existing tmp ``.catalog.db``, and every
dying import moved INSIDE the bodies that still need it so this file still
COLLECTS after the deletion.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests._catalog_fixture_ops import ActiveCatalog


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2)."""
    return ActiveCatalog()


def _slug() -> str:
    """A per-test discriminator for owner names and file paths."""
    return uuid.uuid4().hex[:8]


def _local_catalog(tmp_path) -> tuple[Any, Path]:
    """A real LOCAL SQLite ``Catalog`` rooted in *tmp_path*, plus its dir.

    ONLY for the DIE and GAP cohorts. Imports ``nexus.catalog.catalog``
    inside the body so this file still collects after the deletion; callers
    must also carry ``local_catalog_backend``.
    """
    from nexus.catalog.catalog import Catalog

    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db"), d


# ── Gate parsing ─────────────────────────────────────────────────────────


class TestEventSourcedGate:
    """nexus-i711w Stage 2: DIE. The subject is
    ``nexus.catalog.catalog._read_event_sourced_gate`` — a private function
    ON the dying module, gating a write path that only the local catalog
    has. It retires with the module, so the import stays inside the bodies.
    """

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "ON", ""])
    def test_on_values(self, monkeypatch: pytest.MonkeyPatch, val: str):
        # PR ζ (nexus-o6aa.9.5): empty string is ON (the default-on
        # branch); only explicit falsy tokens flip it OFF.
        from nexus.catalog.catalog import _read_event_sourced_gate

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", val)
        assert _read_event_sourced_gate() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off"])
    def test_off_values(self, monkeypatch: pytest.MonkeyPatch, val: str):
        from nexus.catalog.catalog import _read_event_sourced_gate

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", val)
        assert _read_event_sourced_gate() is False

    def test_unset_is_on(self, monkeypatch: pytest.MonkeyPatch):
        # PR ζ: default flipped to ON. The irreversibility window
        # opens here; the bootstrap guardrail in _ensure_consistent
        # falls back to legacy when events.jsonl is empty / absent.
        from nexus.catalog.catalog import _read_event_sourced_gate

        monkeypatch.delenv("NEXUS_EVENT_SOURCED", raising=False)
        assert _read_event_sourced_gate() is True


# ── Legacy path (gate explicitly OFF) — pre-ζ behaviour unchanged ────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestLegacyPathStillRuns:
    """nexus-i711w Stage 2: DIE. Asserts the ABSENCE of an ``events.jsonl``
    — a local-only artifact with no service-mode counterpart."""

    def test_register_does_not_write_events_jsonl_by_default(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # PR ζ flipped the default to ES; opt back into legacy for
        # this assertion that no events.jsonl is produced.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        cat, d = _local_catalog(tmp_path)
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
    def test_register_owner_persists_the_owner_row(self, active_catalog):
        """``register_owner`` lands exactly one owner carrying its fields.

        nexus-i711w Stage 2: PORTED. Was
        ``test_register_owner_writes_event_log_first``. Two of its three
        assertions were local-only artifacts (``events.jsonl``,
        ``owners.jsonl``); the OwnerRegistered emission is still pinned by
        ``TestShadowEmitSuppressedWhenEventSourced``.

        CARDINALITY IS PRESERVED DELIBERATELY: the old assertion was a
        single-row SELECT keyed on the literal prefix "1.1", which only
        holds for a virgin catalog. ``get_owner_by_prefix`` would be the
        nearest one-shot equivalent but returns ONE owner and so could not
        observe a duplicate; filtering ``list_owners()`` states the
        "exactly one owner by this name" the test always meant, and the
        minted prefix is asserted against the returned Tumbler instead of a
        hardcoded one.
        """
        cat = active_catalog
        slug = _slug()
        name = f"es-register-owner-{slug}"
        owner = cat.register_owner(name, "repo", repo_hash=slug)

        rows = [o for o in cat.list_owners() if o.get("name") == name]
        assert len(rows) == 1, f"expected exactly one owner {name!r}, got {len(rows)}"
        assert rows[0]["owner_type"] == "repo"
        assert rows[0]["repo_hash"] == slug
        assert rows[0]["tumbler_prefix"] == str(owner)

    def test_register_persists_title_chunk_count_and_head_hash(self, active_catalog):
        """``register`` lands all three fields on the document row.

        nexus-i711w Stage 2: PORTED. Was
        ``test_register_writes_event_log_first``; the DocumentRegistered
        emission half is still pinned by ``TestNewPathReplays``, which
        replays ``events.jsonl`` and diffs the documents it reconstructs.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-register-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md",
            content_type="prose",
            file_path=f"{slug}/doc.md",
            chunk_count=12,
            head_hash="aaaa1111",
        )

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert (entry.title, entry.chunk_count, entry.head_hash) == (
            "doc.md", 12, "aaaa1111",
        )


# ── Equivalence: new path ≡ legacy path ──────────────────────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestEquivalence:
    """nexus-i711w Stage 2: DIE. Diffs the legacy write path against the
    event-sourced write path, both of which exist only inside
    ``nexus/catalog/catalog.py``, by snapshotting raw ``.catalog.db``
    tables. Neither path nor artifact survives.

    A sequence of mutations under the new path produces a SQLite
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
        from nexus.catalog.catalog import Catalog

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


@pytest.mark.usefixtures("local_catalog_backend")
class TestNewPathReplays:
    """nexus-i711w Stage 2: DIE. ``events.jsonl`` replayed through the
    ``Projector`` into a fresh ``CatalogDB`` and diffed against the live
    ``.catalog.db``; the service catalog has neither log nor projection.
    This is also what still pins the register DocumentRegistered event that
    the ported test above dropped."""

    def test_events_jsonl_replay_matches_live_sqlite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        from nexus.catalog.catalog_db import CatalogDB
        from nexus.catalog.event_log import EventLog
        from nexus.catalog.projector import Projector

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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


@pytest.mark.usefixtures("local_catalog_backend")
class TestShadowEmitSuppressedWhenEventSourced:
    """nexus-i711w Stage 2: DIE. The shadow-emit double-write gate over
    ``events.jsonl``. This is also what still pins the register_owner
    OwnerRegistered event that the ported test above dropped."""

    def test_no_double_write_when_both_gates_on(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Both gates on. The event-sourced path should write the event
        # once via _write_to_event_log; shadow emit should NOT write a
        # second copy after the SQLite commit.
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        cat, d = _local_catalog(tmp_path)
        cat.register_owner("nexus", "repo", repo_hash="abab")

        log = EventLog(d)
        events = list(log.replay())
        # Exactly ONE OwnerRegistered (not two).
        assert len(events) == 1
        assert events[0].type == ev.TYPE_OWNER_REGISTERED


# ── Idempotency under the new path ───────────────────────────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestIdempotencyUnderEventSourced:
    def test_register_same_file_path_twice_returns_same_tumbler(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 9 — PINNED, not ported, not deleted.

        ``register``'s file_path idempotency: a second ``register`` of the
        same ``file_path`` under the same owner must return the SAME tumbler
        and create no second row. Owed by the service substrate; nothing
        live asserts it, and the "no duplicate" half is asserted here as a
        count of DocumentRegistered EVENTS, which has no service-mode
        observable. This is the SPECIFICATION SOURCE for the fresh
        service-side test i711w.1 will write.
        """
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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
