---
title: "RDR-086: Chash Span Surface — Authoring, Resolution, and Verification"
id: RDR-086
status: draft
type: Feature
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-16
revised: 2026-04-17
related_issues: []
related: [RDR-053, RDR-075, RDR-078, RDR-082, RDR-083]
---

# RDR-086: Chash Span Surface

RDR-083 shipped the `chash:` citation grammar, the scanner, and the
`AnchorResolver` that plugs into RDR-082's Resolver registry. It did
not ship the machinery that makes those citations *usable at speed* —
and the 2026-04-17 ART-instance feedback surfaced a deeper problem:
it also didn't surface the machinery authors need. An author or
agent that wants to cite a specific chunk has no CLI or MCP path to
obtain the chash — they have to drop to the raw ChromaDB API and
copy the hash by hand.

**The primitive is partly built already.** Gate layer-3 discovered
that significant chunks of infrastructure this RDR would otherwise
reinvent already exist:

- **Per-collection chash lookup** — `catalog.resolve_span(span,
  physical_collection, t3)` at `src/nexus/catalog/catalog.py:575`
  takes a `chash:<hex>` span and returns `{chunk_text, metadata,
  chunk_hash}`. Missing: a collection-agnostic (global) variant.
- **Backfill command** — `_backfill_chunk_text_hash()` at
  `src/nexus/commands/collection.py:307` populates missing
  `chunk_text_hash` on existing chunks; wired to `nx collection
  backfill-hash`. No T2 reinvention needed for existing chunks.
- **Write-site coverage** — the indexing pipelines already write
  `chunk_text_hash` on every chunk at **six** sites (see RF-2).
  There is no instrumentation gap.

What is genuinely missing, and what this RDR actually owns:

1. **A collection-agnostic `resolve_chash(chash)`** that doesn't
   require the caller to know which physical collection holds the
   chunk.
2. **A T2 speedup layer** so the global lookup doesn't pay the
   13-min-serial ChromaDB-filter tax measured in RF-6.
3. **`chunk_text_hash` on structured returns** of `search`, `query`,
   and `nx_answer` — the forward path that closes the authoring
   loop (ART feedback, RF-1).
4. **`nx doc cite`** — one-shot CLI that composes (3) into a
   ready-to-paste markdown citation.

Reframed: this RDR is **extend + speed + surface + compose**, not
build-from-scratch.

Four concrete holes to close:

1. `nx doc check-grounding` counts chash-shaped citations but cannot
   tell a hash-of-a-real-chunk from a hash-of-an-unindexed-chunk. Its
   coverage ratio is therefore a *shape* signal, not a *grounding*
   signal.
2. `nx doc check-extensions` passes chash hex values as `doc_ids` to
   a SQL query against `topic_assignments.doc_id`, which is populated
   with ChromaDB collection-scoped IDs (`knowledge__art:doc:chunk:0`).
   The namespaces never intersect; every input returns `no_data`. The
   command emits a WARNING and the docstring marks it `[experimental]`,
   but it is currently inert.
3. **Authors have no path to obtain a chash.** `search(structured=True)`
   returns `{ids, tumblers, distances, collections}`. `query(structured=True)`
   is similar. `nx_answer` envelopes carry final-text, not hashes. An
   agent that just *found* the exact chunk to cite still has no way
   to cite it without ChromaDB-API surgery.
4. **No "cite this claim" command.** The normal workflow is "here's
   my prose claim, find the best-matching chunk, give me a ready-to-
   paste `[display](chash:…)` markdown link." That's a three-step
   ChromaDB dance today — embed the claim, top-k query, copy hash
   metadata by hand. It should be one command.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: `chash:` citations are unverifiable

Authors can write `[claim](chash:<64-hex>)` and the scanner will count
it, but neither the renderer nor the grounding validator can confirm
the chunk exists in the corpus. A citation against a made-up hash
passes the scanner; a citation against a real chunk whose indexing
has rolled over (RDR-053 fixed boundaries but reindexing events
exist) passes too. The author has no tool-driven feedback loop.

#### Gap 2: `check-extensions` cannot map chash to catalog entry

The command's correctness premise is "for each chunk cited in prose,
look up its projection similarity against a primary-source collection."
Without chash → catalog-entry resolution, "for each chunk cited" is
impossible. The query runs, returns nothing, and the WARNING fires.
Every ART/knowledge project that would benefit from author-extension
auto-flagging currently has to do it by hand, exactly the state
RDR-083 Gap 3 was supposed to leave behind.

#### Gap 3: `--fail-ungrounded` has no signal to gate on

RDR-083's original scope listed `--fail-ungrounded` as the CI knob
for machine-checked grounding. It was dropped from v1 because its
semantics depend entirely on `resolve_chash`. Users who want a
build-breaking grounding gate today have nothing — `--fail-under` on
shape-coverage is the closest, and it can't distinguish a doc full
of made-up chash hashes from a doc full of real ones.

#### Gap 4: `--expand-citations` cannot render chunk text

RDR-083's Phase 2 polish for `nx doc render --expand-citations`
(inline footnote/tooltip with the cited chunk text) is blocked on
the same resolver. The renderer currently preserves `chash:` links
verbatim.

#### Gap 5: No forward path from retrieval to chash

`search(structured=True)` returns `{ids, tumblers, distances,
collections}`. `query(structured=True)` returns document-level
results with the same shape. `nx_answer` returns final text. None
of them surface the underlying chunk's content hash. An agent
running `search(query=<claim>, corpus=<primary-source>, limit=1,
structured=True)` finds the right chunk but cannot read its chash
out of the payload. The authoring workflow then forces a descent
into the ChromaDB API to recover the metadata — exactly the
"descend to the raw backend" anti-pattern the MCP surface exists to
prevent. Surfacing `chunk_text_hash` on the structured-return
payload closes the forward direction. The ART-instance feedback
(2026-04-17) estimates this single field change closes ~80% of the
authoring gap on its own.

#### Gap 6: No one-shot "cite this claim" command

Even with `chunk_text_hash` on structured returns, the author still
runs three steps: call `search`, inspect the top result, assemble
a markdown link. A focused CLI — `nx doc cite "<claim text>"
--against <collection>` — collapses this to one invocation that
emits a ready-to-paste `[summary](chash:<hex>)` snippet (plus, on
`--json`, the resolved metadata for pipeline scripting). Without it
the `chash:` citation grammar stays correct-but-painful, which the
ART-instance feedback identifies as adoption-limiting for RDR-083's
main value proposition.

## Context

### Background

`chash:` is already a first-class catalog concept
(`src/nexus/catalog/catalog.py:resolve_span`) that accepts spans
like `chash:<64hex>` and `chash:<64hex>:<start>-<end>` when given a
specific physical collection. The per-collection signature is fine
for RDR-078's plan-execution step (caller knows the collection);
it's inadequate for prose citations where the author pastes a chash
without a collection. The missing primitive is a *global* chash
lookup.

RDR-053 (Xanadu fidelity) stabilised chunk boundaries and hash
inputs, so chash stability across reindex events is not a new design
problem — it is guaranteed as long as the collection's chunking
parameters don't change.

### Technical Environment

- Catalog SQLite at `~/.config/nexus/catalog/catalog.db` (the query
  cache rebuilt from JSONL on mtime change).
- ChromaDB Cloud (or local) holds the chunks; each chunk's metadata
  includes the `chash` hex value — this is where the authoritative
  mapping lives.
- T3 client is `chromadb.CloudClient` or `chromadb.PersistentClient`
  depending on mode; both expose `collection.get(where={"chash": <hex>})`.

## Research Findings

### Investigation (all resolved as of 2026-04-17)

- **Verify** — does every indexing path actually write the hash
  to ChromaDB metadata? **Resolved (RF-2)**: six write sites —
  `code_indexer.py:371`, `doc_indexer.py:591` + `:666`,
  `prose_indexer.py:96` + `:141`, `pipeline_stages.py:163`.
  `pdf_chunker.py` produces chunks but does NOT write the hash —
  the write happens downstream in `doc_indexer.py` or
  `pipeline_stages.py`.
- **Verify** — what fraction of chunks in a typical corpus have
  the hash vs. missing? **Resolved (RF-5)**: live sample of 10
  chunks per prefix on prod returned `chunk_text_hash` on 10/10
  for every citable-content prefix (code, docs, rdr, knowledge).
  `taxonomy__centroids` correctly has none (topic centroids,
  not indexed chunks).
- **Design choice** — global lookup cost: SHA-256 is 64 hex chars;
  querying every T3 collection by metadata is O(N_collections) per
  chash. For `check-extensions` across a 20-citation doc with 100
  collections, that's 2000 ChromaDB calls. Needs an index.
- **Option** — maintain a `chash → (collection, doc_id)` table in
  T2 SQLite, populated at indexing time. Single JOIN replaces
  N_collections scans. Most scalable; requires a new T2 migration.

### External evidence

- **RF-1 (2026-04-17)** — ART-instance observation on the
  authoring gap. The instance tried to author `chash:` citations
  against `docs__art-grossberg-papers` and could not — no CLI or
  MCP surface exposes the chunk-to-hash mapping. `operator_extract`
  works on text content, not metadata. `store_get`/`store_list`
  show document-level views, not per-chunk hashes. `nx taxonomy`
  operates at the topic/collection level. Even `search(structured=True)`
  omits the chunk hash. The instance had to drop to `collection.get(include=["metadatas"])`
  on the raw ChromaDB client to recover the hash and assemble the
  citation by hand. Reported as a polish-bead against nexus
  (2026-04-17 conversation). This RDR is the owner.

### Source-site investigation (2026-04-17)

- **RF-2** — **Every indexing path already writes the per-chunk hash
  to ChromaDB metadata under the key `chunk_text_hash`.** Verified
  by `grep chunk_text_hash src/nexus/`:
  - `src/nexus/code_indexer.py:371` — code path
  - `src/nexus/doc_indexer.py:591` — markdown batch path
  - `src/nexus/doc_indexer.py:666` — PDF batch path
  - `src/nexus/prose_indexer.py:96` — CCE markdown path
  - `src/nexus/prose_indexer.py:141` — single-chunk CCE path
  - `src/nexus/pipeline_stages.py:163` — streaming PDF pipeline

  **Six write sites, not three.** The initial draft cited only the
  first three; `prose_indexer.py` and `pipeline_stages.py` were
  missed. `pdf_chunker.py` produces the chunks but does **not**
  write the hash — that happens in `doc_indexer.py` (batch) and
  `pipeline_stages.py` (streaming), downstream of the chunker.

  Two keys coexist on every chunk: `content_hash` (SHA-256 of the
  whole source file, for staleness / dedup) and `chunk_text_hash`
  (SHA-256 of the chunk text — the citable identity). RDR-053's
  "chash" primitive maps to `chunk_text_hash`.

  Implication for the plan: **there is no instrumentation gap.**
  The dual-write to T2 lands at exactly the six known sites.

- **RF-3** — **Forward-path plumbing is a one-liner per return
  site.** The `search(structured=True)` return builder at
  `src/nexus/mcp/core.py:168-174` already has the chunk's full
  metadata in scope via `r.metadata`. Adding
  `"chunk_text_hash": [r.metadata.get("chunk_text_hash", "") for r in page]`
  to the returned dict is a one-line change. Same shape applies
  to `query(structured=True)` at `:376-380` and the `nx_answer`
  envelope. **No architectural change** — the data is already
  available where the surface is constructed; it was simply
  dropped on the floor.

- **RF-4** — **Live confirmation that the current structured
  return drops the hash.** Sampled
  `search(query="plan match confidence threshold FTS5 sentinel",
  corpus="rdr", limit=3, structured=True)` against prod on
  2026-04-17. Returned payload:
  `{"ids":["e21392fe9c74061f_4", …], "tumblers":["","",""],
  "distances":[…], "collections":["rdr__nexus-571b8edd"]}`. No
  `chunk_text_hash` key. Tumblers also empty — chunks carry no
  tumbler directly (tumblers address documents in the catalog
  JSONL, chunks are implicit sub-addresses), so the empty-list
  there is semantically correct and not part of this RDR's scope.

- **RF-5 (resolved 2026-04-17)** — Direct-T3 sampling was
  initially blocked: `T3Database()` bare constructor hit
  `chromadb.errors.ChromaError: Permission denied` because empty
  credential args were forwarded to `CloudClient` without the
  config fallback that the `make_t3()` factory applies. Fixed in
  `src/nexus/db/t3.py:193` — the constructor now falls back to
  `nexus.config.get_credential()` for empty args when a client
  isn't injected and local mode isn't set. Tests in
  `tests/test_t3.py::test_bare_constructor_falls_back_to_get_credential`
  and three sibling cases pin the behaviour (explicit args still
  win, `_client=` injection skips fallback, local mode skips
  fallback).

  With the fix in place, sampled 10 chunks across one collection
  per prefix (code, docs, rdr, knowledge, taxonomy):

  | Collection prefix | chunk_text_hash present | non-empty |
  |---|---|---|
  | `code__` | 10/10 | 10/10 |
  | `docs__` | 10/10 | 10/10 |
  | `rdr__`  | 10/10 | 10/10 |
  | `knowledge__` | 1/1  | 1/1  |
  | `taxonomy__centroids` | 0/10 | 0/10 |

  All citable-content prefixes carry the 64-char SHA-256 hex on
  every chunk. `taxonomy__centroids` correctly has no hash — those
  rows are topic centroids, not indexed chunks, and are out of
  scope for `chash:` citations. Design assumption fully confirmed
  against prod.

- **RF-6 (2026-04-17)** — Order-of-magnitude measurement of the
  ChromaDB-filter-vs-T2-JOIN asymmetry. Sampled a single
  `chunk_text_hash` lookup across 20 representative prod
  collections via `col.get(where={"chunk_text_hash": <hex>})`:
  mean ~300ms per collection (289ms observed; cold-cache and
  first-call effects in the noise), 5.78s total for 20 serial
  calls. Extrapolation for a 20-citation doc scanned across the
  136 prod collections is on the order of minutes serial, tens
  of seconds at 10× parallelism — either way an order of
  magnitude too slow for an interactive author surface. A T2
  SQLite JOIN on a `chash_index` table is ~50µs per lookup,
  well under the interactive budget.

  The numeric projection ("~13 minutes") is order-of-magnitude,
  not a precise latency — ChromaDB Cloud cold-cache and network
  variance make the exact figure noisy. The conclusion
  (a T2 speedup layer is required, not optional) is insensitive
  to the variance. **Critical Assumption 2 verified.**

- **RF-7 (architecture, 2026-04-17)** — Chash stability across
  reindex events (Critical Assumption 3) is guaranteed by
  construction, not by observation. `chunk_text_hash` is
  `sha256(chunk.text.encode()).hexdigest()` at the write sites
  (RF-2). For the hash to change across a reindex, the chunk
  text would have to change, which requires one of: (a) a change
  to the chunker's splitting logic, (b) a change to text
  normalization, (c) a change in the source file. (a) and (b)
  are architectural changes tracked by RDR-053's stability
  mandate and would themselves require a dedicated hash-migration
  plan. (c) is the normal case — the hash *should* change when
  the source text changes, and downstream `check-grounding`
  would correctly report the old chash as unresolved. **Critical
  Assumption 3 verified by architecture** (RDR-053 §chunk-boundary
  stability); no empirical reindex comparison needed.

- **RF-8 (design note, 2026-04-17)** — `nx_answer` envelope
  shape. RF-3 claimed the `nx_answer` envelope can surface
  `chunk_text_hash` alongside `search` and `query`. On closer
  read, `nx_answer` currently returns `str` (a rendered
  final answer). Surfacing chash through it requires either:
  (a) a new `structured=True` kwarg (precedent: `trace=True`)
  that returns `{final_text, chunks: [{id, chash, …}], …}`,
  or (b) deferring `nx_answer` plumbing and relying on
  `search(structured=True)` / `query(structured=True)` as the
  authoring surfaces. Recommend (a): the kwarg pattern is
  precedented, callers opt in, default behaviour unchanged.
  Open question to resolve before Phase 3 starts: `nx_answer`
  composes multi-step plans — surface chash from all retrieval
  steps, only the final step, or only when the final step is
  itself a retrieval op? The structured envelope shape depends
  on this call.

- **RF-9 (gate discovery, 2026-04-17)** — **Significant existing
  infrastructure.** Gate layer-3 surfaced that two pieces of the
  originally-proposed build are already shipped:

  1. **`Catalog.resolve_span(span, physical_collection, t3)`** at
     `src/nexus/catalog/catalog.py:575` — takes a `chash:<hex>` span
     (also supports `chash:<hex>:<start>-<end>` char-range spans)
     and returns `{chunk_text, metadata, chunk_hash}`. It is
     collection-scoped: the caller must already know the physical
     collection. For the reverse-direction primitive this RDR
     proposed, the collection-scoped half is done; the missing
     piece is a **global** variant that scans all collections (or
     hits the T2 speedup index, once built).

  2. **`_backfill_chunk_text_hash(col)`** at
     `src/nexus/commands/collection.py:307` plus the
     `nx collection backfill-hash [--all]` CLI subcommand. It
     reads each chunk's stored text and writes `chunk_text_hash`
     when missing, skipping already-hashed rows. The T2
     `chash_index` bootstrap doesn't need a new command — the
     same iteration can write both destinations.

  3. **Call-site confirmation**: `catalog.py:1287` and `:1425`
     already invoke `col.get(where={"chunk_text_hash": ...})`
     for span resolution, so the ChromaDB-side metadata filter
     is a proven path.

  **Implication**: Phase 1 shrinks from "build backfill command
  + instrument six indexers" to "add T2 dual-write at the six
  existing sites + reuse the existing backfill loop to populate
  T2 for historical chunks." Phase 2 shrinks from "build
  resolve_chash from scratch" to "extend `resolve_span` with a
  collection-agnostic variant that defers to the T2 index."

### Critical Assumptions

- [x] `chash` is populated on every chunk in every indexing path —
  **Status**: Verified at write-site + live sample 2026-04-17
  (RF-2, RF-5). **Six** indexing paths all write the per-chunk
  SHA-256 under the metadata key `chunk_text_hash`: `code_indexer.py:371`,
  `doc_indexer.py:591` + `:666`, `prose_indexer.py:96` + `:141`,
  `pipeline_stages.py:163`. Prod-T3 live sample (10 chunks per
  prefix) confirms 10/10 present on every citable-content prefix.
- [x] A T2 `chash_index` table is the right primitive vs. relying on
  ChromaDB metadata filter — **Status**: Verified 2026-04-17 (RF-6).
  Order-of-magnitude measurement: ChromaDB metadata filter
  ~300ms/call; extrapolation to a 20-citation doc across the 136
  prod collections is on the order of minutes serial, tens of
  seconds even at 10× parallelism. T2 SQLite JOIN is ~50µs per
  lookup. The speedup layer is required for interactive author
  latency, not an optimisation.
- [x] Chash values are stable across the re-indexing events observed
  in the nexus + ART corpora over the last 30 days — **Status**:
  Verified by architecture 2026-04-17 (RF-7). `chunk_text_hash` is
  `sha256(chunk.text)` at the write sites; stability follows from
  RDR-053's chunk-boundary-stability mandate. A chunker or
  normalisation change would itself be a hash-migration event
  requiring a dedicated plan.

## Proposed Solution

### Approach

Four cooperating surfaces — one primitive, two retrieval surface
changes, one convenience command:

1. **T2 `chash_index` table**, populated at the six existing
   indexing write sites (code, docs batch, PDF batch, CCE
   markdown, CCE single-chunk, streaming PDF — see RF-2).
   Schema: `(chash TEXT PRIMARY KEY, physical_collection TEXT,
   doc_id TEXT, created_at TEXT)`.

2. **`catalog.resolve_chash(chash) -> ChunkRef | None`** (reverse
   direction) that consults the T2 index first, falls back to a T3
   metadata filter when the index is empty (fresh install), returns
   `None` on miss.

3. **`chunk_text_hash` on structured retrieval returns** (forward
   direction) — added to `search(structured=True)`,
   `query(structured=True)`, and the `nx_answer` envelope. Agents
   and authors that `search` for a claim can now read the citable
   hash straight off the payload instead of descending to ChromaDB.

4. **`nx doc cite "<claim>" --against <collection>`** (convenience
   CLI) that composes `search(limit=1, structured=True)` with the
   hash surfaced in #3 and emits a ready-to-paste
   `[<display>](chash:<hex>)` markdown link. `--json` returns the
   structured payload for pipeline scripting.

Downstream consumers unblocked by this work (RDR-083):

- `nx doc check-grounding --fail-ungrounded` ships.
- `nx doc check-extensions` replaces its chash-as-doc-id proxy with
  the real catalog `doc_id` via `resolve_chash`. The WARNING and
  `[experimental]` marking come off.
- `nx doc render --expand-citations` renders footnote blocks with
  the resolved chunk text.

### Technical Design

(Deferred — pseudocode omitted until the Investigation items are
verified. The interface is small; the uncertainty is in the
indexing-path instrumentation.)

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Per-collection chash→chunk lookup | `src/nexus/catalog/catalog.py:575` `resolve_span(span, physical_collection, t3)` | **Reuse** — already handles `chash:<hex>` and `chash:<hex>:<start>-<end>` spans |
| Global chash→chunk lookup | none | **Add** — `Catalog.resolve_chash(chash: str) -> ChunkRef | None`; consults T2 index; falls back to iterating collections when the T2 index is empty |
| T2 `chash_index` table + migration | `src/nexus/db/migrations.py` | **Add** — schema `(chash TEXT PRIMARY KEY, physical_collection TEXT, doc_id TEXT, created_at TEXT)`; indexed on `chash` (primary) and `physical_collection` (for collection-delete cleanup) |
| Indexing dual-write | `code_indexer.py:371`, `doc_indexer.py:591` + `:666`, `prose_indexer.py:96` + `:141`, `pipeline_stages.py:163` | **Extend** — after the existing ChromaDB upsert at each of the six sites, insert-or-replace into the T2 `chash_index`. Best-effort: a T2 failure logs and continues; backfill recovers |
| Backfill loop | `src/nexus/commands/collection.py:307` `_backfill_chunk_text_hash()` | **Extend** — the same per-chunk iteration that writes `chunk_text_hash` to T3 also writes `(chash, collection, doc_id)` to T2 if missing. One pass, two destinations |
| Backfill CLI | `nx collection backfill-hash [--all]` | **Reuse** — no new command; the extended loop above backfills T2 whenever a caller runs the existing command |
| `search(structured=True)` payload | `src/nexus/mcp/core.py:168-174` | **Extend** — add `"chunk_text_hash": [r.metadata.get("chunk_text_hash", "") for r in page]` to the returned dict (metadata already in scope) |
| `query(structured=True)` payload | `src/nexus/mcp/core.py` `query` tool structured branch | **Extend** — surface the representative-chunk hash per document result |
| `nx_answer` envelope | `src/nexus/mcp/core.py:nx_answer` | **Extend with opt-in** — new `structured=True` kwarg (precedent: `trace=True`) returns `{final_text, chunks: [{id, chash, …}], …}`; default `structured=False` preserves the current `str` return |
| `nx doc cite` command | `src/nexus/commands/doc.py` (RDR-082 group) | **Add** — new subcommand composing `search(limit=N, structured=True)` + `--min-similarity` gate + markdown link emission |
| `nx collection delete` cleanup | `src/nexus/commands/collection.py` delete path (nexus-lub cascade) | **Extend** — add `chash_index WHERE physical_collection = ?` to the cascade so deleted collections don't leave orphan hashes pointing at gone chunks |

### Decision Rationale

T2 index over pure-ChromaDB filter: one JOIN replaces
O(N_collections) scans and is ~10× faster at the measured corpus
scale. The index is best-effort reconstructable from T3 ground
truth via the backfill command.

## Alternatives Considered

### Alternative 1: Query ChromaDB per collection at call time

**Pros**: No new table, no migration, no backfill.
**Cons**: O(N_collections) per chash. `check-extensions` on a
20-citation doc would hit rate limits on Cloud.

### Alternative 2: Fold chash resolution into the existing catalog JSONL

**Pros**: JSONL is the catalog source of truth.
**Cons**: JSONL is document-level, not chunk-level; the addition
would balloon file size. The catalog's SQLite cache is the right
place.

## Trade-offs

### Consequences

- New T2 table + migration.
- Indexing paths gain one SQLite write per chunk (bounded cost).
- `check-grounding` and `check-extensions` become meaningful; the
  `[experimental]` marking comes off.

### Risks and Mitigations

- **Risk**: Backfill on a large corpus (>1M chunks) is slow.
  **Mitigation**: `--resume-from` flag; chunked commits.
- **Risk**: Chash collision across collections (two collections
  index the same document, same chash).
  **Mitigation**: Primary key is `(chash, physical_collection)` —
  collisions across collections are allowed and represented.

### Failure Modes

- **Empty T2 index (fresh install)**: `resolve_chash` detects zero
  rows for the target chash and falls back to iterating
  collections with the ChromaDB metadata filter. Slow (tens of
  seconds worst case) but correct. A one-line `_log.warning` at
  first fallback tells the operator to run `nx collection
  backfill-hash --all`.

- **Indexing pipeline crashes mid-batch** (T3 upsert committed, T2
  write did not): next run's idempotent `INSERT OR REPLACE` into
  `chash_index` corrects. If the process exits permanently mid-
  batch, `nx collection backfill-hash` reconciles via the
  existing loop.

- **T2 write fails while T3 succeeds**: logged at warning level;
  the chunk is usable via the slow ChromaDB fallback until
  backfill runs. Not a correctness issue.

- **T3 upsert fails while T2 was already written** (e.g., during
  a retry cycle where the T2 row was created speculatively):
  `resolve_chash` returns a `ChunkRef` whose `physical_collection`
  still exists but `doc_id` does not — the `get_collection.get()`
  call returns empty. Phase 2's resolver treats this as a miss
  and deletes the stale T2 row ("self-healing read"). No
  invariant violated.

- **`nx collection delete` race**: the cascade extension in
  Phase 1 removes `chash_index` rows for the deleted collection
  before returning. If another process reads `chash_index`
  between the Chroma delete and the T2 delete, it gets a stale
  row; the self-healing read above repairs it on the next
  request.

- **Backfill on 1M+ chunks**: the existing
  `_backfill_chunk_text_hash` loop already paginates at
  `_BACKFILL_BATCH`; adding T2 writes stays within the same
  batching. No special handling required — the existing loop's
  per-batch commit is the recovery unit.

- **Chunk text re-chunked under a new boundary (architectural
  change to chunker)**: RDR-053 forbids this without a migration
  plan. If one happens anyway, stale T2 rows point at chunks
  whose `chunk_text_hash` no longer matches. The self-healing
  read catches each on access; a full `nx collection
  backfill-hash --all` repopulates.

## Implementation Plan

### Prerequisites

- [ ] RDR-083 shipped (dependency for the consumers).
- [ ] RF items Verify-1 and Verify-2 resolved.

### Minimum Viable Validation

1. Index a small corpus with the instrumentation.
2. Confirm `resolve_chash` returns the expected chunk via both
   paths (T2 index and ChromaDB fallback) for a sampled hash.
3. Run `nx doc check-grounding --fail-ungrounded` on a doc with one
   real chash and one made-up chash — verify exit 1, correct error
   locations.
4. Run `search(structured=True)` on a real corpus — verify
   `chunk_text_hash` appears in the returned dict with length
   matching `ids`, and every hash round-trips through
   `resolve_chash` to its owning chunk.
5. Run `nx doc cite "<a real claim>" --against docs__art-grossberg-papers`
   and confirm the emitted markdown link has a resolvable chash
   (feed the output through `nx doc check-grounding` with
   `--fail-ungrounded`; expect exit 0).

### Phase 1: T2 `chash_index` + dual-write at the six existing sites

- Add migration for `chash_index` table
  (`chash TEXT PRIMARY KEY, physical_collection TEXT, doc_id TEXT,
  created_at TEXT`).
- At each of the six write sites listed in RF-2, after the existing
  ChromaDB upsert, insert-or-replace into `chash_index`. Failure
  to write T2 is best-effort — log and continue; the backfill
  loop reconciles.
- Extend `_backfill_chunk_text_hash()` at `commands/collection.py:307`
  so the same per-chunk iteration that populates `chunk_text_hash`
  in T3 also populates the T2 row. No new CLI — reuse
  `nx collection backfill-hash [--all]`.
- Extend `nx collection delete` (the nexus-lub cascade) with
  `DELETE FROM chash_index WHERE physical_collection = ?` so
  deleting a collection doesn't leave orphan hashes pointing at
  gone chunks.

### Phase 2: Global `resolve_chash` (extends existing `resolve_span`)

- Add `Catalog.resolve_chash(chash: str) -> ChunkRef | None` —
  collection-agnostic. Lookup path:
  1. `SELECT physical_collection, doc_id FROM chash_index WHERE chash = ?`.
  2. On hit, validate the target collection still exists (guards
     against orphan rows from race with collection delete).
  3. Delegate to the existing per-collection `resolve_span` to
     fetch chunk text + metadata.
  4. On T2 miss (fresh install, backfill not yet run): iterate
     collections calling `col.get(where={"chunk_text_hash":
     chash})` — slow fallback, logs once per process.
- Unit tests with fixture T2 (rows + deleted collection) + fixture T3.

### Phase 3: Surface `chunk_text_hash` on structured retrieval returns

- **`search(structured=True)`** at `src/nexus/mcp/core.py:168-174` —
  add `"chunk_text_hash": [r.metadata.get("chunk_text_hash", "")
  for r in page]` to the returned dict. One-line change; metadata
  is already in scope.
- **`query(structured=True)`** — same addition to the document-
  result builder.
- **`nx_answer(structured=True)`** (new kwarg, default False) —
  return `{final_text, chunks: [{id, chash, collection, distance,
  text}], plan_id, step_count}` instead of the current `str`. The
  question "which step's chash" resolves as: **all retrieval-op
  steps contribute their top chunks to a single merged `chunks`
  list, ordered by final-step relevance**; non-retrieval steps
  (summarize, generate) contribute nothing to `chunks`.
- Unit + integration tests: every surfaced hash round-trips
  through `resolve_chash`.

### Phase 4: RDR-083 consumer wiring (precise spec)

- **`check-grounding` gains `--fail-ungrounded`** — exit non-zero
  when any `chash:` span in the input doc fails `resolve_chash`.
- **`check-extensions` fix** — **the change is in the caller, not
  in `chunk_grounded_in`**. Current flow (RDR-083 v1) passes the
  raw chash hex value as `doc_id` to
  `CatalogTaxonomy.chunk_grounded_in(doc_id, source_collection,
  threshold)`. The fix: call `Catalog.resolve_chash(chash)` first,
  extract `ChunkRef.doc_id`, pass the resolved doc_id to
  `chunk_grounded_in` **unchanged**. The `chunk_grounded_in`
  method's signature and semantics do NOT change — it still
  accepts a ChromaDB-scoped `doc_id: str`. This preserves every
  existing caller.
- **Remove the `[experimental]` marker and the "all inputs returned
  no_data" WARNING** on `check-extensions` once the resolver is
  populated.
- **`nx doc render --expand-citations`** — new flag that resolves
  every `chash:` span via `resolve_chash` and emits a footnote
  block with the chunk text.

### Phase 5: `nx doc cite` authoring CLI

- `nx doc cite "<claim>" --against <collection> [--limit N]
  [--min-similarity F] [--json]`.
- Flow: `search(query=claim, corpus=collection, limit=N,
  structured=True)` → reads the new `chunk_text_hash` field →
  emits markdown.
- **Default stdout shape**: `[<first 60 chars of matched chunk>](chash:<hex>)`.
  Exits non-zero when the top result's `distance` exceeds the
  similarity threshold (`--min-similarity` default 0.30; lower
  = more strict since we compare raw cosine distance, not
  similarity).
- **`--json` schema**: `{"candidates": [{"chash": str, "distance":
  float, "collection": str, "chunk_excerpt": str (first 200 chars),
  "markdown_link": str}], "query": str, "threshold_met": bool}`.
- **Failure modes specified**:
  - Empty collection → exit 2 with "no indexed content in
    <collection>".
  - Top result above threshold → exit 1, print warning to
    stderr, emit nothing to stdout (lets shell pipelines
    `nx doc cite "..." --against X > cite.md` fail loud).
  - Multiple candidates tied within 0.01 distance → `--json`
    returns all tied candidates; default stdout picks the first
    and notes `# N candidates tied (see --json)`.
- Docs + live smoke against `docs__art-grossberg-papers`.

## References

- RDR-053 (Xanadu fidelity — chunk hash stability)
- RDR-075 (cross-collection projection)
- RDR-078 (plan-centric retrieval — `resolve_span` call site)
- RDR-082 (doc render / `nx doc` command group)
- RDR-083 (corpus-evidence tokens — the consumer of this RDR)

## Revision History

- 2026-04-16 — Draft authored to own the deferred span-resolution
  work registered in RDR-083 §v1 Scope Reduction. The deferrals
  (resolve_chash, chash → doc-id mapping, `--fail-ungrounded`,
  `--expand-citations`) all depend on the single primitive
  specified here.
- 2026-04-17 — Scope expanded after ART-instance feedback (RF-1):
  authors today cannot obtain chash values through any nexus CLI
  or MCP surface, forcing direct ChromaDB-API use. Added Gap 5
  (no forward path from retrieval to chash), Gap 6 (no "cite
  this" CLI), and corresponding Phase 3 (`chunk_text_hash` in
  structured returns) and Phase 5 (`nx doc cite`) to the
  implementation plan. Title retyped "Chash Span Resolution" →
  "Chash Span Surface" to reflect that this RDR owns the
  primitive end-to-end (authoring + verification), not just
  reverse resolution.
- 2026-04-17 — **Gate layer-3 BLOCKED** (3 Critical, 2 Significant,
  3 Observations). Rewrite applied:
  - RF-2 corrected: six write sites (not three) —
    `prose_indexer.py:96` + `:141` and `pipeline_stages.py:163`
    were missed; `pdf_chunker.py` does not write the hash.
  - RF-9 added: significant existing infrastructure surfaced —
    `Catalog.resolve_span` at `catalog.py:575` is a shipped
    per-collection chash lookup; `_backfill_chunk_text_hash` at
    `commands/collection.py:307` + `nx collection backfill-hash`
    is a shipped backfill command.
  - RF-6 wording downgraded from precise "~13 min" to "order of
    magnitude" — conclusion (T2 speedup required) unchanged.
  - Framing reset from "ship the primitive end-to-end" to
    **extend + speed + surface + compose**. Phase 1 shrinks to
    dual-write at six sites + backfill-loop extension + delete
    cleanup. Phase 2 shrinks to "extend `resolve_span` with a
    global variant."
  - Phase 4 spec made precise: the `check-extensions` fix is
    in the caller (resolve chash → extract doc_id → call
    existing `chunk_grounded_in` with the resolved doc_id).
    `chunk_grounded_in`'s contract does NOT change.
  - Failure-modes section expanded with dual-write race,
    self-healing read, collection-delete cascade, backfill cost.
  - Phase 5 (`nx doc cite`) gained `--min-similarity`,
    failure-mode specs, and explicit `--json` schema.
  - Existing Infrastructure Audit table rebuilt to distinguish
    **Reuse** / **Extend** / **Add** per component.
