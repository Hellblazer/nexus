---
name: using-nx-skills
description: Use when starting any conversation or task — establishes that nx skills must be checked before every response, including clarifying questions. If a skill plausibly applies, invoke it.
effort: low
---

# Using nx Skills

If a skill plausibly applies, invoke it. False positives are cheap; misses cost time. Skills evolve — read the current version, don't rely on memory.

## Plan Reuse

Before any multi-agent pipeline:
1. `mcp__plugin_nx_nexus__plan_search(query="<task description>", limit=3)`
2. If a match returns, present it as a starting structure
3. If "No matching plans.", route normally

After a successful pipeline:
- `mcp__plugin_nx_nexus__plan_save(query="<task>", plan_json={"steps":[...],"tools_used":[...],"outcome_notes":"..."}, tags="<agents>")`

## Routing

**Before code:**
- About to implement → `/nx:brainstorming-gate` (mandatory)
- Multi-step → `/nx:create-plan`
- Needs design across modules → `/nx:architecture` then `/nx:create-plan`

**Something broken:**
- Failure / exception / unexpected behaviour → `/nx:debug` immediately
- 2 failed fix attempts without `/nx:debug` → invoke now

**Analyzing code:**
- Structure / dependencies → `/nx:analyze-code`
- Why something behaves a certain way → `/nx:deep-analysis`

**Executing:**
- Plan approved → `/nx:implement`
- Beads need enrichment → `/nx:enrich-plan`

**Quality gates:**
- Code ready → `/nx:review-code`
- Plan ready → `/nx:plan-audit` (validates against codebase)
- Critique reasoning soundness → `/nx:substantive-critique`
- Tests written → `/nx:test-validate`

**Analytical questions (route through `nx_answer`):**
- "how does…" / "tradeoffs…" / "compare…" / "why was this designed…" → `/nx:query`
- Design walks from concept to code → `/nx:research`
- Critique a change set → `/nx:review`
- Cross-corpus synthesis or ranking → `/nx:analyze`
- Why was this written this way → `/nx:debug`
- Documentation gaps → `/nx:document`
- 3+ validated findings to keep → `/nx:knowledge-tidy`
- PDF to index → `/nx:pdf-process`

Direct `search` / `query` MCP calls are for keyword retrieval ("find X in collection Y"). Verb-shaped questions go through `nx_answer`.

**RDR lifecycle:** `/nx:rdr-create` → `/nx:rdr-research` → `/nx:rdr-gate` → `/nx:rdr-accept` → `/nx:rdr-close`. List/show: `/nx:rdr-list`, `/nx:rdr-show NNN`. Audit: `/nx:rdr-audit`.

**Git:** isolation → `/nx:git-worktrees`. Done → `/nx:finishing-branch`. Receiving review → `/nx:receiving-review`.

**Catalog/linking:** entries, links, tumblers, link-context seeding → `/nx:catalog`.

**Reference (no agent dispatch):** `/nx:serena-code-nav`, `/nx:nexus`, `/nx:cli-controller`, `/nx:writing-nx-skills`.

## Essential MCP Tools (always available)

**Sequential Thinking** (`mcp__plugin_nx_sequential-thinking__sequentialthinking`): debugging hypotheses, design choices, plan evaluation. `needsMoreThoughts: true` to continue, `isRevision: true` to correct.

**nx Storage Tiers** (read widest → narrowest before any work):
- T3 `nx search`: permanent knowledge across all sessions/projects
- T2 `nx memory`: project decisions, findings, session context
- T1 `nx scratch`: this session's discoveries, shared across all agents

Write path: T1 (immediate, shared) → `--persist` to T2 (survives session) → `/nx:knowledge-tidy` to T3 (permanent, cross-project).

## Common Mistakes

| Mistake | Correction |
|---------|------------|
| `search` for an analytical question | `nx_answer` via `/nx:query` or a verb skill |
| Test fails → try a different fix | `/nx:debug` |
| Implement without brainstorming-gate | `brainstorming-gate` first |
| Plan exists, start implementing | `/nx:plan-audit` first |
| Symbol callers via grep | `/nx:serena-code-nav` |
| Implement review feedback blindly | `/nx:receiving-review` first |
| Manual worktree setup | `isolation: "worktree"` on Agent tool, or `/nx:git-worktrees` |
