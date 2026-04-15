# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-079 P5 — empirical min_confidence calibration harness (nexus-o5q).

Closes RDR-078 PQ-2 / RDR-079 Gap C: ``min_confidence=0.85`` was an
educated guess. This harness measures precision / recall / F1 across a
labeled paraphrase dataset at every threshold in [0.70, 0.95] step 0.05,
prints the ROC table, and asserts the chosen threshold delivers an
acceptable operating point.

The harness runs entirely offline — the local ONNX MiniLM embedder is
bundled with nexus, so no API keys or network calls are made. The
dataset (:mod:`tests.fixtures.calibration_paraphrases`) mixes 48
positive intents across the 9 shipped seeds with 6 adversarial
negatives that should clear no threshold.

Methodology recorded in ``docs/rdr/rdr-079-calibration.md`` — this
module is the reproducible source.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import chromadb
import pytest

from tests.fixtures.calibration_paraphrases import (
    Paraphrase,
    paraphrase_dataset,
)


_SEED_DIR = Path(__file__).resolve().parents[1] / "nx" / "plans" / "builtin"

# Swept wide enough to expose the discrimination band of the T1 ONNX
# MiniLM embedder. RDR-079 P5 empirical finding: MiniLM cosine on
# plan-description paraphrases sits in a low band (0.45–0.75) — the
# shipped default ``min_confidence=0.85`` is far too high and must
# drop for the T1 cache path to be useful at all.
_THRESHOLDS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]

# Acceptance band — the best threshold must deliver at least this F1
# against the MiniLM embedder. 0.40 is the minimum "cache is doing
# something useful" bar; if we can't clear this, the cache path should
# be disabled by default until a stronger embedder is wired up.
_MIN_ACCEPTABLE_F1 = 0.40


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int

    @property
    def precision(self) -> float:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0


@pytest.fixture(scope="module")
def library_with_seeds(tmp_path_factory):
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.plans.seed_loader import load_seed_directory

    tmp = tmp_path_factory.mktemp("cal")
    lib = PlanLibrary(tmp / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    load_seed_directory(_SEED_DIR, library=lib, outcome="success",
                       scope_override="global")
    return lib


@pytest.fixture(scope="module")
def seed_ids_by_identity(library_with_seeds) -> dict[tuple[str, str], int]:
    """Map ``(verb, strategy)`` → plan_id for the 9 shipped seeds."""
    ids: dict[tuple[str, str], int] = {}
    with library_with_seeds._lock:
        rows = library_with_seeds.conn.execute(
            "SELECT id, verb, dimensions FROM plans WHERE project = ?",
            ("",),
        ).fetchall()
    for row in rows:
        plan_id, verb, dims_json = row
        if not dims_json:
            continue
        try:
            dims = json.loads(dims_json)
        except json.JSONDecodeError:
            continue
        strategy = dims.get("strategy", "")
        ids[(verb or "", strategy)] = plan_id
    return ids


@pytest.fixture(scope="module")
def cache(library_with_seeds, tmp_path_factory):
    from nexus.plans.session_cache import PlanSessionCache

    client = chromadb.EphemeralClient()
    c = PlanSessionCache(client=client, session_id="calibration-session")
    c.populate(library_with_seeds)
    return c


def _evaluate_at_threshold(
    dataset: list[Paraphrase],
    ids: dict[tuple[str, str], int],
    cache,
) -> dict[float, ThresholdMetrics]:
    """Score every paraphrase once, bucket by threshold.

    Each intent's top cosine hit is converted to ``confidence = 1 -
    distance``. A positive label counts as TP when the top hit matches
    the expected plan AND its confidence clears the threshold; a
    negative label counts as TN when the top hit's confidence is below
    every threshold (or the cache returns no hits).

    Mismatched expected plan_id at or above threshold is a FP (a plan
    matched, but the wrong one).
    """
    counters = {
        t: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for t in _THRESHOLDS
    }

    for entry in dataset:
        expected_id = ids.get(
            (entry.expected_verb, entry.expected_strategy),
        ) if entry.is_positive else None

        hits = cache.query(entry.intent, n=1)
        if not hits:
            # No cosine hit — every threshold sees it as miss.
            for t in _THRESHOLDS:
                if entry.is_positive:
                    counters[t]["fn"] += 1
                else:
                    counters[t]["tn"] += 1
            continue

        top_plan_id, top_distance = hits[0]
        top_confidence = max(0.0, 1.0 - float(top_distance))

        for t in _THRESHOLDS:
            passes = top_confidence >= t
            if entry.is_positive:
                if passes and top_plan_id == expected_id:
                    counters[t]["tp"] += 1
                elif passes and top_plan_id != expected_id:
                    counters[t]["fp"] += 1
                else:  # below threshold → didn't match anything
                    counters[t]["fn"] += 1
            else:
                if passes:
                    counters[t]["fp"] += 1
                else:
                    counters[t]["tn"] += 1

    return {
        t: ThresholdMetrics(
            threshold=t,
            true_positive=c["tp"], false_positive=c["fp"],
            false_negative=c["fn"], true_negative=c["tn"],
        )
        for t, c in counters.items()
    }


def test_calibration_harness_runs_and_reports(
    library_with_seeds, seed_ids_by_identity, cache, capsys,
) -> None:
    """The harness executes and prints a reproducible ROC table."""
    dataset = paraphrase_dataset()
    assert len([p for p in dataset if p.is_positive]) >= 40, (
        "RDR-079 bead nexus-o5q requires ≥40 labeled intents"
    )

    metrics_by_t = _evaluate_at_threshold(
        dataset, seed_ids_by_identity, cache,
    )

    # Emit ROC table — visible via `pytest -s` and copy-pasted into
    # docs/rdr/rdr-079-calibration.md.
    print()
    print("min_confidence calibration (MiniLM, 9 seeds, 54 intents)")
    print(f"{'thr':>6} {'TP':>4} {'FP':>4} {'FN':>4} {'TN':>4} "
          f"{'prec':>6} {'recall':>7} {'F1':>6}")
    for t in _THRESHOLDS:
        m = metrics_by_t[t]
        print(
            f"{t:>6.2f} {m.true_positive:>4d} {m.false_positive:>4d} "
            f"{m.false_negative:>4d} {m.true_negative:>4d} "
            f"{m.precision:>6.3f} {m.recall:>7.3f} {m.f1:>6.3f}",
        )

    # Every threshold produced defined metrics (no crashes on corners).
    # Note: ``false_positive`` combines two contributors — a positive whose
    # top match is the WRONG plan above threshold, and a negative whose top
    # match scores above threshold. So ``TP + FN`` does not equal the count
    # of positives; the total-count invariant is the only clean check.
    for t in _THRESHOLDS:
        m = metrics_by_t[t]
        assert (m.true_positive + m.false_positive
                + m.false_negative + m.true_negative) == len(dataset)


def test_best_threshold_clears_minimum_f1(
    library_with_seeds, seed_ids_by_identity, cache,
) -> None:
    """The ROC's best-F1 threshold must clear ``_MIN_ACCEPTABLE_F1``.

    If this fails, the ONNX MiniLM cosine is not discriminating plans
    well enough — either the dataset is too hard, or the model is too
    small, or both. The fix in that case is to either (a) switch the
    T1 cache to voyage-context-3 or (b) lower the shipped
    ``min_confidence`` to a value the model can defensibly hit.
    """
    dataset = paraphrase_dataset()
    metrics_by_t = _evaluate_at_threshold(
        dataset, seed_ids_by_identity, cache,
    )
    best = max(metrics_by_t.values(), key=lambda m: m.f1)
    assert best.f1 >= _MIN_ACCEPTABLE_F1, (
        f"best-F1 threshold {best.threshold:.2f} has F1={best.f1:.3f}, "
        f"below the minimum acceptable {_MIN_ACCEPTABLE_F1:.2f}. "
        f"See test output for the full ROC table."
    )


def test_negatives_do_not_match_above_high_threshold(
    library_with_seeds, seed_ids_by_identity, cache,
) -> None:
    """The 6 adversarial-negative intents must not score above 0.90 on
    any seed. If they do, the cosine space is too permissive and the
    shipped threshold should rise accordingly."""
    dataset = [p for p in paraphrase_dataset() if not p.is_positive]
    for entry in dataset:
        hits = cache.query(entry.intent, n=1)
        if not hits:
            continue
        _, distance = hits[0]
        confidence = max(0.0, 1.0 - float(distance))
        assert confidence < 0.90, (
            f"adversarial intent {entry.intent!r} scored "
            f"confidence={confidence:.3f} — above 0.90 is a signal "
            f"the embedder can't tell a wrong match from a right one"
        )
