# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o8dil.5 Fix 1 (P0, CRITICAL): the TTL sweep must not leak.

``nx store put --ttl Nd`` unconditionally registers a catalog manifest row
for the note (``commands/store.py``'s ``put_cmd`` never gates catalog
registration on ``ttl_days``). ``PgVectorRepository#delete``'s anti-join
(RDR-191 F10c fix, bead nexus-o8dil.5 round 1) refuses to delete a chash any
LIVE manifest row still references. Pre-fix (this bead's round 2),
``HttpVectorClient.expire()`` deleted the T3 chunk WITHOUT first retracting
that manifest row -- so a TTL-lapsed note's chunk survived the delete call
silently (the anti-join blocked it) while ``expire()`` still reported
"Expired N entries", a false-success signal. No later GC pass reclaims it
either: ``nx t3 gc`` treats any manifest reference, dangling or not, as
"keep". That is a permanent, silent leak -- exactly the class of bug the
project's fail-loud-on-correctness directive bans.

The fix retracts the manifest (tombstones the owning catalog document,
mirroring what ``nx store delete`` does) BEFORE deleting the chunk, and
reports the delete's ACTUAL count (from the server's own ``{"deleted": N}``
response) rather than the number of TTL-lapsed candidates found.

Real engine substrate (``t2_service_env``) is required -- the bug lives at
the PgVectorRepository anti-join boundary in the real Postgres service; a
mocked T3 client (``tests/test_t3.py``'s pre-existing ``expire()`` coverage,
and ``tests/test_http_vector_client_parity.py::TestExpire``'s mocked-``_post``
coverage) cannot see it, since both mock the delete response as
unconditional full success.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.integration]

_COLLECTION = "knowledge__o8dil5-expire__bge-base-en-v15-768__v1"


def _old_iso(days_ago: int = 100) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _seed_ttl_lapsed_note(client, cat, content: str, title: str) -> str:
    """Seed a TTL-lapsed knowledge note exactly the way `nx store put --ttl`
    would have, minus the CLI's own current-time stamping (which cannot be
    backdated without sleeping) -- production catalog registration +
    manifest write, real T3 chunk with TTL-expired metadata baked in.

    Returns the chash (T3 natural id / manifest chash).
    """
    from nexus.catalog.store_hook import (
        catalog_store_hook_tracked,
        single_chunk_manifest_metadata,
        store_put_manifest_direct,
    )

    chash, manifest_metadatas = single_chunk_manifest_metadata(content)
    assert chash == hashlib.sha256(content.encode()).hexdigest()

    # Real production catalog registration (commands/store.py put_cmd's
    # exact call) -- mints the tumbler, no file_path, content_type=knowledge.
    tumbler, created = catalog_store_hook_tracked(
        title=title, doc_id=chash, collection_name=_COLLECTION,
    )
    assert created is True

    # The T3 chunk itself, with TTL-expired metadata baked in directly --
    # db.put() always stamps indexed_at=now with no override, so a test
    # cannot produce an already-expired row through it without sleeping.
    # RDR-194 P3d / catalog-029-manifest-chunk-fk.xml: this MUST land before
    # the manifest write below -- the FK now refuses a manifest row naming a
    # chash with no matching nexus.chunks row, so the chunk-before-manifest
    # order production's real hook already follows (upsert_chunks, THEN the
    # post-store manifest hook) has to hold here too, not just be produced
    # in some order that happened to work before the FK existed.
    client.upsert_chunks_with_embeddings(
        _COLLECTION,
        ids=[chash],
        documents=[content],
        embeddings=[],
        metadatas=[{
            "title": title,
            "ttl_days": 1,
            "indexed_at": _old_iso(),
            "chunk_text_hash": chash,
            "doc_id": tumbler,
        }],
    )

    # Real production manifest write (put_cmd's exact call).
    store_put_manifest_direct(tumbler, manifest_metadatas, collection=_COLLECTION)
    return chash


def _chunk_present(client, chash: str) -> bool:
    from nexus.errors import CollectionNotFoundError

    try:
        result = client.get_collection(_COLLECTION).get(ids=[chash], include=[])
    except CollectionNotFoundError:
        # An emptied collection (its last chunk just got reclaimed) is not
        # listed by list_collections() at all -- absence of the collection
        # means absence of the chunk, not an error.
        return False
    return chash in (result.get("ids") or [])


def test_expire_reclaims_ttl_lapsed_chunk_whose_manifest_still_references_it(t2_service_env):
    """The chunk must actually be gone after expire(), and the reported
    count must match what was really reclaimed -- not the number of
    TTL-lapsed candidates found."""
    import nexus.db.http_vector_client as hvc
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    client = hvc.HttpVectorClient(tenant=tenant)

    content = "o8dil5 expire-reap fixture content, TTL 1 day, indexed 100 days ago"
    chash = _seed_ttl_lapsed_note(client, cat, content, title="o8dil5-expire-note")

    # Control: both halves are live before expire() runs.
    assert _chunk_present(client, chash), "control: the T3 chunk must exist before expire()"

    count = client.expire()

    assert count == 1, (
        "expire() must report exactly the one entry it actually reclaimed -- "
        "a count that does not match reality is the false-success signal "
        "this bead closes"
    )
    assert not _chunk_present(client, chash), (
        "the TTL-lapsed chunk must actually be gone from T3. Surviving here "
        "means expire() reported success while PgVectorRepository#delete's "
        "anti-join silently refused the delete because the note's own "
        "catalog manifest row was never retracted first -- and nothing "
        "else ever reclaims it (nx t3 gc treats any manifest reference, "
        "dangling or not, as keep)"
    )


def test_expire_preserves_a_chunk_genuinely_shared_with_a_live_document(t2_service_env):
    """Non-vacuity control in the other direction: a TTL-lapsed note whose
    content happens to be identical to another STILL-LIVE document's chunk
    (RDR-108 collapse-by-design) must survive -- the anti-join protecting
    real shared content is correct behaviour, and expire()'s reported count
    must reflect that (0 reclaimed), not silently claim the shared chunk
    was deleted."""
    import nexus.db.http_vector_client as hvc
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    client = hvc.HttpVectorClient(tenant=tenant)

    content = "o8dil5 shared-content fixture: identical text, one TTL note, one permanent"
    chash = _seed_ttl_lapsed_note(client, cat, content, title="o8dil5-ttl-twin")

    # A second, PERMANENT document manifesting the SAME chash, registered
    # directly (bypassing catalog_store_hook_tracked's own dedup-by-chash,
    # which would otherwise collapse this onto the TTL note's tumbler
    # instead of minting a genuinely separate owner) -- mirrors
    # tests/test_5axey_chash_catalog_lookups.py's
    # _register_knowledge_doc_directly, constructing the two-owners-one-
    # chash state RDR-108's chunk collapse produces by design.
    owner = cat.register_owner("o8dil5-expire-shared", "curator")
    permanent_tumbler = cat.register(
        owner, "o8dil5-permanent-twin", content_type="knowledge",
        physical_collection=_COLLECTION, meta={"doc_id": chash},
    )
    cat.append_manifest_chunks(
        str(permanent_tumbler), [{"chash": chash, "position": 0}], collection=_COLLECTION,
    )
    cat.resync_chunk_count_cache(str(permanent_tumbler))

    assert _chunk_present(client, chash), "control: the shared chunk must exist before expire()"

    count = client.expire()

    assert count == 0, (
        "the shared chunk's owner (the TTL note) must fail to reclaim it "
        "because the permanent twin's manifest row still references it -- "
        "expire() must report 0, not silently claim a delete that the "
        "anti-join correctly refused"
    )
    assert _chunk_present(client, chash), (
        "the chunk must survive: a live document's manifest still references it"
    )


def test_expire_does_not_reap_a_twin_owned_by_a_different_collection(t2_service_env):
    """nexus-h7nax (RDR-191 round-3 critique): ``expire()`` calls
    ``reap_catalog_manifest_for_chashes(expired_ids)`` WITHOUT
    ``expected_collection=name`` -- unlike ``nx store delete --id`` / MCP
    ``store_delete`` (nexus-c53hy), which both pass it. The test above
    (``test_expire_preserves_a_chunk_genuinely_shared_with_a_live_document``)
    cannot see this: it registers its "permanent twin" under the SAME
    ``_COLLECTION`` as the TTL note, so both catalog documents are valid
    ``resolve_knowledge_doc_for_chash`` candidates and the ambiguous-chash
    guard (2+ candidates -> None, no-op) protects the twin regardless of
    ``expected_collection`` -- the guard this bead adds is never exercised.

    To reach the guard, the reap's chash resolution must land on exactly
    ONE candidate that is NOT the note being expired -- so this test seeds
    the TTL-lapsed T3 chunk directly (no catalog registration of its own,
    unlike ``_seed_ttl_lapsed_note``), and registers only the "twin" as a
    catalog document, under a DIFFERENT physical_collection. The chash
    resolves unambiguously to the twin; without the collection guard the
    reap tombstones it purely because it happens to share content with an
    unrelated collection's TTL-lapsed note.

    PgVectorRepository#delete's anti-join is itself collection-scoped
    (``catalog_document_chunks.collection`` is part of the "still
    referenced" join predicate) -- the twin's manifest row records ITS OWN
    physical_collection, not ``_COLLECTION``, so it never blocks deleting
    the TTL note's chunk from ``_COLLECTION`` either way. That means the
    fix does not trade "twin destroyed" for "note's own chunk stuck" --
    both must hold simultaneously post-fix.
    """
    import nexus.db.http_vector_client as hvc
    from tests._catalog_fixture_ops import ActiveCatalog, documents_by_title

    tenant = t2_service_env
    cat = ActiveCatalog()
    client = hvc.HttpVectorClient(tenant=tenant)

    content = "o8dil5/h7nax cross-collection twin fixture: TTL note with no catalog row of its own"
    chash = hashlib.sha256(content.encode()).hexdigest()

    # The TTL-lapsed T3 chunk, seeded WITHOUT its own catalog registration --
    # deliberately, so resolve_knowledge_doc_for_chash's global lookup finds
    # exactly one (WRONG) candidate instead of tripping the ambiguous-chash
    # no-op the test above relies on.
    client.upsert_chunks_with_embeddings(
        _COLLECTION,
        ids=[chash],
        documents=[content],
        embeddings=[],
        metadatas=[{
            "title": "o8dil5-h7nax-uncataloged-ttl-note",
            "ttl_days": 1,
            "indexed_at": _old_iso(),
            "chunk_text_hash": chash,
        }],
    )

    other_collection = "knowledge__o8dil5-h7nax-twin__bge-base-en-v15-768__v1"
    owner = cat.register_owner("o8dil5-h7nax-twin-owner", "curator")
    twin_title = "o8dil5-h7nax-cross-collection-twin"
    twin_tumbler = cat.register(
        owner, twin_title, content_type="knowledge",
        physical_collection=other_collection, meta={"doc_id": chash},
    )
    # RDR-194 P3d / catalog-029-manifest-chunk-fk.xml: the FK keys on
    # (tenant, collection, chash) -- the T3 chunk above lives under
    # _COLLECTION, not other_collection, so the twin's manifest row needs
    # its OWN nexus.chunks row under other_collection before the append
    # below. Same content/chash, a second physical copy under the twin's
    # own collection -- exactly what a real cross-collection duplicate
    # would have indexed.
    client.upsert_chunks_with_embeddings(
        other_collection,
        ids=[chash],
        documents=[content],
        embeddings=[],
        metadatas=[{"title": twin_title, "chunk_text_hash": chash}],
    )
    cat.append_manifest_chunks(
        str(twin_tumbler), [{"chash": chash, "position": 0}], collection=other_collection,
    )
    cat.resync_chunk_count_cache(str(twin_tumbler))

    assert len(documents_by_title(twin_title)) == 1, "control: twin must exist before expire()"
    assert _chunk_present(client, chash), "control: TTL chunk must exist before expire()"

    count = client.expire()

    assert len(documents_by_title(twin_title)) == 1, (
        "the twin, registered under a DIFFERENT physical_collection than "
        "the one expire() is sweeping, must survive: its chash "
        "coincidentally matching the TTL-lapsed note's own chunk must not "
        "let the reap resolve onto it and tombstone it (nexus-h7nax -- "
        "expire() omits expected_collection=name at the reap call site)"
    )
    assert count == 1, (
        "the TTL note's own chunk (in _COLLECTION) must still be correctly "
        "reclaimed -- the collection guard protecting the twin must not "
        "also block the common single-owner reclaim it exists to allow"
    )
    assert not _chunk_present(client, chash), (
        "the TTL-lapsed chunk in _COLLECTION must actually be gone"
    )
