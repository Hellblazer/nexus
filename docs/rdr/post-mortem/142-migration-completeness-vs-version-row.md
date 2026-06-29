# Post-Mortem: RDR-142 Migration Completeness vs the Version Row

## RDR Summary

`nx upgrade --dry-run` could report "no pending migrations" while the next daemon start
would still re-attempt a deferred/gated migration step — because the dry-run pending
computation was a pure version-range filter (`introduced > last_seen`) that never consulted
`apply_pending`'s actual per-step preconditions. The 5.6.2 `_check_deferred_migrations`
stopgap hardcoded two conditions on two tables (whack-a-mole). RDR-142 locked **Direction A**:
extract a read-only step-resolver that runs each eligible step's precondition checks (no DDL,
no row writes) and classifies would-succeed / would-defer / would-gate, then report `--dry-run`
from it and delete the stopgap.

## Implementation Status

**Implemented.** Shipped to `develop` across two phases (merges `88bfebc6`, `9307565a`,
`c1e78109`). Stacked review (code-review-expert + substantive-critic) returned 0 Critical on
both implementation phases; full suite 11408 passed. Phase-review-gate PASSED. The single
adjacent item (daemon-bootstrap gate-crash hardening) was explicitly scoped out and remains
tracked in `nexus-3lbhb`.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- **Read-only resolver (Direction A)**: `resolve_pending_steps` + `StepOutcome` /
  `PreconditionVerdict` / `StepResolution` + an optional `precondition` on the `Migration`
  dataclass. Per-step classifiers cover all **7** defer/gate sites (the stopgap covered 4 on
  2 tables), so coverage generalizes by construction.
- **Anti-drift**: the gate cutoff + message helpers are shared between the real
  `_check_high_volume_orphans` and the classifier; agreement tests assert the resolver verdict
  equals the real `apply_pending` outcome on identical fixtures.
- **Dry-run rewire + stopgap deletion**: `nx upgrade --dry-run` reports from the resolver;
  `_check_deferred_migrations` deleted (grep-clean), sequenced so honesty was never worse than
  5.6.2 at any commit.
- **Non-vacuous regression tests**: the gated-path tests reach the orphan/undrained/bad-uri
  branches (not the 5.6.2 catalog-absent early-return vacuousness the RDR warned against).

### What Diverged from the Plan

- **`resolve_blocking_steps` was added beyond the planned `resolve_pending_steps`.** The plan
  said dry-run reports the version-eligible set. But the deleted stopgap probed *table state*
  regardless of the version gate, and the bead's "coverage-not-worse" constraint required
  preserving that. So the dry-run uses `resolve_blocking_steps` = the eligible set UNION
  precondition-bearing steps whose table is incomplete even though the version gate passed.
- **Output had to distinguish two failure classes (caught at P2.1 review).** A version-gate-
  passed step is NOT in `apply_pending`'s eligible set, so labelling it "would gate on next
  start" was false (the daemon won't run it; the impact is runtime query errors). The critic
  caught this; the output now separates "eligible (will run)" from a "Table-state checks"
  section, and renders the undrained-queue gate as informational ("apply_pending drains
  first") rather than a hard BLOCKED alarm.

### What Was Added Beyond the Plan

- An orphan-prediction read-only mirror of the two-pass catalog backfill (Pass-1 direct +
  Pass-2 `superseded_by` hop) via `NOT EXISTS`, with a defensive fallback to a simple
  unmapped-row count (and a structured warning) when the catalog schema is unusable — so the
  read-only dry-run never crashes on a corrupt catalog.
- The `last_seen` override on `resolve_pending_steps`, so `--force --dry-run` previews a full
  re-migration.

### What Was Planned but Not Implemented (deferred by design)

- The adjacent **daemon-bootstrap gate-crash hardening** (degrade-not-crash on an uncaught
  `MigrationError`) was explicitly flagged as separable in §Proposed Solution and tracked in
  `nexus-3lbhb`. Not part of the dry-run-honesty core; left open.

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Missing failure mode** | 1 | the version-gate-passed-but-incomplete-table state — the plan's "report the eligible set" missed that the stopgap also covered below-version table state; surfaced as the coverage-not-worse tension | Yes — enumerate the stopgap's exact coverage before specifying the replacement |
| **Under-specified architecture** | 1 | output messaging: the plan said "report from the resolver" but not how to frame eligible vs table-state steps; the wrong framing (false "next-start crash") was caught at review | Partly — output semantics are easy to leave to implementation; a one-line "distinguish will-run from already-passed" in the plan would have pre-empted it |

Both divergences are the same root: replacing a table-state probe (the stopgap) with a
version-gated resolver (the plan) left a coverage seam that "coverage-not-worse" forced back
in, and the reunion needed careful output framing. The stacked reviewers caught the framing
issue before it shipped.

---

## RDR Quality Assessment

### What the RDR Got Right

- The **root-cause research (CA-1)** was decisive and correct: the lie was in the dry-run
  computation, not row-stamping. This killed two tempting wrong directions (B: persist a
  deferred-set; C: don't advance the row) with evidence, saving real effort.
- The **"NOT a version-range filter with a flag" critical note** correctly anticipated the
  failure mode of a naive implementation.
- **Sequencing discipline** ("honesty never worse than 5.6.2 at any commit") and the
  **non-vacuous-test warning** (don't inherit the catalog-absent early-return) were both
  concrete and both honoured.
- **Separating the adjacent gate-crash hardening** into its own bead prevented scope creep.

### What the RDR Missed

- It under-specified that the stopgap's *table-state* coverage (below the version gate) had to
  be preserved — the "coverage-not-worse" constraint lived in the bead, not the RDR's §Plan,
  and only surfaced during implementation as the `resolve_blocking_steps` addition.
- It said nothing about how to frame the two failure classes in operator output, which is
  where the only review-caught Significant issues landed.

---

## Key Takeaways for RDR Process Improvement

1. **When replacing a stopgap, enumerate its exact coverage in the §Plan, not just its
   mechanism.** RDR-142 described the stopgap as "4 hardcoded conditions" but the §Plan
   specified the replacement in terms of the *version-eligible set* — a different shape that
   silently dropped the stopgap's below-version table-state coverage until the bead's
   "coverage-not-worse" rule forced it back. A "replacement must cover X, Y, Z that the
   stopgap covered" checklist belongs in the RDR.
2. **Operator-facing output framing is part of the design, not an implementation detail.** The
   only Significant review findings were about labels ("would gate on next start" for a step
   that won't run). One sentence of intended framing in the §Plan would have pre-empted them.
3. **Adversarial agreement tests are the right anti-drift mechanism for "two code paths must
   agree."** The resolver predicts (read-only) what the real migration does (read-write); a
   test that runs BOTH on identical fixtures and asserts the verdicts match is stronger than
   trying to share literal code across an inherently asymmetric pair.
4. **A locked root-cause (CA-1) earns its cost.** Rejecting Directions B and C up front with
   verified evidence meant zero wasted implementation on a tracking table or a per-step
   watermark — the kind of speculative machinery that's expensive to build and unbuild.
