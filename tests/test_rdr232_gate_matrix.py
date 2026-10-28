# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 2 (nexus-jypmj.2.8): the gate-on matrix's pure core and the golden-path override.

``scripts/bench/rdr232_gate_matrix.py`` turns per-request plan selections (gate off and gate on) into the
plan-side figures of one cell of the matrix: wrong-shape admission, eligible-plan recall (overall and over the
requests labelled lookup) and the requests whose selection changed. The substrate part (the matcher over the
seeded library) sits in the golden journey test; these tests need no tenant, no service and no model.
``tests/_rdr232_golden.py`` reads ``NX_RDR232_GOLDEN_PATH``; the default is golden_v1 and is unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bench import rdr232_gate_matrix as gm
from bench.schema import Query
from tests import _rdr232_golden as g


def _q(qid: str, kind: str, bad: list[str]) -> Query:
    return Query(qid=qid, category="x", text=f"text of {qid}", answer_kind=kind, unacceptable_plans=bad)


def _sel(selected: str | None, candidates: list[str] | None = None) -> dict:
    return {"selected": selected, "candidates": candidates if candidates is not None else ([selected] if selected else [])}


QUERIES = [
    _q("A-lookup", "lookup", ["research:default"]),
    _q("B-lookup", "lookup", ["research:default"]),
    _q("C-synth", "synthesis", ["lookup:hybrid-factual-lookup"]),
    _q("D-synth", "synthesis", ["lookup:hybrid-factual-lookup"]),
]
KINDS = {
    "A-lookup": ("lookup", "heuristic"), "B-lookup": ("unknown", "unknown"),
    "C-synth": ("synthesis", "heuristic"), "D-synth": ("synthesis", "model-assisted"),
}
OFF = {
    "A-lookup": _sel("lookup:hybrid-factual-lookup", ["lookup:hybrid-factual-lookup", "research:default"]),
    "B-lookup": _sel("research:default"),
    "C-synth": _sel("lookup:hybrid-factual-lookup"),
    "D-synth": _sel("research:default", ["research:default", "lookup:hybrid-factual-lookup"]),
}
ON = {
    "A-lookup": OFF["A-lookup"],
    "B-lookup": _sel(None, []),
    "C-synth": _sel("research:default", ["research:default"]),
    "D-synth": OFF["D-synth"],
}


def test_a_cell_reports_wrong_shape_and_recall_off_and_on() -> None:
    cell = gm.cell(QUERIES, OFF, ON, KINDS)
    assert cell["requests"] == 4
    # off: B selects research:default (unacceptable for B), C selects the lookup plan (unacceptable for C)
    assert cell["wrong_shape_admission_rate"] == {"off": 0.5, "on": 0.0, "off_count": 2, "on_count": 0}
    # recall: a request counts when some candidate is not labelled unacceptable. Off: A and D hit; B (only
    # research:default) and C (only the lookup plan) miss. On: C now offers research:default and hits; B offers nothing.
    assert cell["eligible_plan_recall"] == {"off": 0.5, "on": 0.75, "off_hits": 2, "on_hits": 3}
    assert cell["misses"] == {"off": 0, "on": 1}
    assert cell["wrong_shape_requests"] == {"off": ["B-lookup", "C-synth"], "on": []}


def test_a_cell_restricts_recall_to_the_requests_labelled_lookup() -> None:
    cell = gm.cell(QUERIES, OFF, ON, KINDS)
    lookup = cell["lookup_only"]
    assert lookup["requests"] == 2
    # A: off and on offer the lookup plan, acceptable. B: off offers only research:default (unacceptable) and on offers nothing
    assert lookup["eligible_plan_recall"] == {"off": 0.5, "on": 0.5, "off_hits": 1, "on_hits": 1}
    assert lookup["wrong_shape_admission_rate"]["off_count"] == 1 and lookup["wrong_shape_admission_rate"]["on_count"] == 0


def test_a_cell_lists_the_requests_whose_selection_changed_with_the_kind_used() -> None:
    cell = gm.cell(QUERIES, OFF, ON, KINDS)
    assert cell["selection_changed"] == [
        {"qid": "B-lookup", "label": "lookup", "kind_used": "unknown", "off": "research:default", "on": None},
        {"qid": "C-synth", "label": "synthesis", "kind_used": "synthesis",
         "off": "lookup:hybrid-factual-lookup", "on": "research:default"},
    ]


def test_a_gate_that_changes_nothing_reports_no_change_and_equal_figures() -> None:
    cell = gm.cell(QUERIES, OFF, OFF, KINDS)
    assert cell["selection_changed"] == []
    assert cell["wrong_shape_admission_rate"]["off"] == cell["wrong_shape_admission_rate"]["on"]
    assert cell["eligible_plan_recall"]["off"] == cell["eligible_plan_recall"]["on"]


def test_a_cell_refuses_selections_that_miss_a_request() -> None:
    short = {k: v for k, v in OFF.items() if k != "D-synth"}
    with pytest.raises(ValueError, match="D-synth"):
        gm.cell(QUERIES, short, ON, KINDS)
    with pytest.raises(ValueError, match="D-synth"):
        gm.cell(QUERIES, OFF, ON, {k: v for k, v in KINDS.items() if k != "D-synth"})


def test_a_cell_with_no_lookup_labelled_request_says_so_instead_of_a_figure() -> None:
    cell = gm.cell([QUERIES[2], QUERIES[3]], OFF, ON, KINDS)
    assert cell["lookup_only"]["requests"] == 0 and cell["lookup_only"]["eligible_plan_recall"] is None


def test_kinds_come_from_the_report_per_request_id_for_both_readings() -> None:
    report = {"questions_detail": [
        {"qid": "A-lookup", "label": "lookup", "det_kind": "unknown", "det_decision": "unknown",
         "final_kind": "lookup", "final_decision": "model-assisted"},
        {"qid": "C-synth", "label": "synthesis", "det_kind": "synthesis", "det_decision": "heuristic",
         "final_kind": "synthesis", "final_decision": "heuristic"},
    ]}
    det, final = gm.kinds_from_report(report)
    assert det == {"A-lookup": ("unknown", "unknown"), "C-synth": ("synthesis", "heuristic")}
    assert final == {"A-lookup": ("lookup", "model-assisted"), "C-synth": ("synthesis", "heuristic")}


def test_kind_disagreements_name_the_requests_not_the_text() -> None:
    a = {"x": ("lookup", "d"), "y": ("synthesis", "d")}
    b = {"x": ("lookup", "other-decision"), "y": ("debug", "d")}
    assert gm.kind_disagreements(a, b) == ["y"]
    with pytest.raises(ValueError):
        gm.kind_disagreements(a, {"x": ("lookup", "d")})


# ── the golden-path override ────────────────────────────────────────────────────────────────────────────


def test_the_golden_path_is_golden_v1_unless_the_override_is_set() -> None:
    assert g.golden_path({}) == g.GOLDEN_V1_PATH == g.REPO / "bench" / "queries" / "golden_v1.yaml"
    assert g.golden_path({g.GOLDEN_PATH_ENV: ""}) == g.GOLDEN_V1_PATH
    assert g.golden_path({g.GOLDEN_PATH_ENV: "   "}) == g.GOLDEN_V1_PATH
    assert g.GOLDEN_PATH_ENV == "NX_RDR232_GOLDEN_PATH"


def test_the_override_names_another_set_by_absolute_or_repo_relative_path() -> None:
    held = g.golden_path({g.GOLDEN_PATH_ENV: "bench/queries/golden_heldout_v1.yaml"})
    assert held == g.HELDOUT_PATH and held.is_file()
    assert g.golden_path({g.GOLDEN_PATH_ENV: str(g.HELDOUT_PATH)}) == g.HELDOUT_PATH


def test_an_override_that_names_no_file_fails_loudly() -> None:
    with pytest.raises(FileNotFoundError, match="NX_RDR232_GOLDEN_PATH"):
        g.golden_path({g.GOLDEN_PATH_ENV: "bench/queries/does_not_exist.yaml"})


def test_the_module_constant_is_the_default_when_the_environment_has_no_override(monkeypatch) -> None:
    monkeypatch.delenv(g.GOLDEN_PATH_ENV, raising=False)
    assert g.golden_path() == g.GOLDEN_V1_PATH
    assert isinstance(g.GOLDEN_PATH, Path)
