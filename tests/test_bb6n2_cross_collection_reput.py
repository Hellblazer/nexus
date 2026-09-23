# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bb6n2 round 2: a store_put reconcile must key on (collection,
title), never adopt a DIFFERENT collection's document by chash
coincidence.

Found live while moving RDR post-mortems: `nx store put <file>
--collection nexus-rdr-research --title <T>`, where a catalog document
titled <T> already existed under a DIFFERENT collection
(``knowledge__1-1``):

- A 1-chunk (unsplit) note correctly minted a NEW document in the new
  collection.
- A 6-chunk (split) note instead reconciled onto the EXISTING document
  from the old collection: it rewrote that document's manifest
  (``meta.doc_id``) to the new content's chunks, but left
  ``physical_collection`` and ``source_uri`` still naming the OLD
  collection — the catalog then disagreed with T3 (repaired by hand).

Root cause (confirmed by reading ``catalog_store_hook_tracked``):
``resolve_knowledge_doc_for_chash`` — the chash-based dedup checked
FIRST, before the collection-scoped ``by_source_uri`` lookup — is built
on ``docs_for_chashes``, a catalog-WIDE reverse lookup (chash is a pure
function of chunk text, collection-independent). It matched ANY
knowledge-content-type, store_put-origin document whose manifest
referenced the SAME chash as this put's first chunk, with zero
collection check and zero update to physical_collection/source_uri on
adoption. There is no structural difference between the split and
unsplit CODE paths here — both call the exact same
``catalog_store_hook_tracked`` — whether a given re-put's first chunk
happens to collide catalog-wide is purely circumstantial: Sam's split
note's new content shared its FIRST chunk's exact text with the old
document (a stable opening paragraph across the revision), while the
unsplit note's single whole-note chash covered the ENTIRE content and
so needed a full-content match to collide, which its own revision
happened not to produce. This suite reproduces the defect directly
with byte-identical content across collections (guaranteed to collide
regardless of split-ness), for both an unsplit and a split note, so
neither reproduction depends on which parts of a real revision changed.

Fix: ``resolve_knowledge_doc_for_chash`` now accepts an optional
*collection* filter, and the store_put reconcile path passes its own
target collection — a cross-collection chash match is no longer a
candidate at all, so the (collection, title)-keyed source_uri / ghost
lookups below it get the chance to mint a genuinely new document. The
one case where this dedup DOES still match (same collection) now also
stamps physical_collection/source_uri defensively, same shape as the
existing source_uri-reconcile branch.

Real engine substrate (``t2_service_env``): the bug is a real
``docs_for_chashes``/``physical_collection`` round trip, not something
an in-memory double reproduces.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]

_OLD_COLLECTION = "knowledge__bb6n2-cross-old__bge-base-en-v15-768__v1"
_NEW_COLLECTION = "knowledge__bb6n2-cross-new__bge-base-en-v15-768__v1"

# bge-base-en-v1.5's real 512-token window (resolved for this
# collection's model segment) tokenizes short repeated phrases
# efficiently -- empirically confirmed splitting into 3 pieces at 300
# reps (9338 chars, well past NOTE_SPLIT_CHARS=1689 too).
_SPLIT_CONTENT = "bb6n2 cross-collection split fixture. " + ("filler sentence content number " * 300)
_UNSPLIT_CONTENT = "bb6n2 cross-collection unsplit fixture, short content."


def _put_note(client, *, collection: str, title: str, content: str):
    """Real store_put-shaped write via the actual catalog reconcile +
    manifest-write functions under test."""
    from nexus.catalog.store_hook import (
        catalog_store_hook_tracked,
        note_manifest_metadata,
        note_pieces,
        store_put_manifest_direct,
    )

    pieces = note_pieces(content, collection)
    doc_id, manifest_metadatas = note_manifest_metadata(pieces)
    tumbler, created = catalog_store_hook_tracked(
        title=title, doc_id=doc_id, collection_name=collection,
    )
    chashes = [m["chunk_text_hash"] for m in manifest_metadatas]
    for piece, chash, meta in zip(pieces, chashes, manifest_metadatas):
        client.upsert_chunks_with_embeddings(
            collection,
            ids=[chash],
            documents=[piece],
            embeddings=[],
            metadatas=[{"title": title, "chunk_text_hash": chash, "doc_id": tumbler}],
        )
    store_put_manifest_direct(tumbler, manifest_metadatas, collection=collection)
    return tumbler, created, len(pieces)


def test_split_note_reput_into_a_new_collection_creates_a_new_document(t2_service_env):
    """The bug's own reproduction shape: an already-split (multi-chunk)
    note, re-put with IDENTICAL content into a DIFFERENT collection
    under the same title, must mint a new document in the new
    collection — never adopt the old collection's document."""
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.factory import make_catalog_reader

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    title = "bb6n2-cross-split-note"

    assert len(_SPLIT_CONTENT) > 1689, "control: fixture must actually split"

    old_tumbler, old_created, old_piece_count = _put_note(
        client, collection=_OLD_COLLECTION, title=title, content=_SPLIT_CONTENT,
    )
    assert old_created is True
    assert old_piece_count > 1, "control: the OLD note must genuinely be split (multi-chunk)"

    new_tumbler, new_created, new_piece_count = _put_note(
        client, collection=_NEW_COLLECTION, title=title, content=_SPLIT_CONTENT,
    )

    assert new_tumbler != old_tumbler, (
        "a same-titled re-put into a DIFFERENT collection must mint its own "
        f"document, not adopt the old collection's tumbler. old={old_tumbler!r} "
        f"new={new_tumbler!r}"
    )
    assert new_created is True, "the new-collection document must be a genuine new mint"

    reader = make_catalog_reader()
    new_entry = reader.resolve(new_tumbler)
    assert new_entry.physical_collection == _NEW_COLLECTION
    assert new_entry.source_uri.endswith(f"/{_NEW_COLLECTION}/{title}") or _NEW_COLLECTION in new_entry.source_uri, (
        f"new document's source_uri must name the NEW collection. Got: {new_entry.source_uri!r}"
    )

    old_entry = reader.resolve(old_tumbler)
    assert old_entry.physical_collection == _OLD_COLLECTION, (
        "the OLD document must be untouched by the new-collection put -- "
        f"its physical_collection must still be {_OLD_COLLECTION!r}, got "
        f"{old_entry.physical_collection!r}"
    )
    assert old_entry.chunk_count == old_piece_count, (
        "the OLD document's manifest must be untouched (same chunk_count "
        f"as its own original put), not rewritten to the new put's chunks"
    )


def test_unsplit_note_reput_into_a_new_collection_creates_a_new_document(t2_service_env):
    """The SAME defect reproduced on an unsplit (single-chunk) note: with
    byte-identical content, its single whole-note chash collides
    catalog-wide too, so pre-fix this ALSO wrongly adopted the old
    collection's document -- the split/unsplit distinction Sam's live
    report described was circumstantial (which chunk happened to match
    a real revision's unchanged text), not a structural difference in
    the code path. Proves the fix is not split-count-dependent."""
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.factory import make_catalog_reader

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    title = "bb6n2-cross-unsplit-note"

    old_tumbler, old_created, old_piece_count = _put_note(
        client, collection=_OLD_COLLECTION, title=title, content=_UNSPLIT_CONTENT,
    )
    assert old_created is True
    assert old_piece_count == 1, "control: the OLD note must genuinely be unsplit"

    new_tumbler, new_created, _new_piece_count = _put_note(
        client, collection=_NEW_COLLECTION, title=title, content=_UNSPLIT_CONTENT,
    )

    assert new_tumbler != old_tumbler
    assert new_created is True

    reader = make_catalog_reader()
    new_entry = reader.resolve(new_tumbler)
    assert new_entry.physical_collection == _NEW_COLLECTION

    old_entry = reader.resolve(old_tumbler)
    assert old_entry.physical_collection == _OLD_COLLECTION
    assert old_entry.chunk_count == old_piece_count


def test_same_collection_chash_dedup_still_reconciles_and_stamps_source_uri(t2_service_env):
    """Non-vacuity control: the collection-scoping fix must not break the
    legitimate SAME-collection chash dedup this lookup exists for --
    re-putting byte-identical content under the SAME collection and
    title must still reconcile onto the SAME document, not mint a
    duplicate."""
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.factory import make_catalog_reader

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    title = "bb6n2-same-collection-note"

    first_tumbler, first_created, _ = _put_note(
        client, collection=_OLD_COLLECTION, title=title, content=_UNSPLIT_CONTENT,
    )
    assert first_created is True

    second_tumbler, second_created, _ = _put_note(
        client, collection=_OLD_COLLECTION, title=title, content=_UNSPLIT_CONTENT,
    )

    assert second_tumbler == first_tumbler, (
        "an identical same-collection, same-title re-put must reconcile onto "
        "the SAME document, not mint a duplicate"
    )
    assert second_created is False

    reader = make_catalog_reader()
    entry = reader.resolve(first_tumbler)
    assert entry.physical_collection == _OLD_COLLECTION
