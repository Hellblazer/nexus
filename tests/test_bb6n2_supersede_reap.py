# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bb6n2: store_put's supersede path must reap the superseded
note's now-unreferenced chunk(s) in the same call.

Pre-fix, re-putting the same (collection, title) with changed content
reconciled the CATALOG row onto the new chash(es) but left the OLD T3
chunk row behind: unreferenced by any manifest, still returned by raw
vector search, accumulating one orphan per content-changing re-put
(measured live: nexus/nexus-bb6n2-measurement-2026-09-23, 97 orphans
across 4 knowledge__* collections on the production tenant).

Root cause (confirmed by reading ``store_put_manifest_direct``): it
bypasses the generic ``fire_batch``/``manifest_write_batch_hook`` chain
entirely (a deliberate, load-bearing, fail-loud path — see its own
docstring), so the indexer's ``mcp_infra._sweep_superseded_vectors``
mechanism, which reaps this exact class for ``atomic_manifest_replace``
callers that DO go through that chain, was never reachable from here.

Real engine substrate (``t2_service_env``) is required, matching the
sibling nexus-rnqbw suite this borrows its seeding shape from — the
guard chain under test (``orphaned_chashes`` -> ``docs_for_chashes``,
``live_note_chashes`` -> a real catalog document listing, and T3
``delete``) is a genuine PgVectorRepository/CatalogRepository round
trip, not something an in-memory double can stand in for.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]

_COLLECTION = "knowledge__bb6n2-supersede-reap__bge-base-en-v15-768__v1"


def _chunk_present(client, chash: str) -> bool:
    from nexus.errors import CollectionNotFoundError

    try:
        result = client.get_collection(_COLLECTION).get(ids=[chash], include=[])
    except CollectionNotFoundError:
        return False
    return chash in (result.get("ids") or [])


def _put_note(client, *, title: str, content: str) -> tuple[str, list[str]]:
    """Real store_put-shaped write (single or split), mirroring the MCP
    ``store_put`` tool's own call sequence: catalog reconcile, T3
    chunk(s), then the direct manifest write under test. Returns
    ``(tumbler, chashes)``.
    """
    from nexus.catalog.store_hook import (
        catalog_store_hook_tracked,
        note_manifest_metadata,
        note_pieces,
        store_put_manifest_direct,
    )

    pieces = note_pieces(content, _COLLECTION)
    doc_id, manifest_metadatas = note_manifest_metadata(pieces)
    tumbler, _created = catalog_store_hook_tracked(
        title=title, doc_id=doc_id, collection_name=_COLLECTION,
    )
    chashes = [m["chunk_text_hash"] for m in manifest_metadatas]
    for piece, chash, meta in zip(pieces, chashes, manifest_metadatas):
        client.upsert_chunks_with_embeddings(
            _COLLECTION,
            ids=[chash],
            documents=[piece],
            embeddings=[],
            metadatas=[{"title": title, "chunk_text_hash": chash, "doc_id": tumbler}],
        )
    store_put_manifest_direct(tumbler, manifest_metadatas, collection=_COLLECTION)
    return tumbler, chashes


def test_supersede_reaps_the_old_chunk_in_the_same_call(t2_service_env):
    """Re-putting the same (collection, title) with DIFFERENT content
    must delete the old, now-unreferenced chunk from T3 in the same
    call — not merely reconcile the catalog row onto the new chash and
    leave the old one behind (the bug nexus-bb6n2 names)."""
    import nexus.db.http_vector_client as hvc

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    title = "bb6n2-note"

    _tumbler, old_chashes = _put_note(client, title=title, content="version one of this note")
    assert len(old_chashes) == 1
    old_chash = old_chashes[0]
    assert _chunk_present(client, old_chash), "control: old chunk must exist after the first put"

    _tumbler2, new_chashes = _put_note(
        client, title=title, content="version TWO of this note, completely different text",
    )
    assert new_chashes != old_chashes

    assert not _chunk_present(client, old_chash), (
        "the superseded chunk must be reaped in the same call as the "
        "re-put that dropped it from the manifest — it must not survive "
        "as an orphan, unreferenced by any manifest, still returned by "
        f"raw vector search. old_chash={old_chash!r}"
    )
    assert _chunk_present(client, new_chashes[0]), "control: the new chunk must exist"


def test_supersede_still_protects_a_chunk_genuinely_shared_with_a_live_document(
    t2_service_env,
):
    """Non-vacuity control: content-addressed sharing means identical
    chunk text is legitimately ONE T3 row referenced by several
    documents (CLAUDE.md § catalog/T3 split) — the reap must not delete
    a chash a DIFFERENT, still-live document's manifest still
    references, even though the superseded document no longer does."""
    import nexus.db.http_vector_client as hvc
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    cat = ActiveCatalog()

    shared_content = "bb6n2 shared-content fixture: one superseded note, one permanent twin"

    _tumbler, old_chashes = _put_note(client, title="bb6n2-twin-note", content=shared_content)
    shared_chash = old_chashes[0]
    assert _chunk_present(client, shared_chash), "control: shared chunk must exist before supersede"

    # A second, permanent document manifesting the SAME chash — the
    # sibling fixture shape test_rnqbw_same_call_reap_ordering.py's own
    # shared-content control uses.
    owner = cat.register_owner("bb6n2-shared", "curator")
    permanent_tumbler = cat.register(
        owner, "bb6n2-permanent-twin", content_type="knowledge",
        physical_collection=_COLLECTION, meta={"doc_id": shared_chash},
    )
    cat.append_manifest_chunks(
        str(permanent_tumbler), [{"chash": shared_chash, "position": 0}], collection=_COLLECTION,
    )
    cat.resync_chunk_count_cache(str(permanent_tumbler))

    # Now supersede the FIRST note with different content — its own
    # manifest drops shared_chash, but the permanent twin's manifest
    # still references it.
    _put_note(client, title="bb6n2-twin-note", content="bb6n2 superseding content, unrelated text")

    assert _chunk_present(client, shared_chash), (
        "a chunk another LIVE document's manifest still references must "
        f"survive a sibling document's supersede. shared_chash={shared_chash!r}"
    )
