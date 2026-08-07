# Post-mortem: RDR-158 — Retire the SQLite T2 Backend

**Closed 2026-08-07** (`close_reason: implemented`), epic `nexus-tdkg1`.
Accepted 2026-06-12; the retirement itself completed across June–August 2026
with the terminal deletion at `nexus-i711w` (P4, Stage 2), well before the
formal close. Close record: T2 `nexus_rdr/158`.

## What shipped

The PG service became the only T2 path in every mode. The eight-domain SQLite
stores, the local catalog, the SQLite schema/bootstrap, the reader stack, and
the client migration chain were deleted; every T2 domain store is an HTTP
client over the engine's PG tables (`T2Database` facade). SQLite survives
solely as a frozen migration *source* (read-only downgrade/troubleshooting
path) — that is in-scope by design, per the NO-SQLITE directive, not residue.

Terminal deletion (`nexus-i711w`, Stage 2 sub-stage B) also removed the
T2-daemon lifecycle class (`t2_daemon.py`, `catalog_write_shim`, spin guard,
discovery) — the daemon-era single-writer machinery RDR-152 replaced.

## What went well

- **Copy-not-move discipline held end to end**: the frozen SQLite source made
  every stage rehearsable and reversible; no data-loss incident across the
  entire retirement.
- **Per-commit stacked reviews carried the quality gate**: every deletion
  commit went through code-review-expert + substantive-critic to closure
  (burndown records, T2 21549–21593 range), catching stranded marker-bucketed
  tests (`nexus-sghyo` lesson) and vacuous-isolation risks before merge.

## What went badly — the lesson

**The RDR's own lifecycle lagged its implementation by weeks.** The phase-gate
beads (P3 review/critique, P4 phase-review-gate) were parked as `deferred`
rather than closed-with-evidence, the epic could not close over them, and the
RDR sat `accepted` long after the work shipped. Nobody noticed until the
2026-08-07 grooming sweep; the deferred gates were then adjudicated closed on
the per-commit review evidence (`nexus-02o82`) and the RDR closed the same day.

Rule extracted: **deferred gate beads rot — close gates with evidence at the
phase boundary, or the container epic and RDR silently stop reflecting
reality.** Sub-stage B's own deferred findings (the six-bead cleanup cohort:
`nexus-5uj6t`, `nexus-zmfan`, `nexus-2tdkx`, `nexus-pmag3`, `nexus-37jha`,
`nexus-vw7zk`) were likewise invisible-ready for a week until the same sweep
surfaced them; all six landed 2026-08-07 (`c3031b07`).

## Residuals

- `nexus-ejfgh` — retired-verb refusal wording still names RDR-158-deleted
  substrates in `catalog delete --help`.
- `nexus-1vt0b` — the SERVICE-catalog test-isolation guard the deleted
  local-isolation file used to provide has no replacement yet.
