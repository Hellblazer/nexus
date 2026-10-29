# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10): item-by-facet oracle labels and the assessor scorer.

The coverage assessor decides, per required facet, whether the evidence it was handed supports it. Scoring that
decision needs a truth the assessor did not produce: a hand (or fixture) label of which item bears on which golden
facet. Scoring the assessor with its own ledger would be circular, so this module has no path from a ledger to a
label: the provenance of a labelled request is a closed vocabulary (:data:`PROVENANCES`) that does not contain
``ledger``, the loader refuses anything else, and the scaffold writer derives labels only from the fixture's authored
piece labels and from the golden set's hand ``ground_truth`` grades.

**File format** (``bench/queries/golden_oracle_labels_v1.yaml``)::

    oracle_version: 1
    golden_set_version: 1
    requests:
      G06-synthesis-art-rdr084:
        provenance: fixture            # fixture | ground_truth | human  (``drafted``: see below)
        items:
          - chash: "<64 hex, sha256 of the chunk text>"     # or: doc_path_contains: "rdr-049-"
            closed: true               # true: a facet not listed is `neutral` for this item
            note: "## Locked Architecture"
            facets: {locked-architecture: supports}          # slug -> supports | contradicts | neutral
      G04-lookup-min-confidence:
        provenance: human
        todo: true                     # no labels yet: a human pass fills `items`
        items: []

An item matches an anchor by ``chash`` (the exact chunk) or by ``doc_path_contains`` (a substring of the anchor's
document path, which the caller resolves: the envelope's anchors carry a tumbler, not a path). A ``chash`` label is
CHUNK-level; a ``doc_path_contains`` label is DOCUMENT-level ("the gold document supports the facet"), so any held
chunk of that document makes the facet oracle-``supported`` even when the chunk the assessor was shown does not hold
the fact. Scoring that as a chunk-level false negative would charge the assessor for being right, so document-level
labels are scored apart (``document_level`` in :func:`aggregate`) and an item may say ``level: document`` (checked
against its key).

**Drafted labels** (Sam, 2026-10-28; critique S3). A separate file (``DEFAULT_DRAFTED_PATH``) holds labels an agent
drafted from the run's retrieved items without seeing the assessor's ledger (the packet is built mechanically by
:mod:`bench.rdr232_oracle_packet`). They have provenance ``drafted``, fill only the requests the hand file leaves
``todo``, and are reported apart from hand labels: :func:`aggregate` returns ``{"hand": ..., "drafted": ...}`` and never
pools them. Every rate carries a 95 percent Wilson interval (:func:`wilson_interval`). :func:`spot_check_sample` draws
the human spot check: stratified, plus every pair where assessor and drafter disagree, each recorded as unchecked. ``closed: false`` says
only the listed facets are known; an item is then unlabeled for every other facet. An anchor that no item matches is
unlabeled for every facet, never ``neutral``: absence of a label is not a negative label.

**Scoring** (:func:`score_run`, :func:`aggregate`). Per runtime facet the oracle state is ``supported`` (some held
anchor is labelled ``supports`` for one of the facet's slugs), ``contradicted``, ``not_supported`` (every held anchor
is labelled and none supports or contradicts) or ``unknown`` (some anchor is unlabeled). The numbers:

* ``false_negative_rate``: of the facets the oracle says are supported and the assessor judged
  (covered, unsupported or contradicted), the share it did not cover. This is the destructive direction (it drives
  repair, the partial notice and the skip). A facet decided by a deterministic row (``source_missing``,
  ``no_evidence``, ``scope_conflict``) is not the assessor's and is excluded.
* ``spurious_contradicts_rate``: of the facets the assessor marked contradicted (or covered with reason
  ``conflicted``), the share where no item it cited is labelled ``contradicts``. A facet whose cited items are not all
  labelled is not scored.
* ``false_skip``: runs whose generation was skipped (``uncovered``) while the oracle says some facet was supported.
* ``negative_control``: the assessor's state for facets the oracle says nothing bears on (``not_supported``).

Every rate carries its numerator, denominator and the count it could not score; a rate over an empty denominator is
``None``, never ``0.0``.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import click
import yaml

from bench.rdr232_facet_map import FacetResolution
from bench.schema import GOLDEN_SET_VERSION, Query, load_golden_set

ORACLE_VERSION = 1
DEFAULT_ORACLE_PATH = Path(__file__).resolve().parents[2] / "bench" / "queries" / "golden_oracle_labels_v1.yaml"
GOLDEN_PATH = Path(__file__).resolve().parents[2] / "bench" / "queries" / "golden_v1.yaml"
HELDOUT_PATH = Path(__file__).resolve().parents[2] / "bench" / "queries" / "golden_heldout_v1.yaml"

ROLES: tuple[str, ...] = ("supports", "contradicts", "neutral")
#: Where a label came from. ``ledger`` is deliberately not here: a label read off the assessor's own output is circular.
PROVENANCES: tuple[str, ...] = ("fixture", "ground_truth", "human", "drafted")
#: The provenances a person (or an authored fixture) produced; ``drafted`` is an agent's, spot-checked by a person.
HAND_PROVENANCES: tuple[str, ...] = ("fixture", "ground_truth", "human")
DEFAULT_DRAFTED_PATH = Path(__file__).resolve().parents[2] / "bench" / "queries" / "golden_oracle_labels_drafted_v1.yaml"
LEVELS: tuple[str, ...] = ("chunk", "document")
#: Finding reasons decided by a deterministic row, not by the assessor.
DETERMINISTIC_REASONS: frozenset[str] = frozenset({"source_missing", "no_evidence", "scope_conflict"})

DocPathOf = Callable[[Mapping[str, Any]], str]


def provenance_group(provenance: str) -> str:
    """``hand`` for a person's or fixture's label, ``drafted`` for an agent's; anything else is refused."""
    if provenance in HAND_PROVENANCES:
        return "hand"
    if provenance == "drafted":
        return "drafted"
    raise ValueError(f"provenance {provenance!r} must be one of {list(PROVENANCES)}")


def wilson_interval(num: int, den: int, z: float = 1.96) -> tuple[float, float] | None:
    """The Wilson score interval for *num* of *den* (95 percent at the default *z*); ``None`` over an empty
    denominator. Wilson, not the normal approximation: it stays inside [0, 1] and is not degenerate at 0 of *n*, where
    a rate read from a small sample most needs an honest upper bound (0 errors in 15 pairs still allows about 20
    percent)."""
    if den < 0 or num < 0 or num > den:
        raise ValueError(f"{num} of {den} is not a count")
    if den == 0:
        return None
    p, z2 = num / den, z * z
    denom = 1 + z2 / den
    centre = (p + z2 / (2 * den)) / denom
    half = z * math.sqrt(p * (1 - p) / den + z2 / (4 * den * den)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class ItemLabel:
    chash: str = ""
    doc_path_contains: str = ""
    closed: bool = False
    note: str = ""
    facets: Mapping[str, str] = field(default_factory=dict)

    def matches(self, chash: str, doc_path: str) -> bool:
        if self.chash:
            return bool(chash) and self.chash.lower() == chash.lower()
        return bool(self.doc_path_contains) and bool(doc_path) and self.doc_path_contains.lower() in doc_path.lower()

    def role(self, slug: str) -> str | None:
        return self.facets.get(slug) or ("neutral" if self.closed else None)

    @property
    def level(self) -> str:
        """``document`` for a ``doc_path_contains`` label, ``chunk`` for a ``chash`` label."""
        return "document" if self.doc_path_contains and not self.chash else "chunk"


@dataclass(frozen=True)
class RequestOracle:
    qid: str
    slugs: tuple[str, ...]
    provenance: str
    items: tuple[ItemLabel, ...] = ()

    @property
    def todo(self) -> bool:
        return not self.items

    def label_of(self, chash: str, doc_path: str, slug: str) -> tuple[str | None, str]:
        """``(role, level)`` for (anchor, slug): the role is ``None`` when no item labels it (the level is then ``""``).
        The first matching item decides."""
        for item in self.items:
            if item.matches(chash, doc_path):
                role = item.role(slug)
                return role, (item.level if role is not None else "")
        return None, ""

    def role_of(self, chash: str, doc_path: str, slug: str) -> str | None:
        """The oracle's role for (anchor, slug), or ``None`` when no item labels it. The first matching item decides."""
        return self.label_of(chash, doc_path, slug)[0]

    def labelled_slugs(self) -> frozenset[str]:
        return frozenset(s for item in self.items for s in item.facets)


@dataclass(frozen=True)
class OracleSet:
    requests: Mapping[str, RequestOracle]

    def todo_pairs(self) -> list[tuple[str, str]]:
        """(request, slug) pairs no item labels: what a human pass still owes."""
        return [(qid, s) for qid, r in self.requests.items() for s in r.slugs if s not in r.labelled_slugs()]

    def todo_requests(self) -> list[str]:
        return [qid for qid, r in self.requests.items() if r.todo]

    def summary(self) -> dict[str, Any]:
        pairs = sum(len(r.slugs) for r in self.requests.values())
        todo = self.todo_pairs()
        return {
            "requests": len(self.requests), "requests_labelled": len(self.requests) - len(self.todo_requests()),
            "requests_todo": len(self.todo_requests()), "request_facet_pairs": pairs,
            "pairs_labelled": pairs - len(todo), "pairs_todo": len(todo),
        }

    def provenance_summary(self) -> dict[str, dict[str, int]]:
        """Labelled requests and (request, facet) pairs by ``hand`` and ``drafted``: what each rate in a report rests on."""
        out = {g: {"requests": 0, "pairs": 0} for g in ("hand", "drafted")}
        for r in self.requests.values():
            if r.todo:
                continue
            row = out[provenance_group(r.provenance)]
            row["requests"] += 1
            row["pairs"] += len(r.labelled_slugs())
        return out


# ── load ─────────────────────────────────────────────────────────────────────────────────────────────────


def _check_item(path: Path, qid: str, i: int, row: Mapping[str, Any], slugs: Sequence[str]) -> ItemLabel:
    where = f"{path}: {qid} item {i}"
    keys = [k for k in ("chash", "doc_path_contains") if row.get(k)]
    if len(keys) != 1:
        raise ValueError(f"{where}: exactly one of chash or doc_path_contains is required")
    facets = dict(row.get("facets") or {})
    for slug, role in facets.items():
        if slug not in slugs:
            raise ValueError(f"{where}: facet {slug!r} is not a required facet of the request")
        if role not in ROLES:
            raise ValueError(f"{where}: role {role!r} for {slug!r} must be one of {list(ROLES)}")
    if not facets and not row.get("closed"):
        raise ValueError(f"{where}: an item with no facets must be closed (it then labels every facet neutral)")
    item = ItemLabel(
        chash=str(row.get("chash") or ""), doc_path_contains=str(row.get("doc_path_contains") or ""),
        closed=bool(row.get("closed", False)), note=str(row.get("note") or ""), facets=facets,
    )
    if row.get("level") is not None and row["level"] != item.level:
        raise ValueError(f"{where}: level {row['level']!r} contradicts the item's key (a chash label is chunk-level, a doc_path_contains label is document-level)")
    return item


def _load_requests(path: Path, by_qid: Mapping[str, Query], raw: Mapping[str, Any], *, drafted: bool) -> dict[str, RequestOracle]:
    out: dict[str, RequestOracle] = {}
    for qid, row in raw.items():
        slugs = tuple(by_qid[qid].required_facets)
        provenance = row.get("provenance")
        if provenance not in PROVENANCES:
            raise ValueError(f"{path}: {qid} provenance {provenance!r} must be one of {list(PROVENANCES)} (a label derived from a ledger is circular)")
        if drafted and provenance != "drafted":
            raise ValueError(f"{path}: {qid} provenance {provenance!r}: the drafted file holds drafted labels only")
        if not drafted and provenance == "drafted":
            raise ValueError(f"{path}: {qid}: drafted labels live in {DEFAULT_DRAFTED_PATH.name}, not in the hand file")
        items = tuple(_check_item(path, qid, i, it, slugs) for i, it in enumerate(row.get("items") or []))
        if drafted:
            if not items:
                raise ValueError(f"{path}: {qid} carries no items (a drafted entry labels something)")
        elif bool(row.get("todo")) != (not items):
            raise ValueError(f"{path}: {qid} must carry items or todo: true, not both and not neither")
        out[qid] = RequestOracle(qid, slugs, str(provenance), items)
    return out


def load_oracle(path: Path, queries: Sequence[Query], *, drafted_path: Path | None = None) -> OracleSet:
    """Read and validate the label file against the golden set(s) *queries*. Every golden request must appear
    (labelled, or ``todo: true`` with no items), so a request cannot be silently absent.

    *drafted_path* merges a second file of ``drafted`` labels: it fills only requests the hand file leaves ``todo``
    (a drafted label never shadows a hand label), names no request twice, and must exist when given."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if data.get("oracle_version") != ORACLE_VERSION:
        raise ValueError(f"{path}: oracle_version must be {ORACLE_VERSION}, got {data.get('oracle_version')!r}")
    if data.get("golden_set_version") != GOLDEN_SET_VERSION:
        raise ValueError(f"{path}: golden_set_version must be {GOLDEN_SET_VERSION}, got {data.get('golden_set_version')!r}")
    by_qid = {q.qid: q for q in queries}
    raw = data.get("requests") or {}
    unknown = sorted(set(raw) - set(by_qid))
    if unknown:
        raise ValueError(f"{path}: labels for unknown requests {unknown}")
    missing = sorted(set(by_qid) - set(raw))
    if missing:
        raise ValueError(f"{path}: golden requests without an entry (use todo: true): {missing}")
    out = _load_requests(path, by_qid, raw, drafted=False)
    if drafted_path is not None:
        with open(drafted_path, encoding="utf-8") as f:
            ddata = yaml.safe_load(f) or {}
        if ddata.get("oracle_version") != ORACLE_VERSION or ddata.get("golden_set_version") != GOLDEN_SET_VERSION:
            raise ValueError(f"{drafted_path}: oracle_version and golden_set_version must be {ORACLE_VERSION} and {GOLDEN_SET_VERSION}")
        draw = ddata.get("requests") or {}
        unknown = sorted(set(draw) - set(by_qid))
        if unknown:
            raise ValueError(f"{drafted_path}: drafted labels for unknown requests {unknown}")
        shadowed = sorted(qid for qid in draw if not out[qid].todo)
        if shadowed:
            raise ValueError(f"{drafted_path}: drafted labels may not shadow hand labels; {shadowed} are hand-labelled")
        out.update(_load_requests(drafted_path, by_qid, draw, drafted=True))
    return OracleSet(out)


# ── scaffold: labels derived mechanically from existing hand-labelled data ───────────────────────────────


def derive_fixture_items(pieces: Sequence[tuple[str | None, str]], slugs: Sequence[str]) -> list[dict[str, Any]]:
    """Items for the authored pieces of a fixture document: ``(facet slug or None, text)`` -> a closed item keyed by the
    chunk's sha256. The fixture authored each piece to evidence the facet it names (or none); that authoring IS the
    label, made before any assessor existed."""
    items: list[dict[str, Any]] = []
    for facet, text in pieces:
        if facet is not None and facet not in slugs:
            raise ValueError(f"fixture piece labelled {facet!r}, which the request does not require")
        items.append({
            "chash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "closed": True,
            "note": text.strip().splitlines()[0][:80], "facets": {facet: "supports"} if facet else {},
        })
    return items


def derive_ground_truth_items(query: Query) -> list[dict[str, Any]]:
    """For a request with ONE required facet, each ``ground_truth`` entry graded 3 (the hand grade for "the answer")
    labels its document as supporting that facet. A grade-1 entry is related, not an answer, so it stays unlabeled;
    a multi-facet request is not derived (a document grade says nothing about which facet)."""
    if len(query.required_facets) != 1:
        return []
    slug = query.required_facets[0]
    return [
        {"doc_path_contains": key, "level": "document", "closed": False, "facets": {slug: "supports"}}
        for key, grade in sorted(query.ground_truth.items()) if grade == 3
    ]


def scaffold(queries: Sequence[Query], *, fixture: Mapping[str, Sequence[tuple[str | None, str]]] | None = None) -> dict[str, Any]:
    """The label file as a dict: fixture-derived requests (*fixture* maps qid -> authored pieces), ground-truth-derived
    single-facet requests, and ``todo`` for the rest."""
    fixture = fixture or {}
    requests: dict[str, Any] = {}
    for q in queries:
        if q.qid in fixture:
            requests[q.qid] = {"provenance": "fixture", "items": derive_fixture_items(fixture[q.qid], q.required_facets)}
            continue
        items = derive_ground_truth_items(q)
        if items:
            requests[q.qid] = {"provenance": "ground_truth", "items": items}
        else:
            requests[q.qid] = {"provenance": "human", "todo": True, "items": []}
    return {"oracle_version": ORACLE_VERSION, "golden_set_version": GOLDEN_SET_VERSION, "requests": requests}


# ── scoring ──────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FacetScore:
    fid: str
    assessor_state: str
    assessor_reason: str
    oracle_state: str
    false_negative: bool | None = None  # None: not scorable
    spurious_contradict: bool | None = None  # None: the assessor did not contradict, or not scorable
    #: For an oracle-``supported`` facet, the level of the labels that say so: ``chunk`` when some held chunk is labelled
    #: by its hash, else ``document`` (only a document-path label says it). ``""`` when the facet is not supported.
    oracle_level: str = ""


@dataclass(frozen=True)
class RunScore:
    qid: str
    facets: tuple[FacetScore, ...]
    skipped: bool
    false_skip: bool | None  # None: no skip, or the oracle could not say
    provenance: str = ""
    #: For a false skip, ``chunk`` or ``document``: the level of the labels that make the oracle say a facet was supported.
    skip_level: str = ""


def _anchors_by_id(coverage: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(a.get("item_id")): a for a in coverage.get("anchors") or [] if isinstance(a, Mapping)}


def _labels(oracle: RequestOracle, anchor: Mapping[str, Any], slugs: Sequence[str], doc_path_of: DocPathOf | None) -> list[tuple[str | None, str]]:
    path = doc_path_of(anchor) if doc_path_of is not None else ""
    chash = str(anchor.get("chash") or "")
    return [oracle.label_of(chash, path, s) for s in slugs]


def _roles(oracle: RequestOracle, anchor: Mapping[str, Any], slugs: Sequence[str], doc_path_of: DocPathOf | None) -> list[str | None]:
    return [role for role, _level in _labels(oracle, anchor, slugs, doc_path_of)]


def oracle_state(oracle: RequestOracle, coverage: Mapping[str, Any], slugs: Sequence[str], doc_path_of: DocPathOf | None = None) -> str:
    """``supported`` | ``contradicted`` | ``not_supported`` | ``unknown`` for a facet carrying *slugs*, over the anchors the
    run's final ledger held."""
    anchors = list(_anchors_by_id(coverage).values())
    if not slugs or not anchors:
        return "unknown"
    all_roles = [_roles(oracle, a, slugs, doc_path_of) for a in anchors]
    flat = [r for roles in all_roles for r in roles]
    if "supports" in flat:
        return "supported"
    if "contradicts" in flat:
        return "contradicted"
    return "not_supported" if all(r is not None for r in flat) else "unknown"


def oracle_support_level(oracle: RequestOracle, coverage: Mapping[str, Any], slugs: Sequence[str], doc_path_of: DocPathOf | None = None) -> str:
    """``chunk`` when some held anchor is labelled ``supports`` by a chunk-level label, ``document`` when the only
    ``supports`` labels are document-level, ``""`` when nothing is labelled ``supports``."""
    levels = {
        level for a in _anchors_by_id(coverage).values() for role, level in _labels(oracle, a, slugs, doc_path_of) if role == "supports"
    }
    return "chunk" if "chunk" in levels else ("document" if "document" in levels else "")


def score_run(
    oracle: RequestOracle, coverage: Mapping[str, Any], resolution: FacetResolution, doc_path_of: DocPathOf | None = None,
) -> RunScore:
    """Score one run's final ledger (the envelope's ``coverage`` object) against *oracle*."""
    anchors = _anchors_by_id(coverage)
    scores: list[FacetScore] = []
    for f in coverage.get("findings") or []:
        fid, state, reason = str(f.get("facet_id")), str(f.get("state")), str(f.get("reason") or "")
        slugs = resolution.fid_to_slugs.get(fid, ())
        o_state = oracle_state(oracle, coverage, slugs, doc_path_of) if slugs else "unmapped"
        fn: bool | None = None
        if o_state == "supported" and state in ("covered", "unsupported", "contradicted") and reason not in DETERMINISTIC_REASONS:
            fn = state != "covered"
        spurious: bool | None = None
        if state == "contradicted" or (state == "covered" and reason == "conflicted"):
            cited = [anchors[i] for i in f.get("evidence_ids") or [] if i in anchors]
            cited_roles = [_roles(oracle, a, slugs, doc_path_of) for a in cited] if slugs else []
            if any("contradicts" in roles for roles in cited_roles):
                spurious = False
            elif cited_roles and all(any(r is not None for r in roles) for roles in cited_roles):
                spurious = True
        level = oracle_support_level(oracle, coverage, slugs, doc_path_of) if o_state == "supported" else ""
        scores.append(FacetScore(fid, state, reason, o_state, fn, spurious, level))
    skipped = bool(coverage.get("skip_generation"))
    false_skip: bool | None = None
    skip_level = ""
    if skipped:
        states = [s.oracle_state for s in scores]
        if "supported" in states:
            false_skip = True
            skip_level = "chunk" if any(s.oracle_level == "chunk" for s in scores) else "document"
        elif states and all(s in ("not_supported", "contradicted") for s in states):
            false_skip = False
    return RunScore(oracle.qid, tuple(scores), skipped, false_skip, oracle.provenance, skip_level)


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def _ci(num: int, den: int) -> list[float] | None:
    ci = wilson_interval(num, den)
    return None if ci is None else [ci[0], ci[1]]


def _aggregate_group(scores: Sequence[RunScore]) -> dict[str, Any]:
    """One provenance group's error rates, each with its numerator, denominator, unscored count and 95 percent interval.
    The main blocks are CHUNK-level; ``document_level`` holds what only a document-path label can say."""
    facets = [f for s in scores for f in s.facets]
    chunk_facets = [f for f in facets if f.oracle_level != "document"]
    doc_facets = [f for f in facets if f.oracle_level == "document"]
    judged = [f for f in chunk_facets if f.false_negative is not None]
    fn = sum(1 for f in judged if f.false_negative)
    supported_unassessed = sum(1 for f in chunk_facets if f.oracle_state == "supported" and f.assessor_state == "unassessed")
    doc_judged = [f for f in doc_facets if f.false_negative is not None]
    doc_fn = sum(1 for f in doc_judged if f.false_negative)
    flagged = [f for f in facets if f.assessor_state == "contradicted" or (f.assessor_state == "covered" and f.assessor_reason == "conflicted")]
    scored_flags = [f for f in flagged if f.spurious_contradict is not None]
    spurious = sum(1 for f in scored_flags if f.spurious_contradict)
    skips = [s for s in scores if s.skipped]
    scored_skips = [s for s in skips if s.false_skip is not None]
    chunk_skips = [s for s in scored_skips if s.skip_level != "document"]
    doc_skips = [s for s in scored_skips if s.skip_level == "document"]
    false_skips = sum(1 for s in chunk_skips if s.false_skip)
    negative = [f for f in facets if f.oracle_state == "not_supported"]
    neg_by_state: dict[str, int] = {}
    for f in negative:
        neg_by_state[f.assessor_state] = neg_by_state.get(f.assessor_state, 0) + 1
    falsely_covered = neg_by_state.get("covered", 0)
    return {
        "runs": len(scores), "facets": len(facets), "provenances": dict(Counter(s.provenance for s in scores)),
        "oracle_state_counts": {k: sum(1 for f in facets if f.oracle_state == k) for k in ("supported", "contradicted", "not_supported", "unknown", "unmapped")},
        "false_negative": {
            "rate": _rate(fn, len(judged)), "ci95": _ci(fn, len(judged)), "numerator": fn, "denominator": len(judged),
            "supported_but_unassessed": supported_unassessed,
        },
        "spurious_contradicts": {
            "rate": _rate(spurious, len(scored_flags)), "ci95": _ci(spurious, len(scored_flags)), "numerator": spurious,
            "denominator": len(scored_flags), "flagged": len(flagged), "unscored": len(flagged) - len(scored_flags),
        },
        "false_skip": {
            "rate_of_skips": _rate(false_skips, len(chunk_skips)), "ci95_of_skips": _ci(false_skips, len(chunk_skips)),
            "numerator": false_skips, "denominator": len(chunk_skips), "skips": len(skips), "unscored": len(skips) - len(scored_skips),
            "rate_of_runs": _rate(false_skips, len(scores)), "ci95_of_runs": _ci(false_skips, len(scores)),
        },
        "negative_control": {
            "facets": len(negative), "assessor_state": neg_by_state,
            "falsely_covered": _rate(falsely_covered, len(negative)), "falsely_covered_ci95": _ci(falsely_covered, len(negative)),
        },
        # What only a document-path label says (G01-G03): "the gold document supports the facet". Any held chunk of the
        # document makes the facet supported, so an assessor that finds the fact absent from the chunk it was shown is
        # charged here, not in the chunk-level rate above.
        "document_level": {
            "note": "labels that say a DOCUMENT supports a facet; the assessor reads a chunk, so a miss here may be the chunk's, not the assessor's",
            "false_negative": {
                "rate": _rate(doc_fn, len(doc_judged)), "ci95": _ci(doc_fn, len(doc_judged)), "numerator": doc_fn, "denominator": len(doc_judged),
            },
            "false_skip": {
                "rate_of_skips": _rate(len(doc_skips), len(scored_skips)), "ci95_of_skips": _ci(len(doc_skips), len(scored_skips)),
                "numerator": len(doc_skips), "denominator": len(scored_skips),
            },
        },
    }


def aggregate(scores: Sequence[RunScore]) -> dict[str, Any]:
    """The assessor's error rates, ``hand`` and ``drafted`` apart: drafted labels are an agent's, spot-checked, and a
    rate over them must never be read as a rate over hand labels, so the two are never pooled. A run whose provenance is
    not recorded is refused."""
    groups: dict[str, list[RunScore]] = {"hand": [], "drafted": []}
    for s in scores:
        groups[provenance_group(s.provenance)].append(s)
    return {g: _aggregate_group(v) for g, v in groups.items()}


# ── the human spot check of drafted labels ───────────────────────────────────────────────────────────────

SPOT_CHECK_VERSION = 1
#: The strata a spot check draws at random from the pairs the assessor and the drafter agree on, with their minimum size.
MIN_SUPPORTS_MULTI = 5
MIN_NEUTRAL = 3


class SpotCase(NamedTuple):
    """One run to sample from: the request's oracle, the envelope's ``coverage`` object and the facet resolution."""

    oracle: RequestOracle
    coverage: Mapping[str, Any]
    resolution: FacetResolution


@dataclass(frozen=True)
class SpotPair:
    """One (item, facet) drafted label for a person to check. ``checked`` and ``label_correct`` are the person's marks."""

    qid: str
    item_id: str
    chash: str
    slug: str
    drafted_role: str
    stratum: str
    checked: bool = False
    label_correct: bool | None = None

    def row(self) -> dict[str, Any]:
        return {
            "qid": self.qid, "item_id": self.item_id, "chash": self.chash, "slug": self.slug, "drafted_role": self.drafted_role,
            "stratum": self.stratum, "checked": self.checked, "label_correct": self.label_correct,
        }


@dataclass(frozen=True)
class SpotSample:
    pairs: tuple[SpotPair, ...]
    #: Per stratum, how many pairs short of its minimum the pool was (absent: the minimum was met).
    shortfall: Mapping[str, int]
    #: Per stratum, how many pairs were there to draw from.
    pool: Mapping[str, int]

    def rows(self) -> list[dict[str, Any]]:
        return [p.row() for p in self.pairs]


#: A finding reason for a facet covered while other items contradict it (coverage_ledger.CONFLICTED_REASON).
CONFLICTED = "conflicted"


def _pair_disagrees(role: str, item_id: str, fids: Sequence[str], findings: Mapping[str, Mapping[str, Any]]) -> bool:
    """Whether the assessor's ledger contradicts the drafter's *role* for this item on a facet carried by *fids*. Only
    facets the ledger has a finding for can disagree; a facet the join leaves unmapped cannot be compared."""
    known = [findings[f] for f in fids if f in findings]
    if not known:
        return False
    if role == "supports" and not any(f.get("state") == "covered" for f in known):
        return True  # the drafter says the item supports the facet; the assessor did not cover it (a false negative)
    for f in known:
        if item_id not in (f.get("evidence_ids") or []):
            continue
        if f.get("state") == "covered" and (role == "neutral" or (role == "contradicts" and f.get("reason") != CONFLICTED)):
            return True  # cited as support of a covered facet, which the drafter says it does not support
        if f.get("state") == "contradicted" and role != "contradicts":
            return True
    return False



def spot_check_sample(
    cases: Sequence[SpotCase], *, seed: int = 0, min_supports_multi: int = MIN_SUPPORTS_MULTI, min_neutral: int = MIN_NEUTRAL,
    doc_path_of: DocPathOf | None = None,
) -> SpotSample:
    """The pairs a person checks (critique S3 d): EVERY pair where the assessor and the drafter disagree, plus at least
    *min_supports_multi* drafted ``supports`` labels on multi-facet requests (the destructive direction: a wrongly
    drafted support makes a correct assessor look wrong) and at least *min_neutral* drafted ``neutral`` labels, the
    last two drawn at random (seeded) from the pairs the two AGREE on, so the sample is not made of the pairs most
    likely to be wrong. A pool smaller than its minimum is recorded in ``shortfall``, never filled quietly.

    Only ``drafted`` requests are sampled: a hand label is not spot-checked."""
    disagree: list[SpotPair] = []
    supports_pool: list[SpotPair] = []
    neutral_pool: list[SpotPair] = []
    for case in cases:
        o = case.oracle
        if provenance_group(o.provenance) != "drafted":
            continue
        findings = {str(f.get("facet_id")): f for f in case.coverage.get("findings") or [] if isinstance(f, Mapping)}
        multi = len(o.slugs) > 1
        for item_id, anchor in _anchors_by_id(case.coverage).items():
            chash = str(anchor.get("chash") or "")
            path = doc_path_of(anchor) if doc_path_of is not None else ""
            for slug in o.slugs:
                role, _level = o.label_of(chash, path, slug)
                if role is None:
                    continue
                if _pair_disagrees(role, item_id, case.resolution.slug_to_fids.get(slug, ()), findings):
                    disagree.append(SpotPair(o.qid, item_id, chash, slug, role, "disagreement"))
                elif role == "supports" and multi:
                    supports_pool.append(SpotPair(o.qid, item_id, chash, slug, role, "supports_multi_facet"))
                elif role == "neutral":
                    neutral_pool.append(SpotPair(o.qid, item_id, chash, slug, role, "neutral"))
    rng = random.Random(seed)

    def key(p: SpotPair) -> tuple[str, str, str]:
        return (p.qid, p.item_id, p.slug)

    def draw(pool: list[SpotPair], k: int) -> list[SpotPair]:
        return sorted(rng.sample(sorted(pool, key=key), min(k, len(pool))), key=key)

    chosen_supports = draw(supports_pool, min_supports_multi)
    chosen_neutral = draw(neutral_pool, min_neutral)
    shortfall = {
        name: want - len(got)
        for name, want, got in (("supports_multi_facet", min_supports_multi, chosen_supports), ("neutral", min_neutral, chosen_neutral))
        if len(got) < want
    }
    return SpotSample(
        tuple([*sorted(disagree, key=key), *chosen_supports, *chosen_neutral]), shortfall,
        {"supports_multi_facet": len(supports_pool), "neutral": len(neutral_pool), "disagreement": len(disagree)},
    )


def write_spot_check(path: Path, sample: SpotSample) -> None:
    """Write the sample for a person to mark: each pair carries ``checked: false`` and ``label_correct: null``."""
    body = {"spot_check_version": SPOT_CHECK_VERSION, "shortfall": dict(sample.shortfall), "pool": dict(sample.pool), "pairs": sample.rows()}
    path.write_text(yaml.safe_dump(body, sort_keys=False, width=100), encoding="utf-8")


def summarize_spot_check(path: Path) -> dict[str, Any]:
    """Read a (partly) marked spot-check file: how many pairs were sampled and checked, how many labels were wrong, and
    the 95 percent interval on the error rate. The rate over the agreeing strata is the estimate of drafted-label
    error; the disagreement stratum is the pairs most likely to be wrong and is not."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if data.get("spot_check_version") != SPOT_CHECK_VERSION:
        raise ValueError(f"{path}: spot_check_version must be {SPOT_CHECK_VERSION}")
    pairs = data.get("pairs") or []
    for p in pairs:
        if p.get("checked") and not isinstance(p.get("label_correct"), bool):
            raise ValueError(f"{path}: {p.get('qid')} {p.get('item_id')} {p.get('slug')} is checked but label_correct is not true or false")
    checked = [p for p in pairs if p.get("checked")]
    errors = sum(1 for p in checked if p["label_correct"] is False)
    agreeing = [p for p in checked if p.get("stratum") != "disagreement"]
    agreeing_errors = sum(1 for p in agreeing if p["label_correct"] is False)
    by_stratum: dict[str, dict[str, int]] = {}
    for p in pairs:
        row = by_stratum.setdefault(str(p.get("stratum")), {"sampled": 0, "checked": 0, "errors": 0})
        row["sampled"] += 1
        row["checked"] += int(bool(p.get("checked")))
        row["errors"] += int(bool(p.get("checked")) and p["label_correct"] is False)
    return {
        "sampled": len(pairs), "checked": len(checked), "unchecked": len(pairs) - len(checked), "errors": errors,
        "error_rate": _rate(errors, len(checked)), "error_rate_ci95": _ci(errors, len(checked)),
        "error_rate_agreeing_strata": {
            "checked": len(agreeing), "errors": agreeing_errors, "rate": _rate(agreeing_errors, len(agreeing)), "ci95": _ci(agreeing_errors, len(agreeing)),
        },
        "by_stratum": by_stratum, "shortfall": dict(data.get("shortfall") or {}),
    }


# ── cli ──────────────────────────────────────────────────────────────────────────────────────────────────


def _g06_fixture_pieces() -> dict[str, list[tuple[str | None, str]]]:
    """The G06 fixture's authored pieces (``tests/_rdr232_golden.py``): imported from the repository, not copied."""
    root = Path(__file__).resolve().parents[2]
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from tests._rdr232_golden import ART_RDR_PIECES  # noqa: PLC0415 — test-support module, only the scaffold writer needs it

    return {"G06-synthesis-art-rdr084": list(ART_RDR_PIECES)}


FILE_HEADER = """\
# RDR-232 item-by-facet oracle labels v1 (nexus-jypmj.3.10). Format and scoring: scripts/bench/rdr232_oracle.py.
#
# Labels say which retrieved item bears on which golden facet, independently of any assessor. They are NEVER read
# off a coverage ledger (that would score the assessor with itself). Three hand provenances:
#   fixture       the authored piece labels of the ART RDR-084 fixture (tests/_rdr232_golden.py). The content is
#                 synthetic, so these labels score runs over the FIXTURE corpus (a local engine), not the live ART
#                 RDR-084 in the cloud tenant.
#   ground_truth  a single-facet request's hand `ground_truth` grade-3 documents (golden_v1.yaml) support its facet.
#                 Document-level (`level: document`): an item is matched by a substring of its document path, so it says
#                 the DOCUMENT supports the facet and is scored apart from chunk-level labels.
#   human         todo: true until a human labels items.
# A fourth provenance, `drafted`, lives in golden_oracle_labels_drafted_v1.yaml (see rdr232_oracle.py). 
# Regenerate the fixture and ground_truth entries with `python scripts/bench/rdr232_oracle.py scaffold --write`
# (tests/test_rdr232_oracle.py pins that the committed entries equal the derivation). Edit `human` entries by hand.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RDR-232 oracle label scaffold: write, check or summarise the label file")
    ap.add_argument("action", choices=("summary", "scaffold", "spot-summary"))
    ap.add_argument("--path", type=Path, default=DEFAULT_ORACLE_PATH, help="the label file (spot-summary: the marked spot-check file)")
    ap.add_argument("--write", action="store_true", help="scaffold: write the file (otherwise print it)")
    args = ap.parse_args(argv)
    if args.action == "spot-summary":
        click.echo(json.dumps(summarize_spot_check(args.path), indent=2))
        return 0
    queries = [*load_golden_set(GOLDEN_PATH), *load_golden_set(HELDOUT_PATH)]
    if args.action == "summary":
        click.echo(json.dumps(load_oracle(args.path, queries).summary(), indent=2))
        return 0
    text = FILE_HEADER.replace(" \n", "\n") + yaml.safe_dump(scaffold(queries, fixture=_g06_fixture_pieces()), sort_keys=False, width=100)
    if args.write:
        args.path.write_text(text, encoding="utf-8")
    else:
        click.echo(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
