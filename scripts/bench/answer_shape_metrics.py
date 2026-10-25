# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 answer-shape and evidence-sufficiency measures (nexus-jypmj.1.1).

RDR-232 section 5 "Evaluation before tuning" names seven measures. They are computed here from
the golden-set labels (``bench.schema.Query``) and one ``RunObservation`` per request, a plain
record of what a run did: which plans the matcher offered and chose, what evidence reached the
reducer's input, and, once the later phases exist, what repair and the terminal answer did.

A measure whose inputs only a later phase can produce reports :class:`NotMeasurable`, rendered
"not measurable before Phase N". It is never ``0.0``: a zero reads as a perfect score on the
leakage axis and a total failure on the coverage axes for a run nobody could score.

The seven measures, with their pre-change reading:

* ``eligible_plan_recall``: share of requests with at least one candidate that is not in the
  request's ``unacceptable_plans``. With no eligibility gate this is the ceiling the Phase 2 gate
  must not lower.
* ``wrong_shape_admission_rate``: share of requests whose SELECTED plan is unacceptable.
* ``primary_source_coverage``: mean share of ``required_sources`` represented in the evidence set
  before reduction.
* ``facet_evidence_coverage``: mean share of ``required_facets`` that some evidence item carries.
  Items must be facet-labeled (the fixture's oracle labels, or Phase 3's ledger); unlabeled
  evidence is not measurable before Phase 3.
* ``scope_leakage``: share of evidence items outside ``allowed_corpora`` with no recorded,
  permitted reason. A run with no evidence items is excluded, not scored 0.0.
* ``repair_yield_and_cost``: missing facets repaired within one attempt, and mean repair cost.
  Runs with nothing missing are excluded and repaired counts only among what was missing, so
  the yield stays in [0, 1]. Phase 3.
* ``terminal_completeness``: share of required facets addressed or disclosed as missing in the
  terminal answer. Phase 3.

CLI: ``uv run python scripts/bench/answer_shape_metrics.py <golden.yaml> <observations.json>``
prints the seven measures as JSON.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bench.schema import Query, load_golden_set

MEASURE_NAMES: tuple[str, ...] = (
    "eligible_plan_recall",
    "wrong_shape_admission_rate",
    "primary_source_coverage",
    "facet_evidence_coverage",
    "scope_leakage",
    "repair_yield_and_cost",
    "terminal_completeness",
)


@dataclass(frozen=True)
class NotMeasurable:
    """A measure that cannot be scored yet. ``phase`` is the RDR-232 phase that makes it
    scoreable; ``None`` means no request was observed at all (nothing to score, not a later phase)."""

    phase: int | None
    reason: str = ""

    def __str__(self) -> str:
        if self.phase is None:
            return f"not measured: {self.reason or 'no request observed'}"
        return f"not measurable before Phase {self.phase}"


@dataclass(frozen=True)
class EvidenceItem:
    """One piece of pre-reduction evidence.

    ``source`` is the source ref it belongs to (``RDR-084@ART``; ``""`` for none); ``corpus`` is
    its collection-family prefix; ``facets`` is the set of facet ids it evidences, or ``None``
    when the item is not facet-labeled; ``permitted_reason`` is the recorded reason it was
    admitted outside the request's allowed corpora (``""`` when none was recorded).
    """

    source: str = ""
    corpus: str = ""
    facets: frozenset[str] | None = frozenset()
    permitted_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "corpus": self.corpus,
            "facets": None if self.facets is None else sorted(self.facets),
            "permitted_reason": self.permitted_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvidenceItem:
        facets = d.get("facets")
        return cls(
            source=d.get("source", ""),
            corpus=d.get("corpus", ""),
            facets=None if facets is None else frozenset(facets),
            permitted_reason=d.get("permitted_reason", ""),
        )


@dataclass(frozen=True)
class RepairOutcome:
    """One request's repair: facets missing when it ran, how many it supplied, what it cost."""

    missing: int = 0
    repaired: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class TerminalOutcome:
    """Which required facets the terminal answer addressed, and which it disclosed as missing."""

    addressed: list[str] = field(default_factory=list)
    disclosed_missing: list[str] = field(default_factory=list)


@dataclass
class RunObservation:
    """What one run of one golden request did. ``None`` means "not observed"."""

    qid: str
    selected_plan: str | None = None
    candidate_plans: list[str] | None = None
    evidence: list[EvidenceItem] | None = None
    repair: RepairOutcome | None = None
    terminal: TerminalOutcome | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "qid": self.qid,
            "selected_plan": self.selected_plan,
            "candidate_plans": self.candidate_plans,
            "evidence": None if self.evidence is None else [e.to_dict() for e in self.evidence],
            "repair": None if self.repair is None else vars(self.repair).copy(),
            "terminal": None if self.terminal is None else {
                "addressed": list(self.terminal.addressed),
                "disclosed_missing": list(self.terminal.disclosed_missing),
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunObservation:
        ev = d.get("evidence")
        rep = d.get("repair")
        term = d.get("terminal")
        return cls(
            qid=d["qid"],
            selected_plan=d.get("selected_plan"),
            candidate_plans=d.get("candidate_plans"),
            evidence=None if ev is None else [EvidenceItem.from_dict(e) for e in ev],
            repair=None if rep is None else RepairOutcome(**rep),
            terminal=None if term is None else TerminalOutcome(
                addressed=list(term.get("addressed", [])),
                disclosed_missing=list(term.get("disclosed_missing", [])),
            ),
        )


@dataclass(frozen=True)
class Measure:
    """One measure: its value (or why it has none), how many requests it scored, and detail."""

    name: str
    value: float | NotMeasurable
    n: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _unscored(name: str, phase: int | None, reason: str = "") -> Measure:
    return Measure(name, NotMeasurable(phase, reason), 0)


def _eligible_plan_recall(pairs: list[tuple[Query, RunObservation]]) -> Measure:
    name = "eligible_plan_recall"
    seen = [(q, o) for q, o in pairs if o.candidate_plans is not None]
    if not seen:
        return _unscored(name, None, "no request had its candidate plans observed")
    hits = sum(
        1 for q, o in seen
        if any(p not in set(q.unacceptable_plans) for p in o.candidate_plans or [])
    )
    return Measure(name, hits / len(seen), len(seen), {"with_acceptable_candidate": hits})


def _wrong_shape_admission_rate(pairs: list[tuple[Query, RunObservation]]) -> Measure:
    name = "wrong_shape_admission_rate"
    seen = [(q, o) for q, o in pairs if o.candidate_plans is not None]
    if not seen:
        return _unscored(name, None, "no request had its plan selection observed")
    admitted = sum(1 for q, o in seen if o.selected_plan in set(q.unacceptable_plans))
    in_pool = sum(
        1 for q, o in seen
        if any(p in set(q.unacceptable_plans) for p in o.candidate_plans or [])
    )
    return Measure(
        name, admitted / len(seen), len(seen),
        {"selected_unacceptable": admitted, "unacceptable_in_candidates": in_pool},
    )


def _primary_source_coverage(pairs: list[tuple[Query, RunObservation]]) -> Measure:
    name = "primary_source_coverage"
    seen = [(q, o) for q, o in pairs if o.evidence is not None and q.required_sources]
    if not seen:
        return _unscored(name, None, "no request with required sources had its evidence observed")
    shares = []
    for q, o in seen:
        present = {e.source for e in o.evidence or []}
        shares.append(sum(1 for s in q.required_sources if s in present) / len(q.required_sources))
    return Measure(name, _mean(shares), len(seen))


def _facet_evidence_coverage(pairs: list[tuple[Query, RunObservation]]) -> Measure:
    name = "facet_evidence_coverage"
    observed = [(q, o) for q, o in pairs if o.evidence is not None and q.required_facets]
    if not observed:
        return _unscored(name, None, "no request with required facets had its evidence observed")
    shares = []
    for q, o in observed:
        if any(e.facets is None for e in o.evidence or []):
            continue  # unlabeled evidence: facet coverage needs Phase 3's ledger
        covered: set[str] = set()
        for e in o.evidence or []:
            covered |= e.facets or frozenset()
        shares.append(sum(1 for f in q.required_facets if f in covered) / len(q.required_facets))
    if not shares:
        return _unscored(name, 3, "evidence is not facet-labeled")
    return Measure(name, _mean(shares), len(shares))


def _scope_leakage(pairs: list[tuple[Query, RunObservation]]) -> Measure:
    name = "scope_leakage"
    # A run whose evidence set is empty can leak nothing and says nothing
    # about scope: it is excluded, never scored 0.0.
    seen = [(q, o) for q, o in pairs if o.evidence and q.allowed_corpora]
    if not seen:
        return _unscored(
            name, None, "no request with allowed corpora had any evidence items observed",
        )
    rates, leaked_total, items_total = [], 0, 0
    for q, o in seen:
        allowed = set(q.allowed_corpora)
        items = o.evidence or []
        leaked = sum(1 for e in items if e.corpus not in allowed and not e.permitted_reason)
        rates.append(leaked / len(items))
        leaked_total += leaked
        items_total += len(items)
    return Measure(
        name, _mean(rates), len(seen),
        {"leaked_items": leaked_total, "total_items": items_total},
    )


def _repair_yield_and_cost(pairs: list[tuple[Query, RunObservation]]) -> Measure:
    name = "repair_yield_and_cost"
    ran = [o.repair for _q, o in pairs if o.repair is not None]
    if not ran:
        return _unscored(name, 3, "no repair ran")
    # Yield is repaired over missing, so a run with nothing missing has no
    # yield to report (excluded, not 0.0), and repaired is counted only
    # among what was missing: the ratio cannot pass 1.
    seen = [r for r in ran if r.missing > 0]
    if not seen:
        return _unscored(name, None, "repair ran but no run had anything missing")
    missing = sum(r.missing for r in seen)
    repaired = sum(min(r.repaired, r.missing) for r in seen)
    return Measure(
        name, repaired / missing, len(seen),
        {"missing": missing, "repaired": repaired,
         "mean_cost_usd": _mean([r.cost_usd for r in ran]), "runs_with_repair": len(ran)},
    )


def _terminal_completeness(pairs: list[tuple[Query, RunObservation]]) -> Measure:
    name = "terminal_completeness"
    seen = [(q, o) for q, o in pairs if o.terminal is not None and q.required_facets]
    if not seen:
        return _unscored(name, 3, "no terminal answer was classified")
    shares = []
    for q, o in seen:
        handled = set(o.terminal.addressed if o.terminal else []) | set(
            o.terminal.disclosed_missing if o.terminal else [])
        shares.append(sum(1 for f in q.required_facets if f in handled) / len(q.required_facets))
    return Measure(name, _mean(shares), len(seen))


_COMPUTE = {
    "eligible_plan_recall": _eligible_plan_recall,
    "wrong_shape_admission_rate": _wrong_shape_admission_rate,
    "primary_source_coverage": _primary_source_coverage,
    "facet_evidence_coverage": _facet_evidence_coverage,
    "scope_leakage": _scope_leakage,
    "repair_yield_and_cost": _repair_yield_and_cost,
    "terminal_completeness": _terminal_completeness,
}


def compute_measures(
    queries: list[Query], observations: list[RunObservation],
) -> dict[str, Measure]:
    """All seven measures, in :data:`MEASURE_NAMES` order, over the observed requests."""
    by_qid = {q.qid: q for q in queries}
    pairs: list[tuple[Query, RunObservation]] = []
    for o in observations:
        if o.qid not in by_qid:
            raise ValueError(f"observation for unknown qid {o.qid!r}")
        pairs.append((by_qid[o.qid], o))
    return {name: _COMPUTE[name](pairs) for name in MEASURE_NAMES}


def observations_from_json(path: Path) -> list[RunObservation]:
    with open(path, encoding="utf-8") as f:
        return [RunObservation.from_dict(d) for d in json.load(f)]


def measures_to_json(measures: dict[str, Measure]) -> str:
    out = {
        name: {
            "value": str(m.value) if isinstance(m.value, NotMeasurable) else m.value,
            "n": m.n,
            "detail": m.detail,
        }
        for name, m in measures.items()
    }
    return json.dumps(out, indent=2, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("golden", type=Path, help="golden-set YAML (bench/queries/golden_v1.yaml)")
    ap.add_argument("observations", type=Path, help="observations JSON (list of RunObservation)")
    args = ap.parse_args(argv)
    queries = load_golden_set(args.golden)
    print(measures_to_json(compute_measures(queries, observations_from_json(args.observations))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
