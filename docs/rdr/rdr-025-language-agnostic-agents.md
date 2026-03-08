---
id: RDR-025
title: "Generalize Java Agents to Language-Agnostic Developer/Debugger/Architect-Planner"
type: enhancement
status: draft
priority: P1
created: 2026-03-08
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

3. **Update cross-references** in orchestrator, test-validator, strategic-planner,
   codebase-deep-analyzer, and any skills that reference the old agent names

4. **Add a preflight/doctor check** that validates CLAUDE.md has the sections
   the agents need to function effectively (language, build system, test command,
   coding conventions)

5. **Update plugin registry** (plugin.json, descriptions, skill names)

## Research Findings

### Finding 1: Only 20-25% of each agent is Java-specific

Line-by-line analysis of all three agents confirms:

| Agent | Java-specific | Generic |
|-------|--------------|---------|
| java-developer | Identity line, Maven Mastery section, Java Coding Standards section, `mvn test` in completion protocol, cross-references | Relay Reception, Prior Implementation Search, Core Principles (Test-First, Spartan Design), 5-step workflow, Beads Integration, Context Protocol |
| java-debugger | Identity line, Technical Expertise section (JMH, JUnit 5, Mockito), SLF4J/Logback, Maven dependency conflicts, cross-references | Core Debugging Philosophy, 8-thought Bug Investigation Pattern, 7-step Methodology, Evidence Collection, Documentation Strategy |
| java-architect-planner | Identity line, Usage Examples (Java 24, Spring), Architectural Expertise (synchronized prohibition, Maven multi-module), cross-references | Planning Methodology, Architecture Analysis Pattern, Execution Principles, Quality Assurance, Documentation Requirements |

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
because they lack language/build/test context. A preflight command that checks for
required sections prevents this — warn at session start rather than fail silently
during agent work.

## Decision: Approach D — Polymorphic + CLAUDE.md Delegation

### Agent Renames

| Old Name | New Name | Skill Command (old) | Skill Command (new) |
|----------|----------|--------------------|--------------------|
| java-developer | developer | `/java-implement` | `/implement` |
| java-debugger | debugger | `/java-debug` | `/debug` |
| java-architect-planner | architect-planner | `/java-architecture` | `/architecture` |

### Content Replacement Strategy

For each agent, replace Java-specific sections with:

1. **Identity line**: Generic role + "Read CLAUDE.md to identify language, build system, and conventions"
2. **Tool/framework sections**: CLAUDE.md lookup + fallback detection from build files (`pom.xml`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `package.json`)
3. **Coding standards**: "Consult CLAUDE.md for project-specific conventions. Defaults when silent: prefer immutability, favor composition over inheritance, use modern language idioms"
4. **Build/test commands**: Named examples per language + "check CLAUDE.md" as primary source
5. **Cross-references**: Update all `java-developer` → `developer`, etc.

### Language Detection Pattern (all three agents)

```
Before starting work:
1. Read CLAUDE.md for language, build system, test command, coding conventions
2. If CLAUDE.md doesn't specify: detect from build files
   (pom.xml → Java/Maven, pyproject.toml → Python/uv, go.mod → Go,
    Cargo.toml → Rust, package.json → Node.js/TypeScript)
3. If detection fails: ask the user
```

### Preflight/Doctor Check

Add to the existing `nx:nx-preflight` skill (or create a new `/claude-md-check`
command) a CLAUDE.md validation step:

```
CLAUDE.md Agent Readiness Check:
  [x] CLAUDE.md exists
  [?] Language/runtime specified (e.g., "Python 3.12+", "Java 24", "Go 1.22")
  [?] Build system specified (e.g., "uv", "Maven", "cargo")
  [?] Test command specified (e.g., "uv run pytest", "mvn test", "go test ./...")
  [?] Coding conventions section present

  Warnings:
  - "CLAUDE.md does not specify a test command. The developer and debugger
     agents will ask you for it at runtime."
```

Items marked `[?]` are warnings, not errors — the agents function without them
but with reduced quality.

## Cross-Reference Updates

| File | Change |
|------|--------|
| `nx/agents/orchestrator.md` | Routing table: `java-developer` → `developer`, etc. |
| `nx/agents/test-validator.md` | Relationship section cross-references |
| `nx/agents/strategic-planner.md` | "java-architect-planner" → "architect-planner" |
| `nx/agents/codebase-deep-analyzer.md` | Verify for Java/Maven references |
| `nx/skills/java-development/SKILL.md` | Rename to `development/SKILL.md` |
| `nx/skills/java-debugging/SKILL.md` | Rename to `debugging/SKILL.md` |
| `nx/skills/java-architecture/SKILL.md` | Rename to `architecture/SKILL.md` |
| Plugin registry (`registry.yaml` or equivalent) | Update agent/skill names |
| `nx/hooks/hooks.json` | Verify no Java-specific references |
| Using-nx-skills skill | Update skill directory table |

## Open Questions

**Q1**: Should the old `java-*` agent names be kept as aliases for backwards
compatibility, or removed entirely? Aliases add maintenance burden but prevent
breakage for users who reference them directly.

**Q2**: Should the preflight check be part of `nx:nx-preflight` (existing) or
a standalone `/claude-md-check` command? The existing preflight checks nx
dependencies; CLAUDE.md validation is a different concern.

**Q3**: For the developer agent, should the build system examples be exhaustive
(every language) or limited to the top 5 (Java, Python, Go, Rust, TypeScript)?
Exhaustive is more complete but adds token cost to every invocation.

## Success Criteria

- [ ] All three agents renamed and generalized (no Java-specific content in prompts)
- [ ] All cross-references updated (no "java-developer" strings remain in agents/skills)
- [ ] Agents function correctly for at least 3 non-Java languages (Python, Go, TypeScript)
- [ ] Preflight check warns when CLAUDE.md lacks language/build/test sections
- [ ] Existing Java projects continue to work (CLAUDE.md provides the Java context)
- [ ] Plugin registry updated with new names and descriptions
