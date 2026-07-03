# Post-Mortem: RDR-178 — Unattended Migration

**Closed**: 2026-07-02 (implemented) · **Epic**: nexus-te885 (all §Approach items closed with evidence; cross-walk on the epic's 2026-07-02 close-out comment)

## Outcome vs design

All four pillars shipped as designed; the acceptance shape (nexus-te885.2) passed as a
composed corpus-scale scenario with both reviewers clean, plus a green containerized
`--hole-punch` e2e journey against the cold-acquired published engine. Full suite
11840/0; engine suite 1031/1031. Two engine releases (v0.1.18, v0.1.19) were cut,
published, cold-gated, cloud-deployed, and STEP-6-gated during execution.

## Divergences from the accepted text (all disclosed at the time)

1. **Vocabulary**: the RDR says `verification != passed`; no writer ever emitted
   "passed" — the orchestrator vocabulary is `verified|mismatch|indeterminate`. The
   composed wave-1 review caught doctor enforcing the phantom literal (a
   fail-loud-on-success Critical, fixed 5b98dcca). The RDR text was left as-written;
   implementation and tests pin the real vocabulary.
2. **RDR-177-stall fallback never used**: the design allowed a minimal store-local
   count endpoint if RDR-177 P1 hadn't landed. Code exploration found the deployed
   `relation_counts` surface already sufficed — the decision was amended on
   nexus-s3dd4 (no draft-RDR pillar pulled forward, no throwaway endpoint built).
3. **Gap-8 scope correction**: "cross-substrate" verify-fill shipped Chroma-source
   legs only; the pg-source substrate (the te885.1 incident shape) was explicitly
   re-scoped to a standalone follow-up after the R5 critique flagged the framing as
   overstated. Disclosed on the bead, the epic, and here — not silent reduction.

## What the process caught (worth repeating)

- **The phase-review-gate caught real unfinished scope at close**: nexus-ekk4o
  (server-side delegation) sat open after 11 green children; it was implemented
  rather than deferred, and closed with live-cloud evidence (42/42 collections
  eligible, /version probe).
- **The e2e journey caught 3 product bugs that 6 stacked reviews and ~60 unit tests
  missed**: the aspects-ETL dropped-column crash (a real upgrade blocker), the
  empty-prefilter-as-proof-of-absence over-fill under cross-model collection
  renames, and (earlier, defensively) the chash-width normalization. Unit fixtures
  encode the author's model of the boundary; the journey runs the real one.
- **Stacked reviews produced 4 fix-now Criticals/Highs across the arc** (doctor
  vocabulary, telemetry 2/6-table parity skip, ManifestSource dataclass mismatch,
  P4 import-cycle trap) — every one at a handshake boundary, none visible in green
  tests.

## Standing residuals (standalone beads, not RDR-178 scope)

nexus-te885.4 (report-selection rule), .5 (batch count semantics), .6 (existing_ids
tri-state + probe retry — the acceptance test deliberately pins current behavior and
must flip when it lands), .7 (fan-in confirmation), .8 (pg-source reconcile), .10
(telemetry cheap no-op gate), nexus-f4uyj (MCP file_path search), telemetry
single-store CLI delta routing, conexus-b3rs (their parity-oracle re-baseline).
