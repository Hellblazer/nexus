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

The 2026-07-01 production consolidation (SQLite + ChromaCloud → `api.conexus-nexus.com`, 8 T2 stores + 123,883 vector chunks) **succeeded — but only because an operator babysat it for ~6 hours.** Every intervention below was required; a user running the same migration unattended would have ended with silent holes, stuck processes, or an aborted run:

1. **A silent month-old failure started it.** The 2026-06-30 `migrate all` crashed 6/8 stores (version-skew `ModuleNotFoundError`s), stamped `verification: indeterminate`, and NOTHING flagged it for a month (nexus-aigpt, nexus-5drgy).
2. **Writes diverged post-migration with no warning** — 68 SQLite memory rows accumulated after the store had "moved" (nexus-14ndm).
3. **A ~10s ingress blip permanently failed ~21 batches** — the transient-retry ladder does not cover 502 (nexus-ob4vc); recovery required a full-store re-send because no delta mode exists (nexus-s3dd4).
4. **Throughput required forensics.** The 1-request/s ceiling was diagnosed via client-side packet accounting; root cause was the server un-batching batch requests into 600+ sequential PG round-trips (`ChashHandler`, fixed `f0ab406f`; sibling handlers still un-batch — nexus-1usso).
5. **The efficient vector path needed hand-rolled orchestration.** `migrate vectors --cloud` trombones every chunk through the client (nexus-ekk4o); the server-side `ingest-cloud` (RDR-176 P4) is 2x faster with zero client bandwidth but is synchronous-only — collections >~6k chunks outlive the nginx proxy timeout and 504 while the copy continues detached, forcing a fire-and-absorb-504-then-poll-counts hack (nexus-melvx; predicted pre-incident by nexus-1utk3, closed as duplicate).
6. **Known-derived data blocks a clean report** — `taxonomy__centroids` cannot dim-dispatch and leaves every vector run "NOT clean" (nexus-t0p7o).
7. **Re-runs are not recognized as no-ops** (nexus-1sx01).

## Context

RDR-153 defined the data-quality/report contract; RDR-176 built batched ETL, retry, auth, and observability *gaps 1-7*; RDR-159 made guided-upgrade survivable. Each closed the failures of its own incident. This RDR closes the failures of the first **full-corpus production run** — the class RDR-176's gates (5-chunk `ingest_cloud_gate`) could not surface. RDR-177 (stats layer) is the observability substrate several children consume (job status, per-store counts for verify).

## Proposed Approach (the epic's children ARE the work items — nexus-te885)

**Pillar A — Fail loud, never silently (the month-of-silence class):**
- nexus-aigpt: `nx doctor` reads the latest migration report; `total_failed>0` or `verification != passed` is a loud failure.
- nexus-5drgy: pre-flight import of every store ETL — all-runnable or not-started.
- nexus-14ndm: post-migration local-write divergence tripwire.

**Pillar B — Survive transients autonomously:**
- nexus-ob4vc: 502/503/504 in the retry ladder with backoff + circuit-breaker pause.
- nexus-s3dd4: verify-fill delta mode — patch holes by set-difference, not full re-send.

**Pillar C — Server-side efficiency as the default path:**
- nexus-1usso: multi-row batch imports on the remaining handlers (memory, taxonomy, catalog, aspects) — chash exemplar shipped `f0ab406f`.
- nexus-melvx: ingest-cloud async job semantics (202 + job id, bounded executor, PG-backed job status, idempotent re-POST) — kills the fire-and-poll hack.
- nexus-ekk4o: `migrate vectors --cloud` delegates to ingest-cloud when the engine supports it; client path becomes the fallback.

**Pillar D — Clean-run hygiene:**
- nexus-t0p7o: derived-collection policy (`taxonomy__centroids`) so a complete run reports clean.
- nexus-1sx01: already-migrated detection → true no-op re-run.

**Acceptance shape:** a fresh `nx storage migrate all && nx storage migrate vectors --cloud` on a corpus of tonight's scale completes unattended, reports clean, survives a mid-run ingress blip, and a re-run is a fast no-op. The big-collection ingest gate variant (in nexus-melvx) pins the 504 class.

## Alternatives Considered

1. **Keep the operator runbook.** Rejected: the managed-service journey (RDR-166) promises migration to users who do not have tonight's operator.
2. **One mega-fix in the ETL client only.** Rejected: half the failures are server-side (un-batching, sync-only ingest, no job state); client-side patches would re-create the hack layer.
3. **Fold into RDR-177.** Rejected: 177 is the measurement substrate; this is migration-path behavior. 177's surfaces are consumed here (job status, verify counts) — related, not identical.

## Consequences

**Positive:** the next migration — any user's — runs unattended; every hack from 2026-07-01 becomes a regression test; the engine cut carrying Pillar C makes re-runs ~20-100x faster.
**Negative/risks:** async job machinery adds engine state (jobs table, executor) that must itself be observable (RDR-177 dependency); ten children need sequencing discipline to avoid another RDR-110-scale sprawl — the pillars are the phase boundaries.

## Open Questions

- OQ-1: Should `migrate all` itself move server-side (engine pulls from an uploaded SQLite snapshot) rather than incrementally hardening the client-driven path? (Radical simplification; defer unless Pillars A-C prove insufficient.)
- OQ-2: Job-status surface — dedicated endpoint vs a row in RDR-177's `stats/tenant`? Decide with 177's P1.
- OQ-3: nginx proxy timeout — should conexus raise it for `/v1/migration/*` as belt-and-braces even after async lands? (Relay at planning.)
