# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for link_audit() chash verification against T3 (nexus-5arn, RDR-053 P2.4)."""

from __future__ import annotations

from pathlib import Path

import chromadb
import pytest

from nexus.catalog.catalog import Catalog


def _make_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    return Catalog(catalog_dir, catalog_dir / ".catalog.db")


HASH_A = "a" * 64
HASH_B = "b" * 64


def _col_name(tmp_path):
    """Unique collection name per test to avoid EphemeralClient cross-talk."""
    return f"code__{tmp_path.name}"


class TestLinkAuditChashVerification:
    def test_chash_span_resolvable(self, tmp_path):
        """chash span pointing to an existing chunk → not stale."""
        col_name = _col_name(tmp_path)
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col = t3.get_or_create_collection(col_name)
        col.add(
            ids=["chunk-1"],
            documents=["some chunk text"],
            metadatas=[{"chunk_text_hash": HASH_A}],
        )

        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
            physical_collection=col_name,
        )
        doc_b = cat.register(
            owner, "b.py", content_type="code", file_path="b.py",
            physical_collection=col_name,
        )
        cat.link(doc_a, doc_b, "cites", "test-agent", from_span=f"chash:{HASH_A}")

        result = cat.link_audit(t3=t3)
        assert result["stale_chash_count"] == 0
        assert result["stale_chash"] == []

    def test_chash_span_unresolvable(self, tmp_path):
        """chash span pointing to a missing chunk → stale with reason='missing'."""
        col_name = _col_name(tmp_path)
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        t3.get_or_create_collection(col_name)  # empty collection

        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
            physical_collection=col_name,
        )
        doc_b = cat.register(
            owner, "b.py", content_type="code", file_path="b.py",
            physical_collection=col_name,
        )
        cat.link(doc_a, doc_b, "cites", "test-agent", from_span=f"chash:{HASH_A}")

        result = cat.link_audit(t3=t3)
        assert result["stale_chash_count"] == 1
        assert result["stale_chash"][0]["span"] == f"chash:{HASH_A}"
        assert result["stale_chash"][0]["reason"] == "missing"

    def test_backward_compat_no_t3(self, tmp_path):
        """link_audit() without t3 returns all original keys + stale_chash=[]."""
        col_name = _col_name(tmp_path)
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
            physical_collection=col_name,
        )
        doc_b = cat.register(
            owner, "b.py", content_type="code", file_path="b.py",
            physical_collection=col_name,
        )
        cat.link(doc_a, doc_b, "cites", "test-agent", from_span=f"chash:{HASH_A}")

        result = cat.link_audit()
        # All original keys present
        for key in ("total", "by_type", "by_creator", "orphaned", "orphaned_count",
                     "duplicates", "duplicate_count", "stale_spans", "stale_span_count"):
            assert key in result
        # chash keys present but empty
        assert result["stale_chash"] == []
        assert result["stale_chash_count"] == 0

    def test_t3_none_explicit(self, tmp_path):
        """link_audit(t3=None) skips chash verification."""
        cat = _make_catalog(tmp_path)
        result = cat.link_audit(t3=None)
        assert result["stale_chash_count"] == 0
        assert result["stale_chash"] == []

    def test_chash_span_not_in_stale_spans_after_reindex(self, tmp_path):
        """chash: spans are excluded from stale_spans — they survive re-indexing."""
        col_name = _col_name(tmp_path)
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col = t3.get_or_create_collection(col_name)
        col.add(
            ids=["chunk-1"],
            documents=["some chunk text"],
            metadatas=[{"chunk_text_hash": HASH_A}],
        )

        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
            physical_collection=col_name,
        )
        doc_b = cat.register(
            owner, "b.py", content_type="code", file_path="b.py",
            physical_collection=col_name,
        )
        cat.link(doc_a, doc_b, "quotes", "test-agent", from_span=f"chash:{HASH_A}")

        # Backdate the link so it's older than the document's indexed_at
        cat._db.execute(
            "UPDATE links SET created_at = '2020-01-01T00:00:00Z' WHERE from_tumbler = ?",
            (str(doc_a),),
        )
        cat._db.commit()
        # Re-index (update indexed_at to now)
        cat.update(doc_a, head_hash="new-hash")

        result = cat.link_audit(t3=t3)
        # chash span should NOT appear in stale_spans (it survives re-indexing)
        assert result["stale_span_count"] == 0, \
            f"chash spans should be excluded from stale_spans: {result['stale_spans']}"
        # chash span should still resolve (not stale_chash either)
        assert result["stale_chash_count"] == 0


class TestStaleSpanToSide:
    def test_to_span_stale_detected(self, tmp_path):
        """Stale positional to_span is detected after re-indexing the target document."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
        )
        doc_b = cat.register(
            owner, "b.py", content_type="code", file_path="b.py",
        )
        cat.link(doc_a, doc_b, "quotes", "test-agent", to_span="10-20")

        # Backdate the link
        cat._db.execute(
            "UPDATE links SET created_at = '2020-01-01T00:00:00Z' "
            "WHERE from_tumbler = ?", (str(doc_a),),
        )
        cat._db.commit()
        # Re-index doc_b (the target)
        cat.update(doc_b, head_hash="new-hash")

        result = cat.link_audit()
        assert result["stale_span_count"] >= 1
        sides = [s["side"] for s in result["stale_spans"]]
        assert "to" in sides, f"to_span staleness should be detected: {result['stale_spans']}"


class TestResolveSpanText:
    def test_resolve_span_text_chash(self, tmp_path):
        """resolve_span_text() returns chunk text for chash: spans."""
        col_name = _col_name(tmp_path)
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col = t3.get_or_create_collection(col_name)
        chunk_text = "def hello(): pass"
        col.add(
            ids=["chunk-1"],
            documents=[chunk_text],
            metadatas=[{"chunk_text_hash": HASH_A}],
        )

        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
            physical_collection=col_name,
        )

        from unittest.mock import patch, MagicMock
        mock_t3 = MagicMock()
        mock_t3._client = t3
        with patch("nexus.db.make_t3", return_value=mock_t3):
            result = cat.resolve_span_text(doc, f"chash:{HASH_A}")
        assert result == chunk_text

    def test_resolve_span_text_chash_with_range(self, tmp_path):
        """resolve_span_text() returns sliced text for chash: span with char range."""
        col_name = _col_name(tmp_path)
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col = t3.get_or_create_collection(col_name)
        chunk_text = "def hello(): pass"
        col.add(
            ids=["chunk-1"],
            documents=[chunk_text],
            metadatas=[{"chunk_text_hash": HASH_A}],
        )

        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
            physical_collection=col_name,
        )

        from unittest.mock import patch, MagicMock
        mock_t3 = MagicMock()
        mock_t3._client = t3
        with patch("nexus.db.make_t3", return_value=mock_t3):
            result = cat.resolve_span_text(doc, f"chash:{HASH_A}:4-9")
        assert result == "hello"

    def test_resolve_span_text_chash_not_found(self, tmp_path):
        """resolve_span_text() returns None for missing chash: span."""
        col_name = _col_name(tmp_path)
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        t3.get_or_create_collection(col_name)

        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc = cat.register(
            owner, "a.py", content_type="code", file_path="a.py",
            physical_collection=col_name,
        )

        from unittest.mock import patch, MagicMock
        mock_t3 = MagicMock()
        mock_t3._client = t3
        with patch("nexus.db.make_t3", return_value=mock_t3):
            result = cat.resolve_span_text(doc, f"chash:{HASH_B}")
        assert result is None
