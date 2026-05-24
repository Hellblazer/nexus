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
```

The default install includes MinerU for math-aware PDF extraction (~500 MB of Python deps; first PDF index downloads ~2-3 GB of models — see [PDF indexing notes](#pdf-indexing-and-mineru-models) below).

Verify:

```bash
nx --version
nx doctor
nx doctor --check-mineru   # confirms MinerU is importable (since nexus-2fyb)
```

`nx doctor` checks dependencies, credentials, and connectivity. Everything should show `✓` except cloud credentials (which are optional).

### PDF indexing and MinerU models

The first time you run `nx index pdf <math-paper.pdf>` MinerU downloads its formula and table models (~2-3 GB). On a typical connection this takes 3–8 minutes; the command appears to hang during the download. This happens once per install. Subsequent indexing is fast.

If you don't intend to index math PDFs and want to skip the download, run with `--extractor docling` to use the formula-stripped path:

```bash
nx index pdf --extractor docling some.pdf
```

## First-time setup: the T2 daemon

Since conexus 4.34.0 (RDR-120 storage substrate split), all
user-facing CLI commands that touch persistent state — `nx memory`,
`nx index`, `nx store`, `nx catalog`, the MCP server, and everything
the Claude Code plugin does — route through the **T2 daemon**, a
single arbitrating SQLite-writer process. This is the substrate
fix that lets host CLI usage, multiple Claude Code sessions, Claude
Cowork agents, and dev containers all share state without racing on
the database file.

Register the daemon to start at login (one-time setup):

```bash
nx daemon t2 install --autostart
nx daemon t2 status                # confirm running
```

This writes a LaunchAgent (macOS, `~/Library/LaunchAgents/com.nexus.t2.plist`)
or systemd user-unit (Linux, `~/.config/systemd/user/nexus-t2.service`)
with `KeepAlive=true` / `Restart=on-failure`, so the daemon survives
crashes and reboots. Uninstall with `nx daemon t2 uninstall --autostart`.

If you skip this step, the conexus plugin's SessionStart hook
will auto-spawn the daemon on every session start anyway, so plugin
users still get a working substrate. The autostart path is recommended
if you also use `nx` directly from the shell.

**Local-mode T3 only**: the T3 daemon wraps a local ChromaDB process.
Register it the same way:

```bash
nx daemon t3 install --autostart   # only when NX_LOCAL=1 / no cloud creds
```

Cloud-mode T3 talks directly to ChromaDB Cloud over HTTP and has no
daemon — `nx daemon t3` is a no-op in that configuration.

See [docs/container-integration.md](https://github.com/Hellblazer/nexus/blob/main/docs/container-integration.md) for the full daemon-model story
including TCP / UDS / Cowork transport details.

## Update

```bash
uv tool update conexus
```

After upgrading conexus, restart the daemon so it picks up the new
binary:

```bash
nx daemon t2 stop && nx daemon t2 start    # or: launchctl kickstart -k gui/<uid>/com.nexus.t2
```

The schema-version handshake (RDR-120 P3b) fails loud on
client/daemon version mismatch, so stale daemons fail closed rather
than silently corrupting state.

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

If you use cloud mode (ChromaDB Cloud), add a git remote so the catalog survives disk loss:

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

Local mode uses bundled ONNX embeddings (384d MiniLM). Cloud mode upgrades to Voyage AI (1024d), cross-chunk context (CCE), and reranking.

### 1. Create accounts

| Service | Purpose | Free tier |
|---------|---------|-----------|
| [ChromaDB Cloud](https://trychroma.com) | Vector storage | Generous for individual use |
| [Voyage AI](https://voyageai.com) | Embeddings | 200M tokens/month |

Both free tiers cover typical usage at no cost.

### 2. Configure credentials

Interactive wizard:

```bash
nx config init
```

Or set individually:

```bash
nx config set chroma_api_key sk-...
nx config set chroma_database nexus
nx config set voyage_api_key pa-...
```

### 3. Verify

```bash
nx doctor
```

All items should show `✓`. Fix anything marked `✗` before proceeding.

### 4. Index and search

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

For better local-only embeddings (768d bge-base) without cloud:

```bash
uv tool install conexus --with "conexus[local]" --force
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
uv tool install conexus --force --python 3.13
```

Note: `uv tool update` reuses the existing environment's Python — it won't switch from 3.14 to 3.13 automatically. You must use `--force --python 3.13` to rebuild the environment.

**`nx doctor` reports credentials not set** — Expected for local mode. Only needed if you want cloud embeddings — run `nx config init`.

**Provisioning failed during `nx config init`** — If your ChromaDB plan restricts automatic database creation, create the database manually in the [dashboard](https://trychroma.com) using your `chroma_database` value as the name.

**`nx index repo .` fails with "credentials not set"** — In cloud mode, indexing requires T3 credentials. Run `nx config init` first, or use local mode (no credentials needed).

**`import voyageai` or Pydantic v1 error** — The tool is running under Python 3.14. Fix: `uv tool install conexus --force --python 3.13` (install 3.13 first with `uv python install 3.13` if needed).

**First index is slow or hits a rate limit** — Large repos may take a few minutes. Add `--monitor` for per-file progress. Re-running is safe — unchanged files are skipped.

**`nx search` returns no results** — Run `nx doctor` to verify connectivity. If indexing was interrupted, re-run `nx index repo .` to resume.

**`T2DaemonNotReachableError: No T2 daemon discovery resolved`** — Since 4.34.0 the CLI routes through the T2 daemon. Start it (one of):

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
