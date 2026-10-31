# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 section 1, the lookup row of the compatibility matrix (nexus-jypmj.11; Sam, 2026-10-30).

A ``lookup`` request admits lookup plans only. The shipped row also admitted synthesis plans. This is the
Phase 2 gate-on matrix method (``tests/test_rdr232_golden_fixture.py``) cut down to the one row that moved:
the builtin plan templates are seeded into the test tenant, the matcher runs over the library the way
``nx_answer`` runs it, gate off and gate on, for every request the three golden sets label ``lookup``, with
the gate built for kind ``lookup``. The shipped row is put back in place for the control arm.

The request kinds here are the LABELS, not an extraction's reading (the extraction reports of the
measurement are not in the repo and cost a model call), so this pins the plan side of the row for a request
that is read as a lookup. The offline figures with the extraction kinds, the live runs and the real-traffic
replay are in the RDR-232 Key Discoveries entry and T2 ``nexus/rdr232-jypmj11-rule-measurement-2026-10-30``.
No model call.
"""
from __future__ import annotations

import pytest

from bench import rdr232_gate_matrix as gm
from bench.schema import load_golden_set
from tests import _rdr232_golden as g

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

_SETS = (
    ("golden_v1", g.GOLDEN_V1_PATH),
    ("heldout_v1", g.HELDOUT_PATH),
    ("compound_v1", g.REPO / "bench" / "queries" / "golden_compound_lookup_v1.yaml"),
)
LOOKUP_PLAN = "lookup:hybrid-factual-lookup"


def _observe(library, queries, **kw) -> dict[str, dict]:
    out = {}
    for q in queries:
        sc, bd = g.request_call_shape(q)
        sel = g.observe_plan_selection(library, q.text, scope=sc, bindings=bd, **kw)
        out[q.qid] = {"selected": sel.selected, "candidates": list(sel.candidates)}
    return out


def _lookup_matrix(library, monkeypatch, row) -> dict[str, dict]:
    from nexus.plans import eligibility as el

    monkeypatch.setitem(el._COMPATIBLE, el._L, row)
    figures: dict[str, dict] = {}
    for name, path in _SETS:
        queries = [q for q in load_golden_set(path) if q.answer_kind == "lookup"]
        kinds = {q.qid: ("lookup", "model-assisted") for q in queries}
        off = _observe(library, queries)
        on = _observe(library, queries, eligibility=el.EligibilityFilter("lookup", "model-assisted", enforce=True))
        figures[name] = {"queries": queries, "off": off, "on": on, "cell": gm.cell(queries, off, on, kinds)}
    return figures


def test_the_lookup_only_row_against_the_shipped_row_over_the_three_golden_sets(t2_service_env, monkeypatch) -> None:
    from nexus.mcp_infra import reset_plan_cache_for_tests
    from nexus.plans import eligibility as el

    library = g.seed_builtin_plans()
    reset_plan_cache_for_tests()
    tightened_row = el._COMPATIBLE[el._L]
    shipped_row = frozenset({el._L, el._S})
    try:
        shipped = _lookup_matrix(library, monkeypatch, shipped_row)
        reset_plan_cache_for_tests()
        tightened = _lookup_matrix(library, monkeypatch, tightened_row)
    finally:
        reset_plan_cache_for_tests()
    # non-vacuity: the lookup-labelled requests the figures are over (5 + 5 + 6), not an empty selection
    assert {n: len(shipped[n]["queries"]) for n in shipped} == {"golden_v1": 5, "heldout_v1": 5, "compound_v1": 6}
    for name in shipped:
        s, t = shipped[name]["cell"], tightened[name]["cell"]
        assert t["wrong_shape_admission_rate"]["on_count"] == 0, (name, t["wrong_shape_admission_rate"])
        assert t["eligible_plan_recall"]["on_hits"] >= s["eligible_plan_recall"]["on_hits"], (name, "recall loss")
        assert t["misses"]["on"] <= s["misses"]["on"], (name, "a request that had a plan now has none")
        for qid, sel in tightened[name]["on"].items():
            assert set(sel["candidates"]) <= {LOOKUP_PLAN}, (name, qid, "a lookup was offered a plan that is not a lookup plan")
    # held-out: the one wrong-shape admission of the shipped row (H09) is gone, and a lookup request gains a plan
    held_s, held_t = shipped["heldout_v1"], tightened["heldout_v1"]
    assert held_s["cell"]["wrong_shape_admission_rate"]["on_count"] == 1
    assert held_s["on"]["H09"]["selected"] == "research:citation-traversal", "the control arm reproduces the live H09 selection"
    assert held_t["on"]["H09"]["selected"] == LOOKUP_PLAN
    assert (held_s["cell"]["eligible_plan_recall"]["on_hits"], held_t["cell"]["eligible_plan_recall"]["on_hits"]) == (2, 3)
    # golden_v1 and the compound set: the same recall under both rows
    for name in ("golden_v1", "compound_v1"):
        assert shipped[name]["cell"]["eligible_plan_recall"] == tightened[name]["cell"]["eligible_plan_recall"], name
