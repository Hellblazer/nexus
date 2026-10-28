# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 2 (nexus-jypmj.2.8): the gate-on matrix, pure part.

One CELL is one golden set read with one source of request kinds (the deterministic first pass, or the kinds
a structured extraction produced) and one plan library. The selection of each request is observed twice by the
caller: with the gate off (the unfiltered matcher) and with the gate enforcing for the kind that source gives
the request. This module turns those two selections per request into the plan-side figures, using the same
measure definitions as ``bench.answer_shape_metrics`` (a blacklist reading: only unacceptable plans are
labelled). It touches no service and no model; the matcher runs in ``tests/test_rdr232_golden_fixture.py``.

Request text never enters a cell: requests are named by qid.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bench.answer_shape_metrics import NotMeasurable, RunObservation, compute_measures
from bench.schema import Query

#: ``{"selected": plan key or None, "candidates": [plan keys]}``: what ``observe_plan_selection`` returns.
Selection = Mapping[str, Any]
#: ``qid -> (kind, decision)``.
Kinds = Mapping[str, tuple[str, str]]


def _value(measure: Any) -> float | None:
    return None if isinstance(measure.value, NotMeasurable) else float(measure.value)


def _figures(queries: Sequence[Query], off: Mapping[str, Selection], on: Mapping[str, Selection]) -> dict[str, Any]:
    def measures(sel: Mapping[str, Selection]) -> dict[str, Any]:
        obs = [
            RunObservation(qid=q.qid, selected_plan=sel[q.qid]["selected"], candidate_plans=list(sel[q.qid]["candidates"]))
            for q in queries
        ]
        return compute_measures(list(queries), obs)

    m_off, m_on = measures(off), measures(on)
    wo, wn = m_off["wrong_shape_admission_rate"], m_on["wrong_shape_admission_rate"]
    ro, rn = m_off["eligible_plan_recall"], m_on["eligible_plan_recall"]
    return {
        "wrong_shape_admission_rate": {
            "off": _value(wo), "on": _value(wn),
            "off_count": wo.detail.get("selected_unacceptable"), "on_count": wn.detail.get("selected_unacceptable"),
        },
        "eligible_plan_recall": {
            "off": _value(ro), "on": _value(rn),
            "off_hits": ro.detail.get("with_acceptable_candidate"), "on_hits": rn.detail.get("with_acceptable_candidate"),
        },
    }


def cell(queries: Sequence[Query], off: Mapping[str, Selection], on: Mapping[str, Selection], kinds: Kinds) -> dict[str, Any]:
    """The figures of one cell. *off* and *on* map a qid to its selection with the gate off and on; *kinds* maps
    a qid to the ``(kind, decision)`` the gate was built with. Raises ``ValueError`` naming a request any of the
    three lacks (a cell over a partial observation would report a rate over fewer requests than it claims)."""
    for q in queries:
        for name, table in (("off", off), ("on", on), ("kinds", kinds)):
            if q.qid not in table:
                raise ValueError(f"{name} has no entry for request {q.qid!r}")
    out = _figures(queries, off, on)
    lookup = [q for q in queries if q.answer_kind == "lookup"]
    sub = _figures(lookup, off, on) if lookup else None
    out["requests"] = len(queries)
    out["misses"] = {
        "off": sum(1 for q in queries if off[q.qid]["selected"] is None),
        "on": sum(1 for q in queries if on[q.qid]["selected"] is None),
    }
    out["lookup_only"] = {
        "requests": len(lookup),
        "eligible_plan_recall": sub["eligible_plan_recall"] if sub else None,
        "wrong_shape_admission_rate": sub["wrong_shape_admission_rate"] if sub else None,
    }
    out["wrong_shape_requests"] = {
        "off": [q.qid for q in queries if off[q.qid]["selected"] in set(q.unacceptable_plans)],
        "on": [q.qid for q in queries if on[q.qid]["selected"] in set(q.unacceptable_plans)],
    }
    out["selection_changed"] = [
        {
            "qid": q.qid, "label": q.answer_kind, "kind_used": kinds[q.qid][0],
            "off": off[q.qid]["selected"], "on": on[q.qid]["selected"],
        }
        for q in queries if off[q.qid]["selected"] != on[q.qid]["selected"]
    ]
    return out


def kinds_from_report(report: Mapping[str, Any]) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """``(deterministic, final)`` kinds per qid from a labelled extraction report
    (``bench.rdr232_extract_measure``): the first pass's reading and the reading after the extraction."""
    det: dict[str, tuple[str, str]] = {}
    final: dict[str, tuple[str, str]] = {}
    for row in report["questions_detail"]:
        det[row["qid"]] = (row["det_kind"], row["det_decision"])
        final[row["qid"]] = (row["final_kind"], row["final_decision"])
    return det, final


def kind_disagreements(a: Kinds, b: Kinds) -> list[str]:
    """Sorted qids whose KIND differs between *a* and *b* (the decision label is metadata and is ignored)."""
    if set(a) != set(b):
        raise ValueError(f"the two readings cover different requests: {sorted(set(a) ^ set(b))}")
    return sorted(q for q in a if a[q][0] != b[q][0])
