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


# ---------------------------------------------------------------------------
# TestGraphCTESemantics — nexus-5p2ci.17
# Parity + semantic contract tests for the WITH RECURSIVE CTE replacement of
# the Python BFS in _LinkOps.graph().  Tests are named by the contract they
# lock.  Fixtures use Catalog directly (integration, no mocks except for cap
# patching via patch.object).
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_cat(tmp_path):
    """Linear chain: A→B→C (three nodes, two directed edges)."""
    d = tmp_path / "chain"
    d.mkdir()
    c = Catalog(d, d / ".catalog.db")
    o = c.register_owner("nexus", "repo", repo_hash="aabbccdd")
    a = c.register(o, "a.py", content_type="code", file_path="a.py")
    b = c.register(o, "b.py", content_type="code", file_path="b.py")
    cc = c.register(o, "c.py", content_type="code", file_path="c.py")
    c.link(a, b, "cites", created_by="user")
    c.link(b, cc, "cites", created_by="user")
    return c, a, b, cc


@pytest.fixture
def fan_cat(tmp_path):
    """Fan: A→B, A→C, A→D (one hub with three outbound edges)."""
    d = tmp_path / "fan"
    d.mkdir()
    c = Catalog(d, d / ".catalog.db")
    o = c.register_owner("nexus", "repo", repo_hash="11223344")
    a = c.register(o, "hub.py", content_type="code", file_path="hub.py")
    b = c.register(o, "spoke1.py", content_type="code", file_path="spoke1.py")
    cc = c.register(o, "spoke2.py", content_type="code", file_path="spoke2.py")
    dd = c.register(o, "spoke3.py", content_type="code", file_path="spoke3.py")
    c.link(a, b, "cites", created_by="user")
    c.link(a, cc, "relates", created_by="user")
    c.link(a, dd, "implements", created_by="user")
    return c, a, b, cc, dd


class TestGraphCTEParity:
    """Parity: CTE result == explicit expected set (under-cap graphs)."""

    def test_chain_depth1_exact_nodes(self, chain_cat):
        """A→B→C at depth=1 from A: exact nodes {A, B}."""
        c, a, b, cc = chain_cat
        r = c.graph(a, depth=1)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert node_tumblers == {str(a), str(b)}
        assert len(r["nodes"]) == 2

    def test_chain_depth1_exact_edges(self, chain_cat):
        """A→B→C at depth=1 from A: exactly 1 edge (A→B), not B→C (B is leaf)."""
        c, a, b, cc = chain_cat
        r = c.graph(a, depth=1)
        assert len(r["edges"]) == 1
        e = r["edges"][0]
        assert str(e.from_tumbler) == str(a)
        assert str(e.to_tumbler) == str(b)
        assert e.link_type == "cites"

    def test_chain_depth2_exact_nodes(self, chain_cat):
        """A→B→C at depth=2 from A: exact nodes {A, B, C}."""
        c, a, b, cc = chain_cat
        r = c.graph(a, depth=2)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert node_tumblers == {str(a), str(b), str(cc)}
        assert len(r["nodes"]) == 3

    def test_chain_depth2_exact_edges(self, chain_cat):
        """A→B→C at depth=2 from A: exactly 2 edges (A→B and B→C)."""
        c, a, b, cc = chain_cat
        r = c.graph(a, depth=2)
        assert len(r["edges"]) == 2
        edge_keys = {(str(e.from_tumbler), str(e.to_tumbler), e.link_type) for e in r["edges"]}
        assert (str(a), str(b), "cites") in edge_keys
        assert (str(b), str(cc), "cites") in edge_keys

    def test_fan_depth1_exact_nodes(self, fan_cat):
        """Fan A→{B,C,D} at depth=1: exact nodes {A, B, C, D}."""
        c, a, b, cc, dd = fan_cat
        r = c.graph(a, depth=1)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert node_tumblers == {str(a), str(b), str(cc), str(dd)}
        assert len(r["nodes"]) == 4

    def test_fan_depth1_exact_edge_count(self, fan_cat):
        """Fan A→{B,C,D} at depth=1: exactly 3 edges."""
        c, a, b, cc, dd = fan_cat
        r = c.graph(a, depth=1)
        assert len(r["edges"]) == 3

    def test_seed_always_in_nodes(self, chain_cat):
        """Seed tumbler is always in the returned node set."""
        c, a, b, cc = chain_cat
        r = c.graph(cc, depth=1)  # start from the tail
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(cc) in node_tumblers

    def test_isolated_seed_no_edges(self, tmp_path):
        """An isolated node with no links returns just itself and empty edges."""
        d = tmp_path / "iso"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="deadbeef")
        a = c.register(o, "lonely.py", content_type="code", file_path="lonely.py")
        r = c.graph(a, depth=1)
        assert len(r["nodes"]) == 1
        assert str(r["nodes"][0].tumbler) == str(a)
        assert r["edges"] == []


class TestGraphCTEDirection:
    """Direction filter: out/in/both exact node and edge membership."""

    @pytest.fixture
    def directed_cat(self, tmp_path):
        """A→B (outbound), C→A (inbound), A→C (outbound)."""
        d = tmp_path / "dir"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="cafebabe")
        a = c.register(o, "center.py", content_type="code", file_path="center.py")
        b = c.register(o, "out_only.py", content_type="code", file_path="out_only.py")
        cc = c.register(o, "in_only.py", content_type="code", file_path="in_only.py")
        # a→b (A has outbound to B), c→a (A has inbound from C), a→c (A has outbound to C)
        c.link(a, b, "cites", created_by="user")      # outbound from A
        c.link(cc, a, "relates", created_by="user")   # inbound to A
        c.link(a, cc, "implements", created_by="user")  # outbound from A to C too
        return c, a, b, cc

    def test_direction_out_nodes_exact(self, directed_cat):
        """direction='out' from A at depth=1: A, B, C (A has outbound to both)."""
        c, a, b, cc = directed_cat
        r = c.graph(a, depth=1, direction="out")
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(a) in node_tumblers
        assert str(b) in node_tumblers
        assert str(cc) in node_tumblers
        assert len(r["nodes"]) == 3

    def test_direction_in_nodes_exact(self, directed_cat):
        """direction='in' from A at depth=1: A and C (C→A is the inbound edge)."""
        c, a, b, cc = directed_cat
        r = c.graph(a, depth=1, direction="in")
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(a) in node_tumblers
        assert str(cc) in node_tumblers
        assert str(b) not in node_tumblers
        assert len(r["nodes"]) == 2

    def test_direction_out_edges_exact(self, directed_cat):
        """direction='out': edges must have from_tumbler=A (outbound from A only)."""
        c, a, b, cc = directed_cat
        r = c.graph(a, depth=1, direction="out")
        assert len(r["edges"]) == 2
        for e in r["edges"]:
            assert str(e.from_tumbler) == str(a)

    def test_direction_in_edges_exact(self, directed_cat):
        """direction='in': only the C→A inbound edge at depth=1."""
        c, a, b, cc = directed_cat
        r = c.graph(a, depth=1, direction="in")
        assert len(r["edges"]) == 1
        e = r["edges"][0]
        assert str(e.from_tumbler) == str(cc)
        assert str(e.to_tumbler) == str(a)

    def test_direction_both_includes_all(self, directed_cat):
        """direction='both' at depth=1 from A includes A, B, C; all 3 edges."""
        c, a, b, cc = directed_cat
        r = c.graph(a, depth=1, direction="both")
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert {str(a), str(b), str(cc)} == node_tumblers
        # 3 edges: A→B (cites), C→A (relates), A→C (implements)
        assert len(r["edges"]) == 3


class TestGraphCTENodeCap:
    """Node cap: len(nodes)==cap exactly, warning fires, no dangling edges."""

    def test_cap_truncates_to_limit(self, chain_cat):
        """Chain A→B→C with cap=2: exactly 2 nodes returned."""
        c, a, b, cc = chain_cat
        with patch.object(type(c), "_MAX_GRAPH_NODES", 2):
            r = c.graph(a, depth=2)
        assert len(r["nodes"]) == 2

    def test_cap_1_returns_seed_only(self, chain_cat):
        """Cap=1: only the seed node, no edges."""
        c, a, b, cc = chain_cat
        with patch.object(type(c), "_MAX_GRAPH_NODES", 1):
            r = c.graph(a, depth=2)
        assert len(r["nodes"]) == 1
        assert r["edges"] == []

    def test_cap_warning_fires(self, chain_cat):
        """Warning 'graph_node_limit' is logged when cap fires.

        Patches the module logger directly rather than capturing stdout —
        capsys does not reliably intercept structlog's module-bound stdout
        reference (code-review finding, 2026-06-03).
        """
        import nexus.catalog.catalog_links as cl_mod

        c, a, b, cc = chain_cat
        with patch.object(type(c), "_MAX_GRAPH_NODES", 1), \
                patch.object(cl_mod, "_log") as mock_log:
            c.graph(a, depth=2)
        assert mock_log.warning.call_count == 1
        assert mock_log.warning.call_args.args[0] == "graph_node_limit"

    def test_cap_strict_on_wide_fan(self, tmp_path):
        """STRICT cap on a high-degree hub: A→{B,C,D,E}, cap=2 returns
        exactly 2 nodes. The old BFS checked the cap before processing each
        node and so would overshoot by the hub's full out-degree (returning
        up to 5); the CTE's SQL LIMIT is a hard cap. This fixture (wide fan)
        is what distinguishes strict-cap from overshoot — a linear chain
        cannot (code-review finding, 2026-06-03)."""
        d = tmp_path / "fan"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="fan00001")
        a = c.register(o, "a.py", content_type="code", file_path="a.py")
        for name in ("b", "c", "d", "e"):
            nbr = c.register(o, f"{name}.py", content_type="code",
                             file_path=f"{name}.py")
            c.link(a, nbr, "relates", created_by="user")
        with patch.object(type(c), "_MAX_GRAPH_NODES", 2):
            r = c.graph(a, depth=1)
        assert len(r["nodes"]) == 2  # seed + 1, strict — NOT 1 + full fan
        # No dangling edges under the strict cap, even on a wide fan.
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        for edge in r["edges"]:
            assert str(edge.from_tumbler) in node_tumblers
            assert str(edge.to_tumbler) in node_tumblers

    def test_no_dangling_edges_after_cap(self, chain_cat):
        """No edge references a node not in the returned node set."""
        c, a, b, cc = chain_cat
        with patch.object(type(c), "_MAX_GRAPH_NODES", 2):
            r = c.graph(a, depth=2)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        for edge in r["edges"]:
            assert str(edge.from_tumbler) in node_tumblers
            assert str(edge.to_tumbler) in node_tumblers

    def test_cap_keeps_lowest_depth_nodes(self, chain_cat):
        """With cap=2, seed (depth 0) and immediate neighbor (depth 1) survive."""
        c, a, b, cc = chain_cat
        with patch.object(type(c), "_MAX_GRAPH_NODES", 2):
            r = c.graph(a, depth=2)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(a) in node_tumblers  # depth 0, always survives
        assert str(b) in node_tumblers  # depth 1, closer than C
        assert str(cc) not in node_tumblers  # depth 2, cut by cap


class TestGraphCTELinkTypeFilter:
    """link_types list vs link_type single vs heuristic exclusion."""

    @pytest.fixture
    def mixed_type_cat(self, tmp_path):
        """A→B (cites), A→C (implements-heuristic), A→D (relates)."""
        d = tmp_path / "mixed"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="feedcafe")
        a = c.register(o, "a.py", content_type="code", file_path="a.py")
        b = c.register(o, "b.py", content_type="code", file_path="b.py")
        cc = c.register(o, "c.py", content_type="code", file_path="c.py")
        dd = c.register(o, "d.py", content_type="code", file_path="d.py")
        c.link(a, b, "cites", created_by="user")
        c.link(a, cc, "implements-heuristic", created_by="index_hook")
        c.link(a, dd, "relates", created_by="user")
        return c, a, b, cc, dd

    def test_default_excludes_heuristic(self, mixed_type_cat):
        """Default (no link_type* args) excludes implements-heuristic."""
        c, a, b, cc, dd = mixed_type_cat
        r = c.graph(a, depth=1)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(b) in node_tumblers
        assert str(dd) in node_tumblers
        assert str(cc) not in node_tumblers  # heuristic excluded by default

    def test_include_heuristic_true_includes_all(self, mixed_type_cat):
        """include_heuristic=True includes implements-heuristic nodes."""
        c, a, b, cc, dd = mixed_type_cat
        r = c.graph(a, depth=1, include_heuristic=True)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(cc) in node_tumblers

    def test_link_type_single_filters(self, mixed_type_cat):
        """link_type='cites' returns only B (ignores relates and heuristic)."""
        c, a, b, cc, dd = mixed_type_cat
        r = c.graph(a, depth=1, link_type="cites")
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(b) in node_tumblers
        assert str(cc) not in node_tumblers
        assert str(dd) not in node_tumblers

    def test_link_types_list_overrides_link_type(self, mixed_type_cat):
        """link_types=['cites', 'relates'] takes precedence over link_type single."""
        c, a, b, cc, dd = mixed_type_cat
        r = c.graph(a, depth=1, link_type="cites", link_types=["cites", "relates"])
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(b) in node_tumblers   # via cites
        assert str(dd) in node_tumblers  # via relates
        assert str(cc) not in node_tumblers  # heuristic excluded

    def test_link_types_list_heuristic_excluded_by_default(self, mixed_type_cat):
        """Explicit link_types list that omits implements-heuristic excludes it."""
        c, a, b, cc, dd = mixed_type_cat
        r = c.graph(a, depth=1, link_types=["cites"])
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(cc) not in node_tumblers


class TestGraphCTECycleTermination:
    """Cyclic graphs terminate and return correct deduplicated results."""

    def test_cycle_terminates(self, tmp_path):
        """A→B→A cycle with depth=3 terminates without infinite loop."""
        d = tmp_path / "cycle"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="c0ffee11")
        a = c.register(o, "a.py", content_type="code", file_path="a.py")
        b = c.register(o, "b.py", content_type="code", file_path="b.py")
        c.link(a, b, "relates", created_by="user")
        c.link(b, a, "relates", created_by="user")
        r = c.graph(a, depth=3)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert node_tumblers == {str(a), str(b)}
        assert len(r["nodes"]) == 2

    def test_cycle_edges_deduplicated(self, tmp_path):
        """A↔B cycle: exactly 2 distinct edges (A→B and B→A), not 4."""
        d = tmp_path / "cycle2"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="beefdead")
        a = c.register(o, "a.py", content_type="code", file_path="a.py")
        b = c.register(o, "b.py", content_type="code", file_path="b.py")
        c.link(a, b, "relates", created_by="user")
        c.link(b, a, "relates", created_by="user")
        r = c.graph(a, depth=3)
        assert len(r["edges"]) == 2
        edge_keys = {(str(e.from_tumbler), str(e.to_tumbler), e.link_type) for e in r["edges"]}
        assert (str(a), str(b), "relates") in edge_keys
        assert (str(b), str(a), "relates") in edge_keys

    def test_two_cycle_terminates_at_max_depth(self, tmp_path):
        """A↔B two-cycle at depth=10 (max) terminates, returns exactly
        {A, B} and 2 edges — the UNION (tumbler, depth) dedup keeps the
        working table bounded across the full depth range."""
        d = tmp_path / "twocycle"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="a1b2c3d4")
        a = c.register(o, "a.py", content_type="code", file_path="a.py")
        b = c.register(o, "b.py", content_type="code", file_path="b.py")
        c.link(a, b, "relates", created_by="user")
        c.link(b, a, "relates", created_by="user")
        r = c.graph(a, depth=10)
        assert len(r["nodes"]) == 2  # exactly {a, b}
        assert len(r["edges"]) == 2  # exactly 2 edges, not duplicated

    def test_true_self_loop_terminates(self, tmp_path):
        """A genuine A→A self-link (the most degenerate cycle) terminates
        and returns just A with its single self-edge. UNION dedup on
        (tumbler, depth) prevents (A,0) re-expansion. Distinct code path
        from the two-cycle case — the prior test misnamed a two-cycle as a
        self-loop (review finding, 2026-06-03)."""
        d = tmp_path / "trueselfloop"
        d.mkdir()
        c = Catalog(d, d / ".catalog.db")
        o = c.register_owner("nexus", "repo", repo_hash="5e1f100b")
        a = c.register(o, "a.py", content_type="code", file_path="a.py")
        # A true self-link: same tumbler both ends, allowed via allow_dangling.
        c.link(a, a, "relates", created_by="user", allow_dangling=True)
        r = c.graph(a, depth=10)
        assert len(r["nodes"]) == 1  # exactly {a}
        # Self-edge appears once (a is non-leaf at depth 0 < 10).
        assert len(r["edges"]) == 1
        e = r["edges"][0]
        assert str(e.from_tumbler) == str(a)
        assert str(e.to_tumbler) == str(a)


class TestGraphCTEDepth:
    """Depth semantics: leaf nodes included in node set, edges from non-leaves only."""

    def test_depth1_leaf_nodes_no_edges_from_leaf(self, chain_cat):
        """B is at depth=1 (leaf at depth=1). B→C edge is NOT included."""
        c, a, b, cc = chain_cat
        r = c.graph(a, depth=1)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert str(b) in node_tumblers  # B is included as node
        # B→C edge not present (B is leaf at depth=1, its neighbors not fetched)
        edge_keys = {(str(e.from_tumbler), str(e.to_tumbler)) for e in r["edges"]}
        assert (str(b), str(cc)) not in edge_keys
        assert str(cc) not in node_tumblers  # C not reachable at depth=1

    def test_depth2_mid_node_edges_included(self, chain_cat):
        """B at depth=1 is non-leaf at depth=2; B→C edge IS included."""
        c, a, b, cc = chain_cat
        r = c.graph(a, depth=2)
        edge_keys = {(str(e.from_tumbler), str(e.to_tumbler)) for e in r["edges"]}
        assert (str(b), str(cc)) in edge_keys


class TestGraphCTEGraphMany:
    """graph_many() still dedupes across seeds; contract intact."""

    def test_graph_many_dedupes_nodes(self, chain_cat):
        """Calling graph_many with seeds [A, B] on A→B→C returns {A,B,C} at depth=2."""
        c, a, b, cc = chain_cat
        r = c.graph_many([a, b], depth=2)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        assert {str(a), str(b), str(cc)} == node_tumblers

    def test_graph_many_dedupes_edges(self, chain_cat):
        """graph_many([A, B], depth=1) returns each edge at most once."""
        c, a, b, cc = chain_cat
        r = c.graph_many([a, b], depth=1)
        edge_keys = [(str(e.from_tumbler), str(e.to_tumbler), e.link_type) for e in r["edges"]]
        assert len(edge_keys) == len(set(edge_keys))  # no duplicates

    def test_graph_many_no_dangling_edges(self, chain_cat):
        """No edge in graph_many result references a node not in the node set."""
        c, a, b, cc = chain_cat
        r = c.graph_many([a, b], depth=1)
        node_tumblers = {str(n.tumbler) for n in r["nodes"]}
        for edge in r["edges"]:
            assert str(edge.from_tumbler) in node_tumblers
            assert str(edge.to_tumbler) in node_tumblers
