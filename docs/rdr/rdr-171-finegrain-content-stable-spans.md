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
related_rdrs: [RDR-053, RDR-108, RDR-086, RDR-152, RDR-180, RDR-236]
supersedes: []
external_ref: conductus RDR-001 OQ-3
---

# RDR-171: Fine-Grain Content-Stable Catalog Spans

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

A catalog link can point at part of a document, not only the whole of it. The part is named by a
*span*, a short string stored in the link's `from_span` or `to_span`. Today there are four span
forms (`src/nexus/catalog/catalog_spans.py`, census in 171-research-3):

| Form | Meaning | Survives re-chunking? |
|---|---|---|
| `""` | the whole document | n/a |
| `42-57` | lines 42 to 57 of the source file | yes, but line-grained and unstamped |
| `3:100-250` | characters 100 to 250 of chunk number 3 | no: chunk numbers move |
| `chash:<hex>[:start-end]` | the chunk whose text hashes to `<hex>`, optionally a character range in it | no: the chunk's text, and so its hash, changes |

*Re-chunking* means indexing the same document again with a different chunker, chunk size or
embedding model. The text of the document is unchanged; only the cut points between chunks move.

#### Gap 1: No span pins a passage precisely and survives re-chunking

The finest form, `chash:<hex>:<start>-<end>`, addresses characters inside one chunk. When the
document is re-chunked, that chunk's text changes, its hash changes, and the span no longer
resolves. RDR-053 decision D5 accepted this on purpose for nexus's own links. It is a hard floor for
anyone who wants to pin a *passage* (a sentence, a paragraph, a quotation) rather than a chunk. The
line form survives re-chunking but is line-grained, works only for a readable source file, and
carries no stamp that says whether the file has changed since the span was made.

#### Gap 2: A span that has gone wrong is not reported

RDR-053 D5 made an unresolvable span *detectable* through `link_audit`'s stale-span warning. That
warning no longer exists: it went with the SQLite catalog, and the engine-backed `link_audit` returns
no stale-span key, while the MCP tool's docstring still promises two (171-research-4). A span whose
document changed, or whose source disappeared, is today invisible until someone tries to read it,
and even then an unrecognised span reads as "no passage" with no reason given (171-research-3).

## Relationship to Prior RDRs

Searched the RDR index for span, chash, catalog link, content hash and transclusion.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-053 | Origin | Created `chash:` spans. D5: "When chunking changes, spans degrade to unresolvable (detectable via `link_audit()` stale span warning)." The first half still holds for `chash:`; the detection half no longer exists (Gap 2). This RDR extends D5 with a span that does not degrade on re-chunking, and restores detection for the new form. |
| RDR-108 | Precedent | Content-addressed chunk ids and the `document_chunks` manifest as the authority on document structure. This RDR leaves both alone and addresses the document instead of the chunk. |
| RDR-086 | Precedent | The chash span resolution surface (`resolve_span_text_for_entry`). The new form is one more branch there. |
| RDR-152 | Origin of the June phasing | The SQLite-to-Postgres cutover. The June draft held Phase 2 back until it finished. It has finished (171-research-6), so that hold is removed. |
| RDR-180, RDR-236 | Precedent | Full 64-hex chunk hashes (RDR-180), and no document hash on chunk rows (RDR-236). The new span's hash is 64 hex for the same reason and lives on no chunk row. |

## Context

### Background

The request came from Conductus (RDR-001 open question 3), which wanted to map Obsidian
sub-document references to catalog spans and could only approximate them to the containing chunk.
Conductus's RDR-001 is accepted but its repository has been quiet since 2026-08-28 (171-research-6).
The driver now is Sam, who asked on 2026-11-16 for RDR-171 to be finished. The design does not depend
on Conductus: any caller that wants to pin a passage (a citation in a note, a quotation in a review,
a transclusion) needs the same thing.

### Technical Environment

- Catalog links live in the engine's Postgres tables; `from_span` and `to_span` are free strings.
  No layer checks their grammar on write (171-research-3).
- The client resolves a span with `resolve_span_text_for_entry(entry, span, catalog=...)` in
  `catalog_spans.py`, given an already-resolved `CatalogEntry`.
- Every document has a catalog entry with `file_path` and `source_uri`. A document's hash of record
  is `index_content_hash`, but what it hashes differs by ingest path (171-research-1, -2).
- New persistent state goes to Postgres through Liquibase changesets run by the engine; SQLite is
  not a destination (AGENTS.md hot rule).

## Research Findings

### Investigation

A code read of every place a document hash is computed, stored or read; every span parser; the link
write path in the client and the engine; `link_audit`; and where full document text exists after
indexing. Each finding below carries file and line references, recorded as T2 entries
`nexus_rdr/171-research-1` to `-6`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus catalog client (`catalog_spans.py`, `http_catalog_client.py`, `types.py`) | Yes | Span forms, resolver, `index_content_hash` on the entry, `link_audit` keys |
| nexus indexers (`doc_indexer.py`, `prose_indexer.py`, `code_indexer.py`, `indexer.py`, `pipeline_stages.py`, `catalog/store_hook.py`) | Yes | Three different hash bases; PDF text discarded after indexing |
| engine (`CatalogLinksHandler.java`, `catalog-020`, `catalog-048` changesets) | Yes | Spans stored unvalidated; `index_content_hash` has no backfill |

### Key Discoveries

- **✅ Verified** (source search): the document hash has three byte bases today. Raw file bytes for
  single-file markdown, PDFs and the repo register pass; decoded UTF-8 re-encoded, with Windows line
  ends folded to `\n`, for code, and the same with a leading byte-order mark removed for repo prose;
  the note body as given for stored notes. *Source: 171-research-1.*
- **✅ Verified** (source search): `CatalogEntry` carries the hash of record as `index_content_hash`,
  meaningful only when `index_state` is `complete`; older rows carry none and nothing backfills them.
  There is no field named `content_hash`; `meta.content_hash` exists on some rows only.
  *Source: 171-research-2.*
- **✅ Verified** (source search): eleven sites parse or describe spans; none would match a string
  starting `dchash:`; nothing validates span grammar on write; an unrecognised span resolves to
  `None` silently. *Source: 171-research-3.*
- **✅ Verified** (source search): the stale-span report RDR-053 D5 relied on is gone from the
  engine-backed `link_audit`, though the MCP docstring still advertises it. *Source: 171-research-4.*
- **✅ Verified** (source search): no full-text copy of a PDF or a stored note survives indexing;
  only chunks remain. *Source: 171-research-5.*
- **✅ Verified** (source search): the RDR-152 cutover is complete, and the original consumer is
  dormant. *Source: 171-research-6.*

### Critical Assumptions

- [x] A text-file source re-read later yields the same canonical text when its bytes are unchanged
  (the canonical form below is a pure function of the bytes). — **Status**: Verified —
  **Method**: Source Search (Python's strict UTF-8 decode and the normalization below are
  deterministic).
- [ ] The PDF pipeline holds the full extracted text, in the order the chunker reads it, at a point
  before its page buffer is deleted (`pipeline_stages.py:2727`), so Phase 2 can store it. —
  **Status**: Unverified — **Method**: Source Search at Phase 2 Step 1, before the table is used for
  PDFs. If the text is never whole, Phase 2 joins the stored pages in page order and defines that join
  as the PDF's canonical text.

## Proposed Solution

### Approach

Add a fifth span form, **`dchash:<hash>:<start>-<end>`**. Its offsets count characters in the
document's *canonical text*, the whole document as normalized text, not in a chunk. Chunk
boundaries play no part, so the span survives any re-chunking of unchanged text. `<hash>` is the
SHA-256 of that canonical text. It is a stamp: when the document's current canonical text hashes to
something else, the document changed and the span is stale.

The stamp is **its own hash, not the catalog's `index_content_hash`**. The catalog's hash covers
three different byte bases, is missing on older rows, and changing it would mark every document as
changed and force a re-index (171-research-1, -2). The span's hash is computed by the same function
at mint time and at read time, from the same source, so the two agree whenever the text is the same,
whatever the indexer hashed.

Phase 1 resolves from the source file and serves text documents. Phase 2 adds a stored copy of the
canonical text in a new Postgres table, which serves documents with no readable text source (PDFs,
stored notes) and keeps a pinned passage readable after its source file changes.

### Technical Design

**Canonical text.** `canonical_text(raw: bytes) -> str`: decode as strict UTF-8 (an undecodable
source has no canonical text); remove one leading U+FEFF; replace every `\r\n`, then every remaining
`\r`, with `\n`. Nothing else changes: no Unicode normalization, no trailing-whitespace trimming.
`text_hash(text: str) -> str` is the lowercase hex SHA-256 of `text.encode("utf-8")`.

**Span grammar.** `^dchash:([0-9a-f]{64}):(\d+)-(\d+)$`. Offsets are Unicode code points (Python
string indices) into the canonical text, half-open, with `0 <= start < end <= len(text)`. A caller
working in UTF-16 (JavaScript, Obsidian) converts; the mint helper below returns code-point offsets so
most callers never count by hand. A string that starts `dchash:` and does not match is malformed.

**Minting.** `Catalog.dchash_span(tumbler, start, end) -> str` reads the canonical text (source in
Phase 1; source or stored copy in Phase 2), checks the range, and returns the span.
`Catalog.dchash_span_for_passage(tumbler, passage: str) -> str` finds `passage` in the canonical text
and returns its span; it raises when the passage occurs zero times or more than once. Both are
exposed as `nx catalog span` subcommands (`--start/--end` or `--passage`).

**Resolution.** A new branch in `resolve_span_text_for_entry`, ahead of the final `return None`:

1. Parse the span; malformed raises `ValueError` (as a malformed `chash:` span already does).
2. Read the source: `entry.file_path`, else the path of a `file://` `source_uri`. If it reads and its
   canonical text hashes to the span's hash, return `text[start:end]`.
3. Phase 2: otherwise look up the stored copy by the span's hash; if found, return its slice.
4. Otherwise return `None`. The caller learns why from `dchash_status` below.

`dchash_status(entry, span) -> Literal["ok", "stored_copy", "changed", "unreadable", "malformed",
"out_of_range"]` gives the reason without the text: `ok` resolved from the source; `stored_copy`
resolved from Phase 2's copy because the source changed or is gone; `changed` the source reads but
hashes differently and no copy exists; `unreadable` no source and no copy; `out_of_range` the range
exceeds the text.

**Write validation.** `HttpCatalogClient.link` and `link_if_absent` refuse a `from_span` or
`to_span` that starts `dchash:` and does not match the grammar (a `ValueError` naming the span). Other
forms are untouched; the engine is unchanged.

**Stale report.** `link_audit` gains `stale_dchash` and `stale_dchash_count`: every link with a
`dchash:` span whose status is not `ok`, with the status. The MCP `link_audit` docstring loses its two
keys the code no longer returns (`stale_spans`, `stale_chash`) and names the new one; the
`nx catalog link-audit` CLI prints the count. `stored_copy` is listed because the source has moved on,
even though the passage still reads.

**Phase 2 stored copy.** A Liquibase changeset adds `nexus.document_text`:
`(tenant_id, text_hash bytea(32), body text, char_len int, created_at timestamptz)`, primary key
`(tenant_id, text_hash)`, row-level security like the other tenant tables, and an engine route to
put and get by hash. Rows are written:

- at index time for PDFs (the extracted text, canonicalized) and stored notes (the body,
  canonicalized), the documents with no readable text source;
- at mint time for a text document (the canonical text the span was minted against), so the passage
  stays readable after the file changes.

No backfill runs at boot or in the changeset (Sam's 2026-11-10 rule). A PDF or note indexed before
Phase 2 gains a copy at its next index; minting against one without a copy is refused with a message
naming the re-index.

```text
// Illustrative, verify signatures at implementation
def canonical_text(raw: bytes) -> str          # raises UnicodeDecodeError on non-UTF-8
def text_hash(text: str) -> str                # 64 lowercase hex
def parse_dchash_span(span: str) -> tuple[str, int, int]   # ValueError when malformed
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| span parse and resolve branch | `catalog_spans.py` (`parse_chash_span`, `resolve_span_text_for_entry`) | Extend: one parser and one branch beside the existing forms |
| source read | the line-range branch, `catalog_spans.py:606-614` | Reuse its path choice, not its read: the line branch decodes without the canonical normalization |
| document hash | `index_content_hash` (`types.py:210`) | Not used as the stamp (Approach) |
| stale report | `link_audit`, `http_catalog_client.py:3630` | Extend with `stale_dchash` |
| stored copy | none (171-research-5) | New Postgres table |

### Decision Rationale

Document-relative offsets are the only addressing that ignores chunk boundaries entirely, and a hash
stamp makes a changed document detectable rather than silently wrong. Owning the hash, instead of
borrowing the catalog's, avoids touching any indexer and any existing document's identity.

## Alternatives Considered

### Alternative 1: Stamp with the catalog's `index_content_hash`

**Description**: The June draft's choice: the span carries the document's existing file-level hash.

**Pros**:

- No second hash concept.

**Cons**:

- The catalog hash has three byte bases (171-research-1): a resolver hashing the file it reads would
  disagree with it for every code and prose document, and flag them all stale.
- Older rows have no hash at all (171-research-2).
- Unifying the bases means changing every indexer's hash, which changes every document's identity and
  forces a re-index.

**Reason for rejection**: it fails on most of today's documents, and fixing that costs a re-index of
the whole corpus for no benefit to search.

### Alternative 2: Author-supplied anchors (`anchor:<doc>#Heading` or `^blockid`)

**Description**: Store Obsidian anchors as span ids and relocate them at each index.

**Pros**:

- Survives edits as well as re-chunking.

**Cons**:

- Needs anchor extraction at index time and a mapping refreshed on every re-index; only works where
  authors write anchors.

**Reason for rejection**: the bar here is surviving re-chunking. Edit survival is a larger problem and
can be a later RDR on top of this one.

### Briefly Rejected

- **Offsets into the concatenated chunks**: chunk overlaps and separators differ between chunkings,
  so the offsets drift; this is the problem restated.
- **Do nothing**: leaves passage pins breaking on every re-index, and Gap 2 open.

## Trade-offs

### Consequences

- A passage pin survives re-chunking of unchanged text, which is the stated bar.
- An edit to the document makes the span stale, visibly, in `link_audit`. In Phase 2 a span minted
  against a text file still reads from its stored copy after the edit.
- Phase 1 touches no indexer, no schema and no engine code.
- Phase 2 adds durable state: one row per PDF and note version indexed, and one per minted text
  version.

### Risks and Mitigations

- **Risk**: offsets counted in UTF-16 by a JavaScript caller are wrong for text outside the Basic
  Multilingual Plane (emoji, some CJK).
  **Mitigation**: the grammar says code points; the mint helpers return code-point offsets; a test
  pins a non-BMP case.
- **Risk**: `document_text` grows without bound.
  **Mitigation**: a garbage-collection pass (Day 2) deletes rows that no live document version and no
  `dchash:` link names.

### Failure Modes

- A malformed `dchash:` span is refused at write and raises at read; it never resolves silently.
- A source moved without a re-index reads as `unreadable` in Phase 1; `link_audit` lists it.
- A non-UTF-8 text source has no canonical text: minting refuses it with the decode error; resolving
  reports `unreadable`.

## Implementation Plan

### Prerequisites

- [x] Critical assumption 1 verified (source search).
- [ ] Critical assumption 2 verified at Phase 2 Step 1, before PDFs are stored.

### Minimum Viable Validation

On a local engine: index a markdown file, mint a `dchash:` span for a sentence in it, link two
documents with that span, re-index the file with a different chunk size, and read the span back:
the same sentence. Then edit the file and run `link_audit`: the link is listed with status
`changed` (Phase 1) or `stored_copy` (Phase 2).

### Phase 1: Source-backed spans and the stale report

#### Step 1: Canonical text, hash and grammar

`canonical_text`, `text_hash` and `parse_dchash_span` in `catalog_spans.py`, with tests: BOM, CRLF,
lone CR, non-UTF-8, non-BMP offsets, every malformed shape.

#### Step 2: Resolution and status

The `resolve_span_text_for_entry` branch and `dchash_status`, reading `file_path` or a `file://`
`source_uri`. Tests: re-chunk survival (same text, different chunks), changed file, missing file,
out-of-range span.

#### Step 3: Minting

`Catalog.dchash_span` and `Catalog.dchash_span_for_passage`, and `nx catalog span`. Tests: zero and
multiple passage matches refused; the minted span resolves to the passage.

#### Step 4: Write validation

`link` and `link_if_absent` refuse a malformed `dchash:` span. Test with a fake engine.

#### Step 5: The stale report

`stale_dchash` and its count in `link_audit`, the MCP docstring corrected, the CLI count. Test each
status reaches the report.

### Phase 2: The stored copy

#### Step 1: Verify the PDF text point

Read the PDF pipeline to settle critical assumption 2 and write the canonical-text definition for
PDFs into this RDR (an amendment under grant 3 after review) before any PDF row is written.

#### Step 2: Table and engine route

The Liquibase changeset for `nexus.document_text` with its RLS policy, and an engine route to put and
get by hash; engine integration tests for tenant isolation and idempotent put.

#### Step 3: Writers

PDF pipeline and note writes store the canonical text; minting stores the text it minted against.

#### Step 4: Resolution fallback

Step 3 of resolution and the `stored_copy` status, with tests for a deleted source and an edited one.

#### Step 5: Garbage collection

A maintenance pass that deletes rows no live document version and no `dchash:` link names, run as a
visible, resumable job, never at boot.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `dchash:` link spans | `link_audit` | `dchash_status` | ordinary link delete | `link_audit` stale list | with the catalog |
| `nexus.document_text` (Phase 2) | row count in `nx catalog stats` | get by hash | the GC pass | GC dry run lists what it would delete | with the database |

### New Dependencies

None.

## Test Plan

- **Scenario**: same text, re-chunked at a different size — **Verify**: the span returns the same
  passage.
- **Scenario**: file saved with CRLF line ends and a BOM, span minted, file re-saved with LF and no
  BOM — **Verify**: still `ok` (same canonical text).
- **Scenario**: one character inserted before the passage — **Verify**: `changed` in Phase 1,
  `stored_copy` in Phase 2.
- **Scenario**: an emoji before the passage — **Verify**: offsets are code points; the slice is
  exact.
- **Scenario**: `link` with `dchash:abc:1-2` — **Verify**: refused with the span named.
- **Scenario**: PDF indexed under Phase 2, span minted by passage — **Verify**: resolves with the
  source file deleted.

## Validation

### Testing Strategy

Unit tests for the pure functions; client tests against the fake link engine for write validation
and the audit; the Minimum Viable Validation on a local engine for each phase; engine integration
tests for the Phase 2 table.

### Performance Expectations

Resolving a `dchash:` span reads and hashes the whole source file once. `link_audit` does this once
per distinct document among `dchash:` links, not once per link.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and proposed solution. The
June draft's stamp (the catalog's file-level hash) contradicted 171-research-1 and -2; the stamp is
now the span's own hash.

### Assumption Verification

Assumption 1 is verified by source search. Assumption 2 is unverified and is the first step of
Phase 2, before any PDF row is written; Phase 1 does not depend on it.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `bytes.decode("utf-8")` strict, `hashlib.sha256` | Python stdlib | Source Search |
| `resolve_span_text_for_entry`, `link`, `link_if_absent`, `link_audit` | nexus catalog client | Source Search (171-research-3, -4) |

### Scope Verification

The Minimum Viable Validation is in scope for each phase and runs on a local engine before the phase
closes.

### Cross-Cutting Concerns

- **Versioning**: the span grammar is additive; old clients store a `dchash:` span as a string and
  resolve it to `None`, as they do any unknown form.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: Phase 1 is client-only. Phase 2 adds one changeset and one engine route,
  shipped in an engine cut; the client falls back to Phase 1 behaviour against an older engine.
- **IDE compatibility**: N/A
- **Incremental adoption**: existing spans are untouched; nothing is rewritten.
- **Secret/credential lifecycle**: N/A
- **Memory management**: resolution holds one document's text at a time.

### Proportionality

Right-sized: one span form, one resolver branch, one report key, and one table in Phase 2.

## References

- `src/nexus/catalog/catalog_spans.py`, `http_catalog_client.py`, `types.py`
- `src/nexus/doc_indexer.py`, `prose_indexer.py`, `code_indexer.py`, `indexer.py`, `pipeline_stages.py`,
  `catalog/store_hook.py`, `catalog/note_write.py`
- `service/src/main/java/dev/nexus/service/http/CatalogLinksHandler.java`,
  `service/src/main/resources/db/changelog/catalog-020-index-run-fence.xml`, `catalog-048-reconcile.xml`
- T2 `nexus_rdr/171-research-1` to `-6`
- Conductus RDR-001 open question 3; design memo T2 `nexus/design-finegrain-author-anchored-spans.md`

## Revision History

- 2026-06-26: drafted (source-only Phase 1, stored copy Phase 2 behind the RDR-152 cutover).
- 2026-11-16: rewritten against the current code (171-research-1 to -6): the stamp is the span's own
  hash over a defined canonical text, not the catalog's file hash; the stale report is new code;
  Phase 2 is a Postgres table with no cutover dependency; Sam named as the driver.
