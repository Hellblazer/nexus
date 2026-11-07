# `nexus.mcp` — AGENTS.md

Two MCPServer servers (mcp 2.x, `mcp.server.mcpserver.MCPServer`) and the post-store hook framework that backs them. The single load-bearing concept here is **three parallel hook chains**, each fired from every storage event so per-document enrichment runs symmetrically across MCP `store_put` and CLI bulk ingest.

## Servers

| File | Server | Tools |
|---|---|---|
| `core.py` | `nexus` | 46 registered tools + 13 demoted (plain functions, no `@mcp.tool()`; the 10 `operator_*` since nexus-ivi4s): `search`, `query`, `store_put/get/list`, `memory_*`, `scratch*`, `collection_list`, `plan_save/search`, and more (see the module docstring for the current roster). |
| `catalog.py` | `nexus-catalog` | 10 registered tools + 3 demoted: `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats` (the `catalog_` prefix is dropped — server namespace already provides context) |

`src/nexus/mcp_infra.py` holds singletons (T2/T3 clients), test injection, default post-store hook consumers, and `check_version_compatibility`. The three-chain hook registry itself lives in `nexus.hook_registry.HookRegistry` and is constructor-injected from each entry point (MCP server, CLI command, indexer). `src/nexus/mcp_server.py` is a backward-compat shim re-exporting the tool functions from `core.py` and `catalog.py` (no `__all__` to pin a count against).

## Post-store hook framework

Three parallel chains on `HookRegistry` — pick the one that matches your workload shape:

| Shape | Register with | Fired from | When to use |
|---|---|---|---|
| **Single-doc** | `registry.register_single(fn)` | MCP `store_put` (1×); CLI ingest (per-doc) | Per-document work keyed on `doc_id`. **Currently empty by default.** |
| **Batch** | `registry.register_batch(fn)` | CLI ingest (full batch); MCP `store_put` (1-element batch) | Work that benefits from batched dependency calls — one pgvector ANN query (via nexus-service) for N centroids, one batched T2 upsert. |
| **Document-grain** | `registry.register_document(fn)` | MCP `store_put` + 8 CLI ingest sites in 6 modules | Source-document boundary as stable identity (vs chunk-level `doc_id`). |

All three:

- Capture per-hook exceptions, persist to T2 `hook_failures` (with `chain` column = `single` / `batch` / `document`), never propagate.
- Fire from every storage path so coverage is symmetric.
- Are synchronous all the way down — zero asyncio in the dispatcher.

### Current consumers

- **Batch chain**:
  1. `taxonomy_assign_batch_hook` (RDR-070) — accepts `embeddings=None` from the MCP path and fetches them from T3 inline.
  2. `manifest_write_batch_hook` (RDR-108 D2) — populates the catalog `document_chunks` manifest.
  (`chash_dual_write_batch_hook` was retired by RDR-187 — the chunks tables are the chash store now, so there is nothing left to dual-write. `install_default_hooks` in `hook_registry.py` no longer registers it.)
- **Document-grain chain**: `aspect_extraction_enqueue_hook` (RDR-089). Defined in `aspect_worker.py`, registered via `install_default_hooks` in `hook_registry.py`. Enqueues one task per document to the engine's `aspects` tuple queue; a daemon worker thread claims it and invokes `extract_aspects`. Async dispatch was necessary because the spike measured 26.5s median per document — blocking-inline would have been a non-starter on the ingest path.

### Content-sourcing contract (document-grain chain)

- MCP `store_put` passes `content=<full document text>` literally — the text is in scope at the boundary.
- CLI ingest sites pass `content=""` as the contract signal that the hook may need to read `source_path` itself.
- `aspect_extraction_enqueue_hook` accepts `content` for the hook signature and drops it: the task carries the document's identity only (RDR-235 E2), and the worker re-reads the document from T3 chunks or the source path.

### Enqueue identity & loud-failure contract (RDR-172)

The `doc_id` forwarded to the enqueue is the **catalog document id (tumbler)**, not the chunk hash. `store_put` forwards `catalog_doc_id` — the tumbler returned by `catalog_store_hook` when it registers the note-backed document (RDR-172 P1.1, `mcp/core.py`). Forwarding the chunk hash was the bug `nexus-ov0sw`.

- **Blank `doc_id` → no task.** The task's identity is its `doc_id` (the replace nonce), so the hook resolves a blank one through the catalog and skips the enqueue, logging `aspect_enqueue_skipped_no_doc_id`, when it cannot. The retired table accepted a blank one as NULL; the tuple queue has no such sentinel and `HttpAspectQueue.enqueue` raises `ValueError` for it.
- **No foreign key refuses an unregistered `doc_id`.** The tuple queue checks nothing at enqueue, so a task for a document whose catalog row has not landed yet can exist; the worker's first claim nacks it once (`aspect_task_doc_not_visible_yet`, nexus-1o3ft) and a document still missing on the second is acked as dropped.
- **Loud-failure tripwire.** The hook keeps the enqueue best-effort (never block ingest, RDR-089 P0.1), but its internal swallow would otherwise hide a failure from `hook_registry`'s recorder. It therefore persists its own `hook_failures` row (`hook_name='aspect_extraction_enqueue_hook'`, `chain='document'`) and logs `aspect_extraction_enqueue_failed` (RDR-172 P2.1). The `--fullstack` ingest E2E asserts **zero** such rows, so the silent-total-failure class cannot regress unobserved.

## Drift guard

`tests/test_hook_drift_guard.py` uses `ast.walk` to enforce two guarded sets:

- `GUARDED_NAMES = {taxonomy_assign_batch_hook}` — may only appear in `mcp_infra.py` (definition) and `hook_registry.py` (registration via `install_default_hooks`). `RETIRED_HOOK_NAMES = {chash_dual_write_batch_hook}` — the same guard actively BANS this name from reappearing in `src/`.
- `DOCUMENT_HOOK_GUARDED_NAMES = {aspect_extraction_enqueue_hook}` — may only appear in `aspect_worker.py` (definition) and `hook_registry.py` (registration via `install_default_hooks`).

**New consumers register through `HookRegistry.register_single/batch/document` (or `install_default_hooks` for load-bearing defaults).** Direct calls fail CI.

A separate runtime fire-once test (`test_index_pdf_fires_document_hook_exactly_once` in `tests/test_doc_indexer.py`) drives a sample PDF through `index_pdf` with a counting probe hook to assert the document chain fires exactly once per source document — the AST count guard alone cannot detect a regression that moves a fire site inside a per-chunk loop.

## Out of scope by design

- **Three catalog-registration mechanisms** (`catalog_store_hook` in `catalog/store_hook.py`, `_catalog_pdf_hook` in `pipeline_stages.py`, `_catalog_hook` in `indexer.py`, ad hoc) capture different per-domain metadata. Three legitimate per-domain registrations, not three copies of the same hook.
- **`_catalog_auto_link` is MCP-only** — reads T1 scratch `link-context` entries that agents seed before `store_put`. CLI bulk ingest has no per-file pre-declaration semantics; it uses the post-hoc linkers in `catalog/link_generator.py`. Intentional path-shape coupling.

## Hot rules

- **A tool failure returns through `_fail(...)`** (`_tool_failure.fail`, nexus-6b6hq). The mark makes the registration-boundary patch (`_sdk_patches._patch_tool_failure_is_error`) answer the client `isError: true` with the content MCPServer would have sent for the plain value; an unmarked `"Error: ..."` or `{"error": ...}` reaches the client as a success. `_mcp_tool_error` already marks. The marked value equals the plain one, so in-process callers see no change. List tools fail the same way: `[]` means only no results. The `hook_*` tools are exempt (`EXEMPT_TOOL_PREFIXES`); `nx_answer`'s `[non-answer]` text is never marked. The lint `test_no_failure_return_is_left_unmarked` catches a literal failure return without the mark; a failure built in a variable first is invisible to it.
- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime (no resolution layer exists).
- **Register, don't import.** Add new hook consumers via `HookRegistry.register_*` (or extend `install_default_hooks` for load-bearing defaults), not by direct call. CI's drift guard will reject the bypass.
- **The chash-before-taxonomy ordering invariant is gone.** RDR-187 retired `chash_dual_write_batch_hook`; the remaining batch consumers (`taxonomy_assign_batch_hook`, `manifest_write_batch_hook`) have no registration-order dependency on each other.
- **SDK internals we reach into are few, private, and listed.** `server._lowlevel_server` (instructions, `add_notification_handler` for the channel gate, `lifespan`), `Server.middleware` (the tool-call log and the stale-host check are `tools/call` entries; the SDK calls it provisional for 2.x minors), `Tool.from_function` (the `_fail` patch), and the raw stdio write stream (`channel.py`). The pin is `mcp>=2.3,<3`; raise the cap only with a deliberate migration gated by the MVV handshake probe. Two mcp 1.x patches are retired because 2.x fixed them (the answered-after-cancel reply, sync bodies on the loop); `tests/test_mcp_sdk_patches.py` and `tests/test_mcp_sync_tool_offload.py` pin both on the wire, so a regression fails there. mcp 2.x also hides a non-`ToolError` crash's text from the client; `_tool_failure` keeps the text of every `Exception` (as 1.x sent it, `Error executing tool <name>: <text>`) by re-raising it as a `ToolError`, and logs it with its traceback first because the SDK logs a `ToolError` without one. The stale-host note and the tools that raise on purpose (`nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`) depend on that text.
