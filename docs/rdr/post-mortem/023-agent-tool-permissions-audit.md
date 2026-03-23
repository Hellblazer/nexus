# Post-Mortem: RDR-023 — Agent Tool Permissions Audit and Remediation

**Closed**: 2026-03-07
**Reason**: Implemented
**PR**: #74
**Epic**: nexus-qic4

## What Was Done

Added explicit `tools` frontmatter to all 14 nx agent `.md` files following
least-privilege assignments. Expanded the `PermissionRequest` hook to auto-approve
safe tool types (Read, Grep, Glob, Write, Edit, WebSearch, WebFetch, Agent,
sequential thinking) and expanded the Bash allowlist with git read commands,
`uv run pytest`, and additional `bd` subcommands.

## Plan vs. Actual Divergences

| Planned | Actual | Impact |
|---------|--------|--------|
| Sequential thinking only for agents that reference it | Added to all 14 agents uniformly (Option C) | Simpler, no agent excluded from reasoning primitive |
| Plan did not include sequential thinking in hook | Added auto-approve for sequential thinking MCP tool in hook | Required for defense-in-depth consistency |
| Phase 3 validation (nexus-ryjo) post-merge | Skipped — validation deferred to production observation | Post-conditions in success criteria remain unchecked |
| Phase 4 finalize RDR (nexus-if5a) after validation | RDR accepted before validation; closing now | Process deviation (see below) |

## Process Deviation: Implementation Before RDR Lifecycle

**This RDR was implemented before the formal RDR process was followed.** The
sequence was:

1. RDR-023 created as draft
2. Design brainstormed and approved
3. Implementation plan created and audited
4. **Phases 1+2 implemented, committed, PR opened**
5. RDR gate run (retroactively)
6. RDR accepted (retroactively)
7. RDR closed (now)

The correct sequence should have been:

1. RDR created as draft
2. Research findings recorded to T2
3. RDR gate run → PASSED
4. RDR accepted
5. Design + implementation plan created
6. Implementation executed
7. Validation completed
8. RDR closed with post-mortem

### Why This Happened

The RDR was created as a pre-existing draft that already contained proposed tool
assignments. The brainstorming gate (design exploration) was followed correctly,
but the RDR lifecycle gates (research → gate → accept) were not invoked before
implementation. The implementation felt "obvious" given the mechanical nature of
the changes (add one YAML line per file, expand a shell script), and the team
moved directly to execution.

### Suggested Guardrails

1. **Skill check in brainstorming-gate**: After design approval, before invoking
   `strategic-planning`, the brainstorming-gate skill should check whether the
   work is tracked by an RDR. If so, it should verify the RDR is in `accepted`
   status before allowing the planning skill to proceed. If the RDR is still
   `draft`, it should prompt: "RDR-NNN is still draft. Run `/nx:rdr-gate NNN` before
   planning implementation."

2. **Strategic planner pre-check**: The strategic-planner agent could verify that
   any referenced RDR is in `accepted` status before creating an implementation
   plan. This catches the case where the brainstorming gate is skipped.

3. **bd create hook**: When creating beads that reference an RDR, the bead context
   hook could warn if the referenced RDR is not yet accepted.

## Unverified Assumption

**Q2 remains open**: Whether `tools` frontmatter enforces access at the Claude Code
runtime level (vs. only affecting the system prompt) is unverified. The hook
expansion provides guaranteed enforcement regardless. Production observation will
confirm — if an agent without Bash in its tools list still executes shell commands,
the `tools` field is system-prompt-only and the hook is the sole enforcement layer.

## Outcome

The knowledge-tidier agent (and all other agents) should now have their tool
permissions correctly configured. The PermissionRequest hook is the guaranteed
enforcement layer; the tools frontmatter provides documentation and may also
enforce. Defense-in-depth: both layers must agree for a tool to be available.
