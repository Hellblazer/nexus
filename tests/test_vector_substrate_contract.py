"""RDR-155 P4b Phase 0a: the vector-substrate parity contract.

THE DIFFERENTIAL HARNESS (8zpmf verification strengthening, 2026-07-23):
every semantic the in-process vector consumers rely on — collection
lifecycle, upsert dedup, the Mongo-style where subset, COSINE distance
ordering, delete semantics, metadata round-trips — pinned as executable
contracts and run against EVERY registered substrate. chromadb
(EphemeralClient) is the oracle today; the InMemoryVectorStore joins the
``SUBSTRATES`` list when born (P0a), and any divergence between them is a
FINDING, not drift. The chroma entry deletes WITH the dependency at P3 —
at which point this suite becomes the in-memory store's permanent
conformance pin.

Semantics sources: T3Database pins ``hnsw:space=cosine`` at collection
creation (chroma's default is L2 — the exact trap the plan critique
named); the where-grammar subset is what the T1/plan-cache/test consumers
actually use (``$eq`` implicit + explicit, ``$in``, ``$and``); upsert is
last-write-wins by id. Embeddings are passed EXPLICITLY in core contracts
so store semantics are tested independently of any embedding function; a
deterministic toy EF covers the EF-attached surface.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

import pytest

# ── Substrate registry ───────────────────────────────────────────────────────
# Each entry: (name, factory) — factory returns a chroma-shaped client.
# The InMemoryVectorStore registers here at P0a implementation time; the
# chromadb entry deletes with the dependency at P3.


def _chroma_client():
    import chromadb

    return chromadb.EphemeralClient()


def _inmemory_client():
    from nexus.db.inmemory_vector_store import InMemoryVectorClient

    return InMemoryVectorClient()


SUBSTRATES: list[tuple[str, Any]] = [
    ("chromadb", _chroma_client),
    ("inmemory", _inmemory_client),
]


@pytest.fixture(params=[name for name, _ in SUBSTRATES])
def substrate(request: pytest.FixtureRequest):
    factory = dict(SUBSTRATES)[request.param]
    return factory()


def _fresh_name() -> str:
    # EphemeralClient instances share process state (the documented
    # SharedSystemClient gotcha) — unique names isolate contract tests
    # regardless of substrate instance boundaries.
    return f"contract__{uuid.uuid4().hex[:12]}"


def _make(substrate, **kwargs):
    """Create a collection the way production does: COSINE pinned."""
    return substrate.create_collection(
        _fresh_name(), metadata={"hnsw:space": "cosine"}, **kwargs
    )


# Fixed 4-d unit-ish vectors with known cosine relationships.
_E1 = [1.0, 0.0, 0.0, 0.0]
_E2 = [0.0, 1.0, 0.0, 0.0]          # orthogonal to _E1
_E1_NEAR = [0.9, 0.1, 0.0, 0.0]     # close to _E1
_E1_NEG = [-1.0, 0.0, 0.0, 0.0]     # opposite of _E1


# ── Collection lifecycle ─────────────────────────────────────────────────────


class TestCollectionLifecycle:
    def test_create_then_get(self, substrate) -> None:
        col = _make(substrate)
        assert substrate.get_collection(col.name).name == col.name

    def test_get_missing_raises(self, substrate) -> None:
        with pytest.raises(Exception):
            substrate.get_collection("contract__does_not_exist")

    def test_get_or_create_is_idempotent(self, substrate) -> None:
        name = _fresh_name()
        c1 = substrate.get_or_create_collection(name)
        c1.add(ids=["a"], embeddings=[_E1], documents=["doc a"])
        c2 = substrate.get_or_create_collection(name)
        assert c2.count() == 1

    def test_delete_collection(self, substrate) -> None:
        col = _make(substrate)
        substrate.delete_collection(col.name)
        with pytest.raises(Exception):
            substrate.get_collection(col.name)


# ── Write semantics ──────────────────────────────────────────────────────────


class TestWriteSemantics:
    def test_add_and_count(self, substrate) -> None:
        col = _make(substrate)
        col.add(ids=["a", "b"], embeddings=[_E1, _E2],
                documents=["doc a", "doc b"])
        assert col.count() == 2

    def test_upsert_same_id_is_last_write_wins(self, substrate) -> None:
        col = _make(substrate)
        col.upsert(ids=["a"], embeddings=[_E1], documents=["first"],
                   metadatas=[{"v": 1}])
        col.upsert(ids=["a"], embeddings=[_E1], documents=["second"],
                   metadatas=[{"v": 2}])
        assert col.count() == 1
        got = col.get(ids=["a"], include=["documents", "metadatas"])
        assert got["documents"] == ["second"]
        assert got["metadatas"][0]["v"] == 2

    def test_metadata_type_round_trip(self, substrate) -> None:
        col = _make(substrate)
        meta = {"s": "text", "i": 7, "f": 0.5, "b": True}
        col.add(ids=["a"], embeddings=[_E1], documents=["d"], metadatas=[meta])
        got = col.get(ids=["a"], include=["metadatas"])["metadatas"][0]
        for key, value in meta.items():
            assert got[key] == value
            assert type(got[key]) is type(value)


# ── Read/get semantics: ids, where subset, paging ────────────────────────────


def _seed(col) -> None:
    col.add(
        ids=["a", "b", "c", "d"],
        embeddings=[_E1, _E2, _E1_NEAR, _E1_NEG],
        documents=["doc a", "doc b", "doc c", "doc d"],
        metadatas=[
            {"kind": "x", "rank": 1},
            {"kind": "y", "rank": 2},
            {"kind": "x", "rank": 3},
            {"kind": "z", "rank": 4},
        ],
    )


class TestGetSemantics:
    def test_get_by_ids_preserves_request_shape(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        got = col.get(ids=["a", "c"], include=["documents"])
        assert sorted(got["ids"]) == ["a", "c"]
        assert len(got["documents"]) == 2

    def test_where_implicit_eq(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        got = col.get(where={"kind": "x"})
        assert sorted(got["ids"]) == ["a", "c"]

    def test_where_explicit_eq(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        got = col.get(where={"kind": {"$eq": "y"}})
        assert got["ids"] == ["b"]

    def test_where_in(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        got = col.get(where={"kind": {"$in": ["y", "z"]}})
        assert sorted(got["ids"]) == ["b", "d"]

    def test_where_and(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        got = col.get(where={"$and": [{"kind": "x"}, {"rank": 3}]})
        assert got["ids"] == ["c"]

    def test_where_no_match_is_empty_not_error(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        got = col.get(where={"kind": "nope"})
        assert got["ids"] == []

    def test_limit_offset_page_through_everything(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        page1 = col.get(limit=3, offset=0)
        page2 = col.get(limit=3, offset=3)
        seen = sorted(page1["ids"] + page2["ids"])
        assert seen == ["a", "b", "c", "d"]
        assert len(page1["ids"]) == 3
        assert len(page2["ids"]) == 1


# ── Query semantics: COSINE ordering and distances ───────────────────────────


class TestQuerySemantics:
    def test_cosine_ordering_nearest_first(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        res = col.query(query_embeddings=[_E1], n_results=4)
        # Exact ordering by cosine distance from _E1:
        # a (identical) < c (near) < b (orthogonal) < d (opposite)
        assert res["ids"][0] == ["a", "c", "b", "d"]

    def test_cosine_distance_values(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        res = col.query(query_embeddings=[_E1], n_results=4)
        dists = res["distances"][0]
        assert math.isclose(dists[0], 0.0, abs_tol=1e-6)   # identical
        assert math.isclose(dists[2], 1.0, abs_tol=1e-6)   # orthogonal
        assert math.isclose(dists[3], 2.0, abs_tol=1e-6)   # opposite
        assert dists == sorted(dists)

    def test_n_results_greater_than_count_returns_all(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        res = col.query(query_embeddings=[_E2], n_results=50)
        assert len(res["ids"][0]) == 4

    def test_query_with_where_filters_candidates(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        res = col.query(query_embeddings=[_E1], n_results=4,
                        where={"kind": "x"})
        assert res["ids"][0] == ["a", "c"]

    def test_query_empty_collection_is_empty_shape(self, substrate) -> None:
        col = _make(substrate)
        res = col.query(query_embeddings=[_E1], n_results=3)
        assert res["ids"] == [[]]


# ── Delete semantics ─────────────────────────────────────────────────────────


class TestDeleteSemantics:
    def test_delete_by_ids(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        col.delete(ids=["a", "d"])
        assert col.count() == 2
        assert sorted(col.get()["ids"]) == ["b", "c"]

    def test_delete_by_where(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        col.delete(where={"kind": "x"})
        assert sorted(col.get()["ids"]) == ["b", "d"]

    def test_delete_missing_id_is_noop(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        col.delete(ids=["not_there"])
        assert col.count() == 4


# ── EF-attached surface (documents-only add embeds via the EF) ───────────────


class _ToyEF:
    """Deterministic 4-d embedding: hashes tokens onto axes. Enough to
    prove the EF-attachment plumbing (add without embeddings, query by
    text) works identically across substrates."""

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — chroma EF protocol name
        out = []
        for text in input:
            vec = [0.0, 0.0, 0.0, 0.0]
            for token in text.split():
                vec[hash(token) % 4] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    # chroma 1.x EF protocol: queries route through embed_query; the
    # installed version also introspects name/is_legacy.
    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — chroma EF protocol name
        return self(input)

    def name(self) -> str:
        return "toy-ef-4d"

    def is_legacy(self) -> bool:
        return True


class TestEfAttachedSurface:
    def test_documents_only_add_then_text_query(self, substrate) -> None:
        col = substrate.create_collection(
            _fresh_name(), metadata={"hnsw:space": "cosine"},
            embedding_function=_ToyEF(),
        )
        col.add(ids=["a", "b"], documents=["alpha beta", "gamma delta"])
        res = col.query(query_texts=["alpha beta"], n_results=2)
        assert res["ids"][0][0] == "a"
