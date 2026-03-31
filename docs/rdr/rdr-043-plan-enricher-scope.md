---
title: "Widen Plan-Enricher Scope"
id: RDR-043
type: Enhancement
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-30
accepted_date: 2026-03-30
related_issues:
  - "RDR-036 - Post-Accept Planning Workflow (accepted)"
  - "RDR-042 - AgenticScholar-Inspired Enhancements (accepted)"
---

# RDR-043: Widen Plan-Enricher Scope

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The plan-enricher agent is framed as an "audit findings delivery mechanism" — its description, skill, commands, and internal prompts all center on folding audit findings into beads. The phrase "audit findings" appears 30+ times across the agent ecosystem. This framing is too narrow.

In practice, the enricher's real value is making beads execution-ready by adding whatever context an implementing agent needs: file paths, code patterns, dependency constraints, test commands, design decisions, and relevant prior art. Audit findings are one useful input among many, and they're often absent (standalone enrichment, ad-hoc bead grooming, enrichment after design changes).

The current framing causes two problems:

1. **Degraded mode feels like failure.** When T1 scratch has no audit findings, the enricher warns "proceeding with context-only enrichment (degraded mode)." This treats the common case (enrichment without a preceding audit) as an error state.

2. **Scope is artificially limited.** The enricher could add codebase-derived context (symbol locations, import patterns, related test files) proactively, but the audit-centric framing doesn't prompt for it.

## Context

### Background

RDR-036 introduced the plan-enricher as the third step in the RDR accept chain: `strategic-planner → plan-auditor → plan-enricher`. In that chain, audit findings flow through T1 scratch and the enricher folds them into beads. This works well for the chain use case.

But the enricher is also invoked standalone (`/nx:enrich-plan`) and should work equally well without a preceding audit. The "degraded mode" label discourages standalone use.

### Scope of Change

This is a framing/documentation change across ~15 files, not a behavioral change. The enricher already does context-only enrichment when audit findings are absent — it just calls it "degraded." The fix is to make context enrichment the primary framing and audit findings an optional enhancement.

## Proposed Solution

### Reframe: "Bead Enrichment" not "Audit Findings Delivery"

1. **Agent description**: "Enriches beads with execution context — file paths, code patterns, constraints, test commands, and (when available) audit findings."

2. **Remove "degraded mode" language.** Context-only enrichment is the default, not a fallback. When audit findings are available in T1, incorporate them. When absent, proceed without warning.

3. **Widen the enrichment checklist** in the agent prompt:
   - File paths (absolute, verified to exist)
   - Relevant symbol names and locations (via Serena if available)
   - Test file paths and commands
   - Dependency constraints (which beads must complete first and why)
   - Design decisions from RDR or T2 memory
   - Audit findings (when available from T1 scratch)

4. **Update all references** across agent, skill, command, registry, README, and changelog.

### Files to Update

| File | What changes |
|------|-------------|
| `nx/agents/plan-enricher.md` | Description, prompts, remove "degraded mode" |
| `nx/skills/enrich-plan/SKILL.md` | Description, relay template, quality criteria |
| `nx/commands/enrich-plan.md` | Relay prompt text |
| `nx/commands/rdr-accept.md` | Enricher dispatch prompt |
| `nx/skills/rdr-accept/SKILL.md` | Enricher dispatch description |
| `nx/registry.yaml` | Agent description and triggers |
| `nx/README.md` | Agent table description |
| `nx/CHANGELOG.md` | Release note |

## Alternatives Considered

### Leave as-is (rejected)
The enricher works. But "degraded mode" for the common case is confusing, and the narrow framing undersells the tool.

### Split into two agents (rejected)
One for audit-findings integration, one for general enrichment. Rejected: same logic, same bead update pattern. Two agents would duplicate work.

## Research Findings

### RF-1: Audit-centric language inventory (2026-03-30)

**Classification**: Verified — Codebase Search
**Method**: `grep -rn "audit.finding\|degraded.mode"` across `nx/`
**Confidence**: HIGH

27 references to "audit findings" across 8 files:

| File | References | Key phrases |
|------|-----------|-------------|
| `nx/agents/plan-enricher.md` | 11 | "degraded mode", "context-only enrichment", "audit-identified gaps" |
| `nx/skills/enrich-plan/SKILL.md` | 6 | "audit findings folded in", "context-only if T1 miss" |
| `nx/commands/enrich-plan.md` | 4 | "audit findings using plan-enricher" |
| `nx/commands/rdr-accept.md` | 1 | "enrich beads with audit findings from T1 scratch" |
| `nx/skills/rdr-accept/SKILL.md` | 2 | "enriches beads with audit findings" |
| `nx/registry.yaml` | 2 | description + triggers |
| `nx/README.md` | 1 | agent table description |

### RF-2: Enricher already handles no-audit case (2026-03-30)

**Classification**: Verified — Code Inspection
**Method**: Read `plan-enricher.md` lines 60-67
**Confidence**: HIGH

The enricher already does context-only enrichment when audit findings are absent — it searches T1 for `audit-findings` tag, and if empty, proceeds with codebase search, file paths, and line numbers. The "degraded mode" label is the only problem; the behavior is correct.

### RF-3: No behavioral change needed (2026-03-30)

**Classification**: Verified — Analysis
**Confidence**: HIGH

This is purely a framing/documentation change:
- Remove "degraded mode" language and warning
- Promote context enrichment (file paths, symbols, test commands) to primary purpose
- Demote audit findings from "purpose" to "optional input when available"
- No Python code changes — all agent/skill/command markdown files

## Success Criteria

- [ ] "audit findings" is no longer the primary framing in any agent/skill description
- [ ] Context-only enrichment has no warning or "degraded" label
- [ ] Enrichment checklist includes file paths, symbols, test commands, constraints
- [ ] Standalone `/nx:enrich-plan` works without preceding audit (no warning)
- [ ] RDR accept chain still works (audit findings still incorporated when present)
- [ ] All plugin structure tests pass
