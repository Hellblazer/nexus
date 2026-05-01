# RDR-101 Phase 0: chunk_id Generation Rule

Bead: nexus-o6aa.4

Status: Phase 0 deliverable, not yet folded back into RDR-101.

## Decision

**Option A: UUID7 generated fresh at `ChunkIndexed` event time.**

The `chunk_id` value written as the Chroma natural ID (`add(ids=...)`, `upsert(ids=...)`) is a stdlib-generated UUID7 (`uuid.uuid7()` on Python 3.14+, `uuid7-standard` on 3.13). The value is opaque, embedded-timestamp sortable, and decoupled from both `doc_id` and `position`. No projector logic depends on reproducing `chunk_id` from any other field.

This binds the hedge in RDR-101 §Read paths step 4 (`UUID7 or deterministic digest; Phase 0 picks one`) and §Phase 0 acceptance task (`chunk_id generation rule`).

## Background: today's chunk_id rule

Three production sites construct `chunk_id` today, all using the same deterministic rule:

```
chunk_id = f"{content_hash[:16]}_{chunk_index}"
```

- `src/nexus/doc_indexer.py:719` (PDF chunks)
- `src/nexus/doc_indexer.py:799` (markdown / docling chunks)
- `src/nexus/pipeline_stages.py:206` (streaming pipeline write loop)

The id is then passed into `T3Database.upsert_chunks_with_embeddings(...)` as `ids=` (`src/nexus/indexer.py:797`), which fans out to `col.upsert(ids=chunk_ids, ...)` at `src/nexus/db/t3.py:432-439`. The downstream dual-write helper `dual_write_chash_index(...)` at `src/nexus/db/t2/chash_index.py:267-311` re-uses the same string as the value stored in `chash_index.doc_id` (per RDR-086 the column was named `doc_id` to mean "Chroma chunk identifier"; this is the naming collision that RDR-101 Phase 0 already calls out separately).

The current rule is therefore an instance of option B: a deterministic digest of `(file_content_hash, chunk_index)`. RDR-101's question is whether to keep that scheme (re-keyed to the new `(doc_id, position)` pair) or move to opaque UUID7s.

## Position-numbering invariant analysis

Option B's recovery property requires `(doc_id, position)` to be reproducible from a re-walk of the source. The current chunker does not guarantee this. Concrete instability vectors observed in the chunker code:

1. **AST byte-cap split renumbers everything.** `src/nexus/chunker.py:163-211` (`_enforce_byte_cap`) walks the AST splitter's output, splits any single node that exceeds `_CHUNK_MAX_BYTES`, and renumbers `chunk_index` and `chunk_count` across the whole list (`chunker.py:208-210`). Adding or removing a long function body upstream therefore shifts every later `chunk_index`. Position 7 in version A is not position 7 in version B even when the local content is unchanged.
2. **Line-based fallback is index-sensitive.** `_line_chunk` at `chunker.py:105` produces `(line_start, line_end, text)` triples numbered 0..N-1 with `_CHUNK_LINES=150`. Inserting a 50-line block at the top of a file shifts every subsequent line range AND renumbers the chunks. Recovery from `(doc_id, position)` only works against the version of the file that produced those positions.
3. **Config drift.** `_CHUNK_LINES` is a module constant, `_CHUNK_MAX_BYTES` is sourced from `chroma_quotas.SAFE_CHUNK_BYTES`, `_OVERLAP=0.15` is in-line. Any of these values changing across releases changes the position numbering for the same input. Per `git log` cadence (multiple chunker tweaks over the past 60 days), this is not theoretical.
4. **AST splitter library upgrades.** `tree-sitter-language-pack` and `llama_index.core.node_parser.CodeSplitter` both produce nodes whose count and boundaries can shift with version upgrades. The current chunker's `_make_code_splitter` (`chunker.py:25-50`) treats their output as ground truth.
5. **Markdown chunker has its own positional state machine.** `src/nexus/md_chunker.py:265-415` walks sections, increments `chunk_index` linearly, and falls back to `_split_large_section` which re-numbers from `start_index`. Slight content edits to a section header restructure the section list and renumber everything that follows.

In summary: today's chunker is index-stable only if the input bytes are byte-for-byte identical AND the chunker config + dependencies are pinned. Both invariants are routinely broken by ordinary development. Option B's reproducibility property therefore does not survive a real production lifecycle for this codebase.

A second-order point: today's rule already includes `content_hash[:16]` as the first half, which IS sensitive to any content edit. So today's "deterministic" id is reproducible only against an unmodified file. Any edit recomputes the file-level `content_hash` and re-keys every chunk anyway. We do not actually have an option-B recovery property today; we have an option-B-shaped string whose reproducibility is identical in practice to a fresh opaque id.

## Phase 1 synthesis impact

RDR-101 §Migration Phase 1 walks T3 and emits `ChunkIndexed` events for existing chunks. Phase 2 backfills `doc_id` metadata onto those existing Chroma rows via `ChromaDB.update()`. The synthesis path needs a `chunk_id` value to put into the event; the natural choice is the existing Chroma natural ID (already present as the row PK in T3), copied verbatim.

This works for option A. UUID7 is the rule for **new** writes only; legacy chunks keep their existing `f"{content_hash[:16]}_{chunk_index}"` strings as their `chunk_id` because those strings are already the Chroma natural ID and Chroma treats them as opaque. The corpus becomes heterogeneous (legacy chunks have structured ids, new chunks have UUID7s) but that heterogeneity is invisible to every consumer because no consumer parses the id; everyone joins by `(chunk_id, doc_id)` going forward. RDR-086's `chash_index.doc_id` column (per its current schema) continues to hold whatever string was used as the Chroma id, regardless of which generation rule produced it.

This does not work for option B in its strict form. Option B would require the synthesized `chunk_id` to be `digest(doc_id, position)` where `doc_id` is the freshly minted UUID7 and `position` is the `chunk_index` from the existing T3 metadata. But the existing Chroma natural ID is `f"{content_hash[:16]}_{chunk_index}"`, which does not match `digest(uuid7_doc_id, chunk_index)`. To make option B coherent, every existing chunk would have to be **re-added** under a new id, doubling Chroma storage during the transition (or losing chunk_text continuity if the old rows were deleted before the new rows were verified). For a 98K-chunk live host catalog, this is a one-time but expensive migration with no offsetting recovery benefit (per the position-numbering analysis above, recovery is unreliable anyway).

The conservative choice: option A keeps existing Chroma natural ids as-is, applies UUID7 only to new `ChunkIndexed` events, and pays zero migration cost on the chunk side.

## Why this matters for the projector

RDR-101's projector is a deterministic function `events → SQLite state`. The key lookup is `chunks.chunk_id PK`. Option A makes `chunk_id` a pure surrogate: the projector inserts whatever `chunk_id` value the event carries, with no ability and no need to derive it from `doc_id` or `position`. The projector logic is one column copy.

Option B forces the projector either to (a) trust the event-carried `chunk_id` (in which case option B reduces to option A with a more rigid generator) or (b) recompute `chunk_id` from `(doc_id, position)` at projection time. Path (b) introduces a dependency on the chunker config matching the config that produced the original event, which the projector has no way to verify and which the position-numbering analysis above proves is fragile.

## Test gate

Two tests pin the rule. Both belong in Phase 1 alongside `events.py`.

1. **chunk_id is opaque to projector.** `tests/catalog/test_event_log_projector.py::test_chunk_id_carried_verbatim`. Emit a `ChunkIndexed` event with `chunk_id = "arbitrary-string-not-uuid"`; project; assert `SELECT chunk_id FROM chunks WHERE doc_id = ?` returns `"arbitrary-string-not-uuid"`. The projector must not parse, validate, or recompute the id. This guards against drift toward option B over time.
2. **New writes generate UUID7.** `tests/indexer/test_chunk_id_generation.py::test_new_writes_use_uuid7`. Index a fixture file; harvest the Chroma natural ids written; assert each parses as a valid UUIDv7 (`uuid.UUID(s).version == 7`). This guards against new code paths slipping back into the `f"{content_hash[:16]}_{chunk_index}"` shape during Phase 3.

A non-test invariant to flag in the projector module docstring: "chunk_id is a surrogate id chosen by the indexer; the projector copies it verbatim into the SQLite Chunk row. Do not attempt to reconstruct chunk_id from any other field."

## Candidate RDR-101 amendment

Replace the parenthetical hedge in §Read paths / Index a file step 4 with the binding rule. Block-quoted markdown patch:

> **§Read paths / Index a file step 4** (current text):
>
> > 4. Embed chunks; T3 write per chunk: `add(id=chunk_id, embedding=..., metadata={chunk_id, doc_id, coll_id, position, chash, content_hash})` where `chunk_id` is freshly generated at `ChunkIndexed` time (UUID7 or a deterministic `(doc_id, position)` digest; Phase 0 survey picks one). Critical: `id=chunk_id`, NOT `id=chash`. chash is non-unique across documents (C1) and would collide on identical content; chunk_id is the per-row Chroma natural ID.
>
> **Replace with**:
>
> > 4. Embed chunks; T3 write per chunk: `add(id=chunk_id, embedding=..., metadata={chunk_id, doc_id, coll_id, position, chash, content_hash})` where `chunk_id` is a fresh **UUID7** (`uuid.uuid7()` on Python 3.14+, `uuid7-standard` package on 3.13) generated at `ChunkIndexed` event-emission time. The id is opaque, embedded-timestamp sortable, and decoupled from `doc_id` and `position`. The projector copies `chunk_id` verbatim into the `Chunk` row; it does not parse or reconstruct the id. Critical: `id=chunk_id`, NOT `id=chash`. chash is non-unique across documents (C1) and would collide on identical content; chunk_id is the per-row Chroma natural ID. Migration: existing T3 rows keep their pre-RDR-101 Chroma natural ids verbatim (Phase 1 synthesis copies them into `ChunkIndexed` events; no chunk re-add is required). The corpus is heterogeneous post-migration (legacy structured ids + new UUID7s) but consumers join by id equality, so the heterogeneity is invisible. Rationale for rejecting the deterministic-digest alternative: the chunker's `chunk_index` numbering is unstable across content edits, AST byte-cap splits, chunk-config drift, and library upgrades (Phase 0 deliverable `docs/rdr/post-mortem/rdr-101-chunk-id-rule.md`); a `digest(doc_id, position)` rule would not survive ordinary development and would force a 98K-chunk re-add at migration time with zero offsetting recovery property.

A parallel one-line amendment to §Phase 0 acceptance task:

> **§Phase 0 acceptance task** (current text):
>
> > **`chunk_id` generation rule.** Decide whether `chunk_id` is a fresh UUID7 at `ChunkIndexed` time (opaque, decoupled from content) or a deterministic digest of `(doc_id, position)` (reproducible across re-runs, encodes structure). UUID7 lean for parallel with `doc_id` design; deterministic-digest considered if Phase 1 synthesis needs a stable mapping from existing T3 Chroma natural IDs.
>
> **Replace with**:
>
> > **`chunk_id` generation rule.** **Resolved (nexus-o6aa.4):** UUID7 fresh at `ChunkIndexed` time. Deterministic-digest rejected: chunker `chunk_index` numbering is unstable across content edits and chunker-config drift, and Phase 1 synthesis is cheaper when existing Chroma natural ids are preserved verbatim. See `docs/rdr/post-mortem/rdr-101-chunk-id-rule.md`.

## Notes for the human reviewer

- The RDR-086 `chash_index.doc_id` naming collision (Phase 0 sibling task) is logically independent of this decision. Either chunk_id rule produces a string that gets written into `chash_index.doc_id`; the rename of that column to `chunk_chroma_id` (or similar) is a separate Phase 0 deliverable.
- This deliverable assumes RDR-101's `doc_id = UUID7` decision (RF-101-1, verified) is already binding. If `doc_id` were re-opened, option B would still be the wrong answer for the same chunker-instability reasons.
- Today's `f"{content_hash[:16]}_{chunk_index}"` rule is removed in Phase 3 (new write path). Phases 1 and 2 do not touch it; legacy ids remain valid Chroma row keys forever.
