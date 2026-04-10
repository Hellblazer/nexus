# Getting Started with Nexus

## Prerequisites

- **Python 3.12 or 3.13** (3.14 is not yet supported — [upstream dependency issue](https://github.com/pydantic/pydantic/issues))
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **git**

Check your Python version:

```bash
python3 --version
```

If you're on 3.14+, install 3.13 with `uv python install 3.13` — uv will use it automatically.

## Install

```bash
uv tool install conexus
```

Verify:

```bash
nx --version
nx doctor
```

`nx doctor` checks dependencies, credentials, and connectivity. Everything should show `✓` except cloud credentials (which are optional).

## Update

```bash
uv tool update conexus
```

## Use it (no API keys needed)

Everything below works immediately — no accounts, no network.

### Index and search a repo

```bash
cd your-project
nx index repo .              # index with local ONNX embeddings
nx search "retry logic"      # semantic search, fully local
```

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

The `nx` plugin gives Claude Code agents access to all three storage tiers, 16 specialized agents, and 33 development workflow skills.

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

After installing, run `/nx:nx-preflight` to verify all plugin dependencies are present.

See [plugin documentation](https://github.com/Hellblazer/nexus/blob/main/nx/README.md) for the full agent/skill reference. For local development from a repo checkout:

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

Common flags: `-n 20` (result count), `--json`, `--files` (paths only), `-c` (show matched text). `--hybrid` requires [ripgrep](https://github.com/BurntSushi/ripgrep).

### Upgrade local embedding quality (optional)

For better local-only embeddings (768d bge-base) without cloud:

```bash
uv tool install conexus --with "conexus[local]" --force
```

To force local mode even when cloud credentials exist: `NX_LOCAL=1`.

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

## Next steps

- [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) — every command, every flag
- [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md) — T1, T2, T3 architecture
- [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) — file classification, chunking, frecency
- [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) — config keys, environment variables, tuning
- [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) — decision tracking with Research-Design-Review
