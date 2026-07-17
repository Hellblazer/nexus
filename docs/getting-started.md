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

See the [Quick Start in README.md](https://github.com/Hellblazer/nexus/blob/main/README.md#cli-quick-start) for the full install walkthrough: `uv tool install conexus`, `nx init` (embedder choice, **nexus-service** provisioning — the native Postgres + pgvector backend that serves every persistent tier), updating, and verifying with `nx doctor`.

Once you have a working install, come back here for repo indexing, the storage-tier CLIs, and troubleshooting below. If you're upgrading an *existing* pre-6.0 install rather than installing fresh, skip to [Upgrading an existing install](#upgrading-an-existing-install-skip-this-if-this-is-your-first-install) at the end of this document.

## Use it (no API keys needed)

Everything below works immediately — no accounts, no network.

### Index and search a repo — permanent semantic store (T3)

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

The conexus plugin gives Claude Code agents access to all three storage tiers, 13 specialized agents, and 45 skills covering the RDR lifecycle, plan-centric retrieval, and development workflows.

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
nx search "retry logic" --corpus code   # corpus = which collection group to search (code, docs, knowledge, ...)
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

## Upgrading an existing install (skip this if this is your first install)

Upgrading nexus is **two steps — both required on every upgrade**: update the
code, then converge the data.

```bash
uv tool upgrade conexus       # 1. update the code (preserves your extras, e.g. [local])
nx upgrade                    # 2. converge the data — walks the upgrade ladder
```

`nx upgrade` is the single trigger that converges everything else — it brings the
package, engine, and process preconditions current (provisioning and starting the
service stack if a legacy footprint needs one to migrate into), then walks one
ordered ladder that auto-applies whichever data migrations your install actually
needs: the T2 schema, the ChromaDB → Postgres+pgvector substrate move that 6.0
introduced, pre-RDR-108 chunk identity, and embedder era. Each rung detects,
converges, and verifies before recording completion; the walk is resumable and
idempotent, and your ChromaDB store is left **byte-untouched** as the rollback
source (copy-not-move). There is nothing to sequence by hand and no era to know —
an install dormant since 5.x converges the same way a current one no-ops. Use
`nx doctor` to see what is pending, and `nx upgrade --dry-run` to preview without
changing anything.

You are asked to decide only what the product cannot derive: **billed
re-embedding** (an estimate-and-confirm prompt before anything charges — silent
when nothing bills; pass `nx upgrade --yes` or set `NX_ASSUME_YES=1` to
pre-approve it unattended), a **source collection that has vanished** (re-acquire
or drop; the walk defers rather than guessing), and **rollback**, which is always
yours to invoke and never automatic. On a validation block the migration state is
left `migrated-failed`, reads stay loudly degraded rather than silently empty, and
the rollback command is printed as the remedy.

**Always upgrade with `uv tool upgrade conexus` for step 1.** It preserves the
spec you installed with, so a `[local]` install stays `[local]`. **Do not** re-run
`uv tool install conexus` (or `--force`) just to upgrade: that resets the
environment and **drops `[local]`**, silently downgrading the embedder 768→384-dim,
which dimension-mismatches existing 768-dim collections and makes search return
nothing. To recover: `uv tool install --reinstall "conexus[local]"`.

After step 1, restart the daemon so it picks up the new binary (step 2's
`nx upgrade` then converges the data):

```bash
nx daemon t2 stop && nx daemon t2 start    # or: launchctl kickstart -k gui/<uid>/com.nexus.t2
```

The schema-version handshake (RDR-120 P3b) fails loud on client/daemon version
mismatch, so stale daemons fail closed rather than silently corrupting state.

When you update the Claude Code plugin (`/plugin update`), run **both** upgrade
steps above so the CLI stays in lockstep with the plugin version.

See [docs/migration-runbook.md](https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md) for the full migration details.
