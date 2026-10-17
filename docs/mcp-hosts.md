# Nexus MCP Server on Other Hosts

`nx-mcp` is a plain stdio MCP server, so any host that can launch a local command can use it. This page gives the setup for Cursor, OpenAI Codex CLI, Gemini CLI, and VS Code with GitHub Copilot agent mode, then the generic case. Claude Code and Claude Desktop are covered in [Desktop Deployment](desktop-deployment.md).

**Nothing on this page was run here.** Each host's config format below comes from that host's own documentation, fetched on 2026-10-17 and cited under its heading. We have not started these hosts against Nexus. If a snippet fails, the host's documentation is the authority on its own format; the command and arguments are the part that is ours.

## Before you configure any host

The host starts `nx-mcp` from your install, so install Nexus first. The commands are the "Install by hand" steps in [Desktop Deployment](desktop-deployment.md#install-by-hand); [Installing nexus on Windows](windows-install.md) has the Windows prerequisites.

```bash
uv tool install conexus --python 3.12     # on Linux add: --torch-backend cpu
nx init
```

`nx init` provisions the storage service. Without it the servers start and every tool that needs storage reports that it cannot reach the service.

Check that the host will find the commands:

```bash
command -v nx-mcp nx-mcp-catalog          # Windows: Get-Command nx-mcp
```

A host launched from a desktop icon often does not inherit your shell's `PATH`. If it reports the server as failed or connection closed, replace `nx-mcp` in the snippets with the absolute path that command printed (for example `/Users/you/.local/bin/nx-mcp`; on Windows the full path to `nx-mcp.exe`, with backslashes doubled inside JSON).

## The two servers

Every snippet below registers the same two stdio entries the Claude plugin does, with no arguments and no required environment:

| Entry name | Command | Holds |
|---|---|---|
| `nexus` | `nx-mcp` | Storage tiers, retrieval, plans, tuple space, diagnostics |
| `nexus-catalog` | `nx-mcp-catalog` | Document catalog, link graph, tumbler resolution |

The catalog server is optional; start with `nexus` alone if you want one entry. [MCP Servers](mcp-servers.md) lists every tool on each. The Claude plugin starts both through a launcher script (`conexus/launch/nx_launch.py`) that adds a stand-in when the CLI is missing; that wrapper is Claude-specific and the other hosts run the installed command directly.

The entry names use a hyphen and no underscore because Gemini CLI documents that underscores in server names break its policy matching.

## Cursor

Config lives in `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project).

```json
{
  "mcpServers": {
    "nexus": { "command": "nx-mcp", "args": [] },
    "nexus-catalog": { "command": "nx-mcp-catalog", "args": [] }
  }
}
```

Verify: manage servers from Customize in the sidebar and check that `nexus` is enabled. From a terminal, `agent mcp list` shows configured servers and their status, and `agent mcp list-tools nexus` lists the tools. If a server fails, open the Output panel and pick "MCP Logs".

Source: Cursor docs, [MCP](https://cursor.com/docs/mcp) and [CLI MCP](https://cursor.com/docs/cli/mcp). Config per Cursor docs, not run here.

## OpenAI Codex CLI

Config lives in `~/.codex/config.toml`, or `.codex/config.toml` in a trusted project. The ChatGPT desktop app, Codex CLI and IDE extension share it.

```toml
[mcp_servers.nexus]
command = "nx-mcp"

[mcp_servers.nexus-catalog]
command = "nx-mcp-catalog"
```

Or add them from the command line:

```bash
codex mcp add nexus -- nx-mcp
codex mcp add nexus-catalog -- nx-mcp-catalog
```

Verify: `codex mcp list` shows configured servers; type `/mcp` in the Codex TUI to see the active ones. Codex limits how long it waits for a server to start; if `nexus` is marked failed at first launch, raise `startup_timeout_sec` on its table.

Source: OpenAI, [Connect Codex to an MCP server](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and the Codex repository's config types (`codex-rs/config/src/mcp_types.rs`) for `startup_timeout_sec`. Config per Codex docs, not run here.

## Gemini CLI

Config lives in `~/.gemini/settings.json` (user) or `.gemini/settings.json` (project, which overrides the user file).

```json
{
  "mcpServers": {
    "nexus": { "command": "nx-mcp" },
    "nexus-catalog": { "command": "nx-mcp-catalog" }
  }
}
```

Or add them from the command line (`--scope user` writes the user file; the default scope is project):

```bash
gemini mcp add --scope user nexus nx-mcp
gemini mcp add --scope user nexus-catalog nx-mcp-catalog
```

Verify: `gemini mcp list` prints each server with its connection status; `/mcp` inside a session shows the servers and the tools each one provides.

Two Gemini behaviours matter here. A stdio server shows as connected only in a trusted folder; run `gemini trust` in the folder if it shows disconnected. And Gemini removes host environment variables whose names match `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*AUTH*` or `*CREDENTIAL*` before starting a server, unless you name them in the server's `env` block. If you give Nexus a credential through such a variable (for example `NX_VOYAGE_API_KEY`), add `"env": { "NX_VOYAGE_API_KEY": "$NX_VOYAGE_API_KEY" }` to the entry.

Source: Gemini CLI docs, [MCP servers](https://geminicli.com/docs/tools/mcp-server) and [configuration](https://geminicli.com/docs/reference/configuration). Config per Gemini CLI docs, not run here.

## VS Code and GitHub Copilot agent mode

Config lives in `.vscode/mcp.json` in a workspace, or in your user profile (run the command **MCP: Open User Configuration**). The top-level key is `servers`, not `mcpServers`.

```json
{
  "servers": {
    "nexus": { "type": "stdio", "command": "nx-mcp" },
    "nexus-catalog": { "type": "stdio", "command": "nx-mcp-catalog" }
  }
}
```

A workspace can instead hold a `.mcp.json` at the project root in the portable `mcpServers` shape used by the Cursor snippet above.

Verify: run **MCP: List Servers** from the Command Palette. It lists each server and offers start, stop, restart and **Show Output**. VS Code asks you to trust a server the first time it starts. Then open the Chat view with the Agent picker selected and choose **Configure Tools** to see the tools and toggle them.

Source: VS Code docs, [MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) and the MCP servers guide in the `microsoft/vscode-docs` repository. Config per VS Code docs, not run here.

## Any other MCP host

The server speaks MCP over stdio. Whatever the host calls its config, it needs a server name, the command `nx-mcp`, no arguments, and no environment. The host-neutral JSON shape most clients accept is:

```json
{
  "mcpServers": {
    "nexus": { "command": "nx-mcp", "args": [] }
  }
}
```

The MCP specification describes the [stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#stdio) if your host asks for a transport type. The tool list comes from the host. `nx doctor` checks the storage service the server talks to.

## What does not carry over

You get the MCP tools and the storage behind them. These Claude Code features are not part of the server, so they do not exist on the other hosts:

- **Hooks.** The session-start preflight and `nx upgrade --auto`, the subagent-start guidance injection, and the session-end cleanup are Claude Code hooks. After you update the CLI, run `nx upgrade` yourself.
- **Skills, agents and slash commands.** The `/conexus:*` commands, the conexus skills and the `developer` and reviewer agents ship in the Claude plugin. Nothing on these hosts calls them.
- **The setup stand-in.** When the CLI is missing, the Claude plugin starts a stand-in server with two `setup` tools. A host running `nx-mcp` directly just fails to start if `nx-mcp` is not installed. The `setup` tool on a real `nx-mcp` installs nothing without the plugin's setup script and answers with the install commands.
- **Session scratch (T1).** T1 needs a session id that Claude Code supplies. On other hosts the `scratch` tools report that T1 is unavailable for the process, and every other tool is unaffected. Setting `NX_SESSION_ID` in the server's environment gives it one, and that value names the scratch scope.
- **`nx_answer` and the plan steps that summarize, rank or generate.** These run through `claude -p`, so they need the Claude CLI installed and signed in on the same machine. Search, query, store, memory, catalog and tuple tools do not.
- **The other plugin servers.** The `sequential-thinking` entry and the `sn` plugin's Serena and Context7 servers are separate and are not part of Nexus.

State is shared. A host reading the same service sees the same T2 memory, T3 knowledge and catalog as Claude Code and the `nx` CLI on that machine ([Storage Tiers](storage-tiers.md)).
