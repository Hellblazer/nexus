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
import os

# nexus-aqbrk: this module's dies-roster was WRONG, and measurably so — 6 of
# its 7 tests pass on the engine substrate. The reason read "sqlite-vs-service
# backend probe matrix dies at the RDR-155 P4b flip", but what this file
# actually tests is the has_raw_access GUARDS (see the module docstring): that
# collection_health / collection_audit / merge_candidates degrade instead of
# crashing when taxonomy has no raw .conn. Those guards are what SURVIVES the
# flip, not what dies with it — and the file proves it against its own
# _ServiceTaxonomy stub, so it is substrate-independent by construction.


class _ServiceTaxonomy:
    """Stand-in for HttpTaxonomyStore: no raw ``.conn`` / ``._lock``."""


class _FakeServiceT2:
    def __init__(self) -> None:
        self.taxonomy = _ServiceTaxonomy()

    def close(self) -> None:
        pass


def test_has_raw_access_true_for_raw_shape_false_for_service():
    """has_raw_access reports what the store IS, from its shape alone.

    The True leg used to build a real SQLite ``T2Database(path).taxonomy``.
    That store is deleted (nexus-i711w Stage 2 sub-stage C) and the property
    now raises for a =sqlite pin, so the leg is expressed with the shape the
    predicate actually tests for — a ``_lock`` plus a ``conn`` — rather than
    with a resurrected store. This keeps the DETECTION contract under test,
    which is what the assertion was always about; nothing here depended on the
    handle being a genuine CatalogTaxonomy.

    has_raw_access itself outlives this sub-stage: it retires with the rest of
    the SQLite stores in sub-stage A, when its last True case disappears for
    real rather than by construction.
    """
    from nexus.db.storage_mode import has_raw_access

    class _RawShaped:
        """Duck-typed raw store: what a SQLite domain store looked like."""

        _lock = object()
        conn = object()

    assert has_raw_access(_RawShaped()) is True
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


# ── nexus-9613q.2: fail-loud raw-handle guard on the Http*Stores ─────────────

_HTTP_T2_STORE_CLASSES = [
    ("nexus.db.t2.http_taxonomy_store", "HttpTaxonomyStore"),
    ("nexus.db.t2.http_document_aspects_store", "HttpDocumentAspectsStore"),
    ("nexus.db.t2.http_telemetry_store", "HttpTelemetryStore"),
    ("nexus.db.t2.http_memory_store", "HttpMemoryStore"),
    ("nexus.db.t2.http_plan_library", "HttpPlanLibrary"),
    ("nexus.db.t2.http_chash_index", "HttpChashIndex"),
    ("nexus.db.t2.http_aspect_queue", "HttpAspectQueue"),
    ("nexus.db.t2.http_document_highlights_store", "HttpDocumentHighlightsStore"),
]


def _import_cls(mod_name: str, cls_name: str):
    import importlib

    return getattr(importlib.import_module(mod_name), cls_name)


@pytest.mark.parametrize("mod_name,cls_name", _HTTP_T2_STORE_CLASSES)
def test_all_http_t2_stores_carry_raw_handle_guard(mod_name, cls_name):
    from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin

    cls = _import_cls(mod_name, cls_name)
    assert issubclass(cls, RawHandleGuardMixin), cls


@pytest.mark.parametrize("mod_name,cls_name", _HTTP_T2_STORE_CLASSES)
def test_http_store_conn_and_lock_raise_actionable_attribute_error(mod_name, cls_name):
    from nexus.db.storage_mode import has_raw_access

    cls = _import_cls(mod_name, cls_name)
    store = cls.__new__(cls)  # bypass network init; the guard touches no state

    with pytest.raises(AttributeError) as ei:
        _ = store.conn
    msg = str(ei.value)
    assert "has_raw_access" in msg and "conn" in msg

    with pytest.raises(AttributeError):
        _ = store._lock

    # Must raise AttributeError (not RuntimeError) so hasattr/has_raw_access work.
    assert has_raw_access(store) is False
