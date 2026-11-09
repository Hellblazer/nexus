# SPDX-License-Identifier: AGPL-3.0-or-later
"""Paired, alternating A/B timing, compared by median with every sample kept (nexus-3ceda.11).

Timing A for a while and then B for a while measures the machine's drift as much as the
difference: a warming cache, a CI job starting next door, thermal throttling. So the two
arms run in pairs, in ABBA order (A B, then B A, then A B, ...), and each pair is compared
on its own: whatever moved the machine during one pair moved both of its samples.

The summary is the median of each arm and the median of the per-pair differences. Medians,
because a run of latencies has a long right tail and a mean follows it. No sample is
dropped: an outlier is a fact about the system under test as often as it is noise, so the
report keeps every sample and states how many lie past the usual 1.5 IQR fences, and the
reader decides.
"""
from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from typing import Any

Clock = Callable[[], float]


def run_paired(
    a: Callable[[], object], b: Callable[[], object], pairs: int, *, clock: Clock = time.perf_counter,
) -> dict[str, list[float]]:
    """Time *pairs* runs of each arm, alternating which goes first: pair 0 runs A then B,
    pair 1 runs B then A, and so on, so with an even count each arm leads half the pairs.
    Returns the seconds of each run, in pair order."""
    if pairs < 1:
        raise ValueError(f"pairs must be at least 1, got {pairs}")
    samples: dict[str, list[float]] = {"a": [], "b": []}

    def timed(arm: str, fn: Callable[[], object]) -> None:
        t0 = clock()
        fn()
        samples[arm].append(clock() - t0)

    for i in range(pairs):
        order = (("a", a), ("b", b)) if i % 2 == 0 else (("b", b), ("a", a))
        for arm, fn in order:
            timed(arm, fn)
    return samples


def _outliers(xs: list[float]) -> int | None:
    """How many samples lie outside the 1.5 IQR fences; None (not computed) below four
    samples. With no spread (every sample equal but one) any deviation counts: that is
    the 1.5 IQR rule, and the samples are in the report to judge it by."""
    if len(xs) < 4:
        return None
    q1, _, q3 = statistics.quantiles(xs, n=4, method="inclusive")
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return sum(1 for x in xs if x < lo or x > hi)


def summarize(samples: dict[str, list[float]]) -> dict[str, Any]:
    """Medians of each arm and of the per-pair difference ``b - a``, every sample, and the
    outlier counts. ``b_over_a`` is the ratio of the medians."""
    a, b = samples["a"], samples["b"]
    if len(a) != len(b) or not a:
        raise ValueError(f"paired samples need equal, non-zero counts, got {len(a)} and {len(b)}")
    diffs = [y - x for x, y in zip(a, b)]
    median_a, median_b = statistics.median(a), statistics.median(b)
    return {
        "pairs": len(a),
        "median_a_s": median_a,
        "median_b_s": median_b,
        "median_paired_diff_s": statistics.median(diffs),
        "b_over_a": (median_b / median_a) if median_a else None,
        "b_faster_pairs": sum(1 for d in diffs if d < 0),
        "outliers": {"a": _outliers(a), "b": _outliers(b)},
        "samples_s": {"a": a, "b": b},
    }
