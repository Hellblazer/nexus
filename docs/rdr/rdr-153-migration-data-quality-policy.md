---
title: "RDR-153: Migration Data-Quality Policy and Structured Issue Reporting for the SQLite→Postgres T2 Migration"
id: RDR-153
type: Feature
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-08
accepted_date: 2026-06-08
related_issues: [nexus-sa14p, nexus-9sjn3, nexus-r0esi, nexus-f1m8s]
related_tests: []
related: [RDR-152, RDR-101, RDR-108]
---

# RDR-153: Migration Data-Quality Policy and Structured Issue Reporting

## Problem Statement

RDR-152 replaces the SQLite single-writer T2 stack with a Postgres + Java service. The migration runs **with strict foreign-key constraints deliberately** — the constraints are the diagnostic. A full data-state audit of prod T2 (T2 memory `nexus_rdr/152-DATA-STATE-AUDIT`, 2026-06-08) ran every store through the constrained migration and surfaced significant accumulated referential drift that SQLite (enforcing no FKs) had hidden for months.

We are greenfield (zero prod deployments), so the goal is not to preserve a deployed schema, it is to **move forward cleanly while capturing exactly what was wrong**. Today the ETL reacts to bad rows ad-hoc (per-row error logs, an opaque skip count). That is insufficient: there is no machine-readable record of what was noted, handled, or failed, so we cannot triage, recover, or learn from a migration run.

### Evidence (the audit)

Clean and migrating ~100%: `memory` (2,621), `chash` (435,474), `plans` (89), `topics` (447, parent refs valid), telemetry events `relevance_log`/`search_telemetry`/`tier_writes`, `nx_answer_runs` (183, post-nexus-5gaj7), catalog `documents` (16,837) + `document_chunks` (129,073, 0 orphan) + `collections` (64).

Dirty (rejected by constraints):

| Table | Dirty / Total | Class |
|---|---|---|
| `topic_assignments` | 51,286 / 180,806 (28%) | orphan `topic_id` → deleted topic |
| `topic_links` | 11,103 / 17,638 (63%) | orphan `topic_id` → deleted topic |
| `document_aspects` | 675 / 675 (100%) | orphan `doc_id` → deleted-owner tumbler (stale, pre-rebuild space) |
| `aspect_extraction_queue` | 3 / 7 | same |
| `hook_failures` | 234 / 234 | timestamp format (`2026-04-23 10:47:54`, space not ISO-8601) |

Soft danglers (no FK enforced — import but reference missing parents): `nx_answer_runs.plan_id` 155/181 reference deleted plans; catalog `links` 273/1,719 (16%) reference missing docs.

Not separately surfaced as dirty in the audit table (`document_highlights`, `aspect_promotion_log`): no orphan rows were flagged for these. The strict-FK boundary plus the `failed` catch-all path is the backstop — any anomaly that the audit did not enumerate is caught at migrate time and recorded, never silently dropped.

### Three root classes (gaps)

#### Gap 1: Orphan-from-deleted-parent has no migration policy

Dominant class. Deleted topics/owners/plans left dependents dangling. `next_seq` high-water numbering never reuses ids, and SQLite enforced no FK, so danglers accumulated silently: ~62k taxonomy rows + 675 aspects + 155 runs. The ETL has no defined, recorded policy for these rows — it skips them with an opaque count.

#### Gap 2: Identity-mismatch FKs are discovered ad-hoc, not recorded

A correct FK was placed on the wrong identity — `topic_assignments.doc_id` is a chunk content-hash, not a document tumbler (resolved in nexus-sa14p by not registering `fk_ta_catalog_doc`). The schema correction happened but left no machine-readable trace in any migration artifact.

#### Gap 3: Format anomalies fail the migration instead of normalizing

`hook_failures.occurred_at` is space-separated, not ISO-8601 (the lone timestamp outlier). A parseable-but-non-canonical value should normalize, not reject; today there is no lenient-parse-then-canonicalize step and no record that normalization occurred.

#### Gap 4: No machine-readable record of what each run noted, handled, or failed

Across all classes, the ETL reacts per-row (error logs + a bare skip count). There is no structured, per-class, per-table artifact, so a run cannot be triaged, recovered from, or learned from.

## Decision

1. **Keep foreign keys strict.** We do not weaken the schema to force data in. The constraints are the integrity boundary and the diagnostic.

2. **Per-class handling policy:**

   | Class | Policy | Report action |
   |---|---|---|
   | Orphan-from-deleted-parent (FK-enforced) | **Skip-and-record.** Do not import; the row is garbage referencing a deleted parent. | `skipped` |
   | Identity mismatch (correct data, wrong FK) | **Resolve the schema** — don't register the wrong FK; the field is an opaque identity. | `schema_corrected` |
   | Format anomaly (parseable) | **Normalize in ETL** — parse lenient, emit canonical form (ISO-8601 for timestamps). Fail only if unparseable. | `handled` |
   | Soft dangler (no FK, missing non-enforced parent) | **Import-but-flag.** The row imports; record an advisory so the dangling reference is known. | `flagged` |
   | Unparseable / unexpected | **Fail-and-record** (never silently drop). | `failed` |

3. **Every migration run emits one structured JSON report** capturing all of the above — per store, per table, per class, with counts, sample ids, and the action taken. This `migration-report.json` is the triage / recovery / learning artifact and a Phase-4 gate input.

## JSON report schema

```json
{
  "schema_version": "1",
  "migration_id": "<uuid>",
  "started_at": "<iso8601>",
  "completed_at": "<iso8601>",
  "source": { "sqlite": "<path>", "catalog_db": "<path>" },
  "target": { "service_url": "<url>", "db_schema_version": "<liquibase id>" },
  "stores": [
    {
      "store": "taxonomy",
      "tables": [
        {
          "table": "topic_assignments",
          "read": 180806, "written": 129520,
          "skipped": 51286, "flagged": 0, "failed": 0,
          "issues": [
            {
              "class": "orphan_parent",
              "constraint": "topic_assignments.topic_id -> topics.id",
              "reason": "topic_id references a deleted topic",
              "action": "skipped",
              "severity": 3,
              "count": 51286,
              "sample_ids": ["<doc_id|topic_id>", "..."],
              "sample_truncated": true
            }
          ]
        }
      ]
    }
  ],
  "summary": {
    "total_read": 0, "total_written": 0,
    "total_skipped": 0, "total_flagged": 0, "total_failed": 0,
    "max_severity": 0,
    "by_action": { "skipped": 0, "handled": 0, "flagged": 0,
                   "schema_corrected": 0, "failed": 0 }
  }
}
```

- Two distinct enums, never mixed: each issue has a `class` (what is wrong) and an `action` (what the ETL did).
  - `class` ∈ {`orphan_parent`, `identity_mismatch`, `format_anomaly`, `soft_dangler`, `unexpected`}.
  - `action` ∈ {`skipped`, `handled`, `flagged`, `schema_corrected`, `failed`} mirrors the policy table.
  - `summary.by_action` aggregates by the five `action` values (not by class); this is the gate-facing rollup.
- `sample_ids` are capped (e.g. 200/issue) with `sample_truncated` set; the full set is reproducible by re-running. For composite-key tables the id is the key tuple joined with `:` (e.g. `topic_assignments` → `"<doc_id>:<topic_id>"`); the convention is recorded per table in the issue's `reason`.
- The report is self-describing and stable (`schema_version`) so downstream triage tooling can evolve independently.

## Approach (phased)

1. **Issue-record primitive.** A shared `MigrationIssue` dataclass + an in-run `IssueCollector` that each ETL writes to, replacing ad-hoc per-row error logging. Lives in a new `src/nexus/migration/` package (`migration_report.py`), **not** under `src/nexus/db/t2/` — RDR-152 Phase 4 deletes the entire `src/nexus/db/t2/` subtree, but the report primitive and the `migration-report show` reader must survive the SQLite decommission (the Phase-4 gate itself reads a report). The CLI surface lives in `src/nexus/commands/` alongside the other `nx storage` subcommands.
2. **Per-store ETL integration.** Each `migrate_*` function applies the policy and records issues. Every ETL wraps its per-row write in a catch-all that emits a `failed` issue for any unparseable/unexpected input (never a silent drop):
   - taxonomy: pre-check `topic_id`/`from`/`to_topic_id` against migrated topics → skip-and-record orphans; record the doc_id-is-chash schema correction once.
   - telemetry: normalize `hook_failures.occurred_at` (and any space-form timestamp) to ISO → record `handled`; record `nx_answer_runs.plan_id` danglers as `flagged`.
   - catalog: record orphan-endpoint `links` as `flagged`.
   - aspects: orphan doc_id (stale) → skip-and-record; `aspect_extraction_queue` orphans (3/7) follow the same skip-and-record policy, the 4 valid rows migrate. See Consequences for the aspects-at-cutover regression.
   - **Idempotent writes.** Every ETL write is `INSERT ... ON CONFLICT (<natural-key columns>) DO NOTHING` so a re-run after parent repair does not PK-violate on already-migrated rows. The conflict target is each table's natural key (e.g. `memory(project,title)`, `topic_assignments(doc_id,topic_id)`, `chash_index(chash)`). This is what makes the recovery path in Consequences real, not aspirational.
3. **Report emission + orchestration.** `nx storage migrate <store> --report <path>` writes the per-store report; `--report` defaults to `<config>/migration-reports/migration-<id>.json` when omitted. `nx storage migrate all --report <path>` runs stores in the **RDR-152 Phase 2 ladder order** (memory → plans → telemetry → taxonomy → aspects → chash → catalog last; catalog is graph-heavy and runs last per RDR-152) and merges per-store results into one document. Fixes the harness gap where verification silently skipped (nexus-r0esi).
4. **Triage surface.** `nx storage migration-report show <path>` summarizes by action and by severity (`max_severity` first); re-running after parent repair is idempotent (recovery path, per the ON CONFLICT contract above).

## Alternatives considered

- **Weaken FKs / import everything.** Rejected — destroys the diagnostic and carries garbage into Postgres.
- **Clean the source SQLite first (DELETE orphans), then migrate.** Rejected as the *primary* mechanism: it mutates prod, discards the learning, and the orphans recur until the *creation* paths are root-caused. The report makes an informed source cleanup possible later.
- **Silent skip (current nexus-0a7xc behavior).** Insufficient — a bare count is not triage-able; we need per-class records with ids.

## Consequences

- The migration becomes **self-documenting**: every run produces an auditable, machine-readable data-quality artifact.
- Skipped rows are **recoverable**: repair (or accept) the parent, re-run idempotently.
- The report is a **Phase-4 gate input** with a mechanically checkable criterion: destructive SQLite deletion may proceed only when `summary.total_failed == 0`. `failed` is reserved for unparseable/unexpected input (every expected-bad row lands in `skipped`/`flagged`/`handled`/`schema_corrected`), so `total_failed == 0` is exactly "nothing unexplained." `max_severity` gives the one-glance signal; the gate predicate is the `total_failed` count, not severity.
- It is the foundation for **root-causing the orphan-creation paths** (the topic/owner/plan deletion paths that don't cascade-clean dependents) — a follow-on, not in scope here.
- **Aspects (100% stale)** are flagged as a distinct outcome: they reference a pre-rebuild owner space and are not repairable by re-keying. Skip-and-record means the Postgres `document_aspects` table is **empty at cutover** — this is an **accepted known regression**: aspect-dependent read paths (the `operator_filter`/`operator_groupby` SQL fast path keyed on `document_aspects.confidence`, and `nx_answer` plan steps that filter on aspect columns) return empty until re-extraction runs. Re-extraction (not migration) is a tracked follow-on; the Phase-4 SQLite deletion does **not** depend on it (the stale rows carry no recoverable value), but production use of aspect queries does. A follow-on bead owns the re-extraction.

## Resolved questions

- **Report path: default, with override.** `--report` defaults to a path under the target config dir (e.g. `<config>/migration-reports/migration-<id>.json`); an explicit `--report <path>` overrides. Lower friction for the one-shot cutover; the run always produces an artifact even when the operator forgets to name one.
- **Severity rank: yes.** Each issue class carries an ordinal severity (`failed` > `skipped` > `flagged` > `handled` > `schema_corrected`) so the report has a single triage sort key. Carried in the JSON as a numeric `severity` on each issue and surfaced by `migration-report show`.
- **`nx storage migrate all`: build now.** Ship the dependency-ordered orchestrator that merges per-store results into one document, rather than per-store + a standalone merge step. It runs the RDR-152 Phase 2 ladder order (memory → plans → telemetry → taxonomy → aspects → chash → catalog last; see Approach step 3). It is the one-shot cutover's actual entry point and it closes the harness gap where verification silently skipped (nexus-r0esi).

### Severity ordinals

| Class action | `severity` |
|---|---|
| `failed` | 4 |
| `skipped` | 3 |
| `flagged` | 2 |
| `handled` | 1 |
| `schema_corrected` | 0 |

The JSON report's per-issue object gains a `severity` integer; the `summary` gains `max_severity` for a one-glance gate signal.
