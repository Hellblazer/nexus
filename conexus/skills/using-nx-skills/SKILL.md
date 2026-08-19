---
name: using-nx-skills
description: Use when starting any turn — you MUST scan the available conexus skill list and invoke `Skill` for any matching skill BEFORE producing any other response (clarifying questions, code, or prose included). Direct answers without first invoking a matching skill are a defect. False positives are cheap; misses cost real time.
effort: low
---

# Using Conexus Skills

**You MUST invoke `Skill` for any plausibly-matching conexus skill before producing any other response.** This is not a hint or a preference — it is a hard rule. Skipping a matching skill is a defect, not an optimization. False positives are cheap; misses cost real time. Skills evolve — read the current version, don't rely on memory.

## Plan Reuse

Before any multi-agent pipeline:
1. `mcp__plugin_conexus_nexus__plan_search(query="<task description>", limit=3)`
2. If a match returns, present it as a starting structure
3. If "No matching plans.", route normally

After a successful pipeline:
- Retrieval pipelines auto-grow the plan library via `nx_answer` — no manual save needed. Only `plan_save` a genuinely reusable **retrieval** plan, and it **requires a `verb`** (research / analyze / query / review / …): `mcp__plugin_conexus_nexus__plan_save(query="<question>", plan_json={...}, verb="<verb>", tags="<ops>")`. **Implementation / pipeline / phased-execution plans do NOT go here** — they live in beads + T2 memory. A verb-less save is refused (it pollutes the verb-dimensional plan-match library).

## Routing

**Before code:**
- About to implement with NO design of record → `/conexus:brainstorming-gate` (mandatory; a locked T2 memo, accepted RDR, or reviewed bead IS the approved design — implement it without re-gating)
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

**`nx_answer` is for questions whose answer must be REDUCED FROM MANY DOCUMENTS.** It composes search/query plus `claude -p` operators under a plan-match gate. Use it for:
- cross-corpus synthesis over `knowledge__` / `docs__` / `rdr__` ("what approaches to X appear across this corpus")
- ranking or comparing across many documents ("which of these papers does Y best")
- RDR research phases, where the deliverable is a synthesis and minutes are acceptable

Do NOT use it for: file:line answers (Serena/Grep), anything already in a local RDR / bead / T2 memory, or single-fact lookups (`search` / `query`, seconds (mean ~8s)).

**Measured cost (n=142 executed runs, 2026-04..2026-08; T2 `nexus/nx-answer-capability-analysis-2026-08-19`):** p50 80s, p95 217s, max 371s; 0.7% finish under 5s. 35% miss the plan gate and pay ~53s more (p50) for an inline planner. A call can hit its 300s timeout and return nothing. Budget minutes, not seconds. The trade is worth it when the alternative is twenty minutes of reading — not when it is one grep.

- Reduce-from-many-documents questions ("what approaches to X appear in…", "tradeoffs across…", "compare… across the corpus") → `/conexus:query`
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

**Sequential Thinking** (`mcp__plugin_conexus_sequential-thinking__sequentialthinking`) — call it BEFORE every decision, not only "non-trivial" ones (that qualifier measured to zero top-level calls in a full session, 2026-08-19): what to dispatch, which fix, how to read a reviewer's verdict or a measurement, whether to push. The orchestrator holds itself to the same rule it writes into briefs. Workflow: hypothesis → evidence → evaluate → branch or proceed. `needsMoreThoughts: true` to continue, `isRevision: true` to correct, `branchFromThought: N` + `branchId` to explore alternatives.

**Conexus Storage Tiers — check before any work, write your findings back.** Read widest → narrowest:
- **T3** `nx search`: permanent knowledge across all sessions and projects — **check before researching from scratch**. (Tier checks use `search`; `nx_answer` is for synthesis, not for looking whether something exists.)
- **T2** `nx memory`: project decisions, findings, session context — **check before project work**.
- **T1** `nx scratch`: this session's discoveries, shared across all sibling agents — **check before duplicating sibling work**.

Write path: T1 (immediate, shared with siblings) → `--persist` flag to T2 (survives session) → `/conexus:knowledge-tidy` to T3 (permanent, cross-project). **Findings not stored are findings lost** — call `store_put` (T3) or `memory_put` (T2) before returning a result you'd want a future session to know.

## Common Mistakes

| Mistake | Correction |
|---------|------------|
| `search(query="tradeoffs across the X papers")` when you need them reduced, not listed | `nx_answer` via `/conexus:analyze` — budget minutes |
| `search(query="compare X across projects")` | `nx_answer` via `/conexus:analyze` — cross-corpus compare is the shape operators earn their cost on |
| `nx_answer` for a file:line, single-fact, or already-in-T2 question | `search` / `query` (seconds; mean ~8s, tail to ~45s) or Serena — `nx_answer`'s p50 is 80s |
| Researching from scratch without checking T3 | `nx search` first (seconds) — prior sessions may have already answered |
| Returning findings without storing them | `store_put` (T3) or `memory_put` (T2) before returning |
| Test fails → try a different fix | `/conexus:debug` |
| Implement undesigned work without brainstorming-gate | `brainstorming-gate` first (unless a design of record exists) |
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
| "I'll just answer this quickly" | Fine when the answer is local and you can point at it. If it has to be reduced from many documents, that is `nx_answer` — and it costs minutes. |
| "I know what that means" | Knowing the concept ≠ knowing this project's history with it. Check T2/T3. |
| "This finding isn't worth storing" | Findings not stored are findings lost. The next session will redo your work. |
