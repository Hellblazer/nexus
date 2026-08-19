---
title: "Token-Aware Voyage Batch Splitting: Make the 120K-Tokens-Per-Request Ceiling a Planned Bound Instead of an Opaque 500"
id: RDR-195
type: Bug Fix
status: accepted
accepted_date: 2026-08-19
priority: high
author: Gerasimos Pollatos
reviewed-by: self
created: 2026-08-19
related_issues: ["#1465"]
---

# RDR-195: Token-Aware Voyage Batch Splitting

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Indexing a large PHP repository into a `code__*` collection fails. The engine forwards the whole
upsert batch into one Voyage `/v1/embeddings` request, Voyage rejects it for exceeding the
per-request token ceiling, and the failure reaches the user as an untyped engine 500:

```
ERROR d.nexus.service.http.VectorHandler - event=vector_handler_error op=/upsert-chunks
java.lang.RuntimeException: Voyage AI request failed: HTTP 400 body={"detail":"Request to model
'voyage-code-3' failed. The max allowed tokens per submitted batch is 120000. Your batch has
280871 tokens after truncation. Please lower the number of tokens in the batch.",
"error_code":"TOO_MANY_TOKENS_IN_BATCH"}
```

Every bound on a Voyage embed request in this stack is a **row count** derived from gateway-timeout
or local-memory reasoning. No layer has ever been token-aware. The ceiling is therefore not
approached and respected — it is discovered by hitting it, on a request that has already been
billed for tokenization.

### Enumerated gaps to close

#### Gap 1: No token-aware batching on the Voyage standard-embeddings path

`VoyageEmbedder.embed` / `embedWithUsage` / `embedDouble`
(`service/src/main/java/dev/nexus/service/vectors/VoyageEmbedder.java:100-146`) each serialize their
entire input list through one `buildJson` into one POST. `EmbedderRouter.embedForCollectionWithUsage`
(`EmbedderRouter.java:219-224`) is a pure pass-through, and `PgVectorRepository.upsertChunksInternal`
(`PgVectorRepository.java:655-659`) makes exactly one embed call per inbound request. The only
upstream bound is the Python client's `_CODE_UPSERT_CHUNK_CAP = 300`
(`src/nexus/db/http_vector_client.py:518`), a count.

300 chunks at up to `SAFE_CHUNK_BYTES` (12,288 — `src/nexus/db/limits.py`) is ~3.6 MB of source in
one request, on the order of 1M tokens against a 120,000-token ceiling. The observed failure —
280,871 tokens, 2.3x over — is a mild instance, not a worst case. Even a modest ~1,400-byte average
chunk puts 300 of them over the line.

`Bge768Embedder` already solved the structurally identical problem for the local ONNX path
(`nexus-zu4ma`, shipped in `engine-service-v0.1.81`): a greedy sub-batch planner bounded by
`MAX_PADDED_TOKEN_AREA` (`Bge768Embedder.java:119-124`). The Voyage path never received the
equivalent treatment, so the engine has one embedder that plans its batches and one that does not.

#### Gap 2: The token-limit 400 is untyped, non-retryable, and unrecoverable

`VoyageEmbedder.callApi` treats only `429` and `>= 500` as retryable (`:223`) and maps `401/403` to
the typed `UpstreamAuthException` (`:230-237`). Every other status — including this precisely
described, perfectly actionable 400 — falls through to a bare `RuntimeException` (`:238-239`), which
`VectorHandler` renders as a 500 (`VectorHandler.java:211-217`). On the client side
`_GATEWAY_RETRY_CODES = {502, 503, 504}` (`http_vector_client.py`) excludes 500, so nothing retries
and nothing adapts.

The upstream response carries a machine-readable `error_code` and the exact offending token count.
Discarding that into an untyped 500 throws away the one signal that makes the whole problem
self-correcting.

#### Gap 3: A single oversize file has no recovery path

`ChunkBatcher._flush_batch` bisects a failed flush only when the batch spans **two or more files**
(`src/nexus/chunk_batcher.py:477`). A single large source file whose own chunks exceed the token
ceiling takes the `error` arm and fails outright. The one recovery mechanism that exists cannot
engage on the narrowest case.

## Context

### Background

Discovered 2026-08-19 while indexing a large PHP repository in `mode: local` with
`embed_model: voyage-code-3` — a local engine on the reporter's own box calling Voyage for
embeddings.

Reproduced on **`engine-service-v0.1.80`** (the `nexus-service-mac-arm64` signed native binary,
installed per RDR-161), which was this branch's `REQUIRED_ENGINE_VERSION` floor at reproduction
time. The floor has since moved to `(0, 1, 82)` (the v0.1.81/v0.1.82 cuts of 2026-08-19, which
shipped with conexus 7.11.0); neither cut touched `VoyageEmbedder` or the client paging loop, so
the defect is live on the currently-pinned engine identity as well — not an artifact of a stale
local build.

Impact is a hard stop, not a degradation: affected files are not indexed, and the operator sees a
500 with no indication that batch size is the cause or that a smaller batch would succeed. Because
the client cap is a count, the failure is a function of the *content* being indexed — repositories
with large source files fail where repositories with small ones do not, with no diagnostic
distinguishing them.

The blast radius is `voyage-code-3` and `voyage-3` only. `CceEmbedder` (`voyage-context-3`, serving
the `docs__`/`knowledge__`/`rdr__` prefixes) issues **one API call per text**
(`CceEmbedder.java:200-233`) to match Python's per-text convention, so it cannot exceed a *batch*
ceiling by construction. This RDR does not change CCE.

### Technical Environment

- Engine: Java 25, `dev.nexus.service`, `com.sun.net.httpserver`-based HTTP layer.
- Voyage AI `/v1/embeddings`, `encoding_format: "base64"`, `truncation: true`. The request body is a
  **byte contract** (`nexus-f4wcg`): Voyage serves per-request-stable results that can differ across
  byte-different-but-semantically-equal bodies by ~4e-5 cosine, so `buildJson` is byte-faithful to
  the Python SDK's wire body and locked by `VoyageEmbedderBodyTest`.
- Client: `HttpVectorClient.upsert_chunks` pages by `per_collection_chunk_cap`
  (`http_vector_client.py:637-759`), the self-described "ONE choke point" at `:1563-1567`.
- Prior art in the same package: `Bge768Embedder`'s `nexus-zu4ma` sub-batch planner, with its
  package-private `onnxInvocationCount()` non-vacuity instrument (`Bge768Embedder.java:141-147`,
  `:362-370`) and the `Bge768BatchCompositionTest` composition-stability gate.

## Research Findings

### Investigation

Traced the full embed path from the client's batch assembly to the Voyage POST, and audited the
engine for any pre-existing token accounting.

- **Client batching.** `per_collection_chunk_cap` (`http_vector_client.py:637-759`) resolves to
  `_ONNX_LOCAL_UPSERT_CHUNK_CAP` (16, memory-derived), `_CODE_UPSERT_CHUNK_CAP` (300), or
  `_CCE_UPSERT_CHUNK_CAP` (64, timeout-derived after a live 504 at 172 CCE chunks). The function's
  own docstring states that **no token-budget cap is added** and classifies one as "a throughput
  optimization for a future pass, not a correctness gap." That reasoning was written for the
  onnx-local memory constraint, where 512-token truncation bounds the worst case. It does not
  transfer to Voyage, where the ceiling is a hard remote API limit.
- **Paging.** `upsert_chunks` pages at `:1563-1567` with a fixed stride `range(0, n, cap)`. Both the
  `ChunkBatcher` flush path and the legacy per-file fallback route through it, making it the single
  effective control point on the client.
- **Engine.** `VoyageEmbedder` declares exactly three constants (`:61-63`): the URL, `MAX_RETRIES`,
  `RETRY_BASE_MS`. No batch cap, no token budget, no estimator. `truncation: true` (`:166`) caps each
  *individual* text at the model's 32K context; it does nothing about the batch total — the error
  message's "after truncation" confirms Voyage applies per-text truncation first and *then* rejects
  the aggregate.
- **No token utilities exist.** Swept the engine for `MAX_BATCH`, `estimateTokens`, `tiktoken`,
  `120000`, `MAX_TOKENS`. The only tokenizers are HuggingFace BERT-vocab ones for the local ONNX
  models (`Bge768Embedder.java:73`); none is Voyage's BPE. `limits.py` has no token constant.
- **Precedent.** `Bge768Embedder` (`:100-147`, `:247-370`) is the in-package template for exactly
  this fix: a documented budget in a derived unit, a greedy planner, a single-call fast path when the
  input already fits, and an invocation counter that lets the test prove a split occurred without a
  mocking framework.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Voyage AI `/v1/embeddings` | Yes (official docs, Context7) | Max 1,000 inputs per request. Total tokens per request varies by model: **120,000** for `voyage-code-3`, `voyage-3-large`, `voyage-finance-2`, `voyage-law-2`; 320,000 for `voyage-3.5`/`voyage-2`; 1,000,000 for `*-lite`. Per-text context 32K. |
| Voyage AI `/v1/contextualizedembeddings` | Yes (official docs, Context7) | Max 1,000 input lists, 16,000 chunks, **120,000** tokens with auto-chunking — **32,000** without. Not exercised by this RDR (CceEmbedder is per-text) but recorded: a future CCE batching change inherits a *tighter* ceiling than the standard endpoint. |
| Voyage AI tokenization | Yes (official docs) | Token counting is offered only via the Python SDK's `count_tokens` (HuggingFace `tokenizers`). No JVM-native path, no documented ratio. Confirms an exact in-engine token count is not available. |
| `Bge768Embedder` (in-repo) | Yes (source) | Established sub-batch planner pattern, budget unit, and non-vacuity instrument. Reused as the design template. |

### Key Discoveries

- **Documented** — `voyage-code-3`'s per-request ceiling is 120,000 tokens with a 1,000-input cap.
  The 1,000-input cap is never the binding constraint here: the client already caps at 300.
- **Documented** — Voyage truncates each text to the model context *before* evaluating the batch
  total, so per-text truncation can never rescue an oversize batch. The error string's "after
  truncation" is direct evidence.
- **Verified (source)** — the 400 response carries a stable machine-readable
  `error_code: "TOO_MANY_TOKENS_IN_BATCH"`. This makes an adaptive split possible, which in turn
  means a token *estimate* never has to be correct for the system to be correct.
- **Verified (source)** — no exact Voyage token count is obtainable inside the engine. Any in-engine
  budget must be an estimate.
- **Verified (source)** — `CceEmbedder`'s per-text fan-out makes the CCE path immune, so the fix is
  correctly scoped to `VoyageEmbedder` and must not be hoisted into `EmbedderRouter`, which serves
  both.
- **Verified (source)** — the embed call at `PgVectorRepository.java:640-659` and
  `CombinedWriteService.java:190-197` both run **outside any transaction** (RDR-181), so splitting one
  logical embed into several upstream calls introduces no transactional exposure.
- **Verified (source)** — the parity gate cannot be perturbed by this change: `EmbedParityTest`'s
  `CORPUS` is three short texts (a few hundred tokens) and `tests/db/test_embed_parity.py` drives
  `/v1/vectors/embed` with the same corpus. Both stay orders of magnitude below any budget, take the
  single-request fast path, and therefore keep a byte-identical body.

### Critical Assumptions

- [x] Voyage's `/v1/embeddings` embeds each input independently, so partitioning a batch does not
  change any individual vector — **Status**: **Verified — Live Spike (2026-08-19, nexus-kmtlp.1)**
  — **Method**: live `voyage-code-3` calls with the engine's exact `buildJson` body shape; record
  T2 `nexus_rdr/195-research-3`. Five probe texts (56 B to 9 KB, code/prose/unicode) embedded
  alone, inside a 125-input/65K-token batch, inside a 29-input batch, and (round 3) inside a
  263-input/101K-token near-ceiling batch, at different positions; worst cross-composition
  cosine 0.99994, max-abs 1.1e-3, and the 9 KB and 2.2 KB probes bit-exact across compositions.
  What the evidence supports, stated no more strongly than measured: a text's vector takes one
  of a small number of discrete, numerically-equivalent variants (≤ 2.2e-4 cosine apart) that
  byte-identical repeated requests ALSO cycle through — 20 identical single-input repeats gave
  1–2 distinct vectors per text, 20 identical 2-input repeats gave 2 per text, and the identical
  near-ceiling batch repeated gave 168/263 bit-identical vectors with min cosine 0.99978 — so
  every cross-composition deviation observed (≥ 0.99994) is SMALLER than the variance the same
  request exhibits on repeat. Composition was never seen to move a vector outside that band, and
  shuffled vs different-filler vs alone comparisons gave identical or near-identical results
  (no dependence on neighbour content). That is the property the design needs: splitting a
  batch changes nothing beyond the variance the unsplit request already carries, and the status
  quo (300-text batches, bisect re-composition) already lives with it. Whether the variants come
  from replica, kernel path, or padding shape is not established and is not load-bearing.
  Consequence for tests: any live-Voyage equality assertion on batched output must tolerate
  ~2e-4 cosine; `1 - 1e-6` is the ONNX bar, not Voyage's.

  This was recorded as unverified deliberately until the spike ran, because it is the **only**
  assumption here whose failure is *silent*: wrong vectors would be stored with no error, no 400,
  and no log line. Every other failure mode in this RDR is loud.

  The three pieces of pre-spike supporting evidence, kept for the record:
  (a) The architecture itself is the strongest signal — cross-input context is what the *separate*
  contextualized endpoint and model exist to provide. `CceEmbedder.java:202` states outright that
  batching multiple texts into one CCE call "produces different embeddings due to cross-document
  context propagation", making the standard endpoint the deliberate contrasting case.
  (b) The status quo **already depends on this property**: `VoyageEmbedder` sends up to 300 texts
  per request today, and `ChunkBatcher`'s bisect (`chunk_batcher.py:477`) already re-composes
  batches in production on flush failure. This RDR does not introduce the dependency; it increases
  how often re-composition happens.
  (c) `Bge768BatchCompositionTest` gates the analogous property for the local ONNX path
  (cosine `1 - 1e-6`, measured floor ~1e-12) — but that is a *different embedder*, so it is an
  analogy, not corroboration of Voyage's remote behavior. Labelling it as corroboration was an
  overclaim in the first draft of this RDR.

  **The spike** (run, see Status above): embed one fixed text alone, then inside a large batch,
  and diff the vectors. Cheap, decisive, and it ran before implementation began.
- [x] The `nexus-f4wcg` byte contract survives, because `buildJson` is not modified and each
  sub-batch is serialized by the same unmodified method — **Status**: Verified — **Method**: Source
  Search (`VoyageEmbedder.java:150-169`; `VoyageEmbedderBodyTest` asserts `buildJson` output only).
- [x] The embed-parity gate never splits and therefore cannot regress — **Status**: Verified —
  **Method**: Source Search (`EmbedParityTest` `CORPUS`; `tests/db/test_embed_parity.py`).
- [x] `error_code: "TOO_MANY_TOKENS_IN_BATCH"` is the discriminator for an oversize batch —
  **Status**: Verified — **Method**: Source Search of the captured upstream response body.
- [ ] The bytes-per-token ratio for real source code — **Status**: Unverified, and deliberately left
  so — **Method**: Spike during implementation. This is **not load-bearing**: see Decision Rationale.
  The adaptive split demotes the estimator to a throughput parameter. A wrong ratio costs extra round
  trips, never a failed index.

**Method definitions**:

- **Source Search**: API verified against dependency source code (standard method for libraries)
- **Spike**: Behavior verified by running code against a live service (for opaque services only)
- **Docs Only**: Based on documentation reading alone (insufficient for load-bearing assumptions)

## Proposed Solution

### Approach

Two changes, at two layers, for two distinct reasons.

**1. Engine — a token-aware sub-batch planner in `VoyageEmbedder`, plus a typed oversize-batch
error with adaptive halving.** This is the authoritative enforcement point: it is below every
caller (`PgVectorRepository`, `CombinedWriteService`, the `/v1/vectors/embed` parity endpoint,
`StagingHandler`) and cannot be bypassed. It follows the `nexus-zu4ma` planner already established
in this package.

The planner is a *throughput* mechanism and the typed error is the *correctness* mechanism. That
split is the core of this design; it is argued once, canonically, in Decision Rationale.

**2. Client — a byte budget on the `upsert_chunks` paging loop.** Justified **not** as
defense-in-depth but by version skew: the engine and the Python client ship on **decoupled release
lifecycles** (AGENTS.md documents the engine-service release as an explicitly separate cadence). A
client release reaching a user whose engine predates the engine half is a legitimate, unpreventable
deployment state. The client must not depend on engine behavior it cannot assert.

**Residual risk for exactly that population, stated rather than glossed.** A new-client/old-engine
user gets a byte proxy for tokens with **no typed-400 backstop**, because the old engine has none —
which is precisely the profile Alternative 3 (client-only) is rejected for. The client half
therefore **reduces the probability** of this failure for skewed users; it does not eliminate it.
Two consequences follow, and the second is a design constraint rather than a caveat:

1. Skewed users can still hit the ceiling on unusually token-dense content, and for them the
   failure remains an opaque 500. Only the engine half makes it recoverable.
2. The client's byte budget must therefore be sized **with deliberate extra headroom below** the
   engine's per-model token budget — not tuned to the same target. The client is the layer without
   a safety net, so it should be the more conservative of the two. Sizing them equally would be the
   worst of both worlds: no headroom where there is no backstop.

### Technical Design

**Engine — `VoyageEmbedder.java`.**

Constants beside `:61-63`, each documented with its provenance in the `nexus-zu4ma` style:

- `MAX_BATCH_TEXTS` — 1,000, the documented API input cap. Asserted, never silently truncated,
  following `VoyageReranker.MAX_DOCS_PER_REQUEST` (`VoyageReranker.java:58-59`, `:130-135`).
- `MAX_BATCH_ESTIMATED_TOKENS` — a budget below the documented ceiling, with headroom for estimator
  error in the *cheap* direction. **This must be resolved per model, not hardcoded.**
  `VoyageEmbedder` is instantiated with **two** different models — `voyage-code-3`
  (`EmbedderRouter.java:104`, `:142`) and `voyage-3` (`:112`, `:145`) — and the documented ceiling
  is per model tier (120,000 for `voyage-code-3`; 320,000 for the `voyage-3.5`/`voyage-2` tier).
  A single constant would therefore be either incorrect for one model or needlessly punitive for
  the other. The budget is keyed off the instance's own `model` field, with an explicit
  **fail-safe default of the tightest documented ceiling (120,000) for any model not in the
  table**. That direction is deliberate: an unknown model can then only ever be *over*-split, which
  costs round trips, never *under*-split, which would fail. Note that `voyage-3` does not appear in
  Voyage's current published limit table at all (the tiers list `voyage-3.5`/`voyage-2` but not
  `voyage-3`), which is precisely why the default must be conservative rather than guessed.
- A bytes-to-tokens divisor, explicitly labelled provisional and self-correcting.

Interfaces (signatures only; no implementations per template guidance):

```text
// Illustrative — verify API signatures during implementation
List<List<String>> planBatches(List<String> texts)      // greedy; never emits an empty batch;
                                                        // a single over-budget text gets its own
static long estimateTokens(String text)                 // provisional; UTF-8 length / divisor
int voyageRequestCount()                                // package-private non-vacuity instrument
void resetVoyageRequestCount()                          // package-private, mirrors Bge768Embedder
```

- `embed`, `embedWithUsage`, `embedDouble` iterate `planBatches(texts)`, concatenate results **in
  input order**, and — for `embedWithUsage` — **sum** `usage.total_tokens` across sub-calls, exactly
  as `CceEmbedder.embedWithUsage` already does (`CceEmbedder.java:223-233`).
- **Single-request fast path**: when the input already fits, exactly one POST is issued with a body
  byte-identical to today's. `Bge768Embedder` takes the same care (`:261`).
- `buildJson` is **not modified**. Each sub-batch is serialized by the same method, so the
  `nexus-f4wcg` byte contract holds per request and `VoyageEmbedderBodyTest` needs no change.
- `callApi` gains recognition of `400` with `error_code == "TOO_MANY_TOKENS_IN_BATCH"`, surfaced as a
  typed exception (naming to follow the existing `UpstreamAuthException` convention). The caller
  catches it, halves that sub-batch, and retries the halves. A single text that still trips it is
  rethrown with the upstream detail intact — that is a genuine, un-splittable input, and it must
  fail loudly rather than be dropped.
**Termination and cost bound.** The adaptive split must terminate, and a reviewer should not have
to derive that:

- Each halving level strictly decreases sub-batch size, reaching size 1 in at most
  `ceil(log2(n))` levels.
- A size-1 batch cannot trip the limit **for any model whose limits are published**.
  `truncation: true` is always sent (`:166`, and `nexus-f4wcg` records that omitting it is
  load-bearing for parity, so it will not be removed), so Voyage truncates each text to the model
  context *before* evaluating the aggregate. For `voyage-code-3` that context is 32,000 tokens
  against a 120,000 batch ceiling.
- **Scope of that claim, stated precisely.** Voyage publishes a generic 32,000-token per-text
  context and per-tier batch ceilings, but not a per-model context table — and `voyage-3`, one of
  only two models `VoyageEmbedder` instantiates, is absent from the published tiers entirely (see
  Technical Design). So "context < ceiling for every model" is **expected, not proven**, for an
  undocumented model. The design is safe either way: the conservative per-model default and the
  defensive rethrow both fail in the loud direction.
- Therefore halving terminates in **success** at or before size 1 for every documented model, and
  the "single text still 400s" arm is a **defensive** rethrow — for an upstream contract change, or
  for an undocumented model whose context exceeds its ceiling. Not a path this design expects to
  reach, but not provably unreachable either, and it is more honest to say so than to claim a proof
  the published data does not support.
- **Cost** is logarithmic in estimator error, not linear: a divisor optimistic by a factor `k`
  costs roughly `ceil(log2(k))` extra round trips per oversize batch. Those failed attempts *are*
  billed by Voyage (the request was tokenized before rejection), so the cost is real, bounded, and
  shrinks to zero as the divisor is calibrated — which is exactly why Performance Expectations
  measures adaptive-split counts.
- **A single pathological item does not degrade that to linear.** With one over-budget chunk among
  many, only the half containing it keeps failing, so each level costs one failure plus one success
  — about `2 * log2(n)` requests, still logarithmic. And on the *indexing* path a single
  over-budget chunk cannot arise at all: chunkers cap at `SAFE_CHUNK_BYTES` (12,288 bytes ≈ a few
  thousand tokens, `src/nexus/db/limits.py:39`), far below any per-model budget. The unbounded
  `/v1/vectors/embed` parity endpoint is the one caller that accepts arbitrary text and could
  present such an item. Cheap insurance regardless: cap total sub-requests per original batch and
  fail loudly on exhaustion, so no input can turn one logical embed into unbounded upstream spend.
  Specified (gate remediation, 2026-08-19): a constant `MAX_SUB_REQUESTS_PER_BATCH = 64` beside the
  planner's other constants — ≥ `2 * log2(n)` for any n the 1,000-input API cap admits, with a wide
  margin over the ~`ceil(1000/300)`-to-tens range the planner produces in practice, so it can only
  fire on adversarial input, never on calibration drift. On exhaustion the embed fails with the
  typed oversize error carrying the sub-request count and the offending batch's size — same loud
  path as the single-unsplittable-text rethrow, never a silent partial result.
  `VoyageEmbedderBatchSplitTest` includes an exhaustion case (cap forced low via the test seam)
  asserting the typed error and the absence of further upstream calls.
- **Retry budgets do not multiply.** `callApi` retries only 429/5xx (`:223`), so the split must
  live **above** `callApi`, in the caller owning the sub-batch list. The split path and the
  transient-retry path are then orthogonal: each sub-request keeps its own independent 3-attempt
  transient budget and the halving contributes a separate O(log) factor rather than compounding
  into `3^depth`. This placement is a design constraint, not an implementation detail.
- **Billing** inherits the existing documented stance (`:203-206`): `usage.total_tokens` comes from
  the final successful response, so the reported total under-counts tokenization Voyage billed for
  rejected attempts. Same safe direction as today, not a new divergence.
- **Request-rate interaction (429), distinct from the retry-budget point above** (maintainer
  addition, 2026-08-19): sub-batching multiplies the request *rate* — one logical embed that was
  one oversized POST becomes N smaller POSTs in quick succession, and a bulk index run multiplies
  that again across pages. This is a live incident class: a 2026-08-15 bulk index run tripped
  Voyage rate limits (conexus-ddh0) with today's one-POST-per-page behavior, so the planner makes
  that pressure strictly worse in exchange for making each request valid. The design's answer is
  the existing per-sub-request 3-attempt 429/5xx retry with backoff in `callApi` (each sub-request
  arrives with the same independent budget it has today), plus the sub-request cap above bounding
  worst-case fan-out per logical batch. If bulk-run 429 pressure recurs post-implementation, the
  remedy is pacing at the *caller* (the staged bulk-load pipeline below, or inter-sub-request
  delay), not loosening the planner — the planner's job is validity, not throughput shaping.
- **Staged bulk-load reconciliation** (maintainer addition, 2026-08-19): the planned `/v1/staging`
  bulk load-then-promote pipeline (bead nexus-b50zw) is complementary, not overlapping — the
  engine-side planner covers every Voyage call including staged `embed_fill` (`StagingHandler` is
  already in the caller list), while the client byte budget below applies only to the direct
  `upsert_chunks` paging path and deliberately does not extend to a staged path, whose batch
  shaping is owned by the staging pipeline's own design.

- **Test seam**: add the injectable constructor mirroring `VoyageReranker.java:90-100`
  (`url`, `retryBaseMs`, `Optional<ProxySelector>`; production keeps the 3-arg form). Today
  `VoyageEmbedder` has only the 3-arg constructor and a `private static final VOYAGE_URL` (`:61`), so
  **no fake-server test of this class is possible at all**. This is a prerequisite of the test plan,
  not a convenience.

**Client — `src/nexus/db/http_vector_client.py`.**

- Replace the fixed-stride `range(0, n, cap)` at `:1567` with an accumulator closing a page when
  either the count cap or a byte budget would be exceeded. Always emit at least one chunk per page,
  so a single large chunk still ships (it is already bounded by `SAFE_CHUNK_BYTES`).
- New module constant beside `_CODE_UPSERT_CHUNK_CAP` (`:518`), type-hinted per project convention.
  **Sized with deliberate extra headroom below the engine's per-model token budget, not tuned to the
  same target** (see Approach, residual-risk note): against a pre-fix engine this constant has no
  typed-400 backstop behind it, so the layer without a safety net must be the more conservative of
  the two. Its docstring should say so, since the reason is not visible from the constant itself.
- Applied **only** where the collection routes to a genuine multi-text Voyage batch. Reuse the
  existing `_CCE_COLLECTION_PREFIXES` (`:634`) and `_serving_embedding_mode()` that
  `per_collection_chunk_cap` already consults (`:748-759`) — CCE fans out per text and onnx-local has
  its own memory-derived cap, so neither should change behavior.
- Preserve the `embeddings` passthrough slicing in lockstep with ids (`:1576-1577`) under the now
  variable stride, and keep the existing per-page structured logging.
- **Passthrough pages are exempt from the byte budget** (maintainer addition, 2026-08-19): a page
  that carries caller-supplied `embeddings` never reaches Voyage, so gating it by a
  Voyage-token-derived byte budget would shrink pages for no upstream benefit. The budget applies
  only when the page will cause a server-side embed. Alignment of the passthrough slices under
  variable stride (the bullet above) is unaffected.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Sub-batch planner | `Bge768Embedder.planBatches`/`MAX_PADDED_TOKEN_AREA` (`:119-124`, `:247-300`) | **Reuse the pattern**, not the code: the budget unit is model-specific (padded token area vs estimated API tokens) but the greedy-planner shape, documented-provenance constant, single-call fast path, and invocation-counter instrument transfer directly. |
| Non-vacuity instrument | `Bge768Embedder.onnxInvocationCount()` (`:141-147`, `:362-370`) | **Reuse the pattern.** Required by the vacuous-gate doctrine — a sub-batching test that cannot prove a split happened is vacuous. |
| Oversize-input refusal | `VoyageReranker.MAX_DOCS_PER_REQUEST` (`:58-59`, `:130-135`) | **Reuse the convention**: assert the API cap loudly, "refusing to silently truncate". |
| Typed upstream error | `UpstreamAuthException` (`VoyageEmbedder.java:230-237`) | **Extend**: same typed-exception convention for a second recognized upstream condition. |
| Test HTTP seam | `VoyageReranker` 5-arg constructor (`:90-100`); `CceEmbedderParallelTest:90-198` | **Reuse both**: the constructor shape and the fake-`HttpServer` test harness pattern. |
| Composition-stability gate | `Bge768BatchCompositionTest` | **Reuse the pattern** as the argument that re-composing batches is embedding-stable. |
| Usage-token summing | `CceEmbedder.embedWithUsage` (`:223-233`) | **Reuse**: identical multi-call accumulation requirement. |
| Client byte budget | `ChunkBatcher(max_bytes=...)` (`chunk_batcher.py:107`, `:273`, `:300-313`) | **Reject** — see Alternatives. Fully implemented but wrong layer. |
| Client paging | `upsert_chunks` loop (`:1563-1567`) | **Extend**: the self-documented single choke point. |

### Decision Rationale

**Why the engine is the authoritative layer.** It sits below every caller. Fixing only the client
leaves the `/v1/vectors/embed` parity endpoint, `StagingHandler`, and the combined-write path each
able to reproduce the failure, and leaves the engine's two embedders inconsistent — one plans its
batches, one does not.

**Why an estimator is acceptable despite being inexact.** This is the design's load-bearing
argument, and it is precisely where this RDR diverges from `nexus-zu4ma`. That bead needed a
*measured*, empirically-safe constant because its failure mode is an OOM: there is no recoverable
signal, so the budget itself has to be right. Voyage instead returns a precise, typed, per-request
400 naming the exact overage. That converts the problem from "predict correctly" to "approach
sensibly and adapt on refusal". The estimator therefore governs cost, not correctness — and the
honest consequence, recorded here rather than hidden, is that a badly-tuned divisor is a *performance*
defect that tests must measure, not a correctness defect they must prevent.

**Why the client change is not redundant.** Because the two artifacts ship on separate lifecycles
and nothing in the release process forbids a new client from meeting an old engine. Framing it as
belt-and-braces would be dishonest and would invite a reviewer to delete it; framing it as skew
tolerance states the actual requirement.

**Why not hoist to `EmbedderRouter`.** It serves both `VoyageEmbedder` and `CceEmbedder`. CCE
already fans out per text, so a router-level planner would double-batch it, and the token budget
would have to be re-derived per embedder — pushing model-specific knowledge up out of the class that
owns it.

## Alternatives Considered

### Alternative 1: Wire the existing `ChunkBatcher(max_bytes=...)`

**Description**: `ChunkBatcher` already implements a byte budget (constructor param
`chunk_batcher.py:107`, stored `:146`, and three live checks — `:273` refuse-oversize-file,
`:300-301` would-overflow pre-flush, `:313` flush trigger); `indexer.py` constructs the batcher
without it, so it defaults to `None` and every byte check short-circuits.

**Pros**:

- Zero new mechanism; the code is written and tested.
- Flushes earlier, reducing the per-request payload.

**Cons**:

- Does not cover the failing case. When a *single* file exceeds the budget, `add()` returns `False`
  (`:255-260`) and the caller falls back to the legacy per-file path — which routes through
  `upsert_chunks` and its count-only paging anyway.
- Adds a second tuning knob that only partially overlaps the real bound.

**Reason for rejection**: it constrains batch *assembly* while the failure occurs in batch
*transmission*. Fixing `upsert_chunks` paging covers both the batcher flush path and the legacy
per-file path at one choke point. Wiring `max_bytes` remains available as an independent throughput
tuning decision, unrelated to this correctness fix.

### Alternative 2: Engine-only fix

**Description**: token-aware split in `VoyageEmbedder` alone; leave the client's count-only paging.

**Pros**:

- Single responsibility, single tuning knob, smallest diff.
- Correct for every caller, including ones the client cannot see.

**Cons**:

- A client running against a pre-fix engine still fails, and the decoupled release lifecycles make
  that pairing a normal state rather than an edge case.
- Leaves 3.6 MB POSTs on the wire, which is a latent gateway-timeout risk independent of tokens.

**Reason for rejection**: correct but not sufficient under version skew.

### Alternative 3: Client-only fix

**Description**: byte budget on `upsert_chunks` paging; no engine change.

**Pros**:

- Ships in a normal PyPI release — no engine tag, no deploy, no `REQUIRED_ENGINE_VERSION` bump.
- Reaches every user quickly.

**Cons**:

- Every other engine embed caller stays broken.
- Bytes are a proxy for tokens; without the engine's typed-400 backstop, a bad ratio is once again a
  correctness defect.

**Reason for rejection**: leaves the authoritative layer unfixed. Retained as the *first*
implementation phase precisely because of its independent delivery path.

### Briefly Rejected

- **A real Voyage tokenizer in the JVM**: no offline JVM tokenizer exists for Voyage's BPE; the
  bundled DJL tokenizers are BERT-vocab for the local ONNX models. Rejected as unavailable, not as
  undesirable.
- **Lower `_CODE_UPSERT_CHUNK_CAP` from 300**: still count-based, so still incorrect for arbitrary
  chunk sizes, and it re-tunes a constant whose value encodes gateway-timeout findings, conflating
  two unrelated bounds.
- **Make 400 retryable in `callApi`**: retrying an identical oversize body is guaranteed to fail
  again and bills tokenization twice. The batch must change, not merely be resent.

## Trade-offs

### Consequences

- Large-file code indexing succeeds where it currently fails outright. (Positive)
- The engine's two embedders converge on one batch-planning idiom instead of one planning and one
  not. (Positive)
- An actionable upstream condition becomes a typed error with a recovery path rather than an opaque
  500. (Positive)
- A large batch becomes several HTTP round trips, so wall-clock for an oversize batch rises even on
  the success path. (Negative, and inherent — the alternative is failure.)
- Two tuning constants now exist across two languages, both approximating one remote limit. Drift
  between them is a real maintenance cost, mitigated by cross-referencing each to this RDR. (Negative)
- The engine half requires an `engine-service` tag, a deploy, and a `REQUIRED_ENGINE_VERSION` bump
  before it reaches local-mode users. (Negative — delivery cost, unavoidable for a Java change.)

### Risks and Mitigations

- **Risk**: the bytes-to-tokens estimate is too optimistic, so batches still 400.
  **Mitigation**: the typed error plus adaptive halving makes this self-correcting by construction —
  a miss costs round trips, not correctness. A test asserts recovery from an injected 400.
- **Risk**: the estimate is too pessimistic, quietly shrinking batches and inflating request counts
  and latency for everyone.
  **Mitigation**: a test asserts the small-input case issues **exactly one** request, so a regression
  that over-splits the common path fails. The implementation spike measures the real ratio.
- **Risk**: a reviewer reasonably suspects the split perturbs the `nexus-f4wcg` byte contract or the
  embed-parity gate.
  **Mitigation**: `buildJson` is untouched; the parity corpora are demonstrably below any budget and
  take the single-request path. Both are stated as verified assumptions above with source references.
- **Risk**: the sub-batching test passes without ever splitting, i.e. a vacuous gate.
  **Mitigation**: the package-private request counter makes the split directly observable, and the
  test asserts on it. This follows the vacuous-gate doctrine and `nexus-zu4ma`'s own RED/GREEN
  demonstration.
- **Risk**: client and engine budgets drift apart over time.
  **Mitigation**: both constants cite this RDR and the documented per-model ceiling; the engine's
  typed-400 path remains the backstop regardless of what the client believes.

### Failure Modes

- **Visible**: an un-splittable single text over the ceiling rethrows with the upstream detail intact
  — the operator sees the model, the limit, and the actual token count.
- **Visible**: an input list above `MAX_BATCH_TEXTS` is refused with a message naming the cap, never
  silently truncated (`VoyageReranker` convention).
- **Silently degrading (accepted, and instrumented)**: a pessimistic estimate over-splits. Nothing
  fails; throughput drops. This is why the request counter is a production-visible instrument and why
  the one-request-for-small-input test exists.
- **Recovery**: an oversize sub-batch halves and retries automatically. Partial-page failures on the
  client remain idempotent-retry-safe via `ON CONFLICT` dedup plus full-file staleness retry, exactly
  as the existing paging comment documents (`:1553-1562`).
- **Diagnosis**: structured log events for the planned batch count and for each adaptive split, in
  the existing `event=voyage_*` idiom, so a post-hoc log read distinguishes "planned correctly" from
  "estimator missed and recovered".

## Implementation Plan

### Prerequisites

- [x] **Composition spike (blocking).** Embed one fixed text alone and inside a large batch against
  live Voyage; diff the vectors. This verifies Critical Assumption 1, the only assumption here whose
  failure is silent. Implementation must not begin until it passes — gate round 1 reclassified this
  from a false "Verified (Docs Only)" and it is the reason that gate round returned BLOCKED.
  **PASSED 2026-08-19** (nexus-kmtlp.1; result and the measured repeat-variance band in Critical
  Assumption 1 and T2 `nexus_rdr/195-research-3`).
- [x] All *other* load-bearing Critical Assumptions verified by Source Search; the one remaining
  unverified item (the bytes-per-token ratio) is explicitly non-load-bearing — see Decision
  Rationale.
- [ ] `VoyageEmbedder` test-seam constructor added — no fake-server test of the class is possible
  without it.
- [x] A JDK provisioned in the implementation environment. Verified absent at authoring time
  (`/usr/libexec/java_home` finds no runtime), which blocks `./mvnw test`, the engine build, AND —
  via the gate-jar freshness check — the entire Python unit suite. Local mode runs a *downloaded
  signed native binary* (RDR-161), so a user can run the engine without a JDK but cannot build one;
  this prerequisite is therefore easy to overlook until the suite errors at setup. (Maintainer box:
  GraalVM 25.0.3 present, `./mvnw -q test-compile` green — nexus-kmtlp.2, 2026-08-19.)
- [x] `scripts/build-gate-jar.sh` run after engine edits (the substrate freshness gate rejects a
  stale jar and the whole Python suite errors at setup). Requires the JDK above. (Rebuilt
  2026-08-19, suite reaches collection — nexus-kmtlp.2; re-run after every Phase 2 engine edit.)

### Minimum Viable Validation

Re-index the large PHP repository that produced the original failure, end to end, against a locally
built engine, with **zero** `TOO_MANY_TOKENS_IN_BATCH` occurrences, and confirm semantic search
returns hits from the newly indexed files. In scope, not deferred: this is the exact journey that
fails today.

### Phase 1: Client byte budget

Independent delivery path (normal PyPI release) and the version-skew half.

#### Step 1: Byte-aware paging

Convert the `upsert_chunks` paging loop (`:1563-1567`) to an accumulator bounded by count and bytes,
gated to non-CCE / non-onnx-local collections via the existing helpers. Pages carrying
caller-supplied `embeddings` are exempt from the byte bound (they never reach Voyage — see
Technical Design); count-cap paging applies to them unchanged. Preserve `embeddings`
lockstep slicing and per-page logging. The byte budget carries deliberate extra headroom below the
engine's per-model token budget — see Technical Design; equal sizing is the one tuning choice this
phase must not make.

#### Step 2: Tests

Page boundaries fall where bytes dictate; a single oversize chunk still ships; CCE and onnx-local
paging is byte-for-byte unchanged; `embeddings` passthrough stays aligned with ids; a
passthrough-page batch larger than the byte budget pages by count alone (the exemption is
observable, not decorative).

### Phase 2: Engine planner and typed error

#### Step 1: Test seam

Add the injectable constructor mirroring `VoyageReranker.java:90-100`.

#### Step 2: Planner

Constants, `planBatches`, `estimateTokens`, the request counter, and the `MAX_BATCH_TEXTS` assertion.
Wire `embed` / `embedWithUsage` / `embedDouble`, summing usage tokens. Preserve the single-request
fast path. Do not touch `buildJson`.

#### Step 3: Typed oversize error and adaptive halving

Recognize `400` + `TOO_MANY_TOKENS_IN_BATCH` in `callApi`; halve and retry in the caller; rethrow with
detail when a single text cannot be split.

#### Step 4: Tests

`VoyageEmbedderBatchSplitTest` per the Test Plan.

### Phase 3: Operational activation

#### Activation Step 1: Engine release and floor bump

Cut an `engine-service-vX.Y.Z` tag via the `engine-release` skill, then bump
`REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — which moves `PINNED_SERVICE_TAG` by
derivation — in the paired client release, per AGENTS.md § Engine-service release. Local-mode users
receive the engine half only through that constant.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| (none — no persistent resource created) | N/A | N/A | N/A | N/A | N/A |

This RDR adds no collection, index, data store, or config entry. Its only durable artifacts are two
source constants, both covered by ordinary code review.

**One operational signal deserves more than "no persistent resource, therefore N/A".** The
adaptive-split rate is a cost-and-throughput signal, not merely a debug line: a steady-state
non-zero rate means the divisor is optimistic and every affected batch is paying for rejected
tokenization. Emitting it as a structured event is the in-scope minimum (it rides the existing
`event=voyage_*` idiom and the request counter). Surfacing it as a threshold, rather than something
an operator must go read a log to discover, is a reasonable follow-up — called out here so it is a
deliberate deferral rather than an omission.

### New Dependencies

None. No third-party addition in either language; the fix uses only what both codebases already have.

## Test Plan

New `service/src/test/java/dev/nexus/service/vectors/VoyageEmbedderBatchSplitTest.java`, using a fake
`com.sun.net.httpserver.HttpServer` on `127.0.0.1:0` per `CceEmbedderParallelTest.java:90-198`, with
`Optional.empty()` for the proxy so an ambient `HTTPS_PROXY` can never route the localhost upstream:

- **Scenario**: an over-budget input list — **Verify**: more than one POST, each under budget, proven
  via the package-private request counter (the non-vacuity assertion).
- **Scenario**: sub-batch responses returned out of order — **Verify**: vectors map back to their
  exact input positions.
- **Scenario**: multi-sub-batch `embedWithUsage` — **Verify**: `usage.total_tokens` equals the sum
  across sub-calls, not the last response's value.
- **Scenario**: upstream returns `400` `TOO_MANY_TOKENS_IN_BATCH` on the first attempt — **Verify**:
  the batch halves, retries, and succeeds; the counter shows the extra attempts.
- **Scenario**: a single text alone still trips the 400 — **Verify**: a typed error propagates with
  the upstream detail, and nothing is silently dropped.
- **Scenario**: an input list that already fits — **Verify**: **exactly one** POST, body byte-identical
  to the pre-change body (the regression guard on the common path and on the parity gate).
- **Scenario**: an input list above `MAX_BATCH_TEXTS` — **Verify**: loud refusal naming the cap, no
  truncation.
- **Scenario**: the same oversize input against an embedder built for `voyage-code-3` versus one
  built for a higher-tier model — **Verify**: the budget is resolved per model, so the higher-tier
  embedder issues strictly fewer requests. This is the regression guard on the per-model table.
- **Scenario**: an embedder constructed with a model absent from the table (including `voyage-3`,
  which Voyage's current published limits do not cover) — **Verify**: it falls back to the tightest
  documented ceiling rather than an optimistic one, i.e. it over-splits rather than failing.

Python, extending the existing `upsert_chunks` paging tests:

- **Scenario**: chunks whose cumulative bytes exceed the budget before the count cap — **Verify**:
  pages break on bytes.
- **Scenario**: one chunk larger than the budget — **Verify**: it still ships in a page of its own.
- **Scenario**: a `docs__`/`knowledge__`/`rdr__` collection, and onnx-local mode — **Verify**: paging
  unchanged from today.
- **Scenario**: `embeddings` passthrough under variable stride — **Verify**: vectors stay aligned
  with their ids.

## Validation

### Testing Strategy

1. **Scenario**: `./mvnw test` for the engine suite.
   **Expected**: green, including `VoyageEmbedderBodyTest` **unmodified** — direct evidence the byte
   contract was preserved rather than accommodated.
2. **Scenario**: `uv run pytest -n auto`.
   **Expected**: green; `tests/db/test_embed_parity.py` in particular unchanged and passing.
3. **Scenario**: `scripts/build-gate-jar.sh` then the engine-substrate tests.
   **Expected**: green against the freshly built jar.
4. **Scenario**: the Minimum Viable Validation above.
   **Expected**: the PHP repository indexes with zero `TOO_MANY_TOKENS_IN_BATCH`; search returns its
   files.

"Done" means: every scenario in the Test Plan implemented, the non-vacuity assertion demonstrated
RED before GREEN (a deliberately disabled planner must fail the invocation-count assertion, following
`nexus-zu4ma`'s recorded RED/GREEN evidence), and the MVV executed.

### Performance Expectations

Measurement strategy, not estimates. During implementation, record from the MVV run: the number of
Voyage requests per upsert page, the count of adaptive-split events, the observed
bytes-per-token ratio for PHP source, **and the 429 count** (gate remediation, 2026-08-19: the
request-rate paragraph in Technical Design concedes sub-batching makes rate-limit pressure
"strictly worse" — conexus-ddh0 is the live precedent — so the run that calibrates the divisor
must also count 429s; `callApi`'s retry path already logs them, the MVV records the total). A
non-zero 429 count on the MVV's single-repository run triggers the caller-pacing remedy named in
Technical Design before Phase 3 activation, not after a field incident. The ratio calibrates the
divisor; a non-zero steady-state adaptive-split count indicates the divisor is too optimistic, and
a request count materially above the theoretical minimum indicates it is too pessimistic. No
throughput target is asserted here.

## Finalization Gate

### Contradiction Check

No contradictions remain between research findings, design principles, and proposed solution. Two
are worth recording rather than quietly resolving.

**Resolved in gate round 1 (was a real internal contradiction).** Draft 1 marked Critical Assumption
1 as Verified via "Docs Only" while this document's own Method definitions declare Docs Only
"insufficient for load-bearing assumptions" — a document contradicting its own stated standard, on
its only silent failure mode. Now recorded as Unverified with a blocking spike. See Revision History.

**Standing, and deliberate.** One tension is worth naming explicitly rather than leaving implicit. `per_collection_chunk_cap`'s
docstring states that a token budget is "a throughput optimization for a future pass, not a
correctness gap." This RDR contradicts that sentence *for the Voyage path* and agrees with it for the
path it was written about. The docstring's reasoning is sound where the binding constraint is local
ONNX memory with a 512-token truncation bound; it does not transfer to a hard remote per-request
ceiling. Implementation should amend that docstring so the distinction is recorded at the code, not
only here.

### Assumption Verification

**The one load-bearing assumption that gated implementation is now verified by live spike
(2026-08-19).** Gate round 1 (see Revision History) found that assumption 1 — batch partitioning
does not change any individual vector — was marked Verified on the strength of "Docs Only,
corroborated by in-repo precedent", where the precedent was `Bge768BatchCompositionTest`, a
*different embedder*. That was an overclaim, and it landed on the one assumption in this document
whose failure is **silent**: wrong vectors stored, no error raised. It was recorded as **Unverified
— Spike required before implementation** and placed as the first Prerequisite; the spike then ran
(nexus-kmtlp.1) and passed: every cross-composition deviation (≥ 0.99994 cosine) is smaller than
the variance byte-identical repeated requests exhibit on their own (down to 0.99978 for a
near-ceiling batch). Detail in Critical Assumption 1.

Of the remaining four: three are Verified by Source Search (byte contract preserved because
`buildJson` is untouched; the parity corpora never split; `TOO_MANY_TOKENS_IN_BATCH` is the
discriminator). The fourth — the bytes-per-token ratio — is intentionally unverified and explicitly
**non-load-bearing**, per the argument consolidated in Decision Rationale: the adaptive split means
an incorrect ratio degrades throughput and cannot cause failure. It is measured during
implementation per Performance Expectations.

So, after the spike: four Verified, one Unverified-and-not-load-bearing (the bytes-per-token
ratio, measured during implementation, non-blocking).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/embeddings` — 1,000-input and 120,000-token per-request limits for `voyage-code-3` | Voyage AI | Docs Only (official API reference) |
| `POST /v1/embeddings` — per-text truncation applied before batch-total evaluation **(load-bearing: this is what makes the halving terminate)** | Voyage AI | Observed in a live upstream response — the captured 400 body reads "Your batch has 280871 tokens **after truncation**", which is direct empirical evidence of the ordering, not a docs reading |
| `400` `error_code: "TOO_MANY_TOKENS_IN_BATCH"` | Voyage AI | Source Search (captured upstream response) |
| Voyage token counting available only via the Python SDK | Voyage AI | Docs Only |
| `POST /v1/contextualizedembeddings` limits (recorded, not exercised) | Voyage AI | Docs Only |

**Which of these are load-bearing, stated precisely** — the round-1 lesson was that a blanket
"these are all fine" is how a load-bearing item hides in a list:

- The three remaining **Docs Only** entries (the ceiling values, the SDK-only token counting, and
  the contextualized-endpoint limits) are **not** load-bearing. The design never depends on a
  ceiling being exactly 120,000: it approaches a budget *below* whatever the ceiling is and adapts
  on refusal, so a documentation error changes only how many round trips the common path takes. The
  contextualized row is recorded for future work and is not exercised at all.
- The **truncation-ordering** entry **is** load-bearing, and is deliberately not Docs Only. It is
  the premise of the termination argument: if Voyage evaluated the batch aggregate *before*
  truncating each text, a single sufficiently large text could exceed the ceiling on its own and the
  halving would have no terminating case. The live 400 body settles the ordering empirically, which
  is stronger than the published docs and is why it is classified as observed rather than read.

### Scope Verification

The Minimum Viable Validation — re-indexing the PHP repository that produced the original failure
with zero `TOO_MANY_TOKENS_IN_BATCH` and confirming search hits — is in scope and executed during
implementation, not deferred. It needs no cloud deploy to prove — the reporting environment is
`mode: local`, so a locally built engine suffices. It does, however, require provisioning a JDK
first: that environment has none today, which blocks both the engine build and the Python suite.
That is a setup prerequisite, not a scope reduction; the MVV itself remains in scope and unconditional.

### Cross-Cutting Concerns

- **Versioning**: the engine half requires an `engine-service-vX.Y.Z` tag plus a
  `REQUIRED_ENGINE_VERSION` bump (which moves `PINNED_SERVICE_TAG` by derivation) under the
  paired-release choreography. The client half is version-independent and is what makes the interim
  skew safe.
- **Build tool compatibility**: Maven for the engine, `uv` for the client. No build change.
- **Licensing**: N/A — no new dependency.
- **Deployment model**: unchanged. Local mode gets the engine half via the pinned tag; cloud mode via
  the deploy. The client half rides a normal PyPI release.
- **IDE compatibility**: N/A.
- **Incremental adoption**: inherent — the two halves are independently correct and independently
  shippable, in either order.
- **Secret/credential lifecycle**: N/A — no new credential. The existing Voyage key handling is
  untouched.
- **Memory management**: improved incidentally. Smaller request bodies mean a smaller peak JSON
  string per POST than today's up-to-3.6 MB body. No streaming strategy needed; each sub-batch is
  built, sent, and released as today.

### Proportionality

Acceptable, but this section was self-graded generously in draft 1 and gate round 1 said so. The
change is small — two functions, a handful of constants, one new exception type — while the document
spans two languages, two release lifecycles, and a class carrying both an explicit byte contract and
a cross-language parity gate. That context is what the length buys.

Sections that earn their length: Decision Rationale (the estimator-vs-correctness argument),
Alternatives (three real options, each rejected for a distinct reason), the Existing Infrastructure
Audit (what keeps this from reinventing `nexus-zu4ma`), and Termination and cost bound (which closes
the design's most obvious hole).

Trimmed in response to gate round 1: the estimator-vs-correctness argument was being re-derived in
four places; Approach and Assumption Verification now point at Decision Rationale instead of
restating it. Risks and Mitigations still expresses it, but in risk form with distinct mitigations,
which is its own content rather than a repeat.

Day 2 Operations is deliberately near-empty — no persistent resource is created — with one genuine
operational signal (adaptive-split rate) called out rather than N/A'd away.

## References

- Voyage AI, Embeddings API reference — per-model total-token and input-count limits.
- Voyage AI, Contextualized Chunk Embeddings — 120,000 with auto-chunking / 32,000 without; 16,000
  chunk cap. Recorded for a future CCE batching change.
- Voyage AI, Rate Limits and Tokenization — batch-size guidance and `count_tokens`.
- `service/src/main/java/dev/nexus/service/vectors/VoyageEmbedder.java` — the unbatched path.
- `service/src/main/java/dev/nexus/service/vectors/Bge768Embedder.java` — `nexus-zu4ma` sub-batch
  planner, budget provenance, invocation-count instrument.
- `service/src/test/java/dev/nexus/service/vectors/Bge768BatchCompositionTest.java` —
  composition-stability and non-vacuity precedent.
- `service/src/main/java/dev/nexus/service/vectors/CceEmbedder.java` — per-text fan-out; usage-token
  summing.
- `service/src/main/java/dev/nexus/service/vectors/VoyageReranker.java` — API-cap assertion and test
  seam conventions.
- `src/nexus/db/http_vector_client.py` — `per_collection_chunk_cap`, the paging choke point.
- `src/nexus/chunk_batcher.py` — the two-file bisect limitation and the unwired `max_bytes`.
- RDR-181 — server-side embed-skip; establishes that the embed call runs outside any transaction.
- `nexus-f4wcg` — the Voyage request-body byte contract.
- AGENTS.md § Engine-service release — the paired-release choreography.

## Revision History

Gate findings are appended here to keep the design sections clean. Each gate round gets a dated
subsection.

### Prerequisite spike — 2026-08-19 — PASSED (nexus-kmtlp.1 / .2)

Critical Assumption 1 flipped Unverified → Verified (Live Spike); Prerequisites 1, 4, 5 ticked.
Three rounds against live `voyage-code-3`, engine-exact body shape. Round 1: five probes alone
vs inside 125-input and 29-input mixed batches, worst cosine 0.99994. Round 2: byte-identical
batched requests repeated give 0.99996–0.99997, identical single short inputs repeat bit-exact,
several cross-composition pairs exactly 1.0. Round 3 (after the substantive-critic flagged the
causal wording as under-evidenced and the near-ceiling regime as untested, T2
`nexus_rdr/195-critique-kmtlp1-composition-spike-2026-08-19`): 20 identical repeats give 1–2
discrete vectors per text; a 263-input/101K-token batch repeated gives 168/263 bit-identical,
min cosine 0.99978; probes inside it vs alone 0.99994–1.0 (2.2 KB probe bit-identical to an
alone variant). Conclusion stated as measured: cross-composition deviation never exceeds the
same request's repeat variance; no dependence on neighbour content. Side finding: the
`VoyageEmbedder.java` rationale "omitting `truncation` gives 0.99995 drift" is within that
band (only that field toggled, 3x each) — and `truncation` is the API default, so omission is
semantically identical and the 3000-char probe is below the per-text context where the flag
could act; Javadoc corrected in place (field kept for SDK byte parity). Tolerance guidance for
any live-Voyage batched equality assertion: ≥ 2e-4 cosine. Record: T2
`nexus_rdr/195-research-3`.

### In-house gate — 2026-08-19 — PASSED (0 critical, 3 significant remediated in place)

First gate run with T2-verifiable results (the contributed rounds below were self-attested from a
fork). Layers 1–2 passed (structure complete; the one unverified assumption carries an explicit
risk assessment and a blocking spike). Layer 3 adversarial critique: 0 critical, 3 significant,
3 observations (full critique: T2 `nexus_rdr/195-gate-critique-2026-08-19`). All three
significants remediated in this document rather than filed: (1) the passthrough byte-budget
exemption propagated into Phase 1 Step 1 and its tests (the same section-drift class gate round 2
fixed for the headroom constraint); (2) Performance Expectations now records the 429 count during
the MVV, with a pre-Phase-3 pacing trigger (the design concedes rate pressure gets strictly worse;
conexus-ddh0 is the precedent); (3) the sub-request cap is now specified — constant, value with
derivation, exhaustion contract on the typed-error path, and an exhaustion test.

### Maintainer adoption — 2026-08-19

Adopted in-repo from PR #1466 (author retains authorship; contributed gate rounds below preserved
as the contributor's record). Amendments on adoption, per the review posted on the PR: status
flipped to `draft` pending an in-house gate run (the contributed gate attestation is
self-reported from a fork that cannot write the project store — a process constraint, not a
content criticism); reproduction floor re-verified against the moved `(0, 1, 82)` floor (v7.11.0;
neither the v0.1.81 nor v0.1.82 cut touched `VoyageEmbedder`); request-rate (429) interaction
paragraph added (conexus-ddh0 incident class); staged bulk-load (nexus-b50zw) reconciliation
added; `embeddings`-passthrough pages exempted from the client byte budget.

### Gate round 1 — 2026-08-19 — BLOCKED, then remediated

Layers 1 (structural) and 2 (assumption audit) passed. Layer 3 (adversarial critique) returned
1 CRITICAL, 2 SIGNIFICANT, 3 OBSERVATION. All six are addressed below; the CRITICAL is what made
this round BLOCKED rather than PASSED.

Two defects were also found by the author's own re-verification *before* Layer 3 ran, and are
recorded here because both changed the design rather than merely the prose:

- **Author, pre-critique — no termination proof.** The adaptive halving had no argument that it
  terminates. Added *Termination and cost bound*, which establishes that halving terminates in
  success at or before size 1 for every documented model, that the split must live above `callApi`
  so transient-retry budgets cannot compound to `3^depth`, and that billing inherits the existing
  documented under-count stance.
- **Author, pre-critique — the token budget could not be one constant.** `VoyageEmbedder` is
  instantiated with **two** models, `voyage-code-3` (`EmbedderRouter.java:104`, `:142`) and
  `voyage-3` (`:112`, `:145`), whose tiers carry different ceilings. A single constant would be
  wrong for one and punitive for the other. Now resolved per model with a fail-safe default of the
  tightest documented ceiling, so an unknown model over-splits rather than fails. `voyage-3` is
  absent from Voyage's current published limit table entirely, which is the concrete reason the
  default must be conservative rather than guessed.

**CRITICAL 1 — Critical Assumption 1 was verified by a method this document calls insufficient, on
its only silent failure mode.** Draft 1 marked "partitioning a batch does not change any individual
vector" as Verified via "Docs Only, corroborated by in-repo precedent" — where the precedent,
`Bge768BatchCompositionTest`, exercises a *different embedder* (local ONNX), making it an analogy
rather than corroboration of Voyage's remote behavior. Every other failure mode in this RDR is loud;
this one is silent. **Accepted and fixed**: reclassified to *Unverified — Spike required before
implementation*, promoted to the first Prerequisite as blocking, and the supporting evidence
restructured into three explicitly-labelled tiers, none of which is presented as a substitute for
the spike. The strongest of them replaced the weak analogy: `CceEmbedder.java:202` states that
batching into one *contextualized* call changes embeddings via cross-document context propagation,
which makes the standard endpoint the deliberate contrasting case. Also recorded that the status quo
already depends on this property (300 texts per request today; `ChunkBatcher` bisect already
re-composes batches in production), so this RDR increases the frequency of re-composition rather
than introducing the dependency.

**SIGNIFICANT 2 — the version-skew justification overclaimed what the client half delivers.** A
new-client/old-engine user gets a byte proxy with no typed-400 backstop — exactly the profile
Alternative 3 is rejected for — and draft 1 never said so. **Accepted and fixed**: the residual risk
is now stated for that population, with the consequence turned into a design constraint — the client
budget must carry deliberate extra headroom *below* the engine's, because the client is the layer
without a safety net. Sizing them equally would put no headroom where there is no backstop.

**SIGNIFICANT 3 — the termination proof over-generalized.** "The same inequality holds for every
model in the fleet" is not supported: Voyage publishes a generic per-text context and per-tier
ceilings, not a per-model context table, and `voyage-3` is absent from the tiers. **Accepted and
fixed**: the claim is now scoped to documented models and labelled *expected, not proven* for
undocumented ones, with the conservative default and defensive rethrow named as what keeps it safe
either way.

**OBSERVATION 4 — worst-case cost analysis incomplete.** Partially accepted, with a correction. The
critique suggested one pathological item could cascade toward `O(n)` sub-requests; the analysis does
not support that — with a single over-budget item only the half containing it keeps failing, so each
level costs one failure plus one success, about `2 * log2(n)`, still logarithmic. Two things were
nonetheless added: on the indexing path a single over-budget chunk cannot arise at all (chunkers cap
at `SAFE_CHUNK_BYTES`, well below any per-model budget), leaving the unbounded `/v1/vectors/embed`
endpoint as the only caller that could present one; and a cap on total sub-requests per original
batch as cheap insurance, so no input can turn one logical embed into unbounded upstream spend.

**OBSERVATION 5 — no production monitoring beyond logs.** Accepted. Day 2 Operations no longer
N/A's monitoring wholesale: the adaptive-split rate is identified as a cost-and-throughput signal,
structured emission is the in-scope minimum, and threshold alerting is named as a deliberate
deferral rather than left as an omission.

**OBSERVATION 6 — proportionality self-graded generously.** Accepted. The estimator-vs-correctness
argument was being re-derived in four sections; Approach and Assumption Verification now point at
Decision Rationale rather than restating it, and the Proportionality section itself was rewritten to
state what the length buys and what was trimmed, instead of simply asserting "right-sized".

### Gate round 2 — 2026-08-19 — PASSED

Re-gate of the revised document verified all six round-1 remediations as real rather than cosmetic,
confirmed the citations (`CceEmbedder.java:202` and `EmbedderRouter.java:104`/`:112`/`:142`/`:145`
checked exactly), found no orphaned "Verified" label surviving anywhere, and confirmed Prerequisites,
Critical Assumptions, and Assumption Verification independently converge on the same tally: one
blocking unverified, one non-blocking unverified, three verified. 0 CRITICAL, 0 OBSERVATION.

One SIGNIFICANT (minor, non-blocking) and its fix: the client byte-budget **headroom constraint**
appeared only in Approach, so a reader consulting Technical Design or Phase 1 alone would miss it.
Propagated to both, with a note that the constant's own docstring should carry the reason — the
constant does not reveal on its own why it must be more conservative than the engine's.

### Round-1 citation corrections (accuracy of citations, since the document is the deliverable):
`chunk_batcher.py` line references were carried over from `main` and rebased onto `develop`
(`:107`/`:146`/`:273`/`:300-301`/`:313`); `CombinedWriteService.java` corrected to `:190-197`; the
reproducing engine identity was pinned (`engine-service-v0.1.80`, which is also this branch's
`REQUIRED_ENGINE_VERSION` floor, so the defect is live on the currently-pinned engine rather than a
stale local build).
