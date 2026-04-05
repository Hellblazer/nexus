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


class TestDanglingLinkPrevention:
    def test_link_rejects_dangling_from(self, tmp_path):
        cat, _, doc_b, _ = _make_catalog_with_docs(tmp_path)
        with pytest.raises(ValueError, match="from_tumbler.*not found"):
            cat.link(Tumbler.parse("1.1.99"), doc_b, "cites", created_by="user")

    def test_link_rejects_dangling_to(self, tmp_path):
        cat, doc_a, _, _ = _make_catalog_with_docs(tmp_path)
        with pytest.raises(ValueError, match="to_tumbler.*not found"):
            cat.link(doc_a, Tumbler.parse("1.1.99"), "cites", created_by="user")

    def test_link_allow_dangling_bypasses_check(self, tmp_path):
        cat, doc_a, _, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, Tumbler.parse("1.1.99"), "cites", created_by="user", allow_dangling=True)
        assert len(cat.links_from(doc_a)) == 1

    def test_link_if_absent_rejects_dangling_from(self, tmp_path):
        cat, _, doc_b, _ = _make_catalog_with_docs(tmp_path)
        with pytest.raises(ValueError, match="from_tumbler.*not found"):
            cat.link_if_absent(Tumbler.parse("1.1.99"), doc_b, "cites", created_by="user")

    def test_link_if_absent_rejects_dangling_to(self, tmp_path):
        cat, doc_a, _, _ = _make_catalog_with_docs(tmp_path)
        with pytest.raises(ValueError, match="to_tumbler.*not found"):
            cat.link_if_absent(doc_a, Tumbler.parse("1.1.99"), "cites", created_by="user")

    def test_link_if_absent_both_dangling(self, tmp_path):
        cat, _, _, _ = _make_catalog_with_docs(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            cat.link_if_absent(Tumbler.parse("1.1.98"), Tumbler.parse("1.1.99"),
                               "cites", created_by="user")

    def test_link_if_absent_existing_dangling_returns_false(self, tmp_path):
        """Existing dangling link returns False without re-validating endpoints."""
        cat, doc_a, _, _ = _make_catalog_with_docs(tmp_path)
        ghost = Tumbler.parse("1.1.99")
        cat.link_if_absent(doc_a, ghost, "cites", created_by="user", allow_dangling=True)
        result = cat.link_if_absent(doc_a, ghost, "cites", created_by="user")
        assert result is False

    def test_link_if_absent_allow_dangling(self, tmp_path):
        cat, doc_a, _, _ = _make_catalog_with_docs(tmp_path)
        result = cat.link_if_absent(doc_a, Tumbler.parse("1.1.99"), "cites",
                                    created_by="user", allow_dangling=True)
        assert result is True

    def test_link_returns_bool_created(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        result = cat.link(doc_a, doc_b, "cites", created_by="user")
        assert result is True  # new link

    def test_link_returns_bool_merged(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        result = cat.link(doc_a, doc_b, "cites", created_by="other")
        assert result is False  # merged


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


class TestIdempotentLink:
    def test_link_idempotent_merges_co_discovered_by(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        cat.link(doc_a, doc_b, "cites", created_by="user")
        links = cat.links_from(doc_a, link_type="cites")
        assert len(links) == 1
        assert links[0].created_by == "bib_enricher"  # original creator preserved
        assert links[0].meta.get("co_discovered_by") == ["user"]

    def test_link_idempotent_same_creator_no_co_discovered(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_b, "cites", created_by="user")
        links = cat.links_from(doc_a, link_type="cites")
        assert len(links) == 1
        assert "user" not in links[0].meta.get("co_discovered_by", [])

    def test_link_if_absent_returns_true_on_new(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        result = cat.link_if_absent(doc_a, doc_b, "cites", created_by="user")
        assert result is True
        assert len(cat.links_from(doc_a)) == 1

    def test_link_if_absent_returns_false_on_duplicate(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        result = cat.link_if_absent(doc_a, doc_b, "cites", created_by="other")
        assert result is False
        assert len(cat.links_from(doc_a)) == 1

    def test_batch_id_roundtrip(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher", batch_id="run-123")
        link = cat.links_from(doc_a)[0]
        assert link.meta["batch_id"] == "run-123"


class TestUnlinkProvenance:
    def test_unlink_tombstone_preserves_created_by(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        cat.unlink(doc_a, doc_b, "cites")
        content = (tmp_path / "catalog" / "links.jsonl").read_text()
        lines = [json.loads(l) for l in content.strip().splitlines()]
        tombstone = [l for l in lines if l.get("_deleted")][0]
        assert tombstone["created_by"] == "bib_enricher"


class TestLinkQuery:
    def test_link_query_by_type(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "implements", created_by="user")
        results = cat.link_query(link_type="cites")
        assert len(results) == 1
        assert results[0].link_type == "cites"

    def test_link_query_by_created_by(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        cat.link(doc_a, doc_c, "relates", created_by="user")
        results = cat.link_query(created_by="bib_enricher")
        assert len(results) == 1
        assert results[0].created_by == "bib_enricher"

    def test_link_query_combined_filters(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        results = cat.link_query(link_type="cites", created_by="bib_enricher")
        assert len(results) == 1
        assert results[0].to_tumbler == doc_b

    def test_link_query_pagination(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        cat.link(doc_b, doc_c, "cites", created_by="user")
        page1 = cat.link_query(link_type="cites", limit=2, offset=0)
        page2 = cat.link_query(link_type="cites", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 1

    def test_link_query_empty_returns_empty(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        results = cat.link_query(link_type="nonexistent")
        assert results == []

    def test_link_query_by_tumbler_both_directions(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_c, doc_a, "relates", created_by="user")
        results = cat.link_query(tumbler=str(doc_a), direction="both")
        assert len(results) == 2

    def test_link_query_created_at_before_sql(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        # Future cutoff: all links match
        results = cat.link_query(link_type="cites", created_at_before="2099-01-01T00:00:00")
        assert len(results) == 2
        # Past cutoff: no links match
        results = cat.link_query(link_type="cites", created_at_before="2000-01-01T00:00:00")
        assert len(results) == 0

    def test_link_query_no_filter_returns_all(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "relates", created_by="bib_enricher")
        results = cat.link_query()
        assert len(results) == 2

    def test_link_query_limit_zero_returns_all(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "relates", created_by="user")
        cat.link(doc_b, doc_c, "cites", created_by="user")
        results = cat.link_query(limit=0)
        assert len(results) == 3  # unlimited

    def test_link_query_by_tumbler_out_only(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_c, doc_a, "relates", created_by="user")
        results = cat.link_query(tumbler=str(doc_a), direction="out")
        assert len(results) == 1
        assert results[0].link_type == "cites"


class TestBulkUnlink:
    def test_bulk_unlink_by_created_by(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        removed = cat.bulk_unlink(created_by="bib_enricher")
        assert removed == 1
        assert len(cat.link_query(created_by="bib_enricher")) == 0
        assert len(cat.link_query(created_by="user")) == 1

    def test_bulk_unlink_by_type(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "implements", created_by="user")
        removed = cat.bulk_unlink(link_type="cites")
        assert removed == 1
        assert len(cat.link_query()) == 1

    def test_bulk_unlink_dry_run_no_delete(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        count = cat.bulk_unlink(link_type="cites", dry_run=True)
        assert count == 1
        assert len(cat.link_query(link_type="cites")) == 1  # still there

    def test_bulk_unlink_time_range(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        # All links created "now" — a future cutoff should match all
        removed = cat.bulk_unlink(link_type="cites", created_at_before="2099-01-01T00:00:00")
        assert removed == 2

    def test_bulk_unlink_no_filters_raises(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        with pytest.raises(ValueError, match="at least one filter"):
            cat.bulk_unlink()

    def test_bulk_unlink_no_filters_dry_run_allowed(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        count = cat.bulk_unlink(dry_run=True)
        assert count == 1  # counts all links
        assert len(cat.link_query()) == 1  # nothing deleted

    def test_bulk_unlink_time_range_excludes_past_cutoff(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        # A past cutoff should match nothing (links were created "now")
        removed = cat.bulk_unlink(link_type="cites", created_at_before="2000-01-01T00:00:00")
        assert removed == 0
        assert len(cat.link_query(link_type="cites")) == 2

    def test_bulk_unlink_tombstones_preserve_created_by(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        cat.bulk_unlink(created_by="bib_enricher")
        content = (tmp_path / "catalog" / "links.jsonl").read_text()
        lines = [json.loads(l) for l in content.strip().splitlines()]
        tombstone = [l for l in lines if l.get("_deleted")][0]
        assert tombstone["created_by"] == "bib_enricher"


class TestValidateLink:
    def test_validate_link_valid_returns_empty(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        errors = cat.validate_link(doc_a, doc_b, "cites")
        assert errors == []

    def test_validate_link_both_endpoints_missing(self, tmp_path):
        cat, _, _, _ = _make_catalog_with_docs(tmp_path)
        from nexus.catalog.tumbler import Tumbler
        errors = cat.validate_link(Tumbler.parse("1.1.99"), Tumbler.parse("1.1.98"), "cites")
        assert len(errors) == 2
        assert any("from_tumbler" in e for e in errors)
        assert any("to_tumbler" in e for e in errors)

    def test_validate_link_deleted_endpoint(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.delete_document(doc_a)
        errors = cat.validate_link(doc_a, doc_b, "cites")
        assert len(errors) == 1
        assert "from_tumbler" in errors[0]

    def test_validate_link_duplicate(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        errors = cat.validate_link(doc_a, doc_b, "cites")
        assert len(errors) == 1
        assert "duplicate" in errors[0]


class TestLinkAudit:
    def test_link_audit_empty_catalog(self, tmp_path):
        cat, _, _, _ = _make_catalog_with_docs(tmp_path)
        result = cat.link_audit()
        assert result["total"] == 0
        assert result["orphaned_count"] == 0
        assert result["duplicate_count"] == 0

    def test_link_audit_stats_by_type(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        cat.link(doc_b, doc_c, "relates", created_by="user")
        result = cat.link_audit()
        assert result["total"] == 3
        assert result["by_type"]["cites"] == 2
        assert result["by_type"]["relates"] == 1

    def test_link_audit_stats_by_creator(self, tmp_path):
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="bib_enricher")
        cat.link(doc_a, doc_c, "cites", created_by="user")
        result = cat.link_audit()
        assert result["by_creator"]["bib_enricher"] == 1
        assert result["by_creator"]["user"] == 1

    def test_link_audit_orphaned_after_delete_document(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.delete_document(doc_a)
        result = cat.link_audit()
        assert result["orphaned_count"] == 1
        assert result["orphaned"][0]["from"] == str(doc_a)

    def test_link_audit_no_duplicates_with_unique_constraint(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_a, doc_b, "cites", created_by="other")  # merge, not duplicate
        result = cat.link_audit()
        assert result["duplicate_count"] == 0
        # Verify merge actually happened
        links = cat.links_from(doc_a, link_type="cites")
        assert len(links) == 1
        assert links[0].meta.get("co_discovered_by") == ["other"]


class TestGraphTraversal:
    def test_graph_includes_starting_node(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        result = cat.graph(doc_a, depth=1)
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_a) in node_tumblers
        assert str(doc_b) in node_tumblers

    def test_graph_isolated_node(self, tmp_path):
        cat, doc_a, _, _ = _make_catalog_with_docs(tmp_path)
        result = cat.graph(doc_a, depth=1)
        assert len(result["nodes"]) == 1
        assert str(result["nodes"][0].tumbler) == str(doc_a)
        assert result["edges"] == []

    def test_graph_depth_clamped(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        # depth=100 is clamped to _MAX_GRAPH_DEPTH — verify same result as explicit max
        result_clamped = cat.graph(doc_a, depth=100)
        result_max = cat.graph(doc_a, depth=cat._MAX_GRAPH_DEPTH)
        assert {str(n.tumbler) for n in result_clamped["nodes"]} == \
               {str(n.tumbler) for n in result_max["nodes"]}

    def test_graph_deleted_starting_node(self, tmp_path):
        cat, doc_a, doc_b, _ = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user", allow_dangling=True)
        cat.delete_document(doc_a)
        result = cat.graph(doc_a, depth=1)
        node_tumblers = {str(e.tumbler) for e in result["nodes"]}
        assert str(doc_a) not in node_tumblers
        assert str(doc_b) in node_tumblers

    def test_graph_node_limit_truncates(self, tmp_path):
        from unittest.mock import patch
        cat, doc_a, doc_b, doc_c = _make_catalog_with_docs(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.link(doc_b, doc_c, "relates", created_by="user")
        # Cap at 1 — only starting node in visited, BFS cannot expand
        with patch.object(type(cat), '_MAX_GRAPH_NODES', 1):
            result = cat.graph(doc_a, depth=2)
            assert len(result["nodes"]) == 1  # only starting node
            assert result["edges"] == []  # no expansion happened

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
