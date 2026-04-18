# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx collection merge-candidates`` — RDR-087 Phase 4.3.

Pair-wise cross-collection overlap ranking. Candidates where two
collections share topics with high similarity hint at merge/bridge
opportunities — surfaced for a human or agent to decide on.

``--create-link`` is **opt-in**; the command never writes catalog
edges without explicit confirmation. RDR §bridge-link workflow
explicitly defers auto-bridge to a later cycle.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_t2(path: Path) -> None:
    """Build a T2 DB with a mix of legitimate merge signals and
    hub-dominated noise.

    - 5 topics: 2 cross-collection hubs (topic_ids 4, 5), 3 normal.
    - Pair (code__a, docs__b): shared across 3 normal topics, high sim.
    - Pair (code__a, code__c): shared only via hub topics → suppressible.
    - One NULL source_collection row — must be excluded per RDR Failure
      Modes.
    """
    from nexus.db.t2 import T2Database

    db = T2Database(path)
    c = db.taxonomy.conn
    c.executemany(
        "INSERT OR IGNORE INTO topics "
        "(id, label, collection, created_at) VALUES (?, ?, ?, ?)",
        [
            (1, "auth",   "docs__b", "2026-04-01"),
            (2, "search", "docs__b", "2026-04-01"),
            (3, "db",     "docs__b", "2026-04-01"),
            (4, "hub-A",  "code__c", "2026-04-01"),  # hub: wide source_collection spread
            (5, "hub-B",  "code__c", "2026-04-01"),  # hub
        ],
    )
    c.executemany(
        "INSERT INTO topic_assignments "
        "(doc_id, topic_id, assigned_by, similarity, "
        " assigned_at, source_collection) VALUES (?, ?, ?, ?, ?, ?)",
        [
            # (code__a, docs__b) pair — 3 distinct topics, mean sim 0.8.
            ("a1", 1, "projection", 0.9, "2026-04-01", "code__a"),
            ("a2", 2, "projection", 0.8, "2026-04-01", "code__a"),
            ("a3", 3, "projection", 0.7, "2026-04-01", "code__a"),
            # (code__a, code__c) pair — only via hubs (topics 4, 5).
            ("a4", 4, "projection", 0.6, "2026-04-01", "code__a"),
            ("a5", 5, "projection", 0.6, "2026-04-01", "code__a"),
            # Hub topics also get chunks from other collections — makes
            # them actual cross-collection hubs.
            ("b1", 4, "projection", 0.7, "2026-04-01", "docs__b"),
            ("c1", 4, "projection", 0.7, "2026-04-01", "docs__d"),
            ("e1", 4, "projection", 0.7, "2026-04-01", "docs__e"),
            ("b2", 5, "projection", 0.7, "2026-04-01", "docs__b"),
            ("c2", 5, "projection", 0.7, "2026-04-01", "docs__d"),
            ("e2", 5, "projection", 0.7, "2026-04-01", "docs__e"),
            # NULL source_collection — must be ignored.
            ("x1", 1, "hdbscan",    None, None,           None),
        ],
    )
    c.commit()
    db.close()


# ── Core query ──────────────────────────────────────────────────────────────


class TestComputeMergeCandidates:
    def test_orders_by_score_desc(self, tmp_path: Path) -> None:
        from nexus.merge_candidates import compute_merge_candidates
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            pairs = compute_merge_candidates(
                db.taxonomy.conn,
                min_shared=1,
                min_similarity=0.0,
            )
        finally:
            db.close()
        # Best pair by (shared × mean_sim) must be first.
        assert pairs[0].a == "code__a"
        assert pairs[0].b == "docs__b"
        assert pairs[0].shared_topics == 3
        assert pairs[0].mean_sim == pytest.approx((0.9 + 0.8 + 0.7) / 3)

    def test_excludes_null_source_collection(self, tmp_path: Path) -> None:
        from nexus.merge_candidates import compute_merge_candidates
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            pairs = compute_merge_candidates(
                db.taxonomy.conn, min_shared=1, min_similarity=0.0,
            )
        finally:
            db.close()
        # NULL-source row pointed at topic 1 (docs__b) — if included,
        # it would add a NULL→docs__b row. Must not appear.
        assert not any(p.a is None or p.a == "" for p in pairs)

    def test_excludes_self_pairs(self, tmp_path: Path) -> None:
        """source_collection == topics.collection is a same-collection
        assignment, not a cross-collection candidate."""
        from nexus.merge_candidates import compute_merge_candidates
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            pairs = compute_merge_candidates(
                db.taxonomy.conn, min_shared=1, min_similarity=0.0,
            )
        finally:
            db.close()
        for p in pairs:
            assert p.a != p.b

    def test_min_shared_threshold_filters(self, tmp_path: Path) -> None:
        from nexus.merge_candidates import compute_merge_candidates
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            pairs = compute_merge_candidates(
                db.taxonomy.conn, min_shared=3, min_similarity=0.0,
            )
        finally:
            db.close()
        # Only (code__a, docs__b) hits 3 shared topics.
        assert len(pairs) == 1
        assert (pairs[0].a, pairs[0].b) == ("code__a", "docs__b")

    def test_min_similarity_threshold_filters(self, tmp_path: Path) -> None:
        from nexus.merge_candidates import compute_merge_candidates
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            pairs = compute_merge_candidates(
                db.taxonomy.conn, min_shared=1, min_similarity=0.65,
            )
        finally:
            db.close()
        # (code__a, code__c) pair has mean_sim 0.6 → filtered out.
        by_pair = {(p.a, p.b) for p in pairs}
        assert ("code__a", "code__c") not in by_pair

    def test_exclude_hubs_subtracts_top_n_hub_topics(
        self, tmp_path: Path,
    ) -> None:
        """Hub-dominated pair drops below threshold when hubs are
        excluded from the shared-topic count."""
        from nexus.merge_candidates import compute_merge_candidates
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        db = T2Database(db_path)
        try:
            # Top-2 hubs is enough to drop both hub topics (ids 4, 5).
            pairs = compute_merge_candidates(
                db.taxonomy.conn,
                min_shared=1,
                min_similarity=0.0,
                exclude_hubs=True,
                hub_top_n=2,
            )
        finally:
            db.close()
        by_pair = {(p.a, p.b): p.shared_topics for p in pairs}
        # (code__a, docs__b): unchanged — zero hub topics in the pair.
        assert by_pair[("code__a", "docs__b")] == 3
        # (code__a, code__c): dropped — its 2 shared topics are the hubs.
        assert ("code__a", "code__c") not in by_pair


# ── CLI ────────────────────────────────────────────────────────────────────


class TestMergeCandidatesCli:
    def test_default_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )

        result = runner.invoke(
            main,
            ["collection", "merge-candidates", "--min-shared", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "code__a" in result.output
        assert "docs__b" in result.output

    def test_json_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )

        result = runner.invoke(
            main,
            ["collection", "merge-candidates",
             "--min-shared", "1", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "candidates" in payload
        assert len(payload["candidates"]) >= 1

    def test_exclude_hubs_flag_wires_through(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )

        result = runner.invoke(
            main,
            ["collection", "merge-candidates",
             "--min-shared", "1", "--exclude-hubs", "--hub-top-n", "2",
             "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        pairs = {(c["a"], c["b"]) for c in payload["candidates"]}
        # Hub-only pair suppressed.
        assert ("code__a", "code__c") not in pairs
        # Legitimate pair survives.
        assert ("code__a", "docs__b") in pairs

    def test_create_link_is_opt_in_default_does_nothing(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        """Default invocation must NOT write catalog edges. RDR §bridge-
        link workflow explicitly defers auto-bridge. The ``--create-
        link`` flag is opt-in and currently surfaces a 'not yet
        implemented' advisory rather than writing silently."""
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )

        result = runner.invoke(
            main, ["collection", "merge-candidates", "--min-shared", "1"],
        )
        assert result.exit_code == 0, result.output
        # No mention of link creation happened.
        assert "creating" not in result.output.lower()
        assert "wrote" not in result.output.lower()

    def test_create_link_flag_emits_advisory(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )

        result = runner.invoke(
            main,
            ["collection", "merge-candidates",
             "--min-shared", "1", "--create-link"],
        )
        # Surface a deferred-workflow advisory (non-zero exit is fine —
        # pins that the flag is recognised but takes no destructive
        # action in this bead).
        assert result.exit_code != 0
        assert "deferred" in result.output.lower()
