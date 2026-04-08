# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler

GHOST, GHOST2 = Tumbler.parse("1.1.99"), Tumbler.parse("1.1.98")


@pytest.fixture
def cat(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    c = Catalog(d, d / ".catalog.db")
    o = c.register_owner("nexus", "repo", repo_hash="571b8edd")
    a = c.register(o, "auth.py", content_type="code", file_path="auth.py")
    b = c.register(o, "db.py", content_type="code", file_path="db.py")
    x = c.register(o, "api.py", content_type="code", file_path="api.py")
    return c, a, b, x


def _jsonl(tmp_path):
    return [json.loads(ln) for ln in (tmp_path / "catalog" / "links.jsonl").read_text().strip().splitlines()]


class TestDangling:
    @pytest.mark.parametrize("method", ["link", "link_if_absent"])
    @pytest.mark.parametrize("ft,tk,pat", [("ghost", "b", "from_tumbler.*not found"), ("a", "ghost", "to_tumbler.*not found")])
    def test_rejects(self, cat, method, ft, tk, pat):
        c, a, b, _ = cat
        with pytest.raises(ValueError, match=pat):
            getattr(c, method)(GHOST if ft == "ghost" else a, GHOST if tk == "ghost" else b, "cites", created_by="user")

    def test_both_dangling(self, cat):
        with pytest.raises(ValueError, match="not found"):
            cat[0].link_if_absent(GHOST2, GHOST, "cites", created_by="user")

    def test_allow_dangling_and_idempotent(self, cat):
        c, a, _, _ = cat
        c.link(a, GHOST, "cites", created_by="user", allow_dangling=True)
        assert len(c.links_from(a)) == 1
        assert c.link_if_absent(a, GHOST, "cites", created_by="user") is False
        assert c.link_if_absent(a, Tumbler.parse("1.1.97"), "cites", created_by="user", allow_dangling=True) is True

    def test_link_returns_bool(self, cat):
        c, a, b, _ = cat
        assert c.link(a, b, "cites", created_by="user") is True
        assert c.link(a, b, "cites", created_by="other") is False


class TestCreation:
    def test_lookup(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        out = c.links_from(a)
        assert len(out) == 1
        assert (out[0].link_type, out[0].from_tumbler, out[0].to_tumbler) == ("cites", a, b)

    def test_jsonl(self, cat, tmp_path):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        recs = _jsonl(tmp_path)
        assert len(recs) == 1 and recs[0]["link_type"] == "cites"

    def test_multiple_types(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, b, "implements", created_by="index_hook")
        assert {lk.link_type for lk in c.links_from(a)} == {"cites", "implements"}

    def test_created_by_and_span(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        assert c.links_from(a)[0].created_by == "bib_enricher"
        c.link(a, b, "quotes", created_by="user", from_span="3-7", to_span="1-2")
        lk = [lk for lk in c.links_from(a) if lk.link_type == "quotes"][0]
        assert (lk.from_span, lk.to_span) == ("3-7", "1-2")

    def test_backlinks(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(x, b, "implements", created_by="user")
        assert len(c.links_to(b)) == 2 and c.links_to(b, link_type="cites")[0].from_tumbler == a
        assert len(c.links_to(b, link_type="cites")) == 1


class TestUnlink:
    def test_specific_type(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        assert c.unlink(a, b, "cites") == 1 and c.links_from(a) == []

    def test_tombstone_and_provenance(self, cat, tmp_path):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        c.unlink(a, b, "cites")
        content = (tmp_path / "catalog" / "links.jsonl").read_text()
        assert '"_deleted": true' in content
        assert [r for r in _jsonl(tmp_path) if r.get("_deleted")][0]["created_by"] == "bib_enricher"

    def test_all_types(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, b, "implements", created_by="user")
        assert c.unlink(a, b) == 2 and c.links_from(a) == []

    def test_nonexistent(self, cat):
        assert cat[0].unlink(cat[1], cat[2], "cites") == 0


class TestIdempotent:
    def test_merges_co_discovered(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        c.link(a, b, "cites", created_by="user")
        lks = c.links_from(a, link_type="cites")
        assert len(lks) == 1 and lks[0].created_by == "bib_enricher"
        assert lks[0].meta.get("co_discovered_by") == ["user"]

    def test_same_creator_no_co_discovered(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, b, "cites", created_by="user")
        lks = c.links_from(a, link_type="cites")
        assert len(lks) == 1 and "user" not in lks[0].meta.get("co_discovered_by", [])

    @pytest.mark.parametrize("pre,expected", [(False, True), (True, False)])
    def test_if_absent_return(self, cat, pre, expected):
        c, a, b, _ = cat
        if pre:
            c.link(a, b, "cites", created_by="user")
        assert c.link_if_absent(a, b, "cites", created_by="other" if pre else "user") is expected
        assert len(c.links_from(a)) == 1

    def test_batch_id(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="bib_enricher", batch_id="run-123")
        assert c.links_from(a)[0].meta["batch_id"] == "run-123"


class TestQuery:
    @pytest.mark.parametrize("kw,count", [
        ({"link_type": "cites"}, 1), ({"link_type": "nonexistent"}, 0), ({"created_by": "bib_enricher"}, 1),
    ])
    def test_single_filter(self, cat, kw, count):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        c.link(a, x, "implements", created_by="user")
        assert len(c.link_query(**kw)) == count

    def test_combined(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        c.link(a, x, "cites", created_by="user")
        r = c.link_query(link_type="cites", created_by="bib_enricher")
        assert len(r) == 1 and r[0].to_tumbler == b

    def test_pagination(self, cat):
        c, a, b, x = cat
        for s, d in [(a, b), (a, x), (b, x)]:
            c.link(s, d, "cites", created_by="user")
        assert len(c.link_query(link_type="cites", limit=2, offset=0)) == 2
        assert len(c.link_query(link_type="cites", limit=2, offset=2)) == 1

    def test_no_filter_and_limit_zero(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, x, "relates", created_by="bib_enricher")
        c.link(b, x, "cites", created_by="user")
        assert len(c.link_query()) == 3
        assert len(c.link_query(limit=0)) == 3

    @pytest.mark.parametrize("direction,count", [("both", 2), ("out", 1)])
    def test_tumbler_direction(self, cat, direction, count):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(x, a, "relates", created_by="user")
        r = c.link_query(tumbler=str(a), direction=direction)
        assert len(r) == count
        if direction == "out":
            assert r[0].link_type == "cites"

    @pytest.mark.parametrize("cutoff,expected", [("2099-01-01T00:00:00", 2), ("2000-01-01T00:00:00", 0)])
    def test_created_at_before(self, cat, cutoff, expected):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, x, "cites", created_by="user")
        assert len(c.link_query(link_type="cites", created_at_before=cutoff)) == expected


class TestBulkUnlink:
    def test_by_created_by(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        c.link(a, x, "cites", created_by="user")
        assert c.bulk_unlink(created_by="bib_enricher") == 1
        assert len(c.link_query(created_by="bib_enricher")) == 0 and len(c.link_query(created_by="user")) == 1

    def test_by_type(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, x, "implements", created_by="user")
        assert c.bulk_unlink(link_type="cites") == 1 and len(c.link_query()) == 1

    def test_dry_run(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        assert c.bulk_unlink(link_type="cites", dry_run=True) == 1
        assert len(c.link_query(link_type="cites")) == 1

    @pytest.mark.parametrize("cutoff,removed,remaining", [("2099-01-01T00:00:00", 2, 0), ("2000-01-01T00:00:00", 0, 2)])
    def test_time_range(self, cat, cutoff, removed, remaining):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, x, "cites", created_by="user")
        assert c.bulk_unlink(link_type="cites", created_at_before=cutoff) == removed
        assert len(c.link_query(link_type="cites")) == remaining

    def test_no_filters_raises(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        with pytest.raises(ValueError, match="at least one filter"):
            c.bulk_unlink()

    def test_no_filters_dry_run(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        assert c.bulk_unlink(dry_run=True) == 1 and len(c.link_query()) == 1

    def test_tombstone_created_by(self, cat, tmp_path):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        c.bulk_unlink(created_by="bib_enricher")
        assert [r for r in _jsonl(tmp_path) if r.get("_deleted")][0]["created_by"] == "bib_enricher"


class TestValidateAndAudit:
    def test_validate_valid(self, cat):
        assert cat[0].validate_link(cat[1], cat[2], "cites") == []

    def test_validate_both_missing(self, cat):
        errs = cat[0].validate_link(GHOST, GHOST2, "cites")
        assert len(errs) == 2 and any("from_tumbler" in e for e in errs) and any("to_tumbler" in e for e in errs)

    def test_validate_deleted(self, cat):
        c, a, b, _ = cat
        c.delete_document(a)
        errs = c.validate_link(a, b, "cites")
        assert len(errs) == 1 and "from_tumbler" in errs[0]

    def test_validate_duplicate(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        errs = c.validate_link(a, b, "cites")
        assert len(errs) == 1 and "duplicate" in errs[0]

    def test_audit_empty(self, cat):
        r = cat[0].link_audit()
        assert r["total"] == 0 and r["orphaned_count"] == 0 and r["duplicate_count"] == 0

    def test_audit_stats(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="bib_enricher")
        c.link(a, x, "cites", created_by="user")
        c.link(b, x, "relates", created_by="user")
        r = c.link_audit()
        assert r["total"] == 3 and r["by_type"] == {"cites": 2, "relates": 1}
        assert r["by_creator"] == {"bib_enricher": 1, "user": 2}

    def test_audit_orphaned(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        c.delete_document(a)
        r = c.link_audit()
        assert r["orphaned_count"] == 1 and r["orphaned"][0]["from"] == str(a)

    def test_audit_no_dup_with_merge(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, b, "cites", created_by="other")
        assert c.link_audit()["duplicate_count"] == 0
        lks = c.links_from(a, link_type="cites")
        assert len(lks) == 1 and lks[0].meta.get("co_discovered_by") == ["other"]


class TestGraph:
    def test_starting_node(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        assert {str(a), str(b)} <= {str(e.tumbler) for e in c.graph(a, depth=1)["nodes"]}

    def test_isolated(self, cat):
        c, a, _, _ = cat
        r = c.graph(a, depth=1)
        assert len(r["nodes"]) == 1 and str(r["nodes"][0].tumbler) == str(a) and r["edges"] == []

    def test_depth_clamped(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user")
        assert ({str(n.tumbler) for n in c.graph(a, depth=100)["nodes"]}
                == {str(n.tumbler) for n in c.graph(a, depth=c._MAX_GRAPH_DEPTH)["nodes"]})

    def test_deleted_start(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "cites", created_by="user", allow_dangling=True)
        c.delete_document(a)
        nodes = {str(e.tumbler) for e in c.graph(a, depth=1)["nodes"]}
        assert str(a) not in nodes and str(b) in nodes

    def test_node_limit(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(b, x, "relates", created_by="user")
        with patch.object(type(c), "_MAX_GRAPH_NODES", 1):
            r = c.graph(a, depth=2)
            assert len(r["nodes"]) == 1 and r["edges"] == []

    @pytest.mark.parametrize("depth", [1, 2])
    def test_depth_n(self, cat, depth):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a if depth == 1 else b, x, "relates" if depth == 1 else "implements", created_by="user")
        nodes = {str(e.tumbler) for e in c.graph(a, depth=depth)["nodes"]}
        assert str(b) in nodes and str(x) in nodes
        if depth == 1:
            assert len(c.graph(a, depth=1)["edges"]) == 2

    def test_no_cycles(self, cat):
        c, a, b, _ = cat
        c.link(a, b, "relates", created_by="user")
        c.link(b, a, "relates", created_by="user")
        r = c.graph(a, depth=3)
        assert str(b) in {str(e.tumbler) for e in r["nodes"]} and len(r["edges"]) == 2

    def test_type_filter_and_direction(self, cat):
        c, a, b, x = cat
        c.link(a, b, "cites", created_by="user")
        c.link(a, x, "implements", created_by="user")
        c.link(x, a, "relates", created_by="user")
        filt = {str(e.tumbler) for e in c.graph(a, depth=1, link_type="cites")["nodes"]}
        assert str(b) in filt and str(x) not in filt
        out = {str(e.tumbler) for e in c.graph(a, depth=1, direction="out")["nodes"]}
        assert str(b) in out and str(x) in out
        inv = {str(e.tumbler) for e in c.graph(a, depth=1, direction="in")["nodes"]}
        assert str(x) in inv and str(b) not in inv
