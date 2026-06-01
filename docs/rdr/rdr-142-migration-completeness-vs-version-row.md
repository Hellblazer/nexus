---
title: "Migration Completeness vs the Version Row: apply_pending Advances `_nexus_version` While Deferred/Gated Steps Remain"
id: RDR-142
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-01
related_issues: [nexus-6e6u1]
related_rdrs: [RDR-108, RDR-076, RDR-096]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-142: Migration Completeness vs the Version Row: apply_pending Advances `_nexus_version` While Deferred/Gated Steps Remain

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

#### Gap 1: the schema-version row and the set of actually-applied migration steps can disagree, so "no pending migrations" is a lie when a deferred/gated step remains

`nx upgrade --dry-run` reported `Up to date (v5.6.0). No pending migrations.` Yet restarting the T2 daemon ran `bootstrap_schema → apply_pending → migrate_document_aspects_pk_to_doc_id` (RDR-108 Phase 1c) and tripped `_check_high_volume_orphans`, raising `MigrationError` and preventing the daemon from starting (issue #1061 finding E2). The `_nexus_version` gate (`cli_version == stored_row`) and the daemon's bootstrap `apply_pending` **disagree on whether migrations are complete.**

The root behaviour: `apply_pending` advances the stored `_nexus_version` row toward the current version, but some migration steps do not actually complete on that pass — they either:

- **Defer** (`MigrationRetry`): RDR-108 Phase 1c's PK migrations (`migrate_document_aspects_pk_to_doc_id`, `migrate_aspect_extraction_queue_pk_to_doc_id`) skip when the catalog `.catalog.db` is absent and retry on a later bootstrap (logged `migration_step_skipped … reason='catalog absent'` / `migration_skipped_not_marking_done`).
- **Gate** (`MigrationError`): the same PK migration raises when a collection has high-volume unmapped orphans, requiring operator curation (`NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD`) before it can complete.

When the version row advances past (or independently of) a step that deferred, `nx upgrade --dry-run`'s "pending == cli_version != row" check sees nothing pending, while the next `apply_pending` on daemon bootstrap re-attempts the deferred step and can crash the daemon. The 5.6.2 hotfix (#1061 E2, PR #1065) added `_check_deferred_migrations` so `--dry-run` *reports* the two known deferred conditions — but that is **detection of specific conditions, not a structural fix.** A new deferred/gated migration added later would silently reintroduce the "no pending" lie for its own step (whack-a-mole), and the version-row-vs-applied-steps disagreement remains the underlying defect.

## Context

- **RDR-076** established the idempotent upgrade mechanism and the `_nexus_version` gate.
- **RDR-108 Phase 1c** introduced the deferring/gating PK migrations (`migrate_document_aspects_pk_to_doc_id`, `migrate_aspect_extraction_queue_pk_to_doc_id`) that defer on catalog-absent and gate on high-volume orphans.
- **#1061 E2 (5.6.2)** shipped `_check_deferred_migrations` in `src/nexus/commands/upgrade.py` — honest reporting of the two known deferred conditions in `--dry-run`, explicitly scoped as reporting-only with this RDR as the named architectural follow-up.

Relevant code:
- `src/nexus/db/migrations.py` — `apply_pending`, the version-row stamping, `MigrationRetry`, `MigrationError`, `migration_skipped_not_marking_done`, `_check_high_volume_orphans`, `_catalog_db_path_from_conn`.
- `src/nexus/commands/upgrade.py` — the `--dry-run` "no pending" computation and the `_check_deferred_migrations` probe (5.6.2 reporting stopgap).
- T2 `nexus/project_release_5_6_2` (the hotfix record naming this follow-up).

## Research Findings

_To be populated via `/conexus:rdr-research`. Key questions to resolve:_

1. **Where exactly does the version row advance relative to a deferred step?** Does `apply_pending` stamp the row per-step (and a deferred step is skipped-not-stamped, so the row should NOT advance past it) or once at the end (advancing past deferred steps)? The `migration_skipped_not_marking_done` event suggests per-path stamping awareness exists — characterise precisely what it does and does not protect.
2. **Is the disagreement a stamping bug or an inherent property?** If `apply_pending` already declines to mark-done when a step defers, why does `--dry-run` still report "no pending"? (Likely the dry-run "pending" computation keys on `cli_version != row` rather than "are there steps apply_pending would still run".)
3. **Enumerate all deferring/gating steps**, current and structural (catalog-absent retry, high-volume-orphan gate, any future ALTER-pattern PK migration).

## Proposed Solution

_Draft — to be locked after research. Candidate directions (decide one):_

- **A. Make `--dry-run` ask `apply_pending` itself (dry/no-op mode).** Instead of `cli_version != row`, have the dry-run path invoke the same step-resolution `apply_pending` uses and report any step that *would run or defer*. Single source of truth; no per-condition probe. The `_check_deferred_migrations` stopgap is then deleted.
- **B. Persist a "deferred steps" set.** Record which steps deferred (vs completed) in a small table; the version row advances only when the deferred set is empty; `--dry-run` reads the set. Generalises beyond the two known conditions.
- **C. Do not advance the version row past a deferred step.** Tighten the stamping so the row never moves ahead of the highest fully-applied step; the gate (`cli_version != row`) then correctly reports pending. Smallest conceptual change if stamping is the actual bug.

Direction C is the cleanest if research confirms the row is advancing incorrectly; A is the most robust against future deferred steps; B is the heaviest. The 5.6.2 `_check_deferred_migrations` probe should be removed or subsumed by whichever lands (it is a documented stopgap, not permanent surface).

## Implementation Plan

_To be detailed after the direction is locked. Must include: removal/subsumption of the 5.6.2 `_check_deferred_migrations` stopgap; a regression test that a deferred (catalog-absent) AND a gated (high-volume-orphan) migration are both reported by `--dry-run` AND that the version row does not falsely report complete; a guard against the whack-a-mole class (a new deferred step is covered by construction, not by adding another hardcoded probe)._

## Trade-offs

- Direction A/C change core migration-gate semantics — must not regress the idempotent-upgrade guarantees (RDR-076) or the bootstrap retry behaviour (a genuinely catalog-absent environment must still be allowed to start and retry later, not hard-block the daemon).
- Removing the `_check_deferred_migrations` stopgap is desirable (no duplicate sources of truth) but must be sequenced so `--dry-run` honesty is never worse than 5.6.2 at any commit.

## Alternatives Considered

- **Keep the 5.6.2 per-condition probe and just add new probes as deferred steps appear.** Rejected as the explicit non-goal: it is the whack-a-mole pattern this RDR exists to end.

## Critical Assumptions

- **CA-1**: The disagreement is reproducible — a DB at `cli_version` with a deferred (catalog-absent) RDR-108 Phase 1c PK migration reports "no pending" via `--dry-run` while `apply_pending` on bootstrap still attempts the step. [from #1061 E2 evidence; re-confirm against current code]
- **CA-2**: `apply_pending`'s step resolution can be invoked in a dry/no-op mode (Direction A) without side effects, OR the version-row stamping can be made to not advance past a deferred step (Direction C) without breaking RDR-076 idempotency.
- **CA-3**: A genuinely catalog-absent environment (legitimate, e.g. fresh install before first catalog write) must still bootstrap and defer gracefully — the fix must not convert a benign defer into a hard daemon-start failure.

## Finalization Gate

_Pending. Run `/conexus:rdr-gate` after research verifies CA-1..CA-3._

## References

- RDR-108 (graph identity normalization; Phase 1c PK migrations), RDR-076 (idempotent upgrade), RDR-096 (URI source identity; source_path drop).
- Issue #1061 finding E2; PR #1065 (5.6.2 `_check_deferred_migrations` reporting stopgap).
- T2: `nexus/project_release_5_6_2`.
- Code: `src/nexus/db/migrations.py` (apply_pending, version stamping, MigrationRetry/Error), `src/nexus/commands/upgrade.py` (`_check_deferred_migrations`).

## Revision History

- 2026-06-01: Draft. Filed as the architectural follow-up named by the 5.6.2 #1061 E2 hotfix (which shipped reporting-only). Direction to be locked after research (A: dry-run delegates to apply_pending / B: persisted deferred-set / C: don't advance the row past a deferred step).
