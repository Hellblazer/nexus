---
title: "Xanadu Fidelity — Tumbler Arithmetic and Content-Addressed Spans"
id: RDR-053
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-05
accepted_date:
related_issues: [nexus-zr3u]
---

# RDR-053: Xanadu Fidelity — Tumbler Arithmetic and Content-Addressed Spans

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus's catalog system explicitly draws on Ted Nelson's Xanadu vision (tumblers, typed links, append-only truth store), but three specific departures undermine the Xanadu contract in ways that have practical consequences:

1. **No tumbler arithmetic** — Nelson's tumblers are transfinitesimal numbers supporting ADD and SUBTRACT (LM Ch. 4/16). Without arithmetic, span overlap detection, span-weighted reranking, and content ordering within documents require ad-hoc integer parsing each time.

2. **Position-based chunk spans** — `from_span`/`to_span` on links encode chunk indices (`"3"`) or character ranges (`"3:100-250"`). Re-indexing a document silently shifts which content a span refers to, violating Nelson's survivability principle: "Links between bytes can survive deletions, insertions and rearrangements" (LM Ch. 4/43).

3. **No content-identity for chunks** — chunks are identified by position index, not content hash. The same text chunked differently produces different addresses with no way to detect the equivalence.

These are not cosmetic gaps. The stale-span problem (flagged by `link_audit()` in RDR-052) means citation links degrade silently over time. The lack of arithmetic means RF-7's proposed span-weighted reranking will be verbose and fragile.

## Context

### Background

RDR-052 (Catalog-First Query Routing) added tumbler hierarchy helpers (`depth`, `ancestors()`, `lca()`, `descendants()`) and ghost-element resolution (`resolve_chunk()`). The substantive critique from a Nelson/Xanadu perspective (2026-04-05) identified these three gaps as the most significant departures from Xanadu fidelity with practical consequences.

The `Catalog` class docstring now documents these departures explicitly. This RDR proposes solutions.

### Technical Environment

- `src/nexus/catalog/tumbler.py` — Tumbler dataclass (frozen, hashable, 3-4 segments)
- `src/nexus/catalog/catalog.py` — Catalog with link graph, resolve_chunk()
- `src/nexus/catalog/catalog_db.py` — SQLite schema for links (from_span, to_span TEXT)
- ChromaDB chunk metadata includes `content_hash` field (already computed during indexing)
- `src/nexus/indexer.py` — computes `content_hash` per chunk during repo indexing

### Primary Sources

- Nelson, T.H. *Literary Machines* (1981/1987). Ch. 4/15-23 (tumbler design), Ch. 4/41-50 (link structure), Ch. 4/16 (transfinitesimal arithmetic), Ch. 4/43 (survivability).
- RDR-052 RF-6: Tumbler hierarchy analysis (Nelson vs nexus vs ltree)
- RDR-052 RF-7: Revised GST with tumblers as coordinate system

## Research Findings

### Investigation

**RF-1: Nelson's Tumbler Arithmetic (2026-04-05)**

Nelson describes tumblers as "transfinitesimal numbers" — the inverse of Cantor's transfinite numbers. Initial draft identified three operations: successor, difference, comparison.

**Evidence basis**: Documented (LM Ch. 4/16). Superseded by RF-4.

**RF-2: Content-Hash Chunk Identity (2026-04-05)**

ChromaDB chunk metadata includes `content_hash` computed by all four indexers (code, prose, PDF, markdown) using SHA-256.

**CORRECTED by RF-6**: `content_hash` is file-level, not chunk-level. All chunks from the same file share the same hash. The proposed `hash:{content_hash}` span format is therefore ambiguous for multi-chunk files.

**Evidence basis**: Verified — source read of all four indexers (RF-6).

**RF-3: Span Stability Options (2026-04-05)**

Three approaches to span survivability, in increasing fidelity to Nelson:

| Approach | Survivability | Complexity | Nelson fidelity |
|---|---|---|---|
| Position index (current) | Breaks on re-index | None (status quo) | Low — violates Ch. 4/43 |
| Content hash (chunk-level) | Survives re-index if chunk boundaries unchanged | Low — new `chunk_text_hash` at index time | Medium — content-addressed |
| Source byte range | Survives any chunking change | High — requires source content access | High — maps to Nelson's byte-level addressing |

Recommendation: Chunk-level content hash (middle path). Requires adding `chunk_text_hash = sha256(chunk_text)` at index time (RF-6 correction). Source byte ranges rejected — nexus doesn't store raw source content.

**Evidence basis**: Verified.

**RF-4: Nelson's Exact ADD/SUBTRACT Semantics (2026-04-05)**

Confirmed from LM Ch. 4/33-36 (primary source in Mixedbread xanadu store):

**ADD** (LM 4/34): Augend is an address tumbler, addend is a *difference tumbler* (a span). For each leading zero in the addend, copy the corresponding address digit. At the first non-zero digit, add. Copy remaining from addend. Example: `1.1.0.2.0.2.2.0.1.777 + 0.0.0.1 = 1.1.0.3` — digits below the changed level are dropped.

**SUBTRACT** (LM 4/35): For identical leading digits, yield zero. At first difference, subtract. Copy remaining from minuend.

**Non-commutativity** (LM 4/33): `A+B ≠ B+A`. Difference tumblers must always be packaged with their bound address tumbler.

**Design correction**: The draft's `distance() -> int` misrepresents Nelson's model — a difference tumbler is itself a Tumbler, not a scalar. For span overlap detection (the actual use case), only comparison operators are needed. Drop `distance()`.

**Evidence basis**: Verified — LM Ch. 4/33-36, primary source text.

**RF-5: Tuple Comparison Correctness (2026-04-05)**

`sorted(list[tuple[int,...]])` produces exactly Nelson's tumbler line from LM 4/21. Verified against all 13 tumblers from the primary source — order matches.

**Edge case (latent defect)**: Python tuple comparison treats shorter tuples as "less than" longer ones with the same prefix: `(1,1,3) < (1,1,3,1)` = True. But `(1,1,3,0) < (1,1,3)` = False — a chunk tumbler with segment 0 is treated as *greater* than its parent document.

Since chunk indices start at 0 in the codebase, `Tumbler.__lt__` must pad the shorter tuple to handle this. Test: `Tumbler.parse("1.1.3") < Tumbler.parse("1.1.3.0")` must return True.

**Evidence basis**: Verified — Python execution + LM 4/21 comparison.

**RF-6: content_hash Is File-Level, Not Chunk-Level (2026-04-05)**

| Indexer | Hash input | Hash function | Granularity |
|---------|-----------|---------------|-------------|
| code_indexer.py | UTF-8 bytes of entire source file | SHA-256 | File |
| prose_indexer.py | UTF-8 bytes of entire file | SHA-256 | File |
| doc_indexer.py (PDF) | Raw file bytes (64KB blocks) | SHA-256 | File |
| doc_indexer.py (markdown) | Raw file bytes | SHA-256 | File |

All chunks from the same file share the same `content_hash`. The proposed `hash:{content_hash}` span format must be revised to either:
- `hash:{file_hash}:{chunk_index}` — file version + position (partial survivability)
- Add new `chunk_text_hash = sha256(chunk_text)` at index time — true chunk-level content identity

The second option is preferred: it provides content-addressed chunk identity independent of position, matching the IPFS DAG chunking analogue (RF-9).

**Evidence basis**: Verified — source read of all four indexers.

**RF-7: Span Inventory — Zero Existing Spans (2026-04-05)**

Live catalog (`~/.config/nexus/catalog/links.jsonl`): 1,082 active links, **zero** with non-empty `from_span` or `to_span`. All links are document-to-document.

**Design impact**: No backward compatibility required. `nx catalog migrate-spans` command can be omitted from Phase 2. Content-hash spans can be the only span format from day one.

**Evidence basis**: Verified — Python analysis of live catalog JSONL.

**RF-8: Nelson's Survivability Grounded in Append-Only Storage (2026-04-05)**

> "A Xanadu link is not between points, but between spans of data. Thus we may visualize it as a strap between bytes." (LM 4/42)
> "SURVIVABILITY: Links between bytes can survive deletions, insertions and rearrangements, if anything is left at each end." (LM 4/43)

Nelson's survivability depends on append-only byte storage — bytes are never truly deleted, so byte addresses are permanent. Nexus does not have this property (chunks are replaced on re-index). The chunk-text-hash approach is the correct pragmatic adaptation: content-addressed identity survives re-indexing as long as the chunk text itself is preserved.

**Evidence basis**: Verified — LM Ch. 4/42-43.

**RF-9: Content Addressing Comparison (2026-04-05)**

| System | Addressing | Granularity | Partial survival |
|--------|-----------|-------------|-----------------|
| Git | SHA-256 of `"blob {size}\0{content}"` | File (blob) | No |
| IPFS | CID (multihash of DAG node) | File or chunk | No |
| Xanadu | Position (tumbler) | Byte range | Yes (append-only) |
| Nexus (proposed) | SHA-256 of chunk text | Chunk | No |

IPFS's DAG chunking is the closest analogue to per-chunk content addressing. Neither git nor IPFS provides partial survival — that property is unique to Xanadu's append-only model.

**Evidence basis**: Documented.

### Critical Assumptions

- [x] `content_hash` is present in ChromaDB metadata for all indexed chunks — **Status**: Verified — **Method**: Source Search (all four indexers)
- [x] Tumbler tuple comparison produces the same ordering as Nelson's transfinitesimal arithmetic for fixed-depth hierarchies — **Status**: Conditionally verified — **Method**: Execution test (RF-5). Edge case at zero segment requires explicit handling in `__lt__`.
- [x] Existing links with position-based spans can be migrated — **Status**: Verified empty — **Method**: Spike (RF-7). Zero existing span links. No migration needed.
- [ ] `chunk_text_hash` can be added to the indexing pipeline without breaking existing collections — **Status**: Unverified — **Method**: Needs Spike (add field to metadata dict, verify ChromaDB accepts new metadata keys on existing collections)

## Proposed Solution

### Approach

Two components, implementable independently:

**Component 1: Tumbler Arithmetic** — Add comparison operators to the `Tumbler` class. No `distance()` method — Nelson's difference is a tumbler, not a scalar (RF-4).

**Component 2: Content-Addressed Spans** — Add `chunk_text_hash` to the indexing pipeline, use as span identifier. No migration needed (RF-7: zero existing spans).

### Technical Design

#### Component 1: Tumbler Arithmetic

Add to `Tumbler` (tumbler.py):

```python
def __lt__(self, other: Tumbler) -> bool:
    """Ordering on integer segments with depth-aware padding.
    
    Parent tumblers sort before their children:
    (1,1,3) < (1,1,3,0) must be True (RF-5 edge case).
    Pads shorter tuple with -1 sentinel before comparison.
    """

def __le__(self, other: Tumbler) -> bool: ...
def __gt__(self, other: Tumbler) -> bool: ...
def __ge__(self, other: Tumbler) -> bool: ...

@staticmethod
def spans_overlap(a_start: Tumbler, a_end: Tumbler, b_start: Tumbler, b_end: Tumbler) -> bool:
    """True if span [a_start, a_end] overlaps [b_start, b_end]."""
```

No `distance()` — per RF-4, Nelson's difference tumbler is itself a tumbler (non-commutative, bound to an address). For span overlap detection, comparison operators suffice.

Ordering enables: `sorted(tumblers)`, span overlap detection, span-weighted reranking.

#### Component 2: Content-Addressed Spans

**Indexing change**: Add `chunk_text_hash = sha256(chunk_text.encode())` to chunk metadata at index time. This is distinct from the existing file-level `content_hash` (RF-6). Each chunk gets its own identity.

New span format (no backward compatibility needed — RF-7 confirms zero existing spans):

```
# New (content-addressed):
from_span = "chash:abc123def456"   # chunk_text_hash of the referenced chunk

# Legacy (still accepted but not created):
from_span = "3"           # chunk index 3
from_span = "10-20"       # line range 10-20
```

The `chash:` prefix identifies content-addressed spans. New links use `chash:` when `chunk_text_hash` is available.

**Resolution**: `resolve_span(span_str, physical_collection)` queries ChromaDB for the chunk with matching `chunk_text_hash` in metadata. Returns the chunk content and position. Falls back to position-based resolution for legacy spans without the `chash:` prefix.

**No migration command needed** (RF-7): zero existing span links. Content-hash spans are the default from day one.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Tumbler ordering | `tumbler.py` Tumbler class | Extend — add comparison dunder methods |
| Content-hash spans | `catalog.py` link(), link_audit() | Extend — new span format + resolution |
| Span migration | `commands/catalog.py` | New subcommand `nx catalog migrate-spans` |
| Span resolution | `catalog.py` resolve_chunk() | Extend — add content-hash resolution path |

### Decision Rationale

Content-hash spans over source byte ranges: nexus does not store raw source content. Content hashes are already computed and stored in ChromaDB metadata. This gives meaningful survivability (stable across re-indexing when chunk boundaries are unchanged) without requiring a fundamental architecture change.

Tumbler arithmetic as comparison operators over transfinitesimal number space: fixed-depth tumblers make lexicographic comparison mathematically equivalent to Nelson's scheme. The complexity of true transfinitesimal arithmetic is not justified when the address space is bounded to 4 levels.

## Alternatives Considered

### Alternative 1: Source Byte Ranges

**Description**: Store byte offsets into the original source file as span identifiers.

**Pros**: Maximum survivability — survives any chunking change, faithful to Nelson's byte-level addressing.

**Cons**: Requires access to the original source file at span resolution time. Nexus does not store raw content. Would require either (a) always having the git repo checked out, or (b) storing source snapshots — both violate the "vectors + chunk text only" design principle.

**Reason for rejection**: Architectural incompatibility with nexus's storage design.

### Briefly Rejected

- **Semantic hash (embedding distance)**: Use vector similarity to find the "same" chunk after re-indexing. Too fuzzy — similar chunks are not the same chunk. False positives in citation links are worse than stale spans.

## Trade-offs

### Consequences

- Tumbler ordering enables `sorted()` on tumbler collections, span overlap queries, and cleaner span-weighted reranking
- Content-hash spans survive re-indexing (when chunk boundaries unchanged), reducing stale span count
- Migration is non-destructive (JSONL append) but requires ChromaDB access for hash lookup
- Two span formats coexist during transition — resolution must handle both

### Risks and Mitigations

- **Risk**: Content hashes may not be available for all chunks (older indexing runs, PDF chunks)
  **Mitigation**: `migrate-spans` reports un-migratable spans. Position spans remain valid as fallback.
- **Risk**: Tumbler comparison semantics may diverge from Nelson's for edge cases (different-depth tumblers)
  **Mitigation**: Document the simplification explicitly. Add tests for cross-depth comparison.

### Failure Modes

- **Stale content-hash span**: If a chunk's content changes but the hash is not updated (bug in indexer), the span resolves to the old content. Visible via `link_audit()` stale span detection.
- **Missing hash in ChromaDB**: `resolve_span()` falls back to position-based resolution and logs a warning. Degraded but not broken.

## Implementation Plan

### Prerequisites

- [ ] Verify content_hash availability across all collection types (code, docs, knowledge, rdr)
- [ ] Count existing links with position spans (for migration scope)

### Phase 1: Tumbler Arithmetic

#### Step 1: Add comparison operators to Tumbler

Add `__lt__`, `__le__`, `__gt__`, `__ge__` with depth-aware padding (RF-5: pad shorter tuple with -1 sentinel so parents sort before children).

#### Step 2: Add `spans_overlap()` static method

Implement overlap detection using the comparison operators.

#### Step 3: Tests

TDD: ordering edge cases including RF-5 zero-segment case (`1.1.3 < 1.1.3.0`), different depths, same parent, cross-owner, overlap detection.

### Phase 2: Content-Addressed Spans

#### Step 1: Add `chunk_text_hash` to indexing pipeline

In all four indexers (code, prose, PDF, markdown), compute `chunk_text_hash = hashlib.sha256(chunk_text.encode()).hexdigest()` and include in ChromaDB metadata alongside existing file-level `content_hash`.

#### Step 2: Extend span format and resolution

Add `chash:` prefix parsing to span resolution. `resolve_span()` queries ChromaDB for chunk with matching `chunk_text_hash` metadata.

#### Step 3: Update link creation to use chunk-text-hash spans

When creating links with spans, look up the chunk's `chunk_text_hash` from ChromaDB and use `chash:{chunk_text_hash}` format.

#### Step 4: Tests

TDD: chunk_text_hash computation, content-hash resolution, fallback to position for legacy, stale hash detection via link_audit().

## Test Plan

- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.1.10")` — **Verify**: True (integer, not lexicographic)
- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.2.1")` — **Verify**: True (parent differs)
- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.1.3.0")` — **Verify**: True (RF-5 parent < child)
- **Scenario**: Span overlap between `[1.1.3, 1.1.7]` and `[1.1.5, 1.1.10]` — **Verify**: True
- **Scenario**: `chunk_text_hash` present in ChromaDB metadata after indexing — **Verify**: distinct per chunk
- **Scenario**: Link with `from_span="chash:abc123"` resolves to correct chunk — **Verify**: content matches
- **Scenario**: Re-index document, resolve chunk-text-hash span — **Verify**: same content if chunk unchanged
- **Scenario**: Re-index with different chunking, resolve chunk-text-hash span — **Verify**: graceful fallback (hash not found, fall back to position)

## Validation

### Testing Strategy

1. **Tumbler ordering**: All comparison operators tested across same-depth, cross-depth, same-parent, cross-parent combinations
2. **Content-hash resolution**: Unit test with mock ChromaDB returning chunk by content_hash metadata filter
3. **Migration**: Integration test with real catalog + ChromaDB ephemeral client
4. **Stale span audit**: Verify `link_audit()` reports content-hash spans whose hash no longer exists in ChromaDB

## Finalization Gate

> Complete each item before marking Accepted.

### Contradiction Check

No contradictions found between research findings and proposed solution. RF-3's middle-path recommendation (content hash) is directly adopted.

### Assumption Verification

- content_hash presence needs verification across all collection types before implementation
- Tumbler arithmetic equivalence to Nelson's scheme for fixed-depth hierarchies needs formal argument

### Scope Verification

Minimum viable validation: create a link with content-hash span, re-index the document, verify the span still resolves to the correct content. This is in scope for Phase 2 Step 4.

### Cross-Cutting Concerns

- **Versioning**: Span format is backward-compatible (hash: prefix distinguishes new format)
- **Memory management**: N/A — no new persistent resources
- **Incremental adoption**: Position spans continue to work indefinitely. Migration is optional.

### Proportionality

Two focused components. Implementation plan is ~4 steps per phase. Right-sized for the scope.

## References

- Nelson, T.H. *Literary Machines* (1981/1987) — Ch. 4/15-23 (tumbler design), Ch. 4/16 (transfinitesimal arithmetic), Ch. 4/41-50 (link structure), Ch. 4/43 (survivability)
- RDR-052: Catalog-First Query Routing — RF-6 (tumbler hierarchy), RF-7 (ghost elements as coordinate system)
- RDR-052 Xanadu critique (2026-04-05) — identified the three departures addressed here
- `src/nexus/catalog/catalog.py` Catalog docstring — documents deliberate Xanadu departures

## Revision History

Initial draft from Xanadu/Nelson substantive critique findings (2026-04-05).
