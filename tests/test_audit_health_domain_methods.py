# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for CatalogTaxonomy / Telemetry / DocumentAspects domain methods
introduced by nexus-xcji (RDR-112 P0.5).

Each method here replaces a previous ``.conn.execute`` reach-through
from ``src/nexus/collection_audit.py``,
``src/nexus/collection_health.py``, or
``src/nexus/operators/aspect_sql.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


# ── CatalogTaxonomy ────────────────────────────────────────────────────────


def _seed_topic(
    db: T2Database, tid: int, label: str, collection: str,
) -> None:
    db.taxonomy.conn.execute(
        "INSERT INTO topics (id, label, collection, terms, created_at) "
        "VALUES (?, ?, ?, '[]', datetime('now'))",
        (tid, label, collection),
    )
    db.taxonomy.conn.commit()


def _seed_assignment(
    db: T2Database, *, doc_id: str, topic_id: int,
    source_collection: str | None, similarity: float | None = None,
    assigned_by: str = "projection",
) -> None:
    db.taxonomy.conn.execute(
        "INSERT INTO topic_assignments "
        "(doc_id, topic_id, source_collection, similarity, assigned_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (doc_id, topic_id, source_collection, similarity, assigned_by),
    )
    db.taxonomy.conn.commit()


def test_query_cross_projections_ordered_by_score(db: T2Database) -> None:
    _seed_topic(db, 1, "alpha", "docs__a")
    _seed_topic(db, 2, "beta", "code__b")
    _seed_topic(db, 3, "gamma", "knowledge__c")
    _seed_assignment(db, doc_id="d1", topic_id=1, source_collection="src", similarity=0.9)
    _seed_assignment(db, doc_id="d2", topic_id=2, source_collection="src", similarity=0.5)
    _seed_assignment(db, doc_id="d3", topic_id=3, source_collection="src", similarity=0.1)

    rows = db.taxonomy.query_cross_projections("src", top_n=2)
    assert len(rows) == 2
    # docs__a has 1 shared × 0.9 = 0.9; code__b has 1 × 0.5 = 0.5
    assert rows[0] == ("docs__a", 1, pytest.approx(0.9))
    assert rows[1] == ("code__b", 1, pytest.approx(0.5))


def test_query_cross_projections_excludes_self_and_null_similarity(
    db: T2Database,
) -> None:
    _seed_topic(db, 1, "alpha", "src")
    _seed_topic(db, 2, "beta", "other")
    _seed_assignment(db, doc_id="d1", topic_id=1, source_collection="src", similarity=0.9)
    _seed_assignment(db, doc_id="d2", topic_id=2, source_collection="src", similarity=None)

    assert db.taxonomy.query_cross_projections("src") == []


def test_query_hub_topic_ids_ordered_deterministically(db: T2Database) -> None:
    for tid, lbl, col in [(1, "a", "x"), (2, "b", "y"), (3, "c", "z")]:
        _seed_topic(db, tid, lbl, col)
    # topic 1: 3 source collections; topic 2: 2; topic 3: 1
    for src in ("a", "b", "c"):
        _seed_assignment(db, doc_id=f"{src}1", topic_id=1, source_collection=src)
    for src in ("a", "b"):
        _seed_assignment(db, doc_id=f"{src}2", topic_id=2, source_collection=src)
    _seed_assignment(db, doc_id="a3", topic_id=3, source_collection="a")

    hubs = db.taxonomy.query_hub_topic_ids(limit=10)
    assert hubs == [(1, 3), (2, 2), (3, 1)]


def test_count_hub_topic_assignments(db: T2Database) -> None:
    _seed_topic(db, 1, "a", "x")
    _seed_assignment(db, doc_id="d1", topic_id=1, source_collection="src")
    _seed_assignment(db, doc_id="d2", topic_id=1, source_collection="src")
    _seed_assignment(db, doc_id="d3", topic_id=1, source_collection="other")

    assert db.taxonomy.count_hub_topic_assignments(1, "src") == 2
    assert db.taxonomy.count_hub_topic_assignments(1, "missing") == 0


def test_count_assignments_for_source(db: T2Database) -> None:
    _seed_topic(db, 1, "a", "x")
    _seed_assignment(db, doc_id="d1", topic_id=1, source_collection="src")
    _seed_assignment(db, doc_id="d2", topic_id=1, source_collection="src")
    _seed_assignment(db, doc_id="d3", topic_id=1, source_collection="other")

    assert db.taxonomy.count_assignments_for_source("src") == 2
    assert db.taxonomy.count_assignments_for_source("missing") == 0


def test_count_assignments_in_topics_for_source(db: T2Database) -> None:
    for tid in (1, 2, 3):
        _seed_topic(db, tid, f"t{tid}", "x")
    _seed_assignment(db, doc_id="d1", topic_id=1, source_collection="src")
    _seed_assignment(db, doc_id="d2", topic_id=2, source_collection="src")
    _seed_assignment(db, doc_id="d3", topic_id=3, source_collection="src")
    _seed_assignment(db, doc_id="d4", topic_id=1, source_collection="other")

    assert db.taxonomy.count_assignments_in_topics_for_source(
        "src", [1, 2],
    ) == 2
    assert db.taxonomy.count_assignments_in_topics_for_source("src", []) == 0


def test_rank_collections_by_incoming_projection(db: T2Database) -> None:
    _seed_topic(db, 1, "a", "docs__a")
    _seed_topic(db, 2, "b", "code__b")
    _seed_topic(db, 3, "c", "knowledge__c")
    # docs__a has 2 distinct source-collection incoming projections
    _seed_assignment(db, doc_id="d1", topic_id=1, source_collection="src1")
    _seed_assignment(db, doc_id="d2", topic_id=1, source_collection="src2")
    # code__b has 1
    _seed_assignment(db, doc_id="d3", topic_id=2, source_collection="src1")
    # knowledge__c has 0 incoming → absent from map

    ranks = db.taxonomy.rank_collections_by_incoming_projection(
        ["docs__a", "code__b", "knowledge__c"],
    )
    assert ranks["docs__a"] == 1
    assert ranks["code__b"] == 2
    assert "knowledge__c" not in ranks


def test_rank_collections_empty_input(db: T2Database) -> None:
    assert db.taxonomy.rank_collections_by_incoming_projection([]) == {}


# ── Telemetry ──────────────────────────────────────────────────────────────


def test_query_top_distances_filters_window(db: T2Database) -> None:
    now = datetime.now(UTC)
    inside = (now - timedelta(days=1)).isoformat()
    outside = (now - timedelta(days=60)).isoformat()
    db.telemetry.conn.executemany(
        "INSERT INTO search_telemetry "
        "(ts, query_hash, collection, kept_count, raw_count, top_distance) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (inside, "h1", "src", 5, 5, 0.3),
            (inside, "h2", "src", 0, 5, 0.8),
            (outside, "h3", "src", 5, 5, 0.5),  # outside window
            (inside, "h4", "other", 5, 5, 0.4),  # different collection
            (inside, "h5", "src", 5, 0, 0.9),  # raw_count = 0 excluded
            (inside, "h6", "src", 5, 5, None),  # NULL excluded
        ],
    )
    db.telemetry.conn.commit()

    distances = db.telemetry.query_top_distances("src", days=30)
    assert sorted(distances) == [0.3, 0.8]


def test_query_top_distances_empty(db: T2Database) -> None:
    assert db.telemetry.query_top_distances("missing") == []


def test_query_top_distances_rejects_zero_days(db: T2Database) -> None:
    with pytest.raises(ValueError, match="days must be >= 1"):
        db.telemetry.query_top_distances("src", days=0)


# ── DocumentAspects paginated helpers ─────────────────────────────────────


def _seed_aspect(
    db: T2Database, *, source_uri: str, collection: str = "code__c",
    confidence: float | None = None,
    proposed_method: str | None = None,
    extras: str | None = None,
) -> None:
    db.document_aspects.conn.execute(
        "INSERT INTO document_aspects "
        "(collection, source_path, source_uri, confidence, proposed_method, "
        " extras, extracted_at, model_version, extractor_name) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'test', 'test')",
        (collection, source_uri, source_uri, confidence, proposed_method, extras),
    )
    db.document_aspects.conn.commit()


def test_filter_uris_by_predicate(db: T2Database) -> None:
    _seed_aspect(db, source_uri="u1", proposed_method="nips")
    _seed_aspect(db, source_uri="u2", proposed_method="acl")
    _seed_aspect(db, source_uri="u3", proposed_method=None)

    matched = db.document_aspects.filter_uris_by_predicate(
        ["u1", "u2", "u3", "u4"],
        where_sql="proposed_method = ?",
        where_params=("nips",),
    )
    assert matched == {"u1"}


def test_filter_uris_empty_input(db: T2Database) -> None:
    assert db.document_aspects.filter_uris_by_predicate(
        [], where_sql="proposed_method = ?", where_params=("x",),
    ) == set()


def test_filter_uris_batches_above_300(db: T2Database) -> None:
    # 350 rows, all matching the predicate; batch must paginate cleanly.
    for i in range(350):
        _seed_aspect(db, source_uri=f"u{i}", proposed_method="hit")
    matched = db.document_aspects.filter_uris_by_predicate(
        [f"u{i}" for i in range(350)],
        where_sql="proposed_method = ?",
        where_params=("hit",),
    )
    assert len(matched) == 350


def test_select_field_by_uris_column(db: T2Database) -> None:
    _seed_aspect(db, source_uri="u1", proposed_method="nips")
    _seed_aspect(db, source_uri="u2", proposed_method="acl")

    result = db.document_aspects.select_field_by_uris(
        ["u1", "u2", "missing"],
        select_expr="proposed_method",
    )
    assert result == {"u1": "nips", "u2": "acl"}


def test_select_field_by_uris_json_extract(db: T2Database) -> None:
    _seed_aspect(db, source_uri="u1", extras='{"k": "v1"}')
    _seed_aspect(db, source_uri="u2", extras='{"k": "v2"}')

    result = db.document_aspects.select_field_by_uris(
        ["u1", "u2"],
        select_expr="json_extract(extras, ?)",
        select_params=("$.k",),
    )
    assert result == {"u1": "v1", "u2": "v2"}


def test_fold_confidence_by_uris(db: T2Database) -> None:
    _seed_aspect(db, source_uri="u1", confidence=0.1)
    _seed_aspect(db, source_uri="u2", confidence=0.5)
    _seed_aspect(db, source_uri="u3", confidence=0.9)
    _seed_aspect(db, source_uri="u4", confidence=None)

    sum_, min_, max_, count = db.document_aspects.fold_confidence_by_uris(
        ["u1", "u2", "u3", "u4", "missing"],
    )
    assert sum_ == pytest.approx(1.5)
    assert min_ == pytest.approx(0.1)
    assert max_ == pytest.approx(0.9)
    assert count == 3


def test_fold_confidence_empty(db: T2Database) -> None:
    assert db.document_aspects.fold_confidence_by_uris([]) == (0.0, None, None, 0)


def test_fold_confidence_no_matches(db: T2Database) -> None:
    _seed_aspect(db, source_uri="u1", confidence=0.5)
    sum_, min_, max_, count = db.document_aspects.fold_confidence_by_uris(
        ["other"],
    )
    assert (sum_, min_, max_, count) == (0.0, None, None, 0)
