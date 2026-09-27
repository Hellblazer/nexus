# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.2 (RDR-192 Step 1, client half): fixture matrix pinning
TODAY's verdict of every client-observable liveness predicate against the
canonical R1-R8 row shapes (definitions: ``bd show nexus-wbfpw.1``, the
engine-half sibling S1a -- nexus-wbfpw.2 uses the identical rows).

Real engine substrate required (``t2_service_env``): every predicate under
test -- raw ``search()``/``get()`` over the wire, ``indexer_utils.
orphaned_chashes`` / ``live_note_chashes``, and ``nx t3 gc``'s dry-run
candidacy -- is a genuine PgVectorRepository/CatalogRepository round trip,
not something an in-memory double can stand in for (same rationale as
``tests/test_bb6n2_supersede_reap.py``).

Each row is built through a REAL public client write path, never a raw SQL
seed: ``store_put`` (R7), a catalog manifest write (``append_manifest_
chunks``/``write_manifest``, R2/R3/R5/R6), a catalog delete producing a
tombstone (R3), and a real cross-model collection rename (R4). R8 has no
public write path at all (the pre-nexus-b6enc "legacy note" shape a real
store_put can no longer produce, since b6enc always pairs the reverse
``meta.doc_id`` stamp with a manifest write) -- it is seeded through the
narrowest client call that reproduces the shape, named at its own site
below.

This file only PINS today's behavior. Where a verdict looks like a defect
(see the per-row rationale above ``EXPECTED``), a comment names the RDR-192
step that is expected to change it -- nothing here is fixed, and no
production code changes with it.
"""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from nexus.cli import main

pytestmark = [pytest.mark.integration]

_MODEL = "bge-base-en-v15-768"


def _coll(name: str) -> str:
    return f"knowledge__wbfpw2-{name}__{_MODEL}__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_chunk(client, collection: str, chash: str, text: str) -> None:
    """Bare T3 write, no catalog call -- same upsert shape store_hook's
    own note-write uses (mirrors ``test_bb6n2_supersede_reap.py::
    _put_note``'s per-piece upsert), used for every row that needs a
    physical chunk row with no (or a separately-built) manifest entry.

    ``indexed_at`` is stamped explicitly: the real ``store_put`` path
    (``HttpVectorClient.put``, ``http_vector_client.py`` ~2885) stamps it
    via ``make_chunk_metadata`` before every upsert, but the lower-level
    ``upsert_chunks_with_embeddings`` this helper (and ``_put_note``
    below) calls directly does NOT add it on its own -- omitting it here
    would leave every row's ``nx t3 gc`` candidacy answered by "no
    indexed_at" (predicate 8's own skip path) rather than by the
    liveness relationship this test exists to pin.
    """
    client.upsert_chunks_with_embeddings(
        collection, ids=[chash], documents=[text], embeddings=[],
        metadatas=[{
            "chunk_text_hash": chash, "title": collection,
            "indexed_at": datetime.now(UTC).isoformat(),
        }],
    )


def _get_visible(client, collection: str, chash: str) -> bool:
    """Raw ``get()`` over the wire -- predicate 1 as the client sees it."""
    from nexus.errors import CollectionNotFoundError

    try:
        result = client.get_collection(collection).get(ids=[chash], include=[])
    except CollectionNotFoundError:
        return False
    return chash in (result.get("ids") or [])


def _search_visible(client, collection: str, chash: str, text: str) -> bool:
    """Raw ``search()`` over the wire -- predicate 1 as the client sees
    it, querying with the chunk's own text (a local bge-768 embed puts
    an exact self-match at or near rank 0, so a generous ``n_results``
    is a reliable visibility probe, not a relevance test)."""
    result = client.search(text, [collection], n_results=50, structured=True)
    return chash in (result.get("ids") or [])


def _put_note(client, *, collection: str, title: str, content: str) -> tuple[str, list[str]]:
    """R7's write path: the store_put shape (a registered note whose
    manifest names its chunks), mirroring ``test_bb6n2_supersede_reap.py::
    _put_note``. Not the MCP tool's exact sequence: it writes chunks with
    ``upsert_chunks_with_embeddings`` and the bare
    ``store_put_manifest_direct`` rather than ``put_note_pieces`` and the
    recovery wrapper; none of the five predicates read the difference.
    Returns ``(tumbler, chashes)``."""
    from nexus.catalog.store_hook import (
        catalog_store_hook_tracked,
        note_manifest_metadata,
        note_pieces,
        store_put_manifest_direct,
    )

    pieces = note_pieces(content, collection)
    doc_id, manifest_metadatas = note_manifest_metadata(pieces)
    tumbler, _created = catalog_store_hook_tracked(
        title=title, doc_id=doc_id, collection_name=collection,
    )
    chashes = [m["chunk_text_hash"] for m in manifest_metadatas]
    for piece, chash, meta in zip(pieces, chashes, manifest_metadatas):
        client.upsert_chunks_with_embeddings(
            collection, ids=[chash], documents=[piece], embeddings=[],
            metadatas=[{
                "title": title, "chunk_text_hash": chash, "doc_id": tumbler,
                "indexed_at": datetime.now(UTC).isoformat(),
            }],
        )
    store_put_manifest_direct(tumbler, manifest_metadatas, collection=collection)
    return tumbler, chashes


def _orphaned(reader, collection: str, chash: str) -> bool:
    """``indexer_utils.orphaned_chashes`` as a generic superseded-vector
    sweep would call it -- predicate 5's first half. ``doc_id`` is a
    probe value that can never match a real registered document, so
    nothing is excluded from the "still referenced" test; True means
    the union guard would let a sweep delete this chash."""
    from nexus.indexer_utils import orphaned_chashes

    return chash in orphaned_chashes(
        reader, "__wbfpw2-probe-doc__", [chash], collection=collection,
    )


def _live_note(reader, collection: str, chash: str) -> bool:
    """``indexer_utils.live_note_chashes`` -- predicate 5's second half,
    the reverse ``meta.doc_id`` lookup RDR-145 added to protect a
    manifest-less note from the union guard above."""
    from nexus.indexer_utils import catalog_documents_for_collection, live_note_chashes

    docs = catalog_documents_for_collection(reader, collection)
    return chash in live_note_chashes(docs)


def _t3_gc_candidate(runner: CliRunner, collection: str, chash: str) -> bool:
    """``nx t3 gc --dry-run`` candidacy -- predicate 8. ``--orphan-window
    1s`` so age is never the deciding factor for chunks this same test
    wrote moments ago (RDR-192 Step 1's own instruction)."""
    result = runner.invoke(
        main, ["t3", "gc", "-c", collection, "--dry-run", "--orphan-window", "1s"],
    )
    assert result.exit_code == 0, result.output
    return chash in result.output


def _rollback_decision(client, catalog_doc_id: str, collection: str, chash: str) -> str:
    """``store_hook.rollback_uncataloged_chunk_write``'s own composed
    verdict for one chash (nexus-wbfpw.8, T2 nexus/review-rdr-192-phase1-
    code finding 4): the code review found this composition -- the union
    guard (``orphaned_chashes``) plus the notes guard (``live_note_
    chashes``) plus self-exclusion of the rolled-back call's OWN document
    -- built directly on top of two of this file's own pinned primitives,
    yet never itself pinned against the SAME R1-R8 rows.

    Called as though the rolled-back write belonged to *catalog_doc_id*,
    an UNRELATED document -- never the row's own owner -- so self-
    exclusion is exercised as a genuine no-op for every row here (the
    self-collision it exists to prevent is covered separately by
    ``test_b6enc_store_put_ghost_compensation.py``; this table's purpose
    is pinning the ordinary cross-row composition, not the self-collision
    edge case).

    DESTRUCTIVE: a 'delete' verdict really deletes the T3 row via the
    engine's own delete call. Must be the LAST predicate read for its
    row -- every other column for this row must already be captured
    before this is called.

    Returns 'delete' when *chash* lands in the outcome's ``attempted``
    tuple, 'protect' when it lands in ``protected`` instead -- one or the
    other always holds; nothing else is a valid outcome for a chash that
    was actually written (see ``ChunkRollbackOutcome``'s own contract).
    """
    from nexus.catalog.store_hook import rollback_uncataloged_chunk_write

    outcome = rollback_uncataloged_chunk_write(
        client, [chash], collection=collection, catalog_doc_id=catalog_doc_id,
    )
    if chash in outcome.attempted:
        assert chash not in outcome.protected, outcome
        return "delete"
    assert chash in outcome.protected, outcome
    return "protect"


def test_client_liveness_matrix_pins_todays_verdict(t2_service_env):
    """Build R1-R8 through real client write paths, then pin today's
    verdict of every client-observable liveness predicate as a table."""
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from tests._catalog_fixture_ops import ActiveCatalog, active_reader

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    cat = ActiveCatalog()
    reader = active_reader()
    runner = CliRunner()

    owner = cat.register_owner("wbfpw2-owner", "curator")

    # An UNRELATED document, never any row's own owner -- passed as
    # rollback_uncataloged_chunk_write's catalog_doc_id for every row's
    # probe below so self-exclusion is exercised as a genuine no-op (see
    # _rollback_decision's own docstring).
    unrelated_doc = str(cat.register(
        owner, "wbfpw2-rollback-unrelated-doc", content_type="knowledge",
        physical_collection=_coll("rollback-unrelated"),
    ))

    rows: dict[str, dict] = {}

    # ---- R1: chunk in A, no manifest row anywhere. ------------------------
    coll = _coll("r1")
    text = "wbfpw2 R1 -- a bare T3 chunk, never manifested by any document."
    chash = _chash(text)
    _write_chunk(client, coll, chash, text)
    rows["R1"] = {"collection": coll, "chash": chash, "text": text}

    # ---- R2: chunk in A, own-collection manifest row, document LIVE. ------
    coll = _coll("r2")
    text = "wbfpw2 R2 -- a chunk with a live own-collection manifest owner."
    chash = _chash(text)
    _write_chunk(client, coll, chash, text)
    doc = cat.register(owner, "wbfpw2-r2-doc", content_type="knowledge", physical_collection=coll)
    cat.append_manifest_chunks(str(doc), [{"chash": chash, "position": 0}], collection=coll)
    cat.resync_chunk_count_cache(str(doc))
    rows["R2"] = {"collection": coll, "chash": chash, "text": text}

    # ---- R3: chunk in A, only own-collection manifest row(s) point at a
    # TOMBSTONED document -- catalog delete producing the tombstone. -------
    coll = _coll("r3")
    text = "wbfpw2 R3 -- a chunk whose only manifest owner is tombstoned."
    chash = _chash(text)
    _write_chunk(client, coll, chash, text)
    doc = cat.register(owner, "wbfpw2-r3-doc", content_type="knowledge", physical_collection=coll)
    cat.append_manifest_chunks(str(doc), [{"chash": chash, "position": 0}], collection=coll)
    cat.resync_chunk_count_cache(str(doc))
    assert cat.delete_document(str(doc)), "control: the tombstone write must report a real delete"
    rows["R3"] = {"collection": coll, "chash": chash, "text": text}

    # ---- R4: chunk in A, only manifest row is in collection B -- built via
    # a REAL cross-model collection rename (client write path #4). The
    # rename's COPY branch re-homes catalog_documents + the manifest row
    # onto the target collection but never touches nexus.chunks itself
    # (confirmed by service/src/test/java/dev/nexus/service/
    # CatalogRenameCollectionTest.java::renameCollection_crossModelCopy
    # Branch_targetExists_repointsDocsAndManifests's own fixture comment),
    # so the physical chunk this test writes into the source collection is
    # stranded there, unmanifested -- the production "rename COPY branch
    # leaves source-collection chunk rows" shape (T2 nexus/rdr-192-
    # reverification-2026-09-26, Producers section).
    src = _coll("r4-src")
    tgt = _coll("r4-tgt")
    text = "wbfpw2 R4 -- a chunk stranded in its source collection by a cross-model rename."
    chash = _chash(text)
    _write_chunk(client, src, chash, text)
    doc = cat.register(owner, "wbfpw2-r4-doc", content_type="knowledge", physical_collection=src)
    cat.append_manifest_chunks(str(doc), [{"chash": chash, "position": 0}], collection=src)
    cat.resync_chunk_count_cache(str(doc))
    # The cross-model COPY branch requires the TARGET to already carry a
    # chunk under the same chash (fk_catalog_chunks_chunk on the re-homed
    # manifest row) -- a real, independent T3 write into tgt, not a copy
    # of the source row.
    _write_chunk(client, tgt, chash, text)
    HttpCatalogClient().rename_collection(src, tgt, cross_model=True)
    rows["R4"] = {"collection": src, "chash": chash, "text": text}

    # ---- R5: chunk in A shared by two live documents; ONE is re-manifested
    # (a manifest write) to no longer reference it -- the other still does.
    coll = _coll("r5")
    text = "wbfpw2 R5 -- a chunk shared by two documents, one re-manifested away from it."
    chash = _chash(text)
    _write_chunk(client, coll, chash, text)
    doc_keep = cat.register(owner, "wbfpw2-r5-doc-keep", content_type="knowledge", physical_collection=coll)
    doc_drop = cat.register(owner, "wbfpw2-r5-doc-drop", content_type="knowledge", physical_collection=coll)
    cat.append_manifest_chunks(str(doc_keep), [{"chash": chash, "position": 0}], collection=coll)
    cat.append_manifest_chunks(str(doc_drop), [{"chash": chash, "position": 0}], collection=coll)
    cat.resync_chunk_count_cache(str(doc_keep))
    cat.resync_chunk_count_cache(str(doc_drop))
    cat.write_manifest(str(doc_drop), [], collection=coll)  # re-manifest doc_drop WITHOUT the chash
    rows["R5"] = {"collection": coll, "chash": chash, "text": text}

    # ---- R6: one chash present in BOTH A and B (two independent physical
    # copies of identical content), with a live manifest row in B only. ----
    coll_a = _coll("r6-a")
    coll_b = _coll("r6-b")
    text = "wbfpw2 R6 -- identical content independently embedded into two collections."
    chash = _chash(text)
    _write_chunk(client, coll_a, chash, text)  # the A copy: never manifested
    _write_chunk(client, coll_b, chash, text)  # the B copy
    doc = cat.register(owner, "wbfpw2-r6-doc", content_type="knowledge", physical_collection=coll_b)
    cat.append_manifest_chunks(str(doc), [{"chash": chash, "position": 0}], collection=coll_b)
    cat.resync_chunk_count_cache(str(doc))
    rows["R6"] = {"collection": coll_a, "chash": chash, "text": text}  # probe the A copy

    # ---- R7: a current note -- the real store_put shape (write path #1). -
    coll = _coll("r7")
    text = "wbfpw2 R7 -- a real store_put note, with its manifest row."
    _tumbler, chashes = _put_note(client, collection=coll, title="wbfpw2-r7-note", content=text)
    rows["R7"] = {"collection": coll, "chash": chashes[0], "text": text}

    # ---- R8: a legacy current note -- the pre-nexus-b6enc shape. No public
    # write path produces this today (b6enc always pairs the reverse
    # doc_id stamp with a real manifest write), so it is seeded through
    # the NARROWEST client call that reproduces the shape: register() with
    # meta={"doc_id": chash} and NO manifest write at all -- exactly
    # is_note_shaped's identity predicate (indexer_utils.py: no file_path
    # + a truthy meta.doc_id), with the manifest half simply never called.
    coll = _coll("r8")
    text = "wbfpw2 R8 -- a legacy note protected only by the reverse doc_id lookup."
    chash = _chash(text)
    _write_chunk(client, coll, chash, text)
    cat.register(
        owner, "wbfpw2-r8-legacy-note", content_type="knowledge",
        physical_collection=coll, meta={"doc_id": chash},
    )
    rows["R8"] = {"collection": coll, "chash": chash, "text": text}

    # Give indexed_at a moment's headroom so a 1s orphan-window is never
    # ambiguous with "just written" (RDR-192 Step 1's own instruction:
    # a window small enough that age is not the deciding factor).
    time.sleep(1.5)

    observed: dict[str, dict[str, bool]] = {}
    for row, fx in rows.items():
        observed[row] = {
            "search": _search_visible(client, fx["collection"], fx["chash"], fx["text"]),
            "get": _get_visible(client, fx["collection"], fx["chash"]),
            "orphaned_chashes": _orphaned(reader, fx["collection"], fx["chash"]),
            "live_note_chashes": _live_note(reader, fx["collection"], fx["chash"]),
            "t3_gc_candidate": _t3_gc_candidate(runner, fx["collection"], fx["chash"]),
        }
        # DESTRUCTIVE -- must come last: a "delete" verdict really removes
        # the chash from T3, so nothing above may re-read this row's chunk
        # afterward. See _rollback_decision's own docstring.
        observed[row]["rollback"] = _rollback_decision(
            client, unrelated_doc, fx["collection"], fx["chash"],
        )

    _PREDICATES = (
        "search", "get", "orphaned_chashes", "live_note_chashes",
        "t3_gc_candidate", "rollback",
    )
    _ROWS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")

    # Non-vacuity (mandatory per the bead's own acceptance criteria): the
    # table actually has the shape later steps will diff against -- every
    # row, every predicate column, nothing silently narrowed away.
    assert set(observed) == set(_ROWS), f"missing/extra rows: {set(observed) ^ set(_ROWS)}"
    for row, cols in observed.items():
        assert set(cols) == set(_PREDICATES), f"{row} is missing a predicate column: {set(_PREDICATES) - set(cols)}"

    # TODAY's verdict, pinned by a real run against the dev jar
    # (2026-09-26). Rationale per row, matching the RDR-192 predicate
    # families (T2 nexus/rdr-192-reverification-2026-09-26):
    #
    # R1 manifest-less -> visible under predicate 1 (own-collection join
    #    finds no manifest row at all, so nothing hides it); orphaned by
    #    both client guards and t3 gc (nothing protects it anywhere).
    # R2 a live own-collection owner -> visible, protected everywhere.
    # R3 own-collection manifest row(s) all point at a TOMBSTONED owner ->
    #    HIDDEN under predicate 1 (search/get correctly treat it as dead),
    #    and orphaned_chashes AGREES here (True: deletable) -- a tombstoned
    #    owner's manifest reference does not count as "still referenced"
    #    for this guard. t3_gc_candidate is still False, though, because
    #    ``nx t3 gc``'s own alive-set (``chashes_for_collection_with_
    #    tombstone_protected``) is DELIBERATELY wider than orphaned_
    #    chashes: nexus-dkymw's ruling protects a tombstoned-but-not-yet-
    #    purged document's chashes too, so ``nx catalog purge-trash``'s
    #    recovery window is never raced by ``nx t3 gc``'s own clock. So R3
    #    disagrees across THREE predicates at once: dead (search/get),
    #    deletable (orphaned_chashes), protected (t3 gc) -- a real,
    #    already-existing three-way split this bead's own table exists to
    #    make visible, not a defect introduced here.
    # R4 manifest moved to B by a real rename; the physical row stranded
    #    in the source collection is manifest-less there -> same as R1.
    # R5 the OTHER live document's manifest row still references it ->
    #    visible, protected everywhere (content-addressed sharing safety).
    # R6 the A copy is manifest-less in A (the B-only manifest cannot
    #    protect a different physical collection's row under any
    #    collection-scoped predicate) -> same as R1/R4.
    # R7 a real store_put note with its own manifest row -> visible,
    #    protected everywhere -- and additionally recognized as a NOTE by
    #    live_note_chashes (is_note_shaped: no file_path + meta.doc_id).
    # R8 the legacy shape: no manifest row anywhere, but a live document's
    #    meta.doc_id names this chash -> live_note_chashes protects it
    #    (True) even though it is otherwise indistinguishable from R1/R4/R6
    #    to every OTHER predicate; search/get and orphaned_chashes read it
    #    exactly like R1 (manifest-less-is-vacuously-live / no protective
    #    reference respectively) since neither of those consult
    #    live_note_chashes on its own -- t3 gc DOES consult it (t3.py
    #    unions referenced with live_note_chashes before deciding
    #    candidacy), so R8 is the one row where live_note_chashes=True
    #    changes t3_gc_candidate from what R1/R4/R6's shape would
    #    otherwise produce.
    #
    # rollback (nexus-wbfpw.8, finding 4): store_hook.rollback_uncataloged_
    # chunk_write's own composed verdict -- the union guard alone decides
    # UNLESS the notes guard also protects it, in which case notes wins.
    # Written as literal per-row expectations, not derived from the
    # orphaned_chashes/live_note_chashes columns above, so a future change
    # to either predicate that this table's own asserts would catch is
    # ALSO independently caught here if it silently changed rollback's
    # actual delete/protect decision.
    #   R1 delete  -- union guard alone: nothing references it anywhere.
    #   R2 protect -- union guard alone: still referenced (own manifest).
    #   R3 delete  -- union guard alone: tombstoned owner does not protect.
    #   R4 delete  -- union guard alone: stranded, nothing references it.
    #   R5 protect -- union guard alone: the OTHER document still does.
    #   R6 delete  -- union guard alone: the B-only manifest can't reach A.
    #   R7 protect -- union guard alone: its own manifest still references it.
    #   R8 protect -- union guard says deletable, but the notes guard
    #      rescues it (a live document's own meta.doc_id still names it) --
    #      the ONE row where the two guards disagree and the notes guard's
    #      own composition, not the union guard alone, decides the outcome.
    EXPECTED = {
        "R1": {"search": True, "get": True, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": True, "rollback": "delete"},
        "R2": {"search": True, "get": True, "orphaned_chashes": False, "live_note_chashes": False, "t3_gc_candidate": False, "rollback": "protect"},
        "R3": {"search": False, "get": False, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": False, "rollback": "delete"},
        "R4": {"search": True, "get": True, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": True, "rollback": "delete"},
        "R5": {"search": True, "get": True, "orphaned_chashes": False, "live_note_chashes": False, "t3_gc_candidate": False, "rollback": "protect"},
        "R6": {"search": True, "get": True, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": True, "rollback": "delete"},
        "R7": {"search": True, "get": True, "orphaned_chashes": False, "live_note_chashes": True, "t3_gc_candidate": False, "rollback": "protect"},
        "R8": {"search": True, "get": True, "orphaned_chashes": True, "live_note_chashes": True, "t3_gc_candidate": False, "rollback": "protect"},
    }
    assert observed == EXPECTED, f"today's verdict changed:\n  observed={observed}\n  expected={EXPECTED}"

    # RDR-192's own MVV (a) / this bead's acceptance criterion: R7 (a
    # current, correctly-manifested note) must be a non-candidate for
    # every DESTRUCTIVE predicate under test.
    assert observed["R7"]["orphaned_chashes"] is False, "R7 must not be flagged deletable by the union-guard sweep"
    assert observed["R7"]["t3_gc_candidate"] is False, "R7 must not be a nx t3 gc dry-run candidate"
    assert observed["R7"]["search"] is True and observed["R7"]["get"] is True, "R7 must remain visible"
