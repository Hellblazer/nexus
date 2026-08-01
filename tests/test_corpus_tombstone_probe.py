# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-9n485 — t3_collection_name's tombstoned-candidate observability.

``t3_collection_name`` (nexus.corpus) already wants LIVE semantics: a
promoted/legacy candidate with no queryable content should be skipped in
favor of one that has some, exactly what ``collection_exists()`` already
gives it. That decision is correct as-is — this is NOT the collision-class
bug ``collection_rename.py`` had (see the bead's caller-semantics table).
The bead's complaint is that the skip is SILENT: an operator whose
"promoted" target got fully trashed sees the resolver quietly fall through
to a different (or missing) name with zero trace of why. This adds a
structlog debug event naming the tombstoned candidate WITHOUT changing
which name is returned — pinned by comparing against the pre-existing
untouched behavior in ``tests/test_corpus.py`` (all ``_FakeT3``-based, so
never TOMBSTONED, never emits this event).
"""
from __future__ import annotations

import logging
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient

# RDR-109 Phase 2: matches tests/test_corpus.py's module marker -- pins
# cloud-mode canonical embedding names (voyage-context-3) regardless of the
# host environment's local/cloud default.
pytestmark = pytest.mark.usefixtures("cloud_mode")

STATS_PATH = "/v1/vectors/stats"
COLLECTIONS_PATH = "/v1/vectors/collections"


def _patch_get(monkeypatch, *, stats_names: set[str], raw_names: set[str]) -> None:
    def fake_get(path: str, *, tenant: str = "default") -> Any:
        if path == STATS_PATH:
            return [
                {"name": n, "dim": 384, "count": 1, "last_write": "x"}
                for n in stats_names
            ]
        if path == COLLECTIONS_PATH:
            return [{"name": n} for n in raw_names]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)


def test_tombstoned_promoted_candidate_logs_debug_and_falls_through_to_legacy(
    monkeypatch,
) -> None:
    legacy = "knowledge__art"
    promoted = "knowledge__art__voyage-context-3__v1"
    # promoted has physical rows (raw) but zero live chunks (stats) --
    # tombstoned. legacy is live.
    _patch_get(
        monkeypatch,
        stats_names={legacy}, raw_names={legacy, promoted},
    )
    client = HttpVectorClient()

    # Bump structlog so the DEBUG event fires under capture_logs (matches
    # tests/test_catalog_path.py's pattern; default filter level is WARNING).
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with capture_logs() as cap:
            result = t3_collection_name(legacy, t3=client)
    finally:
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

    # Decision UNCHANGED: falls through to the live legacy name exactly as
    # collection_exists()-only semantics already dictate.
    assert result == legacy

    tombstone_events = [
        e for e in cap
        if e.get("event") == "t3_collection_name_promoted_candidate_tombstoned"
    ]
    assert len(tombstone_events) == 1, cap
    assert tombstone_events[0]["promoted"] == promoted


def test_live_promoted_candidate_no_tombstone_log(monkeypatch) -> None:
    """Sanity control: a LIVE promoted target returns immediately with no
    tombstone observability noise at all."""
    legacy = "knowledge__art"
    promoted = "knowledge__art__voyage-context-3__v1"
    _patch_get(monkeypatch, stats_names={promoted}, raw_names={promoted})
    client = HttpVectorClient()

    with capture_logs() as cap:
        result = t3_collection_name(legacy, t3=client)

    assert result == promoted
    tombstone_events = [
        e for e in cap
        if e.get("event") == "t3_collection_name_promoted_candidate_tombstoned"
    ]
    assert tombstone_events == []


def test_fakeT3_backend_never_emits_tombstone_log(monkeypatch) -> None:
    """The existing _FakeT3-based suite (tests/test_corpus.py) never has
    the ambiguity in the first place -- confirm probe_collection_state's
    isinstance gate keeps it silent for a non-HttpVectorClient backend."""
    from tests.test_corpus import _FakeT3

    legacy = "knowledge__art"
    t3 = _FakeT3({legacy})  # promoted absent, legacy present

    with capture_logs() as cap:
        result = t3_collection_name(legacy, t3=t3)

    assert result == legacy
    assert cap == []
