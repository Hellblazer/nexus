# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-i711w.1 exit-criteria contracts — service-side twins of the GAP items.

Each test here is the SERVICE-SIDE behavioural twin of a pinned local-catalog
spec test (annotated ``# GAP nexus-i711w.1 item N`` in
``tests/test_catalog_event_sourced_mutators.py`` and
``tests/test_catalog_event_sourced_register.py``). The pinned tests are the
SPECIFICATION and retire together with ``src/nexus/catalog/{catalog,
catalog_db,event_log,projector,events}.py``; these tests carry each spec's
assertions over BY MEANING against a REAL ``HttpCatalogClient`` talking to the
REAL Java engine + Postgres (same fixture stack as
``tests/db/test_http_catalog_integration.py`` — never a FakeCatalogHandler).

Where the pinned spec's only observable was the emitted ``events.jsonl``
payload, the service twin asserts the equivalent ROW state through the public
read API (``resolve`` / ``get_manifest``): the event-sourced local arm projects
the payload straight into the row, so row state after the verb is the
substrate-neutral expression of the same contract.

Where the ENGINE diverges from the spec, the assertion is written exactly as
the spec says and marked ``xfail(strict=True)`` with the Java evidence — a
divergence here is a product finding, not a test to weaken:

  item 1  update() re-derive of chunk_count      — DIVERGES (see test)
  item 3  update() indexed_at refresh            — CONVERGED (nexus-927mo, 2026-08-01)
  item 8  delete_document() manifest cascade     — DIVERGES (see test)

Marked @pytest.mark.integration — skipped automatically when the service jar,
PG binaries, or a JVM are absent (identical gating to
test_http_catalog_integration.py).

Fixture stack: IMPORTED from tests/db/test_http_catalog_integration.py (the
test_http_shape_parity_integration.py precedent, same as the sibling
test_i711w_gap_xfails.py / test_i711w_gap_contracts_fresh.py); pytest caches
module-scoped fixtures per requesting module, so this file still gets its own
fresh PG + service instance and cannot contaminate (or be contaminated by)
any sibling module's data.
"""
from __future__ import annotations

import time
import uuid

import pytest

# pytest resolves imported fixtures by name (same pattern as
# tests/db/test_http_shape_parity_integration.py). Each module gets its own
# module-scoped instances — fresh PG + fresh service for this file.
from tests.db.test_http_catalog_integration import (  # noqa: F401, PLC2701 — pytest resolves imported fixtures by name
    _ALL_PREREQS,
    _JAR,
    _JAVA,
    _PG_CTL,
    _seed_chunks,
    cat,
    pg_instance,
    service,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────────────


def _ch(seed: str) -> str:
    """Full 64-lowercase-hex sha256 chash (RDR-180 manifest boundary guard)."""
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


def _slug() -> str:
    """Per-test discriminator for file paths / collections.

    The service catalog is shared across this module's tests within one
    tenant; register()'s file_path idempotency would otherwise silently
    alias two tests' documents onto one row.
    """
    return uuid.uuid4().hex[:8]


_BIB_KWARGS = {
    "bib_year": 2019,
    "bib_authors": "Dana",
    "bib_venue": "OSDI",
    "bib_citation_count": 314,
    "bib_semantic_scholar_id": "ss42",
}


def _assert_bib_preserved(entry) -> None:
    """The five enriched bib_* values planted by _BIB_KWARGS survive."""
    assert entry.bib_year == 2019
    assert entry.bib_authors == "Dana"
    assert entry.bib_venue == "OSDI"
    assert entry.bib_citation_count == 314
    assert entry.bib_semantic_scholar_id == "ss42"


def _manifest_rows(n: int, seed: str) -> list[dict]:
    return [
        {
            "position": pos,
            "chash": _ch(f"{seed}-{pos}"),
            "chunk_index": pos,
            "line_start": None,
            "line_end": None,
            "char_start": None,
            "char_end": None,
        }
        for pos in range(n)
    ]


def _seed_manifest_rows(pg_instance: dict, collection: str, rows: list[dict]) -> None:
    """RDR-194 P3d / catalog-029-manifest-chunk-fk.xml: seed the nexus.chunks
    rows a manifest write for *rows* needs before the FK-enforcing engine
    accepts it. See tests/db/test_http_catalog_integration.py's _seed_chunks
    for the underlying pattern."""
    _seed_chunks(pg_instance, "default", collection, [r["chash"] for r in rows])


# ── Module-scoped fixtures (engine stack imported above; owner is local) ──────


@pytest.fixture(scope="module")
def owner(cat) -> str:
    """One shared owner for the module; per-test uniqueness comes from _slug().

    Registered WITHOUT repo_root on purpose: item 9 must exercise the
    file_path idempotency leg, and a repo_root would make the engine derive a
    source_uri from relative file_paths (CatalogRepository.deriveSourceUri,
    service/.../db/CatalogRepository.java:402-412), re-routing idempotency
    through the source_uri leg instead.
    """
    t = cat.register_owner(
        name="i711w-gap-contracts",
        owner_type="curator",
        tumbler_prefix="1.1",
    )
    return str(t)


# ── update() ──────────────────────────────────────────────────────────────────


class TestUpdateContracts:
    def test_update_re_derives_chunk_count_from_manifest_when_omitted(
        self, cat, owner, pg_instance,
    ) -> None:
        """update() with no chunk_count must refresh it from the manifest count.

        # nexus-i711w.1 item 1
        Spec: tests/test_catalog_event_sourced_mutators.py::TestUpdateEventSourced::
        test_update_re_derives_chunk_count_from_manifest_when_omitted
        (nexus-zq79 F4). The spec's observable was the emitted event payload;
        the service-side observable is the row itself, which the local
        event-sourced arm projects that payload into.
        """
        slug = _slug()
        tumbler = cat.register(
            owner, "re-derive doc", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        collection = f"knowledge__i711w-{slug}__voyage-context-3__v1"
        rows = _manifest_rows(5, f"rederive-{slug}")
        _seed_manifest_rows(pg_instance, collection, rows)
        cat.append_manifest_chunks(str(tumbler), rows, collection=collection)
        # DE-CONFOUNDING (2026-07-30). The original shape — append 5 rows, then
        # assert 5 after a head_hash-only update — stopped proving anything the
        # moment nexus-e4gel ALSO made append_manifest_chunks fold the count:
        # the count was already 5 before update() ran, so the update-path
        # re-derivation this test is named for could be removed entirely and it
        # would still pass. (Its old comment asserted the append path does NOT
        # fold; that is no longer true.) Forcing an explicit, DISAGREEING count
        # first means only the update path can restore it.
        cat.update(tumbler, chunk_count=99)
        assert cat.resolve(tumbler).chunk_count == 99, (
            "precondition: an explicit chunk_count must win, leaving the row "
            "disagreeing with its 5-row manifest"
        )
        # Update with head_hash only — no chunk_count in fields.
        cat.update(tumbler, head_hash="updated")

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.chunk_count == 5, (
            f"expected re-derived chunk_count=5, got {entry.chunk_count} — "
            "update() must re-derive chunk_count from the document_chunks "
            "count when the caller omits it (nexus-zq79 F4)"
        )

    def test_update_respects_caller_supplied_chunk_count(
        self, cat, owner, pg_instance,
    ) -> None:
        """An explicit chunk_count must NOT be re-derived — caller intent wins.

        # nexus-i711w.1 item 2
        Spec: tests/test_catalog_event_sourced_mutators.py::TestUpdateEventSourced::
        test_update_respects_caller_supplied_chunk_count (nexus-zq79 F4,
        orphan-backfill paths).
        """
        slug = _slug()
        tumbler = cat.register(
            owner, "caller-count doc", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        # 3 manifest rows present, but caller wants to assert 99.
        collection = f"knowledge__i711w-{slug}__voyage-context-3__v1"
        rows = _manifest_rows(3, f"caller-{slug}")
        _seed_manifest_rows(pg_instance, collection, rows)
        cat.append_manifest_chunks(str(tumbler), rows, collection=collection)
        cat.update(tumbler, chunk_count=99)

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.chunk_count == 99

    def test_update_refreshes_indexed_at_when_head_hash_changes(
        self, cat, owner,
    ) -> None:
        """indexed_at must advance when update() changes head_hash.

        # nexus-i711w.1 item 3
        Spec: tests/test_catalog_event_sourced_mutators.py::TestUpdateEventSourced::
        test_update_refreshes_indexed_at_when_head_hash_changes (nexus-zq79 F7).
        The local Catalog stamps indexed_at at register time; the engine
        leaves it NULL, so plant a known original through the public update()
        (indexed_at is on the engine's UPDATABLE_DOC_COLUMNS whitelist) to
        keep the spec's before/after comparison non-degenerate.
        """
        slug = _slug()
        original_at = "2020-01-01T00:00:00.000000+00:00"
        tumbler = cat.register(
            owner, "indexed-at doc", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        cat.update(tumbler, indexed_at=original_at)
        assert cat.resolve(tumbler).indexed_at == original_at  # precondition

        time.sleep(0.01)
        cat.update(tumbler, head_hash="rev2")

        refreshed_at = cat.resolve(tumbler).indexed_at
        assert refreshed_at != original_at, (
            f"indexed_at must advance on re-index; "
            f"original={original_at!r} refreshed={refreshed_at!r}"
        )

    def test_update_without_bib_kwargs_preserves_existing_bib(
        self, cat, owner,
    ) -> None:
        """An update not passing bib_* must carry current values forward.

        # nexus-i711w.1 item 4
        Spec: tests/test_catalog_event_sourced_mutators.py::TestUpdateEventSourced::
        test_event_sourced_update_without_bib_kwargs_preserves_existing_bib
        (nexus-6ha8a clobber regression, update() emission site).
        """
        slug = _slug()
        tumbler = cat.register(
            owner, "bib-preserve doc", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        cat.update(tumbler, bib_year=2020, bib_authors="X")
        cat.update(tumbler, chunk_count=9)

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.chunk_count == 9
        assert entry.bib_year == 2020
        assert entry.bib_authors == "X"


# ── rename_collection() / update_document_collection() ────────────────────────


class TestCollectionMoveBibPreservation:
    def test_rename_preserves_enriched_bib_columns(self, cat, owner) -> None:
        """bib_* preservation across rename_collection.

        # nexus-i711w.1 item 5
        Spec: tests/test_catalog_event_sourced_mutators.py::
        TestRenameCollectionEventSourced::test_rename_preserves_enriched_bib_columns
        (nexus-6ha8a). Engine: renameCollectionTxn re-homes catalog_documents
        with a bare SET physical_collection (CatalogRepository.java:2434),
        which cannot clobber bib_*.

        register_collection first: the service verb is POST /collections/rename
        and 404s unless a catalog_collections row exists (the measured
        precondition divergence documented on the ported
        test_rename_moves_every_row_in_the_collection).
        """
        slug = _slug()
        old, new = f"docs__old_{slug}", f"docs__new_{slug}"
        cat.register_collection(old, content_type="docs")
        tumbler = cat.register(
            owner, "rename-bib doc", content_type="prose",
            file_path=f"{slug}/a.md", physical_collection=old,
        )
        cat.update(tumbler, **_BIB_KWARGS)

        n = cat.rename_collection(old, new)
        assert n == 1

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.physical_collection == new
        _assert_bib_preserved(entry)

    def test_update_document_collection_preserves_enriched_bib_columns(
        self, cat, owner,
    ) -> None:
        """bib_* preservation across update_document_collection.

        # nexus-i711w.1 item 6
        Spec: tests/test_catalog_event_sourced_mutators.py::
        TestUpdateDocumentCollectionEventSourced::
        test_update_document_collection_preserves_enriched_bib_columns
        (nexus-6ha8a, fourth DocumentRegisteredPayload emission site). The
        service arm posts a bare /update with only physical_collection
        (http_catalog_client.py:1844-1850) — exactly the emission site whose
        clobber behaviour had no service-side test.
        """
        slug = _slug()
        tumbler = cat.register(
            owner, "udc-bib doc", content_type="prose",
            file_path=f"{slug}/a.md", physical_collection=f"docs__old_{slug}",
        )
        cat.update(tumbler, **_BIB_KWARGS)

        assert cat.update_document_collection(str(tumbler), f"docs__new_{slug}") is True

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.physical_collection == f"docs__new_{slug}"
        _assert_bib_preserved(entry)

    def test_update_documents_collection_batch_preserves_enriched_bib_columns(
        self, cat, owner,
    ) -> None:
        """bib_* preservation across update_documents_collection_batch.

        # nexus-i711w.1 item 7
        Spec: tests/test_catalog_event_sourced_mutators.py::
        TestUpdateDocumentCollectionEventSourced::
        test_update_documents_collection_batch_preserves_enriched_bib_columns
        (nexus-6ha8a). The service batch form is a client-side loop over
        update_document_collection (http_catalog_client.py:1852-1860,
        server-side batch tracked by nexus-gmiaf.24).
        """
        slug = _slug()
        tumbler = cat.register(
            owner, "batch-bib doc", content_type="prose",
            file_path=f"{slug}/a.md", physical_collection=f"docs__old_{slug}",
        )
        cat.update(tumbler, **_BIB_KWARGS)

        n = cat.update_documents_collection_batch([(str(tumbler), f"docs__new_{slug}")])
        assert n == 1

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.physical_collection == f"docs__new_{slug}"
        _assert_bib_preserved(entry)


# ── delete_document() ─────────────────────────────────────────────────────────


class TestDeleteDocumentContracts:
    def test_delete_cascades_to_document_chunks(self, cat, owner, pg_instance) -> None:
        """delete_document must cascade-purge the document_chunks manifest.

        # nexus-i711w.1 item 8
        Spec: tests/test_catalog_event_sourced_mutators.py::
        TestDeleteDocumentEventSourced::test_delete_cascades_to_document_chunks
        (+ the legacy-path half; nexus-8g79.7). The spec asserted the
        document_chunks count via raw SQL; get_manifest() is the public read
        of the same storage state, so no raw SQL is needed here.
        """
        slug = _slug()
        tumbler = cat.register(
            owner, "delete-cascade doc", content_type="prose",
            file_path=f"{slug}/doc.md",
        )
        collection = f"knowledge__i711w-{slug}__voyage-context-3__v1"
        rows = _manifest_rows(3, f"del-{slug}")
        _seed_manifest_rows(pg_instance, collection, rows)
        cat.append_manifest_chunks(str(tumbler), rows, collection=collection)
        assert len(cat.get_manifest(str(tumbler))) == 3  # precondition

        assert cat.delete_document(tumbler) is True

        # documents row gone (both substrates agree: resolve → None) AND
        # document_chunks rows gone.
        assert cat.resolve(tumbler) is None
        manifest_after = cat.get_manifest(str(tumbler))
        assert manifest_after == [], (
            "a deleted document's manifest must read EMPTY; pre-fix "
            f"this left {len(manifest_after)} readable rows. NOTE the engine "
            "satisfies this by TOMBSTONE-FILTERING the read (nexus-mqd6t), "
            "not by physically purging: the rows survive on disk so "
            "document_restore can bring the document back whole (RDR-156 "
            "P1.2). The observable contract — deleted means unreadable — is "
            "identical; the storage mechanism is not."
        )


# ── register() ────────────────────────────────────────────────────────────────


class TestRegisterIdempotency:
    def test_register_same_file_path_twice_returns_same_tumbler(
        self, cat, owner,
    ) -> None:
        """Second register of the same file_path: SAME tumbler, no second row.

        # nexus-i711w.1 item 9
        Spec: tests/test_catalog_event_sourced_register.py::
        TestIdempotencyUnderEventSourced::
        test_register_same_file_path_twice_returns_same_tumbler. The spec's
        "no duplicate" half counted DocumentRegistered events; the
        service-side observable is the row count for that file_path under the
        owner. No source_uri is passed and the owner has no repo_root, so the
        engine cannot derive one (deriveSourceUri returns '' for a relative
        path with no repo_root, CatalogRepository.java:402-412) — this
        exercises exactly the file_path idempotency leg
        (CatalogRepository.java:456-465), not the source_uri leg.
        """
        slug = _slug()
        file_path = f"{slug}/doc.md"
        first = cat.register(
            owner, "idempotent doc", content_type="prose", file_path=file_path,
        )
        second = cat.register(
            owner, "idempotent doc", content_type="prose", file_path=file_path,
        )
        assert str(first) == str(second)

        # Only one live row for that file_path under this owner.
        rows = [
            d for d in cat.by_owner(owner) if d.file_path == file_path
        ]
        assert len(rows) == 1, (
            f"expected exactly one document row for {file_path!r}, "
            f"got {len(rows)}: {[str(d.tumbler) for d in rows]}"
        )
