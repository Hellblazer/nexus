---
title: RDR-079 P5 — min_confidence calibration
status: closed
close_reason: implemented
type: calibration-artifact
bead: nexus-o5q
date: 2026-04-15
closed_date: 2026-04-18
note: |
  Calibration artifact — a one-shot ROC measurement that established
  min_confidence=0.40 as the plan-matcher threshold (see ROC table).
  The measurement is complete; the threshold was adopted.
  rdr-close skill gap-replay gate does not apply to calibration
  artifacts (no Problem Statement gap headings by design).
---

# RDR-079 P5 — min_confidence calibration

Closes RDR-078 PQ-2 / RDR-079 Gap C. Prior to this measurement,
`min_confidence=0.85` was an educated guess and had no ROC evidence.

## Methodology

Harness: `tests/test_min_confidence_calibration.py`. Dataset:
`tests/fixtures/calibration_paraphrases.py`.

- 48 positive intents across the 9 shipped seed plans (8 per scenario
  verb × 5 verbs = 40; 2 per meta-verb × 4 meta-verbs = 8).
- 6 adversarial negatives (unrelated questions — weather, sports,
  arithmetic) that MUST score below every operating threshold.
- Seed plans loaded into an in-memory `PlanLibrary` via
  `load_seed_directory`. T1 cache populated with `PlanSessionCache`
  over an `EphemeralClient` using the bundled ONNX MiniLM embedder
  — no API keys, fully offline, reproducible on any machine.
- For each intent, the top-1 cosine hit's confidence
  (`1 - distance`) is compared against every threshold in
  `[0.40, 0.95]` step `0.05`. Counters:
  - **TP** — top hit matches expected plan AND clears threshold.
  - **FP** — (a) top hit is wrong plan above threshold, or
    (b) adversarial negative scored above threshold.
  - **FN** — positive intent's top hit fell below threshold.
  - **TN** — adversarial negative fell below threshold.

## ROC table (MiniLM, 9 seeds, 54 intents)

| thr  | TP | FP | FN | TN | precision | recall | F1 |
|-----:|---:|---:|---:|---:|----------:|-------:|---:|
| 0.40 | 18 |  6 | 24 |  6 | 0.750 | 0.429 | **0.545** |
| 0.45 | 14 |  4 | 30 |  6 | 0.778 | 0.318 | 0.452 |
| 0.50 |  9 |  1 | 38 |  6 | 0.900 | 0.191 | 0.316 |
| 0.55 |  8 |  1 | 39 |  6 | 0.889 | 0.170 | 0.286 |
| 0.60 |  4 |  0 | 44 |  6 | 1.000 | 0.083 | 0.154 |
| 0.65 |  3 |  0 | 45 |  6 | 1.000 | 0.062 | 0.118 |
| 0.70 |  1 |  0 | 47 |  6 | 1.000 | 0.021 | 0.041 |
| 0.75 |  1 |  0 | 47 |  6 | 1.000 | 0.021 | 0.041 |
| 0.80 |  0 |  0 | 48 |  6 | 0.000 | 0.000 | 0.000 |
| 0.85 |  0 |  0 | 48 |  6 | 0.000 | 0.000 | 0.000 |
| 0.90 |  0 |  0 | 48 |  6 | 0.000 | 0.000 | 0.000 |
| 0.95 |  0 |  0 | 48 |  6 | 0.000 | 0.000 | 0.000 |

Regenerate: `uv run pytest tests/test_min_confidence_calibration.py -s`.

> **Reading note**: precision = 1.000 at thresholds ≥ 0.60 is an
> abstention artifact, not evidence that the embedder is highly
> confident. Above 0.60, MiniLM has near-totally stopped firing (TP ≤ 4
> across 48 positives). The cases it does fire on happen to be correct,
> but the sample is too small to draw a quality inference. Trust the
> F1 column — not the precision column read in isolation — when
> picking an operating point.

## Findings

### F-1 — Shipped `min_confidence=0.85` is broken for the MiniLM T1 cache

At 0.85 the cache returns **zero** matches on 48 realistic paraphrase
intents. The cache path effectively never fires — every `plan_match`
falls through to FTS5 fallback. The shipped default was chosen from an
intuition calibrated against a different (stronger, unnamed) embedder
and never verified against the bundled MiniLM.

### F-2 — MiniLM cosine concentrates plan-description similarity in [0.40, 0.55]

Even the best paraphrase matches (top-1 correct plan) rarely clear
cosine 0.55. At 0.50 the ROC splits cleanly: 9 TP / 1 FP — precision
stays above 0.90 for all thresholds ≥ 0.50.

### F-3 — Adversarial negatives clear below 0.50 universally

All 6 "what's the weather", "who won the world cup" style intents
scored below 0.50. The `test_negatives_do_not_match_above_high_threshold`
test asserts the < 0.90 bound; in practice the negatives stay well
below 0.50.

## Recommended operating points

| Goal | Threshold | Tradeoff |
|------|----------:|----------|
| **F1-optimal** | **0.40** | F1 = 0.545, recall = 0.43, precision = 0.75. Best overall accuracy. |
| **Precision-first** | 0.50 | F1 = 0.316, recall = 0.19, precision = 0.90. Use when downstream cost of a wrong plan is high (e.g., auto-execution without user confirmation). |
| **Recall-first** | 0.40 | Same threshold as F1-optimal — MiniLM doesn't usefully score above 0.45 for paraphrase matches. |

## Ship decision

**Lower the shipped `plan_match` default to `min_confidence=0.40`**
for F1-optimal MiniLM operation. Callers that need precision-first
behavior override explicitly:

```python
plan_match(intent, library=lib, cache=cache, min_confidence=0.50)
```

Alternative (not shipped in this bead): migrate the T1 cache to
`voyage-context-3` (the same embedder T3 uses for docs/rdr/knowledge
collections). `voyage-context-3` is CCE-capable and should score
plan-description paraphrases substantially higher. Tracked as a
follow-up — switching requires wiring the per-session `VoyageAIEF`
into `PlanSessionCache` and paying the per-SessionStart embedding
cost against the Voyage rate budget.

## Why MiniLM cosine is low on this dataset

Two effects stack:

1. The bundled ONNX MiniLM is a small, general-purpose embedder. It
   sees plan descriptions (terse, technical, written for authoring
   humans) and natural-language intents (conversational) as semantically
   close but not near-duplicate in embedding space.
2. Plan descriptions are written as imperatives ("Walk from a concept
   into the prose corpus…"); user intents are often questions ("how
   does…", "why is…"). Cosine over MiniLM dims heavily penalises the
   phrasing difference.

Both effects dissolve under a CCE embedder like `voyage-context-3`,
which is trained to match questions with relevant documents. The
harness in this bead is the regression test that will fire when that
swap happens — re-running should show the F1 curve shift right (higher
thresholds become viable).

## Reproducibility

- Dataset: frozen in `tests/fixtures/calibration_paraphrases.py`
  (version-controlled; changes should be PR-reviewed).
- Harness: `tests/test_min_confidence_calibration.py`.
- Runtime: ~1 second on a Macbook (ONNX-only, no network).
- Test `test_best_threshold_clears_minimum_f1` asserts `F1 ≥ 0.40` at
  the best threshold; drops below 0.40 → CI fails → someone broke
  either the embedder wiring or the dataset labels.
