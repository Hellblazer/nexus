# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-077 projection-quality columns + ICF hub detection.

Phase 1 (nexus-nsh): migration adds ``similarity``, ``assigned_at``,
``source_collection`` columns and ``idx_topic_assignments_source`` index to
``topic_assignments``.
Phase 2 (nexus-uti): write-path atomic commit — AssignResult, prefer-higher
UPSERT, 3-tuple tuple shape across all five call sites.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from nexus.db.t2 import T2Database
from tests._t2_fixture_ops import canonical_chunk_id
from tests.conftest import make_vector_test_client
from typing import Any


def _seed_chunks_for_tenant(
    tenant: str, collection: str, chash_hexes: list[str], *, dim: int = 384,
) -> None:
    """RDR-194 P3d (nexus-tk070.p3d): seed real nexus.chunks rows so a
    topic_assignments insert for (tenant, collection, chash) satisfies the
    new topic_assignments_chunk_fk composite FK. Mirrors tests/test_taxonomy.py's
    helper of the same name (this module has no import path to it, so a
    copy lives here instead). Batched into ONE multi-row INSERT — several
    callers in this file seed hundreds of doc_ids per collection.
    """
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — laziness contract, see module docstring

    if not chash_hexes:
        return
    state = ensure_engine()
    embed_col = {384: "embedding_384", 768: "embedding_768", 1024: "embedding_1024"}[dim]
    vec = "[" + ",".join(["0"] * dim) + "]"
    values = ", ".join(
        f"('{tenant}', '{collection}', decode('{c}', 'hex'), 'seed', '{vec}'::vector)"
        for c in chash_hexes
    )
    sql = (
        f"INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('{tenant}', '{collection}') "
        "ON CONFLICT DO NOTHING; "
        f"INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, {embed_col}) "
        f"VALUES {values} ON CONFLICT DO NOTHING;"
    )
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
            "-U", state["pg_user"], "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"_seed_chunks_for_tenant failed: {proc.stdout}\n{proc.stderr}"


@pytest.fixture(autouse=True)
def _no_live_claude_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARD guard (RDR-155 P4b P0a'): nothing in this module may ever reach
    the real ``claude -p`` subprocess (operators fall back from a failed
    SQL fast path to live dispatch with a 300s timeout per call — a
    substrate regression must fail loud, not hang the suite)."""
    async def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("test must not reach live claude dispatch")

    monkeypatch.setattr(
        "nexus.operators.dispatch.claude_dispatch", _forbidden,
    )


def _insert_topic(
    db: T2Database, *, topic_id: int, collection: str,
    label: str = "seed", created_at: str = "2026-04-14",
) -> None:
    """Seed one ``topics`` row with an explicit id on either substrate.

    SQLite: raw INSERT OR IGNORE (the pre-flip shape). Engine: the
    fidelity-preserving ``import_topic`` surface, which preserves the
    explicit id (RDR-155 P4b P0a' — raw-conn seeding routed through the
    import surface instead of psql).
    """
    db.taxonomy.import_topic(
        src_id=topic_id, label=label, parent_id=None,
        collection=collection, centroid_hash=None, doc_count=0,
        created_at=_full_iso(created_at), review_status="pending",
        terms=None,
    )


def _unique_topic_base() -> int:
    """Return a session-unique topic-id base.

    The engine's ``topics_pk`` is PRIMARY KEY (id) — GLOBAL across tenants —
    and the whole pytest session shares ONE engine, so explicit literal ids
    (1, 2, ...) collide with the BIGSERIAL ids other tests' discover_topics
    already claimed (409 integrity violation on the fidelity import).
    Microsecond-monotonic bases keep every test's explicit ids disjoint
    from each other and from the (small) serial range. Used on both
    substrates so the two legs assert the same shapes.
    """
    import time

    return 1_000_000 + (time.time_ns() // 1_000) % 10**12


def _full_iso(ts: str) -> str:
    """Normalize the fixtures' date / naive-datetime literals to the full
    ISO-8601 shape the engine's import surface requires (parseTsStrict);
    SQLite stores the literal verbatim so only the engine branch calls this."""
    if len(ts) == 10:  # bare date
        ts = f"{ts}T00:00:00"
    if not (ts.endswith("Z") or "+" in ts[10:]):
        ts = f"{ts}Z"
    return ts


# _make_taxonomy_db + TestAddProjectionQualityColumns DELETED (RDR-158 P4
# Stage 4, nexus-i711w): their subject was the 4.3.0
# _add_projection_quality_columns migration, which died with
# nexus/db/migrations.py. The live write-path/ICF behaviour these columns
# feed is pinned by the remaining classes below.


# ── Phase 2 (nexus-uti) — write-path atomic commit ──────────────────────────


@pytest.fixture()
def chroma_client() -> Any:
    """Ephemeral ChromaDB client per test.

    nexus-alnpa: ``make_vector_test_client()`` instances share a
    process-global in-memory backend, so collections leak across tests and
    across files within the same process. A sibling test that leaves a
    same-named collection (e.g. ``nt_coll``) pollutes this file's
    ``discover_topics``/``assign_single`` calls, which then fail ONLY under
    full-suite ordering (passes solo). Clear all collections on entry so each
    test starts from a clean backend regardless of what ran before. See the
    ``project_chromadb_ephemeral_shared_state`` note.
    """
    client = make_vector_test_client()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    return client


@pytest.fixture()
def db(tmp_path: Path) -> T2Database:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


def _seed_topic(db: T2Database, *, topic_id: int = 1, collection: str = "code__src") -> None:
    """Create a single topic row for assignment tests."""
    _insert_topic(db, topic_id=topic_id, collection=collection)


def _read_assignment(db: T2Database, doc_id: str, topic_id: int) -> dict | None:
    row = db.taxonomy.conn.execute(
        "SELECT doc_id, topic_id, assigned_by, similarity, "
        "assigned_at, source_collection "
        "FROM topic_assignments WHERE doc_id = ? AND topic_id = ?",
        (doc_id, topic_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "doc_id": row[0],
        "topic_id": row[1],
        "assigned_by": row[2],
        "similarity": row[3],
        "assigned_at": row[4],
        "source_collection": row[5],
    }



def _build_two_clusters_in_chroma(
    client: Any, collection_name: str = "coll_A",
) -> list[dict]:
    """Seed ``collection_name`` centroids for two well-separated clusters."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    return embeddings


class TestAssignSingleReturnsNamedTuple:
    """SC-2 case 4: AssignResult shape + distance→similarity inversion."""

    def test_assign_single_returns_namedtuple(
        self, db: T2Database, chroma_client: Any, t2_service_env: str,
    ) -> None:
        from nexus.db.t2.taxonomy_compute import AssignResult

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [canonical_chunk_id(f"d-{i}") for i in range(60)]
        texts = [f"text {i}" for i in range(60)]
        # RDR-194 P3d: discover_topics persists the discovered CLUSTER as
        # topic_assignments rows too — each doc_id needs a matching
        # nexus.chunks row for topic_assignments_chunk_fk.
        _seed_chunks_for_tenant(t2_service_env, "nt_coll", doc_ids)
        db.taxonomy.discover_topics(
            "nt_coll", doc_ids, embeddings, texts, chroma_client,
        )

        # Query with an embedding close to cluster A.
        new_emb = rng.standard_normal(384).astype(np.float32) * 0.1
        new_emb[0] += 3.0
        result = db.taxonomy.assign_single("nt_coll", new_emb, chroma_client)
        assert result is not None
        assert isinstance(result, AssignResult)
        assert isinstance(result.topic_id, int)
        assert isinstance(result.similarity, float)
        # Raw cosine ∈ [-1, 1]; near-cluster-A query should give a positive sim.
        assert -1.0 <= result.similarity <= 1.0


class TestProjectAgainst3Tuple:
    """``project_against`` emits 3-tuples with raw cosine similarity."""

    def test_chunk_assignments_carry_similarity(
        self, db: T2Database, chroma_client: Any, t2_service_env: str,
    ) -> None:
        rng = np.random.default_rng(42)
        for name in ("code__pA", "code__pB"):
            embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
            embs[:30, 0] += 3.0
            embs[30:, 1] += 3.0
            doc_ids = [canonical_chunk_id(f"{name}-d{i}") for i in range(60)]
            texts = [f"text {name} {i}" for i in range(60)]
            # RDR-194 P3d: discover_topics persists the discovered CLUSTER
            # as topic_assignments rows too — each doc_id needs a matching
            # nexus.chunks row for topic_assignments_chunk_fk.
            _seed_chunks_for_tenant(t2_service_env, name, doc_ids)
            db.taxonomy.discover_topics(
                name, doc_ids, embs, texts, chroma_client,
            )
            src_coll = chroma_client.get_or_create_collection(
                name, embedding_function=None,
            )
            src_coll.add(
                ids=doc_ids,
                embeddings=embs.tolist(),
                documents=texts,
            )

        result = db.taxonomy.project_against(
            "code__pA", ["code__pB"], chroma_client, threshold=-1.0,
        )
        chunk_assignments = result["chunk_assignments"]
        assert chunk_assignments, "expected at least one projection match"
        for item in chunk_assignments:
            assert len(item) == 3, "each assignment must be a 3-tuple"
            doc_id, topic_id, similarity = item
            assert isinstance(doc_id, str)
            assert isinstance(topic_id, int)
            assert isinstance(similarity, float)


# ── Phase 3 (nexus-qab) — ICF computation ───────────────────────────────────


def _seed_projection_rows(
    db: T2Database, rows: list[tuple[str, int, str]], tenant: str,
) -> None:
    """Seed projection topic_assignments with ``(doc_id, topic_id, source_collection)``.

    Creates referenced ``topics`` rows on demand so FKs resolve.

    RDR-194 P3d: also seeds a matching nexus.chunks row per (source_collection,
    doc_id) — topic_assignments_chunk_fk requires it before assign_topic's
    INSERT below.
    """
    topic_ids = {tid for _, tid, _ in rows}
    for tid in topic_ids:
        # Per-topic label: the engine enforces root-topic uniqueness on
        # (tenant_id, collection, label) (taxonomy-004), so a shared
        # 'seed' label 23505s on the second import.
        _insert_topic(db, topic_id=tid, collection="code__any",
                      label=f"seed-{tid}")
    by_collection: dict[str, list[str]] = {}
    for doc_id, _tid, src in rows:
        by_collection.setdefault(src, []).append(canonical_chunk_id(doc_id))
    for src, chashes in by_collection.items():
        _seed_chunks_for_tenant(tenant, src, chashes)
    for doc_id, tid, src in rows:
        db.taxonomy.assign_topic(
            canonical_chunk_id(doc_id), tid, assigned_by="projection",
            similarity=0.9, source_collection=src,
            assigned_at="2026-04-14T00:00:00",
        )
    db.taxonomy.clear_icf_cache()


class TestICF:
    """RDR-077 Phase 3 — ``compute_icf_map`` SC-3 + SC-8."""

    def test_icf_log2_base(self, db: T2Database, t2_service_env: str) -> None:
        """N=4, DF=2 → ICF = log2(4/2) = 1.0 exactly."""
        import math

        B = _unique_topic_base()
        # Topic B+1 appears in 2 of 4 collections; B+2..B+4 each in 1.
        _seed_projection_rows(db, [
            ("docA", B + 1, "code__c1"),
            ("docB", B + 1, "code__c2"),
            ("docC", B + 2, "code__c1"),
            ("docD", B + 3, "code__c3"),
            ("docE", B + 4, "code__c4"),
        ], t2_service_env)
        icf = db.taxonomy.compute_icf_map()
        # N_effective = 4 distinct source_collections.
        assert icf[B + 1] == pytest.approx(math.log2(4 / 2))
        assert icf[B + 2] == pytest.approx(math.log2(4 / 1))
        assert icf[B + 3] == pytest.approx(math.log2(4 / 1))
        assert icf[B + 4] == pytest.approx(math.log2(4 / 1))

    def test_icf_df_equals_n_yields_zero(self, db: T2Database, t2_service_env: str) -> None:
        """Ubiquitous topic (appears in every collection) → ICF = 0."""
        B = _unique_topic_base()
        _seed_projection_rows(db, [
            ("docA", B + 1, "code__c1"),
            ("docB", B + 1, "code__c2"),
            ("docC", B + 1, "code__c3"),
        ], t2_service_env)
        icf = db.taxonomy.compute_icf_map()
        assert icf[B + 1] == pytest.approx(0.0)

    def test_icf_n_effective_excludes_null_source(self, db: T2Database, t2_service_env: str) -> None:
        """SUPERSEDED (RDR-194 D1/P3b, nexus-11pe7): source_collection is NOT
        NULL as of taxonomy-010-1 -- a "legacy NULL row" can no longer be
        constructed at all, so this no longer tests ICF's exclusion logic;
        it tests that the write itself is refused. compute_icf_map's own
        NULL-exclusion predicate is now structurally dead code (every row is
        NOT NULL by construction), retirement of which is out of scope here
        (D0.10 census-leg-retirement discipline: it retires with whatever
        follow-up phase removes the now-unreachable branch, not silently
        alongside an unrelated bead).
        """
        import httpx

        B = _unique_topic_base()
        _seed_projection_rows(db, [
            ("docA", B + 1, "code__c1"),
            ("docB", B + 1, "code__c2"),
            ("docC", B + 2, "code__c1"),
        ], t2_service_env)
        # A NULL source_collection write must now fail loud, not silently
        # land and get excluded downstream.
        _insert_topic(db, topic_id=B + 99, collection="code__any", label="legacy")
        with pytest.raises(httpx.HTTPStatusError):
            db.taxonomy.import_assignment(
                doc_id=canonical_chunk_id("docLegacy"), topic_id=B + 99, assigned_by="projection",
                similarity=None, assigned_at=None, source_collection=None,
            )
        db.taxonomy.clear_icf_cache()

        icf = db.taxonomy.compute_icf_map()
        # The refused row never landed: N_effective stays 2 (c1, c2); the
        # legacy topic is absent from the map.
        assert B + 99 not in icf
        # Topic B+1 in both collections → ICF = 0.
        assert icf[B + 1] == pytest.approx(0.0)

    def test_icf_disabled_when_n_lt_2(self, db: T2Database, t2_service_env: str) -> None:
        """Single-collection corpus → empty map (ICF undefined)."""
        B = _unique_topic_base()
        _seed_projection_rows(db, [
            ("docA", B + 1, "code__only"),
            ("docB", B + 2, "code__only"),
        ], t2_service_env)
        icf = db.taxonomy.compute_icf_map()
        assert icf == {}

    def test_icf_disabled_when_no_projection_rows(self, db: T2Database) -> None:
        """Empty taxonomy → empty map, no SQL error."""
        icf = db.taxonomy.compute_icf_map()
        assert icf == {}


# ── Phase 4a (nexus-jt1) — ICF-weighted projection + CLI defaults ───────────


class TestDefaultProjectionThreshold:
    """RDR-077 Phase 4a: per-corpus-type threshold defaults."""

    def test_default_threshold_code_prefix(self) -> None:
        from nexus.corpus import default_projection_threshold
        assert default_projection_threshold("code__foo") == 0.70

    def test_default_threshold_knowledge_prefix(self) -> None:
        from nexus.corpus import default_projection_threshold
        assert default_projection_threshold("knowledge__bar") == 0.50

    def test_default_threshold_docs_and_rdr(self) -> None:
        from nexus.corpus import default_projection_threshold
        assert default_projection_threshold("docs__mix") == 0.55
        assert default_projection_threshold("rdr__alpha") == 0.55

    def test_default_threshold_unknown_prefix_fallback(self) -> None:
        from nexus.corpus import default_projection_threshold
        # Unknown prefix → safe under-match bias at 0.70.
        assert default_projection_threshold("other__weird") == 0.70


@pytest.fixture()
def fixture_icf_ranking(
    db: T2Database, chroma_client: Any, t2_service_env: str,
) -> T2Database:
    """≥10 collections — calibration spread for ICF ranking tests (SC-3, S-3).

    Each collection gets one ubiquitous "hub" topic + one distinct domain
    topic. After seeding every collection's projection rows, the hub's
    DF reaches N_effective while each domain topic's DF is 1.

    Deterministic: fixed seeds, doc ids, assigned_at values.
    """
    rng = np.random.default_rng(77)
    n_collections = 12  # exceeds the S-3 ≥10 threshold
    for idx in range(n_collections):
        col = f"code__icfR{idx:02d}"
        # 30 docs per collection — enough for HDBSCAN to form at least
        # one topic per well-separated cluster.
        embs = rng.standard_normal((30, 384)).astype(np.float32) * 0.1
        embs[:15, 0] += 3.0
        embs[15:, 1] += 3.0
        doc_ids = [canonical_chunk_id(f"{col}-d{i}") for i in range(30)]
        texts = [f"text {col} {i}" for i in range(30)]
        # RDR-194 P3d: discover_topics persists the discovered CLUSTER as
        # topic_assignments rows too — each doc_id needs a matching
        # nexus.chunks row for topic_assignments_chunk_fk. The later
        # persist_assignments (below) reassigns the SAME doc_ids into
        # other collections' topics with source_collection=col, which
        # this same seed already backs.
        _seed_chunks_for_tenant(t2_service_env, col, doc_ids)
        db.taxonomy.discover_topics(col, doc_ids, embs, texts, chroma_client)
        src_coll = chroma_client.get_or_create_collection(
            col, embedding_function=None,
        )
        src_coll.add(ids=doc_ids, embeddings=embs.tolist(), documents=texts)

    # Cross-project every source against every other so N_effective ≈
    # n_collections for any topic that matches broadly. Persist through the
    # BATCHED public surface (persist_assignments — same projection-upsert
    # semantics on both substrates): the previous per-row assign_topic loop
    # issued ~1.4k sequential HTTP POSTs on the engine substrate, tripping
    # the client read timeout and wedging the session engine for every
    # later test in the file (RDR-155 P4b P0a' hang mechanism).
    collections = [f"code__icfR{i:02d}" for i in range(n_collections)]
    pending: list[dict] = []
    for src in collections:
        targets = [c for c in collections if c != src]
        result = db.taxonomy.project_against(
            src, targets, chroma_client, threshold=-1.0, top_k=2,
        )
        pending.extend(
            {
                "doc_id": doc_id,
                "topic_id": topic_id,
                "assigned_by": "projection",
                "similarity": similarity,
                "source_collection": src,
            }
            for doc_id, topic_id, similarity
            in result.get("chunk_assignments", [])
        )
    db.taxonomy.persist_assignments(pending)
    db.taxonomy.clear_icf_cache()
    return db


class TestIcfRankingFixture:
    """S-3: ≥10-collection fixture exercises the SC-3 calibration spread."""

    def test_fixture_produces_large_n_effective(
        self, fixture_icf_ranking: T2Database,
    ) -> None:
        icf = fixture_icf_ranking.taxonomy.compute_icf_map()
        # ICF is a non-empty map; at least one topic is broadly
        # shared and one is narrowly scoped, so the spread is real.
        assert icf, "fixture must produce a usable ICF map"
        values = sorted(icf.values())
        assert values[0] <= values[-1]  # spread is measurable
        # N_effective should reach the fixture's collection count
        # (N=12 collections project into each other). Read through the
        # public projection-counts surface (substrate-blind) instead of a
        # raw COUNT(DISTINCT source_collection).
        counts = (
            fixture_icf_ranking.taxonomy.get_projection_counts_by_collection()
        )
        assert len(counts) == 12

    def test_icf_weighted_ranking_differs_from_raw(
        self, fixture_icf_ranking: T2Database, chroma_client: Any,
    ) -> None:
        """SC-3 calibration spread: icf_map reorders the top-K topics for
        a source vs. the unweighted baseline. Uses a generous threshold so
        both paths return many matches for comparison."""
        src = "code__icfR00"
        targets = [f"code__icfR{i:02d}" for i in range(1, 12)]

        baseline = fixture_icf_ranking.taxonomy.project_against(
            src, targets, chroma_client, threshold=-1.0, top_k=3,
        )
        baseline_order = [m["topic_id"] for m in baseline["matched_topics"]]

        icf_map = fixture_icf_ranking.taxonomy.compute_icf_map()
        weighted = fixture_icf_ranking.taxonomy.project_against(
            src, targets, chroma_client, threshold=-1.0, top_k=3,
            icf_map=icf_map,
        )
        weighted_order = [m["topic_id"] for m in weighted["matched_topics"]]

        # If every topic has ICF ≈ constant the orders can match; what we
        # care about is that the weighted run respects the icf_map input,
        # producing at least one assignment whose chosen topic has ICF > 0
        # (i.e., ICF weighting didn't collapse everything to zero).
        assert baseline_order and weighted_order
        kept_any_nonzero_icf = any(
            icf_map.get(tid, 1.0) > 0.0 for tid in weighted_order
        )
        assert kept_any_nonzero_icf


class TestProjectAgainstIcf:
    """``project_against(icf_map=...)`` — weighting at filter time only."""

    def _seed_two_corpora(
        self, db: T2Database, chroma_client: Any, tenant: str,
    ) -> None:
        rng = np.random.default_rng(42)
        for name in ("code__icfA", "code__icfB"):
            embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
            embs[:30, 0] += 3.0
            embs[30:, 1] += 3.0
            doc_ids = [canonical_chunk_id(f"{name}-d{i}") for i in range(60)]
            texts = [f"text {name} {i}" for i in range(60)]
            # RDR-194 P3d: discover_topics persists the discovered CLUSTER
            # as topic_assignments rows too — each doc_id needs a matching
            # nexus.chunks row for topic_assignments_chunk_fk.
            _seed_chunks_for_tenant(tenant, name, doc_ids)
            db.taxonomy.discover_topics(
                name, doc_ids, embs, texts, chroma_client,
            )
            src_coll = chroma_client.get_or_create_collection(
                name, embedding_function=None,
            )
            src_coll.add(
                ids=doc_ids,
                embeddings=embs.tolist(),
                documents=texts,
            )

    def test_icf_suppresses_hub_topics_below_threshold(
        self, db: T2Database, chroma_client: Any, t2_service_env: str,
    ) -> None:
        """A topic with ICF=0 must fail threshold regardless of raw cosine."""
        self._seed_two_corpora(db, chroma_client, t2_service_env)
        # Without ICF: at low threshold we get matches.
        baseline = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client, threshold=0.1,
        )
        assert baseline["chunk_assignments"], "baseline should match"

        # Craft an ICF map that zeros out every target topic — equivalent
        # to every topic being ubiquitous. Filter drops everything.
        zero_icf = {
            m["topic_id"]: 0.0 for m in baseline["matched_topics"]
        }
        result = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client,
            threshold=0.1, icf_map=zero_icf,
        )
        assert not result["chunk_assignments"], (
            "zero-ICF topics must be filtered out before persistence"
        )
        assert len(result["novel_chunks"]) == result["total_chunks"]

    def test_stored_similarity_is_raw_cosine_even_with_icf(
        self, db: T2Database, chroma_client: Any, t2_service_env: str,
    ) -> None:
        """Raw cosine stored; ICF only affects what gets through the filter."""
        self._seed_two_corpora(db, chroma_client, t2_service_env)
        baseline = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client, threshold=0.1,
        )
        raw_lookup = {(d, t): s for d, t, s in baseline["chunk_assignments"]}

        # High-ICF map (2.0 everywhere) — doubles the adjusted score but the
        # raw cosine returned in chunk_assignments must be unchanged.
        high_icf = {m["topic_id"]: 2.0 for m in baseline["matched_topics"]}
        weighted = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client,
            threshold=0.1, icf_map=high_icf,
        )
        for d, t, s in weighted["chunk_assignments"]:
            if (d, t) in raw_lookup:
                assert s == pytest.approx(raw_lookup[(d, t)]), (
                    "icf_map must not mutate stored raw cosine"
                )

    def test_missing_topic_in_icf_map_defaults_to_one(
        self, db: T2Database, chroma_client: Any, t2_service_env: str,
    ) -> None:
        """ICF map lookup missing entries → weight 1.0 (no suppression)."""
        self._seed_two_corpora(db, chroma_client, t2_service_env)
        # Empty ICF map — every target topic defaults to 1.0, result matches
        # the baseline no-icf case.
        baseline = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client, threshold=0.1,
        )
        with_empty_icf = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client,
            threshold=0.1, icf_map={},
        )
        assert (
            len(baseline["chunk_assignments"])
            == len(with_empty_icf["chunk_assignments"])
        )


class TestProjectCmdFlag:
    """CLI flag wiring for ``nx taxonomy project --use-icf``."""

    def test_project_cmd_has_use_icf_flag(self) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["taxonomy", "project", "--help"])
        assert result.exit_code == 0
        assert "--use-icf" in result.output
        assert "ICF" in result.output

    def test_project_cmd_help_mentions_corpus_defaults(self) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["taxonomy", "project", "--help"])
        assert "code__*" in result.output or "0.70" in result.output
        assert "knowledge__*" in result.output or "0.50" in result.output
        # Reference to tuning doc for operators. Click wraps at hyphens,
        # inserting "- " breaks, so normalise before the containment check.
        collapsed = " ".join(result.output.split()).replace("- ", "-")
        assert "exploration/taxonomy-projection-tuning.md" in collapsed


# ── Phase 5 (nexus-84v) — nx taxonomy hubs ──────────────────────────────────


def _seed_projection_assignments(db: T2Database, rows: list[dict], tenant: str) -> None:
    """Seed explicit-``assigned_at`` projection assignments on either
    substrate.

    SQLite keeps the per-row ``assign_topic`` path (the only public writer
    that accepts ``assigned_at``). The engine leg routes through the
    fidelity-preserving ``import_rows_batch`` — one POST instead of
    hundreds of sequential per-row round-trips (the RDR-155 P4b P0a'
    read-timeout / engine-wedge mechanism).

    RDR-194 P3d: also seeds a matching nexus.chunks row per
    (source_collection, doc_id) — topic_assignments_chunk_fk requires it
    before the batch INSERT below.
    """
    by_collection: dict[str, list[str]] = {}
    for r in rows:
        by_collection.setdefault(r["source_collection"], []).append(r["doc_id"])
    for src, chashes in by_collection.items():
        _seed_chunks_for_tenant(tenant, src, chashes)
    db.taxonomy.import_rows_batch(
        "assignment",
        [
            {
                **r,
                "assigned_by": "projection",
                "assigned_at": _full_iso(r["assigned_at"]),
            }
            for r in rows
        ],
    )


@pytest.fixture()
def fixture_hub_synthetic(db: T2Database, t2_service_env: str) -> tuple[T2Database, int]:
    """5 collections × 100 docs — half assigned to a stopword-labeled hub,
    half spread across 5 distinct domain topics (one per collection).

    Deterministic: fixed doc_ids and assigned_at values so tests don't
    depend on wall-clock drift. Yields ``(db, B)`` where ``B`` is the
    session-unique topic-id base — hub topic is ``B+1``, domain topics
    ``B+2..B+6`` (see ``_unique_topic_base``).
    """
    B = _unique_topic_base()
    topics = [
        (B + 1, "assert helpers",            "code__c0", "2026-04-01"),
        (B + 2, "ingest-pipeline",           "code__c0", "2026-04-01"),
        (B + 3, "member-proposal-workflow",  "code__c1", "2026-04-01"),
        (B + 4, "payroll-audit",             "code__c2", "2026-04-01"),
        (B + 5, "ballot-scanner",            "code__c3", "2026-04-01"),
        (B + 6, "treasury-reconciliation",   "code__c4", "2026-04-01"),
    ]
    for tid, label, collection, created in topics:
        _insert_topic(
            db, topic_id=tid, collection=collection,
            label=label, created_at=created,
        )

    rows: list[dict] = []
    # Hub topic B+1: 50 docs from every one of 5 collections.
    for col_idx in range(5):
        col = f"code__c{col_idx}"
        rows.extend(
            {
                "doc_id": canonical_chunk_id(f"{col}-hub-d{d}"),
                "topic_id": B + 1,
                "similarity": 0.85,
                "source_collection": col,
                "assigned_at": f"2026-04-10T12:{col_idx:02d}:00",
            }
            for d in range(50)
        )
    # Five domain topics: B+2..B+6 each gets 50 docs from one collection.
    for col_idx, tid in enumerate((B + 2, B + 3, B + 4, B + 5, B + 6)):
        col = f"code__c{col_idx}"
        rows.extend(
            {
                "doc_id": canonical_chunk_id(f"{col}-dom-d{d}"),
                "topic_id": tid,
                "similarity": 0.82,
                "source_collection": col,
                "assigned_at": f"2026-04-10T13:{col_idx:02d}:00",
            }
            for d in range(50)
        )
    _seed_projection_assignments(db, rows, t2_service_env)
    db.taxonomy.clear_icf_cache()
    return db, B


class TestHubs:
    def test_hubs_detects_stopword_topic(
        self, fixture_hub_synthetic: tuple[T2Database, int],
    ) -> None:
        db, B = fixture_hub_synthetic
        hubs = db.taxonomy.detect_hubs(min_collections=2)
        topic_ids = [h.topic_id for h in hubs]
        # Only the hub topic (B+1) spans all 5 collections.
        assert B + 1 in topic_ids
        assert hubs[0].topic_id == B + 1  # sorted by score desc
        assert "assert" in hubs[0].matched_stopwords

    def test_hubs_excludes_single_collection_domain_topics(
        self, fixture_hub_synthetic: tuple[T2Database, int],
    ) -> None:
        db, B = fixture_hub_synthetic
        hubs = db.taxonomy.detect_hubs(min_collections=2)
        topic_ids = {h.topic_id for h in hubs}
        # Domain topics each live in a single source collection → DF=1,
        # excluded by min_collections=2.
        for domain_topic in (B + 2, B + 3, B + 4, B + 5, B + 6):
            assert domain_topic not in topic_ids

    def test_hubs_max_icf_threshold(
        self, fixture_hub_synthetic: tuple[T2Database, int],
    ) -> None:
        db, B = fixture_hub_synthetic
        # With N_effective=5 and the hub topic at DF=5, ICF=log2(1)=0.
        hubs = db.taxonomy.detect_hubs(
            min_collections=2, max_icf=0.5,
        )
        assert [h.topic_id for h in hubs] == [B + 1]

        # No ICF filter ever, but also no label stopword filter — so every
        # DF≥2 topic shows up. In this fixture only the hub topic has DF≥2.
        hubs_none = db.taxonomy.detect_hubs(
            min_collections=2, max_icf=None,
        )
        assert [h.topic_id for h in hubs_none] == [B + 1]

    def test_hubs_min_collections_threshold(
        self, fixture_hub_synthetic: tuple[T2Database, int],
    ) -> None:
        db, _B = fixture_hub_synthetic
        # Asking for DF>=6 → nothing (we only have 5 collections).
        hubs = db.taxonomy.detect_hubs(min_collections=6)
        assert hubs == []


    def test_hubs_warn_stale_without_flag_leaves_fields_default(
        self, fixture_hub_synthetic: tuple[T2Database, int],
    ) -> None:
        db, _B = fixture_hub_synthetic
        hubs = db.taxonomy.detect_hubs(min_collections=2)
        assert hubs[0].max_last_discover_at is None
        assert hubs[0].never_discovered_count == 0
        assert hubs[0].is_stale is False

    def test_hubs_cli_flag_wiring(self) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        result = CliRunner().invoke(main, ["taxonomy", "hubs", "--help"])
        assert result.exit_code == 0
        for flag in ("--min-collections", "--max-icf", "--warn-stale", "--explain"):
            assert flag in result.output
        # Points the operator at the tuning doc.
        collapsed = " ".join(result.output.split()).replace("- ", "-")
        assert "exploration/taxonomy-projection-tuning.md" in collapsed


# ── Phase 6 (nexus-w4k) — nx taxonomy audit ─────────────────────────────────


class TestAudit:
    @pytest.fixture()
    def audit_db(self, db: T2Database, t2_service_env: str) -> tuple[T2Database, int]:
        """Seed a collection with a known similarity distribution.

        Yields ``(db, B)``: hub topic is ``B+1``, others ``B+2..B+4``
        (see ``_unique_topic_base``).
        """
        B = _unique_topic_base()
        topics = [
            (B + 1, "assert helpers",   "code__auditC0", "2026-04-01"),
            (B + 2, "ingest-pipeline",  "code__auditC0", "2026-04-01"),
            (B + 3, "payroll-audit",    "code__auditC0", "2026-04-01"),
            # Cross-collection participant so ICF has N_effective >= 2.
            (B + 4, "ballot-scanner",   "code__peer",    "2026-04-01"),
        ]
        for tid, label, coll, created in topics:
            _insert_topic(
                db, topic_id=tid, collection=coll,
                label=label, created_at=created,
            )

        # RDR-194 P3d: topic_assignments_chunk_fk requires a matching
        # nexus.chunks row for every (source_collection, doc_id) below.
        _seed_chunks_for_tenant(
            t2_service_env, "code__peer", [canonical_chunk_id("peer-doc-1")],
        )
        _seed_chunks_for_tenant(
            t2_service_env, "code__auditC0",
            [canonical_chunk_id(f"auditC0-d{i}") for i in range(11)]
            + [canonical_chunk_id("hdbscan-doc")],
        )

        db.taxonomy.assign_topic(
            canonical_chunk_id("peer-doc-1"), B + 4,
            assigned_by="projection", similarity=0.80,
            source_collection="code__peer",
            assigned_at="2026-04-05T00:00:00",
        )

        # code__auditC0 projects 11 rows; 6 → B+1 (hub), 3 → B+2, 2 → B+3.
        sims = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.98]
        topic_per_row = [1, 1, 1, 1, 1, 1, 2, 2, 2, 3, 3]
        for i, (sim, tid) in enumerate(zip(sims, topic_per_row)):
            db.taxonomy.assign_topic(
                canonical_chunk_id(f"auditC0-d{i}"), B + tid,
                assigned_by="projection", similarity=sim,
                source_collection="code__auditC0",
                assigned_at=f"2026-04-10T12:{i:02d}:00",
            )
        # HDBSCAN row that must NOT be counted.
        db.taxonomy.assign_topic(
            canonical_chunk_id("hdbscan-doc"), B + 1, assigned_by="hdbscan",
            source_collection="code__auditC0")
        db.taxonomy.clear_icf_cache()
        return db, B

    def test_audit_quantiles(self, audit_db: tuple[T2Database, int]) -> None:
        db, _B = audit_db
        report = db.taxonomy.audit_collection("code__auditC0")
        # Sorted 0.10..0.98, 11 rows → nearest-rank p10=0.20, p50=0.60, p90=0.95.
        assert report.total_assignments == 11
        assert report.p10 == pytest.approx(0.20)
        assert report.p50 == pytest.approx(0.60)
        assert report.p90 == pytest.approx(0.95)

    def test_audit_below_threshold_count(self, audit_db: tuple[T2Database, int]) -> None:
        db, _B = audit_db
        report = db.taxonomy.audit_collection(
            "code__auditC0", threshold=0.50,
        )
        assert report.threshold == 0.50
        # Rows below 0.50: 0.10 / 0.20 / 0.30 / 0.40 = 4.
        assert report.below_threshold_count == 4

    def test_audit_uses_per_corpus_default_threshold(
        self, audit_db: tuple[T2Database, int],
    ) -> None:
        db, _B = audit_db
        report = db.taxonomy.audit_collection("code__auditC0")
        # code__* default → 0.70; 6 rows below (0.10..0.60).
        assert report.threshold == 0.70
        assert report.below_threshold_count == 6

    def test_audit_top_receiving_hubs(self, audit_db: tuple[T2Database, int]) -> None:
        db, B = audit_db
        report = db.taxonomy.audit_collection(
            "code__auditC0", top_n=5,
        )
        ids = [h.topic_id for h in report.top_receiving_hubs]
        assert ids == [B + 1, B + 2, B + 3]

    def test_audit_pattern_pollution_flags(self, audit_db: tuple[T2Database, int]) -> None:
        db, B = audit_db
        report = db.taxonomy.audit_collection("code__auditC0")
        polluted_ids = [h.topic_id for h in report.pattern_pollution]
        assert B + 1 in polluted_ids  # "assert helpers" matches 'assert'
        assert B + 2 not in polluted_ids
        assert B + 3 not in polluted_ids

    def test_audit_excludes_hdbscan_rows(self, audit_db: tuple[T2Database, int]) -> None:
        db, _B = audit_db
        report = db.taxonomy.audit_collection("code__auditC0")
        # 11 projection rows only — the hdbscan row is ignored.
        assert report.total_assignments == 11

    def test_audit_handles_empty_projection(self, db: T2Database) -> None:
        report = db.taxonomy.audit_collection("code__none")
        assert report.total_assignments == 0
        assert report.p10 is None
        assert report.p50 is None
        assert report.p90 is None
        assert report.below_threshold_count == 0
        assert report.top_receiving_hubs == []
        assert report.pattern_pollution == []

    def test_audit_cli_flag_wiring(self) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        result = CliRunner().invoke(main, ["taxonomy", "audit", "--help"])
        assert result.exit_code == 0
        assert "--collection" in result.output
        assert "--threshold" in result.output
        assert "--top-n" in result.output
        collapsed = " ".join(result.output.split()).replace("- ", "-")
        assert "exploration/taxonomy-projection-tuning.md" in collapsed
