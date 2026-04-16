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
