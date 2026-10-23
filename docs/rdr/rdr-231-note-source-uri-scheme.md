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
on develop, 2026-10-23):

| Role | Site |
| --- | --- |
| Mints a stored URI | `aspect_readers.uri_for` `:206` (the store hook `:883` and exporter `:652` call it); `aspect_extractor.py` `:1301`, `:1414`, `:1577` (aspect rows' `source_uri`); `commands/enrich.py:1525` (dry-run read) |
| Mints a transient read URI | `aspect_readers._read_https_uri` `:789` (T3-first read of an https source) |
| Classifies by prefix | `reconcile_stale._STORE_PUT_URI_PREFIX` `:230`, used `:379`; `recovery_bundle.classify_store_put_origin` `:94` and its residue counter `:185`; `backfill.py` `:84`, `:102`, `:111` (residue census and rewrite) |
| Parses the URI | `recovery_bundle._resolve_link_endpoint` `:499-501` (collection/title re-derivation) |
| Dispatches on scheme name | `aspect_readers._READERS` `:1107`, `_STAT_HANDLERS` `:1939`; result labels `"scheme": "chroma"` `:521`, `"served_from": "chroma"` `:797`; `catalog/types._KNOWN_URI_SCHEMES` `:80`; `doc/catalog_links._LINK_SAFE_SCHEMES` `:285` (derived by subtracting from `_KNOWN_URI_SCHEMES`, so a scheme added to the known set becomes link-safe unless it is excluded by name) |
| Compares URIs exactly | `http_catalog_client.by_source_uri` `:2184`; store hook chash-dedup guard `:930`; exporter import `:745`, `:748`; `recovery_bundle._resolve_link_endpoint` `:498` |
| User-facing text | `commands/catalog.py:606` (register help), `commands/enrich.py:2094` (help) |
| Comments and docs only | `integrity.py:623` (a docstring; there is no constant there), `doctor_references.py:14`, `aspect_worker.py:585`, `db/t3.py:771`, `catalog/AGENTS.md:9`, `db/AGENTS.md:7` |
| Engine | rename rewrite `CatalogRepository.java:9713-9734` (collision pre-check) and `:9915-9929` (three UPDATEs); `deriveSourceUri` javadoc `:933`. Two applied changelogs mention it in comments (`hygiene-001-not-null.xml:93`, `aspects-004-doc-id-backfill.xml:30`); applied changesets are never edited. |

`engine_version.py:577` is release history and stays as written. A change that
misses one site leaves notes that a classifier, reader or rename treats as
something else. The fix delivers one definition per side, and a lint that no other
spelling survives.

#### Gap 3: Changing the spelling duplicates notes for clients that do not have the change

A re-put finds its Document by `source_uri`. Once stored rows say `nx-note://`, any
client that still sends `chroma://` misses them, and the engine mints a second
Document. Engine-side canonicalization of incoming URIs is necessary but not
sufficient: an un-upgraded client also compares URIs locally. Its `by_source_uri`
drops any row whose `source_uri` is not byte-equal to the one it asked for
(`http_catalog_client.py:2184`), so an `nx-note://` row returned for a `chroma://`
query is discarded, and the store hook goes on to register. Its classifiers
(`reconcile_stale.py:379`, `recovery_bundle.py:94`) stop recognising notes, so an
old client's recovery-bundle export silently leaves them out. It has no reader or
stat handler for `nx-note` (`aspect_readers.py:1107`, `:1939`), so
`nx doctor --check-references` reports every sampled note as unreadable. Old
recovery bundles and exports carry `chroma://` indefinitely. The reverse also
holds: a new client that sends `nx-note://` to an engine without canonicalization
mints a duplicate beside every un-backfilled `chroma://` row, and that duplicate
then makes the backfill collide. The fix must keep one Document per note for old
clients, new clients and old artifacts, against old and new engines.

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
The 2026-10-23 analysis on nexus-t7fbb traced the sites; the critique in T2
`nexus/critique-48f3l-and-rdr231-draft-2026-10-23` (part B) found the gaps this
revision closes.

### Technical Environment

Client: `src/nexus/aspect_readers.py`, `catalog/store_hook.py`,
`catalog/http_catalog_client.py`, `catalog/recovery_bundle.py`, `exporter.py`, the
catalog commands in Gap 2, `db/client_identity.py`, `engine_version.py`. Engine:
`CatalogRepository`, `AspectRepository`, `CatalogHandler`, `AspectHandler`,
Liquibase changelogs. The live-URI partial unique index
`ux_catalog_documents_live_source_uri` (`catalog-016`: unique `(tenant_id,
source_uri)` over live rows with a non-empty URI) constrains any rewrite.

Mixed versions arise in two places. Locally, the engine is the client's pinned
dependency (`engine_version.REQUIRED_ENGINE_VERSION`, `engine_version.py:684`;
convergence installs it), so an old client meets a new engine only briefly, and a
new client meets an old engine only until `nx daemon restart-stale` runs. Against
the managed cloud engine, clients of any version at or above the managed floor
talk to whatever engine is deployed, indefinitely.

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

- **Verified**: three tables hold a note URI: `catalog_documents.source_uri`, `document_aspects.source_uri` and `document_highlights.source_uri`. The rename rewrites all three (`CatalogRepository.java:9915-9929`); `CatalogRenameIdentityCascadeTest` enumerates holders by sweeping every `nexus.*` text and json column for the URI (`columnsHolding`, `:356-386`), not from a hand-kept list.
- **Verified**: the engine's register paths report `created=false` when the idempotency lookup matches (`RegisterOutcome`, `CatalogRepository.java:1074`, `:1126`), and the store hook threads it (`store_hook.py:1080`). The rollback in `rollback_minted_catalog_entry` (`store_hook.py:1119`) therefore deletes a live note only if the engine's lookup misses, that is, only if register is not canonicalized.
- **Verified**: an old client's `by_source_uri` discards any row not byte-equal to its query (`http_catalog_client.py:2184`); its chash-dedup guard does the same (`store_hook.py:930`).
- **Verified**: the catalog client and the T2 stores send `X-Nexus-Client-Version` through `client_identity_headers()` (`db/client_identity.py:50`); hooks and probes do not.
- **Verified**: `_normalize_source_uri` (`catalog/types.py:93`) has no production caller; `_KNOWN_URI_SCHEMES` matters at runtime only through `_LINK_SAFE_SCHEMES`.
- **Verified**: under FORCE ROW LEVEL SECURITY, Liquibase's role (`nexus_admin`, NOBYPASSRLS, no tenant GUC at migration time) reads and writes zero rows of a tenant table unless the changeset toggles NO FORCE first (`aspects-004-doc-id-backfill.xml` header; `fk-004-chunks-collection-registry.xml:175`). A Liquibase `sqlCheck` precondition on row counts would therefore always see zero.
- **Documented**: `chroma://` under a file-routed prefix is residue, not a note (`recovery_bundle.py:28-40`, `reconcile_stale.py:220-229`).

### Critical Assumptions

- [ ] The three tables in Key Discoveries are the only places a note URI is stored. — **Status**: Verified in source (rename cascade and its information_schema sweep); not yet checked against production data — **Method**: run the `columnsHolding` sweep for `chroma://` against a restored copy of the cloud database before the gate, and assert in the backfill test that the sweep finds no note URI after the changeset
- [ ] No tenant holds a live legacy note row and a live `nx-note://` row for the same note, so the backfill cannot collide on catalog-016. — **Status**: Unverified on production; true by construction for any database no new client has written to — **Method**: the census query in Technical Design step 5, run read-only per tenant against the cloud database before the engine tag, and the same count as the changeset's in-transaction guard
- [ ] Every engine route that writes, looks up or returns a note URI is in the route table of Technical Design step 2. — **Status**: Verified by source search (this revision) — **Method**: `NoteUriRouteGateTest` (Test Plan) fails when a `SOURCE_URI` reference appears in a repository method that the table neither lists nor exempts

## Proposed Solution

### Approach

Store only the canonical spelling; accept both on input; show each client the
spelling it understands. The engine canonicalizes every incoming note URI, renders
note URIs back as `chroma://` to any request that does not declare it understands
`nx-note://`, rewrites both spellings on rename, and backfills existing rows. The
client declares the new spelling, accepts both everywhere, and mints `nx-note://`
only to an engine known to canonicalize.

### Technical Design

1. **One note-URI rule per side.** Client: in `aspect_readers` beside
   `FILE_ROUTED_PREFIXES`, `NOTE_URI_SCHEME = "nx-note://"`,
   `LEGACY_NOTE_URI_SCHEME = "chroma://"`, `is_note_uri(uri)` (either spelling,
   collection segment not file-routed), `canonical_note_uri(uri)` and
   `legacy_note_uri(uri)`. Engine: a `NoteUri` helper with `canonical` and `legacy`
   and its own copy of the three prefixes, pinned equal to the Python tuple by a
   parity test. Both functions return every non-note URI unchanged, so residue is
   never touched by any step below.

2. **Engine input canonicalization.** Each route applies `NoteUri.canonical` to the
   caller's URI before it reads or writes:

   | Route | Repository entry point |
   | --- | --- |
   | `POST /v1/catalog/register` | `upsertDocument` `:801` (before `guardDocumentIdentity` `:811`) |
   | `POST /v1/catalog/doc/register` | `registerDocumentWithOutcome` via `deriveSourceUri` `:890` (so the idempotency lookup `:1120`, insert and race-loser lookup `:1205` all see it) |
   | `POST /v1/catalog/doc/register_many` | `registerDocumentManyWithOutcome` via `deriveSourceUri` `:1310` (batch idempotency fetch `:1318`, insert, race patch) |
   | `POST /v1/catalog/update`, `/update_many` | `buildUpdateDocumentQuery` `:2067` when `source_uri` is in the field map |
   | `POST /v1/catalog/import/document` | `importDocumentsBatch` `:11353`, `doImportDocument` `:11416` (before their identity guards) |
   | `GET /v1/catalog/list?source_uri=` | handler `:522-542` before `documentsBySourceUri` `:3199` |
   | `GET /v1/catalog/resolve?source_uri=` | handler `:895-910` before `documentsBySourceUri` |
   | `POST /v1/aspects/upsert`, `/import` | `upsertAspect` `:190`, `importAspectsBatch` `:518`, `doImportAspect` `:615` |
   | `POST /v1/aspects/highlights/upsert`, `/highlights/import` | `upsertHighlight` `:689`, `importHighlightsBatch` `:826`, `doImportHighlight` `:886` |
   | `GET /v1/aspects/highlights/get_by_source_uri` | `getHighlightBySourceUri` `:760` |
   | `POST /v1/aspects/operator-query` | the `source_uris` list before `filterBySourceUris`, `groupByField`, `confidenceAggregate` |

   Exempt, with the reason recorded in the gate test: `POST /v1/catalog/merge`
   (`mergeDocuments` `:3747` moves a stored URI; no caller URI enters) and the
   rename (step 4).

3. **Engine output: legacy rendering by default.** A request carrying
   `X-Nexus-Note-Uri: nx-note` receives stored URIs. Any other request receives
   `NoteUri.legacy` of every note URI, applied where rows become responses:
   `docRowFromRecord` (`:11919`, every document row), `recordToMap` and
   `highlightToMap` (`AspectRepository` `:1987`, `:2036`), and the operator-query
   outputs (`matched_uris`, `uri_groups[].source_uri`, `AspectHandler.java:420-440`).
   An un-upgraded client therefore sees exactly the data it sees today:

   | Old-client path | Behaviour against the new engine |
   | --- | --- |
   | Re-put (`store_hook` `:988`) | Lookup canonicalized, row rendered `chroma://`, exact match holds: the existing note is updated, no duplicate. |
   | Chash-dedup guard (`store_hook` `:930`) | Compares `chroma://` with `chroma://`, as today. |
   | Concurrent first put reaching register (`:1080`) | Canonicalized idempotency lookup returns `created=false`; the rollback never targets a live note. |
   | `nx catalog show/list`, exports | Print `chroma://`, as today. |
   | Aspect extraction | Mints `chroma://` for its own T3 read; the upsert is canonicalized on store and rendered `chroma://` on read. |
   | reconcile-stale, recovery-bundle export, backfill-source-uri | Classify on `chroma://`, as today; notes stay in bundles. |
   | `nx doctor --check-references` | Has a stat handler for `chroma`, as today. |
   | Import of an old bundle or export (`exporter.py:745`, `recovery_bundle.py:498`) | `by_source_uri` matches through canonicalization plus legacy rendering. |

   The new client sends the header from `client_identity_headers()`, so every
   shared client class carries it with `X-Nexus-Client-Version`. Requests outside
   those classes (hooks, probes, `curl`) get the legacy spelling, the conservative
   default.

4. **Rename rewrite, both spellings.** `renameCollectionTxn` runs its collision
   pre-check (`:9713-9734`) and its three UPDATEs (`:9915-9929`) once per scheme,
   `chroma://X/ -> chroma://Y/` and `nx-note://X/ -> nx-note://Y/`, so residue keeps
   its spelling and notes keep theirs.

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
   rollback, so a reverse rewrite would protect no real downgrade (see Open
   Questions). The same commit seeds and effect-asserts it in
   `SchemaUpgradeRehearsalIntegrationTest`'s data leg (row DML on FORCE-RLS
   tables), and raises `migration_ddl_per_worker` in `tests/ddl_ceiling.toml` by
   its DDL count.

6. **Client.** Readers and stat handlers register under both scheme names;
   `_KNOWN_URI_SCHEMES` gains `nx-note`, and `_LINK_SAFE_SCHEMES` excludes it by
   name. `by_source_uri` and the store hook's guard compare `canonical_note_uri`
   of both sides. Every classifier in Gap 2 calls `is_note_uri`; `backfill.py`
   keeps matching only legacy residue. Every minting site calls one
   `mint_note_uri(collection, rest)`, which returns `nx-note://` only when the
   serving engine's `/version` is at least `NOTE_URI_MIN_ENGINE_VERSION`
   (`engine_version.py`, the same at-start probe pattern as
   `CHANNEL_ANNOUNCE_MIN_ENGINE_VERSION` `:720`), and `chroma://` otherwise,
   including when the probe cannot resolve a version. `chroma://` is safe against
   every engine: an old one stores what it always stored, a new one canonicalizes
   it. The same release moves `REQUIRED_ENGINE_VERSION` to the tag, which
   converges local engines and is the managed floor.

7. **Read-accept forever.** Neither side ever refuses `chroma://`.

```text
// Illustrative: engine helper
static String canonical(String uri)  // note "chroma://c/r" -> "nx-note://c/r"; anything else unchanged
static String legacy(String uri)     // note "nx-note://c/r" -> "chroma://c/r"; anything else unchanged
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Client note-URI rule | `reconcile_stale._STORE_PUT_URI_PREFIX`, literals in Gap 2 | Replace: one definition in `aspect_readers`, every site imports it |
| Engine `NoteUri` | rename prefix literals `:9717-9718` | New helper; the rename calls it |
| Input canonicalization | `deriveSourceUri` (register paths) | Extend: canonicalize inside it; other routes call `NoteUri.canonical` directly |
| Legacy rendering | `docRowFromRecord`, `recordToMap`, `highlightToMap` | Extend: one flag from the request header |
| Capability header | `client_identity_headers()` | Extend: second header |
| Engine floor | `engine_version.py` feature floors | Extend: `NOTE_URI_MIN_ENGINE_VERSION` |
| Backfill | `aspects-004-1`, `fk-004-0-reconcile-precount` shapes | New changeset |
| Route coverage pin | `TombstoneFilterGateTest` exempt-list pattern | New gate test, same pattern |

### Decision Rationale

Legacy rendering is the only option under which an un-upgraded client behaves
exactly as it does today on every path, including the local comparisons and
classifiers that engine-side canonicalization cannot reach. Its cost is small
because document rows have one renderer. The header, not a parsed
`X-Nexus-Client-Version`, decides the rendering: development builds report
`unknown`, and a capability flag cannot drift from the code that implements it.

## Alternatives Considered

### Alternative 1: Keep chroma:// and document it as opaque identity

**Description**: No code change; document the scheme as historical.
**Pros**: Zero risk, no DATA EFFECT.
**Cons**: The misleading name stays on every note.
**Reason for rejection**: Sam ruled for the change (2026-10-23).

### Alternative 2: Client-only switch with dual lookup

**Description**: Mint `nx-note://`, look up both spellings on re-put.
**Cons**: Old clients still mint `chroma://` against rows they can no longer match; the engine rename stops rewriting new-scheme notes.
**Reason for rejection**: Gap 3 stays open for every un-upgraded install.

### Alternative 3: Canonicalize, and echo the caller's spelling only on lookup routes

**Description**: `/list` and `/resolve` render matches in the query's spelling; every other response returns `nx-note://`.
**Pros**: Smaller engine change; fixes the re-put duplicate.
**Cons**: Old clients still misclassify notes: recovery-bundle exports omit them, `nx doctor --check-references` reports them unreadable.
**Reason for rejection**: leaves silent data loss in a recovery artifact.

### Alternative 4: Engine-enforced minimum client version

**Description**: After the tag, the engine refuses catalog writes from clients below a floor.
**Cons**: No refusal mechanism exists (`X-Nexus-Client-Version` is logged, not enforced); strands every cloud client until it upgrades.
**Reason for rejection**: a hard outage to fix a cosmetic name.

### Briefly Rejected

- **Backfill without engine canonicalization**: an old client's next re-put of a backfilled note mints a duplicate.
- **A Liquibase `sqlCheck` precondition for the collision count**: it runs under FORCE RLS before any toggle and always counts zero.

## Trade-offs

### Consequences

- Positive: the note identity names what it is; one definition per side; old clients and artifacts keep working unchanged.
- Negative: a DATA EFFECT changeset on every tenant's catalog; every note-carrying response passes through a spelling choice; `chroma://` stays in the code as the legacy spelling for as long as old clients exist.

### Risks and Mitigations

- **Risk**: the backfill collides on catalog-016. **Mitigation**: the in-transaction guard aborts before any row moves; the pre-tag census finds pairs first; `mint_note_uri` keeps new clients from creating twins against old engines.
- **Risk**: a route is missed, so an old client mints a duplicate or sees `nx-note://`. **Mitigation**: `NoteUriRouteGateTest`.
- **Risk**: a classifier misses the new spelling and treats notes as residue. **Mitigation**: one `is_note_uri`, and a lint that no `chroma://` or `nx-note://` literal, and no `"chroma"` scheme key, appears outside the definitions.

### Failure Modes

A missed engine input route shows as a duplicate Document after an old client's
re-put: two live rows whose URIs differ only in scheme. A missed output route shows
as `nx-note://` in an old client's output, and in the worst case as notes missing
from its recovery bundle. A missed client classifier shows as notes reported as
migration residue by `nx catalog reconcile-stale`. A collision halts the engine's
migration at boot, with the count in the error.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified
- [ ] Engine-tag freeze ended (>= 2026-10-26 13:46Z)

### Minimum Viable Validation

On a substrate engine with the change: register a note with `chroma://` (as an old
client does), run the backfill, then re-put the same note once without the header
(old client) and once with it (new client). Assert one live Document whose stored
`source_uri` is `nx-note://...`, that the header-less read shows `chroma://` and the
header read shows `nx-note://`, and that a matching aspect row was rewritten too.
Then rename the collection and assert both the note and a seeded residue row under
a `code__` collection follow the rename in their own spelling.

### Phase 1: Code Implementation

#### Step 1: Engine `NoteUri`, input canonicalization, legacy rendering, rename, backfill changeset (engine tag)

Includes the parity test, `NoteUriRouteGateTest`, the rehearsal seed, the DDL
ceiling bump, and a `docs/wire-contract-pending.md` entry marked additive (a new
request header; responses change only for callers that send it).

#### Step 2: Client note-URI rule, readers and classifiers accept both, canonical comparison, header

#### Step 3: Client minting through `mint_note_uri`, `NOTE_URI_MIN_ENGINE_VERSION` and `REQUIRED_ENGINE_VERSION` set to the tag

### Phase 2: Operational Activation

#### Activation Step 1: Census, deploy the engine tag (backfill runs), then release the client

Before the tag, run the step 5 census read-only per tenant against the cloud
database; any pair is merged first. The engine deploys before the client tag
(nexus-1emxn choreography (a)): the wire change is additive. The client release
branch carries the data-effect relay attestation for the engine range
(`scripts/list_data_effects.py --record-relay-attestation`), committed on the
release branch so `check_engine_release_floor.py` finds it in the tagged tree.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Backfilled `source_uri` values | N/A | `nx catalog show` (new client) | N/A | census: no live legacy note rows in the three tables | catalog backup before deploy |

### New Dependencies

None.

## Test Plan

- **Scenario**: `NoteUriRouteGateTest`: every `SOURCE_URI` reference in `CatalogRepository` and `AspectRepository` lies in a method the route table lists or the exempt list names with a reason; each listed route, driven with `chroma://` and no header, stores `nx-note://` and returns `chroma://` — **Verify**: an unlisted method or a leaked spelling fails the test.
- **Scenario**: old-client re-put after backfill (no header) — **Verify**: same Document, `created=false`, no duplicate.
- **Scenario**: new-client re-put of a backfilled note (header) — **Verify**: same Document.
- **Scenario**: rename with a note and a `code__` residue row — **Verify**: each follows in its own spelling; the collision pre-check fires for either spelling.
- **Scenario**: backfill with a planted live twin — **Verify**: the migration aborts, no row changed, FORCE still set on all three tables.
- **Scenario**: backfill effect in `SchemaUpgradeRehearsalIntegrationTest` — **Verify**: seeded note rows in all three tables rewritten, residue untouched, `columnsHolding("chroma://")` finds only the residue.
- **Scenario**: Java and Python file-routed prefixes — **Verify**: parity test equal.
- **Scenario**: classifiers (reconcile-stale, recovery bundle, doctor references) on both spellings — **Verify**: both classify as notes; residue does not.
- **Scenario**: `mint_note_uri` against an engine below the floor and with an unresolvable version — **Verify**: mints `chroma://`.
- **Scenario**: import of an old recovery bundle carrying `chroma://` — **Verify**: converges onto the backfilled Document.
- **Scenario**: `_LINK_SAFE_SCHEMES` — **Verify**: excludes `nx-note`.

## Validation

### Testing Strategy

The Minimum Viable Validation and the route gate run on the test engine substrate;
the backfill runs in the schema-upgrade rehearsal; the classifier, reader and floor
cases are unit tests against both spellings.

### Performance Expectations

N/A (a prefix test per URI on input and output).

## Finalization Gate

### Contradiction Check

To be completed at gate.

### Assumption Verification

To be completed at gate.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Catalog and aspect routes in Technical Design step 2 | nexus engine | Source Search (this revision); pinned by `NoteUriRouteGateTest` |

### Scope Verification

To be completed at gate.

### Cross-Cutting Concerns

- **Versioning**: engine tag carries the changeset and the routes; the client release pins it.
- **Deployment model**: engine first, additive.
- **Incremental adoption**: old clients see the legacy spelling; new clients mint the new one only to engines that canonicalize.
- Others: N/A.

### Proportionality

To be completed at gate.

## Open Questions for Sam

1. Downgrade below the tag is unsupported: an older engine over backfilled rows has no canonicalization, so its clients mint duplicates. Is that acceptable as stated, or should the changeset ship a reverse-rewrite rollback for an operator-run `liquibase rollback`?
2. On a collision the migration halts and names the count. Halt (proposed), or merge each pair automatically inside the changeset?
3. `rehomeCollection` (`CatalogRepository.java:10083`) moves documents without rewriting note URIs, and `/v1/aspects/rename_collection` (`AspectRepository.renameAspectCollection` `:429`) moves aspect rows without rewriting theirs. Both predate this RDR. In scope here, or a separate bead?

## References

- nexus-t7fbb and its 2026-10-23 analysis comment; T2 `nexus/critique-48f3l-and-rdr231-draft-2026-10-23` (B)
- `src/nexus/aspect_readers.py`, `src/nexus/catalog/store_hook.py`, `src/nexus/catalog/http_catalog_client.py`, `src/nexus/catalog/recovery_bundle.py`, `src/nexus/commands/catalog_cmds/reconcile_stale.py`, `src/nexus/commands/catalog_cmds/backfill.py`, `src/nexus/exporter.py`, `src/nexus/db/client_identity.py`, `src/nexus/engine_version.py`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java`, `AspectRepository.java`, `http/CatalogHandler.java`, `http/AspectHandler.java`, `service/src/test/java/dev/nexus/service/CatalogRenameIdentityCascadeTest.java`
- `catalog-016-source-uri-unique.xml`, `aspects-001-baseline.xml`, `aspects-004-doc-id-backfill.xml`, `fk-004-chunks-collection-registry.xml`

## Revision History
- 2026-10-23: First draft (nexus-t7fbb), after Sam's ruling to make the change as one RDR-scoped engine-plus-client change.

- 2026-10-23: Revised against the T2 critique (part B). Old clients: chose legacy rendering (the engine shows `chroma://` to any request without `X-Nexus-Note-Uri: nx-note`) and stated each old-client path; recorded that register's `created=false` closes the rollback hazard once register is canonicalized. Replaced Critical Assumption 1: three tables hold note URIs, and the backfill and rename cover all three. Completed the Gap 2 inventory and dropped the false `integrity.py` constant. Added the engine route table and `NoteUriRouteGateTest`. Scoped the note predicate (and so the backfill, canonicalizer and rename) away from file-routed residue. Gave the guard and UPDATE SQL, and made the guard an in-transaction `DO` block, because a `sqlCheck` precondition counts zero under FORCE RLS. Added the rehearsal seed, DDL ceiling and relay attestation. Added `NOTE_URI_MIN_ENGINE_VERSION` so a new client sends `nx-note://` only to an engine that canonicalizes. Added Alternatives 3 and 4 and three open questions.
