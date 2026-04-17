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
not ship the machinery that makes those citations *verifiable*. The
2026-04-17 ART-instance feedback then surfaced a deeper problem: it
also didn't ship the machinery to *author* them. An author or agent
that wants to cite a specific chunk has no CLI or MCP path to obtain
the chash — they have to drop to the raw ChromaDB API, run
`collection.get(include=["metadatas"])`, and copy the `chash` value by
hand. Every `search`, `query`, and `nx_answer` structured return omits
the chunk hash. That makes the whole `chash:` citation primitive
dead-on-arrival for typical Tier-3 grounding workflows.

This RDR owns the chash primitive surface end-to-end — authoring
and verification are mirror workflows over the same T2 index. The
original draft (2026-04-16) covered only the reverse direction
(resolve hash → chunk). This revision widens scope to the forward
direction (surface chunk hashes through normal retrieval) and the
convenience CLI that composes them.

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

### Investigation (to be completed during drafting)

- **Verify** — does every indexing path (`code_indexer.py`,
  `doc_indexer.py`, `pdf_chunker.py`) actually write `chash` to
  ChromaDB metadata? Sampling: query an existing `rdr__` and
  `knowledge__` collection via `collection.get(limit=5, include=["metadatas"])`
  and confirm the `chash` key is present.
- **Verify** — what fraction of chunks in a typical corpus have a
  `chash` value vs. missing? If gappy, indexing backfill is a
  prerequisite.
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

### Critical Assumptions

- [ ] `chash` is populated on every chunk in every indexing path —
  **Status**: Unverified — **Method**: Sampling against live
  collections.
- [ ] A T2 `chash_index` table is the right primitive vs. relying on
  ChromaDB metadata filter — **Status**: Unverified — **Method**:
  Measure ChromaDB filter cost on a populated collection at 100k
  chunks; compare to SQLite JOIN.
- [ ] Chash values are stable across the re-indexing events observed
  in the nexus + ART corpora over the last 30 days — **Status**:
  Unverified — **Method**: Compare `chash` for a sampled chunk
  before/after a scheduled reindex.

## Proposed Solution

### Approach

Four cooperating surfaces — one primitive, two retrieval surface
changes, one convenience command:

1. **T2 `chash_index` table**, populated at indexing time by each
   pipeline (`code_indexer`, `doc_indexer`, `pdf_chunker`).
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
| Chash → chunk lookup | `src/nexus/catalog/catalog.py:resolve_span` | **Extend** — add a collection-agnostic `resolve_chash` method that delegates to a T2 index |
| T2 migration | `src/nexus/db/migrations.py` | **Add** — new migration for `chash_index` table |
| Indexing instrumentation | `src/nexus/code_indexer.py`, `src/nexus/doc_indexer.py`, `src/nexus/pdf_chunker.py` | **Extend** — write to `chash_index` on each chunk upsert |
| Backfill command | `src/nexus/commands/catalog.py` | **Extend** — `nx catalog rebuild-chash-index` walks existing T3 collections and populates T2 |
| `search(structured=True)` payload | `src/nexus/mcp/core.py` search tool | **Extend** — add `chunk_text_hash` (list aligned with `ids`) to the returned dict |
| `query(structured=True)` payload | `src/nexus/mcp/core.py` query tool | **Extend** — surface the representative-chunk hash on each document result |
| `nx_answer` envelope | `src/nexus/mcp/core.py:nx_answer` | **Extend** — when a plan's final step is a retrieval op, include the top chunks' hashes in the envelope so the caller can cite without re-querying |
| `nx doc cite` command | `src/nexus/commands/doc.py` (RDR-082 group) | **Add** — new subcommand composing `search(limit=1, structured=True)` + markdown link emission |

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

- Empty T2 index (fresh install): `resolve_chash` falls back to
  ChromaDB metadata filter; first-call latency is higher until
  backfill runs.
- Indexing pipeline crashes mid-write: next run's idempotent upsert
  corrects.

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

### Phase 1: Indexing instrumentation

- Add `chash_index` table migration.
- Extend the three indexers to upsert.
- Backfill command.

### Phase 2: Catalog resolver (reverse direction)

- `resolve_chash(chash) -> ChunkRef | None` method.
- Unit tests with fixture T2 + fixture T3.

### Phase 3: Retrieval surface (forward direction)

- Add `chunk_text_hash` to `search(structured=True)` payload.
- Add the same to `query(structured=True)` document results.
- Add the same to the `nx_answer` envelope when the final step is
  a retrieval op.
- Unit + integration tests: verify every surfaced hash resolves.

### Phase 4: RDR-083 consumer wiring

- `check-grounding` gains `--fail-ungrounded`.
- `check-extensions` replaces the chash-as-doc-id proxy with real
  resolved catalog doc_id.
- Remove `[experimental]` marker and WARNING path when the resolver
  is populated.
- `nx doc render --expand-citations` ships.

### Phase 5: Authoring CLI

- `nx doc cite "<claim>" --against <collection> [--limit N] [--json]`.
- Default output: `[<first 60 chars of chunk>](chash:<hex>)`.
- `--json`: structured payload with the top-N candidate chunks +
  hashes so pipeline scripts can pick a specific one.
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
