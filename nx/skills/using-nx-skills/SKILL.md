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

**ALL analytical questions go through `nx_answer`.** A verb-shaped question ("how does X work", "what tradeoffs in Y", "compare X across projects", "why was Z designed this way") routes to a skill that calls `nx_answer`. `nx_answer` composes search/query/operators under a plan-match-first gate — composed retrieval is strictly more useful than raw chunks. Raw `search` is for keyword lookup only ("find X in collection Y").

- "how does…" / "tradeoffs…" / "compare…" / "why was this designed…" → `/nx:query`
- Design walks from concept to code → `/nx:research`
- Critique a change set → `/nx:review`
- Cross-corpus synthesis or ranking → `/nx:analyze`
- Why was this written this way → `/nx:debug`
- Documentation gaps → `/nx:document`
- 3+ validated findings to keep → `/nx:knowledge-tidy`
- PDF to index → `/nx:pdf-process`

**RDR lifecycle:** `/nx:rdr-create` → `/nx:rdr-research` → `/nx:rdr-gate` → `/nx:rdr-accept` → (implementation phases) → `/nx:rdr-close`. List/show: `/nx:rdr-list`, `/nx:rdr-show NNN`. Audit: `/nx:rdr-audit`.

**Phase boundary inside an implementation arc:** every phase-review bead, before close, runs `/nx:phase-review-gate <rdr-id> --phase N`. Pass 1 enumerates the RDR's numbered §Approach items; Pass 2 validates each has a closing-bead pointer (`ItemN=nexus-xxxx`) or explicit `none` deferral. BLOCKED on any unaccounted item. Not optional. Prevents the silent scope reduction class (RDR-112 Phase 1 / nexus-52lb, 2026-05-15: T3 daemon silently dropped from a 6-bead close, found three phases later, 2-3 days of replanning).

**Git:** isolation → `/nx:git-worktrees`. Done → `/nx:finishing-branch`. Receiving review → `/nx:receiving-review`.

**Catalog/linking:** entries, links, tumblers, link-context seeding → `/nx:catalog`.

**Reference (no agent dispatch):** `/nx:serena-code-nav`, `/nx:nexus`, `/nx:cli-controller`, `/nx:writing-nx-skills`.

## Essential MCP Tools (always available)

**Sequential Thinking** (`mcp__plugin_nx_sequential-thinking__sequentialthinking`) — use for any non-trivial decision: debugging hypotheses, design choices, plan evaluation, risk assessment. Workflow: hypothesis → evidence → evaluate → branch or proceed. `needsMoreThoughts: true` to continue, `isRevision: true` to correct, `branchFromThought: N` + `branchId` to explore alternatives.

**nx Storage Tiers — check before any work, write your findings back.** Read widest → narrowest:
- **T3** `nx search` / `nx_answer`: permanent knowledge across all sessions and projects — **check before researching from scratch**.
- **T2** `nx memory`: project decisions, findings, session context — **check before project work**.
- **T1** `nx scratch`: this session's discoveries, shared across all sibling agents — **check before duplicating sibling work**.

Write path: T1 (immediate, shared with siblings) → `--persist` flag to T2 (survives session) → `/nx:knowledge-tidy` to T3 (permanent, cross-project). **Findings not stored are findings lost** — call `store_put` (T3) or `memory_put` (T2) before returning a result you'd want a future session to know.

## Common Mistakes

| Mistake | Correction |
|---------|------------|
| `search(query="how does X work", …)` for an analytical question | `nx_answer(question="how does X work", …)` via `/nx:query` or a verb skill |
| `search(query="tradeoffs in Y")` | `nx_answer` via `/nx:analyze` — `search` returns chunks, you need composition |
| `search(query="compare X across projects")` | `nx_answer` via `/nx:analyze` — cross-corpus compare is what plan operators do |
| Researching from scratch without checking T3 | `nx search` / `nx_answer` first — prior sessions may have already answered |
| Returning findings without storing them | `store_put` (T3) or `memory_put` (T2) before returning |
| Test fails → try a different fix | `/nx:debug` |
| Implement without brainstorming-gate | `brainstorming-gate` first |
| Plan exists, start implementing | `/nx:plan-audit` first |
| Symbol callers via grep | `/nx:serena-code-nav` |
| Implement review feedback blindly | `/nx:receiving-review` first |
| Manual worktree setup | `isolation: "worktree"` on Agent tool, or `/nx:git-worktrees` |

## Red Flags

Thoughts that mean STOP — you are rationalizing past a tier check:

| Thought | Reality |
|---------|---------|
| "Let me explore the codebase first" | T3 `nx search` first — prior research may already cover it. |
| "I can just grep for it" | T2 `nx memory` first if it's a project decision; T3 if it's general. |
| "I'll just answer this quickly" | Verb-shape question? → `nx_answer`. Even quick answers benefit from composed retrieval. |
| "I know what that means" | Knowing the concept ≠ knowing this project's history with it. Check T2/T3. |
| "This finding isn't worth storing" | Findings not stored are findings lost. The next session will redo your work. |
