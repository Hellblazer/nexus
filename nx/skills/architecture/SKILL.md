---
name: architecture
description: Use when complex features need architectural design before implementation, or when system design decisions span multiple modules
---

# Architecture Skill

Delegates to the **architect-planner** agent (model: opus).

## Code Navigation

**REQUIRED SUB-SKILL:** Use **nx:serena-code-nav** for symbol-level architecture discovery. Combine with `nx search --hybrid` for semantic discovery — Serena for precision, nx search for conceptual queries.

- **Map system structure**: `jet_brains_get_symbols_overview` for class/interface inventories without reading files
- **Find architectural patterns**: `jet_brains_find_referencing_symbols` to trace abstraction usage across the codebase
- **Understand module boundaries**: `jet_brains_find_referencing_symbols` to track cross-module calls
- **Interface analysis**: `jet_brains_type_hierarchy` to see full implementation tree
- **Serena for precision, nx search for semantic discovery**

### Architecture Discovery Workflow
```
1. nx search --corpus code --hybrid (30-50 results) for semantic discovery
2. jet_brains_get_symbols_overview to map key classes in discovered files
3. jet_brains_type_hierarchy to trace abstraction patterns
4. jet_brains_find_referencing_symbols to understand cross-module usage
5. Synthesize findings with `mcp__sequential-thinking__sequentialthinking`
6. Design architecture with clear boundaries
```

## When This Skill Activates

- Before implementing complex features requiring architecture design
- When planning multi-step implementations
- Before major refactoring efforts
- When system design decisions are needed
- When establishing module boundaries or interfaces

## Pipeline Position

```
strategic-planner -> plan-auditor -> architect-planner -> developer
```

## Agent Invocation

Use the Task tool to invoke **architect-planner**:

```markdown
## Relay: architect-planner

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Architecture design with execution plan

### Quality Criteria
- [ ] Component boundaries clearly defined
- [ ] Interfaces specified
- [ ] Execution plan created with beads
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Architecture Methodology

The architect-planner uses `nx search --corpus code --hybrid` for discovery (30-50 results), then `mcp__sequential-thinking__sequentialthinking`:
1. Understand system architecture and integration patterns
2. Synthesize findings into architectural approach
3. Define component boundaries and interfaces
4. Create execution plan with beads
5. Identify risks and mitigations

## Success Criteria

- [ ] All requirements addressed in design
- [ ] Component boundaries clearly defined
- [ ] Interfaces specified
- [ ] Execution plan created with beads
- [ ] Beads created for trackable work
- [ ] Risks identified with mitigations
- [ ] Ready for plan-auditor validation

## Agent-Specific PRODUCE

- **Architecture Designs**: Store in nx T3 via store_put tool: content="# Architecture: {component}\n{design}", collection="knowledge", title="architecture-{project}-{component}", tags="architecture,design"
- **Execution Plans**: Store in nx T2 memory via memory_put tool: content="plan", project="{project}", title="plan-{component}.md", ttl="30d"
- **Design Decisions**: Store in nx T3 via store_put tool: content="# Decision: {topic}\n{rationale}", collection="knowledge", title="decision-architect-{topic}", tags="decision,architecture"
- **Beads**: Epic → Phase → Task hierarchy with `bd dep add` for dependencies
- **Design Notes**: Use T1 scratch for working notes during architecture analysis:
  - scratch tool: action="put", content="Design consideration: {note}", tags="architecture,design"
  - scratch_manage tool: action="flag", id="<id>", project="{project}", title="architecture-notes.md"
