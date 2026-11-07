# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-244 Track A scorer: chunk rows to ranked documents, then NDCG@10, recall@10, MRR.

Identity comes from each row's chunk hash, mapped by the caller (``keys_for``) to the catalog
titles of the documents in the RUN'S collection that hold it (nexus-2k66t.2.5: the caller drops a
document in any other collection). The row's own path is never read. Rules (nexus-2k66t.2.4):

* Rows are taken in the order ``nx search`` returned them: that order is the system under test
  (its boosted score, file diversity cap and collection interleave; no reranker on a one-collection
  search). Within a run of consecutive rows of equal distance, rows are ordered by their sorted key
  tuple, so a tie's order does not depend on the engine's (nexus-2k66t.2.4).
* A row credits every key it holds that is not yet ranked, in key order, at consecutive ranks.
  This is generous when documents share text. ``shared_rows`` counts the rows that credited more
  than one key inside the top ``k``.
* The ranked list stops at ``k`` keys. A row whose keys straddle the cut contributes its first keys
  in key order until the list holds ``k``.
* A row with no key fails the query (``EmptyKeyError``, carrying every such row and its count); it
  is never put in a bucket of its own.
* Duplicate text (Sam, ask rdr244-duplicate-text, 2026-11-06): the caller wraps ``keys_for`` and
  the qrels with ``collapse`` and ``collapse_qrels``, so byte-identical documents rank and judge as
  one class. A row whose documents all fall in one class is then not a shared row.
* Fewer than ``k`` distinct keys: the query is scored on what returned, and ``short_by`` says how
  many were missing. Never padded.

Gain is ``2^g - 1`` (``scripts/bench/metrics.py``). Every CoIR pool qrels score is 1, where this
equals linear gain (T2 nexus_rdr/244-research-15).
"""
from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

K: int = 10


@dataclass(frozen=True)
class Row:
    chash: str
    distance: float


@dataclass(frozen=True)
class Ranked:
    keys: list[str]
    short_by: int
    shared_rows: int


@dataclass(frozen=True)
class QueryScore:
    ndcg: float
    recall: float
    rr: float


class EmptyKeyError(RuntimeError):
    """Result rows whose chunk hash maps to no document in the run's collection."""

    def __init__(self, chashes: list[str]) -> None:
        self.chashes = chashes
        super().__init__(f"{len(chashes)} result rows map to no document in the run collection: {chashes}")


def rank_documents(rows: Sequence[Row], keys_for: Callable[[str], list[str]], *, k: int = K) -> Ranked:
    keyed = [(r, sorted(set(keys_for(r.chash)))) for r in rows]
    empty = [r.chash for r, ks in keyed if not ks]
    if empty:
        raise EmptyKeyError(empty)
    keyed = _break_ties(keyed)
    seen: list[str] = []
    shared = 0
    for _row, ks in keyed:
        credited = 0
        for key in ks:
            if len(seen) >= k:
                break
            if key not in seen:
                seen.append(key)
                credited += 1
        if credited > 1:
            shared += 1
    return Ranked(keys=seen, short_by=max(0, k - len(seen)), shared_rows=shared)


def collapse(keys_for: Callable[[str], list[str]], class_of: Callable[[str], str]) -> Callable[[str], list[str]]:
    """``keys_for`` with every key replaced by its duplicate-text class representative."""
    return lambda chash: sorted({class_of(k) for k in keys_for(chash)})


def collapse_qrels(qrels: dict[str, int], class_of: Callable[[str], str]) -> dict[str, int]:
    """Judgements per class; a class takes the highest grade of its members."""
    out: dict[str, int] = {}
    for key, grade in qrels.items():
        c = class_of(key)
        out[c] = max(out.get(c, grade), grade)
    return out


def _break_ties(keyed: list[tuple[Row, list[str]]]) -> list[tuple[Row, list[str]]]:
    out: list[tuple[Row, list[str]]] = []
    i = 0
    while i < len(keyed):
        j = i
        while j + 1 < len(keyed) and keyed[j + 1][0].distance == keyed[i][0].distance:
            j += 1
        out.extend(sorted(keyed[i:j + 1], key=lambda rk: tuple(rk[1])))
        i = j + 1
    return out


def ndcg_at_k(keys: Sequence[str], qrels: dict[str, int], *, k: int = K) -> float:
    dcg = sum((2 ** qrels.get(key, 0) - 1) / math.log2(i + 2) for i, key in enumerate(keys[:k]))
    ideal = sorted(qrels.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return 0.0 if idcg == 0 else dcg / idcg


def recall_at_k(keys: Sequence[str], qrels: dict[str, int], *, k: int = K) -> float:
    """Document-level recall@k as RDR-244 defines it: 1 when any relevant document is in the top k."""
    return 1.0 if any(qrels.get(key, 0) > 0 for key in keys[:k]) else 0.0


def mrr(keys: Sequence[str], qrels: dict[str, int], *, k: int = K) -> float:
    for i, key in enumerate(keys[:k]):
        if qrels.get(key, 0) > 0:
            return 1.0 / (i + 1)
    return 0.0


def score_query(keys: Sequence[str], qrels: dict[str, int], *, k: int = K) -> QueryScore:
    return QueryScore(ndcg_at_k(keys, qrels, k=k), recall_at_k(keys, qrels, k=k), mrr(keys, qrels, k=k))


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..100) of a non-empty sample."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def bootstrap_interval(values: Sequence[float], *, resamples: int = 10_000, seed: int = 2026,
                       level: float = 0.95,
                       stat: Callable[[Sequence[float]], float] = mean) -> tuple[float, float]:
    """Percentile bootstrap interval of ``stat`` (the mean by default) over queries."""
    n = len(values)
    rng = random.Random(seed)
    means = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    # Nearest rank, as percentile(); rounded first so 0.025 * 10000 is 250, not 250.00000000000023.
    lo = means[max(0, math.ceil(round((1 - level) / 2 * resamples, 9)) - 1)]
    hi = means[max(0, math.ceil(round((1 + level) / 2 * resamples, 9)) - 1)]
    return lo, hi
