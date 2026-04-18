# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx collection audit <name>`` — RDR-087 Phase 4.2.

Four sections in one report: distance histogram, top-5 cross-projections,
orphan chunks, hub-topic assignments. Section 1 (distance histogram)
ships telemetry-only in this bead; the live-probe fallback is deferred
to follow-up bead ``nexus-fx2d``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_t2(path: Path) -> None:
    """Build a T2 DB with topics, topic_assignments, and search_telemetry
    seeded for a ``code__main`` collection under audit plus a few others
    so cross-projection + hub queries have data."""
    from nexus.db.t2 import T2Database

    db = T2Database(path)
    c = db.taxonomy.conn
    c.executemany(
        "INSERT OR IGNORE INTO topics "
        "(id, label, collection, created_at) VALUES (?, ?, ?, ?)",
        [
            (1, "auth",    "code__main",    "2026-04-01"),
            (2, "search",  "code__main",    "2026-04-01"),
            (3, "db",      "docs__alpha",   "2026-04-01"),
            (4, "misc",    "code__other",   "2026-04-01"),
            (5, "hub-A",   "code__main",    "2026-04-01"),  # high-src hub
            (6, "hub-B",   "code__main",    "2026-04-01"),
        ],
    )
    # topic_assignments:
    # - topic 3 (docs__alpha) gets multiple chunks from code__main → cross-projection pair.
    # - topic 4 (code__other) gets 1 chunk from code__main → another pair.
    # - topics 5 and 6 get chunks from many source collections → they're cross-coll hubs.
    c.executemany(
        "INSERT INTO topic_assignments "
        "(doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            # code__main → docs__alpha/db (3 shared docs, avg sim 0.7)
            ("cm1", 3, "projection", 0.8, "2026-04-01", "code__main"),
            ("cm2", 3, "projection", 0.7, "2026-04-01", "code__main"),
            ("cm3", 3, "projection", 0.6, "2026-04-01", "code__main"),
            # code__main → code__other/misc (1 shared doc, avg sim 0.5)
            ("cm4", 4, "projection", 0.5, "2026-04-01", "code__main"),
            # Hub-A (topic 5) gets chunks from 3 source collections → hub
            ("cm5", 5, "projection", 0.9, "2026-04-01", "code__main"),
            ("hx",  5, "projection", 0.9, "2026-04-01", "docs__alpha"),
            ("hy",  5, "projection", 0.9, "2026-04-01", "code__other"),
            # Hub-B (topic 6) gets chunks from 2 source collections
            ("cm6", 6, "projection", 0.85, "2026-04-01", "code__main"),
            ("hy2", 6, "projection", 0.85, "2026-04-01", "code__other"),
        ],
    )
    c.commit()
    # search_telemetry: seed 15 rows for code__main in the last 30d
    # with top_distance values spread across buckets.
    now = datetime.now(UTC)
    tel_rows = []
    dists = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.05,
             1.25, 0.15, 0.25, 0.35, 0.45]  # 15 samples
    for i, d in enumerate(dists):
        ts = (now - timedelta(days=1)).isoformat()
        tel_rows.append(
            (ts, f"hash{i:04d}", "code__main", 5, 3, d, 0.45),
        )
    db.telemetry.log_search_batch(tel_rows)
    db.close()


def _seed_catalog_conn(db_path: Path) -> "sqlite3.Connection":
    """Build a minimal catalog SQLite cache directly — skip the
    JSONL/git facade that would rebuild from source on first open."""
    import sqlite3

    from nexus.catalog.catalog_db import _SCHEMA_SQL

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_SQL)
    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    new_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    conn.executemany(
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, corpus, "
        " physical_collection, chunk_count, head_hash, indexed_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("1.1", "linked doc (old)", "t", 2026, "code", "src/linked.py",
             "code", "code__main", 5, "h1", old_ts, "{}"),
            ("1.2", "orphan old", "t", 2026, "code", "src/orphan.py",
             "code", "code__main", 3, "h2", old_ts, "{}"),
            ("1.3", "orphan recent", "t", 2026, "code", "src/recent.py",
             "code", "code__main", 1, "h3", new_ts, "{}"),
        ],
    )
    # Incoming link onto 1.1 — makes it non-orphan.
    conn.execute(
        "INSERT INTO links (from_tumbler, to_tumbler, link_type, created_by) "
        "VALUES (?, ?, ?, ?)",
        ("2.1", "1.1", "cites", "test"),
    )
    conn.commit()
    return conn


# ── Section 2: cross-projections ────────────────────────────────────────────


class TestCrossProjections:
    def test_ranked_by_score_shared_x_similarity(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_cross_projections
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            pairs = compute_cross_projections(
                db.taxonomy.conn, "code__main", top_n=5,
            )
        finally:
            db.close()

        # code__main → docs__alpha is higher-score than → code__other.
        names = [p.other_collection for p in pairs]
        assert "docs__alpha" in names
        assert "code__other" in names
        idx_alpha = names.index("docs__alpha")
        idx_other = names.index("code__other")
        assert idx_alpha < idx_other
        # code__main should NOT project to itself even though topics 5/6
        # are in code__main (that's not cross-projection).
        assert "code__main" not in names

    def test_empty_when_no_projection_rows(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_cross_projections
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            pairs = compute_cross_projections(
                db.taxonomy.conn, "code__unseen", top_n=5,
            )
        finally:
            db.close()
        assert pairs == []


# ── Section 3: orphan chunks ────────────────────────────────────────────────


class TestOrphanChunks:
    def test_flags_old_unlinked_documents(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_orphan_chunks

        conn = _seed_catalog_conn(tmp_path / "catalog.db")
        try:
            orphans = compute_orphan_chunks(
                conn, "code__main", age_days=30, limit=20,
            )
        finally:
            conn.close()
        # 1.1 has incoming link → not orphan.
        # 1.2 is old + unlinked → orphan.
        # 1.3 is unlinked but young (5d) → not orphan.
        tumblers = {o.tumbler for o in orphans}
        assert "1.2" in tumblers
        assert "1.1" not in tumblers
        assert "1.3" not in tumblers

    def test_empty_when_collection_clean(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_orphan_chunks

        conn = _seed_catalog_conn(tmp_path / "catalog.db")
        try:
            orphans = compute_orphan_chunks(
                conn, "code__notreal", age_days=30, limit=20,
            )
        finally:
            conn.close()
        assert orphans == []


# ── Section 4: hub assignments ──────────────────────────────────────────────


class TestHubAssignments:
    def test_top_10_by_source_collection_breadth(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_hub_assignments
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            hubs = compute_hub_assignments(
                db.taxonomy.conn, "code__main", top_n=10,
            )
        finally:
            db.close()

        # Topic 5 (hub-A) sees chunks from 3 collections; topic 6 from 2;
        # topic 3 from 1 (only code__main); topic 4 from 1 (only code__main).
        # Assuming "hub" threshold is simply top-N by src_count, all topics
        # appear in the top-10; code__main chunks in each:
        by_id = {h.topic_id: h for h in hubs}
        # code__main contributes 1 chunk (cm5) to topic 5.
        assert by_id[5].chunks_in_hub == 1
        # code__main contributes 1 chunk (cm6) to topic 6.
        assert by_id[6].chunks_in_hub == 1


# ── Section 1: distance histogram (telemetry-only for this bead) ────────────


class TestDistanceHistogramTelemetryOnly:
    def test_buckets_cover_0_to_2_in_10_bins(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_distance_histogram
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            hist = compute_distance_histogram(
                db.taxonomy.conn, "code__main",
            )
        finally:
            db.close()
        assert len(hist.buckets) == 10
        assert sum(hist.buckets) == hist.sample_size == 15
        assert hist.source == "telemetry"

    def test_reports_empty_source_when_no_rows(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_distance_histogram
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            hist = compute_distance_histogram(db.taxonomy.conn, "code__cold")
        finally:
            db.close()
        assert hist.sample_size == 0
        assert hist.source == "empty"


# ── Section 5: chash_index coverage (RDR-087 Phase 4.6 / nexus-c2op) ────────


class TestChashCoverageSection:
    """Audit section 5 ratio + missing_sample shape.

    The production ``compute_chash_coverage`` hits T3 for the total
    chunk count. We exercise the pure-T2 path by stubbing make_t3's
    collection.count() so the test is deterministic without network.
    """

    def _seed_chash_index(self, db_path: Path, rows: list[tuple[str, str, str]]):
        """Seed ``chash_index`` rows: (chash, collection, doc_id)."""
        from nexus.db.t2.chash_index import ChashIndex

        idx = ChashIndex(db_path)
        try:
            for chash, coll, doc_id in rows:
                idx.upsert(chash=chash, collection=coll, doc_id=doc_id)
        finally:
            idx.close()

    def test_full_coverage_ratio_1(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.collection_audit import compute_chash_coverage

        db_path = tmp_path / "memory.db"
        self._seed_chash_index(db_path, [
            ("h0", "code__x", "id0"),
            ("h1", "code__x", "id1"),
            ("h2", "code__x", "id2"),
        ])

        class _FakeCol:
            def count(self): return 3
        class _FakeT3:
            def get_or_create_collection(self, _n): return _FakeCol()

        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        cov = compute_chash_coverage("code__x")
        assert cov is not None
        assert cov.total_chunks == 3
        assert cov.indexed_rows == 3
        assert cov.ratio == 1.0
        assert cov.missing_sample == []

    def test_partial_coverage_ratio_less_than_one(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.collection_audit import compute_chash_coverage

        db_path = tmp_path / "memory.db"
        # Only 2 of 4 chunks indexed.
        self._seed_chash_index(db_path, [
            ("h0", "code__x", "id0"),
            ("h1", "code__x", "id1"),
        ])

        class _FakeCol:
            def count(self): return 4
            def get(self, **kwargs):
                return {
                    "ids": ["id0", "id1", "id2", "id3"],
                    "metadatas": [{}, {}, {}, {}],
                }
        class _FakeT3:
            def get_or_create_collection(self, _n): return _FakeCol()

        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        cov = compute_chash_coverage("code__x")
        assert cov is not None
        assert cov.total_chunks == 4
        assert cov.indexed_rows == 2
        assert cov.ratio == 0.5
        # Missing-sample contains id2 + id3 (any of the non-indexed ids
        # in the sample page; bounded at 5).
        assert set(cov.missing_sample).issubset({"id2", "id3"})
        assert len(cov.missing_sample) == 2

    def test_empty_t3_collection_returns_none_ratio(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.collection_audit import compute_chash_coverage

        db_path = tmp_path / "memory.db"
        self._seed_chash_index(db_path, [])

        class _FakeCol:
            def count(self): return 0
        class _FakeT3:
            def get_or_create_collection(self, _n): return _FakeCol()

        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        cov = compute_chash_coverage("code__empty")
        assert cov is not None
        assert cov.total_chunks == 0
        assert cov.indexed_rows == 0
        assert cov.ratio is None

    def test_missing_t2_returns_none(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.collection_audit import compute_chash_coverage

        # T2 file does not exist.
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: tmp_path / "nonexistent.db",
        )
        cov = compute_chash_coverage("code__x")
        assert cov is None


# ── CLI integration ─────────────────────────────────────────────────────────


class TestCollectionAuditCli:
    def test_default_output_covers_four_sections(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        catalog_db_path = tmp_path / "catalog.db"
        _seed_catalog_conn(catalog_db_path).close()

        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr(
            "nexus.collection_audit._open_catalog_conn",
            lambda: __import__("sqlite3").connect(str(catalog_db_path)),
        )

        result = runner.invoke(
            main, ["collection", "audit", "code__main"],
        )
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        for section_hint in ["distance histogram", "cross-projection", "orphan", "hub"]:
            assert section_hint in out

    def test_json_flag_emits_parseable_payload(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        import json
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        catalog_db_path = tmp_path / "catalog.db"
        _seed_catalog_conn(catalog_db_path).close()

        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr(
            "nexus.collection_audit._open_catalog_conn",
            lambda: __import__("sqlite3").connect(str(catalog_db_path)),
        )

        result = runner.invoke(
            main, ["collection", "audit", "code__main", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["collection"] == "code__main"
        assert "distance_histogram" in payload
        assert "cross_projections" in payload
        assert "orphans" in payload
        assert "hub_assignments" in payload
