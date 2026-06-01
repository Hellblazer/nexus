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

_Verified 2026-06-01 (codebase-deep-analyzer; T2 `nexus_rdr/142-research-CA1-CA3`)._

**The root cause is (b) — the dry-run check, not (a) row-stamping. Row-stamping is already correct.**

1. **Row-stamping is correct; it never advances past a deferred/gated step.** `apply_pending` stamps `_nexus_version` **once at the end of the pass** (`migrations.py:2490-2495`), guarded by `if any_skipped: return` (`:2477-2483`). A `MigrationRetry` is caught (`:2457-2465`), sets `any_skipped=True`, and `continue`s — so the row is **not** stamped and the path is **not** marked done. A `MigrationError` is **uncaught** and propagates (no stamp). So the watermark logic does the right thing.
2. **The lie lives in the dry-run computation.** `upgrade.py:319-326` computes pending as a pure version-range filter (`introduced > last_seen AND introduced <= current`) — it **never consults `apply_pending`**. Failure sequence: a prior pass completed and stamped `row = current`; later the catalog is absent again, so `apply_pending` on bootstrap re-attempts and *defers* the RDR-108 Phase 1c PK steps — but dry-run sees `last_seen == current` → empty range → "no pending", blind to the steps `apply_pending` would still attempt. Cause **(b)**.
3. **Defer is non-fatal; gate crashes the daemon.** `MigrationRetry` (catalog-absent) → daemon boots and retries on every open until the catalog appears. `MigrationError` (high-volume orphans, undrained queue, NULL `source_uri`) → uncaught → crashes daemon bootstrap. #1061 E2's daemon crash was specifically the **gate**, not the defer.
4. **Defer/gate sites enumerated (7):** `document_aspects` PK (4.30.0; Retry on catalog-absent `:1900`, Error on orphans `:1579→1474`); `aspect_extraction_queue` PK (4.30.0; Retry `:1928`, Error on undrained queue `:1717`, Error on orphans `:1738`); `migrate_drop_source_path_column` (4.31.0; Retry on source_path-still-in-PK `:1328`, Error on NULL `source_uri` `:1304`). The fix must generalize across all of these, not hardcode the two #1061 E2 conditions.

## Proposed Solution

**Direction A — make `--dry-run` ask `apply_pending`'s own step-resolution (LOCKED by research).** Extract the step-filter loop (`migrations.py:2446-2448`) into a side-effect-free resolver that returns the steps `apply_pending` *would attempt on this connection now* (including ones that would defer/gate), and have `upgrade.py`'s dry-run report from that instead of the `last_seen != current` version-range filter (`:319-326`). Single source of truth; generalizes across all 7 defer/gate sites by construction. The 5.6.2 `_check_deferred_migrations` stopgap (`upgrade.py:33-153`) is then **deleted** (subsumed) — no more per-condition hardcoded probes.

**Rejected — Direction C (don't advance the row past a deferred step).** Research showed the row already does not advance past a deferred/gated step (`any_skipped: return`), so C addresses a non-bug. Worse, making the watermark per-step-persistent would mean the row never reaches `current` while any step legitimately defers (e.g. a long-lived catalog-absent install) → dry-run would list those steps as pending *forever* and every `apply_pending` would re-scan the full step range on every start. Counterproductive.

**Rejected — Direction B (persist a deferred-steps set).** Heavier (new tracking table) and unnecessary once A makes the dry-run consult the live resolver; the steps are already individually idempotent so no persistence of "applied-ness" beyond the existing watermark is needed.

**Adjacent (decide in the plan — possibly separate scope):** the `MigrationError` *gate* crashes daemon bootstrap (uncaught). A completeness fix could make the bootstrap path report-and-degrade (surface the gate loudly, let the daemon start in a known-degraded state) rather than hard-crash, instead of relying on the `NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD` env workaround. This is separable from the dry-run honesty fix (the core of this RDR) and may warrant its own phase or RDR; flagged so it isn't silently dropped.

## Implementation Plan

_To be detailed after the direction is locked. Must include: removal/subsumption of the 5.6.2 `_check_deferred_migrations` stopgap; a regression test that a deferred (catalog-absent) AND a gated (high-volume-orphan) migration are both reported by `--dry-run` AND that the version row does not falsely report complete; a guard against the whack-a-mole class (a new deferred step is covered by construction, not by adding another hardcoded probe)._

## Trade-offs

- Direction A/C change core migration-gate semantics — must not regress the idempotent-upgrade guarantees (RDR-076) or the bootstrap retry behaviour (a genuinely catalog-absent environment must still be allowed to start and retry later, not hard-block the daemon).
- Removing the `_check_deferred_migrations` stopgap is desirable (no duplicate sources of truth) but must be sequenced so `--dry-run` honesty is never worse than 5.6.2 at any commit.

## Alternatives Considered

- **Keep the 5.6.2 per-condition probe and just add new probes as deferred steps appear.** Rejected as the explicit non-goal: it is the whack-a-mole pattern this RDR exists to end.

## Critical Assumptions

_Verified 2026-06-01 (codebase-deep-analyzer)._

- **CA-1 — VERIFIED (root cause is (b))**: The disagreement is real and traced to the dry-run version-range filter (`upgrade.py:319-326`), not to row-stamping (which correctly does not advance on `any_skipped`/`MigrationError`, `migrations.py:2477-2495`). Dry-run reports "no pending" whenever `last_seen == current`, blind to steps `apply_pending` would re-attempt.
- **CA-2 — VERIFIED (Direction A feasible)**: `apply_pending` has no dry-run mode today, but its step-filter loop (`migrations.py:2446-2448`) is cleanly extractable to a side-effect-free resolver. Steps are individually idempotent (`PRAGMA`/`sqlite_master`/`_is_already_migrated` guards), so a resolver that lists would-run steps needs no DDL. Direction C is rejected (see §Proposed Solution) — it would regress, not help.
- **CA-3 — VERIFIED**: A catalog-absent defer is non-fatal — `MigrationRetry` is caught, the daemon boots and retries on every open. Only the `MigrationError` *gate* crashes bootstrap (uncaught). The dry-run fix (Direction A) does not change defer/gate runtime behaviour; the adjacent gate-crash hardening is tracked separately in §Proposed Solution.

## Finalization Gate

_Pending. Run `/conexus:rdr-gate` after research verifies CA-1..CA-3._

## References

- RDR-108 (graph identity normalization; Phase 1c PK migrations), RDR-076 (idempotent upgrade), RDR-096 (URI source identity; source_path drop).
- Issue #1061 finding E2; PR #1065 (5.6.2 `_check_deferred_migrations` reporting stopgap).
- T2: `nexus/project_release_5_6_2`.
- Code: `src/nexus/db/migrations.py` (apply_pending, version stamping, MigrationRetry/Error), `src/nexus/commands/upgrade.py` (`_check_deferred_migrations`).

## Revision History

- 2026-06-01: Draft. Filed as the architectural follow-up named by the 5.6.2 #1061 E2 hotfix (which shipped reporting-only).
- 2026-06-01: Research (CA-1..CA-3 verified, codebase-deep-analyzer). Root cause confirmed as the dry-run version-range filter, not row-stamping. **Direction A locked** (dry-run delegates to an extracted side-effect-free `apply_pending` step-resolver; delete the `_check_deferred_migrations` stopgap); **Directions B and C rejected** with reasoning. Adjacent gate-crashes-daemon hardening flagged as separable. Ready for gate.
