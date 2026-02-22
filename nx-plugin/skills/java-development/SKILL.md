---
name: java-development
description: >
  Implement Java features using TDD methodology. Triggers: plan approved,
  user says "implement", bead in_progress for implementation, executing plan phase.
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash, LSP
memory: project
# See ~/.claude/registry.yaml for full agent metadata
---

# Java Development Skill

Delegates to the **java-developer** agent (sonnet). See [registry.yaml](../../registry.yaml).

## LSP Usage Patterns

**CRITICAL**: Use LSP for code navigation instead of text search (900x faster).

- **Before modifying interfaces**: Use `goToImplementation` to find all implementers
- **Before refactoring methods**: Use `findReferences` to find all callers
- **Understanding dependencies**: Use `hover` for quick type info and JavaDoc
- **Finding method definitions**: Use `goToDefinition` instead of Grep
- **Class structure**: Use `documentSymbol` for method/field inventory
- **Prefer LSP over Grep** for all symbol navigation tasks

### Example Workflow
```
1. Read plan requirement
2. Use LSP.documentSymbol to understand existing class structure
3. Use LSP.goToDefinition to examine dependencies
4. Write failing test (TDD)
5. Use LSP.findReferences to check impact of changes
6. Implement solution
```

## When This Skill Activates

- After plan-auditor approves a plan (required prerequisite)
- When user says "implement", "write code", "build this"
- When bead for implementation task is in_progress
- Executing a phase from approved plan
- Writing or modifying production Java code

## Agent Invocation

## Relay Template (Use This Format)

When invoking this agent via Task tool, use this exact structure:

```markdown
## Relay: {agent-name}

**Task**: [1-2 sentence summary of what needs to be done]
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- Files: [key files or "none"]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] [Criterion 3]
```

**Required**: All fields must be present. Agent will validate relay before starting.

For additional optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## TDD Methodology

The java-developer agent follows test-driven development:
1. Write failing test that defines expected behavior
2. Implement minimum code to pass the test
3. Refactor while keeping tests green
4. Repeat for each requirement
5. Ensure all existing tests still pass

## Java 24 Standards

- `var` for local variables where appropriate
- Virtual threads for concurrent operations
- Record classes for data carriers
- Pattern matching for instanceof
- No `synchronized` (use concurrent collections)
- Dynamic ports in tests

## Success Criteria

- [ ] All tests written and passing (TDD)
- [ ] Code follows project conventions
- [ ] No regressions in existing tests
- [ ] Implementation matches plan requirements
- [ ] Ready for code-review-expert relay
