---
name: serena-code-nav
description: Use when navigating code by symbol — finding definitions, all callers, type hierarchies, renaming safely, or editing a method body without reading the whole file. Use instead of Grep for any symbol-level task.
effort: medium
---

# Serena Code Navigation

Serena provides LSP-backed code intelligence: it understands symbols, not text.
Use it for symbol-level tasks. Use Grep for text-pattern tasks.

## MANDATORY Setup

**Tool names vary by backend (JetBrains vs LSP).** Load with both variants — only available ones resolve:

```
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_find_symbol,mcp__plugin_sn_serena__find_symbol")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_find_referencing_symbols,mcp__plugin_sn_serena__find_referencing_symbols")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_get_symbols_overview,mcp__plugin_sn_serena__get_symbols_overview")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_type_hierarchy,mcp__plugin_sn_serena__type_hierarchy")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_rename,mcp__plugin_sn_serena__rename_symbol")
ToolSearch("select:mcp__plugin_sn_serena__replace_symbol_body,mcp__plugin_sn_serena__insert_before_symbol,mcp__plugin_sn_serena__insert_after_symbol")
ToolSearch("select:mcp__plugin_sn_serena__search_for_pattern,mcp__plugin_sn_serena__find_file,mcp__plugin_sn_serena__list_dir")
```

Then call `mcp__plugin_sn_serena__initial_instructions` for full backend-specific parameter docs.

Project is auto-activated via `--project-from-cwd`. No `activate_project` call needed.

## Decision Guide

Tool names below show `LSP name` / `JetBrains name`. Use whichever resolved in Setup.

| Task | Use | Not |
|------|-----|-----|
| Find where a symbol is defined | `find_symbol` / `jet_brains_find_symbol` | Grep (finds comments/strings too) |
| Find all callers of a function | `find_referencing_symbols` / `jet_brains_find_referencing_symbols` | Grep (misses aliased calls) |
| Inventory all methods in a file | `get_symbols_overview` / `jet_brains_get_symbols_overview` | Read (reads entire file body) |
| Understand class inheritance | `type_hierarchy` / `jet_brains_type_hierarchy` | Manual import tracing |
| Replace an entire function body | `replace_symbol_body` | Read + Edit (line arithmetic) |
| Insert code before/after a method | `insert_before/after_symbol` | Read + Edit (fragile on large files) |
| Rename a symbol across codebase | `rename_symbol` / `jet_brains_rename` | Grep + Edit (corrupts comments/strings) |
| Pattern search across files | `search_for_pattern` | Grep (prefer Grep — it's faster) |
| Find files by name | Glob | `find_file` |
| Semantic/conceptual search | `mcp__plugin_nx_nexus__search` | Serena (no semantic search) |
| Exact text, comments, config | Grep | Serena |

## Workflow Patterns

Examples use short names — substitute whichever variant resolved in Setup.

### A — Find a symbol definition

```
find_symbol("ClassName")                      # locate the class
get_symbols_overview("path/to/file.py")       # see all methods
# then Read only the specific method if needed
```

### B — Impact analysis before refactoring

```
find_symbol("method_name")                    # confirm the right symbol
find_referencing_symbols("method_name")       # all callers — LSP-accurate
# review each call site before changing the signature
```

### C — Surgical method replacement

```
get_symbols_overview("path/to/file.py")       # confirm symbol exists
replace_symbol_body("method_name", new_body)  # replace precisely
```

### D — Codebase structure pass (context-efficient)

```
get_symbols_overview("path/to/file.py")       # symbols without file body
type_hierarchy("ClassName")                   # inheritance tree
# read only the methods you actually need after this
```

### E — Safe cross-codebase rename

```
find_symbol("old_name")                       # confirm target
find_referencing_symbols("old_name")          # preview scope
rename_symbol("old_name", "new_name")         # LSP-safe: skips comments/strings
```

## Gotchas

- **Project auto-activated.** Via `--project-from-cwd` when `.serena/project.yml` exists.
- **All tools are deferred.** They do not exist until loaded via `ToolSearch`. Do Setup above before any call.
- **Stale index after bulk external edits.** If many files were edited via Edit/Write (not Serena), symbol results may be stale. Re-indexing is automatic on next tool use.
- **Large reference sets.** `find_referencing_symbols` on widely-used names (e.g., `put`, `search`) may return hundreds of results. Scope by file or type when possible.
