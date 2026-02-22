---
name: codebase-analysis
description: >
  Analyze codebase structure, patterns, and architecture. Triggers when:
  exploring new codebase, onboarding to project, asking "how does X work",
  "where is Y defined", before major refactoring, or understanding module structure.
allowed-tools: Task, Read, Glob, Grep, Bash
memory: project
context: fork
# See ~/.claude/registry.yaml for full agent metadata
---

# Codebase Analysis Skill

Delegates to the **codebase-deep-analyzer** agent (model: sonnet).

## When This Skill Activates

- Exploring a new codebase or module
- Onboarding to a project
- Trying to understand how a feature works
- Before undertaking major refactoring
- When asking "how does X work?" or "where is Y defined?"
- Understanding dependencies between components

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

## Analysis Methodology

The codebase-deep-analyzer uses sequential thinking:
1. Form hypothesis about architecture (e.g., "This appears to be MVC pattern")
2. Gather evidence from code structure, naming, dependencies
3. Validate/refute hypothesis against actual code
4. Map module/package structure and document patterns
5. Persist findings to nx store: `echo "..." | nx store put - --collection knowledge --title "architecture-{project}-{component}" --tags "architecture"`

## Success Criteria

- [ ] Architecture overview documented
- [ ] Module/component map created
- [ ] Key patterns identified and explained
- [ ] Dependency relationships mapped
- [ ] Findings stored in nx store for future reference
