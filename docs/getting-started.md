# Getting Started with Nexus

## Prerequisites

- **Python 3.12 or 3.13** (3.14 is not yet supported — [upstream dependency issue](https://github.com/pydantic/pydantic/issues))
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **git**
- **[Node.js](https://nodejs.org/)** — required *only* if you install the Claude Code plugin(s). The conexus plugin bundles the `sequential-thinking` MCP server, spawned via `npx -y …`; the companion `sn` plugin bundles `context7` the same way. Either requires `node` and `npm` on PATH. The `nx` CLI alone does not need it. Install with `brew install node` (macOS) or follow the [Node.js installer](https://nodejs.org/) for your platform.

Check your Python version:

```bash
python3 --version
```

If you're on 3.14+, install 3.13 with `uv python install 3.13` — uv will use it automatically.

## Install

See the [CLI quick-start in README.md](https://github.com/Hellblazer/nexus/blob/main/README.md#cli-quick-start) for the full install walkthrough: `uv tool install conexus`, `nx init` (**nexus-service** provisioning — the native Postgres + pgvector + bge-768 backend that serves every persistent tier), updating, and verifying with `nx doctor`.

Once you have a working install, come back here for repo indexing, the storage-tier CLIs, and troubleshooting below. If you're upgrading an *existing* pre-6.0 install rather than installing fresh, skip to [Upgrading an existing install](#upgrading-an-existing-install-skip-this-if-this-is-your-first-install) at the end of this document — pre-PG installs need a **two-hop** upgrade via `conexus==6.18.1` (the last migration-capable release); a direct jump to current migrates nothing.

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
nx catalog search "auth"       # find documents by metadata
nx catalog show "auth module"  # full entry with all links
nx catalog links "paper X"     # explore the citation/implementation graph
```

The catalog tracks every indexed document and the relationships between them. It's populated automatically when you index repos and PDFs — there is no separate init/setup step (`nx catalog setup` / `init` are retired guided refusals; the nexus service owns the catalog).

The enhanced `query` MCP tool uses catalog metadata for scoped search — `query(question="...", author="Fagin")` searches only that author's collections in a single call.

The catalog lives in the nexus service's Postgres — the same database that holds T2 and (for local-service users) T3 — so it's as durable as that service's storage, with no separate local git/JSONL layer to configure (`nx catalog sync` / `pull` are retired).

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
claude --plugin-dir ./conexus
```

(For the Serena/Context7 companion plugin, add `--plugin-dir ./sn` as well.)

## Cloud mode (optional)

Local mode embeds with the on-device bge-768 ONNX model (768-dim) the service provisions; the bundled minilm-384 remains a zero-download fallback. The managed-cloud deployment embeds server-side with Voyage AI (1024d), cross-chunk context (CCE), and reranking.

In managed-cloud mode there is no local service and no local Postgres: `nx` talks HTTPS to a hosted nexus service that owns its cloud Postgres + pgvector and embeds with Voyage AI server-side. You do not create a ChromaDB Cloud account or supply a Voyage key yourself (the service owns it).

### 1. Point nx at the managed service

Set the service endpoint and your bearer token in the environment:

```bash
export NX_SERVICE_URL=https://api.conexus-nexus.com   # or your provider's URL
export NX_SERVICE_TOKEN=<your-managed-service-token>
```

Both are required — mode detection never consults the token by itself, so a box that exports only `NX_SERVICE_TOKEN` resolves to local mode and the token is silently ignored. `NX_SERVICE_URL` is what switches `nx` into managed mode; the URL is deliberately never defaulted for mode resolution, even on the default deployment. (These are read from the environment; persist them in your shell profile or your process manager.)

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
the `nx init` service stack already embeds with bge-768 server-side). Ask for the
extra when you install the CLI:

```bash
uv tool install "conexus[local]"
```

The extra then travels with the install and you do not ask for it again: on the
generation layout it is recorded in the generation's receipt and `nx self install`
carries it into every later generation; on the older uv-tool layout `uv tool
upgrade conexus` retains the spec you installed with. What loses it is re-running
a bare `uv tool install conexus` — see the warning under
[Upgrading an existing install](#upgrading-an-existing-install-skip-this-if-this-is-your-first-install).

Adding the extra to an install that did not ask for it is a uv-tool-layout
operation — `uv tool install --reinstall "conexus[local]"`. On a generation box
that command rebuilds the uv tree and takes the shims back, so run it only if
`nx doctor` reports no generation install.

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

**`nx` command not found** — Make sure `~/.local/bin` is on your PATH; that is
where both layouts put the `nx` entry point (`NX_BIN_DIR` overrides it on the
generation layout). If it is on PATH and `nx` still does not resolve, `ls -l
~/.local/bin/nx` says which layout you are on: a small shell script is a
nexus-owned generation shim, a symlink into `~/.local/share/uv/tools/` is the
uv-tool layout. Reinstall accordingly — `nx self install` from any working `nx`,
`scripts/reinstall-tool.sh` from a checkout, or `uv tool install conexus` if
nothing is installed yet.

**`nx` resolves but nothing starts** — On the generation layout every command
resolves `~/.local/share/nexus/tools/current` at spawn, so a missing or dangling
`current` breaks all of them at once (the shims exit 70 to say so). `nx doctor`'s
*Generation layout* row names the fault; `scripts/reinstall-tool.sh` from a
checkout rebuilds a generation and repoints `current`.

**Crash on startup (Python 3.14)** — Nexus requires Python 3.12–3.13. This is a
uv-tool-layout symptom: a generation install builds its own virtualenv at a
supported Python every time, so it cannot land on 3.14. Check which interpreter
your install actually uses with `nx doctor`, or `head -1 $(which nx)` on the
uv-tool layout. If it reports 3.14, rebuild the uv environment:

```bash
uv python install 3.13
uv tool install conexus --force --python 3.13   # use "conexus[local]" here if you rely on the bge-768 embedder
```

Note: `uv tool upgrade` reuses the existing environment's Python — it won't
switch from 3.14 to 3.13 automatically, which is why this one case wants
`--force`. Because `--force` rebuilds from scratch it drops optional extras, so
re-include `[local]` (i.e. install `"conexus[local]"`) if you use the bge-768
embedder. Do not reach for this on a generation box: it rebuilds the uv tree and
takes the shims back without fixing anything, since the generation's own
interpreter is already supported.

**`nx doctor` reports credentials not set** — Expected for local mode. Only needed for managed-cloud mode — export `NX_SERVICE_URL` + `NX_SERVICE_TOKEN` in the environment.

**`nx index repo .` fails with a service-auth error** — In managed-cloud mode, indexing requires a reachable service and a valid `NX_SERVICE_TOKEN`. Export the token (`export NX_SERVICE_TOKEN=…`) and confirm the endpoint with `nx doctor`, or use local mode (run `nx daemon service start`, no token needed).

**`import voyageai` or Pydantic v1 error** — The tool is running under Python 3.14, so this is the same uv-tool-layout case as *Crash on startup* above and takes the same fix: `uv tool install conexus --force --python 3.13` (install 3.13 first with `uv python install 3.13` if needed; re-include `[local]` — `"conexus[local]"` — if you use the bge-768 embedder, since `--force` drops extras).

**First index is slow or hits a rate limit** — Large repos may take a few minutes. Add `--monitor` for per-file progress. Re-running is safe — unchanged files are skipped.

**`nx search` returns no results** — Run `nx doctor` to verify connectivity. If indexing was interrupted, re-run `nx index repo .` to resume.

**`T2DaemonNotReachableError` / `T2SchemaVersionMismatchError`** — These no
longer occur: the SQLite T2 daemon they came from is retired, along with the
`nx daemon t2` verb group. T2 is served by the nexus-service. If a storage
error mentions the service instead, `nx doctor` is the diagnostic.

**A `com.nexus.t2` LaunchAgent / `nexus-t2.service` unit keeps failing at
boot** — It is a leftover from a pre-retirement install trying to run
`nx daemon t2 start`, which no longer exists. `nx upgrade` removes it on the
next run; to check by hand, `launchctl list | grep com.nexus.t2` (macOS) or
`systemctl --user list-unit-files | grep nexus-t2` (Linux).

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
nx self install               # 1. update the code (preserves your extras, e.g. [local])
nx upgrade                    # 2. converge the data — walks the upgrade ladder
```

Step 1 does not replace the install you are running. `nx self install` builds a
new generation under `~/.local/share/nexus/tools/gen-<stamp>`, repoints the
`current` symlink and rewrites the `~/.local/bin` shims — so it succeeds with
Claude Code sessions open, the storage service up, and an `nx index` in flight.
Live processes keep executing from the tree they resolved at spawn and converge
at their next spawn; older generations are reaped once nothing is bound to them.
The install source and any extras travel in the generation's receipt, which is
what makes a `[local]` install stay `[local]`.

If `nx self install` reports that this nx is not running from a generation, the
box is still on the older uv-tool layout and step 1 is `uv tool upgrade conexus`
instead. `nx doctor`'s *Generation layout* row is the discriminator; the two
steps are otherwise identical.

`nx upgrade` is the single trigger that converges everything else — it brings the
package, engine, and process preconditions current, then walks the upgrade
ladder's remaining data rung (chunk-identity rekey) plus schema convergence via
the engine's Liquibase changesets. The walk is resumable and idempotent; use
`nx doctor` to see what is pending, and `nx upgrade --dry-run` to preview without
changing anything.

**Upgrading from a pre-PG install (5.x, or 6.x that never migrated off
ChromaDB) is a two-hop path** — the Chroma-era migration machinery was retired
after its two-release deprecation window (RDR-155/158), and releases past
6.18.1 no longer carry it. Current releases detect a stranded pre-PG footprint
at startup and print this exact path, but do NOT block on it, so know it up
front:

```bash
nx self install --version 6.18.1  # 1. hop to the last migration-capable release
nx upgrade                        # 2. migrate there: ChromaDB → Postgres+pgvector
                                  #    (copy-not-move; Chroma left byte-untouched
                                  #    — a relic afterwards, nothing reads it)
nx self install                   # 3. hop forward to current and converge the rest
nx upgrade
```

Hop 1 is a deliberate version pin — an older client, on purpose, to run the
migration it still carries — and under the generation layout that is safe by
construction: it builds 6.18.1 as a new generation and flips to it, leaving the
newer tree on disk for hop 3 rather than overwriting anything. Keep the
`--version` pin — a bare `nx self install` installs the newest release, which is
the hop this procedure exists to avoid.

A box on the older uv-tool layout runs the same sequence in uv vocabulary —
`uv tool install conexus==6.18.1` for hop 1 and `uv tool upgrade conexus` for
hop 3, with `nx upgrade` unchanged in between and after. The startup banner picks
hop 1's form to match the layout it finds, so the command it prints is the one to
run; it describes hop 3 only as "upgrade back to this version", which is step 1
of this section.

Running plain `nx upgrade` on a current release over a pre-PG store migrates
nothing — searches then look empty because reads target the (empty) PG
substrate, not your untouched Chroma data. If that happens, nothing is lost:
follow the two-hop above.

You are asked to decide only what the product cannot derive: **billed
re-embedding** (an estimate-and-confirm prompt before anything charges — silent
when nothing bills; pass `nx upgrade --yes` or set `NX_ASSUME_YES=1` to
pre-approve it unattended), a **source collection that has vanished** (re-acquire
or drop; the walk defers rather than guessing), and **rollback**, which is always
yours to invoke and never automatic. On a validation block the migration state is
left `migrated-failed`, reads stay loudly degraded rather than silently empty, and
the rollback command is printed as the remedy.

**Use step 1's command for your layout and nothing else.** Both `nx self install`
and `uv tool upgrade conexus` preserve the spec you installed with, so a `[local]`
install stays `[local]`. **Do not** re-run `uv tool install conexus` (or
`--force`) just to upgrade: that resets the environment and **drops `[local]`**,
silently downgrading the embedder 768→384-dim, which dimension-mismatches existing
768-dim collections and makes search return nothing. On a uv-tool box, recover
with `uv tool install --reinstall "conexus[local]"`. On a generation box that
command does a second kind of damage — it rebuilds the uv tree and re-symlinks
over the nexus-owned shims, so `nx` resolves through uv instead of through
`current`. `nx doctor` reports the reclaimed shims and `nx self install` rewrites
them.

Step 1 leaves running processes on their old code by design, so after it, restart
the storage service to move it onto the new generation (step 2's `nx upgrade`
then converges the data):

```bash
nx daemon service stop && nx daemon service start
```

`nx upgrade` also cycles a stale supervisor for you, so this is the manual form.
Claude Code sessions and other holders need no intervention — they converge when
they next spawn, and `nx doctor` lists any that are still bound to an older
generation. (The T2 daemon that used to need its own restart here is retired.)

When you update the Claude Code plugin (`/plugin update`), run **both** upgrade
steps above so the CLI stays in lockstep with the plugin version.

See [docs/migration-runbook.md](https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md) for the full migration details.
