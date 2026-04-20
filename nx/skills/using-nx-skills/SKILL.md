---
name: using-nx-skills
description: Use when starting any conversation or task — establishes that nx skills must be checked before every response, including clarifying questions
effort: low
---

<EXTREMELY-IMPORTANT>
If you think there is even a 1% chance a skill might apply to what you are doing, you MUST invoke the skill.

IF A SKILL APPLIES TO YOUR TASK, YOU DO NOT HAVE A CHOICE. YOU MUST USE IT.

This is not negotiable. This is not optional. You cannot rationalize your way out of this.
</EXTREMELY-IMPORTANT>

# Using nx Skills

## The Rule

**Invoke relevant skills BEFORE any response or action.** Even a 1% chance a skill might apply means you should invoke the skill to check. If an invoked skill turns out to be wrong for the situation, you do not need to use it.

## Plan Reuse

Before dispatching any multi-agent pipeline:
1. Call `mcp__plugin_nx_nexus__plan_search(query="<task description>", limit=3)`
2. If a matching template is returned, present it to the user and offer to use it as the starting structure
3. If no match ("No matching plans."), proceed with standard routing

After a multi-agent pipeline completes successfully:
1. Call `mcp__plugin_nx_nexus__plan_save(query="<task description>", plan_json=<relay chain as JSON>, tags="<agent names>")`
2. The `plan_json` should capture: `{"steps": [...], "tools_used": [...], "outcome_notes": "..."}`

Plan reuse is opportunistic — the skill functions normally when the plan library is empty.

## Routing: What Skill Do I Use?

**Before writing any code:**
- About to implement? → `/nx:brainstorming-gate` FIRST (mandatory, no exceptions)
- Multi-step work? → `/nx:create-plan` before touching code
- Feature needs design (APIs, data models, component boundaries, integration)? → `/nx:architecture`

**Something is broken:**
- Test failure, exception, unexpected behavior → `/nx:debug` IMMEDIATELY (do not guess-and-retry)
- After 2 failed fix attempts without `/nx:debug` → you are wasting time, invoke it NOW

**Analyzing code:**
- Need to understand structure, patterns, dependencies → `/nx:analyze-code`
- Need to investigate WHY something behaves a certain way → `/nx:deep-analysis`
- Rule: if `analyze-code` didn't answer the question, escalate to `deep-analysis`

**Executing work:**
- Plan approved, ready to build? → `/nx:implement`
- Beads need enrichment after audit? → `/nx:enrich-plan`

**Quality gates:**
- Code changes ready? → `/nx:review-code`
- Plan exists? → `/nx:plan-audit` (validates against codebase reality)
- Want logic/structure critique? → `/nx:substantive-critique` (reasoning soundness)
- Tests written? → `/nx:test-validate`

**Research and knowledge:**
- Analytical question over nx knowledge (compare, extract, synthesize) → `/nx:query`
- Unfamiliar topic or technology comparison → `/nx:research`
- 3+ validated findings worth keeping → `/nx:knowledge-tidy`
- PDF to index → `/nx:pdf-process`

**RDR lifecycle:** `/nx:rdr-create` → `/nx:rdr-research` → `/nx:rdr-gate` → `/nx:rdr-accept` → `/nx:rdr-close`
- List: `/nx:rdr-list` | Show: `/nx:rdr-show NNN`
- Audit the base rate of silent-scope-reduction on a project: `/nx:rdr-audit [project]`

**Git workflow:**
- Need workspace isolation? → `/nx:git-worktrees`
- Implementation done, ready to merge/PR? → `/nx:finishing-branch`
- Receiving review feedback? → `/nx:receiving-review` (verify before implementing)

**Catalog and linking:**
- Working with catalog entries, links, or tumblers → `/nx:catalog`
- Seeding link context before `store_put` → `/nx:catalog` (Seed section)

**Reference skills (invoke when relevant, no agent dispatch):**
- Symbol navigation (definitions, callers, renames) → `/nx:serena-code-nav`
- nx CLI usage → `/nx:nexus`
- Interactive CLI/REPL control → `/nx:cli-controller`
- Creating/editing nx skills → `/nx:writing-nx-skills`

## Essential MCP Tools

**Use these directly — they are always available, no skill invocation needed.**

**Sequential Thinking** (`mcp__plugin_nx_sequential-thinking__sequentialthinking`):
Use for any non-trivial decision: debugging hypotheses, design choices, plan evaluation, risk assessment. State hypothesis → gather evidence → evaluate → branch or proceed. Set `needsMoreThoughts: true` to continue, `isRevision: true` to correct.

**nx Storage Tiers** (read widest → narrowest before any work):
- **T3** `nx search`: Permanent knowledge across all sessions and projects — check before researching
- **T2** `nx memory`: Project decisions, findings, session context — check before project work
- **T1** `nx scratch`: This session's discoveries, shared across all agents — check before duplicating sibling work

**Write path:** T1 (immediate, shared) → `--persist` flag to T2 (survives session end) → `/nx:knowledge-tidy` to T3 (permanent, cross-project).

## Skill Priority

When multiple skills could apply:

1. **Discipline skills first** (brainstorming-gate) — these determine HOW to approach
2. **Process skills second** (strategic-planning, code-review) — these guide workflow
3. **Implementation skills third** (development, debugging) — these execute work

## Common Mistakes

| Mistake | Correct Action |
|---------|---------------|
| Test fails → try a different fix | Test fails → `/nx:debug` |
| "Simple" feature → start coding | Any feature → `brainstorming-gate` first |
| Complex feature → `/nx:create-plan` | Complex feature → `/nx:architecture` THEN `/nx:create-plan` |
| Plan looks good → start implementing | Plan exists → `/nx:plan-audit` first |
| grep for symbol callers | Symbol navigation → `/nx:serena-code-nav` |
| Read whole file to find a method | Symbol lookup → `/nx:serena-code-nav` |
| Skip review, it's a small change | Any change → `/nx:review-code` before commit |
| Implement review feedback blindly | Receiving feedback → `/nx:receiving-review` first |
| Merge without verifying tests | Branch done → `/nx:finishing-branch` |
| Manual worktree setup | Need isolation → `/nx:git-worktrees` or `isolation: "worktree"` on Agent tool |

## Red Flags

These thoughts mean STOP — you are rationalizing:

| Thought | Reality |
|---------|---------|
| "This is just a simple question" | Questions are tasks. Check for skills. |
| "I need more context first" | Skill check comes BEFORE gathering context. |
| "Let me explore the codebase first" | Skills tell you HOW to explore. Check first. |
| "I can check git/files quickly" | Files lack conversation context. Check for skills. |
| "Let me gather information first" | Skills tell you HOW to gather information. |
| "This doesn't need a formal skill" | If a skill exists, use it. |
| "I remember this skill" | Skills evolve. Read current version. |
| "This doesn't count as a task" | Action = task. Check for skills. |
| "The skill is overkill" | Simple things become complex. Use it. |
| "I'll just do this one thing first" | Check BEFORE doing anything. |
| "This feels productive" | Undisciplined action wastes time. Skills prevent this. |
| "I know what that means" | Knowing the concept ≠ using the skill. Invoke it. |

## Skill Types

**Rigid** (brainstorming-gate): Follow exactly. Do not adapt away discipline.

**Flexible** (patterns, reference): Adapt principles to context.

The skill itself tells you which type it is.

## User Instructions

Instructions say WHAT, not HOW. "Add X" or "Fix Y" does not mean skip workflows. Always check skills first.
