# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-lub regression — `nx collection delete` must cascade-purge
all taxonomy state tied to the deleted collection.

Four tables carry per-collection rows:
  * ``topics`` (keyed by ``collection``)
  * ``topic_assignments`` (via topic_id FK, plus ``source_collection``)
  * ``topic_links`` (via from/to topic_id FK)
  * ``taxonomy_meta`` (keyed by ``collection``)

Pre-fix behavior: `nx collection delete` removed the Chroma collection
but left all four orphaned — `nx taxonomy status` continued to list the
deleted collection with its pre-delete topic count; hub detection
traversed orphan edges inflating ICF denominators.

Post-fix contract: `CatalogTaxonomy.purge_collection(name)` removes
every row tied to *name* transactionally, returns a count dict so the
CLI can report what was cleaned.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from nexus.db.http_vector_client import HttpVectorClient


def _chash(seed: str) -> str:
    """Deterministic full-width chunk hash (RDR-180: 64 lowercase hex).

    The engine enforces the full-sha256 width contract on every chash-shaped
    field; the SQLite twin accepted any string. Derive per-test values from a
    stable seed — never random."""
    return hashlib.sha256(seed.encode()).hexdigest()


# Assignment doc_ids are chunk chashes on the wire (see
# HttpTaxonomyStore.import_assignment) — full-width on both substrates.
D_DOOMED1 = _chash("doomed:doc1:0")
D_DOOMED2 = _chash("doomed:doc2:0")
D_KEEP1 = _chash("keepme:doc1:0")
D_KEEP2 = _chash("keepme:doc2:0")


def _src_ids(tmp_path: Path, n: int) -> list[int]:
    """Deterministic per-test topic ids for the fidelity-import path.

    ``import_topic`` PRESERVES the given id, and the topics PK is global
    across tenants on the engine — fixed literals (1, 2, 3) collide across
    tests sharing the session PG. Derive from the per-test tmp_path."""
    base = int.from_bytes(hashlib.sha256(str(tmp_path).encode()).digest()[:6], "big")
    return [base + i for i in range(1, n + 1)]


def _seed_topic(tax, *, src_id: int, label: str, collection: str,
                centroid_hash: str, doc_count: int) -> int:
    """Seed one topics row on either substrate: raw SQLite INSERT on the
    legacy backend, the fidelity-import surface on the engine (the settled
    seeding idiom — cf. tests/db/test_telemetry_retention_marker.py)."""
    return tax.import_topic(
        src_id=src_id, label=label, parent_id=None, collection=collection,
        centroid_hash=centroid_hash, doc_count=doc_count,
        created_at="2026-04-16T00:00:00Z", review_status="pending", terms="[]",
    )


def _seed_chunk(tenant: str, collection: str, chash_hex: str, *, dim: int = 384) -> None:
    """RDR-194 P3d (nexus-tk070.p3d): seed a real nexus.chunks row so a
    topic_assignments insert for (tenant, collection, chash) satisfies
    the new topic_assignments_chunk_fk composite FK. Mirrors
    tests/test_taxonomy.py's ``_seed_chunks_for_tenant`` (this module has
    no import path to it, so a lean single-row copy lives here instead).
    """
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — laziness contract, see module docstring

    state = ensure_engine()
    embed_col = {384: "embedding_384", 768: "embedding_768", 1024: "embedding_1024"}[dim]
    vec = "[" + ",".join(["0"] * dim) + "]"
    sql = (
        f"INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('{tenant}', '{collection}') "
        "ON CONFLICT DO NOTHING; "
        f"INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, {embed_col}) "
        f"VALUES ('{tenant}', '{collection}', decode('{chash_hex}', 'hex'), 'seed', '{vec}'::vector) "
        "ON CONFLICT DO NOTHING;"
    )
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
            "-U", state["pg_user"], "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"_seed_chunk failed: {proc.stdout}\n{proc.stderr}"


def _seed_assignment(tax, *, doc_id: str, topic_id: int, assigned_by: str,
                     source_collection: str, tenant: str) -> None:
    # RDR-194 P3d: topic_assignments_chunk_fk requires a matching
    # nexus.chunks row for (tenant, source_collection, doc_id) before
    # the assignment insert.
    _seed_chunk(tenant, source_collection, doc_id)
    tax.import_assignment(
        doc_id=doc_id, topic_id=topic_id, assigned_by=assigned_by,
        similarity=None, assigned_at=None, source_collection=source_collection,
    )


def _seed_link(tax, *, from_topic_id: int, to_topic_id: int, link_count: int) -> None:
    tax.import_topic_link(
        from_topic_id=from_topic_id, to_topic_id=to_topic_id,
        link_count=link_count, link_types="[]",
    )


def _seed_meta(tax, *, collection: str, doc_count: int = 10) -> None:
    tax.import_taxonomy_meta(
        collection=collection, last_discover_doc_count=doc_count,
        last_discover_at="2026-04-14T12:00:00Z",
    )


def _meta_row_present(tax, collection: str, *, doc_count: int = 10) -> bool:
    """Backend-blind presence probe for a taxonomy_meta row seeded with
    ``last_discover_doc_count=doc_count``: ``needs_rebalance`` returns True
    when the row is ABSENT on both substrates (SQLite: no-prior-discovery;
    engine: 404), and False for a present row probed at its own count."""
    return not tax.needs_rebalance(collection, doc_count)


@pytest.fixture
def seeded_taxonomy(tmp_path: Path, t2_service_env: str):
    """Open a real T2Database on disk and seed two collections with
    topics, assignments, and cross-collection links so the cascade
    path is exercised, not mocked.

    RDR-194 P3d (nexus-tk070.p3d): now depends on ``t2_service_env``
    explicitly (not just the autouse ``_pin_t2_substrate`` pin) to get
    the minted tenant NAME, which the new topic_assignments_chunk_fk
    seeding (``_seed_chunk``) needs to target the same tenant this
    fixture's own ``T2Database``/HTTP calls resolve via their bearer
    token.
    """
    from nexus.db.t2 import T2Database

    tenant = t2_service_env
    db_path = tmp_path / "memory.db"
    db = T2Database(db_path)
    tax = db.taxonomy

    sid1, sid2, sid3 = _src_ids(tmp_path, 3)

    # --- Seed collection A (to be deleted) ---
    t_a1 = _seed_topic(tax, src_id=sid1, label="A-Topic-1", collection="docs__doomed",
                       centroid_hash=_chash("h1"), doc_count=5)
    t_a2 = _seed_topic(tax, src_id=sid2, label="A-Topic-2", collection="docs__doomed",
                       centroid_hash=_chash("h2"), doc_count=3)

    # --- Seed collection B (must survive) ---
    t_b1 = _seed_topic(tax, src_id=sid3, label="B-Topic-1", collection="docs__keepme",
                       centroid_hash=_chash("h3"), doc_count=8)

    # --- Assignments: mix source_collection and topic_id ownership ---
    # Native A assignment (doc in A, topic in A)
    _seed_assignment(tax, doc_id=D_DOOMED1, topic_id=t_a1,
                     assigned_by="hdbscan", source_collection="docs__doomed",
                     tenant=tenant)
    # Projection of doomed chunks into B's topic
    _seed_assignment(tax, doc_id=D_DOOMED2, topic_id=t_b1,
                     assigned_by="projection", source_collection="docs__doomed",
                     tenant=tenant)
    # Projection of B chunks into A's topic (must also be purged — doomed
    # topic_id → NULL FK residue left behind otherwise)
    _seed_assignment(tax, doc_id=D_KEEP1, topic_id=t_a2,
                     assigned_by="projection", source_collection="docs__keepme",
                     tenant=tenant)
    # Native B assignment — must survive
    _seed_assignment(tax, doc_id=D_KEEP2, topic_id=t_b1,
                     assigned_by="hdbscan", source_collection="docs__keepme",
                     tenant=tenant)

    # --- topic_links: A→B, B→A, A→A ---
    _seed_link(tax, from_topic_id=t_a1, to_topic_id=t_b1, link_count=2)
    _seed_link(tax, from_topic_id=t_b1, to_topic_id=t_a1, link_count=1)
    _seed_link(tax, from_topic_id=t_a1, to_topic_id=t_a2, link_count=3)  # A→A, both doomed

    # --- taxonomy_meta ---
    _seed_meta(tax, collection="docs__doomed")
    _seed_meta(tax, collection="docs__keepme")

    yield db, tax, {"t_a1": t_a1, "t_a2": t_a2, "t_b1": t_b1}
    db.close()


class TestPurgeCollection:
    """Unit tests for the new purge_collection method."""

    def test_purge_removes_topics_for_collection(self, seeded_taxonomy):
        db, tax, _ids = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")
        assert counts["topics"] == 2

        assert tax.get_topics_for_collection("docs__doomed") == []

        # Survivor untouched
        assert len(tax.get_topics_for_collection("docs__keepme")) == 1

    def test_purge_removes_assignments_by_topic_and_source(self, seeded_taxonomy):
        db, tax, ids = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")

        # Seeded 4 assignments; 3 reference doomed (native + 2 projections).
        # Only the native-B assignment (topic_id=B, source=B) should survive.
        assert counts["assignments"] == 3
        remaining = tax.get_assignments_for_docs(
            [D_DOOMED1, D_DOOMED2, D_KEEP1, D_KEEP2]
        )
        assert remaining == {D_KEEP2: ids["t_b1"]}

        # No projection row keyed to the doomed source_collection survives
        assert "docs__doomed" not in tax.get_projection_counts_by_collection()

    def test_purge_removes_links_touching_doomed_topics(self, seeded_taxonomy):
        db, tax, ids = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")

        # 3 seeded links; all 3 touch a doomed topic (A→B, B→A, A→A).
        assert counts["links"] == 3
        # Empty-shape tolerance: the SQLite store returns {} and the Http
        # store [] for the no-links case; both are falsy.
        assert not tax.get_topic_link_pairs(
            [ids["t_a1"], ids["t_a2"], ids["t_b1"]]
        )

    def test_purge_removes_taxonomy_meta_row(self, seeded_taxonomy):
        db, tax, _ids = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")

        assert counts["meta"] == 1
        assert not _meta_row_present(tax, "docs__doomed")
        # Survivor meta row untouched
        assert _meta_row_present(tax, "docs__keepme")


    def test_purge_unknown_collection_returns_zero_counts(self, seeded_taxonomy):
        """Purging a collection with no rows is a silent no-op."""
        db, tax, _ids = seeded_taxonomy
        counts = tax.purge_collection("docs__never-existed")
        assert counts == {"topics": 0, "assignments": 0, "links": 0, "meta": 0}


# ── nexus-8a8e — pdf_pipeline cascade ────────────────────────────────────────


class TestPipelineDeleteCascade:
    """nexus-8a8e: `nx collection delete` must purge pipeline_buffer rows
    keyed to the deleted collection. Without the cascade, a subsequent
    ``nx index pdf`` returns "skip" at ``create_pipeline`` because the old
    row's ``status='completed'`` is still present — surfaces as "0 chunks"
    with no extraction message and forces users to reach for ``--force``.
    """

    def test_cli_delete_cascades_pipeline_rows(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch

        from nexus.commands.collection import delete_cmd
        from nexus.db.t2 import T2Database
        from tests.pipeline_fake_engine import make_fake_engine_db

        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass  # schema initialized

        pdb, engine = make_fake_engine_db()
        # Two rows targeting the doomed collection; one that must survive.
        pdb.create_pipeline("hA", "/a.pdf", "knowledge__delos")
        pdb.write_page("hA", 0, "a")
        pdb.create_pipeline("hB", "/b.pdf", "knowledge__delos")
        pdb.write_chunk("hB", 0, "b", "cid-b")
        pdb.create_pipeline("hC", "/c.pdf", "docs__keep")
        pdb.flush_all()

        # make_t3()/_t3() return the service-backed HttpVectorClient
        # unconditionally in production since RDR-155 P4a.2 -- cloud
        # creds / is_local_mode() no longer affect the handle type.
        # delete_collection is a direct call on both handles.
        fake_t3 = MagicMock(spec=HttpVectorClient)
        fake_t3.delete_collection = MagicMock()

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake_t3), \
             patch("nexus.mcp_infra.default_db_path", return_value=db_path), \
             patch(
                 "nexus.commands._helpers.default_db_path",
                 return_value=db_path,
             ), \
             patch("nexus.db.http_pipeline_client.HttpPipelineDB", return_value=pdb):
            result = runner.invoke(delete_cmd, ["knowledge__delos", "--yes"])

        assert result.exit_code == 0, result.output

        # Verify against the fake engine's state directly (the purge closed
        # the shared client on context exit).
        assert "hA" not in engine.pipelines
        assert "hB" not in engine.pipelines
        assert not any(h == "hA" for (h, _) in engine.pages)
        assert not any(h == "hB" for (h, _) in engine.chunks)
        # Survivor row untouched.
        assert "hC" in engine.pipelines

        # Output must include the pipeline-rows count so operators can
        # see the cascade worked without re-running with `--force`.
        assert "pipeline" in result.output.lower() or "2" in result.output, (
            f"Expected pipeline cleanup count in delete output. "
            f"Got: {result.output!r}"
        )

    def test_cli_delete_cascades_pipeline_when_t3_absent(self, tmp_path):
        """Same fail-open contract as taxonomy/chash: cascade runs even
        when the T3 collection is already gone (recovery path)."""
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch
        from nexus.errors import CollectionNotFoundError as NotFoundError

        from nexus.commands.collection import delete_cmd
        from nexus.db.t2 import T2Database
        from tests.pipeline_fake_engine import make_fake_engine_db

        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass

        pdb, engine = make_fake_engine_db()
        pdb.create_pipeline("orphan_h", "/o.pdf", "docs__gone")

        # make_t3()/_t3() return the service-backed HttpVectorClient
        # unconditionally in production since RDR-155 P4a.2 -- cloud
        # creds / is_local_mode() no longer affect the handle type.
        # delete_collection is a direct call on both handles.
        fake_t3 = MagicMock(spec=HttpVectorClient)
        fake_t3.delete_collection = MagicMock(
            side_effect=NotFoundError("Collection [docs__gone] does not exist"),
        )

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake_t3), \
             patch("nexus.mcp_infra.default_db_path", return_value=db_path), \
             patch(
                 "nexus.commands._helpers.default_db_path",
                 return_value=db_path,
             ), \
             patch("nexus.db.http_pipeline_client.HttpPipelineDB", return_value=pdb):
            result = runner.invoke(delete_cmd, ["docs__gone", "--yes"])

        assert result.exit_code == 0, result.output
        assert "orphan_h" not in engine.pipelines
