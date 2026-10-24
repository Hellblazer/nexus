# Desktop deployment

Nexus runs in three Claude surfaces, all backed by shared host state so it round-trips across them and with the `nx` CLI. This document covers install, first-run behavior, drift detection, and uninstall for each surface. The shared-state substrate is RDR-120; the unified-surface design is RDR-126.

> **Upgrading from a pre-PG (Chroma-era) install.** 6.0 moved the permanent
> vector store (T3) from ChromaDB to the Postgres + pgvector nexus-service;
> 7.x then deleted the Chroma read path outright (RDR-155 P4b), so **this
> release's `nx upgrade` cannot read or migrate a Chroma/SQLite install** —
> running it against unmigrated pre-PG data trips a loud two-hop redirect
> instead of migrating anything. If you are on a pre-6.0 install (or still
> carry `chroma.sqlite3` / `t2.db` / `memory.db` on disk), do NOT run a bare
> `nx upgrade` on this version expecting it to migrate you: install the
> pinned `conexus==6.18.1`, run `nx upgrade` **there** to migrate the data
> (copy-not-move, so the Chroma files stay behind afterward as a relic
> nothing reads, with no path back to that era, Sam 2026-08-29),
> then upgrade back to this version. Run it against a local engine, with
> `NX_SERVICE_URL` and the `service_url` config key cleared, `NX_LOCAL=1`
> exported (needed in addition; it does not by itself override `service_url`),
> and no `--service-url`, even if you mean to end up on the managed cloud; that is a
> later hop with the current client.
> [migration-runbook.md § Installs that predate Postgres](migration-runbook.md#installs-that-predate-postgres)
> carries that procedure — including the exact first-hop command, which
> **differs by install layout**, so do not assume the one you last used —
> and the operator's manual order of operations if the guided path blocks.
>
> **On an install that is already PG-backed**, `nx upgrade` is the one
> trigger: it provisions and verifies the service if needed, then walks
> every pending data migration (idempotent, resumable). The signed native
> service binary + relocatable Postgres bundle are acquired automatically by
> `nx daemon service install-binary <tag>` / `nx init`. **macOS note:** that
> binary is ad-hoc signed (not Developer-ID/notarized) — `install-binary`
> fetches it without quarantine, but a copy you download from a GitHub
> release *page* in a browser is Gatekeeper-blocked; clear it with
> `xattr -d com.apple.quarantine <file>`.
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

There are two routes, and both end in the same install:

1. **Route one, one command in a terminal.** `curl -LsSf https://hellblazer.github.io/nexus/install.sh | sh` on macOS and Linux, or `irm https://hellblazer.github.io/nexus/install.ps1 | iex` in a normal PowerShell window on Windows. It installs `uv` if you do not have it, installs the CLI, adds the plugin to Claude Code when `claude` is on your `PATH`, and asks you one question before it downloads anything. With Local it asks a second, whether to start at login. The script's question has no Not now choice, because typing the command is your permission to install. To skip the question, add a flag (`--local`, `--cloud` or `--cli-only`, and with `--local` also `--autostart` or `--no-autostart`): after `sh -s --` on macOS and Linux, and on Windows as `& ([scriptblock]::Create((irm https://hellblazer.github.io/nexus/install.ps1))) --local --no-autostart`, because the `iex` form cannot take flags. The start-at-login question defaults to No. If you press Enter at the question, or the script has no terminal, it installs the `nx` command only. `/conexus:setup` in Claude Code, or `nx init` (Local) or `nx config init` (Cloud) in a terminal, sets up a service later. On Linux the script asks `uv` for the CPU build of torch. If your `uv` is older than 0.9.19, run `uv self update` first, or the build downloads about 4.5 GB of GPU libraries that Nexus does not use.
2. **Route two, from inside Claude.** Install `uv` (and git, if you do not have it), add the plugin, and answer one question. When the install finishes, run `/reload-plugins` or start a new session, and in that session Claude asks it:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

The question has four choices:

| Choice | What happens | Download |
|---|---|---|
| Local | Install the CLI and run a Nexus service on this machine | about 0.8 GB down (0.9 GB on Linux), 2.5 to 2.9 GB on disk |
| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | about 0.45 GB down (0.6 GB on Linux), 1.8 to 2.2 GB on disk |
| CLI only | Install the CLI and decide about a service later | about 0.45 GB down (0.6 GB on Linux), 1.8 to 2.2 GB on disk |
| Not now | Nothing is installed | none |

These sizes include PDF support (docling, MinerU and torch), which the setup tool installs by default. The setup tool's `slim` option leaves it out (about 0.25 GB less down on macOS, 0.37 GB on Linux; 0.85 GB less on disk on macOS, 1.3 GB on Linux), and the first PDF you index then fetches it. The sizes are measured on 7.85.0 on macOS and Linux (2026-10-24): the download is the wheels in the lock, the Python and, for Local, the service and its models; the sizes on disk are the install itself. Windows was not measured.

`uv` also keeps a download cache, about 2.1 GB on Linux and 1.8 GB on macOS after a first install (an earlier 7.81.0 measurement found 4.8 GB on Windows). On Linux the cache and the install together measured about 2.2 GB, not 4.2 GB, because they share files, so the cache is not all extra space. `uv cache clean` empties it but frees only the files the install does not share. Besides Claude itself and git, `uv` is the one prerequisite of route two. Route two is described under "First run" below, and the commands that both routes run for you are under "Install by hand" at the end of that section.

On first session start with the CLI installed, the plugin's SessionStart hook runs `nx upgrade --auto` (converging any pending migration) plus a preflight check. **It installs no daemon** — the T2 daemon it used to auto-install is retired (`nexus-i711w`). The storage service is provisioned by `nx init` and managed with `nx daemon service start|status|stop`.

Tool names: `mcp__plugin_conexus_nexus__*` and `mcp__plugin_conexus_nexus-catalog__*`. Slash-command prefix: `/conexus:*`.

### First run

The plugin installs the Nexus CLI only after you answer the question, and the plugin's servers and most of its hooks are programs of the CLI. What a first session does depends on whether the CLI is there (RDR-228, Phase 1):

- **The CLI is installed and `nx init` has run.** The two Nexus servers start and connect, and the session-start hooks run. This is the working state.
- **The CLI is installed and `nx init` has not run.** The servers start, and every tool that needs the service reports that it cannot reach it. Run `nx init`.
- **The CLI is not installed.** Nothing is installed until you answer. Each Nexus server entry starts a stand-in that connects and offers two tools, `setup` and `setup_status`. `setup` installs only after the model has asked you and acted on your answer (`local`, `cloud`, `cli-only` or `not-now`; `cloud` opens a page in your browser where you type the managed service's address, token and tenant yourself); it starts a background process, downloads up to about 0.8 GB (0.9 GB on Linux) for the local choice, PDF support included, and `setup_status` follows it. Without the plugin's setup process, or on a host that cannot run it, the tools answer with the commands below and install nothing. One session-start message tells the model that Nexus is not installed. In a Desktop conversation the hooks run in a cloud workspace and that message is not sent; the stand-in's tool descriptions say it there. Every other command hook that needs the CLI writes one line to its error stream and does nothing; the lockstep hook says nothing; the hook that calls a server tool when a subagent starts finds no such tool on the stand-in, which Claude Code records in the subagent's transcript and does not show. The 17 `/conexus:*` commands that run `nx` fail with the shell's "command not found".

When setup is done, in Claude Code reconnect the `nexus` server (`/mcp`, the server, Reconnect) or start a new session. In Claude Desktop quit the app fully and open it again: Desktop keeps a plugin's server running across conversations and has no Reconnect, so a new conversation still talks to the stand-in. `/conexus:setup` runs the same question again for a user who answered Not now.

The plugin's three local servers and all of its command hooks start through `uv`, so `uv` must be on the `PATH` the application starts with. Without `uv` none of them runs and the application shows the servers as failed. Skills, commands and the `sn` plugin's hosted server do not need it.

### Install by hand

When a route fails, or when you want each step yourself, the install is three terminal commands, then a reconnect of the `nexus` server or a new session:

```bash
uv tool install conexus --python 3.12     # on Linux add: --torch-backend cpu
nx self install
nx init
```

The commands are the same on Windows; [Installing nexus on Windows](windows-install.md) has that platform's prerequisites.

## Surface 2: Claude Desktop chat and Cowork (account plugin)

**Audience**: Claude Desktop users who open chat or Cowork sessions.

**This is the recommended Desktop route.** The `.mcpb` extension (Surface 3) does not update itself; the account plugin runs the `nx` you have installed and installs the CLI for you. The record behind it (RDR-228): one live Desktop conversation on macOS, and Desktop's local half on macOS and Windows driven through an MCP client that starts the plugin's server as Desktop does; no live Windows Desktop conversation was run. If you run the `.mcpb`, switch to this surface, and once the plugin works remove the extension (Settings → Connectors → Desktop → Conexus → Uninstall) so two servers do not run.

Claude Desktop keeps two plugin stores. Claude Code and the Desktop Code tab read `~/.claude`, which is where `claude plugin install` writes. Chat and Cowork read the plugins enabled on your claude.ai account, which Desktop downloads itself. A plugin installed only from the command line is not visible to chat or Cowork: on a clean Windows 11 machine with only the CLI plugin installed, the Code tab found the plugin and chat and Cowork found none (measured 2026-10-11, conexus 7.77.1, Claude Desktop 1.24012.9.0).

Install, with the same two routes as Surface 1:

1. **Route one.** Run the one command from Surface 1 in a terminal. It cannot reach the plugin store of your claude.ai account, so you still do the plugin step of route two in the application.
2. **Route two.** Install `uv`. In Claude Desktop, open Customize → Plugins, add the marketplace `Hellblazer/nexus`, and install `conexus` (and `sn` if you want it). If you added `Hellblazer/nexus` before, remove it and add it again first. An older copy has no setup tool. Then open a conversation and say "Set up Nexus". Claude calls the plugin's `setup` tool, shows the four choices from Surface 1, and asks which you want. Desktop shows no message when Nexus is missing, because the session-start hook runs in a cloud workspace and not on your machine (measured 2026-10-16: a Desktop conversation starts no local Claude Code session), so you have to ask. Do not type `/conexus:setup` in Desktop; it lists the plugin's skills, not its commands (seen on Claude Desktop 2.42304.0 on macOS). Desktop has no progress display: ask "Is Nexus set up yet?" and Claude reads the status with `setup_status`. When it says setup is done, quit Claude Desktop fully and open it again: Desktop keeps the plugin's server running across conversations, so a new conversation still talks to the stand-in.

The plugin runs `nx-mcp` from the CLI install, so the account plugin alone is not enough: without the CLI each Nexus server entry starts a stand-in that has only the two setup tools (see "First run" under Surface 1). To install the CLI by hand instead, see "Install by hand" under Surface 1.

The plugin copy does not follow release tags. To upgrade it after a new release, remove the `Hellblazer/nexus` marketplace in Customize → Plugins and add it again. On the measured machines an account marketplace that was added earlier stayed at the release it was added at (7.67.0 through 15 releases, while Desktop synced every 20 minutes), and conexus could not be enabled from it until the marketplace was removed and added again (RDR-228 Key Discovery 6). Why it does not follow new release tags is not yet understood. Nexus itself is updated through the CLI: run `nx self install`, and the plugin then runs whichever CLI is installed.

Having both the account plugin and the CLI plugin is fine. The Code tab logged that the plugin exists in both places and used the account copy, so there is one set of tools.

Both Nexus servers (`nexus` and `nexus-catalog`) connected from the account plugin on both the macOS and the Windows machine; Desktop starts them on the host with a login-shell `PATH` (RDR-228 Key Discovery 5, conexus 7.77.1). An earlier install on the same macOS machine, before the plugin ran the installed `nx-mcp`, failed both with `MCP error -32000: Connection closed` (nexus-qkb9v).

State is shared with the host CLI Claude and any other session running against the same service. `tests/test_cowork_sdk_bridge.py` covers the host-side round trip through T2.

Session scratch (T1) is not available in a Desktop chat or a Cowork session. The Nexus server has no session id. Claude Desktop sets none for the server it spawns, and the server does not borrow the last Claude Code session's. A Cowork conversation's cloud container does carry `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE=1` and `CLAUDE_CODE_ENTRYPOINT=remote_cowork`, but also `CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS=1`, so `nx-mcp` runs on the Mac, spawned by Desktop, and never sees them (measured 2026-10-21 through the remote-devices bridge: T1 unavailable). So the `scratch` tools report that T1 is unavailable for the process; search, memory, store and catalog work as usual, and T2 (`memory_put`) is the store that carries notes across conversations. A server you configure yourself can be given a scratch scope by setting `NX_SESSION_ID` in its environment; the account plugin's server entry is not editable, so there T1 stays unavailable. `mailbox_send` and `tuple_subscribe` need the same variable (`NX_SESSION_ID`, a UUID, unique per server); on the account-plugin server a Desktop or Cowork session can pass its real session id as `mailbox_send`'s `from_address` (verified 2026-10-21). That server has no session token to send, so the engine records no session for the write and a recipient's drain hook and channel mark the message `unattested`; that is the expected reading for mail from here, not a fault (see [Tuple Space](tuple-space.md)). On the account-plugin server Claude Desktop starts (chat, Cowork), `tuple_subscribe` is refused, since no session id resolves there. On a self-configured server outside Claude Code that sets `NX_SESSION_ID`, it arms the directory name and the channel waiter runs, but whether that host shows a channel push is unverified, so read with `tuple_in`/`tuple_rd`.

## Surface 3: Claude Desktop chat (Desktop Extension)

**Audience**: Claude Desktop users who do NOT have Claude Code installed.

**Not the recommended route.** The extension does not update itself, so users sit on old versions; prefer the account plugin in Surface 2. The extension stays available.

The plugin route in Surface 2 is the one that installs the CLI for you. The extension is an alternative, and it needs the CLI and the service installed by hand first ("Install by hand" under Surface 1). Do not run the extension and the plugin together: once the plugin works, uninstall the extension (see "Uninstall" below), because two servers run side by side otherwise.

Pre-requisite: [uv](https://docs.astral.sh/uv/) on host PATH.

- macOS: `brew install uv`
- Linux: `pipx install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (from conexus 7.72.0, RDR-224; full path in [Installing nexus on Windows](windows-install.md)): `winget install astral-sh.uv`. The bundle's launcher resolves `uv` through `PATHEXT` (to `uv.exe`). Windows has no `exec`, so the launcher runs the MCP server as a child on its own stdin and stdout handles, waits for it, exits with its exit code, and ends the server with it if the host kills the launcher (a Job Object). The service itself still comes from `nx init --service`, which works on native Windows x64 (tested from PyPI on a clean Windows 11 guest at conexus 7.72.1). The `.mcpb` bundle itself has not been walked end to end on Windows (nexus-ijue9.21), so treat this path as untested; Windows is not yet declared supported. Tracked in RDR-224.

Install:

1. Download `conexus.mcpb` from the [latest GitHub release](https://github.com/Hellblazer/nexus/releases/latest).
2. Double-click the file. Claude Desktop registers it under Settings → Connectors → Desktop as "Conexus". On Windows the double-click does nothing: the Claude Desktop package registers only the `claude:` protocol and no `.mcpb` file type, and the Customize → Plugins upload dialog accepts zips and hides `.mcpb` files (measured 2026-10-11, Claude Desktop 1.24012.9.0). No working way to install the bundle on Windows has been found, so use the account plugin (Surface 2) there.
3. First launch: uv resolves Nexus's dependency stack (pydantic-core, tree-sitter, numpy, torch, onnxruntime, and friends — `chromadb` was removed from the dependency set at RDR-155 P4b). Cold install ~20s on a warm network; warm restarts ~5s; the first start with Include PDF support on (below) downloads the PDF stack too and takes longer. If the install lands inside PyPI's post-release propagation window (~10-25 min where the download index does not yet serve a just-published version — nexus-r433b), the bundle's bootstrap retries the resolution with backoff for ~15 min, logging `[conexus-mcpb] PyPI has not finished propagating…` to the extension's log each attempt, before giving up with a try-again-shortly message. `NX_MCPB_SKIP_RESOLVE_RETRY=1` (or `true`) disables the retry, and with it the dependency lock: the launch then hands off to `uv run` with no step left to fall back unpinned, so it installs unpinned (nexus-puzdu).
4. **First launch installs and starts nothing else.** `nx-mcp` used to auto-install the T2 daemon here; that path retired with the daemon (`nexus-i711w`). The extension expects the storage service to already exist on the host — provision it once with `nx init`. Without it, tools that reach T2 or T3 fail with an unresolvable-endpoint error rather than silently starting anything.

Tool names: `mcp__conexus__*` (no `plugin_` infix — this is the .mcpb namespace, distinct from the Claude Code plugin's).

Note: if you have the conexus account plugin (Surface 2), do not also install the `.mcpb`. The bundle adds a second copy of the tools under a different namespace. That combination has not been tested. The Claude Code plugin by itself does not give chat or Cowork any tools; see Surface 2.

## CLI coexistence

`nx <verb>` commands run on the host shell. They route through the same `nexus-service` all three surfaces use (T2 hard-defaults to the service backend since RDR-152). State is fully shared:

- `nx memory put -p X -t Y` → visible from Claude Code, Cowork, and Claude Desktop chat
- `nx search foo` → searches the same T3 collections the MCP `search` tool sees
- `nx index repo .` → indexes show up immediately in all surface tool results

Service lifecycle: `nx daemon service status` / `start` / `stop` are the canonical operations (`nx init` provisions and offers the OS autostart unit). No Claude surface installs the CLI, provisions the service or starts it; see "First run" under Surface 1. The `nx daemon t2` commands that used to cover the SQLite opt-out are retired (nexus-i711w) — that path no longer has a daemon.

## Drift detection

After upgrading conexus or after the plugin rename (`nx` → `conexus` at v5.0.0), `nx doctor` surfaces two kinds of drift:

- **Plugin name drift**: the installed Claude Code plugin still has `name: "nx"` but the CLI expects `conexus`. Fix is three commands, in order:

  ```
  /plugin uninstall nx@nexus-plugins      # in Claude Code: removes the old plugin and its hooks
  /plugin install conexus@nexus-plugins   # in Claude Code: registers the new plugin
  /reload-plugins                          # in Claude Code: activates it
  ```

  The uninstall is not optional. Claude Code does not remove a renamed plugin, and the old `nx` plugin keeps its own hooks while it is installed: v4.34.x hooks auto-approved every Bash command (GHSA-mc84-6gjq-vm2p). `nx doctor` fails the "Retired Claude Code plugin" row, and every MCP startup logs `retired_plugin_still_installed`, while `nx@nexus-plugins` is still in the plugin registry. Install alone leaves the new plugin staged but inactive; reload alone won't pick up the renamed plugin from marketplace.json.

- **Post-commit hook stanza drift**: the installed `.git/hooks/post-commit` predates the pgrep guard fix (nexus-mkj6u 2026-05-23). Fix:

  ```
  nx hooks update <repo>
  ```

Both warnings include the resolution commands; `nx doctor` is the single explicit-invocation surface that consolidates them.

For Claude Desktop `.mcpb` users specifically, the bundle also performs a best-effort stale-install check at MCP server startup (MCPB v0.4 has no auto-update). When the installed `conexus` is older than the latest on PyPI, it writes a warning to stderr, which Claude Desktop puts only in the extension's log (`~/Library/Logs/Claude/mcp-server-Conexus.log` on macOS); a user who never opens that log does not see it, so this page and the install page are the main way `.mcpb` users learn of the plugin route. The warning says the extension does not update itself and names the recommended route, the conexus plugin (Surface 2), with the switch steps and the release URL as the manual fallback:

```
[conexus-mcpb] installed conexus=X.Y.Z, latest on PyPI=A.B.C. This .mcpb
extension does not update itself. The recommended Desktop route is the
conexus plugin: it runs the nx you have installed, so `nx self install`
updates Nexus for it. In Claude Desktop open Customize -> Plugins, add the
marketplace Hellblazer/nexus (if you added it before, remove it and add it
again, because the plugin copy does not follow new releases), install
conexus, say "Set up Nexus", then quit Claude Desktop fully and open it
again. Afterwards remove the Conexus extension (Settings -> Connectors) so
two servers do not run. To stay on the .mcpb, re-download it from
https://github.com/Hellblazer/nexus/releases/latest and re-install it.
(Set NX_MCPB_SKIP_UPDATE_CHECK=1 to silence.)
```

The check is non-fatal: a network failure, timeout, or unreachable PyPI never blocks startup. Set `NX_MCPB_SKIP_UPDATE_CHECK=1` in the environment to opt out entirely.

## Updating the Desktop Extension

The `.mcpb` does **not** auto-update, and the account plugin (Surface 2) is the recommended Desktop route, because it runs your installed `nx` and installs the CLI for you; the plugin copy itself is upgraded by removing and re-adding the marketplace (see Surface 2). If you stay on the `.mcpb`, the steps below are the only update path. MCPB v0.4 has no update mechanism, and the extension's Python environment is pinned at install time: Claude Desktop builds a `uv` venv under `~/Library/Application Support/Claude/Claude Extensions/local.mcpb.<id>.conexus/.venv` from the bundle's `pyproject.toml` (`conexus>=X.Y.Z`), and every launch runs `uv run` against **that existing venv**. `uv run` reuses the resolved venv rather than re-resolving against PyPI, so a newer published conexus is not picked up until the bundle is re-installed and the venv is rebuilt. The venv is built under the release's dependency lock: the bundle carries the release's `constraints.txt` (the file the wheel and the installers ship) and `bootstrap.py` writes it into the bundle's `pyproject.toml` before `uv sync`, so a transitive dependency published after the release cannot reach the extension. The bundle also pins `conexus` itself to the release it was built from. If the sync cannot resolve with the pins in force (any failure other than the post-release PyPI lag), the launcher removes them, logs that to the extension's log, and installs once without them rather than failing the start; a bundle packed without the file installs unpinned and logs that too. Every launch re-applies the pins, so an extension that fell back repeats the failed pinned resolve first.

To opt out of the lock (resolve every dependency fresh; versions may then differ from the release's lock), use any one of:

- **The extension's setting.** The extension's settings in Claude Desktop (Settings → Connectors → Desktop → Conexus) list "Install the newest dependencies (unpinned)". It reaches the launcher as `NX_MCPB_INSTALL_UNPINNED=true`. Restart the extension.
- **An environment variable the app can see**: `NX_INSTALL_UNPINNED=1`, the variable the CLI installers read. Desktop does not inherit a shell's environment, so on macOS run `launchctl setenv NX_INSTALL_UNPINNED 1` and quit and reopen Claude Desktop; on Windows set a user environment variable (System Properties → Environment Variables) and restart Claude Desktop. Only `1` and `true` count.
- **`NX_MCPB_SKIP_RESOLVE_RETRY=1`**, which also skips the PyPI-propagation retry (above).
- **Delete `constraints.txt`** from the extension directory (`~/Library/Application Support/Claude/Claude Extensions/local.mcpb.<id>.conexus/` on macOS). The launcher then installs unpinned and logs that the file is missing.

**PDF support is opt-in for the extension** (nexus-dzc61). The bundle installs bare `conexus`, so PDF indexing is off unless you turn on "Include PDF support" in the extension's settings (Settings → Connectors → Desktop → Conexus) and restart the extension. It reaches the launcher as `NX_MCPB_INCLUDE_PDF=true`, and `bootstrap.py` rewrites the bundle's requirement to `conexus[pdf]` before the sync: about 0.25 GB more to download and 0.85 GB more on disk on macOS (the CLI's measured figures; on hellmini on 2026-10-24 the extension's venv went from 791 MB to 1.7 GB). The model weights follow on first use (about 0.5 GB, plus 1.2 GB for the first PDF with formulas). Turning it off writes the bare requirement back, and the next start's sync removes the PDF packages (77 of them in that measurement); downloaded model weights and uv's download cache stay. The sizes are macOS figures: on Linux the bundle takes PyPI's torch, which is the CUDA build and several GB larger (the CLI installers choose the CPU build there; the extension does not). The extra is not declared in the bundle's `pyproject.toml`, because `uv sync` resolves every declared extra on every start: with the setting off, a start resolves exactly what it did before the setting existed. If the sync fails with PDF support on, the launcher logs it, reverts to the bare requirement, re-applies the release's lock and syncs once more, so a PDF-only failure does not stop the extension starting (a failure that bare would hit too, such as PyPI's post-release lag outlasting the retries, still does). With `NX_MCPB_SKIP_RESOLVE_RETRY=1` the launcher does not sync and leaves the requirement as the last start wrote it. The first start after turning the setting on downloads the PDF stack before the server answers; on a slow connection Claude Desktop may report that the extension failed to start, and restarting it continues from uv's download cache. With the setting off, or after a fallback, indexing a PDF stops before writing anything (exit 69) and names the setting. The release's resolve gate (`scripts/check_mcpb_resolve.py`) resolves both variants.

So the update is a manual, idempotent re-install:

1. **Download** the new `conexus.mcpb` from the [latest release](https://github.com/Hellblazer/nexus/releases/latest) (the same asset attached to every GitHub release alongside the wheel and sdist).
2. **Double-click it** (or Claude Desktop → Settings → Connectors → Desktop → install). Claude Desktop replaces the existing "Conexus" extension in place; you do not need to uninstall first.
3. **First launch resolves the new version.** `uv` rebuilds the venv from the new manifest's `conexus>=X.Y.Z` pin (~20s cold, ~5s warm; longer with Include PDF support on), pulling the new conexus from PyPI. The stale-install warning stops firing once installed == latest.
4. **Verify** (optional): the installed version is the `version` field in the extension's `manifest.json`, and the resolved package is `<ext>/.venv/bin/python -c "from importlib.metadata import version; print(version('conexus'))"`.

What the update does **not** touch: the host storage service (`nexus-service` + Postgres), the T3 store, the catalog, and all stored data are owned by the OS / user account and shared across the CLI, the Claude Code plugin, and the Desktop extension. Re-installing the bundle swaps only the bundle files and its venv. The service is not restarted by an extension update.

**Version skew is expected and tolerated.** The Desktop connector (its venv) and the host CLI are independent installs that can briefly differ, since they update on different triggers (a `.mcpb` re-install vs a CLI reinstall). Both are clients of the same `nexus-service` over HTTP — since `nexus-i711w` there is no T2 daemon and no daemon RPC — so align them by updating whichever is behind. After a release, update both: the CLI via `nx self install`, and the Desktop extension via the re-install above. `nx self install` builds a new `~/.local/share/nexus/tools/gen-<stamp>`, flips the `current` pointer and rewrites the shims; `uv tool upgrade conexus` does not touch a generation install at all, and is the right command only on a box still on the legacy uv tree. The engine itself is a separate artifact on its own release cadence; `nx doctor` reports when it is below the identity this client expects.

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

Remove `conexus` (and the `Hellblazer/nexus` marketplace, if you want it gone) in Claude Desktop under Customize → Plugins. This is separate from the Claude Code plugin; uninstalling one leaves the other.

### Daemon + data (full nuke)

```
nx uninstall                       # DRY RUN: preview what would be removed
nx uninstall --yes --remove-data   # perform the teardown, including local data
```

Dry-run by default (`--yes` is required to act). With `--remove-data` it
also wipes the local nexus data dir (irreversible; a shallow-path guard
refuses a misconfigured `NEXUS_CONFIG_DIR`) and the model cache at
`~/.cache/nexus`. On a local install this stops
the engine-service + Postgres stack, removes the OS autostart unit, and
clears the first-run marker. On a managed-only client there is no local
service to stop — it clears the local endpoint config (`service_url` +
`service_token`) and never touches the remote tenant's data.

The `daemon_uninstall` MCP tool does the equivalent teardown in one step for
in-chat use (`confirm=true` to act, plus `remove_data=true` for the data
wipe — `confirm=false` only describes what would be removed), including
booting out a legacy `com.nexus.t2` unit if one is still present.

## Verification

The full surface story (install OLD conexus → install hooks → upgrade to current → verify drift detection → run `nx hooks update` → verify drift resolved → verify marketplace.json rename → verify plugin-name-drift detection) was exercised by a sandbox upgrade shakeout that was deleted in cleanup step 11; `tests/e2e/hook-surface-shakeout` is the surviving plugin-hook gate.

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
- **The `.mcpb` reads `config.yml`, NOT your shell env — the mode record and service URL must be persisted, or the extension resolves the wrong embedding mode** (the single most likely Desktop footgun). Claude Desktop spawns the `.mcpb` as a GUI subprocess that does **not** inherit your interactive shell's environment. `is_local_mode()` resolves the persisted mode record, then `service_url`, then PG credentials — via `~/.config/nexus/config.yml`, never `~/.zshrc`/`~/.bashrc` exports. (It does NOT key on API keys: `VOYAGE_API_KEY` is no longer a client credential at all — the client does no embedding — and `CHROMA_API_KEY` no longer exists.) So a machine whose cloud configuration lives only in shell exports will run the extension in **local mode** (bge-768 local embedder), even though your CLI in a terminal resolves cloud mode fine, and the bge-768 local query cannot match cloud voyage-1024 collections.

  **Symptom (nexus-vply6, fixed 2026-09-24 — every platform, not just this one): a NAMED, LOUD error, never a silent empty result.** Older versions of this note described a silent failure diagnosed by grepping `~/Library/Logs/Claude/mcp-server-Conexus.log` for `collection_dimension_mismatch_skipped` / `search_all_collections_dimension_skipped` — those lines never existed on any real (service-backed) install; they were retired-Chroma-era test-double log events that this doc cited by mistake. What actually happens now: the search/query tool's response text (readable directly in the Claude Desktop chat, no log file needed) starts with `this install's current query-side embedding mode (<mode>) cannot serve N of the targeted collection(s): ...` and names, for each affected collection, the model it was written with and this process's actual serving mode (`onnx-local` for the footgun above). The same message names BOTH remedies below by case, so read it before picking one:

  - **Managed/cloud service intended** (this footgun): persist the cloud config via `nx config set service_url <url>` / `nx config set service_token <token>` (or the interactive wizard, `nx config init`; there is no `nx init --cloud` flag), then fully **quit and relaunch Claude Desktop** so the extension re-spawns and re-reads `config.yml` — a terminal `nx config set` alone does nothing for an already-running `.mcpb` process.
  - **Local install with Voyage embeddings intended**: the already-running service has not picked up `NX_VOYAGE_API_KEY` (it is read only at process spawn); set it in the SERVICE's own environment and restart the service: `nx daemon service stop && nx daemon service start`.

  **6.0 cloud creds** are the managed nexus-service endpoint plus either a
  mint credential and its tenant (`NX_SERVICE_URL` + `NX_MINT_TOKEN` +
  `NX_MINT_TENANT`, saved as `service_url`, `mint_token` and `mint_tenant`) or a
  static service token (`NX_SERVICE_URL` + `NX_SERVICE_TOKEN`); the setup
  page and `nx config init` ask for the kind you hold. Embedding runs server-side, so you do
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
  Then fully quit + relaunch Claude Desktop so the extension re-spawns and re-reads `config.yml`. **Verify** it took: search for something specific and ask for the top `file:line` results with scores; confirm the cited locations are real (a great-sounding answer is not proof — the model can reconstruct from `store_get`/FTS even when vector search is skipping), and confirm the search/query response text no longer opens with `this install's current query-side embedding mode (...) cannot serve`. On a fresh machine with no CLI, you only get local mode (bge-768) — fine for content you index locally at 768d, but it will not reach pre-existing cloud-1024 collections until the creds are in `config.yml`.

  Related, but a DIFFERENT failure shape: a single stale, orphaned collection (e.g. named `…minilm-l6-v2-384…` but actually storing 1024-dim vectors — leftover from a prior embedder generation, not a current mode mismatch) is tolerated gracefully when OTHER targeted collections are healthy — it is skipped and logged (`collection_search_failed`, always at WARNING now, never silently downgraded), and the CLI/MCP response both name it explicitly so a partial result is never mistaken for a complete one. It should still be deleted (`nx collection delete <name>`); an `nx doctor` drift check for this class is tracked separately. If a search's ENTIRE targeted scope is that class of orphan (not just one collection among many), it now raises the same named error as the mode-mismatch case above, rather than returning an empty result.
- **Cowork SDK bridge dropped a tool call**: rare; the sentinel test in `tests/test_cowork_sdk_bridge.py` catches structural regressions. Diagnostic recipe: `nx daemon service status` then `nx memory list -p _cowork_test`.
