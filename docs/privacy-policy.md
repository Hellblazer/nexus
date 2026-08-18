# Privacy Policy — Conexus

_Effective: 2026-08-18_

> **Change note (2026-08-18):** corrected three data-locality misstatements
> present since the 2026-06-20 revision — T1 session scratch is Postgres-backed
> service state, not in-memory ChromaDB; the catalog is served by the same
> `nexus-service` Postgres, not a local `~/.config/nexus/catalog/` file store;
> and the legacy ChromaDB directory is a frozen historical artifact on this
> release, not a live migration source `nx upgrade` reads. No change in what
> data Conexus actually collects or where it is willing to send it — this is
> a correction to where already-described data physically lives.

Conexus is a self-hosted MCP server and Claude Code / Claude Desktop extension that indexes content on your machine and provides semantic search and persistent memory across Claude conversations. This policy describes what data Conexus handles, where it goes, and what is never collected.

## 1. What Conexus stores

All persistent data lives on the Postgres server backing your `nexus-service` — the host machine's own disk in local mode, or the operator's server in managed-cloud mode:

- **Indexed content** — text from files you ask Conexus to index (`nx index repo`, `nx index pdf`), plus structured metadata (file paths, chunk identifiers, taxonomy assignments). The T3 vector store is the native nexus-service (Postgres 17 + pgvector). Stored in:
  - the local nexus-service's Postgres cluster on disk (local mode — embeddings + chunk text, embedded server-side with bge-768)
  - a managed nexus-service's Postgres (managed-cloud mode — only if you point Conexus at a hosted service)
  - **frozen legacy artifact only, on installs that predate the 6.0 substrate move:** `~/.local/share/nexus/chroma/` may still hold a pre-PG ChromaDB store left over from before you migrated. On this release it is inert history, not a live migration source — the Chroma read path was deleted outright (RDR-155 P4b), and an install that still carries unmigrated Chroma/SQLite data is detected and redirected to a two-hop upgrade (install the last migration-capable release, migrate there, then upgrade forward) rather than being read directly by the version you are running.
- **Memory entries (T2)** — anything you (or an agent) writes via `nx memory put` or the `memory_put` MCP tool. Served by the same `nexus-service` Postgres as everything else (local mode: on the host disk; managed-cloud mode: the operator's server) — the SQLite T2 substrate is deleted (RDR-158). `~/.config/nexus/memory.db`, where still present from a pre-migration install, is an inert frozen snapshot, not a live store.
- **Catalog** — document registry and typed-link graph. Served by the same `nexus-service` Postgres (local mode: on the host disk; managed-cloud mode: the operator's server). The local SQLite/JSONL catalog is deleted (RDR-158 P4) — there is no `~/.config/nexus/catalog/` store on this release.
- **Session scratch (T1)** — ephemeral working notes shared across agents within a session. Served by `nexus-service` over Postgres, keyed to a session id (local mode: on the host disk; managed-cloud mode: the operator's server) — not in-memory ChromaDB, a substrate that is retired. Rows are cleared on session close and are swept automatically after 24 hours regardless, so scratch never accumulates as durable state.
- **Plan library** — saved query execution plans. Stored alongside memory entries (same backend and locality).
- **Logs** — structured operational logs at `~/.config/nexus/logs/` (rotating, 10 MB × 5). Always local to the host running the CLI/MCP process, in both local and managed-cloud mode.

## 2. What Conexus sends to third parties

**Local mode** (default — no credentials configured):
Nothing leaves the machine. Embeddings are computed locally by the on-machine nexus-service (bge-768 ONNX), and search runs against the local on-disk Postgres + pgvector store.

**Managed-cloud mode** (you opt in by pointing Conexus at a hosted nexus-service):
- **The managed nexus-service** — chunk text + embeddings are stored in the managed service's Postgres for retrieval, under that service operator's policy. Your data leaves your machine for whoever hosts the service.
- **Voyage AI** — in managed-cloud mode the service embeds chunk text and query strings with Voyage's API server-side. See https://www.voyageai.com/privacy.
- **Semantic Scholar** (only when you run `nx enrich bib`) — bibliographic metadata lookups for PDFs you ask Conexus to enrich. See https://www.semanticscholar.org/about/privacy.
- **Anthropic Claude** (only when an MCP operator tool fires) — chunks passed to `claude -p` subprocesses run by `operator_*` / `nx_answer` / `nx_tidy` are sent to Anthropic's API per Anthropic's standard data policy.

You control which (if any) of the above are reachable by deciding whether to set the corresponding credentials.

## 3. What Conexus never collects

- Conexus does not query or extract data from Claude's memory, chat history, conversation summaries, or user-uploaded files.
- Conexus does not transmit telemetry, analytics, crash reports, or usage data to the Conexus author.
- Conexus does not include any third-party tracking, advertising, or session-recording components.
- Conexus does not collect personally identifiable information beyond what the user explicitly writes into the indexed content or memory.

## 4. Data retention

- Local-mode data persists on disk until you delete it. There is no automatic purge of T2 memory (`nx memory delete`), T3 collections (`nx store delete`), or the catalog (`nx catalog gc`).
- Managed-cloud data persists in the hosted nexus-service's Postgres under that service operator's retention policy.
- T1 session scratch is keyed to a session id and is not durable: rows are cleared on session close and are swept automatically after 24 hours regardless, so scratch never accumulates as long-lived state.

## 5. Data export and deletion

- **Export** — `nx store export <collection>` produces a `.nxexp` archive of any T3 collection. `nx memory get` returns memory entries.
- **Delete** — `nx store delete`, `nx memory delete`, `nx catalog gc`, and the `daemon_uninstall` MCP tool with `remove_data=true` all remove data permanently. `remove_data=true` wipes the nexus **config directory** (`~/.config/nexus/`, or `NEXUS_CONFIG_DIR`); it does **not** touch `~/.local/share/nexus/`, which holds the Chroma store and the embedding-model cache. Use the Uninstall step below to remove both.
- **Uninstall** — removing Conexus and deleting `~/.config/nexus/` plus `~/.local/share/nexus/` removes everything Conexus persisted.

## 6. Children's privacy

Conexus is a developer tool. It is not directed to children under 13 and is not designed for use by minors.

## 7. Changes to this policy

Updates to this policy ship in `docs/privacy-policy.md` with each release. The version of the policy that applies to your installation is the one shipped in that installation.

## 8. Contact

Issues, questions, and security reports: https://github.com/Hellblazer/nexus/issues
