---
title: "Qwen-MCP Figure Augmentation Hook — Single-Path VL via MCP-Client Integration"
id: RDR-150
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-21
accepted_date:
supersedes: docs/proposals/m3docrag-application.md
related_issues: []
related_rdrs: [RDR-110, RDR-112, RDR-113]
related_external: []
reactivates_beads: [nexus-6h0e]
related_tests: []
---

# RDR-150: Qwen-MCP Figure Augmentation Hook

> Supersedes the multi-path proposal in
> `docs/proposals/m3docrag-application.md` (spike branch
> `feature/nexus-m3docrag-proposal-and-colbert-spike`). The proposal's
> P0 spike PASSED 2026-05-17 with cross-paper MRR@10 0.56 → 0.87
> (+0.31). The qwen coprocessor is operational; this RDR is the
> single-path integration that ships the win.

## Problem Statement

Visually rich PDFs (architecture diagrams, charts, complex tables) lose
information when indexed text-only. The original M3DocRAG proposal
established §5.1's text-vs-multimodal gap; the local spike confirmed it
on our own corpus and showed that *turning the visual signal into text
at ingest* (VL-augmentation) closes the gap without changing retrieval.

The qwen coprocessor at `qwentescence` (Qwen 3.6-35B-A3B-VL via
Apache-2.0 unsloth GGUF + BF16 vision projector, ~1k–5k token prefill
per image, ~30–50 tok/s output) is operational and addressable via the
**qwen MCP supervisor** that already runs on the host. The clean
integration is *MCP-to-MCP*: nexus's daemon acts as an MCP client of
the qwen supervisor and calls its VL tools at ingest time.

The constraint that simplifies the design: **qwen-MCP-or-nothing**. If
the qwen supervisor or its VL backend is unavailable at ingest time,
the augmentation hook is a no-op and ingest continues as today. No
fallback model, no background queue, no retry semantics.

### Enumerated gaps to close

#### Gap 1: Nexus has no MCP-client capability

Today nexus is an MCP *server* (exposed to Claude Code and other
clients). It does not act as a client of any other MCP server. Adding
the qwen MCP integration is the first instance of nexus consuming
another MCP server's tools.

#### Gap 2: No figure-augmentation hook on the ingest path

Docling and MinerU produce figure/table bounding boxes; nothing
consumes them. Chunks lose the visual signal between extraction and
embedding.

#### Gap 3: Availability semantics need to be explicit

"qwen-MCP-or-nothing" is the constraint, but the exact discipline —
pre-flight check vs try-and-skip-on-timeout — needs to be settled
before implementation.

#### Gap 4: Idempotency across re-ingest

VL prefill cost is significant (5–15 s per image). Re-running ingest
on the same document must not re-spend prefill. Figure-chash-keyed
caching is the discipline.

## Context

### Background

The M3DocRAG proposal (`docs/proposals/m3docrag-application.md` on the
spike branch) explored multiple paths for handling visually rich PDFs:

| Path | Status |
|---|---|
| Full M3DocRAG pipeline (Qwen2-VL in answer path) | REJECTED — nexus has no generation path |
| ColPali retrieval on Apple Silicon | REJECTED 2026-05-17 — retrieval ranking too flat |
| Text-only ColBERT reranker | Different problem (vocabulary mismatch on text); independently revivable |
| SigLIP figure-only retrieval | PASSED 2026-05-17 (bead `nexus-8siy`) — kept as historical option |
| **VL-augmentation via qwen** | **PASSED 2026-05-17** — bead `nexus-6h0e` |

With the qwen coprocessor now operational and the user constraint
"qwen-MCP-or-nothing," the multi-path branching collapses. SigLIP
fallback drops (no fallback). ColBERT drops from this RDR's scope (it
addresses a different problem). ColPali stays rejected.

This RDR ships the P0 path as a single-purpose ingest hook.

### Spike results (carry-forward)

From `docs/proposals/m3docrag-application.md` §5c, first run 2026-05-17,
cross-paper (M3DocRAG + Beyond Similarity Search):

| Pipeline | recall@10 | MRR@10 | mean target rank |
|---|---|---|---|
| baseline (caption-only) | 1.0000 | 0.5602 | 2.39 |
| VL-augmented | 1.0000 | 0.8716 | 1.33 |

ΔMRR@10 = +0.31. Mean target rank improved 2.39 → 1.33. Avg VL
throughput 3.0 s per image. Decision rule (≥10 pp lift on
figure-bearing queries) cleared.

### Technical Environment

- **Nexus daemon**: Python, running over UDS (RDR-112). Currently an
  MCP server only.
- **Qwen MCP supervisor**: separate process at `qwentescence`,
  exposing tools `qwen_oneshot`, `qwen_backends`, `qwen_send`,
  `qwen_poll`, `qwen_sessions`, `qwen_spawn`, `qwen_stop`,
  `qwen_extensions`, `qwen_reload_extensions`.
- **Qwen VL backend**: Qwen 3.6-35B-A3B-UD-Q4_K_XL (~21 GB) with
  BF16 vision projector (`mmproj-Qwen3.6-35B-A3B-BF16.gguf`, ~0.85
  GB). llama-server `/v1/chat/completions`-compatible.
- **Ingest pipeline**: Docling/MinerU produces page-level structured
  output with figure/table bounding boxes; existing chunker
  (`pdf_chunker.py`) operates on the structured output.
- **`pdf2image`**: already a nexus dependency.

### Prior art consulted

- M3DocRAG paper (Cho et al., arXiv:2411.04952) §5.1 — text-vs-
  multimodal gap concentrated in visual evidence.
- The local M3DocRAG proposal — spike outcomes carry forward.
- RDR-112 storage-as-service / daemon model — relevant for nexus's
  MCP-client lifecycle.
- RDR-113 host-trust model — relevant for trusting the qwen
  supervisor's connection.

## Research Findings

### Investigation

This RDR consolidates prior spike work; no new measurements yet. The
spike outcomes from `m3docrag-application.md` are the empirical basis.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
|---|---|---|
| Qwen MCP supervisor tool list | Partial (observed in prior session) | Tools available: `qwen_oneshot`, `qwen_backends`, `qwen_send`, `qwen_poll`, `qwen_sessions`, `qwen_spawn`, `qwen_stop`, `qwen_extensions`. Schema not yet inspected in detail. |
| `qwen_oneshot` signature for image-in | Unverified | Need to confirm: accepts image content arrays (OpenAI format) or path/URI; returns structured text |
| `qwen_backends` introspection | Unverified | Need to confirm: lists currently-loaded backends with VL capability flag |
| Docling figure bounding boxes | Inherited from spike | Spike already consumed these successfully |
| `pdf2image` figure region render | Inherited from spike | Spike already rendered to PNG |
| Existing MCP-client libraries for Python | Unverified | Likely candidates: `mcp` (anthropic), `mcp-python-sdk`. Need to pick one. |

### Key Discoveries

- **Verified (spike 2026-05-17)**: VL-augmentation lifts MRR@10 by 0.31
  on visually rich papers without changing retrieval architecture.
- **Verified (cockpit substrate, RDR-112)**: nexus's daemon can host
  arbitrary additional connections; adding an MCP client is structural,
  not architectural.
- **Documented (qwen MCP supervisor)**: tool surface exists and is
  stable enough to depend on.
- **Assumed**: pre-flight availability check via `qwen_backends` query
  is the right discipline (vs try-and-skip-on-timeout).
- **Assumed**: figure-chash idempotency works across re-ingest without
  edge cases (e.g., same figure across multiple papers cached
  globally).

### Critical Assumptions

- [ ] **A1** — A Python MCP-client library is mature enough to depend
  on (lifecycle, reconnection, schema). **Status**: Unverified.
  **Method**: Source Search (`mcp-python-sdk`, anthropic `mcp` package)
  + tiny spike (connect, list-tools, call one tool).
- [ ] **A2** — `qwen_oneshot` (or equivalent) accepts image content
  in a stable format and returns structured text suitable for chunk
  augmentation. **Status**: Unverified. **Method**: Spike — call
  with a fixture figure; inspect response shape.
- [ ] **A3** — Pre-flight availability check via `qwen_backends`
  returns a deterministic VL-capable flag. **Status**: Unverified.
  **Method**: Source Search of qwen supervisor + spike.
- [ ] **A4** — Figure-chash idempotency cache survives re-ingest
  without correctness hazards (figures with different surrounding
  context should not cross-pollute). **Status**: Unverified.
  **Method**: Paper design + small spike with two papers sharing a
  figure (rare but possible).
- [ ] **A5** — Docling's figure bounding boxes are stable enough that
  the same paper re-ingested produces matching chashes (i.e., the
  rendered PNG bytes are deterministic). **Status**: Unverified.
  **Method**: Spike — index the same paper twice; compare cache hit
  rate (target 100 %).
- [ ] **A6** — Pre-flight check is the right discipline (vs
  per-figure try-and-skip-on-timeout). **Status**: Settled by
  constraint — pre-flight matches "either every figure in this
  ingest gets augmented or none do." Documented for traceability.

## Proposed Solution

### Approach

Add an MCP-client capability to the nexus daemon. At ingest time, if
the qwen supervisor is reachable and a VL backend is loaded, augment
each Docling-detected figure with VL-generated text and attach to the
owning chunk. Otherwise, skip cleanly. Idempotent via figure chash.

### Technical Design

#### 1. MCP-client capability for the nexus daemon

A new module `src/nexus/mcp_client/` housing the daemon's MCP-client
connections. Initially exposes one connection (to the qwen supervisor)
but designed to host more (other coprocessors in future).

```text
# Illustrative — verify library signature during implementation

class NexusMcpClient:
    """MCP client embedded in the nexus daemon."""

    def __init__(self, transport_config: TransportConfig): ...
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def list_tools(self) -> list[ToolDescriptor]: ...
    def call_tool(self, name: str, args: dict) -> ToolResult: ...
    def is_available(self) -> bool: ...
```

Lifecycle: connection initiated lazily on first use; held open across
ingest calls; reconnect on transport error with backoff; explicit
shutdown on daemon stop.

#### 2. Qwen supervisor configuration

New `.nexus.yml` block:

```yaml
ingest:
  qwen_mcp:
    enabled: true             # opt-in (default)
    transport: stdio          # or "uds" or "tcp" — match supervisor config
    command: ["qwen-stack", "supervisor", "serve"]   # if stdio
    socket_path: null         # if uds
    timeout_seconds: 30       # per-call timeout; figure-level
    preflight_timeout_seconds: 5
    vl_backend_capability_check: true  # call qwen_backends on preflight
```

The configuration shape mirrors `~/.qwen-coprocessor-stack/config.json`
where overlapping — no duplication, just nexus's view of the
connection.

#### 3. Pre-flight availability check

At ingest start (once per ingest run), the augmentation hook:

1. Attempts MCP-client connect to the qwen supervisor.
2. On success, calls `qwen_backends` to list loaded backends.
3. Checks for at least one VL-capable backend.
4. If all three succeed within `preflight_timeout_seconds`: proceed
   with augmentation for this ingest run.
5. If any step fails: log a single structured event
   (`vl_augmentation_unavailable`), set the run's augmentation flag
   to False, ingest continues unchanged.

The flag is sticky for the run — no per-figure rechecks. Either every
figure gets augmented or none do. Matches the "explicit no-op"
constraint.

#### 4. Per-figure augmentation

For each figure or table from Docling's bounding-box output:

1. Render the figure region to PNG via `pdf2image`.
2. Compute `chash = sha256(png_bytes)`.
3. Check augmentation cache (T2 table `vl_augmentation_cache`); if
   hit, attach cached text to the owning chunk and continue.
4. Otherwise, call `qwen_oneshot` (or equivalent VL tool) with the
   PNG plus a structured prompt:
   - Caption (from Docling)
   - Surrounding prose (the chunk text that owns the figure)
   - Instruction: "Describe this figure in one sentence. For charts,
     name axes, units, and the trend. For tables, list row/column
     labels and any salient values."
5. Receive structured text; cache by chash; attach to the owning
   chunk; tag chunk metadata with `vl_augmented: true` and
   `vl_augmentation_chashes: [<chash>, ...]`.
6. If the call fails or times out (`timeout_seconds`): log a single
   structured event, skip this figure, continue with the next.
   Partial-augmentation is acceptable inside a single ingest run as
   long as the pre-flight cleared (figures that *did* get the call
   succeeded are augmented; figures that hit a transient backend error
   are not). The run's augmentation flag stays True; the next ingest
   re-attempts only the figures that aren't cached.

#### 5. Idempotency cache schema

New T2 table `vl_augmentation_cache`:

```sql
CREATE TABLE vl_augmentation_cache (
    figure_chash   TEXT PRIMARY KEY,            -- sha256 of PNG bytes
    augmentation   TEXT NOT NULL,                -- VL-generated text
    model_id       TEXT NOT NULL,                -- qwen backend id at call time
    prompt_version TEXT NOT NULL,                -- prompt template version
    created_at     REAL NOT NULL                 -- epoch seconds
);
```

Cache lookup is keyed by `figure_chash`. Including `model_id` +
`prompt_version` in the row enables future cache invalidation if the
prompt or model changes meaningfully — but lookup is by chash alone
(simpler).

#### 6. Chunk attachment

The augmentation text is appended to the chunk whose bounding-box
region contains the figure. The chunk's byte budget is honored —
augmentation truncated if necessary. Chunk metadata is updated with:

- `vl_augmented: true`
- `vl_augmentation_chashes: [<chash>, ...]`
- `vl_augmentation_model: <backend_id>`

Downstream retrieval can prefer / disprefer these chunks if needed
(addressed in future RDR if observed quality issues).

#### 7. No automatic re-augmentation

If a paper was ingested while qwen was unavailable (run's flag False,
no figures augmented), it stays unaugmented until the user explicitly
re-ingests. Idempotency cache keys make re-ingest cheap (cached
figures skipped; only new figures call qwen).

This is the "otherwise just don't do anything as we currently do
today" constraint, made explicit.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| MCP-client capability | none in nexus | **New** — `src/nexus/mcp_client/` |
| Qwen supervisor connection | external (qwen-coprocessor-stack) | **Adopt entire** |
| Figure bounding boxes | Docling/MinerU output | **Reuse entire** |
| `pdf2image` region render | existing dep | **Reuse entire** |
| `vl_augmentation_cache` table | none | **New** — T2 migration |
| Augmentation hook on ingest path | none | **New** — `src/nexus/ingest/vl_augmentation.py` |
| Configuration | `.nexus.yml` | **Extend** — new `ingest.qwen_mcp` block |
| Chunk metadata fields | existing chunker | **Extend** — add `vl_augmented`, `vl_augmentation_chashes`, `vl_augmentation_model` |

### Decision Rationale

Three forces drove the design:

1. **The spike already passed.** The empirical case for VL-augmentation
   is settled. This RDR ships the win, not re-evaluates it.
2. **Qwen MCP integration is the cleanest interface.** Shell-out adds
   per-figure subprocess overhead; direct HTTP bypasses the
   MCP-as-uniform-interface story; nexus-as-MCP-client is the right
   abstraction even though it's novel for nexus today.
3. **The "qwen-or-nothing" constraint kills complexity.** No fallback
   model, no background queue, no retry semantics. Either qwen is up
   or we no-op.

## Alternatives Considered

### Alternative 1: Shell out to qwen-stack CLI

**Description**: For each figure, spawn `qwen oneshot ...` as a
subprocess.

**Pros**:
- No new MCP-client abstraction in nexus.
- Robust against MCP library churn.

**Cons**:
- Process-spawn overhead per figure (~50 ms × 10–20 figures/paper).
- Loses MCP's typed schema.
- Doesn't generalize to other coprocessors later.

**Reason for rejection**: nexus already lives in the MCP world; adding
an MCP-client is the structurally right move, not a hack.

### Alternative 2: Direct OpenAI-compatible HTTP

**Description**: Call the qwen backend's `/v1/chat/completions`
endpoint directly, discovering the URL from
`~/.qwen-coprocessor-stack/config.json`.

**Pros**:
- Simplest from nexus's side.
- No MCP-client dependency.

**Cons**:
- Bypasses the qwen supervisor's discovery, lifecycle, and capability
  management.
- Couples nexus to the backend's HTTP shape rather than the supervisor's
  tool shape.
- Reading the supervisor's config from disk is fragile across upgrades.

**Reason for rejection**: the user constraint is explicit — "use our
qwen MCP interface."

### Alternative 3: Fallback to SigLIP figure-only embeddings when qwen
unavailable

**Description**: If qwen is down, fall back to SigLIP figure
embeddings (already PASSED spike, bead `nexus-8siy`).

**Pros**:
- Visual signal preserved even when qwen down.

**Cons**:
- Adds SigLIP infrastructure for an edge case.
- Conflicts with the user's "do nothing if unavailable" constraint.

**Reason for rejection**: explicit constraint. The bead `nexus-8siy`
remains historically open for an independent decision later.

### Alternative 4: Background queue with retry-when-online

**Description**: When qwen is down, queue figures for augmentation;
when qwen comes back, dequeue and augment.

**Pros**:
- Eventually augments everything.

**Cons**:
- Operational complexity (queue persistence, retry policy, dead-letter
  handling).
- Conflicts with "do nothing if unavailable" constraint.
- Most papers ingested while qwen is down likely never come back to
  the queue anyway.

**Reason for rejection**: explicit constraint + complexity isn't worth
it. Re-ingest is the recovery path; idempotency cache makes it cheap.

### Alternative 5: Per-figure try-and-skip-on-timeout (no pre-flight)

**Description**: Skip the pre-flight; just attempt each figure with a
short timeout; figures that succeed are augmented, others are not.

**Pros**:
- Slightly simpler control flow.
- Resilient to transient backend hiccups.

**Cons**:
- Partial-augmentation across an ingest produces inconsistent corpora:
  some figures in a paper are augmented, some not.
- No clear signal to the user that augmentation is unavailable.

**Reason for rejection**: pre-flight matches "either available or not,
don't degrade" semantic. Settled by constraint (A6).

### Briefly Rejected

- **In-process VL inference inside the nexus daemon**: defeats the
  whole "qwen is the dedicated VL compute" point.
- **ColBERT/ColPali changes to retrieval**: different problem scope;
  can revisit independently. The proposal's P1/P3 paths remain
  available as future work.

## Trade-offs

### Consequences

- (+) Closes the visual-evidence gap empirically demonstrated by the
  spike.
- (+) Single-path design; no branching complexity.
- (+) MCP-client capability becomes a generic nexus daemon facility
  for future coprocessors.
- (+) Idempotent and cheap to re-run; figure-chash caching avoids
  re-spending prefill.
- (+) Fail-soft: ingest continues if qwen is unavailable.
- (−) Nexus daemon grows an MCP-client side; new lifecycle and
  reconnection logic to maintain.
- (−) Depends on the qwen supervisor's stability; qwen-tool-schema
  churn could break the integration.
- (−) Papers ingested while qwen is down are stuck unaugmented
  until manually re-ingested.

### Risks and Mitigations

- **Risk**: qwen supervisor's MCP tool schema changes (e.g.,
  `qwen_oneshot` renamed or signature shifted).
  **Mitigation**: pin to a tool-name + signature contract; nexus's
  side has a small adapter that's easy to update; integration test
  exercises the contract on every release.

- **Risk**: Figure-chash collisions across papers cause incorrect
  augmentation reuse.
  **Mitigation**: figure-chash is SHA-256 of PNG bytes; collisions are
  vanishingly improbable but A4 verifies via spike.

- **Risk**: Docling bounding boxes are not deterministic across
  re-ingest (paper text changes → different OCR → different boxes).
  **Mitigation**: A5 spike measures cache hit rate on re-ingest; if
  not 100 %, expand the chash to include normalized bounding-box
  coordinates as a tie-breaker.

- **Risk**: VL throughput at scale (large papers × many figures)
  exceeds acceptable ingest latency.
  **Mitigation**: 3.0 s avg per figure (spike) × 10–20 figures/paper =
  30–60 s per paper of one-time prefill. Acceptable for indexing.
  Parallelize across figures if needed (qwen supervisor decides
  whether to actually parallelize on its side).

- **Risk**: Augmentation text quality is variable across figure types.
  **Mitigation**: prompt template versioning (`prompt_version` in
  cache); we can iterate on prompts without invalidating all cached
  augmentations (only newly-augmented figures use the new prompt).

### Failure Modes

- *Visible*: figure-bearing query returns lower-ranked result on a
  paper ingested while qwen was unavailable; user notices and
  re-ingests.
- *Silent*: qwen supervisor responds with a degraded VL backend (e.g.,
  the projector not loaded); augmentation text is low-quality but
  cached. Recovery: cache invalidation by `model_id` if needed.
- *Recovery*: re-ingest the paper; cached augmentations skipped, new
  ones generated.

## Implementation Plan

### Prerequisites

- [ ] A1–A5 verified via spikes (see Critical Assumptions).
- [ ] Qwen MCP supervisor's tool surface inventoried in detail
      (schemas via `qwen_extensions` and equivalent).
- [ ] Python MCP-client library chosen and added as a nexus
      dependency.

### Minimum Viable Validation

Re-run the original spike with the production implementation: index
the same 5–10 figure-bearing papers (M3DocRAG, BSS, OO and others
from `knowledge__dt-papers`) both with and without the production VL
hook; measure recall@10 on the original query set. Replicate the
spike's MRR@10 0.56 → 0.87 lift to ≥ ±0.05.

### Phase 1: MCP-client infrastructure

#### Step 1: Pick + integrate MCP-client library

Survey `mcp-python-sdk`, anthropic `mcp`, and any community options.
Pick one. Add as nexus dep. Smoke-test connect-and-list-tools against
the qwen supervisor.

#### Step 2: `src/nexus/mcp_client/` module

`NexusMcpClient` wrapping the chosen library with nexus's lifecycle
discipline (lazy connect, reconnect with backoff, structured logging
via loguru).

#### Step 3: Configuration plumbing

Add `ingest.qwen_mcp` block to `.nexus.yml` schema; wire into the
daemon startup; document in `docs/cli-reference.md`.

### Phase 2: VL augmentation hook

#### Step 4: `vl_augmentation_cache` T2 migration

New migration in `src/nexus/db/migrations.py` adding the
`vl_augmentation_cache` table.

#### Step 5: `src/nexus/ingest/vl_augmentation.py`

The hook module:
- `pre_flight_check(client) -> bool` (returns ingest's augmentation
  flag).
- `augment_figures(client, figures, chunks) -> AugmentationResult`
  (per-ingest entry point).
- Internal: render, chash, cache lookup, call `qwen_oneshot`, attach
  to chunk, update metadata.

#### Step 6: Wire into ingest pipeline

Add the pre-flight call at the top of the per-paper ingest and the
per-figure loop after Docling extraction. Maintain back-compat: if
`ingest.qwen_mcp.enabled: false`, the hook is skipped entirely
(no-op even if qwen is up).

### Phase 3: Validation

#### Step 7: Replicate the spike on production code

Re-index 5–10 figure-bearing papers; measure recall@10 / MRR@10 lift;
confirm cache hit rate ≥ 99 % on re-ingest of the same papers (A5).

#### Step 8: Documentation

Update `docs/cli-reference.md` for the new config block. Note the
"qwen-or-nothing" semantic and the re-ingest recovery path
explicitly.

### Day 2 Operations

- **Cache management**: `nx ingest vl-cache list/stats/purge` CLI
  commands (TBD; small surface).
- **Backend changes**: if the qwen backend's model changes (e.g.,
  Qwen 3.6 → Qwen 4), cache rows carry `model_id`; downstream code
  can selectively invalidate.
- **Monitoring**: structured `vl_augmentation_unavailable` events
  signal qwen-down ingests; cockpit binding (under RDR-118/119) can
  surface these.

### New Dependencies

- Python MCP-client library (TBD; ~one dependency).
- No new system-level dependencies (qwen supervisor is external to
  nexus).

## Test Plan

- **Scenario**: pre-flight passes; augmentation runs.
  **Verify**: chunks gain `vl_augmented: true` metadata and
  augmentation text; cache rows created.
- **Scenario**: pre-flight fails (qwen down).
  **Verify**: single structured event logged; ingest continues; no
  augmentation; chunks have no `vl_augmented` metadata.
- **Scenario**: per-figure timeout on a backend hiccup.
  **Verify**: single structured event per figure; that figure not
  augmented; other figures in the same ingest still attempted.
- **Scenario**: re-ingest same paper.
  **Verify**: cache hit rate ≥ 99 % (A5); zero new qwen calls if no
  figures changed.
- **Scenario**: re-ingest after qwen-down ingest.
  **Verify**: pre-flight now passes; all previously-unaugmented
  figures now augmented; cache populated.
- **Scenario**: paper with no figures.
  **Verify**: pre-flight skipped or no-op; no augmentation rows.
- **Scenario**: MVV replicates spike lift.
  **Verify**: cross-paper MRR@10 within ±0.05 of 0.87.

## Validation

### Testing Strategy

1. **Scenario**: production code replicates the cross-paper spike
   result.
   **Expected**: MRR@10 = 0.87 ± 0.05; mean target rank ≤ 1.5.

### Performance Expectations

- Pre-flight check: ≤ 5 s p99 (timeout enforced).
- Per-figure VL call: ≤ 30 s p99 (timeout enforced); ~3.0 s p50
  (per spike).
- Per-paper augmentation overhead: ~30–60 s p50; cached re-ingest
  ≤ 1 s overhead.
- Cache hit rate on re-ingest of unchanged paper: ≥ 99 %.

## Finalization Gate

(deferred — sketch only)

### Contradiction Check

(deferred)

### Assumption Verification

(deferred — A1–A5 unverified; spikes required before Phase 2)

### Scope Verification

MVV in scope: re-run the original cross-paper spike with the
production code; replicate the lift. Reactivates bead `nexus-6h0e`.

### Cross-Cutting Concerns

- **Versioning**: `prompt_version` in cache enables future prompt
  iteration. Cache rows carry `model_id`.
- **Build tool compatibility**: depends on chosen MCP-client library;
  must work in nexus's existing build.
- **Licensing**: qwen GGUF is Apache-2.0; MCP-client library must be
  compatible.
- **Deployment model**: qwen supervisor lifecycle is independent;
  nexus daemon connects/disconnects as needed.
- **IDE compatibility**: N/A.
- **Incremental adoption**: `ingest.qwen_mcp.enabled: false` disables
  the hook entirely (default opt-in but easy to opt-out).
- **Secret/credential lifecycle**: N/A — qwen supervisor connection
  is local (UDS or stdio); no credentials.
- **Memory management**: VL response text is bounded by chunk byte
  budget; truncation discipline matches existing chunker.

### Proportionality

Single-purpose RDR. Scope is exactly the qwen-MCP figure augmentation
hook. Multi-path alternatives explicitly rejected. ColBERT, SigLIP,
ColPali remain available as independent future work, not part of this
RDR.

## References

- `docs/proposals/m3docrag-application.md` (branch
  `feature/nexus-m3docrag-proposal-and-colbert-spike`) — superseded by
  this RDR; spike data carries forward.
- Cho et al., M3DocRAG (arXiv:2411.04952) §5.1 — text-vs-multimodal
  gap evidence.
- Qwen 3.6-35B-A3B model card (unsloth GGUF release, 2026-04-24).
- RDR-110: Semantic Tuple Space (subspaces for structured event
  logging).
- RDR-112: Storage-as-Service Container Boundary (daemon lifecycle
  applies to the MCP-client side).
- RDR-113: Host-Trust Model (qwen supervisor connection trust).
- Bead `nexus-6h0e` — VL augmentation hook (filed during spike;
  reactivated under this RDR).

## Revision History

_2026-05-21 — initial draft. Supersedes the multi-path proposal at
`docs/proposals/m3docrag-application.md`. Single-path design under the
constraint "qwen-MCP-or-nothing": MCP-client integration with the qwen
supervisor; pre-flight availability check; per-figure synchronous
augmentation; idempotent via figure chash; explicit no-op when qwen
unavailable. Reactivates bead `nexus-6h0e`._
