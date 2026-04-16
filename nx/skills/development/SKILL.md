---
name: development
description: Use when a plan has been approved and implementation work is ready to begin, before writing production code
effort: medium
---

# Development Skill

Delegates to the **developer** agent (sonnet). See [registry.yaml](../../registry.yaml).

## Code Navigation

**REQUIRED SUB-SKILL:** Use **nx:serena-code-nav** for all symbol-level navigation — finding definitions, callers, type hierarchies, and surgical edits. Serena replaces text-pattern Grep for any symbol task.

- **Before modifying interfaces**: `find_referencing_symbols` to find all implementers and callers
- **Before refactoring methods**: `find_referencing_symbols` to find all callers
- **Class structure**: `get_symbols_overview` for method/field inventory without reading the file
- **Finding method definitions**: `find_symbol` instead of Grep
- **Replacing a method body**: `replace_symbol_body` — no line arithmetic, immune to drift

### Example Workflow
```
1. Read plan requirement
2. get_symbols_overview to understand existing class structure
3. find_symbol to locate dependencies
4. Write failing test (TDD)
5. find_referencing_symbols to check impact of changes
6. Implement with replace_symbol_body or insert_before/after_symbol
```

## When This Skill Activates

- After `mcp__plugin_nx_nexus__nx_plan_audit` validates a plan (required prerequisite — RDR-080)
- When a bead for an implementation task is in_progress
- Executing tasks from an approved implementation plan
- Writing or modifying production code

## Pre-Dispatch: Seed Link Context

Before dispatching the developer agent, seed T1 scratch with link targets so the auto-linker can create catalog links when the agent stores findings. See `/nx:catalog` skill for full reference.

1. If the task references an RDR (pattern `RDR-\d+`), resolve it: `mcp__plugin_nx_nexus-catalog__search(query="RDR-NNN")`
2. Check T1 scratch for `rdr-planning-context` (set by strategic-planner for RDR-driven beads)
3. Seed: `mcp__plugin_nx_nexus__scratch(action="put", content='{"targets": [{"tumbler": "<tumbler>", "link_type": "implements"}], "source_agent": "developer"}', tags="link-context")`
4. If no RDR/document reference found, skip seeding (auto-linker handles empty context gracefully)

## Agent Invocation

Use the Agent tool to invoke **developer**:

```markdown
## Relay: developer

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Working implementation with tests

### Quality Criteria
- [ ] All tests written and passing (TDD)
- [ ] Code follows project conventions
- [ ] No regressions in existing tests
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Debugger Escalation

If the developer agent returns with `## ESCALATION: Debugger Required` in its output (detect by scanning for `<!-- ESCALATION -->` or the literal string `ESCALATION: Debugger Required`):

1. **Do not re-dispatch the developer.** The circuit breaker fired for a reason.
2. **Escalation guard — check before dispatching:**
   mcp__plugin_nx_nexus__scratch(action="search", query="circuit-breaker-fired-for-[bead-id]", limit=1
   - **If found**: do NOT dispatch the debugger. Report to the user: "Developer circuit breaker has fired twice for bead [ID]. The debugger's fix did not resolve the issue. Human investigation recommended." **Stop here.**
   - **If not found**: write the guard entry NOW: mcp__plugin_nx_nexus__scratch(action="put", content="circuit-breaker-fired-for-[bead-id]", tags="escalation-guard". Then proceed to step 3.
3. **Dispatch the debugger** using this relay:

```markdown
## Relay: debugger

**Task**: Diagnose test failure that developer could not resolve: [Failing test(s) from escalation]
**Bead**: [same bead as developer]

### Input Artifacts
- Error: [Error field from escalation report]
- Hypothesis: [Hypothesis field from escalation report]
- What was tried: [What I tried field — both attempts]
- Diagnostic suggestion: [Diagnostic suggestion field]
- nx scratch: [search scratch for tag "failed-approach" — include any pre-escalation entries the developer wrote during earlier attempts]
- Files: [files from original developer relay]

### Deliverable
Root cause analysis and fix with all tests passing

### Quality Criteria
- [ ] Root cause identified with evidence
- [ ] Fix implemented
- [ ] All failing tests now pass
```

4. After the debugger resolves the issue, re-dispatch the developer using this relay:

```markdown
## Relay: developer (resumed after debugger)

**Task**: Resume implementation. Debugger resolved: [one-sentence summary of fix]. Continue from [remaining plan step].
**Bead**: [same bead]

### Input Artifacts
- nx store: [debugger's debug-finding title, if stored]
- nx memory: [{project}/debug-journal.md, if stored]
- Files: [originally affected files + any files the debugger modified]

### Deliverable
Complete remaining implementation steps with all tests passing

### Context Notes
Circuit breaker previously fired. Debugger root cause: [one sentence].
Do not retry approaches listed in scratch under tag "failed-approach".

### Quality Criteria
- [ ] All tests pass (including the previously failing ones)
- [ ] Remaining plan steps completed
```

## Post-Implementation Review

Code review steps are baked into plans by the strategic planner. When
executing a plan, follow the review tasks at the designated points.
For ad-hoc implementation outside a plan, use `/nx:review-code` when
the scope warrants it.

## TDD Methodology

The developer agent follows test-driven development:
1. Write failing test that defines expected behavior
2. Implement minimum code to pass the test
3. Refactor while keeping tests green
4. Repeat for each requirement
5. Ensure all existing tests still pass

**REQUIRED:** Run verification (tests pass, no regressions) before claiming any task is done.
**REQUIRED SUB-SKILL:** Use `/nx:review-code` after implementation for quality review.

## Success Criteria

- [ ] All tests written and passing (TDD)
- [ ] Code follows project conventions
- [ ] No regressions in existing tests
- [ ] Implementation matches plan requirements
- [ ] Ready for code-review-expert relay

## Agent-Specific PRODUCE

Outputs generated by the developer agent:

- **T2 memory**: Implementation checkpoints promoted when validated via scratch_manage tool: action="promote", entry_id="<id>", project="{project}", title="impl-checkpoints.md"
- **T1 scratch**: Implementation checkpoints via scratch tool: action="put", content="Checkpoint: {component} implemented, tests pass", tags="impl" (promote to T2 when validated)
- **Code**: Implemented files with passing tests; relay to code-review-expert on completion

**Session Scratch (T1)**: Agent uses scratch tool for ephemeral working notes during the session. Flagged items auto-promote to T2 at session end.
