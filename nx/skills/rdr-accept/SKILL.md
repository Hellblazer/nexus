---
name: rdr-accept
description: Use when a gated RDR returned PASSED and you want to officially accept it for implementation
effort: medium
---

# RDR Accept Skill

Accepts an RDR after it passes the gate. This is the author/reviewer decision point between gate validation and implementation.

## When This Skill Activates

- User says "accept this RDR", "mark as accepted", "approve the RDR"
- User invokes `/nx:rdr-accept`
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
7. **Planning handoff** (default: yes) — Auto-detect complexity:
   - Scan for any plan section (Implementation Plan, Approach, Steps, etc.)
   - Count step-like subheadings (Phase, Step, Stage, Part, or numbered ###)
   - If 2+ steps → mandatory; otherwise → default yes (opt-out, not opt-in)
   - Ask: "Invoke strategic planner to build execution beads? (y/n) [default: yes]"
   - **If no:** Continue — no beads created
   - **If yes — execute the full chain (3 sequential dispatches + catalog enrichment, orchestrated by this skill):**
     1. Write T1 scratch entry tagged `rdr-planning-context`: mcp__plugin_nx_nexus__scratch(action="put", content="RDR {id}: planning context for {title}. RDR file: {rdr_file}", tags="rdr-planning-context,rdr-{id}"
     2. **Dispatch strategic-planner** (Agent tool) — create phased plan with beads. **Wait for completion.**
     3. **Call `mcp__plugin_nx_nexus__nx_plan_audit`** — audit the plan against codebase. T1 scratch has rdr-planning-context tag.
     4. **Call `mcp__plugin_nx_nexus__nx_enrich_beads`** — enrich beads with execution context (+ audit findings from T1), write epic bead ID to T2.
     5. **Catalog links** (if catalog initialized): Search for related RDRs and create `relates` links:
        - `mcp__plugin_nx_nexus-catalog__search(query="<rdr-title-keywords>", content_type="rdr")`
        - For each result that is NOT this RDR: `mcp__plugin_nx_nexus-catalog__link(from_tumbler="<this-rdr-title>", to_tumbler="<related-rdr-tumbler>", link_type="relates", created_by="rdr-accept")`
        - Skip silently if catalog not initialized or RDR not yet indexed.
     6. Report chain completion to user.
   - **Important:** The strategic-planner dispatch is sequential. The MCP tool calls (nx_plan_audit, nx_enrich_beads) follow immediately after — no agent-to-agent relay needed.

## Agent Invocation

Steps 1-6 execute directly — no agent delegation. Step 7 (planning handoff) uses one agent dispatch and two MCP tool calls **sequentially**:
1. `strategic-planner` (Agent tool) — creates plan and beads
2. `mcp__plugin_nx_nexus__nx_plan_audit` — validates plan against codebase (RDR-080: no agent spawn)
3. `mcp__plugin_nx_nexus__nx_enrich_beads` — enriches beads with execution context (RDR-080: no agent spawn)

The accept skill writes a T1 scratch entry tagged `rdr-planning-context` before step 1.

**Important**: Step 2 (T2 `status: accepted` write) must complete before Step 7 (planner dispatch) to satisfy RDR-024 Guardrail 2 (strategic-planner pre-check warns if RDR not accepted).

## Success Criteria

- [ ] T2 gate result verified as PASSED before accepting
- [ ] T2 metadata updated with status=accepted and accepted_date
- [ ] File frontmatter updated to match T2
- [ ] README index regenerated
- [ ] Files staged via git add
- [ ] Planning handoff prompt shown with correct default (phase count heuristic)
- [ ] T1 scratch entry written with rdr-planning-context tag before planner dispatch
- [ ] Strategic-planner dispatched and completed (if user accepts)
- [ ] mcp__plugin_nx_nexus__nx_plan_audit called and result verified (if user accepts)
- [ ] mcp__plugin_nx_nexus__nx_enrich_beads called and beads enriched (if user accepts)

## Agent-Specific PRODUCE

Outputs produced directly by this skill (Steps 1-6):

- **T2 memory**: Updated status record via memory_put tool: project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,accepted"
- **Filesystem**: Updated RDR markdown (frontmatter `status: accepted`, `accepted_date`), regenerated `{rdr_dir}/README.md`
- **T1 scratch**: mcp__plugin_nx_nexus__scratch(action="put", content="RDR NNN: accepted YYYY-MM-DD" for ephemeral tracking during multi-step acceptance flow
- **T1 scratch**: rdr-planning-context tag entry via scratch tool (for nx_plan_audit context)
- **Agent dispatch + MCP calls**: strategic-planner (Agent tool) → `mcp__plugin_nx_nexus__nx_plan_audit` → `mcp__plugin_nx_nexus__nx_enrich_beads` (optional, user-confirmed, orchestrated by this skill — RDR-080)
