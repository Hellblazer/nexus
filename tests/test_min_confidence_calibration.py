# SPDX-License-Identifier: AGPL-3.0-or-later
"""The plan-matcher threshold gate RDR-079 said existed and did not.

`docs/rdr/rdr-079-calibration.md` § Reproducibility names
`tests/test_min_confidence_calibration.py` as its harness and states:
"Test test_best_threshold_clears_minimum_f1 asserts F1 >= 0.40 at the
best threshold; drops below 0.40 -> CI fails -> someone broke either the
embedder wiring or the dataset labels."

No such file has ever existed — checked against the full git history on
2026-08-22, four months after the RDR closed. Only the dataset shipped.
So the threshold that governs every cosine plan match has been unguarded
the entire time, while a closed design record asserted CI was watching
it. A gate that is documented but absent is worse than a missing one:
it is a missing gate that people rely on.

This file makes the claim true. It is deliberately a REGRESSION PIN on
the measurement RDR-079 recorded, not a re-derivation: the numbers there
were measured against the bundled MiniLM, which is now the settled
choice rather than an interim one (the voyage-context-3 migration that
document proposed is retired), so they should reproduce.

Hermetic: bundled ONNX MiniLM, no network, no service.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.fixtures.calibration_paraphrases import paraphrase_dataset

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"

#: RDR-079's ship decision. Not a target to tune toward — the number the
#: matcher actually uses, imported so a drift in either shows up here.
_SHIPPED_FLOOR = 0.40

#: RDR-079 measured F1 = 0.545 at 0.40 on the 48-positive dataset. The
#: floor here is the document's own stated CI condition (F1 >= 0.40),
#: kept as the assertion rather than the measured value so ordinary
#: dataset maintenance does not red the build.
_MIN_F1 = 0.40


def _templates() -> list[dict]:
    rows = []
    for path in sorted(_BUILTIN_DIR.glob("*.yml")):
        t = yaml.safe_load(path.read_text())
        dims = dict(t["dimensions"])
        rows.append({
            "verb": dims.get("verb"),
            "strategy": dims.get("strategy"),
            "name": t.get("name"),
            "description": t["description"],
        })
    return rows


@pytest.fixture(scope="module")
def scored():
    """(paraphrase, best_plan, best_cosine) for every dataset intent."""
    from nexus.db.local_ef import LocalEmbeddingFunction
    from nexus.plans.match_text import _synthesize_match_text

    rows = _templates()
    ef = LocalEmbeddingFunction()
    vecs = ef([
        _synthesize_match_text(
            description=r["description"], verb=r["verb"],
            name=r["name"], scope="global",
        )
        for r in rows
    ])

    def cos(a, b):
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    out = []
    for para in paraphrase_dataset():
        q = ef([para.intent])[0]
        ranked = sorted(
            ((cos(q, v), r) for v, r in zip(vecs, rows, strict=True)),
            key=lambda x: x[0], reverse=True,
        )
        best_score, best_row = ranked[0]
        out.append((para, best_row, best_score))
    return out


def _f1_at(scored, threshold: float) -> tuple[float, int, int, int]:
    tp = fp = fn = 0
    for para, best_row, score in scored:
        fired = score >= threshold
        if para.is_positive:
            correct = (
                best_row["verb"] == para.expected_verb
                and best_row["strategy"] == para.expected_strategy
            )
            if fired and correct:
                tp += 1
            elif fired:
                fp += 1
            else:
                fn += 1
        elif fired:
            fp += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) else 0.0
    )
    return f1, tp, fp, fn


def test_the_shipped_floor_matches_the_matcher(scored):
    """The document's chosen threshold and the code's default must agree.

    Two copies of one number drift until the stale one wins an argument
    it should not (the nexus-b6qlf class).
    """
    import inspect

    from nexus.plans.matcher import plan_match

    default = inspect.signature(plan_match).parameters["min_confidence"].default
    assert default == _SHIPPED_FLOOR, (
        f"plan_match defaults to min_confidence={default}, but RDR-079's "
        f"ship decision and this calibration are pinned at {_SHIPPED_FLOOR}. "
        f"If the floor moved deliberately, re-run the ROC and update the RDR."
    )


def test_best_threshold_clears_minimum_f1(scored):
    """The assertion RDR-079 said was running in CI. Now it is."""
    best_f1, best_t = 0.0, 0.0
    for step in range(20, 71):
        threshold = step / 100.0
        f1, _, _, _ = _f1_at(scored, threshold)
        if f1 > best_f1:
            best_f1, best_t = f1, threshold
    assert best_f1 >= _MIN_F1, (
        f"best achievable F1 is {best_f1:.3f} at threshold {best_t:.2f}, "
        f"below the {_MIN_F1} floor RDR-079 set. Someone broke the embedder "
        f"wiring, the match-text synthesis, or the dataset labels — this is "
        f"the check that document said would catch it."
    )


def test_this_dataset_only_partly_calibrates_the_live_floor(scored):
    """Pins how far this harness can justify min_confidence — which is
    less than it used to, and MORE than an earlier version of this test
    claimed.

    That earlier version said the dataset and the floor govern "disjoint"
    populations, because category-level plans route by DIMENSION now. The
    routing is real but the disjointness is FALSE: the category route only
    fires when nx_answer derives a verb, and `infer_verb` returns None for
    plenty of ordinary questions (its own parametrization asserts as much
    — "tumblers", "catalog manifest"). On that path a category plan is
    gated by min_confidence exactly as RDR-079 measured it. The
    populations OVERLAP, on the verb-underivable path.

    So this dataset still says something about the live floor; it just no
    longer says everything, and nothing here exercises plan_match end to
    end, so treat these numbers as embedding-level evidence rather than
    routing-level evidence. Closing that gap is nexus-kinam.

    Measured 2026-08-22: F1 plateaus around 0.06-0.11 (F1 = 0.698) and is
    0.367 at the shipped 0.40 — a curve whose optimum sits at a threshold
    that admits nearly everything. That is the routing design's premise
    seen from the other side: cosine has no discriminative power at
    category-plan genericity.
    """
    best_f1, best_t = 0.0, 0.0
    for step in range(1, 71):
        threshold = step / 100.0
        f1, _, _, _ = _f1_at(scored, threshold)
        if f1 > best_f1:
            best_f1, best_t = f1, threshold

    # NON-VACUITY: a total embedding failure zeroes every score, which
    # would satisfy both assertions below for the wrong reason.
    assert best_f1 > 0.5, (
        f"best F1 is {best_f1:.3f} — the scoring pipeline is degenerate, "
        f"so nothing below this line means anything"
    )
    assert any(s > 0.3 for _p, _b, s in scored), (
        "no intent scores above 0.3 against any template; the embedder or "
        "match-text synthesis is broken, not merely uncalibrated"
    )

    assert best_t < _SHIPPED_FLOOR, (
        f"F1 now peaks at {best_t:.2f}, at or above the shipped floor "
        f"{_SHIPPED_FLOOR}. If category-plan cosine matching genuinely "
        f"improved, re-measure before assuming the routing is still needed."
    )

    positives = [s for p, _, s in scored if p.is_positive]
    assert max(positives) < 0.75, (
        f"a category plan now scores {max(positives):.3f} against a "
        f"paraphrase. The premise that a topic-free plan cannot win a "
        f"topical-similarity contest would need re-examining."
    )


def test_the_dataset_labels_plans_that_actually_ship(scored):
    """A dataset scoring the matcher against ABSENT plans measures nothing.

    Eight meta-verb positives were removed on 2026-08-22 when their
    templates were retired (nexus-77cct); this keeps that class from
    coming back silently.
    """
    shipped = {(r["verb"], r["strategy"]) for r in _templates()}
    for para, _best, _score in scored:
        if not para.is_positive:
            continue
        assert (para.expected_verb, para.expected_strategy) in shipped, (
            f"dataset expects {para.expected_verb}/{para.expected_strategy} "
            f"for {para.intent!r}, but no shipped template has those "
            f"dimensions. Retired plan? Remove its entries."
        )


def test_dataset_is_not_vacuous(scored):
    """Non-vacuity: a dataset that shrank to nothing would pass every
    assertion above."""
    positives = [p for p, _, _ in scored if p.is_positive]
    negatives = [p for p, _, _ in scored if not p.is_positive]
    assert len(positives) >= 30, len(positives)
    assert len(negatives) >= 5, len(negatives)
