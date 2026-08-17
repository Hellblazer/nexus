# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-191 Phase 1: server-side GC prune (``nexus.gc_quarantine_orphans`` /
``gc_restore_rereferenced`` / ``gc_expire_quarantine``, catalog-023) —
real-engine equivalence + zero-wire-crossing proof.

Per ``tests/AGENTS.md``: integration over mocks, real substrate via
``t2_service_env`` (wraps ``ensure_engine``/``mint_test_tenant``).

The load-bearing claim under test: the SQL anti-join — keyed on
``chunks_<dim>.chash`` (the PK column) joined to
``catalog_document_chunks.chash`` — classifies exactly the chunks a client-
side fetch-diff-copy-delete algorithm would classify as orphan/live, AND
does so without a single chunk row, document, or embedding crossing the
``/v1/vectors`` wire.

RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: the client-side fallback
(``_fetch_all_chunk_metadata`` + a Python diff against ``catalog
.chashes_for_collection``, plus ``chunk_quarantine.py``'s client-side
``quarantine_orphans``/``restore_rereferenced``/``expire_quarantine``) is
RETIRED — the manifest-chunk FK (catalog-029, VALIDATEd) makes the
completeness apparatus it existed to prove correct unreachable by
construction. Server-side prune is now the ONLY path;
:func:`test_serverside_failure_skips_only_that_collection` and
:func:`test_serverside_path_crosses_zero_chunk_payloads` (the latter
retained, since it still proves the DEFAULT path's zero-wire-crossing
property) cover what remains. The former same-session A/B contrast test
(:func:`test_fallback_path_crosses_full_chunk_payloads`, proving the OLD
path sent full payloads) is DELETED alongside the path it exercised — see
:mod:`nexus.indexer`'s ``_prune_deleted_files`` docstring for the full
retirement rationale; a collection whose engine predates catalog-023 now
goes UNPRUNED with a loud WARNING, never a silent fallback.
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


def test_serverside_prune_sweeps_rdr_collection_when_passed(t2_service_env) -> None:
    """nexus-3lswy regression guard, REBASED onto the server-side path
    (RDR-191 Phase 6, nexus-o8dil.33): RDR became a first-class 4th
    collection (catalog registration, doc_id_resolver, staleness cache all
    cover it), but the GC pass once silently never swept ``rdr__`` orphans
    because the call site never threaded ``rdr_collection`` through. The
    original regression test exercised the (now-retired) client-side
    fallback; this proves the SAME property — the optional
    ``rdr_collection`` kwarg actually gets swept — via the real server-side
    anti-join, the only prune path left. Per-collection isolation: code
    and docs stay untouched/empty while only the rdr collection's orphan
    moves."""
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.chunk_quarantine import quarantine_collection_name
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    rdr_coll = "rdr__gcq-rdrsweep__bge-base-en-v15-768__v1"
    rdr_owner = cat.register_owner("gcq-rdrsweep", "curator")
    _chashes, rdr_live, rdr_orphans = _seed(cat, db, rdr_coll, rdr_owner, n_live=1, n_orphan=1)

    _prune_deleted_files(
        "code__gcq-rdrsweep-unused", "docs__gcq-rdrsweep-unused", db,
        catalog=cat, rdr_collection=rdr_coll,
    )

    remaining = set(db.get_collection(rdr_coll).get_all_metadata()["ids"])
    assert remaining == set(rdr_live), (
        f"the rdr_collection kwarg must actually be swept; expected only "
        f"the live chash to survive, got {remaining}"
    )
    qname = quarantine_collection_name(rdr_coll)
    quarantined = set(db.get_collection(qname).get_all_metadata()["ids"])
    assert quarantined == set(rdr_orphans), (
        f"the rdr collection's orphan must be quarantined; got {quarantined}"
    )


def test_serverside_prune_rerun_with_no_new_orphans_is_idempotent(t2_service_env) -> None:
    """RDR-191 Phase 6 (nexus-o8dil.33) rebase of the former mock-substrate
    ``test_prune_deleted_files_idempotent`` (test_indexer.py) onto the real
    server-side path, the only prune path left: re-running the sweep with
    no NEW orphans since the first pass is a no-op — the first pass's
    quarantine move is not re-attempted, nothing new moves, and the origin/
    quarantine populations are byte-identical across both calls."""
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.chunk_quarantine import quarantine_collection_name
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    coll_name = "code__gcq-idempotent__bge-base-en-v15-768__v1"
    owner = cat.register_owner("gcq-idempotent", "curator")
    chashes, live_chashes, orphan_chashes = _seed(cat, db, coll_name, owner, n_live=2, n_orphan=2)

    _prune_deleted_files(coll_name, "docs__gcq-idempotent-unused", db, catalog=cat)
    first_remaining = set(db.get_collection(coll_name).get_all_metadata()["ids"])
    qname = quarantine_collection_name(coll_name)
    first_quarantined = set(db.get_collection(qname).get_all_metadata()["ids"])
    assert first_remaining == set(live_chashes)
    assert first_quarantined == set(orphan_chashes)

    _prune_deleted_files(coll_name, "docs__gcq-idempotent-unused", db, catalog=cat)
    second_remaining = set(db.get_collection(coll_name).get_all_metadata()["ids"])
    second_quarantined = set(db.get_collection(qname).get_all_metadata()["ids"])
    assert second_remaining == first_remaining, (
        f"a second pass with no new orphans must not change the origin "
        f"collection; first={first_remaining} second={second_remaining}"
    )
    assert second_quarantined == first_quarantined, (
        f"a second pass with no new orphans must not re-move or duplicate "
        f"quarantine entries; first={first_quarantined} second={second_quarantined}"
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
    and let the sweep continue to the NEXT collection (the nexus-ou4tb
    contract) — proven here by letting the second collection go through the
    REAL (now-only) server-side path rather than mocking it too.

    Non-vacuity: the first collection's prune RAISES. If the call site is
    unguarded, the exception escapes ``_prune_deleted_files`` and this test
    ERRORS rather than fails — and the second collection's orphans are never
    quarantined, which is the post-state actually asserted below.

    RDR-191 Phase 6 (nexus-o8dil.33) rebase: the second collection used to
    take the (now-retired) client-side fallback when this test's mock
    returned ``False`` for it; it now delegates to the REAL
    ``_prune_collection_serverside`` instead, so this test still proves
    "the sweep continues to a collection that succeeds" without depending
    on retired machinery.
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

    real_prune_serverside = indexer_mod._prune_collection_serverside

    def _fail_first(db_, collection_name, qname, stamp):
        if collection_name == boom_coll:
            raise ConnectionError("simulated transient engine blip")
        return real_prune_serverside(db_, collection_name, qname, stamp)

    with patch.object(indexer_mod, "_prune_collection_serverside", _fail_first):
        _prune_deleted_files(boom_coll, ok_coll, db, catalog=cat)

    # The raising collection was skipped, NOT swept: everything it had is
    # still in the origin collection, orphans included.
    boom_remaining = set(db.get_collection(boom_coll).get_all_metadata()["ids"])
    assert boom_remaining == set(boom_live) | set(boom_orphans), (
        "a failed server-side prune must leave its collection untouched; "
        f"got {boom_remaining}"
    )

    # The sweep continued: the SECOND collection was still pruned, via the
    # real server-side path.
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


def test_route_unavailable_prunes_nothing_and_crosses_no_wire(t2_service_env) -> None:
    """RDR-191 Phase 6 (nexus-o8dil.33) rebase of the former
    ``test_fallback_path_crosses_full_chunk_payloads``: with the server
    route made unreachable (simulating a pre-catalog-023 engine), there is
    no client-side fallback left to fall back TO — the collection is left
    completely untouched (no quarantine, no deletes) and NOTHING crosses
    the ``/v1/vectors`` wire for it, replacing the old "fallback sends full
    payloads" contrast with "no route means no action, ever, not even a
    read" — the manifest-chunk FK makes the completeness apparatus the old
    fallback existed to prove correct unreachable by construction, so
    retiring the fallback rather than working around its absence is the
    correct fix, not a regression to paper over."""
    import nexus.db.http_vector_client as hvc
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    coll_name = "code__gcq-fallback__bge-base-en-v15-768__v1"
    owner = cat.register_owner("gcq-fallback", "curator")
    chashes, live_chashes, orphan_chashes = _seed(cat, db, coll_name, owner, n_live=3, n_orphan=2)

    posted: list[tuple[str, dict]] = []
    real_post = hvc._post

    def _spy_post(path, body, **kwargs):
        posted.append((path, body))
        return real_post(path, body, **kwargs)

    # Simulate a pre-route engine: the client has no gc_quarantine_orphans
    # capability from the caller's point of view.
    from structlog.testing import capture_logs

    with capture_logs() as cap, \
         patch.object(hvc.HttpVectorClient, "gc_quarantine_orphans", None), \
         patch.object(hvc.HttpVectorClient, "gc_restore_rereferenced", None), \
         patch.object(hvc.HttpVectorClient, "gc_expire_quarantine", None), \
         patch("nexus.db.http_vector_client._post", side_effect=_spy_post):
        _prune_deleted_files(coll_name, "docs__gcq-fallback-unused", db, catalog=cat)

    assert not posted, (
        f"a route-unavailable collection must cross NO wire at all now "
        f"(no client-side fallback exists to fall back to); posted: {posted}"
    )
    # "never a silent fallback" must be PINNED, not assumed: a structured
    # WARNING naming the collection must fire, not just a passive skip.
    warn_logs = [
        r for r in cap
        if r.get("event") == "gc_serverside_prune_unavailable_no_client_fallback"
    ]
    assert warn_logs, (
        f"a route-unavailable collection must log a structured WARNING "
        f"(never a silent skip); captured events: {[r.get('event') for r in cap]}"
    )
    assert warn_logs[0]["collection"] == coll_name
    assert warn_logs[0].get("log_level") == "warning", warn_logs[0]

    remaining = set(db.get_collection(coll_name).get_all_metadata()["ids"])
    assert remaining == set(chashes), (
        "a route-unavailable collection must be left COMPLETELY untouched "
        f"(orphans included — no fallback prunes them any more); got {remaining}"
    )
