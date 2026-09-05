# Nexus

**Persistent memory and semantic search for Claude.** Three storage tiers that survive across sessions, an event-sourced document catalog with typed links, and a specification-before-code workflow for tracking decisions. Local-first; no API keys required. Knowledge compounds across conversations instead of evaporating when the window closes.

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

<a href="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=1024&ssl=1">
  <img src="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=480&ssl=1" alt="A brass-ribbed crystal dome on a hilltop at dusk" align="right" width="320" />
</a>

**Start here**: [**How I actually use Nexus**](https://tensegrity.blog/2026/04/26/how-i-actually-use-nexus/) — the conceptual overview and the shape of the substrate. Then [**Installing Nexus**](https://tensegrity.blog/2026/04/26/installing-nexus/) — a ten-minute hands-on walkthrough from `uv tool install` through your first search.

## Prerequisites

Python 3.12–3.13 (3.14 not yet supported — [upstream dependency issue](https://github.com/pydantic/pydantic/issues)), [`uv`](https://docs.astral.sh/uv/), `git`. For hybrid search, [`ripgrep`](https://github.com/BurntSushi/ripgrep). For the Claude Code plugin, [Node.js](https://nodejs.org/) (the bundled `sequential-thinking` and `context7` servers spawn via `npx`).

## Install for Claude

Three surfaces share one host substrate: the `nx` CLI (the `conexus` package). Claude Desktop's `.mcpb` bundles it and resolves it on first launch; the Claude Code plugin and Cowork use a **separately-installed** CLI (`uv tool install conexus`). Pick the one that matches how you use Claude.

### Claude Desktop chat

Download `conexus.mcpb` from the [latest release](https://github.com/Hellblazer/nexus/releases/latest) and double-click. Claude Desktop registers it under Settings → Connectors. Requires [uv](https://docs.astral.sh/uv/) installed on the host (the standard installer or Homebrew puts it where Claude Desktop resolves it — no PATH setup needed); deps resolve on first launch (~20s).

### Claude Code (terminal)

```bash
uv tool install conexus                  # 1. the nx CLI (the plugin's MCP servers ARE this package)
nx self install                          #    then move it onto the generation layout (drops the stray `av` wheel; on Linux, CPU-only torch instead of the CUDA build)
/plugin marketplace add Hellblazer/nexus # 2. add the marketplace
/plugin install conexus@nexus-plugins    # 3. install the plugin
```

The plugin's MCP servers (`nx-mcp`, `nx-mcp-catalog`) are console-scripts from the `conexus` package, so **the `nx` CLI must be installed too**: `/plugin install` alone leaves the servers unable to launch. Install the CLI first (step 1; see [CLI quick-start](#cli-quick-start) to then provision the storage backend).

The plugin ships 13 specialized agents, 43 skills (RDR lifecycle, plan-centric retrieval, dev workflows), and 48 MCP tools split across two focused servers. Session hooks load project context at startup.

### Claude Cowork

Works automatically once the conexus plugin is installed in Claude Code on the host. State round-trips bidirectionally with the host CLI through the storage service.

For the full deployment story across all three surfaces (install, service lifecycle, drift detection, uninstall), see [docs/desktop-deployment.md](docs/desktop-deployment.md).

## What it does

- **Persistent memory** — three storage tiers (T1 session scratch, T2 memory bank, T3 semantic knowledge store, both persistent tiers served by the native Postgres-backed `nexus-service`) so Claude remembers across conversations.
- **Semantic search** — index your code, docs, RDRs, and PDFs once; search by meaning afterward. Tree-sitter AST chunking across 31 languages, CCE prose chunking, PDF auto-routing.
- **Typed document catalog** — Xanadu-inspired addressing with typed links (`cites`, `implements`, `supersedes`). Walk from a design doc to the code that implements it.
- **RDR: Research-Design-Review** — write a spec before you code. Captures the problem, research, alternatives, and chosen approach. The corpus is searchable, so prior decisions surface during new design work.
- **Local-first** — runs entirely on your machine: an on-device bge-768 ONNX embedder over a bundled Postgres 17 + pgvector service that `nx init` provisions for you. Voyage AI (server-side embeddings) is opt-in for the managed-cloud deployment.

## CLI quick-start

```bash
uv tool install conexus        # install the nx CLI
nx self install                # move it onto the generation layout: uv's own tree carries `av` (PyAV) whose bundled ffmpeg collides with opencv's, and on Linux the CUDA torch build (~4.5 GB); the generation build excludes av and installs CPU-only torch (NX_TORCH_BACKEND=auto opts a GPU box back in)
nx init                        # acquires the signed engine + Postgres bundle, provisions pgvector + bge-768, starts the service, offers autostart
nx doctor                      # verify the stack
nx index repo .                # index your repo + discover topics
nx search "how does retry work"   # semantic search, fully local
```

You never choose an engine version: every conexus release is built pinned to the exact `engine-service` release it was tested against, and `nx init` acquires that signed binary + Postgres bundle automatically (cosign-verified). You do **not** need PostgreSQL installed — nexus always provisions from its own self-contained Postgres bundle (pgvector already compiled in) and never touches a PostgreSQL you may already have. Advanced: export `NEXUS_SERVICE_TAG=engine-service-vX.Y.Z` to override the pin (air-gapped installs, engine testing).

`nx init` provisions the bundled Postgres 17 + pgvector cluster, fetches the bge-768 ONNX model the service embeds with, starts the persistent service, and offers to register the OS autostart unit so it restarts at login/boot (prompt defaults to yes; `--yes` accepts non-interactively, `--no-autostart` starts a session supervisor only). There is **no** separate `nx daemon t2 install` step — T2 (notes/plans) is served by the same service in the default config. The permanent vector store (T3) serves through this native service; the bundled binary + Postgres are cosign-verified and acquired automatically. `nx init` is idempotent — safe to re-run. (The older `nx init --service` flag still works but is deprecated — plain `nx init` is the path now.) **First run only:** this downloads roughly 600 MB (the signed ~134 MB service binary, the relocatable Postgres bundle, and the ~416 MB bge-768 ONNX model) and takes a few minutes; subsequent starts are fast.

The `nx` CLI provides direct access to all storage tiers, indexing, search, the catalog, and taxonomy. See [Getting Started](docs/getting-started.md) for a walkthrough, [CLI Reference](docs/cli-reference.md) for every command and flag.

## Updating

```bash
nx self install                          # 1. update the code — PRESERVES your extras (e.g. [local])
nx upgrade                               # 2. converge the data
```

Upgrading nexus is: update the code, then run `nx upgrade`. That single trigger converges everything else — it brings the package, engine, and process preconditions current, then walks one ordered ladder. The T2-schema and ChromaDB→Postgres+pgvector substrate-move rungs (and the chunk-identity / embedder-era migrations that were co-resident inside the substrate move) retired with the Chroma + client-SQLite migration machinery at RDR-155 P4b; the RDR-180 chash rekey is the ladder's sole remaining data rung today, detecting, converging, and verifying before it records completion, resumable and idempotent, with your existing store left byte-untouched as a rollback target. There is nothing to sequence by hand and no era to know for any install that has already reached the PG substrate (6.0+): `nx doctor` reports the pending rung read-only, `nx upgrade` walks it, and a dormant-but-migrated install converges the same way a current one no-ops. A **pre-PG install** (5.x, or 6.x that never migrated off ChromaDB) is a separate two-hop — the Chroma-era migration machinery retired at RDR-155 P4b, so hop through `conexus==6.18.1` first (`nx upgrade` there migrates ChromaDB → Postgres+pgvector, copy-not-move), then upgrade forward to current; see [Getting Started § Upgrading an existing install](docs/getting-started.md#upgrading-an-existing-install-skip-this-if-this-is-your-first-install) for the exact commands. Rollback is always yours to invoke and never automatic.

**Step 1 installs a new generation; it does not replace the one you are running.** `nx self install` builds a fresh tree beside the existing ones under `~/.local/share/nexus/tools/`, repoints the `current` symlink and rewrites the `~/.local/bin` shims. Nothing is swapped underneath a live process, so it always succeeds with Claude Code sessions open, the storage service up, and an `nx index` in flight — those holders keep running from their own tree and converge at their next spawn. The install source and your extras travel in the generation's own receipt, so a `[local]` install stays a `[local]` install. Older generations are reaped once nothing is bound to them (the last three are kept by default; `--keep N` to change that).

**On a box still using the older uv-tool layout, `nx self install` now CONVERGES it** (7.20.0, nexus-gu9zo). It builds a generation beside the existing uv tree, flips `current`, takes over the `~/.local/bin` shims, and registers the old tree so live holders keep running from it until nothing is bound to it. Your extras bridge across from the uv receipt, so a `[local]` install stays `[local]`. Before 7.20.0 the command refused here and pointed at a repo script most users do not have — no packaged install could reach the generation layout at all. `nx doctor`'s *Generation layout* row tells you which layout you are on. On either layout, **do not** upgrade with `uv tool install conexus --force` / `uv tool install conexus`: that *resets* the install and **drops `[local]`**, silently downgrading your embedder from 768-dim to 384-dim, which dimension-mismatches existing 768-dim collections and makes search return nothing. On a uv-tool box, recover with `uv tool install --reinstall "conexus[local]"`. On a generation box, that same command rebuilds a `[local]`-less uv tree beside your install (a plain `uv tool install` leaves the nexus shims alone — "Executable already exists"; `--force` takes them, and then every spawn resolves through uv's tree instead of `current`). Since 7.21.0 this is self-repairing: the next `nx upgrade` (the SessionStart hook runs it) or `nx self install` rewrites the shims back to `current`, registers uv's tree for reap, and — if uv's tree is the newer version, i.e. you meant to upgrade — builds a generation at that version from your own receipt, so `[local]` survives. Never run `uv tool uninstall conexus` on a generation box: it deletes the nexus shims at those paths; a reaped tree is what makes uv refuse to rebuild.

When you update the **Claude Code plugin** (`/plugin update`), run **both** upgrade steps above (`nx self install` then `nx upgrade`) so the CLI stays in lockstep with the plugin version.

### Something broken?

[nexus-recovery-runbook](https://gist.github.com/Hellblazer/08f0a615e3d73e47d8062bce4829b611) is a
diagnose-first recovery procedure meant to be handed to a Claude Code session as its first message —
the assistant runs it phase by phase, pausing for your explicit go-ahead before anything that upgrades
or migrates data, and gathers redacted forensics + opens a GitHub issue (or emails a fallback address)
if it can't resolve things itself. It's a convenience for a broken install, not a substitute for filing
an issue directly if something looks wrong — and it carries its own guardrails (read-only diagnosis
first, no destructive commands without confirmation, no secrets ever leave the machine), but you're
trusting an LLM to run real commands against your install. Review what it does before handing it off,
especially the first time.

## Going deeper

| If you want to... | Read |
|---|---|
| Understand the architecture | [Storage Tiers](docs/storage-tiers.md), [Architecture](docs/architecture.md) |
| Install, upgrade, or uninstall the agent | [Agent Lifecycle & Operations](docs/operations/agent-lifecycle.md) |
| Use the hosted managed service | [Managed Onboarding](docs/managed-onboarding.md) |
| Write an RDR | [RDR: Research-Design-Review](docs/rdr.md) |
| Index a repo or PDFs | [Repo Indexing](docs/repo-indexing.md) |
| Configure or tune | [Configuration](docs/configuration.md) |
| Run in containers or Cowork | [Container Integration](docs/container-integration.md) |
| Back up my knowledge store | [Storage Tiers § T3 Backup and Migration](docs/storage-tiers.md#t3-backup-and-migration-exportimport) |
| Fix empty search results after upgrading | [Getting Started § Troubleshooting](docs/getting-started.md#troubleshooting) |
| Browse the docs tree | [docs/README.md](docs/README.md) |
| Read the conceptual story | [How I actually use Nexus](https://tensegrity.blog/2026/04/26/how-i-actually-use-nexus/) |
| Walk through a fresh install | [Installing Nexus](https://tensegrity.blog/2026/04/26/installing-nexus/) |
| Browse the full series | [Tensegrity blog](https://tensegrity.blog/) |

## License

Dual-licensed. Open source under AGPL-3.0-or-later
([LICENSE](LICENSE)); commercial
licenses are available for organizations that need non-AGPL terms — see
[LICENSING.md](LICENSING.md).
