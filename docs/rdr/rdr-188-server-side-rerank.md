---
title: "Server-Side Reranking: Retire the Last Client-Side Voyage Consumer So the Client Carries Zero Voyage Credentials"
id: RDR-188
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: ""
created: 2026-07-22
related_issues: [nexus-r9c78, nexus-r5f3c]
related: [RDR-152, RDR-155, RDR-156, RDR-160, RDR-166]
---

# RDR-188: Server-Side Reranking — Retire the Last Client-Side Voyage Consumer

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The reranker is the ONE remaining genuine runtime client-side Voyage
consumer: `scoring.rerank_results` calls rerank-2.5 through
`get_voyage_client`, so every client install that reranks must carry a
live `VOYAGE_API_KEY`. Everything else already moved server-side — in
service mode (local and managed, post RDR-155 P4a Seam B) all embedding
Voyage traffic is owned by the engine (`code_indexer._service_mode_stub`,
the doc_indexer mirror, `pipeline_stages` streaming `embed_fn=None`,
`HttpVectorClient.upsert_chunks_with_embeddings` discarding caller
vectors — the nexus-fsquc paid-Voyage-TWICE scar). The remaining
client-side `voyage_client.embed` loops are Chroma-era migration-source
legacies that die at RDR-155 P4b.

The cost of the residual is not hypothetical: the 6.15.0 shakeout
(2026-07-21) found Hal's shell exporting a DEAD `VOYAGE_API_KEY` that
silently degraded reranking per tmux pane while everything else worked —
a whole failure class (`nexus-r5f3c`'s other half) that exists ONLY
because one code path still needs a client-side key. Hal's framing:
"client-side Voyage anything is moot if the server owns it."

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

1. `voyage_api_key` leaves the CLIENT credential chain entirely: config
   wizard, env plumbing (`r5f3c`'s supervisor pass-through), doctor
   credential lines, and the shell-export dead-key degradation class all
   become structurally impossible on the client.
2. The engine owns all Voyage traffic (embed + rerank), reranking as a
   server-side stage of the search/query endpoints (optionally fused with
   RRF per RDR-156 P5.2 when that unblocks).
3. Clients on older engines degrade LOUDLY per the one-engine-per-release
   rule (floor bump delivers the feature; no silent no-rerank fallback).

## Research Findings

(Populated by /conexus:rdr-research. Candidate questions:)
- R1: Exact client call graph of `scoring.rerank_results` +
  `cross_encoder.py` — which entry points rerank (search_cmd, query MCP,
  nx_answer plans) and with what candidate-count/latency envelope.
- R2: Engine-side placement — a rerank stage inside the existing
  combined-query functions' consumer (`/v1/vector/search`-family) vs a
  dedicated `/v1/rerank` endpoint the client composes; latency budget vs
  the RDR-156 P4 combined-query one-round-trip claim.
- R3: Voyage rerank-2.5 API limits (batch size, tokens, rate) from the
  engine's JVM HTTP client; retry/ratelimit governor reuse (relates
  nexus-5g5ek's CCE concurrency memo).
- R4: Local-mode story — server-side cross-encoder (ONNX) parity or
  keep client-side cross-encoder for no-Voyage installs; decide where
  the local rerank lives post-migration.
- R5: Credential-chain retirement blast radius — every client surface
  that reads/plumbs/validates VOYAGE_API_KEY (config wizard, doctor,
  supervisor env pass, mode-lint heuristics `is_local_mode` legacy
  voyage-key clause) and the ordering of their removal.

## Proposed Solution

(Locked at acceptance; sketch:) Add a rerank stage to the engine's
search/query path (flagged per-request; model rerank-2.5 via the
engine-held key), repoint `scoring.rerank_results` to consume server-side
scores (or drop client rerank entirely where the endpoint composes it),
then retire the client Voyage credential chain in a follow-up phase once
no client code path reads the key. Engine floor bump delivers; the same
conexus release ships the client repoint.

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
  (latency + rate-limit exposure inside a request); needs the R3 governor
  answer and a bounded degrade (loud, per no-silent-fallbacks).
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
  tripwire test); mode-lint sweep for retired voyage heuristics.
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
