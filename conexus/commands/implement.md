---
allowed-tools: Bash
description: Implement feature using developer agent
---

# Implementation Request

```!
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Git context
  if git rev-parse --git-dir > /dev/null 2>&1; then
    echo "**Branch:** $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
    echo ""
  fi

  echo "**Note:** Ensure plan has been validated by mcp__plugin_conexus_nexus__nx_plan_audit (RDR-080) before implementing."
  echo ""

  # Bead context
  echo "### Active Work"
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo ""

  # Project type
  echo "### Project Info"
  echo "**Project type:**"
  _pt_found=0
  _pt() { if compgen -G "$1" >/dev/null 2>&1; then echo "- $2"; _pt_found=1; fi; }
  _pt "pyproject.toml" "Python"
  _pt "setup.py" "Python (setup.py)"
  _pt "Cargo.toml" "Rust"
  _pt "go.mod" "Go"
  _pt "package.json" "Node.js / TypeScript"
  _pt "pom.xml" "Java/Kotlin (Maven)"
  _pt "build.gradle*" "Java/Kotlin (Gradle)"
  _pt "Gemfile" "Ruby"
  _pt "composer.json" "PHP"
  _pt "*.csproj" "C#/.NET"
  _pt "CMakeLists.txt" "C/C++ (CMake)"
  _pt "Package.swift" "Swift"
  _pt "mix.exs" "Elixir"
  _pt "build.sbt" "Scala (sbt)"
  _pt "pubspec.yaml" "Dart/Flutter"
  _pt "deps.edn" "Clojure"
  _pt "project.clj" "Clojure (Leiningen)"
  _pt "*.cabal" "Haskell"
  _pt "stack.yaml" "Haskell (Stack)"
  _pt "Project.toml" "Julia"
  _pt "DESCRIPTION" "R"
  _pt "build.zig" "Zig"
  _pt "dune-project" "OCaml"
  _pt "shard.yml" "Crystal"
  if [ "$_pt_found" -eq 0 ]; then echo "- Unknown (no recognized build/marker file)"; fi

```

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Task to Implement

$ARGUMENTS

## Action

**PREREQUISITE**: Plan must be validated by mcp__plugin_conexus_nexus__nx_plan_audit (RDR-080) before implementation.

Invoke the **development** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: developer

**Task**: Implement "$ARGUMENTS" using TDD methodology
**Bead**: [fill from active in_progress bead above]

### Input Artifacts
- Files: [fill from existing files to modify or target package]

### Plan Context
[fill from approved mcp__plugin_conexus_nexus__nx_plan_audit (RDR-080) output]

### Requirements
$ARGUMENTS

### Deliverable
Working implementation with passing tests, following TDD red-green-refactor cycle.

### Quality Criteria
- [ ] Tests written before implementation (TDD)
- [ ] All tests pass (run the project's test command from CLAUDE.md)
- [ ] Code follows project conventions (check CLAUDE.md)
- [ ] No regressions introduced in existing tests

**IMPORTANT**: After implementation completes, MUST delegate to code-review-expert for quality review.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
