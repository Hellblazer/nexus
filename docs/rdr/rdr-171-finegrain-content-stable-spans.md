---
title: "Fine-Grain Content-Stable Catalog Spans: A dchash Document-Offset Span Form That Survives Re-Chunking"
id: RDR-171
type: Architecture
status: accepted
accepted_date: 2026-11-16
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-26
related_issues: [nexus-3kybd]
related_rdrs: [RDR-053, RDR-108, RDR-086, RDR-152, RDR-180, RDR-236, RDR-243]
supersedes: []
external_ref: conductus RDR-001 OQ-3
---

# RDR-171: Fine-Grain Content-Stable Catalog Spans

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus keeps a *catalog*: one entry per indexed document, each addressed by a *tumbler* (a stable
dotted id such as `1.2.45`), with *links* between entries. A link can point at part of a document,
not only the whole of it. The part is named by a *span*, a short string stored in the link's
`from_span` or `to_span`. Documents are stored for search as *chunks*, pieces of bounded size. Today there are four span forms (`src/nexus/catalog/catalog_spans.py`, census in
171-research-3):

| Form | Meaning | Survives re-chunking? |
|---|---|---|
| `""` | the whole document | n/a |
| `42-57` | lines 42 to 57 of the source file | yes, but line-grained and unstamped |
| `3:100-250` | characters 100 to 250 of chunk number 3 | no: chunk numbers move |
| `chash:<hex>[:start-end]` | the chunk whose text hashes to `<hex>` (a *chunk hash*), optionally a character range in it | no: the chunk's text, and so its hash, changes |

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

RDR-053 D5 made an unresolvable span *detectable* through `link_audit`, the catalog's link
health report. That report no longer lists stale spans: the check went with the old SQLite catalog,
and today's `link_audit` returns no stale-span key, while the MCP tool's docstring still promises
two (171-research-4). A span whose document changed, or whose source disappeared, is invisible until
someone tries to read it, and even then an unrecognised span reads as "no passage" with no reason
given (171-research-3). This RDR closes the gap for the new form only; the four existing forms stay
unreported, as they are today.

## Relationship to Prior RDRs

Searched the RDR index for span, chash, catalog link, content hash and transclusion (showing one
document's passage inside another).

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-053 | Origin | Created `chash:` spans. D5: "When chunking changes, spans degrade to unresolvable (detectable via `link_audit()` stale span warning)." The first half still holds for `chash:`; the detection half no longer exists (Gap 2). D5 also rests on nexus not storing raw source text; this RDR keeps that: it stores no document text. |
| RDR-108 | Precedent | Content-addressed chunk ids and the `document_chunks` manifest as the authority on document structure. This RDR leaves both alone and addresses the document instead of the chunk. |
| RDR-086 | Precedent | The chash span resolution surface (`resolve_span_text_for_entry`). The new form is one more branch there. |
| RDR-152 | Origin of the June phasing | The SQLite-to-Postgres cutover. The June draft held its stored-copy phase behind it. The cutover has finished (171-research-6); the stored-copy phase has left this RDR (Decision Rationale). |
| RDR-180, RDR-236 | Precedent | Full 64-hex chunk hashes (RDR-180), and no document hash on chunk rows (RDR-236). The new span's hash is 64 hex for the same reason and lives on no chunk row. |

## Context

### Background

The request came from Conductus, a separate project that connects an Obsidian notes vault to the
nexus catalog. Its RDR-001 (open question 3) wanted to map Obsidian links to a heading or block of a
note onto catalog spans, and could only approximate them to the containing chunk. Conductus's
RDR-001 is accepted but its repository has been quiet since 2026-08-28 (171-research-6). The driver
now is Sam, who asked on 2026-11-16 for RDR-171 to be finished. Conductus's own vault notes, indexed as
stored notes and edited often, fall outside this RDR's scope (171-research-10). The design does not depend on
Conductus: any caller that wants to pin a passage (a citation in a note, a quotation in a review, a
transclusion) needs the same thing.

### Technical Environment

- Catalog links live in Postgres tables owned by the *engine*, the Java service behind every
  nexus client. `from_span` and `to_span` are free strings; no layer checks their grammar on write
  (171-research-3).
- The Python client resolves a span with `resolve_span_text_for_entry(entry, span, catalog=...)` in
  `catalog_spans.py`, given an already-resolved `CatalogEntry`.
- A catalog entry carries `file_path` and `source_uri`. `file_path` is repo-relative for documents
  indexed from a repository and absolute for single files outside any git checkout, which belong to
  a *curator* owner (an owner with no repository); `source_uri` is a `file://` URI for a file, an `nx-note://` URI for a
  note, other schemes stored as given, and empty on legacy rows and for a relative path whose owner
  has no repository root (171-research-7, -9). `HttpCatalogClient.resolve_path(tumbler)` turns a
  repository document into an absolute path and returns `None` for a curator-owned one
  (171-research-9).
- A document's hash of record is `index_content_hash`, but what it hashes differs by ingest path
  (171-research-1, -2).

## Research Findings

### Investigation

A code read of every place a document hash is computed, stored or read; every span parser; the link
write path in the client and the engine; `link_audit`; where full document text exists after
indexing; and, after gate round 1, path resolution, the resolver's error handling and the client's
write paths. Each finding carries its source (file and line references for code), recorded as T2
entries `nexus_rdr/171-research-1` to `-10`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus catalog client (`catalog_spans.py`, `http_catalog_client.py`, `types.py`) | Yes | Span forms, resolver, `resolve_path`, the three link write paths, `link_audit` keys, file size |
| nexus indexers (`doc_indexer.py`, `prose_indexer.py`, `code_indexer.py`, `indexer.py`, `pipeline_stages.py`, `catalog/store_hook.py`) | Yes | Three different hash bases; repo-relative `file_path` |
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
- **✅ Verified** (source search): the sites that parse or describe spans would not match a string
  starting `dchash:`; nothing validates span grammar on write; an unrecognised span resolves to
  `None` silently. *Source: 171-research-3.*
- **✅ Verified** (source search): the stale-span report RDR-053 D5 relied on is gone from
  `link_audit`, though the MCP docstring still advertises it. *Source: 171-research-4.*
- **✅ Verified** (source search): no full-text copy of a PDF survives indexing; only chunks remain.
  For stored notes no separate body table was found, but the engine schema was not checked.
  *Source: 171-research-5.*
- **✅ Verified** (source search): the RDR-152 cutover is complete, and the original consumer is
  dormant. *Source: 171-research-6.*
- **✅ Verified** (source search): a repo-indexed document's `file_path` is repo-relative;
  `resolve_path` anchors it on the owner's repository root, and `aspect_readers.file_uri_path`
  turns a `file://` URI into a path. *Source: 171-research-7.*
- **✅ Verified** (source search): `resolve_path` returns `None` for a curator-owned document, and
  a single file outside any git checkout registers under a curator owner by absolute path with a
  `file://` `source_uri` (one inside a checkout registers under that repository's owner,
  `doc_indexer._repo_home_for`);
  `src/nexus/catalog/` holds 40 modules, its cap. *Source: 171-research-9.*
- **✅ Verified** (source search): the resolver turns every exception in its chash branch into
  `None` except an engine outage (`VectorServiceError`), which it re-raises (171-research-9), and
  `nx catalog show` catches only that error around it; links reach the engine through
  three client methods (`link`, `link_if_absent`, `links_upsert_many`); no class named `Catalog`
  exists; `http_catalog_client.py` is 4 lines under its size ceiling. *Source: 171-research-8.*

### Critical Assumptions

- [x] A source whose bytes are unchanged yields the same canonical text, and a different document at
  the same path yields a different hash. The canonical form is a pure function of the bytes, and
  SHA-256 is collision-resistant, so a span never resolves against text it was not minted on. —
  **Status**: Verified — **Method**: Source Search (Python's strict UTF-8 decode, the normalization
  below and `hashlib.sha256` are deterministic).

## Proposed Solution

### Approach

Add a fifth span form, **`dchash:<hash>:<start>-<end>`** ("document content hash"). Its offsets
count characters in the document's *canonical text*, the whole document as normalized text, not in
a chunk. Chunk boundaries play no part, so the span survives any re-chunking of unchanged text.
`<hash>` is the SHA-256 of that canonical text. It is a stamp: when the document's current canonical
text hashes to something else, the document changed and the span is stale.

The stamp is **its own hash, not the catalog's `index_content_hash`**. The catalog's hash covers
three different byte bases, is missing on older rows, and changing it would mark every document as
changed and force a re-index (171-research-1, -2). The span's hash is computed by the same function
at mint time and at read time, from the same source, so the two agree whenever the text is the same,
whatever the indexer hashed.

The span serves text documents with a readable source file: markdown, code and plain text. Nexus
stores no copy of document text for it. PDFs (whose files are binary) and stored notes (which have
no file) are out of scope, and an edit to a source file makes its spans stale rather than keeping the
old passage readable. A stored copy of document text that would cover those cases is left to a
separate RDR when a caller needs it (Sam, 2026-11-16, after gate round 1).

### Technical Design

The new logic lives in a new subpackage, `src/nexus/catalog/dchash/`, because `src/nexus/catalog/`
is at its 40-module cap (171-research-9); existing files gain only calls into it. The client file
`http_catalog_client.py` is 4 lines under its size ceiling (171-research-8), so `link_audit` moves
out of it into the subpackage first.

**Canonical text.** `canonical_text(raw: bytes) -> str`: decode as strict UTF-8 (an undecodable
source has no canonical text); remove one leading U+FEFF; replace every `\r\n`, then every remaining
`\r`, with `\n`. Nothing else changes: no Unicode normalization, no trailing-whitespace trimming.
`text_hash(text: str) -> str` is the lowercase hex SHA-256 of `text.encode("utf-8")`.

**Span grammar.** The whole string must match `dchash:([0-9a-f]{64}):(0|[1-9][0-9]*)-(0|[1-9][0-9]*)`
(ASCII digits, no leading zeros, nothing after the end), with `start < end`. Offsets are Unicode
code points (Python string indices) into the canonical text, half-open, with `end <= len(text)`. A
caller working in UTF-16 (JavaScript, Obsidian) converts. A string that starts `dchash:` and fails
the grammar is malformed. `parse_dchash_span(span) -> tuple[str, int, int]` raises `ValueError` on a
malformed span.

**Source lookup.** `dchash_source(entry, catalog) -> Path | None`, trying in order: 
`catalog.resolve_path(entry.tumbler)` when a catalog is given (it anchors a repo-relative
`file_path` on the repository root, 171-research-7); then, when that gives `None`, as it does for a
curator-owned single file (171-research-9), or no catalog is given, the path
`aspect_readers.file_uri_path` gives for a `file://` `source_uri`; then an absolute `file_path`;
otherwise `None`.

**Minting.** `dchash_span(catalog, tumbler, start, end) -> str` reads the source's canonical text,
checks `start < end <= len(text)`, and returns the span. `dchash_span_for_passage(catalog, tumbler,
passage: str) -> str` refuses an empty passage, applies the same line-end normalization to it, finds
it in the canonical text, and returns its span; it raises when the passage occurs zero times or more
than once, overlapping occurrences included. Both refuse a PDF (`content_type` `pdf`) and a document
with no file source by name before reading anything. Both are functions taking a catalog reader, so
the client's write whitelist does not change. `nx catalog span <tumbler> --passage TEXT` (or
`--start/--end`) prints a span; it lives in a new `commands/catalog_cmds/span.py`, and its help
recommends `--passage`.

**Resolution.** A new branch in `resolve_span_text_for_entry`, before its final `return None`, for a
span starting `dchash:`. It parses, reads the source through `dchash_source`, and returns
`text[start:end]` when the canonical text hashes to the span's hash and the range fits; otherwise
`None`. Every exception inside it, a malformed span's `ValueError` included, becomes `None`. That includes the catalog client's
engine errors (`httpx.HTTPError` and `RuntimeError` from `resolve_path`): the branch makes no
vector-store call, so it has no `VectorServiceError` to re-raise, and `nx catalog show` never fails
on a dchash span (171-research-8, -9).

**Status.** `dchash_status(entry | None, span, catalog) -> str` says why one span does or does not
resolve against the entry on its own side of the link (`from_span` against the `from` document,
`to_span` against the `to` document), without the text: `ok`; `malformed`; `missing` (the caller
found no entry, or a deleted one, and passes `None`);
`unreadable` (no source path, the file cannot be read, or it is not UTF-8); `changed` (the source
reads but hashes differently); `out_of_range` (the range exceeds the text). An engine error is not a
status: `httpx.HTTPError` and `RuntimeError` from `resolve_path` propagate, so an outage fails the
audit instead of listing every span as stale.

**Write validation.** The client's three link write paths, `link`, `link_if_absent` and
`links_upsert_many`, refuse a `from_span` or `to_span` that starts `dchash:` and fails the grammar
(a `ValueError` naming the span). `links_upsert_many` checks every edge before its first request, so a
bad edge on a later page writes nothing. The recovery-bundle import, which calls `link` per record
(171-research-9), counts such a refusal in its summary and goes on to the next record. Other forms are
untouched; the engine is unchanged, so another client could still store a malformed span; the
resolver and the report treat it as `malformed`. The span help in `nx catalog link` and the MCP link
tool's field description name the new form.

**Stale report.** `link_audit` gains `stale_dchash` and `stale_dchash_count`: one row per `dchash:`
span whose status is not `ok`, naming the link, the side (`from` or `to`) and the status. They come
from `dchash_audit(links, catalog) -> list[dict]`, which groups the `dchash:` spans by document, reads
each document once, and keeps only its hash and length while it checks that document's spans. The report is local to the machine it runs on, because it reads source files by
path. The MCP `link_audit` docstring loses the two keys the code no longer returns (`stale_spans`,
`stale_chash`), and the paragraph explaining them, and names the two new keys; `nx catalog link-audit` prints the count.

```text
// Illustrative, verify signatures at implementation
def canonical_text(raw: bytes) -> str                      # UnicodeDecodeError on non-UTF-8
def text_hash(text: str) -> str                            # 64 lowercase hex
def parse_dchash_span(span: str) -> tuple[str, int, int]   # ValueError when malformed
def dchash_status(entry, span: str, catalog) -> str        # ok | malformed | missing | unreadable | changed | out_of_range
def dchash_audit(links: list[dict], catalog) -> list[dict] # the stale_dchash rows, one read per document
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| span parse, canonical text, mint, status, audit | none | New subpackage `src/nexus/catalog/dchash/` (module cap, 171-research-9) |
| resolve branch | `catalog_spans.py` `resolve_span_text_for_entry` | Extend: one branch calling the new module |
| source lookup | `HttpCatalogClient.resolve_path`, `aspect_readers.file_uri_path` | Reuse both (171-research-7) |
| document hash | `index_content_hash` (`types.py:210`) | Not used as the stamp (Approach) |
| stale report | `link_audit`, `http_catalog_client.py:3630` | Move into the subpackage (size ceiling, 171-research-8), then extend with `stale_dchash` |

### Decision Rationale

Document-relative offsets are the only addressing that ignores chunk boundaries entirely, and a hash
stamp makes a changed document detectable rather than silently wrong. Owning the hash, instead of
borrowing the catalog's, touches no indexer and no existing document's identity. Reading the source
file, and storing no text, keeps RDR-053's position that nexus does not hold raw source, and keeps
the change client-only. Gate round 1 showed that a stored copy (for PDFs, notes and edited files) is
its own design, with its own table, writers, clean-up and sensitivity rules; Sam chose to leave it to
a later RDR.

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

### Alternative 2: A stored copy of each document's text

**Description**: The June draft's Phase 2: a Postgres table of canonical text keyed by hash, written
at index time for PDFs and notes and at mint time for text files, so spans resolve without a source
and survive edits.

**Pros**:

- Covers PDFs and notes; keeps an edited file's old passage readable.

**Cons**:

- Gate round 1: a table keyed by hash alone cannot find a document's current text or decide which
  rows are still used; it needs an association table, a clean-up job with a grace period, RDR-243
  sensitivity handling, a job to fill existing documents, and an engine version check.
- Reverses RDR-053's position that nexus stores no raw source text.

**Reason for rejection**: deferred, not refused. It is a separate design and becomes its own RDR when
a caller needs PDFs, notes or edit survival (Sam, 2026-11-16).

### Alternative 3: Author-supplied anchors (`anchor:<doc>#Heading` or `^blockid`)

**Description**: Store Obsidian anchors as span ids and relocate them at each index.

**Pros**:

- Survives edits as well as re-chunking.

**Cons**:

- Needs anchor extraction at index time and a mapping refreshed on every re-index; only works where
  authors write anchors.

**Reason for rejection**: the bar here is surviving re-chunking. Edit survival is a larger problem.

### Briefly Rejected

- **Offsets into the concatenated chunks**: chunk overlaps and separators differ between chunkings,
  so the offsets drift; this is the problem restated.
- **Do nothing**: leaves passage pins breaking on every re-index, and Gap 2 open.

## Trade-offs

### Consequences

- A passage pin in a text document survives re-chunking of unchanged text, which is the stated bar.
- An edit to the document makes the span stale, visibly, in `link_audit`.
- PDFs and stored notes cannot carry a `dchash:` span; minting against one is refused.
- The change is client-only: no indexer, schema or engine code changes.

### Risks and Mitigations

- **Risk**: offsets counted in UTF-16 by a JavaScript caller are wrong for text outside the Basic
  Multilingual Plane (emoji, some CJK).
  **Mitigation**: the grammar says code points; `--passage` minting returns code-point offsets; a test
  pins a non-BMP case.
- **Risk**: a caller counting offsets in an editor that shows Windows line ends or a byte-order mark
  is off by those characters, and the wrong passage still fits the range.
  **Mitigation**: the CLI and docs steer callers to `--passage`, which finds the text itself.

### Failure Modes

- A malformed `dchash:` span is refused by the Python client's three write paths; one stored by
  another client resolves to `None` and appears in `link_audit` as `malformed`.
- A source moved or deleted reads as `unreadable`; an edited one as `changed`. Both appear in
  `link_audit`.
- A non-UTF-8 source has no canonical text: minting refuses it with the decode error; status reports
  `unreadable`.
- `nx catalog show` never crashes inside the `dchash:` branch, because it turns every error,
  the catalog client's `httpx.HTTPError` and `RuntimeError` included, into `None`. The status and
  the audit let those two propagate.

## Implementation Plan

### Prerequisites

- [x] The critical assumption is verified (source search).

### Minimum Viable Validation

On a local engine: index a markdown file from a repository (so its `file_path` is repo-relative)
and a second one with `nx index md` (curator-owned, found through its `file://` `source_uri`), mint a
`dchash:` span with `--passage` for a sentence in each, link documents with those spans from a working
directory outside the repository, force a re-index of both (`--force`), and read the spans back: the
same sentences. Then edit one file and run `link_audit`: its span is listed with status `changed`.
Survival across a different chunk size is pinned by the Step 2 unit test, since no command sets the
chunk size.

### Phase 1: Code Implementation

#### Step 0: The subpackage, and `link_audit` moved

Create `src/nexus/catalog/dchash/` and move `link_audit` out of `http_catalog_client.py` into it,
unchanged, so the client file has room for the calls the later steps add. The existing `link_audit`
tests pass unchanged.

#### Step 1: Canonical text, hash and grammar

`canonical_text`, `text_hash` and `parse_dchash_span` in the subpackage. Tests: BOM, CRLF, lone CR,
non-UTF-8, non-BMP offsets, `start >= end`, leading zeros, non-ASCII digits, a trailing newline,
every other malformed shape.

#### Step 2: Source lookup, resolution and status

`dchash_source`, the `resolve_span_text_for_entry` branch and `dchash_status`. Tests: re-chunk
survival (same text, different chunks); a repo-relative `file_path` resolved from a working directory
outside the repository; a curator-owned single file with a catalog passed, found through its
`file://` `source_uri`; a `file://` `source_uri` with no catalog; changed, missing, unreadable and
out-of-range spans; a malformed span returns `None`; `resolve_path` raising `httpx.ConnectError` or
`RuntimeError` makes the resolver return `None` and `dchash_status` raise; `nx catalog show` on a
document with a stale `dchash:` link prints it and the rest of its links.

#### Step 3: Minting

`dchash_span`, `dchash_span_for_passage` and `nx catalog span`. Tests: empty, zero, multiple and
overlapping passage matches refused; a passage typed with CRLF found; `start >= end` refused; a PDF
or a note refused by name; the minted span resolves to the passage.

#### Step 4: Write validation

`link`, `link_if_absent` and `links_upsert_many` refuse a malformed `dchash:` span, the recovery-bundle
import counts the refusal and goes on, and the span help names the new form. Tests against the fake
link engine: one per write path; a bad edge on the second page of `links_upsert_many` writes nothing;
a bundle with one malformed span imports the rest and reports one failure.

#### Step 5: The stale report

`dchash_audit`, `stale_dchash` and its count in `link_audit`, the MCP docstring corrected, the CLI
count. Tests: each non-`ok` status reaches the report with its side; a document with many spans is
read once; an engine error fails the audit instead of listing stale spans.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `dchash:` link spans | `link_audit` | `dchash_status` | ordinary link delete | `link_audit` stale list | with the catalog |

No other persistent resource is created.

### New Dependencies

None.

## Test Plan

- **Scenario**: same text, re-chunked at a different size — **Verify**: the span returns the same
  passage.
- **Scenario**: repo-indexed document, span read from a working directory outside the repository —
  **Verify**: resolves (`resolve_path` anchors the relative path).
- **Scenario**: a single file indexed with `nx index md`, span minted and read with a catalog —
  **Verify**: resolves through its `file://` `source_uri`.
- **Scenario**: file saved with CRLF line ends and a BOM, span minted, file re-saved with LF and no
  BOM — **Verify**: still `ok` (same canonical text).
- **Scenario**: one character inserted before the passage — **Verify**: `changed`, listed in
  `link_audit`.
- **Scenario**: an emoji before the passage — **Verify**: offsets are code points; the slice is
  exact.
- **Scenario**: `link`, `link_if_absent` or `links_upsert_many` with `dchash:abc:1-2`,
  `dchash:<64 hex>:7-3` or `dchash:<64 hex>:01-3` — **Verify**: refused with the span named, and
  nothing written.
- **Scenario**: a stored malformed `dchash:` span — **Verify**: `nx catalog show` lists the
  document's links; `link_audit` reports `malformed`.

## Validation

### Testing Strategy

Unit tests for the pure functions; client tests against the fake link engine for write validation
and the audit; the Minimum Viable Validation on a local engine before the phase closes.

### Performance Expectations

Resolving a `dchash:` span reads and hashes the whole source file once. `link_audit` does this once
per distinct document among `dchash:` links, not once per link.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and proposed solution. The
June draft's stamp (the catalog's file-level hash) contradicted 171-research-1 and -2, and its source
lookup contradicted 171-research-7; the stamp is now the span's own hash and the lookup goes through
`resolve_path`.

### Assumption Verification

The one critical assumption is verified by source search.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `bytes.decode("utf-8")` strict, `hashlib.sha256` | Python stdlib | Source Search |
| `resolve_path`, `link`, `link_if_absent`, `links_upsert_many`, `link_audit` | nexus catalog client | Source Search (171-research-4, -7, -8) |
| `aspect_readers.file_uri_path` | nexus | Source Search (171-research-7) |
| `resolve_path` returning `None` for curator owners | nexus catalog client | Source Search (171-research-9) |

### Scope Verification

The Minimum Viable Validation is in scope and runs on a local engine before the phase closes.

### Cross-Cutting Concerns

- **Versioning**: the span grammar is additive; an older client stores a `dchash:` span as a string
  and resolves it to `None`, as it does any unknown form.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: client-only; no engine cut.
- **IDE compatibility**: N/A
- **Incremental adoption**: existing spans are untouched; nothing is rewritten.
- **Secret/credential lifecycle**: N/A
- **Memory management**: resolution holds one document's text at a time.
- **Sensitive documents (RDR-243)**: no text is stored or sent anywhere; a span carries only a hash
  and two offsets, and resolving one reads a file already on the caller's machine, as the line form
  does.

### Proportionality

Right-sized: one span form, one new subpackage, one resolver branch, one report key and its count,
no schema.

## References

- `src/nexus/catalog/catalog_spans.py`, `http_catalog_client.py`, `types.py`, `recovery_bundle.py`,
  `src/nexus/aspect_readers.py`, `src/nexus/mcp/catalog.py`, `src/nexus/commands/catalog_cmds/links.py`,
  `scripts/file_size_ratchet.py`
- `src/nexus/doc_indexer.py`, `prose_indexer.py`, `code_indexer.py`, `indexer.py`, `pipeline_stages.py`,
  `catalog/store_hook.py`, `catalog/note_write.py`, `commands/catalog.py`
- `service/src/main/java/dev/nexus/service/http/CatalogLinksHandler.java`,
  `service/src/main/resources/db/changelog/catalog-020-index-run-fence.xml`, `catalog-048-reconcile.xml`
- T2 `nexus_rdr/171-research-1` to `-10`; `nexus_rdr/171-scope-decision-2026-11-16`
- Conductus RDR-001 open question 3; design memo T2 `nexus/design-finegrain-author-anchored-spans.md`

## Revision History

- 2026-06-26: drafted (source-only Phase 1, stored copy Phase 2 behind the RDR-152 cutover).
- 2026-11-16: rewritten against the current code (171-research-1 to -6): the stamp is the span's own
  hash over a defined canonical text, not the catalog's file hash; the stale report is new code;
  Sam named as the driver.
- 2026-11-16: Gate round 1 — BLOCKED (3 Critical, 7 Significant, 3 ship-blocker(s)); blob `3b9d78764797`; critique `nexus_rdr/171-gate-critique-2026-11-16-r1`.
- 2026-11-16: scope narrowed to source-backed text documents; the stored copy left to a later RDR
  (Sam, `nexus_rdr/171-scope-decision-2026-11-16`).
- 2026-11-16: Gate round 2 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); blob `82011306dcb4`; critique `nexus_rdr/171-gate-critique-2026-11-16-r2`.
