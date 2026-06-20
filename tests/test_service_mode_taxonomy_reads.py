# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-9613q.4: taxonomy diagnostic READS degrade gracefully in service mode.

collection_health (_default_projection_rank_fn / _default_hub_score_fn),
collection_audit (run_collection_audit) and merge_candidates
(run_merge_candidates) reached ``t2.taxonomy.conn`` on a T2Database whose
taxonomy resolves to an HttpTaxonomyStore in service mode (the 6.0 default) —
no raw ``.conn``, so they crashed (audit/merge) or silently returned empty
(health). They now guard with ``has_raw_access`` and skip / degrade instead of
crashing.
"""
from __future__ import annotations

import pytest


class _ServiceTaxonomy:
    """Stand-in for HttpTaxonomyStore: no raw ``.conn`` / ``._lock``."""


class _FakeServiceT2:
    def __init__(self) -> None:
        self.taxonomy = _ServiceTaxonomy()

    def close(self) -> None:
        pass


def test_has_raw_access_true_for_sqlite_false_for_service(tmp_path):
    from nexus.db.storage_mode import has_raw_access
    from nexus.db.t2 import T2Database

    db = T2Database(tmp_path / "t2.db")  # epsilon-allow: test asserts raw-access detection on a real SQLite store
    try:
        assert has_raw_access(db.taxonomy) is True
    finally:
        db.close()
    assert has_raw_access(_ServiceTaxonomy()) is False


def test_projection_rank_fn_returns_empty_in_service_mode(monkeypatch):
    import nexus.collection_health as ch

    monkeypatch.setattr(ch, "_open_t2", lambda: _FakeServiceT2())
    assert ch._default_projection_rank_fn(["c1", "c2"]) == {}


def test_hub_score_fn_returns_none_in_service_mode(monkeypatch):
    import nexus.collection_health as ch

    monkeypatch.setattr(ch, "_open_t2", lambda: _FakeServiceT2())
    assert ch._default_hub_score_fn("c1") is None


def test_merge_candidates_reports_service_mode_unavailable(monkeypatch):
    import nexus.merge_candidates as mc

    monkeypatch.setattr(mc, "_open_t2", lambda: _FakeServiceT2())
    out = mc.run_merge_candidates(
        min_shared=1, min_similarity=0.0, exclude_hubs=False,
        hub_top_n=10, limit=10, fmt="human",
    )
    assert "service mode" in out.lower()
    assert "merge-candidate" in out.lower()


def test_collection_audit_does_not_crash_in_service_mode(monkeypatch):
    import nexus.collection_audit as ca

    monkeypatch.setattr(ca, "_open_t2", lambda: _FakeServiceT2())
    monkeypatch.setattr(ca, "_open_catalog_conn", lambda: None)
    # live=False so no T3 probe; chash coverage has its own error boundary.
    report = ca.run_collection_audit(collection="x__y__v1", live=False)
    assert report.distance_histogram.source == "empty"
    assert report.cross_projections == []
    assert report.hub_assignments == []
