---
title: "Identity When a Path Moves: Repo Owners and Curator Documents Keep Their Identity Across a Move"
id: RDR-254
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by:
created: 2026-11-19
accepted_date:
related_issues: [GH-1571, GH-1605, nexus-o19i0]
related: [RDR-101, RDR-103, RDR-137, RDR-231]
---

# RDR-254: Identity When a Path Moves

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

> **Status of this draft:** research only. The gate is held (halcyon,
> 2026-11-18). The Problem Statement and Research Findings are written from two
> characterization tests; the Proposed Solution lists candidate designs and the
> open questions, and decides nothing yet.

Terms. The **catalog** is nexus's registry of documents; each document has a
**tumbler** (a dotted address such as `1.23.22`) and belongs to an **owner**.
A **repo owner** stands for one git repository that `nx index repo` indexed; it
carries a **repo hash**, the first eight hex digits of `sha256` of the
repository's absolute path, and a **repo root**, that path. A **curator owner**
holds documents indexed one by one, such as `nx index pdf`. A document's
**source_uri** is the `file://` address of the file it came from; its
**file_path** is that path (relative to the repo root for a repo owner, absolute
for a curator document). A **chunk** is a piece of a document's text stored in
T3 for search.

## Problem Statement

A document's identity in the catalog is the path of the file it came from, and
a repository's identity is the path of its folder. When a file or a repository
moves on disk, nexus has no notion that it is the same thing in a new place.
Depending on the shape of the move, it either duplicates everything (search
returns the same text twice, and metadata and links stay on the old copy) or
keeps the old records and silently leaves them pointing at the old path. No
command repairs either outcome. GH #1571 reports the first for a single PDF;
GH #1605 reports it for a whole repository.

### Enumerated gaps to close

#### Gap 1: A moved curator document is registered as a second document

Indexing a PDF again from its new path mints a new document. The old one keeps
its dead path, its chunks, its metadata and its links, and search returns both
(GH #1571). Both rows carry the same content hash, and registration never reads
it. Pointing the old row at the new path with `nx catalog update` then fails
with HTTP 409, because the new row already holds that `source_uri`.

#### Gap 2: A renamed repository becomes a second owner and every document is re-created

Moving a repository to a folder with a different name registers a new owner on
the next `nx index repo` and re-indexes every file. The old owner and its
documents stay, with dead `file://` addresses, until someone deactivates them
(GH #1605).

#### Gap 3: A repository moved under the same folder name keeps its documents on dead paths

Moving a repository to a folder with the same name keeps the owner, and nexus
repoints the owner at the new folder. Every existing document still points at
the old path, and indexing again returns each old document unchanged. Nothing
duplicates, so nothing looks wrong, but every `source_uri` the owner holds is
dead and nothing reports it.

#### Gap 4: A second clone with the same folder name takes over the first clone's owner

Two clones of one repository in folders with the same name (for example
`~/work/nexus` and `~/scratch/nexus`) share one owner. Registering either one
takes the owner over without an error, so the owner swaps between them on every
registration. Files the two clones share by relative path are only ever
registered once, under whichever clone got there first, so the second clone's
copies are never documents of their own.

#### Gap 5: No route moves an owner or a document to a new path

There is no command that re-roots an owner (changes its repo root and repo hash
and rewrites its documents' `source_uri`) or re-points a document whose new
path already has a row. `nx catalog update` repoints one document at a time and
refuses when the new path is taken; `nx catalog merge` moves the `source_uri`
only when the surviving row has none; `nx catalog reconcile-stale` never offers
a moved document whose old row still has chunks; `nx doctor --fix-paths` skips
curator owners.

## Relationship to Prior RDRs

Searched the RDR corpus for repo owner identity, repo hash, renamed directories,
moved files and `source_uri` identity.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-101 (event-sourced catalog, closed; its SQLite and Chroma substrate superseded by RDR-152 and RDR-155) | Precedent, never built | It designed a same-owner rename: `nx catalog rename <doc_id> <new_uri>` appends a `DocumentRenamed { doc_id, new_source_uri }` event and the projector updates `documents.source_uri`, with "zero T3 touch" because chunks carry no path. A move across owners was a delete plus a new registration. Neither the event nor the command exists in `src/nexus` or the engine, and RDR-101's post-mortem covers only the collection rename that shipped. Its chunks-carry-no-path invariant still holds (RDR-108), so a same-owner re-point in place remains the cheap shape for Gaps 1, 3 and 5. |
| RDR-103 (collection name authority) | Origin (of the repo hash) | Its "Repo-owner identity stability" section derives the repo hash from the main repository's path and names renamed repository folders as unstable. That rationale, the hash as a stable key, does not hold across a move, which is Gaps 2 to 4. |
| RDR-137 (eliminate repos.json) | Adjacent | It found worktree and main-repo dual registration and resolved worktrees to their main repository. Any change to how an owner is matched has to keep that resolution. |
| RDR-231 (note source_uri scheme) | Precedent | It settled rename and move for notes (its Gap 4: a note moved between collections by any path other than rename keeps a URI naming the old collection). A precedent for rewriting `source_uri` in place on a move. |

## Context

### Background

Found from GH #1571 (2026-09, nx 7.57.0) and GH #1605 (nx 7.78.0), both still
open. An interim workaround for #1571 is posted on the issue (comment
6988725830): repoint the old row with `nx catalog update`, then re-index the new
path with `--force`, deleting a duplicate first if one already exists. It was
read from the code and has not been run end to end.

### Technical Environment

Owner matching lives in the engine, `CatalogRepository.resolveOwnerAddress` and
`upsertOwner`. Document registration lives in the engine's register path, which
has a `source_uri` leg and a `file_path` leg, and in the Python client,
`doc_indexer._register_or_lookup_doc_id` and `_find_curator_document`. The repo
hash comes from `src/nexus/repo_identity.py`. Collection names use the owner
tumbler, not the repo hash (RDR-103), so re-rooting an owner does not rename a
collection.

## Research Findings

### Investigation

Two characterization tests pin today's behaviour. Each passes against the
current code and records it; a change to what they assert is a design decision
of this RDR, not a regression. Full record: T2
`nexus/moved-path-identity-characterization-2026-11-19`.

- `service/src/test/java/dev/nexus/service/OwnerConvergenceByNameCharacterizationTest.java`,
  3 tests against a real Postgres: same-name move, second clone, renamed move.
- `tests/test_moved_curator_doc_identity_characterization.py`, 3 tests at the
  registration level: moved PDF, the 409 on repoint, byte-identical copies.

Both tests are committed on branch `fix/gh1571-moved-pdf-identity` (8380e2e27).

### Key Discoveries

- **Verified** (test, source): an owner resolves by (name, owner type) before
  repo hash (`CatalogRepository.java:471`); `upsertOwner` keeps the matched
  owner's prefix and overwrites its repo hash and repo root
  (`CatalogRepository.java:664`, `:728`, `:600-602`). No owner upsert rewrites a
  document's `source_uri`.
- **Verified** (test, source): document registration has a `file_path` leg
  (`CatalogRepository.java:1170-1182`). When the `source_uri` lookup misses, it
  matches the owner-scoped relative path and returns the old document,
  `created=false`, with its `source_uri` unchanged. This is why Gap 3 does not
  duplicate and why Gap 4 hides the second clone's files.
- **Verified** (test, source): the repo hash is `sha256(main repo path)[:8]`
  (`repo_identity.py:295-296`).
- **Verified** (test): renamed move mints a new owner and leaves the old one
  untouched, not deactivated and not linked (Gap 2).
- **Verified** (test, source): a curator document's identity is its absolute
  path (`_find_curator_document`, `doc_indexer.py:1228`, `:3918`; docstring
  `:775-776`). The content hash is stamped on the row after registration
  (`doc_indexer.py:1764`, `multi_batch_write.py:357`) and never read at
  registration (`doc_indexer.py:995`).
- **Verified** (test): repointing the old row returns 409 from the partial unique
  index `ux_catalog_documents_live_source_uri` on (tenant, `source_uri`) for
  live rows with a non-empty `source_uri` (`catalog-016-source-uri-unique.xml:150-160`).
- **Verified** (test): two byte-identical copies at two live paths are two
  documents (nexus-o19i0).
- **Documented** (source): `merge` moves the `source_uri` only when the surviving
  row has none (`CatalogRepository.java:4054-4055`); `reconcile-stale` has no
  bucket for a moved document whose old row keeps chunks and a manifest
  (`reconcile_stale.py:755`, `:799`); `doctor --fix-paths` skips curator owners
  (`doctor.py:3943`).
- **Documented** (source): one move is absorbed today. `_find_curator_document`'s
  legacy relative-path leg (`doc_indexer.py:777-815`) adopts an old row at a new
  location when the path the row holds no longer exists. Absolute-path
  registrations, PDFs included, never take it. It is a working precedent for
  "the old path is gone, so the new location may claim the row".
- **Documented** (source): a file moved inside one repository is already
  recognised by content hash. Every row `nx index repo` sends to the engine's
  reconcile carries the file's sha256 as `content_hash` and in its
  `base_metadata` (`indexer.py:1567-1568`). On a complete walk only
  (`catalog-048-reconcile.xml:224`, `:635`), the commit takes each of the
  owner's live rows that the walk did not see, and if a present row has the
  same `metadata.content_hash` it classes the old row `rename` (`:637-654`):
  its links move to the new row (`:674`) and the old row is trashed (`:713`).
  The new row has its own tumbler, so identity is not kept, but links are.
  "Gone" here means absent from a complete walk of the owner's root, not a
  filesystem probe. This is Alternative 2, shipped for repo files within one
  owner; it cannot reach Gaps 2 and 3, where the relative path either changes
  owner or does not change at all.
- **Documented** (source): the tenant-wide `same-as` linker
  (`nexus.catalog_link_same_content`, created by `content_hash_dedup`) links
  live pdf and paper rows that share a content hash. It runs from
  `nx index repo` (`indexer.py:1748`) and from the catalog links command, not
  from `nx index pdf`, so a moved curator PDF gets no `same-as` link to its old
  row unless the user runs that command.

### Critical Assumptions

- [ ] A moved document can be recognised as the same document from evidence the
  catalog already holds (content hash, old path gone, same owner) without false
  merges of genuinely distinct copies (nexus-o19i0 keeps live copies separate).
  The repo reconcile's rename rule already uses exactly this evidence within one
  owner (Key Discoveries); whether it extends safely to curator documents,
  where no complete walk exists, is the open part.
  — **Status**: Unverified — **Method**: Source Search, then Spike
- [ ] Rewriting `source_uri` in place leaves chunks and links intact, because
  chunks carry no path (RDR-101's invariant). — **Status**: Unverified —
  **Method**: Source Search
- [ ] Two live clones under one folder name are rare enough, or detectable
  enough, that refusing or splitting them is acceptable. — **Status**:
  Unverified — **Method**: Docs Only (needs usage evidence)

## Proposed Solution

Not decided. This draft records the candidate designs and the questions each
must answer.

### Approach

Candidates, to be compared in Alternatives once research finishes:

1. **An explicit move command** (the GH #1605 ask, and the generalisation of
   GH #1571's second suggestion): `nx catalog owners --execute move --owner <t>
   --new-root <path>` re-roots an owner, rewriting its repo root, repo hash and
   every document's `source_uri` in one engine transaction; a document form
   re-points one curator document, absorbing a duplicate row if the new path
   already has one. Nothing is inferred; the user says what moved.
2. **Recognition at registration**: when a registration misses on path, look
   for a live row in the same owner with the same content hash whose path no
   longer exists, and re-point it instead of minting. Extends the legacy
   relative-path precedent to absolute paths.
3. **Owner matching by hash, not name**: match an owner by repo hash first, so a
   same-name second clone gets its own owner (Gap 4), and treat a name match
   with a different hash as a move only when the old root is gone.

### Technical Design

To be written after the open questions below are answered.

Open questions:

- Is recognition at registration (candidate 2) safe given nexus-o19i0, or does
  identity across a move have to be an explicit act (candidate 1)?
- Gap 3 today leaves dead paths silently. Should the same-name case re-root the
  documents automatically (the old root is gone, the owner already followed),
  or only report them?
- Gap 4: should a second live clone get its own owner, or be refused with a
  message naming the first clone?
- Does a move need to be one engine transaction per owner (thousands of
  documents), and is that a visible, resumable job under the no-boot-time-data-
  migration rule?

## Alternatives Considered

The three candidates from Approach, compared on the five gaps. A preliminary
read; the open questions above can change it.

| | Gap 1 moved PDF | Gap 2 renamed repo | Gap 3 same-name repo move | Gap 4 second clone | Gap 5 no route |
| --- | --- | --- | --- | --- | --- |
| 1. Explicit move command | closes, when the user runs it | closes, when run | closes, when run | no | closes |
| 2. Recognition at registration | closes automatically | no (a new owner, nothing to match within) | no (the relative path matches, so registration never misses) | no | no |
| 3. Owner matching by hash first | no | no (the hash changes with the path) | becomes a new owner instead, like Gap 2 | closes | no |

### Alternative 1: an explicit move command

**Description**: `nx catalog owners --execute move` re-roots an owner, and a
document form re-points one curator document, in one engine transaction each.

**Pros**:

- The user states what moved, so it never merges two distinct files
  (nexus-o19i0 stays true).
- It is the shape RDR-101 designed for the same-owner case, and chunks carry no
  path, so it rewrites catalog rows only.
- It is the remedy GH #1605 asks for.

**Cons**:

- Nothing happens unless the user knows to run it; the default outcome of a
  move stays wrong.
- It does not touch Gap 4.

### Alternative 2: recognition at registration

**Description**: a registration that misses on path looks, within the same
owner, for a live row with the same content hash whose path no longer exists,
and re-points it.

**Pros**:

- The common case (a file moved and indexed again) fixes itself.
- `_find_curator_document`'s legacy relative-path leg already does this for a
  narrower case (`doc_indexer.py:777-815`).
- For repo files it is already built: the reconcile's rename rule
  (Key Discoveries). The new work is the curator path only.

**Cons**:

- A curator registration has no complete walk to say the old path is gone, so
  it would have to probe the filesystem, and an unmounted volume or a path from
  another machine looks gone too. The repo rule avoids that probe.
- It works only within one owner, so a renamed repository (Gap 2) is out of
  reach, and a same-name repository move (Gap 3) changes no relative path, so
  the repo rule never fires there.
- A file moved and edited in one step has a new hash and is not recognised.

### Alternative 3: owner matching by hash first

**Description**: resolve an owner by repo hash before name, so two live clones
get two owners.

**Pros**:

- Closes the silent takeover in Gap 4.

**Cons**:

- It turns every same-name move (Gap 3) into a second owner with duplicated
  documents, the Gap 2 outcome, unless it is paired with Alternative 1 or with a
  rule that a name match whose old root is gone is a move.

### Briefly Rejected

- **Match curator documents by title**, as GH #1571 suggests: titles are not
  unique, and two distinct papers can share one.
- **Make the old row an alias of the new one** (the reporter's workaround): it
  keeps two rows and two chunk sets, and the alias still has to be found and
  set by hand.

Preliminary direction, not a decision: Alternative 1 as the route for every
gap but Gap 4, with Alternative 3's hash-first matching for Gap 4 only when the
old root still exists, and Alternative 2 considered for curator documents once
its false-match risk is measured.

## Trade-offs

These follow the preliminary direction above and change if it does.

### Consequences

- A move becomes something the user does on purpose. Until they run the
  command, a moved PDF still indexes as a second document and a renamed
  repository still becomes a second owner; the command repairs the result
  after the fact, so the default outcome of a move is unchanged.
- The re-point keeps the tumbler. The repo reconcile's rename keeps links but
  mints a new tumbler; an explicit move keeps both, so a citation by tumbler
  survives a move made with the command and does not survive one the reconcile
  absorbs. The two paths then disagree on what a move preserves.
- Chunks are not touched. A move rewrites catalog rows (owner root and hash,
  `source_uri`, `file_path`) only, because chunks carry no path (RDR-108); the
  cost of a move is one row per document, not a re-embed.
- When the new path already has its own row (the GH #1571 state), the move has
  to absorb that row: its chunks, manifest and links fold into the old one
  before the old row takes the path, or the live `source_uri` index refuses
  the write. That makes the document form a merge, not a rename.
- Hash-first owner matching for Gap 4 gives a second live clone its own owner.
  Files the two clones share are then indexed twice, once per owner, which is
  the right result for two working trees but doubles their storage.

### Risks

- A move of a large owner rewrites thousands of rows. As one engine
  transaction it holds locks for its duration; split up, it can stop half done.
  Either way it is a data move, so under the rule against boot-time data
  migrations it must be a visible, resumable job the user starts, not
  something the engine does on its own.
- Hash-first owner matching depends on telling "the old root still exists"
  from "the old root is gone". A root on an unmounted volume looks gone, so a
  second clone on a removable disk could be read as a move and take the owner
  over, the very bug Gap 4 names. The check has to fail toward a separate
  owner.
- Absorbing the duplicate row loses whatever only that row holds (links made
  to it, aspects extracted from it) unless each is carried over. The repo
  reconcile's rename moves links only (`catalog-048-reconcile.xml:674`), so
  aspects and other per-document records are the part to enumerate.
- If curator recognition (Alternative 2) is added later, a byte-identical copy
  made on purpose at a new path while the original sits on an unmounted
  volume would be folded into the original, which breaks nexus-o19i0's rule
  that live copies stay separate.

## Implementation Plan

To be written after the design is chosen. The two characterization tests are
the starting point: each case they pin becomes a test whose expected value the
chosen design changes.

## Finalization Gate

Not ready: the gate is held until the design is chosen.
