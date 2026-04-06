---
name: writing-nx-skills
description: Use when creating new nx plugin skills, editing existing skills, or verifying skill quality before committing
effort: low
---

# Writing nx Skills

## Overview

Guide for authoring skills in the nx Claude Code plugin. Skills are reference guides for proven techniques, patterns, or tools that help future Claude instances find and apply effective approaches.

**Methodology:** TDD-for-documentation — write pressure scenarios (test cases), verify baseline behavior without the skill, then write the skill and verify compliance.

## nx Skill Conventions

### Frontmatter

Three fields. Max 1024 characters total:

```yaml
---
name: skill-name-with-hyphens
description: Use when [specific triggering conditions]
effort: low|medium|high
---
```

- `name`: Letters, numbers, hyphens only
- `description`: MUST start with "Use when". Describe triggering conditions, NEVER summarize workflow.
- `effort`: Reasoning depth hint — `low` (reference/lookup), `medium` (guided workflow), `high` (deep analysis/critique)

### Skill Types in nx

**Agent-delegating skills** (majority): Invoke a specific agent via Agent tool relay.
CI-enforced required sections (`test_plugin_structure.py` will fail without these):
- **Agent Invocation** heading (or **Relay Template** heading) — cross-reference to RELAY_TEMPLATE.md
- `## Success Criteria`
- `## Agent-Specific PRODUCE` — T1/T2/T3 outputs
- Mention of `scratch` (T1 scratch tier acknowledgment)

Also expected:
- `## When This Skill Activates`

**Hybrid skills** (e.g., query): Do significant direct work AND dispatch agents. These are still classified as agent-delegating — they must have all four CI-enforced sections above. If a skill dispatches agents at any point, it is agent-delegating.

**Standalone skills** (nexus, serena-code-nav, cli-controller, brainstorming-gate, using-nx-skills, writing-nx-skills): Provide guidance directly without agent delegation. Listed in `_STANDALONE_SKILLS` in `tests/test_plugin_structure.py`.
Required sections:
- `## When This Skill Activates`
- `## Success Criteria` (optional for pure reference)

**Discipline skills** (brainstorming-gate): Enforce process rules.
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

Use the Agent tool with standardized relay format.
See [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md) for required fields and examples.
```

Do NOT inline the relay template. One source of truth: `agents/_shared/RELAY_TEMPLATE.md`.

### Storage Tier References

Skills that produce outputs must document which tiers they use:
- **T1 scratch**: scratch tool: action="put", content="...", tags="..." — session-scoped ephemeral notes
- **T2 memory**: memory_put tool: content="...", project="{repo}", title="file.md" — cross-session state
- **T3 knowledge**: store_put tool: content="...", collection="knowledge", title="..." — permanent validated findings

### Registry Integration

After creating a skill, update `nx/registry.yaml`:
- Agent-delegating: under `agents:` — add model, skill, slash_command, triggers, and entry in `model_summary`
- Standalone: under `standalone_skills:` with tools and triggers
- RDR: under `rdr_skills:` with slash_command and triggers
- If slash command differs from skill name: add to `naming_aliases`

### Updating using-nx-skills

When adding a new skill, also add it to `nx/skills/using-nx-skills/SKILL.md` routing tree. This file is injected every session — if your skill isn't listed there, it won't be discovered.

## Known Pitfalls

### /beads:update --description silently corrupts multi-line content

**Never** instruct agents to use `/beads:update <id> --description "..."` for multi-line or markdown content. Shell escaping silently destroys backticks (executed as command substitution), `$variables` (expanded to empty), and nested quotes (break argument boundaries). No error is raised — the command reports success with mangled data.

**Correct pattern**: Write content to a temp file via the Write tool, then use `--body-file`:

```
Step 1 — Write enriched content using the Write tool (file_path: /tmp/bead-<id>.md)
Step 2 — /beads:update <id> --body-file /tmp/bead-<id>.md
```

Short single-phrase values (e.g. `--design "revised scope"`) are safe. The bug only affects multi-line content with markdown formatting.

This applies to `--description`, `--notes`, `--design`, and any flag that accepts free-text content longer than a simple phrase. Use `--body-file` or `--design-file` respectively.

## Quality Checklist

**CI-enforced (tests will break):**
- [ ] Frontmatter has `name`, `description`, and `effort` — only these three fields
- [ ] Description starts with "Use when"
- [ ] Description has no workflow keywords (first, then, step, next, finally, after, before, workflow, process, pipeline)
- [ ] No YAML comments in frontmatter
- [ ] Agent-delegating: has **Agent Invocation** heading (or Relay Template heading)
- [ ] Agent-delegating: has `## Agent-Specific PRODUCE` section
- [ ] Agent-delegating: mentions `scratch` somewhere in text
- [ ] Has `## Success Criteria` section
- [ ] All `../../agents/_shared/` cross-references resolve to existing files

**Convention (not CI-enforced but expected):**
- [ ] `effort` matches skill type (low=reference, medium=workflow, high=analysis)
- [ ] Added to `nx/registry.yaml` (including `model_summary` for agent skills)
- [ ] Added to `using-nx-skills` routing tree

**Verify before committing:**
```bash
uv run pytest tests/test_plugin_structure.py -k "skill_name" -v
```

## Testing Skills

Run structural validation:
```bash
pytest tests/test_plugin_structure.py -v -k "skill_name"
```

For behavioral testing, follow TDD-for-documentation — create pressure scenarios, test baseline without skill, verify compliance with skill.
