---
title: "Honest Local-Mode Naming and Cross-Encoder Salience: Two Naming/Scoring Designs Touching the Same Test-Suite Mode-Default Surface"
id: RDR-109
type: Architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-10
revised: 2026-05-11
accepted_date: 2026-05-11
closed_date: 2026-05-20
related_issues: [nexus-59vl, nexus-2wc1, nexus-7kf7]
related_rdrs: [RDR-038, RDR-059, RDR-089, RDR-097, RDR-101, RDR-103]
github_issues: [667]
implementation_notes: |
  Implemented across multiple phases between 2026-05-10 and
  2026-05-20. Problem Statement gap closures (per /conexus:rdr-close
  pass-2 validation 2026-05-20):
  - Gap 1 (nexus-59vl GH #667: local-ONNX collections falsely
    labeled voyage-*) -> src/nexus/catalog/collection_name.py:100
    (CollectionName validates against CANONICAL_EMBEDDING_MODELS |
    LOCAL_EMBEDDING_MODELS so local-ONNX gets a local-shaped token)
  - Gap 2 (nexus-2wc1: cross-encoder salient-sentence aspect,
    RDR-097 follow-up) -> src/nexus/salience.py:58
    (extract_salient_sentences, Phase 5; salient_sentences column
    added to document_aspects via the registered migration)
  - Gap 3 (test suite has no first-class concept of mode-under-test)
    -> tests/conftest.py:251 (cloud_mode fixture; complemented by
    _MODE_LINT_EXCLUDE_FILES / _MODE_LINT_EXCLUDE_NODEIDS lint guard)

  Fallout closed:
  - nexus-7kf7 (RDR-109 Phase 2 fallout, originally mis-diagnosed
    as RDR-109-tied): actually RDR-108 Phase 3 fallout; fixed in
    PR #884 (commit e306da8d). See nexus-7kf7 close note for the
    real root cause (PDF staleness_cache + misclass-prune
    doc_id-keyed where filter).

  Process win from the close cycle: the gap-3 cloud_mode fixture
  caught the CI-vs-local mode-dispatch defect class that produced
  the #881 main-red and was later applied across the post-merge
  review follow-ups (PR #883). feedback_pin_local_mode_in_cloud_tests.md
  records the discipline.

  No deviations from the accepted approach. Closing as `implemented`.
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

#### Gap 1: nexus-59vl (GH #667) — local-ONNX collections falsely labeled as voyage-*

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

#### Gap 2: nexus-2wc1 — cross-encoder salient-sentence aspect (RDR-097 follow-up)

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

#### Gap 3: test suite has no first-class concept of "what mode is this test running in"

The current test suite was written when ``is_local_mode()``
returned True in CI (no API keys → local default). Tests of
cloud-mode behavior were the exception, opting in via
``monkeypatch.setenv("NX_LOCAL", "0")`` + key fixtures. Tests
of local-mode behavior were the default and didn't need to
declare anything. Any production write-path change that makes
mode-aware decisions immediately surfaces inconsistent
assumptions across ~30 voyage-asserting tests + 14 local-mode-
asserting tests. This is the load-bearing precondition that
forces Gaps 1 and 2 to share an RDR.

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

**Decision (2026-05-11): option (a) — local-default with
explicit cloud-mode fixtures.** Reasons:

- Matches the current CI environment (no API keys → local mode
  is what CI already runs); zero migration cost for the local-
  mode-asserting tests.
- Cloud-mode tests are the minority (~30 out of 7000+) and
  already have a clear shape (assert voyage-* tokens, expect
  cloud EF dispatch). They migrate to opt in via a
  ``cloud_mode`` fixture that monkeypatches the relevant
  ``is_local_mode``/credential getters.
- (b) would require API keys in CI or per-test mocks for every
  test that touches T3; (c) doubles the test matrix without
  evidence the dual-mode coverage is needed at this scale.

Concrete steps:

1. Add a ``cloud_mode`` pytest fixture in ``tests/conftest.py``
   that monkeypatches ``is_local_mode`` to return False and
   sets the four credential env vars
   (``CHROMA_API_KEY``/``VOYAGE_API_KEY``/``CHROMA_TENANT``/
   ``CHROMA_DATABASE``) to test sentinels.
2. Audit + migrate ~30 voyage-asserting tests to depend on the
   ``cloud_mode`` fixture (or to a class-level ``pytestmark =
   pytest.mark.usefixtures("cloud_mode")``).
3. Remove any leftover autouse mode-flipping shortcut that
   leaks across files.
4. Add a lint test (``test_mode_declarations_are_explicit``)
   implemented as a **grep-based heuristic** with a documented
   exclusion list. The check scans test files for the regex
   ``voyage-(context|code)-3`` and flags any matching test
   function whose pytest dependency graph does not include the
   ``cloud_mode`` fixture (resolved via
   ``request.fixturenames`` introspection at collection time).
   Known false-positive classes (must be excluded explicitly):
   - String literals inside docstrings or comments
     (``# voyage-context-3``).
   - ``parametrize`` data tuples whose values are not asserted
     against collection names (e.g., test data labels).
   - Tests of ``corpus.canonical_embedding_model`` itself —
     legitimately assert the canonical voyage token without
     needing cloud mode (these are schema-canonical-set tests,
     not write-path mode tests).

   The exclusion list lives in ``tests/conftest.py`` as
   ``_MODE_LINT_EXCLUDE`` and is referenced by the lint test
   so additions show up in code review. Sentence-transformer-
   shaped AST analysis is explicitly out of scope; the grep-
   plus-fixture-graph approach is sufficient given the small
   migration set.
5. Document the convention in ``tests/AGENTS.md`` (or local
   ``CLAUDE.md``).

### Phase 2: Honest local-mode naming (nexus-59vl)

- Add `LOCAL_EMBEDDING_MODELS` to `corpus.CANONICAL_EMBEDDING_MODELS`
  (`minilm-l6-v2-384`, `bge-base-en-v15-768`).
- Add `effective_embedding_model_for_writes(content_type)`
  mode-aware function. WRITE paths use it; tests of canonical
  schema continue to call `canonical_embedding_model` directly.
- Add `voyage_model_for_collection(name)` name-aware dispatch
  (reads conformant segment for cloud-mode-flip safety).
- Add `_embedding_fn(collection_name)` name-aware dispatch in
  T3Database. **Both directions are name-aware:**
  - Cloud mode + local-token collection name → builds
    ``LocalEmbeddingFunction`` (handles the legacy local
    collections after credentials added — the original 59vl
    fix).
  - Local mode + voyage-token collection name → raises
    ``IncompatibleCollectionError("collection X was indexed
    with voyage-* but cloud credentials are unavailable;
    re-index in local mode or restore credentials")``. Without
    this branch, the inverse hazard (user drops to local mode
    against legacy voyage-named collections) reproduces the
    same dim-mismatch class as RDR-059 (1024-dim stored vs
    384-dim query). Fail loud, not silent noise.
- Migration: existing local-mode collections keep their
  voyage-* names (no rename); new writes go to local-token
  names; cloud-credentials add doesn't break the existing
  collections (the name-aware EF dispatch keeps them
  queryable).
- **Reindex-creates-ghost handling**: on first write to a
  local-token-named collection for a repo that already has a
  voyage-named collection in T3, log a structured warning
  (``legacy_voyage_collection_superseded``) naming both
  collections and recommending ``nx catalog gc`` (or filing a
  follow-up bead for the GC pass). The legacy collection is
  NOT auto-deleted — operators decide.
- Closes nexus-59vl + GH #667.

### Phase 3: Cross-encoder scoring substrate (nexus-2wc1 prerequisite)

- Add cross-encoder scoring infrastructure for arbitrary
  `[query, sentence]` pairs.
  - Cloud: `voyage-rerank-2.5` (already wired in
    `src/nexus/scoring.py:16`).
  - Local: ONNX cross-encoder (e.g.
    `cross-encoder/ms-marco-MiniLM-L-6-v2`, ~80MB).
- Cloud/local dispatch follows the Phase 2 mode-aware pattern.
- **Dependency packaging (resolves a Phase 3 blocker)**:
  Phase 3 must NOT introduce a PyTorch dependency (RDR-038
  F-03 is load-bearing: zero-PyTorch is why local mode is
  viable on machines without a CUDA stack). Required research:
  evaluate ``fastembed`` cross-encoder support first (already
  the runtime backing ``LocalEmbeddingFunction``), fall back
  to ``onnxruntime``-direct only if fastembed lacks CE rerank.
  ``sentence-transformers`` is explicitly out of scope (pulls
  PyTorch). The resolved choice ships as an optional extra
  ``conexus[cross-encoder]`` in ``pyproject.toml`` so the
  default install remains lean.
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

## Phase coupling and shipping order

Phases 1-2 are tightly coupled (Phase 2 cannot land without
Phase 1 unblocking the test suite) and ship together as a
single feature arc. Phases 3-5 share the mode-aware-write-path
substrate from Phase 2 but are otherwise independent:

- **Phase 2 closes nexus-59vl as soon as Phase 2's PR merges.**
  Holding the bead's close gate on Phase 4's calibration would
  conflate two unrelated concerns. The bead's only contract is
  honest naming + the dim-mismatch-hazard fix.
- Phase 3 may ship before Phase 4 calibration completes; it
  adds the substrate without changing retrieval ranking. Phase
  4 then exercises the substrate against held-out QA. Phase 5
  ships the user-visible aspect extractor + boost only after
  Phase 4 measurements pass.
- If Phase 4 or 5 surfaces blocking design questions, Phases
  3-5 may split into a separate RDR; Phase 2's close gate does
  not depend on that decision.

## RDR-103 intersection

RDR-103 Gap 2 introduced ``canonical_embedding_model(content_type)``
in ``src/nexus/corpus.py:115`` as the single source of
truth for cloud-mode collection naming. Phase 2's
``effective_embedding_model_for_writes(content_type)`` does
NOT replace that function — it WRAPS it. Cloud mode delegates
to ``canonical_embedding_model`` verbatim (preserving the
RDR-103 invariant); local mode returns the local-EF token.
Tests of the RDR-103 canonical set continue to call
``canonical_embedding_model`` directly and stay independent
of mode. The naming-authority surface remains single-rooted;
mode-awareness is layered on top.

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

## Phase 4b measurement outcome (2026-05-11)

Calibration sweep run via `scripts/rdr-109-calibrate.py` over four
content_types with 35 programmatically-generated Q&A items each
(see `data/calibration/rdr-109/results.md` for full table).

| content_type | baseline | best non-zero weight | Pareto-clean? |
|---|---:|---:|---|
| knowledge | 0.286 | 0.343 (-2 regression) | **NO** |
| rdr | 0.343 | 0.343 (no movement) | n/a |
| code | 0.486 | **0.514 @ w=0.025** | **YES** |
| docs | 0.429 | **0.486 @ w=0.025** | **YES** |

Boost mechanism: token-overlap between the query and per-chunk
salient sentences (extracted by `scripts/rdr_109_salience.py` via
Phase 3 cross-encoder + per-content-type seed queries). All weights
≥ 0.025 produce identical hit rates because the typical overlap is
1-3 tokens out of ~10 query tokens; the boost is therefore a
near-uniform tie-breaker rather than a magnitude-sensitive signal.

Decision: ship Phase 5's boost as opt-in (default OFF) per the
existing feature-flag plan; recommend `w=0.025` as the
default-when-on value. The Phase 5 default-on gate (boost passes
measurements with no Pareto regression) is NOT met for knowledge
corpora. Per the lines 290-293 split clause, this is documented as
the Phase 5 design constraint rather than a blocker: the mechanism
ships, the default does not.

Caveats:

- Programmatic Q&A (template-derived from chunk content) produces
  near-paraphrase questions whose retrieval characteristics may
  differ from hand-curated benchmarks. The aggregate trend
  (mechanism positive on code/docs, neutral on rdr, mixed on
  knowledge) is informative; absolute hit rates should be
  interpreted within this caveat.
- Sweep ran on small corpora (rag-papers 142, workspace-code 927,
  docs-1-4 384, rdr-1-2 130) for tractable salience-cache build.
  Larger corpora may exhibit different reordering windows.

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
