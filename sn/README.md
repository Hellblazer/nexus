# sn — Serena + Context7 MCP Plugin for Claude Code

A Claude Code plugin that bundles two MCP servers, injects their usage guidance into every subagent, and auto-approves their tools.

## What It Does

Subagents spawned by Claude Code don't see your CLAUDE.md instructions. They have access to MCP tools but don't know they should use them or how to call them correctly.

`sn` fixes this by:

1. **Bundling MCP servers**: Serena (code intelligence, JetBrains or LSP backend) and Context7 (live library documentation), both pinned to an exact revision in `.mcp.json`.
2. **Injecting usage guidance**: a `SubagentStart` hook injects the tool routing table and workflow into every subagent's context, and a `SessionStart` hook prints a short reminder into the main conversation.
3. **Auto-approving MCP tools**: `PreToolUse` and `PermissionRequest` hooks approve every `mcp__plugin_sn_serena__*` tool the pinned Serena exposes and both `mcp__plugin_sn_context7__*` tools, so agents never hit a permission prompt. The Serena list is a generated snapshot, not a hand-kept list.

## Install

```bash
/plugin install sn@nexus-plugins
```

### Prerequisites

- **Serena**: requires `uvx` ([uv](https://docs.astral.sh/uv/) must be installed)
- **Context7**: requires `npx` ([Node.js](https://nodejs.org/) must be installed)
- **Serena project config**: each project needs a `.serena/project.yml`. Auto-generate one with:
  ```bash
  uvx --from git+https://github.com/oraios/serena serena project create /path/to/project
  ```
  This auto-detects languages and registers the project in `~/.serena/serena_config.yml`.

## What Gets Injected

Every subagent receives a `system-reminder` block containing:

### Serena Guidance

- **Project activation**: automatic via `--project-from-cwd`
- **Setup**: ToolSearch lines that load both backend variants of each tool; only the available ones resolve
- **Routing table**: task to tool, per backend, and when to use Grep/Read/Glob instead
- **Rules**: `get_symbols_overview` before reading whole files, `find_referencing_symbols` before signature changes

Parameter signatures are delegated to Serena's own `initial_instructions` tool, which is backend-aware.

### Worktree Guidance and Guard

Serena resolves every path against the project root it found at startup (`--project-from-cwd` is read once). Claude Code subagents share the parent's MCP connection, so a subagent dispatched with `isolation: "worktree"` sends its edits to that server, and the server writes them into the primary checkout. Three incidents; the tool reported success each time.

The plugin handles this in two places, both keyed on the `cwd` field of the hook payload and one detector (`hooks/scripts/worktree_guard.py`: the cwd's nearest `.git` is a file whose `gitdir:` points under `.git/worktrees/`):

- **SubagentStart**: a worktree agent gets `worktree-section.md` ahead of the routing table: use the built-in `LSP` tool for navigation and `Edit`/`Write` with absolute paths for edits; Serena read tools stay available.
- **PreToolUse / PermissionRequest**: every Serena write tool (`replace_in_files`, `replace_symbol_body`, `insert_*`, `rename_symbol`, `jet_brains_rename`/`move`/`safe_delete`/`inline_symbol`, `*_lines`, `replace_content`, memory writes) is denied with a reason naming the worktree. Read tools are approved as before.

For the read side to work in worktrees, install the native LSP plugins for your languages (`claude plugin install pyright-lsp@claude-plugins-official`, likewise `jdtls-lsp`, `kotlin-lsp`, `typescript-lsp`) and their binaries.

### Context7 Guidance

- **Workflow**: `resolve-library-id` then `query-docs`
- **When to use**: API syntax, framework setup, version migration, library debugging
- **When not to use**: general programming, business logic, code review

## MCP Servers Included

| Server | Purpose |
|--------|---------|
| `serena` | Code intelligence (symbol navigation, refactoring, type hierarchy, inspections) |
| `context7` | Live documentation lookup for libraries and frameworks |

### Serena Configuration

Serena is started with `--context claude-code --project-from-cwd`:

- **`--context claude-code`**: excludes tools that overlap with Claude's built-in capabilities (`create_text_file`, `read_file`, `execute_shell_command`, `find_file`, `list_dir`, `search_for_pattern`) and enables `single_project: true`.
- **`--project-from-cwd`**: auto-detects the project from the current working directory by searching for `.serena/project.yml` or `.git`.

### Pins and the tool snapshot

`.mcp.json` pins Serena to a git revision and Context7 to a package version, so a fresh MCP spawn gets the server the plugin was tested with. `hooks/scripts/serena-tools.txt` is the list of Serena tools available under the `claude-code` context at that revision; `scripts/sync_sn_serena_tools.py` (in the nexus repo) regenerates it from the pin, and `tests/test_sn_plugin.py` fails if the snapshot, the injected section, or the pin disagree. To bump Serena: change the revision in `.mcp.json`, run the script, fix what the tests name.

## Plugin Structure

```
sn/
├── .claude-plugin/
│   └── plugin.json               # Plugin manifest
├── .mcp.json                      # Serena + Context7 MCP server configs (pinned)
├── hooks/
│   ├── hooks.json                 # SessionStart, SubagentStart, PreToolUse, PermissionRequest
│   └── scripts/
│       ├── session-start.sh       # main-conversation reminder
│       ├── mcp-inject.sh          # SubagentStart injection (JSON envelope)
│       ├── serena-section.md      # injected Serena guidance
│       ├── context7-section.md    # injected Context7 guidance
│       ├── auto-approve-sn-mcp.sh # PreToolUse / PermissionRequest wrapper
│       ├── auto_approve_sn_mcp.py # the allowlist decision (stdlib only)
│       ├── worktree_guard.py      # linked-worktree detection + Serena write-tool set
│       ├── worktree-section.md    # injected ahead of the routing table in worktrees
│       └── serena-tools.txt       # generated Serena tool snapshot
└── README.md
```

## Relationship to conexus

`sn` is project-agnostic. It works in any repository where Serena is configured. It lives in the same marketplace as `conexus` but has no dependency on it. Install either or both.
