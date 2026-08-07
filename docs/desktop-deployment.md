# Desktop deployment

Nexus runs in three Claude surfaces, all backed by shared host state so it round-trips across them and with the `nx` CLI. This document covers install, first-run behavior, drift detection, and uninstall for each surface. The shared-state substrate is RDR-120; the unified-surface design is RDR-126.

> **Upgrading.** 6.0 moved the permanent vector store (T3) from ChromaDB to
> the Postgres + pgvector nexus-service. After upgrading the CLI, run
> **`nx upgrade`** — one trigger provisions and verifies the service if needed,
> then walks every pending data migration (copy-not-move, rollback-safe; your
> ChromaDB store is left intact as the source). The signed native service binary + relocatable Postgres bundle
> are acquired automatically by `nx daemon service install-binary <tag>` / `nx
> init --service`. **macOS note:** that binary is ad-hoc signed (not
> Developer-ID/notarized) — `install-binary` fetches it without quarantine, but a
> copy you download from a GitHub release *page* in a browser is Gatekeeper-
> blocked; clear it with `xattr -d com.apple.quarantine <file>`.
>
> **One service.** T2 (notes/plans) and T3 both serve through the native
> `nexus-service` (Postgres 17 + pgvector) — the RDR-152 hard default, and now
> the only path. The SQLite T2 substrate is **deleted** (`nexus-i711w`) and
> `NX_STORAGE_BACKEND=sqlite` is retired (RDR-158 P3): setting it is a hard
> error carrying the stranded-install redirect — the on-disk SQLite files are
> frozen migration sources readable only by the last migration-capable 6.x
> release. Migrating **forward** is
> unaffected — `nx doctor` and `nx upgrade` do not go through that path.

## Surface 1: Claude Code (terminal)

**Audience**: developers who already use Claude Code from the command line.

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

On first session start, the plugin's SessionStart hook runs `nx upgrade --auto` (converging any pending migration) plus a preflight check. **It installs no daemon** — the T2 daemon it used to auto-install is retired (`nexus-i711w`). The storage service is provisioned by `nx init` and managed with `nx daemon service start|status|stop`.

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
4. **First launch installs and starts nothing else.** `nx-mcp` used to auto-install the T2 daemon here; that path retired with the daemon (`nexus-i711w`). The extension expects the storage service to already exist on the host — provision it once with `nx init`. Without it, tools that reach T2 or T3 fail with an unresolvable-endpoint error rather than silently starting anything.

Tool names: `mcp__conexus__*` (no `plugin_` infix — this is the .mcpb namespace, distinct from the Claude Code plugin's).

Note: Claude Code users who ALREADY have the conexus plugin should NOT also install the .mcpb. Their Claude Desktop chat already exposes Nexus tools through the plugin's local-agent-mode path. Installing the .mcpb adds a second copy under a different namespace; the model can target either but the duplication is confusing.

## CLI coexistence

`nx <verb>` commands run on the host shell. They route through the same `nexus-service` all three surfaces use (T2 hard-defaults to the service backend since RDR-152). State is fully shared:

- `nx memory put -p X -t Y` → visible from Claude Code, Cowork, and Claude Desktop chat
- `nx search foo` → searches the same T3 collections the MCP `search` tool sees
- `nx index repo .` → indexes show up immediately in all surface tool results

Service lifecycle: `nx daemon service status` / `start` / `stop` are the canonical operations (`nx init` provisions and offers the OS autostart unit); the Claude surfaces auto-start what they need on first launch. The `nx daemon t2` commands that used to cover the SQLite opt-out are retired (nexus-i711w) — that path no longer has a daemon.

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

What the update does **not** touch: the host storage service (`nexus-service` + Postgres), the T3 store, the catalog, and all stored data are owned by the OS / user account and shared across the CLI, the Claude Code plugin, and the Desktop extension. Re-installing the bundle swaps only the bundle files and its venv. The service is not restarted by an extension update.

**Version skew is expected and tolerated.** The Desktop connector (its venv) and the host CLI are independent installs that can briefly differ, since they update on different triggers (a `.mcpb` re-install vs `uv tool upgrade conexus` / a CLI reinstall). Both are clients of the same `nexus-service` over HTTP — since `nexus-i711w` there is no T2 daemon and no daemon RPC — so align them by updating whichever is behind. After a release, update both: the CLI via `uv tool upgrade conexus`, and the Desktop extension via the re-install above. The engine itself is a separate artifact on its own release cadence; `nx doctor` reports when it is below the identity this client expects.

## Uninstall

### Claude Code plugin

```
/plugin uninstall conexus@nexus-plugins
```

The plugin cache is removed. The host storage service and stored data are unaffected — they belong to the OS / user account, not the plugin.

### Claude Desktop Extension

Settings → Connectors → Desktop → Conexus → Uninstall. **Caveat**: Claude Desktop removes the `.mcpb` bundle but does NOT cascade to the LaunchAgent / systemd unit. To fully remove:

```
nx daemon service uninstall --autostart
# Optionally: rm -rf ~/.config/nexus
```

(If the box predates the T2 daemon's retirement it may also carry a
`com.nexus.t2` / `nexus-t2` unit; `nx upgrade` removes that one for you.)

The MCPB spec has no manifest-level uninstall hook, so this cascade limitation is structural.

### Cowork

Nothing to uninstall on the Cowork side — sessions inherit whatever Claude Desktop has configured.

### Daemon + data (full nuke)

```
nx daemon service uninstall --autostart  # remove autostart unit
nx daemon service stop --with-pg         # stop the nexus-service + Postgres
rm -rf ~/.config/nexus                   # remove the provisioned Postgres cluster, service binary + config, any legacy SQLite
# Managed-cloud: your data lives in the managed service, not locally — manage it there.
```

The `daemon_uninstall` MCP tool does all of the above in one step (with
`remove_data=true` for the last line), including booting out a legacy
`com.nexus.t2` unit if one is still present.

## Verification

`tests/e2e/upgrade-shakeout.sh` exercises the full surface story in a sandbox: install OLD conexus → install hooks → upgrade to current → verify drift detection → run `nx hooks update` → verify drift resolved → verify marketplace.json rename → verify plugin-name-drift detection. 11/11 green is the gate before any release.

### Cowork bidirectional sentinel (manual)

The host-side substrate round-trip is regression-tested by `tests/test_cowork_sdk_bridge.py` (a `memory_put` is visible to a later `memory_get` against the same T2, both directions). The cross-process SDK bridge itself can only be confirmed by hand, because it needs a running Claude Desktop and a Cowork session. Run this recipe after any change to the storage substrate, the SDK transport wiring, or the MCP server entry points:

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

Both directions resolving the sentinel confirms the bridge shares one T2 with the host. A failure on step 1 points at the SDK transport (the VM never reached the host service); a failure on step 2 points at write-attribution or a stale read in the shared substrate — start with `nx daemon service status` then `nx memory list -p _cowork_test`.

### Minimum Viable Validation (RDR-126 P6) — REMOVED

**Both halves are deleted.** There is no automated or manual MVV for the Desktop
surface right now. Disposition `nexus-uvn3t`, after the T2 daemon retirement
(`nexus-i711w` Stage 2 sub-stage B). Recorded here because a missing gate that
nobody knows is missing is worse than a red one.

**P6-A** (`scripts/p6-clean-run.sh`, automated, pre-release) drove this repo's
`nx-mcp` in a sandboxed `$HOME` with a shimmed `launchctl`/`systemctl` and
asserted that the first MCP tool call wrote a `com.nexus.t2` LaunchAgent and
emitted the first-run banner. Neither happens: the MCP first-run install path is
gone (tombstone in `src/nexus/mcp/_first_run.py`), and the banner it was the sole
producer of has no production caller. Dead by construction, and nothing in CI ran
it, so it would have failed first at a release cut, by hand.

**P6-B** (`scripts/p6-desktop-profile.sh`, manual, post-release) stood up an
isolated Claude Desktop profile (`--user-data-dir`) as a fresh-account stand-in
and walked: install the `.mcpb` -> banner -> `memory_put`/`memory_get`
round-trip -> `daemon_uninstall`. Every step but the last is now dead, and the
last one should not run casually:

- the banner step has no producer, as above;
- the **round-trip cannot pass on a fresh install**. It was chosen precisely
  because it was cheap and credential-free — the deleted P6-A said so outright,
  "`memory_*` is pure T2/SQLite, so no Voyage/Chroma creds are needed". RDR-152
  flipped the hard default SQLITE -> SERVICE, so `memory_put` now needs the
  engine + Postgres. Verified on a virgin `HOME` with a scrubbed env:
  `storage_backend_for("memory")` resolves SERVICE and the write fails
  `ServiceEndpointUnresolvableError`, because no lease exists and MCP boot
  starts nothing. It passes only on a box that already has a working install —
  which is not a fresh-account journey;
- `daemon_uninstall` is the one step that still does something, and it is a
  **host-level teardown**: both autostart units (the storage-service unit, and
  any legacy `com.nexus.t2` from a pre-retirement install), the engine-service +
  Postgres stack, and the first-run marker. The isolated profile scopes Claude
  Desktop, not nexus state.

Re-establishing Desktop MVV means deciding what a post-daemon first run is
supposed to DO before writing a harness that asserts it. The banner subsystem's
own fate (RDR-126 §3) was decided at nexus-37jha: deleted outright, since it
had been producer-less since the deletion above and nothing re-points it at a
service-mode first-run event. Re-establishing a service-mode first-run banner
is a new capability, not a restoration — file it separately if the
Desktop/plugin-first onboarding story wants one. Writing a new gate against
today's unspecified behaviour would recreate exactly the rot deleted here.

## Failure modes

- **uv not on PATH (Claude Desktop chat install)**: `.mcpb` install fails with a cryptic error. Mitigation: README documents `brew install uv` / `pipx install uv` as pre-requisite.
- **The `.mcpb` reads `config.yml`, NOT your shell env — the mode record and service URL must be persisted, or the extension silently runs local mode** (the single most likely Desktop footgun). Claude Desktop spawns the `.mcpb` as a GUI subprocess that does **not** inherit your interactive shell's environment. `is_local_mode()` resolves the persisted mode record, then `service_url`, then PG credentials — via `~/.config/nexus/config.yml`, never `~/.zshrc`/`~/.bashrc` exports. (It does NOT key on API keys: `VOYAGE_API_KEY` is no longer a client credential at all — the client does no embedding — and `CHROMA_API_KEY` no longer exists.) So a machine whose cloud configuration lives only in shell exports will run the extension in **local mode** (bge-768 local embedder), even though your CLI in a terminal resolves cloud mode fine. Symptoms: searches return "no results" or feel thin, and `~/Library/Logs/Claude/mcp-server-Conexus.log` shows `collection_dimension_mismatch_skipped` / `search_all_collections_dimension_skipped` — typically `got 768` (local bge query) against collections that expect `1024` (voyage). The bge-768 local query simply cannot match cloud voyage-1024 collections. Fix: persist the cloud config via `nx config set` (or re-run `nx init --cloud`).

  **6.0 cloud creds** are the managed nexus-service endpoint + bearer token
  (`NX_SERVICE_URL` + `NX_SERVICE_TOKEN`); embedding runs server-side, so you do
  **not** supply a Voyage key. The legacy `chroma_api_key` / `chroma_tenant` /
  `chroma_database` keys are the retired pre-6.0 ChromaDB-Cloud surface.

  > **Managed-cloud Desktop (the working path):** persist the endpoint + token
  > to `config.yml` — `nx config set service_url <url>` and
  > `nx config set service_token <token>` (or `nx config init`). Resolution is
  > env-first-then-`config.yml` (RDR-166), so the `.mcpb` (which never sees
  > your shell env) picks the persisted values up on relaunch; a terminal
  > session with `NX_SERVICE_*` exported still wins there. See
  > [Managed-Cloud Credentials](configuration.md#managed-cloud-credentials).
  > For a **local** install there is no footgun: `nx init` writes the token to
  > `~/.config/nexus/pg_credentials` and the service is discovered via its lease.
  Then fully quit + relaunch Claude Desktop so the extension re-spawns and re-reads `config.yml`. **Verify** it took: search for something specific and ask for the top `file:line` results with scores; confirm the cited locations are real (a great-sounding answer is not proof — the model can reconstruct from `store_get`/FTS even when vector search is skipping), and confirm the Conexus log shows no `dimension_mismatch_skipped`. On a fresh machine with no CLI, you only get local mode (bge-768) — fine for content you index locally at 768d, but it will not reach pre-existing cloud-1024 collections until the creds are in `config.yml`.

  Related: a single name/dim-mismatched collection (e.g. a stale collection named `…minilm-l6-v2-384…` that actually stores 1024-dim vectors) produces the same `dimension_mismatch_skipped` line on every search and should be deleted (`nx collection delete <name>`); an `nx doctor` drift check for this class is tracked separately.
- **Cowork SDK bridge dropped a tool call**: rare; the sentinel test in `tests/test_cowork_sdk_bridge.py` catches structural regressions. Diagnostic recipe: `nx daemon service status` then `nx memory list -p _cowork_test`.
