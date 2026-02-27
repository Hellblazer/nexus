---
title: "RDR Process Validation"
id: RDR-001
type: architecture
status: draft
priority: high
author: Hal Hildebrand
created: 2026-02-27
related_issues: ["BFDB RDR-001", "BFDB RDR-002", "BFDB RDR-003"]
---

# RDR-001: RDR Process Validation

## Summary

Evaluates the RDR (Research Design Review) process as practiced across three design documents in the BFDB project. The process — draft, gate review (3 layers), fix, re-gate, accept, implement — caught 8 critical issues with zero false positives at the Critical level. Layer 3 (AI critique) is the engine; Layer 2 (assumption audit) was a dead layer. This RDR formalizes what worked, identifies gaps, and proposes specific fixes.

## Motivation

The nexus project provides RDR tooling (`/rdr-create`, `/rdr-gate`, `/rdr-research`, `/rdr-close`) but has never validated the process against real use. The BFDB project ran three complete RDR lifecycles in a single session, providing the first empirical data on what the process catches, what it misses, and where the tooling falls short.

The question is not "does the process work?" — it clearly does. The question is: "what should be changed before recommending this process to other projects?"

## Evidence Base

### What Was Tested

| RDR | Topic | Gate Rounds | Critical | Significant | Observations |
|-----|-------|-------------|----------|-------------|-------------|
| BFDB-001 | Namespaces | 3 (gate, re-gate, re-gate) | 3 (+1 elevated) | 3 | 3 |
| BFDB-002 | Name Lifecycle | 2 (gate, re-gate) | 3 | 3 | 5 |
| BFDB-003 | Instance Bindings | 2 (gate, re-gate) | 1 | 4 | 3 |
| **Total** | | **7 rounds** | **8** | **10** | **11** |

### What Layer 3 Caught

Every critical issue was found by Layer 3 (AI critique via substantive-critic agent). Notable catches:

- **BFDB-001 C1**: Doc said "case-insensitive," code had no `LOWER()` anywhere. ltree is case-sensitive. Lookups would silently fail for any name with uppercase letters.

- **BFDB-002 C1**: The author's own phantom-edge analysis was factually wrong. `find_or_create_object` also creates phantom edges via `ON CONFLICT DO UPDATE` (the edge PK is not the update target). The gate caught the author making a factual error about their own code.

- **BFDB-002 C2**: Deprecating an entity node would deprecate all of its names simultaneously, directly contradicting the rename use case that was the RDR's stated purpose. Required introducing binding nodes — a new concept not in the original design.

- **BFDB-003 C1**: The metadata registration DO block called `find_node_named('bfdb.instance_of')` before `new_predicate_named` had ever been called. The predicate would not exist, the lookup would return NULL, and the block would silently insert NULL foreign keys.

### What Layer 2 Did Not Catch

Layer 2 (assumption audit via T2 research findings) was **never invoked** across all three RDRs. `/rdr-research` was never called. When Layer 2 ran during gate review, it printed "No research findings recorded" and moved on. The layer exists in the process design and in the tooling, but it has no content to audit when no research has been recorded.

Assumptions that deserved research but were never verified:
- `iso_639` language codes are adequate for international name disambiguation (BFDB-001)
- Rename as three additive operations vs. a single atomic operation (BFDB-002)
- Public namespace bindings and instance bindings are "complementary, not redundant" (BFDB-003)

### What Layer 1 Did Not Catch

Layer 1 (structural validation) caught nothing substantive in any RDR. It verifies that section headings exist and are non-empty — a formatting check, not a correctness check. Its value is organizational hygiene.

## Design: Process Improvements

### P1. Hard-block close without gate pass (Critical)

**Problem**: RDR-001 was prematurely closed before re-gate. The close was then manually reverted. The `rdr-close` tool emits a warning but does not hard-block.

**Fix**: `rdr-close` must check that RDR status is `accepted` or `final` and refuse to close otherwise. Require `--force` to override.

### P2. Either enforce Layer 2 or merge it into Layer 3 (Critical)

**Problem**: Layer 2 was a dead layer across all three RDRs — no research was ever recorded.

**Option A — Enforce**: Require at least one `/rdr-research` finding before `/rdr-gate` can run. Print a blocking warning; require `--skip-research` to override.

**Option B — Merge**: Instruct the Layer 3 AI critic to explicitly enumerate each assumption in the design and state whether it is verified by evidence. Remove Layer 2 as a separate step. This is simpler and may be more effective — the AI critic already reads the design document and can identify unverified claims.

**Recommendation**: Option B. The AI critic is already the engine of the gate. Having it explicitly call out assumptions is more reliable than depending on the author to pre-populate T2 findings.

### P3. Add Test Plan section to RDR template (Significant)

**Problem**: No RDR had a test plan section. Tests were added during implementation but the gate never evaluated whether tests cover the design. BFDB-003's catalog count test (`= 7`) is brittle.

**Fix**: Add `## Test Plan` to the RDR template. Gate Layer 3 should evaluate whether the test plan covers the design's edge cases and failure modes.

### P4. Add `reviewed-by` field to frontmatter (Significant)

**Problem**: All three RDRs were accepted on the strength of the AI gate review alone. No human approval record exists.

**Fix**: Add `reviewed-by` to YAML frontmatter. For solo projects, `reviewed-by: self` is acceptable. For collaborative projects, require at least one reviewer other than the author.

### P5. Define the status model precisely (Significant)

**Problem**: BFDB-002 gate returned "Conditional Accept" — a status not in the lifecycle model. This led to ambiguity about whether implementation could proceed.

**Fix**: Define exactly four gate outcomes:
- **BLOCKED** — critical issues found, must fix and re-gate
- **PASSED** — no critical issues (may have significant/observations)
- **ACCEPTED** — author/reviewer decision after gate passes
- **IMPLEMENTED** — code matches accepted design

Remove "Conditional Accept" as a concept. The gate either blocks or passes.

### P6. Separate gate history from design body (Minor)

**Problem**: Gate findings appended to the RDR create long documents. By BFDB-002, there are three interleaved finding sections.

**Fix**: Move gate findings to a `## Revision History` appendix at the end of the document. Keep the `## Design` section as the canonical current design without historical noise.

### P7. Cross-RDR dependency tracking (Minor)

**Problem**: No tooling checks whether an RDR contradicts or modifies a previous accepted RDR. BFDB-002 modifies `global_names` from BFDB-001. BFDB-003 references `bfdb.instance` from BFDB-001.

**Fix**: During gate review, Layer 3 should be given the list of related RDRs (from `related_issues`) and check for consistency. This is a prompt engineering fix, not a tooling change.

## Severity Assessment of the BFDB Gate Reviews

### Critical level: accurate

All 8 critical issues would have caused runtime failures, silently incorrect behavior, or fundamental semantic errors. Zero items at this level were over-classified.

### Significant level: slightly under-calibrated

Two issues classified as Significant should have been Critical:

- **BFDB-002 S2** (`binding` column nullable): A NULL binding in `global_names` would allow inserting a deprecation record targeting a nonexistent binding node, corrupting the deprecation graph.

- **BFDB-003 S2** (`instance_of=key` semantically incorrect): The catalog's metadata would record `instance_of` as the primary key column, misleading any tooling that uses catalog metadata. For a self-hosting catalog, metadata correctness is data correctness.

### Observation level: one item clearly under-classified

- **BFDB-001**: ltree character constraints (`[A-Za-z0-9_]` labels only). Real-world database names with hyphens won't work. This is a user-facing incompatibility that should be Significant with a required resolution.

## What the Process Does Not Cover

These are gaps in the RDR process, not bugs in any individual RDR:

1. **Performance analysis** — No RDR discussed performance. Phantom edge/node accumulation has a real performance dimension.
2. **Rollback documentation** — BFDB's monotonic-only philosophy means no rollback. This was never explicitly stated as a deliberate architectural constraint.
3. **Post-implementation verification** — The process ends at acceptance. No gate runs on the implementation to verify it matches the design.
4. **Security review** — No security analysis was done on any RDR.

## Industry Comparison

| Feature | RDR Process | ADRs (Nygard) | Google Design Docs | Rust RFCs |
|---------|-------------|---------------|---------------------|-----------|
| Gate review | 3-layer automated | None (just record) | Human reviewers | FCP + human merge |
| AI critique | Yes (Layer 3) | No | No | No |
| Implementation plan | Required section | Not standard | Required | Not standard |
| Post-mortem | Template exists | Not standard | Rare | Not standard |
| Status model | Draft→Accepted | Proposed→Accepted→Deprecated | Draft→Approved | Pre-RFC→FCP→Merged |
| Research tracking | Layer 2 (unused) | No | No | No |

The RDR process is more rigorous than ADRs (which are just decision records with no review gate) and comparable to Google Design Docs (which have human reviewers instead of AI critique). The AI critique layer is novel and empirically effective.

## Implementation Plan

### Phase 1: Template and Tooling Fixes

1. Add `## Test Plan` section to `resources/rdr/TEMPLATE.md`
2. Add `reviewed-by` field to template frontmatter
3. Hard-block `rdr-close` without accepted status
4. Update `rdr-gate` Layer 3 prompt to enumerate assumptions explicitly (merge Layer 2)
5. Update `rdr-gate` Layer 3 prompt to check related RDRs for consistency
6. Define gate outcomes in `docs/rdr/workflow.md`: BLOCKED, PASSED only

### Phase 2: Documentation

7. Update `docs/rdr/workflow.md` with the validated status model
8. Update `docs/rdr/templates.md` with new sections
9. Add this RDR's findings as a "Process Validation" reference

## Open Questions

- Should Layer 2 be removed entirely or kept as an optional enhancement for high-stakes RDRs?
- Should post-implementation verification be a required step or opt-in?
- How should the process scale for multi-contributor projects? The current process was validated in a solo-author context.
- Should the gate prompt include performance and security checklists, or are those separate review processes?
