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

CATALOG SUBSTRATE (nexus-i711w Stage 2). The discriminator applied to this
file: an assertion about the EVENT (a typed line landed in ``events.jsonl``,
a replay reproduced the projection) has no service-mode expression and
retires with ``nexus/catalog/{catalog,catalog_db,event_log,projector,
events}.py``; an assertion about the VERB'S OUTCOME (a row exists, a field
changed) is implemented on BOTH substrates and ports. Four tests were
BOTH — their event half is still pinned elsewhere in this file
(``TestShadowEmitSuppressedAcrossMutators`` pins the owner/register/update/
delete event sequence, ``TestFullReplayEqualsLive`` pins the alias and the
rename/bib replay), so converting them to their outcome half loses no
coverage that this file does not still assert. They were renamed to say
what they now assert.

Three cohorts stay PINNED to the local SQLite catalog, each stated at its
own site, and each carrying ``local_catalog_backend`` so the pin is
EXPLICIT: without it they pass only because a fresh tmp ``.catalog.db``
does not exist yet and ``Catalog`` forces ``read_only=True`` over an
EXISTING file when the backend is service.

  1. DIE — subject IS the local event-sourcing machinery.
  2. GAP nexus-i711w.1 — contracts the service substrate OWES but that
     NOTHING live currently tests. Converting them is wrong (there is no
     live observable to assert against); deleting them is wrong (the
     contract loses its only written record). They stay pinned, behaving
     exactly as today, each annotated with its i711w.1 item number so the
     spec is traceable from the code.

``nexus.catalog.catalog`` / ``catalog_db`` / ``event_log`` / ``projector``
/ ``events`` are therefore imported INSIDE the bodies that still need
them, never at module scope, so this file still COLLECTS once the local
catalog is deleted.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, unroutable_write_target


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2).

    Deliberately NOT the local ``Catalog`` these tests used to build: the
    subject of the tests that take this fixture is the mutator's OUTCOME
    (``update`` / ``delete_document`` / ``rename_collection``), which
    ``HttpCatalogClient`` implements, so exercising it against the live
    substrate is strictly more coverage than the local-only form.
    """
    return ActiveCatalog()


def _slug() -> str:
    """A per-test discriminator for owner names / paths / collections.

    The service catalog is shared within a tenant; two tests registering
    ``doc.md`` under owner ``nexus`` would collide on ``register``'s
    file_path idempotency and silently share a document. A distinct slug
    keeps each test's cardinality assertions its own.
    """
    return uuid.uuid4().hex[:8]


def _local_catalog(tmp_path) -> tuple[Any, Path]:
    """A real LOCAL SQLite ``Catalog`` rooted in *tmp_path*, plus its dir.

    ONLY for the DIE and GAP cohorts (see the module docstring). Imports
    ``nexus.catalog.catalog`` inside the body so this file still collects
    after the local catalog is deleted. Callers must also carry
    ``local_catalog_backend``.
    """
    from nexus.catalog.catalog import Catalog

    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db"), d


# ── update ───────────────────────────────────────────────────────────────


class TestUpdateEventSourced:
    def test_update_persists_chunk_count_and_head_hash(self, active_catalog):
        """``update`` lands both fields on the document row.

        nexus-i711w Stage 2: PORTED. Was
        ``test_update_emits_document_registered_via_event_log``, which
        asserted the ``events.jsonl`` line AND the resulting row. Only the
        second half has a service-mode observable; the event half is still
        pinned by ``TestShadowEmitSuppressedAcrossMutators``, which asserts
        the exact owner/register/update/delete event sequence, so nothing
        this file used to say about the update event is lost.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-update-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        cat.update(tumbler, chunk_count=42, head_hash="updated")

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert (entry.chunk_count, entry.head_hash) == (42, "updated")

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_update_re_derives_chunk_count_from_manifest_when_omitted(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 1 — PINNED, not ported, not deleted.

        The re-derivation contract (``update()`` with no ``chunk_count``
        must refresh it from the ``document_chunks`` count) is owed by the
        service substrate but nothing live asserts it, and its only
        observable here is the emitted event payload. This test is the
        SPECIFICATION SOURCE for the fresh service-side test i711w.1 will
        write; it stays pinned and behaves exactly as today.

        nexus-zq79 F4: when caller omits chunk_count, cat.update() must
        re-derive it from the current document_chunks count so the emitted
        event payload is fresh (not the resolve-time stale snapshot).
        Event replay would otherwise project the old 0.
        """
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_update_respects_caller_supplied_chunk_count(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 2 — PINNED, not ported, not deleted.

        The caller-intent-wins half of item 1's contract: an explicit
        ``chunk_count`` must NOT be re-derived. Same reason it cannot be
        converted — its only observable is the emitted event payload.

        nexus-zq79 F4: caller intent wins — when chunk_count is passed
        explicitly (e.g. orphan-backfill paths), use the caller's value
        without re-derivation.
        """
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_update_refreshes_indexed_at_when_head_hash_changes(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 3 — PINNED, not ported, not deleted.

        ``indexed_at`` must advance when ``head_hash`` changes so
        ``nx catalog show`` last_indexed tracks re-indexes. Owed by the
        service substrate, asserted by nothing live.

        nexus-zq79 F7: cat.update(head_hash=...) must refresh
        documents.indexed_at to now. Pre-fix, indexed_at stayed at the
        original register stamp forever; `nx catalog show` last_indexed
        never advanced on re-indexed files.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, _ = _local_catalog(tmp_path)
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

    def test_event_sourced_update_persists_bib_kwargs(self, active_catalog):
        """All 8 ``bib_*`` kwargs passed to ``update`` land on the row.

        nexus-i711w Stage 2: PORTED — the assertion is purely the verb's
        outcome (read back through ``resolve``), and the service arm carries
        every one of the 8 columns (``http_catalog_client._to_entry``,
        sourced from ``CatalogRepository.docRowFromRecord``).

        nexus-6ha8a (was nexus-9l2lg Task 5's deferred decision):
        ``DocumentRegisteredPayload`` now carries all 8 ``bib_*`` fields
        and the projector's ``_v0_document_registered`` writes them into
        its INSERT/ON CONFLICT SET clause. ``update()``'s event-sourced
        branch sources them from ``rec_dict``, which already carries
        bib_* forward from the current row (nexus-9l2lg Task 2) — so a
        caller passing ``bib_*`` kwargs under event-sourced mode now
        persists them, matching the non-event-sourced path.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-bib-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        cat.update(
            tumbler, bib_year=2020, bib_authors="X", bib_venue="V",
            bib_citation_count=5, bib_semantic_scholar_id="ss1",
            bib_openalex_id="W1", bib_doi="10.1/x",
            bib_enriched_at="2026-01-01T00:00:00Z",
        )
        entry = cat.resolve(tumbler)
        assert entry.bib_year == 2020
        assert entry.bib_authors == "X"
        assert entry.bib_venue == "V"
        assert entry.bib_citation_count == 5
        assert entry.bib_semantic_scholar_id == "ss1"
        assert entry.bib_openalex_id == "W1"
        assert entry.bib_doi == "10.1/x"
        assert entry.bib_enriched_at == "2026-01-01T00:00:00Z"

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_event_sourced_update_without_bib_kwargs_preserves_existing_bib(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 4 — PINNED, not ported, not deleted.

        The bib_*-preservation-across-``update`` contract. Item 4 covers all
        four ``DocumentRegisteredPayload`` emission sites; this is the
        ``update()`` one (items 7/8/10 below are ``rename_collection`` /
        ``update_document_collection`` / the batch form). Owed by the
        service substrate, asserted by nothing live.

        Clobber regression for the ``update()`` emission site itself:
        an update that doesn't pass bib_* must carry the current values
        forward, not reset them (mirrors the non-event-sourced pin in
        test_catalog_bib_columns.py)."""
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, _ = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", chunk_count=0,
        )
        cat.update(tumbler, bib_year=2020, bib_authors="X")
        cat.update(tumbler, chunk_count=9)
        entry = cat.resolve(tumbler)
        assert entry.chunk_count == 9
        assert entry.bib_year == 2020
        assert entry.bib_authors == "X"


# ── delete_document ──────────────────────────────────────────────────────


class TestDeleteDocumentEventSourced:
    def test_delete_removes_the_document(self, active_catalog):
        """``delete_document`` reports True and the document stops resolving.

        nexus-i711w Stage 2: PORTED. Was
        ``test_delete_emits_event_and_removes_row``; the DocumentDeleted
        event half is still pinned by
        ``TestShadowEmitSuppressedAcrossMutators``.

        ``resolve`` is the substrate-neutral form of the old
        ``SELECT count(*) ... WHERE tumbler = ?``: the service arm
        tombstones rather than DELETEs, and ``resolve`` returning None is
        what BOTH arms say about a deleted document.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-delete-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path=f"{slug}/doc.md",
        )
        assert cat.resolve(tumbler) is not None
        assert cat.delete_document(tumbler) is True
        assert cat.resolve(tumbler) is None

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_delete_cascades_to_document_chunks(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 8 — PINNED, not ported, not deleted.

        ``delete_document`` must cascade-purge the ``document_chunks``
        manifest. This test and the legacy-path one below are the two
        halves of item 6 (event-sourced and non-event-sourced). Owed by
        the service substrate, asserted by nothing live: the service arm
        has no manifest-after-delete assertion anywhere.

        nexus-8g79.7: deleting a document must purge its
        document_chunks manifest rows in the same write — pre-fix the
        manifest was left as FK orphans because the schema has no
        ON DELETE CASCADE and the projector handler only DELETEd from
        documents.
        """
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, _ = _local_catalog(tmp_path)
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

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_delete_cascades_to_document_chunks_legacy_path(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 8 (legacy-path half) — PINNED.

        nexus-8g79.7: same cascade for the non-event-sourced path."""
        monkeypatch.delenv("NEXUS_EVENT_SOURCED", raising=False)
        cat, _ = _local_catalog(tmp_path)
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
    def test_set_alias_updates_alias_of(self):
        """``set_alias`` populates the alias row's ``alias_of``.

        nexus-i711w Stage 2: PORTED. Was
        ``test_set_alias_emits_event_and_updates_alias_of``; the
        DocumentAliased event half is still pinned by
        ``TestFullReplayEqualsLive``, which replays an alias and asserts the
        projected ``alias_of``.

        ``unroutable_write_target()`` rather than ``ActiveCatalog``:
        ``set_alias`` mutates but is NOT on ``CATALOG_WRITE_OPS``
        (nexus-iltyk), so a plain ``ActiveCatalog`` refuses to route it as a
        write on the SQLite arm and the assertion could not observe it.

        Read back by scanning ``all_documents`` for the ALIAS row rather
        than ``resolve(alias)``: ``resolve`` follows the alias by default
        and would hand back the CANONICAL entry, whose ``alias_of`` is ""
        — the assertion would be structurally unable to see the write it is
        about.
        """
        cat = unroutable_write_target()
        slug = _slug()
        owner = cat.register_owner(f"es-alias-{slug}", "repo", repo_hash=slug)
        canonical = cat.register(
            owner, "canonical.md", content_type="prose",
            file_path=f"{slug}/canonical.md",
        )
        alias = cat.register(
            owner, "alias.md", content_type="prose",
            file_path=f"{slug}/alias.md",
        )
        cat.set_alias(alias, canonical)

        rows = [
            d for d in cat.all_documents() if str(d.tumbler) == str(alias)
        ]
        assert len(rows) == 1, f"expected exactly one alias row, got {len(rows)}"
        assert rows[0].alias_of == str(canonical)


# ── rename_collection ────────────────────────────────────────────────────


class TestRenameCollectionEventSourced:
    def test_rename_moves_every_row_in_the_collection(self, active_catalog):
        """``rename_collection`` re-homes every document and reports the count.

        nexus-i711w Stage 2: PORTED. Was
        ``test_rename_emits_per_row_events_and_updates_sqlite``; the per-row
        DocumentRegistered emission half is still pinned by
        ``test_shadow_emit_rename_replay_reconstructs_bib_columns``.

        ``list_by_collection`` is the substrate-neutral form of the old
        ``SELECT count(*) ... WHERE physical_collection = ?``. Counting the
        rows in the NEW collection (not just trusting the returned int)
        keeps the assertion able to see a rename that reports 2 and moves 1.

        MEASURED PRECONDITION DIVERGENCE (found by attempting the port, not
        by reading it): the local ``rename_collection`` renames by scanning
        ``documents.physical_collection``, so a collection that exists only
        as a value on document rows renames fine. The service verb is
        ``POST /collections/rename`` and 404s ``collection not found`` unless
        the collection has a ``catalog_collections`` row. The explicit
        ``register_collection`` below is that precondition, not a workaround
        — and it is why this test had to be attempted rather than assumed.
        """
        cat = active_catalog
        slug = _slug()
        old, new = f"docs__old_{slug}", f"docs__new_{slug}"
        owner = cat.register_owner(f"es-rename-{slug}", "repo", repo_hash=slug)
        cat.register_collection(old, content_type="docs")
        cat.register(
            owner, "a.md", content_type="prose",
            file_path=f"{slug}/a.md",
            physical_collection=old,
        )
        cat.register(
            owner, "b.md", content_type="prose",
            file_path=f"{slug}/b.md",
            physical_collection=old,
        )

        n = cat.rename_collection(old, new)
        assert n == 2

        assert len(cat.list_by_collection(new)) == 2
        assert cat.list_by_collection(old) == []

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_rename_preserves_enriched_bib_columns(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """GAP nexus-i711w.1 item 5 — PINNED, not ported, not deleted.

        bib_* preservation across ``rename_collection``. Owed by the
        service substrate, asserted by nothing live.

        nexus-6ha8a clobber regression: rename_collection's two
        DocumentRegisteredPayload emission sites (per-row event-sourced
        loop + shadow-emit loop) must carry forward the row's current
        bib_* values, not reset them to defaults."""
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, _ = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "a.md", content_type="prose",
            file_path="a.md", physical_collection="docs__old",
        )
        cat.update(
            tumbler, bib_year=2019, bib_authors="Dana", bib_venue="OSDI",
            bib_citation_count=314, bib_semantic_scholar_id="ss42",
        )

        n = cat.rename_collection("docs__old", "docs__new")
        assert n == 1

        entry = cat.resolve(tumbler)
        assert entry.physical_collection == "docs__new"
        assert entry.bib_year == 2019
        assert entry.bib_authors == "Dana"
        assert entry.bib_venue == "OSDI"
        assert entry.bib_citation_count == 314
        assert entry.bib_semantic_scholar_id == "ss42"


@pytest.mark.usefixtures("local_catalog_backend")
class TestUpdateDocumentCollectionEventSourced:
    """GAP nexus-i711w.1 items 6 and 7 — PINNED, not ported, not deleted.

    bib_* preservation across ``update_document_collection`` (item 8) and
    ``update_documents_collection_batch`` (item 10). Both are owed by the
    service substrate and asserted by nothing live. Note the service arm's
    ``update_document_collection`` posts a bare ``/update`` with only
    ``physical_collection`` (http_catalog_client.py:1844) and the batch form
    is a client-side loop over it, so this is exactly the emission site
    whose clobber behaviour has no service-side test.

    nexus-6ha8a clobber regression: _update_document_collection_locked
    (backing update_document_collection / update_documents_collection_batch)
    is the fourth DocumentRegisteredPayload emission site — must carry
    forward current bib_* values, not reset them."""

    def test_update_document_collection_preserves_enriched_bib_columns(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, _ = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "a.md", content_type="prose",
            file_path="a.md", physical_collection="docs__old",
        )
        cat.update(
            tumbler, bib_year=2019, bib_authors="Dana", bib_venue="OSDI",
            bib_citation_count=314, bib_semantic_scholar_id="ss42",
        )

        assert cat.update_document_collection(str(tumbler), "docs__new") is True

        entry = cat.resolve(tumbler)
        assert entry.physical_collection == "docs__new"
        assert entry.bib_year == 2019
        assert entry.bib_authors == "Dana"
        assert entry.bib_venue == "OSDI"
        assert entry.bib_citation_count == 314
        assert entry.bib_semantic_scholar_id == "ss42"

    def test_update_documents_collection_batch_preserves_enriched_bib_columns(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, _ = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "a.md", content_type="prose",
            file_path="a.md", physical_collection="docs__old",
        )
        cat.update(
            tumbler, bib_year=2019, bib_authors="Dana", bib_venue="OSDI",
            bib_citation_count=314, bib_semantic_scholar_id="ss42",
        )

        n = cat.update_documents_collection_batch([(str(tumbler), "docs__new")])
        assert n == 1

        entry = cat.resolve(tumbler)
        assert entry.physical_collection == "docs__new"
        assert entry.bib_year == 2019
        assert entry.bib_authors == "Dana"
        assert entry.bib_venue == "OSDI"
        assert entry.bib_citation_count == 314
        assert entry.bib_semantic_scholar_id == "ss42"


# ── End-to-end: full replay equals live SQLite ────────────────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestFullReplayEqualsLive:
    """nexus-i711w Stage 2: DIE. Subject IS the local event-sourcing
    machinery — ``events.jsonl`` replayed through the ``Projector`` into a
    fresh ``CatalogDB`` and diffed against the live ``.catalog.db``. The
    service catalog is Postgres with no event log and no projection to
    rebuild, so there is nothing on that side to diff. Retires with
    ``nexus/catalog/{catalog,catalog_db,event_log,projector,events}.py``.

    These two are also what still pins the alias and the rename/bib EVENT
    emission that the ported tests above dropped."""

    def test_register_update_alias_delete_replay_matches(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        from nexus.catalog.catalog_db import CatalogDB
        from nexus.catalog.event_log import EventLog
        from nexus.catalog.projector import Projector

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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

    def test_shadow_emit_rename_replay_reconstructs_bib_columns(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-6ha8a follow-up (cre-auto finding 4): rename_collection's
        shadow-emit loop (ES=0 + shadow-emit on) writes events.jsonl for
        future replay. Confirm the replayed JSONL actually reconstructs
        the enriched bib_* values on the renamed row — not just that the
        live SQLite happens to be correct."""
        from nexus.catalog.catalog_db import CatalogDB
        from nexus.catalog.event_log import EventLog
        from nexus.catalog.projector import Projector

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        cat, d = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        tumbler = cat.register(
            owner, "a.md", content_type="prose",
            file_path="a.md", physical_collection="docs__old",
        )
        cat.update(
            tumbler, bib_year=2019, bib_authors="Dana", bib_venue="OSDI",
            bib_citation_count=314, bib_semantic_scholar_id="ss42",
        )
        n = cat.rename_collection("docs__old", "docs__new")
        assert n == 1
        cat._db.close()

        # Replay events.jsonl into a fresh CatalogDB.
        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        with sqlite3.connect(str(tmp_path / "projected.db")) as proj:
            row = proj.execute(
                "SELECT physical_collection, bib_year, bib_authors, "
                "bib_venue, bib_citation_count, bib_semantic_scholar_id "
                "FROM documents WHERE tumbler = ?",
                (str(tumbler),),
            ).fetchone()
        assert row == ("docs__new", 2019, "Dana", "OSDI", 314, "ss42")


# ── Shadow emit still suppressed ─────────────────────────────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestShadowEmitSuppressedAcrossMutators:
    """nexus-i711w Stage 2: DIE. Subject is the shadow-emit double-write
    gate over ``events.jsonl`` — a local-only artifact. This is also the
    test that still pins the exact owner/register/update/delete EVENT
    sequence the two ported mutator tests above dropped."""

    def test_no_double_writes_when_both_gates_on(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        cat, d = _local_catalog(tmp_path)
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
