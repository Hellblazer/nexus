---
title: "Fine-Grain Content-Stable Catalog Spans: A dchash Document-Offset Span Form That Survives Re-Chunking"
id: RDR-171
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-26
related_issues: [nexus-3kybd]
related_rdrs: [RDR-053, RDR-108, RDR-086, RDR-152]
supersedes: []
external_ref: conductus RDR-001 OQ-3
---

# RDR-171: Fine-Grain Content-Stable Catalog Spans

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Catalog link spans (`from_span` / `to_span`) today address a fragment of a document by one of
four forms (`src/nexus/catalog/catalog_spans.py`):

| Form | Meaning | Survives re-chunking? |
|---|---|---|
| `""` | whole document | n/a |
| `42-57` | source-file line range (reads `file_path`) | yes (source-relative) |
| `3:100-250` | chunk-index : char-range (via `document_chunks` manifest) | no (position drifts) |
| `chash:<hex>[:start-end]` | content-addressed **chunk** ± char-range within that chunk | **no** |

The finest-grain form, `chash:<chunk_text_hash>:<start>-<end>`, anchors a char-range **inside a
chunk** identified by the chunk's content hash. When a document is re-indexed with a different
chunker, chunk size, or embedding model, the chunk boundaries shift, the chunk text changes, its
`sha256` changes, and the span no longer resolves. **RDR-053 D5 accepted this consequence
deliberately**: *"When chunking changes, spans degrade to unresolvable (detectable via
`link_audit()` stale span warning)."* That is the right call for nexus's own internal links, but
it is a hard floor for an external consumer that wants to pin a *passage*, not a *chunk*.

**Consumer driver (Conductus RDR-001 OQ-3):** Conductus wants to map Obsidian sub-document
references to precise catalog spans. Today it can only approximate to the containing chunk, and
that approximation breaks on every re-index. Conductus needs a span that **survives re-chunking**
of unchanged document content. (Surviving document *edits* — the author-anchor-relocation problem,
`[[Note#Heading]]` / `[[Note^blockid]]` tracking — was considered and is explicitly **out of
scope** for this RDR; see Alternatives.)

The gap: there is no span form whose offsets are stable across re-chunking. The two source-relative
forms (`42-57`) are stable but coarse (line granularity, source-file only) and carry no
content-identity stamp for stale detection.

## Decision

Add a fifth span form: **`dchash:<doc_content_hash>:<start>-<end>`** — a char-range whose offsets
index the document's **canonical text** (the whole document), not a chunk. Because the offsets are
document-relative, chunk boundaries are irrelevant and the span survives any re-chunking of
unchanged content. The embedded `doc_content_hash` is the existing **file-level `content_hash`**
(distinct from `chunk_text_hash`, per RDR-053), used as a stale-detector: if the document's current
`content_hash` no longer matches, the span is stale.

This **extends** RDR-053 D5 / RDR-108; it does not reverse them. `chash:` chunk spans remain the
default for nexus-internal links; `dchash:` is the precise-passage form for content-stable pinning.

### Resolution (canonical text = source-preferred, stored-copy fallback)

A new branch in `resolve_span_text_for_entry`:

1. If the source is readable at `file_path` / `source_uri` → slice `[start:end]` from it (reuses
   the existing line-range read path's file access).
2. Verify the document's current file-level `content_hash` equals the span's `doc_content_hash`.
3. If the source is absent **or** the hash mismatches → fall back to a nexus-stored **canonical
   copy** keyed by `content_hash`.
4. If no canonical text matches the hash anywhere → **stale**; surface via the existing
   `link_audit()` stale-span warning (reuse the RDR-053 D5 contract; do not invent a new signal).

### Phasing

The stored-copy fallback is a **new storage obligation** — a content-addressed `document_content`
table (`content_hash → raw text`, deduped). This collides with **RDR-152** (the in-flight
SQLite→Postgres/Java-service cutover): any new T2 table must be carried through that migration, and
`develop` is world-blocked on the release boundary. Therefore:

- **Phase 1 — source-only.** Ship `dchash:` syntax + resolution + stale-detection reading from the
  **source only** (`file_path`). Zero new storage, no cutover collision, fully usable for
  file-backed Conductus / Obsidian documents.
- **Phase 2 — stored-copy fallback.** Add the content-addressed `document_content` table,
  **sequenced after the RDR-152 cutover** so the table is born in Postgres, not retrofitted into the
  retiring SQLite. Delivers the no-source-present guarantee.

**Implementation of both phases waits on the cutover / release boundary.** This RDR is authored to
`accepted` now; no code lands on `develop` until the boundary lifts.

## Alternatives Considered

- **Family B — author-supplied stable anchors** (`anchor:<doc>#Heading` / `^blockid`): store
  Obsidian anchors as first-class span ids, re-located at index time, surviving re-chunking **and**
  edits. Rejected for this RDR: the chosen stability bar is survive-re-chunking-only; anchor
  relocation across edits is materially more machinery (anchor extraction at index, an
  anchor→location mapping refreshed each re-index) for a guarantee Conductus did not ask for. Left
  as a possible future RDR if the edit-survival bar is later raised.
- **Reconstruct-from-chunks offsets** (offsets into the concatenation of ordered chunk texts):
  rejected — chunk overlaps/separators differ across chunkings, so the reconstructed text (and thus
  the offsets) drift, defeating the survive-re-chunking purpose.
- **Do nothing / keep `chash:` only:** rejected — leaves Conductus approximating to the containing
  chunk with a span that breaks on every re-index.

## Consequences

- A passage pin survives re-chunking of unchanged content (the stated bar). It does **not** survive
  content edits (hash mismatch → stale), which is the honest, detectable failure mode D5 already
  established.
- Phase 1 is a pure additive parser+resolver change reusing existing primitives (line-range file
  read, file-level `content_hash`, `link_audit` stale signal) — small surface.
- Phase 2 introduces a durable content store; it is deliberately deferred behind RDR-152 so it does
  not couple new schema to the dying SQLite tier.
- Producer/consumer contract: Conductus supplies `dchash:<hash>:<start>-<end>` as `from_span` /
  `to_span` via the catalog link API; nexus resolves it. Back-reference to conductus RDR-001 OQ-3
  retained on `nexus-3kybd`.

## Open Items (resolve in research phase)

1. Confirm file-level `content_hash` is present and stable on `CatalogEntry` for the target corpora
   (code / docs / knowledge / Obsidian-note ingest).
2. Define the exact `dchash:` regex so it cannot collide with `chash:` or the `N:N-N`
   chunk-index:char form.
3. Decide `content_hash` canonicalization (newline / encoding normalization) so the source-read and
   stored-copy paths hash identically — otherwise step 2 of resolution false-flags stale.
4. Phase-2 storage must target the **post-cutover Postgres** schema; coordinate the table shape with
   RDR-152 (`nexus-gmiaf`).
5. Confirm whether Conductus computes offsets against the raw note bytes or a normalized form; the
   offset basis must match nexus's canonical-text definition.

## Design Heritage

Approved design memo: T2 `nexus/design-finegrain-author-anchored-spans.md` (brainstorming-gate,
2026-06-26). Heritage: RDR-053 (chash content-addressed spans), RDR-108 (content-addressed natural
IDs + manifest as authoritative doc structure), RDR-086 (chash span resolution surface).
