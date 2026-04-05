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

Nelson describes tumblers as "transfinitesimal numbers" — the inverse of Cantor's transfinite numbers. Key operations:

- **Successor**: Given tumbler T, the next tumbler in the address space (step forward one position)
- **Difference**: Given tumblers A and B, compute the "distance" between them (span width)
- **Comparison**: A < B in the tumbler ordering (for span overlap detection: A.from <= B.to AND B.from <= A.to)

In the original Xanadu, these operate on potentially infinite-depth nested addresses. Nexus tumblers have fixed depth (3-4 segments), making the arithmetic substantially simpler: lexicographic comparison on integer tuples suffices for ordering and overlap.

**Evidence basis**: Documented (LM Ch. 4/16). The transfinitesimal number space is a generalization; for fixed-depth tumblers, tuple comparison is mathematically equivalent.

**RF-2: Content-Hash Chunk Identity (2026-04-05)**

ChromaDB chunk metadata already includes `content_hash` (computed by `indexer.py` during repo indexing). This hash uniquely identifies chunk content regardless of position. If a document is re-indexed with identical content but different chunking parameters, the `content_hash` of surviving chunks remains the same.

Limitation: `content_hash` is computed over the raw chunk text. If the chunking algorithm changes the chunk boundaries (different split points), the hash changes even if the underlying source content is identical. Content-hash stability is at the chunk level, not the source level.

**Evidence basis**: Verified — `content_hash` in ChromaDB metadata confirmed by reading `indexer.py`.

**RF-3: Span Stability Options (2026-04-05)**

Three approaches to span survivability, in increasing fidelity to Nelson:

| Approach | Survivability | Complexity | Nelson fidelity |
|---|---|---|---|
| Position index (current) | Breaks on re-index | None (status quo) | Low — violates Ch. 4/43 |
| Content hash | Survives re-index if chunk boundaries unchanged | Low — use existing `content_hash` | Medium — content-addressed, not position-addressed |
| Source byte range | Survives any chunking change | High — requires source content access | High — maps to Nelson's byte-level addressing |

Recommendation: Content hash (middle path). Source byte ranges are impractical — nexus doesn't store raw source content (by design, per `docs/historical.md`). Content hash is already available and provides meaningful survivability.

**Evidence basis**: Verified — `content_hash` available in metadata. Source byte ranges rejected because nexus stores vectors + chunk text only.

### Critical Assumptions

- [x] `content_hash` is present in ChromaDB metadata for all indexed chunks — **Status**: Verified — **Method**: Source Search (indexer.py)
- [ ] Tumbler tuple comparison produces the same ordering as Nelson's transfinitesimal arithmetic for fixed-depth hierarchies — **Status**: Assumed — **Method**: Docs Only (needs formal verification against LM definition)
- [ ] Existing links with position-based spans can be migrated to content-hash spans via a one-time backfill — **Status**: Unverified — **Method**: Needs Spike (count existing span links, verify content_hash availability for each)

## Proposed Solution

### Approach

Two components, implementable independently:

**Component 1: Tumbler Arithmetic** — Add comparison and distance operators to the `Tumbler` class.

**Component 2: Content-Addressed Spans** — Replace position-index span format with content-hash span format. Maintain backward compatibility during migration.

### Technical Design

#### Component 1: Tumbler Arithmetic

Add to `Tumbler` (tumbler.py):

```python
def __lt__(self, other: Tumbler) -> bool:
    """Lexicographic ordering on integer segments."""

def __le__(self, other: Tumbler) -> bool: ...
def __gt__(self, other: Tumbler) -> bool: ...
def __ge__(self, other: Tumbler) -> bool: ...

def distance(self, other: Tumbler) -> int:
    """Integer distance in the last differing segment.
    
    For same-depth tumblers at the same parent, this is the
    document-number difference. For different-depth tumblers,
    compare at the shallowest common depth.
    """

def spans_overlap(self, self_end: Tumbler, other_start: Tumbler, other_end: Tumbler) -> bool:
    """True if span [self, self_end] overlaps [other_start, other_end]."""
```

Ordering enables: `sorted(tumblers)`, span overlap detection, span-weighted reranking.

#### Component 2: Content-Addressed Spans

New span format alongside existing position format:

```
# Current (position-based):
from_span = "3"           # chunk index 3
from_span = "3:100-250"   # chunk 3, chars 100-250
from_span = "10-20"       # line range 10-20

# New (content-addressed):
from_span = "hash:abc123def456"   # content_hash of the referenced chunk
```

The `hash:` prefix distinguishes content-addressed spans from position spans. Both formats coexist — existing links with position spans continue to work. New links created by agents use content-hash spans when the `content_hash` is available.

**Resolution**: `resolve_span(span_str, physical_collection)` queries ChromaDB for the chunk with matching `content_hash` metadata. Returns the chunk content and position. Falls back to position-based resolution if the hash prefix is absent.

**Migration**: A backfill command (`nx catalog migrate-spans`) reads all links with position spans, looks up the chunk's `content_hash` from ChromaDB, and rewrites the span. Non-destructive — appends a new JSONL line per Nelson's append-only principle.

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

Add `__lt__`, `__le__`, `__gt__`, `__ge__` using tuple comparison on segments. Add `distance()` method.

#### Step 2: Add `spans_overlap()` utility

Implement overlap detection using the comparison operators.

#### Step 3: Tests

TDD: ordering edge cases (different depths, same parent, cross-owner), overlap detection, distance computation.

### Phase 2: Content-Addressed Spans

#### Step 1: Extend span format and resolution

Add `hash:` prefix parsing to span resolution. Extend `resolve_chunk()` to handle content-hash lookup via ChromaDB metadata query.

#### Step 2: Update link creation to use content-hash spans

When creating links with spans, look up the chunk's `content_hash` from ChromaDB and use `hash:{content_hash}` format.

#### Step 3: Migration command

`nx catalog migrate-spans` — backfill existing position spans to content-hash format.

#### Step 4: Tests

TDD: content-hash resolution, fallback to position, migration idempotency, stale hash detection.

## Test Plan

- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.1.10")` — **Verify**: True (integer, not lexicographic)
- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.2.1")` — **Verify**: True (parent differs)
- **Scenario**: Span overlap between `[1.1.3, 1.1.7]` and `[1.1.5, 1.1.10]` — **Verify**: True
- **Scenario**: Link with `from_span="hash:abc123"` resolves to correct chunk — **Verify**: content matches
- **Scenario**: Re-index document, resolve content-hash span — **Verify**: same content returned
- **Scenario**: Re-index with different chunking, resolve content-hash span — **Verify**: graceful fallback
- **Scenario**: `nx catalog migrate-spans` on link with position span — **Verify**: span updated to hash format

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
