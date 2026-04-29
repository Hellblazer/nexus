# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Integration harness for RDR-097 hybrid retrieval (P1.5).

Runs the ``hybrid-factual-lookup`` plan and a vector-only baseline
against the ``knowledge__hybridrag`` corpus, computes recall@10
against fixture expectations, and asserts the hybrid plan is not
worse than the vector-only baseline on any fixture. The actual
"hybrid is better" claim is reported in test output for measurement,
not gated.

Run with::

    uv run pytest -m integration tests/test_hybrid_plan_factual_qa.py -v

Skipped automatically when API keys are absent (Chroma + Voyage +
Anthropic). The harness is opt-in via the ``integration`` marker so
the unit suite stays fast.

Fixture extension protocol
--------------------------

The fixture set below ships with placeholder questions intended as a
runnable smoke surface. To turn the harness into a real recall-gate,
replace the ``expected_tumblers`` and ``expected_substrings`` fields
with values pulled from the live corpus. The bead nexus-qlfa carries
the corpus-side prep; this file is the test side.

Cost note
---------

Each fixture run dispatches: 1 search (Voyage embedding), 2
``store_get_many`` calls (T3 reads), 1 rank step (Anthropic), 1
generate step (Anthropic). The budget sweep parametrize doubles
that. Keep the fixture count modest; expensive sweeps belong in a
nightly bench rather than the integration marker.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# ── API-key gate ───────────────────────────────────────────────────────────


def _t3_reachable() -> bool:
    """Return True when Chroma + Voyage credentials are wired."""
    needed = ("CHROMA_API_KEY", "VOYAGE_API_KEY", "CHROMA_TENANT", "CHROMA_DATABASE")
    if not all(os.environ.get(k) for k in needed):
        return False
    try:
        from nexus.db import make_t3
        make_t3()
        return True
    except Exception:
        return False


def _anthropic_reachable() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_T3_AVAILABLE: bool = _t3_reachable()
_ANTHROPIC_AVAILABLE: bool = _anthropic_reachable()

requires_full_stack = pytest.mark.skipif(
    not (_T3_AVAILABLE and _anthropic_reachable()),
    reason=(
        "Hybrid plan integration test requires CHROMA_API_KEY, "
        "VOYAGE_API_KEY, CHROMA_TENANT, CHROMA_DATABASE, and "
        "ANTHROPIC_API_KEY"
    ),
)

requires_t3_only = pytest.mark.skipif(
    not _T3_AVAILABLE,
    reason=(
        "Vector-baseline test requires CHROMA_API_KEY, VOYAGE_API_KEY, "
        "CHROMA_TENANT, CHROMA_DATABASE"
    ),
)


# ── Fixtures ──────────────────────────────────────────────────────────────


#: Target collection. The harness assumes ``knowledge__hybridrag`` has
#: been indexed and catalog-linked locally before running.
TARGET_COLLECTION = "knowledge__hybridrag"


#: 5 question fixtures from the bead. The placeholder values below are
#: structurally complete (the harness runs against them) but the
#: ``expected_tumblers`` lists need to be populated with real corpus
#: tumblers before recall@10 yields a meaningful number. Until then,
#: the tests act as a smoke surface and the recall numbers are
#: informational only.
HYBRIDRAG_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "fx-1-method-overview",
        "question": "what is HybridRAG and how does it combine vector and graph retrieval",
        "expected_tumblers": [],
        "expected_substrings": ["HybridRAG", "vector", "graph"],
        "long_context": False,
    },
    {
        "id": "fx-2-experimental-result",
        "question": "what F1 score does HybridRAG report on its evaluation benchmark",
        "expected_tumblers": [],
        "expected_substrings": ["F1", "benchmark"],
        "long_context": False,
    },
    {
        "id": "fx-3-context-precision",
        "question": "how does graph traversal affect context precision in HybridRAG",
        "expected_tumblers": [],
        "expected_substrings": ["context", "precision", "graph"],
        "long_context": False,
    },
    {
        "id": "fx-4-budget-tradeoff",
        "question": "what is the recommended budget split between vector and graph retrieval",
        "expected_tumblers": [],
        "expected_substrings": ["budget", "split", "60", "40"],
        "long_context": False,
    },
    {
        "id": "fx-5-long-context-stress",
        "question": "describe the full retrieval pipeline architecture of HybridRAG end to end",
        "expected_tumblers": [],
        "expected_substrings": ["retrieval", "pipeline", "architecture"],
        "long_context": True,
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────


def _vector_only_baseline(
    question: str, *, limit: int = 10, threshold: float = 2.0,
) -> dict[str, Any]:
    """Run a pure vector-recall baseline against the target collection.

    No traverse, no rank, no generate — just the search MCP tool +
    store_get_many for content. Returns ``{ids, contents}`` aligned
    1:1 with the search hit order.
    """
    from nexus.mcp.core import search, store_get_many
    raw = search(
        query=question,
        corpus=TARGET_COLLECTION,
        limit=limit,
        threshold=threshold,
        structured=True,
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"search returned non-dict: {raw!r}")
    ids = raw.get("ids") or []
    if not ids:
        return {"ids": [], "contents": []}
    hyd = store_get_many(
        ids=ids, collections=TARGET_COLLECTION, structured=True,
    )
    contents = hyd.get("contents", []) if isinstance(hyd, dict) else []
    return {"ids": ids, "contents": contents}


async def _run_hybrid_plan(
    question: str,
    *,
    vector_budget: int = 6,
    graph_budget: int = 4,
    limit: int = 40,
) -> dict[str, Any]:
    """Run hybrid-factual-lookup via plan_run, bypassing nx_answer."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.plans.match import Match
    from nexus.plans.runner import plan_run

    library = PlanLibrary(path=default_db_path())
    rows = library.search_plans("hybrid-factual-lookup", limit=5)
    plan_row = next(
        (r for r in rows if r.get("name") == "hybrid-factual-lookup"),
        None,
    )
    if plan_row is None:
        raise RuntimeError(
            "hybrid-factual-lookup not seeded — run `nx catalog setup` first"
        )

    match = Match(
        plan_id=plan_row["id"],
        name=plan_row["name"],
        description=plan_row.get("query") or "",
        confidence=1.0,
        dimensions=json.loads(plan_row.get("dimensions") or "{}"),
        tags=plan_row.get("tags") or "",
        plan_json=plan_row["plan_json"],
        required_bindings=json.loads(plan_row.get("required_bindings") or "[]"),
        optional_bindings=json.loads(plan_row.get("optional_bindings") or "[]"),
        default_bindings=json.loads(plan_row.get("default_bindings") or "{}"),
        parent_dims=None,
    )

    result = await plan_run(
        match,
        {
            "question": question,
            "corpus": TARGET_COLLECTION,
            "limit": limit,
            "vector_budget_chunks": vector_budget,
            "graph_budget_chunks": graph_budget,
        },
    )
    return {
        "step_outputs": result.step_outputs,
        "match": match,
    }


def _recall_at_n(
    retrieved_ids: list[str],
    expected_tumblers: list[str],
    contents: list[str],
    expected_substrings: list[str],
    n: int = 10,
) -> dict[str, float]:
    """Recall@N against two complementary signals.

    ``recall_tumbler``: fraction of expected_tumblers seen in the
    first N retrieved IDs. Returns 0.0 when ``expected_tumblers`` is
    empty (placeholder fixture without ground truth).

    ``recall_substring``: fraction of expected_substrings appearing
    in any of the first N hydrated contents. Always populated by
    the placeholder fixtures so the harness has a working signal
    even before tumbler ground-truth is filled in.
    """
    top_ids = retrieved_ids[:n]
    top_contents = contents[:n]

    if expected_tumblers:
        hits_t = sum(1 for t in expected_tumblers if any(t in r for r in top_ids))
        recall_t = hits_t / len(expected_tumblers)
    else:
        recall_t = 0.0

    if expected_substrings:
        joined = "\n\n".join(top_contents).lower()
        hits_s = sum(1 for s in expected_substrings if s.lower() in joined)
        recall_s = hits_s / len(expected_substrings)
    else:
        recall_s = 0.0

    return {"recall_tumbler": recall_t, "recall_substring": recall_s}


def _record_telemetry(
    fixture_id: str,
    budget: tuple[int, int],
    *,
    hybrid_recall: dict[str, float],
    baseline_recall: dict[str, float],
    hybrid_ids: list[str],
    baseline_ids: list[str],
    elapsed_ms_hybrid: int,
    elapsed_ms_baseline: int,
) -> None:
    """Persist per-run I/O to T2 memory for diff-checking across runs.

    Uses the memory MCP tool with project='rdr-097' so the records
    are isolated from regular project memory and easy to wipe.
    """
    from nexus.mcp.core import memory_put
    payload = {
        "fixture_id": fixture_id,
        "budget": list(budget),
        "hybrid_recall": hybrid_recall,
        "baseline_recall": baseline_recall,
        "hybrid_top_ids": hybrid_ids[:10],
        "baseline_top_ids": baseline_ids[:10],
        "elapsed_ms_hybrid": elapsed_ms_hybrid,
        "elapsed_ms_baseline": elapsed_ms_baseline,
    }
    title = f"{fixture_id}-v{budget[0]}-g{budget[1]}.json"
    try:
        memory_put(
            content=json.dumps(payload, indent=2),
            project="rdr-097",
            title=title,
            tags="rdr-097,p1.5,hybrid-factual-qa",
        )
    except Exception:
        # Telemetry recording is best-effort — never fail the test on it.
        pass


# ── Tests ─────────────────────────────────────────────────────────────────


@requires_t3_only
@pytest.mark.parametrize("fixture", HYBRIDRAG_FIXTURES, ids=lambda f: f["id"])
def test_vector_only_baseline_runs(fixture: dict[str, Any]) -> None:
    """Smoke: every fixture's question hits the vector path without error.

    Establishes a recall@10 floor against ``expected_substrings`` so
    the hybrid path has something to compare against. Asserts the
    baseline retrieves at least one chunk.
    """
    out = _vector_only_baseline(fixture["question"])
    assert out["ids"], (
        f"Baseline returned zero hits for {fixture['id']!r}; corpus "
        f"may not be indexed or threshold may be too tight."
    )
    recall = _recall_at_n(
        out["ids"], fixture["expected_tumblers"],
        out["contents"], fixture["expected_substrings"],
    )
    print(
        f"\n[{fixture['id']}] baseline "
        f"recall_tumbler={recall['recall_tumbler']:.2f} "
        f"recall_substring={recall['recall_substring']:.2f} "
        f"(hits={len(out['ids'])})"
    )


@requires_full_stack
@pytest.mark.parametrize(
    "budget",
    [(6, 4), (4, 2)],
    ids=lambda b: f"v{b[0]}-g{b[1]}",
)
@pytest.mark.parametrize("fixture", HYBRIDRAG_FIXTURES, ids=lambda f: f["id"])
@pytest.mark.asyncio
async def test_hybrid_not_worse_than_baseline(
    fixture: dict[str, Any], budget: tuple[int, int],
) -> None:
    """Recall gate: hybrid recall_substring >= baseline recall_substring.

    Substring recall is the primary gate because tumbler recall is
    only meaningful once the fixtures carry real expected_tumblers.
    The "is hybrid better" claim is reported in test output (print)
    but is not asserted — the bead's gate is "not worse than".

    Telemetry is recorded to T2 memory project='rdr-097' so a
    cross-run diff can answer "did the change regress recall on
    fixture X at budget Y" without re-running the whole suite.
    """
    vector_budget, graph_budget = budget

    t0 = time.perf_counter()
    baseline = _vector_only_baseline(fixture["question"])
    elapsed_baseline = int((time.perf_counter() - t0) * 1000)
    baseline_recall = _recall_at_n(
        baseline["ids"], fixture["expected_tumblers"],
        baseline["contents"], fixture["expected_substrings"],
    )

    t1 = time.perf_counter()
    hybrid = await _run_hybrid_plan(
        fixture["question"],
        vector_budget=vector_budget,
        graph_budget=graph_budget,
    )
    elapsed_hybrid = int((time.perf_counter() - t1) * 1000)

    # Pull ranked output. The plan's step5 is rank → step5.ranked is
    # a list[str] of top-ranked chunk contents. We have no ID-level
    # recall on the rank output, so substring recall is the signal.
    step_outputs = hybrid["step_outputs"]
    rank_out = step_outputs[4] if len(step_outputs) > 4 else {}
    ranked_contents = rank_out.get("ranked") or []
    # IDs from the merged retrieval streams (steps 1 + 2):
    s1_ids = (step_outputs[0] or {}).get("ids") or []
    s2_ids = (step_outputs[1] or {}).get("ids") or []
    hybrid_ids = list(s1_ids) + list(s2_ids)

    hybrid_recall = _recall_at_n(
        hybrid_ids, fixture["expected_tumblers"],
        ranked_contents, fixture["expected_substrings"],
    )

    _record_telemetry(
        fixture["id"], budget,
        hybrid_recall=hybrid_recall,
        baseline_recall=baseline_recall,
        hybrid_ids=hybrid_ids,
        baseline_ids=baseline["ids"],
        elapsed_ms_hybrid=elapsed_hybrid,
        elapsed_ms_baseline=elapsed_baseline,
    )

    print(
        f"\n[{fixture['id']} v{vector_budget}-g{graph_budget}] "
        f"baseline.substring={baseline_recall['recall_substring']:.2f} "
        f"hybrid.substring={hybrid_recall['recall_substring']:.2f} "
        f"baseline.tumbler={baseline_recall['recall_tumbler']:.2f} "
        f"hybrid.tumbler={hybrid_recall['recall_tumbler']:.2f} "
        f"(elapsed: hybrid={elapsed_hybrid}ms baseline={elapsed_baseline}ms)"
    )

    # Gate: hybrid recall_substring >= baseline. The "better" claim
    # is reported but not asserted (per bead acceptance criteria).
    assert hybrid_recall["recall_substring"] >= baseline_recall["recall_substring"], (
        f"Hybrid worse than baseline on {fixture['id']!r} "
        f"at budget v{vector_budget}-g{graph_budget}: "
        f"hybrid={hybrid_recall['recall_substring']:.2f} < "
        f"baseline={baseline_recall['recall_substring']:.2f}"
    )


@pytest.mark.asyncio
async def test_implements_link_type_fires() -> None:
    """P1.3 feedback loop: does ``implements`` actually fire on knowledge__*?

    Walks the ``factual-evidence`` purpose (cites + implements +
    relates) from a sample of seeds in ``knowledge__hybridrag`` and
    counts edges by link_type. If the implements tally is zero
    across all 5 fixtures, RDR-097 P1.3 should drop ``implements``
    from the alias before merge — knowledge collections are sparse
    in implements links by construction (code↔RDR linker output).

    This is a measurement test, not a gate. The result is printed for
    review and recorded to T2 memory so the next session can see it
    without re-running.
    """
    from collections import Counter
    import click
    from nexus.commands.catalog import _get_catalog
    try:
        cat = _get_catalog()
    except click.ClickException as e:
        pytest.skip(f"catalog not available: {e.message}")
    db = cat._db

    # Sample up to 10 seeds from the target collection.
    rows = db.execute(
        "SELECT tumbler FROM documents WHERE physical_collection = ? LIMIT 10",
        (TARGET_COLLECTION,),
    ).fetchall()
    if not rows:
        pytest.skip(
            f"{TARGET_COLLECTION} not registered — corpus prerequisite missing"
        )

    from nexus.catalog.tumbler import Tumbler
    seeds: list[Tumbler] = []
    for r in rows:
        try:
            seeds.append(Tumbler.parse(r[0]))
        except Exception:
            continue

    counts: Counter = Counter()
    for seed in seeds:
        result = cat.graph(
            seed, depth=2, direction="both",
            link_types=["cites", "implements", "relates"],
        )
        for edge in result.get("edges") or []:
            counts[getattr(edge, "link_type", "") or "(blank)"] += 1

    print(f"\nP1.3 link-type tally on {TARGET_COLLECTION}: {dict(counts)}")
    # Record for cross-session diffing.
    try:
        from nexus.mcp.core import memory_put
        memory_put(
            content=json.dumps(dict(counts), indent=2),
            project="rdr-097",
            title="link-type-tally-knowledge-hybridrag.json",
            tags="rdr-097,p1.3,p1.5,implements-decision",
        )
    except Exception:
        pass

    # No assert — measurement only. The decision to drop ``implements``
    # is human-driven; this test surfaces the data.
