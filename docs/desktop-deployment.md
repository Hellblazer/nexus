# Desktop deployment

Nexus runs in three Claude surfaces, all backed by one host daemon so state is shared across them and with the `nx` CLI. This document covers install, first-run behavior, drift detection, and uninstall for each surface. The substrate that makes this work landed in RDR-120 (4.34.0+); the unified-surface design is RDR-126.

## Surface 1: Claude Code (terminal)

**Audience**: developers who already use Claude Code from the command line.

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

On first session start, the plugin's SessionStart hook auto-installs the host T2 daemon (LaunchAgent on macOS, systemd user unit on Linux) and ensures it is running. Subsequent sessions are a no-op.

Tool names: `mcp__plugin_conexus_nexus__*` and `mcp__plugin_conexus_nexus-catalog__*`. Slash-command prefix: `/conexus:*`.

## Surface 2: Claude Cowork (cloud agents)

**Audience**: Claude Code users who open Cowork sessions for cloud-driven tasks.

No separate install. Once the conexus plugin is installed in Claude Code on the host, Claude Desktop passes the configured MCP servers into the Cowork VM via the Anthropic SDK transport (`--mcp-config` with `"type": "sdk"`). The MCP server stays running on the host; the VM agent's tool calls are bridged back through the SDK channel.

State is shared with the host CLI Claude and any other Cowork sessions or dev containers running against the same host daemons. Verified by a bidirectional T2 sentinel test (see `tests/test_cowork_sdk_bridge.py`).

## Surface 3: Claude Desktop chat (Desktop Extension)

**Audience**: Claude Desktop users who do NOT have Claude Code installed.

Pre-requisite: [uv](https://docs.astral.sh/uv/) on host PATH.

- macOS: `brew install uv`
- Linux: `pipx install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`

Install:

1. Download `conexus.mcpb` from the [latest GitHub release](https://github.com/Hellblazer/nexus/releases/latest).
2. Double-click the file. Claude Desktop registers it under Settings → Connectors → Desktop as "Conexus".
3. First launch: uv resolves Nexus's dependency stack (~237 packages, including chromadb, pydantic-core, tree-sitter, numpy, torch, onnxruntime). Cold install ~20s on a warm network; warm restarts ~5s.
4. Also at first launch: `nx-mcp` auto-installs the T2 daemon (idempotent if already present from Claude Code or CLI use) and starts it.

Tool names: `mcp__conexus__*` (no `plugin_` infix — this is the .mcpb namespace, distinct from the Claude Code plugin's).

Note: Claude Code users who ALREADY have the conexus plugin should NOT also install the .mcpb. Their Claude Desktop chat already exposes Nexus tools through the plugin's local-agent-mode path. Installing the .mcpb adds a second copy under a different namespace; the model can target either but the duplication is confusing.

## CLI coexistence

`nx <verb>` commands run on the host shell. They talk to the same T2 daemon all three surfaces use. State is fully shared:

- `nx memory put -p X -t Y` → visible from Claude Code, Cowork, and Claude Desktop chat
- `nx search foo` → searches the same T3 collections the MCP `search` tool sees
- `nx index repo .` → indexes show up immediately in all surface tool results

Daemon lifecycle: `nx daemon t2 status` / `nx daemon t2 start` / `nx daemon t2 install --autostart` are the canonical operations. Any of the three Claude surfaces auto-installs on first launch, but the CLI commands are always available for explicit lifecycle control.

## Drift detection

After upgrading conexus (`uv tool upgrade conexus`) or after the plugin rename (`nx` → `conexus` at v5.0.0), `nx doctor` surfaces two kinds of drift:

- **Plugin name drift**: the installed Claude Code plugin still has `name: "nx"` but the CLI expects `conexus`. Fix:

  ```
  /reload-plugins      # in Claude Code
  ```

  Claude Code re-reads the marketplace and swaps the installed plugin in place. No explicit uninstall + install needed.

- **Post-commit hook stanza drift**: the installed `.git/hooks/post-commit` predates the pgrep guard fix (nexus-mkj6u 2026-05-23). Fix:

  ```
  nx hooks update <repo>
  ```

Both warnings include the resolution commands; `nx doctor` is the single explicit-invocation surface that consolidates them.

## Uninstall

### Claude Code plugin

```
/plugin uninstall conexus@nexus-plugins
```

The plugin cache is removed. The host T2 daemon and stored data are unaffected — they belong to the OS / user account, not the plugin.

### Claude Desktop Extension

Settings → Connectors → Desktop → Conexus → Uninstall. **Caveat**: Claude Desktop removes the `.mcpb` bundle but does NOT cascade to the LaunchAgent / systemd unit. To fully remove:

```
nx daemon t2 uninstall --autostart
# Optionally: rm -rf ~/.config/nexus
```

The MCPB spec has no manifest-level uninstall hook, so this cascade limitation is structural.

### Cowork

Nothing to uninstall on the Cowork side — sessions inherit whatever Claude Desktop has configured.

### Daemon + data (full nuke)

```
nx daemon t2 uninstall --autostart    # remove autostart unit + stop daemon
rm -rf ~/.config/nexus                 # remove T2 SQLite + config
# T3: chromadb cloud accounts persist outside the daemon; manage there.
```

## Verification

`tests/e2e/upgrade-shakeout.sh` exercises the full surface story in a sandbox: install OLD conexus → install hooks → upgrade to current → verify drift detection → run `nx hooks update` → verify drift resolved → verify marketplace.json rename → verify plugin-name-drift detection. 11/11 green is the gate before any release.

## Failure modes

- **uv not on PATH (Claude Desktop chat install)**: `.mcpb` install fails with a cryptic error. Mitigation: README documents `brew install uv` / `pipx install uv` as pre-requisite.
- **GUI-spawned subprocess can't see shell-env credentials**: `is_local_mode()` flips to True; T3 dispatch fails. Mitigation: `nx doctor` surfaces the credential-persistence warning; persist via `nx config set chroma_api_key "$CHROMA_API_KEY"` etc.
- **Cowork SDK bridge dropped a tool call**: rare; the sentinel test in `tests/test_cowork_sdk_bridge.py` catches structural regressions. Diagnostic recipe: `nx daemon t2 status` then `nx memory list -p _cowork_test`.
