# SPDX-License-Identifier: AGPL-3.0-or-later
"""``Query`` dataclass + YAML loader for the bench harness."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_CATEGORIES = frozenset({"factual", "comparative", "compositional"})

#: RDR-232 golden set (nexus-jypmj.1.1). The file-level version stamp every golden set carries; a
#: change to the label vocabulary or a relabeling of existing requests bumps it, so a baseline is
#: never compared against a differently labeled set.
GOLDEN_SET_VERSION = 1

#: The request kinds a golden request is labeled with, by hand and independently of any plan's own
#: declaration (RDR-232 Phase 2 exit criteria depend on that). ``unknown`` labels a request whose
#: phrasing does not settle a kind; ``discovery`` covers listing requests.
ANSWER_KINDS = frozenset({
    "lookup", "synthesis", "discovery", "comparison", "audit", "debug", "unknown",
})

#: Collection-family prefixes a request may allow evidence from.
ALLOWED_CORPUS_PREFIXES = frozenset({"knowledge", "code", "docs", "rdr"})

#: Labels a golden request must carry, and the kind of value each takes.
GOLDEN_LIST_FIELDS = (
    "required_sources", "required_facets", "allowed_corpora", "unacceptable_plans",
)
GOLDEN_REQUIRED_FIELDS = ("answer_kind", *GOLDEN_LIST_FIELDS)


@dataclass
class Query:
    """One bench query with manually-labeled GT.

    ``ground_truth`` maps an RDR-file basename substring (e.g.
    ``"rdr-049-"``) to a relevance grade in {0, 1, 2, 3}, where 3 is
    most relevant. See ``bench.metrics.grade_for_path`` for the matching
    semantics.

    ``scope`` is forwarded to the path-B/C handlers when set; an empty
    string means "let the handler choose its own default".
    """

    qid: str
    category: str
    text: str
    ground_truth: dict[str, int] = field(default_factory=dict)
    scope: str = ""
    #: RDR-232 golden-set labels (empty on a pre-golden fixture such as ``spike_5q.yaml``).
    #: ``required_sources`` are source refs the answer must draw on (``RDR-084@ART``);
    #: ``required_facets`` are stable facet ids the request asks for; ``allowed_corpora`` are the
    #: collection-family prefixes evidence may come from; ``unacceptable_plans`` are
    #: plan keys that would be the wrong shape for this request: ``<verb>:<strategy>`` for a
    #: builtin plan, ``grown:<strategy>`` for a grown plan (nexus-jypmj.1.14 names the fixture's
    #: clone of plan 473 that way; no builtin can produce a ``grown:`` key).
    answer_kind: str = ""
    required_sources: list[str] = field(default_factory=list)
    required_facets: list[str] = field(default_factory=list)
    allowed_corpora: list[str] = field(default_factory=list)
    unacceptable_plans: list[str] = field(default_factory=list)


def _validate(query: dict, idx: int) -> None:
    for required in ("qid", "category", "text"):
        if required not in query:
            raise ValueError(
                f"queries[{idx}]: missing required field {required!r}"
            )
    if query["category"] not in VALID_CATEGORIES:
        raise ValueError(
            f"queries[{idx}]: unknown category {query['category']!r}; "
            f"must be one of {sorted(VALID_CATEGORIES)}"
        )


def _validate_golden(query: dict, idx: int) -> None:
    """Strict label checks for a golden-set request (``load_golden_set``)."""
    for required in GOLDEN_REQUIRED_FIELDS:
        if required not in query:
            raise ValueError(
                f"queries[{idx}]: missing required golden field {required!r}"
            )
    if query["answer_kind"] not in ANSWER_KINDS:
        raise ValueError(
            f"queries[{idx}]: unknown answer_kind {query['answer_kind']!r}; "
            f"must be one of {sorted(ANSWER_KINDS)}"
        )
    for name in GOLDEN_LIST_FIELDS:
        value = query[name]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(
                f"queries[{idx}]: {name!r} must be a list of non-empty strings, got {value!r}"
            )
    unknown = sorted(set(query["allowed_corpora"]) - ALLOWED_CORPUS_PREFIXES)
    if unknown:
        raise ValueError(
            f"queries[{idx}]: allowed_corpora {unknown} not in "
            f"{sorted(ALLOWED_CORPUS_PREFIXES)}"
        )


def load_queries(path: Path, *, _golden: bool = False) -> list[Query]:
    """Load a YAML file of queries.

    Schema:

    .. code-block:: yaml

        queries:
          - qid: Q1-factual-tumblers
            category: factual
            text: "Which RDR introduced catalog tumblers?"
            ground_truth:
              "rdr-049-": 3
              "rdr-053-": 1
            scope: "rdr"   # optional

    A request may also carry the RDR-232 golden-set labels (``answer_kind``,
    ``required_sources``, ``required_facets``, ``allowed_corpora``,
    ``unacceptable_plans``); one that names ``answer_kind`` must carry them all. A file that
    must be a golden set is read with :func:`load_golden_set`, which also demands
    ``golden_set_version``.
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict) or "queries" not in data:
        raise ValueError(f"{path}: top-level 'queries' key required")
    raw = data["queries"]
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'queries' must be a list")
    out: list[Query] = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            raise ValueError(f"{path}: queries[{i}] must be a mapping")
        _validate(q, i)
        if _golden or "answer_kind" in q:
            _validate_golden(q, i)
        out.append(Query(
            qid=str(q["qid"]),
            category=str(q["category"]),
            text=str(q["text"]),
            ground_truth=dict(q.get("ground_truth", {})),
            scope=str(q.get("scope", "")),
            answer_kind=str(q.get("answer_kind", "")),
            required_sources=list(q.get("required_sources", [])),
            required_facets=list(q.get("required_facets", [])),
            allowed_corpora=list(q.get("allowed_corpora", [])),
            unacceptable_plans=list(q.get("unacceptable_plans", [])),
        ))
    return out


def load_golden_set(path: Path) -> list[Query]:
    """Load a golden set (RDR-232): every request carries every label, and the file declares
    ``golden_set_version: 1``. Rejects a missing field, an unknown kind or corpus prefix, a
    missing or unsupported version, and a duplicate ``qid``."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level mapping required")
    version = data.get("golden_set_version")
    if version != GOLDEN_SET_VERSION:
        raise ValueError(
            f"{path}: golden_set_version must be {GOLDEN_SET_VERSION}, got {version!r}"
        )
    out = load_queries(path, _golden=True)
    seen: set[str] = set()
    for q in out:
        if q.qid in seen:
            raise ValueError(f"{path}: duplicate qid {q.qid!r}")
        seen.add(q.qid)
    return out
