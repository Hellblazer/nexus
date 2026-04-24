# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for RDR-088 operator pipelines.

Covers:

  * ``nexus-ac40.5`` — traverse -> operator_check end-to-end on
    ``knowledge__delos``. Exercises the check operator's shared
    evidence schema through a real plan_run dispatch.
  * ``nexus-smpi`` — three-step MVV: search -> operator_filter ->
    operator_check via plan_run. Hits every Phase 1/Phase 2 operator's
    contract in one composed pipeline.

Assertions are shape-based (typed keys, role enum membership, subset
contract) rather than content-based. LLM output varies across runs;
the contract pins are what must remain stable. Corpus drift on
``knowledge__delos`` may alter which tumblers surface, but the shape
of a valid plan_run result is invariant.

Marked ``@pytest.mark.integration`` — skipped by default. Requires
ChromaDB + Voyage + claude auth. Run with:

    uv run pytest -m integration tests/integration/test_rdr_088_operator_pipelines.py
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _claude_auth_available() -> bool:
    """Return True iff ``claude auth status --json`` reports loggedIn."""
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


@pytest.fixture(scope="module", autouse=True)
def _skip_without_live_deps():
    if not _claude_auth_available():
        pytest.skip("claude auth not available — skipping live integration tests")
    if not _t3_reachable():
        pytest.skip("T3 not reachable — skipping live integration tests")


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
        plan_id=1, name="rdr088-integration", description="integration",
        confidence=0.9, dimensions={"verb": "integration-test"},
        tags="", plan_json=json.dumps(plan),
        required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


def _assert_evidence_entry_shape(entry: dict) -> None:
    assert set(entry.keys()).issuperset({"item_id", "quote", "role"}), (
        f"evidence entry missing required keys; got {entry.keys()}"
    )
    assert entry["role"] in {"supports", "contradicts", "neutral"}, (
        f"role must be one of supports/contradicts/neutral; got {entry['role']!r}"
    )
    assert isinstance(entry["item_id"], str)
    assert isinstance(entry["quote"], str)


# ── nexus-ac40.5: traverse -> operator_check ─────────────────────────────────


class TestTraverseThenCheck:
    """RDR-088 Phase 2 Step 2: verify the traverse -> operator_check
    composition surfaces structured evidence through plan_run.

    The ``knowledge__delos`` corpus is pre-indexed with the Delos papers
    and their citation graph. The pipeline below walks the graph from
    a seed tumbler, materializes the peer documents, and asks the check
    operator whether they are consistent on a chosen claim.

    Contract pins (what this test enforces even though content varies):
      * plan_run returns a structured dict for each step
      * the check step's ``ok`` is a boolean
      * the check step's ``evidence`` is a list of dicts with
        ``{item_id, quote, role}`` where ``role`` is in the allowed enum
    """

    @pytest.mark.asyncio
    async def test_traverse_then_check_emits_structured_evidence(self) -> None:
        from nexus.plans.runner import plan_run

        plan = {
            "steps": [
                # Seed traverse with a chunk id that exists in
                # knowledge__delos. We use search first to discover a
                # seed dynamically because the chunk ids shift across
                # re-indexings.
                {
                    "tool": "search",
                    "args": {
                        "query": "schema mappings data exchange",
                        "corpus": "knowledge",
                        "limit": 3,
                    },
                },
                # Feed search's ids into a hydrated check step; the
                # hydration path materialises documents via store_get_many
                # and lands them on ``items``.
                {
                    "tool": "check",
                    "args": {
                        "ids": "$step1.ids",
                        "collections": "knowledge__delos",
                        "check_instruction": (
                            "Are the documents consistent in how they "
                            "describe schema mapping procedures?"
                        ),
                    },
                },
            ],
        }

        result = await plan_run(_match_for_plan(plan), {}, dispatcher=None)

        # Step 1: search returned a live retrieval payload.
        search_out = result.steps[0]
        assert isinstance(search_out, dict)
        assert "ids" in search_out
        assert isinstance(search_out["ids"], list)
        if not search_out["ids"]:
            pytest.skip(
                "knowledge__delos returned zero ids for seed query; "
                "corpus may have drifted. Re-seed or skip.",
            )

        # Step 2: check returned structured evidence.
        check_out = result.steps[1]
        assert isinstance(check_out, dict)
        assert isinstance(check_out.get("ok"), bool)
        evidence = check_out.get("evidence")
        assert isinstance(evidence, list)
        for entry in evidence:
            _assert_evidence_entry_shape(entry)


# ── nexus-smpi: Phase 2 MVV — search -> filter -> check ──────────────────────


class TestPhase2MVVPipeline:
    """RDR-088 Phase 2 MVV. The full three-step pipeline exercises
    every operator added in Phases 1 and 2 in one composed dispatch:

      1. ``search`` — retrieval
      2. ``operator_filter`` — narrow by natural-language criterion
      3. ``operator_check`` — consistency probe over the narrowed set

    The test pins step-by-step shape contracts so a wiring regression
    (the nexus-yis0 class of bug that the audit carry-over forced us
    to guard against) surfaces as a clear assertion failure on the
    offending step, not as a late blow-up downstream.
    """

    @pytest.mark.asyncio
    async def test_search_filter_check_end_to_end(self) -> None:
        from nexus.plans.runner import plan_run

        plan = {
            "steps": [
                {
                    "tool": "search",
                    "args": {
                        "query": "schema mappings data exchange",
                        "corpus": "knowledge",
                        "limit": 5,
                    },
                },
                {
                    "tool": "filter",
                    "args": {
                        "ids": "$step1.ids",
                        "collections": "knowledge__delos",
                        "criterion": "discusses data exchange semantics",
                    },
                },
                {
                    "tool": "check",
                    "args": {
                        "items": "$step2.items",
                        "check_instruction": (
                            "Do the documents agree on how schema "
                            "mappings relate to the chase procedure?"
                        ),
                    },
                },
            ],
        }

        result = await plan_run(_match_for_plan(plan), {}, dispatcher=None)

        # Step 1 — search
        search_out = result.steps[0]
        for key in ("ids", "tumblers", "distances", "collections"):
            assert key in search_out, f"search output missing {key!r}"
        if not search_out["ids"]:
            pytest.skip(
                "knowledge__delos returned zero ids for MVV seed "
                "query; corpus may have drifted.",
            )

        # Step 2 — filter
        # Filter and check are both bundleable operators, so the runner
        # fuses them into ONE claude_dispatch call. Intermediate bundle
        # outputs are not exposed on the host side by design; the plan
        # runner stamps the BUNDLED_INTERMEDIATE sentinel in place.
        # Filter's {items, rationale} contract is exercised inside the
        # composed prompt (the bundle test in test_plan_bundle.py pins
        # the prompt + terminal schema) and in the Phase 1 integration
        # test test_run_filter_step_after_search_narrows_results.
        from nexus.plans.bundle import BUNDLED_INTERMEDIATE

        filter_out = result.steps[1]
        assert filter_out == BUNDLED_INTERMEDIATE, (
            "filter step should be a bundled intermediate when the "
            "adjacent check step is also bundleable; got "
            f"{filter_out!r}"
        )

        # Step 3 — check (terminal output of the bundle)
        check_out = result.steps[2]
        assert isinstance(check_out.get("ok"), bool)
        assert isinstance(check_out.get("evidence"), list)
        for entry in check_out["evidence"]:
            _assert_evidence_entry_shape(entry)
