# Post-mortem: RDR-157 — End-User Distribution and Installation

**Closed:** 2026-06-16 (accepted 2026-06-14). Epic `nexus-vwvv5`.

## What shipped

- **P1 (CA-3, PR #1188):** pgvector CI gate. The reduced zonky bundle falsified Strategy A (initdb/pg_ctl/postgres only); pivoted to Strategy B — build a complete PG from source in `manylinux_2_28` (glibc 2.28) + pgvector. Five CI traps documented. aarch64 / mac-arm64 deferred to P3.
- **P3.2 (PR #1209):** local-distribution PG bundle — `pg_trgm` CA-3 assertion, rerun-idempotency, CA-2 size tripwire.
- **P4.1 (PR #1210):** `nx init --service` collapse + native-binary launch.
- **P4.2 (PR #1211):** release-sandbox `service` E2E, live-validated.
- **P5 (PR #1212):** handoff doc to conexus RDR-001 (`docs/rdr/rdr-157-handoff-to-conexus-rdr-001.md`).
- **Signing (PR #1213, `nexus-1odsm`):** keyless cosign signing of engine-service release assets (publisher half).

P3 + P4 phase-review-gates PASSED. All stacked reviews (code-review-expert + substantive-critic) reached 0 Critical after fixes.

## What was deliberately left open

These are intentional follow-ons, not unshipped core scope:

- `nexus-f9bgu` — Windows distribution (native-image + relocatable PG + windows pgvector `.dll`), release N+1.
- `nexus-1jt17` — consumer-half cosign *verification* (verify the engine-service signature before `docker run`); lives on the conexus RDR-001 side.
- `nexus-ykrhb` — productionize native-image for nexus-service (a separate "lean-in" track).
- `nexus-luxe6` — the standing release blocker (`develop` unreleasable since RDR-155 P4a). RDR-157's close satisfies only condition (a) of that blocker; (b) conexus RDR-001 orchestration, (c) xr7.8.9, and (d) the two-release deprecation window remain.

## Lessons

- **Falsify the cheap strategy first.** The zonky reduced-bundle spike killed Strategy A before sinking effort into a from-source build that turned out to be necessary anyway — but the spike was what made the pivot defensible rather than speculative.
- **Publisher/consumer split is a real boundary.** Cosign signing (publisher, engine side) and signature verification (consumer, conexus side) are separate halves with separate owners. The substantive-critic caught a false "tracked under nexus-w5v8j" claim on the consumer half; the correct tracking bead is `nexus-1jt17` under RDR-001.
- **Em-dash discipline slipped twice** in commit subjects/bodies this arc; grep drafts pre-commit.
