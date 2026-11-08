---
name: writing-nx-skills
description: Use when creating new conexus plugin skills, editing existing skills, verifying skill quality before committing, or promoting a memory file into a skill, script or hook
effort: low
---

# Writing Conexus Skills

## Overview

Guide for authoring skills in the conexus Claude Code plugin. Skills are reference guides for proven techniques, patterns, or tools that help future Claude instances find and apply effective approaches.

**Methodology:** TDD-for-documentation — write pressure scenarios (test cases), verify baseline behavior without the skill, then write the skill and verify compliance.

## When This Skill Activates

- Creating a new conexus plugin skill, or editing an existing one.
- Verifying a skill's quality before committing it.
- Promoting a memory file into a skill, a Lua script or a hook: a file whose "How to apply" is a procedure, ordered steps, over about 8 KB, or a bright line. See Promoting a Memory File below.

## Skill Conventions

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

### Skill Types

**Agent-delegating skills** (majority): Invoke a specific agent via Agent tool relay.
CI-enforced required sections (`test_plugin_structure.py` will fail without these):
- **Agent Invocation** heading (or **Relay Template** heading) — cross-reference to RELAY_TEMPLATE.md
- `## Success Criteria`
- `## Agent-Specific PRODUCE` — T1/T2/T3 outputs
- Mention of `scratch` (T1 scratch tier acknowledgment)

Also expected:
- `## When This Skill Activates`

**Hybrid skills**: Do significant direct work AND dispatch agents. These are still classified as agent-delegating — they must have all four CI-enforced sections above. If a skill dispatches agents at any point, it is agent-delegating.

**Standalone skills** (nexus, serena-code-nav, cli-controller, brainstorming-gate, writing-nx-skills): Provide guidance directly without agent delegation. Listed in `_STANDALONE_SKILLS` in [tests/test_plugin_structure.py](https://github.com/Hellblazer/nexus/blob/main/tests/test_plugin_structure.py).
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
- `**REQUIRED SUB-SKILL:** Use /conexus:<skill-name> for [purpose]`
- `**REQUIRED BACKGROUND:** Understand /conexus:<skill-name> before using this skill`

Never use `@` syntax (force-loads files, burns context).

### Relay Cross-Reference

Agent-delegating skills reference the canonical relay template:
```markdown
## Agent Invocation

Use the Agent tool with standardized relay format.
See [${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY_TEMPLATE.md](../../resources/agent-shared/RELAY_TEMPLATE.md) for required fields and examples.
```

Do NOT inline the relay template. One source of truth: `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY_TEMPLATE.md`.

### Storage Tier References

Skills that produce outputs must document which tiers they use:
- **T1 scratch**: scratch tool: action="put", content="...", tags="..." — session-scoped ephemeral notes
- **T2 memory**: memory_put tool: content="...", project="{repo}", title="file.md" — cross-session state
- **T3 knowledge**: store_put tool: content="...", collection="<subject>", title="..." — permanent validated findings

### Registry Integration

After creating a skill, update [conexus/registry.yaml](https://github.com/Hellblazer/nexus/blob/main/conexus/registry.yaml):
- Agent-delegating: under `agents:` — add model, skill, slash_command, triggers, and entry in `model_summary`
- Standalone: under `standalone_skills:` with tools and triggers
- RDR: under `rdr_skills:` with slash_command and triggers
- If slash command differs from skill name: add to `naming_aliases`

### Routing a new skill

Add it to [conexus/skills/orchestration/reference.md](https://github.com/Hellblazer/nexus/blob/main/conexus/skills/orchestration/reference.md) § Skill Routing so it is discoverable. The SessionStart text (`GUIDANCE_IMPERATIVE` in [src/nexus/session_start_guidance.py](https://github.com/Hellblazer/nexus/blob/main/src/nexus/session_start_guidance.py), PyPI wheel) names only the situations and the handful of skills and agents that answer them; it is not a menu. Add a skill there only when it answers a situation the imperative already lists, and mind its byte budget ([tests/test_session_start_guidance.py](https://github.com/Hellblazer/nexus/blob/main/tests/test_session_start_guidance.py), [tests/hooks/test_session_start_combined_budget.py](https://github.com/Hellblazer/nexus/blob/main/tests/hooks/test_session_start_combined_budget.py)). Wrapper skills that only dispatch an agent the model already dispatches directly do not earn a place: the six that did (code-review, debugging, development, test-validation, query, using-nx-skills) were retired with zero model loads in 30 days (nexus-dg7i6.6).

## Promoting a Memory File

A memory file is read when someone remembers to read it, and its index line has to summarise the whole procedure. Moving a procedure into a skill, a script or a hook fixes that. A move that shrinks the source can also drop a rule, so a promotion follows five parts, in this order (RDR-240).

### The criterion

A memory file is a candidate when its "How to apply" is any of:

- a procedure, written as ordered steps;
- longer than about 8 KB;
- a bright line, a rule that must hold every time.

### The target

What the memory holds decides where it goes:

| The memory holds | Target | Why | Example |
| --- | --- | --- | --- |
| A procedure that needs judgment | Skill | Loads on demand; the model applies it | release checklist, review gate |
| A fixed sequence with one right answer | Lua script in a procedure library | Runs the same way every time; nothing to misremember | what to check before a push |
| A bright line that must always hold | Hook | Memory is context, a hook is enforcement | no `git stash`, no full-disk `find` |

A procedure library is a directory of Lua scripts, one `<name>.lua` per procedure, each with a four-line header that names its arguments and the verbs it may call. One runner executes them in a sandbox, through the `procedure_run(name, args)` tool. A procedure only one repo needs goes in that repo's library (`.nexus/procedures/`). A practice every conexus user should follow goes in the library that ships with `nx`. Personal rules stay as memory.

A script target counts only once `procedure_run` ships in a released package. Until then the source stays whole, or the target is a skill.

A file that holds both kinds, a fixed part and a judgment part, is split by part. Each part gets its own target, and the source keeps what no target holds. Example: `feedback_test_runs_before_push.md` holds a fixed sequence, what to check before a push, and a judgment part. The fixed sequence becomes a script (`preflight`) and the judgment part becomes a skill. Until `procedure_run` ships, the fixed part stays in the source or goes into the skill with the judgment part.

For a hook target, read the `hook-development` skill (plugin-dev) and write the test first: a test that trips on the bright line, before the hook exists.

### Citing the sources

The target cites its source files, each by file path or T2 title. The source shrinks to the rule and its why, plus a pointer. The pointer names the target by what runs it: a skill by its `/conexus:` name, a script by its path and the call that runs it (`.nexus/procedures/preflight.lua`, `procedure_run("preflight", ...)`), a hook by its file. The source is not deleted. In Bidirectional Memory-Skill Learning, the one paper that tests promotion, memory and skills together beat skills alone.

### The loss check

Before the source shrinks, dispatch `substantive-critic` with the target and the whole source file. For a split file, give it the union of the targets and the whole source file, once. The critic first enumerates the source's rules and maps each one to a line in a target, then lists every rule the target lacks. Brief it with "Ship when the list is empty". Empty means the critic reports 0 ship-blockers and lists nothing as lacking. Record the list, empty or not, on the bead. Promote one source file at a time, each with its own check. Unchecked compaction can score below having no context at all (Agentic Context Management), so a short target is not evidence that nothing was lost.

### The empty-list rule

A promotion lands only with an empty list. A rule on the list goes into the target, and the check runs again; the source does not shrink in between. The source shrinks in the same change that lands the target, and only after the list is empty and recorded on the bead. It does not shrink in a later change. The same check applies when the target is a script or a hook.

## Known Pitfalls

### bd update --description silently corrupts multi-line content

**Never** instruct agents to use `bd update <id> --description "..."` for multi-line or markdown content. Shell escaping silently destroys backticks (executed as command substitution), `$variables` (expanded to empty), and nested quotes (break argument boundaries). No error is raised — the command reports success with mangled data.

**Correct pattern**: Write content to a temp file via the Write tool, then use `--body-file`:

```
Step 1 — Write enriched content using the Write tool (file_path: /tmp/bead-<id>.md)
Step 2 — bd update <id> --body-file /tmp/bead-<id>.md
```

Short single-phrase values (e.g. `--design "revised scope"`) are safe. The bug only affects multi-line content with markdown formatting.

This applies to `--description`, `--notes`, `--design`, and any flag that accepts free-text content longer than a simple phrase. Use `--body-file` or `--design-file` respectively.

## Quality Checklist

**CI-enforced (tests will break):**
- [ ] Frontmatter has `name`, `description`, and `effort` — only these three fields
- [ ] Description starts with "Use when"
- [ ] Description has none of the CI-checked keywords: `Triggers:`, `user says`, `workflow`, `process:` (the literal `BAD_KEYWORDS` list in `test_every_skill_frontmatter_is_valid` — narrower than it once was; don't over-scrub prose that merely mentions a step count)
- [ ] No YAML comments in frontmatter
- [ ] Agent-delegating: has **Agent Invocation** heading (or Relay Template heading)
- [ ] Agent-delegating: has `## Agent-Specific PRODUCE` section
- [ ] Agent-delegating: mentions `scratch` somewhere in text
- [ ] Has `## Success Criteria` section
- [ ] All `../../resources/agent-shared/` cross-references resolve to existing files

**Convention (not CI-enforced but expected):**
- [ ] `effort` matches skill type (low=reference, medium=workflow, high=analysis)
- [ ] Added to [conexus/registry.yaml](https://github.com/Hellblazer/nexus/blob/main/conexus/registry.yaml) (including `model_summary` for agent skills)
- [ ] Named in the orchestration skill's `reference.md` § Skill Routing

**Verify before committing** (from a nexus checkout, where [tests/test_plugin_structure.py](https://github.com/Hellblazer/nexus/blob/main/tests/test_plugin_structure.py) lives):
```bash
uv run pytest <path to test_plugin_structure.py> -k "skill_name" -v
```

## Testing Skills

Run structural validation from a nexus checkout ([tests/test_plugin_structure.py](https://github.com/Hellblazer/nexus/blob/main/tests/test_plugin_structure.py)):
```bash
pytest <path to test_plugin_structure.py> -v -k "skill_name"
```

For behavioral testing, follow TDD-for-documentation — create pressure scenarios, test baseline without skill, verify compliance with skill.
