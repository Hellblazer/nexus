# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 (nexus-jypmj.1.13, .6, .5): the three engine wire changes against the REAL engine.

Boots the gate jar on an ephemeral Postgres (the fixtures of ``test_http_telemetry_store_integration``) and
drives the real clients. The Java tests pin the engine's side of each change (``CatalogRepositoryTest``,
``CatalogHandlerTraverseScopeFilterTest``, ``CatalogHandlerManifestEnvelopeTest``,
``TelemetryHandlerPlannerCostTest``); the unit tests pin the client's side against fakes; this file pins that the
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
        assert tel_store._supports_nx_answer_planner_cost() is True


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


class TestPlannerCost:
    def test_a_stored_planner_cost_reads_back_and_the_two_sided_check_accepts_the_row(self, tel_store):  # noqa: F811
        from nexus.commands.answer_runs import _costs_agree

        tel_store.record_nx_answer_run(
            question="it-planner-record", plan_id=0, matched_confidence=None, step_count=0, final_text="t",
            cost_usd=0.0125, duration_ms=5, planner_cost_usd=0.004,
        )
        tel_store.record_nx_answer_run_complete(
            question="it-planner-complete", plan_id=0, matched_confidence=None, step_count=0, final_text="t",
            cost_usd=0.0125, duration_ms=5, created_at="2026-10-28T15:00:01.250000+00:00", steps=None,
            success=True, planner_cost_usd=0.0085,
        )
        rows = {r["question"]: r for r in tel_store.query_nx_answer_runs(limit=300)["rows"]}

        assert rows["it-planner-record"]["planner_cost_usd"] == pytest.approx(0.004)
        assert rows["it-planner-complete"]["planner_cost_usd"] == pytest.approx(0.0085)
        assert _costs_agree(0.0125, 0.0085, planner_share_allowed=True, planner_cost=rows["it-planner-record"]["planner_cost_usd"])
        assert not _costs_agree(0.0125, 0.0085, planner_share_allowed=True, planner_cost=0.001), "an over-count is now caught"

    def test_a_run_with_no_planner_dispatch_reads_back_null_not_zero(self, tel_store):  # noqa: F811
        tel_store.record_nx_answer_run(
            question="it-planner-none", plan_id=3, matched_confidence=0.9, step_count=1, final_text="t",
            cost_usd=0.01, duration_ms=5,
        )
        (row,) = [r for r in tel_store.query_nx_answer_runs(limit=300)["rows"] if r["question"] == "it-planner-none"]
        assert "planner_cost_usd" in row and row["planner_cost_usd"] is None
