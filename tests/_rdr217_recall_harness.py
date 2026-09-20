# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 P1.1 (bead nexus-lqo4p.1) — the retrieval measurement harness.

Measures a query set against a named collection set on either retrieval route.
It takes NO number by itself: P1.2 takes the vector-only before-number and P1.4
re-runs the identical query set through ``hybrid_search``. This module and
``tests/fixtures/rdr217_lexical_query_set.json`` are the artifact that makes
those two readings comparable.

PRECISION@K IS THE PRIMARY FIGURE, NOT RECALL@K, and that is a correction the
Phase 1 review (bead nexus-lqo4p.2) forced before any number was taken. The
RDR's Phase 1 text says recall, so this is a deliberate, recorded deviation
rather than a silent swap.

Why: recall@k is CEILING-CAPPED by the size of the ground truth. Measured on
this repo, ``resolve_active_session_id`` has 122 literal occurrences and
``pgvector`` had 681; against a ground truth of even 50 distinct chunks,
recall@10 cannot exceed 0.2, and a route returning ten perfect hits would
score 0.2. The difference between two routes then compresses into noise, and a
flat result would be uninterpretable — exactly the defect bead .2 exists to
catch, because Phase 1's number is what unblocks the Phase 3 surface decision.
Precision@k — of the rows returned, how many are relevant — is bounded at 1.0
regardless of ground-truth size and answers the actual question: when a
developer types an identifier, how much of the window is worth reading.

Recall@k is still computed and reported, with its CEILING alongside it, so a
reader can see when a recall figure was structurally unable to reach 1.0.

WHY THE ARITHMETIC IS UNDER TEST rather than written inline at the call site:
an off-by-one is invisible, because a figure wrong by one rank still looks like
a figure, and it feeds a decision. ``tests/test_rdr217_recall_harness.py``
pins it at the boundaries.

GROUND TRUTH IS RESOLVED FROM THE CORPUS, NOT HAND-LABELLED. Both relevance
kinds are deterministic functions of the indexed chunks, so the same query set
resolves identically on any box holding the same index and there is no stored
list of chunk hashes to rot. See the artifact's ``relevance_model`` section for
the bias literal containment carries for identifier queries and why the prose
queries exist.

NO CLIENT-SIDE RE-RANKING HAPPENS HERE, deliberately; the artifact's
``ordering`` section carries the reasoning. Rows are consumed in engine order.
There is no sort key in this module, which is how it avoids inheriting the trap
a sibling instrument fell into when a field moved out from under its sort.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

QUERY_SET_PATH = Path(__file__).parent / "fixtures" / "rdr217_lexical_query_set.json"

SHAPES: frozenset[str] = frozenset({"identifier", "rare_token", "prose"})
GROUND_TRUTH_KINDS: frozenset[str] = frozenset({"literal_token", "source_path_contains"})

#: A corpus row must carry an id under one of these keys, or it cannot be
#: scored. Enforced rather than documented (bead .2 code-review finding 2): the
#: corpus builder lives in P1.2 and is not written yet, so a shape mismatch
#: would otherwise resolve to an empty ground truth and report every query as
#: unanswerable, or — worse — resolve a few and report a healthy average over
#: the remnant.
_ID_KEYS: tuple[str, ...] = ("id", "chunk_text_hash")

#: Fraction of corpus rows permitted to lack a usable id before the corpus is
#: refused outright. Not zero: a real index can carry an odd row. Not lax
#: either, because the failure this guards is silent.
_MAX_UNIDENTIFIED_FRACTION = 0.01


class CorpusContractError(ValueError):
    """The corpus does not match the row identity space the routes return."""


class UninterpretableMeasurement(AssertionError):
    """A report whose numbers cannot be read as what they claim to be."""


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
    precision: float | None   # hits / retrieved-within-k; None when nothing came back
    recall: float | None      # hits / relevant; None when the corpus holds none
    recall_ceiling: float | None  # min(1.0, k / relevant): the most recall COULD be

    @property
    def recall_is_capped(self) -> bool:
        """True when recall@k could not have reached 1.0 however good the route.

        A capped recall figure is not wrong, but it is not comparable to an
        uncapped one and must never be averaged with one silently.
        """
        return self.recall_ceiling is not None and self.recall_ceiling < 1.0


@dataclass(frozen=True)
class RecallReport:
    route: str
    k: int
    collections: tuple[str, ...]
    corpus_fingerprint: str
    corpus_size: int
    per_query: tuple[QueryResult, ...]

    def macro_precision(self, shape: str | None = None) -> float | None:
        """Mean per-query precision@k, optionally within one shape. THE PRIMARY
        FIGURE: bounded at 1.0 whatever the ground truth's size."""
        scored = [
            r.precision for r in self.per_query
            if r.precision is not None and (shape is None or r.shape == shape)
        ]
        return sum(scored) / len(scored) if scored else None

    def macro_recall(self, shape: str | None = None) -> float | None:
        """Mean per-query recall@k, optionally within one shape.

        MACRO, not micro: a micro average pools hits and would let one
        identifier with many relevant chunks dominate its whole shape. Queries
        with ``recall is None`` are excluded rather than scored zero — a query
        the corpus cannot answer measures the corpus, not the route.

        Read this against :meth:`capped` before quoting it.
        """
        scored = [
            r.recall for r in self.per_query
            if r.recall is not None and (shape is None or r.shape == shape)
        ]
        return sum(scored) / len(scored) if scored else None

    def unanswerable(self) -> tuple[str, ...]:
        """Query ids whose ground truth resolved to nothing in this corpus."""
        return tuple(r.query_id for r in self.per_query if r.recall is None)

    def capped(self) -> tuple[str, ...]:
        """Query ids whose recall@k was structurally unable to reach 1.0."""
        return tuple(r.query_id for r in self.per_query if r.recall_is_capped)

    def mislabelled_rare_tokens(self) -> tuple[str, ...]:
        """Query ids declared ``rare_token`` whose ground truth is not rare.

        A shape label is prose in the artifact and nothing could check it,
        which is how the set shipped "pgvector" — 681 literal occurrences — in
        a slot labelled rare. Rarity is a property of the CORPUS, so it can
        only be checked once the corpus is known, which is here. A token more
        than ``k`` of whose chunks are relevant is not rare by any reading, and
        leaving it in means the rare-token shape is not being measured at all
        while appearing to be.
        """
        return tuple(
            r.query_id for r in self.per_query
            if r.shape == "rare_token" and r.relevant > self.k
        )

    def must_be_interpretable(self) -> None:
        """Raise unless this report's averages mean what they appear to mean.

        Bead .2 code-review finding 1: nothing stopped a run where most queries
        were unanswerable from reporting a healthy-looking average over the
        remainder. A measurement phase must not be able to record that quietly,
        so the check is a method the recording step calls rather than a comment
        asking it to look. Capped recall is reported, not refused — it is a real
        figure with a stated ceiling — but it is named here so a report cannot
        quote recall without meeting the caveat.
        """
        problems: list[str] = []
        total = len(self.per_query)
        if not total:
            raise UninterpretableMeasurement("the report is empty")
        bad = self.unanswerable()
        if len(bad) * 2 >= total:
            problems.append(
                f"{len(bad)} of {total} queries are unanswerable in this corpus "
                f"({', '.join(bad)}); the averages describe a remnant, not the set"
            )
        if not any(r.precision is not None for r in self.per_query):
            problems.append("no query returned any row; nothing was measured")
        mislabelled = self.mislabelled_rare_tokens()
        if mislabelled:
            problems.append(
                f"queries declared rare_token whose ground truth exceeds k={self.k}: "
                f"{', '.join(mislabelled)}; the rare-token shape is not being "
                "measured, whatever the labels say"
            )
        if problems:
            raise UninterpretableMeasurement("; ".join(problems))


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


def _row_id(row: dict) -> str:
    for key in _ID_KEYS:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def corpus_fingerprint(corpus: list[dict]) -> str:
    """A stable digest of the corpus's chunk ids.

    Bead .2 code-review finding 3: comparability between the P1.2 before-number
    and the P1.4 after-number rested entirely on caller discipline, with nothing
    on the report to check afterwards. Two reports carrying different
    fingerprints were not measured against the same corpus, whatever their
    prose says, and a comparison between them is void.
    """
    ids = sorted(_row_id(r) for r in corpus)
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    return f"{len(ids)}:{digest[:32]}"


def validate_corpus(corpus: list[dict]) -> None:
    """Refuse a corpus whose rows are not in the routes' identity space.

    The routes return rows carrying ``id``, ``content``, ``collection`` and
    ``distance`` byte-identically (verified across both). A corpus built to a
    different shape would resolve ground truth to nothing and report every
    query as unanswerable — or resolve a few and report an average over those.
    """
    if not corpus:
        raise CorpusContractError("the corpus is empty; no query could be scored")
    unidentified = sum(1 for r in corpus if not _row_id(r))
    if unidentified > max(1, int(len(corpus) * _MAX_UNIDENTIFIED_FRACTION)):
        raise CorpusContractError(
            f"{unidentified} of {len(corpus)} corpus rows carry no id under any of "
            f"{_ID_KEYS}; ground truth resolved against them would be silently "
            "empty or partial"
        )


def precision_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float | None:
    """|relevant ∩ first-k retrieved| / |first-k retrieved|.

    Bounded at 1.0 whatever the ground truth's size, which is why this and not
    recall@k is the primary figure. ``None`` when nothing was retrieved:
    precision over an empty window is undefined, and 0.0 would report "the
    route returned junk" for "the route returned nothing".
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    window = retrieved_ids[:k]
    if not window:
        return None
    return len(set(window) & relevant_ids) / len(set(window))


def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float | None:
    """|relevant ∩ first-k retrieved| / |relevant|.

    ``None`` when ``relevant_ids`` is empty: recall is undefined against an
    empty ground truth, and 0.0 would report a corpus gap as a retrieval
    failure — the two have opposite remedies.

    CEILING-CAPPED when ``|relevant| > k``; see :func:`recall_ceiling` and read
    the module docstring before quoting this figure.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if not relevant_ids:
        return None
    return len(set(retrieved_ids[:k]) & relevant_ids) / len(relevant_ids)


def recall_ceiling(relevant_ids: set[str], k: int) -> float | None:
    """The largest recall@k this query COULD score: ``min(1.0, k / |relevant|)``.

    Reported beside every recall figure so a capped one announces itself
    instead of reading as a poor result.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if not relevant_ids:
        return None
    return min(1.0, k / len(relevant_ids))


def resolve_ground_truth(query: Query, corpus: list[dict]) -> set[str]:
    """The relevant chunk ids for *query*, computed from *corpus*."""
    needle = query.ground_truth_value.lower()
    relevant: set[str] = set()
    for row in corpus:
        chunk_id = _row_id(row)
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
    """Run *query_set* over *collections* on *route* and report the figures.

    *corpus* is the chunk inventory ground truth resolves against, passed in
    rather than fetched so the caller owns which chunks the measurement scored
    against — and fingerprinted onto the report so a later comparison can
    verify it rather than trust it.
    """
    if route not in ("vector", "hybrid"):
        raise ValueError(f"unknown route {route!r}; expected 'vector' or 'hybrid'")
    validate_corpus(corpus)
    call = client.search if route == "vector" else client.hybrid_search

    results: list[QueryResult] = []
    for q in query_set.queries:
        relevant = resolve_ground_truth(q, corpus)
        rows = call(q.text, collections, n_results=k)
        retrieved = [r.get("id", "") for r in rows]
        # Computed ONCE and shared, so QueryResult.hits can never desync from
        # .precision/.recall (bead .2 code-review finding 4).
        window = set(retrieved[:k])
        hits = len(window & relevant)
        results.append(QueryResult(
            query_id=q.id, shape=q.shape,
            relevant=len(relevant), retrieved=len(retrieved), hits=hits,
            precision=(hits / len(window)) if window else None,
            recall=(hits / len(relevant)) if relevant else None,
            recall_ceiling=recall_ceiling(relevant, k),
        ))
    return RecallReport(
        route=route, k=k, collections=tuple(collections),
        corpus_fingerprint=corpus_fingerprint(corpus), corpus_size=len(corpus),
        per_query=tuple(results),
    )
