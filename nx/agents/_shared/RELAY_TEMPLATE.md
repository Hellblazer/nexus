# Relay Template

All agent relays follow this standardized structure. Skills and agents reference this file instead of duplicating the template.

## Required Fields

| Field | Description | Example |
|-------|-------------|---------|
| **Task** | 1-2 sentence summary | "Review code changes for security issues" |
| **Bead** | Bead ID with status, or 'none' | "Delos-1234 (status: in_progress)" |
| **Input Artifacts** | Context being provided | nx store docs, files, nx memory |
| **Deliverable** | What the agent should produce | "Structured review with findings" |
| **Quality Criteria** | Checklist for success | Checkboxes of requirements |

## Template

```markdown
## Relay: [Target Agent]

**Task**: [1-2 sentence summary of what needs to be done]
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- nx scratch: [scratch IDs or "none"]       # optional: ephemeral T1 items for this pipeline run
- Files: [key files touched or relevant]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] [Criterion 3]
```

## Optional Fields

| Field | When to Use |
|-------|-------------|
| **Context Notes** | Special blockers, assumptions, warnings, or constraints |
| **Preceding Work** | What was done before this relay (if relevant) |
| **Time Constraints** | Deadlines or urgency indicators |
| **Related Beads** | Other beads that may be affected |
| **nx scratch** | Passing ephemeral scratch IDs to a downstream agent in the same pipeline session |

### Extended Template (with optional fields)

```markdown
## Relay: [Target Agent]

**Task**: [1-2 sentence summary]
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- nx scratch: [scratch IDs or "none"]       # optional: ephemeral T1 items for this pipeline run
- Files: [key files touched]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] [Criterion 3]

### Context Notes
[Special context, blockers, warnings, or constraints]

### Related Beads
- [Related bead ID and brief description]
```

## Validation Checklist

A valid relay MUST have:

1. [ ] Non-empty **Task** field (1-2 sentences)
2. [ ] **Bead** field present (even if 'none')
3. [ ] At least one **Input Artifact** listed (nx store/nx memory/Files)
4. [ ] **Deliverable** description (what success looks like)
5. [ ] At least one **Quality Criterion** in checkbox format: `- [ ] [criterion]`

**Quality Criteria Format Enforcement**:
- MUST use markdown checkbox syntax: `- [ ] [Criterion text]`
- NOT plain text: ~~"Quality Criteria: X, Y, Z"~~ (invalid)
- NOT bullets without checkboxes: ~~"- Criterion 1"~~ (invalid)
- Valid: `- [ ] All tests pass` ✓
- Valid: `- [ ] Code reviewed and approved` ✓

## Examples

### Good Relay

```markdown
## Relay: code-review-expert

**Task**: Review authentication refactoring for security issues and best practices
**Bead**: Delos-12345 (status: in_progress)

### Input Artifacts
- nx store: decision-architect-auth-redesign
- nx memory: Delos/phase2-auth.md
- Files: src/main/java/auth/AuthController.java, src/main/java/auth/TokenValidator.java

### Deliverable
Structured code review with findings categorized by severity (Critical/Important/Suggestion)

### Quality Criteria
- [ ] All changed files analyzed
- [ ] Security vulnerabilities flagged
- [ ] Authentication flow validated
- [ ] Specific remediation guidance provided
```

### Minimal Valid Relay

```markdown
## Relay: test-validator

**Task**: Verify test coverage for new utility methods
**Bead**: none

### Input Artifacts
- Files: src/main/java/utils/StringUtils.java

### Deliverable
Coverage report with gap identification

### Quality Criteria
- [ ] Coverage percentage calculated
- [ ] Missing test cases identified
```

## Usage in Skills

Skills should reference this template rather than inline it:

```markdown
## Agent Invocation

Use the Task tool with standardized relay format.
See [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md) for required fields.
```

## Usage in Agents

Agents receiving relays **MUST validate** before starting work:

```markdown
## Relay Reception (MANDATORY)

Before starting, validate relay contains:
1. [ ] Non-empty **Task** field (1-2 sentences)
2. [ ] **Bead** field present (ID with status, or 'none')
3. [ ] **Input Artifacts** section with at least one artifact listed
4. [ ] **Deliverable** description
5. [ ] At least one **Quality Criterion** in checkbox format: - [ ]

If validation fails:
1. Search nx T3 store for missing context: Use search tool: query="[task topic]", corpus="knowledge", n=5
2. Check nx T2 memory: Use memory_get tool: project="{project}", title="ACTIVE_INDEX.md"
3. Query active beads: bd list --status=in_progress
4. Flag incomplete relay in response to user
5. Proceed with available context, documenting assumptions

See [CONTEXT_PROTOCOL.md](./CONTEXT_PROTOCOL.md) RECOVER section for details.
```

**Validation is non-optional**. Agents must check relay structure before executing work.


## Relationship to Context Protocol

This template is a subset of the [Shared Context Protocol](./CONTEXT_PROTOCOL.md).
The Context Protocol covers the full lifecycle (RECEIVE, PRODUCE, RELAY, RECOVER).
This template focuses specifically on the RELAY structure.
