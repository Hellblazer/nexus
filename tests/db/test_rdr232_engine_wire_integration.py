# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 (nexus-jypmj.1.13, .6, .5): the three engine wire changes against the REAL engine.

Boots the gate jar on an ephemeral Postgres (the fixtures of ``test_http_telemetry_store_integration``) and
drives the real clients. The Java tests pin the engine's side of each change (``CatalogRepositoryTest``,
``CatalogHandlerTraverseScopeFilterTest``, ``CatalogHandlerManifestEnvelopeTest``,
``TelemetryHandlerOutOfStepCostTest``); the unit tests pin the client's side against fakes; this file pins that the
two agree on the wire: the flag the client reads is the flag the engine sends, the predicate the client builds is
accepted and answered with the marker the client requires, and a stored planner cost reads back as the number
the two-sided cost check expects.

Integration-marked: it boots PostgreSQL and the jar (``scripts/build-gate-jar.sh`` first), so it is out of the
default loop and runs in CI's integration job. It adds no ``t2_tenant`` case.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from tests.db.test_http_telemetry_store_integration import (  # noqa: F401 — fixtures, re-exported to this module
    _ALL_PREREQS,
    java_service,
    pg_service,
    tel_store,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ALL_PREREQS, reason="skipped: missing jar or PG binaries (scripts/build-gate-jar.sh)"),
]

RDR_COL = "rdr__7-9__voyage-context-3__v1"
CODE_COL = "code__7-9__voyage-code-3__v1"


@pytest.fixture
def catalog(java_service):  # noqa: F811
    base_url, token = java_service
    # The per-tenant once-per-process ghost sweep deletes a registered-but-chunkless collection on the first
    # request; burn it on a request that registers nothing (the Java handler tests do the same).
    httpx.get(f"{base_url}/v1/catalog/collections/list", headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
    cat = HttpCatalogClient(base_url=base_url, tenant="default", _token=token)
    yield cat
    cat.close()


def _register(cat: HttpCatalogClient, title: str, collection: str, content_type: str) -> str:
    out = cat._post("/doc/register", {
        "owner_prefix": "7.9", "title": title, "content_type": content_type,
        "source_uri": f"file:///it/{title}.md", "physical_collection": collection,
    })
    return out["tumbler"]


class TestTheFlagsTheClientReadsAreTheOnesTheEngineSends:
    def test_all_three_capabilities_read_true_from_the_real_version(self, catalog, tel_store):  # noqa: F811
        assert catalog.supports_graph_scope_filter() is True
        assert catalog.supports_manifest_row_limit() is True
        assert tel_store._supports_nx_answer_out_of_step_cost() is True

    def test_the_node_walk_capability_reads_true_from_the_real_version(self, catalog):
        assert catalog.supports_graph_node_walk() is True


class TestGraphScopeFilter:
    def _fixture(self, cat):
        seed = _register(cat, "seed", RDR_COL, "rdr")
        inside = _register(cat, "inside", RDR_COL, "rdr")
        outside = _register(cat, "outside", CODE_COL, "code")
        for to in (inside, outside):
            cat._post("/link", {"from_tumbler": seed, "to_tumbler": to, "link_type": "cites", "created_by": "it"})
        return seed, inside, outside

    def test_the_predicate_the_client_builds_is_applied_and_answered_with_the_marker(self, catalog):
        seed, inside, outside = self._fixture(catalog)

        got = catalog.graph(seed, link_types=["cites"], direction="out", scope_filter={"collections": [RDR_COL]})

        assert {str(n.tumbler) for n in got["nodes"]} == {seed, inside}
        assert got["scope_filter"]["applied"] is True and got["scope_filter"]["dropped_out_of_scope"] == 1
        assert outside not in {str(n.tumbler) for n in got["nodes"]}

    def test_without_a_predicate_the_reply_carries_no_marker(self, catalog):
        seed, inside, outside = self._fixture(catalog)

        got = catalog.graph(seed, link_types=["cites"], direction="out")

        assert {str(n.tumbler) for n in got["nodes"]} == {seed, inside, outside}
        assert "scope_filter" not in got

    def test_traverse_end_to_end_reports_the_prefilter_as_applied(self, catalog):
        from nexus.mcp.core import traverse

        seed, inside, outside = self._fixture(catalog)
        with patch("nexus.mcp.core._get_catalog", return_value=catalog):
            out = traverse(
                seeds=[seed], link_types=["cites"], direction="out",
                policy={"allowed_collections": RDR_COL},  # one full collection name: no registry read needed
            )

        assert out["traversal"]["scope_prefilter"] == "applied"
        assert out["traversal"]["engine_dropped_out_of_scope"] == 1
        assert set(out["tumblers"]) == {seed, inside}

    def test_a_malformed_predicate_is_a_400_from_the_engine(self, catalog):
        seed, _inside, _outside = self._fixture(catalog)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            catalog._post("/traverse", {"seeds": [seed], "scope_filter": {"collections": "rdr"}}, mutates=False)
        assert caught.value.response.status_code == 400


class TestNodeWalkEqualsTheClientsOwnScoring:
    """nexus-jypmj.18, the differential: the engine's ``node_walk`` for a scoped walk is what
    ``traversal_policy._distance_and_role`` computes over the COMPLETE edge set, which is what the unfiltered walk of
    the same graph returns (it is far under the 500-node cap). The Java tests pin hand-built cases and the unit tests
    pin the client against a double that re-implements the rule; this is the one place the two real implementations
    meet on the same graph."""

    LINKS = [  # (from, to, type): S1, S2 seeds; R1, R2 out-of-scope relays; the rest in scope
        ("S1", "R1", "implements"), ("R1", "A", "cites"), ("S1", "B", "relates"), ("S2", "B", "cites"),
        ("S2", "R2", "relates"), ("R2", "C", "implements"), ("R2", "D", "relates"), ("C", "E", "cites"),
        ("F", "S1", "implements"), ("A", "S1", "relates"), ("G", "R1", "cites"), ("S2", "H", "implements"),
        ("S2", "H", "quotes"), ("D", "I", "supersedes"), ("R1", "I", "implements"), ("S1", "J", "relates"),
    ]
    TYPES = ["implements", "cites", "relates", "quotes", "supersedes"]

    def _build(self, cat):
        names = sorted({n for frm, to, _ in self.LINKS for n in (frm, to)})
        tumbler = {
            n: _register(cat, f"diff-{n}", CODE_COL if n.startswith("R") else RDR_COL, "code" if n.startswith("R") else "rdr")
            for n in names
        }
        for frm, to, kind in self.LINKS:
            cat._post("/link", {"from_tumbler": tumbler[frm], "to_tumbler": tumbler[to], "link_type": kind, "created_by": "it"})
        return tumbler

    @pytest.mark.parametrize("direction", ["out", "in", "both"])
    def test_every_returned_node_carries_the_depth_and_role_the_client_computes_over_the_complete_edges(
        self, catalog, direction,
    ):
        from nexus.plans.traversal_policy import _distance_and_role

        t = self._build(catalog)
        seeds = [t["S1"], t["S2"]]
        full = catalog.graph_many(seeds, depth=3, direction=direction, link_types=self.TYPES)
        scoped = catalog.graph_many(
            seeds, depth=3, direction=direction, link_types=self.TYPES, scope_filter={"collections": [RDR_COL]},
        )

        assert len(full["nodes"]) < 100, "far under the engine's 500-node cap: the unfiltered edge set is complete"
        assert "node_walk" not in full, "a walk without the predicate carries no node_walk"
        dist, role = _distance_and_role(seeds, full["edges"], direction)
        walk = scoped["node_walk"]
        returned = {str(n.tumbler) for n in scoped["nodes"]}
        assert returned == set(walk), "node_walk states every node it returned and no other"
        in_scope = {str(n.tumbler) for n in full["nodes"] if n.physical_collection == RDR_COL}
        assert returned == in_scope | set(seeds), "the predicate removed the relays and nothing else"

        for node in sorted(returned):
            assert walk[node]["depth"] == dist[node], (direction, node, walk[node], dist[node])
            if node in seeds:
                assert "role" not in walk[node]
            else:
                assert walk[node]["role"] == role[node], (direction, node, walk[node], role[node])

        # Non-vacuity: scoring from the edges the scoped reply returns (the client's behaviour before node_walk) gets
        # some node wrong, so node_walk is doing work the equality above would not otherwise be testing.
        edge_dist, edge_role = _distance_and_role(seeds, scoped["edges"], direction)
        misscored = {
            n for n in returned - set(seeds)
            if edge_dist.get(n) != dist[n] or edge_role.get(n) != role[n]
        }
        assert misscored, f"{direction}: no node was reached through a removed relay, so the walk proves nothing"
        non_seed = returned - set(seeds)
        assert len(non_seed) >= 3, (direction, non_seed)
        if direction == "both":
            assert {walk[n]["role"] for n in non_seed} == {"citation", "implementation", "related"}
            assert max(walk[n]["depth"] for n in non_seed) >= 2


class TestManifestRowLimit:
    def test_the_engine_echoes_the_limit_and_the_client_accepts_the_reply(self, catalog):
        seed = _register(catalog, "manifest-seed", RDR_COL, "rdr")

        got = catalog.get_manifests_sampled([seed], 3)

        assert got is not None, "the echo and row_counts are what the client requires of a limit that ran"
        manifests, counts = got
        assert manifests == {} and counts == {}, "a document with no manifest rows is absent, as for get_manifests"

    def test_a_non_positive_limit_is_a_400(self, catalog):
        seed = _register(catalog, "manifest-seed-2", RDR_COL, "rdr")
        with pytest.raises(httpx.HTTPStatusError) as caught:
            catalog._post("/manifest/get_many", {"doc_ids": [seed], "max_rows_per_doc": 0}, mutates=False)
        assert caught.value.response.status_code == 400


class TestOutOfStepCost:
    def test_a_stored_out_of_step_cost_reads_back_and_the_two_sided_check_accepts_the_row(self, tel_store):  # noqa: F811
        from nexus.commands.answer_runs import _costs_agree

        tel_store.record_nx_answer_run(
            question="it-planner-record", plan_id=0, matched_confidence=None, step_count=0, final_text="t",
            cost_usd=0.0125, duration_ms=5, out_of_step_cost_usd=0.004,
        )
        tel_store.record_nx_answer_run_complete(
            question="it-planner-complete", plan_id=0, matched_confidence=None, step_count=0, final_text="t",
            cost_usd=0.0125, duration_ms=5, created_at="2026-10-28T15:00:01.250000+00:00", steps=None,
            success=True, out_of_step_cost_usd=0.0085,
        )
        rows = {r["question"]: r for r in tel_store.query_nx_answer_runs(limit=300)["rows"]}

        assert rows["it-planner-record"]["out_of_step_cost_usd"] == pytest.approx(0.004)
        assert rows["it-planner-complete"]["out_of_step_cost_usd"] == pytest.approx(0.0085)
        assert _costs_agree(0.0125, 0.0085, planner_share_allowed=True, out_of_step_cost=rows["it-planner-record"]["out_of_step_cost_usd"])
        assert not _costs_agree(0.0125, 0.0085, planner_share_allowed=True, out_of_step_cost=0.001), "an over-count is now caught"

    def test_a_library_plan_run_stores_its_extraction_share_and_the_check_is_exact_for_it(self, tel_store):  # noqa: F811
        from nexus.commands.answer_runs import _step_breakdown

        tel_store.record_nx_answer_run(
            question="it-oos-library", plan_id=7, matched_confidence=0.9, step_count=1, final_text="t",
            cost_usd=0.034, duration_ms=5, out_of_step_cost_usd=0.004,
            steps=[{"step_index": 0, "operator": "summarize", "source": "llm", "cost_usd": 0.03, "elapsed_ms": 1, "ok": True}],
        )
        (row,) = [r for r in tel_store.query_nx_answer_runs(limit=300, include_steps=True)["rows"] if r["question"] == "it-oos-library"]
        assert row["out_of_step_cost_usd"] == pytest.approx(0.004)
        assert _step_breakdown([row])["cost_consistency_violations"] == []
        row["cost_usd"] = 0.5
        assert len(_step_breakdown([row])["cost_consistency_violations"]) == 1

    def test_a_run_with_no_out_of_step_call_reads_back_null_not_zero(self, tel_store):  # noqa: F811
        tel_store.record_nx_answer_run(
            question="it-planner-none", plan_id=3, matched_confidence=0.9, step_count=1, final_text="t",
            cost_usd=0.01, duration_ms=5,
        )
        (row,) = [r for r in tel_store.query_nx_answer_runs(limit=300)["rows"] if r["question"] == "it-planner-none"]
        assert "out_of_step_cost_usd" in row and row["out_of_step_cost_usd"] is None
