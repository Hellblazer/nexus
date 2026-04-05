# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog, CatalogEntry
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
