# SPDX-License-Identifier: AGPL-3.0-or-later
"""SC-11: store_get_many 500-ID hydration no-truncation test.

Validates that store_get_many returns exactly N entries for N input IDs
with no silent truncation at the ChromaDB MAX_QUERY_RESULTS=300 boundary.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestStoreGetMany500ID:
    """SC-11: 500-ID hydration produces 500 contents with no truncation."""

    def test_500_id_hydration_no_truncation(self):
        """Pass 500 IDs to store_get_many and verify the returned
        contents list has exactly 500 entries — no silent quota truncation."""
        from nexus.mcp.core import store_get_many

        n = 500
        ids = [f"doc-{i:04d}" for i in range(n)]
        fake_docs = {
            f"doc-{i:04d}": {"content": f"content for document {i}"}
            for i in range(n)
        }

        # Mock T3 to return docs by ID.
        mock_t3 = MagicMock()

        def mock_get_by_id(col_name: str, doc_id: str):
            return fake_docs.get(doc_id)

        mock_t3.get_by_id = mock_get_by_id

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
            )

        assert isinstance(result, dict)
        assert "contents" in result
        assert "missing" in result
        assert len(result["contents"]) == n, (
            f"Expected {n} contents, got {len(result['contents'])}. "
            f"Silent truncation at ChromaDB quota boundary?"
        )
        assert len(result["missing"]) == 0
        # Verify no empty entries.
        assert all(c for c in result["contents"]), "Some contents are empty"

    def test_hydration_with_missing_ids(self):
        """IDs not found in T3 land in 'missing', not silently dropped."""
        from nexus.mcp.core import store_get_many

        ids = ["exists-1", "missing-1", "exists-2", "missing-2"]
        found = {"exists-1": {"content": "a"}, "exists-2": {"content": "b"}}

        mock_t3 = MagicMock()
        mock_t3.get_by_id = lambda col, doc_id: found.get(doc_id)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert len(result["contents"]) == 4  # 1:1 with input ids
        assert result["contents"][0] == "a"
        assert result["contents"][1] == ""  # missing → empty string
        assert result["contents"][2] == "b"
        assert result["contents"][3] == ""
        assert set(result["missing"]) == {"missing-1", "missing-2"}


class TestStoreGetManyBatchBoundary:
    """Verify store_get_many correctly handles IDs at and above the ChromaDB
    MAX_QUERY_RESULTS=300 boundary using a real per-document lookup.

    Uses a structured fake T3 to verify the per-ID dispatch loop doesn't
    truncate at 300, without requiring ChromaDB Cloud credentials.
    """

    def test_300_id_boundary_no_truncation(self):
        """301 IDs must all be returned — no off-by-one at the quota boundary."""
        from nexus.mcp.core import store_get_many

        n = 301  # one above the ChromaDB MAX_QUERY_RESULTS cap
        ids = [f"doc-{i:04d}" for i in range(n)]
        store = {doc_id: {"content": f"body-{i}"} for i, doc_id in enumerate(ids)}

        mock_t3 = MagicMock()
        mock_t3.get_by_id = lambda col, doc_id: store.get(doc_id)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert len(result["contents"]) == n, (
            f"Expected {n} contents at quota boundary, got {len(result['contents'])}"
        )
        assert len(result["missing"]) == 0

    def test_all_missing_above_boundary(self):
        """301 IDs that are all absent land in 'missing', not silently dropped."""
        from nexus.mcp.core import store_get_many

        n = 301
        ids = [f"absent-{i}" for i in range(n)]

        mock_t3 = MagicMock()
        mock_t3.get_by_id = lambda col, doc_id: None

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert len(result["contents"]) == n
        assert all(c == "" for c in result["contents"])
        assert len(result["missing"]) == n


class TestStoreGetManyLimitPerSource:
    """RDR-097 P1.0: ``limit_per_source`` truncation kwarg.

    Three input shapes:
      - ``None`` (default): no truncation; preserves existing behavior.
      - ``int``: truncate single-stream ``ids`` to first N.
      - ``list[int]``: pair with parallel-stream ``ids`` (``list[list[str]]``);
        truncate each stream to its corresponding limit, then flatten.
    """

    def _make_t3(self, store: dict[str, dict]):
        mock_t3 = MagicMock()
        mock_t3.get_by_id = lambda col, doc_id: store.get(doc_id)
        return mock_t3

    def test_limit_per_source_none_preserves_default(self):
        from nexus.mcp.core import store_get_many

        ids = [f"doc-{i}" for i in range(10)]
        store = {doc_id: {"content": f"body-{doc_id}"} for doc_id in ids}
        with patch("nexus.mcp.core._get_t3", return_value=self._make_t3(store)):
            result = store_get_many(
                ids=ids, collections="knowledge", structured=True
            )
        assert len(result["contents"]) == 10
        assert len(result["missing"]) == 0

    def test_limit_per_source_int_truncates_single_stream(self):
        from nexus.mcp.core import store_get_many

        ids = [f"doc-{i}" for i in range(20)]
        store = {doc_id: {"content": f"body-{doc_id}"} for doc_id in ids}
        mock_t3 = self._make_t3(store)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=5,
            )

        assert len(result["contents"]) == 5
        assert result["contents"] == [f"body-doc-{i}" for i in range(5)]
        assert len(result["missing"]) == 0

    def test_limit_per_source_zero_returns_empty(self):
        from nexus.mcp.core import store_get_many

        ids = [f"doc-{i}" for i in range(5)]
        store = {doc_id: {"content": f"body-{doc_id}"} for doc_id in ids}
        with patch("nexus.mcp.core._get_t3", return_value=self._make_t3(store)):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=0,
            )
        assert result["contents"] == []
        assert result["missing"] == []

    def test_limit_per_source_negative_raises_valueerror(self):
        from nexus.mcp.core import store_get_many

        ids = ["doc-1", "doc-2", "doc-3"]
        with patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=-1,
            )
        assert "error" in result
        assert "limit_per_source" in result["error"]
        assert "negative" in result["error"].lower() or "non-negative" in result["error"].lower()

    def test_limit_per_source_list_truncates_parallel_streams(self):
        from nexus.mcp.core import store_get_many

        stream_a = [f"a{i}" for i in range(4)]
        stream_b = [f"b{i}" for i in range(3)]
        store = {
            **{x: {"content": f"body-{x}"} for x in stream_a},
            **{x: {"content": f"body-{x}"} for x in stream_b},
        }
        mock_t3 = self._make_t3(store)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
                limit_per_source=[2, 1],
            )

        # Stream-major flatten: [a0, a1] then [b0]
        assert len(result["contents"]) == 3
        assert result["contents"] == ["body-a0", "body-a1", "body-b0"]
        assert result["missing"] == []

    def test_limit_per_source_list_with_single_stream_ids_raises(self):
        from nexus.mcp.core import store_get_many

        ids = ["doc-1", "doc-2", "doc-3"]
        with patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=[2],
            )
        assert "error" in result
        assert "parallel" in result["error"].lower()

    def test_limit_per_source_list_length_mismatch_raises_valueerror(self):
        from nexus.mcp.core import store_get_many

        stream_a = [f"a{i}" for i in range(4)]
        stream_b = [f"b{i}" for i in range(3)]
        with patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
                limit_per_source=[2],
            )
        assert "error" in result
        msg = result["error"].lower()
        assert "1" in result["error"] and "2" in result["error"]
        assert "length" in msg or "stream" in msg

    def test_limit_per_source_int_with_parallel_ids_broadcasts(self):
        from nexus.mcp.core import store_get_many

        stream_a = [f"a{i}" for i in range(4)]
        stream_b = [f"b{i}" for i in range(4)]
        store = {
            **{x: {"content": f"body-{x}"} for x in stream_a},
            **{x: {"content": f"body-{x}"} for x in stream_b},
        }
        with patch("nexus.mcp.core._get_t3", return_value=self._make_t3(store)):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
                limit_per_source=2,
            )

        assert len(result["contents"]) == 4
        assert result["contents"] == ["body-a0", "body-a1", "body-b0", "body-b1"]

    def test_parallel_ids_with_parallel_collections_aligns(self):
        from nexus.mcp.core import store_get_many

        stream_a = ["a1", "a2"]
        stream_b = ["b1", "b2"]
        store = {
            "a1": {"content": "body-a1"},
            "a2": {"content": "body-a2"},
            "b1": {"content": "body-b1"},
            "b2": {"content": "body-b2"},
        }

        # Track which collection each id was looked up in.
        lookups: list[tuple[str, str]] = []

        def stub_get(col_name: str, doc_id: str):
            lookups.append((col_name, doc_id))
            return store.get(doc_id)

        mock_t3 = MagicMock()
        mock_t3.get_by_id = stub_get

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections=["knowledge__alpha", "knowledge__beta"],
                structured=True,
            )

        assert len(result["contents"]) == 4
        # First two ids should be looked up in alpha, next two in beta.
        # Resolve t3_collection_name's effect on the names.
        from nexus.mcp.core import t3_collection_name
        alpha = t3_collection_name("knowledge__alpha")
        beta = t3_collection_name("knowledge__beta")
        assert lookups[0] == (alpha, "a1")
        assert lookups[1] == (alpha, "a2")
        assert lookups[2] == (beta, "b1")
        assert lookups[3] == (beta, "b2")

    def test_parallel_ids_with_scalar_collections_broadcasts(self):
        from nexus.mcp.core import store_get_many

        stream_a = ["a1"]
        stream_b = ["b1"]
        store = {
            "a1": {"content": "body-a1"},
            "b1": {"content": "body-b1"},
        }
        with patch("nexus.mcp.core._get_t3", return_value=self._make_t3(store)):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
            )
        assert len(result["contents"]) == 2
        assert result["contents"] == ["body-a1", "body-b1"]
