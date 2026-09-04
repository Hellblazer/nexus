---
name: using-nx-skills
description: Use when work matches a conexus skill's territory. Something is broken or two fix attempts have failed; work spans modules or needs design before code; code, tests, or a plan need a quality gate; an answer must be reduced from many documents rather than looked up; prior work in T1/T2/T3 has not been checked; or a validated finding is about to go unstored.
effort: low
---

# Using Conexus Skills

Conexus skills carry this project's accumulated practice for specific situations: which tools apply, which storage tier already holds prior work, and the failure modes this project has already paid for. When a situation matches, invoking `Skill` is usually cheaper than re-deriving the approach, and it is how a session inherits what earlier ones learned. Skills change; read the current version rather than recalling a prior one.

SessionStart emits only a condensed form of this rule plus its trigger conditions (nexus-h33x8.5). This file is the full routing menu, MCP tool catalogue, Common Mistakes table, Red Flags table, and RDR lifecycle chain, read in full whenever `Skill` is invoked on `using-nx-skills`.

## Plan Reuse

Before any multi-agent pipeline:
1. `mcp__plugin_conexus_nexus__plan_search(query="<task description>", limit=3)`
2. If a match returns, present it as a starting structure
3. If "No matching plans.", route normally

After a successful pipeline:
- Retrieval pipelines grow the plan library on their own through `nx_answer`; no manual save is needed. Only `plan_save` a genuinely reusable retrieval plan, and it requires a `verb` (research / analyze / query / review / …): `mcp__plugin_conexus_nexus__plan_save(query="<question>", plan_json={...}, verb="<verb>", tags="<ops>")`. Implementation, pipeline, and phased-execution plans do NOT go here; they live in beads and T2 memory. A verb-less save is refused because it pollutes the verb-dimensional plan-match library.

## Routing

Before code:
- About to implement with no design of record → `/conexus:brainstorming-gate` (mandatory; a locked T2 memo, accepted RDR, or reviewed bead is the approved design, so implement it without re-gating)
- Multi-step → `/conexus:create-plan`
- Needs design across modules → `/conexus:architecture` then `/conexus:create-plan`

Something broken:
- Failure, exception, or unexpected behaviour → `/conexus:debug` immediately
- 2 failed fix attempts without `/conexus:debug` → invoke now

Analyzing code:
- Structure or dependencies → `/conexus:analyze-code`
- Why something behaves a certain way → `/conexus:deep-analysis`

Executing:
- Plan approved → `/conexus:implement`
- Beads need enrichment → `/conexus:enrich-plan`

Quality gates:
- Code ready → `/conexus:review-code`
- Plan ready → `/conexus:plan-audit` (validates against the codebase)
- Critique reasoning soundness → `/conexus:substantive-critique`
- Tests written → `/conexus:test-validate`

`nx_answer` is for questions whose answer must be REDUCED FROM MANY DOCUMENTS. It composes search and query plus `claude -p` operators under a plan-match gate. Use it for:
- cross-corpus synthesis over `knowledge__` / `docs__` / `rdr__` ("what approaches to X appear across this corpus")
- ranking or comparing across many documents ("which of these papers does Y best")
- RDR research phases, where the deliverable is a synthesis and minutes are acceptable

Do NOT use it for file:line answers (Serena or Grep), anything already in a local RDR, bead, or T2 memory, or single-fact lookups (`search` / `query`, seconds; mean about 8s).

Measured cost (n=142 executed runs, 2026-04 to 2026-08; T2 `nexus/nx-answer-capability-analysis-2026-08-19`): p50 80s, p95 217s, max 371s; 0.7% finish under 5s. 35% miss the plan gate and pay about 53s more (p50) for an inline planner. A call can hit its 300s timeout and return nothing. Budget minutes, not seconds. The trade is worth it when the alternative is twenty minutes of reading, and not when it is one grep.

Two return shapes (RDR-200, landing with the Phase 1 go-live; headless is the only live shape as of this writing). Headless (the default; `continuation` unset or `False`) runs the terminal synthesis server-side in a `claude -p` subprocess and returns a finished answer. The cost figures above describe this shape and nothing here changes it. Continuation (opt-in `continuation=True`, not yet live: nexus-4e75w.4 built and tested the assembly behind a closed go-live gate and nexus-4e75w.5 wires the return path) stops after server-side retrieval and returns an envelope carrying the exact prompt and schema the synthesis would have dispatched, plus evidence provenance, so the calling session performs the reduction in its own context instead of paying a second subprocess. This does not change when to call `nx_answer`. The routing decision below (reduce-from-many-documents versus `search` or `query`) is unaffected, and `nexus-h33x8.6`'s narrowing stands.

- Reduce-from-many-documents questions ("what approaches to X appear in…", "tradeoffs across…", "compare… across the corpus") → `/conexus:query`
- Design walks from concept to code → `/conexus:research`
- Critique a change set → `/conexus:review`
- Cross-corpus synthesis or ranking → `/conexus:analyze`
- Why was this written this way → `/conexus:debug`
- Documentation gaps → `/conexus:document`
- 3+ validated findings to keep → `/conexus:knowledge-tidy`
- PDF to index → `/conexus:pdf-process`

RDR lifecycle: `/conexus:rdr-create` → `/conexus:rdr-research` → `/conexus:rdr-gate` → `/conexus:rdr-accept` → (implementation phases) → `/conexus:rdr-close`. List and show: `/conexus:rdr-list`, `/conexus:rdr-show NNN`. Audit: `/conexus:rdr-audit`.

Phase boundary inside an implementation arc: every phase-review bead, before close, runs `/conexus:phase-review-gate <rdr-id> --phase N`. Pass 1 enumerates the RDR's numbered §Approach items; Pass 2 validates that each has a closing-bead pointer (`ItemN=nexus-xxxx`) or an explicit `none` deferral. BLOCKED on any unaccounted item. Not optional. It prevents the silent scope reduction class (RDR-112 Phase 1 / nexus-52lb, 2026-05-15: the T3 daemon was silently dropped from a 6-bead close, found three phases later, at a cost of 2 to 3 days of replanning).

Git: isolation → `/conexus:git-worktrees`. Done → `/conexus:finishing-branch`. Receiving review → `/conexus:receiving-review`.

Catalog and linking: entries, links, tumblers, link-context seeding → `/conexus:catalog`.

Reference (no agent dispatch): `/conexus:serena-code-nav`, `/conexus:nexus`, `/conexus:cli-controller`, `/conexus:writing-nx-skills`.

## Essential MCP Tools (always available)

Sequential Thinking (`mcp__plugin_conexus_sequential-thinking__sequentialthinking`): call it BEFORE every decision, not only "non-trivial" ones. That qualifier measured to zero top-level calls in a full session on 2026-08-19. Decisions include what to dispatch, which fix, how to read a reviewer's verdict or a measurement, and whether to push. The orchestrator holds itself to the same rule it writes into briefs. Workflow: hypothesis → evidence → evaluate → branch or proceed. `needsMoreThoughts: true` to continue, `isRevision: true` to correct, `branchFromThought: N` + `branchId` to explore alternatives. The thought is the record: it is what reviewers, siblings, and the census can see; internal reasoning is not.

Conexus Storage Tiers: check before any work, and write your findings back. Read widest to narrowest:
- T3 `nx search`: permanent knowledge across all sessions and projects. Check before researching from scratch. (Tier checks use `search`; `nx_answer` is for synthesis, not for looking whether something exists.)
- T2 `nx memory`: project decisions, findings, session context. Check before project work.
- T1 `nx scratch`: this session's discoveries, shared across all sibling agents. Check before duplicating sibling work.

Write path: T1 (immediate, shared with siblings) → `--persist` flag to T2 (survives the session) → `/conexus:knowledge-tidy` to T3 (permanent, cross-project). Findings not stored are findings lost: call `store_put` (T3) or `memory_put` (T2) before returning a result you would want a future session to know.

## Common Mistakes

| Mistake | Correction |
|---------|------------|
| `search(query="tradeoffs across the X papers")` when you need them reduced, not listed | `nx_answer` via `/conexus:analyze`; budget minutes |
| `search(query="compare X across projects")` | `nx_answer` via `/conexus:analyze`; cross-corpus compare is the shape operators earn their cost on |
| `nx_answer` for a file:line, single-fact, or already-in-T2 question | `search` / `query` (seconds; mean about 8s, tail to about 45s) or Serena. `nx_answer`'s p50 is 80s |
| Researching from scratch without checking T3 | `nx search` first (seconds); prior sessions may have already answered |
| Returning findings without storing them | `store_put` (T3) or `memory_put` (T2) before returning |
| Test fails → try a different fix | `/conexus:debug` |
| Implement undesigned work without brainstorming-gate | `brainstorming-gate` first (unless a design of record exists) |
| Plan exists, start implementing | `/conexus:plan-audit` first |
| Symbol callers via grep | `/conexus:serena-code-nav` |
| Implement review feedback blindly | `/conexus:receiving-review` first |
| Manual worktree setup | `isolation: "worktree"` on the Agent tool, or `/conexus:git-worktrees` |

## Red Flags

Thoughts that mean STOP, because you are rationalizing past a tier check:

| Thought | Reality |
|---------|---------|
| "Let me explore the codebase first" | T3 `nx search` first; prior research may already cover it. |
| "I can just grep for it" | T2 `nx memory` first if it's a project decision; T3 if it's general. |
| "I'll just answer this quickly" | Fine when the answer is local and you can point at it. If it has to be reduced from many documents, that is `nx_answer`, and it costs minutes. |
| "I know what that means" | Knowing the concept is not knowing this project's history with it. Check T2/T3. |
| "This finding isn't worth storing" | Findings not stored are findings lost. The next session will redo your work. |
