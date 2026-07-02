---
title: "Unattended Migration: Industrialize the Store-Migration Path from Babysat to Autonomous"
id: RDR-178
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
created: 2026-07-01
related_issues: [nexus-te885, nexus-aigpt, nexus-5drgy, nexus-14ndm, nexus-ob4vc, nexus-melvx, nexus-1usso, nexus-ekk4o, nexus-s3dd4, nexus-t0p7o, nexus-1sx01]
related: [RDR-152, RDR-153, RDR-155, RDR-159, RDR-176, RDR-177]
---

# RDR-178: Unattended Migration

## Problem Statement

The 2026-07-01 production consolidation (SQLite + ChromaCloud → `api.conexus-nexus.com`, 8 T2 stores + 123,883 vector chunks) **succeeded — but only because an operator babysat it for ~6 hours.** Every gap below required a live intervention; a user running the same migration unattended would have ended with silent holes, stuck processes, or an aborted run.

#### Gap 1: Migration failures are silent — nothing reads the report

The 2026-06-30 `migrate all` crashed 6/8 stores (version-skew `ModuleNotFoundError`s), stamped `verification: indeterminate`, and NOTHING flagged it for a month. `nx doctor` never reads migration reports; a `total_failed=120` artifact sat unexamined (nexus-aigpt). The crash class itself — orchestrator importing ETL modules the installed wheel lacks — has no pre-flight guard, so stores fail AT THEIR TURN after earlier stores already wrote (nexus-5drgy).

#### Gap 2: Post-migration writes diverge with no warning

68 SQLite memory rows accumulated after the store had "moved" to the cloud — three diverging copies of the memory store with no tripwire anywhere (nexus-14ndm).

#### Gap 3: Transient ingress failures defeat the retry ladder and force full re-sends

A ~10s ingress blip permanently failed ~21 batches at ~3/s with no backoff — despite `_etl_with_retry` nominally covering 502 (RDR-176 Gap 6): the incident path bypassed it (nexus-ob4vc, re-scoped post-audit). Recovery then required re-sending the ENTIRE store because no verify-fill delta mode exists — 158k rows re-shipped to patch a 270-row hole (nexus-s3dd4).

#### Gap 4: The server un-batches batch imports — throughput requires forensics to diagnose

The 1-request/s ceiling was diagnosed via client-side packet accounting; root cause was the server expanding each 200-row batch request into 600+ sequential PG round-trips (`ChashHandler`, fixed `f0ab406f`). Sibling handlers carry the same disease at the repository layer even where batch endpoints exist (nexus-1usso, re-scoped post-audit).

#### Gap 5: The efficient server-side vector path is synchronous-only and un-orchestrated

`migrate vectors --cloud` trombones every chunk through the client (nexus-ekk4o); the server-side `ingest-cloud` (RDR-176 P4) is 2x faster with zero client bandwidth but collections >~60s of copy outlive the nginx proxy timeout and 504 at the edge while the copy continues DETACHED — no completion signal, no progress, no admission control; five collections needed fire-and-absorb-504-then-poll-counts (nexus-melvx; predicted pre-incident by nexus-1utk3, closed as duplicate).

#### Gap 6: Known-derived data blocks a clean report

`taxonomy__centroids` cannot dim-dispatch (non-four-segment name) and leaves every vector run permanently "NOT clean" even when all real content transferred (nexus-t0p7o).

#### Gap 7: Re-runs are not recognized as no-ops

Neither `guided-upgrade` nor the migrate legs detect already-migrated state; a re-run re-ships everything (nexus-1sx01).

#### Gap 8: Cross-substrate deltas have no owner — post-cutover local-pgvector writes existed nowhere else

27,283 chunks (two whole post-cutover collections + newer writes to a third) lived only in LOCAL pgvector — in neither the trychroma source nor the migrated corpus. Neither vector-ETL leg reads local pgvector, so the class was invisible to the tooling; it was found by a hand-rolled per-collection count spectrum and reconciled ad hoc via passthrough upsert (closed bead nexus-te885.1). The generalized owner is nexus-s3dd4's verify-fill, whose scope explicitly extends to cross-substrate set-difference — not just same-store blip-patching.

## Context

RDR-153 defined the data-quality/report contract; RDR-176 closed ITS OWN seven gaps (batched ETL, non-mutation downgrade, config-first auth, route coverage, progress, retry, immutable source — distinct from this RDR's Gap numbering); RDR-159 made guided-upgrade survivable. Each closed the failures of its own incident. This RDR closes the failures of the first **full-corpus production run** — the class RDR-176's gates (5-chunk `ingest_cloud_gate`) could not surface. RDR-177 (stats layer) is the observability substrate several children consume (job status, per-store counts for verify).

## Proposed Approach (the epic's children ARE the work items — nexus-te885)

**Pillar A — Fail loud, never silently (the month-of-silence class):**
- nexus-aigpt: `nx doctor` reads the latest migration report; `total_failed>0` or `verification != passed` is a loud failure.
- nexus-5drgy: pre-flight import of every store ETL — all-runnable or not-started.
- nexus-14ndm: post-migration local-write divergence tripwire.

**Pillar B — Survive transients autonomously:**
- nexus-ob4vc (re-scoped post-audit): root-cause + close the 502-retry BYPASS — the ladder already covers 5xx (RDR-176 Gap 6) but the batch-import call sites didn't route through it; route them, add the circuit-breaker pause, pin with a fake-502-burst regression test.
- nexus-s3dd4: verify-fill delta mode — patch holes by set-difference, not full re-send. Count surfaces come from RDR-177 P1 when available; **stall fallback**: if 177 P1 has not landed by Wave 2, s3dd4 builds a minimal store-local count endpoint and swaps to the unified surface later — the epic never serializes on a draft RDR. Scope includes cross-substrate reconciliation (Gap 8), not just same-store blip-patching.

**Pillar C — Server-side efficiency as the default path:**
- nexus-1usso (re-scoped post-audit), two distinct sub-items: (a) NEW batch endpoints for the Catalog + Aspect import paths (the only handlers lacking them); (b) repository-layer multi-row conversion for the EXISTING importBatch handlers (memory/taxonomy/telemetry/plans have endpoints but their repositories still loop per-row `.execute()`). Chash exemplar shipped `f0ab406f`.
- nexus-melvx: ingest-cloud async job semantics (202 + job id, bounded executor, PG-backed job status, idempotent re-POST) — kills the fire-and-poll hack.
- nexus-ekk4o: `migrate vectors --cloud` delegates to ingest-cloud when the engine supports it; client path becomes the fallback.

**Pillar D — Clean-run hygiene:**
- nexus-t0p7o: derived-collection policy (`taxonomy__centroids`) so a complete run reports clean.
- nexus-1sx01: already-migrated detection → true no-op re-run.

**Acceptance shape (owned by nexus-te885.2, Wave 3, the epic-closing gate):** a fresh `nx storage migrate all && nx storage migrate vectors --cloud` on a corpus-scale fixture completes unattended, reports clean (`total_failed==0`, `verification==passed`), survives a mid-run injected 5xx blip with zero permanently-failed batches, and an immediate re-run is a fast no-op. The big-collection ingest gate variant (in nexus-melvx) pins the 504 class; nexus-te885.2 composes the whole scenario — the epic does not close on 11 green children alone.

## Alternatives Considered

1. **Keep the operator runbook.** Rejected: the managed-service journey (RDR-166) promises migration to users who do not have tonight's operator.
2. **One mega-fix in the ETL client only.** Rejected: half the failures are server-side (un-batching, sync-only ingest, no job state); client-side patches would re-create the hack layer.
3. **Fold into RDR-177.** Rejected: 177 is the measurement substrate; this is migration-path behavior. 177's surfaces are consumed here (job status, verify counts) — related, not identical.

## Consequences

**Positive:** the next migration — any user's — runs unattended; every hack from 2026-07-01 becomes a regression test; the engine cut carrying Pillar C makes re-runs ~20-100x faster.
**Negative/risks:** async job machinery adds engine state (jobs table, executor) that must itself be observable (RDR-177 dependency) and must NOT persist the client-supplied ChromaCloud credentials (RDR-176's test-enforced non-persistence decision extends to the jobs table); ten children need sequencing discipline to avoid another RDR-110-scale sprawl — the pillars are the phase boundaries. Accepted sunk-cost exposure: if OQ-1 (fully server-side migrate-all) is later adopted, parts of Wave 1's client-side hardening (retry routing, pre-flight import check) are obsoleted — accepted because incremental hardening ships user value regardless of OQ-1's resolution.

## Open Questions

- OQ-1: Should `migrate all` itself move server-side (engine pulls from an uploaded SQLite snapshot) rather than incrementally hardening the client-driven path? (Radical simplification; defer unless Pillars A-C prove insufficient.)
- OQ-2: Job-status surface — dedicated endpoint vs a row in RDR-177's `stats/tenant`? Decide with 177's P1.
- OQ-3: nginx proxy timeout — should conexus raise it for `/v1/migration/*` as belt-and-braces even after async lands? (Relay at planning.)
