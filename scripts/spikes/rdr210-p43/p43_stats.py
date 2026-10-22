# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure statistics for the P4.3 measurements. No I/O, no clock; every random
choice takes a seed.

* :func:`kendall_tau_b`       rank correlation of two score vectors (ties handled).
* :func:`best_f1_threshold`   labelled GLOBAL best-F1 distance threshold.
* :func:`bootstrap_threshold` seeded query-level bootstrap of that threshold.
* :func:`factor_interval`     the bge calibration factor, 0.45 / threshold.
* :func:`count_inversions`    cross-family inversions on one ranked list.
"""
from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: voyage-code-3's relevance threshold: the baseline every factor is relative to
#: (``nexus.scoring._CALIBRATION_BASELINE``).
BASELINE_THRESHOLD = 0.45
#: The RDR's expected bge factor range (210-research-17: 1.38, bootstrap 1.36-1.48).
EXPECTED_FACTOR_RANGE = (1.36, 1.48)


# ── rank agreement ───────────────────────────────────────────────────────────

def kendall_tau_b(x: Sequence[float], y: Sequence[float]) -> float:
    """Kendall's tau-b of paired values *x*, *y* (ties in either vector are
    handled by the tau-b denominator). Returns 1.0 for fewer than two pairs and
    for two vectors that are both constant (nothing to disagree about); 0.0 when
    exactly one is constant."""
    if len(x) != len(y):
        raise ValueError("kendall_tau_b: length mismatch")
    n = len(x)
    if n < 2:
        return 1.0
    conc = disc = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = x[i] - x[j]
            dy = y[i] - y[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                conc += 1
            else:
                disc += 1
    denom = math.sqrt((conc + disc + ties_x) * (conc + disc + ties_y))
    if denom == 0:
        x_const = conc + disc + ties_y == 0
        y_const = conc + disc + ties_x == 0
        return 1.0 if (x_const and y_const) else 0.0
    return (conc - disc) / denom


def top_k_overlap(a: Sequence[str], b: Sequence[str], k: int) -> float:
    """|top-k(a) ∩ top-k(b)| / k over two best-first id lists."""
    if k < 1:
        raise ValueError("k must be positive")
    return len(set(a[:k]) & set(b[:k])) / k


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


# ── best-F1 threshold and its bootstrap ──────────────────────────────────────

@dataclass(frozen=True)
class LabelledQuery:
    """One query's top-k candidates: ``(distance, relevant)`` pairs, plus the
    number of relevant chunks the target holds in the whole corpus."""
    items: tuple[tuple[float, bool], ...]
    relevant_in_corpus: int = 0


def best_f1_threshold(
    queries: Sequence[LabelledQuery], *, fn_basis: str = "pool",
) -> tuple[float, float]:
    """The distance threshold ``t`` (keep a row when ``distance <= t``) that
    maximises F1 over ALL queries' candidates pooled, with its F1.

    ``fn_basis`` fixes what counts as a miss: ``"pool"`` = relevant candidates
    above ``t`` among the pooled top-k (recall is relative to what the top-k
    retrieved), ``"corpus"`` = every relevant chunk the targets hold, retrieved
    or not. Candidate thresholds are the distinct observed distances; ties on F1
    take the lowest. Returns ``(nan, 0.0)`` when no candidate is relevant."""
    if fn_basis not in ("pool", "corpus"):
        raise ValueError(f"fn_basis must be 'pool' or 'corpus', got {fn_basis!r}")
    pooled = sorted((d, rel) for q in queries for d, rel in q.items)
    total_rel_pool = sum(1 for _, rel in pooled if rel)
    if total_rel_pool == 0:
        return float("nan"), 0.0
    total_rel = (
        total_rel_pool if fn_basis == "pool"
        else max(sum(q.relevant_in_corpus for q in queries), total_rel_pool)
    )
    best_t, best_f1 = float("nan"), -1.0
    tp = fp = 0
    i = 0
    n = len(pooled)
    while i < n:
        d = pooled[i][0]
        while i < n and pooled[i][0] == d:
            if pooled[i][1]:
                tp += 1
            else:
                fp += 1
            i += 1
        fn = total_rel - tp
        f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0
        if f1 > best_f1 + 1e-12:
            best_t, best_f1 = d, f1
    return best_t, best_f1


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (*q* in [0, 100]) of an ascending list."""
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def bootstrap_threshold(
    queries: Sequence[LabelledQuery], *, resamples: int = 1000, seed: int = 210,
    fn_basis: str = "pool",
) -> list[float]:
    """Best-F1 thresholds of *resamples* query-level resamples (queries drawn
    with replacement, ``random.Random(seed)``). A resample with no relevant
    candidate is skipped, so the list can be shorter than *resamples*."""
    rng = random.Random(seed)
    n = len(queries)
    out: list[float] = []
    for _ in range(resamples):
        sample = [queries[rng.randrange(n)] for _ in range(n)]
        t, _f1 = best_f1_threshold(sample, fn_basis=fn_basis)
        if not math.isnan(t):
            out.append(t)
    return out


@dataclass(frozen=True)
class FactorResult:
    threshold: float
    f1: float
    factor: float
    threshold_ci: tuple[float, float]
    factor_ci: tuple[float, float]
    resamples_used: int


def factor_interval(
    queries: Sequence[LabelledQuery], *, resamples: int = 1000, seed: int = 210,
    fn_basis: str = "pool", baseline: float = BASELINE_THRESHOLD,
) -> FactorResult:
    """The factor ``baseline / threshold`` with a 95% bootstrap interval.

    The factor interval is the baseline over the threshold interval's ends,
    swapped (a larger threshold is a smaller factor)."""
    t, f1 = best_f1_threshold(queries, fn_basis=fn_basis)
    boots = sorted(bootstrap_threshold(queries, resamples=resamples, seed=seed, fn_basis=fn_basis))
    lo = percentile(boots, 2.5)
    hi = percentile(boots, 97.5)
    nan = float("nan")
    return FactorResult(
        threshold=t, f1=f1,
        factor=baseline / t if t and not math.isnan(t) else nan,
        threshold_ci=(lo, hi),
        factor_ci=(baseline / hi if hi else nan, baseline / lo if lo else nan),
        resamples_used=len(boots),
    )


def intervals_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    if any(math.isnan(v) for v in (*a, *b)):
        return False
    return a[0] <= b[1] and b[0] <= a[1]


# ── cross-family inversions ──────────────────────────────────────────────────

@dataclass(frozen=True)
class RankedRow:
    """One row of a surface's final order, best first."""
    family: str      # "bge" or "voyage"
    relevant: bool
    key: str = ""    # chunk id, for diagnostics


@dataclass(frozen=True)
class InversionResult:
    #: an irrelevant row of family X ranks above the best relevant row of Y
    a_over_b: bool
    b_over_a: bool
    #: the family had no relevant row in the list (its direction is not counted)
    a_missing: bool
    b_missing: bool


def count_inversions(rows: Sequence[RankedRow], fam_a: str, fam_b: str) -> InversionResult:
    """Inversions in one query's final order, both directions.

    ``a_over_b``: some irrelevant *fam_a* row ranks above the BEST (highest
    ranked) relevant *fam_b* row, i.e. family A sinks a relevant B hit. A family
    with no relevant row in the list cannot be inverted against, and is reported
    in ``*_missing`` so a short list is visible rather than read as a clean pass."""
    def best_relevant(fam: str) -> int | None:
        for i, r in enumerate(rows):
            if r.family == fam and r.relevant:
                return i
        return None

    def irrelevant_above(fam: str, limit: int) -> bool:
        return any(r.family == fam and not r.relevant for r in rows[:limit])

    best_a = best_relevant(fam_a)
    best_b = best_relevant(fam_b)
    return InversionResult(
        a_over_b=best_b is not None and irrelevant_above(fam_a, best_b),
        b_over_a=best_a is not None and irrelevant_above(fam_b, best_a),
        a_missing=best_a is None,
        b_missing=best_b is None,
    )


@dataclass(frozen=True)
class InversionTotals:
    queries: int
    a_over_b: int
    b_over_a: int
    a_missing: int
    b_missing: int


def tally_inversions(results: Sequence[InversionResult]) -> InversionTotals:
    return InversionTotals(
        queries=len(results),
        a_over_b=sum(r.a_over_b for r in results),
        b_over_a=sum(r.b_over_a for r in results),
        a_missing=sum(r.a_missing for r in results),
        b_missing=sum(r.b_missing for r in results),
    )


def order_by_calibrated(
    rows: Sequence[tuple[float, str]], factor_of: Callable[[str], float],
) -> list[int]:
    """Indices of *rows* (``(raw_distance, family)``) nearest-first on the
    calibrated scale, ties by original index. Used to check a merge order
    against ``distance * factor`` without the engine."""
    return sorted(range(len(rows)), key=lambda i: (rows[i][0] * factor_of(rows[i][1]), i))
