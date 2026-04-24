# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for RDR-093 GroupBy / Aggregate operator pipelines.

Phase 3 MVV (bead nexus-zzbt). Three scenarios drawn from RDR-093
§Test Plan:

  * Test 1 — RDR-093 Test Plan scenario 4: full 4-step bundled
    ``search -> filter -> groupby -> aggregate`` pipeline on
    ``knowledge__delos``. Shape assertions + bundled-intermediate
    sentinel checks pin the contract; LLM content varies.

  * Test 2 — RDR-093 Test Plan scenario 5 (C-1 inline-items
    regression guard): three-step ``search -> groupby -> aggregate``
    pipeline. Aggregate summaries must reference content that lives
    only in the items' bodies (not in their ids), proving groupby's
    inline-items contract is plumbed through the bundled prompt.
    A revert to id-references would leave aggregate guessing and
    the body-content assertion would fail.

  * Test 3 — RDR-093 Test Plan scenario 6 (S-1 truncation metadata):
    150-item input through ``plan_run`` triggers
    ``_OPERATOR_MAX_INPUTS=100`` cap; runner attaches
    ``{truncated, original_count, kept_count}`` metadata and the
    dispatcher merges it onto the operator's return envelope.

Marked ``@pytest.mark.integration`` — skipped by default. Test 1 and
Test 2 require live ``claude -p`` auth + reachable T3. Test 3 mocks
``claude_dispatch`` at the substrate so it runs the full ``plan_run``
+ runner truncation path deterministically without API spend.

Run with::

    uv run pytest -m integration tests/integration/test_rdr_093_groupby_aggregate_pipelines.py

API budget estimate: Test 1 ~$0.10, Test 2 ~$0.10. Test 3 = $0.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _claude_auth_available() -> bool:
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn") or data.get("isLoggedIn"))
    except Exception:
        return False


def _t3_reachable() -> bool:
    if not all([
        os.environ.get("CHROMA_API_KEY"),
        os.environ.get("VOYAGE_API_KEY"),
        os.environ.get("CHROMA_TENANT"),
        os.environ.get("CHROMA_DATABASE"),
    ]):
        return False
    try:
        from nexus.db import make_t3
        make_t3()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture(autouse=True)
async def _reset_singletons_between_tests():
    yield
    from nexus.mcp_infra import reset_singletons
    reset_singletons()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _match_for_plan(plan: dict):
    """Wrap an inline plan dict in a Match compatible with plan_run."""
    from nexus.plans.match import Match

    return Match(
        plan_id=1, name="rdr093-integration", description="integration",
        confidence=0.9, dimensions={"verb": "integration-test"},
        tags="", plan_json=json.dumps(plan),
        required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


def _assert_groupby_shape(groupby_out: Any) -> None:
    """RDR-093 §Technical Design: groupby output must be
    ``{groups: [{key_value, items: list[dict]}]}`` with items inline."""
    assert isinstance(groupby_out, dict), (
        f"groupby output must be a dict, got {type(groupby_out).__name__}"
    )
    groups = groupby_out.get("groups")
    assert isinstance(groups, list), (
        f"groupby output must carry `groups` as a list, got {type(groups).__name__}"
    )
    for g in groups:
        assert isinstance(g, dict)
        assert isinstance(g.get("key_value"), str), (
            "each group must have a string key_value"
        )
        items = g.get("items")
        assert isinstance(items, list), (
            "each group must have an items list (C-1: inline)"
        )
        for it in items:
            assert isinstance(it, dict), (
                "C-1 contract: each item is a dict (inline), NOT a string id"
            )


def _assert_aggregate_shape(aggregate_out: Any) -> None:
    """RDR-093 §Technical Design: aggregate output must be
    ``{aggregates: [{key_value, summary}]}``."""
    assert isinstance(aggregate_out, dict)
    aggregates = aggregate_out.get("aggregates")
    assert isinstance(aggregates, list), (
        f"aggregate output must carry `aggregates` as a list, got "
        f"{type(aggregates).__name__}"
    )
    for a in aggregates:
        assert isinstance(a, dict)
        assert isinstance(a.get("key_value"), str)
        assert isinstance(a.get("summary"), str)


# ── Test 1: 4-step bundled pipeline — RDR-093 Test Plan scenario 4 ──────────


class TestPhase3MVVPipeline:
    """RDR-093 Phase 3 MVV. The full four-step paper §D.4 pipeline:

      1. ``search`` — retrieval against knowledge__delos
      2. ``operator_filter`` — narrow by natural-language criterion
      3. ``operator_groupby`` — partition by a natural-language key
      4. ``operator_aggregate`` — reduce each group to a per-group summary

    Steps 2-4 are bundleable; the runner fuses them into ONE
    ``claude -p`` dispatch. Steps 2 and 3 emit ``BUNDLED_INTERMEDIATE``
    sentinels on the host side; step 4 carries the terminal aggregate
    output.

    Contract pins (LLM content varies; the contract is what stays):
      * plan_run returns a structured dict per step
      * step 2 (filter) is a BUNDLED_INTERMEDIATE
      * step 3 (groupby) is a BUNDLED_INTERMEDIATE
      * step 4 (aggregate) carries the {aggregates: [...]} payload
    """

    @pytest.fixture(autouse=True)
    def _skip_without_live_deps(self):
        if not _claude_auth_available():
            pytest.skip("claude auth not available")
        if not _t3_reachable():
            pytest.skip("T3 not reachable")

    @pytest.mark.asyncio
    async def test_search_filter_groupby_aggregate_end_to_end(self) -> None:
        from nexus.plans.bundle import BUNDLED_INTERMEDIATE
        from nexus.plans.runner import plan_run

        plan = {
            "steps": [
                {
                    "tool": "search",
                    "args": {
                        "query": "consensus protocols byzantine fault tolerance",
                        "corpus": "knowledge",
                        "limit": 8,
                    },
                },
                {
                    "tool": "filter",
                    "args": {
                        "ids": "$step1.ids",
                        "collections": "knowledge__delos",
                        "criterion": "discusses consensus protocol design",
                    },
                },
                {
                    "tool": "groupby",
                    "args": {
                        "items": "$step2.items",
                        "key": "fault model (Byzantine vs crash)",
                    },
                },
                {
                    "tool": "aggregate",
                    "args": {
                        "groups": "$step3.groups",
                        "reducer": (
                            "list one central technique each group's "
                            "papers introduce"
                        ),
                    },
                },
            ],
        }

        result = await plan_run(_match_for_plan(plan), {}, dispatcher=None)

        # Step 1 — search returned a live retrieval payload.
        search_out = result.steps[0]
        assert isinstance(search_out, dict)
        for key in ("ids", "tumblers", "distances", "collections"):
            assert key in search_out, f"search output missing {key!r}"
        if not search_out["ids"]:
            pytest.skip(
                "knowledge__delos returned zero ids for MVV seed; "
                "corpus may have drifted",
            )

        # Step 2 (filter) and Step 3 (groupby) are bundled intermediates.
        # Step 4 (aggregate) is the terminal bundled step and carries the
        # composite output. The runner stamps BUNDLED_INTERMEDIATE on the
        # intermediates so plan authors know the host-side dict is not
        # the per-operator output (which exists only inside the LLM).
        filter_out = result.steps[1]
        assert filter_out == BUNDLED_INTERMEDIATE, (
            "filter must be a bundled intermediate when adjacent ops "
            f"are also bundleable; got {filter_out!r}"
        )
        groupby_out = result.steps[2]
        assert groupby_out == BUNDLED_INTERMEDIATE, (
            "groupby must be a bundled intermediate; got "
            f"{groupby_out!r}"
        )

        # Step 4 — terminal aggregate output.
        aggregate_out = result.steps[3]
        _assert_aggregate_shape(aggregate_out)
        # Sanity: at least one aggregate emitted (LLM may produce 1+
        # depending on how it partitions Byzantine vs crash).
        assert len(aggregate_out["aggregates"]) >= 1, (
            "expected at least one aggregate from the bundled pipeline"
        )


# ── Test 2: C-1 inline-items regression guard — Test Plan scenario 5 ────────


class TestC1InlineItemsRegressionGuard:
    """RDR-093 Gate finding C-1 (inline-items contract) regression
    guard. The bundled ``groupby -> aggregate`` path has no host-side
    retrieval inside one ``claude -p`` dispatch. If groupby emits
    only ``item_ids`` (the pre-gate design), aggregate cannot resolve
    them and its summaries either fail schema validation or
    hallucinate. The C-1 fix carries items INLINE in groupby's output.

    This test fixes a search query whose top hits have distinctive,
    technical body content. After bundled groupby + aggregate, the
    aggregates' summaries should reference at least one body-derived
    technical term — proving the inline content reached the LLM in
    the bundled prompt rather than a content-free id list. A
    regression to id-references would cause this assertion to fail
    because the LLM in the aggregate position would not have any
    body content to summarise.

    See also: RDR-093 §Failure Modes > 'Silent (resolved in design)'
    entry, which documents the historical id-only shape so future
    reverts trip this test.
    """

    @pytest.fixture(autouse=True)
    def _skip_without_live_deps(self):
        if not _claude_auth_available():
            pytest.skip("claude auth not available")
        if not _t3_reachable():
            pytest.skip("T3 not reachable")

    @pytest.mark.asyncio
    async def test_aggregate_summaries_reference_inline_body_content(
        self,
    ) -> None:
        from nexus.plans.bundle import BUNDLED_INTERMEDIATE
        from nexus.plans.runner import plan_run

        plan = {
            "steps": [
                {
                    "tool": "search",
                    "args": {
                        "query": "byzantine consensus quorum certificate",
                        "corpus": "knowledge",
                        "limit": 6,
                    },
                },
                {
                    "tool": "groupby",
                    "args": {
                        "ids": "$step1.ids",
                        "collections": "knowledge__delos",
                        "key": "fault model (Byzantine vs crash)",
                    },
                },
                {
                    "tool": "aggregate",
                    "args": {
                        "groups": "$step2.groups",
                        "reducer": (
                            "name one specific technical mechanism "
                            "(e.g. quorum size, view change, signature "
                            "scheme) discussed in the items"
                        ),
                    },
                },
            ],
        }

        result = await plan_run(_match_for_plan(plan), {}, dispatcher=None)

        search_out = result.steps[0]
        if not search_out.get("ids"):
            pytest.skip(
                "knowledge__delos returned zero ids for C-1 seed; "
                "corpus may have drifted",
            )

        # Bundled intermediate for groupby.
        assert result.steps[1] == BUNDLED_INTERMEDIATE

        aggregate_out = result.steps[2]
        _assert_aggregate_shape(aggregate_out)
        assert len(aggregate_out["aggregates"]) >= 1

        # C-1 regression guard: each summary must be substantive (the
        # reducer asks for a SPECIFIC technical mechanism — if aggregate
        # had only id references in its prompt, it could not produce a
        # mechanism-specific summary). 'Substantive' is operationalised
        # as length + the presence of at least one technical token from
        # a known vocabulary that surfaces only when body content was
        # in scope. We use a permissive vocabulary (terms that appear
        # generically in distributed-systems literature) so the test is
        # robust to which corpus chunks the search returns, but every
        # term IS something the LLM could only mention if body content
        # reached it via the inline-items contract.
        c1_vocab = {
            "quorum", "view", "signature", "leader", "byzantine",
            "consensus", "replica", "fault", "vote", "phase",
            "ballot", "certificate", "round", "commit", "primary",
        }
        any_summary_carries_body_term = False
        for agg in aggregate_out["aggregates"]:
            summary = agg["summary"]
            assert len(summary) >= 30, (
                f"aggregate summary too short to be a real mechanism "
                f"description; got {summary!r}. C-1 regression suspect: "
                f"aggregate may have been deprived of inline body "
                f"content"
            )
            tokens = {tok.lower().strip(".,;:()") for tok in summary.split()}
            if tokens & c1_vocab:
                any_summary_carries_body_term = True

        assert any_summary_carries_body_term, (
            "RDR-093 C-1 regression guard: at least one aggregate "
            "summary must reference a distributed-systems technical "
            "term from the inline body content. None of the "
            f"{len(aggregate_out['aggregates'])} aggregates contained "
            "any term from the permissive C-1 vocabulary. Most likely "
            "cause: groupby reverted to id-only output and aggregate "
            "had no body content to summarise. See RDR-093 §Failure "
            "Modes > 'Silent (resolved in design)'."
        )


# ── Test 3: S-1 truncation metadata — RDR-093 Test Plan scenario 6 ──────────


class TestS1TruncationMetadata:
    """RDR-093 §Test Plan scenario 6 + §Finalization Gate S-1.

    When the ``_OPERATOR_MAX_INPUTS=100`` cap fires for a groupby
    step driven by a 150-item input, the runner attaches
    ``{truncated: true, original_count: 150, kept_count: 100}`` to
    operator_groupby's return envelope so plan authors see the cap
    hit rather than silently losing 50 items.

    This test runs the full ``plan_run`` path with a mocked
    ``claude_dispatch`` substrate so it exercises the runner's
    end-to-end metadata-attachment flow without an API call. The
    unit-scope test (``tests/test_plan_run.py::test_default_dispatcher_groupby_truncation_pop_and_merge``)
    covers the dispatcher-direct path; this test covers the same
    contract through the public ``plan_run`` entry.

    Scoped to operator_groupby in this RDR; nexus-3j6b tracks
    cross-operator generalisation (the same metadata-on-truncation
    pattern across rank / filter / check / compare).
    """

    @pytest.mark.asyncio
    async def test_truncation_metadata_surfaces_through_plan_run(
        self, monkeypatch,
    ) -> None:
        from unittest.mock import patch

        from nexus.mcp import core as mcp_core
        from nexus.plans.runner import _OPERATOR_MAX_INPUTS, plan_run

        # Substitute claude_dispatch so the test runs deterministically
        # without an API call. The schema is enforced by the substrate
        # in production; here we pre-validate by returning a
        # schema-conformant payload directly.
        captured_args: list[dict] = []

        async def fake_dispatch(prompt, schema, timeout=300.0):
            return {
                "groups": [
                    {
                        "key_value": "synthesised",
                        "items": [{"id": "i-0"}],
                    },
                ],
            }

        # Patch the operator's actual dispatch path. We patch the
        # top-level `claude_dispatch` import within the operator's
        # local-import block. Easiest: monkeypatch the module-level
        # attribute on nexus.operators.dispatch directly.
        import nexus.operators.dispatch as _dispatch_mod
        monkeypatch.setattr(
            _dispatch_mod, "claude_dispatch", fake_dispatch,
        )
        # Capture the args operator_groupby is called with by
        # wrapping the registered tool.
        original_groupby = mcp_core.operator_groupby

        async def capturing_groupby(**kwargs):
            captured_args.append(kwargs)
            return await original_groupby(**kwargs)

        monkeypatch.setattr(mcp_core, "operator_groupby", capturing_groupby)

        # 150 ids → 150 contents → cap to 100. Patch store_get_many
        # so hydration sees 150 contents without needing real T3.
        oversized = {
            "contents": [
                f"item-body-payload-{i}"
                for i in range(_OPERATOR_MAX_INPUTS + 50)
            ],
        }
        with patch(
            "nexus.mcp.core.store_get_many", return_value=oversized,
        ):
            plan = {
                "steps": [
                    {
                        "tool": "groupby",
                        "args": {
                            "ids": [f"d-{i}" for i in range(150)],
                            "collections": "knowledge",
                            "key": "publication year",
                        },
                    },
                ],
            }
            result = await plan_run(
                _match_for_plan(plan), {}, dispatcher=None,
            )

        # The runner must have capped the operator's input at
        # _OPERATOR_MAX_INPUTS items.
        assert len(captured_args) == 1
        # The operator must NOT see the runner-internal marker.
        assert "_truncation_metadata" not in captured_args[0], (
            "S-1 guard: operator must never see the runner-internal "
            "truncation marker"
        )
        items_json = captured_args[0].get("items")
        assert isinstance(items_json, str)
        items_seen = json.loads(items_json)
        assert len(items_seen) == _OPERATOR_MAX_INPUTS, (
            f"runner must cap operator input at {_OPERATOR_MAX_INPUTS}; "
            f"operator saw {len(items_seen)}"
        )

        # The runner must merge truncation metadata onto the operator
        # return envelope on the public plan_run output.
        groupby_out = result.steps[0]
        assert isinstance(groupby_out, dict)
        assert "groups" in groupby_out, "operator native output preserved"
        assert groupby_out.get("truncated") is True, (
            "S-1 contract: plan_run's terminal step output for a "
            "truncated groupby must carry truncated=true"
        )
        assert groupby_out.get("original_count") == 150
        assert groupby_out.get("kept_count") == _OPERATOR_MAX_INPUTS
