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
from pathlib import Path

import pytest

from nexus.db.http_vector_client import HttpVectorClient

_ENGINE_SUBSTRATE = os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine"


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
    from nexus.db.storage_mode import has_raw_access

    if has_raw_access(tax):
        cur = tax.conn.execute(
            "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-04-16T00:00:00Z')",
            (label, collection, centroid_hash, doc_count, "[]"),
        )
        tax.conn.commit()
        return cur.lastrowid
    return tax.import_topic(
        src_id=src_id, label=label, parent_id=None, collection=collection,
        centroid_hash=centroid_hash, doc_count=doc_count,
        created_at="2026-04-16T00:00:00Z", review_status="pending", terms="[]",
    )


def _seed_assignment(tax, *, doc_id: str, topic_id: int, assigned_by: str,
                     source_collection: str) -> None:
    from nexus.db.storage_mode import has_raw_access

    if has_raw_access(tax):
        tax.conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, source_collection) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, topic_id, assigned_by, source_collection),
        )
        tax.conn.commit()
        return
    tax.import_assignment(
        doc_id=doc_id, topic_id=topic_id, assigned_by=assigned_by,
        similarity=None, assigned_at=None, source_collection=source_collection,
    )


def _seed_link(tax, *, from_topic_id: int, to_topic_id: int, link_count: int) -> None:
    from nexus.db.storage_mode import has_raw_access

    if has_raw_access(tax):
        tax.conn.execute(
            "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
            "VALUES (?, ?, ?, ?)",
            (from_topic_id, to_topic_id, link_count, "[]"),
        )
        tax.conn.commit()
        return
    tax.import_topic_link(
        from_topic_id=from_topic_id, to_topic_id=to_topic_id,
        link_count=link_count, link_types="[]",
    )


def _seed_meta(tax, *, collection: str, doc_count: int = 10) -> None:
    from nexus.db.storage_mode import has_raw_access

    if has_raw_access(tax):
        tax.conn.execute(
            "INSERT INTO taxonomy_meta (collection, last_discover_doc_count, last_discover_at) "
            "VALUES (?, ?, ?)",
            (collection, doc_count, "2026-04-14T12:00:00Z"),
        )
        tax.conn.commit()
        return
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
def seeded_taxonomy(tmp_path: Path):
    """Open a real T2Database on disk and seed two collections with
    topics, assignments, and cross-collection links so the cascade
    path is exercised, not mocked."""
    from nexus.db.t2 import T2Database

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
                     assigned_by="hdbscan", source_collection="docs__doomed")
    # Projection of doomed chunks into B's topic
    _seed_assignment(tax, doc_id=D_DOOMED2, topic_id=t_b1,
                     assigned_by="projection", source_collection="docs__doomed")
    # Projection of B chunks into A's topic (must also be purged — doomed
    # topic_id → NULL FK residue left behind otherwise)
    _seed_assignment(tax, doc_id=D_KEEP1, topic_id=t_a2,
                     assigned_by="projection", source_collection="docs__keepme")
    # Native B assignment — must survive
    _seed_assignment(tax, doc_id=D_KEEP2, topic_id=t_b1,
                     assigned_by="hdbscan", source_collection="docs__keepme")

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

    @pytest.mark.skipif(
        _ENGINE_SUBSTRATE,
        reason="dies-roster: sabotages the raw SQLite conn to prove the "
               "client-side purge transaction rolls back; the engine substrate "
               "has no raw handle to sabotage — dies at the RDR-155 P4b flip",
    )
    def test_purge_is_transactional(self, seeded_taxonomy):
        """If any step fails mid-cascade, the whole purge rolls back.

        sqlite3.Connection.execute is read-only at the C-API level so
        we sabotage via a wrapper that masquerades as the real conn.
        """
        db, tax, _ids = seeded_taxonomy

        class FlakyConn:
            def __init__(self, real):
                self._real = real
            def execute(self, sql, params=()):
                if "DELETE FROM taxonomy_meta" in sql:
                    raise RuntimeError("sabotaged")
                return self._real.execute(sql, params)
            def commit(self):
                return self._real.commit()
            def rollback(self):
                return self._real.rollback()

        real_conn = tax.conn
        try:
            tax.conn = FlakyConn(real_conn)
            with pytest.raises(RuntimeError, match="sabotaged"):
                tax.purge_collection("docs__doomed")
        finally:
            tax.conn = real_conn

        # After rollback: all seeded rows must still be present.
        topics_remaining = tax.conn.execute(
            "SELECT COUNT(*) FROM topics WHERE collection = ?",
            ("docs__doomed",),
        ).fetchone()[0]
        assert topics_remaining == 2, (
            "purge_collection must be transactional; "
            "a mid-cascade failure must roll back every prior delete"
        )

    def test_purge_unknown_collection_returns_zero_counts(self, seeded_taxonomy):
        """Purging a collection with no rows is a silent no-op."""
        db, tax, _ids = seeded_taxonomy
        counts = tax.purge_collection("docs__never-existed")
        assert counts == {"topics": 0, "assignments": 0, "links": 0, "meta": 0}


@pytest.mark.skipif(
    _ENGINE_SUBSTRATE,
    reason="dies-roster: exercises the LOCAL client-side fan-out cascade "
           "(mocked _t3 handle, chromadb NotFoundError fail-open, "
           "purge-after-Chroma-delete ordering); in service mode "
           "purge_collection_cascade routes to the engine's single atomic "
           "deleteCollection and never touches the mocked handle — dies at "
           "the RDR-155 P4b flip",
)
class TestCollectionDeleteCommandCascades:
    """Integration: `nx collection delete` cascades via Click entry point."""

    def test_cli_delete_cascades_when_t3_collection_absent(self, tmp_path):
        """Discovered during 4.5.0 shakeout: if the Chroma collection is
        already gone (previous delete left orphan taxonomy rows), the T3
        delete raises NotFoundError. The cascade MUST still run so the
        orphans can be cleaned up — otherwise the recovery case never
        terminates and users are stuck with manual sqlite surgery per
        the pre-fix workaround."""
        from click.testing import CliRunner
        from chromadb.errors import NotFoundError
        from unittest.mock import MagicMock, patch

        from nexus.db.t2 import T2Database
        from nexus.commands.collection import delete_cmd

        db_path = tmp_path / "memory.db"
        db = T2Database(db_path)
        _seed_topic(db.taxonomy, src_id=1, label="Orphan", collection="docs__gone",
                    centroid_hash=_chash("h"), doc_count=1)
        db.close()

        # make_t3()/_t3() return the service-backed HttpVectorClient
        # unconditionally in production since RDR-155 P4a.2 -- cloud
        # creds / is_local_mode() no longer affect the handle type.
        # delete_collection is a direct call on both handles.
        fake_t3 = MagicMock(spec=HttpVectorClient)
        fake_t3.delete_collection = MagicMock(
            side_effect=NotFoundError("Collection [docs__gone] does not exist")
        )

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake_t3), \
             patch("nexus.mcp_infra.default_db_path", return_value=db_path), \
             patch(
                 "nexus.commands._helpers.default_db_path",
                 return_value=db_path,
             ):
            result = runner.invoke(delete_cmd, ["docs__gone", "--yes"])

        assert result.exit_code == 0, result.output
        assert "already absent" in result.output, (
            f"Expected informational note that T3 collection was absent. "
            f"Got: {result.output!r}"
        )

        # Cascade DID run despite the NotFoundError
        with T2Database(db_path) as verify_db:
            remaining = verify_db.taxonomy.get_topics_for_collection("docs__gone")
        assert remaining == [], (
            "Cascade must run even when T3 collection is absent"
        )

    def test_cli_delete_calls_purge_collection(self, tmp_path, monkeypatch):
        """The CLI path must invoke purge_collection after the Chroma
        delete — not skip it, not run before (order matters for the
        count report)."""
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch

        from nexus.db.t2 import T2Database
        from nexus.commands.collection import delete_cmd

        db_path = tmp_path / "memory.db"
        db = T2Database(db_path)
        # Seed one topic for the doomed collection so purge has work
        _seed_topic(db.taxonomy, src_id=1, label="Only", collection="docs__doomed",
                    centroid_hash=_chash("h"), doc_count=1)
        _seed_meta(db.taxonomy, collection="docs__doomed")
        db.close()

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
             ):
            result = runner.invoke(delete_cmd, ["docs__doomed", "--yes"])

        assert result.exit_code == 0, result.output
        assert fake_t3.delete_collection.called
        # Must mention the taxonomy cascade in the report
        assert "taxonomy" in result.output.lower() or "topic" in result.output.lower(), (
            f"Delete report missing taxonomy cleanup count. Output: {result.output!r}"
        )

        # Cascade actually happened
        with T2Database(db_path) as verify_db:
            topics = verify_db.taxonomy.get_topics_for_collection("docs__doomed")
            meta_present = _meta_row_present(verify_db.taxonomy, "docs__doomed")
        assert topics == []
        assert not meta_present


# ── Phase 1.4 (nexus-r9b) — chash_index cascade ──────────────────────────────


@pytest.mark.skipif(
    _ENGINE_SUBSTRATE,
    reason="dies-roster: the chash_index router cascade's live subject is the "
           "frozen SQLite twin (RDR-158 pre-migration installs); the engine's "
           "/v1/chash endpoints are width-validating accept-and-no-op remnants "
           "(RDR-187 orphan-by-design), so rows can't be seeded or counted "
           "engine-side — dies at the RDR-155 P4b flip",
)
class TestChashIndexDeleteCascade:
    """RDR-086 Phase 1.4: `nx collection delete` must also remove every
    chash_index row pointing at the deleted collection. Without the cascade,
    Phase 2's ``Catalog.resolve_chash`` would return stale (collection,
    doc_id) tuples for chunks that no longer exist in T3.
    """

    def test_cli_delete_cascades_chash_index(self, tmp_path, monkeypatch):
        """After CLI delete, every chash_index row for that collection is gone."""
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch

        from nexus.db.t2 import T2Database
        from nexus.commands.collection import delete_cmd

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            # Full-width chashes (RDR-180): the engine rejects anything
            # shorter than the full 64-hex sha256 with HTTP 400.
            db.chash_index.upsert(
                chash=_chash("gone-1"), collection="code__gone",
            )
            db.chash_index.upsert(
                chash=_chash("gone-2"), collection="code__gone",
            )
            # Row in a different collection — must survive the cascade.
            db.chash_index.upsert(
                chash=_chash("stays-1"), collection="code__stays",
            )

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
             ):
            result = runner.invoke(delete_cmd, ["code__gone", "--yes"])

        assert result.exit_code == 0, result.output

        with T2Database(db_path) as verify_db:
            gone_rows = verify_db.chash_index.count_for_collection("code__gone")
            stays_rows = verify_db.chash_index.count_for_collection("code__stays")
        assert gone_rows == 0, "cascade must clear deleted collection's rows"
        assert stays_rows == 1, "cascade must NOT touch other collections"

    def test_cli_delete_cascades_chash_index_when_t3_absent(
        self, tmp_path, monkeypatch,
    ):
        """Cascade runs even when the Chroma delete raises NotFoundError —
        same fail-open contract as the taxonomy cascade.
        """
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch
        from chromadb.errors import NotFoundError

        from nexus.db.t2 import T2Database
        from nexus.commands.collection import delete_cmd

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.chash_index.upsert(
                chash=_chash("orphan-1"), collection="docs__orphan",
            )

        # make_t3()/_t3() return the service-backed HttpVectorClient
        # unconditionally in production since RDR-155 P4a.2 -- cloud
        # creds / is_local_mode() no longer affect the handle type.
        # delete_collection is a direct call on both handles.
        fake_t3 = MagicMock(spec=HttpVectorClient)
        fake_t3.delete_collection = MagicMock(
            side_effect=NotFoundError(
                "Collection [docs__orphan] does not exist",
            )
        )

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake_t3), \
             patch("nexus.mcp_infra.default_db_path", return_value=db_path), \
             patch(
                 "nexus.commands._helpers.default_db_path",
                 return_value=db_path,
             ):
            result = runner.invoke(delete_cmd, ["docs__orphan", "--yes"])

        assert result.exit_code == 0, result.output

        with T2Database(db_path) as verify_db:
            remaining = verify_db.chash_index.count_for_collection("docs__orphan")
        assert remaining == 0

    def test_cli_delete_reports_chash_index_count(self, tmp_path, monkeypatch):
        """Delete output must include the chash_index row count so the
        operator sees the full cascade's effect, not just taxonomy rows.
        """
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch

        from nexus.db.t2 import T2Database
        from nexus.commands.collection import delete_cmd

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            for i in range(5):
                db.chash_index.upsert(
                    chash=_chash(f"reported-{i:02d}"), collection="code__reported",
                )

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
             ):
            result = runner.invoke(delete_cmd, ["code__reported", "--yes"])

        assert result.exit_code == 0, result.output
        assert "chash" in result.output.lower() or "5" in result.output, (
            f"Expected chash_index cleanup count in delete output. "
            f"Got: {result.output!r}"
        )


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
        from chromadb.errors import NotFoundError

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
