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

from click.testing import CliRunner

from nexus.cli import main
from tests._chunk_seed import seed_chunks_direct
from tests._reapable_age import age_chunks_past_grace

# Not integration-marked (nexus-wbfpw.38): the substrate provisions itself,
# and CI's default selection must run this RDR-192 pin.

_MODEL = "bge-base-en-v15-768"


def _coll(name: str) -> str:
    return f"knowledge__wbfpw2-{name}__{_MODEL}__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_chunk(client, collection: str, chash: str, text: str) -> None:
    """Bare T3 write, no catalog call -- substrate SQL with the engine's real
    embedding (the engine refuses an ownerless upsert-chunks write from RDR-223
    Phase 3 on; mirrors ``test_bb6n2_supersede_reap.py::_put_note``'s
    per-piece seed), used for every row that needs a physical chunk row with
    no (or a separately-built) manifest entry.

    No ``indexed_at`` is stamped: since RDR-192 Step 8 (nexus-wbfpw.18) ``nx t3 gc``
    takes its candidates from the engine's reapable predicate, which never reads it.
    """
    seed_chunks_direct(
        collection, ids=[chash], documents=[text], embed=True,
        metadatas=[{"chunk_text_hash": chash, "title": collection}],
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
    """R7's write path: a registered note whose manifest names its chunks,
    written the way MCP ``store_put`` writes it now (RDR-223 P2.2,
    nexus-z0o2p.12): the catalog reconcile, then one ``write_manifest_many``
    request carrying the pieces and the manifest. Returns ``(tumbler, chashes)``."""
    from nexus.catalog.note_write import write_note
    from nexus.catalog.store_hook import (
        catalog_store_hook_tracked,
        note_content_hash,
        note_manifest_metadata,
        note_pieces,
    )

    pieces = note_pieces(content, collection)
    doc_id, manifest_metadatas = note_manifest_metadata(pieces)
    tumbler, _created = catalog_store_hook_tracked(
        title=title, doc_id=doc_id, collection_name=collection,
    )
    write_note(
        catalog_doc_id=tumbler, collection=collection, pieces=pieces, title=title,
        content_hash=note_content_hash(content, manifest_metadatas),
    )
    return tumbler, [m["chunk_text_hash"] for m in manifest_metadatas]


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
    """``nx t3 gc --dry-run`` candidacy -- predicate 8. Since RDR-192 Step 8 (nexus-wbfpw.18) the
    verb's candidates ARE the engine's default-grace reapable listing; the row chunks are aged past
    that grace first (``age_chunks_past_grace``), and no window is passed (there is none to pass)."""
    result = runner.invoke(main, ["t3", "gc", "-c", collection, "--dry-run"])
    # A dry run that names a refusal a real run would hit exits 1 (nexus-wbfpw.18 round 3) but still
    # prints the listing: the candidacy read here is the listing, so accept that exit only when the
    # output says it is a refusal, never a crash.
    assert result.exit_code == 0 or "would REFUSE" in result.output, result.output
    return chash in result.output


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

    # nx t3 gc takes its candidates from the engine's reapable predicate, which honours a 30 day
    # grace on last_written_at and has no tunable window. Every row's chunks stand for chunks that
    # were orphaned long ago, so age them past it; ownership, not age, then decides each verdict.
    for fx in rows.values():
        age_chunks_past_grace(fx["collection"])

    # The verb's candidacy IS the default-grace reapable listing (RDR-192 Step 8, nexus-wbfpw.18).
    listings = {
        row: {r["chash"] for r in client.reapable_chunks(fx["collection"])}
        for row, fx in rows.items()
    }

    observed: dict[str, dict[str, bool]] = {}
    for row, fx in rows.items():
        observed[row] = {
            "search": _search_visible(client, fx["collection"], fx["chash"], fx["text"]),
            "get": _get_visible(client, fx["collection"], fx["chash"]),
            "orphaned_chashes": _orphaned(reader, fx["collection"], fx["chash"]),
            "live_note_chashes": _live_note(reader, fx["collection"], fx["chash"]),
            "t3_gc_candidate": _t3_gc_candidate(runner, fx["collection"], fx["chash"]),
        }

    _PREDICATES = (
        "search", "get", "orphaned_chashes", "live_note_chashes",
        "t3_gc_candidate",
    )
    _ROWS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")

    # Non-vacuity (mandatory per the bead's own acceptance criteria): the
    # table actually has the shape later steps will diff against -- every
    # row, every predicate column, nothing silently narrowed away.
    assert set(observed) == set(_ROWS), f"missing/extra rows: {set(observed) ^ set(_ROWS)}"
    for row, cols in observed.items():
        assert set(cols) == set(_PREDICATES), f"{row} is missing a predicate column: {set(_PREDICATES) - set(cols)}"

    # TODAY's verdict, pinned by a real run against the dev jar. Rationale
    # per row, matching the RDR-192 predicate families (T2 nexus/rdr-192-
    # reverification-2026-09-26).
    #
    # RDR-192 Step 5 (nexus-wbfpw.10, migrated onto live(c)): search/get now
    # AGREE with orphaned_chashes on every manifest-less-in-this-collection
    # row (R1/R4/R6/R8) -- live(c) requires a live own-collection manifest
    # owner, so a chunk with none is HIDDEN from content reads too, not just
    # flagged deletable by the union guard. Only R3 (a tombstoned owner)
    # still disagrees across predicates -- see its own rationale below.
    #
    # R1 manifest-less -> HIDDEN under predicate 1/live(c) (no live
    #    own-collection manifest owner at all); orphaned by both client
    #    guards and t3 gc (nothing protects it anywhere).
    # R2 a live own-collection owner -> visible, protected everywhere.
    # R3 own-collection manifest row(s) all point at a TOMBSTONED owner ->
    #    HIDDEN under predicate 1 (search/get correctly treat it as dead),
    #    and orphaned_chashes AGREES here (True: deletable) -- a tombstoned
    #    owner's manifest reference does not count as "still referenced"
    #    for this guard. t3_gc_candidate is still False, though, because
    #    the engine's reapable predicate (which ``nx t3 gc`` now takes its
    #    candidates from, nexus-wbfpw.18) counts a manifest row in ANY owner
    #    state: a tombstoned owner keeps its row, so the chunk is
    #    ``nx catalog purge-trash``'s (nexus-dkymw's ruling), not gc's. So R3
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
    #    to every OTHER predicate; search/get and orphaned_chashes now read
    #    it exactly like R1 (HIDDEN by live(c) / no protective reference
    #    respectively) since neither of those consult live_note_chashes on
    #    its own. ``nx t3 gc`` no longer consults it either (nexus-wbfpw.18):
    #    its candidates are the engine's reapable listing, and an aged R8
    #    chunk satisfies every condition of that predicate (RDR-192 R8), so
    #    the dry-run NAMES it. What protects it now is the verb's census
    #    gate: the collection reads legacy-unmanifested = 1, so a real run
    #    refuses (``test_wbfpw18_t3_gc_substrate.py``). The column flipped
    #    False -> True with that move.
    #
    # (The per-row ``rollback`` verdict this table once pinned went with
    # the client-side chunk rollback at nexus-z0o2p.32: no writer
    # leaves a chunk without its owner any more, so nothing rolls a chunk back.)
    EXPECTED = {
        "R1": {"search": False, "get": False, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": True},
        "R2": {"search": True, "get": True, "orphaned_chashes": False, "live_note_chashes": False, "t3_gc_candidate": False},
        "R3": {"search": False, "get": False, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": False},
        "R4": {"search": False, "get": False, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": True},
        "R5": {"search": True, "get": True, "orphaned_chashes": False, "live_note_chashes": False, "t3_gc_candidate": False},
        "R6": {"search": False, "get": False, "orphaned_chashes": True, "live_note_chashes": False, "t3_gc_candidate": True},
        "R7": {"search": True, "get": True, "orphaned_chashes": False, "live_note_chashes": True, "t3_gc_candidate": False},
        "R8": {"search": False, "get": False, "orphaned_chashes": True, "live_note_chashes": True, "t3_gc_candidate": True},
    }
    assert observed == EXPECTED, f"today's verdict changed:\n  observed={observed}\n  expected={EXPECTED}"

    # nexus-wbfpw.18: the verb's dry-run candidacy equals the default-grace reapable listing, row
    # by row, and the listings are not all empty (a vacuous equality proves nothing).
    for row, fx in rows.items():
        assert observed[row]["t3_gc_candidate"] == (fx["chash"] in listings[row]), (
            f"{row}: dry-run candidacy diverged from the reapable listing {listings[row]}"
        )
    assert any(listings.values()), "non-vacuity: at least one row's reapable listing is non-empty"

    # RDR-192's own MVV (a) / this bead's acceptance criterion: R7 (a
    # current, correctly-manifested note) must be a non-candidate for
    # every DESTRUCTIVE predicate under test.
    assert observed["R7"]["orphaned_chashes"] is False, "R7 must not be flagged deletable by the union-guard sweep"
    assert observed["R7"]["t3_gc_candidate"] is False, "R7 must not be a nx t3 gc dry-run candidate"
    assert observed["R7"]["search"] is True and observed["R7"]["get"] is True, "R7 must remain visible"
