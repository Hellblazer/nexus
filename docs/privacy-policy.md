# Privacy Policy — Conexus

_Effective: 2026-06-20_

Conexus is a self-hosted MCP server and Claude Code / Claude Desktop extension that indexes content on your machine and provides semantic search and persistent memory across Claude conversations. This policy describes what data Conexus handles, where it goes, and what is never collected.

## 1. What Conexus stores

All persistent data lives on the host machine running Conexus:

- **Indexed content** — text from files you ask Conexus to index (`nx index repo`, `nx index pdf`), plus structured metadata (file paths, chunk identifiers, taxonomy assignments). As of 6.0 the T3 vector store is the native nexus-service (Postgres 17 + pgvector). Stored in:
  - the local nexus-service's Postgres cluster on disk (local mode — embeddings + chunk text, embedded server-side with bge-768)
  - a managed nexus-service's Postgres (managed-cloud mode — only if you point Conexus at a hosted service)
  - `~/.local/share/nexus/chroma/` (legacy ChromaDB store, read only as the migration source for `nx guided-upgrade`)
- **Memory entries** — anything you (or an agent) writes via `nx memory put` or the `memory_put` MCP tool. Stored in `~/.config/nexus/memory.db` (SQLite, FTS5).
- **Catalog** — document registry and typed-link graph. Stored in `~/.config/nexus/catalog/` (JSONL + SQLite cache).
- **Session scratch** — ephemeral working notes shared across agents within a session. In-memory ChromaDB; wiped at session end.
- **Plan library** — saved query execution plans. Stored alongside memory in `memory.db`.
- **Logs** — structured operational logs at `~/.config/nexus/logs/` (rotating, 10 MB × 5).

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
- T1 session scratch is wiped automatically when the host process exits.

## 5. Data export and deletion

- **Export** — `nx store export <collection>` produces a `.nxexp` archive of any T3 collection. `nx memory get` returns memory entries.
- **Delete** — `nx store delete`, `nx memory delete`, `nx catalog gc`, and `nx daemon t2 uninstall --remove-data` (full T2 wipe) all remove data permanently.
- **Uninstall** — removing Conexus and deleting `~/.config/nexus/` plus `~/.local/share/nexus/` removes everything Conexus persisted.

## 6. Children's privacy

Conexus is a developer tool. It is not directed to children under 13 and is not designed for use by minors.

## 7. Changes to this policy

Updates to this policy ship in `docs/privacy-policy.md` with each release. The version of the policy that applies to your installation is the one shipped in that installation.

## 8. Contact

Issues, questions, and security reports: https://github.com/Hellblazer/nexus/issues
