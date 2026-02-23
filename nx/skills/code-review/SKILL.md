---
name: code-review
description: >
  Review code for quality, security, and best practices. Triggers when:
  completing feature implementation, fixing bugs, refactoring code,
  after git commit, before pull request, when code quality check is needed.
# See ../../registry.yaml for full agent metadata
allowed-tools: Task, Read, Glob, Grep, Bash
memory: user
---

# Code Review Skill

Delegates to the **code-review-expert** agent (model: sonnet).

## When This Skill Activates

- After writing or modifying significant code (10+ lines)
- When completing a feature or bug fix
- After refactoring existing code
- Before creating a pull request
- When code quality, security, or best practices review is needed

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

## Review Methodology

The code-review-expert agent uses hypothesis-driven review:
1. Form hypothesis about code quality patterns
2. Gather evidence from code structure, naming, patterns
3. Validate against best practices and security requirements
4. Document findings with file:line references

## Agent-Specific PRODUCE

- **Session Scratch (T1)**: `nx scratch put "<notes>" --tags "review"` — working review notes during session; flagged items auto-promote to T2 at session end
- **nx memory**: `nx memory put "..." --project {project}_active --title review-findings.md` — persistent review findings across sessions
- **nx store** (optional): `echo "..." | nx store put - --collection knowledge --title "pattern-code-{topic}" --tags "pattern,code-review"` — recurring violation patterns worth long-term storage
- **Beads**: creates bug beads (`bd create "..." -t bug`) for critical findings that require follow-up work

## Success Criteria

- [ ] All changed files analyzed
- [ ] Security vulnerabilities flagged
- [ ] Best practices validated
- [ ] Specific remediation guidance provided
- [ ] At least one positive feedback item included
- [ ] T2 memory updated with session findings (if multi-session work)

**Session Scratch (T1)**: Agent uses `nx scratch` for ephemeral working notes during the session. Flagged items auto-promote to T2 at session end.
