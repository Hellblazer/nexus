---
title: "The Collection Name Becomes a Label: Retiring What RDR-204 Left Reading the Name"
id: RDR-229
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-10-17
accepted_date:
related_issues: ["nexus-j4gl6", "nexus-zvcou"]
related_rdrs: [RDR-101, RDR-103, RDR-155, RDR-156, RDR-176, RDR-204, RDR-210, RDR-225, RDR-227]
---

# RDR-229: The Collection Name Becomes a Label

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

A collection is a named set of chunks (pieces of indexed text with their
vectors) that share one embedding model. Its name has the shape
`<content_type>__<owner_id>__<embedding_model>__v<n>`, for example
`code__1-1__voyage-code-3__v1`. That shape is called the conformant name
(RDR-103). It puts four facts about the collection into the string that
identifies it. The shape was a ChromaDB constraint: in ChromaDB the name was
the only reliable place to keep collection metadata. ChromaDB was retired at
RDR-155. Collections are now Postgres rows, and one team owns both the client
(`nx`, Python) and the server (the engine, a Java service).

Bead nexus-j4gl6 (Sam, 2026-06-29) asks to stop encoding those facts in the
name, and to plan the work fully: "no need to encode anything in the name...
given we control the horizontal AND the vertical, revisit these constraints."
Its trigger, the production local-to-managed cutover, has passed (Sam's
2026-10-17 grooming).

Most of the bead's first two steps shipped in the meantime, under a different
record. RDR-204 (closed 2026-09-10) made `nexus.catalog_collections` (the
per-tenant collection registry table) carry `content_type`, `owner_id`,
`embedding_model`, `dimension` and `lifecycle_state` under NOT NULL
constraints, made the engine read that row instead of the name, and moved the
client's three main attribute helpers onto the row. RDR-204 also considered
the bead's third step, opaque names for new collections, and Sam rejected it
on 2026-09-09. Sam reopened that step on 2026-10-18 and confirmed it on
2026-11-08 ("go on to Phases 3 and 4"; Decisions, D1). This RDR is
therefore about what RDR-204 left behind, and about the step it rejected.

Sam's 2026-10-18 rulings go further than RDR-204's Phase 4 did. A new
collection is named by an opaque generated id with its attributes shown
beside it (D2), and the collection gets a surrogate key, `collection_id`,
that every collection-scoped table references (D3). The name stops being a
key and becomes a display label.

What is left is not cosmetic. The research census (Research Findings, the
"parse-site census") found code that still reads facts out of the name on
both sides of the wire, including on write paths. The gaps below are the
defects that census shows, independent of what a new collection's name
should look like.

### Enumerated gaps to close

#### Gap 1: The client still takes the embedding model and content type from the name on write, score and taxonomy paths

RDR-204 moved `collection_content_type`, `collection_owner` and
`collection_model` onto the registry row. A second ring of helpers in
`src/nexus/corpus.py` was not moved: `embedding_model_for_collection` and its
alias `index_model_for_collection`, `voyage_model_for_collection`,
`embedding_model_for_collection_calibrated`, `default_projection_threshold`
and `_legacy_content_type_for_collection` still parse the name. They are
called from the paths that stamp `embedding_model` into every chunk's
metadata on write, from the score calibration in `scoring.py`, and from the
taxonomy projection thresholds. The owner segment is worse than stale: for
repo collections the engine deliberately overwrites the row's `owner_id`
with a tumbler-derived value that differs from the name (nexus-6pbwx), so a
reader that takes the owner from the name reads a value the row has
overridden.

#### Gap 2: Lifecycle state is still read from the `quarantine-` name prefix

RDR-204 Gap 4 replaced the `quarantine-` prefix with a `lifecycle_state`
column for corpus resolution. The engine still infers `lifecycle_state` from
the prefix when it registers a row, still guards quarantine operations by the
prefix (`QuarantineOrigin.isQuarantineName`), routes reaper work by it, and
two live SQL functions refuse or select by it. The client skips quarantine
collections by a prefix tuple in five places. A collection's lifecycle is a
column; the prefix is a second source of truth for it.

#### Gap 3: New writes still have to have the conformant shape

`get_or_create_collection` refuses a new non-conformant name
(`src/nexus/db/t3.py:733-742`); `nx catalog rename-collection` refuses a
non-conformant target without `--allow-legacy`; the indexer reroutes any
non-conformant collection to a rendered one; the client derives
`legacy_grandfathered` and, when attributes are missing, all four attributes
from the name it is registering; the next version number is computed by
parsing a name although the row has a `model_version` column. Every
collection name also still passes ChromaDB's own limits (3 to 63 characters,
Chroma's character set, ChromaDB Cloud's 128-byte cap) in
`validate_collection_name`, a limit no Postgres object needs.

#### Gap 4: The census gates that guard RDR-204's result cannot see most of what is left

RDR-204 left two ratchet tests (tests that pin a count of a retired idiom so
it can only fall): `tests/test_collection_name_parse_census.py` on the client
and `CollectionParseGateTest` on the engine. The client gate matches four raw
call shapes plus callers of three named primitives outside `corpus.py`; it
does not see callers of the second-ring helpers, f-string prefix tests,
substring owner matches, or `startswith` on a tuple variable. The engine gate
counts Java string literals that contain `__`; it cannot see the
`quarantine-` prefix and does not scan SQL at all. Both gates are green while
most of the surface in the census is unguarded.

#### Gap 5: Retiring name parsing can strand a migration path

Some code legitimately reads a name that has no row on this install yet:
restoring a recovery bundle (whose format carries the recorded name, not the
source row), `nx catalog migrate-fallback`, the indexer's legacy-collection
migration, and the engine's ETL import. A reader repoint that removes their
name-parse fallback before they have another source would break the upgrade
and restore paths. The bead names this hazard directly: the reader cutover
must not be able to strand a migration path.

#### Gap 6: The name is the reference key of the vector and taxonomy schema

`catalog_collections` is keyed `(tenant_id, name)`. Eight live foreign keys
point at that key or at the RDR-225 composite `(tenant_id, name,
embedding_model)`. The name also sits inside two primary keys, the chunks
table's `(tenant_id, collection, chash, embedding_model)` and
`taxonomy_centroids`', and three more foreign keys reach the name through the
chunks key (the manifest, `topic_assignments`, `chunk_orphaned_at`). Fifteen
further columns in twelve tables hold a name with no foreign key at all. The
RDR-227 per-collection index is built with the predicate `WHERE collection =
'<name>'` and named from a hash that includes the name. So changing what a
collection is called means rewriting every one of those rows and rebuilding
its indexes. `renameCollectionTxn` does exactly that today: it inserts a new
registry row, rewrites the name across the fourteen
`COLLECTION_SCOPED_TABLES`, retags quarantine metadata, rewrites note URIs and
tombstones the old row. A display label cannot be free while it is also the
key.

#### Gap 7: Some facts are reachable only through the name shape

A knowledge subject (`distributed-systems`) resolves to its collection by
rendering `knowledge__<subject>__<model>__v1` and probing for that name. The
row holds the subject in `owner_id`, but only because registration derives
it from the name, and no reader goes from subject to row. `nx index repo`
finds its owner and version by row, then parses `model_version` out of the
returned name and renders the final name on the client. An opaque id for new
collections (D2) breaks every path that finds a collection by rendering its
name, so these paths have to find the row by its attributes first.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-101 (event-sourced catalog) | Origin | Defined the four-segment name and created `catalog_collections`. Its rationale for the name was ChromaDB's metadata poverty and name regex; both expired with RDR-155. |
| RDR-103 (catalog as collection-name authority) | Origin | Made the catalog the only place that renders a name (`CollectionName.render`). Its surviving rationale, "switching embedding models necessarily creates a new collection", is kept: a model switch still mints a sibling collection. Only parse-as-read is retired here, as RDR-204 already said. |
| RDR-155 / RDR-156 (pgvector consolidation; capability leverage) | Precedent | Moved T3 onto Postgres and kept collection names verbatim. RDR-156 turned the "names verbatim" prose contract into `ON UPDATE CASCADE`, which is why a label can change without a manual cascade. |
| RDR-176 (survivable managed migration) | Precedent, closed | Shipped nexus-zvcou (one config-first service URL and token chain for every storage command) as nexus-t9rmg.8. The bead asked to fold zvcou in or hard-depend on it; it is satisfied and carries no work here. |
| RDR-204 (embedding profile; `catalog_collections` as the authority) | Origin, closed | Shipped most of nexus-j4gl6 steps 1 and 2. Rejected step 3 (opaque names) on 2026-09-09 with a reopen trigger. This RDR takes over its two census gates, repairs their blind spots, and finishes the reader move. It does not restate RDR-204's schema; it builds on it. |
| RDR-210 (one local engine serves both embedders) | Adjacent, closed 2026-10-26 | Finds model siblings and its re-embed target by the row tuple `(content_type, owner_id)`, never by a name (its Gap 4 fix and its RDR-229 row). One name-based sibling matcher is left, `repo_identity.list_sibling_collections`, with no caller in `src`; P2.7 retires it. |
| RDR-225 (one vector table per embedding model) | Adjacent, accepted and deployed | Keys every chunk on `(tenant_id, collection, embedding_model)` through a composite foreign key to `catalog_collections`, written from the row, never the name. Phase 3 here moves that key to `(tenant_id, collection_id, embedding_model)`; the partitioning (by model, then tenant) does not change, because no partition key holds the name. |
| RDR-227 (per-collection HNSW above the router threshold) | Adjacent, deployed | Builds each per-collection index as a partial index `WHERE collection = '<name>'` and names it from a hash of model, tenant and name. Phase 3 rebuilds every such index on `collection_id`. |

Searched `docs/rdr/README.md` and the RDR tree for "collection name",
"naming", "conformant", "opaque", "quarantine", "segment" and "sibling". No
other draft overlaps.

**Scope boundary with RDR-210.** RDR-210 owns how a dual-mode engine finds
and fills model siblings. RDR-229 owns where any code learns a collection's
attributes. The interface between them is one rule, which RDR-210 shipped: a
sibling is found by the row tuple `(content_type, owner_id)` through the
catalog list route's filters, never by rendering or matching a name. The old
Open Question 6 (which lands first) is moot.

**Sequencing with RDR-225.** No blocking dependency. RDR-225 is deployed
(nexus-3wh8d, 29 of 31 beads closed; open are the 14-day drop of
`chunks_retired_225` and its phase-review gate). Two ordering rules apply:
every changeset this RDR adds is ordered after `vectors-030` (and, for the
Phase 3 key work, after the latest `vectors-` changeset on develop at the
time), and a function
`vectors-030` defines (for example `quarantine_restore_chunks`) is redefined
with its four-column write sites intact and stays under
`PlpgsqlCheckGateTest`. The manifest's new `embedding_model` column
(`vectors-030-model-tenant-partition-functions.xml` lines 786-791) lets Phase 2 rewrite `chash_conformance_report` without
parsing.

## Context

### Background

The bead was filed in June 2026, before RDR-204 existed, and deferred until
the cutover settled. RDR-204 then did the large part of the work under the
same thesis. The question this draft answers is "what still reads the name,
and what would it take to stop", measured on the current tree, not "should
the name stop carrying metadata", which RDR-204 already settled for reads.

### Technical Environment

- Client: Python 3.12+, `src/nexus/corpus.py` (name helpers and resolution),
  `src/nexus/mcp_infra.py` (`get_collection_row`, a cached copy of each
  collection's registry row fed by `GET /v1/vectors/stats`),
  `src/nexus/catalog/collection_name.py` (`CollectionName`, render and parse).
- Engine: Java service, jOOQ only, Liquibase changelog under
  `service/src/main/resources/db/changelog`, `CollectionRegistry` (per-tenant
  row cache), `QuarantineOrigin`, `ChunkReaper`.
- Schema: `nexus.catalog_collections(tenant_id, name, content_type, owner_id,
  embedding_model, model_version, dimension, lifecycle_state,
  legacy_grandfathered, superseded_by, ...)`, primary key `(tenant_id, name)`
  (`catalog-001-baseline.xml:211`); since RDR-225 also UNIQUE `(tenant_id,
  name, embedding_model)` (`vectors-030:126`) as the target of the chunk
  foreign key. `nexus.chunks` and `nexus.taxonomy_centroids` are partitioned
  by `embedding_model`, then by `tenant_id`.

## Research Findings

### Investigation

Source search on develop `2ad1f90c9` (2026-10-18). The client census was run
by importing the existing census scanner
(`tests/test_collection_name_parse_census.py::_collection_name_parse_sites`)
and by an AST walk of `src/nexus` for calls to every `corpus.py` function
that reads a name. The engine census used `rg` over `service/src/main/java`
for `__` literals and the `quarantine-` prefix. The SQL census resolved each
plpgsql function to its last definer in `db.changelog-master.xml` include
order, with `<rollback>` blocks stripped, and then searched those bodies for
`split_part(...'__'...)`, `string_to_array(...'__')` and the `quarantine-`
prefix. Findings are registered in T2 `nexus_rdr/229-research-1` to `-13`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus engine (`service/src/main`) | Yes | No `__` literal remains; the `quarantine-` prefix is read in 11 places; three live SQL functions parse names. |
| nexus client (`src/nexus`) | Yes | 46 sites under the existing census, 75 second-ring helper calls outside it, 9 raw shapes it cannot match. |
| Liquibase / PostgreSQL | N/A | No new library behaviour is relied on. |

### Key Discoveries

- **✅ Verified** (source search) — RDR-204 shipped most of the bead's steps 1
  and 2, and Sam rejected its opaque-name phase on 2026-09-09 (commit
  `6d9e7d124`), with the reopen trigger "a field report of a mint-time name
  whose segments disagree with the row it registered". *Source: RDR-204 lines
  754-763; T2 `229-research-1`.*
- **✅ Verified** (source search) — Of the three essential sites the bead
  named, `_is_same_model_passthrough` is deleted (`3ccf25a48`), engine dim
  dispatch reads the registry, and the 4-segment guard is still live at
  `src/nexus/db/t3.py:733-742`, with the rename gate at
  `src/nexus/commands/catalog_cmds/collections.py:279`. *Source: T2
  `229-research-2`.*
- **✅ Verified** (source search) — **The parse-site census: 152 sites on five
  surfaces.** This is the count the rest of this RDR cites.
  1. *Client, inside the existing census* (46 sites, 19 files):
     `collection_shape.py:190/193/197`; `commands/catalog.py:1249/1286/1465/
     1628/1634/1662/1876/1877`; `commands/collection.py:704/860/861/947/
     1199(x2)/1206/1235`; `db/http_vector_client.py:1109/1153/3212/5717`;
     `db/t3.py:413/920/1405/1843`; `commands/index.py:115/1141/1150/2345`;
     `catalog/chunk_quarantine.py:139/144`;
     `commands/catalog_cmds/reconcile_stale.py:313/385`; and one each at
     `catalog/orphan_backfill.py:371`, `catalog/owner_scope.py:39`,
     `commands/catalog_cmds/doctor.py:1053`,
     `commands/catalog_cmds/integrity.py:651`, `commands/doctor.py:1558`,
     `commands/enrich.py:1207`, `commands/store.py:761`, `exporter.py:1450`,
     `health.py:8916`, `mcp/core.py:3731`, `search_engine.py:660`.
     *T2 `229-research-3`.*
  2. *Client, second ring outside the census* (75 calls): see T2
     `229-research-4` for the full list by helper. The decisive ones are
     chunk metadata `embedding_model` taken from the name
     (`db/http_vector_client.py:3211-3214`, `db/t3.py:919-921`,
     `catalog/note_write.py:260`), the indexers' target model
     (`doc_indexer.py:1753/2710`, `indexer.py:5288/5289/5513`), score
     calibration (`scoring.py:124`, `catalog/store_hook.py:193`), taxonomy
     thresholds by prefix (`commands/taxonomy_cmd.py:2715/2724/2935`,
     `db/t2/http_taxonomy_store.py:2625`), and the next version computed by
     parsing a name (`catalog/http_catalog_client.py:3299`).
  3. *Client, raw shapes the census cannot match* (9 sites):
     `corpus.py:1306` and `corpus.py:2489-2490` (f-string prefix tests),
     `repo_identity.py:451-452` and `:455` (substring and hash-suffix sibling
     match), `_BYPASS_SCHEMA_PREFIXES` prefix tests at `db/t3.py:151`,
     `exporter.py:1393`, `commands/collection.py:697`,
     `commands/catalog_cmds/t3_orphans.py:89`,
     `commands/catalog_cmds/integrity.py:550`, and one script,
     `scripts/rdr-108-je0b-backfill.py:92`. *T2 `229-research-5`.*
  4. *Engine Java, quarantine prefix* (11 sites):
     `CatalogRepository.java:7983-7984` (new-row lifecycle in
     `upsertCollection`), `:8375`, `:11261-11262`
     (`lifecycleStateForImport`); `QuarantineOrigin.java:85/162` (the
     predicate itself at `:73`); `VectorHandler.java:1854`;
     `PgVectorRepository.java:5756/5772`; `ChunkReaper.java:278/828/830`.
     Names are rendered with the prefix at `ChunkReaper.java:986` and
     `PgVectorRepository.java:5817`. *T2 `229-research-6`.*
  5. *Live SQL* (11 expressions in three functions):
     `nexus.chash_conformance_report(dim)`,
     `vectors-005-repoint-functions-views.xml:1193-1295`, nine
     `split_part(collection, '__', 3)` model IN-lists;
     `nexus.quarantine_restore_chunks`,
     `vectors-030-model-tenant-partition-functions.xml:2198`;
     `nexus.reaper_expire_client_quarantine`,
     `vectors-028-reaper-expire-client-quarantine.xml:107`. *T2
     `229-research-7`.*

  The parsers these sites call are defined in `corpus.py`:
  `_CONFORMANT_COLLECTION_RE` (:153), `is_conformant_collection_name` (:161),
  `parse_conformant_collection_name` (:182),
  `embedding_model_for_collection_name` (:768),
  `model_version_for_collection_name` (:785), `_LEGACY_SPLIT_RE` (:825) and
  `split_candidate_collection_name` (:828), plus `CollectionName.parse`
  (`catalog/collection_name.py:96`). They are definitions, not counted
  sites.
- **✅ Verified** (source search) — `validate_collection_name`
  (`corpus.py:30-95`) still enforces ChromaDB's 63-character, charset and
  128-byte limits. No Postgres object is named from a collection name:
  RDR-225 leaf names and PCI index names are hashes. *T2 `229-research-8`.*
- **✅ Verified** (source search) — For repo collections the row's
  `owner_id` is set from the documents' tumbler and differs from the name's
  owner segment by design (`catalog-044`, nexus-6pbwx). *T2
  `229-research-9`.*
- **✅ Verified** (source search) — RDR-225 writes each chunk's
  `embedding_model` from the registry row and keys the chunk foreign key on
  `(tenant_id, name, embedding_model)`; a rename stays inside its partition
  when the model is unchanged. *T2 `229-research-10`.*
- **✅ Verified** (source search) — RDR-210 finds siblings and its re-embed
  target by name. *T2 `229-research-11`.*
- **✅ Verified** (source search) — nexus-zvcou is closed, shipped under
  RDR-176 as nexus-t9rmg.8, label `verification:passed`. *T2
  `229-research-12`.*
- **✅ Verified** (source search) — The recovery bundle format carries a
  collection's recorded name and no registry row, so its import must read the
  name (`catalog/recovery_bundle.py:403-414`). `db/embed_migrate.py`, which
  RDR-204's census listed as a holdout, no longer exists.
- **❓ Assumed** (needs a spike) — On every production tenant, every live
  row's `content_type` and `embedding_model` equal what its name parses to.
  RDR-204 measured zero disagreements for the model on 2026-09-06; nobody
  has measured since RDR-225's walk. *T2 `229-research-13`.*
- **✅ Verified** (source search, develop `1fcfaf57b`, 2026-11-08) — **The
  surrogate-key census.** Eight live foreign keys target the registry
  (`topic_assignments` fk-002:241 with an ON UPDATE CASCADE that never
  fires, `document_aspects`, `topics`, `taxonomy_meta`, `document_highlights`
  in fk-003, `chunks` vectors-030:1063 with no ON UPDATE, `canary_query_ring`
  vectors-054:90, and the conditional `aspect_extraction_queue`). The name is
  inside the chunks and centroid primary keys (vectors-030:677, :690), and
  three ON UPDATE CASCADE foreign keys reach it through the chunks key
  (vectors-030:1018/1033/1048). Fifteen name columns in twelve tables have no
  foreign key; four more carriers are not columns (quarantine
  `origin_collection` metadata, quarantine sibling names, `nx-note://` source
  URIs, aspect tasks in the tuple space). No partition key or leaf name holds
  the name. *T2 `229-research-14`.*
- **✅ Verified** (source search) — `renameCollectionTxn`
  (`CatalogRepository.java:11160-11492`) never updates a registry name in
  place; it inserts, rewrites fourteen tables, retags and tombstones. The
  RDR-227 per-collection index is keyed by name in its predicate
  (`PciBuilderSession.java:103-105`) and its name hash
  (`PciCatalog.java:415-439`). At least 123 of 345 engine routes carry a
  collection name, always in the query string or body, never in the path.
  *T2 `229-research-15`.*
- **✅ Verified** (source search) — A knowledge subject resolves by name
  rendering and probing (`corpus.py:1318-1527`); `nx index repo` parses the
  version from a returned name (`http_catalog_client.py:3646`);
  `collectionForTuple` breaks ties by name and filters on
  `legacy_grandfathered`. Fifteen MCP tools, 48 CLI options and 30 positional
  CLI arguments address collections by name. *T2 `229-research-16`.*
- **✅ Verified** (measured) — conexus shipped 36 releases between 2026-10-08
  and 2026-11-07 (`git tag`), so "a stated number of releases" (D4) is weeks,
  not months, at the current rate.

### Critical Assumptions

- [ ] Every live registry row on both production tenants agrees with its
  name on `content_type`, `embedding_model` and `model_version`, and its
  `lifecycle_state` is `quarantine` exactly when its name starts with
  `quarantine-`. If this is false, repointing a reader changes what it
  returns, and that is a behaviour change to review row by row, not a
  refactor. — **Status**: Unverified — **Method**: Spike (Phase 1 Step 2, a
  read-only census on both tenants, before any reader moves).
- [ ] The row cache (`mcp_infra.get_collection_row`) is populated for every
  collection a write path names, so a row-first read on the write path costs
  no extra round trip. — **Status**: Unverified — **Method**: Source Search
  of `get_collection_row`'s refresh path plus a test that counts HTTP calls
  per indexed document before and after (Phase 2 Step 2).
- [x] No code path needs a fact from the name that the row cannot carry,
  except the migration sources in Gap 5. — **Status**: Verified —
  **Method**: Source Search (the parse-site census: every extracted value is
  content type, owner, model, version or the quarantine prefix, each a
  column).
- [x] Nothing in Postgres requires a collection name to fit ChromaDB's
  limits. — **Status**: Verified — **Method**: Source Search (no object is
  named from a collection name; the per-collection index name is a hash).
- [ ] Re-keying `nexus.chunks` and `nexus.taxonomy_centroids` on
  `collection_id` (a new column backfilled on every row, a new primary key
  per leaf, three child foreign keys re-pointed) and rebuilding every
  per-collection index fits in a maintenance window per tenant on the larger
  production tenant. — **Status**: Unverified — **Method**: Spike (P3.2, a
  timed run on a PITR fork of that tenant, before the changeset is written
  in its final form).
- [ ] No two live rows of one tenant share a label once renames stop
  tombstoning, so the partial UNIQUE on live labels (Design item 6) can be
  created without data repair. — **Status**: Unverified, expected true (the
  label is the primary key today) — **Method**: Spike (folded into P1.2's
  census).

## Proposed Solution

### Approach

Fix what reads the name first, then change what the name is. Phases 1 and 2
close Gaps 1, 2, 4, 5 and 7 by moving every reader onto the registry row;
they change no schema key and rename nothing. Phase 3 adds the surrogate key
and moves every reference onto it in the strangler order (expand, switch,
contract): new id columns are added and backfilled beside the name columns,
writers fill both, readers and constraints switch to the id, and only then
does Phase 4 drop the name columns and the parsers. At every step the old
path still works, so each step ships on its own and can stop.

Nothing is renamed by any phase. An existing collection's name becomes its
label unchanged. Only collections created after Phase 3 get a minted label.

### Technical Design

**1. One resolver, one parser, three classes of caller.** The client gets a
single read path for a collection's attributes and a single place where a
name may still be parsed.

```text
// Illustrative; verify against corpus.py and mcp_infra.py during implementation
resolve_collection(ref: str | int) -> CollectionAttributes
    # a label or a collection_id; reads the registry row (cached);
    # raises CollectionNotRegisteredError
find_collection(content_type, owner_id, embedding_model) -> CollectionAttributes | None
    # by attributes, through GET /v1/catalog/collections/for_tuple
CollectionAttributes: collection_id, label, content_type, owner_id,
    embedding_model, model_version, dimension, lifecycle_state
legacy_names.candidate_attributes(name: str) -> CandidateAttributes | None
    # the only name parser left; callers must be classified
```

Every caller of `legacy_names` is one of three classes, recorded in the
census beside the site: **mint** (the name is the user's own input or one
`nx` is about to register, so no row exists yet; this class empties at
Phase 3, when minting stops rendering names), **migration source** (the name
comes from another install or an older era: recovery bundle import,
migrate-fallback, legacy-collection migration), or **diagnostic** (doctor,
reconcile and shape checks whose purpose is to report a name that disagrees
with its row). Any other caller is a defect the census refuses. RDR-204's
four dispositions (a) to (d) map onto these three; its class (b),
write-model resolution, becomes a row read and leaves the list.

**2. Lifecycle is a column, set by the caller that knows it.** The engine
code that quarantines chunks knows it is quarantining; it passes
`lifecycle_state = 'quarantine'` when it registers the sibling, and
`upsertCollection` stops inferring the state from the prefix. Reads of
lifecycle (`QuarantineOrigin`, `ChunkReaper` routing, `VectorHandler`,
`PgVectorRepository`, the two SQL functions, and the client's
`_BYPASS_SCHEMA_PREFIXES` checks) read the row's `lifecycle_state`. The
`quarantine-` prefix stays on the labels that already have it. The ETL
import path, which receives rows from another install, takes
`lifecycle_state` from the imported row when present and falls back to the
prefix as a migration source.

**3. SQL reads columns.** `chash_conformance_report(dim)` filters manifest
rows by the manifest's `embedding_model` column (the `vectors-030`
changeset adds it, lines 786-791) joined to
`nexus.embedding_models.dimension`, instead of nine `split_part` IN-lists
that also hard-code the model list. The two quarantine functions test
`lifecycle_state` instead of the prefix.

**4. Census gates that can see the surface.** Before any site moves, both
gates are widened and re-pinned at the measured count:

- Client: the fifth class covers every `corpus.py` function that reaches a
  parser, derived at test time by walking `corpus.py`'s call graph from the
  parser definitions, not typed as a list. New shapes: f-string `startswith`
  / `endswith` whose literal part ends in `__`, `startswith` on a module
  constant that holds a collection prefix, and `"__<x>__" in name`
  substring tests. Calls inside `legacy_names` are the floor.
- Engine: the Java gate also counts the `quarantine-` literal outside the
  one constant that renders it. A new SQL gate resolves each live plpgsql
  function to its last definer, as the research census did, and pins the
  count of name-parsing expressions in live bodies.

**5. Find by attributes, never by rendering (closes Gap 7).** A knowledge
subject resolves through `find_collection('knowledge', subject, model)`: the
row's `owner_id` already holds the subject (`docs/collections.md` keeps a
knowledge collection's owner equal to its subject, and `catalog-044` exempts
it from the documents-owner rewrite). On a write with no row, the client
registers one with explicit attributes and lets the engine mint the label
(item 8). `nx index repo` reads `model_version` from the `for_tuple`
response instead of parsing it from the returned name, and uses the
returned row instead of rendering a name. `collectionForTuple` breaks ties
by `collection_id`, not by name. The name-rendering branch of
`t3_collection_name` stays only as a migration source for a legacy
two-segment knowledge name that has no row.

**6. The surrogate key (D3).** `catalog_collections` gains `collection_id
bigint GENERATED ALWAYS AS IDENTITY`, unique across the installation, with a
UNIQUE `(tenant_id, collection_id)` that every reference targets, so
`tenant_id` still leads every foreign key (row-level security and partition
pruning on tenant leaves are unchanged). At Phase 4 the primary key becomes
`(tenant_id, collection_id)` and `(tenant_id, name)` stops being a key.

A bigint rather than a UUID because the id joins the chunks primary key,
the largest index in the schema, and the manifest, `topic_assignments` and
`chunk_orphaned_at` keys that reference it: eight bytes replace a text name
that is about forty bytes in the conformant shape, so every one of those
indexes shrinks. The cost is that one installation-wide sequence lets a
tenant estimate how many collections other tenants have created; tenants
are a handful and operator-run, and the label (item 8) shows only the id.

The key moves table by table in strangler order:

- **Expand.** Every referencing table gains a `collection_id` column next to
  its name column: the eight foreign-key tables, `chunks` and
  `taxonomy_centroids` (whose primary keys hold the name), the manifest and
  `chunk_orphaned_at` (which reach the name through the chunks key), and the
  operational name columns with no foreign key (`physical_collection`,
  `pdf_pipeline`, `chash_remap`, `reembed_jobs`, `superseded_by` as
  `superseded_by_id`). The engine writes both columns on every insert; a
  per-tenant backfill fills the old rows in bounded batches. The quarantine
  `origin_collection` metadata tag gains an `origin_collection_id` beside it.
- **Switch.** Each foreign key is re-created on `(tenant_id, collection_id)`
  (or `(tenant_id, collection_id, embedding_model)` for the RDR-225
  composite), `NOT VALID` first and validated after, as fk-002 did. The
  chunks and centroid primary keys are rebuilt per leaf on `(tenant_id,
  collection_id, chash, embedding_model)` and `(tenant_id, collection_id,
  topic_id, embedding_model)`, and the three child foreign keys follow. Every
  per-collection index is rebuilt with `WHERE collection_id = <id>` and a
  name hashed from model, tenant and id, so a label change never strands
  one. Readers (engine repositories, SQL functions, client caches) key on
  the id. Every engine route that takes a collection accepts
  `collection_id`; a label is still accepted at the route boundary and
  resolved to the id once, by `CollectionRegistry`, which is keyed by id
  with a label index beside it. Responses carry both.
- **Contract (Phase 4).** The name columns on referencing tables are
  dropped, the `(tenant_id, name, embedding_model)` UNIQUE is dropped, and
  the name-based arms of `COLLECTION_SCOPED_TABLES` go with them.

The history tables (`relevance_log`, `search_telemetry`, `hook_failures`,
`gc_audit`) gain a nullable `collection_id` for joins and keep their name
column as a record of the label at the time of the event. A rename stops
rewriting them; their old rows keep the old label, which is what a log
should say.

**7. The label and what uniqueness it keeps.** The `name` column becomes
the label: the string a person types and reads, and what the 15 MCP tools,
the 48 CLI options and the 30 positional CLI arguments accept. It is no
longer a key. No foreign key targets it, no index is named from it, and no
partition holds it. It keeps one uniqueness rule, for addressing only: **a
label is unique among a tenant's live collections**, enforced by a partial
UNIQUE index on `(tenant_id, name)` over rows that are not superseded. Two
collections of one tenant can no longer answer to the same label, so a
label in a command names one collection without ambiguity. A superseded
(tombstoned) row keeps its label for history and releases it, so the label
can be reused. Model siblings no longer collide, because each sibling gets
its own minted label. Labels in different tenants are independent, as
names are today.

The label rule replaces ChromaDB's limits in `validate_collection_name`:
non-empty, valid UTF-8 without control characters, at most 255 bytes, and
not of the minted form (item 8) unless the engine minted it, so a user
cannot claim a label that a later mint would produce.

A rename becomes `UPDATE catalog_collections SET name = <new>` on one row.
`renameCollectionTxn` stops inserting, rewriting fourteen tables and
tombstoning. It still rewrites the `nx-note://<label>/` source URIs of the
collection's notes, as it does today; moving note identity off the label is
a catalog-identity change outside this RDR.

**8. Minting (D2).** When a collection is registered with no label, the
engine takes the next `collection_id` and sets the label to `c` followed by
that id in decimal (`c4812`), in the one insert. It is opaque: it carries
no content type, owner, model or version. Listings show the attributes
beside it (`nx collection list`, `collection_list`, `nx catalog show`). A
user who wants a readable label sets one with `nx collection rename`; a subject or repo collection is found by its attributes
either way (item 5), so a label is never needed to find one. Every client
path that renders a conformant name for a new collection
(`CollectionName.render`, `t3_collection_name`'s write branch, the
indexer's reroute, `collection_for_repo`'s client-side render) stops doing
so and registers by attributes. Existing collections keep their names as
labels.

**9. The non-stranding rule.** Phase 2 may repoint a reader only after every
migration source it serves has a test that runs a row-less legacy name
through it and passes. Those tests are written first (P2.1) and stay in the
suite until the fallback they guard is retired (item 10).

**10. Recovery bundles carry the row (D4).** From Phase 3 the recovery
bundle and export format record each collection's registry row, including
`collection_id`, label and attributes, and import registers from the row.
Import re-maps the source installation's `collection_id` to the id the
target mints, and keeps the label (or reports a clash with a live label on
the target and refuses that collection). An old bundle, which carries names
only, still imports through `legacy_names` for N minor releases after the
release that ships the new format. N is open for Sam to set at the gate
(Open Item O1; this draft proposes 20, about one month at the measured
rate). From minor release N+1 on it is refused with a message that says to re-export from the source, and,
because a recovery bundle may outlive its source, names the last conexus
version that still imports it.

**11. `legacy_grandfathered` goes (D5).** Phase 3 stops writing it: the
client stops deriving it, and `collectionForTuple` drops its
`legacy_grandfathered = false` filter (a grandfathered collection has the
same attributes as any other). Phase 4 drops the column and
`idx_catalog_collections_legacy` with it.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `resolve_collection`, `find_collection` | `corpus.collection_content_type` / `collection_owner` / `collection_model`, `mcp_infra.get_collection_row`, `HttpCatalogClient.collection_for` | Extend: one function returning the whole row by label or id, one by attributes; the three helpers stay as thin wrappers until their callers move. |
| `legacy_names` module | `corpus.split_candidate_collection_name`, `embedding_model_for_collection_name`, `model_version_for_collection_name`, `parse_conformant_collection_name` | Replace: move them into one module whose callers are classified; delete the rest. |
| Client census | `tests/test_collection_name_parse_census.py`, `tests/test_parse_conformant_collection_name_caller_census.py` | Extend: new shapes, derived helper set, class labels per site. |
| Engine census | `CollectionParseGateTest` | Extend with the `quarantine-` literal; add a SQL live-body gate beside `PlpgsqlCheckGateTest`. |
| Lifecycle at registration | `CatalogRepository.upsertCollection`, `lifecycleStateForImport` | Extend: explicit `lifecycle_state` input; prefix only as the import fallback. |
| Surrogate key | `catalog_collections`, the fk-002 / fk-003 / vectors-030 / vectors-054 foreign keys | New column and constraints; the fk-002 `NOT VALID` then validate pattern is reused for every switch. |
| Row cache | Engine `CollectionRegistry` (keyed by tenant and name), client `mcp_infra._collections_cache`, client registration cache | Change: key by `collection_id`, with a label index for the route boundary. |
| Rename | `CatalogRepository.renameCollectionTxn`, `COLLECTION_SCOPED_TABLES` | Replace: a one-row label update plus the note-URI rewrite; `COLLECTION_SCOPED_TABLES` keeps serving delete and rehome, keyed by id. |
| Per-collection index | `PciBuilderSession`, `PciCatalog` | Change: predicate and name hash on `collection_id`; every existing index rebuilt. |
| Recovery bundle | `catalog/recovery_bundle.py` | Extend: the format carries the registry row; the name path stays for N minor releases (Open Item O1). |

### Decision Rationale

Splitting the work at the reader line first keeps Phases 1 and 2 free of
schema risk while they fix the defects that exist today (chunk metadata and
lifecycle read from a string the row overrides). The surrogate key then
removes the reason a label could not change: today the name is a key in
eight foreign keys, two primary keys and every per-collection index, so a
rename rewrites the schema's largest tables. Once those reference an id, a
rename is one row and the label can be whatever a person finds useful,
which is what Sam's opaque-id ruling needs. The census-first order follows
RDR-204's method, which worked; the widened gates exist because RDR-204's
gates reported done while 106 of the census's sites were outside them. The
strangler order is Sam's own condition for reopening the phase ("with good
planning/execution (strangler pattern, etc), this should not be an issue",
2026-10-18).

## Alternatives Considered

### Alternative 1: Close the bead as done by RDR-204

**Description**: Treat RDR-204 as the bead's resolution and record the
second ring as follow-up beads.

**Reason for rejection**: Sam reopened RDR-204's Phase 4 (D1). It would also
leave the write-path model read (Gap 1) and the lifecycle prefix (Gap 2) as
known defects behind green gates (Gap 4).

### Alternative 2: Keep `(tenant_id, name)` as the key and make the name a label anyway

**Description**: The 2026-10-17 draft's choice. Keep the name as the
primary key and foreign-key target, relax its shape, and rely on
`renameCollectionTxn` (or `ON UPDATE CASCADE`) whenever a label changes.

**Pros**:

- No change to `COLLECTION_SCOPED_TABLES`, RDR-225's composite foreign keys,
  the manifest, the per-collection indexes or any route.

**Cons**:

- A label change still rewrites every chunk, manifest and taxonomy row of
  the collection and rebuilds its per-collection index; the label is not
  free.
- The chunks foreign key has no ON UPDATE, which is why the rename inserts a
  second registry row and tombstones the first; the tombstone trail stays.
- Every chunk-keyed index keeps a text name in its key.

**Reason for rejection**: Sam chose the surrogate key (D3, reversing this
draft's earlier rejection).

### Alternative 3: Rename every collection to an opaque id now

Rejected: D2 applies to new collections, and existing names stay as their
labels. Renaming every existing collection moves no information and breaks
every script and note URI that names one.

### Briefly Rejected

- **A UUID `collection_id`**: twice the bytes of a bigint in the chunks
  primary key and its three children, for a uniqueness the identity
  sequence already gives within one installation; the recovery bundle
  re-maps ids across installations (item 10), so global uniqueness is not
  needed.
- **Label unique across live and superseded rows**: would stop a label from
  being reused after a collection is retired, for no addressing benefit.
- **No uniqueness on the label**: a label typed in a command could name two
  collections, and every CLI and MCP surface would need an id instead.
- **Keep parsing, but check the parse against the row at every read**:
  doubles the work and keeps two sources of truth.
- **Drop the `quarantine-` prefix from existing quarantine labels**: the
  prefix is harmless once nothing reads it, and operators recognise it.

## Trade-offs

### Consequences

- Chunk metadata, scoring and taxonomy use the model and content type the
  engine actually stored, which can differ from the name only where the row
  was corrected, which is where it matters.
- Every reader of a registered collection depends on the row cache being
  current. A stale cache was already a dependency of corpus resolution since
  RDR-204.
- A rename is one row and is instant. Tombstones from renames stop
  accumulating.
- New collections have opaque labels such as `c4812`. A person finds a
  subject or repo collection by its subject or repo, as today; a person who
  wants a readable label sets one.
- Scripts and docs that build names by string concatenation stop being a
  supported interface.
- History tables keep the label as it was when the row was written.

### Risks and Mitigations

- **Risk**: A row and its name disagree on a production tenant, so a
  repoint changes results. **Mitigation**: P1.2 measures it first; each
  disagreeing row is listed and decided before Phase 2 starts.
- **Risk**: A migration source loses its only way to learn attributes.
  **Mitigation**: the non-stranding rule (item 9) and its tests-first step.
- **Risk**: The chunks re-key and the per-collection index rebuild take
  longer than a maintenance window on the larger tenant. **Mitigation**:
  Critical Assumption 5, timed on a PITR fork in P3.2 before the changeset
  is final; the backfill runs in bounded per-tenant batches while both
  columns are written, so only the key swap needs the window.
- **Risk**: A writer misses the dual write, leaving a row with a null
  `collection_id` when the switch validates. **Mitigation**: the `NOT VALID`
  then validate pattern fails the validation loudly; a census test counts
  null ids per table before each switch.
- **Risk**: The engine change to registration lifecycle reaches a client
  that still relies on the prefix inference. **Mitigation**: the client
  never registers quarantine siblings since nexus-syfes (the engine does,
  from the origin's row), so only engine callers change.
- **Risk**: An old recovery bundle is the only copy of a lost installation
  after its fallback retires. **Mitigation**: the refusal names the last
  conexus version that imports it (item 10).

### Failure Modes

- A write path reads a collection with no row: `CollectionNotRegisteredError`
  names the collection and the register call that should have preceded the
  write. Visible, not silent.
- A new parse site is added outside `legacy_names`: the client census fails
  with the file, line and shape.
- The SQL live-body gate misresolves a function's last definer: the gate's
  non-vacuity assertion requires it to find at least the functions this RDR
  rewrites, so an empty scan fails rather than passing.
- A label clash on a recovery-bundle import: that collection is refused
  with both labels named; the rest of the bundle imports.
- A route receives a label that two live rows hold: impossible once the
  partial UNIQUE exists; before it exists (Phases 1 and 2) the name is
  still the primary key.

## Implementation Plan

### Prerequisites

- [x] Sam's rulings on the open questions (D1 to D5, 2026-10-18 and
  2026-11-08).
- [ ] Critical Assumptions 1 and 6 measured (P1.2).
- [ ] Critical Assumption 5 measured (P3.2) before the Phase 3 switch
  changesets are final.

### Minimum Viable Validation

On a substrate-backed engine (the local-service gate), a fixture tenant with
one collection whose row was corrected so its `owner_id` and
`embedding_model` differ from its name: `nx index md` into it stamps the
row's model into chunk metadata, `nx search` calibrates with the row's
model, and a quarantine of one of its chunks registers a sibling whose
`lifecycle_state` is `quarantine` without the engine reading the prefix. In
the same run, a recovery bundle recorded under a legacy name restores. Both
census gates are at their Phase 2 pins. After Phase 3, the same fixture is
renamed and searched: the rename touches one registry row, its chunks and
per-collection index are unchanged, and `store_put` to a new subject
registers a collection with a minted label that `nx search --corpus
<subject>` finds.

### Proposed beads

Not created; this list is for planning at acceptance. Each implementation
bead is test-first: its failing test lands first in the same branch, and
its acceptance is that test green plus the census pin it lowers.

**Phase 1: the census and the measurement**

- P1.1 Widen the client census (new shapes, derived helper set, class label
  per site) and re-pin at the measured count. First live run pasted into the
  bead.
- P1.2 Read-only census on both production tenants: per live row, row versus
  name for content type, model, version and lifecycle; owner differences
  counted separately as expected; live labels that would clash under a
  partial UNIQUE (Critical Assumption 6). Bounded load, results in T2.
  Blocks P2.2 onward.
- P1.3 Widen the engine Java gate to the `quarantine-` literal; add the SQL
  live-body gate. First live run pasted into the bead.
- P1.4 `http_catalog_client` reads the next version from the `for_tuple`
  row's `model_version`, not by parsing the returned name; `collectionForTuple`
  breaks ties by a stable column, not by name. Test: a row whose
  `model_version` differs from its name's suffix.
- P1.R Review gate for Phase 1 (code-review-expert and substantive-critic;
  "Ship when the three gates are green at pins that match a fresh census").

**Phase 2: readers move to the row**

- P2.1 Non-stranding tests first: recovery bundle import, migrate-fallback,
  legacy-collection migration, ETL import, each with a row-less legacy name.
  Blocks P2.2 to P2.8.
- P2.2 `resolve_collection`, `find_collection` and the `legacy_names`
  module; move the parsers; classify every remaining caller.
- P2.3 Write paths: chunk metadata model and indexer target model from the
  row (Gap 1). Includes the per-document HTTP call count test for Critical
  Assumption 2.
- P2.4 Score calibration and taxonomy thresholds from the row.
- P2.5 Engine: explicit `lifecycle_state` at registration; lifecycle reads
  from the row in `QuarantineOrigin`, `ChunkReaper`, `VectorHandler`,
  `PgVectorRepository`; client `_BYPASS_SCHEMA_PREFIXES` reads the row.
- P2.6 Changeset after `vectors-030`: `chash_conformance_report`,
  `quarantine_restore_chunks`, `reaper_expire_client_quarantine` read
  columns. Under `PlpgsqlCheckGateTest`.
- P2.7 Retire `repo_identity.list_sibling_collections`' name matcher (no
  caller in `src`; RDR-210 already finds siblings by row tuple) and move
  `resolve_corpus` stage 3 to the row tuple.
- P2.8 Subject and repo resolution by attributes (item 5):
  `t3_collection_name` finds a knowledge subject through `find_collection`;
  `collection_for_repo` uses the returned row instead of rendering. Test: a
  knowledge collection whose label is not of the conformant shape is found
  by its subject.
- P2.R Review gate for Phase 2. Engine cut for P2.5 and P2.6; client floor
  bumps in the release that ships P2.3 onward.

**Phase 3: the surrogate key and the label**

- P3.1 Expand: `catalog_collections.collection_id` with UNIQUE `(tenant_id,
  collection_id)`; `collection_id` columns on every referencing table
  (item 6 list); engine dual-writes; per-tenant bounded backfill; a census
  test that counts null ids per table.
- P3.2 Spike: time the chunks and centroid re-key and the per-collection
  index rebuild on a PITR fork of the larger production tenant (Critical
  Assumption 5). Result in T2 and on the bead; it sets the window and the
  batch size for P3.3 and P3.4.
- P3.3 Switch the foreign keys: the eight registry foreign keys and the
  RDR-225 composite onto `collection_id`, `NOT VALID` then validated; the
  chunks and centroid primary keys and their three children rebuilt per
  leaf. Walked on a PITR fork before the cloud deploy.
- P3.4 Rebuild every per-collection index on `collection_id` (predicate and
  name hash); `PciCatalog` reads the id back from the predicate.
- P3.5 Engine readers and routes: `CollectionRegistry` keyed by id with a
  label index; every collection-taking route accepts `collection_id` and a
  label; responses carry both. Client caches keyed by id.
- P3.6 Label rule: partial UNIQUE on live `(tenant_id, name)`;
  `validate_collection_name` becomes the label rule (item 7). Test: a
  100-character label and a non-conformant label are accepted, a control
  character is refused, a second live row with one label is refused, a
  superseded row's label can be reused.
- P3.7 Rename becomes a one-row label update plus the note-URI rewrite;
  history tables stop being rewritten. Test: a rename leaves chunk,
  manifest and taxonomy rows untouched and the per-collection index in
  place.
- P3.8 Minting (item 8): the engine mints `c<id>` when no label is given;
  every client render-for-new-collection path registers by attributes
  instead; the strict conformant guard in `t3.py`, the rename gate and the
  indexer reroute are removed. Registration requires explicit attributes
  (`http_catalog_client.py:3145-3151`). Stop writing
  `legacy_grandfathered`; drop the `for_tuple` filter on it.
- P3.9 Recovery bundle format carries the registry row and `collection_id`;
  import re-maps ids and checks label clashes. The release that ships it
  starts the N-minor-release count (item 10, Open Item O1).
- P3.10 Rewrite `docs/collections.md` Rule 3 and `docs/storage-tiers.md` §
  Collections for labels, minted ids and attribute lookup.
- P3.R Review gate for Phase 3. Engine cuts for P3.1, P3.3 to P3.5; client
  floor bumps in the release that first depends on each.

**Phase 4: contract and retire**

- P4.1 Primary key to `(tenant_id, collection_id)`; drop the name columns on
  referencing tables, the `(tenant_id, name, embedding_model)` UNIQUE and
  `superseded_by` (replaced by `superseded_by_id`).
- P4.2 Drop `legacy_grandfathered` and `idx_catalog_collections_legacy`.
- P4.3 Delete every parser outside `legacy_names`; both census gates fall to
  the migration-source and diagnostic floor.
- P4.4 At minor release N+1 after P3.9 (Open Item O1): old bundles refused with the
  re-export message naming the last version that imports them;
  `legacy_names` loses its bundle callers.
- P4.G Phase-review gate across all four phases before close.

### Phase dependencies

P1.2 blocks all of Phase 2. P2.1 blocks P2.2 to P2.8. P3.1 waits for Phase
2. P3.2 blocks P3.3 and P3.4. P3.3 blocks P3.5 and P3.7. P3.8 waits for
P2.8. Phase 4 waits for every Phase 3 bead to be deployed on both
production tenants; P4.4 waits for its release count.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Census pins (client, engine, SQL) | In scope (the tests print their tables) | In scope | N/A | In scope (`-m lint` and the Java gate tests) | N/A |
| `lifecycle_state` at registration | `nx collection list` | `nx catalog show` | Existing lifecycle verbs | `nx doctor` row versus name diagnostic | Recovery bundle |
| `collection_id` and label | `nx collection list` (id, label, attributes) | `nx catalog show` | `nx collection delete` (by label or id) | `nx doctor` null-id and label-clash census | Recovery bundle (carries the row from P3.9) |

### New Dependencies

None.

## Test Plan

- **Scenario**: a collection whose row's model differs from its name's
  segment is written to — **Verify**: chunk metadata carries the row's
  model.
- **Scenario**: a repo collection whose `owner_id` was set from tumblers —
  **Verify**: sibling lookup finds its siblings by row owner, not name owner.
- **Scenario**: the reaper quarantines chunks — **Verify**: the sibling row
  has `lifecycle_state = 'quarantine'` and the registration code path takes
  no input from the prefix (asserted by registering a fixture quarantine
  sibling under a non-prefixed name).
- **Scenario**: a recovery bundle recorded under a legacy two-segment and a
  four-segment name is restored after each Phase 2 and Phase 3 bead —
  **Verify**: it restores with the same attributes as before.
- **Scenario**: a new raw parse site of each shape is added to a fixture
  file — **Verify**: the client census fails, naming the shape.
- **Scenario**: `chash_conformance_report` before and after the rewrite on
  the same seeded data — **Verify**: identical rows.
- **Scenario**: rename a collection after P3.7 — **Verify**: one registry
  row changes; chunk, manifest, taxonomy and history rows and the
  per-collection index are untouched; search by the new label returns the
  same results.
- **Scenario**: `store_put` to a new knowledge subject after P3.8 —
  **Verify**: the registered collection has a minted `c<id>` label, the
  subject in `owner_id`, and a second `store_put` to the same subject finds
  it without rendering a name.
- **Scenario**: a recovery bundle in the new format imported into an
  installation where its label is live — **Verify**: that collection is
  refused naming both labels; the others import with re-mapped ids.

## Validation

### Testing Strategy

The census gates are the acceptance signal for Phases 1, 2 and 4: each bead
lowers a pin and the gate refuses a regression. Phase 3 adds the null-id
census per table and the `NOT VALID` then validate step as its acceptance
signal. Behaviour is pinned by the scenarios above, all against a real
engine substrate (no mocks of the registry).

### Performance Expectations

No new query on a hot path is intended: the row cache already serves corpus
resolution. Critical Assumption 2 states the one place this could fail (a
write path naming a collection the cache does not hold) and P2.3 measures
it. The chunks key shrinks from a text name to a bigint, so the chunks
primary key and its children get smaller; the one-off cost is the re-key
and index rebuild, measured in P3.2.

## Decisions (Sam)

Sam answered the draft's open questions on 2026-10-18 (bead nexus-j4gl6
comments) and again on 2026-11-08 (asks `rdr229-q1` to `rdr229-q5` on the
threads board, recorded through halcyon's session).

- **D1** (Q1): go on to Phases 3 and 4; RDR-204's rejected Phase 4 is
  reopened. "It's painful, but with good planning/execution (strangler
  pattern, etc), this should not be an issue."
- **D2** (Q2): `nx` mints an opaque generated id for a new collection, with
  the attributes shown beside it. Existing names are not renamed. Items 7
  and 8.
- **D3** (Q3): a surrogate `collection_id` is the key, and every
  collection-scoped table references it. This reverses the draft's
  rejection of Alternative 2 (now the chosen design). Item 6.
- **D4** (Q4): the recovery bundle carries the registry row, and the name
  fallback stays for a stated number of releases, then retires (item 10).
  The number is Open Item O1.
- **D5** (Q5): stop writing `legacy_grandfathered` in Phase 3; drop the
  column in Phase 4. Item 11.
- Q6 (order with RDR-210) is moot: RDR-210 closed first and finds siblings
  by row tuple.

## Open Item for Sam

- **O1. How many minor releases does the old-bundle fallback last?** D4
  says a stated number; Sam sets it at the gate. Proposed: 20 minor
  releases after the release that ships the new bundle format (P3.9).
  conexus shipped 36 releases in the 30 days to 2026-11-07, so 20 is about a
  month at that rate; a smaller number shortens the window in which a
  recovery bundle from a lost installation restores on a current release,
  and the refusal message names the last version that still imports one
  either way.

## Finalization Gate

### Contradiction Check

None known. The draft's tension with RDR-204's 2026-09-09 ruling is
resolved by D1. The draft's rejection of a surrogate key is reversed by D3,
and Alternative 2 now records the rejected name-as-key design.

### Assumption Verification

Four Critical Assumptions are unverified, and each has a bead that verifies
it before the work that depends on it (P1.2 for 1 and 6, P2.3 for 2, P3.2
for 5).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `GET /v1/catalog/collections/list` with `content_type` and `lifecycle_state` filters | nexus engine | Source Search (RDR-204 Phase 2) |
| `GET /v1/catalog/collections/for_tuple` | nexus engine | Source Search (`CatalogRepository.java:10529`, `http_catalog_client.py:3612-3657`) |
| `catalog_document_chunks.embedding_model` | nexus schema | Source Search (`vectors-030` lines 786-791) |
| `ALTER TABLE ... ADD CONSTRAINT ... NOT VALID` then `VALIDATE CONSTRAINT` on a partitioned referencing table | PostgreSQL 17 | Source Search (fk-002 and vectors-030 already use it on `nexus.chunks`) |

### Scope Verification

The Minimum Viable Validation runs in the local-service gate during Phase 2
and again after Phase 3, and is not deferred.

### Cross-Cutting Concerns

- **Versioning**: P2.5, P2.6 and the Phase 3 engine beads ride engine cuts;
  the client floor bumps in the release that first depends on each, engine
  deployed first. Routes accept both a label and an id from P3.5, so an
  older client keeps working until the floor bump.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: every changeset is walked on a PITR fork rehearsal
  before the cloud deploy. The P3.3 key switch and the P3.4 index rebuild
  run in a maintenance window sized by P3.2.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phases 1 and 2 change no schema key and no name.
  Each Phase 3 step leaves the name path working until Phase 4.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: N/A.

### Proportionality

The schema work is now large: a new key on the registry, eight foreign keys
and two partitioned primary keys moved, every per-collection index rebuilt,
and a route contract that accepts ids. That is what D3 asks for, and it is
the only way a label becomes free to change. The client side is as before:
large, and moved under a ratchet.

## References

- Bead nexus-j4gl6 (description, notes, and Sam's 2026-10-18 rulings);
  nexus-zvcou (closed).
- Asks `rdr229-q1` to `rdr229-q5` on threads/halcyon (2026-11-08).
- `docs/rdr/rdr-204-embedding-profile-and-collection-authority.md`, Phase 3
  and Phase 4 sections.
- `docs/rdr/rdr-225-vector-tables-per-embedding-model.md`, Technical Design.
- `docs/rdr/rdr-227-per-collection-hnsw-above-the-router-threshold.md`.
- `docs/rdr/rdr-210-dual-mode-local-engine.md`, Gap 4.
- `docs/collections.md`, Rules 3 and 6.
- T2 `nexus_rdr/229-research-1` to `-16`.

## Revision History

- 2026-10-17: Drafted from bead nexus-j4gl6 on develop `2ad1f90c9`.
- 2026-11-08: Revised for Sam's rulings D1 to D5 on develop `1fcfaf57b`:
  Phases 3 and 4 kept; a surrogate `collection_id` replaces the name as the
  key (Alternative 2 reversed); the name becomes a label unique among a
  tenant's live collections; new collections get a minted opaque label;
  subject and repo lookups by attributes (Gaps 6 and 7, design items 5 to
  11); recovery bundles carry the row, with the fallback's length left as Open
  Item O1 (proposed 20 minor releases);
  `legacy_grandfathered` dropped; the open questions became Decisions; new
  surrogate-key census (T2 `229-research-14` to `-16`).
