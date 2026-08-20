# RDR-194 Post-Mortem — Post-RDR-187 FK Census and Structural Wart Retirement

Closed: 2026-08-20 (implemented). Shipped as conexus 7.13.0 paired with
engine-service-v0.1.84 (deploy-FIRST choreography: the engine was deployed and
cloud-gated before the client cut — zero refusal window, zero inert window).

## What was planned vs what shipped

Nine decisions (D0–D9), eleven changesets (322→333), every §Decision item
accounted at the phase gate (T2 `nexus/rdr194-phase-gate-crosswalk`). Two
deviations, both recorded in-file as CORRECTIONs, neither silent:

- **P5b inverted from PK-widen to DROP.** The substantive critic established
  mid-review that `migration_jobs` had been dead since 2026-07-24 (handler +
  repository deleted at RDR-155 P4b; zero producers/consumers) — the planned
  tenant-keying would have hardened a table nobody uses, and its changeset
  header carried a false wire claim that D0.2 would have made permanent. Sam
  ruled DROP. The census-to-phase liveness gap (nobody re-checked the table
  between the 08-15 census and the phase) is the transferable lesson.
- **chash_alias.old_bytes comment omitted** — the table was dropped by the
  legacy-deletion arc one day after the RDR was drafted.

## Evidence it works

- Candidate-migration rehearsal: populated v0.1.83 store walked all 11
  changesets, delta exact, row invariants EXACT.
- Live deploy: every migration NOTICE exact-matched the pre-measured
  populations (plans ttl=0: exactly 2; every other arm 0; all four
  cross-tenant guards found zero, matching cloud-count-5). STEP-6 cloud gate
  PASS first run, zero advisories. cloud-count-3 deploy-window verify: zero
  unvalidated FKs.
- fk_census ground truths pin every new constraint against pg_catalog
  directly, with composite-conkey set equality (a same-named de-scoped FK
  cannot pass).

## What went well

- **Measure-then-migrate.** Every risky step was gated on a live-store count
  (cc1–cc6) taken before the changeset was allowed to ship, and every count
  was later confirmed exactly by the deploy's own NOTICEs.
- **The cc5 BYPASSRLS story.** The measurement was blocked (FORCE-RLS makes
  cross-tenant rows invisible to every reachable role — a vacuous zero would
  have passed the gate falsely); conexus refused to report the vacuous zero,
  measured via a diag/admin hybrid with exact coverage anchors, and the
  delivery gate (`check_rdr194_cc5_delivery_gate.py`, engine-release Step 3d)
  mechanized the measured-vs-vacuous distinction after review caught two
  false-accept regexes by executing the script.
- Stacked reviews earned their keep repeatedly: the dead-table ship-blocker,
  the D5 write-path gap (nexus-24rof), the `$ne:null` vacuity trap avoided in
  favor of `$gt:0`, a real production bug in `nx memory promote` caught by a
  call-site census before any test could see it.

## Friction worth remembering

- The wire-contract ledger's release-PR window: entries stay in Unshipped
  until the release tag exists, then move to Shipped — and `_sha_declared`'s
  v7.11.0 carve-out already handles the pre-tag PR. Two failed ledger edits
  here came from `str.index` matching prose mentions of the section headings;
  anchor on `\n## Heading\n`.
- Docker-lane contention produced one false battery red (skew-window lease
  missing); the isolated rerun discriminated environmental from real.
- Exit-code pipe-masking (`| tail` swallowing the gate's rc) recurred twice
  in one day despite a standing memory; the gate-into-file-then-read pattern
  is the only reliable shape.
