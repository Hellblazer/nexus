# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-engine integration test for the ``_prune_deleted_files`` /
``_fetch_all_chunk_metadata`` single-shot ``get_all_metadata`` fast path
(index-output-ux-assessment-2026-08-10 §7.2/a3: 547.8s of a real
``nx index repo`` run, 70% of wall, was this function's paginated sweep).

Deliberately a standalone module rather than an addition to
``test_indexer.py`` / ``test_indexer_e2e.py``: both of those files carry
module- or class-scoped fixtures (``test_indexer.py``'s
``pytestmark = pytest.mark.usefixtures("cloud_mode")``;
``test_indexer_e2e.py``'s autouse ``_legacy_vector_backend`` pinning
``NX_STORAGE_BACKEND_VECTORS=local``) that exist for THOSE files' own
client-embed / cloud-canonical-naming test needs and would fight a test
that wants the real server-embedded service-vector path this fast path
actually runs against in production.

Per ``tests/AGENTS.md``: integration over mocks, real substrate via
``t2_service_env`` (wraps ``ensure_engine``/``mint_test_tenant`` from
``tests/_engine_substrate.py``).
"""
from __future__ import annotations

import hashlib

import pytest

pytestmark = [pytest.mark.integration]


def test_prune_deleted_files_fast_path_against_real_engine(t2_service_env) -> None:
    """End-to-end: real HTTP ``POST /v1/vectors/get-all-metadata`` round
    trip against a live engine, not a mock. Seeds live + orphan chunks
    through the real server-side-embed write path (``t2_service_env`` sets
    ``NX_LOCAL=1`` — RDR-160: the local engine embeds every collection
    with bge-768 regardless of the collection name's model segment, so no
    Voyage credential is needed here), runs the real ``_prune_deleted_files``
    against the real engine, and asserts:

    1. the fast path actually fired — a genuine
       ``POST /v1/vectors/get-all-metadata`` round trip was made and the
       old ``POST /v1/vectors/get`` full-collection-scan path (what
       ``_paginated_get`` uses) was NOT — i.e. this run did not silently
       degrade to the paginated sweep it replaces, and
    2. orphan/live classification is still correct end-to-end (live chunks
       survive, orphans are quarantined) — the perf fix didn't cost
       correctness.

    The "no fallback-warning log" signal alone is NOT sufficient here (it
    would pass vacuously under the OLD code too, which never emits that
    event because it never attempts the fast path at all) — this spies on
    the actual outgoing HTTP path instead, at the one seam
    (``nexus.db.http_vector_client._post``) both routes share.
    """
    from unittest.mock import patch

    import nexus.db.http_vector_client as hvc
    from nexus.indexer import _prune_deleted_files
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    # bge-base-en-v15-768: the RDR-160 local-mode server-embed token
    # (nexus.db.local_ef._MODEL_TOKENS) — corpus.py's
    # effective_embedding_model_for_writes documents that in
    # service-vector + local mode the SERVER embeds with bge-768
    # independent of what the collection name's model segment says, but a
    # MISMATCHED segment still gets refused (422) — so name the collection
    # with the real token rather than a placeholder.
    coll_name = "code__gam-fastpath__bge-base-en-v15-768__v1"
    owner = cat.register_owner("gam-fastpath", "curator")

    n_live, n_orphan = 6, 4
    ids: list[str] = []
    chashes: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for i in range(n_live + n_orphan):
        text = f"def probe_{i}(): return {i}\n"
        chash = hashlib.sha256(f"{coll_name}:{i}".encode()).hexdigest()
        chashes.append(chash)
        ids.append(chash)  # RDR-180: chunk ids are the FULL 64-hex chash
        docs.append(text)
        metas.append({"chunk_text_hash": chash, "title": f"probe_{i}.py:1-1"})

    db.upsert_chunks_with_embeddings(
        coll_name, ids=ids, documents=docs, embeddings=[], metadatas=metas,
    )

    # Register + manifest only the LIVE subset — the remaining n_orphan
    # chashes are real T3 chunks with no manifest row anywhere, i.e.
    # orphans by construction (no delete-then-GC choreography needed).
    live_chashes = set(chashes[:n_live])
    for i in range(n_live):
        tumbler = str(cat.register(
            owner, f"probe_{i}.py",
            content_type="code",
            file_path=f"/tmp/gam-fastpath/probe_{i}.py",
            physical_collection=coll_name,
            chunk_count=1,
        ))
        cat.write_manifest(tumbler, [{"chash": chashes[i], "position": 0}])

    # (path, collection) per outgoing POST. chunk_quarantine.py (out of
    # this fix's scope — see the developer report) still does its own
    # small paginated ``/v1/vectors/get`` sweep of the QUARANTINE SIBLING
    # collection (``quarantine-code__...``) once this run has moved
    # orphans into it, so the assertion below scopes to the ORIGIN
    # collection specifically, not "no /v1/vectors/get call at all".
    posted: list[tuple[str, str]] = []
    real_post = hvc._post

    def _spy_post(path, body, **kwargs):
        posted.append((path, body.get("collection", "")))
        return real_post(path, body, **kwargs)

    with patch("nexus.db.http_vector_client._post", side_effect=_spy_post):
        _prune_deleted_files(coll_name, "docs__gam-fastpath-unused", db, catalog=cat)

    posted_paths = [p for p, _ in posted]
    assert "/v1/vectors/get-all-metadata" in posted_paths, (
        f"expected the single-shot fast path to fire; posted paths: {posted}"
    )
    origin_full_scans = [
        (p, c) for p, c in posted if p == "/v1/vectors/get" and c == coll_name
    ]
    assert not origin_full_scans, (
        "the full-collection PAGINATED scan (/v1/vectors/get) fired against "
        f"the ORIGIN collection — the fast path silently degraded to the "
        f"sweep it replaces: {posted}"
    )

    remaining = set(db.get_collection(coll_name).get_all_metadata()["ids"])
    assert remaining == set(ids[:n_live]), (
        f"expected exactly the live ids to survive; got {remaining}"
    )
