# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10): an ``nx_answer`` structured envelope to a ``RunObservation``.

``scripts/bench/answer_shape_metrics.py`` scores seven measures from one :class:`RunObservation` per golden request.
Until now nothing built one from a live run. :func:`observation_from_envelope` does, from the envelope
``nx_answer(structured=True, trace=True)`` returns, and :func:`envelope_facts` flattens the same envelope into the
numbers the Phase 3 driver tabulates (reply classes, unassessed causes, repair, cost). Neither calls a model.

What the envelope can and cannot supply:

* ``evidence``: the coverage ledger's anchors (what the terminal reduction was handed). Each is an
  :class:`EvidenceItem` with its corpus (the collection prefix) and, where a ``named_sources`` entry resolved to the
  anchor's document, its source ref. ``facets`` come from the ORACLE (:mod:`bench.rdr232_oracle`) when it labels the
  anchor, else ``None`` (unlabeled): never from the ledger, because scoring the assessor with its own ledger is
  circular. With no oracle every anchor is unlabeled and ``facet_evidence_coverage`` reads "not measurable".
* ``repair``: the facets the repair targeted (``missing``) and how many of them the final ledger covers
  (``repaired``). Retrieval is a known-zero step and the reassessment's cost is not separable from the first round's
  in the steps, so ``cost_usd`` is 0.0; the assessment as a whole is in :func:`envelope_facts` (``assessment_usd``).
  Repair yield by facet origin is :func:`repair_by_origin`: ``whole_question`` facets replay the plan's own search, so
  their yield is expected near zero and must be read apart.
* ``terminal``: ``None``. Whether the prose addressed or disclosed each facet needs a judgment no envelope carries.
* ``selected_plan`` / ``candidate_plans``: the envelope names plan ids, not ``<verb>:<strategy>`` keys, so they are
  filled only when the caller supplies ``plan_key_of``.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

from collections.abc import Callable, Mapping
from typing import Any

from bench.answer_shape_metrics import EvidenceItem, RepairOutcome, RunObservation
from bench.rdr232_oracle import DocPathOf, RequestOracle

#: The step operator name of one assessment call (``coverage_ledger.ASSESSMENT_OPERATOR``); duplicated as a literal so
#: this module imports nothing from ``nexus``.
ASSESSMENT_OPERATOR = "operator_check"


def _coverage(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
    cov = envelope.get("coverage")
    return cov if isinstance(cov, Mapping) else {}


def _source_by_tumbler(envelope: Mapping[str, Any]) -> dict[str, str]:
    """Document tumbler -> source ref, for the named sources that resolved to a document."""
    named = envelope.get("named_sources")
    out: dict[str, str] = {}
    for s in (named or {}).get("sources") or []:
        if isinstance(s, Mapping) and s.get("tumbler") and s.get("source"):
            out[str(s["tumbler"])] = str(s["source"])
    return out


def _corpus_of(collection: str) -> str:
    return str(collection).split("__", 1)[0] if collection else ""


def _anchor_facets(oracle: RequestOracle | None, anchor: Mapping[str, Any], doc_path_of: DocPathOf | None) -> frozenset[str] | None:
    """The golden slugs *anchor* supports according to the ORACLE, or ``None`` when the oracle does not label it."""
    if oracle is None:
        return None
    chash = str(anchor.get("chash") or "")
    path = doc_path_of(anchor) if doc_path_of is not None else ""
    if not any(item.matches(chash, path) for item in oracle.items):
        return None
    return frozenset(s for s in oracle.slugs if oracle.role_of(chash, path, s) == "supports")


def observation_from_envelope(
    qid: str, envelope: Mapping[str, Any], *, oracle: RequestOracle | None = None,
    plan_key_of: Callable[[int], str | None] | None = None, doc_path_of: DocPathOf | None = None,
) -> RunObservation:
    """The :class:`RunObservation` of golden request *qid* for one structured *envelope* (see the module docstring)."""
    cov = _coverage(envelope)
    anchors = [a for a in cov.get("anchors") or [] if isinstance(a, Mapping)]
    sources = _source_by_tumbler(envelope)
    evidence: list[EvidenceItem] | None = None
    if anchors:
        evidence = [
            EvidenceItem(
                source=sources.get(str(a.get("tumbler") or ""), ""), corpus=_corpus_of(str(a.get("collection") or "")),
                facets=_anchor_facets(oracle, a, doc_path_of),
            )
            for a in anchors
        ]
    repair: RepairOutcome | None = None
    rep = cov.get("repair") if isinstance(cov.get("repair"), Mapping) else {}
    if rep.get("outcome") not in (None, "not_attempted") and rep.get("facet_ids"):
        targets = set(rep["facet_ids"])
        covered = {f.get("facet_id") for f in cov.get("findings") or [] if isinstance(f, Mapping) and f.get("state") == "covered"}
        repair = RepairOutcome(missing=len(targets), repaired=len(targets & covered), cost_usd=0.0)
    selected: str | None = None
    candidates: list[str] | None = None
    if plan_key_of is not None:
        plan_id = envelope.get("plan_id")
        selected = plan_key_of(plan_id) if isinstance(plan_id, int) else None
        cands = [c.get("plan_id") for c in ((envelope.get("plan_choice") or {}).get("candidates") or []) if isinstance(c, Mapping)]
        keys = [plan_key_of(c) for c in cands if isinstance(c, int)]
        candidates = [k for k in keys if k] or None
    return RunObservation(
        qid=qid, selected_plan=selected, candidate_plans=candidates, evidence=evidence, repair=repair, terminal=None,
    )


def repair_by_origin(envelope: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Repair yield split by the targeted facet's ORIGIN (``clause``, ``whole_question``, ...): per origin, how many
    facets were targeted and how many the final ledger covers. Empty when no repair ran."""
    cov = _coverage(envelope)
    rep = cov.get("repair") if isinstance(cov.get("repair"), Mapping) else {}
    if rep.get("outcome") in (None, "not_attempted"):
        return {}
    origin_of = {
        str(f.get("id")): str(f.get("origin") or "unknown")
        for f in (envelope.get("request_coverage") or {}).get("facets") or [] if isinstance(f, Mapping)
    }
    covered = {f.get("facet_id") for f in cov.get("findings") or [] if isinstance(f, Mapping) and f.get("state") == "covered"}
    out: dict[str, dict[str, int]] = {}
    for fid in rep.get("facet_ids") or []:
        row = out.setdefault(origin_of.get(str(fid), "unknown"), {"targeted": 0, "covered_after": 0})
        row["targeted"] += 1
        row["covered_after"] += int(fid in covered)
    return out


def assessment_usd(envelope: Mapping[str, Any]) -> float:
    """USD spent on assessment calls: the sum of the ``operator_check`` step rows (the plan's own steps carry other
    operator names), the only place assessment cost is recoverable from an envelope."""
    return round(sum(
        float(s.get("cost_usd") or 0.0) for s in envelope.get("steps") or []
        if isinstance(s, Mapping) and s.get("operator") == ASSESSMENT_OPERATOR
    ), 6)


def envelope_facts(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The per-run facts the Phase 3 driver tabulates, read from one envelope (closed names and counts; no text)."""
    cov = _coverage(envelope)
    assessment = cov.get("assessment") if isinstance(cov.get("assessment"), Mapping) else {}
    summary = envelope.get("run_summary") if isinstance(envelope.get("run_summary"), Mapping) else {}
    facets = cov.get("facets") if isinstance(cov.get("facets"), Mapping) else {}
    return {
        "answer_shape": envelope.get("answer_shape"),
        "cost_usd": float(envelope.get("cost_usd") or 0.0),
        "assessment_usd": assessment_usd(envelope),
        "state": cov.get("state"), "reason": cov.get("reason"), "skip_generation": bool(cov.get("skip_generation")),
        "facets_required": int(cov.get("facets_required") or 0),
        "facets": dict(facets),
        "facets_conflicted": int(cov.get("facets_conflicted") or 0),
        "unassessed_by_cause": dict(cov.get("unassessed_by_cause") or {}),
        "assessment_outcome": assessment.get("outcome"),
        "assessment_calls": int(assessment.get("calls") or 0),
        "assessment_latency_ms": int(assessment.get("latency_ms") or 0),
        "failure_cause": assessment.get("failure_cause"),
        "accepted_records": int(assessment.get("accepted_records") or 0),
        "rejected_records": int(assessment.get("rejected_records") or 0),
        "call_log": [list(c) for c in assessment.get("call_log") or []],
        "reply_classes": dict(assessment.get("reply_classes") or {}),
        "repair_entered": bool(cov.get("repair_entered")),
        "repair_outcome": (cov.get("repair") or {}).get("outcome") if isinstance(cov.get("repair"), Mapping) else None,
        "repair_by_origin": repair_by_origin(envelope),
        "persistence": summary.get("persistence"),
        "returned_coverage_state": summary.get("returned_coverage_state"),
        "persisted_coverage_state": summary.get("persisted_coverage_state"),
        "code_anchor_share": _code_share(cov),
    }


def _code_share(cov: Mapping[str, Any]) -> float | None:
    """Share of the final reduction's anchors held in ``code`` collections (the MVV's "no unrelated code" check)."""
    by = (cov.get("corpus_counts") or {}).get("by_collection") or {}
    total = sum(by.values())
    return None if not total else sum(n for c, n in by.items() if _corpus_of(c) == "code") / total
