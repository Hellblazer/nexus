---
title: "Server-Side Reranking: Retire the Last Client-Side Voyage Consumer So the Client Carries Zero Voyage Credentials"
id: RDR-188
type: Architecture
status: closed
closed_date: 2026-07-23
accepted_date: 2026-07-22
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-07-22
related_issues: [nexus-r9c78, nexus-r5f3c]
related: [RDR-152, RDR-155, RDR-156, RDR-160, RDR-166]
---

# RDR-188: Server-Side Reranking — Retire the Last Client-Side Voyage Consumer

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

#### Gap 1: The reranker is the last genuine runtime client-side Voyage consumer

`scoring.rerank_results` calls rerank-2.5 through `get_voyage_client`,
so every client install that reranks must carry a live
`VOYAGE_API_KEY`. Everything else already moved server-side — in
service mode (local and managed, post RDR-155 P4a Seam B) all embedding
Voyage traffic is owned by the engine (`code_indexer._service_mode_stub`,
the doc_indexer mirror, `pipeline_stages` streaming `embed_fn=None`,
`HttpVectorClient.upsert_chunks_with_embeddings` discarding caller
vectors — the nexus-fsquc paid-Voyage-TWICE scar). The remaining
client-side `voyage_client.embed` loops are Chroma-era migration-source
legacies that die at RDR-155 P4b.

#### Gap 2: Dead-key silent degradation — rerank quietly reverts to distance order

The cost of the residual is not hypothetical: the 6.15.0 shakeout
(2026-07-21) found Hal's shell exporting a DEAD `VOYAGE_API_KEY` that
silently degraded reranking per tmux pane while everything else worked —
a whole failure class (`nexus-r5f3c`'s other half) that exists ONLY
because one code path still needs a client-side key. Research pinned
the mechanism: `_rerank_cloud`'s broad `except Exception`
(`scoring.py:460-462`) returns original order WARN-only, and
`get_credential`'s env-over-config precedence means a stale shell key
always beats a valid config.yml key. Hal's framing: "client-side
Voyage anything is moot if the server owns it."

#### Gap 3: Client behavior signals are inferred from key presence — retirement without replacement signals silently regresses

Two client code paths infer behavior from `voyage_api_key` presence
rather than from what the server actually does: `is_local_mode()`'s
legacy fallback clause (mode inference) and
`_voyage_thresholds_active()` (Voyage-calibrated distance-threshold
gating). Blanking the client key without replacement signals would
misclassify configured cloud installs as local and silently regress
search thresholds — so credential retirement is a design problem, not
a deletion.

## Context

- The engine already holds `NX_VOYAGE_API_KEY` (it performs all
  embedding) — granting it rerank traffic adds no new credential surface.
- The engine has ZERO rerank code today (`grep -ri rerank service/src` is
  empty). This is a new engine endpoint/stage: SECOND LIFECYCLE work
  (engine-service release + `REQUIRED_ENGINE_VERSION` bump), plus the
  client repoint in the same conexus release (fix-delivery coupling).
- Composes with world-blocked `nexus-70r3c.18` (RDR-156 P5.2 server-side
  RRF fusion): rerank as a server-side stage of the search/query endpoint
  makes retrieval one round-trip — no client fan-out, no client key.
- Local-mode-without-Voyage installs use the cross-encoder path
  (`cross_encoder.py`); the design must preserve the no-Voyage local
  posture (RDR-109/160 lineage) — server-side rerank applies where a
  Voyage key exists SERVER-side, never resurrecting a client key
  requirement anywhere.
- No-knob-reflex directive: fold into the existing search/query journey —
  no new client verb, no new client configuration.

## Desired End State

1. **Zero client Voyage CONSUMPTION** (the precise scope of the title's
   shorthand): no client code path reads `voyage_api_key` to shape its
   own behavior — rerank, mode inference, threshold gating, doctor
   verdicts. The shell-export dead-key degradation class becomes
   structurally impossible because no client code consumes the key.
   The key MAY persist in `config.yml` on local-service installs solely
   as engine-bootstrap material handed to the supervised engine's env
   (`storage_service_daemon`'s plumb is the engine's embed-key delivery
   wire, not a client consumer) — unless P3 lands the engine-owned
   credential-source alternative, which would retire even that
   (decided in-phase; both outcomes satisfy this end state).
2. The engine owns all Voyage traffic (embed + rerank), reranking as a
   server-side stage of the search/query endpoints (optionally fused with
   RRF per RDR-156 P5.2 when that unblocks).
3. Older local engines CONVERGE, never refuse (one-engine doctrine,
   `engine_version.py`): the `REQUIRED_ENGINE_VERSION` bump makes
   rerank support part of the pinned dependency and existing
   convergence machinery installs it before a search request ever
   observes a mismatch. The only surviving floor-style check is the
   managed-cloud handshake (`managed_endpoint.py`), where an
   under-floor server surfaces a handshake-time signal — never a
   per-search refusal or a silent no-rerank fallback.

## Research Findings

(2026-07-22, two-agent codebase + web sweep; file:line verified.)

### R1 — Client call graph: ONE production caller

`rerank_results` (`src/nexus/scoring.py:374`) has exactly one production
caller: `src/nexus/commands/search_cmd.py:453-458` (the `nx search`
CLI). The MCP search/query tools and the nx_answer plan runner never
rerank — they call `search_engine.search_cross_corpus` and return
distance-sorted results (`search_engine.py:494`'s docstring hands
results back "for the caller's reranker"; only search_cmd is that
caller). Gate: `not no_rerank and not is_local_mode() and
len(set(collections)) > 1`; candidate count is the CLI's final `n`
(upstream already overfetched 4x knowledge/docs/rdr, 2x code). Model
from `config["embeddings"]["rerankerModel"]` (default `rerank-2.5`).

**The dead-key silence has two mechanisms** in `_rerank_cloud`
(`scoring.py:419-468`), both WARN-only, invisible to the CLI user:
(1) key absent → `get_voyage_client()` returns None → skip; (2) key
present-but-dead (the 6.15.0 shakeout case) → `voyageai.Client` built
with NO validation → auth error is non-retryable → broad
`except Exception` at `scoring.py:460-462` returns original order.
Root cause of "dead shell key wins": `get_credential`
(`config.py:781-795`) gives env var unconditional precedence over
config.yml.

### R2 — Engine placement: fused stage (Option A) wins

Engine search surface is `/v1/vectors/*` in `VectorHandler.java`
(dispatch :119-140): `/search`, `/hybrid-search`,
`/search-metadata-scoped`, `/search-topic-scoped`, `/search-graph-hop`,
all executing under `tenantScope.withTenant(...)` via jOOQ refs to the
RDR-156 P4 PG functions. `grep -ri rerank service/src` confirms zero
engine rerank code. **Option A (fused per-request stage, optional
`rerank`/`rerank_top_k` DTO fields on the 5 existing search bodies)**
preserves the one-round-trip claim, adds no new tenancy surface (rerank
scores rows already fetched under RLS), and composes with RDR-156 P5.2
later. Option B (dedicated `/v1/rerank`) keeps a second round trip —
the RDR's own A2 already flags it weaker. Constraint: the Voyage call
runs synchronously inside the request — needs a bounded timeout and a
LOUD degrade (the client's current silent fallback-to-input-order must
NOT be inherited). Reuse: `VoyageEmbedder.callApi`
(`VoyageEmbedder.java:207-258` — 3-attempt backoff, retries 429/5xx,
typed `UpstreamAuthException` → 502) is the template for a
`VoyageReranker` sibling; `NX_VOYAGE_API_KEY` already read at
`Main.java:110`. EmbedderRouter is unnecessary — rerank scores
`(query, chunk_text)` pairs regardless of embedding model.

### R3 — Voyage rerank-2.5 limits (docs.voyageai.com, 2026-07-22)

`POST /v1/rerank`; models `rerank-2.5` / `rerank-2.5-lite` (32K
context). Query ≤8k tokens; ≤1,000 docs/request; query+doc ≤32k;
total ≤600k tokens. Rate: 2M TPM / 2000 RPM tier 1 (lite 4M TPM);
tiers scale 2x/3x with billing. ~$0.05/1M input tokens. Our envelope
(top-N ≤ tens of candidates) sits far under all caps. **Governor gap
is pre-existing engine-wide debt**: no proactive rate limiter exists
(T2 `nexus/research-server-side-embed-reduction-2026-07-05`); the
reranker inherits VoyageEmbedder's reactive retry shape as the first
cut.

### R4 — Local-mode story: server-side cross-encoder is buildable

Client `LocalCrossEncoder` (`cross_encoder.py:46`) = onnxruntime CPU +
HF tokenizers, `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80MB),
lazy-downloaded. Engine's `Bge768Embedder.java` (DJL tokenizer +
OrtSession) is architecturally identical — a server-side cross-encoder
clones that pattern; `pom.xml` already bundles per-platform
onnxruntime natives for all 4 binary targets, so no new native-image
reachability config expected (confirm with a native smoke). Model
provisioning mirrors the bge-768 flow (`service_bge_model.py`, init
size-floor guard) at ~80MB vs bge-768's ~416MB.

### R5 — Credential-retirement blast radius: three categories

**(a) Rerank-driven, retires with this RDR**: `scoring.py:394-451`
(`_rerank_cloud`), `db/__init__.py:94-147` (`get_voyage_client` —
docstring says it exists solely for the reranker), doctor voyage lines
(`doctor.py:2089-2145`), part of `health.py:723-731`.

**(b) Migration-source legacy, dies at RDR-155 P4b (out of scope)**:
`pipeline_stages.py:712-734`, `doc_indexer.py` (5 sites), `indexer.py`
("Phase-4 deletion target" comments), `indexer_utils.py:556-574`,
`commands/collection.py:912-920`, `db/t3.py`, the migration module.

**(c) The hard part — needs replacement signals, not deletion**:
- `config.py:571-599` `is_local_mode()` legacy clause infers mode from
  `voyage_api_key` presence; blanking the key could misclassify a
  cloud install as local.
- `search_engine.py:308-331` `_voyage_thresholds_active()` gates
  Voyage-calibrated distance thresholds on client key presence —
  silently regresses to False when the key leaves unless replaced by a
  server-reported capability signal (the code already flags this class,
  nexus-h8rf6.9/xbw0f).
- `daemon/storage_service_daemon.py:533-570` — the supervisor plumb
  that injects `NX_VOYAGE_API_KEY` into the locally-supervised engine's
  env. NOT rerank-specific: it is the delivery wire for the engine's
  own EMBED key on local-service installs. "Zero client Voyage
  credentials" is unreachable while this plumb exists in current form —
  either the engine gets an independent credential source, or the RDR
  scopes this plumb as surviving (the key then lives in the client
  credential chain solely as engine-bootstrap material, never consumed
  by client code paths).

**Ordering**: P1/P2 (engine stage + client repoint) retire (a)
independently; (b) is P4b, do-not-start; (c) each needs its own design
decision inside P3 — mode signal, threshold capability signal, and the
supervisor-plumb scope call. P3 also sweeps `mcp/core.py:6563`'s
`voyage_key_found` diagnostics boolean (not behavior-shaping, but its
meaning changes once the client key is bootstrap-only material).

Full agent reports: T1 scratch (tags `rdr-188,research`) + T3
`rdr188-r2-r3-research-engine-rerank-placement-2026-07-22`.

## Proposed Solution

(Locked at acceptance; research-informed:)

1. **Fused rerank stage (R2 Option A)**: optional `rerank` /
   `rerank_top_k` request fields on the existing `/v1/vectors/*` search
   handlers. Rerank runs on rows already fetched under RLS inside
   `tenantScope.withTenant` — one round trip preserved, no new tenancy
   surface. `VoyageReranker` clones `VoyageEmbedder.callApi`'s
   retry/backoff/auth shape against `POST /v1/rerank` (rerank-2.5);
   bounded timeout; upstream failure degrades LOUD (structured error
   field in the response, never a silent fallback to input order — the
   client's current behavior is the anti-pattern, per
   no-silent-fallbacks).
2. **Client repoint**: `search_cmd` passes the rerank flag and consumes
   server scores — and SURFACES the server's structured degraded-rerank
   field to the user (Gap 2's defect was WARN-only invisibility; the
   repoint must not recreate it one layer up). `scoring._rerank_cloud`
   + `get_voyage_client` retire (category (a) of R5). Engine delivery
   is the standard `REQUIRED_ENGINE_VERSION` bump: local installs
   converge automatically per the one-engine doctrine (see Desired End
   State 3) — no new mismatch-refusal path is built. **Scope: MCP
   search/query and nx_answer do NOT gain rerank in this RDR** — they
   never reranked (R1); the engine stage being available on all 5
   endpoints makes their later opt-in a request-field flip (composes
   with RDR-156 P5.2), tracked separately if ever wanted.
3. **Local mode**: server-side cross-encoder in the engine, cloning the
   `Bge768Embedder` DJL/ONNX pattern with the ~80MB
   ms-marco-MiniLM-L-6-v2 model provisioned bge-768-style (R4);
   client `cross_encoder.py` retires with it. Native-image smoke
   confirms the already-bundled onnxruntime natives suffice.
4. **Credential-chain retirement (the R5 category-(c) designs, own
   phase)**: `is_local_mode()` loses its voyage-key inference clause
   (mode comes from explicit config/service presence);
   `_voyage_thresholds_active()` is replaced by a server-reported
   capability signal (which embedder family served the query); the
   `storage_service_daemon` supervisor plumb is explicitly re-scoped as
   engine-bootstrap material — the key may persist in config.yml solely
   to launch the local engine, with zero client code paths consuming
   it (or moves to engine-owned config; decided in-phase).

Engine floor bump delivers; the same conexus release ships the client
repoint.

## Alternatives Considered

- A1: Keep client-side rerank, harden key hygiene (doctor checks for
  dead keys). Rejected: treats the symptom; the class persists.
- A2: Dedicated client-callable `/v1/rerank` proxy endpoint only (no
  fused stage). Simpler engine change; keeps a second round-trip and
  client-side orchestration. Evaluate in research (R2).
- A3: Wait for RDR-156 P5.2 fusion (world-blocked) and do both at once.
  Rejected as a gate: P5.2 composes but must not block the credential
  retirement.

## Trade-offs

- Engine gains an outbound-Voyage rerank dependency in the serving path
  (latency + rate-limit exposure inside a request), bounded by a
  timeout with a loud degrade (per no-silent-fallbacks). P1 ships
  ACCEPTING the pre-existing engine-wide governor gap as-is (reactive
  429/5xx retry only, no proactive limiter — R3): our top-N envelope
  sits far under the 2000 RPM / 2M TPM ceilings, and the governor is
  tracked as its own debt bead `nexus-rb67a`, not a blocker here.
- Managed-cloud rerank cost accrues to the service operator rather than
  the client key holder — consistent with embeddings today.

## Implementation Plan

(Beads at /conexus:create-plan after acceptance. Expected phases:
P1 engine rerank stage + tests + engine tag; P2 client repoint + floor
bump; P3 credential-chain retirement + doctor/wizard/mode-heuristic
cleanup + docs.)

## Test Plan

- Engine: rerank-stage unit + integration (Testcontainers) with a fake
  Voyage upstream; degrade-loud on upstream failure.
- Client: parity test that search results with rerank enabled flow
  through the server path with zero client Voyage reads (credential-read
  tripwire test); a test asserting the server's structured
  degraded-rerank field is SURFACED to the user (rendered/warned) — the
  Gap 2 invisibility class must not reappear at the repoint layer;
  mode-lint sweep for retired voyage heuristics.
- E2E: fresh-install MVV unchanged; rerank quality spot-check vs the
  client-side baseline (score-order parity on a fixed corpus).

## Validation

- `grep -r get_voyage_client src/nexus` returns only migration-source
  legacies slated for P4b (then: nothing).
- A client install with NO voyage key anywhere reranks successfully in
  service mode.

## Finalization Gate

/conexus:rdr-gate 188 before acceptance; stacked review per standing
discipline; engine work follows the engine-release skill.

## References

- Bead nexus-r9c78 (audit of record, 2026-07-22).
- 6.15.0 shakeout record (auto-memory project_release_6_15_0): the dead
  shell-key rerank degradation.
- RDR-156 P5.2 / nexus-70r3c.18 (server-side RRF fusion, world-blocked).
- nexus-5g5ek (CCE concurrency + Voyage rate-governor memo).

## Revision History

- 2026-07-22: Created from bead nexus-r9c78 (Hal elevation call).
- 2026-07-22: Research findings recorded (R1-R5, two-agent sweep).
- 2026-07-22: Gate round 1 BLOCKED (2 Criticals) — Desired End State
  rescoped to zero-client-CONSUMPTION (supervisor plumb = engine
  bootstrap material, both P3 outcomes valid); engine delivery reframed
  from "loud floor degrade" to the one-engine CONVERGENCE doctrine;
  MCP/nx_answer explicitly out of scope; degraded-field surfacing test
  added; governor debt accepted with bead nexus-rb67a.
- 2026-07-22: CLOSED (implemented, no divergences). All four Proposed
  Solution items delivered as locked: engine fused rerank + local
  cross-encoder shipped in engine-service-v0.1.52 (deployed +
  per-query-diff cloud-gated GREEN 2026-07-23Z, T2 [21062]:
  parity 106/113 == baseline, zero per-query regressions,
  reranker=rerank-2.5 active, zero degrade events); client repoint +
  Voyage-reranker deletion + tripwires (ce76b8d1/3bbe4e7f/a3a7eaa3);
  floor (0,1,52) (3ac406ad); credential-chain retirement (d206ebdf).
  Phase cross-walk PASS: T2 nexus_rdr/188-phase-review-gate-2026-07-22
  [21064]. Stacked reviews [21052]/[21053]/[21057]/[21058]/[21060].
  Deferred by design (unfiled, per §Proposed Solution item 2): MCP
  search/query + nx_answer rerank opt-in — a request-field flip when
  wanted. Category-(b) migration-source Voyage loops remain RDR-155
  P4b scope.
