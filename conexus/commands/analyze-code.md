---
allowed-tools: Bash
description: Analyze codebase using codebase-deep-analyzer agent
---

# Codebase Analysis Request

```!
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Project type detection
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
  echo ""

  # Module structure
  echo "### Top-level Structure"
  echo '```'
  ls -d */ 2>/dev/null | head -15 || echo "No subdirectories"
  echo '```'
  echo ""

  # Source locations
  echo "### Source Locations"
  echo '```'
  find . -type d -name "src" 2>/dev/null | grep -v node_modules | grep -v target | head -10 || echo "No src directories found"
  echo '```'

```

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Analysis Scope

$ARGUMENTS

## Action

Invoke the **codebase-analysis** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: codebase-deep-analyzer

**Task**: Analyze codebase architecture, patterns, and dependencies
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from key entry points in project structure above]

### Analysis Scope
$ARGUMENTS

### Deliverable
Comprehensive architecture analysis: module structure map, identified design patterns, dependency graph, entry points, coding conventions, and technical debt assessment.

### Quality Criteria
- [ ] Module structure mapped with responsibilities
- [ ] Key design patterns identified and documented
- [ ] Dependencies documented (internal and external)
- [ ] Entry points identified for each module
- [ ] Coding conventions and idioms noted
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
