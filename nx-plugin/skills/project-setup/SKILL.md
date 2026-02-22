---
name: project-setup
description: >
  Create project management infrastructure for multi-week projects. Triggers:
  starting projects over 3 weeks, requiring phase tracking, user says "set up project".
# See ../../registry.yaml for full agent metadata
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash
---

# Project Setup Skill

Delegates to the **project-management-setup** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- When starting multi-week projects (>3 weeks)
- When systematic tracking and resumability is required
- When phase tracking is needed
- When knowledge integration infrastructure is required
- When user says "set up project management"

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

## Infrastructure Created

The project-management-setup agent creates (stored in Nexus T2 via `nx memory put`):

- `overview.md` — Project overview and goals
- `continuation.md` — Session resumption context (`nx pm resume` injects this)
- `phase-N.md` — Phase-specific objectives, tasks, success criteria
- `architecture.md` — Key design decisions (software projects)

Access via `nx pm resume` or `nx memory get --project <name>_active --title <doc>.md`.

## Success Criteria

- [ ] `nx pm init` completed; `nx pm status` returns project info
- [ ] Core T2 documents created (overview, continuation, phase docs)
- [ ] `nx pm resume` injects actionable context
- [ ] Epic bead created for project
- [ ] Phase beads created with dependencies
- [ ] Ready for strategic-planning to begin
