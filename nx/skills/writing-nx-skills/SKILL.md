---
name: writing-nx-skills
description: Use when creating new nx plugin skills, editing existing skills, or verifying skill quality before committing
---

# Writing nx Skills

## Overview

Guide for authoring skills in the nx Claude Code plugin. Skills are reference guides for proven techniques, patterns, or tools that help future Claude instances find and apply effective approaches.

**REQUIRED BACKGROUND:** Understand superpowers:writing-skills for the foundational TDD-for-documentation methodology. This skill adds nx-specific conventions on top.

## nx Skill Conventions

### Frontmatter

Only two fields. Max 1024 characters total:

```yaml
---
name: skill-name-with-hyphens
description: Use when [specific triggering conditions]
---
```

- `name`: Letters, numbers, hyphens only
- `description`: MUST start with "Use when". Describe triggering conditions, NEVER summarize workflow.

### Skill Types in nx

**Agent-delegating skills** (majority): Invoke a specific agent via Task tool relay.
Required sections:
- `## When This Skill Activates`
- `## Agent Invocation` with cross-reference to RELAY_TEMPLATE.md
- `## Success Criteria`
- `## Agent-Specific PRODUCE` (T1/T2/T3 outputs)

**Standalone skills** (nexus, cli-controller): Provide guidance directly without agent delegation.
Required sections:
- `## When This Skill Activates`
- `## Success Criteria` (optional for pure reference)

**Discipline skills** (brainstorming-gate, verification-before-completion): Enforce process rules.
Required sections:
- `<HARD-GATE>` or `<EXTREMELY-IMPORTANT>` blocks
- Rationalization prevention table
- Red flags list

### Cross-References

Use explicit markers, not file paths:
- `**REQUIRED SUB-SKILL:** Use nx:skill-name for [purpose]`
- `**REQUIRED BACKGROUND:** Understand nx:skill-name before using this skill`

Never use `@` syntax (force-loads files, burns context).

### Relay Cross-Reference

Agent-delegating skills reference the canonical relay template:
```markdown
## Agent Invocation

Use the Task tool with standardized relay format.
See [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md) for required fields and examples.
```

Do NOT inline the relay template. One source of truth: `agents/_shared/RELAY_TEMPLATE.md`.

### Storage Tier References

Skills that produce outputs must document which tiers they use:
- **T1 scratch**: `nx scratch put "..." --tags "..."` — session-scoped ephemeral notes
- **T2 memory**: `nx memory put "..." --project {repo} --title file.md` — cross-session state
- **T3 knowledge**: `nx store put ...` — permanent validated findings

### Registry Integration

After creating a skill, add it to `nx/registry.yaml`:
- Agent-delegating: under `agents:` with model, skill, slash_command, triggers
- Standalone: under `standalone_skills:` with tools and triggers
- RDR: under `rdr_skills:` with slash_command and triggers

## Quality Checklist

- [ ] Frontmatter has only `name` and `description`
- [ ] Description starts with "Use when"
- [ ] Description has no workflow summary
- [ ] Agent-delegating: has Agent Invocation cross-reference
- [ ] Agent-delegating: has Agent-Specific PRODUCE section
- [ ] Agent-delegating: mentions nx scratch (T1)
- [ ] Has Success Criteria section
- [ ] Added to registry.yaml
- [ ] Tests pass: `pytest tests/test_plugin_structure.py -v`

## Testing Skills

Run structural validation:
```bash
pytest tests/test_plugin_structure.py -v -k "skill_name"
```

For behavioral testing, follow the TDD-for-documentation approach from superpowers:writing-skills — create pressure scenarios, test baseline without skill, verify compliance with skill.
