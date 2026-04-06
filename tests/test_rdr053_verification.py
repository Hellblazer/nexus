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
        """chash span pointing to a missing chunk → stale."""
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
