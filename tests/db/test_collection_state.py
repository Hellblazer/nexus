# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-9n485 — three-state collection existence probe.

``collection_exists()`` conflates TOMBSTONED (soft-deleted) with ABSENT
(never existed) on the HttpVectorClient backend. These tests pin
``probe_collection_state``'s dispatch: real ``isinstance`` gate on
HttpVectorClient (never duck-typed, since MagicMock auto-vivifies any
attribute), degrading to a two-state PRESENT/ABSENT read for every other
backend.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.db.collection_state import CollectionState, probe_collection_state
from nexus.db.http_vector_client import HttpVectorClient


def _patch_get(monkeypatch, handler) -> list[str]:
    paths: list[str] = []

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        paths.append(path)
        return handler(path)

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    return paths


class TestProbeCollectionStateNonHttpBackend:
    """Any t3_db that is NOT a real HttpVectorClient degrades to 2-state."""

    def test_mock_exists_true_reads_present(self) -> None:
        fake = MagicMock()
        fake.collection_exists = MagicMock(return_value=True)
        assert probe_collection_state(fake, "any__coll") is CollectionState.PRESENT

    def test_mock_exists_false_reads_absent_not_tombstoned(self) -> None:
        fake = MagicMock()
        fake.collection_exists = MagicMock(return_value=False)
        assert probe_collection_state(fake, "any__coll") is CollectionState.ABSENT

    def test_unconfigured_mock_collection_probe_attr_is_never_consulted(self) -> None:
        """MagicMock auto-vivifies .collection_probe -- proving the dispatch
        does NOT duck-type on it (that would return a bare Mock, not a
        CollectionState, and corrupt every caller's equality check)."""
        fake = MagicMock()
        fake.collection_exists = MagicMock(return_value=False)
        # fake.collection_probe exists (auto-vivified) but must be ignored.
        result = probe_collection_state(fake, "any__coll")
        assert result is CollectionState.ABSENT
        fake.collection_probe.assert_not_called()

    def test_real_t3database_never_tombstoned(self, local_t3) -> None:
        local_t3.put(collection="knowledge__probe_helper_present", content="x", title="d.md")
        assert probe_collection_state(local_t3, "knowledge__probe_helper_present") is CollectionState.PRESENT
        assert probe_collection_state(local_t3, "knowledge__probe_helper_absent") is CollectionState.ABSENT


class TestProbeCollectionStateHttpVectorClient:
    """Real HttpVectorClient instance: full three-state dispatch."""

    def test_present_when_live_in_stats(self, monkeypatch) -> None:
        def handler(path: str) -> Any:
            if path == "/v1/vectors/stats":
                return [{"name": "live__coll", "dim": 384, "count": 1, "last_write": "x"}]
            if path == "/v1/vectors/collections":
                return [{"name": "live__coll"}]
            raise AssertionError(path)

        _patch_get(monkeypatch, handler)
        client = HttpVectorClient()
        assert probe_collection_state(client, "live__coll") is CollectionState.PRESENT

    def test_tombstoned_when_absent_from_stats_present_raw(self, monkeypatch) -> None:
        def handler(path: str) -> Any:
            if path == "/v1/vectors/stats":
                return []
            if path == "/v1/vectors/collections":
                return [{"name": "trashed__coll"}]
            raise AssertionError(path)

        _patch_get(monkeypatch, handler)
        client = HttpVectorClient()
        assert probe_collection_state(client, "trashed__coll") is CollectionState.TOMBSTONED

    def test_absent_when_missing_from_both(self, monkeypatch) -> None:
        def handler(path: str) -> Any:
            if path == "/v1/vectors/stats":
                return []
            if path == "/v1/vectors/collections":
                return []
            raise AssertionError(path)

        _patch_get(monkeypatch, handler)
        client = HttpVectorClient()
        assert probe_collection_state(client, "never__existed") is CollectionState.ABSENT
