# Getting Started with Nexus

## Prerequisites

- **Python 3.12 or 3.13** (3.14 is not yet supported — [upstream dependency issue](https://github.com/pydantic/pydantic/issues))
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **git**
- **[Node.js](https://nodejs.org/)** — required *only* if you install the Claude Code plugin. The plugin bundles the `sequential-thinking` and `context7` MCP servers, both spawned via `npx -y …`, which requires `node` and `npm` on PATH. The `nx` CLI alone does not need it. Install with `brew install node` (macOS) or follow the [Node.js installer](https://nodejs.org/) for your platform.

Check your Python version:

```bash
python3 --version
```

If you're on 3.14+, install 3.13 with `uv python install 3.13` — uv will use it automatically.

## Install

```bash
uv tool install conexus
nx init                       # guided: choose your local embedder
```

The default install uses the built-in ONNX MiniLM embedder (384-dim). `nx init` then walks you through the embedder choice as a guided, informed decision (see [Choosing a local embedder](#choosing-a-local-embedder-nx-init) below): the recommended **bge-768** (BAAI/bge-base-en-v1.5, 768-dim, materially better local search, ~140 MB one-time model download) or the bundled **minilm-384** (instant, lower quality). Choosing bge-768 adds the `[local]` extra for you and provisions the model. The default install also includes MinerU for math-aware PDF extraction (~500 MB of Python deps; first PDF index downloads ~2-3 GB of models, see [PDF indexing notes](#pdf-indexing-and-mineru-models) below).

You can still request the extra directly at install time with `uv tool install "conexus[local]"`, but `nx init` is the recommended path: it presents the trade-off, provisions the model, and detects pre-existing 384-dim collections that would need migrating.

### Updating

```bash
uv tool upgrade conexus
```

Always upgrade with `uv tool upgrade conexus` — it preserves the spec you installed with, so a `[local]` install stays `[local]`. **Do not** re-run `uv tool install conexus` (or `--force`) just to upgrade: that resets the environment and **drops `[local]`**, silently downgrading the embedder 768→384-dim, which dimension-mismatches existing 768-dim collections and makes search return nothing. To recover: `uv tool install --reinstall "conexus[local]"`. When you update the Claude Code plugin, upgrade the CLI to the matching version at the same time.

Verify:

```bash
nx --version
nx doctor
nx doctor --check-mineru   # confirms MinerU is importable (since nexus-2fyb)
```

`nx doctor` checks dependencies, credentials, and connectivity. Everything should show `✓` except cloud credentials (which are optional).

### Choosing a local embedder (`nx init`)

`nx init` is the guided first-run setup for local mode. It is distinct from `nx config init` (the cloud-credentials wizard).

```bash
nx init                       # interactive: prompts for the embedder
nx init --yes                 # accept the recommended bge-768 non-interactively
nx init --embedder minilm-384 # pick a specific embedder, no prompt
```

In **local mode** it presents the two on-device embedders and records your choice in `~/.config/nexus/config.yml` under `local.embed_model`:

- **bge-768** (BAAI/bge-base-en-v1.5, 768-dim): recommended. Materially better local search quality. One-time ~140 MB model download on first use.
- **minilm-384** (all-MiniLM-L6-v2, 384-dim): bundled, instant, lower quality.

When you choose bge-768, `nx init`:

1. Adds the `[local]` extra if it is missing. For a `uv tool` install it runs an extras-preserving reinstall for you; in a dev/editable checkout it prints the manual command (`pip install 'conexus[local]'` or `uv sync --extra local`) rather than touching your tree.
2. Pre-fetches the bge-768 model into a stable cache (`local.fastembed_cache_path`, default `~/.local/share/nexus/fastembed_cache`) so it is not re-downloaded on every reboot. If you are offline it prints an actionable message and retries automatically on your next local search. (When the `[local]` extra had to be installed in this same run, the running process cannot import the freshly-installed package, so the model is fetched on your first local search instead; re-run `nx init` to provision it immediately.)
3. Detects any pre-existing collections indexed with the old 384-dim embedder (which would otherwise silently return nothing under bge-768) and offers a safe migration, see [Migrating collections after an embedder change](#migrating-collections-after-an-embedder-change).

In **cloud mode** `nx init` is a no-op: embeddings run server-side via Voyage, so there is no local model to provision. It points you at `nx config init` for credentials.

If you skip `nx init`, local mode keeps working on the default 384-dim embedder; `nx doctor` will remind you that bge-768 is available, and will also flag the degraded case where you chose bge-768 but the `[local]` extra is missing.

### Migrating collections after an embedder change

Changing the active embedder (for example default 384 → bge-768 via `nx init`) does **not** silently re-index existing collections. New content is embedded into new collection names; the old 384-dim collections remain and `nx search` returns nothing for them. `nx init` detects this and offers a safe, ordered migration:

1. **Preview** of exactly what would change (the stale collections are listed before anything runs).
2. **Double confirmation** before any destructive step.
3. **Reindex first** into the new 768-dim collections.
4. **Delete the old collections only after** the reindex is verified populated.

If a reindex fails partway, the old collection is left fully intact; there is no delete-before-reindex path. Two cases are reported but never auto-deleted (there is no source to re-embed from, so deleting would lose data):

- **`code__` collections**: reindex these yourself with `nx index repo <path>`.
- **Manual entries** (notes added via `nx store put` / the MCP `store_put` tool, with no source file). A collection that mixes indexed files with manual notes is migrated only after an explicit confirmation that names the note loss, and never under `--yes`.

### PDF indexing and MinerU models

The first time you run `nx index pdf <math-paper.pdf>` MinerU downloads its formula and table models (~2-3 GB). On a typical connection this takes 3–8 minutes; the command appears to hang during the download. This happens once per install. Subsequent indexing is fast.

If you don't intend to index math PDFs and want to skip the download, run with `--extractor docling` to use the formula-stripped path:

```bash
nx index pdf --extractor docling some.pdf
```

## First-time setup: the storage backend

In the default configuration every persistent tier — `nx memory`,
`nx index`, `nx store`, `nx catalog`, the MCP server, and everything the
Claude Code plugin does — is served by the native **nexus-service** over
Postgres + pgvector. A single `nx init` provisions and starts it (see the next
section), so there is **no** separate T2-daemon install step in the default
flow.

> **Opt-in: the standalone SQLite T2 daemon.** Users who deliberately select a
> SQLite T2 backend (`NX_STORAGE_BACKEND=sqlite`, or a per-store override) can
> register the legacy single-writer SQLite daemon to start at login:
>
> ```bash
> nx daemon t2 install --autostart
> nx daemon t2 status                # confirm running
> ```
>
> This writes a LaunchAgent (macOS, `~/Library/LaunchAgents/com.nexus.t2.plist`)
> or systemd user-unit (Linux, `~/.config/systemd/user/nexus-t2.service`) with
> `KeepAlive=true` / `Restart=on-failure`. Uninstall with
> `nx daemon t2 uninstall --autostart`. This path is not needed for the default
> service-backed install.

**T3 (the permanent vector store)** is served by the native nexus-service over
Postgres 17 + pgvector — in **both** local and cloud mode. Provision and start
it with:

```bash
nx daemon service install-binary <engine-service-vX.Y.Z>   # acquire the cosign-verified native binary + relocatable PG+pgvector bundle
nx init                                                    # provision Postgres, fetch the bge-768 ONNX, start the service, offer autostart
```

Pick `<engine-service-vX.Y.Z>` from the
[engine-service releases](https://github.com/Hellblazer/nexus/releases) (the
newest `engine-service-v*` tag). There is no `latest` resolution — the tag is
explicit. You can instead export `NEXUS_SERVICE_TAG=engine-service-vX.Y.Z`, in
which case `nx init` acquires the binary itself and the explicit
`install-binary` step is optional.

`nx init` provisions the local service backend by default (the older
`nx init --service` flag still works but is deprecated). It offers to register
the OS autostart unit so the service restarts at login/boot — the prompt
defaults to yes; `--yes` accepts non-interactively, and `--no-autostart` starts
a session supervisor only (no persistent unit). `nx init` is idempotent — safe
to re-run. The service embeds with bge-768 (local) or, in the managed-cloud
deployment, server-side via Voyage with the operator's key (clients supply only
`NX_SERVICE_TOKEN`, never a Voyage key).

> The legacy `nx daemon t3` (a managed ChromaDB subprocess) is retired as a
> serving path — T3 no longer serves from ChromaDB. ChromaDB data from a
> pre-6.0 install is read only as the migration *source* (see Upgrading below).

See [docs/container-integration.md](https://github.com/Hellblazer/nexus/blob/main/docs/container-integration.md) for reaching the
service from a container, and [docs/migration-runbook.md](https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md) for the
migration details.

### Using the hosted managed service (no local stack)

Prefer not to run the service stack yourself? Point `nx` at the hosted Conexus
managed service with an operator-provisioned URL + token — no local service, no
migration. See [docs/managed-onboarding.md](https://github.com/Hellblazer/nexus/blob/main/docs/managed-onboarding.md)
for the greenfield journey (`nx config set service_url/service_token` → probe →
first store/search).

## Update

```bash
uv tool upgrade conexus
```

`uv tool upgrade` preserves the spec you installed with (so a `[local]` install stays `[local]`). Do **not** upgrade by re-running `uv tool install conexus` / `--force` — that resets the environment and drops `[local]`, silently downgrading the embedder 768→384-dim (see [Updating](#updating) above).

After upgrading conexus, restart the daemon so it picks up the new
binary:

```bash
nx daemon t2 stop && nx daemon t2 start    # or: launchctl kickstart -k gui/<uid>/com.nexus.t2
```

The schema-version handshake (RDR-120 P3b) fails loud on
client/daemon version mismatch, so stale daemons fail closed rather
than silently corrupting state.

### Upgrading to 6.0 (migrating off ChromaDB)

6.0 moves the permanent vector store (T3) from ChromaDB to the Postgres +
pgvector service. Existing installs migrate with one command:

```bash
uv tool upgrade conexus       # get the 6.0 CLI
nx guided-upgrade             # detect -> provision+verify the service -> migrate -> validate -> unlock
```

`nx guided-upgrade` detects your existing ChromaDB footprint, provisions and
starts the service, version-pins it (its `/version` must report a
`release_version` — present from engine-service v0.1.6+; the code floor is
v0.1.8 but earlier binaries are below it / omit the field and fail closed),
health-gates it, then migrates your collections into pgvector with validation
and **copy-not-move** rollback safety — your ChromaDB store is left intact as
the source. Voyage-capability and version pre-flights fail loud *before* any
migration. It is idempotent (safe to re-run) but not a no-op after success: a
re-run re-copies at full cost. On a validation block it leaves the migration
state `migrated-failed` and offers a rollback command rather than auto-reverting.

## Use it (no API keys needed)

Everything below works immediately — no accounts, no network.

### Index and search a repo

```bash
cd your-project
nx index repo .              # index with local ONNX embeddings
nx search "retry logic"      # semantic search, results grouped by topic
nx taxonomy status           # see auto-discovered topics and coverage
nx taxonomy review           # curate topic labels interactively (optional)
```

After indexing, Nexus automatically discovers topics across your codebase and groups search results by them. If the `claude` CLI is available, topics are also auto-labeled with human-readable names. Run `nx taxonomy status` to see what was discovered.

### Scratch — ephemeral inter-agent context (T1)

```bash
nx scratch put "working hypothesis: the cache TTL is too short"
nx scratch list
nx scratch search "cache"
```

### Memory — persistent project notes (T2)

```bash
nx memory put "auth uses JWT with 24h expiry" -p myproject -t auth-notes
nx memory search "JWT" -p myproject
nx memory get -p myproject -t auth-notes
```

### Catalog — document registry and link graph (optional)

```bash
nx catalog setup               # one command: init + populate + generate links
nx catalog search "auth"       # find documents by metadata
nx catalog show "auth module"  # full entry with all links
nx catalog links "paper X"     # explore the citation/implementation graph
```

The catalog tracks every indexed document and the relationships between them. It's populated automatically when you index repos and PDFs. Run `setup` once to backfill from your existing collections and seed plan templates.

The enhanced `query` MCP tool uses catalog metadata for scoped search — `query(question="...", author="Fagin")` searches only that author's collections in a single call.

If you use managed-cloud mode (a hosted nexus service), add a git remote so the local catalog survives disk loss:

```bash
cd ~/.config/nexus/catalog && git remote add origin git@github.com:you/nexus-catalog.git
nx catalog sync
```

On a new machine, restore with: `nx catalog setup --remote git@github.com:you/nexus-catalog.git`

See [Document Catalog](catalog.md) for details.

## Claude Code plugin (optional)

The conexus plugin gives Claude Code agents access to all three storage tiers, 13 specialized agents, and 43 skills covering the RDR lifecycle, plan-centric retrieval, and development workflows.

**Plugin-only prerequisite: [Node.js](https://nodejs.org/).** The plugin's `sequential-thinking` and `context7` MCP servers are spawned via `npx -y …` and silently fail to start without `node`/`npm` on PATH. Install with `brew install node` (macOS) or your platform's installer before running the plugin commands below.

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

After installing, run `/conexus:nx-preflight` to verify all plugin dependencies are present.

See [plugin documentation](https://github.com/Hellblazer/nexus/blob/main/conexus/README.md) for the full agent/skill reference. For local development from a repo checkout:

```bash
claude --plugin-dir ./nx
```

## Cloud mode (optional)

Local mode embeds with the on-device bge-768 ONNX model (768-dim) the service provisions; the bundled minilm-384 remains a zero-download fallback. The managed-cloud deployment embeds server-side with Voyage AI (1024d), cross-chunk context (CCE), and reranking.

In managed-cloud mode there is no local service and no local Postgres: `nx` talks HTTPS to a hosted nexus service that owns its cloud Postgres + pgvector and embeds with Voyage AI server-side. You do not create a ChromaDB Cloud account or supply a Voyage key yourself (the service owns it).

### 1. Point nx at the managed service

Set the service endpoint and your bearer token in the environment:

```bash
export NX_SERVICE_URL=https://api.conexus-nexus.com   # or your provider's URL
export NX_SERVICE_TOKEN=<your-managed-service-token>
```

`NX_SERVICE_URL` defaults to `https://api.conexus-nexus.com`, so a hosted user on the default deployment only needs `NX_SERVICE_TOKEN`. (These are read from the environment; persist them in your shell profile or your process manager.)

### 2. Verify

```bash
nx doctor
```

All items should show `✓`. Fix anything marked `✗` before proceeding.

### 3. Index and search

```bash
nx index repo .
nx search "how does authentication work"
nx search "retry logic" --corpus code
nx search "API changelog" --corpus docs
nx search "database pool" --hybrid       # semantic + keyword matching
```

Topics are discovered and labeled automatically after indexing. Search results are grouped and boosted by topic. Check `nx taxonomy status` to see the topic map for each collection.

Common flags: `-n 20` (result count), `--json`, `--files` (paths only), `-c` (show matched text). `--hybrid` requires [ripgrep](https://github.com/BurntSushi/ripgrep).

### Upgrade local embedding quality (optional)

For the Python-side bge-768 embedder (used by non-service local indexing paths;
the `nx init` service stack already embeds with bge-768 server-side):

```bash
uv tool install --reinstall "conexus[local]"
```

To force local mode even when cloud credentials exist: `NX_LOCAL=1`.

### Taxonomy config (optional)

Auto-labeling is on by default (`taxonomy.auto_label: true` in `.nexus.yml`). To turn it off, or to exclude specific collections (e.g., code collections when running locally):

```yaml
# .nexus.yml
taxonomy:
  auto_label: false                          # disable AI label generation
  local_exclude_collections:                 # skip these in local mode
    - code__myrepo
```

## Troubleshooting

**`nx` command not found** — Make sure `~/.local/bin` is on your PATH. Run `uv tool install conexus` again and check the output for the install location.

**Crash on startup (Python 3.14)** — Nexus requires Python 3.12–3.13. Check your nx install's Python with: `head -1 $(which nx)`. If it shows `python3.14`, the tool was installed under the wrong Python. Fix:

```bash
uv python install 3.13
uv tool install conexus --force --python 3.13   # use "conexus[local]" here if you rely on the bge-768 embedder
```

Note: `uv tool upgrade` reuses the existing environment's Python — it won't switch from 3.14 to 3.13 automatically. You must use `--force --python 3.13` to rebuild the environment. Because `--force` rebuilds from scratch it drops optional extras, so re-include `[local]` (i.e. install `"conexus[local]"`) if you use the bge-768 embedder.

**`nx doctor` reports credentials not set** — Expected for local mode. Only needed for managed-cloud mode — export `NX_SERVICE_URL` + `NX_SERVICE_TOKEN` in the environment.

**`nx index repo .` fails with a service-auth error** — In managed-cloud mode, indexing requires a reachable service and a valid `NX_SERVICE_TOKEN`. Export the token (`export NX_SERVICE_TOKEN=…`) and confirm the endpoint with `nx doctor`, or use local mode (run `nx daemon service start`, no token needed).

**`import voyageai` or Pydantic v1 error** — The tool is running under Python 3.14. Fix: `uv tool install conexus --force --python 3.13` (install 3.13 first with `uv python install 3.13` if needed; re-include `[local]` — `"conexus[local]"` — if you use the bge-768 embedder, since `--force` drops extras).

**First index is slow or hits a rate limit** — Large repos may take a few minutes. Add `--monitor` for per-file progress. Re-running is safe — unchanged files are skipped.

**`nx search` returns no results** — Run `nx doctor` to verify connectivity. If indexing was interrupted, re-run `nx index repo .` to resume.

**`T2DaemonNotReachableError: No T2 daemon discovery resolved`** — Only on the opt-in SQLite T2 backend (`NX_STORAGE_BACKEND=sqlite`); the default service backend does not use this daemon. Start it (one of):

```bash
nx daemon t2 ensure-running              # one-shot spawn (idempotent)
nx daemon t2 install --autostart         # durable LaunchAgent / systemd unit
```

If `ensure-running` succeeds but the next command still errors, check
the daemon's log:

- macOS: `~/Library/Logs/nexus-t2.err`
- Linux: `journalctl --user -u nexus-t2.service`

**`T2SchemaVersionMismatchError`** — The client's conexus version differs from the running daemon's. Restart the daemon so it picks up the new binary:

```bash
nx daemon t2 stop && nx daemon t2 start
# or under launchd:
launchctl kickstart -k gui/$(id -u)/com.nexus.t2
```

**Daemon is up but the CLI says "discovery file not found"** — Race between `launchctl bootout` / `systemctl stop` and the next CLI call. The daemon's spawn-lock release lags briefly behind process termination. Wait 2–3 seconds and retry, or use `nx daemon t2 ensure-running --timeout=10`.

**Upgrading from an earlier version — topics missing from search** — Topic discovery runs automatically on new indexes. To populate topics for collections indexed before this feature was added, run:

```bash
nx taxonomy discover --all
```

## Next steps

- [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) — every command, every flag
- [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md) — T1, T2, T3 architecture
- [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) — file classification, chunking, frecency
- [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) — config keys, environment variables, tuning
- [Taxonomy](https://github.com/Hellblazer/nexus/blob/main/docs/catalog.md#topic-taxonomy) — topic discovery, auto-labeling, and search clustering
- [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr.md) — decision tracking with Research-Design-Review
