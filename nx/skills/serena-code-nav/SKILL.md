---
name: serena-code-nav
description: Use when navigating code by symbol — finding definitions, all callers, type hierarchies, renaming safely, or editing a method body without reading the whole file. Use instead of Grep for any symbol-level task.
effort: medium
---

# Serena Code Navigation

Serena provides LSP-backed code intelligence: it understands symbols, not text.
Use it for symbol-level tasks. Use Grep for text-pattern tasks.

## MANDATORY Setup

**Do this before any Serena tool call — every time:**

```python
# Step 1: Load tools (deferred — not available until this runs)
ToolSearch("select:mcp__plugin_serena_serena__activate_project")
ToolSearch("select:mcp__plugin_serena_serena__jet_brains_find_symbol")
ToolSearch("select:mcp__plugin_serena_serena__jet_brains_find_referencing_symbols")
ToolSearch("select:mcp__plugin_serena_serena__jet_brains_get_symbols_overview")
ToolSearch("select:mcp__plugin_serena_serena__jet_brains_type_hierarchy")
ToolSearch("select:mcp__plugin_serena_serena__replace_symbol_body")
ToolSearch("select:mcp__plugin_serena_serena__insert_before_symbol")
ToolSearch("select:mcp__plugin_serena_serena__insert_after_symbol")
ToolSearch("select:mcp__plugin_serena_serena__rename_symbol")
ToolSearch("select:mcp__plugin_serena_serena__search_for_pattern")
ToolSearch("select:mcp__plugin_serena_serena__get_current_config")

# Step 2: Activate project (required — Serena errors without this)
mcp__plugin_serena_serena__activate_project(project_name="nexus")
```

If unsure whether Serena is already activated, call `get_current_config` to check.

## Decision Guide

| Task | Use | Not |
|------|-----|-----|
| Find where a symbol is defined | `jet_brains_find_symbol` | Grep (finds comments/strings too) |
| Find all callers of a function | `jet_brains_find_referencing_symbols` | Grep (misses aliased calls) |
| Inventory all methods in a file | `jet_brains_get_symbols_overview` | Read (reads entire file body) |
| Understand class inheritance | `jet_brains_type_hierarchy` | Manual import tracing |
| Replace an entire function body | `replace_symbol_body` | Read + Edit (line arithmetic) |
| Insert code before/after a method | `insert_before/after_symbol` | Read + Edit (fragile on large files) |
| Rename a symbol across codebase | `rename_symbol` | Grep + Edit (corrupts comments/strings) |
| Pattern search across files | `search_for_pattern` | Grep (prefer Grep — it's faster) |
| Find files by name | Glob | `find_file` |
| Read a specific known file | Read | `read_file` |
| Semantic/conceptual search | mcp__plugin_nx_nexus__search(query="...", hybrid=true | Serena (no semantic search) |
| Exact text, comments, config | Grep | Serena |

## Workflow Patterns

### A — Find a symbol definition

```
jet_brains_find_symbol("ClassName")          # locate the class
jet_brains_get_symbols_overview("path/to/file.py")  # see all methods
# then Read only the specific method if needed
```

### B — Impact analysis before refactoring

```
jet_brains_find_symbol("method_name")                 # confirm the right symbol
jet_brains_find_referencing_symbols("method_name")    # all callers — LSP-accurate
# review each call site before changing the signature
```

### C — Surgical method replacement

```
jet_brains_get_symbols_overview("path/to/file.py")   # confirm symbol exists
replace_symbol_body("method_name", new_body)          # replace precisely
# no line arithmetic; no need to read the whole file first
```

### D — Codebase structure pass (context-efficient)

```
jet_brains_get_symbols_overview("path/to/file.py")   # symbols without file body
jet_brains_type_hierarchy("ClassName")                # inheritance tree
# read only the methods you actually need after this
```

### E — Safe cross-codebase rename

```
jet_brains_find_symbol("old_name")                    # confirm target
jet_brains_find_referencing_symbols("old_name")       # preview scope
rename_symbol("old_name", "new_name", file="...")     # LSP-safe: skips comments/strings
```

## Gotchas

- **Project must be activated first.** `jet_brains_*` tools error without `activate_project`.
- **All tools are deferred.** They do not exist until loaded via `ToolSearch`. Do Step 1 above before any call.
- **Python only for this project.** Serena is configured for Python; avoid on YAML/TOML/Markdown.
- **Stale index after bulk external edits.** If many files were edited via Edit/Write (not Serena), call `mcp__plugin_serena_serena__restart_language_server` before navigating.
- **Large reference sets.** `jet_brains_find_referencing_symbols` on widely-used names (e.g., `put`, `search`) may return hundreds of results. Scope by file or type when possible.
- **Backend determines tool availability.** If `jet_brains_*` tools are unavailable, check `get_current_config` — the non-prefixed variants (`find_symbol`, `get_symbols_overview`) may be available instead.
