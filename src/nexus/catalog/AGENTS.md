# `nexus.catalog` — AGENTS.md

The catalog is a Xanadu-inspired document registry that tracks *what* is indexed and *how documents relate*. Since the nexus-i711w terminal deletion the catalog is SERVICE-OWNED in every mode: the authoritative store is the Java engine's Postgres tables, reached through `HttpCatalogClient`. The local SQLite + JSONL catalog (event log, projector, `.catalog.db`) is deleted; a surviving on-disk `~/.config/nexus/catalog/` is a frozen migration source only.

## Core concepts

- **Tumbler** — hierarchical address (`1.2.5`) identifying a document. Every entry has one. `tumbler.py` provides depth, ancestors, lca, JSONL readers.
- **Owner** — the top-level tumbler segment. Repos use `owner_type='repo'` with a `repo_hash`; humans use `'curator'`. Repo owners without a hash are rejected (the shadow-registration bug class).
- **Source URI** — persistent identity. Validated at register time against `_KNOWN_URI_SCHEMES = {file, chroma, https, nx-scratch, x-devonthink-item}`. Bare paths normalize to `file://<abspath>` (`types.py:_normalize_source_uri`).
- **Link** — typed edge between documents. Built-in types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`, plus custom. Every link carries `created_by` provenance.

## Three link-creation paths

1. **Post-hoc** (batch, after indexing): `link_generator.py` exposes `generate_citation_links()`, `generate_rdr_filepath_links()`, `generate_prose_filepath_links()`, `generate_pdf_corpus_links()`. Run when a corpus is fully indexed.
2. **Auto-linker** — `auto_linker.py` fires on every `store_put` MCP call. Reads `link-context` from T1 scratch (tag `link-context`), creates links to seeded targets. Skills seed before dispatch; agents self-seed from their task prompt.
3. **Agent-direct** — agents call the `catalog_link` MCP tool during work for precise typed links.

## Two graph views

- `catalog_links` — **live documents only**. Use this for "what's actually linked right now."
- `catalog_link_query` — **all links including orphans** (where one endpoint has been deleted). Use for audits and provenance archaeology.

The `query` MCP tool has catalog-aware routing: `author`, `content_type`, `subtree`, `follow_links`, `depth`. Example: `query("how does path resolution work", follow_links="implements", subtree="1.1")`.

## Files

| File | Purpose |
|---|---|
| `http_catalog_client.py` | `HttpCatalogClient` — the catalog handle in every mode. Full read surface + whitelisted writes — including the RUNFENCE index-run fence ops `begin_index_run`/`complete_index_run`/`fail_index_run` (nexus-5xn3k.3) — over the engine's `/v1/catalog/*` routes. `manifest_verify`/`manifest_verify_all`/`manifest_orphans`/`manifest_backfill` client methods RETIRED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) — the manifest-chunk FK makes the dangling state they diagnosed unreachable; `manifest_null_collection_report` (the FK's own explicit carve-out — NULL-collection rows are unenforced under `MATCH SIMPLE`) is the one census survivor. `nexus.manifest_verify(text)` itself is not dropped server-side (`CatalogRepository.completeIndexRun` depends on it internally), but this client no longer has a route onto it. |
| `catalog_protocol.py` | `CatalogReader` / `CatalogWriter` Protocols + `CATALOG_WRITE_OPS` (the tooling-enforced write whitelist). Annotate consumers with these, never a concrete class. |
| `factory.py` | `make_catalog_reader()` / `make_catalog_writer()` — the only sanctioned construction path (shared process-lifetime service client, nexus-5en9j). |
| `types.py` | Substrate-neutral value types: `CatalogEntry`, `CatalogLink`, `ManifestRow`, `make_relative`, `_normalize_source_uri`, `_default_registry_path`. |
| `tumbler.py` | `Tumbler` dataclass + `parse`, hierarchy helpers, JSONL readers with resilience (truncated rows, bad JSON). |
| `auto_linker.py` | Storage-boundary auto-linking from T1 scratch link-context. Hook firing site is `mcp/core.py`. |
| `link_generator.py` | Post-hoc batch linkers (citation, RDR↔file-path, prose↔file-path, pdf same-as via shared head_hash). |
| `catalog_spans.py`, `store_hook.py`, `orphan_backfill.py`, `manifest_backfill.py`, `write_priority.py` | Span resolution, post-store registration hook, backfill tooling, indexer fairness window. |
| `manifest_heal.py` | Shared manifest-gap heal core (nexus-8g0ch + nexus-c21fk) — rebuilds `document_chunks` manifest rows from T3 chunks already stored, without re-embedding. Consumed by both `nx catalog reconcile` and the `nx index repo` self-heal pass. |
| `chunk_quarantine.py` | Orphan-chunk soft delete (nexus-xukbj) — moves GC orphans to a sibling `quarantine__*` collection (excluded from every search corpus by construction) instead of hard-deleting; restores chashes that become referenced again. |
| `dt_link_generator.py` | DEVONthink semantic + structural link generation (RDR-139 Layer B) — writes `relates` edges from DT 'See Also' similarity and author-curated item links; gated on `devonthink.available`. |
| `collection_name.py` | `CollectionName` value object (RDR-103 Phase 1) — the four-segment `(content_type, owner_id, embedding_model, model_version)` tuple rendered as `<content_type>__<owner_id>__<embedding_model>__v<n>`; `parse` is strict. |

Deleted in nexus-i711w (RDR-158 P4 terminal deletion): `catalog.py` (the local `Catalog` class), `catalog_db.py`, `event_log.py`, `projector.py`, `events.py`, `catalog_owners.py`, `catalog_sync.py`, `catalog_links.py`, `catalog_docs.py`, `catalog_backup.py`, `catalog_git.py`, `catalog_writes.py` (ManifestRow relocated to `types.py`), `consolidation.py`, `collections_owner_backfill.py`, `synthesizer.py`.

## Adding a new source-URI scheme

The bar is **register a reader first, then add to the allow-list**. New schemes that have no reader cause silent register-success-but-extract-failure.

1. Add a `_read_<scheme>_uri()` function to `src/nexus/aspect_readers.py` returning `ReadOk` / `ReadFail`.
2. Register it in `_READERS` dict.
3. Add the scheme to `_KNOWN_URI_SCHEMES` in `types.py`.
4. Update the lock test in `tests/test_catalog.py::test_known_uri_schemes_table_is_locked_to_planned_set`.
5. Update the `--source-uri` CLI help text in `commands/catalog.py`.
6. Test the round-trip: register → resolve → read.

## Key invariants

- **The engine's Postgres catalog is canonical.** Every mutation flows through the `CATALOG_WRITE_OPS` whitelist on a `make_catalog_writer()` proxy; reads through `make_catalog_reader()`. Direct construction of catalog handles outside `factory.py` is lint-banned (`storage_boundary_lint.py`).
- **Every new writer method MUST be added to `CATALOG_WRITE_OPS`** (`catalog_protocol.py`) or it silently raises `AttributeError` through the closed `_ServiceCatalogWriter` whitelist, which a bare `except` upstream can turn into a feature that no-ops forever (the nexus-kgos1 trap).
- **Tumblers are append-only.** Updating an entry preserves the tumbler; deletion creates a tombstone, not a free slot. Reusing a tumbler corrupts the link graph.
- **Owners with `owner_type='repo'` MUST carry a `repo_hash`.** Enforced in `register_owner`. The empty-hash variant produced 83 orphan owners in the wild before the guard.
- **No `._db` / `._dir` reach-ins.** `HttpCatalogClient._db` raises by design; the boundary-lint baselines (`CATALOG_DB_ACCESS_BASELINE`, `CATALOG_DIR_ACCESS_BASELINE`) are enforced at 0.
