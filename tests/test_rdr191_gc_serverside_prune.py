# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-191 Phase 1: server-side GC prune (``nexus.gc_quarantine_orphans`` /
``gc_restore_rereferenced`` / ``gc_expire_quarantine``, catalog-023) —
real-engine equivalence + zero-wire-crossing proof.

Per ``tests/AGENTS.md``: integration over mocks, real substrate via
``t2_service_env`` (wraps ``ensure_engine``/``mint_test_tenant``).

The load-bearing claim under test: the SQL anti-join — keyed on
``chunks_<dim>.chash`` (the PK column) joined to
``catalog_document_chunks.chash`` — classifies exactly the chunks the OLD
client-side algorithm (``_fetch_all_chunk_metadata`` + a Python diff against
``catalog.chashes_for_collection``) would have classified as orphan/live,
AND does so without a single chunk row, document, or embedding crossing the
``/v1/vectors`` wire. The zero-wire-crossing tests are a same-session A/B:
:func:`test_fallback_path_crosses_full_chunk_payloads` proves the OLD
(still-live, Phase-1-preserved) path DOES send full metadata/documents/
embeddings, and :func:`test_serverside_path_crosses_zero_chunk_payloads`
proves the code path that now runs BY DEFAULT does not — together they are
the non-vacuity proof this file exists to give (there is no bisectable
"before this change" commit to diff against from inside the working tree,
per this session's constraints; the fallback path IS byte-for-byte the pre-
RDR-191 algorithm, unconditionally reachable and independently exercised
here, so the contrast is exact).
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.integration]


def _seed(cat, db, coll_name: str, owner: str, n_live: int, n_orphan: int):
    """Real server-embedded chunks (n_live + n_orphan) in *coll_name*, a
    manifest row for exactly the first *n_live* chashes. Returns
    (chashes, live_chashes, orphan_chashes)."""
    ids: list[str] = []
    chashes: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for i in range(n_live + n_orphan):
        text = f"def gcq_probe_{i}(): return {i}\n"
        chash = hashlib.sha256(f"{coll_name}:{i}".encode()).hexdigest()
        chashes.append(chash)
        ids.append(chash)
        docs.append(text)
        metas.append({"chunk_text_hash": chash, "title": f"gcq_probe_{i}.py:1-1"})

    db.upsert_chunks_with_embeddings(
        coll_name, ids=ids, documents=docs, embeddings=[], metadatas=metas,
    )

    live_chashes = chashes[:n_live]
    for i in range(n_live):
        tumbler = str(cat.register(
            owner, f"gcq_probe_{i}.py",
            content_type="code",
            file_path=f"/tmp/{coll_name}/gcq_probe_{i}.py",
            physical_collection=coll_name,
            chunk_count=1,
        ))
        cat.write_manifest(
            tumbler, [{"chash": chashes[i], "position": 0}], collection=coll_name,
        )

    return chashes, live_chashes, chashes[n_live:]


# ── EQUIVALENCE ──────────────────────────────────────────────────────────


def test_serverside_prune_quarantines_exactly_the_orphans(t2_service_env) -> None:
    """Real engine, real server-side route: live chunks survive in the
    origin collection, orphans move to the quarantine sibling — the exact
    classification the old client-side diff produced, now computed by the
    SQL anti-join."""
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.chunk_quarantine import quarantine_collection_name
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    coll_name = "code__gcq-equiv__bge-base-en-v15-768__v1"
    owner = cat.register_owner("gcq-equiv", "curator")
    n_live, n_orphan = 6, 4
    _chashes, live_chashes, orphan_chashes = _seed(cat, db, coll_name, owner, n_live, n_orphan)

    _prune_deleted_files(coll_name, "docs__gcq-equiv-unused", db, catalog=cat)

    remaining = set(db.get_collection(coll_name).get_all_metadata()["ids"])
    assert remaining == set(live_chashes), (
        f"expected exactly the live chashes to survive in origin; got {remaining}"
    )
    qname = quarantine_collection_name(coll_name)
    quarantined = set(db.get_collection(qname).get_all_metadata()["ids"])
    assert quarantined == set(orphan_chashes), (
        f"expected exactly the orphan chashes in quarantine; got {quarantined}"
    )


def test_serverside_prune_noOrphans_movesNothing(t2_service_env) -> None:
    """No orphans present: server path is a clean zero-op — nothing moves,
    nothing crosses beyond the anti-join's own read."""
    import nexus.db.http_vector_client as hvc
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    coll_name = "code__gcq-noorphan__bge-base-en-v15-768__v1"
    owner = cat.register_owner("gcq-noorphan", "curator")
    chashes, live_chashes, _orphans = _seed(cat, db, coll_name, owner, n_live=3, n_orphan=0)

    _prune_deleted_files(coll_name, "docs__gcq-noorphan-unused", db, catalog=cat)

    remaining = set(db.get_collection(coll_name).get_all_metadata()["ids"])
    assert remaining == set(live_chashes)


# ── PER-COLLECTION ISOLATION ─────────────────────────────────────────────


def test_serverside_failure_skips_only_that_collection(t2_service_env) -> None:
    """A non-404 failure in the server-side prune must skip THAT collection
    and let the sweep continue (the nexus-ou4tb contract the legacy arms in
    this same function already honour).

    Non-vacuity: the first collection's prune RAISES. If the call site is
    unguarded, the exception escapes ``_prune_deleted_files`` and this test
    ERRORS rather than fails — and the second collection's orphans are never
    quarantined, which is the post-state actually asserted below.
    """
    import nexus.db.http_vector_client as hvc
    import nexus.indexer as indexer_mod
    from nexus.catalog.chunk_quarantine import quarantine_collection_name
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    boom_coll = "code__gcq-isolate-boom__bge-base-en-v15-768__v1"
    ok_coll = "docs__gcq-isolate-ok__bge-base-en-v15-768__v1"
    boom_owner = cat.register_owner("gcq-isolate-boom", "curator")
    ok_owner = cat.register_owner("gcq-isolate-ok", "curator")

    _c1, boom_live, boom_orphans = _seed(cat, db, boom_coll, boom_owner, n_live=2, n_orphan=2)
    _c2, ok_live, ok_orphans = _seed(cat, db, ok_coll, ok_owner, n_live=2, n_orphan=3)

    def _fail_first(db_, collection_name, qname, stamp):
        if collection_name == boom_coll:
            raise ConnectionError("simulated transient engine blip")
        return False  # benign fallback signal: use the client-side path

    with patch.object(indexer_mod, "_prune_collection_serverside", _fail_first):
        _prune_deleted_files(boom_coll, ok_coll, db, catalog=cat)

    # The raising collection was skipped, NOT swept: everything it had is
    # still in the origin collection, orphans included.
    boom_remaining = set(db.get_collection(boom_coll).get_all_metadata()["ids"])
    assert boom_remaining == set(boom_live) | set(boom_orphans), (
        "a failed server-side prune must leave its collection untouched; "
        f"got {boom_remaining}"
    )

    # The sweep continued: the SECOND collection was still pruned.
    ok_remaining = set(db.get_collection(ok_coll).get_all_metadata()["ids"])
    assert ok_remaining == set(ok_live), (
        "the sweep must continue past a failed collection; "
        f"second collection left as {ok_remaining}"
    )
    ok_quarantined = set(
        db.get_collection(quarantine_collection_name(ok_coll)).get_all_metadata()["ids"]
    )
    assert ok_quarantined == set(ok_orphans), (
        f"second collection's orphans should be quarantined; got {ok_quarantined}"
    )


# ── ZERO-WIRE-CROSSING (same-session A/B; see module docstring) ─────────


def test_serverside_path_crosses_zero_chunk_payloads(t2_service_env) -> None:
    """The DEFAULT (server-first) path never sends a chunk's text, metadata,
    or embedding over the wire — only the three ``/v1/vectors/gc/*`` calls
    fire, and none of their request bodies carry document/metadata/
    embedding payloads."""
    import nexus.db.http_vector_client as hvc
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    coll_name = "code__gcq-wire__bge-base-en-v15-768__v1"
    owner = cat.register_owner("gcq-wire", "curator")
    _seed(cat, db, coll_name, owner, n_live=3, n_orphan=2)

    posted: list[tuple[str, dict]] = []
    real_post = hvc._post

    def _spy_post(path, body, **kwargs):
        posted.append((path, body))
        return real_post(path, body, **kwargs)

    with patch("nexus.db.http_vector_client._post", side_effect=_spy_post):
        _prune_deleted_files(coll_name, "docs__gcq-wire-unused", db, catalog=cat)

    posted_paths = [p for p, _ in posted]
    gc_calls = [p for p in posted_paths if p.startswith("/v1/vectors/gc/")]
    assert gc_calls, f"expected the RDR-191 server route to fire; posted paths: {posted_paths}"

    forbidden = {"/v1/vectors/get-all-metadata", "/v1/vectors/get",
                 "/v1/vectors/get-embeddings", "/v1/vectors/store-get"}
    hit_forbidden = [p for p in posted_paths if p in forbidden]
    assert not hit_forbidden, (
        f"server-first path must never touch the old chunk-payload routes; hit: {hit_forbidden}"
    )

    # No outgoing body anywhere in this run carries a chunk payload key.
    payload_keys = {"metadatas", "documents", "embeddings"}
    for path, body in posted:
        leaked = payload_keys & set(body.keys())
        assert not leaked, f"POST {path} body carried chunk payload keys {leaked}: {body}"


def test_fallback_path_crosses_full_chunk_payloads(t2_service_env) -> None:
    """Contrast case: with the server route made unreachable (simulating a
    pre-route engine — REQUIRED_ENGINE_VERSION is (0, 1, 69), this route
    ships in the NEXT tag), the OLD client-side path DOES send full
    metadata/documents/embeddings — proving the assertions in
    :func:`test_serverside_path_crosses_zero_chunk_payloads` are
    non-vacuous, not just "no route happened to fire"."""
    import nexus.db.http_vector_client as hvc
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    coll_name = "code__gcq-fallback__bge-base-en-v15-768__v1"
    owner = cat.register_owner("gcq-fallback", "curator")
    _seed(cat, db, coll_name, owner, n_live=3, n_orphan=2)

    posted: list[tuple[str, dict]] = []
    real_post = hvc._post

    def _spy_post(path, body, **kwargs):
        posted.append((path, body))
        return real_post(path, body, **kwargs)

    # Simulate a pre-route engine: the client has no gc_quarantine_orphans
    # capability from the caller's point of view.
    with patch.object(hvc.HttpVectorClient, "gc_quarantine_orphans", None), \
         patch.object(hvc.HttpVectorClient, "gc_restore_rereferenced", None), \
         patch.object(hvc.HttpVectorClient, "gc_expire_quarantine", None), \
         patch("nexus.db.http_vector_client._post", side_effect=_spy_post):
        _prune_deleted_files(coll_name, "docs__gcq-fallback-unused", db, catalog=cat)

    posted_paths = [p for p, _ in posted]
    assert "/v1/vectors/get-all-metadata" in posted_paths, (
        f"expected the OLD fast-path full-collection metadata read to fire "
        f"when the server route is unavailable; posted: {posted_paths}"
    )
    # The old quarantine_orphans() copy path fetches metadatas+documents
    # (+ embeddings via a separate get-embeddings call) for the batch it
    # is about to move — that IS the wire cost RDR-191 eliminates.
    payload_bearing = [
        (p, body) for p, body in posted
        if {"metadatas", "documents"} & set(body.keys())
    ]
    assert payload_bearing, (
        f"expected the fallback path to carry chunk payloads over the wire "
        f"at least once (the cost this RDR eliminates by default); posted: {posted_paths}"
    )
