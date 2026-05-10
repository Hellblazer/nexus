---
title: "Honest Local-Mode Naming and Cross-Encoder Salience: Two Naming/Scoring Designs Touching the Same Test-Suite Mode-Default Surface"
id: RDR-109
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-10
revised: 2026-05-10
related_issues: [nexus-59vl, nexus-2wc1]
related_rdrs: [RDR-038, RDR-059, RDR-089, RDR-097, RDR-101, RDR-103]
github_issues: [667]
---

# RDR-109: Honest Local-Mode Naming and Cross-Encoder Salience

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Two superficially-independent feature beads landed on the same
structural surface during the 2026-05-10 sweep and both pushed
back when implemented as single PRs. They are bundled into one
RDR because they share a load-bearing precondition: **the test
suite has no first-class concept of "what mode is this test
running in"**, and any code change that makes mode-aware
decisions in production write paths immediately surfaces
inconsistent assumptions across ~30+ legacy tests.

The two beads:

### nexus-59vl (GH #667): Local-ONNX collections falsely labeled as voyage-*

- Collection names + per-chunk metadata encode `voyage-context-3`
  / `voyage-code-3` even when the on-disk vectors are 384-dim
  MiniLM produced by `LocalEmbeddingFunction`.
- RDR-103's canonical-set is hard-coded to Voyage tokens.
- Today: cosmetic + observability bug (`nx doctor` lies about
  the model in use).
- On local→cloud mode flip (user adds `VOYAGE_API_KEY` later):
  query-time correctness bug. Voyage EF returns 1024-dim
  vectors against MiniLM-384 stored vectors. ChromaDB rejects
  on dim mismatch, or — if any future change bypasses the dim
  guard — silently produces noise.
- Same RDR-059 class of bug the original Voyage tokens were
  introduced to prevent.

### nexus-2wc1: Cross-encoder salient-sentence aspect (RDR-097 follow-up)

- Reframing of SAGE (Wang et al., 2026) for nexus's existing
  cross-encoder infrastructure: score document sentences against
  seed queries at index time, store top-30% as a new aspect
  field, apply quality boost at retrieval time.
- Reuses `voyage-rerank-2.5` (cloud) / ONNX cross-encoder
  (local) — folds into the same cloud/local pattern as
  embeddings.
- Requires calibration: seed-query selection per content_type,
  quality-boost weight (the bead suggests `+0.05`), sentence
  boundary detection library choice.
- "One phase, one branch, one PR" framing in the original bead
  doesn't fit: every calibration is global and changes search
  quality across every query.

## Why bundle them in one RDR

**Surface 1 — mode-aware write paths.** 59vl introduces
`effective_embedding_model_for_writes(content_type)` which
consults `is_local_mode()` to decide whether to use the cloud
canonical token or the local-EF token. 2wc1's
attention-guided-v1 extractor also needs to pick between cloud
rerank and ONNX rerank based on `is_local_mode()`. Both fixes
inject a mode-aware decision into a write path that pre-2026
was unconditionally cloud-shaped.

**Surface 2 — test-suite mode default.** The current test
suite was written when `is_local_mode()` returned True in CI
(no API keys → local default). Tests of cloud-mode behavior
were the exception, opting in via `monkeypatch.setenv("NX_LOCAL",
"0")` + key fixtures. Tests of local-mode behavior were the
default and didn't need to declare anything.

When 59vl made the WRITE path mode-aware, ~30 tests that
asserted voyage-* collection names started failing in CI
because CI was now in local mode (their assertions wanted
voyage tokens but the writes produced local-EF tokens). The
attempted fix — an autouse `_force_cloud_mode_default` fixture
in `conftest.py` — broke 14 OTHER tests that depended on
local-mode behavior (no credentials → local chroma → no API
calls).

The same surface will trip 2wc1 when the cross-encoder picks
between cloud rerank and ONNX rerank.

**Decision: redesign the test-suite mode default as part of
the RDR scope, not as a per-PR shim.**

## Risk if we ignore this

- **59vl shipped as written**: ~30 voyage-asserting tests fail
  in CI permanently; or the autouse-fixture shortcut hides the
  inconsistency at the cost of 14 local-mode-asserting tests.
- **2wc1 shipped without calibration**: a magic constant
  (`+0.05`) ships and changes retrieval quality across every
  search with no measurement. The bead's three open questions
  (seed-query selection, sentence boundary detection,
  quality-boost weight) become permanent unmeasured choices.
- **Test suite stays mode-implicit**: every future mode-aware
  feature hits the same per-PR shim cycle.

## Proposed phases

### Phase 1: Test-suite mode-default redesign (foundation)

- Pick one of (a) local-default with explicit cloud-mode
  fixtures, or (b) cloud-default with explicit local-mode
  fixtures, or (c) per-test parametrize across both.
- Migrate the 30 voyage-asserting tests + 14 local-mode-asserting
  tests to declare their mode explicitly.
- New tests must declare mode; lint gate at PR review time.
- Removes the autouse-fixture shortcut that surfaces the
  mode-default question on every mode-aware feature PR.

### Phase 2: Honest local-mode naming (nexus-59vl)

- Add `LOCAL_EMBEDDING_MODELS` to `corpus.CANONICAL_EMBEDDING_MODELS`
  (`minilm-l6-v2-384`, `bge-base-en-v15-768`).
- Add `effective_embedding_model_for_writes(content_type)`
  mode-aware function. WRITE paths use it; tests of canonical
  schema continue to call `canonical_embedding_model` directly.
- Add `voyage_model_for_collection(name)` name-aware dispatch
  (reads conformant segment for cloud-mode-flip safety).
- Add `_embedding_fn(collection_name)` name-aware dispatch in
  T3Database (builds `LocalEmbeddingFunction` for any local-
  token collection even in cloud mode).
- Migration: existing local-mode collections keep their
  voyage-* names (no rename); new writes go to local-token
  names; cloud-credentials add doesn't break the existing
  collections (the name-aware EF dispatch keeps them
  queryable).
- Closes nexus-59vl + GH #667.

### Phase 3: Cross-encoder scoring substrate (nexus-2wc1 prerequisite)

- Add cross-encoder scoring infrastructure for arbitrary
  `[query, sentence]` pairs.
  - Cloud: `voyage-rerank-2.5` (already wired in
    `src/nexus/scoring.py:16`).
  - Local: ONNX cross-encoder (e.g.
    `cross-encoder/ms-marco-MiniLM-L-6-v2`, ~80MB).
- Cloud/local dispatch follows the Phase 2 mode-aware pattern.
- No new aspect extractor yet; just the substrate.

### Phase 4: Calibration (nexus-2wc1 measurement)

- Curate seed-query set per content_type (papers, code, prose,
  rdr).
- Build held-out QA set: 30+ questions per content_type with
  known-good chunks.
- Sweep quality-boost weight `[0.0, 0.025, 0.05, 0.075, 0.10,
  0.15]`; measure top-5 hit rate vs. baseline.
- Pick boost weight that maximizes hit rate without regressing
  any baseline-passing query (no Pareto regression).

### Phase 5: Aspect extractor + retrieval boost (nexus-2wc1 integration)

- Add `attention-guided-v1` extractor in `aspect_extractor.py`.
- New T2 aspect field `salient_sentences` in `DocumentAspects`
  (RDR-089 schema bump).
- `search_cross_corpus`: token-overlap quality boost for chunks
  whose text overlaps with stored salient_sentences.
- Gated behind `.nexus.yml` feature flag; off by default.
- A/B measurement on `knowledge__hybridrag` and
  `knowledge__rag-papers`.
- Default-on if measurements pass.

## Success criteria

- Phase 1: test suite has zero implicit-mode assertions; new
  mode-aware features land without per-PR fixture shims.
- Phase 2: GH #667 closed; mode-flip dim-mismatch hazard
  unreachable; `nx doctor` reports honest local labels.
- Phase 3-5: held-out QA hit-rate measurably improves on at
  least one corpus type at the calibrated boost weight; no
  Pareto regression on any baseline-passing query.

## Out of scope

- SAGE's differential-attention mechanism (the original
  proposal). Filed as v2 follow-up if measurement shows
  boilerplate noise.
- Query-time SAGE-style scoring. Index-time only by design.
- Renaming existing local-mode collections to use local-EF
  tokens. Existing collections keep their voyage-* names; the
  name-aware EF dispatch in Phase 2 handles them. Forced
  rename would invalidate every existing chash span and break
  the catalog manifest.

## References

- nexus-59vl + GH #667: bug + 3 fix options.
- nexus-2wc1: feature spec + calibration questions.
- RDR-097: hybrid retrieval plan (cross-encoder companion).
- RDR-038: local mode introduction (the EF dispatch this RDR
  refines).
- RDR-059: original model-mismatch incident; the precedent for
  why the dim-mismatch hazard matters.
- RDR-089: structured-aspects framework (the schema 2wc1's new
  field bumps).
- RDR-101 / RDR-103: collection-naming canonical set.
