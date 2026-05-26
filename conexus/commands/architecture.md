---
allowed-tools: Bash
description: Design architecture and create phased execution plans using architect-planner agent
---

# Architecture Request

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

  # Project structure
  echo "### Project Structure"
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
  echo ""

  # Active beads context
  echo "### Active Beads"
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
    echo ""
    bd list --type=epic --limit=3 2>/dev/null || echo "No epics"
  else
    echo "Beads not available"
  fi
  echo ""

  echo "### Pipeline Position"
  echo ""
  echo "strategic-planner -> nx_plan_audit -> architect-planner -> developer"
  echo ""
  echo "### Tip"
  echo ""
  echo "The agent uses the search tool with corpus='code' and hybrid=true (30-50 results) for discovery,"
  echo "then LSP for precision navigation (documentSymbol, goToImplementation, findReferences)."
```

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Feature/Component to Architect

$ARGUMENTS

## Action

Invoke the **architecture** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: architect-planner

**Task**: Design architecture for: $ARGUMENTS
**Bead**: [fill from active epic/feature bead above or create new]

### Input Artifacts
- Files: [fill from key existing source files for context]

### Requirements
$ARGUMENTS

### Deliverable
Comprehensive architecture design with component boundaries, interface contracts, dependency graph, phased execution plan with beads, and risk assessment with mitigations.

### Quality Criteria
- [ ] All requirements addressed in design
- [ ] Component boundaries clearly defined with interface contracts
- [ ] Integration points with existing code identified
- [ ] Phased execution plan created with beads and dependencies
- [ ] Risks identified with concrete mitigations
- [ ] Design follows project conventions (check CLAUDE.md)
- [ ] Ready for nx_plan_audit validation

**IMPORTANT**: After architecture is designed, MUST delegate to nx_plan_audit for validation before implementation begins.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
