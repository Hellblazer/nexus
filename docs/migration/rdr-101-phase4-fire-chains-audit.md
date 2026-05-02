# RDR-101 Phase 4: `fire_post_document_hooks` Chain Audit

**Bead:** `nexus-buv0`
**Goal:** Catalogue every consumer of the document-grain post-store chain
that reads `source_path` from the chain payload, and decide each one's
migration path under RDR-101 Phase 4 (replace `source_path` chunk
identity with the catalog `doc_id`).

The bead title says "exporter `fire_store_chains` consumer audit"; the
scope was widened on inspection because every direct
`fire_post_document_hooks` call site shares the same signature
constraint, so a per-site decision matrix is more useful than an
exporter-only one.

## Consumer inventory

### Registered hooks

Exactly one consumer reads the document chain payload today:

| Hook | Site | Signature | Reads |
|---|---|---|---|
| `aspect_extraction_enqueue_hook` | `src/nexus/aspect_worker.py:444` | `(source_path, collection, content)` | `source_path` as the queue identity |

The other two chains (`fire_post_store_hooks` per-doc, `fire_post_store_batch_hooks` batch) consume the per-chunk natural-id (sha256-derived `chunk_chroma_id`), not `source_path`. Their consumers (`chash_dual_write_batch_hook`, `taxonomy_assign_batch_hook`) are out of scope for this audit; their identity field is correct as-is.

### Direct fire sites

`fire_post_document_hooks` is called from 9 sites (1 MCP boundary + 8 CLI ingest, per the AST drift guard at `tests/test_hook_drift_guard.py`):

| # | Site | Identity passed today |
|---|---|---|
| 1 | `src/nexus/mcp/core.py:944` (MCP `store_put`) | `doc_id` (slug) |
| 2 | `src/nexus/indexer.py:869` (per-file ingest) | `str(file)` (filesystem path) |
| 3 | `src/nexus/code_indexer.py:490` | `str(file_path)` |
| 4 | `src/nexus/prose_indexer.py:264` | `str(file_path)` |
| 5 | `src/nexus/doc_indexer.py:528` (`_index_doc_file_generic`) | `sp` (source_path arg) |
| 6 | `src/nexus/doc_indexer.py:1065` (index_pdf streaming) | `str(pdf_path)` |
| 7 | `src/nexus/doc_indexer.py:1110` (index_pdf batch) | `str(pdf_path)` |
| 8 | `src/nexus/pipeline_stages.py:743` (PDF streaming) | `str(pdf_path)` |
| 9 | `src/nexus/mcp_infra.py:953` (`fire_store_chains`) | `sp` from `source_paths` arg (exporter feeds this) |

The MCP path (#1) already passes the catalog `doc_id` slug (a comment in `aspect_worker.aspect_extraction_enqueue_hook` documents this: "`source_path` is a doc_id at the MCP boundary, not a real filesystem path"). The other 8 pass filesystem paths.

## Per-consumer decision

`aspect_extraction_enqueue_hook` is the only consumer; its decision drives the whole chain.

**Use of `source_path` in the hook + downstream:**

1. **Enqueue:** `t2.aspect_queue.enqueue(collection, source_path, content)` writes a row to `aspect_extraction_queue`. The schema (`AspectExtractionQueue.QueueRow` at `src/nexus/db/t2/aspect_extraction_queue.py:111`) carries `(collection, source_path, content_hash, content, retry_count)`.
2. **Worker drain:** `aspect_worker._process_one` and `_process_batch` re-read `source_path` from the queue row and:
   - Pass it to `extract_aspects(content, source_path, collection, lookup_path=...)`.
   - Fall back to `Path(row.source_path).read_text()` when content is empty (CLI ingest path).
   - Pre-PR-471: extract_aspects routes through `read_source(uri)` with a `chroma://` URI built from `source_path`.

**Decision: migrate to `doc_id` (with `source_path` retained for disk fallback).**

The hook's identity field is purely a catalog lookup key. Once PR #471's chroma reader keys on `doc_id` via `doc_id_lookup`, the queue row needs to carry `doc_id` so the worker can build the same lookup. The CLI disk-fallback (`Path(row.source_path).read_text()`) still needs the filesystem path; we keep it as a denormalized field on the queue row.

This decision is identical to `nexus-tdgc`'s scope (the bead filed earlier as a deferred slice from `nexus-dcym`). The two beads should be folded together; the migration is a single PR, not two.

## Migration plan

The work is one logical PR with five mechanical changes; splitting them is harder than landing them together.

1. **Queue schema** (`src/nexus/db/t2/aspect_extraction_queue.py`):
   - Add `doc_id TEXT NOT NULL DEFAULT ''` column to `aspect_extraction_queue`.
   - Migrate existing rows: `doc_id = ""` (legacy rows).
   - Update `QueueRow` dataclass to add `doc_id: str = ""`.
   - Update `enqueue()` and `claim_next()` SQL to read/write the column.

2. **Hook signature** (`src/nexus/mcp_infra.py`):
   - Change `fire_post_document_hooks(source_path, collection, content)` to `fire_post_document_hooks(source_path, collection, content, *, doc_id="")`.
   - Update `register_post_document_hook` docstring (signature contract).
   - Update `fire_store_chains` to pass `doc_id` from its `doc_ids` arg.

3. **Hook implementation** (`src/nexus/aspect_worker.py:aspect_extraction_enqueue_hook`):
   - Accept `doc_id` keyword.
   - Pass it to `aspect_queue.enqueue(collection, source_path, content, doc_id=doc_id)`.

4. **Worker drain** (`src/nexus/aspect_worker.py:_process_one`, `_process_batch`):
   - Read `row.doc_id` from queue.
   - Build `doc_id_lookup = lambda c, s: row.doc_id or ""`.
   - Pass to `extract_aspects(content, source_path, collection, lookup_path=..., doc_id_lookup=doc_id_lookup)`.

5. **8 fire sites:**
   - `indexer.py:869`, `code_indexer.py:490`, `prose_indexer.py:264`, `doc_indexer.py:528,1065,1110`, `pipeline_stages.py:743`: each has `ctx.doc_id_resolver` (or equivalent) in scope from RDR-101 Phase 3 plumbing. Add `doc_id=ctx.doc_id_resolver(path)` to the fire call.
   - `mcp_infra.py:953` (`fire_store_chains`): pass `doc_id=did` from the existing `doc_ids` zip iteration.
   - `mcp/core.py:944`: already passes `doc_id` as positional `source_path`; thread it explicitly via the `doc_id=` kwarg too.

6. **Tests:**
   - `tests/test_hook_drift_guard.py`: update the AST guard to also verify `doc_id=` is passed at every fire site. The current guard counts call sites; the new shape should additionally assert keyword presence so a future sites that omits `doc_id` is caught.
   - Per-fire-site test: a chunk indexed with a known doc_id flows through the queue with that doc_id intact.
   - Worker-drain test: `doc_id_lookup` is supplied to `extract_aspects` when the queue row carries one, falls back to legacy behavior when empty.

## Dependencies and ordering

- **Blocked by `nexus-o6aa.10.1` (PR #471):** the worker's `doc_id_lookup` plumbing only matters if `extract_aspects` accepts the keyword. PR #471 adds it.
- **Folds in `nexus-tdgc`:** identical scope. Recommend closing `nexus-tdgc` as duplicate and tracking the migration solely under `nexus-buv0` (or vice versa, whichever stays).
- **Blocks `nexus-o6aa.10.3` (prune verb):** same as `nexus-tdgc`. The prune verb's coverage gate cannot turn green for collections whose aspect-extraction queue rows still key on `source_path`-only.

## Out of scope

- The exporter call sites (`exporter.py:389,409`) pass `source_paths` extracted from chunk metadata. Once the prune verb (`nexus-o6aa.10.3`) drops `source_path` from chunk metadata, the exporter will need to also extract `doc_id` from metadata and pass that as the `doc_ids` arg. That is a one-line change; bundling it into this PR would couple the migration to the prune verb's schedule. Defer until the prune verb lands.
- The `chash_dual_write_batch_hook` and `taxonomy_assign_batch_hook` chains. Their identity is `chunk_chroma_id`, which is not affected by Phase 4.
- The `register_post_store_hook` chain (currently empty by default). No live consumers, no migration needed.

## Acceptance check

- [x] Markdown report listing every direct fire site and every registered consumer.
- [x] Decision per consumer (single consumer; migrate to `doc_id`).
- [x] Migration plan (5 mechanical changes; one PR).
- [x] Dependency analysis (blocked-by `.10.1`, folds with `nexus-tdgc`, blocks `.10.3`).
- [x] Out-of-scope flags (exporter remap, batch chains, empty store-hook chain).
