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
        provenance: fixture            # fixture | ground_truth | human
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
document path, which the caller resolves: the envelope's anchors carry a tumbler, not a path). ``closed: false`` says
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
PROVENANCES: tuple[str, ...] = ("fixture", "ground_truth", "human")
#: Finding reasons decided by a deterministic row, not by the assessor.
DETERMINISTIC_REASONS: frozenset[str] = frozenset({"source_missing", "no_evidence", "scope_conflict"})

DocPathOf = Callable[[Mapping[str, Any]], str]


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


@dataclass(frozen=True)
class RequestOracle:
    qid: str
    slugs: tuple[str, ...]
    provenance: str
    items: tuple[ItemLabel, ...] = ()

    @property
    def todo(self) -> bool:
        return not self.items

    def role_of(self, chash: str, doc_path: str, slug: str) -> str | None:
        """The oracle's role for (anchor, slug), or ``None`` when no item labels it. The first matching item decides."""
        for item in self.items:
            if item.matches(chash, doc_path):
                return item.role(slug)
        return None

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
    return ItemLabel(
        chash=str(row.get("chash") or ""), doc_path_contains=str(row.get("doc_path_contains") or ""),
        closed=bool(row.get("closed", False)), note=str(row.get("note") or ""), facets=facets,
    )


def load_oracle(path: Path, queries: Sequence[Query]) -> OracleSet:
    """Read and validate the label file against the golden set(s) *queries*. Every golden request must appear
    (labelled, or ``todo: true`` with no items), so a request cannot be silently absent."""
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
    out: dict[str, RequestOracle] = {}
    for qid, row in raw.items():
        slugs = tuple(by_qid[qid].required_facets)
        provenance = row.get("provenance")
        if provenance not in PROVENANCES:
            raise ValueError(f"{path}: {qid} provenance {provenance!r} must be one of {list(PROVENANCES)} (a label derived from a ledger is circular)")
        items = tuple(_check_item(path, qid, i, it, slugs) for i, it in enumerate(row.get("items") or []))
        if bool(row.get("todo")) != (not items):
            raise ValueError(f"{path}: {qid} must carry items or todo: true, not both and not neither")
        out[qid] = RequestOracle(qid, slugs, str(provenance), items)
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
        {"doc_path_contains": key, "closed": False, "facets": {slug: "supports"}}
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


@dataclass(frozen=True)
class RunScore:
    qid: str
    facets: tuple[FacetScore, ...]
    skipped: bool
    false_skip: bool | None  # None: no skip, or the oracle could not say


def _anchors_by_id(coverage: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(a.get("item_id")): a for a in coverage.get("anchors") or [] if isinstance(a, Mapping)}


def _roles(oracle: RequestOracle, anchor: Mapping[str, Any], slugs: Sequence[str], doc_path_of: DocPathOf | None) -> list[str | None]:
    path = doc_path_of(anchor) if doc_path_of is not None else ""
    chash = str(anchor.get("chash") or "")
    return [oracle.role_of(chash, path, s) for s in slugs]


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
        scores.append(FacetScore(fid, state, reason, o_state, fn, spurious))
    skipped = bool(coverage.get("skip_generation"))
    false_skip: bool | None = None
    if skipped:
        states = [s.oracle_state for s in scores]
        if "supported" in states:
            false_skip = True
        elif states and all(s in ("not_supported", "contradicted") for s in states):
            false_skip = False
    return RunScore(oracle.qid, tuple(scores), skipped, false_skip)


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def aggregate(scores: Sequence[RunScore]) -> dict[str, Any]:
    """The assessor's error rates over *scores*, each with its numerator, denominator and unscored count."""
    facets = [f for s in scores for f in s.facets]
    judged = [f for f in facets if f.false_negative is not None]
    fn = sum(1 for f in judged if f.false_negative)
    supported_unassessed = sum(1 for f in facets if f.oracle_state == "supported" and f.assessor_state == "unassessed")
    flagged = [f for f in facets if f.assessor_state == "contradicted" or (f.assessor_state == "covered" and f.assessor_reason == "conflicted")]
    scored_flags = [f for f in flagged if f.spurious_contradict is not None]
    spurious = sum(1 for f in scored_flags if f.spurious_contradict)
    skips = [s for s in scores if s.skipped]
    scored_skips = [s for s in skips if s.false_skip is not None]
    false_skips = sum(1 for s in scored_skips if s.false_skip)
    negative = [f for f in facets if f.oracle_state == "not_supported"]
    neg_by_state: dict[str, int] = {}
    for f in negative:
        neg_by_state[f.assessor_state] = neg_by_state.get(f.assessor_state, 0) + 1
    return {
        "runs": len(scores), "facets": len(facets),
        "oracle_state_counts": {k: sum(1 for f in facets if f.oracle_state == k) for k in ("supported", "contradicted", "not_supported", "unknown", "unmapped")},
        "false_negative": {
            "rate": _rate(fn, len(judged)), "numerator": fn, "denominator": len(judged),
            "supported_but_unassessed": supported_unassessed,
        },
        "spurious_contradicts": {
            "rate": _rate(spurious, len(scored_flags)), "numerator": spurious, "denominator": len(scored_flags),
            "flagged": len(flagged), "unscored": len(flagged) - len(scored_flags),
        },
        "false_skip": {
            "rate_of_skips": _rate(false_skips, len(scored_skips)), "numerator": false_skips,
            "denominator": len(scored_skips), "skips": len(skips), "unscored": len(skips) - len(scored_skips),
            "rate_of_runs": _rate(false_skips, len(scores)),
        },
        "negative_control": {
            "facets": len(negative), "assessor_state": neg_by_state,
            "falsely_covered": _rate(neg_by_state.get("covered", 0), len(negative)),
        },
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
# off a coverage ledger (that would score the assessor with itself). Three provenances:
#   fixture       the authored piece labels of the ART RDR-084 fixture (tests/_rdr232_golden.py). The content is
#                 synthetic, so these labels score runs over the FIXTURE corpus (a local engine), not the live ART
#                 RDR-084 in the cloud tenant.
#   ground_truth  a single-facet request's hand `ground_truth` grade-3 documents (golden_v1.yaml) support its facet.
#                 Document-level: an item is matched by a substring of its document path.
#   human         todo: true until a human labels items. 
# Regenerate the fixture and ground_truth entries with `python scripts/bench/rdr232_oracle.py scaffold --write`
# (tests/test_rdr232_oracle.py pins that the committed entries equal the derivation). Edit `human` entries by hand.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RDR-232 oracle label scaffold: write, check or summarise the label file")
    ap.add_argument("action", choices=("summary", "scaffold"))
    ap.add_argument("--path", type=Path, default=DEFAULT_ORACLE_PATH)
    ap.add_argument("--write", action="store_true", help="scaffold: write the file (otherwise print it)")
    args = ap.parse_args(argv)
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
