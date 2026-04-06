# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import pytest

import chromadb

from nexus.catalog.catalog import Catalog, CatalogEntry, _SPAN_PATTERN
from nexus.catalog.tumbler import Tumbler


def _make_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    return Catalog(catalog_dir, catalog_dir / ".catalog.db")


class TestRegisterOwner:
    def test_first_owner(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        assert str(owner) == "1.1"

    def test_second_owner(self, tmp_path):
        cat = _make_catalog(tmp_path)
        cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        owner2 = cat.register_owner("arcaneum", "repo", repo_hash="aabb1122")
        assert str(owner2) == "1.2"

    def test_owner_persists_to_jsonl(self, tmp_path):
        cat = _make_catalog(tmp_path)
        cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        jsonl = (tmp_path / "catalog" / "owners.jsonl").read_text()
        records = [json.loads(line) for line in jsonl.strip().splitlines()]
        assert len(records) == 1
        assert records[0]["name"] == "nexus"

    def test_owner_for_repo_lookup(self, tmp_path):
        cat = _make_catalog(tmp_path)
        cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        found = cat.owner_for_repo("571b8edd")
        assert found is not None
        assert str(found) == "1.1"

    def test_owner_for_repo_not_found(self, tmp_path):
        cat = _make_catalog(tmp_path)
        assert cat.owner_for_repo("nonexistent") is None

    def test_curator_owner(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("hal-research", "curator")
        assert str(owner) == "1.1"


class TestRegisterDocument:
    def test_first_document(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(
            owner, "indexer.py",
            content_type="code",
            file_path="src/nexus/indexer.py",
            physical_collection="code__nexus",
            chunk_count=10,
        )
        assert str(doc) == "1.1.1"

    def test_auto_increment(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc2 = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        assert str(doc2) == "1.1.2"

    def test_resolve(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(
            owner, "indexer.py",
            content_type="code",
            file_path="src/nexus/indexer.py",
            physical_collection="code__nexus",
            chunk_count=10,
        )
        entry = cat.resolve(doc)
        assert entry is not None
        assert entry.title == "indexer.py"
        assert entry.tumbler == doc
        assert entry.content_type == "code"

    def test_document_persists_to_jsonl(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        jsonl = (tmp_path / "catalog" / "documents.jsonl").read_text()
        records = [json.loads(line) for line in jsonl.strip().splitlines()]
        assert len(records) == 1
        assert records[0]["title"] == "a.py"


class TestGhostElement:
    def test_ghost_with_empty_collection(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("hal-research", "curator")
        ghost = cat.register(owner, "Future Paper", content_type="paper", physical_collection="")
        entry = cat.resolve(ghost)
        assert entry is not None
        assert entry.chunk_count == 0
        assert entry.physical_collection == ""

    def test_ghost_with_zero_chunks(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("hal-research", "curator")
        ghost = cat.register(owner, "Placeholder", content_type="knowledge", chunk_count=0)
        entry = cat.resolve(ghost)
        assert entry.chunk_count == 0


class TestIdempotency:
    def test_register_same_file_path_returns_existing(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc1 = cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        doc2 = cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        assert doc1 == doc2

    def test_idempotent_does_not_duplicate_jsonl(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        jsonl = (tmp_path / "catalog" / "documents.jsonl").read_text()
        records = [json.loads(line) for line in jsonl.strip().splitlines()]
        assert len(records) == 1


class TestUpdate:
    def test_update_head_hash(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="src/a.py", head_hash="aaa")
        cat.update(doc, head_hash="bbb")
        entry = cat.resolve(doc)
        assert entry.head_hash == "bbb"

    def test_update_preserves_tumbler(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        cat.update(doc, chunk_count=42)
        entry = cat.resolve(doc)
        assert entry.tumbler == doc

    def test_update_merges_meta(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="knowledge", meta={"doc_id": "abc123"})
        cat.update(doc, meta={"venue": "NeurIPS", "year_enriched": 2017})
        entry = cat.resolve(doc)
        # Both original and new keys should be present
        assert entry.meta["doc_id"] == "abc123"
        assert entry.meta["venue"] == "NeurIPS"


    def test_update_missing_tumbler_raises(self, tmp_path):
        cat = _make_catalog(tmp_path)
        with pytest.raises(KeyError):
            cat.update(Tumbler.parse("1.1.999"), title="x")


class TestEnsureConsistent:
    def test_malformed_jsonl_does_not_crash_constructor(self, tmp_path):
        """Corrupted JSONL at startup should not raise — catalog degrades gracefully."""
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir(parents=True)
        (catalog_dir / "owners.jsonl").write_text("NOT-JSON\n")
        (catalog_dir / "documents.jsonl").touch()
        (catalog_dir / "links.jsonl").touch()
        # Should not raise
        cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
        assert cat.all_documents() == []


class TestCompactReturn:
    def test_compact_returns_removed_counts(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.update(doc, head_hash="new")  # adds second JSONL line for same doc
        removed = cat.compact()
        assert "documents.jsonl" in removed
        assert removed["documents.jsonl"] >= 1  # at least one overwrite removed


class TestTumblerPermanence:
    def test_deleted_tumbler_not_reused(self, tmp_path):
        """Tumbler numbers must never be reused after deletion + compact."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc1 = cat.register(owner, "first.py", content_type="code", file_path="first.py")
        assert str(doc1) == "1.1.1"
        cat.delete_document(doc1)
        cat.compact()
        # After delete+compact, next doc should be 1.1.2, NOT 1.1.1
        doc2 = cat.register(owner, "second.py", content_type="code", file_path="second.py")
        assert str(doc2) == "1.1.2"

    def test_defrag_preserves_tombstones(self, tmp_path):
        """defrag() keeps tombstones; compact() removes them."""
        import json as _json
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.delete_document(doc)
        cat.defrag()
        # Tombstone should still be in JSONL
        content = (tmp_path / "catalog" / "documents.jsonl").read_text()
        lines = [_json.loads(l) for l in content.strip().splitlines()]
        assert any(l.get("_deleted") for l in lines)

    def test_compact_removes_tombstones(self, tmp_path):
        import json as _json
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.delete_document(doc)
        cat.compact()
        content = (tmp_path / "catalog" / "documents.jsonl").read_text()
        assert content.strip() == ""  # tombstone removed, no live records

    def test_content_hash_dedup(self, tmp_path):
        """Same owner + title + head_hash → returns existing tumbler."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc1 = cat.register(owner, "paper", content_type="paper", head_hash="deadbeef")
        doc2 = cat.register(owner, "paper", content_type="paper", head_hash="deadbeef")
        assert doc1 == doc2  # same tumbler


class TestSpanValidation:
    def test_valid_line_span(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        cat.link(doc_a, doc_b, "quotes", created_by="user", from_span="10-20", to_span="42-57")
        links = cat.links_from(doc_a)
        assert links[0].from_span == "10-20"
        assert links[0].to_span == "42-57"

    def test_valid_chunk_span(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        cat.link(doc_a, doc_b, "quotes", created_by="user", to_span="3:100-250")
        links = cat.links_from(doc_a)
        assert links[0].to_span == "3:100-250"

    def test_invalid_span_rejected(self, tmp_path):
        import pytest
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        with pytest.raises(ValueError, match="invalid from_span"):
            cat.link(doc_a, doc_b, "quotes", created_by="user", from_span="garbage")


class TestFind:
    def test_find_by_title(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "authentication module", content_type="code", file_path="auth.py")
        cat.register(owner, "database schema", content_type="code", file_path="db.py")
        results = cat.find("authentication")
        assert len(results) == 1
        assert results[0].title == "authentication module"

    def test_find_with_content_type(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "auth module", content_type="code", file_path="auth.py")
        cat.register(owner, "auth design", content_type="rdr", file_path="auth.md")
        results = cat.find("auth", content_type="rdr")
        assert len(results) == 1
        assert results[0].content_type == "rdr"


class TestByFilePath:
    def test_lookup(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "indexer.py", content_type="code", file_path="src/nexus/indexer.py")
        entry = cat.by_file_path(owner, "src/nexus/indexer.py")
        assert entry is not None
        assert entry.title == "indexer.py"

    def test_not_found(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        assert cat.by_file_path(owner, "nonexistent.py") is None


class TestByOwner:
    def test_list_all_for_owner(self, tmp_path):
        cat = _make_catalog(tmp_path)
        o1 = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        o2 = cat.register_owner("arcaneum", "repo", repo_hash="aabb1122")
        cat.register(o1, "a.py", content_type="code", file_path="a.py")
        cat.register(o1, "b.py", content_type="code", file_path="b.py")
        cat.register(o2, "c.py", content_type="code", file_path="c.py")
        entries = cat.by_owner(o1)
        assert len(entries) == 2


class TestDeleteDocument:
    def test_delete_document_resolve_returns_none(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        assert cat.delete_document(doc) is True
        assert cat.resolve(doc) is None

    def test_delete_document_links_preserved(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.delete_document(doc_a)
        # Links should still be queryable (RF-9: orphaned links preserved)
        links = cat.links_from(doc_a)
        assert len(links) == 1
        assert links[0].link_type == "cites"

    def test_delete_document_jsonl_tombstone(self, tmp_path):
        import json
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.delete_document(doc)
        content = (tmp_path / "catalog" / "documents.jsonl").read_text()
        lines = [json.loads(l) for l in content.strip().splitlines()]
        tombstone = [l for l in lines if l.get("_deleted")]
        assert len(tombstone) == 1
        assert tombstone[0]["tumbler"] == str(doc)

    def test_delete_document_rebuild_excludes(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.delete_document(doc)
        cat.rebuild()
        assert cat.resolve(doc) is None

    def test_delete_document_not_found_returns_false(self, tmp_path):
        cat = _make_catalog(tmp_path)
        assert cat.delete_document(Tumbler.parse("1.1.999")) is False

    def test_delete_document_fts_index_updated(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "authentication module", content_type="code", file_path="auth.py")
        cat.delete_document(doc)
        results = cat.find("authentication")
        assert len(results) == 0


class TestDescendants:
    def test_descendants_of_owner(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.register(owner, "b.py", content_type="code", file_path="b.py")
        results = cat.descendants("1.1")
        assert len(results) == 2

    def test_descendants_excludes_prefix_itself(self, tmp_path):
        """The prefix owner '1.1' should not appear in its own descendants."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        tumblers = [r["tumbler"] for r in cat.descendants("1.1")]
        assert "1.1" not in tumblers

    def test_descendants_of_store(self, tmp_path):
        cat = _make_catalog(tmp_path)
        o1 = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        o2 = cat.register_owner("arcaneum", "repo", repo_hash="aabb1122")
        cat.register(o1, "a.py", content_type="code", file_path="a.py")
        cat.register(o2, "b.py", content_type="code", file_path="b.py")
        results = cat.descendants("1")
        assert len(results) == 2

    def test_descendants_empty(self, tmp_path):
        cat = _make_catalog(tmp_path)
        cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        results = cat.descendants("1.1")
        assert results == []


class TestResolveChunk:
    def test_resolve_chunk_parses_document_prefix(self, tmp_path):
        """resolve_chunk extracts doc tumbler + chunk index from 4-segment address."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="a.py",
                     physical_collection="code__nexus", chunk_count=5)
        result = cat.resolve_chunk(Tumbler.parse("1.1.1.3"))
        assert result is not None
        assert result["document_tumbler"] == "1.1.1"
        assert result["chunk_index"] == 3
        assert result["physical_collection"] == "code__nexus"

    def test_resolve_chunk_not_a_chunk(self, tmp_path):
        """3-segment tumbler is a document, not a chunk — returns None."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        assert cat.resolve_chunk(Tumbler.parse("1.1.1")) is None

    def test_resolve_chunk_document_not_found(self, tmp_path):
        """Chunk of a non-existent document returns None."""
        cat = _make_catalog(tmp_path)
        assert cat.resolve_chunk(Tumbler.parse("1.1.999.3")) is None

    def test_resolve_chunk_out_of_range(self, tmp_path):
        """Chunk index beyond chunk_count returns None."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(owner, "a.py", content_type="code", file_path="a.py",
                     physical_collection="code__nexus", chunk_count=5)
        assert cat.resolve_chunk(Tumbler.parse("1.1.1.10")) is None


class TestLinkAuditStaleSpans:
    def test_stale_span_detected(self, tmp_path):
        """Links with spans to re-indexed docs appear in stale_spans."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        # Create link with span, backdate it
        cat.link(doc_a, doc_b, "quotes", created_by="user", from_span="10-20")
        cat._db.execute(
            "UPDATE links SET created_at = '2020-01-01T00:00:00Z' WHERE from_tumbler = ?",
            (str(doc_a),),
        )
        cat._db.commit()
        # Re-index doc_a (update indexed_at to now)
        cat.update(doc_a, head_hash="new-hash")
        audit = cat.link_audit()
        assert audit["stale_span_count"] >= 1
        assert any(s["from"] == str(doc_a) for s in audit["stale_spans"])

    def test_no_stale_span_when_fresh(self, tmp_path):
        """Links created after indexing have no stale spans."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        cat.link(doc_a, doc_b, "quotes", created_by="user", from_span="10-20")
        audit = cat.link_audit()
        assert audit["stale_span_count"] == 0


class TestRebuild:
    def test_rebuild_from_jsonl(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")

        # Create fresh Catalog pointing at same dir — simulates restart
        cat2 = Catalog(tmp_path / "catalog", tmp_path / "catalog" / ".catalog.db2")
        cat2.rebuild()
        entry = cat2.resolve(doc)
        assert entry is not None
        assert entry.title == "a.py"

    def test_rebuild_excludes_tombstoned_documents(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")

        # Manually write tombstone to documents.jsonl
        import json
        tombstone = {"tumbler": str(doc), "_deleted": True, "title": "", "author": "",
                     "year": 0, "content_type": "", "file_path": "a.py", "corpus": "",
                     "physical_collection": "", "chunk_count": 0, "head_hash": "",
                     "indexed_at": "", "meta": {}}
        with (tmp_path / "catalog" / "documents.jsonl").open("a") as f:
            f.write(json.dumps(tombstone) + "\n")

        cat2 = Catalog(tmp_path / "catalog", tmp_path / "catalog" / ".catalog.db2")
        cat2.rebuild()
        assert cat2.resolve(doc) is None


# ── nexus-f2vp: _ensure_consistent sets degraded flag on failure ─────────


class TestEnsureConsistentDegradedFlag:
    """Catalog must surface rebuild failures, not silently serve stale data."""

    def test_degraded_false_on_success(self, tmp_path):
        cat = _make_catalog(tmp_path)
        assert cat.degraded is False

    def test_degraded_true_on_rebuild_failure(self, tmp_path):
        """Rebuild failure (e.g. disk full, SQLite corruption) sets degraded flag."""
        from unittest.mock import patch

        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        (catalog_dir / "documents.jsonl").write_text("{}\n")

        with patch("nexus.catalog.catalog.CatalogDB.rebuild", side_effect=RuntimeError("disk full")):
            cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")

        assert cat.degraded is True


# ── nexus-l8hp: _SPAN_PATTERN chash support ─────────────────────────────────


class TestSpanPattern:
    """_SPAN_PATTERN must accept chash:<sha256hex> in addition to legacy formats."""

    def test_chash_valid(self):
        assert _SPAN_PATTERN.match("chash:" + "a" * 64) is not None

    def test_chash_too_short(self):
        assert _SPAN_PATTERN.match("chash:" + "a" * 63) is None

    def test_chash_too_long(self):
        assert _SPAN_PATTERN.match("chash:" + "a" * 65) is None

    def test_chash_uppercase_rejected(self):
        assert _SPAN_PATTERN.match("chash:" + "A" * 64) is None

    def test_chash_non_hex_rejected(self):
        assert _SPAN_PATTERN.match("chash:" + "g" * 64) is None

    def test_legacy_empty_still_matches(self):
        assert _SPAN_PATTERN.match("") is not None

    def test_legacy_line_range_still_matches(self):
        assert _SPAN_PATTERN.match("42-57") is not None

    def test_legacy_chunk_char_range_still_matches(self):
        assert _SPAN_PATTERN.match("3:100-250") is not None


# ── nexus-l8hp: resolve_span() ───────────────────────────────────────────────


class TestResolveSpan:
    """resolve_span() resolves chash: spans via ChromaDB metadata query."""

    def test_resolve_chash_found(self, tmp_path):
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col_name = f"code__span_{tmp_path.name}"
        col = t3.create_collection(col_name)
        chunk_hash = "a" * 64
        col.add(
            ids=["id1"],
            documents=["hello world"],
            metadatas=[{"chunk_text_hash": chunk_hash, "source": "test.py"}],
        )
        result = cat.resolve_span(f"chash:{chunk_hash}", col_name, t3)
        assert result is not None
        assert result["chunk_text"] == "hello world"
        assert result["chunk_hash"] == chunk_hash
        assert result["metadata"]["source"] == "test.py"

    def test_resolve_chash_not_found(self, tmp_path):
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col_name = f"code__span_{tmp_path.name}"
        t3.create_collection(col_name)
        result = cat.resolve_span("chash:" + "b" * 64, col_name, t3)
        assert result is None

    def test_resolve_empty_span_returns_none(self, tmp_path):
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col_name = f"code__span_{tmp_path.name}"
        t3.create_collection(col_name)
        result = cat.resolve_span("", col_name, t3)
        assert result is None

    def test_resolve_legacy_span_returns_none(self, tmp_path):
        cat = _make_catalog(tmp_path)
        t3 = chromadb.EphemeralClient()
        col_name = f"code__span_{tmp_path.name}"
        t3.create_collection(col_name)
        result = cat.resolve_span("42-57", col_name, t3)
        assert result is None


# ── nexus-4v96: link() with chash: spans ─────────────────────────────────────


class TestLinkChashSpans:
    def test_link_with_chash_from_span(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        created = cat.link(doc_a, doc_b, "cites", "test-agent", from_span="chash:" + "a" * 64)
        assert created is True

    def test_link_with_chash_to_span(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        created = cat.link(doc_a, doc_b, "cites", "test-agent", to_span="chash:" + "b" * 64)
        assert created is True

    def test_link_with_chash_both_spans(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        created = cat.link(
            doc_a, doc_b, "cites", "test-agent",
            from_span="chash:" + "a" * 64, to_span="chash:" + "b" * 64,
        )
        assert created is True

    def test_link_rejects_invalid_chash_non_hex(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        with pytest.raises(ValueError, match="invalid"):
            cat.link(doc_a, doc_b, "cites", "test-agent", from_span="chash:" + "z" * 64)

    def test_link_rejects_invalid_chash_too_short(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        with pytest.raises(ValueError, match="invalid"):
            cat.link(doc_a, doc_b, "cites", "test-agent", from_span="chash:" + "a" * 63)

    def test_link_empty_spans_still_works(self, tmp_path):
        """Regression: document-to-document links with empty spans."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        doc_b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        created = cat.link(doc_a, doc_b, "cites", "test-agent")
        assert created is True
