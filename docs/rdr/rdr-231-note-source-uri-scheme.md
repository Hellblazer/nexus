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
related_issues: [nexus-t7fbb, nexus-ql5m5]
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
RDR-007 A2 freeze. Sam's rulings of 2026-10-24 on the draft's open questions are
in § Decisions; one of them folds nexus-ql5m5 in as Gap 4. At the round 1 gate
(2026-10-26) Sam confirmed the backfill and ruled that `chroma://` goes from storage
and from new code: every note is backfilled, the engine keeps accepting the old
spelling on input, and no note is shown back with it (§ Decisions 4 and 5).

In this RDR a **note URI** is `chroma://<c>/<rest>` or `nx-note://<c>/<rest>` whose
first path segment `<c>` (the collection name) does not start with `rdr__`, `docs__`
or `code__`. Those three are `aspect_readers.FILE_ROUTED_PREFIXES`
(`src/nexus/aspect_readers.py:97`): collections whose identity is `file://`. A
`chroma://` URI under one of them is not a note; it is migration residue from an
older `uri_for`, which `nx catalog backfill-source-uri` rewrites to `file://`
(`src/nexus/commands/catalog_cmds/backfill.py:78-115`). The canonical spelling of a
note URI is `nx-note://`; `chroma://` is the legacy spelling.

### Enumerated gaps to close

#### Gap 1: Note identity carries a retired product's name

`aspect_readers.uri_for` (`src/nexus/aspect_readers.py:206`) mints
`chroma://<collection>/<title>` for every non-file collection, and the store hook
uses it as the note's identity (`src/nexus/catalog/store_hook.py:883`). The scheme
resolves nothing against Chroma; it is an opaque, collection-scoped identity. The
fix delivers a scheme whose name says what it is.

#### Gap 2: The scheme is spelled independently at every site that knows it

There is no single definition. Every site below spells it on its own (line numbers
as read on develop 2026-10-23 and 2026-10-26; they drift, so an implementer
re-reads each site before editing it):

| Role | Site |
| --- | --- |
| Mints a stored URI | `aspect_readers.uri_for` (the store hook `:947` and exporter `:652` call it); `http_vector_client.py:5918` (`find_ids_by_title`, through `uri_for`); `aspect_extractor.py` `:1933`, `:2379`, `:2417` (aspect rows' `source_uri`, through `uri_for`, unquoted); `operators/aspect_sql.py:591` (through `uri_for`); |
| Mints a transient read URI | `aspect_readers._read_https_uri` `:789` (T3-first read of an https source); `commands/enrich.py:1617` (dry-run read, quoted); `aspect_extractor.py` `:1332`, `:1445`, `:1609`, which build `chroma://{collection}/{quote(path, safe='/')}` for `read_source`, quoted, so a different string from the stored form |
| Classifies by prefix | Three different predicates: `reconcile_stale._STORE_PUT_URI_PREFIX` `:230`, used `:379`, a bare `startswith("chroma://")` with no collection guard (the residue text at `:220-229` is a comment); `recovery_bundle.classify_store_put_origin` `:90-100`, which first excludes file-routed prefixes by `entry.physical_collection`, then tests `chroma://`; its residue counter `:185`; `backfill.py` `:84`, `:102`, `:111` (residue census and rewrite, keyed by `physical_collection`) |
| Parses the URI | `recovery_bundle._resolve_link_endpoint` `:496-502` (collection/title re-derivation when `by_source_uri` misses) |
| Dispatches on scheme name | `aspect_readers._READERS` `:1107`, `_STAT_HANDLERS` `:1939`; result labels `"scheme": "chroma"` `:521`, `"served_from": "chroma"` `:797`; `catalog/types._KNOWN_URI_SCHEMES` `:80`; `doc/catalog_links._LINK_SAFE_SCHEMES` `:285` (derived by subtracting from `_KNOWN_URI_SCHEMES`, so a scheme added to the known set becomes link-safe unless it is excluded by name) |
| Compares URIs exactly | `http_catalog_client.by_source_uri` `:2184`; store hook chash-dedup guard `:996`; exporter import `:745`, `:748`; `recovery_bundle._resolve_link_endpoint` `:498`; `operators/aspect_sql.py` `:622` and `:669-690` (`u in matched_uris`, `u in uri_to_value` against `operator_filter` and `operator_groupby` results) |
| User-facing text | `commands/catalog.py:606` (register help), `commands/enrich.py:2202` (help) |
| Comments and docs only | `integrity.py:623` (a docstring; there is no constant there), `doctor_references.py:14`, `aspect_worker.py:585`, `db/t3.py:771`, `catalog/AGENTS.md:9`, `db/AGENTS.md:7` |
| Engine | rename rewrite `CatalogRepository.java:9778-9801` (collision pre-check, literals `:9782-9783`) and `:9970-9994` (three UPDATEs); `deriveSourceUri` javadoc `:933`; the other moves in Gap 4, which do not spell it and should. Two applied changelogs mention it in comments (`hygiene-001-not-null.xml:93`, `aspects-004-doc-id-backfill.xml:30`); applied changesets are never edited. |

`engine_version.py:577` is release history and stays as written. A change that
misses one site leaves notes that a classifier, reader or rename treats as
something else. The fix delivers one definition per side, and a lint that no other
spelling survives.

#### Gap 3: Changing the spelling duplicates notes unless the engine canonicalizes

A re-put finds its Document by `source_uri`. Once stored rows say `nx-note://`, any
caller that still sends `chroma://` (an un-upgraded client, an old recovery bundle or
export being imported) misses them, and the engine mints a second Document. The
reverse also holds: a new client that sends `nx-note://` to an engine that has not
backfilled mints a duplicate beside every `chroma://` row, and that duplicate then
makes the backfill collide. The fix must keep one Document per note for every
caller and every artifact. It does not have to keep un-upgraded clients' displays
and classifiers unchanged: Sam ruled (2026-10-26) that old clients upgrade.

#### Gap 4: Moving a note between collections by any path other than rename leaves its URI naming the old collection

The collection rename rewrites `chroma://<old>/` to `chroma://<new>/` in all three
tables (nexus-sis0m.3), and refuses up front, with nothing moved, when a rewritten
URI is already held by a live document (`SourceUriCollision`,
`CatalogRepository.java:9778-9801`). Other engine paths move note rows between
collections and leave `source_uri` as it was (nexus-ql5m5; the highlight rename and
the re-embed move were found while folding that bead in):

| Path | Route | Moves | Site |
| --- | --- | --- | --- |
| Re-home | `POST /v1/catalog/collections/rehome` | every table in `COLLECTION_SCOPED_TABLES`, `catalog_documents.physical_collection` and both aspect tables among them | `CatalogRepository.rehomeCollection` `:10148`, `moveScopedTable` |
| Collection update | `POST /v1/catalog/update`, `/update_many` with `physical_collection` | `catalog_documents.physical_collection` of one document | `buildUpdateDocumentQuery` (`UPDATABLE_DOC_COLUMNS` `:1895-1897`); callers `doc_indexer.py:1125`, `catalog/path_ambiguity.py:387`, `nx catalog migrate-fallback` (`migration.py:257`) |
| Re-embed move (RDR-210 P6) | manifest write with a `move` | `catalog_documents.physical_collection`, `document_aspects`, `aspect_extraction_queue`, `document_highlights` collection, per document | `CatalogRepository.completeMove` `:6391`, called from `writeManifestMany` `:6112` |
| Aspect rename | `POST /v1/aspects/rename_collection` | `document_aspects.collection` | `AspectRepository.renameAspectCollection` `:429` |
| Highlight rename | `POST /v1/aspects/highlights/rename_collection` | `document_highlights.collection` | `AspectRepository.renameHighlightsCollection` `:1358` |

A note moved by re-home, re-embed or a collection update keeps an identity naming its old collection. A
re-put under the new collection misses it and mints a duplicate, and its aspects and
highlights stop joining it by URI. The re-embed move does this to `store_put` notes
by design: RDR-210 P6 moves every document of a re-embedded collection to its
sibling. The fix delivers one rule for paths that move the catalog row: the move
rewrites the note's URI in the same statement, and refuses when the rewritten URI is
held by a live document.

The aspect rename and the highlight rename are different. They move only the
denormalized `collection` column of satellite rows; the note's catalog row stays
where it is, and an aspect row's `source_uri` equals its document's
(`mcp/core.py:4513-4525`). Rewriting the URI there would split the satellite from its
document. In the client's `rename_collection_cascade` they run after the catalog
rename has moved and rewritten everything, and find nothing to move; there is no
in-tree caller of either on its own (`db/t2/__init__.py:412-416`). They are left
unchanged. Of the other writers of `physical_collection` (`nexus_rdr/231-research-16`),
upsert and import take the caller's whole row, `source_uri` included, so the caller's
URI (canonicalized, step 2) is the one stored; `updateDocumentCollection` and
`updateDocumentCollections` (`:3433`, `:3445`) have no in-tree caller and are out of
scope.

## Relationship to Prior RDRs

Searched the RDR titles and bodies for note identity, `source_uri`, the store hook,
rename and Chroma retirement.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-108 (graph identity normalization) | Origin | Made `source_uri` the Document identity; this RDR keeps that rule and changes only the note scheme's spelling. |
| RDR-155 (pgvector T3 consolidation) | Origin of the debt | Retired Chroma (P4b) and left the scheme name behind. |
| RDR-192 (title-only note identity) | Precedent | Defined the title-only note identity this scheme encodes; its convergence rule (re-put reaches the same Document) is the constraint in Gap 3. |
| RDR-210 (dual-mode local engine) | Adjacent | Owns the rename verbs whose rewrite (nexus-sis0m.3) must learn the new scheme, and the P6 re-embed move (`completeMove`) that moves notes to a sibling collection (Gap 4). |

## Context

### Background

Found by the 2026-10-22 cross-surface shakeout (board/nexus-shakeout,
cowork round 3, F15/F16): `nx catalog show 1.11.782` printed
`chroma://knowledge__nexus-testing__voyage-context-3__v1/cowork-shakeout-probe-2026-10-21`.
The 2026-10-23 analysis on nexus-t7fbb traced the sites; the critique in T2
`nexus/critique-48f3l-and-rdr231-draft-2026-10-23` (part B) found the gaps this
revision closes.

### Technical Environment

Client: `src/nexus/aspect_readers.py`, `catalog/store_hook.py`,
`catalog/http_catalog_client.py`, `catalog/recovery_bundle.py`, `exporter.py`, the
catalog commands in Gap 2, `engine_version.py`. Engine:
`CatalogRepository`, `AspectRepository`, `CatalogHandler`, `AspectHandler`,
Liquibase changelogs. The live-URI partial unique index
`ux_catalog_documents_live_source_uri` (`catalog-016`: unique `(tenant_id,
source_uri)` over live rows with a non-empty URI) constrains any rewrite.

Mixed versions arise in two places. Locally, the engine is the client's pinned
dependency (`engine_version.REQUIRED_ENGINE_VERSION`, `engine_version.py:684`;
convergence installs it), so an old client meets a new engine only briefly, and a
new client meets an old engine only until `nx daemon restart-stale` runs. Against
the managed cloud engine, clients of any version at or above the managed floor
talk to whatever engine is deployed, indefinitely; under this RDR an un-upgraded
cloud client keeps working without duplicates but loses its view of notes (Gap 3,
step 3), until it upgrades.

## Research Findings

### Investigation

Source reading of every site in Gap 2 and of every engine method that reads or
writes a `source_uri` column (`grep SOURCE_URI` over `CatalogRepository.java` and
`AspectRepository.java`, each hit mapped to its enclosing method and route), the
catalog-016 index, the rename cascade and its test, and the changeset precedents
for tenant-spanning row DML.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `CatalogRepository` | Yes | Register paths derive the URI in one helper (`deriveSourceUri`, `:890`, called `:1116`, `:1310`); upsert and import paths guard identity with `guardDocumentIdentity` (`:537`); `/update` writes `source_uri` with no guard (`UPDATABLE_DOC_COLUMNS` `:1895`); every document row leaves through `docRowFromRecord` (`:11919`). |
| `AspectRepository` | Yes | Writes `source_uri` in `upsertAspect` `:190`, `importAspectsBatch` `:518`, `doImportAspect` `:615`, `upsertHighlight` `:689`, `importHighlightsBatch` `:826`, `doImportHighlight` `:886`; looks up by it in `getHighlightBySourceUri` `:760`, `filterBySourceUris` `:1716`, `groupByField` `:1763`, `confidenceAggregate` `:1810`; renders it in `recordToMap` `:1987` and `highlightToMap` `:2036`. |
| catalog-016 index | Yes | Unique over live rows per tenant; a rewrite that maps a row onto a live twin fails. |
| `aspects-001-baseline.xml` | Yes | `document_aspects` is unique on `(tenant_id, collection, source_path)` (`:75`) and `document_highlights` on `(tenant_id, doc_id)` (`:121`): neither has a unique key on `source_uri`, so only `catalog_documents` can collide. |
| Changeset precedents | Yes | `aspects-004-doc-id-backfill.xml` (tenant-spanning UPDATE inside a NO FORCE / FORCE toggle on `catalog_documents`); `fk-004-chunks-collection-registry.xml:175-206` (a guard count in a `DO` block inside the toggle, and why an untoggled read sees zero rows). |

### Key Discoveries

- **Verified**: three tables hold a note URI: `catalog_documents.source_uri`, `document_aspects.source_uri` and `document_highlights.source_uri`. The rename rewrites all three (`CatalogRepository.java:9970-9994`); `CatalogRenameIdentityCascadeTest` enumerates holders by sweeping every `nexus.*` text and json column for the URI (`columnsHolding`, `:356-386`), not from a hand-kept list.
- **Verified**: the engine's register paths report `created=false` when the idempotency lookup matches (`RegisterOutcome`, `CatalogRepository.java:1074`, `:1126`), and the store hook threads it (`store_hook.py:1146`). The rollback in `rollback_minted_catalog_entry` (`store_hook.py:1185`) therefore deletes a live note only if the engine's lookup misses, that is, only if register is not canonicalized.
- **Verified**: an old client's `by_source_uri` discards any row not byte-equal to its query (`http_catalog_client.py:2184`); its chash-dedup guard does the same (`store_hook.py:996`).
- **Verified**: `_normalize_source_uri` (`catalog/types.py:93`) has no production caller; `_KNOWN_URI_SCHEMES` matters at runtime only through `_LINK_SAFE_SCHEMES`.
- **Verified**: under FORCE ROW LEVEL SECURITY, Liquibase's role (`nexus_admin`, NOBYPASSRLS, no tenant GUC at migration time) reads and writes zero rows of a tenant table unless the changeset toggles NO FORCE first (`aspects-004-doc-id-backfill.xml` header; `fk-004-chunks-collection-registry.xml:175`). A Liquibase `sqlCheck` precondition on row counts would therefore always see zero.
- **Verified**: the three client classifiers use three predicates (`nexus_rdr/231-research-13`): `reconcile_stale.py:379` is a bare `startswith("chroma://")`; `recovery_bundle.py:90-100` excludes file-routed collections by `entry.physical_collection`, and `backfill.py` selects exactly those rows as its candidates. Only the last two treat `chroma://` under a file-routed collection as residue; the `reconcile_stale.py:220-229` text saying so is a comment, not code.
- **Verified**: `completeMove` (`CatalogRepository.java:6391`, the RDR-210 P6 re-embed move) sets `physical_collection` and the satellites' `collection` and never `source_uri`; it refuses a tombstoned document per document (`TombstonedDocumentException`), which the re-embed runner recognizes (`nexus_rdr/231-research-11`).
- **Verified**: the rename refuses before anything moves when a rewritten URI is held by a live document (`SourceUriCollision`, `:9778-9801`); re-home moves table by table and reports per-table refusals, and chunks, keyed by chash, cannot be held back per document (`nexus_rdr/231-research-12`).
- **Inferred, not run** (`nexus_rdr/231-research-14`): with engine canonicalization and no legacy rendering, an old client's re-put does not duplicate (its byte compare drops the `nx-note://` row, register then returns `created=false`), but the re-put's metadata (tags, category, ttl, the `meta.doc_id` stamp) is not applied, and its classifiers, its recovery-bundle export, `nx doctor --check-references`, `aspect_sql`'s compare and `find_ids_by_title` stop recognising notes.

### Critical Assumptions

- [ ] The three tables in Key Discoveries are the only places a note URI is stored. — **Status**: Verified in source (rename cascade and its information_schema sweep); not yet checked against production data — **Method**: run the `columnsHolding` sweep for `chroma://` against a restored copy of the cloud database before the engine tag, and assert in the backfill test that the sweep finds no note URI after the changeset
- [ ] No tenant holds a live legacy note row and a live `nx-note://` row for the same note, so the backfill cannot collide on catalog-016. — **Status**: Unverified on production; true by construction for any database no new client has written to — **Method**: the census query in Technical Design step 5, run read-only per tenant against the cloud database before the engine tag, and the same count as the changeset's in-transaction guard
- [ ] Every engine route through which a caller's URI enters a write or a lookup is in the route table of Technical Design step 2. — **Status**: Verified by source search (2026-10-23; the string-key and `vectors` domain re-swept by the 2026-10-26 fix checks) — **Method**: `NoteUriRouteGateTest` drives each listed route with `chroma://` and asserts `nx-note://` is stored or matched, and a static sweep fails on any reference to the `source_uri` column, by the jOOQ symbol or by the string key `"source_uri"`, in `CatalogRepository`, `AspectRepository` or the `vectors` package, inside a method the table neither lists nor exempts
- [ ] The cloud engine does not serve from an old and a new instance at once after the backfill. — **Status**: Unverified; a conexus-side fact (`nexus_rdr/231-research-15`) — **Method**: ask conexus for the deploy topology before the tag; deploy with old instances drained, because the changeset runs once and nothing rewrites a lone `chroma://` row an old instance writes after it; run the census for live `chroma://` note rows after the rollout

## Proposed Solution

### Approach

Store and show only `nx-note://`; accept `chroma://` on input forever. The engine
backfills existing rows, canonicalizes every incoming note URI, rewrites note URIs
on every move of a note's catalog row, and returns the stored spelling to every
caller. The client mints and expects `nx-note://` only, and turns a legacy URI from
an old artifact into the new spelling at the one place it reads one.

### Technical Design

1. **One note-URI rule per side.** Client: in `aspect_readers` beside
   `FILE_ROUTED_PREFIXES`, `NOTE_URI_SCHEME = "nx-note://"`, `is_note_uri(uri)`
   (`nx-note://` with a collection segment that is not file-routed),
   `note_uri(collection, rest, *, quote_rest=False)` for every mint (stored mints
   unquoted, the extractor's transient read mints with `quote_rest=True`, so both
   keep today's string apart from the scheme), and `canonical_note_uri(uri)`, which
   turns a legacy note URI into `nx-note://` and returns anything else unchanged.
   Engine: a `NoteUri` helper with `canonical(uri)` and `moveTo(field, oldColl,
   newColl)`, and its own copy of the three prefixes. One fixture corpus of URIs
   (notes in both spellings, residue under each file-routed prefix, `chroma://c`
   with no slash, an empty segment) is run three ways, each answering "is this a
   note, in either spelling": `is_note_uri(canonical_note_uri(u))` in Python,
   `NoteUri.isNote(u)` in Java, and, in SQL, the step 5 predicate or the same
   predicate on the `nx-note://` form. The three answers must agree on every row,
   and the corpus's residue rows are built from `FILE_ROUTED_PREFIXES` so a new
   prefix joins the corpus.

2. **Engine input canonicalization, forever.** Each route applies
   `NoteUri.canonical` to the caller's URI before it reads or writes:

   | Route | Repository entry point |
   | --- | --- |
   | `POST /v1/catalog/register` | `upsertDocument` (before `guardDocumentIdentity`) |
   | `POST /v1/catalog/doc/register` | `registerDocumentWithOutcome` via `deriveSourceUri` (so the idempotency lookup, insert and race-loser lookup all see it) |
   | `POST /v1/catalog/doc/register_many` | `registerDocumentManyWithOutcome` via `deriveSourceUri` (batch idempotency fetch, insert, race patch) |
   | `POST /v1/catalog/update`, `/update_many` | `buildUpdateDocumentQuery` when `source_uri` is in the field map |
   | `POST /v1/catalog/import/document` | `importDocumentsBatch`, `doImportDocument` (before their identity guards) |
   | `GET /v1/catalog/list?source_uri=` | `documentsBySourceUri` |
   | `GET /v1/catalog/resolve?source_uri=` | `documentsBySourceUri` |
   | `POST /v1/aspects/upsert`, `/import` | `upsertAspect`, `importAspectsBatch`, `doImportAspect` |
   | `POST /v1/aspects/highlights/upsert`, `/highlights/import` | `upsertHighlight`, `importHighlightsBatch`, `doImportHighlight` |
   | `GET /v1/aspects/highlights/get_by_source_uri` | `getHighlightBySourceUri` |
   | `POST /v1/aspects/operator-query` | the `source_uris` list before `filterBySourceUris`, `groupByField`, `confidenceAggregate` |

   Exempt, with the reason recorded in the gate test: `POST /v1/catalog/merge`
   (`mergeDocuments` moves a stored URI; no caller URI enters), the moves in step 4,
   and the `vectors` routes, which only return the catalog's stored `source_uri`
   (`PgVectorRepository.sourceUrisByChash`, `enrichSearchRows` `:8162`, `:8224`,
   `ManifestLabelStage`) and take none. `/list` and `/resolve` canonicalize inside
   `documentsBySourceUri`, not in the handler, so a later caller of that method
   gets it too.

3. **Engine output: the stored spelling, to everyone.** No response rewriting and no
   capability header. After the backfill every note URI a response carries, the
   `include_source_uri` field of the `vectors` routes included, is `nx-note://`. An
   un-upgraded client therefore still converges on one Document per note (its
   re-put's byte compare misses, register's canonicalized idempotency lookup returns
   `created=false`, and the rollback never targets a live note), but the re-put's
   metadata is not applied, and its classifiers, its recovery-bundle export,
   `nx doctor --check-references`, `aspect_sql` and `find_ids_by_title` stop
   recognising notes until it upgrades (`nexus_rdr/231-research-14`, inferred from
   source, not run).
   This is the cost Sam accepted (Decisions 5).

4. **Every move of a note's catalog row rewrites its URI, and refuses a taken one.**
   `NoteUri.moveTo(field, oldColl, newColl)` is a jOOQ `CASE` that rewrites
   `nx-note://<old>/` to `nx-note://<new>/` and `chroma://<old>/` to
   `chroma://<new>/`, every other value unchanged, so file-routed residue keeps its
   own spelling. Each path sets `source_uri = NoteUri.moveTo(...)` in the same
   UPDATE that moves the row, for `catalog_documents` and for the document's
   `document_aspects` and `document_highlights` rows.

   - **Rename** (`renameCollectionTxn`): its collision pre-check
     (`:9778-9801`) and its three UPDATEs (`:9970-9994`) run through `moveTo`, so
     the pre-check covers both spellings.
   - **Re-home** (`rehomeCollection`): the same pre-check, run before anything
     moves: if any live document in the source would land on a URI a live document
     already holds, the re-home throws `SourceUriCollision` (409) and moves nothing,
     as the rename does. Re-home cannot hold back one document, because its chunks
     and manifest rows move by table and chunks are keyed by chash
     (`nexus_rdr/231-research-12`), so the refusal is whole. Only live rows are
     checked, because catalog-016 constrains only live rows; tombstones move with
     their URIs rewritten.
   - **Collection update** (`buildUpdateDocumentQuery`): when the field map moves
     `physical_collection` and carries no `source_uri`, the UPDATE sets
     `source_uri = moveTo(...)` from the row's current collection to the new one,
     after the same check for that document (409 `SourceUriCollision`). When the
     caller sends `source_uri` too, the caller's URI, canonicalized, is stored.
   - **Re-embed move** (`completeMove`): before its UPDATE, the same check for the
     one document; a taken URI throws `SourceUriCollision`, which rolls back that
     document's manifest write while the batch's other documents proceed
     (`writeManifestMany`'s per-document catch). Two edits make it a per-document
     skip rather than a failed job: `failureDetail`'s allowlist passes
     `SourceUriCollision`'s message through (today it reports "internal error"),
     and `ReembedRunner` gains a skip reason for it beside the tombstone prefix
     (today any other reason is fatal, `ReembedRunner.java:398-406`).
   - **Aspect rename and highlight rename**: unchanged (Gap 4).

5. **Backfill (DATA EFFECT), one changeset.** Tenant-spanning, in one transaction,
   in the shape of `aspects-004-1` and `fk-004-0-reconcile-precount`
   (`splitStatements="false"`): toggle NO FORCE on `catalog_documents`,
   `document_aspects` and `document_highlights`; run the guard; run three UPDATEs;
   restore FORCE in reverse order. The note predicate, written for column `u`:

   ```sql
   starts_with(u, 'chroma://')
   AND NOT (starts_with(split_part(substr(u, 10), '/', 1), 'rdr__')
         OR starts_with(split_part(substr(u, 10), '/', 1), 'docs__')
         OR starts_with(split_part(substr(u, 10), '/', 1), 'code__'))
   ```

   Guard, which aborts the transaction (and with it the toggles) on any collision:

   ```sql
   DO $$
   DECLARE n BIGINT;
   BEGIN
     SELECT count(*) INTO n
       FROM nexus.catalog_documents o
       JOIN nexus.catalog_documents t
         ON t.tenant_id  = o.tenant_id
        AND t.source_uri = 'nx-note://' || substr(o.source_uri, 10)
        AND t.deleted_at IS NULL
      WHERE o.deleted_at IS NULL
        AND <note predicate on o.source_uri>;
     IF n > 0 THEN
       RAISE EXCEPTION 'RDR-231 backfill: % live chroma:// note row(s) have a live nx-note:// twin; merge each pair (POST /v1/catalog/merge) and restart', n;
     END IF;
   END $$;
   ```

   UPDATEs, for each of the three tables:
   `UPDATE nexus.<table> SET source_uri = 'nx-note://' || substr(source_uri, 10) WHERE <note predicate on source_uri>;`.
   Live and tombstoned rows are both rewritten (a later restore must find its
   note). The rewrite is injective and no new value can equal an old one, so a
   single UPDATE cannot trip catalog-016 once the guard has passed. Its comment
   carries the `DATA EFFECT:` line `tests/test_changelog_data_effect_lint.py`
   requires. Rollback is empty: an older engine binary never runs Liquibase
   rollback, so a reverse rewrite would protect no real downgrade (Sam,
   2026-10-24: no rollback; see Decisions). The same commit seeds and effect-asserts it in
   `SchemaUpgradeRehearsalIntegrationTest`'s data leg (row DML on FORCE-RLS
   tables), and raises `migration_ddl_per_worker` in `tests/ddl_ceiling.toml` by
   its DDL count. The NO FORCE toggle takes an ACCESS EXCLUSIVE lock on the three
   tables for the transaction; the pre-tag census records each tenant's row counts
   so the lock time can be estimated before the deploy.

6. **Client.** Every stored and transient mint calls `note_uri`; nothing else
   spells a scheme. Readers and stat handlers register under `nx-note`; the
   `chroma` reader, stat handler and `_KNOWN_URI_SCHEMES` entry go,
   `_KNOWN_URI_SCHEMES` gains `nx-note` (with the lock-test update
   `catalog/CLAUDE.md` § Adding a new source-URI scheme requires), and
   `_LINK_SAFE_SCHEMES` excludes `nx-note` by name. `nx doctor --check-references`
   reports a residue row (step 5 leaves it) as an unknown scheme, which it is. Classifiers: `reconcile_stale`
   and `recovery_bundle` call `is_note_uri`. For `reconcile_stale` that is a
   behaviour change: a `chroma://` row under a file-routed collection, today
   counted as store-put-origin by its bare prefix, is no longer counted (the
   function returns `None` for it, as `recovery_bundle` already does). `backfill.py` and `recovery_bundle`'s residue counter (`:183-186`) keep
   matching `chroma://` residue under file-routed collections by
   `physical_collection`; they are the two client sites that still name the legacy
   scheme, because finding and rewriting that residue is their job.
   `recovery_bundle._resolve_link_endpoint` (`:496-502`), whose fallback re-derives
   a mode-changed identity, parses `nx-note://` after `canonical_note_uri`. The exact-compare sites (`by_source_uri`, the store hook's guard, the
   exporter and recovery-bundle imports, `aspect_sql`) compare `nx-note://` with
   `nx-note://` and need no change beyond the mint; the two imports pass every
   incoming URI through `canonical_note_uri` first, because old bundles and exports
   carry `chroma://` indefinitely. The client mints only `nx-note://`, and refuses
   a note write when the serving engine reports a version below
   `NOTE_URI_MIN_ENGINE_VERSION` (`engine_version.py`, set to the tag; the same
   at-start probe as `CHANNEL_ANNOUNCE_MIN_ENGINE_VERSION`). A local engine lags
   the pinned version until `nx daemon restart-stale` runs (§ Technical
   Environment); without the refusal a new client would register `nx-note://`
   twins beside un-backfilled `chroma://` rows, and the backfill guard would then
   halt that engine's boot. The refusal names the remedy (`nx daemon
   restart-stale`). A probe that resolves no version does not refuse; it logs
   (inferred, not read: whether any engine build reports no version).

7. **Input-accept forever.** The engine never refuses `chroma://` on input; it
   canonicalizes it. No response carries it for a note.

```text
// Illustrative: engine helper
static String canonical(String uri)  // note "chroma://c/r" -> "nx-note://c/r"; anything else unchanged
static Field<String> moveTo(Field<String> f, String oldColl, String newColl)  // per-scheme prefix rewrite
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Client note-URI rule | `reconcile_stale._STORE_PUT_URI_PREFIX`, literals in Gap 2 | Replace: one definition in `aspect_readers`, every site imports it |
| Engine `NoteUri` | rename prefix literals `:9782-9783` | New helper; the rename, re-home and re-embed move call it |
| Input canonicalization | `deriveSourceUri` (register paths) | Extend: canonicalize inside it; other routes call `NoteUri.canonical` directly |
| Move collision refusal | `SourceUriCollision` (rename) | Reuse for re-home and the re-embed move |
| Engine floor | `REQUIRED_ENGINE_VERSION` | Move to the tag in the paired client release |
| Backfill | `aspects-004-1`, `fk-004-0-reconcile-precount` shapes | New changeset |
| Route coverage pin | `TombstoneFilterGateTest` exempt-list pattern | New gate test, same pattern, widened to string-key access and the `vectors` package |

### Decision Rationale

Sam ruled (2026-10-26) that `chroma://` leaves storage and new code, and that old
clients upgrade rather than being served the old spelling. Input canonicalization
is the one compatibility piece that stays, because it is what keeps old clients and
old artifacts from minting duplicates, and it costs one helper call per input
route. Dropping legacy rendering removes the capability header, a rendering choice
in every note-carrying response (the `vectors` routes included), and the hardest
part of the route gate.

## Alternatives Considered

### Alternative 1: Keep chroma:// and document it as opaque identity

**Description**: No code change; document the scheme as historical.
**Pros**: Zero risk, no DATA EFFECT.
**Cons**: The misleading name stays on every note.
**Reason for rejection**: Sam ruled for the change (2026-10-23).

### Alternative 2: Client-only switch with dual lookup

**Description**: Mint `nx-note://`, look up both spellings on re-put.
**Cons**: Old clients still mint `chroma://` against rows they can no longer match; the engine rename stops rewriting new-scheme notes.
**Reason for rejection**: without engine canonicalization, every old client and old artifact mints a duplicate after the backfill (Gap 3).

### Alternative 3: Canonicalize, and echo the caller's spelling only on lookup routes

**Description**: `/list` and `/resolve` render matches in the query's spelling; every other response returns `nx-note://`.
**Pros**: Smaller engine change; fixes the re-put duplicate.
**Cons**: Old clients still misclassify notes: recovery-bundle exports omit them, `nx doctor --check-references` reports them unreadable.
**Reason for rejection**: keeps the old spelling on the wire for one route family only, which is neither the old behaviour nor the new; once old clients upgrade (Decisions 5) it buys nothing.

### Alternative 4: Engine-enforced minimum client version

**Description**: After the tag, the engine refuses catalog writes from clients below a floor.
**Cons**: No refusal mechanism exists (`X-Nexus-Client-Version` is logged, not enforced); strands every cloud client until it upgrades.
**Reason for rejection**: a hard outage to fix a cosmetic name. The chosen design also relies on upgrade, but an old client degrades (it stops recognising notes) instead of failing.

### Alternative 5: Legacy rendering to un-upgraded clients

**Description**: The 2026-10-24 revision of this RDR: the engine renders `chroma://` to any request without an `X-Nexus-Note-Uri: nx-note` header, so old clients behave exactly as today.
**Pros**: No visible change for any old client.
**Cons**: A rendering choice in every note-carrying response, `vectors` routes included; a header the client must tie to its own minting; a gate test that must find every output.
**Reason for rejection**: Sam, 2026-10-26: `chroma://` goes from storage and new code, and old clients upgrade (Decisions 5).

### Briefly Rejected

- **Backfill without engine canonicalization**: an old client's next re-put of a backfilled note mints a duplicate.
- **A Liquibase `sqlCheck` precondition for the collision count**: it runs under FORCE RLS before any toggle and always counts zero.

## Trade-offs

### Consequences

- Positive: the note identity names what it is; one definition per side; no note URI moves without its row; no note carries `chroma://` after the deploy (residue under file-routed collections keeps it until `nx catalog backfill-source-uri` rewrites it to `file://`).
- Negative: a DATA EFFECT changeset on every tenant's catalog; un-upgraded clients stop recognising notes (classifiers, recovery-bundle export, doctor references, aspect SQL) until they upgrade; a re-home or re-embed move of a note whose new URI is taken is refused instead of half-done.

### Risks and Mitigations

- **Risk**: the backfill collides on catalog-016. **Mitigation**: the in-transaction guard aborts before any row moves; the pre-tag census finds pairs first.
- **Risk**: an input route is missed, so an old client or an old artifact mints a duplicate. **Mitigation**: `NoteUriRouteGateTest`, behavioural and static.
- **Risk**: an old cloud client's recovery-bundle export silently omits notes, and its re-puts do not update metadata, until it upgrades. **Mitigation**: the client release notes say so; nothing refuses an old client (Alternative 4 rejected that).
- **Risk**: a new client writes to a local engine that has not restarted onto the tag. **Mitigation**: the `NOTE_URI_MIN_ENGINE_VERSION` write refusal (step 6).
- **Risk**: a rolling cloud deploy serves an old engine instance after the backfill, and an old client's re-put through it writes a lone `chroma://` note row that nothing rewrites. **Mitigation**: the deploy-topology assumption (§ Critical Assumptions): drained deploy, then the post-rollout census.
- **Risk**: a classifier misses the new spelling and treats notes as residue. **Mitigation**: one `is_note_uri`, the fixture corpus shared with the engine and the SQL predicate, and a lint that no `chroma://` or `nx-note://` literal, and no `"chroma"` scheme key, appears outside the definitions, `backfill.py` and `recovery_bundle`'s residue counter.

### Failure Modes

A missed engine input route shows as a duplicate Document after an old client's
re-put or an old artifact's import: two live rows whose URIs differ only in scheme.
A missed client classifier shows as notes reported as migration residue by
`nx catalog reconcile-stale`. A collision halts the engine's migration at boot,
with the count in the error. A move path that misses the rewrite shows as a note
whose URI names a collection it no longer lives in, and as a duplicate on its next
re-put. A refused re-home or collection update shows as a 409 naming the document
that holds the URI; a refused re-embed move shows as that document's skip in the
job's report, with the holder named.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified
- [ ] Engine-tag freeze ended (>= 2026-10-26 13:46Z)

### Minimum Viable Validation

On a substrate engine: seed a `chroma://` note row by SQL in the schema the
previous changeset leaves (the API canonicalizes, so it cannot write one), run the
backfill, then re-put the same note with `chroma://` and with
`nx-note://`. Assert one live Document whose stored `source_uri` is `nx-note://...`,
that both re-puts returned it, that every read returns `nx-note://`, and that the
matching aspect row was rewritten too. Then rename the collection and assert both
the note and a seeded residue row under a `code__` collection follow the rename in
their own spelling.

### Phase 1: Code Implementation

#### Step 1: Engine `NoteUri`, input canonicalization, the three moves of the catalog row with their collision refusals, backfill changeset (engine tag)

Includes the re-home and re-embed rewrites (step 4, closes nexus-ql5m5), the fixture
corpus parity test, `NoteUriRouteGateTest`, the rehearsal seed, the DDL ceiling
bump, and a `docs/wire-contract-pending.md` entry that is not additive: existing
response fields (`source_uri` on catalog, aspect, highlight and `vectors` rows)
change spelling for every caller.

#### Step 2: Client note-URI rule, mints through `note_uri`, readers and classifiers on `nx-note`, `canonical_note_uri` at the two imports, `REQUIRED_ENGINE_VERSION` set to the tag

### Phase 2: Operational Activation

#### Activation Step 1: Census, deploy topology and residue; stage the engine deploy with conexus; push the client tag

Before the tag, run the census read-only per tenant against the cloud database, with
the step 5 note predicate as `<note>`: live pairs (the guard's query), live
`chroma://` note rows, and row counts of the three tables for the lock estimate. Any
pair is merged first. Ask conexus whether the cloud engine deploy is rolling (§
Critical Assumptions). The wire entry is not additive, so the release follows
nexus-1emxn choreography (b): the engine tag is cut, the redeploy is staged with
conexus and confirmed, and it fires at the client-tag push, so the engine and the
client that needs it arrive together. Until a user installs the new client, an old
client loses its view of notes (step 3). The client release branch carries the
data-effect relay attestation for the engine range
(`scripts/list_data_effects.py --record-relay-attestation`), committed on the
release branch so `check_engine_release_floor.py` finds it in the tagged tree.
After the deploy, run the census again for live `chroma://` note rows (expected
zero), and run `nx catalog backfill-source-uri` to rewrite file-routed residue to
`file://`; rows it cannot re-derive stay as residue and are counted.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Backfilled `source_uri` values | N/A | `nx catalog show` (new client) | N/A | census: no live legacy note rows in the three tables | catalog backup before deploy |

### New Dependencies

None.

## Test Plan

- **Scenario**: `NoteUriRouteGateTest`: each listed input route, driven with `chroma://`, stores or matches `nx-note://`; a static sweep finds every `source_uri` reference (jOOQ symbol or string key) in `CatalogRepository`, `AspectRepository` and the `vectors` package inside a listed or exempted method — **Verify**: an unlisted method or a stored `chroma://` note fails the test.
- **Scenario**: old-client re-put after backfill (`chroma://`) — **Verify**: same Document, `created=false`, no duplicate.
- **Scenario**: new-client re-put of a backfilled note — **Verify**: same Document.
- **Scenario**: rename with a note and a `code__` residue row — **Verify**: each follows in its own spelling; the collision pre-check fires for either spelling.
- **Scenario**: re-home a collection holding a note, then re-put the note under the target — **Verify**: one live Document whose URI names the target; its aspect and highlight rows carry the same URI.
- **Scenario**: re-home where the target already holds a live document at a note's rewritten URI — **Verify**: 409 `SourceUriCollision` naming that document; nothing moved in any table, chunks and manifest rows included.
- **Scenario**: re-embed move of a note — **Verify**: the note's URI names the sibling collection, its aspects and highlights match it, and a re-put under the sibling finds it.
- **Scenario**: re-embed move where the sibling already holds a live document at the rewritten URI — **Verify**: that document is skipped with the holder named in the job report; the job's other documents move; the job does not end failed.
- **Scenario**: `/v1/catalog/update` moving a note's `physical_collection` without `source_uri`, then with a taken target URI — **Verify**: URI rewritten to the new collection; then 409 `SourceUriCollision`, nothing changed.
- **Scenario**: a note write from a new client to an engine reporting a version below `NOTE_URI_MIN_ENGINE_VERSION` — **Verify**: refused, naming `nx daemon restart-stale`; nothing registered.
- **Scenario**: `recovery_bundle._resolve_link_endpoint` on an old bundle's `chroma://` link after a mode change — **Verify**: re-derives the target through the `nx-note://` form.
- **Scenario**: aspect rename and highlight rename — **Verify**: `source_uri` unchanged (the catalog row did not move).
- **Scenario**: backfill with a planted live twin — **Verify**: the migration aborts, no row changed, FORCE still set on all three tables.
- **Scenario**: backfill effect in `SchemaUpgradeRehearsalIntegrationTest` — **Verify**: seeded note rows in all three tables rewritten, residue untouched, `columnsHolding("chroma://")` finds only the residue.
- **Scenario**: fixture corpus through `is_note_uri(canonical_note_uri(u))`, `NoteUri.isNote` and the SQL predicate (either spelling) — **Verify**: all three agree on every row.
- **Scenario**: `reconcile_stale` on a `chroma://code__...` residue row — **Verify**: not counted as store-put-origin (the stated behaviour change).
- **Scenario**: `note_uri` with and without `quote_rest` — **Verify**: today's stored and transient strings, scheme apart.
- **Scenario**: import of an old recovery bundle and an old export carrying `chroma://` — **Verify**: converges onto the backfilled Document.
- **Scenario**: `_LINK_SAFE_SCHEMES` — **Verify**: excludes `nx-note`.

## Validation

### Testing Strategy

The Minimum Viable Validation, the route gate and the move scenarios run on the
test engine substrate; the backfill runs in the schema-upgrade rehearsal; the
classifier, reader, mint and import cases are unit tests.

### Performance Expectations

A prefix test per URI on input. The backfill holds an ACCESS EXCLUSIVE lock on three tables for one transaction; the census gives the row counts to estimate it.

## Finalization Gate

### Contradiction Check

Read end to end after the round 1 fixes (2026-10-26). The Approach, Technical Design
steps 1-7, Decisions 4 and 5 and the Test Plan agree on three rules: store and show
only `nx-note://`, canonicalize `chroma://` on input forever, and rewrite the URI on
every move of a note's catalog row, refusing a taken one. The round 1 critique found
that a 2026-10-26 bead comment recorded a "no backfill" decision this RDR did not
mention; Sam withdrew it (Decisions 4), and both beads say so. The "no rollback"
ruling (Decisions 1) matches step 5's empty rollback. Line numbers drift; an
implementer re-reads each site before editing.

### Assumption Verification

Fifteen research records (`nexus_rdr/231-research-1` to `-15`). Verified by source
search: 1-6 and 10-13 (13 replaces 7's residue claim, which was a comment, with the
three predicates the code uses). Inferred, not run: 14 (an old client's behaviour
against the new engine). Assumed, needing facts outside this tree and checked before
the engine tag: 8 and 9 (production data, acknowledged by Sam at gate) and 15 (cloud
deploy topology). If the census finds a pair, Decisions 2 applies (merge, then
deploy).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Input routes in Technical Design step 2 | nexus engine | Source Search; pinned by `NoteUriRouteGateTest` |
| `completeMove`, `rehomeCollection`, `renameCollectionTxn` | nexus engine | Source Search 2026-10-26 (`nexus_rdr/231-research-11`, `-12`) |

### Scope Verification

In scope: Gaps 1 to 4, each with a Technical Design step (Gaps 1 and 2: steps 1 and
6; Gap 3: steps 2, 3 and 7; Gap 4: step 4). Added beyond the originating beads: the
re-embed move (`completeMove`), found by the round 1 critique, because leaving one
move of the catalog row unrewritten would keep Gap 4 open. Examined and left
unchanged: the aspect and highlight renames, which do not move the catalog row
(Gap 4). Out of scope: `updateDocumentCollection(s)` (no caller), applied changelogs
that mention `chroma://` in comments (never edited), `engine_version.py` release
history, and `backfill.py`'s residue rewrite, which keeps matching `chroma://`.

### Cross-Cutting Concerns

- **Versioning**: the engine tag carries the changeset and the routes; the client release pins it through `REQUIRED_ENGINE_VERSION`.
- **Deployment model**: nexus-1emxn choreography (b): the deploy is staged with conexus and fires at the client-tag push; the wire entry is not additive.
- **Incremental adoption**: old clients keep one Document per note but stop recognising notes until they upgrade (step 3).
- Others: N/A.

### Proportionality

The defect is a misleading name, and the fix is a tenant-wide data rewrite, input
canonicalization on every input route, a URI rewrite with a collision refusal on
three move paths, and a route gate test. Sam's 2026-10-26 ruling removed the
largest piece of the earlier design, legacy rendering. Two parts are worth doing for
their own sake: Gap 4's moves leave stale identities today (a re-homed or
re-embedded note duplicates on its next re-put), and the client's three classifier
predicates disagree today. Priority stays low.

## Decisions

Sam, 2026-10-24, on the draft's three open questions:

1. **No rollback.** Downgrading the engine below the tag is unsupported. An older engine over backfilled rows has no canonicalization, so its clients would mint duplicates; the changeset ships no reverse rewrite, because no procedure runs `liquibase rollback` before a downgrade.
2. **Halt on a collision.** The backfill aborts and names the count; an operator merges each pair with `POST /v1/catalog/merge` and restarts. No automatic merge inside the changeset. The pre-tag census is expected to find no pairs.
3. **Fold nexus-ql5m5 in.** The re-home and aspect-rename gaps are Gap 4, in the same engine tag. (Round 1 revision: the re-home and the re-embed move rewrite in Technical Design step 4; the aspect and highlight renames are examined there and left unchanged, because they do not move the catalog row.)

Sam, 2026-10-26, at the round 1 gate (`nexus_rdr/231-research-10`):

4. **The backfill stands.** A comment on nexus-t7fbb and nexus-ql5m5 dated 2026-10-26 recorded a 2026-10-25 "no backfill" decision; Sam withdrew it as a mistake, and both beads now say so.
5. **`chroma://` goes from storage and new code.** Every note is backfilled, new clients never mint or show it, and the engine keeps canonicalizing it on input so old clients and old artifacts do not duplicate. No legacy rendering and no capability header: old clients upgrade, through the paired release and `REQUIRED_ENGINE_VERSION`. This replaces the 2026-10-24 revision's legacy rendering (Alternative 5).

## References

- nexus-t7fbb and its 2026-10-23 analysis comment; T2 `nexus/critique-48f3l-and-rdr231-draft-2026-10-23` (B)
- `src/nexus/aspect_readers.py`, `src/nexus/catalog/store_hook.py`, `src/nexus/catalog/http_catalog_client.py`, `src/nexus/catalog/recovery_bundle.py`, `src/nexus/commands/catalog_cmds/reconcile_stale.py`, `src/nexus/commands/catalog_cmds/backfill.py`, `src/nexus/exporter.py`, `src/nexus/db/client_identity.py`, `src/nexus/engine_version.py`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java`, `AspectRepository.java`, `http/CatalogHandler.java`, `http/AspectHandler.java`, `service/src/test/java/dev/nexus/service/CatalogRenameIdentityCascadeTest.java`
- `catalog-016-source-uri-unique.xml`, `aspects-001-baseline.xml`, `aspects-004-doc-id-backfill.xml`, `fk-004-chunks-collection-registry.xml`

## Revision History
- 2026-10-23: First draft (nexus-t7fbb), after Sam's ruling to make the change as one RDR-scoped engine-plus-client change.

- 2026-10-23: Revised against the T2 critique (part B). Old clients: chose legacy rendering (the engine shows `chroma://` to any request without `X-Nexus-Note-Uri: nx-note`) and stated each old-client path; recorded that register's `created=false` closes the rollback hazard once register is canonicalized. Replaced Critical Assumption 1: three tables hold note URIs, and the backfill and rename cover all three. Completed the Gap 2 inventory and dropped the false `integrity.py` constant. Added the engine route table and `NoteUriRouteGateTest`. Scoped the note predicate (and so the backfill, canonicalizer and rename) away from file-routed residue. Gave the guard and UPDATE SQL, and made the guard an in-transaction `DO` block, because a `sqlCheck` precondition counts zero under FORCE RLS. Added the rehearsal seed, DDL ceiling and relay attestation. Added `NOTE_URI_MIN_ENGINE_VERSION` so a new client sends `nx-note://` only to an engine that canonicalizes. Added Alternatives 3 and 4 and three open questions.

- 2026-10-24: Sam's rulings on the three open questions, recorded in § Decisions: no rollback, halt on a collision, fold nexus-ql5m5 in. Added Gap 4 (re-home, aspect rename and highlight rename move note rows without rewriting their URI; the highlight rename was not in the bead), Technical Design step 4a with the `NoteUri.moveTo` helper shared with the rename, re-home's left-behind rule for a taken URI, three test scenarios, and a risk and a failure mode.

- 2026-10-26: Gate round 1 — BLOCKED (3 Critical, 5 Significant, 3 ship-blocker(s)); commit `62d294522`; critique `nexus_rdr/231-gate-critique-2026-10-26-r1`.

- 2026-10-26: Round 1 fixes and Sam's rulings (Decisions 4 and 5): backfill confirmed; legacy rendering and the capability header dropped, old clients upgrade; re-home refuses a taken URI whole, the re-embed move rewrites and refuses per document, the aspect and highlight renames stay unchanged; Gap 2 split stored and transient mints and added `aspect_sql` (that hunk was left out of a85e22c93 and landed with the fix-check fixes); the three classifier predicates stated and unified; the route gate widened; deploy-topology assumption added.

- 2026-10-26: Fix-check fixes (`nexus_rdr/231-fix-check-a85e22c93`): activation by nexus-1emxn choreography (b); `NOTE_URI_MIN_ENGINE_VERSION` refuses a note write to a lagging engine; the collection update rewrites a moved note's URI; the re-embed collision becomes a per-document skip; `recovery_bundle` link parse and residue counter stated; residue rewritten by `backfill-source-uri` after the deploy; the corpus agreement defined; the route-gate exempt list completed; citations refreshed.
