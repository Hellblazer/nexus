---
title: "RDR Process Validation"
id: RDR-001
type: architecture
status: draft
priority: high
author: Hal Hildebrand
created: 2026-02-27
related_issues: []
---

# RDR-001: RDR Process Validation

## Summary

Evaluates the RDR (Research Design Review) process as practiced across three design documents in a private pilot project. The process — draft, gate review (3 layers), fix, re-gate, accept, implement — caught 8 critical issues with zero findings the author rejected as false positives at the Critical level. Layer 3 (AI critique) is the engine. Layers 1 and 2 both failed to enforce their documented mandates. This RDR formalizes what worked, identifies gaps, and proposes specific fixes.

**Limitation**: All evidence is from a single-author, single-session context (3 RDRs, 7 gate rounds). Proposals may require adjustment for collaborative projects.

## Motivation

The nexus project provides RDR tooling (`/rdr-create`, `/rdr-gate`, `/rdr-research`, `/rdr-close`) but has never validated the process against real use. A pilot project ran three complete RDR lifecycles in a single session, providing the first empirical data on what the process catches, what it misses, and where the tooling falls short.

The question is not "does the process work?" — it clearly does. The question is: "what should be changed before recommending this process to other projects?"

## Evidence Base

### What Was Tested

Three RDRs covering namespace design, name lifecycle management, and type/instance separation in a Postgres-based semantic layer. Each went through the full lifecycle: draft → gate → fix → re-gate → accept → implement.

| RDR | Topic | Gate Rounds | Critical | Significant | Observations |
|-----|-------|-------------|----------|-------------|-------------|
| Pilot-001 | Namespace design | 3 (gate, re-gate, re-gate) | 3 (+1 elevated) | 3 | 4 |
| Pilot-002 | Name lifecycle | 2 (gate, re-gate) | 3 | 6 | 7 |
| Pilot-003 | Type/instance separation | 2 (gate, re-gate) | 1 | 4 | 6 |
| **Total** | | **7 rounds** | **8** | **13** | **17** |

**Counting methodology**: Unique issues across all gate rounds per RDR, including re-gate findings. Issues resolved at re-gate are counted once under their original severity.

### What Layer 3 Caught

Every critical issue was found by Layer 3 (AI critique via substantive-critic agent). Notable categories:

- **Doc/code contradictions**: The design document claimed case-insensitive behavior; the code had no case normalization. The lookup library is case-sensitive. Without the gate, lookups would silently fail for any name with uppercase letters.

- **Author's own factual errors**: The author's analysis of side-effect behavior in a convenience function was wrong. The gate caught the author making a factual error about their own code.

- **Semantic design bugs**: A deprecation mechanism targeted entity nodes instead of binding nodes, which would deprecate all names simultaneously — directly contradicting the rename use case that was the RDR's stated purpose. Required introducing a new concept (binding nodes) not in the original design.

- **Implementation plan completeness**: A metadata registration block referenced predicates that hadn't been created yet. The lookup would return NULL, and the block would silently insert NULL foreign keys. Caught by reviewing the implementation plan against the design.

### What Layer 2 Did Not Catch

Layer 2 (assumption audit via T2 research findings) was **never invoked** across all three RDRs. `/rdr-research` was never called. When Layer 2 ran during gate review, it printed "No research findings recorded" and moved on.

**Root cause**: The author forgot `/rdr-research` existed. This is a discoverability problem, not a process design problem. The tool was not surfaced to the user at the right moment in the workflow. Layer 2 wasn't dead because it's useless — it was dead because the tool wasn't discoverable.

### What Layer 1 Did Not Catch

Layer 1 (structural validation) caught nothing substantive in any RDR. It verifies that section headings exist and are non-empty — a formatting check, not a correctness check.

**Critical gap**: `workflow.md` line 84 specifies that Layer 1 should check "at least one research finding exists." If `/rdr-research` was never called, Layer 1 should have blocked the gate. It did not. Either the tooling does not enforce what `workflow.md` says it enforces, or the actual Layer 1 implementation diverges from the documented specification. This must be investigated before proposing Layer 2 changes.

## Design: Process Improvements

### P1. Hard-block close without gate pass (Critical)

**Problem**: One RDR was prematurely closed before re-gate. The close was then manually reverted. The `rdr-close` tool emits a warning but does not hard-block.

**Note**: `workflow.md` already states close "Requires status: Final." If this requirement existed and the premature close still happened, the tooling was not enforcing its documented mandate. P1 is therefore implementing an existing but un-enforced policy, not introducing new policy.

**Fix**: `rdr-close` must check that RDR status is `accepted` or `final` and refuse to close otherwise. Require `--force` to override.

### P2. Fix Layer 1 enforcement, then decide on Layer 2 (Critical)

**Problem**: Both Layer 1 and Layer 2 failed. Layer 1 should have blocked on absent research findings (per `workflow.md`) but didn't. Layer 2 was never invoked because the author didn't know `/rdr-research` existed.

**Prerequisites**: Before deciding Layer 2's fate, fix Layer 1's enforcement gap. Determine whether the tooling implements the `workflow.md` specification. If not, fix the tooling.

**Then, for Layer 2**:

**Option A — Enforce with discoverability**: Require at least one `/rdr-research` finding before `/rdr-gate` can run. Make `rdr-gate` print a clear prompt: "No research findings recorded. Run `/rdr-research add <id>` to record findings, or `--skip-research` to override." This addresses the discoverability problem directly.

**Option B — Merge into Layer 3**: Instruct the Layer 3 AI critic to explicitly enumerate each assumption in the design and state whether it is verified by evidence. Remove Layer 2 as a separate step.

**Failure mode analysis**:

| Dimension | Option A (Enforce) | Option B (Merge) |
|-----------|-------------------|-----------------|
| Auditability | Structured T2 records with classification (Verified/Documented/Assumed) and method (Source Search/Spike/Docs Only) | Free-form AI text, not systematically queryable |
| Stability | Data either exists or doesn't — stable across model versions | Prompt instruction can be silently deprioritized in long contexts |
| Author burden | Requires author to pre-populate findings | Automatic, no author action |
| Post-mortem integration | T2 records feed the drift classification directly | Free-form output doesn't map cleanly to drift categories |
| Bypass risk | `--skip-research` is trivially avoidable in solo context | AI can be given vague context that omits the instruction |

**Recommendation**: Option A with discoverability fix. The structured T2 records are more auditable and feed the post-mortem's drift classification. The `--skip-research` bypass is acceptable — it creates an explicit paper trail of the skip decision.

**Relationship to existing Layer 2 logic**: Option A supplements the existing assumption-audit behavior described in `workflow.md` (checking that Assumed findings have risk assessments). The discoverability prompt is a prerequisite gate — it ensures findings exist before the audit runs. The existing audit logic is retained unchanged.

### P3. Add Test Plan section to RDR template (Significant)

**Problem**: No RDR had a test plan section. Tests were added during implementation but the gate never evaluated whether tests cover the design.

**Fix**: Add `## Test Plan` to the RDR template. Gate Layer 3 should evaluate whether the test plan covers the design's edge cases and failure modes.

### P4. Add `reviewed-by` field to metadata (Significant)

**Problem**: All three RDRs were accepted on the strength of the AI gate review alone. No human approval record exists.

**Note on format divergence**: `templates.md` specifies markdown-list metadata (`## Metadata` section with `- **Key**: Value` items). The pilot project RDRs and this RDR use YAML frontmatter instead. This format divergence must be resolved as part of P4: either standardize on YAML frontmatter (update `templates.md`) or standardize on markdown-list (update this RDR and the pilot RDRs). The implementation plan assumes YAML frontmatter as the standard, since it is what the tooling's `parse_frontmatter()` function already supports.

**Fix**: Standardize on YAML frontmatter. Add `reviewed-by` field. For solo projects, `reviewed-by: self` is acceptable. For collaborative projects, require at least one reviewer other than the author. Update `templates.md` to specify YAML frontmatter as the metadata format.

### P5. Define the status model precisely (Significant)

**Problem**: One gate returned "Conditional Accept" — a status not in the lifecycle model. This led to ambiguity about whether implementation could proceed.

**Fix**: Define the full status lifecycle:

Gate outcomes (what the gate returns):
- **BLOCKED** — critical issues found, must fix and re-gate
- **PASSED** — no critical issues (may have significant/observations)

RDR statuses (what the document records):
- **Draft** — in progress, not yet gated
- **Accepted** — author/reviewer decision after gate passes
- **Implemented** — code matches accepted design
- **Reverted** — implemented but rolled back
- **Abandoned** — work stopped before implementation
- **Superseded** — replaced by a later RDR

The first three are the primary lifecycle. The last three are terminal states for RDRs that don't reach or stay at Implemented. The `rdr-close` command's close reasons (Implemented, Reverted, Abandoned, Superseded) map directly to the terminal statuses.

Remove "Conditional Accept" as a concept. The gate either blocks or passes. Acceptance is a separate decision by the author/reviewer after the gate passes.

### P6. Separate gate history from design body (Minor)

**Problem**: Gate findings appended to the RDR create long documents. By the second pilot RDR, there were three interleaved finding sections.

**Fix**: Move gate findings to a `## Revision History` appendix at the end of the document. Keep the design sections as the canonical current design without historical noise. Update the template to include the appendix section. Update `rdr-gate` instructions to direct findings to the appendix.

### P7. Cross-RDR dependency tracking (Minor)

**Problem**: No tooling checks whether an RDR contradicts or modifies a previous accepted RDR. In the pilot, later RDRs modified schema structures introduced by earlier ones.

**Fix**: During gate review, Layer 3 should be given the list of related RDRs (from `related_issues`) and check for consistency. This is a prompt engineering fix, not a tooling change.

## Severity Assessment of Pilot Gate Reviews

### Critical level: accurate

All 8 critical issues would have caused runtime failures, silently incorrect behavior, or fundamental semantic errors. No findings the author rejected as false positives at this level.

**Caveat**: In a solo-author context, the author decides which gate findings are valid. This assessment is not independently verified. P4 (`reviewed-by`) partially addresses this for future work.

### Significant level: slightly under-calibrated

Two issues classified as Significant arguably deserved Critical:

- A nullable column that would allow corrupting a deprecation graph (data integrity failure)
- A key/value metadata classification error that would mislead any tooling using catalog metadata (self-hosting correctness failure)

Both were caught and fixed before implementation. The classification mattered less than the catch — but for calibrating future gates, these represent the Significant/Critical boundary being too permissive.

### Observation level: one item under-classified

A character constraint in the naming library (`[A-Za-z0-9_]` labels only) was classified as an Observation but is a user-facing incompatibility that prevents real-world names with hyphens. Should be Significant with a required resolution.

**Retroactive remediation**: Under-classified issues in accepted RDRs should be addressed by spawning a new RDR (following the established pattern where later RDRs address deferred items from earlier ones).

## What the Process Does Not Cover

These are gaps in the RDR process, not bugs in any individual RDR:

1. **Performance analysis** — No RDR discussed performance implications.
2. **Rollback documentation** — No explicit position on rollback strategy.
3. **Post-implementation verification** — The process ends at acceptance. No gate runs on the implementation to verify it matches the design.
4. **Security review** — No security analysis was done on any RDR.

## Industry Comparison

| Feature | RDR Process | ADRs (Nygard) | Google Design Docs | Rust RFCs |
|---------|-------------|---------------|---------------------|-----------|
| Gate review | 3-layer automated | None (just record) | Human reviewers | FCP + human merge |
| AI critique | Yes (Layer 3) | No | No | No |
| Implementation plan | Required section | Not standard | Required | Not standard |
| Post-mortem | Template exists (not used in pilot) | Not standard | Rare | Not standard |
| Status model | Draft→Accepted | Proposed→Accepted→Deprecated | Draft→Approved | Pre-RFC→FCP→Merged |
| Research tracking | Layer 2 (not used in pilot) | No | No | No |

The RDR process is more rigorous than ADRs (which are just decision records with no review gate) and comparable to Google Design Docs (which have human reviewers instead of AI critique). The AI critique layer is novel and empirically effective.

## Implementation Plan

### Phase 1: Fix Enforcement Gaps

1. Investigate Layer 1: does the tooling enforce the "at least one research finding" check specified in `workflow.md`? Fix if not.
2. Hard-block `rdr-close` without accepted/final status (P1)
3. Add discoverability prompt to `rdr-gate` when no research findings exist (P2 Option A)

### Phase 2: Template and Format Updates

4. Standardize on YAML frontmatter — update `docs/rdr/templates.md` (P4 prerequisite)
5. Add `reviewed-by` field to template frontmatter (P4)
6. Add `## Test Plan` section to `resources/rdr/TEMPLATE.md` (P3)
7. Add `## Revision History` appendix section to template (P6)

### Phase 3: Process Documentation

8. Define gate outcomes (BLOCKED, PASSED) and RDR statuses (Draft, Accepted, Implemented, Reverted, Abandoned, Superseded) in `docs/rdr/workflow.md` (P5)
9. Update `rdr-gate` Layer 3 prompt to check related RDRs for consistency (P7)
10. Add this RDR's findings as a "Process Validation" reference

### Test Plan

- P1: Attempt `rdr-close` on a Draft-status RDR — verify it is blocked. Verify `--force` overrides.
- P2: Run `rdr-gate` with no research findings — verify discoverability prompt appears. Verify `--skip-research` overrides.
- P3: Create a new RDR from updated template — verify `## Test Plan` section is present.
- P4: Create a new RDR — verify `reviewed-by` field is in YAML frontmatter.
- P5: Run a gate — verify output uses BLOCKED/PASSED terminology, not "Conditional Accept."
- P6: Run a gate — verify findings are appended to `## Revision History`, not inline.
- P7: Create an RDR with a `related_issues` reference to an accepted RDR — verify gate Layer 3 prompt includes the related RDR content and checks for consistency.

## Open Questions

- Should post-implementation verification be a required step or opt-in?
- How should the process scale for multi-contributor projects? The current process was validated in a solo-author context.
- Should the gate prompt include performance and security checklists, or are those separate review processes?
- Is the Layer 3 AI critique stable across model versions? The entire process depends on critique quality — this is the highest-stakes unacknowledged dependency.

## Revision History

### Gate Review (2026-02-27)

### Critical — Resolved

**C1. Layer attribution error — RESOLVED.** The original draft blamed Layer 2 for being "dead" without noting that Layer 1 also failed its documented mandate (`workflow.md` specifies Layer 1 should check for research findings). Fixed: acknowledged both layers failed, added Layer 1 investigation as prerequisite to P2, reframed the root cause as a discoverability problem.

**C2. P5 implementation plan contradicted P5 design — RESOLVED.** The design proposed 4 outcomes (BLOCKED, PASSED, ACCEPTED, IMPLEMENTED) but the plan said "BLOCKED, PASSED only." Fixed: separated gate outcomes (BLOCKED, PASSED) from RDR statuses (Draft, Accepted, Implemented) and aligned the plan with all states.

### Significant — Resolved

**S1. Evidence table counts inaccurate — RESOLVED.** Counting methodology was unstated; totals didn't match source documents. Fixed: added counting methodology note, corrected totals.

**S2. P6 missing from implementation plan — RESOLVED.** Fixed: added step 7 (template update for Revision History appendix).

**S3. Metadata format divergence unacknowledged — RESOLVED.** RDRs use YAML frontmatter but `templates.md` specifies markdown-list metadata. Fixed: P4 now explicitly addresses the divergence and proposes standardizing on YAML frontmatter.

**S4. P2 Option B failure modes unanalyzed — RESOLVED.** Fixed: added failure mode comparison table for Options A and B. Changed recommendation to Option A based on auditability and post-mortem integration.

**S5. P1 premise unverified — RESOLVED.** `workflow.md` already required status=Final for close. Fixed: noted P1 is implementing existing but un-enforced policy, not new policy.

### Observations — Applied

- O1: "Zero false positives" qualified as "zero findings the author rejected as false positives" with solo-author validation caveat
- O2: Sample size limitation surfaced explicitly in Summary
- O3: Added Test Plan section to this RDR (the gap this RDR identified in P3)
- O4: Industry comparison post-mortem entry corrected to "Template exists (not used in pilot)"
- O5: Added retroactive remediation path for under-classified issues (spawn new RDR)

### Re-gate (2026-02-27)

All prior findings (C1, C2, S1–S5, O1–O5) verified resolved. No new critical issues.

### Significant — Resolved

**S-new-1. P5 status model incomplete — RESOLVED.** Terminal states (Reverted, Abandoned, Superseded) from existing `workflow.md` and `rdr-close` were omitted. Fixed: added all three terminal states to P5, mapped `rdr-close` close reasons to statuses.

**S-new-2. P2 Option A leaves existing Layer 2 audit logic unspecified — RESOLVED.** Fixed: added clarification that Option A supplements (not replaces) the existing assumption-audit behavior. Discoverability prompt is a prerequisite gate; existing audit logic is retained.

### Observations — Applied

- O-new-1: Added P7 test case (cross-RDR consistency check)
- O-new-2: Layer 3 model stability remains as an open question — no mitigation warranted now given insufficient data on cross-model variation
- O-new-3: Renamed gate findings section to "Revision History" to demonstrate P6 convention
