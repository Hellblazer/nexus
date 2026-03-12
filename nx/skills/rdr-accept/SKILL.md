---
name: rdr-accept
description: Use when a gated RDR returned PASSED and you want to officially accept it for implementation
---

# RDR Accept Skill

Accepts an RDR after it passes the gate. This is the author/reviewer decision point between gate validation and implementation.

## When This Skill Activates

- User says "accept this RDR", "mark as accepted", "approve the RDR"
- User invokes `/rdr-accept`
- Gate returns PASSED and user confirms acceptance

## Input

- RDR ID (required) — e.g., `003`

## Behavior

1. **Verify gate result** — read `{id}-gate-latest` from T2. Block if outcome is not PASSED.
2. **Update T2** (process authority) — set `status: "accepted"`, `accepted_date: "YYYY-MM-DD"`.
3. **Update file** — change frontmatter `status: draft` to `status: accepted`, add `accepted_date`.
4. **Update reviewed-by** — set to `self` if empty.
5. **Regenerate README** — update `{rdr_dir}/README.md` index.
6. **Stage files** — `git add` modified files.
7. **Planning handoff** (optional) — Auto-detect complexity:
   - Count `### Phase` headings in the RDR's Implementation Plan section
   - If 2+ phases → default yes; if 0-1 phases or no Implementation Plan → default no
   - Ask: "Invoke strategic planner to build execution beads? (y/n) [default]"
   - **If yes:**
     1. Write T1 scratch entry tagged `rdr-planning-context` with RDR ID and planning metadata: Use scratch tool: action="put", content="RDR {id}: planning context for {title}", tags="rdr-planning-context,rdr-{id}"
     2. Dispatch `strategic-planner` agent via Task tool with relay:
        - Task: Create phased execution plan for RDR-{id}: {title}
        - Bead: Create new epic
        - Input Artifacts: RDR file path, T2 metadata
        - T1 scratch has RDR content and planning context tagged `rdr-planning-context`
     3. Chain proceeds: strategic-planner → plan-auditor → plan-enricher (automatic via successor enforcement)
     4. Plan-enricher writes epic bead ID to T2 at chain end
   - **If no:** Continue — no beads created

## Agent Invocation

Steps 1-6 execute directly — no agent delegation. Step 7 (planning handoff) optionally dispatches the `strategic-planner` agent, which chains to `plan-auditor` then `plan-enricher` via successor enforcement. The accept skill writes a T1 scratch entry tagged `rdr-planning-context` before dispatch to enable the plan-auditor → plan-enricher routing (RDR-036 F-14).

**Important**: Step 2 (T2 `status: accepted` write) must complete before Step 7 (planner dispatch) to satisfy RDR-024 Guardrail 2 (strategic-planner pre-check warns if RDR not accepted).

## Success Criteria

- [ ] T2 gate result verified as PASSED before accepting
- [ ] T2 metadata updated with status=accepted and accepted_date
- [ ] File frontmatter updated to match T2
- [ ] README index regenerated
- [ ] Files staged via git add
- [ ] Planning handoff prompt shown with correct default (phase count heuristic)
- [ ] T1 scratch entry written with rdr-planning-context tag before planner dispatch
- [ ] Strategic-planner dispatched with relay template (if user accepts)

## Agent-Specific PRODUCE

Outputs produced directly by this skill (Steps 1-6):

- **T2 memory**: Updated status record via memory_put tool: project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,accepted"
- **Filesystem**: Updated RDR markdown (frontmatter `status: accepted`, `accepted_date`), regenerated `{rdr_dir}/README.md`
- **T1 scratch**: Use scratch tool: action="put", content="RDR NNN: accepted YYYY-MM-DD" for ephemeral tracking during multi-step acceptance flow
- **T1 scratch**: rdr-planning-context tag entry via scratch tool (for plan-auditor successor routing)
- **Agent dispatch**: strategic-planner via Task tool (optional, user-confirmed)
