---
title: "Xanadu Fidelity — Tumbler Arithmetic and Content-Addressed Spans"
id: RDR-053
type: Architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-05
accepted_date: 2026-04-05
closed_date: 2026-04-06
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
- [x] `chunk_text_hash` can be added to the indexing pipeline without breaking existing collections — **Status**: Verified — **Method**: Spike (ChromaDB accepts heterogeneous metadata schemas; doc1 with no `chunk_text_hash`, doc2 with it — query by `chunk_text_hash` returns only doc2)

**RF-10: Nelson's Full Tumbler Hierarchy vs Nexus Mapping (2026-04-05)**

Nelson's full address (LM 4/28-30):
```
SERVER.0.USER.0.DOCUMENT.VERSION.0.ELEMENT_TYPE.ELEMENT_POS
```
Where `0` digits are major dividers, ELEMENT_TYPE is `1` for bytes or `2` for links.

Nexus mapping:
```
store.owner.document[.chunk]
```

| Nelson segment | Nexus segment | Notes |
|---|---|---|
| SERVER | store | Direct mapping |
| USER | owner | Direct mapping |
| DOCUMENT | document | Direct mapping |
| VERSION | (absent) | `head_hash` tracks version but is not addressable |
| ELEMENT (byte) | chunk | 4th segment = chunk index |
| ELEMENT (link) | (separate table) | Links are in catalog, not in tumbler space |

**Deliberate departures**: (1) No zero-digit major dividers — nexus uses dots throughout. (2) No version segment — documents are mutable (re-indexed in place). (3) Element types (byte vs link) are not distinguished in the tumbler — links live in a separate table. These simplifications are appropriate for nexus's use case where documents are not append-only.

**Evidence basis**: Verified — LM 4/28-30 (OCR text from Mixedbread xanadu store).

**RF-11: ChromaDB Heterogeneous Metadata — Spike Confirmed (2026-04-05)**

Spike: ChromaDB `EphemeralClient` accepts mixed metadata schemas within the same collection. Documents with `content_hash` only coexist with documents that also have `chunk_text_hash`. Metadata filter `where={'chunk_text_hash': 'xyz'}` correctly returns only matching documents.

This means `chunk_text_hash` can be added incrementally — new indexing runs add it, old chunks remain without it, and query-by-hash works for chunks that have it.

**Evidence basis**: Verified — spike execution (Python, chromadb.EphemeralClient).

**RF-12: Depth-Aware Comparison — Spike Confirmed (2026-04-05)**

The -1 sentinel padding approach for cross-depth tumbler comparison passes all 10 test cases:
- Integer ordering: `(1,1,3) < (1,1,10)` = True
- Parent < child: `(1,1,3) < (1,1,3,0)` = True (RF-5 edge case fixed)
- Child > parent: `(1,1,3,0) < (1,1,3)` = False
- Store < owner < document: all correct
- Sorted output matches Nelson's tumbler line from LM 4/21

Implementation: pad shorter tuple with `(-1,) * (max_len - len(self))` before comparison.

**Evidence basis**: Verified — spike execution (Python, all 10 test cases pass).

**RF-13: CCE Chunk Text Identity (2026-04-05)**

The chunk text sent to Voyage AI's `contextualized_embed()` is the same raw text stored in ChromaDB's `documents` field. CCE operates on `inputs=[batch]` where `batch` is a list of raw chunk texts. Therefore `chunk_text_hash = sha256(chunk_text)` is stable across the embedding pipeline — the hash covers the stored text, not the embedding.

For CCE collections (docs, knowledge, rdr): chunk text is preserved verbatim. The hash is stable.
For code collections: chunk text includes tree-sitter extracted content. The hash is stable.

**Evidence basis**: Verified — source read of `doc_indexer.py` `_embed_with_fallback()` and `prose_indexer.py`.

**RF-14: Zero-Digit Major Dividers — Purpose and Why Nexus Omits Them (2026-04-05)**

Nelson's `.0.` separator between tumbler fields serves three purposes (LM 4/20-21, 4/28, 4/32):

1. **Parsability**: Fields can fork to arbitrary depth (`1.2368.792.6` is a 4-digit server field). Without `.0.`, there's no way to know where SERVER ends and USER begins. The zero is "lexical punctuation" — it's the only way to parse the flat digit stream into semantic fields.

2. **Arithmetic consistency**: "The zero digit is reserved for that purpose. This peculiar choice turns out to be arithmetically consistent with the rest of tumbler arithmetic, permitting a uniform arithmetic algorithm that passes along the entire tumbler" (LM 4/21). In ADD, leading zeros in the difference tumbler mean "copy corresponding digit from address" — major dividers act as transparent pass-through points.

3. **Address vs difference distinction**: An address tumbler has *at most three* `.0.` dividers. Difference tumblers may have *any number* of zeros (they're subtraction results). This constraint distinguishes address tumblers from difference tumblers syntactically.

**Why nexus omits them**: Nexus fields are fixed at exactly one digit each (store=1 digit, owner=1 digit, document=1 digit, chunk=optional 1 digit). There is no intra-field forking. Position in the tuple unambiguously identifies which field a digit belongs to — segment 0 is always store, segment 1 is always owner, etc. The `.0.` divider is unnecessary when field depths are fixed.

**Evidence basis**: Verified — LM 4/20-21 (major divider definition), LM 4/28 (examples with field forking), LM 4/32 (difference tumbler zeroes). Primary source OCR from Mixedbread xanadu store.

**RF-15: Revisiting the Fixed-Depth Decision (2026-04-05)**

The fixed-depth tumbler decision was made in RDR-049 when tumblers were "nice addresses for catalog entries" — before arithmetic was on the roadmap. Now that RDR-053 proposes comparison operators and span overlap detection, the question is whether the fixed-depth design should be revisited.

**Arguments for adopting Nelson's `.0.` dividers:**
- Nelson's ADD/SUBTRACT algorithm relies on `.0.` to know where field boundaries are. Without dividers, the arithmetic can't distinguish "step to next document" from "step to next owner."
- Full Nelson arithmetic would come for free — no need to engineer around it with -1 sentinel padding.
- Future extensibility: sub-owners (e.g., `1.0.1.2.0.42` = store 1, owner 1.2, doc 42) become possible.
- Difference tumblers (spans) become a natural type rather than an ad-hoc construction.

**Arguments for keeping fixed-depth:**
- All current use cases (sorting, overlap, ancestors, descendants, LCA) work with tuple comparison + padding. The -1 sentinel spike passes all 10 test cases (RF-12).
- The codebase has ~30 call sites that parse/construct tumblers. Migration is non-trivial.
- SQL `LIKE 'prefix.%'` queries work cleanly with dot-separated integers. Zero-dividers would require `LIKE 'prefix.0.%'` everywhere.
- The simplicity of `int.int.int[.int]` is a significant ergonomic advantage for humans reading catalog output.
- We have no current consumer for ADD/SUBTRACT — only comparison and overlap.

**Recommendation**: Keep fixed-depth for now. The -1 sentinel padding gives correct comparison for our use cases. If/when we need Nelson's full arithmetic (difference tumblers, span construction via ADD), revisit D1 at that point. The migration cost is bounded: `Tumbler.parse()` and `__str__()` are the only serialization points, and the SQL schema stores tumblers as TEXT.

**Evidence basis**: Analysis of codebase (30 call sites), spike results (RF-12), primary source (LM 4/20-21, 4/33-36).

---

## Deviations Register

Every deliberate departure from Nelson's Xanadu design, with rationale and traceability.

### D1: No Zero-Digit Major Dividers

| | |
|---|---|
| **Nelson** | `.0.` between SERVER, USER, DOCUMENT, ELEMENT fields. Required because fields fork to arbitrary depth. Enables ADD/SUBTRACT to know field boundaries. |
| **Nexus** | Dot-separated integer segments with no zero dividers. Fields are always exactly one digit. |
| **Rationale** | Fixed-depth hierarchy makes dividers unnecessary for parsing. Position in tuple identifies field. Simpler SQL (LIKE prefix), simpler human readability. |
| **Consequence** | Cannot support intra-field forking. Cannot implement Nelson's full ADD/SUBTRACT (needs field boundaries). Comparison operators work via -1 sentinel padding (RF-12). |
| **Re-evaluated** | RF-15 (2026-04-05): Revisited after arithmetic entered scope. Decision: keep fixed-depth — comparison suffices for current use cases. Revisit if ADD/SUBTRACT needed. Migration bounded (~30 call sites, TEXT schema). |
| **RF** | RF-14, RF-15 |

### D2: No Version Segment

| | |
|---|---|
| **Nelson** | `N.0.U.0.D.V.0.E` — version (V) between document and element. Each edit creates a new version; all versions permanently addressable. |
| **Nexus** | `store.owner.document[.chunk]` — no version. Documents re-indexed in place. `head_hash` tracks current version but is not addressable. |
| **Rationale** | Nexus documents are git-backed source files — git itself is the version history. Version-addressable tumblers would duplicate git's functionality. The catalog tracks the *current* indexed state, not version history. |
| **Consequence** | Cannot address "the version of indexer.py as of commit abc123" via tumbler. Must use git directly. Links to documents implicitly reference the current version. |
| **RF** | RF-10 |

### D3: Element Types Not Distinguished in Tumbler Space

| | |
|---|---|
| **Nelson** | Element field starts with `1` for bytes, `2` for links. Both live in the same tumbler address space. |
| **Nexus** | 4th segment is always a chunk index. Links are in a separate SQLite table (`links`), not in tumbler space. |
| **Rationale** | Links are metadata about relationships, not content. Putting them in the tumbler space would conflate the addressing of content with the addressing of assertions about content. Separate storage enables typed link queries, bulk operations, and provenance tracking that would be awkward in a flat tumbler space. |
| **Consequence** | Cannot address a specific link by tumbler. Links are identified by `(from_tumbler, to_tumbler, link_type)` tuple. Nelson's "link as addressable content" pattern is lost — a link cannot itself be the target of another link. This forecloses meta-links (annotations on annotations, trust provenance on citations) — a pattern Nelson's design permits (LM 4/41). |
| **RF** | RF-10 |

### D4: TTL Expiry Violates Address Permanence

| | |
|---|---|
| **Nelson** | "ALL ADDRESSES REMAIN VALID" (LM 4/19). Write-once address space. |
| **Nexus** | Entries with `expires_at` set become unresolvable after TTL. Tumblers are never *reused* (high-water mark), but expired entries are tombstoned. |
| **Rationale** | Cached entries (query plans, ephemeral scratch documents) carry TTL. Permanent catalog entries use `expires_at=""` and are not affected. The departure from Nelson applies to any catalog entry with a non-empty `expires_at`, not just T2 plan library entries. |
| **Consequence** | A tumbler that once resolved may stop resolving. Links to expired entries become orphans (detectable via `link_audit()`). |
| **RF** | Catalog docstring (RDR-052 Xanadu critique) |

### D5: Position-Based Chunk Spans (Being Addressed by This RDR)

| | |
|---|---|
| **Nelson** | "Links between bytes can survive deletions, insertions and rearrangements" (LM 4/43). Byte-level addressing in append-only storage ensures survivability. |
| **Nexus (current)** | `from_span`/`to_span` encode chunk indices that shift on re-index. |
| **Nexus (proposed)** | `chash:{chunk_text_hash}` — content-addressed chunk identity. Survives re-indexing when chunk text preserved. |
| **Rationale** | Nexus doesn't store raw source bytes (vectors + chunk text only). Content hashing is the pragmatic middle path between position-based (fragile) and byte-range (requires raw storage). |
| **Consequence** | Spans survive re-indexing when chunk boundaries unchanged. When chunking changes, spans degrade to unresolvable (detectable via `link_audit()` stale span warning). |
| **RF** | RF-3, RF-6, RF-8 |

### D6: No Tumbler Arithmetic (Being Addressed by This RDR)

| | |
|---|---|
| **Nelson** | ADD and SUBTRACT on transfinitesimal numbers (LM 4/33-36). Non-commutative. Difference tumblers are themselves tumblers, bound to an address. |
| **Nexus (current)** | No arithmetic operators on Tumbler. |
| **Nexus (proposed)** | Comparison operators (`__lt__`, `__le__`, etc.) with -1 sentinel padding. No ADD/SUBTRACT — only ordering and overlap detection. |
| **Rationale** | Nelson's full arithmetic requires difference tumblers as a separate type (different rules from address tumblers). For nexus's use cases (sorting, span overlap, reranking), comparison operators suffice. ADD/SUBTRACT would require a `DifferenceTumbler` class and binding semantics that have no current consumer. |
| **Consequence** | Cannot compute span widths or construct spans by tumbler addition. If span-weighted reranking needs span width, it must compute it via segment subtraction at the last differing position (ad-hoc, not general). |
| **RF** | RF-4, RF-12 |

### D7: Single-Store Flat Hierarchy

| | |
|---|---|
| **Nelson** | Server field can fork arbitrarily: `1.2368.792.6` (server 1 → subserver 2368 → sub-sub 792 → leaf 6). The docuverse is a network of servers. |
| **Nexus** | Store is always `1`. Single catalog instance per nexus installation. |
| **Rationale** | Nexus is a single-user local tool, not a distributed network. Multi-store support is YAGNI. The `1` prefix exists to match Nelson's convention that "all servers descend from 1" — it could be extended to multi-store later without breaking existing tumblers. |
| **Consequence** | No federation. All documents live in one address space. Acceptable for the current use case. |
| **RF** | RF-10 |

---

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
    """True if positional span [a_start, a_end] overlaps [b_start, b_end].
    
    Applies only to positional spans (line-range, chunk-index). Content-hash
    spans (chash:) carry no ordering — overlap is undefined for them.
    """
```

No `distance()` — per RF-4, Nelson's difference tumbler is itself a tumbler (non-commutative, bound to an address). For span overlap detection, comparison operators suffice.

Ordering enables: `sorted(tumblers)`, span overlap detection, positional span-weighted reranking. For `chash:` spans, reranking uses a binary signal: "this chunk is referenced by a link" vs "it is not."

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

**Validation**: Update `_SPAN_PATTERN` regex in `catalog.py` to accept `chash:[0-9a-f]{64}` as a valid span format. Without this, `link()` rejects chash spans at the validation gate.

**Resolution**: `resolve_span(span_str, physical_collection)` queries ChromaDB for the chunk with matching `chunk_text_hash` in metadata. Returns the chunk content and position. Falls back to position-based resolution for legacy spans without the `chash:` prefix.

**Audit extension**: `link_audit(t3=None)` — when a T3 client is provided, verifies that `chash:` spans still resolve (the referenced chunk_text_hash exists in ChromaDB). Uses `EphemeralClient` in tests per existing patterns.

**Span policy**: Spans are optional on all link types. Agents creating `cites` or `implements` links that can identify a specific chunk should provide `from_span=chash:{chunk_text_hash}`. Document-to-document links with no chunk context leave `from_span=""`.

**No migration command needed** (RF-7): zero existing span links. Content-hash spans are the default from day one.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Tumbler ordering | `tumbler.py` Tumbler class | Extend — add comparison dunder methods |
| Content-hash spans | `catalog.py` link(), link_audit() | Extend — new span format + resolution |
| Span validation | `catalog.py` _SPAN_PATTERN | Extend — add `chash:` format to regex |
| Span resolution | `catalog.py` resolve_chunk() | Extend — add content-hash resolution path |
| Span audit | `catalog.py` link_audit() | Extend — optional T3 client for chash verification |

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

#### Step 2: Update `_SPAN_PATTERN` and span resolution

Update `_SPAN_PATTERN` in `catalog.py` to accept `chash:[0-9a-f]{64}` format. Add `chash:` prefix parsing to span resolution. `resolve_span()` queries ChromaDB for chunk with matching `chunk_text_hash` metadata.

#### Step 3: Update link creation to use chunk-text-hash spans

When creating links with spans, look up the chunk's `chunk_text_hash` from ChromaDB and use `chash:{chunk_text_hash}` format. Span policy: optional on all link types; agents provide when they can identify a specific chunk.

#### Step 4: Extend `link_audit()` for chash verification

Add `link_audit(t3=None)` — when T3 client provided, verify `chash:` spans still resolve in ChromaDB. Uses `EphemeralClient` in tests.

#### Step 5: Tests

TDD: `_SPAN_PATTERN` accepts chash format, chunk_text_hash computation, content-hash resolution, fallback to position for legacy, `link_audit(t3=...)` detects missing hashes.

## Test Plan

- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.1.10")` — **Verify**: True (integer, not lexicographic)
- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.2.1")` — **Verify**: True (parent differs)
- **Scenario**: `Tumbler.parse("1.1.3") < Tumbler.parse("1.1.3.0")` — **Verify**: True (RF-5 parent < child)
- **Scenario**: Span overlap between `[1.1.3, 1.1.7]` and `[1.1.5, 1.1.10]` — **Verify**: True
- **Scenario**: No overlap between `[1.1.1, 1.1.3]` and `[1.1.5, 1.1.7]` — **Verify**: False
- **Scenario**: `chunk_text_hash` present in ChromaDB metadata after indexing — **Verify**: distinct per chunk
- **Scenario**: Link with `from_span="chash:abc123"` resolves to correct chunk — **Verify**: content matches
- **Scenario**: Re-index document, resolve chunk-text-hash span — **Verify**: same content if chunk unchanged
- **Scenario**: Re-index with different chunking, resolve chunk-text-hash span — **Verify**: graceful fallback (hash not found, fall back to position)
- **Scenario**: `link()` with `from_span="chash:abc..."` (64 hex chars) — **Verify**: accepted by `_SPAN_PATTERN`
- **Scenario**: `link_audit(t3=ephemeral_client)` with chash span pointing to missing hash — **Verify**: reported in stale spans

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
