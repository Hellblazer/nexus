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
  2026-08-22: the voyage-context-3 follow-up this document proposed is
  RETIRED (Sam's decision; see "Alternative considered and RETIRED").
  The plan cache stays on the bundled MiniLM. The population this
  calibration measured — category-level templates — no longer uses the
  cosine path at all, so the 0.40 floor now governs only instance-level
  grown plans.
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

### Alternative considered and RETIRED 2026-08-22

This document originally proposed migrating the T1 plan cache to
`voyage-context-3` and tracked it as a follow-up. **That prescription is
retired: the plan cache stays on the bundled MiniLM.**

Retired by Sam's decision ("we are not substituting voyage embeddings,
that is unworkable"), and the technical case for it had already weakened.
The swap would have paid a per-SessionStart embedding cost against the
Voyage rate budget, on the hot path ahead of every plan match — including
the cache hits that finish in milliseconds — to improve a similarity
score that, for the largest class of plan, cannot help.

Measured 2026-08-21: a category-level plan is TOPIC-FREE by construction
("research any concept") while a real question is topic-bearing, so it
loses a topical-similarity contest no matter how good the embedder is. On
one probe all 17 shipped templates sat inside a 0.12-wide cosine band —
that is an absence of signal, not the phrasing penalty this document
diagnosed below. Category plans now bypass cosine entirely and route by
DIMENSION (T2 `design-dimension-routed-category-plans-2026-08-21`), so
the population this calibration measured no longer uses the cosine path
at all.

What the cosine path serves now is INSTANCE-level grown plans, matching
question against question — where MiniLM already scores 0.94 on a
verbatim repeat. Fixes there are structural rather than embedding
quality: see nexus-7g0rg (a 0.94 match dropped by the unanchored-grown
filter, fixed) and nexus-93cc6 (grown plans carry the originating
question verbatim instead of a generalised description, open).

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

Both effects would be reduced by an embedder trained to match questions
against documents. That is not the path taken — see the retirement note
above — and it would not have addressed the larger effect, which is that
a topic-free description has no topic to match against at any embedding
quality.

The harness in this bead remains as a REGRESSION PIN on the numbers this
document measured, not as an anticipation of a swap. Re-running it after
any change to the plan cache's embedding or match-text synthesis should
reproduce the ROC table above; a drift means something moved under the
threshold this document chose.

## Reproducibility

- Dataset: frozen in `tests/fixtures/calibration_paraphrases.py`
  (version-controlled; changes should be PR-reviewed). The 8 meta-verb
  positives were removed 2026-08-22 when those templates were retired
  (nexus-77cct) — 40 positives remain.
- Harness: `tests/test_min_confidence_calibration.py`.

  **CORRECTION 2026-08-22:** that harness did not exist when this section
  was written, and had never been committed — verified against the full
  git history four months after this RDR closed. Only the dataset
  shipped. So the CI guarantee asserted below was false for the whole
  period, and the threshold governing every cosine plan match was
  unguarded while a closed design record said otherwise. The harness has
  now been written and the claim is true.
- Runtime: ~1 second on a Macbook (ONNX-only, no network).
- Test `test_best_threshold_clears_minimum_f1` asserts `F1 ≥ 0.40` at
  the best threshold; drops below 0.40 → CI fails → someone broke
  either the embedder wiring or the dataset labels. (True as of
  2026-08-22; see the correction above for its first four months.)

### What this calibration no longer establishes

Re-run 2026-08-22 against the current tree, the ROC has moved and, more
importantly, so has its relevance:

| threshold | F1 | TP | FP | FN |
|----------:|---:|---:|---:|---:|
| 0.15 | 0.633 | 19 | 17 | 5 |
| **0.40** (shipped) | **0.367** | 9 | 6 | 25 |
| 0.50 | 0.222 | 5 | 0 | 35 |

F1 plateaus around 0.06-0.11 (F1 = 0.698) and is 0.367 at the shipped
0.40 — the optimum sits at a threshold that admits nearly everything — and the intended plan is rank 1 for only 22 of 40
curated paraphrases even ignoring the threshold. That is this document's
own dataset corroborating, from the other side, why category-level plans
stopped competing on cosine at all.

Which is most of the point: every positive in this dataset is a
category-level verb default, and those plans usually no longer reach the
confidence floor — they route by dimension instead.

**Usually, not always.** The category route fires only when `nx_answer`
derives a verb from the question, and `infer_verb` returns `None` for
plenty of ordinary phrasings. On that path a category plan is gated by
`min_confidence` exactly as this document measured. So the populations
OVERLAP rather than being disjoint (an earlier version of this note
claimed disjoint; that was wrong), and the floor still governs
category plans whenever verb inference misses — as well as the
instance-level grown plans for which no dataset exists at all. The 0.40 floor is therefore
inherited rather than justified: the same state RDR-078's 0.85 was in
before this document measured it. Tracked; do not re-tune the floor from
the curve above, which would be calibrating against traffic that does not
use it.
