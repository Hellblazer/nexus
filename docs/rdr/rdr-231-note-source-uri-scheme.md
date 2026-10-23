---
title: "Note source_uri scheme: replace chroma:// with nx-note://"
id: RDR-231
type: Technical Debt
status: draft
priority: low
author: nexus_8f2
reviewed-by: self
created: 2026-10-23
accepted_date:
related_issues: [nexus-t7fbb]
---

# RDR-231: Note source_uri scheme: replace chroma:// with nx-note://

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

## Problem Statement

A note written by MCP `store_put`, `nx store put` or `nx memory promote` is a catalog
Document whose identity is its `source_uri`: the string the catalog uses to find
"the same document" again on the next write. For notes that string is
`chroma://<collection>/<title>`. Chroma, the vector store that scheme was named for,
was retired in RDR-155. The name now misleads anyone reading `nx catalog show`
(the cross-surface shakeout reported it as a bug, nexus-t7fbb). Sam's ruling
(2026-10-23): replace the scheme as one change covering the writer, the engine's
rename rewrite and a backfill of existing rows, riding an engine tag after the
RDR-007 A2 freeze.

### Enumerated gaps to close

#### Gap 1: Note identity carries a retired product's name

`aspect_readers.uri_for` (`src/nexus/aspect_readers.py:206`) mints
`chroma://<collection>/<title>` for every non-file collection, and the store hook
uses it as the note's identity (`src/nexus/catalog/store_hook.py:883`). The
scheme resolves nothing against Chroma; it is an opaque, collection-scoped
identity. The fix delivers a scheme whose name says what it is.

#### Gap 2: The scheme is spelled independently at every site that knows it

There is no single definition. The client mints it in `aspect_readers.uri_for`
and three times in `aspect_extractor.py` (:1301, :1414, :1577); classifies it by
prefix in `commands/catalog_cmds/reconcile_stale.py:230` (`_STORE_PUT_URI_PREFIX`),
its sibling in `integrity.py`, `backfill.py` and the local reimplementation in
`catalog/recovery_bundle.py`; reads it in `aspect_readers` (chunk reassembly,
content-addressed stat) and `doctor_references`; and synthesizes it in
`exporter.py:627`. The engine rewrites `chroma://<old>/` to `chroma://<new>/` on a
collection rename (`CatalogRepository.java:9713-9718`, nexus-sis0m.3). A change
that misses one site leaves notes that a classifier, reader or rename treats as
something else. The fix delivers one definition per side and a test that no
other spelling survives.

#### Gap 3: Changing the writer alone duplicates every re-put note

A re-put finds its Document by `source_uri`. If the writer mints `nx-note://`
while existing rows hold `chroma://`, the lookup misses and a second Document is
minted beside the first. Old clients (any install not yet upgraded) and old
recovery bundles and exports keep sending `chroma://` indefinitely, so the hazard
outlives any one release. The fix must make both spellings name one Document,
for old clients as well as new.

## Relationship to Prior RDRs

Searched the RDR titles and bodies for note identity, `source_uri`, the store hook,
rename and Chroma retirement.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-108 (graph identity normalization) | Origin | Made `source_uri` the Document identity; this RDR keeps that rule and changes only the note scheme's spelling. |
| RDR-155 (pgvector T3 consolidation) | Origin of the debt | Retired Chroma (P4b) and left the scheme name behind. |
| RDR-192 (title-only note identity) | Precedent | Defined the title-only note identity this scheme encodes; its convergence rule (re-put reaches the same Document) is the constraint in Gap 3. |
| RDR-210 (dual-mode local engine) | Adjacent | Owns the rename verbs whose rewrite (nexus-sis0m.3) must learn the new scheme. |

## Context

### Background

Found by the 2026-10-22 cross-surface shakeout (board/nexus-shakeout,
cowork round 3, F15/F16): `nx catalog show 1.11.782` printed
`chroma://knowledge__nexus-testing__voyage-context-3__v1/cowork-shakeout-probe-2026-10-21`.
The 2026-10-23 analysis on nexus-t7fbb traced the sites in Gap 2.

### Technical Environment

Client: `src/nexus/aspect_readers.py`, `catalog/store_hook.py`, the catalog
commands named above, `exporter.py`, `catalog/recovery_bundle.py`. Engine:
`CatalogRepository` (rename rewrite, document register/upsert, `source_uri`
lookup), Liquibase changelogs. The live-URI partial unique index
(`catalog-016-source-uri-unique.xml`: unique `(tenant_id, source_uri)` over live
rows) constrains any rewrite.

## Research Findings

### Investigation

Source reading of every site in Gap 2 (client and engine), the catalog-016 index,
and the two changelogs that mention the scheme in comments
(`hygiene-001-not-null.xml:93`, `aspects-004-doc-id-backfill.xml:30`). 23 test
files name `chroma://`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus engine `CatalogRepository` | Yes | Rename rewrites the `chroma://<old>/` prefix and maps a collision on the live-URI index to 409 (`SourceUriCollision`). |
| catalog-016 index | Yes | Unique over live rows per tenant; a backfill that maps two rows onto one URI fails. |

### Key Discoveries

- **Documented**: `chroma://` is the store_put-origin marker for every non-file prefix (`aspect_readers.py:85-95`), unrelated to ChromaDB.
- **Documented**: the classifiers use the prefix to tell a note from migration residue (`recovery_bundle.py:28-40`); a scheme they do not know reads as residue.
- **Documented**: the engine rename rewrite is prefix-based (`CatalogRepository.java:9717-9718`).
- **Assumed**: no table other than `catalog_documents` stores a note's `source_uri` as a key that must be rewritten with it (document_aspects history suggests it once did; see Critical Assumptions).

### Critical Assumptions

- [ ] Only `catalog_documents.source_uri` holds the scheme as data that must be backfilled; any other table keys notes by `doc_id`/tumbler. — **Status**: Unverified — **Method**: Source Search (all changelogs and repositories for `source_uri` columns)
- [ ] No tenant has a live `chroma://X/Y` row and a live `nx-note://X/Y` row at once, so the backfill cannot collide on catalog-016. — **Status**: Unverified — **Method**: Source Search plus a pre-backfill census query in the changeset's precondition
- [ ] The engine can canonicalize an incoming `chroma://` URI at every write and lookup route (register, upsert, by-source-uri lookup, manifest writes that carry identity) in one helper. — **Status**: Unverified — **Method**: Source Search

## Proposed Solution

### Approach

Canonicalize on the engine, then switch the writer. The engine accepts both
spellings forever and stores only the new one; the client mints only the new one
once the engine is deployed; a backfill rewrites existing rows.

### Technical Design

1. **One definition per side.** Client: a module constant pair (`NOTE_URI_SCHEME = "nx-note://"`, `LEGACY_NOTE_URI_SCHEME = "chroma://"`) and one `is_note_uri(uri)` predicate used by every classifier and reader in Gap 2. Engine: one `canonicalNoteUri(String)` that maps `chroma://` to `nx-note://` and passes every other scheme through.
2. **Engine canonicalization (closes Gap 3 for old clients).** Every route that writes or looks up a Document by `source_uri` canonicalizes first, so an old client's `chroma://` write lands on, and finds, the `nx-note://` row. The rename rewrite matches both prefixes and writes the new one.
3. **Backfill (DATA EFFECT).** One Liquibase changeset rewrites live and tombstoned `catalog_documents.source_uri` from `chroma://` to `nx-note://`, guarded by a precondition that counts would-be collisions on catalog-016 and halts on any.
4. **Client writer switch.** `uri_for` and the aspect extractor mint `nx-note://`; readers and classifiers accept both through `is_note_uri`. The exporter and recovery bundle write the new scheme and read both.
5. **Read-accept forever.** `chroma://` stays readable on both sides for old bundles and exports; nothing ever refuses it.

```text
// Illustrative: engine helper
static String canonicalNoteUri(String uri)  // "chroma://" + rest -> "nx-note://" + rest; else uri
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Client scheme constants | `reconcile_stale._STORE_PUT_URI_PREFIX`, `aspect_readers` literals | Replace: one definition, every site imports it |
| Engine canonicalizer | rename prefix rewrite in `CatalogRepository` | Extend: the rewrite calls it |
| Backfill | none | New changeset |

### Decision Rationale

Engine-side canonicalization is the only option that keeps an un-upgraded client
from minting duplicates after the backfill; a client-side dual lookup protects
only clients that have the change.

## Alternatives Considered

### Alternative 1: Keep chroma:// and document it as opaque identity

**Description**: No code change; document the scheme as historical.
**Pros**: Zero risk, no DATA EFFECT.
**Cons**: The misleading name stays on every note.
**Reason for rejection**: Sam ruled for the change (2026-10-23).

### Alternative 2: Client-only switch with dual lookup

**Description**: Mint `nx-note://`, look up both spellings on re-put.
**Cons**: Old clients still mint `chroma://`; the engine rename stops rewriting new-scheme notes.
**Reason for rejection**: Gap 3 stays open for every un-upgraded install.

### Briefly Rejected

- **Backfill without engine canonicalization**: an old client's next re-put of a backfilled note mints a duplicate.

## Trade-offs

### Consequences

- Positive: the note identity names what it is; one definition per side.
- Negative: a DATA EFFECT changeset on every tenant's catalog; the scheme check gains a canonicalization step on each write route.

### Risks and Mitigations

- **Risk**: the backfill collides on catalog-016. **Mitigation**: precondition census halts before any row moves.
- **Risk**: a classifier misses the new scheme and treats notes as residue. **Mitigation**: one `is_note_uri`, and a lint that no `"chroma://"` literal remains outside the two constant definitions and the canonicalizer.

### Failure Modes

A missed classifier shows as notes reported as migration residue by
`nx catalog reconcile-stale`/`integrity`; a missed engine route shows as a
duplicate Document after an old client's re-put (detectable by two live rows
whose URIs differ only in scheme).

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified
- [ ] Engine-tag freeze ended (>= 2026-10-26 13:46Z)

### Minimum Viable Validation

On a substrate engine with the change: write a note with an old client path
(`chroma://` URI), run the backfill, re-put the same note with both the old and the
new client paths, and assert one live Document whose `source_uri` is
`nx-note://...`; then rename its collection and assert the URI follows.

### Phase 1: Code Implementation

#### Step 1: Engine canonicalizer, rename rewrite, backfill changeset (engine tag)

#### Step 2: Client scheme constants, `is_note_uri`, readers and classifiers accept both

#### Step 3: Client writers mint `nx-note://` (client release after the engine deploys)

### Phase 2: Operational Activation

#### Activation Step 1: Deploy the engine tag (backfill runs), then release the client

Engine first: the wire change is additive (the engine accepts both), so the
engine deploys before the client tag (nexus-1emxn choreography (a)).

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Backfilled `source_uri` values | N/A | `nx catalog show` | N/A | census query: no live `chroma://` rows | catalog backup before deploy |

### New Dependencies

None.

## Test Plan

- **Scenario**: old-scheme write after backfill — **Verify**: lands on the existing `nx-note://` Document, no duplicate.
- **Scenario**: new-scheme re-put of a backfilled note — **Verify**: same Document.
- **Scenario**: collection rename of a backfilled note and of an old-client note — **Verify**: URI prefix follows the new name in the new scheme.
- **Scenario**: backfill precondition with a planted collision — **Verify**: changeset halts, no row changed.
- **Scenario**: reconcile-stale / integrity / recovery-bundle classifiers on both schemes — **Verify**: both classify as store_put-origin.
- **Scenario**: import of an old recovery bundle carrying `chroma://` — **Verify**: converges onto the backfilled Document.

## Validation

### Testing Strategy

The Minimum Viable Validation runs on the test engine substrate; the classifier
and reader cases are unit tests against both schemes.

### Performance Expectations

N/A (a string canonicalization per write).

## Finalization Gate

### Contradiction Check

To be completed at gate.

### Assumption Verification

To be completed at gate.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Catalog register/upsert/lookup routes | nexus engine | Source Search (pending) |

### Scope Verification

To be completed at gate.

### Cross-Cutting Concerns

- **Versioning**: engine tag carries the changeset; client release follows.
- **Deployment model**: engine first, additive.
- **Incremental adoption**: old clients keep working through canonicalization.
- Others: N/A.

### Proportionality

To be completed at gate.

## References

- nexus-t7fbb and its 2026-10-23 analysis comment
- `src/nexus/aspect_readers.py`, `src/nexus/catalog/store_hook.py`, `src/nexus/catalog/recovery_bundle.py`, `src/nexus/commands/catalog_cmds/reconcile_stale.py`, `src/nexus/exporter.py`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java`, `catalog-016-source-uri-unique.xml`

## Revision History
