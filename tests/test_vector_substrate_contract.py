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

    def test_modify_renames_collection(self, substrate) -> None:
        """col.modify(name=...) — the nx collection rename surface
        (T3Database.rename_collection): rows survive, the new name
        resolves, the old name is gone."""
        col = _make(substrate)
        old = col.name
        new = f"{old}-renamed"
        col.add(ids=["a"], embeddings=[_E1], documents=["doc a"])
        col.modify(name=new)
        renamed = substrate.get_collection(new)
        assert renamed.count() == 1
        with pytest.raises(Exception):
            substrate.get_collection(old)


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

    def test_upsert_existing_id_merges_metadata_keys(self, substrate) -> None:
        """Oracle-verified 2026-07-23: upsert on an existing id replaces
        document + embedding but MERGES metadata at key level (same as
        update). The backfill-hash path depends on this — its normalized
        re-upsert drops non-canonical keys and the merge keeps them."""
        col = _make(substrate)
        col.upsert(ids=["a"], embeddings=[_E1], documents=["first"],
                   metadatas=[{"x": 1, "y": "keep"}])
        col.upsert(ids=["a"], embeddings=[_E1], documents=["second"],
                   metadatas=[{"x": 2}])
        got = col.get(ids=["a"], include=["documents", "metadatas"])
        assert got["documents"] == ["second"]
        assert got["metadatas"][0] == {"x": 2, "y": "keep"}

    def test_embeddings_round_trip_byte_identical(self, substrate) -> None:
        """The reidentify contract: embeddings come back exactly as
        written, NOT normalized (vectors here are deliberately
        non-unit)."""
        raw = [0.25, 0.5, 0.125, 0.0625]
        col = _make(substrate)
        col.add(ids=["a"], embeddings=[raw], documents=["d"])
        got = col.get(ids=["a"], include=["embeddings"])["embeddings"][0]
        assert [float(v) for v in got] == raw

    def test_dimension_mismatch_query_raises_dimension_error(self, substrate) -> None:
        """The embed-migrate / nx doctor stale-collection probe: querying
        with a wrong-dimension vector raises with 'dimension' in the
        message (detect_stale_local_collections classifies on it)."""
        col = _make(substrate)
        col.add(ids=["a"], embeddings=[_E1], documents=["d"])
        with pytest.raises(Exception, match="[Dd]imension"):
            col.query(query_embeddings=[[1.0, 0.0]], n_results=1)

    def test_rejected_wrong_dim_write_leaves_collection_usable(self, substrate) -> None:
        """Blast-radius pin (review finding 2): a refused
        wrong-dimension write must not poison the collection — correct
        subsequent ops keep working. Matters most for the process-scoped
        T1-isolated singleton, where a poisoned collection would break
        every later scratch op in the process."""
        col = _make(substrate)
        col.add(ids=["a"], embeddings=[_E1], documents=["d"])
        with pytest.raises(Exception):
            col.add(ids=["bad"], embeddings=[[1.0, 0.0]], documents=["x"])
        col.add(ids=["b"], embeddings=[_E2], documents=["d2"])
        assert col.count() == 2
        res = col.query(query_embeddings=[_E1], n_results=1)
        assert res["ids"][0] == ["a"]

    def test_metadata_type_round_trip(self, substrate) -> None:
        col = _make(substrate)
        meta = {"s": "text", "i": 7, "f": 0.5, "b": True}
        col.add(ids=["a"], embeddings=[_E1], documents=["d"], metadatas=[meta])
        got = col.get(ids=["a"], include=["metadatas"])["metadatas"][0]
        for key, value in meta.items():
            assert got[key] == value
            assert type(got[key]) is type(value)


class TestUpdateSemantics:
    """The T1 scratch surface: batched ``col.update`` for access-count
    telemetry (t1.py search phase 2 + flag/unflag). Oracle-verified
    2026-07-23: metadata MERGES at key level; unsupplied fields are
    preserved; unknown ids are silently skipped."""

    def test_metadata_only_update_merges_and_preserves(self, substrate) -> None:
        col = _make(substrate)
        col.add(ids=["a"], embeddings=[_E1], documents=["orig doc"],
                metadatas=[{"x": 1, "y": "keep"}])
        col.update(ids=["a"], metadatas=[{"x": 2}])
        got = col.get(ids=["a"], include=["documents", "metadatas"])
        assert got["documents"] == ["orig doc"]
        assert got["metadatas"][0] == {"x": 2, "y": "keep"}
        # embedding untouched: still nearest to _E1 (unit vector, so
        # normalization is identity on both substrates)
        res = col.query(query_embeddings=[_E1], n_results=1)
        assert res["ids"][0] == ["a"]
        assert res["distances"][0][0] == pytest.approx(0.0, abs=1e-6)

    def test_document_and_embedding_update_preserves_metadata(self, substrate) -> None:
        col = _make(substrate)
        col.add(ids=["b"], embeddings=[_E2], documents=["old"],
                metadatas=[{"m": 1}])
        col.update(ids=["b"], documents=["new"], embeddings=[_E1])
        got = col.get(ids=["b"], include=["documents", "metadatas"])
        assert got["documents"] == ["new"]
        assert got["metadatas"][0] == {"m": 1}
        res = col.query(query_embeddings=[_E1], n_results=1)
        assert res["distances"][0][0] == pytest.approx(0.0, abs=1e-6)

    def test_update_unknown_id_is_silent_noop(self, substrate) -> None:
        col = _make(substrate)
        col.add(ids=["a"], embeddings=[_E1], documents=["d"])
        col.update(ids=["missing"], metadatas=[{"z": 9}])
        assert col.get(ids=["missing"])["ids"] == []
        assert col.count() == 1

    def test_batched_update(self, substrate) -> None:
        col = _make(substrate)
        col.add(ids=["a", "b"], embeddings=[_E1, _E2],
                documents=["da", "db"],
                metadatas=[{"n": 0}, {"n": 0}])
        col.update(ids=["a", "b"], metadatas=[{"n": 1}, {"n": 2}])
        got = col.get(ids=["a", "b"], include=["metadatas"])
        by_id = dict(zip(got["ids"], got["metadatas"]))
        assert by_id["a"]["n"] == 1
        assert by_id["b"]["n"] == 2


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

    def test_where_numeric_comparisons(self, substrate) -> None:
        """$gt/$gte/$lt/$lte — the T3 expire path filters ttl_days with
        $gt (t3.py). Oracle-verified 2026-07-23."""
        col = _make(substrate)
        _seed(col)
        assert sorted(col.get(where={"rank": {"$gt": 2}})["ids"]) == ["c", "d"]
        assert sorted(col.get(where={"rank": {"$gte": 3}})["ids"]) == ["c", "d"]
        assert sorted(col.get(where={"rank": {"$lt": 2}})["ids"]) == ["a"]
        assert sorted(col.get(where={"rank": {"$lte": 2}})["ids"]) == ["a", "b"]

    def test_where_comparison_skips_rows_missing_the_field(self, substrate) -> None:
        col = _make(substrate)
        _seed(col)
        col.add(ids=["e"], embeddings=[_E2], documents=["doc e"],
                metadatas=[{"other": 5}])
        got = col.get(where={"rank": {"$gt": 0}})
        assert "e" not in got["ids"]
        assert sorted(got["ids"]) == ["a", "b", "c", "d"]

    def test_peek_first_rows_with_embeddings(self, substrate) -> None:
        """peek() — the verify-deep probe surface (t3.py). First N rows in
        insertion order; ids, embeddings, documents, metadatas present."""
        col = _make(substrate)
        _seed(col)
        p = col.peek(limit=2)
        assert p["ids"] == ["a", "b"]
        assert p["documents"] == ["doc a", "doc b"]
        assert len(p["metadatas"]) == 2
        assert p["embeddings"] is not None and len(p["embeddings"]) == 2

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


# ── Consumer integration: the T3Database facade wrap ─────────────────────────
# The ``nx index --dry-run`` leg (and the canonical test-fixture pattern)
# wraps a substrate client in T3Database via ``_client=`` injection. Pin
# that wrap differentially: strict naming guard, collection creation with
# the production cosine metadata, and the get() preview read the dry-run
# leg performs.


class TestT3FacadeWrap:
    def _t3(self, substrate):
        from nexus.db.t3 import T3Database

        return T3Database(_client=substrate, _ef_override=_ToyEF())

    def test_dry_run_call_shape(self, substrate) -> None:
        t3 = self._t3(substrate)
        name = f"docs__contract-owner__toy-ef__v{uuid.uuid4().int % 9 + 1}"
        col = t3.get_or_create_collection(name)
        col.add(ids=["c1", "c2"], documents=["alpha beta", "gamma delta"],
                metadatas=[{"page_number": 1}, {"page_number": 2}])
        preview = t3.get_or_create_collection(name).get(
            include=["documents", "metadatas"],
        )
        assert sorted(preview["ids"]) == ["c1", "c2"]
        assert len(preview["documents"]) == 2
        assert {m["page_number"] for m in preview["metadatas"]} == {1, 2}

    def test_nonconformant_name_rejected(self, substrate) -> None:
        t3 = self._t3(substrate)
        with pytest.raises(ValueError, match="not conformant"):
            t3.get_or_create_collection("docs__legacy-two-segment")
