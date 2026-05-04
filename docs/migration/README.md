# Migration directory: HISTORICAL forensic records

Everything in this directory is a frozen engineering artifact from the RDR-101 event-sourced catalog migration (Phases 0-6, shipped April-May 2026). The migration is complete; the verbs these docs reference (`synthesize-log`, `t3-backfill-doc-id`, `repair-orphan-chunks`, `prune-deprecated-keys`, `migrate`) were retired in nexus-iftc (PR #496+#497).

## What's here

| File | What it is |
|------|------------|
| `rdr-101-e2e-validation.md` | 2026-05-01 sandbox e2e validation report (against the now-deleted `scripts/validate/rdr-101-migration-e2e.sh` harness). |
| `rdr-101-live-migration-postmortem.md` | First-run forensic post-mortem of the actual production cutover, captures what the synthetic harnesses missed. |
| `rdr-101-phase4-audit-2026-05-02.md` | Wiring audit prompted by the "everything looks like an orphan" observation; verifies which write paths emit `doc_id`. |
| `rdr-101-phase4-fire-chains-audit.md` | Catalogue of every consumer of `fire_post_document_hooks` that read `source_path`, with per-consumer migration disposition. |
| `rdr-101-phase4-reader-audit.md` | Six-key reader audit (which T3 metadata keys were dropped vs migrated vs kept under Phase 4). |

## What was deleted (and why)

- `rdr-101.md` (operator migration guide): the migration is done; the guide instructed operators to run retired verbs.
- `rdr-101-phase4-orphan-recovery.md` (operator runbook): same shape; remediation steps pointed at retired verbs.
- `scripts/validate/rdr-101-migration-e2e.sh` + `rdr-101-migration-e2e-scaled.sh` (validation harnesses): called retired verbs and could no longer execute.

## Where the current authoritative record lives

The RDR family + post-mortems are the lasting record:

- [`docs/rdr/rdr-101-catalog-t3-metadata-design.md`](../rdr/rdr-101-catalog-t3-metadata-design.md) (closed)
- [`docs/rdr/rdr-102-phase4-completion.md`](../rdr/rdr-102-phase4-completion.md) (closed)
- [`docs/rdr/rdr-103-catalog-collection-name-authority.md`](../rdr/rdr-103-catalog-collection-name-authority.md) (closed)
- [`docs/rdr/post-mortem/101-event-sourced-catalog-migration.md`](../rdr/post-mortem/101-event-sourced-catalog-migration.md)
- [`docs/rdr/post-mortem/102-rdr-101-phase4-completion.md`](../rdr/post-mortem/102-rdr-101-phase4-completion.md)
- [`docs/rdr/post-mortem/103-catalog-collection-name-authority.md`](../rdr/post-mortem/103-catalog-collection-name-authority.md)
