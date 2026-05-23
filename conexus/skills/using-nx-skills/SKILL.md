---
name: using-nx-skills
description: Use when starting any turn — you MUST scan the available nx skill list and invoke `Skill` for any matching skill BEFORE producing any other response (clarifying questions, code, or prose included). Direct answers without first invoking a matching skill are a defect. False positives are cheap; misses cost real time.
effort: low
---

# Using nx Skills

**You MUST invoke `Skill` for any plausibly-matching nx skill before producing any other response.** This is not a hint or a preference — it is a hard rule. Skipping a matching skill is a defect, not an optimization. False positives are cheap; misses cost real time. Skills evolve — read the current version, don't rely on memory.

## Plan Reuse

Before any multi-agent pipeline:
1. `mcp__plugin_conexus_nexus__plan_search(query="<task description>", limit=3)`
2. If a match returns, present it as a starting structure
3. If "No matching plans.", route normally

After a successful pipeline:
- `mcp__plugin_conexus_nexus__plan_save(query="<task>", plan_json={"steps":[...],"tools_used":[...],"outcome_notes":"..."}, tags="<agents>")`

## Routing

**Before code:**
- About to implement → `/conexus:brainstorming-gate` (mandatory)
- Multi-step → `/conexus:create-plan`
- Needs design across modules → `/conexus:architecture` then `/conexus:create-plan`

**Something broken:**
- Failure / exception / unexpected behaviour → `/conexus:debug` immediately
- 2 failed fix attempts without `/conexus:debug` → invoke now

**Analyzing code:**
- Structure / dependencies → `/conexus:analyze-code`
- Why something behaves a certain way → `/conexus:deep-analysis`

**Executing:**
- Plan approved → `/conexus:implement`
- Beads need enrichment → `/conexus:enrich-plan`

**Quality gates:**
- Code ready → `/conexus:review-code`
- Plan ready → `/conexus:plan-audit` (validates against codebase)
- Critique reasoning soundness → `/conexus:substantive-critique`
- Tests written → `/conexus:test-validate`

**ALL analytical questions go through `nx_answer`.** A verb-shaped question ("how does X work", "what tradeoffs in Y", "compare X across projects", "why was Z designed this way") routes to a skill that calls `nx_answer`. `nx_answer` composes search/query/operators under a plan-match-first gate — composed retrieval is strictly more useful than raw chunks. Raw `search` is for keyword lookup only ("find X in collection Y").

- "how does…" / "tradeoffs…" / "compare…" / "why was this designed…" → `/conexus:query`
- Design walks from concept to code → `/conexus:research`
- Critique a change set → `/conexus:review`
- Cross-corpus synthesis or ranking → `/conexus:analyze`
- Why was this written this way → `/conexus:debug`
- Documentation gaps → `/conexus:document`
- 3+ validated findings to keep → `/conexus:knowledge-tidy`
- PDF to index → `/conexus:pdf-process`

**RDR lifecycle:** `/conexus:rdr-create` → `/conexus:rdr-research` → `/conexus:rdr-gate` → `/conexus:rdr-accept` → (implementation phases) → `/conexus:rdr-close`. List/show: `/conexus:rdr-list`, `/conexus:rdr-show NNN`. Audit: `/conexus:rdr-audit`.

**Phase boundary inside an implementation arc:** every phase-review bead, before close, runs `/conexus:phase-review-gate <rdr-id> --phase N`. Pass 1 enumerates the RDR's numbered §Approach items; Pass 2 validates each has a closing-bead pointer (`ItemN=nexus-xxxx`) or explicit `none` deferral. BLOCKED on any unaccounted item. Not optional. Prevents the silent scope reduction class (RDR-112 Phase 1 / nexus-52lb, 2026-05-15: T3 daemon silently dropped from a 6-bead close, found three phases later, 2-3 days of replanning).

**Git:** isolation → `/conexus:git-worktrees`. Done → `/conexus:finishing-branch`. Receiving review → `/conexus:receiving-review`.

**Catalog/linking:** entries, links, tumblers, link-context seeding → `/conexus:catalog`.

**Reference (no agent dispatch):** `/conexus:serena-code-nav`, `/conexus:nexus`, `/conexus:cli-controller`, `/conexus:writing-nx-skills`.

## Essential MCP Tools (always available)

**Sequential Thinking** (`mcp__plugin_conexus_sequential-thinking__sequentialthinking`) — use for any non-trivial decision: debugging hypotheses, design choices, plan evaluation, risk assessment. Workflow: hypothesis → evidence → evaluate → branch or proceed. `needsMoreThoughts: true` to continue, `isRevision: true` to correct, `branchFromThought: N` + `branchId` to explore alternatives.

**nx Storage Tiers — check before any work, write your findings back.** Read widest → narrowest:
- **T3** `nx search` / `nx_answer`: permanent knowledge across all sessions and projects — **check before researching from scratch**.
- **T2** `nx memory`: project decisions, findings, session context — **check before project work**.
- **T1** `nx scratch`: this session's discoveries, shared across all sibling agents — **check before duplicating sibling work**.

Write path: T1 (immediate, shared with siblings) → `--persist` flag to T2 (survives session) → `/conexus:knowledge-tidy` to T3 (permanent, cross-project). **Findings not stored are findings lost** — call `store_put` (T3) or `memory_put` (T2) before returning a result you'd want a future session to know.

## Common Mistakes

| Mistake | Correction |
|---------|------------|
| `search(query="how does X work", …)` for an analytical question | `nx_answer(question="how does X work", …)` via `/conexus:query` or a verb skill |
| `search(query="tradeoffs in Y")` | `nx_answer` via `/conexus:analyze` — `search` returns chunks, you need composition |
| `search(query="compare X across projects")` | `nx_answer` via `/conexus:analyze` — cross-corpus compare is what plan operators do |
| Researching from scratch without checking T3 | `nx search` / `nx_answer` first — prior sessions may have already answered |
| Returning findings without storing them | `store_put` (T3) or `memory_put` (T2) before returning |
| Test fails → try a different fix | `/conexus:debug` |
| Implement without brainstorming-gate | `brainstorming-gate` first |
| Plan exists, start implementing | `/conexus:plan-audit` first |
| Symbol callers via grep | `/conexus:serena-code-nav` |
| Implement review feedback blindly | `/conexus:receiving-review` first |
| Manual worktree setup | `isolation: "worktree"` on Agent tool, or `/conexus:git-worktrees` |

## Red Flags

Thoughts that mean STOP — you are rationalizing past a tier check:

| Thought | Reality |
|---------|---------|
| "Let me explore the codebase first" | T3 `nx search` first — prior research may already cover it. |
| "I can just grep for it" | T2 `nx memory` first if it's a project decision; T3 if it's general. |
| "I'll just answer this quickly" | Verb-shape question? → `nx_answer`. Even quick answers benefit from composed retrieval. |
| "I know what that means" | Knowing the concept ≠ knowing this project's history with it. Check T2/T3. |
| "This finding isn't worth storing" | Findings not stored are findings lost. The next session will redo your work. |
