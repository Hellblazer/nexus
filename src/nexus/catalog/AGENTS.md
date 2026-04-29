# `nexus.catalog` — AGENTS.md

The catalog is a Xanadu-inspired document registry that tracks *what* is indexed and *how documents relate*. JSONL files are the source of truth; SQLite + FTS5 is the query cache, rebuilt automatically on mtime change.

## Core concepts

- **Tumbler** — hierarchical address (`1.2.5`) identifying a document. Every entry has one. `tumbler.py` provides depth, ancestors, lca, JSONL readers.
- **Owner** — the top-level tumbler segment. Repos use `owner_type='repo'` with a `repo_hash`; humans use `'curator'`. Repo owners without a hash are rejected (the shadow-registration bug class).
- **Source URI** — persistent identity. Validated at register time against `_KNOWN_URI_SCHEMES = {file, chroma, https, nx-scratch, x-devonthink-item}`. Bare paths normalize to `file://<abspath>`.
- **Link** — typed edge between documents. Built-in types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`, plus custom. Every link carries `created_by` provenance.

## Three link-creation paths

1. **Post-hoc** (batch, after indexing) — `link_generator.py`: `generate_citation_links()`, `generate_code_rdr_links()`, `generate_rdr_filepath_links()`. Run when a corpus is fully indexed.
2. **Auto-linker** — `auto_linker.py` fires on every `store_put` MCP call. Reads `link-context` from T1 scratch (tag `link-context`), creates links to seeded targets. Skills seed before dispatch; agents self-seed from their task prompt.
3. **Agent-direct** — agents call the `catalog_link` MCP tool during work for precise typed links.

## Two graph views

- `catalog_links` — **live documents only**. Use this for "what's actually linked right now."
- `catalog_link_query` — **all links including orphans** (where one endpoint has been deleted). Use for audits and provenance archaeology.

The `query` MCP tool has catalog-aware routing: `author`, `content_type`, `subtree`, `follow_links`, `depth`. Example: `query("how does path resolution work", follow_links="implements", subtree="1.1")`.

## Files

| File | Purpose |
|---|---|
| `catalog.py` | `Catalog` class — `register`, `update`, `link`, `link_query`, `graph`, `delete_document`, `link_audit`, `descendants`, `resolve_chunk`. Holds `_KNOWN_URI_SCHEMES` + `_normalize_source_uri` boundary validator. |
| `catalog_db.py` | SQLite schema, FTS5 tables, UNIQUE link constraint, `descendants()` SQL helper. |
| `tumbler.py` | `Tumbler` dataclass + `parse`, hierarchy helpers, JSONL readers with resilience (truncated rows, bad JSON). |
| `auto_linker.py` | Storage-boundary auto-linking from T1 scratch link-context. Hook firing site is `mcp/core.py`. |
| `link_generator.py` | Post-hoc batch linkers (citation, code↔RDR heuristic, RDR↔file-path). |
| `consolidation.py` | Merges per-paper `knowledge__<paper>` collections into corpus-level `knowledge__<corpus>`. |

## Adding a new source-URI scheme

The bar is **register a reader first, then add to the allow-list**. New schemes that have no reader cause silent register-success-but-extract-failure.

1. Add a `_read_<scheme>_uri()` function to `src/nexus/aspect_readers.py` returning `ReadOk` / `ReadFail`.
2. Register it in `_READERS` dict.
3. Add the scheme to `_KNOWN_URI_SCHEMES` in `catalog.py`.
4. Update the lock test in `tests/test_catalog.py::test_known_uri_schemes_table_is_locked_to_planned_set`.
5. Update the `--source-uri` CLI help text in `commands/catalog.py`.
6. Test the round-trip: register → resolve → read.

## Key invariants

- **JSONL is canonical, SQLite is cache.** Never write to SQLite without going through `Catalog.register/update/link`. Direct SQL writes will be overwritten on next mtime-driven rebuild.
- **Tumblers are append-only.** Updating an entry preserves the tumbler; deletion creates a tombstone, not a free slot. Reusing a tumbler corrupts the link graph.
- **Owners with `owner_type='repo'` MUST carry a `repo_hash`.** Enforced in `register_owner`. The empty-hash variant produced 83 orphan owners in the wild before the guard.
