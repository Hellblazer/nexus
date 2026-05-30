---
allowed-tools: Bash
description: Implement feature using developer agent
---

# Implementation Request

!`nx command-context implement`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Task to Implement

$ARGUMENTS

## Action

**PREREQUISITE**: Plan must be validated by mcp__plugin_conexus_nexus__nx_plan_audit (RDR-080) before implementation.

Invoke the **development** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: developer

**Task**: Implement "$ARGUMENTS" using TDD methodology
**Bead**: [fill from active in_progress bead above]

### Input Artifacts
- Files: [fill from existing files to modify or target package]

### Plan Context
[fill from approved mcp__plugin_conexus_nexus__nx_plan_audit (RDR-080) output]

### Requirements
$ARGUMENTS

### Deliverable
Working implementation with passing tests, following TDD red-green-refactor cycle.

### Quality Criteria
- [ ] Tests written before implementation (TDD)
- [ ] All tests pass (run the project's test command from CLAUDE.md)
- [ ] Code follows project conventions (check CLAUDE.md)
- [ ] No regressions introduced in existing tests

**IMPORTANT**: The developer agent implements and hands back; it cannot run reviewers or commit. After it returns, the development skill's orchestrator MUST drive the full tail: dispatch **code-review-expert** AND **substantive-critic** (both, they catch different issue classes), gate on both returning clean, then commit. A clean code review does not excuse skipping the critic. See the development skill's "Post-Implementation Review + Commit" section.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
