---
allowed-tools: Bash
description: Accept a gated RDR — verifies gate PASSED in T2, updates status to accepted
---

# RDR Accept

!`nx rdr preamble rdr-accept -- '$ARGUMENTS'`

## RDR to Accept

$ARGUMENTS

## Action

> **PROHIBITION — PLANNING CHAIN INTEGRITY**
> You MUST NOT create beads, write plans, enrich beads, or perform any planning/enrichment work yourself.
> You are the **caller only**. Running `bd create`, `bd dep add`, `bd update --description`,
> or writing plan content in the Planning Chain is a HARD STOP — halt and report the error.
> Only the dispatched subagent (strategic-planner) and the MCP tool calls (nx_plan_audit, nx_enrich_beads) do this work.
> Doing it yourself bypasses the audit and enrichment chain, producing unvalidated plans.
>
> **SUBAGENT FAILURE**: If any subagent in the chain fails or returns partial results,
> you MUST NOT compensate by doing the subagent's work yourself. Report the failure,
> state which step broke, and provide the retry command. "Let me finish this directly"
> is the exact behavior this prohibition exists to prevent.
>
> If the Agent tool is not available (e.g., you are a subagent), report:
> "Cannot dispatch planning chain — Agent tool unavailable. Run /conexus:rdr-accept from the main conversation."

All RDR metadata is pre-loaded above. Step 7 requires additional tool calls for planning dispatch.

**Notation**: All references to `<ID>` below mean the **RDR ID** value from the script output (e.g. `027`). All references to `<type>` mean the **Type** value (e.g. `design`). Substitute with the actual values.

**Before executing steps below**, call both T2 lookups listed above (memory_get for metadata and gate result). You need these results for Steps 1–2.

- **Step 1 — T2 idempotency and self-healing**: Compare the **File Status** (from script output above) against the **T2 metadata status** (from your memory_get call):
  - **Both show `accepted`**: True no-op. Print `> RDR is already accepted (file and T2 agree). Nothing to do.` and stop.
  - **File shows `accepted`, T2 does not**: Self-healing — update T2 to match file. Print `> Self-healing: file shows accepted but T2 shows <actual-T2-status>. Updating T2.` Use memory_put to set T2 status to `accepted`. Then stop (no further steps needed).
  - **T2 shows `accepted`, file shows `draft`**: Self-healing — this is the ledger-drift case (RDR-165/166). Print `> Self-healing: T2 shows accepted but file shows draft. Updating file.` Run `nx rdr set-status <ID> accepted`, then `git add` the updated file + README. Then stop.
  - **File shows `draft` and T2 shows `draft` (or T2 record not found)**: Normal flow — proceed to Step 2.
- **Step 2 — Verify gate**: Check that the T2 gate result (from your memory_get call) shows `outcome: "PASSED"`. If the record exists but `outcome` is absent or is not `"PASSED"`, treat as BLOCKED. If no gate record exists at all, also BLOCKED. Report **BLOCKED** and stop. Print: `> Run /conexus:rdr-gate <ID> first.`
- **Step 3 — Update T2** (T2 is the process authority):
  Use **memory_put** tool: content="status: accepted\naccepted_date: <today YYYY-MM-DD>\ntitle: <title>\ntype: <type>\n(preserve other fields from T2 Metadata lookup)", project="<repo-name>_rdr" (same project as in the T2 Lookups above), title="<ID>", ttl="permanent", tags="rdr,<type>"
- **Step 4 — Flip the file frontmatter + README (code-enforced, do NOT hand-edit)**: Run `nx rdr set-status <ID> accepted`. This rewrites the RDR file `status: draft -> accepted`, adds `accepted_date`, and updates the README index-row status cell in one tested action. Hand-editing frontmatter is the source of the RDR-165/166 ledger drift (T2 advanced, file left at `draft`); always use the command so T2 and the file cannot diverge.
- **Step 5 — Update `reviewed-by`**: If `reviewed-by` is empty or placeholder, set to `self` (solo review). (Frontmatter edit; the CLI does not touch this field.)
- **Step 6 — Stage files**: `git add` the modified RDR file and `<rdr-dir>/README.md`.
- **Step 7 — Planning handoff**: Use the step count and recommendation from the script output above.
  - **If step_count >= 2**: The planning chain is **MANDATORY**. Do not ask — print `> Multi-step RDR — dispatching planning chain (mandatory).` and proceed to the Planning Chain below.
  - **If step_count < 2**: Ask: "Invoke strategic planner to build execution beads? (y/n) [default: yes]"
    - **If no:** Accept is complete. Print: `> RDR <ID> accepted. Ready for implementation.`
    - **If yes:** Proceed to the Planning Chain below.

---

### Planning Chain (triggered from Step 7 above)

Execute these steps sequentially when the planning handoff triggers (mandatory multi-phase or user opted in). **Reminder: you are the caller. Do NOT create beads or plans yourself.**

**Step 7a — Write T1 context:**
Write T1 scratch entry: Use scratch tool: action="put", content="RDR-<ID>: planning context for <title>. RDR file: <RDR-file-path>", tags="rdr-planning-context,rdr-<ID>"

**Step 7b — Dispatch strategic-planner (MANDATORY — do NOT do this yourself):**
Dispatch `strategic-planner` agent (via Agent tool, subagent_type="conexus:strategic-planner") with prompt:
> Create phased execution plan for RDR-<ID>: <title>. RDR file: <RDR-file-path>. Read the RDR content for implementation phases. Create epic and task beads with dependencies.

**Wait for the planner to complete before proceeding.**
Note the plan file path and bead IDs from the planner's output.
**If the planner did not create beads, this is a failure — report it and stop. The RDR acceptance is still valid. To retry the planning chain only, run `/conexus:create-plan` manually with the RDR file path.**

**Step 7c — Call `mcp__plugin_conexus_nexus__nx_plan_audit` (MANDATORY — do NOT skip):**
After the planner completes, call:
```
mcp__plugin_conexus_nexus__nx_plan_audit(
    plan_json="<serialized plan from planner output>",
    context="RDR-<ID>: <title>. T1 scratch has rdr-planning-context tag."
)
```
(RDR-080 — no agent spawn; MCP tool executes in-process)

**Step 7d — Call `mcp__plugin_conexus_nexus__nx_enrich_beads` (MANDATORY — do NOT skip):**
After the audit completes, call:
```
mcp__plugin_conexus_nexus__nx_enrich_beads(
    bead_description="RDR-<ID>: <title>",
    context="<audit findings from step 7c, if any>"
)
```
(RDR-080 — no agent spawn; MCP tool executes in-process)

**Step 7e — Verify chain completion:**
Confirm the chain ran: planner created beads, nx_plan_audit validated, nx_enrich_beads enriched.
Print: `> RDR-<ID> accepted. Planning chain complete: planner → nx_plan_audit → nx_enrich_beads. Use 'bd ready' to see executable tasks.`
**If any step was skipped or failed, report which step broke the chain and provide the retry command.**
