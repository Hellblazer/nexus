---
id: RDR-025
title: "Generalize Java Agents to Language-Agnostic Developer/Debugger/Architect-Planner"
type: enhancement
status: closed
priority: P1
created: 2026-03-08
accepted_date: 2026-03-08
closed_date: 2026-03-08
close_reason: implemented
reviewed_by: self
related_issues: ["RDR-023"]
---

# RDR-025: Generalize Java Agents to Language-Agnostic Agents

## Problem Statement

The nx plugin has three Java-specific agents: `java-developer`, `java-debugger`,
and `java-architect-planner`. These agents are unusable for projects in Python,
Go, Rust, TypeScript, or any other language — even though 75-80% of their content
(debugging methodology, TDD workflow, architecture patterns, relay reception,
beads integration) is already language-generic.

Users working in non-Java projects have no developer, debugger, or architect
agent available. The Java-specific knowledge (Java 24 patterns, Maven, JMH) is
hard-coded in agent system prompts rather than delegated to project-level
configuration (CLAUDE.md), which is the industry-standard approach.

**Root cause**: Agent identity and language-specific conventions are embedded in
system prompts rather than read from project configuration at runtime.

## Scope

1. **Rename and generalize three agents**:
   - `java-developer` → `developer`
   - `java-debugger` → `debugger`
   - `java-architect-planner` → `architect-planner`

2. **Replace Java-specific sections** with CLAUDE.md delegation pattern
   (read project config at runtime, adapt to detected language)

3. **Update all cross-references** — every file in the plugin that names the
   old agents (see complete table below)

4. **Add a preflight check** to `nx:nx-preflight` that validates CLAUDE.md has
   the sections agents need (language, build system, test command)

5. **Update plugin registry, skills, and commands** with new names

## Research Findings

### Finding 1: Only 20-25% of each agent is Java-specific

Line-by-line analysis of all three agents confirms:

| Agent | Java-specific | Generic |
|-------|--------------|---------|
| java-developer | Identity line, Maven Mastery section, Java Coding Standards section, `mvn test` in completion protocol, escalation triggers naming `java-debugger`/`java-architect-planner` | Relay Reception, Prior Implementation Search, Core Principles (Test-First, Spartan Design), 5-step workflow, Beads Integration, Context Protocol |
| java-debugger | Identity line, Technical Expertise section (JMH, JUnit 5, Mockito), SLF4J/Logback, Maven dependency conflicts, successor relay naming `java-developer` | Core Debugging Philosophy, 8-thought Bug Investigation Pattern, 7-step Methodology, Evidence Collection, Documentation Strategy |
| java-architect-planner | Identity line, Usage Examples (Java 24, Spring), Architectural Expertise (synchronized prohibition, Maven multi-module), successor relay naming `java-developer` | Planning Methodology, Architecture Analysis Pattern, Execution Principles, Quality Assurance, Documentation Requirements |

### Finding 2: Existing agents already demonstrate the polymorphic pattern

- **code-review-expert**: "Step 0: Pattern Baseline" — reads the codebase to
  discover conventions before evaluating. Explicitly adapts to "language-specific
  conventions and idioms." Never mentions a specific language.
- **test-validator**: Lists Maven as an example, then includes "For projects with
  custom test runners: Identify the test command from CLAUDE.md or build configuration."
  Hybrid pattern: named examples + explicit CLAUDE.md fallback.

### Finding 3: Industry consensus — no per-language agents

- **Aider**: 100+ languages, single polymorphic agent, tree-sitter for detection
- **Cursor**: Cursor Rules (`.cursor/rules`) for language customization, not per-language agents
- **Claude Code**: CLAUDE.md is the documented mechanism for project-specific context
- **Academic literature**: Multi-agent systems differentiate by *role* (planning vs execution), not by programming language

### Finding 4: CLAUDE.md delegation is the correct abstraction layer

Specialization belongs at the project configuration level, not the agent definition
level. CLAUDE.md already exists in every project, is authoritative for that project's
stack, and provides deterministic (explicit) rather than inferential language detection.

### Finding 5: A preflight check prevents degraded agent quality

When CLAUDE.md is thin or absent, polymorphic agents produce lower quality output
because they lack language/build/test context. A preflight check at session start
warns early rather than failing silently during agent work.

## Decision: Approach D — Polymorphic + CLAUDE.md Delegation

### Agent Renames

| Old Name | New Name | Skill Command (old) | Skill Command (new) |
|----------|----------|--------------------|--------------------|
| java-developer | developer | `/java-implement` | `/nx:implement` |
| java-debugger | debugger | `/java-debug` | `/nx:debug` |
| java-architect-planner | architect-planner | `/java-architecture` | `/nx:architecture` |

### Content Replacement Strategy

For each agent, replace Java-specific sections with:

1. **Identity line**: Generic role + "Read CLAUDE.md to identify language, build system, and conventions"
2. **Tool/framework sections**: CLAUDE.md lookup + fallback detection from build files (`pom.xml`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `package.json`)
3. **Coding standards**: "Consult CLAUDE.md for project-specific conventions. Defaults when silent: prefer immutability, favor composition over inheritance, use modern language idioms"
4. **Build/test commands**: Top 5 language examples + "check CLAUDE.md" as primary source
5. **Cross-references and escalation triggers**: Update all `java-developer` → `developer`, `java-debugger` → `debugger`, `java-architect-planner` → `architect-planner` — including Successor Enforcement and Automatic Escalation Triggers sections within each agent

### Language Detection Pattern (all three agents)

```
Before starting work:
1. Read CLAUDE.md for language, build system, test command, coding conventions
2. If CLAUDE.md doesn't specify: detect from build files
   (pom.xml → Java/Maven, pyproject.toml → Python/uv, go.mod → Go,
    Cargo.toml → Rust, package.json → Node.js/TypeScript)
3. If detection fails: ask the user
```

### Preflight Check (extend nx:nx-preflight)

Add a CLAUDE.md validation section to the existing `nx:nx-preflight` skill.
Detection heuristics (case-insensitive grep):

- **Language**: grep for `Python`, `Java`, `Go`, `Rust`, `TypeScript`, `Node.js`,
  `C++`, `C#`, `Ruby`, `Kotlin`, `Swift`, `Scala`
- **Build system**: grep for `uv`, `maven`, `mvn`, `cargo`, `go build`, `go mod`,
  `npm`, `yarn`, `pnpm`, `gradle`, `make`, `cmake`
- **Test command**: grep for `pytest`, `mvn test`, `go test`, `cargo test`,
  `npm test`, `jest`, `vitest`, `make test`

Output format:
```
CLAUDE.md Agent Readiness:
  [x] CLAUDE.md exists
  [x] Language detected: Python 3.12+
  [x] Build system detected: uv
  [x] Test command detected: uv run pytest
  [?] Coding conventions section: not found (optional)
```

Items marked `[?]` are warnings, not errors. Agents function without them but
with reduced quality.

## Cross-Reference Updates (Complete)

### Primary agent files (content rewrites — 20-25%)

| File | Changes |
|------|---------|
| `nx/agents/java-developer.md` | Rename to `developer.md`, rewrite identity/Maven/standards/escalation sections |
| `nx/agents/java-debugger.md` | Rename to `debugger.md`, rewrite identity/expertise/tool sections |
| `nx/agents/java-architect-planner.md` | Rename to `architect-planner.md`, rewrite identity/expertise/examples |

### Agent cross-references (name substitution)

| File | References to update |
|------|---------------------|
| `nx/agents/orchestrator.md` | Routing table, Standard Pipelines |
| `nx/agents/strategic-planner.md` | Relationship section |
| `nx/agents/test-validator.md` | Relationship section |
| `nx/agents/plan-auditor.md` | Successor Enforcement conditions, routing logic |
| `nx/agents/deep-analyst.md` | Relationship section |
| `nx/agents/deep-research-synthesizer.md` | Relationship section |
| `nx/agents/codebase-deep-analyzer.md` | Verify for Java/Maven references |
| `nx/agents/_shared/CONTEXT_PROTOCOL.md` | Proactive search agents list, relay-reliant agents list |
| `nx/agents/_shared/MAINTENANCE.md` | Example references |

### Skills and commands (rename + content update)

| File | Changes |
|------|---------|
| `nx/skills/java-development/SKILL.md` | Rename dir to `development/`, update relay target |
| `nx/skills/java-debugging/SKILL.md` | Rename dir to `debugging/`, update relay target |
| `nx/skills/java-architecture/SKILL.md` | Rename dir to `architecture/`, update relay target |
| `nx/skills/orchestration/SKILL.md` | Routing diagram nodes + routing table |
| `nx/skills/using-nx-skills/SKILL.md` | Skill directory table entries |
| `nx/commands/java-implement.md` | Rename to `implement.md`, update relay target |
| `nx/commands/java-debug.md` | Rename to `debug.md`, update relay target |
| `nx/commands/java-architecture.md` | Rename to `architecture.md`, update relay target |

### Plugin infrastructure

| File | Changes |
|------|---------|
| `nx/registry.yaml` | Agent entries, predecessor/successor chains, all 5 pipeline definitions, model_summary, naming_aliases |
| `nx/README.md` | Agent table, pipeline descriptions, entry points section |
| `nx/hooks/hooks.json` | Verify no Java-specific references |

### Verification command

After all changes: `grep -rl "java-developer\|java-debugger\|java-architect" nx/`
should return zero results (excluding changelogs and RDR docs).

## Migration Impact

**Slash command changes**: `/java-implement` → `/nx:implement`, `/java-debug` → `/nx:debug`,
`/java-architecture` → `/nx:architecture`. Old commands will stop working immediately.
No aliases — all references are plugin-internal and updated atomically in one PR.
Users with muscle memory for the old commands will need to adjust.

**RDR-023 consistency**: The `tools` frontmatter added by RDR-023 is embedded in
the agent file header. File rename preserves it intact. RDR-023's closed
documentation uses old agent names — acceptable since it's a permanent record.

**plan-auditor routing simplification**: Currently routes "Java projects →
java-architect-planner, others → java-developer." After rename, this becomes
"architectural design needed → architect-planner; otherwise → developer" —
simpler and language-agnostic.

## Resolved Questions

**Q1** (backwards-compat aliases): **No aliases.** All references to agent names
are plugin-internal (Agent tool `subagent_type`, skills, orchestrator, registry).
All updated atomically in one PR. No external consumers reference agent names by
string. Users invoke via skill commands, not agent names directly.

**Q2** (preflight location): **Extend `nx:nx-preflight`.** Same category as
existing dependency checks — "is this project ready for nx agents?" One command
for all readiness checks.

**Q3** (build system examples): **Top 5 languages** (Java, Python, Go, Rust,
TypeScript). Each example adds ~10 tokens. CLAUDE.md-first lookup handles anything
not listed. Exhaustive lists (30+ build systems) add ~300 tokens with diminishing
returns.

## Success Criteria

- [ ] All three agents renamed and generalized (no Java-specific content in prompts)
- [ ] `grep -rl "java-developer\|java-debugger\|java-architect" nx/` returns zero results (excluding changelogs)
- [ ] Agents function correctly for at least 3 non-Java languages (Python, Go, TypeScript)
- [ ] Preflight check warns when CLAUDE.md lacks language/build/test sections
- [ ] Nexus project's own CLAUDE.md passes the preflight check without warnings
- [ ] Plugin registry updated with new names and descriptions
