# `nexus.mcp` — AGENTS.md

Two FastMCP servers and the post-store hook framework that backs them. The single load-bearing concept here is **three parallel hook chains**, each fired from every storage event so per-document enrichment runs symmetrically across MCP `store_put` and CLI bulk ingest.

## Servers

| File | Server | Tools |
|---|---|---|
| `core.py` | `nexus` | 14 tools: `search`, `query`, `store_put/get/list`, `memory_*`, `scratch*`, `collection_list`, `plan_save/search` |
| `catalog.py` | `nexus-catalog` | 10 tools: `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats` (the `catalog_` prefix is dropped — server namespace already provides context) |

`mcp_infra.py` holds singletons (T2/T3 clients), test injection, post-store hook registries, and `check_version_compatibility`. `mcp_server.py` is a backward-compat shim re-exporting all 30 functions.

## Post-store hook framework

Three parallel chains — pick the one that matches your workload shape:

| Shape | Register with | Fired from | When to use |
|---|---|---|---|
| **Single-doc** | `register_post_store_hook(fn)` | MCP `store_put` (1×); CLI ingest (per-doc) | Per-document work keyed on `doc_id`. **Currently empty by default.** |
| **Batch** | `register_post_store_batch_hook(fn)` | CLI ingest (full batch); MCP `store_put` (1-element batch) | Work that benefits from batched dependency calls — one ChromaDB query for N centroids, one batched T2 upsert. |
| **Document-grain** | `register_post_document_hook(fn)` | MCP `store_put` + 8 CLI ingest sites in 6 modules | Source-document boundary as stable identity (vs chunk-level `doc_id`). |

All three:

- Capture per-hook exceptions, persist to T2 `hook_failures` (with `chain` column = `single` / `batch` / `document`), never propagate.
- Fire from every storage path so coverage is symmetric.
- Are synchronous all the way down — zero asyncio in the dispatcher.

### Current consumers

- **Batch chain** (registration order is load-bearing):
  1. `chash_dual_write_batch_hook` (RDR-086) — must run first so chash rows exist before topic assignment.
  2. `taxonomy_assign_batch_hook` (RDR-070) — accepts `embeddings=None` from the MCP path and fetches them from T3 inline.
- **Document-grain chain**: `aspect_extraction_enqueue_hook` (RDR-089). Defined in `aspect_worker.py`, registered in `mcp/core.py`. Enqueues to T2 `aspect_extraction_queue`; a daemon worker thread drains it and invokes `extract_aspects`. Async dispatch was necessary because the spike measured 26.5s median per document — blocking-inline would have been a non-starter on the ingest path.

### Content-sourcing contract (document-grain chain)

- MCP `store_put` passes `content=<full document text>` literally — the text is in scope at the boundary.
- CLI ingest sites pass `content=""` as the contract signal that the hook may need to read `source_path` itself.
- `aspect_extraction_enqueue_hook` persists `content` to the queue row when non-empty so the worker has the text without re-reading from disk; CLI rows where content was not in scope rely on the worker's source-path-read fallback.

## Drift guard

`tests/test_hook_drift_guard.py` uses `ast.walk` to enforce two guarded sets:

- `GUARDED_NAMES = {taxonomy_assign_batch_hook, chash_dual_write_batch_hook}` — may only appear in `mcp_infra.py` (definition) and `mcp/core.py` (registration).
- `DOCUMENT_HOOK_GUARDED_NAMES = {aspect_extraction_enqueue_hook}` — may only appear in `aspect_worker.py` (definition) and `mcp/core.py` (registration).

**New consumers register through the `register_post_*_hook` API.** Direct calls fail CI.

A separate runtime fire-once test (`test_index_pdf_fires_document_hook_exactly_once` in `tests/test_doc_indexer.py`) drives a sample PDF through `index_pdf` with a counting probe hook to assert the document chain fires exactly once per source document — the AST count guard alone cannot detect a regression that moves a fire site inside a per-chunk loop.

## Out of scope by design

- **Three catalog-registration mechanisms** (`_catalog_store_hook` in `commands/store.py`, `_catalog_pdf_hook` in `pipeline_stages.py`, `indexer.py:250` ad-hoc) capture different per-domain metadata. Three legitimate per-domain registrations, not three copies of the same hook.
- **`_catalog_auto_link` is MCP-only** — reads T1 scratch `link-context` entries that agents seed before `store_put`. CLI bulk ingest has no per-file pre-declaration semantics; it uses the post-hoc linkers in `catalog/link_generator.py`. Intentional path-shape coupling.

## Hot rules

- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime (no resolution layer exists).
- **Register, don't import.** Add new hook consumers via `register_post_*_hook`, not by direct call. CI's drift guard will reject the bypass.
- **Preserve registration order on the batch chain.** chash before taxonomy. Other orderings violate the chash-rows-exist invariant.
