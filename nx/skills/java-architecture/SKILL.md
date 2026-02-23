---
name: java-architecture
description: >
  Design comprehensive Java architecture and create phased execution plans. Triggers when:
  starting complex features requiring architectural design, planning multi-phase implementations,
  before major refactoring, or when system design decisions are needed.
# See ../../registry.yaml for full agent metadata
allowed-tools: Task, Read, Glob, Grep, Bash, LSP
memory: project
---

# Java Architecture Skill

Delegates to the **java-architect-planner** agent (model: opus).

## LSP Usage Patterns

**CRITICAL**: Use LSP for rapid architecture discovery (combine with `nx search --hybrid` for context).

- **Map system structure**: Use `documentSymbol` for class/interface inventories
- **Find architectural patterns**: Use `goToImplementation` to trace abstraction usage
- **Understand module boundaries**: Use `findReferences` to track cross-module calls
- **Discover dependencies**: Use `hover` to understand type relationships
- **Interface analysis**: Use `goToImplementation` to see all concrete implementations
- **Use LSP for precision, nx search for semantic discovery**

### Architecture Discovery Workflow
```
1. Use nx search --corpus code --hybrid (30-50 results) for semantic discovery
2. Use LSP.documentSymbol to map key classes
3. Use LSP.goToImplementation to trace patterns
4. Use LSP.findReferences to understand usage
5. Synthesize findings with sequential thinking
6. Design architecture with clear boundaries
```

## When This Skill Activates

- Before implementing complex features requiring architecture design
- When planning multi-phase implementations
- Before major refactoring efforts
- When system design decisions are needed
- When establishing module boundaries or interfaces

## Pipeline Position

```
strategic-planner -> plan-auditor -> java-architect-planner -> java-developer
```

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
- nx scratch: [scratch IDs or "none"]           # optional: ephemeral T1 items
- nx pm context: [Phase N, active blockers or "none"]  # optional: from nx pm status
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

## Architecture Methodology

The java-architect-planner uses `nx search --corpus code --hybrid` for discovery (30-50 results), then sequential thinking:
1. Understand system architecture and integration patterns
2. Synthesize findings into architectural approach
3. Define component boundaries and interfaces
4. Create phased execution plan with beads
5. Identify risks and mitigations

## Success Criteria

- [ ] All requirements addressed in design
- [ ] Component boundaries clearly defined
- [ ] Interfaces specified
- [ ] Execution plan with phases created
- [ ] Beads created for trackable work
- [ ] Risks identified with mitigations
- [ ] Ready for plan-auditor validation

## Agent-Specific PRODUCE

- **Architecture Designs**: Store in nx T3 as `printf "# Architecture: {component}\n{design}\n" | nx store put - --collection knowledge --title "architecture-{project}-{component}" --tags "architecture,design"`
- **Execution Plans**: Store in nx T2 memory as `nx memory put "plan" --project {project}_active --title phase-N.md --ttl 30d`
- **Design Decisions**: Store in nx T3 as `printf "# Decision: {topic}\n{rationale}\n" | nx store put - --collection knowledge --title "decision-architect-{topic}" --tags "decision,architecture"`
- **Beads**: Epic → Phase → Task hierarchy with `bd dep add` for dependencies
- **Design Notes**: Use T1 scratch for working notes during architecture analysis:
  ```bash
  nx scratch put "Design consideration: {note}" --tags "architecture,design"
  nx scratch flag <id> --project {project}_active --title architecture-notes.md
  ```
