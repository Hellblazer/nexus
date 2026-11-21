---
title: "Identity When a Path Moves: Repo Owners and Curator Documents Keep Their Identity Across a Move"
id: RDR-254
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-19
accepted_date: 2026-11-19
related_issues: [GH-1571, GH-1605, nexus-o19i0]
related: [RDR-101, RDR-103, RDR-137, RDR-231]
---

# RDR-254: Identity When a Path Moves

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

> **Status:** accepted by Sam, 2026-11-19 (gate PASSED round 2). The plan of
> record is T2 `nexus_rdr/254-plan`.

Terms. The **catalog** is nexus's registry of documents; each document has a
**tumbler** (a dotted address such as `1.23.22`) and belongs to an **owner**.
A **repo owner** stands for one git repository that `nx index repo` indexed; it
carries a **repo hash**, the first eight hex digits of `sha256` of the
repository's absolute path, and a **repo root**, that path. A **curator owner**
holds documents indexed one by one, such as `nx index pdf`. A document's
**source_uri** is the `file://` address of the file it came from; its
**file_path** is that path (relative to the repo root for a repo owner, absolute
for a curator document). A **chunk** is a piece of a document's text stored in
T3 for search, and a document's **manifest** lists its chunks. The **engine**
is the Java service that owns the catalog's Postgres tables; the **client** is
the `nx` command. An **aspect row** holds fields extracted from a document by a
model; a **highlights row** holds a DEVONthink document's highlights. **Trash**
soft-deletes a document (it can be restored); **deactivate** does the same for
an owner. An **alias** is a document marked as a duplicate of another
(`alias_of`). The **repo reconcile** is the step of `nx index repo` that
compares the files it walked with the owner's documents. nexus-o19i0 is the
fix that gave a byte-identical file at a second path its own document with its
own chunks, so two live files with the same bytes are two documents.

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
(GH #1571). Both rows stand for the same bytes (each carries the file's content
hash when it was indexed on or after 2026-11-03), and registration never
compares content. Pointing the old row at the new path with `nx catalog update` then fails
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
  repo hash is unique among non-blank hashes (`:51`). A repo owner is
  registered at four sites. `ensure_owner_for_repo`
  (`http_catalog_client.py:885`) is called by `nx index repo` for a path not yet
  in the local registry (`commands/index.py:107`, `:1145`; errors swallowed),
  on a docs-collection update (`:125`), and by an in-repo `nx index md` or `rdr`
  (`doc_indexer.py:982`; a PDF keeps its curator owner, `:933`). It looks the owner up by repo hash
  (`owner_for_repo`) and, on a miss, upserts with the folder's base name; the
  engine then matches that name first (`resolveOwnerAddress`), which is where a
  second clone takes the first clone's owner over (Gap 4) and a same-name move
  re-roots the owner row (Gap 3). The indexer's own lookup
  (`indexer.py:1388`) registers on a miss with `register_owner`
  (`:1397-1403`), which a registered path whose owner is missing reaches, and
  `commands/catalog.py:1427` does the same from the registry loop. Both send the
  base name, so both take a same-name owner over the same way.
- **Verified** (source): an aspect row's `source_path` is the catalog
  `file_path` the document was registered with (`_canonicalize_source_path`,
  `aspect_worker.py:1368`; the indexer passes it at `indexer.py:6417`). For a
  repo owner that path is relative to the repo root and does not change when the
  repository moves; for a curator document it is absolute and does.
- **Verified** (source): the engine already merges one document into another
  (`CatalogRepository.mergeDocuments`, `:3951`): it remaps every link of the
  duplicate onto the canonical row (`remapLinksForMerge`) and raises the
  canonical's sensitivity when the duplicate is sensitive, in one transaction.
  It leaves the duplicate live as an alias and clears the duplicate's
  `source_uri`, freeing the path for the canonical.
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
- [x] Rewriting `source_uri` in place leaves chunks and links intact, because
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
   move` re-roots a repo owner, or re-points one curator document, in one
   engine transaction. When the new path already has its own row, the move
   absorbs that row into the old one, so the old row keeps its tumbler, links
   and metadata.
2. **Move hints**: detect, don't act. Index runs and the owner census read the
   catalog for the signs of a move and print the exact move command; they
   change nothing. The repository hints read durable catalog state and repeat
   on every `nx index repo` until the move is made; the curator hint prints on
   the run that registers the duplicate. The cases: a curator document whose
   content hash matches a row whose file is gone, an owner whose documents
   point outside its recorded root, and an owner whose root is gone and whose
   files match another owner's. A false match prints one line and merges
   nothing, which is why a filesystem probe is acceptable here when it was not
   for Alternative 2.
3. **A separate owner for a second live clone** closes Gap 4. When a
   repository's folder name matches an owner whose recorded root is still a
   different git work tree on this machine, the client registers the
   repository under its own name, `<folder>-<repo hash>`, and does not take
   the owner over. Nothing is refused.

### Technical Design

**Terms used here.** The *old row* S is the document whose identity is kept:
its tumbler, links, aspects and highlights. The *new row* T is a document
registered at the new path after the move, which the move absorbs. For an
owner move, A is the owner that keeps its identity and B is an owner
registered at the new root before the move.

#### The move command

```
nx catalog owners --execute move --owner <prefix> --new-root <path>
nx catalog owners --execute move --document <tumbler> --new-path <path>
```

It follows the existing mutation arms of `nx catalog owners`
(`owners.py`, the `deactivate` and `reactivate` arms): it reports by default,
and writes only with `--no-dry-run --confirm`. The client checks that the new
path exists, and makes it absolute, so the user cannot point a row at
nothing. `--owner` takes a repo owner only; the client checks that the new
root is a git work tree, resolves it to its main repository and computes its
repo hash (`_repo_identity_with_main`), and sends that main-repository path and
hash, as `ensure_owner_for_repo` would register them. `--document` takes a
curator document only, because a repo file's path is relative to its owner's
root and moves with the owner. The engine enforces both: it refuses
`owners/move` for an owner that is not a live repo owner, and `documents/move`
for a document that is not a live curator document. Both forms print the
same counts, as a dry run and after a write: rows re-pointed, rows absorbed,
rows trashed, rows skipped, aspect and highlight rows rewritten. The report
also lists every absorbed tumbler, since an absorb cannot be fully undone
(below).

The engine gets two routes, each one transaction:

- `POST /v1/catalog/documents/move` with `{tumbler, new_source_uri,
  new_file_path, dry_run}`.
- `POST /v1/catalog/owners/move` with `{owner, new_root, new_repo_hash,
  dry_run}`.

A dry run runs the same code and rolls back, so its counts are the write's
counts. The engine reports the two routes as a capability flag in `GET
/version`, the way it reports the metadata route (`engine_reads/by_meta.py`).
A client whose engine lacks the flag refuses the command with the engine
version it needs, and prints no hint.

**Absorb.** Absorbing T into S runs `mergeDocuments`' steps with S as the
canonical row: T's links are remapped onto S (`remapLinksForMerge`), a
sensitive T raises S, and every other row whose `alias_of` names T is pointed
at S (S's own `alias_of`, if it names T, is cleared, so no row aliases
itself). Then
T's `source_uri` is cleared, as `mergeDocuments` clears a duplicate's, and T is
trashed (`document_trash`, which sets `deleted_at`). T's chunk manifest is left
on the trashed row, and `purge_trash` removes it after its grace window, as for
any trashed document. Absorb refuses when T and S belong to different owners
other than in an owner fold, because the catalog's `source_uri` lookup is not
owner-scoped and a cross-owner absorb would merge documents the user did not
name.

**Document move.** For a curator document S and the new path P:

1. If a live row T other than S holds `file://P`, absorb T into S.
2. Set S's `source_uri` to `file://P` and its `file_path` to P. A legacy row
   whose `file_path` is relative (the `_find_curator_document` legacy leg)
   becomes absolute.
3. Rewrite S's aspect rows. If T had an aspect row at P in S's collection, it
   is the newer extraction: delete S's old-path row first, then re-key T's row
   to S's `doc_id`, so the unique key (tenant, collection, `source_path`) never
   holds two rows. Otherwise set S's row's `source_path` and `source_uri` to P.
   Any other aspect row of T is deleted.
4. Highlights: if S has no highlights row and T has one, re-key T's row to S's
   `doc_id` and collection; otherwise T's row is deleted. S's row's
   `source_uri` becomes `file://P`.

S keeps its own chunks. If the file changed in the move, S's stored content
hash no longer matches the file, and the next index run at P re-chunks S the
way it re-chunks any edited file. The move never moves a chunk.

**Owner move.** For repo owner A, new root R and its repo hash H:

1. Every other owner holding H, live or deactivated, is folded into A. Such an
   owner B exists when the user indexed at R before moving (the Gap 2 order).
   For each live document of B, the live row of A with the same relative
   `file_path` is S, and B's row is absorbed into it. B's aspect rows sit in
   B's collection, never in S's, so they are deleted and S keeps its own,
   which the next aspect extraction refreshes if the file changed; B's
   highlights row moves to S only when S has none, as in the document move.
   B's documents with no counterpart in A (files added after the move) are trashed, and the next index run registers them under A.
   Then B is deactivated and its repo hash cleared, since the hash index allows
   one owner per hash; this runs before step 2 writes H to A.
2. Set A's `repo_root` to R and its `repo_hash` to H. A keeps its name. When A
   already has R and H (the Gap 3 case, where registration re-rooted it), this
   step changes nothing and step 3 does the work.
3. Rewrite the `source_uri` of A's documents, live and trashed, that are
   root-derived: a `file://` URI that ends in `/<file_path>`, with a relative
   `file_path`. The new value is the engine's own derivation for a registration
   at R (`deriveSourceUri`), so the move and a fresh registration agree.
   Every other row (another scheme, an empty URI, an absolute `file_path`) is
   skipped and counted. The old root is never needed, so the move works after
   registration has already overwritten it. When a live row of a third owner
   already holds a new value (a curator document indexed by absolute path under
   R), the move refuses before writing anything and names that row; the user
   moves or trashes it first. The dry run reports the same refusal.
4. Rewrite the `source_uri` of those documents' aspect and highlights rows the
   same way. An aspect row's `source_path` is the relative `file_path` and
   stays as it is.

B's chunks go the way of any trashed document's: `purge_trash` sweeps chunks
only trashed manifests reference after its grace window (inferred from its
`strandedChunkCount` javadoc, not run). Nothing in the move deletes a
collection.

**Undo.** A move can be undone only in part. A second move puts S's path
back. An absorbed row restored from trash comes back without its
`source_uri`, links and highlights, which now belong to S, and a folded owner
reactivated with `--execute reactivate` comes back with no repo hash and its
documents trashed. The dry run is therefore the safeguard, and the report's
list of absorbed tumblers is the record.

**Size and locking.** A move rewrites catalog, aspect and highlight rows
only, never T3. For a repository the size of nexus (7,197 tracked files), an
owner move updates on the order of 7,000 document rows, up to as many aspect
rows, and one owner row; a fold of B absorbs or trashes up to as many of B's
rows again. That is an estimate of 15,000 to 20,000 row writes, not a
measurement, in one Postgres transaction that holds row locks on the two
owners' documents while it runs. It is a user-run command that touches catalog
rows only, so the rule against boot-time data migrations does not apply, and
it leaves nothing to resume: the transaction commits whole or not at all.

#### Move hints

A hint is one line naming the exact command, printed at the end of a run. A
hint never writes, and it always prints the command's dry-run form, never
`--no-dry-run --confirm`: the dry run is the design's safeguard (Undo), so the
user sees the counts and the absorbed tumblers before choosing to write. Every hint is printed only when the engine reports the move
routes, so a client never prints a command its engine refuses. The two
repository hints read durable catalog state at the end of `nx index repo`, so
each repeats on every run until the move is made or the old owner is
deactivated, and neither depends on catching the moment of registration.
Every hint query is fail-open: a query that fails or times out, or data it
cannot read, is logged and dropped and never changes the run's exit code. The
hints also print when the run ends with a `ClickException` (a `--dir` batch
that ends "N of M failed"), because they describe writes that completed; any
other exception prints none.

The curator hint does depend on that moment, by choice. It runs where the
duplicate is written; a later `nx index pdf` or `nx index md` of the unchanged
file returns at the staleness check before any writer
(`_document_is_current`, `doc_indexer.py:2073-2100` and `:3165-3190`). That
skip path already reads the document's fence and one manifest row
(`doc_indexer.py:397-516`); repeating the hint would add a tenant-wide
metadata query to every unchanged file of every run, which is not worth it,
so the curator hint prints on a run that writes the document, which includes
the run that creates the duplicate. Afterwards the
duplicate itself stays visible (search returns the text twice), and the
documented command repairs it at any time.

- **Curator document (Gap 1).** `stamp_registered_content_hash`
  (`multi_batch_write.py:357`) is the one helper both single-file pipelines
  call just before writing a registered document (`doc_indexer.py:1764`,
  `pipeline_stages.py:766`). It is also reached for an in-repo `nx index md`
  or `rdr`, whose owner is a repo owner, so the hint runs only when D's owner
  is a curator owner. After the helper stamps hash H on document D, the client
  asks the metadata route for live rows with `content_hash` equal to H
  (`documents_by_meta`, which is tenant-wide and paged), keeps those under D's
  owner prefix other than D that are not alias rows, and prints `nx catalog
  owners --execute move --document <old tumbler> --new-path <D's path>` for
  each whose file is absent on this machine. A row whose file is present is a
  copy and gets no hint (nexus-o19i0). "Absent on this machine" is read as
  "moved", so a row of a catalog shared with another machine, or on an
  unmounted volume, is named too; the line is a dry run. The hint is bounded
  per run: it asks about at most 50 files, stops when the seconds spent in its
  own queries reach 30 (extraction and the time between files do not count),
  and stops after the first query that fails; when it skips any file the run
  ends with one note, "move hints checked for N of M files", which names no
  write form and is not printed on an engine without the move routes.

  The hint cannot see an old row that has no `meta.content_hash`. PDF and
  markdown documents carry it only since 2026-11-03 (f90135242, nexus-ypgbz):
  an older document gains it only when it is indexed again, changed or with
  `--force`, and `nx catalog backfill-meta` does not fill it. GH #1571's own
  document is of that age. So for a document indexed before 2026-11-03 and not
  since, a user who plans to move it runs `nx index pdf --force` (or `md`) on
  it once before the move, and the hint then works. For one already moved,
  the old row cannot be stamped any more (its file is gone): the user finds
  its tumbler (`nx catalog search '<file name>'`; `nx search` prints paths, not
  tumblers) and runs the document move by hand.
- **Documents outside their root (Gap 3).** At the end of `nx index repo`, the
  client reads the first page of the owner's documents and counts the rows the
  owner move would rewrite: root-derived rows whose `source_uri` differs from
  the URI a fresh registration at the root gives (the engine's
  `rootDerivedUri` and `deriveSourceUri`, mirrored by the client and pinned by
  a parity table). When any do, it
  prints `nx catalog owners --execute move --owner <prefix> --new-root
  <repo_root>`. This catches the same-name move after
  registration has silently re-rooted the owner row, on that run and every
  later one.
- **Renamed repository (Gap 2).** At the end of `nx index repo` for owner X,
  the client lists repo owners whose recorded root is gone on this machine
  (`_classify_owner_root`'s `path_vanished`). For each such owner V, it takes
  X's first 20 documents in tumbler order that carry `meta.content_hash`
  (stamped at registration for repo files, `indexer._register_time_meta`), asks
  the metadata route for each hash, and counts the hashes that also have a
  live row under V's prefix. When V matches at least half of the hashes asked
  and at least two, it prints the owner move command with V's prefix and X's
  root; several qualifying owners print highest count first, ties in tumbler
  order. The loop has its own 30-second budget, and when it runs out the
  matches already found still print. The 20, the half and the two are
  starting values to tune on a real renamed repository; they decide only
  whether a line prints.
- **Census.** `nx catalog owners --census` adds a `docs_outside_root` flag to
  each owner it reports, beside the root class and not as a new one: the root
  classes (`_CENSUS_CLASSES`, `owners.py:121`) are one per owner and drive the
  deactivate arm, and a flagged owner is never deactivate-eligible because of
  the flag. A flagged `healthy` owner is reported with the owner move command
  (the move refuses a root that is not a git work tree, so a flagged owner of
  another class keeps the flag and gets no command), and each `path_vanished`
  owner gets the move command with a `<new root>` placeholder, beside the
  deactivate arm it already offers. The flag is `null` when the page cannot be
  read or every entry on it is malformed. The flag's page read is made on
  every census, one `/list` per owner, even on an engine without the move
  routes.

#### A second live clone (Gap 4)

The rule lives in one client function that every repo owner registration
calls before its upsert: `ensure_owner_for_repo` (`http_catalog_client.py:885`)
and the two direct `register_owner` sites (`indexer.py:1397`,
`commands/catalog.py:1427`) all call it. The direct sites are not replaced by
`ensure_owner_for_repo`, because they carry an explicit repo hash and name
(tests pass a fixed hash, and `commands/catalog.py` hashes the registry path
without main-repository resolution); they keep their own arguments and pass
them to the rule.

On an `owner_for_repo` miss, before the upsert, `ensure_owner_for_repo` looks
up an owner with the same name and owner type (`/owners/by_name`). If one
exists and its recorded `repo_root` is a git work tree on this machine that is
not the same directory as this repository (`os.path.samefile`, so a symlinked
or differently cased path to the same clone is not split), the client
registers this repository as `<folder>-<repo hash>`. The engine sees a new name
and a new hash and creates a new owner. If the recorded root cannot be read (a
permission error), the client also registers a separate owner: a wrong split
costs one duplicate index, a wrong takeover costs the first clone its owner.
If the root is gone, registration proceeds as today and the Gap 3 hint picks
the case up at the end of the run.

The engine's name-first match (`resolveOwnerAddress`) is unchanged, so the
engine characterization test for Gap 4 stays true; the client stops sending
the name that triggers it. Owners two clones already share are not split after
the fact: their documents keep the mixed roots they have. The Gap 3 hint
reports them; there the remedy is the user's call, not necessarily a move.
A clone whose hash the shared owner no longer carries gets its own owner on
its next registration.

## Alternatives Considered

Three candidates, compared on the five gaps. The decision (Proposed Solution)
takes Alternative 1, adds hints that print its command, and for Gap 4 takes
Alternative 3's outcome (two live clones, two owners) by a client-side rule.

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
to run it). For Gap 4, Alternative 3's outcome, and only when the old root
still exists: the client gives the second clone its own name, and the engine's
matching does not change. Alternative 2 is rejected as an automatic act: its
filesystem probe cannot tell an unmounted volume or another machine's path
from a deleted file, so the same probe is used only to print a hint.

## Trade-offs

### Consequences

- A move stays something the user does on purpose. A moved PDF still indexes
  as a second document and a renamed repository still becomes a second owner,
  but the run prints the command that repairs it: for a repository, that run
  and every later one; for a curator document, the run that writes the
  duplicate, and only when the old row was indexed on or after 2026-11-03 or
  re-indexed since (Move hints).
- The move keeps the tumbler. The repo reconcile's rename, which absorbs a file
  moved inside one repository, keeps links but mints a new tumbler. A citation
  by tumbler survives a move made with the command and does not survive one the
  reconcile absorbs. This RDR leaves the reconcile as it is.
- Chunks are not touched. A move rewrites catalog, aspect and highlight rows only, because
  chunks carry no path (RDR-108). A moved and edited file is re-chunked by the
  next index run, as any edited file is.
- When the new path already has its own row (the GH #1571 state, and Gap 2
  when the user indexed before moving), the move absorbs that row, so the
  document form is a merge as well as a rename. The absorbed row is trashed;
  restoring it brings back a row without its path, links or highlights, so an
  absorb is not fully reversible (Technical Design, Undo).
- Files added to a renamed repository between its re-index and the move lose
  the links made to them in that window: the fold trashes them, and the next
  index run registers them under the old owner with new tumblers.
- A second live clone gets its own owner. Files the two clones share are
  indexed twice, once per owner: the right result for two working trees, at
  twice the storage for those files.
- A folded owner's chunks are swept by `purge_trash` after its grace window,
  with its trashed documents; its collections are not deleted.

### Risks

- The second clone's owner is named `<folder>-<repo hash>`, and readers that
  derive from the owner name see that name: the RDR lifecycle project
  (`<owner name>_rdr`, `plans/lifecycle_stream.py`) and the owner-name scope
  (`catalog/owner_scope.py`). The first clone keeps the plain name.
- A large owner move is one transaction of an estimated 15,000 to 20,000 row writes,
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
  acts on a hint, and the line a hint prints is the dry run, which shows what
  would change before the user adds `--no-dry-run --confirm`.
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
the absorb and the four steps of the Technical Design, one transaction, and a
dry run that rolls back; report the move routes as a capability flag in `GET
/version`. Tests against a real Postgres: a plain re-point; an
absorb that remaps links and `alias_of` (an S aliased to T ends with no alias,
not a self-alias), raises sensitivity, clears and trashes
T, moves highlights and re-keys the newer aspect row without a unique-key
violation; a refusal for a missing or trashed tumbler, a repo-owned S, and a T
under another owner; the absorbed tumblers listed in the response; a dry run that writes nothing and reports the same
counts.

#### Step 2: Owner move

Add `POST /v1/catalog/owners/move`: fold every other owner holding the new
repo hash (live or deactivated), re-root, rewrite root-derived `source_uri`
with `deriveSourceUri` and count the rows skipped, and rewrite aspect and
highlights `source_uri`. Tests besides the characterization cases: an owner
already at R and H (the Gap 3 shape) whose documents still name the old root;
a deactivated owner holding H; a row with another scheme or an absolute
`file_path`, skipped; a refusal for a curator or deactivated A; a refusal,
with nothing written, when a third owner's live row holds a new `source_uri`.
Extend
`OwnerConvergenceByNameCharacterizationTest`: the renamed-move case gains a
move and asserts one owner, the old tumblers at the new paths, and the second
owner deactivated with no repo hash. Its same-name and second-clone cases keep
their assertions, because the engine's matching does not change.

### Phase 2: Client command

#### Step 3: `--execute move`

Add `move` to `_MUTATION_ACTIONS` in `owners.py`, with `--document` and
`--new-path`, or `--owner` and `--new-root`; refuse when the new path is
absent or, for an owner, not a git work tree; make the path absolute and, for
an owner, resolve it to its main repository and compute the hash with
`_repo_identity_with_main`; refuse on an engine without the capability flag,
naming the version it needs; print the route's counts and absorbed tumblers.
Add the two `HttpCatalogClient` writer methods to `CATALOG_WRITE_OPS`
(`catalog_protocol.py:348`), or the closed write proxy raises
`AttributeError`. The version the refusal names is a hand-kept
`CATALOG_MOVE_MIN_ENGINE_VERSION` constant, `None` until the engine tag
carrying the routes is cut, like `CATALOG_SENSITIVITY_MIN_ENGINE_VERSION`.
`tests/test_moved_curator_doc_identity_characterization.py`'s moved-PDF and
409 cases become tests of the command: one live row at the new path, the old
tumbler kept.

### Phase 3: Move hints

#### Step 4: Curator hint

Hook the hint to `stamp_registered_content_hash`, the helper both single-file
pipelines call before writing, so `nx index pdf` and `nx index md` both reach
it; the hook records the hint, and the command prints recorded hints at the
end of the run. Run it only for a curator-owned document. Query the metadata
route for the hash, keep rows under the document's owner prefix, and record
the document hint for each whose file is absent; record nothing on an engine
without the move routes. The skip path is not touched. A hint query that fails
or times out is logged and dropped, and never changes the run's exit code.
CLI-level tests through `nx index pdf` and `nx index md`: a moved file prints
the hint, in its dry-run form, on the run that registers it, and a second run
of the unchanged file prints none and makes no `documents_by_meta` call; a
byte-identical copy whose original is present prints none; an in-repo markdown
file prints none; an old row with no `meta.content_hash` prints none and no
error; an old engine prints none; a failing metadata query leaves the exit
code unchanged.

#### Step 5: Repository hints

The documents-outside-root hint and the renamed-repository hint at the end of
`nx index repo`, and the census's `docs_outside_root` flag (not a root class;
never deactivate-eligible) and its line under `path_vanished` owners. The
renamed-repository hint asks the metadata route once per hash and groups the
hits by owner prefix, so its cost does not grow with the number of vanished
owners. A hint query that fails or times out is logged and dropped, and never
changes the run's exit code. CLI-level tests through `nx index repo`: a
same-name move prints the hint, in its dry-run form, on the run that re-roots
the owner and on the next run; a renamed repository prints the owner move
command; a healthy, unmoved repository prints none; no hint writes a row; a
failing metadata query leaves the exit code unchanged.

### Phase 4: Second clone

#### Step 6: Separate owner

In `ensure_owner_for_repo`, on an `owner_for_repo` miss, register under
`<folder>-<repo hash>` when a same-name owner's root is another live git work
tree (not `os.path.samefile` with this one) or cannot be read. Factor the rule
into one function that `ensure_owner_for_repo` and the two direct repo
`register_owner` sites (`indexer.py:1397`, `commands/catalog.py:1427`) call
with their own name, hash and root, so the explicit-hash callers and tests keep
their arguments. CLI-level tests through `nx index repo`: two live clones give two
owners and neither takes the other over; a symlinked path to the same clone is
not split; a gone root falls through to the Gap 3 hint.

### Day 2 Operations

The design creates no new persistent resource. The move writes existing rows.
Its reversal is partial (Technical Design, Undo): a second move puts paths
back; `nx catalog restore` and `--execute reactivate` restore an absorbed row
and a folded owner without what the move gave to S.

### New Dependencies

None.

## Test Plan

- **Scenario**: a PDF indexed before 2026-11-03, moved and indexed again —
  **Verify**: no hint and no error; the document move run by hand repairs it.
- **Scenario**: a PDF moved and indexed again (Gap 1) — **Verify**: the hint
  names the old tumbler, in its dry-run form, on that run, and a later run of the unchanged file
  prints none; after the move one live row holds the new path with
  the old tumbler and links; search returns the text once.
- **Scenario**: a moved and edited PDF — **Verify**: no hint (the hash
  differs); a move run by hand re-points the row, and the next index run
  re-chunks it.
- **Scenario**: a renamed repository indexed at its new path (Gap 2) —
  **Verify**: the hint names the old owner; after the move one owner remains
  with the old tumblers, the new root and hash; the second owner is
  deactivated.
- **Scenario**: a repository moved under the same folder name (Gap 3) —
  **Verify**: the hint prints on that run and on the next one; after the move
  every root-derived `source_uri` names the new root.
- **Scenario**: two live clones with one folder name (Gap 4) — **Verify**: two
  owners; a run from either clone changes neither owner's root.
- **Scenario**: the same clone reached through a symlink — **Verify**: one
  owner, no split.
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
leaves in place. The curator hint's blindness to documents indexed before
2026-11-03 is a stated limit (Move hints), not a contradiction.

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
| `_repo_identity_with_main`, `ensure_owner_for_repo`, `owner_for_repo` | nexus client | Source Search |
| `stamp_registered_content_hash`, `documents_by_meta` | nexus client | Source Search |
| `deriveSourceUri`, `document_trash` | nexus engine | Source Search |

### Scope Verification

The Minimum Viable Validation (GH #1571 end to end) is in scope as the test of
Phase 3 Step 4 run after Phase 2, not deferred.

### Cross-Cutting Concerns

- **Versioning**: the client command needs the engine routes; on an older
  engine it refuses with the engine version it needs, and the hints print
  nothing there.
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

Right-sized: one command with two forms, four hints and one client rule in one
shared function. The
Research Findings carry the line references the design leans on and nothing
else.

## Revision History

- 2026-11-19: Gate round 1 — BLOCKED (1 Critical, 0 Significant, 1 ship-blocker(s)); blob `def7ceb59122`; critique `nexus_rdr/254-gate-critique-2026-11-19-r1`.
- 2026-11-19: Fix check of round 1 fixes FAILED (1 BLOCKS-PLANNING, curator hint repeat claim); fixed once; verdict `nexus_rdr/254-fix-check-3c757064d55d`.
- 2026-11-19: Gate round 2 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); blob `c2ff1151086d`; critique `nexus_rdr/254-gate-critique-2026-11-19-r2`.
- 2026-11-20: Move hints wording matched to the reviewed Phase 3 code (grant 3; reviews `nexus_rdr/254-p3-review-r1`, `nexus/confirm-afba69e8d-none-2026-11-20-r3`): alias rows excluded, per-run bounds and the closing note, fail-open and `ClickException` printing, the engine's outside-root rule, the two-hash minimum, the census move line for healthy owners only, and the `nx catalog search` lookup.
