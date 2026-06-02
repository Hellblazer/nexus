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

- **Plugin name drift**: the installed Claude Code plugin still has `name: "nx"` but the CLI expects `conexus`. Fix is two commands:

  ```
  /plugin install conexus@nexus-plugins   # in Claude Code — registers the new plugin
  /reload-plugins                          # in Claude Code — activates it
  ```

  Install alone leaves the new plugin staged but inactive; reload alone won't pick up the renamed plugin from marketplace.json. Both are required. Optionally `/plugin uninstall nx@nexus-plugins` after to drop the stale entry.

- **Post-commit hook stanza drift**: the installed `.git/hooks/post-commit` predates the pgrep guard fix (nexus-mkj6u 2026-05-23). Fix:

  ```
  nx hooks update <repo>
  ```

Both warnings include the resolution commands; `nx doctor` is the single explicit-invocation surface that consolidates them.

For Claude Desktop `.mcpb` users specifically, the bundle also performs a best-effort stale-install check at MCP server startup (MCPB v0.4 has no auto-update). When the installed `conexus` is older than the latest on PyPI, it emits a one-line warning to stderr naming the GitHub release URL to re-download:

```
[conexus-mcpb] installed conexus=X.Y.Z, latest on PyPI=A.B.C. Re-download
the .mcpb from https://github.com/Hellblazer/nexus/releases/latest and
re-install in Claude Desktop to upgrade. (Set NX_MCPB_SKIP_UPDATE_CHECK=1
to silence.)
```

The check is non-fatal: a network failure, timeout, or unreachable PyPI never blocks startup. Set `NX_MCPB_SKIP_UPDATE_CHECK=1` in the environment to opt out entirely.

## Updating the Desktop Extension

The `.mcpb` does **not** auto-update. MCPB v0.4 has no update mechanism, and the extension's Python environment is pinned at install time: Claude Desktop builds a `uv` venv under `~/Library/Application Support/Claude/Claude Extensions/local.mcpb.<id>.conexus/.venv` from the bundle's `pyproject.toml` (`conexus>=X.Y.Z`), and every launch runs `uv run` against **that existing venv**. `uv run` reuses the resolved venv rather than re-resolving against PyPI, so a newer published conexus is not picked up until the bundle is re-installed and the venv is rebuilt.

So the update is a manual, idempotent re-install:

1. **Download** the new `conexus.mcpb` from the [latest release](https://github.com/Hellblazer/nexus/releases/latest) (the same asset attached to every GitHub release alongside the wheel and sdist).
2. **Double-click it** (or Claude Desktop → Settings → Connectors → Desktop → install). Claude Desktop replaces the existing "Conexus" extension in place; you do not need to uninstall first.
3. **First launch resolves the new version.** `uv` rebuilds the venv from the new manifest's `conexus>=X.Y.Z` pin (~20s cold, ~5s warm), pulling the new conexus from PyPI. The stale-install warning stops firing once installed == latest.
4. **Verify** (optional): the installed version is the `version` field in the extension's `manifest.json`, and the resolved package is `<ext>/.venv/bin/python -c "from importlib.metadata import version; print(version('conexus'))"`.

What the update does **not** touch: the host T2 daemon, the T3 store, the catalog, and all stored data are owned by the OS / user account and shared across the CLI, the Claude Code plugin, and the Desktop extension. Re-installing the bundle swaps only the bundle files and its venv. The daemon is not restarted by an extension update.

**Version skew is expected and tolerated.** The Desktop connector (its venv) and the host T2 daemon are independent installs that can briefly differ, since they update on different triggers (a `.mcpb` re-install vs `uv tool upgrade conexus` / a CLI reinstall). The connector talks to the daemon over RPC within a major version; align them by updating whichever is behind. After a release, update both: the CLI/daemon via `uv tool upgrade conexus` (then restart the daemon), and the Desktop extension via the re-install above.

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

### Cowork bidirectional sentinel (manual)

The host-side substrate round-trip is regression-tested by `tests/test_cowork_sdk_bridge.py` (a `memory_put` is visible to a later `memory_get` against the same T2, both directions). The cross-process SDK bridge itself can only be confirmed by hand, because it needs a running Claude Desktop and a Cowork session. Run this recipe after any change to the daemon substrate, the SDK transport wiring, or the MCP server entry points:

1. **Host writes, VM reads.** In the host CLI (or host Claude Code):
   ```bash
   nx memory put -p _cowork_test -t host-to-vm "sentinel from host $(date +%s)"
   ```
   Open a Cowork session on the same host and ask it to call `memory_get` for `project="_cowork_test", title="host-to-vm"`. It must return the sentinel payload.

2. **VM writes, host reads.** In the Cowork session, ask it to call `memory_put` with `project="_cowork_test", title="vm-to-host", content="sentinel from vm"`. Back on the host:
   ```bash
   nx memory get -p _cowork_test -t vm-to-host    # must show "sentinel from vm"
   ```

3. **Cleanup.**
   ```bash
   nx memory delete -p _cowork_test -t host-to-vm
   nx memory delete -p _cowork_test -t vm-to-host
   ```

Both directions resolving the sentinel confirms the bridge shares one T2 with the host. A failure on step 1 points at the SDK transport (the VM never reached the host daemon); a failure on step 2 points at write-attribution or a stale read in the shared substrate — start with `nx daemon t2 status` then `nx memory list -p _cowork_test`.

### Minimum Viable Validation (RDR-126 P6)

The first-run banner + `daemon_uninstall` lifecycle is validated in two halves.

**P6-A — automated, pre-release (`scripts/p6-clean-run.sh`).** Exercises this repo's `nx-mcp` code in an isolated `$HOME` sandbox with a shimmed `launchctl`/`systemctl`, driven over a raw MCP stdio client (no model, no auth — `memory_*` is pure T2/SQLite). Verifies: `NEWLY_INSTALLED` banner variant on the first tool call (with the uninstall hint), LaunchAgent + first-run marker written, memory round-trip, banner one-shot, `daemon_uninstall` dry-run no-op, and `confirm=true` removal. The real `~/Library/LaunchAgents` daemon is never touched (the shim proves it). Run on a Linux VM as-is to cover the systemd path — `nx-mcp` branches on `sys.platform`, so the same script writes a `nexus-t2.service` unit there.

**P6-B — manual, post-release (`scripts/p6-desktop-profile.sh`).** The literal fresh-account Desktop `.mcpb` run. The Desktop extension resolves `conexus` from PyPI, so this only carries the banner/uninstall code once a release is cut. The helper stands up an isolated Claude Desktop profile (`--user-data-dir`) so a second, independently-authed instance acts as the fresh account without disturbing your primary Desktop. Constraint: quit your primary Claude Desktop first (a concurrent OAuth login across instances collides). The in-window checklist: sign in -> install `conexus.mcpb` via Settings -> Extensions -> confirm the banner on the first turn -> `memory_put`/`memory_get` round-trip -> `daemon_uninstall(confirm=true)` -> relaunch and confirm the host LaunchAgent/systemd unit stays gone. Note the Desktop `.mcpb` installs the daemon into your **real** host (`~/Library/LaunchAgents`), not the profile — that is the one host-level side effect; step 5's `daemon_uninstall` cleans it back up.

## Failure modes

- **uv not on PATH (Claude Desktop chat install)**: `.mcpb` install fails with a cryptic error. Mitigation: README documents `brew install uv` / `pipx install uv` as pre-requisite.
- **GUI-spawned subprocess can't see shell-env credentials**: `is_local_mode()` flips to True; T3 dispatch fails. Mitigation: `nx doctor` surfaces the credential-persistence warning; persist via `nx config set chroma_api_key "$CHROMA_API_KEY"` etc.
- **Cowork SDK bridge dropped a tool call**: rare; the sentinel test in `tests/test_cowork_sdk_bridge.py` catches structural regressions. Diagnostic recipe: `nx daemon t2 status` then `nx memory list -p _cowork_test`.
