---
allowed-tools: Bash
description: Create implementation plan using strategic-planner agent
---

# Planning Request

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

  # Existing beads context
  echo "### Existing Epics/Features"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --type=epic --limit=5 2>/dev/null || echo "No epics found"
    echo ""
    bd list --type=feature --status=open --limit=5 2>/dev/null || echo "No open features"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  # Architecture hints
  echo "### Project Structure"
  echo '```'
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
  [ "$_pt_found" -eq 0 ] && echo "- Unknown (no recognized build/marker file)"
  echo '```'

```

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Feature/Task to Plan

$ARGUMENTS

## Action

Invoke the **strategic-planning** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: strategic-planner

**Task**: Create comprehensive implementation plan for: $ARGUMENTS
**Bead**: [fill from active epic bead above or create new]

### Input Artifacts
- Files: [fill from relevant existing code for context]

### Requirements
$ARGUMENTS

### Deliverable
Phased execution plan with dependency graph, success criteria per phase, test strategy, and beads created for all trackable items.

### Quality Criteria
- [ ] Work broken into logical phases with clear boundaries
- [ ] Dependencies identified and ordered correctly
- [ ] Success criteria defined per phase (measurable)
- [ ] Test strategy included for each phase
- [ ] Beads created for all trackable items

**IMPORTANT**: After planning completes, call `mcp__plugin_conexus_nexus__nx_plan_audit` for validation before implementation (RDR-080 — direct MCP call).
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
