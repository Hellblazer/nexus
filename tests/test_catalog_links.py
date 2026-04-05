# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog, CatalogEntry, CatalogLink
from nexus.catalog.tumbler import Tumbler


def _make_catalog_with_docs(tmp_path: Path) -> tuple[Catalog, Tumbler, Tumbler, Tumbler]:
    """Create catalog with owner and 3 documents."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    doc_a = cat.register(owner, "auth.py", content_type="code", file_path="auth.py")
    doc_b = cat.register(owner, "db.py", content_type="code", file_path="db.py")
    doc_c = cat.register(owner, "api.py", content_type="code", file_path="api.py")
    return cat, doc_a, doc_b, doc_c


class TestLinkCreation:
    def test_link_and_lookup(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        outgoing = cat.links_from(doc_a)
        assert len(outgoing) == 1
        assert outgoing[0].link_type == "cites"
        assert outgoing[0].from_tumbler == doc_a
        assert outgoing[0].to_tumbler == doc_b

    def test_link_persists_to_jsonl(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        jsonl = (tmp_path / "catalog" / "links.jsonl").read_text()
        records = [json.loads(line) for line in jsonl.strip().splitlines()]
        assert len(records) == 1
        assert records[0]["link_type"] == "cites"

    def test_multiple_link_types(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_b, "implements", created_by="index_hook")
        outgoing = cat.links_from(doc_a)
        assert len(outgoing) == 2
        types = {lnk.link_type for lnk in outgoing}
        assert types == {"cites", "implements"}

    def test_created_by_tracking(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        link = cat.links_from(doc_a)[0]
        assert link.created_by == "bib_enricher"

    def test_span_info(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "quotes", created_by="user", from_span="3-7", to_span="1-2")
        link = cat.links_from(doc_a)[0]
        assert link.from_span == "3-7"
        assert link.to_span == "1-2"


class TestBacklinks:
    def test_links_to(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        incoming = cat.links_to(doc_b)
        assert len(incoming) == 1
        assert incoming[0].from_tumbler == doc_a

    def test_links_to_type_filter(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_c, doc_b, "implements", created_by="user")
        incoming = cat.links_to(doc_b, link_type="cites")
        assert len(incoming) == 1
        assert incoming[0].from_tumbler == doc_a


class TestUnlink:
    def test_unlink_specific_type(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        removed = cat.unlink(doc_a, doc_b, "cites")
        assert removed == 1
        assert cat.links_from(doc_a) == []

    def test_unlink_tombstone_in_jsonl(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.unlink(doc_a, doc_b, "cites")
        content = (tmp_path / "catalog" / "links.jsonl").read_text()
        assert '"_deleted": true' in content

    def test_unlink_all_types(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_b, "implements", created_by="user")
        removed = cat.unlink(doc_a, doc_b)
        assert removed == 2
        assert cat.links_from(doc_a) == []

    def test_unlink_nonexistent_returns_zero(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        removed = cat.unlink(doc_a, doc_b, "cites")
        assert removed == 0


class TestGraphTraversal:
    def test_depth_1(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "relates", created_by="user")
        result = cat.graph(doc_a, depth=1)
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_b) in node_tumblers
        assert str(doc_c) in node_tumblers
        assert len(result["edges"]) == 2

    def test_depth_2(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_b, doc_c, "implements", created_by="user")
        result = cat.graph(doc_a, depth=2)
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_b) in node_tumblers
        assert str(doc_c) in node_tumblers

    def test_no_cycles(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "relates", created_by="user")
        cat.link(doc_b, doc_a, "relates", created_by="user")
        result = cat.graph(doc_a, depth=3)
        # Should not loop — visited set prevents infinite traversal
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_b) in node_tumblers
        # Edges deduplicated — exactly 2 distinct directed edges
        assert len(result["edges"]) == 2

    def test_type_filter(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "implements", created_by="user")
        result = cat.graph(doc_a, depth=1, link_type="cites")
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_b) in node_tumblers
        assert str(doc_c) not in node_tumblers

    def test_direction_out(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_c, doc_a, "relates", created_by="user")
        result = cat.graph(doc_a, depth=1, direction="out")
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_b) in node_tumblers
        assert str(doc_c) not in node_tumblers

    def test_direction_in(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_c, doc_a, "relates", created_by="user")
        result = cat.graph(doc_a, depth=1, direction="in")
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_c) in node_tumblers
        assert str(doc_b) not in node_tumblers
