# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 P1.1 (bead nexus-lqo4p.1) — the recall harness.

Measures recall@k for a query set against a named collection set, on either
retrieval route. It takes NO number by itself: P1.2 takes the vector-only
before-number and P1.4 re-runs the identical query set through
``hybrid_search``. This module and ``tests/fixtures/rdr217_lexical_query_set.json``
are the artifact that makes those two readings comparable.

WHY THE RECALL COMPUTATION IS UNDER TEST rather than written inline at the call
site: an off-by-one in it silently corrupts the decision the whole phase feeds,
and the failure is invisible — a recall figure that is wrong by one rank still
looks like a recall figure. ``tests/test_rdr217_recall_harness.py`` pins it,
including the boundaries.

GROUND TRUTH IS RESOLVED FROM THE CORPUS, NOT HAND-LABELLED. Both relevance
kinds are deterministic functions of the indexed chunks, so the same query set
resolves identically on any box holding the same index, and nobody has to trust
a hand-written list of chunk hashes. See the artifact's ``relevance_model``
section for the bias literal containment carries for identifier queries, why it
is the task definition rather than a thumb on the scale for that shape, and why
the prose queries exist.

NO CLIENT-SIDE RE-RANKING HAPPENS HERE, deliberately, and the artifact's
``ordering`` section carries the reasoning. Rows are consumed in engine order.
There is no sort key in this module, which is how it avoids inheriting the trap
a sibling instrument fell into when a field moved out from under its sort.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

QUERY_SET_PATH = Path(__file__).parent / "fixtures" / "rdr217_lexical_query_set.json"

#: The three query shapes. A shape outside this set is a schema error, not a
#: new category to be tolerated silently — the report groups by shape, and an
#: unrecognised one would vanish from the grouping rather than announce itself.
SHAPES: frozenset[str] = frozenset({"identifier", "rare_token", "prose"})

#: The relevance kinds the resolver understands.
GROUND_TRUTH_KINDS: frozenset[str] = frozenset({"literal_token", "source_path_contains"})


@dataclass(frozen=True)
class Query:
    id: str
    shape: str
    text: str
    ground_truth_kind: str
    ground_truth_value: str
    why: str


@dataclass(frozen=True)
class QuerySet:
    schema_version: int
    corpus_prefixes: tuple[str, ...]
    queries: tuple[Query, ...]


@dataclass(frozen=True)
class QueryResult:
    query_id: str
    shape: str
    relevant: int
    retrieved: int
    hits: int
    recall: float | None  # None when the corpus holds no relevant chunk at all


@dataclass(frozen=True)
class RecallReport:
    route: str
    k: int
    collections: tuple[str, ...]
    per_query: tuple[QueryResult, ...]

    def macro_recall(self, shape: str | None = None) -> float | None:
        """Mean of the per-query recalls, optionally within one shape.

        MACRO, not micro, and the difference matters for this query set: a
        micro average pools hits across queries and would let one identifier
        with many relevant chunks dominate every other query in its shape.
        Queries with ``recall is None`` (no relevant chunk in the corpus) are
        EXCLUDED rather than counted as zero — a query the corpus cannot
        answer measures the corpus, not the route.
        """
        scored = [
            r.recall for r in self.per_query
            if r.recall is not None and (shape is None or r.shape == shape)
        ]
        if not scored:
            return None
        return sum(scored) / len(scored)

    def unanswerable(self) -> tuple[str, ...]:
        """Query ids whose ground truth resolved to nothing in this corpus.

        Reported, never silently dropped: a query set where several of these
        appear is not measuring what it claims, and that is a fact about the
        run the report must carry rather than absorb.
        """
        return tuple(r.query_id for r in self.per_query if r.recall is None)


class _Retriever(Protocol):
    def search(self, query: str, collection_names: list[str], n_results: int = ...) -> Any: ...
    def hybrid_search(self, query: str, collection_names: list[str], n_results: int = ...) -> Any: ...


def load_query_set(path: Path | None = None) -> QuerySet:
    """Load and VALIDATE the artifact. A malformed entry raises here rather
    than producing a quietly smaller measurement."""
    raw = json.loads((path or QUERY_SET_PATH).read_text())
    queries: list[Query] = []
    seen: set[str] = set()
    for entry in raw["queries"]:
        qid = entry["id"]
        if qid in seen:
            raise ValueError(f"duplicate query id {qid!r}: it would be counted twice")
        seen.add(qid)
        shape = entry["shape"]
        if shape not in SHAPES:
            raise ValueError(f"{qid}: unknown shape {shape!r}; known: {sorted(SHAPES)}")
        gt = entry["ground_truth"]
        kind = gt["kind"]
        if kind not in GROUND_TRUTH_KINDS:
            raise ValueError(f"{qid}: unknown ground_truth kind {kind!r}")
        value = gt["token"] if kind == "literal_token" else gt["needle"]
        if not value:
            raise ValueError(f"{qid}: empty ground truth would match everything")
        queries.append(Query(
            id=qid, shape=shape, text=entry["text"],
            ground_truth_kind=kind, ground_truth_value=value,
            why=entry["why"],
        ))
    if not queries:
        raise ValueError("the query set is empty; a measurement over it would be vacuous")
    return QuerySet(
        schema_version=raw["schema_version"],
        corpus_prefixes=tuple(raw["corpus_scope"]["collection_prefixes"]),
        queries=tuple(queries),
    )


def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float | None:
    """|relevant ∩ first-k retrieved| / |relevant|.

    Returns ``None`` when ``relevant_ids`` is empty: recall is undefined
    against an empty ground truth, and returning 0.0 there would report a
    corpus gap as a retrieval failure — the two have opposite remedies.

    ``k`` slices the retrieved list, so a route that returned MORE than k rows
    is held to the same k as one that returned fewer. Duplicate ids in
    ``retrieved_ids`` count once, which is why the intersection is over a set
    rather than a running tally.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if not relevant_ids:
        return None
    hits = set(retrieved_ids[:k]) & relevant_ids
    return len(hits) / len(relevant_ids)


def resolve_ground_truth(query: Query, corpus: list[dict]) -> set[str]:
    """The relevant chunk ids for *query*, computed from *corpus*.

    ``corpus`` is a list of chunk rows, each carrying at least ``id`` and the
    fields the query's relevance kind reads. Deterministic: the same index
    yields the same ground truth on any box, with no stored chunk hashes to
    rot.
    """
    needle = query.ground_truth_value.lower()
    relevant: set[str] = set()
    for row in corpus:
        chunk_id = row.get("id") or row.get("chunk_text_hash") or ""
        if not chunk_id:
            continue
        if query.ground_truth_kind == "literal_token":
            haystack = (row.get("content") or row.get("document") or "").lower()
        else:
            haystack = " ".join(str(row.get(f) or "") for f in
                                ("source_uri", "source_path", "title")).lower()
        if needle in haystack:
            relevant.add(chunk_id)
    return relevant


def measure(
    client: _Retriever,
    query_set: QuerySet,
    collections: list[str],
    corpus: list[dict],
    *,
    route: str,
    k: int = 10,
) -> RecallReport:
    """Run *query_set* over *collections* on *route* and report recall@k.

    *corpus* is the chunk inventory the ground truth resolves against, passed
    in rather than fetched here so the caller owns (and can record) exactly
    which chunks the measurement was scored against. A ground truth resolved
    from a different inventory than the one searched is the silent way a recall
    figure becomes meaningless.
    """
    if route not in ("vector", "hybrid"):
        raise ValueError(f"unknown route {route!r}; expected 'vector' or 'hybrid'")
    call = client.search if route == "vector" else client.hybrid_search

    results: list[QueryResult] = []
    for q in query_set.queries:
        relevant = resolve_ground_truth(q, corpus)
        rows = call(q.text, collections, n_results=k)
        retrieved = [r.get("id", "") for r in rows]
        recall = recall_at_k(retrieved, relevant, k)
        results.append(QueryResult(
            query_id=q.id, shape=q.shape,
            relevant=len(relevant), retrieved=len(retrieved),
            hits=len(set(retrieved[:k]) & relevant), recall=recall,
        ))
    return RecallReport(
        route=route, k=k, collections=tuple(collections), per_query=tuple(results),
    )
