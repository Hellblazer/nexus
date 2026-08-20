# RDR-195 Post-Mortem — Token-Aware Voyage Batch Splitting

Closed: 2026-08-19 (implemented). Shipped as conexus 7.12.0 paired with
engine-service-v0.1.83 (deploy fired at client-tag push; zero refusal window).

## What was planned vs what shipped

Shipped as designed, three phases, no scope reduction: Phase 1 client
(byte-budget paging in `upsert_chunks`, 422 detail rendering), Phase 2 engine
(token-aware sub-batch planner with per-model budgets, typed
`TOO_MANY_TOKENS_IN_BATCH` + adaptive halving above the retry loop,
per-planned-batch sub-request cap failing loud, structured 422, per-sub-request
instrumentation), Phase 3 paired release. Phase-3 gate PASSED
(Item1=nexus-kmtlp.8 engine, Item2=nexus-kmtlp.3 client).

## Evidence it works

- MVV on laravel/framework (stand-in large repo): 0 `TOO_MANY_TOKENS_IN_BATCH`,
  0 429s, 0 splits needed at the planned budgets, 74 requests / 78 pages
  (T2 `nexus/rdr-195-mvv-measurements`).
- Post-deploy cloud gate (conexus-54, run 2): zero failures, recall 12/12
  exact==HNSW, Voyage probe byte-identical pre/post (~4.4e-7) — the splitting
  changed no embeddings (batch-partition invariance, 195-research-3, held in
  production).
- Published-bytes package-upgrade MVV: real prior-release box converged to the
  paired engine.

## What went well

- The paired-release choreography executed cleanly end-to-end on its first
  fully mechanized run (`--paired-deploy` pre-tag, deploy relay at client-tag
  push, bare-floor verify post-deploy).
- Research phase spikes (batch-partition invariance, 422 wire shape) settled
  the two riskiest design questions before implementation started.

## Friction worth remembering

- One test-vs-source drift reached the release battery: d9b917fb2 appended
  grants-nexus-diag-4 without moving the block-count pin in
  `tests/db/test_nexus_diag_role.py` (integration-only test, invisible to the
  unit suite). Caught by local-service-gate, fixed on the release branch
  (c14971624).
- Docker contention (concurrent `--acquire` + local-service-gate) manufactured
  4 transient container-startup errors and cost a triage cycle — recorded as
  the serialize-Docker-gates rule.
