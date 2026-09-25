# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-iygza (indexing-brittleness P0.1, client half): drain the chunks a
taxonomized collection holds with no assignment to its own topics.

A taxonomy-assign batch that still fails after the split-in-half retry
(nexus-mg8gx) used to be gone: the tripwire's hook_failures row keeps only
the first chash. Sam's design (2026-09-25) derives the backlog from state
instead of recording it: the engine lists unassigned, manifest-backed
chunks (POST /v1/taxonomy/assignments/unassigned, engine-service-v0.1.132),
and the client feeds each page through the same retrying assign path.

Real engine substrate: the listing is an antijoin the engine computes, and
an assignment has to persist for the next page to see it gone.
"""
from __future__ import annotations

import hashlib
import inspect
import itertools

import httpx
import pytest
from click.testing import CliRunner

import nexus.commands.index as index_mod
import nexus.mcp_infra as mcp_infra
from nexus.cli import main
from nexus.commands.index import _drain_repo_collections
from nexus.db import make_t3
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.mcp_infra import DrainResult, drain_unassigned_chunks
from tests._catalog_fixture_ops import ActiveCatalog

_COLL = "knowledge__iygza-drain__bge-base-en-v15-768__v1"
_BARE = "knowledge__iygza-bare__bge-base-en-v15-768__v1"
#: topics.id is one global sequence in the substrate database, shared by every
#: tenant a test mints, so each seeded topic needs its own id.
_TOPIC_IDS = itertools.count(91001)


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _seed(collection: str, n: int, *, topic: bool) -> tuple[list[str], int | None]:
    """n manifest-backed chunks in *collection*; one topic whose centroid is
    the first chunk's own vector when *topic*. Returns (chashes, topic_id)."""
    t3 = make_t3()
    docs = [f"iygza drain chunk {i} about lighthouses and tides" for i in range(n)]
    ids = [_chash(d) for d in docs]
    t3.upsert_chunks_with_embeddings(
        collection_name=collection, ids=ids, documents=docs,
        embeddings=[[] for _ in ids], metadatas=[{} for _ in ids],
    )
    cat = ActiveCatalog()
    owner = cat.register_owner("iygza-owner", "curator")
    tumbler = str(cat.register(
        owner, f"{collection}-doc", content_type="knowledge",
        physical_collection=collection, chunk_count=n,
    ))
    cat.write_manifest(
        tumbler, [{"chash": c, "position": i} for i, c in enumerate(ids)],
        collection=collection,
    )
    if not topic:
        return ids, None
    tax = HttpTaxonomyStore()
    topic_id = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="iygza-topic", parent_id=None, collection=collection,
        centroid_hash=None, doc_count=0, created_at="2026-09-25T00:00:00Z",
        review_status="pending", terms=None,
    )
    vec = t3.get_embeddings(collection, ids[:1])[0].tolist()
    tax._centroid.upsert([{
        "collection": collection, "topic_id": topic_id, "embedding": vec,
        "label": "iygza-topic", "doc_count": 0,
    }])
    return ids, topic_id


def test_store_lists_unassigned_with_a_cursor(t2_service_env) -> None:
    ids, _ = _seed(_COLL, 5, topic=True)
    tax = HttpTaxonomyStore()

    first = tax.unassigned_chashes(_COLL, limit=3)
    assert first["has_taxonomy"] is True
    assert len(first["chashes"]) == 3
    assert first["next_after"] == first["chashes"][-1]

    rest = tax.unassigned_chashes(_COLL, limit=3, after=first["next_after"])
    assert len(rest["chashes"]) == 2
    assert rest["next_after"] is None
    assert sorted(first["chashes"] + rest["chashes"]) == sorted(ids)


def test_drain_assigns_every_unassigned_chunk(t2_service_env) -> None:
    ids, topic_id = _seed(_COLL, 7, topic=True)

    result = drain_unassigned_chunks(_COLL, page_size=3)

    assert result.has_taxonomy and result.found == 7 and result.assigned == 7
    assert result.lost == 0 and not result.truncated
    got = HttpTaxonomyStore().get_assignments_for_docs(ids)
    assert all(got[c] == topic_id for c in ids), got
    # Non-vacuity: a second drain finds nothing left.
    again = drain_unassigned_chunks(_COLL, page_size=3)
    assert again.found == 0


def test_drain_stops_at_the_chunk_budget(t2_service_env) -> None:
    _seed(_COLL, 7, topic=True)

    result = drain_unassigned_chunks(_COLL, page_size=3, max_chunks=4)

    assert result.found == 4 and result.truncated
    assert HttpTaxonomyStore().unassigned_chashes(_COLL, limit=10)["chashes"], (
        "chunks past the budget stay for the next run"
    )


def test_a_collection_without_topics_is_skipped(t2_service_env) -> None:
    _seed(_BARE, 3, topic=False)

    result = drain_unassigned_chunks(_BARE)

    assert result.has_taxonomy is False and result.found == 0 and result.skipped_reason == ""


def test_an_engine_without_the_route_is_a_named_skip(monkeypatch) -> None:
    def _404(*a, **kw):
        req = httpx.Request("POST", "http://engine/v1/taxonomy/assignments/unassigned")
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))

    monkeypatch.setattr(HttpTaxonomyStore, "unassigned_chashes", _404)

    result = drain_unassigned_chunks(_COLL)

    assert result.found == 0 and "engine" in result.skipped_reason


def test_cli_drain_reports_and_assigns(t2_service_env) -> None:
    ids, _ = _seed(_COLL, 4, topic=True)

    out = CliRunner().invoke(main, ["taxonomy", "drain", "-c", _COLL])

    assert out.exit_code == 0, out.output
    assert "4 unassigned" in out.output and "4 assigned" in out.output, out.output
    assert not HttpTaxonomyStore().unassigned_chashes(_COLL)["chashes"]


def test_cli_drain_exits_nonzero_when_chunks_are_lost(monkeypatch, t2_service_env) -> None:

    _seed(_COLL, 2, topic=True)
    monkeypatch.setattr(
        mcp_infra, "_assign_from_chashes_with_retry",
        lambda collection, doc_ids, **kw: ({"assigned": 0}, list(doc_ids), ["HTTP 500"]),
    )

    out = CliRunner().invoke(main, ["taxonomy", "drain", "-c", _COLL])

    assert out.exit_code == 1, out.output
    assert "2 lost" in out.output, out.output


@pytest.mark.parametrize("flag", [[], ["-c", "x", "--all"]])
def test_cli_drain_requires_exactly_one_scope(flag) -> None:
    out = CliRunner().invoke(main, ["taxonomy", "drain", *flag])
    assert out.exit_code == 2, out.output


def test_index_drain_is_quiet_when_nothing_is_unassigned(monkeypatch, capsys) -> None:

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", lambda name, **kw: DrainResult(name, True))
    _drain_repo_collections(["code__iygza-quiet"])
    assert capsys.readouterr().out == ""


def test_index_drain_reports_work_and_survives_a_failure(monkeypatch, capsys) -> None:

    def _drain(name, **kw):
        if name == "bad":
            raise RuntimeError("engine 503")
        return DrainResult(name, True, found=76, assigned=76, truncated=False)

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _drain)
    _drain_repo_collections(["bad", "docs__1-1"])
    out = capsys.readouterr().out
    assert "bad failed (RuntimeError); next run retries" in out
    assert "docs__1-1: 76 of 76 unassigned chunk(s) assigned, 0 lost" in out


def test_index_repo_calls_the_drain() -> None:
    assert "_drain_repo_collections(collections, client=_t2_client)" in inspect.getsource(index_mod)
