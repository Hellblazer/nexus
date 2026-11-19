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

> **Status of this draft:** design chosen (halcyon for Sam, 2026-11-19): an
> explicit move command, hints that print the command when a move is seen, and
> a separate owner for a second live clone. Ready for the gate.

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

- **Verified** (source): a chunk's metadata holds two keys only,
  `chunk_text_hash` and `embedding_model` (`metadata_schema.ALLOWED_TOP_LEVEL`,
  printed from the module), so no T3 row names a path.
- **Verified** (source): an owner's name is unique per tenant and owner type
  (`catalog_owners_unique_name_type`, `catalog-001-baseline.xml:47`), and its
  repo hash is unique among non-blank hashes (`:51`). The client looks an owner
  up by repo hash first (`cat.owner_for_repo`, `indexer.py:1388`) and registers
  on a miss with the folder's base name (`:1397-1403`); the engine then matches
  that name first (`resolveOwnerAddress`), which is where a second clone takes
  the first clone's owner over (Gap 4).
- **Verified** (source): an aspect row's `source_path` is the catalog
  `file_path` the document was registered with (`_canonicalize_source_path`,
  `aspect_worker.py:1368`; the indexer passes it at `indexer.py:6417`). For a
  repo owner that path is relative to the repo root and does not change when the
  repository moves; for a curator document it is absolute and does.
- **Verified** (source): the engine already merges one document into another
  (`CatalogRepository.mergeDocuments`, `:3951`): it remaps every link of the
  duplicate onto the canonical row (`remapLinksForMerge`) and raises the
  canonical's sensitivity when the duplicate is sensitive, in one transaction.
  It leaves the duplicate live as an alias, so the duplicate keeps the
  `source_uri` the canonical would need.
- **Verified** (source, census of every `*_uri` and `*_path` column in the
  engine's Liquibase changelogs): the tables that store a document's path are
  `catalog_owners` (`repo_root`, `repo_hash`), `catalog_documents`
  (`file_path`, `source_uri`), `document_aspects` (`source_path`,
  `source_uri`) and `document_highlights` (`source_uri`, keyed by `doc_id`).
  Three more hold a path but not a document's identity: `index_failures`
  (a log of files that failed to index), `catalog_reconcile_rows` (one
  reconcile run's staging) and the PDF pipeline's `pdf_path` (one run's state).
  The staging tables of `staging-001` were dropped by `staging-003`, and the
  aspect queue by `aspects-005`.

### Critical Assumptions

- [x] A moved document can be recognised as the same document from evidence the
  catalog already holds (content hash, old path gone, same owner) without false
  merges of genuinely distinct copies (nexus-o19i0 keeps live copies separate).
  — **Status**: Not needed by the decision. Recognition only prints a command;
  the user runs the merge. A false match costs one printed line, so the probe's
  known blind spot (an unmounted volume reads as gone) merges nothing.
  — **Method**: Design decision (halcyon for Sam, 2026-11-19)
- [ ] Rewriting `source_uri` in place leaves chunks and links intact, because
  chunks carry no path (RDR-101's invariant). For chunks and links this holds:
  the manifest `catalog_document_chunks` is keyed by `doc_id`
  (`catalog-001-baseline.xml:171-175`) and `catalog_links` by tumbler
  (`:127-135`). It does not hold for aspects: `document_aspects` is unique on
  (tenant, collection, `source_path`) (`aspects-001-baseline.xml:75`) and also
  stores `source_uri` (`:71`), so a re-point leaves its aspect row on the old
  path, a legacy row with an empty `doc_id` no longer joins, and the next
  extraction at the new path writes a second row for the same document.
  `document_highlights` is keyed by `doc_id` (`:121`) and is safe. A move must
  rewrite the aspect row's path too: its `source_path` for a curator document,
  its `source_uri` for both kinds (the relative `source_path` of a repo file
  does not change). — **Status**: Verified (chunks, links, highlights safe;
  aspects need the rewrite the design includes) — **Method**: Source Search
- [x] Two live clones under one folder name are rare enough, or detectable
  enough, that refusing or splitting them is acceptable. — **Status**: Settled
  by decision: two live clones are a legitimate setup, so the second gets its
  own owner and nothing is refused (halcyon for Sam, 2026-11-19). The split
  needs only the client's existing root probe. — **Method**: Design decision

## Proposed Solution

### Approach

Identity across a move stays an explicit act, so nexus-o19i0 holds: two live
files are two documents until the user says one moved. The design has three
parts.

1. **A move command** closes Gaps 1, 2, 3 and 5. `nx catalog owners --execute
   move` re-roots an owner, or re-points one document, in one engine
   transaction. When the new path already has its own row, the move absorbs
   that row into the old one, so the old row keeps its tumbler, links and
   metadata.
2. **Move hints**: detect, don't act. An index run that sees a move prints
   the exact move command and changes nothing. The cases it sees: a curator
   document whose content hash matches a row whose file is gone, a repository
   whose owner was registered at a root that is gone, and a new repository
   whose files match an owner whose root is gone. A false match prints one
   line and merges nothing, which is why a filesystem probe is acceptable here
   when it was not for Alternative 2.
3. **A separate owner for a second live clone** closes Gap 4. When a
   repository's folder name matches an owner whose recorded root still exists
   as a git work tree with a different repo hash, the client registers the
   repository under its own name, `<folder>-<repo hash>`, and does not take
   the owner over. Nothing is refused.

### Technical Design

**Terms used here.** The *old row* is the document that holds the identity to
keep: its tumbler, links, aspects and highlights. The *new row* is a document
registered at the new path after the move, which the move absorbs. The *old
owner* and *new owner* are the same for repositories.

#### The move command

```
nx catalog owners --execute move --owner <prefix> --new-root <path>
nx catalog owners --execute move --document <tumbler> --new-path <path>
```

It follows the existing mutation arms of `nx catalog owners`
(`owners.py`, the `deactivate` and `reactivate` arms): it reports by default,
and writes only with `--no-dry-run --confirm`. The client checks that the new
path exists, so the user cannot point a row at nothing. For `--owner` it also
checks that the new root is a git work tree and computes its repo hash
(`_repo_identity_with_main`), then sends both to the engine. Both forms
print the same counts, as a dry run and after a write: rows re-pointed, rows
absorbed, rows trashed, aspect rows rewritten.

The engine gets two routes, each one transaction:

- `POST /v1/catalog/documents/move` with `{tumbler, new_source_uri,
  new_file_path, dry_run}`.
- `POST /v1/catalog/owners/move` with `{owner, new_root, new_repo_hash,
  dry_run}`.

A dry run runs the same code and rolls back, so its counts are the write's
counts.

**Document move.** For the old row S and the new path P:

1. If a live row T other than S holds P as its `source_uri`, absorb T into S.
   The absorb reuses `mergeDocuments`' link and sensitivity steps
   (`remapLinksForMerge`; a sensitive T raises S). T's highlights move to S
   when S has none. Then T is trashed (the `deleted_at` soft delete, which
   `nx catalog trash` can restore) and its chunk manifest is released.
2. Set S's `source_uri` to `file://P` and its `file_path` to P.
3. Rewrite S's aspect row: `source_path` and `source_uri` to the new path. If
   T has an aspect row at P, that row is the newer extraction: it is
   re-keyed to S's `doc_id` and S's old-path row is deleted, because the unique
   key (tenant, collection, `source_path`) allows one row per path.
4. Set the `source_uri` of S's highlights row, if it has one, to `file://P`.

The move leaves the three tables that only log a path (`index_failures`,
`catalog_reconcile_rows`, the PDF pipeline's run state) as they are: they
record what happened at the old path, and none is read to find a document.

S keeps its own chunks. If the file changed in the move, S's stored content
hash no longer matches the file, and the next index run at P re-chunks S the
way it re-chunks any edited file. The move never moves a chunk.

**Owner move.** For the old owner A and the new root R with repo hash H:

1. If another live owner B already holds H (the user indexed at R before
   moving, the Gap 2 order), fold B into A. For each of B's live documents,
   the row of A with the same `file_path` is S, and B's row is absorbed into
   it by the document move's step 1. B's documents with no counterpart in A
   (files added after the move) are trashed, and the next index run registers
   them under A. Then B is deactivated (the catalog-022 soft delete, undone by
   `--execute reactivate`) and its repo hash is cleared, since the hash index
   allows one owner per hash.
2. Set A's `repo_root` to R and its `repo_hash` to H. A keeps its name.
3. For every document of A, live or trashed, set `source_uri` to
   `file://R/<file_path>`. The relative `file_path` does not change.
4. Rewrite the `source_uri` of those documents' aspect and highlights rows the
   same way. An aspect row's `source_path` is the relative `file_path` and
   stays as it is.

B's collections and their chunks are left in place. The move's report names
them, and removing them is the user's call (`nx collection delete`), because a
delete is not something a move does on its own.

**Size and locking.** A move rewrites catalog and aspect rows only, never T3.
For a repository the size of nexus (7,197 tracked files), an owner move updates
on the order of 7,000 document rows, up to as many aspect rows, and one owner
row. With a fold of B it also trashes or absorbs up to as many of B's rows
again, about 15,000 to 20,000 row writes in all. That is one Postgres
transaction of seconds, which holds row locks on the two owners' documents
while it runs. It is a user-run command that touches catalog rows only, so the
rule against boot-time data migrations does not apply. It leaves nothing to
resume: the transaction commits whole or not at all.

#### Move hints

Each hint is one line, printed at the end of the run that saw the move, and
names the exact command. A hint never writes.

- **Curator document (Gap 1).** When an index run creates a new curator
  document and stamps its content hash H, the client asks the engine's
  metadata route for the owner's other live rows with `content_hash` equal to
  H (the route behind `nx catalog list --meta`). For each one whose file is
  absent on this machine, it prints `nx catalog owners --execute move
  --document <old tumbler> --new-path <new path> --no-dry-run --confirm`. A
  row whose file is still present is a copy and gets no hint (nexus-o19i0).
- **Same-name repository move (Gap 3).** When `owner_for_repo` misses on the
  repo hash and an owner with the same name and owner type exists, the client
  reads that owner's recorded `repo_root`. If that root is gone, registration
  proceeds as it does today (the engine re-roots the owner row), and the run
  prints `nx catalog owners --execute move --owner <prefix> --new-root <path>
  --no-dry-run --confirm` so the documents follow. Documents are never
  re-pointed automatically.
- **Renamed repository (Gap 2).** When an index run registers a new repo owner,
  the client lists repo owners whose recorded root is gone (the census
  classifier, `_classify_owner_root`). For each, it looks up the content hashes
  of up to 20 of the new owner's files in that owner. When at least half are
  found, it prints the owner move command with the old owner's prefix and the
  new root.
- **Census.** `nx catalog owners --census` adds, under each `path_vanished`
  owner, the move command with a `<new root>` placeholder, next to the
  deactivate arm it already offers.

#### A second live clone (Gap 4)

The change is in the client, at the point where `owner_for_repo` misses
(`indexer.py:1388`). Before registering, the client looks up an owner with the
same name and owner type. If one exists, its recorded `repo_root` is still a
git work tree on this machine, and that work tree's repo hash differs from
this repository's, the client registers this repository under the name
`<folder>-<repo hash>`. The engine sees a new name and a new hash and creates a
new owner. Its existing name-first match (`resolveOwnerAddress`) is unchanged,
so the engine characterization test for Gap 4 stays true; the client stops
sending the name that triggers it.

If the root is gone, the case is Gap 3 and gets the hint. If the root's status
cannot be read (a permission error), the client treats it as present and
registers a separate owner: a wrong split costs one duplicate index, a wrong
takeover costs the first clone's documents.

Existing owners that two clones already share are not split after the fact.
The first registration from each clone after this change resolves by its own
repo hash, and a clone whose hash the shared owner no longer carries gets its
own owner from then on.

## Alternatives Considered

Three candidates, compared on the five gaps. The decision (Proposed Solution)
takes Alternative 1, adds hints that print its command, and takes Alternative
3 for Gap 4 only.

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

Decision (halcyon for Sam, 2026-11-19): Alternative 1 for Gaps 1, 2, 3 and 5,
with move hints answering its main con (nothing happens unless the user knows
to run it). Alternative 3's hash-first matching only for Gap 4, and only when
the old root still exists. Alternative 2 is rejected as an automatic act: its
filesystem probe cannot tell an unmounted volume or another machine's path
from a deleted file, so the same probe is used only to print a hint.

## Trade-offs

### Consequences

- A move stays something the user does on purpose. A moved PDF still indexes
  as a second document and a renamed repository still becomes a second owner,
  but the run that does it now prints the command that repairs it.
- The move keeps the tumbler. The repo reconcile's rename, which absorbs a file
  moved inside one repository, keeps links but mints a new tumbler. A citation
  by tumbler survives a move made with the command and does not survive one the
  reconcile absorbs. This RDR leaves the reconcile as it is.
- Chunks are not touched. A move rewrites catalog and aspect rows only, because
  chunks carry no path (RDR-108). A moved and edited file is re-chunked by the
  next index run, as any edited file is.
- When the new path already has its own row (the GH #1571 state, and Gap 2
  when the user indexed before moving), the move absorbs that row, so the
  document form is a merge as well as a rename. The absorbed row is trashed and
  can be restored.
- Files added to a renamed repository between its re-index and the move lose
  the links made to them in that window: the fold trashes them, and the next
  index run registers them under the old owner with new tumblers.
- A second live clone gets its own owner. Files the two clones share are
  indexed twice, once per owner: the right result for two working trees, at
  twice the storage for those files.
- The absorbed owner's collections stay until the user deletes them.

### Risks

- A large owner move is one transaction of some 15,000 to 20,000 row writes,
  holding row locks on two owners' documents for seconds. An index run on
  either owner at the same time waits for it or fails on a conflict and is run
  again. The move is never resumable work: it commits whole or not at all.
- The root probe that splits a second clone cannot tell an unmounted volume
  from a deleted folder. A first clone on an unmounted disk reads as gone, so
  the second clone takes the owner over, as it does today, and the run prints
  the Gap 3 hint. The probe fails toward a separate owner only where it can:
  a root it cannot read counts as present.
- A hint can name the wrong old row or owner (a file deleted on purpose whose
  bytes match the new file, an owner whose root is on another machine). Nothing
  acts on a hint, and the move's dry run shows what would change before the
  user confirms.
- Absorbing a row loses anything only that row holds that the move does not
  carry. The census in Key Discoveries lists every table that keys on a path or
  a document id; the move carries links, sensitivity, highlights and aspects.
  A table added later that keys on a document must be added to the move.

## Implementation Plan

### Prerequisites

- [x] Critical Assumptions verified or settled by decision.
- [ ] An engine release carrying the two move routes. Cutting it is Sam's.

### Minimum Viable Validation

GH #1571 end to end against a real engine: index a PDF, link it, move the file,
index it again, run the printed hint, and check one live document at the new
path, with the old tumbler, the link, and no second search hit.

### Phase 1: Engine move routes

#### Step 1: Document move

Add `POST /v1/catalog/documents/move` (CatalogHandler, CatalogRepository) with
the four steps of the Technical Design, one transaction, and a dry run that
rolls back. Tests against a real Postgres: a plain re-point; an absorb that
remaps links, raises sensitivity, moves highlights and re-keys the newer aspect
row; a refusal for a missing or trashed tumbler; a dry run that writes nothing
and reports the same counts.

#### Step 2: Owner move

Add `POST /v1/catalog/owners/move`: re-root, rewrite `source_uri` on live and
trashed documents, rewrite aspect and highlights `source_uri`, and fold an
owner already holding the new repo hash. Extend
`OwnerConvergenceByNameCharacterizationTest`: the renamed-move case gains a
move and asserts one owner, the old tumblers at the new paths, and the second
owner deactivated with no repo hash. Its same-name and second-clone cases keep
their assertions, because the engine's matching does not change.

### Phase 2: Client command

#### Step 3: `--execute move`

Add `move` to `_MUTATION_ACTIONS` in `owners.py`, with `--document` and
`--new-path`, or `--owner` and `--new-root`; refuse when the new path is
absent or, for an owner, not a git work tree; compute the repo hash with
`_repo_identity_with_main`; print the route's counts.
`tests/test_moved_curator_doc_identity_characterization.py`'s moved-PDF and
409 cases become tests of the command: one live row at the new path, the old
tumbler kept.

### Phase 3: Move hints

#### Step 4: Curator hint

After a curator registration creates a document and stamps its content hash,
query the metadata route and print the document hint for each matching row
whose file is absent. Tests: a moved PDF prints the hint; a byte-identical copy
whose original is present prints none.

#### Step 5: Repository hints

The same-name hint at the `owner_for_repo` miss (`indexer.py:1388`), the
renamed-repository hint after a new owner registers, and the census line under
`path_vanished` owners. Tests for each, and one that no hint writes a row.

### Phase 4: Second clone

#### Step 6: Separate owner

At the `owner_for_repo` miss, register under `<folder>-<repo hash>` when a
same-name owner's root is a live git work tree with a different hash, or
cannot be read. Tests: two live clones give two owners and neither takes the
other over; a gone root falls through to the Gap 3 hint.

### Day 2 Operations

The design creates no new persistent resource. The move writes existing rows,
and its reversals are existing commands: `nx catalog trash` restores an
absorbed row, `--execute reactivate` restores a folded owner, and a second move
puts paths back.

### New Dependencies

None.

## Test Plan

- **Scenario**: a PDF moved and indexed again (Gap 1) — **Verify**: the hint
  names the old tumbler; after the move one live row holds the new path with
  the old tumbler and links; search returns the text once.
- **Scenario**: a moved and edited PDF — **Verify**: no hint (the hash
  differs); a move run by hand re-points the row, and the next index run
  re-chunks it.
- **Scenario**: a renamed repository indexed at its new path (Gap 2) —
  **Verify**: the hint names the old owner; after the move one owner remains
  with the old tumblers, the new root and hash; the second owner is
  deactivated.
- **Scenario**: a repository moved under the same folder name (Gap 3) —
  **Verify**: the hint prints; after the move every `source_uri` names the new
  root.
- **Scenario**: two live clones with one folder name (Gap 4) — **Verify**: two
  owners; a run from either clone changes neither owner's root.
- **Scenario**: two byte-identical live copies — **Verify**: no hint; two
  documents (nexus-o19i0).
- **Scenario**: a move whose dry run is not confirmed — **Verify**: no row
  changes, and the counts match a later confirmed run.

## Validation

### Testing Strategy

Engine routes are tested against a real Postgres, the client command and
hints against the test engine substrate. The two characterization tests are
the baseline: each case they pin either keeps its assertion (the engine's
owner matching) or changes to the fixed outcome in the step that fixes it.

### Performance Expectations

A move is one row write per document and aspect row, with no re-embed. Against
re-indexing a renamed repository, which re-chunks and re-embeds every file,
it is cheaper by the embedding cost of the whole repository.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. The Key Discovery that the repo reconcile mints a new
tumbler on a rename is a difference this RDR records in Consequences and
leaves in place.

### Assumption Verification

Assumption 2 is verified by source search, including the census of path
columns. Assumptions 1 and 3 are settled by the design decision, not by
evidence: the design no longer depends on recognition being safe, and splitting
a second clone is the chosen behaviour.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `CatalogRepository.mergeDocuments`, `remapLinksForMerge` | nexus engine | Source Search |
| `owners.py` mutation arms (`deactivate`, `reactivate`) | nexus client | Source Search |
| `nx catalog list --meta` metadata route | nexus engine | Source Search |
| `_repo_identity_with_main`, `owner_for_repo` | nexus client | Source Search |

### Scope Verification

The Minimum Viable Validation (GH #1571 end to end) is in scope as the test of
Phase 3 Step 4 run after Phase 2, not deferred.

### Cross-Cutting Concerns

- **Versioning**: the client command needs the engine routes; on an older
  engine it refuses with the engine version it needs, as the metadata route's
  callers do.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: local and cloud alike; the routes are tenant-scoped like
  every catalog route.
- **IDE compatibility**: N/A
- **Incremental adoption**: each phase stands alone; hints ship only after the
  command they print.
- **Secret/credential lifecycle**: N/A
- **Memory management**: the engine updates rows in SQL, without loading a
  document list into memory.

### Proportionality

Right-sized: one command with two forms, four hints and one client rule. The
Research Findings carry the line references the design leans on and nothing
else.
