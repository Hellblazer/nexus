# RDR-025 Implementation Plan: Generalize Java Agents to Language-Agnostic

**Epic**: nexus-s42m (RDR-025: Generalize Java Agents to Language-Agnostic Developer/Debugger/Architect-Planner)
**RDR**: docs/rdr/rdr-025-language-agnostic-agents.md (status: accepted)
**Branch**: `feature/nexus-s42m-language-agnostic-agents`
**Estimated effort**: ~3 hours
**Constraint**: Single PR, squash-merge to main (no broken refs in intermediate commits)

## Executive Summary

Rename 3 Java-specific agents to language-agnostic names, rewrite 20-25% of their
content to use CLAUDE.md delegation instead of hard-coded Java knowledge, update 28
files containing cross-references, and add a CLAUDE.md readiness check to the
nx-preflight command. All changes ship in a single atomic PR.

## Dependency Graph

```
Phase 1: Setup (nexus-txg0)
    |
    +---> Phase 2.1: developer agent rewrite (nexus-migt) ----+
    |                                                          |
    +---> Phase 2.2: debugger agent rewrite (nexus-s7oe) -----+---> Phase 5.1: agent cross-refs (nexus-bp0k)
    |                                                          |---> Phase 5.2: shared protocols (nexus-f1mb)
    +---> Phase 2.3: architect-planner rewrite (nexus-d3me) ---+---> Phase 5.4: command cross-refs (nexus-b54i)
              |              |             |
              v              v             v
    Phase 3.1 (nexus-booa) 3.2 (pruf)  3.3 (4g4o) ---> Phase 5.3: skill cross-refs (nexus-c25y)
    Phase 4.1 (nexus-jmzk) 4.2 (vr8o)  4.3 (t86e)
              |              |             |
              +--------------+-------------+
              v
    Phase 6.1: registry.yaml (nexus-k3nx)
              |
              v
    Phase 6.2: README.md (nexus-7dwk)
              |
              v
    Phase 7.1: preflight check (nexus-3i0r) [independent, parallel-safe]
              |
              v
    Phase 8.1: verification + PR (nexus-m5tm)
```

**Critical path**: Phase 1 -> Phase 2 (any) -> Phase 3/4 -> Phase 6.1 -> Phase 6.2 -> Phase 8.1

**Parallelization**: Phase 2.1/2.2/2.3 can run in parallel. Phase 3 and 4 can run in parallel per-agent. Phase 5 subtasks can run in parallel. Phase 7 is independent of Phases 3-6.

---

## Phase 1: Setup

### Task 1.1: Create branch and verify baseline
**Bead**: nexus-txg0
**Estimated**: 5 min

**Steps**:
1. `git checkout -b feature/nexus-s42m-language-agnostic-agents`
2. Run baseline count:
   ```bash
   grep -rl "java-developer\|java-debugger\|java-architect" nx/ | wc -l
   grep -rl "java-developer\|java-debugger\|java-architect" nx/ | sort
   ```
3. Record the count (expected: ~28 files) for post-verification

**Success criteria**:
- [ ] Feature branch created
- [ ] Baseline reference count documented

---

## Phase 2: Agent File Renames + Content Rewrites

This is the most complex phase. Each agent requires ~20-25% content rewrite.

### Task 2.1: Rewrite developer agent
**Bead**: nexus-migt
**File**: `nx/agents/java-developer.md` -> `nx/agents/developer.md`
**Estimated**: 20 min

**Steps**:
1. `git mv nx/agents/java-developer.md nx/agents/developer.md`
2. Update frontmatter:
   - `name: developer`
   - `description`: Remove "Java" — "Executes development tasks using test-first methodology..."
3. **Replace identity paragraph** (line 61):
   - REMOVE: "elite Java architect and Maven expert with deep expertise in Java 24 patterns, JSRs, and modern development practices"
   - ADD: "expert software developer who adapts to any language and build system. Read CLAUDE.md to identify the project's language, build system, test framework, and coding conventions before starting work."
4. **Replace Maven Mastery paragraph** (line 69):
   - REMOVE entire paragraph
   - ADD Language Detection Pattern:
     ```
     **Project Detection**: Before starting work:
     1. Read CLAUDE.md for language, build system, test command, coding conventions
     2. If CLAUDE.md doesn't specify: detect from build files
        (pom.xml -> Java/Maven, pyproject.toml -> Python/uv, go.mod -> Go,
         Cargo.toml -> Rust, package.json -> Node.js/TypeScript)
     3. If detection fails: ask the user
     ```
5. **Replace Java Coding Standards** (lines 75-82):
   - REMOVE all 7 Java-specific bullet points
   - ADD:
     ```
     **Coding Standards**:
     - Consult CLAUDE.md for project-specific conventions (this is authoritative)
     - When CLAUDE.md is silent, apply sensible defaults:
       - Prefer immutability and value objects
       - Favor composition over inheritance
       - Use modern language idioms (var/auto inference, pattern matching, etc.)
       - Write clean, readable code that favors clarity over cleverness
     ```
6. **Replace completion protocol** (line 213 `mvn test`):
   - REMOVE: "All tests pass (mvn test)"
   - ADD: "All tests pass (run the project's test command from CLAUDE.md)"
7. **Update escalation triggers** (lines 192-208):
   - REPLACE: `java-debugger` -> `debugger`
   - REPLACE: `java-architect-planner` -> `architect-planner`
8. **Update relay protocol** (lines 222-237):
   - REPLACE: `java-architect-planner` -> `architect-planner`
   - REPLACE: `java-debugger` -> `debugger`
9. **Update relationship section** (lines 235-236):
   - REPLACE: `java-architect-planner` -> `architect-planner`
   - REPLACE: `java-debugger` -> `debugger`
10. Preserve `tools` frontmatter from RDR-023
11. **Update Usage Examples** (line 13): Replace "Intermittent NPEs in service layer" with generic example (e.g., "Intermittent crashes in service layer under load with unclear root cause")
12. **Generalize escalation exception type** (line 195): Replace "NullPointerException with unclear cause" -> "Exception with unclear cause"
13. **Remove Java tag** (line 135): Replace `--tags "insight,java"` -> `--tags "insight"` in Agent-Specific PRODUCE section

**Success criteria**:
- [ ] File renamed to `developer.md`
- [ ] No "Java" in identity, coding standards, or build sections
- [ ] CLAUDE.md delegation pattern present
- [ ] Language Detection Pattern included
- [ ] All internal cross-references use new agent names
- [ ] `tools` frontmatter preserved

### Task 2.2: Rewrite debugger agent
**Bead**: nexus-s7oe
**File**: `nx/agents/java-debugger.md` -> `nx/agents/debugger.md`
**Estimated**: 20 min

**Steps**:
1. `git mv nx/agents/java-debugger.md nx/agents/debugger.md`
2. Update frontmatter:
   - `name: debugger`
   - `description`: "Systematically investigates bugs, test failures, and performance issues using hypothesis-driven debugging..."
3. **Replace identity paragraph** (line 54):
   - REMOVE: "elite Java debugging specialist with deep expertise in modern Java 24 patterns, concurrent programming"
   - ADD: "expert debugging specialist who adapts to any language and runtime. Read CLAUDE.md to identify the project's language, test framework, logging infrastructure, and debugging tools before starting investigation."
4. **Replace Technical Expertise** (lines 79-83):
   - REMOVE: Java 24 features list, JUnit 5, Mockito, JMH, JavaFX, LWJGL, Protocol Buffers
   - ADD:
     ```
     **Technical Expertise:**
     - Consult CLAUDE.md for language-specific patterns, test frameworks, and profiling tools
     - Common debugging across ecosystems: concurrency issues, resource leaks, type errors,
       serialization failures, dependency conflicts
     - Build system diagnostics (Maven, uv/pip, Go modules, Cargo, npm — detect from build files)
     ```
5. **Replace Investigation Tools** (lines 95-99):
   - REMOVE: SLF4J/Logback, JMH
   - ADD:
     ```
     **Investigation Tools:**
     - **Logging**: Use the project's logging framework (check CLAUDE.md or imports)
     - **Strategic Instrumentation**: Temporary print/log statements for immediate feedback
     - **Performance Profiling**: Use language-appropriate profilers (check CLAUDE.md)
     - **Test-Driven Debugging**: Create focused tests to isolate and reproduce issues
     - **Memory Analysis**: Use `nx memory` as persistent scratch pad for organizing findings
     ```
6. **Replace Code Analysis Approach** (lines 107-112):
   - REMOVE: "AutoCloseable implementations", "vectorized algorithm implementations for SIMD", "Maven dependency conflicts"
   - ADD generic: "resource management patterns", "dependency version conflicts", "build system configuration"
7. **Update successor enforcement** (line 140):
   - REPLACE: `java-developer` -> `developer`
8. **Update relay/relationship sections**:
   - REPLACE: all `java-developer` -> `developer`
   - REPLACE: "Java-specific debugging" -> "language-specific debugging" in relationship section
9. **Update Usage Examples** (line 12): Replace "NullPointerException Investigation: NPE in data processor" with generic example (e.g., "Exception Investigation: Crashes in data processor when handling certain input patterns")

**Success criteria**:
- [ ] File renamed to `debugger.md`
- [ ] No Java-specific tool/framework references in expertise or investigation sections
- [ ] CLAUDE.md delegation pattern present
- [ ] Successor enforcement updated to `developer`

### Task 2.3: Rewrite architect-planner agent
**Bead**: nexus-d3me
**File**: `nx/agents/java-architect-planner.md` -> `nx/agents/architect-planner.md`
**Estimated**: 20 min

**Steps**:
1. `git mv nx/agents/java-architect-planner.md nx/agents/architect-planner.md`
2. Update frontmatter:
   - `name: architect-planner`
   - `description`: "Designs comprehensive software architecture and creates phased execution plans..."
3. **Replace identity paragraph** (line 41):
   - REMOVE: "elite Java architect and strategic planner with deep expertise in Java 24 patterns"
   - ADD: "expert software architect and strategic planner who adapts to any language and build system. Read CLAUDE.md to identify the project's language, build system, module structure, and architectural patterns before starting design work."
4. **Replace Usage Examples** (lines 12-14):
   - REMOVE: "Java 24", "Spring application", "Java with comprehensive testing"
   - ADD generic examples: "scalable microservice architecture for real-time data processing", "modernize legacy monolith to modular architecture", "implement distributed consensus algorithm with comprehensive testing"
5. **Replace Architectural Expertise** (lines 52-57):
   - REMOVE: "Java 24 features: records, pattern matching, virtual threads, var inference", "never use synchronized", "Maven multi-module builds"
   - ADD:
     ```
     **Architectural Expertise:**
     - Consult CLAUDE.md for language-specific patterns, module systems, and build conventions
     - Apply modern patterns: microservices, event-driven architecture, reactive programming
     - Design for scalability, maintainability, and performance
     - Leverage language-idiomatic concurrency patterns (check CLAUDE.md for project conventions)
     - Integrate with the project's build system and module structure
     ```
6. **Update sequential thinking constraints** (line 68):
   - REMOVE: "Java 24 patterns, existing structure"
   - ADD: "language idioms, existing structure, CLAUDE.md conventions"
7. **Update successor enforcement** (line 162):
   - REPLACE: `java-developer` -> `developer`
8. **Update relationship section** (lines 199-201):
   - REPLACE: `java-developer` -> `developer`
   - Update strategic-planner relationship: "You focus on language-specific architecture" -> "You focus on technical architecture and design patterns"

**Success criteria**:
- [ ] File renamed to `architect-planner.md`
- [ ] No Java-specific patterns, framework refs, or language constraints
- [ ] CLAUDE.md delegation pattern present
- [ ] Successor enforcement updated to `developer`

---

## Phase 3: Skill Directory Renames

### Task 3.1: Rename development skill
**Bead**: nexus-booa
**Estimated**: 7 min

**Steps**:
1. `git mv nx/skills/java-development/ nx/skills/development/`
2. Update `development/SKILL.md`:
   - `name: development`
   - `description`: Remove "Java" — "Use when a plan has been approved and implementation work is ready to begin"
   - Replace "Java Development Skill" heading -> "Development Skill"
   - Replace "java-developer" -> "developer" (all occurrences)
   - Remove "Java 24 Standards" section entirely (lines 73-80)
   - Update Agent-Specific PRODUCE: "java-developer agent" -> "developer agent"

**Success criteria**:
- [ ] Directory renamed to `development/`
- [ ] No "java-developer" or "Java 24" references in SKILL.md
- [ ] All relay targets reference `developer`

### Task 3.2: Rename debugging skill
**Bead**: nexus-pruf
**Estimated**: 7 min

**Steps**:
1. `git mv nx/skills/java-debugging/ nx/skills/debugging/`
2. Update `debugging/SKILL.md`:
   - `name: debugging`
   - `description`: Remove "Java" — "Use when tests fail, exceptions occur, or behavior is non-deterministic..."
   - Replace "Java Debugging Skill" heading -> "Debugging Skill"
   - Replace "java-debugger" -> "debugger" (all occurrences)
   - Update trigger descriptions to be language-agnostic
   - Update Agent-Specific PRODUCE: "java-debugger agent" -> "debugger agent"

**Success criteria**:
- [ ] Directory renamed to `debugging/`
- [ ] No "java-debugger" references in SKILL.md

### Task 3.3: Rename architecture skill
**Bead**: nexus-4g4o
**Estimated**: 7 min

**Steps**:
1. `git mv nx/skills/java-architecture/ nx/skills/architecture/`
2. Update `architecture/SKILL.md`:
   - `name: architecture`
   - `description`: Remove "Java" — "Use when complex features need architectural design before implementation..."
   - Replace "Java Architecture Skill" heading -> "Architecture Skill"
   - Replace "java-architect-planner" -> "architect-planner" (all occurrences)
   - Replace "java-developer" -> "developer" in pipeline position
   - Update Architecture Methodology description

**Success criteria**:
- [ ] Directory renamed to `architecture/`
- [ ] No "java-architect-planner" or "java-developer" references in SKILL.md

---

## Phase 4: Command File Renames

### Task 4.1: Rename implement command
**Bead**: nexus-jmzk
**Estimated**: 7 min

**Steps**:
1. `git mv nx/commands/java-implement.md nx/commands/implement.md`
2. Update content:
   - `description`: "Implement feature using developer agent"
   - Replace relay target: `java-developer` -> `developer`
   - Replace skill reference: `java-development` -> `development`
   - Expand project detection beyond Maven/Gradle:
     Add `pyproject.toml`, `go.mod`, `Cargo.toml`, `package.json` checks
   - Generalize quality criteria: remove "Java 24, FP32-only" specifics
   - Generalize deliverable: remove "Java 24 patterns (var, modern concurrency, no synchronized)"

**Success criteria**:
- [ ] File renamed to `implement.md`
- [ ] Relay targets `developer` agent
- [ ] Project detection covers 5+ build systems

### Task 4.2: Rename debug command
**Bead**: nexus-vr8o
**Estimated**: 7 min

**Steps**:
1. `git mv nx/commands/java-debug.md nx/commands/debug.md`
2. Update content:
   - `description`: "Debug test failures using debugger agent"
   - Replace relay target: `java-debugger` -> `debugger`
   - Replace skill reference: `java-debugging` -> `debugging`
   - Generalize test failure detection beyond surefire:
     Add pytest output (`*.xml` in `reports/`), `go test` stderr, `cargo test` output
   - Replace "surefire-reports" references with generic test output detection

**Success criteria**:
- [ ] File renamed to `debug.md`
- [ ] Relay targets `debugger` agent
- [ ] Test failure detection is language-agnostic

### Task 4.3: Rename architecture command
**Bead**: nexus-t86e
**Estimated**: 7 min

**Steps**:
1. `git mv nx/commands/java-architecture.md nx/commands/architecture.md`
2. Update content:
   - `description`: "Design architecture and create phased execution plans using architect-planner agent"
   - Replace relay target: `java-architect-planner` -> `architect-planner`
   - Replace skill reference: `java-architecture` -> `architecture`
   - Expand project detection beyond Maven/Gradle
   - Update pipeline position string
   - Generalize quality criteria: remove "Java 24 patterns" specifics

**Success criteria**:
- [ ] File renamed to `architecture.md`
- [ ] Relay targets `architect-planner` agent
- [ ] Project detection covers multiple build systems

---

## Phase 5: Cross-Reference Updates

### Task 5.1: Update agent cross-references (7 files)
**Bead**: nexus-bp0k
**Estimated**: 15 min

**Files and specific changes**:

| File | Changes |
|------|---------|
| `orchestrator.md` | Routing table (3 rows), Decision Framework (3 refs), Standard Pipelines (4 pipeline descriptions), Relationship section (1 ref). Replace "Design Java architecture" -> "Design architecture". Replace "(if Java)" -> removed. |
| `strategic-planner.md` | Relationship section: "java-architect-planner" -> "architect-planner", update description to remove "Java-specific" |
| `test-validator.md` | Relationship section: "java-developer" -> "developer", "java-debugger" -> "debugger" |
| `plan-auditor.md` | Successor Enforcement: "java-architect-planner" -> "architect-planner", "java-developer" -> "developer". Routing logic: "Java projects -> java-architect-planner, others -> java-developer" -> "architectural design needed -> architect-planner; otherwise -> developer" |
| `deep-analyst.md` | Relationship section: "java-debugger" -> "debugger", "specific Java bugs" -> "specific bugs" |
| `deep-research-synthesizer.md` | Relay Protocol: "java-architect-planner" -> "architect-planner" (2 occurrences) |
| `codebase-deep-analyzer.md` | Relationship section: "java-architect-planner" -> "architect-planner". Verify usage examples — "multi-module Maven project" is a valid example but should note it as one example among many. |

**Success criteria**:
- [ ] All 7 files updated
- [ ] Zero `java-developer`, `java-debugger`, or `java-architect-planner` references remain

### Task 5.2: Update shared protocol files (2 files)
**Bead**: nexus-f1mb
**Estimated**: 5 min

**Files and specific changes**:

| File | Changes |
|------|---------|
| `_shared/CONTEXT_PROTOCOL.md` | Proactive search agents list: "java-architect-planner" -> "architect-planner". Relay-reliant agents list: "java-developer" -> "developer", "java-debugger" -> "debugger". |
| `_shared/MAINTENANCE.md` | Example reference: "java-developer.md" -> "developer.md" |

**Success criteria**:
- [ ] Both files updated
- [ ] Agent lists reflect new names

### Task 5.3: Update skill cross-references (5 skill files)
**Bead**: nexus-c25y
**Estimated**: 10 min

**Files and specific changes**:

| File | Changes |
|------|---------|
| `skills/orchestration/SKILL.md` | Routing diagram: replace 3 node names + 6 edge references. Routing table: replace 3 rows. |
| `skills/using-nx-skills/SKILL.md` | Skill directory table: 3 rows ("java-architecture" -> "architecture", "java-development" -> "development", "java-debugging" -> "debugging"), skill names, command names, descriptions. Skill Priority example: "java-development, java-debugging" -> "development, debugging". |
| `skills/brainstorming-gate/SKILL.md` | Terminal state warning: "java-development, java-architecture" -> "development, architecture" |
| `skills/deep-analysis/SKILL.md` | "After java-debugger" -> "After debugger" |
| `skills/plan-validation/SKILL.md` | "Proceed to java-developer" -> "Proceed to developer" |
| `skills/test-validation/SKILL.md` | "trigger java-debugging" -> "trigger debugging" |

**Success criteria**:
- [ ] All 6 skill files updated (5 listed + test-validation)
- [ ] Routing diagram reflects new names
- [ ] Skill directory table uses new names and commands

### Task 5.4: Update command cross-references (1 file)
**Bead**: nexus-b54i
**Estimated**: 3 min

**File**: `commands/orchestrate.md`

**Changes**: "Development: java-developer, java-architect-planner, java-debugger" -> "Development: developer, architect-planner, debugger"

**Success criteria**:
- [ ] Agent listing uses new names

---

## Phase 6: Infrastructure Updates

### Task 6.1: Update registry.yaml
**Bead**: nexus-k3nx
**Estimated**: 15 min

This is the single source of truth. Every section needs updating:

1. **Agent entries** (3 renames):
   - `java-architect-planner:` -> `architect-planner:`
     - `skill: java-architecture` -> `skill: architecture`
     - `slash_command: /java-architecture` -> `slash_command: /architecture`
     - `description`: remove "Java"
     - `successors: [java-developer]` -> `successors: [developer]`
   - `java-debugger:` -> `debugger:`
     - `skill: java-debugging` -> `skill: debugging`
     - `slash_command: /java-debug` -> `slash_command: /debug`
     - `description`: remove "Java"
     - `successors: [java-developer]` -> `successors: [developer]`
   - `java-developer:` -> `developer:`
     - `skill: java-development` -> `skill: development`
     - `slash_command: /java-implement` -> `slash_command: /implement`
     - `description`: remove "Java"
     - `predecessors: [plan-auditor, java-architect-planner]` -> `predecessors: [plan-auditor, architect-planner]`
2. **Cross-references in other agents' entries**:
   - `code-review-expert.predecessors: [java-developer]` -> `[developer]`
   - `plan-auditor.successors: [java-architect-planner, java-developer]` -> `[architect-planner, developer]`
   - `test-validator.predecessors: [java-developer, code-review-expert]` -> `[developer, code-review-expert]`
3. **Pipelines** (3 affected):
   - `feature`: `java-architect-planner` -> `architect-planner`, `java-developer` -> `developer`
   - `bug`: `java-debugger` -> `debugger`, `java-developer` -> `developer`
   - `architecture`: `java-architect-planner` -> `architect-planner`
4. **naming_aliases** (2 entries to update):
   - `/java-implement: java-development` -> `/implement: development`
   - `/java-debug: java-debugging` -> `/debug: debugging`
   - No `/architecture` alias needed — the slash command maps directly to the `architecture/` skill directory by convention
5. **model_summary**:
   - opus: `java-architect-planner` -> `architect-planner`, `java-debugger` -> `debugger`
   - sonnet: `java-developer` -> `developer`

**Success criteria**:
- [ ] All 3 agent entries renamed with updated fields
- [ ] All predecessor/successor chains use new names
- [ ] All 3 affected pipelines updated
- [ ] naming_aliases section updated
- [ ] model_summary section updated

### Task 6.2: Update README.md
**Bead**: nexus-7dwk
**Estimated**: 10 min

**Changes**:
1. **Entry points table**: "Debug a Java failure" -> "Debug a failure", `/java-debug` -> `/debug`
2. **Directory structure**: 3 skill dirs renamed
3. **Agent table**: 3 rows updated (agent name, skill name, command name, descriptions)
4. **Standard pipelines**: Remove "(Java)" annotations, update agent names in all 3 affected pipelines. Remove the "Non-Java workflows" note since all workflows are now language-agnostic.
5. **Slash commands**: 3 entries updated (`/java-implement` -> `/implement`, etc.)

**Success criteria**:
- [ ] All 5 sections updated
- [ ] No "Java" qualifier on pipelines or entry points
- [ ] Directory structure matches actual filesystem

---

## Phase 7: Preflight Enhancement

### Task 7.1: Add CLAUDE.md validation to nx-preflight
**Bead**: nexus-3i0r
**Estimated**: 20 min

**Steps**:
1. Add section 6 to `nx/commands/nx-preflight.md` after the uv check:
   ```bash
   # -- 6. CLAUDE.md Agent Readiness --
   echo "### 6. CLAUDE.md Agent Readiness"
   echo ""
   if [ -f "CLAUDE.md" ]; then
     echo "[x] CLAUDE.md exists"
     # Language detection (case-insensitive)
     LANG_MATCH=$(grep -iE "Python|Java|Go|Rust|TypeScript|Node\.js|C\+\+|C#|Ruby|Kotlin|Swift|Scala" CLAUDE.md | head -1)
     if [ -n "$LANG_MATCH" ]; then
       echo "[x] Language detected: $(echo "$LANG_MATCH" | head -c 60)"
     else
       echo "[?] Language: not found (optional — agents can detect from build files)"
     fi
     # Build system detection
     BUILD_MATCH=$(grep -iE "uv|maven|mvn|cargo|go build|go mod|npm|yarn|pnpm|gradle|make|cmake" CLAUDE.md | head -1)
     if [ -n "$BUILD_MATCH" ]; then
       echo "[x] Build system detected: $(echo "$BUILD_MATCH" | head -c 60)"
     else
       echo "[?] Build system: not found (optional)"
     fi
     # Test command detection
     TEST_MATCH=$(grep -iE "pytest|mvn test|go test|cargo test|npm test|jest|vitest|make test|uv run pytest" CLAUDE.md | head -1)
     if [ -n "$TEST_MATCH" ]; then
       echo "[x] Test command detected: $(echo "$TEST_MATCH" | head -c 60)"
     else
       echo "[?] Test command: not found (optional)"
     fi
     echo ""
     echo "Status: PASS (CLAUDE.md present)"
   else
     echo "[ ] CLAUDE.md not found"
     echo ""
     echo "Status: WARN"
     echo "Agents work best when CLAUDE.md specifies language, build system, and test command."
     echo "See: https://docs.anthropic.com/en/docs/claude-code/memory#claudemd"
   fi
   ```
2. Update the summary table to include a `CLAUDE.md` row
3. Verify the nexus project's own CLAUDE.md passes

**Success criteria**:
- [ ] Preflight detects language, build system, test command from CLAUDE.md
- [ ] Missing sections show `[?]` warnings (not failures)
- [ ] Nexus project's CLAUDE.md passes all checks
- [ ] Summary table includes CLAUDE.md row

---

## Phase 8: Verification and PR

### Task 8.1: Verification and PR creation
**Bead**: nexus-m5tm
**Estimated**: 10 min

**Steps**:
1. Run verification grep (two patterns — agent names AND skill/command names):
   ```bash
   grep -rl "java-developer\|java-debugger\|java-architect" nx/
   grep -rl "java-develop\|java-debug\|java-architect" nx/
   ```
   Expected output: zero results for both (excluding CHANGELOG.md and any RDR docs under docs/rdr/)
2. Verify new files exist:
   ```bash
   ls nx/agents/developer.md nx/agents/debugger.md nx/agents/architect-planner.md
   ls nx/skills/development/SKILL.md nx/skills/debugging/SKILL.md nx/skills/architecture/SKILL.md
   ls nx/commands/implement.md nx/commands/debug.md nx/commands/architecture.md
   ```
3. Verify old files are gone:
   ```bash
   ls nx/agents/java-developer.md nx/agents/java-debugger.md nx/agents/java-architect-planner.md 2>&1
   ```
   Expected: all "No such file"
4. Stage all changes and commit
5. Push branch and create PR
6. PR must be squash-merged to ensure atomicity on main

**Success criteria**:
- [ ] Verification grep returns zero results in `nx/` (excluding changelogs/RDR docs)
- [ ] All 9 renamed files exist at new paths
- [ ] All 9 old paths are gone
- [ ] PR created and passes CI

---

## Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Missed cross-reference | Medium | Medium | Phase 8 verification grep catches this |
| Content rewrite quality | Medium | Low | RDR specifies exact sections to change; code-review-expert reviews PR |
| Git history loss on rename | Low | Low | Use `git mv` which preserves rename tracking |
| Intermediate broken state | High | Low | Squash-merge PR ensures atomicity on main |
| CLAUDE.md preflight false positives | Low | Medium | All checks are warnings (`[?]`), not errors |
| Skill/command resolution breaks | Medium | Low | Test with `claude --plugin-dir ./nx` after changes |

---

## Test Strategy

1. **Automated**: The verification grep in Phase 8 serves as the primary automated check
2. **Manual**: After all changes, test with `claude --plugin-dir ./nx`:
   - Run `/implement` — verify it routes to `developer` agent
   - Run `/debug` — verify it routes to `debugger` agent
   - Run `/architecture` — verify it routes to `architect-planner` agent
   - Run `/nx-preflight` — verify CLAUDE.md check appears
3. **Regression**: Verify existing commands that reference these agents still work:
   - `/orchestrate` — verify routing table uses new names
   - `/create-plan` — verify successor chain references new names

---

## Complete File Manifest (28 files)

### Renamed files (9)
| Old Path | New Path | Bead |
|----------|----------|------|
| `nx/agents/java-developer.md` | `nx/agents/developer.md` | nexus-migt |
| `nx/agents/java-debugger.md` | `nx/agents/debugger.md` | nexus-s7oe |
| `nx/agents/java-architect-planner.md` | `nx/agents/architect-planner.md` | nexus-d3me |
| `nx/skills/java-development/SKILL.md` | `nx/skills/development/SKILL.md` | nexus-booa |
| `nx/skills/java-debugging/SKILL.md` | `nx/skills/debugging/SKILL.md` | nexus-pruf |
| `nx/skills/java-architecture/SKILL.md` | `nx/skills/architecture/SKILL.md` | nexus-4g4o |
| `nx/commands/java-implement.md` | `nx/commands/implement.md` | nexus-jmzk |
| `nx/commands/java-debug.md` | `nx/commands/debug.md` | nexus-vr8o |
| `nx/commands/java-architecture.md` | `nx/commands/architecture.md` | nexus-t86e |

### Cross-reference updates (18 files)
| File | Bead |
|------|------|
| `nx/agents/orchestrator.md` | nexus-bp0k |
| `nx/agents/strategic-planner.md` | nexus-bp0k |
| `nx/agents/test-validator.md` | nexus-bp0k |
| `nx/agents/plan-auditor.md` | nexus-bp0k |
| `nx/agents/deep-analyst.md` | nexus-bp0k |
| `nx/agents/deep-research-synthesizer.md` | nexus-bp0k |
| `nx/agents/codebase-deep-analyzer.md` | nexus-bp0k |
| `nx/agents/_shared/CONTEXT_PROTOCOL.md` | nexus-f1mb |
| `nx/agents/_shared/MAINTENANCE.md` | nexus-f1mb |
| `nx/skills/orchestration/SKILL.md` | nexus-c25y |
| `nx/skills/using-nx-skills/SKILL.md` | nexus-c25y |
| `nx/skills/brainstorming-gate/SKILL.md` | nexus-c25y |
| `nx/skills/deep-analysis/SKILL.md` | nexus-c25y |
| `nx/skills/plan-validation/SKILL.md` | nexus-c25y |
| `nx/skills/test-validation/SKILL.md` | nexus-c25y |
| `nx/commands/orchestrate.md` | nexus-b54i |
| `nx/registry.yaml` | nexus-k3nx |
| `nx/README.md` | nexus-7dwk |

### New/modified sections (1 file)
| File | Bead |
|------|------|
| `nx/commands/nx-preflight.md` | nexus-3i0r |

### No changes needed (confirmed)
| File | Reason |
|------|--------|
| `nx/hooks/hooks.json` | No Java-specific references found |
