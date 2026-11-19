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
related_rdrs: [RDR-101, RDR-103, RDR-155, RDR-156, RDR-176, RDR-204, RDR-210, RDR-225, RDR-227, RDR-231, RDR-236, RDR-246, RDR-247, RDR-248]
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

Bead nexus-j4gl6 (Sam, 2026-06-29) asked to stop encoding those facts in the
name, and to plan the work fully: "no need to encode anything in the name...
given we control the horizontal AND the vertical, revisit these constraints."
Its trigger, the production local-to-managed cutover, has passed (Sam's
2026-10-17 grooming). The bead was closed on 2026-11-10, "folded into RDR-247
(nexus-f3yqd.8 ...) Superseded here", and Sam's 2026-11-12 ruling then
deferred RDR-247 until this RDR is ready (Relationship to Prior RDRs). No
bead tracks RDR-229 now; this RDR is its record until its beads are planned
at acceptance (T2 `229-research-31`, `229-research-35`).

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
and `_legacy_content_type_for_collection` still parse the name. `nx index
repo` takes its code and docs target model from the name
(`indexer.py:5408-5409`, `:5496-5498`), and the taxonomy projection
thresholds take their content type from it. Since the draft, note writes,
`nx index md`/`pdf` and score calibration read the row first, but each
falls back to a name parse when the row read answers nothing
(`embedding_model_row_preferred`, `doc_indexer.py:1600-1612`,
`scoring.py:176-182`; T2 `229-research-20`). The owner segment is worse than stale: for
repo collections the engine deliberately overwrites the row's `owner_id`
with a tumbler-derived value that differs from the name (nexus-6pbwx), so a
reader that takes the owner from the name reads a value the row has
overridden.

Other sites read no attribute out of the name. They route behaviour by its
content-type prefix. There are 23 client sites: tests against prefix
constants, loops over prefix-keyed tables (`_REGISTRY`, `_PREFIX_TO_KEY`,
`_PREFIX_CONTENT_TYPES`, `CHROMA_IDENTITY_FIELD`), a literal prefix tuple,
and the shipped default taxonomy exclude pattern `"code__*"`. The
`taxonomy__` half of `_BYPASS_SCHEMA_PREFIXES` adds 12 more sites, and the
engine's `NoteUri.isFileRoutedCollection` tests a live collection at one
site (T2 `229-research-34`, `229-research-39`, `229-research-42`). Apart
from these, `aspect_readers.is_note_uri` and the engine's `NoteUri.isNote`
classify a URI string over a frozen list. They stay as they are once a
rename stops moving `chroma://` residue (item 12). A minted label carries no prefix. A
minted collection would therefore lose its aspect extractor, its quality
boost, its search threshold and over-fetch, its chunk content type and its
file-routed identity.

#### Gap 2: Lifecycle state is still read from the `quarantine-` name prefix

RDR-204 Gap 4 replaced the `quarantine-` prefix with a `lifecycle_state`
column for corpus resolution. The engine still infers `lifecycle_state` from
the prefix when it registers a row, still guards quarantine operations by the
prefix (`QuarantineOrigin.isQuarantineName`), routes reaper work by it, and
two live SQL functions refuse or select by it (the Java reads it in 12
places; T2 `229-research-22`, `-23`). The client skips quarantine collections by a
prefix tuple in twelve places (T2 `229-research-21`). A collection's lifecycle is a
column; the prefix is a second source of truth for it.

#### Gap 3: New writes still have to have the conformant shape

`get_or_create_collection` refuses a new non-conformant name
(`src/nexus/db/t3.py:671-684`, T2 `229-research-18`); `nx catalog rename-collection` refuses a
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
counts Java string literals that contain `__` (it pins three, RDR-231's
`NoteUri.FILE_ROUTED_PREFIXES`; T2 `229-research-17`); it cannot see the
`quarantine-` prefix and does not scan SQL at all. Both gates are green while
most of the surface in the census is unguarded.

#### Gap 5: Retiring name parsing can strand a migration path

Some code legitimately reads a name that has no row on this install yet:
restoring a recovery bundle (whose format carries the recorded name, not the
source row), `nx catalog migrate-fallback`, the indexer's legacy-collection
migration, and the engine's ETL import. (The tenant restore of RDR-248's
`nx backup` loads rows as they were exported and parses no name, so it is
not one of these; its key model is item 10's and P3.11's. T2
`229-research-41`.) A reader repoint that removes their
name-parse fallback before they have another source would break the upgrade
and restore paths. The bead names this hazard directly: the reader cutover
must not be able to strand a migration path.

#### Gap 6: The name is the reference key of the vector and taxonomy schema

`catalog_collections` is keyed `(tenant_id, name)`. Eight live foreign keys
point at that key or at the RDR-225 composite `(tenant_id, name,
embedding_model)`. The name also sits inside two primary keys, the chunks
table's `(tenant_id, collection, chash, embedding_model)` and
`taxonomy_centroids`', and three more foreign keys reach the name through the
chunks key (the manifest, `topic_assignments`, `chunk_orphaned_at`). Sixteen
further columns in thirteen tables hold a name with no foreign key at all
(T2 `229-research-27`). The
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
| RDR-225 (one vector table per embedding model) | Adjacent, closed 2026-10-22 (T2 `229-research-25`) | Keys every chunk on `(tenant_id, collection, embedding_model)` through a composite foreign key to `catalog_collections`, written from the row, never the name. Phase 3 here moves that key to `(tenant_id, collection_id, embedding_model)`, which targets a new registry UNIQUE on those three columns (item 6, P3.1); the partitioning (by model, then tenant) does not change, because no partition key holds the name. |
| RDR-246 (data moves are background jobs) | Dependency, accepted 2026-11-16 | Its `nexus.data_moves` registry and `DataMoveWorker` run Phase 3's stamp and key swap as visible, resumable background moves, never at boot (item 6; T2 `229-research-32`). Its activation (nexus-f3yqd.2) is a Phase 3 prerequisite. |
| RDR-247 (one index per row) | Downstream, deferred 2026-11-12 | Waits for this RDR and then rides with it as one visible job (Scope boundary with RDR-247). Received nexus-j4gl6 when Sam closed it on 2026-11-10. |
| RDR-236 (chunk-row metadata normalization) | Adjacent, accepted, Phase 4 in progress | Its not-additive Phase 4 cut and Phase 3's dual write both change the chunk write path (Sequencing). |
| RDR-248 (cloud tenant backup) | Adjacent, closed 2026-11-15 | Its tenant export keys `catalog_collections` and the chunks on names; its table model changes with P3.1, P3.3 and P4.1 (item 10, P3.11). |
| RDR-231 (note source URI scheme) | Adjacent, closed 2026-11-10 | Owns `NoteUri` and `note-uri-corpus.json`, which route by the label's prefix until P2.9 (item 12). |
| RDR-227 (per-collection HNSW above the router threshold) | Adjacent, closed 2026-10-12 (T2 `229-research-25`) | Builds each per-collection index as a partial index `WHERE collection = '<name>'` and names it from a hash of model, tenant and name. Phase 3 rebuilds every such index on `collection_id`. |

Searched `docs/rdr/README.md` and the RDR tree for "collection name",
"naming", "conformant", "opaque", "quarantine", "segment" and "sibling". No
other draft overlaps.

**Scope boundary with RDR-210.** RDR-210 owns how a dual-mode engine finds
and fills model siblings. RDR-229 owns where any code learns a collection's
attributes. The interface between them is one rule, which RDR-210 shipped: a
sibling is found by the row tuple `(content_type, owner_id)` through the
catalog list route's filters, never by rendering or matching a name. The old
Open Question 6 (which lands first) is moot.

**Sequencing with RDR-225.** No blocking dependency. RDR-225 is closed
(2026-10-22; nexus-3wh8d and every bead under it are closed, including the
14-day drop of `chunks_retired_225` and its phase-review gate; T2
`229-research-25`). Two ordering rules apply:
every changeset this RDR adds is ordered after `vectors-030` (and, for the
Phase 3 key work, after the latest `vectors-` changeset on develop at the
time, `vectors-062` on 2026-11-19), and a function
`vectors-030` or a later changeset defines (for example
`quarantine_restore_chunks`, last defined in `vectors-053`; T2
`229-research-23`) is redefined
with its four-column write sites intact and stays under
`PlpgsqlCheckGateTest`. The manifest's new `embedding_model` column
(`vectors-030-model-tenant-partition-functions.xml` lines 786-791) lets Phase 2 rewrite `chash_conformance_report` without
parsing.

**Scope boundary with RDR-247.** Sam deferred RDR-247 on 2026-11-12: "keep
both indexes and wait for RDR-229 ... When RDR-229 is ready on its own
merits, RDR-247 rides with it as one sequence, run as a visible job: stamp
collection ids (stamp-before-build), build each per-collection index on the
id, then the bounded drop of the shared leaf graph" (nexus-f3yqd.8, comment
of 2026-11-12; RDR-247 Revision History; T2 `229-research-35`). Phase 3
follows that order. RDR-229 owns the stamp (P3.1) and the per-collection
index rebuild on the id (P3.4). A collection's rows are stamped before its
index is built on the id, so no index is built twice. RDR-247 owns the
bounded drop of the shared leaf graph, which is not in this RDR. The stamp
cannot stop at new rows, the cost comparison's option (c): D3's chunks
primary key holds `collection_id`, so every row must carry one before P3.3c
swaps the key. What (c) adds, stamping a collection's old rows just before
its index build, fits P3.1's order. It is not an alternative to stamping
everything.

**Sequencing with RDR-236, RDR-246, RDR-248 and RDR-231.** RDR-246 (accepted)
supplies the data-move mechanism Phase 3 runs on, and its activation
(nexus-f3yqd.2) is a Phase 3 prerequisite (item 6). RDR-236 (accepted, Phase
4 in progress: "Refuse, retire, sweep (not-additive, one paired cut)")
changes what a chunk write may carry, and Phase 3's dual write changes the
same write path. Inferred, not read: `collection_id` is a column, not a
metadata key RDR-236's allow-list governs, so the two do not conflict, and
their engine cuts are ordered at planning. RDR-248 (closed 2026-11-15)
exports and restores a tenant keyed on names (item 10). RDR-231 (closed
2026-11-10) put the collection label inside every note's
`nx-note://<label>/` URI and owns `NoteUri` (item 12). T2
`229-research-35`.

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

Every census was re-run on develop `020118bf7` (2026-11-19) as a script,
each first calibrated on the tree its old record was measured on. The
client second-ring walk and the SQL census reproduce their old counts
exactly there. The raw-shape census is now a scripted AST walk over
`src/nexus` and `scripts/` (f-string and constant prefix tests, substring
owner matches, the legacy hash suffix, and the existing scanner's shapes
over `scripts/`); on `2ad1f90c9` it finds 31 sites where the hand list had
nine. The surrogate-key census was re-run as a static walk of the included
changelogs on `1fcfaf57b` and `020118bf7` and diffed. Findings are
registered in T2 `nexus_rdr/229-research-17` to `-31`; the records that
still hold (`-9`, `-12`, `-13`) are noted in T2
`nexus_rdr/229-refresh-2026-11-19`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus engine (`service/src/main`) | Yes | Three `__` literals remain, RDR-231's `NoteUri.FILE_ROUTED_PREFIXES`, read as a content-type prefix test in two places that route note handling (T2 `229-research-34`); the `quarantine-` prefix is read in 12 places; three live SQL functions parse names (T2 `229-research-22`, `-23`). |
| nexus client (`src/nexus`, `scripts/`) | Yes | 46 sites under the existing census, 89 second-ring helper calls outside it, 43 raw shapes it cannot match, 33 of them in `src/nexus` (T2 `229-research-19` to `-21`). |
| Liquibase / PostgreSQL | N/A | No new library behaviour is relied on. |

### Key Discoveries

- **✅ Verified** (source search) — RDR-204 shipped most of the bead's steps 1
  and 2, and Sam rejected its opaque-name phase on 2026-09-09 (commit
  `6d9e7d124`), with the reopen trigger "a field report of a mint-time name
  whose segments disagree with the row it registered". *Source: RDR-204 lines
  754-763; T2 `229-research-1`, re-verified in `229-research-17`.*
- **✅ Verified** (source search) — Of the three essential sites the bead
  named, `_is_same_model_passthrough` is deleted (`3ccf25a48`), engine dim
  dispatch reads the registry, and the 4-segment guard is still live at
  `src/nexus/db/t3.py:671-684`, with the rename gate at
  `src/nexus/commands/catalog_cmds/collections.py:287`. *Source: T2
  `229-research-2`, lines updated in `229-research-18`.*
- **✅ Verified** (source search, develop `020118bf7`, 2026-11-19) — **The
  parse-site census: 203 sites on five surfaces** (152 on `2ad1f90c9`).
  This is the count the rest of this RDR cites. *T2 `229-research-31`.*
  1. *Client, inside the existing census* (46 sites, 20 files):
     `collection_shape.py:190/193/197`; `commands/catalog.py:1481/1518/1721/
     1925/1931/1960/2174/2175`; `commands/collection.py:790/970/971/1111/
     1426(x2)/1433/1469`; `db/http_vector_client.py:1316/1360/7499`;
     `db/t3.py:351/858/1347/1791`; `commands/index.py:134/1201/1210/2451`;
     `catalog/chunk_quarantine.py:148/153`;
     `commands/catalog_cmds/reconcile_stale.py:314/386`; and one each at
     `catalog/http_catalog_client.py:4065`,
     `catalog/orphan_backfill.py:390`, `catalog/owner_scope.py:39`,
     `commands/catalog_cmds/doctor.py:1145`,
     `commands/catalog_cmds/integrity.py:651`, `commands/doctor.py:1646`,
     `commands/enrich.py:1768`, `commands/store.py:1045`, `exporter.py:1904`,
     `health.py:11236`, `mcp/core.py:4300`, `search_engine.py:916`.
     *T2 `229-research-19`.*
  2. *Client, second ring outside the census* (89 calls): see T2
     `229-research-20` for the count by helper. Since the draft, note
     writes, the md/pdf indexer and score calibration read the row first and
     parse the name only as a fallback (`catalog/note_write.py:281`,
     `catalog/store_hook.py:197`, `doc_indexer.py:1600-1612`,
     `scoring.py:176-182`). The decisive name-only reads left are
     `T3Database.put`'s chunk metadata `embedding_model`
     (`db/t3.py:857-860`, with no production caller found), `nx index
     repo`'s target model (`indexer.py:5408-5409/5496-5498`) and registration
     model (`indexer.py:5649-5650`), taxonomy thresholds by prefix
     (`commands/taxonomy_cmd.py:2862/2871/3142`,
     `db/t2/http_taxonomy_store.py:2667`), and the next version computed by
     parsing a name (`catalog/http_catalog_client.py:3875`).
  3. *Client, raw shapes the census cannot match* (43 sites: 33 in
     `src/nexus`, 10 in `scripts/`). In `src/nexus`: `corpus.py:1429` and
     `corpus.py:2736-2737` (f-string prefix tests), `repo_identity.py:471-472`
     and `:475` (substring and hash-suffix sibling match), 12
     `_BYPASS_SCHEMA_PREFIXES` prefix tests (`db/t3.py:91`,
     `exporter.py:1847`, `commands/collection.py:783`,
     `commands/catalog_cmds/t3_orphans.py:89`,
     `commands/catalog_cmds/integrity.py:550`,
     `commands/catalog_cmds/collections.py:70/591`,
     `commands/catalog_cmds/doctor.py:438/651/768/911/1096`), 11
     `FILE_ROUTED_PREFIXES` tests (`aspect_readers.py`, `aspect_worker.py`,
     `catalog/recovery_bundle.py`, `commands/catalog_cmds/backfill.py`,
     `exporter.py`), and six tests on other content-type prefix constants
     (`aspect_extractor.py:1024/1039`, `scoring.py:532`, `health.py:12516`,
     `commands/catalog_cmds/doctor.py:1570`,
     `upgrade_ladder/rungs/note_tags_array.py:131`). The routing class
     (item 1) is wider than these shapes. The routing census (T2
     `229-research-39`) also scans loops over prefix tables, literal prefix
     tuples, default glob patterns and client SQL. It finds 23 routing sites
     plus the `taxonomy__` half of the 12 `_BYPASS_SCHEMA_PREFIXES` tests.
     The three `FILE_ROUTED_PREFIXES` tests in `catalog/recovery_bundle.py`
     are migration sources.
     In `scripts/`:
     `rdr-108-je0b-backfill.py:92`, `upgrade_boot_seed.py:67/86`, and seven
     in `bench/`, `experiments/` and `spikes/`. *T2 `229-research-21`.*
  4. *Engine Java* (14 sites). The `quarantine-` prefix (12):
     `CatalogRepository.java:9829-9830` (new-row lifecycle in
     `upsertCollection`), `:10241`, `:13528-13529`
     (`lifecycleStateForImport`); `QuarantineOrigin.java:85/162/171` (the
     predicate itself at `:73`); `VectorHandler.java:2592`;
     `PgVectorRepository.java:7419/7438`; `ChunkReaper.java:280/877/879`.
     Names are rendered with the prefix at `ChunkReaper.java:1035` and
     `PgVectorRepository.java:7549`. A content-type prefix (2):
     `NoteUri.java:178` (`isFileRoutedCollection`, called at
     `CatalogRepository.java:4399`) and `:194`, over
     `FILE_ROUTED_PREFIXES` (`NoteUri.java:22`, RDR-231); both route by
     content type (`NoteUri.isNote` is called at
     `CatalogRepository.java:4206/4215/4393`).
     *T2 `229-research-22`, `229-research-34`.*
  5. *Live SQL* (11 expressions in three functions):
     `nexus.chash_conformance_report(dim)`,
     `vectors-005-repoint-functions-views.xml:1081-1183`, nine
     `split_part(collection, '__', 3)` model IN-lists;
     `nexus.quarantine_restore_chunks`,
     `vectors-053-quarantine-restore-chunk-keys-only.xml:122`;
     `nexus.reaper_expire_client_quarantine`,
     `vectors-056-gc-quarantine-expires-at.xml:404`. *T2
     `229-research-23`.*

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
  RDR-225 leaf names and PCI index names are hashes. *T2 `229-research-8`,
  re-verified in `229-research-24`.*
- **✅ Verified** (source search) — For repo collections the row's
  `owner_id` is set from the documents' tumbler and differs from the name's
  owner segment by design (`catalog-044`, nexus-6pbwx). *T2
  `229-research-9`.*
- **✅ Verified** (source search) — RDR-225 writes each chunk's
  `embedding_model` from the registry row and keys the chunk foreign key on
  `(tenant_id, name, embedding_model)`; a rename stays inside its partition
  when the model is unchanged. *T2 `229-research-10`, re-verified in
  `229-research-25`.*
- **✅ Verified** (source search) — RDR-210 (closed 2026-10-26) finds
  siblings and its re-embed target by the row tuple `(content_type,
  owner_id)`, never by a name. The name matcher
  `repo_identity.list_sibling_collections` has no caller in `src/nexus`.
  *T2 `229-research-26`, superseding `229-research-11`.*
- **✅ Verified** (source search) — nexus-zvcou is closed, shipped under
  RDR-176 as nexus-t9rmg.8, label `verification:passed`. *T2
  `229-research-12`.*
- **✅ Verified** (source search) — The recovery bundle format carries a
  collection's recorded name and no registry row, so its import must read the
  name (`catalog/recovery_bundle.py:582-593`; T2 `229-research-29`).
  `db/embed_migrate.py`, which RDR-204's census listed as a holdout, no
  longer exists.
- **❓ Assumed** (needs a spike) — On every production tenant, every live
  row's `content_type` and `embedding_model` equal what its name parses to.
  RDR-204 measured zero disagreements for the model on 2026-09-06; nobody
  has measured since RDR-225's walk. *T2 `229-research-13`.*
- **✅ Verified** (source search, develop `1fcfaf57b`, 2026-11-08;
  re-run on `020118bf7`, 2026-11-19) — **The surrogate-key census.** Eight live foreign keys target the registry
  (`topic_assignments` fk-002:241 with an ON UPDATE CASCADE that never
  fires, `document_aspects`, `topics`, `taxonomy_meta`, `document_highlights`
  in fk-003, `chunks` vectors-030:1063 with no ON UPDATE, `canary_query_ring`
  vectors-054:90, and the conditional `aspect_extraction_queue`). The name is
  inside the chunks and centroid primary keys (vectors-030:677, :690), and
  three ON UPDATE CASCADE foreign keys reach it through the chunks key
  (vectors-030:1018/1033/1048). Sixteen name columns in thirteen tables have
  no foreign key to the registry, two of them reaching it only through the
  chunks key (item 6 lists all sixteen with a disposition; T2
  `229-research-48`) (the one added since 2026-11-08 is
  `catalog_reconcile_rows.physical_collection`, `catalog-048-reconcile.xml:103`,
  a staging table reaped after 24 hours); four more carriers are not columns
  (quarantine `origin_collection` metadata, quarantine sibling names,
  `nx-note://` source URIs, aspect tasks in the tuple space). No partition
  key or leaf name holds the name. *T2 `229-research-14`, re-run in
  `229-research-27`.*
- **✅ Verified** (source search) — `renameCollectionTxn`
  (`CatalogRepository.java:11134-11484`) never updates a registry name in
  place; it inserts, rewrites fourteen tables, retags and tombstones. The
  RDR-227 per-collection index is keyed by name in its predicate
  (`PciBuilderSession.java:116-118`) and its name hash
  (`PciCatalog.java:644-669`). At least 123 of 361 engine routes carry a
  collection name, always in the query string or body, never in the path;
  none of the routes added since 2026-11-08 carries one. *T2
  `229-research-15`, re-run in `229-research-28`.*
- **✅ Verified** (source search) — A knowledge subject resolves by name
  rendering and probing (`corpus.py:1336-1545`); `nx index repo` parses the
  version from a returned name (`http_catalog_client.py:3875`);
  `collectionForTuple` breaks ties by name and filters on
  `legacy_grandfathered`. Fifteen MCP tools, 49 CLI options and 30 positional
  CLI arguments address collections by name (the option added since
  2026-11-08 is `nx catalog resolve --corpus`). *T2 `229-research-16`,
  re-run in `229-research-29`.*
- **✅ Verified** (measured on a local engine, chas, 2026-11-12) — Stamping
  a new nullable bigint column (the stand-in for `collection_id`) on every
  chunk row costs about 52 min and 24.6 GB of WAL per 1M rows; only 14.5%
  of the updates are HOT, the HNSW graphs do not grow, and fill factor 90
  does not pay for itself. Rewriting one collection's rows just before its
  per-collection index is built costs about the same per row. One
  per-collection index build costs 32-40 min and 52-63 GB of device writes
  per 1M rows. No schema change since the run touches `nexus.chunks`, its
  leaves or `taxonomy_centroids`. The run does not time the per-leaf
  primary-key swap on a production tenant. *T2 `229-research-30`, citing
  T2 `nexus/rdr247-measurement-backfill` and `nexus/rdr247-measurement-M4`.*
- **✅ Verified** (source search) — Bead nexus-j4gl6 is closed (2026-11-10,
  Sam's grooming walk: "folded into RDR-247 ... so index layout and
  collection identity ... are designed together"); RDR-247 is deferred, and
  RDR-246's data-move registry (`nexus.data_moves`) is on develop, with
  per-tenant and per-leaf runs, a verification query and an `on_complete`.
  Phase 3 runs on it (item 6). *T2 `229-research-31`, `229-research-32`.*
- **✅ Verified** (measured) — conexus shipped 36 client releases between
  2026-10-08 and 2026-11-07 (`git tag`), 24 of them minor (`x.y.0`, v7.75.0
  to v7.98.0), so "a stated number of releases" (D4) is weeks, not months,
  at the current rate. *T2 `229-research-36`.*

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
  no extra round trip. A collection with no row yet, or a failed read,
  answers as item 1 says, not with an error. The cache cannot hold a
  collection with no chunk yet: "The listing omits a registered collection
  that holds no chunk yet" (`corpus.embedding_model_row_preferred`; T2
  `229-research-41`). So from P3.8 the registering call returns the row and
  the writer uses it. The name fallback applies only to a legacy name
  registered before P3.8. — **Status**: Unverified —
  **Method**: Source Search of `get_collection_row`'s refresh path plus a
  test that counts HTTP calls per indexed document before and after, on an
  existing collection and a new one (P2.3).
- [x] No code path needs a fact from the name that the row cannot carry,
  except the migration sources in Gap 5 and the URI-string classifiers
  (item 1's fifth class), which read a URI's segment, not a live
  collection. — **Status**: Verified —
  **Method**: Source Search (the parse-site census: every extracted value is
  content type, owner, model, version or the quarantine prefix, each a
  column). The census also holds sites that extract nothing and route by the
  content-type prefix (T2 `229-research-34`, widened in `229-research-39`); the row carries what they
  need too (`content_type`), but they are not repointed by moving a parse
  (item 12, P2.9). The URI-string classifiers (the fifth class) take
  nothing from a row. They read a URI's segment against RDR-231's frozen
  list, which stays complete because no `chroma://` URI carries a
  prefix-free segment once a rename stops moving residue (item 7; T2
  `229-research-42`).
- [x] Nothing in Postgres requires a collection name to fit ChromaDB's
  limits. — **Status**: Verified — **Method**: Source Search (no object is
  named from a collection name; the per-collection index name is a hash).
- [ ] Re-keying `nexus.chunks` and `nexus.taxonomy_centroids` on
  `collection_id` (a new column backfilled on every row, a new primary key
  per leaf, three child foreign keys re-pointed) and rebuilding every
  per-collection index completes as background work (item 6, "How each
  Phase 3 step runs") in a time the operator can see and accept, both on the
  larger production tenant and on a large local store, and the key swap's
  one `on_complete` transaction, the directly validated composite foreign
  key included, holds its locks for a time the operator accepts. Until
  this is measured, the RDR does not claim that no maintenance window is
  needed. — **Status**: Unverified — **Method**: Spike (P3.2, a timed run
  on a PITR fork of that tenant and a sizing on a local store). P3.2 runs
  before P3.1's changeset is final, because the registering INSERT carries
  each move's `chunk_size`, `sleep_ms` and `statement_timeout_ms`. A local
  measurement bounds the backfill and the index rebuild per 1M rows (T2
  `229-research-30`); it does not time the key swap on that tenant.
- [ ] No two live rows of one tenant share a label once renames stop
  tombstoning, so the partial UNIQUE on live labels (item 7) can be
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
does Phase 4 drop the name columns and the parsers. Up to P3.6 the old
path still works, so each of those steps ships on its own and can stop.
From P3.7 the referencing tables' name columns are dead until P4.1 drops
them (item 7), so an engine downgraded past P3.7 reads stale names; the
safe downgrade point is still open (Contradiction Check). On each install, readers leave the name path only once every
stamp move there has completed (item 6, "Readers during the stamp").

Nothing is renamed by any phase. An existing collection's name becomes its
label unchanged. Only collections created after Phase 3 get a minted label.

### Technical Design

**1. One resolver, one parser, five classes of caller.** The client gets a
single read path for a collection's attributes and a single place where a
name may still be parsed.

```text
// Illustrative; verify against corpus.py and mcp_infra.py during implementation
resolve_collection(ref: str | int) -> Row(CollectionAttributes) | NoRow | Unreadable
    # a label or a collection_id; reads the registry row (cached);
    # three outcomes, as the write paths tell them apart today:
    #   Row: the row answers;
    #   NoRow: no row backs the name (the cache also omits a collection
    #     with no chunk yet); from P3.8 the writer uses the row its
    #     registering call returned, and only a legacy name registered
    #     before P3.8 answers with the mint-class derivation, as
    #     embedding_model_row_preferred does;
    #   Unreadable: the read failed; a write answers with the name too,
    #     flagged, so the unchanged-content skip does not judge a stored
    #     stamp against that guess (doc_indexer._target_model, nexus-s8r5o)
    # a strict reader may turn NoRow into CollectionNotRegisteredError
find_collection(content_type, owner_id, embedding_model) -> CollectionAttributes | None
    # by attributes, through GET /v1/catalog/collections/for_tuple
CollectionAttributes: collection_id, label, content_type, owner_id,
    embedding_model, model_version, dimension, lifecycle_state
legacy_names.candidate_attributes(name: str) -> CandidateAttributes | None
    # the only name parser left; callers must be classified
```

Every census site is one of five classes, recorded in the census beside the
site. Three are callers of `legacy_names`: **mint** (the name is the user's
own input or one `nx` is about to register, so no row exists yet; this class
empties at Phase 3, when minting stops rendering names), **migration
source** (the name comes from another install or an older era: recovery
bundle import, migrate-fallback, legacy-collection migration), or
**diagnostic** (doctor, reconcile and shape checks whose purpose is to
report a name that disagrees with its row). The fourth is **routing by
content type**: a site on this install that picks behaviour by the name's
content-type prefix (T2 `229-research-39`). A site that classifies a name
from another install, such as a recovery-bundle entry, is a migration
source, not this class. So this class never calls `legacy_names`. It reads
the row's `content_type` instead (item 12), and the class is empty from
P2.9 on. A user-configured name pattern (`fnmatch` over
`aspects.docs_collections` or the taxonomy exclude list) names labels. It
is not prefix routing and keeps matching the label. Only a shipped default
that is itself a prefix pattern (`"code__*"`) is routing. The fifth is
**URI-string classifiers over a frozen list**. These sites classify a
source_uri string by its collection segment against RDR-231's frozen
`rdr__`/`docs__`/`code__` list: `NoteUri.isNote`, `fileRouted` and
`canonical` in the engine; `aspect_readers.is_note_uri`,
`canonical_note_uri` and `_collection_is_file_routed`
(`aspect_readers.py:126`), and `db/note_uri_pairs.py:69` on the client.
They are not repointed, and they stay at their count (item 12; T2
`229-research-39`, `229-research-42`).
Any other site is a defect the census refuses. RDR-204's
four dispositions (a) to (d) map onto the first three; its class (b),
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
  substring tests. Calls inside `legacy_names` are the floor. For the
  routing class it also matches every shape the routing census scans (T2
  `229-research-39`): `startswith` on a literal prefix tuple, and on a name
  bound by a loop or comprehension over a prefix constant, a prefix-keyed
  dict or its `.items()`/`.keys()`. It also matches a default glob string
  `<type>__*`, client SQL text with `starts_with(..., '<type>__')`, `LIKE
  '<type>...'` or `split_part(..., '__', ...)`, and every `fnmatch` call,
  each labelled as a label pattern, a file path or routing.
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
pruning on tenant leaves are unchanged). The RDR-225 composite foreign key
needs one more: it now references "UNIQUE (tenant_id, name,
embedding_model)" (`vectors-030:126-127`, `:1062-1066`), and PostgreSQL
requires the referenced columns to be "the columns of a non-deferrable
unique or primary key constraint" (T2 `229-research-44`). So P3.1 also adds
UNIQUE `(tenant_id, collection_id, embedding_model)`, and it stays after
Phase 4 as that key's target. At Phase 4 the primary key becomes
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
  `superseded_by_id`, NULL for a live row and the successor's id for a
  tombstone; the table after the history tables, below, gives
  every name column without a registry foreign key its disposition). The
  engine writes both columns on every insert; a
  registered data move fills the old rows (below). The quarantine
  `origin_collection` metadata tag gains an `origin_collection_id` beside it.
- **Switch.** Each foreign key is re-created on `(tenant_id, collection_id)`
  (or `(tenant_id, collection_id, embedding_model)` for the RDR-225
  composite), `NOT VALID` first and validated after by a data move's
  `on_complete` (P3.3a). The exception is the RDR-225 composite from the
  partitioned `nexus.chunks`, which PostgreSQL validates directly (below).
  Each validation is its own move, one per referencing table: the seven
  registry foreign keys from tables that are not partitioned
  (`topic_assignments`, `document_aspects`, `topics`, `taxonomy_meta`,
  `document_highlights`, `canary_query_ring`, and `aspect_extraction_queue`
  where it still exists), and the three child foreign keys into
  `nexus.chunks` (the manifest, `topic_assignments`, `chunk_orphaned_at`;
  vectors-030:1017-1050). Each move's driving table is the referencing
  table. Its template is the stamp's re-stamp of rows whose id is still
  null, which matches no row once the stamp has run. Its verification
  returns rows with a null id, and its `on_complete` is the VALIDATE. A
  template with no DML is not allowed: dml_utils raises "i_sql_text must
  contain <driving_table> exactly once" and the same for
  `<chunking_clause>` (`assert_chunking_template.sql:28-40`; T2
  `229-research-44`). The P3.3a changeset registers the seven registry
  moves. The three child moves are registered by the `nexus.chunks` swap's
  `on_complete` in its own transaction (the `taxonomy_centroids` swap
  registers none), because their constraints exist only after it,
  and a move worked before its constraint exists would fail for good: the
  worker "goes on to the next open move" past a blocked one
  (`DataMoveWorker.java:49`), and an `on_complete` error that is not
  transient "is the move's" (`DataMoveFailureClassifier.java:165-168`).
  Inferred, not read: VALIDATE of a missing constraint raises 42704, and
  an `on_complete` can INSERT into `nexus.data_moves`. Four of these
  moves are chunk-family (`topic_assignments` twice, the manifest,
  `chunk_orphaned_at`), and such an `on_complete` ends the per-collection
  index builders (`VerifyingFinisher.java:40-41`). So P3.4's builds start
  only after every validation move has completed, read by the boot-time
  completion check (below). The
  chunks and centroid primary keys are rebuilt per leaf on `(tenant_id,
  collection_id, chash, embedding_model)` and `(tenant_id, collection_id,
  topic_id, embedding_model)`, and the three child foreign keys follow. Every
  per-collection index is rebuilt with `WHERE collection_id = <id>` and a
  name hashed from model, tenant and id, so a label change never strands
  one. Readers (engine repositories, SQL functions, client caches) key on
  the id, on each install only once every stamp move there has
  `completed_at` set ("Readers during the stamp", below; T2
  `229-research-43`). Every engine route that takes a collection accepts
  `collection_id`; a label is still accepted at the route boundary and
  resolved to the id once, by `CollectionRegistry`, which is keyed by id
  with a label index beside it. Responses carry both.
- **Contract (Phase 4).** The name columns on referencing tables are
  dropped, the `(tenant_id, name, embedding_model)` UNIQUE is dropped, and
  the name-based arms of `COLLECTION_SCOPED_TABLES` go with them. Dropping
  `superseded_by` drops the label index whose predicate reads it, so P4.1
  re-creates that index on `superseded_by_id` (item 7).

**How each Phase 3 step runs (RDR-246).** No Phase 3 step rewrites the rows
of, or builds an index on, a table that grows with user data inside the
boot-time schema walk. That is the rule RDR-246 (accepted 2026-11-16) and the
upgrade-boot write gate (nexus-f3yqd.1) enforce. On those tables a Phase 3
changeset only adds what costs no row rewrite: nullable `collection_id`
columns, constraints added `NOT VALID`, and one INSERT per move into
`nexus.data_moves` ("a changeset registers a move with one INSERT, and
DataMoveWorker (the engine thread) works the open ones",
`datamoves-001-registry.xml`; T2 `229-research-32`). `catalog_collections`
itself is small. Its new identity column and the UNIQUE `(tenant_id,
collection_id)` are ordinary changeset work. The changeset gate raises no
finding for them, because its generated-column rule matches `generated always
as (` and `catalog_collections` is not one of rule 2's boot-scan tables, so
no exemption line is needed (T2 `229-research-41`; inferred, not read: the
identity fill rewrites that small table). Three kinds of work follow, each
while the engine serves:

- *The stamp is a data move.* P3.1's changeset adds, `NOT VALID`, on every
  table in the Expand list, a CHECK that the id is set wherever the name is
  set: `(<name column> IS NULL) = (collection_id IS NULL)`. The exception
  is `superseded_by`, which is `TEXT NOT NULL DEFAULT ''`
  (`catalog-001-baseline.xml:208`) and empty on a live row: the engine
  writes `.set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "")` to make a row current
  (`CatalogRepository.java:9922`) and the new name on a rename's tombstone
  (`:11477`). Its CHECK is `(superseded_by = '') = (superseded_by_id IS
  NULL)`, added `NOT VALID` in P3.1 and validated with the stamp. The
  stamp fills it from the text column: a non-empty `superseded_by`
  resolves to the `collection_id` of the row with the same `tenant_id` and
  name, and `''` stays NULL. Inferred, not read: a tombstone whose
  successor was later deleted names no row, so P1.2 counts those (T2
  `229-research-50`). The
  partitioned `nexus.chunks` already carries a `NOT VALID` CHECK this way
  (`chunks_chash_octet_check`; T2 `229-research-41`). P3.1 then registers one
  move per table. `DataMoveWorker` runs it through dml_utils in primary-key
  chunks, per tenant, and per leaf on the partitioned tables (runs named
  `<label>/<tenant>/<leaf>`). Its verification query returns the tenant's
  rows whose `collection_id` is still null. Its `on_complete` validates the
  CHECK. Its `statement_timeout_ms` bounds each verification run and the
  `on_complete`, and RDR-246 sets the `on_complete`'s `lock_timeout` to
  "min(5 s, `statement_timeout_ms` / 2)". P3.2 sizes the timeout, with
  `chunk_size` and `sleep_ms`. The move reports in `boot_work` as
  `data-move:<label>`, with `on_stop: "resumes from the last committed
  batch"`, so a killed engine resumes at the first unprocessed chunk
  (RDR-246 lines 867-868).
- *Every writer sets the id from P3.1's cut.* Inferred, not read:
  PostgreSQL checks a `NOT VALID` constraint on every inserted and every
  updated row. So an insert without the id fails at once, and so does an
  update of a row the stamp has not reached unless it sets the id. The
  writers are 109 engine Java write sites (jOOQ and raw SQL) and 26 live
  SQL functions across the tables that gain the column. Insert and update
  paths both count, among them the chunk movers (`gc_quarantine_orphans`,
  `gc_restore_rereferenced`, `quarantine_restore_chunks`,
  `reaper_quarantine_chunks`), rename and re-home, `catalog_reconcile_commit`
  and the taxonomy assigners (T2 `229-research-41`). The census found no
  writer of `taxonomy_centroids` because it matched named tables only. One
  writes through a helper: `TaxonomyCentroidRepository.upsertCentroids`
  runs `ctx.insertInto(ct.table())` with `ct = DimTables.CENTROIDS.get(dim)`
  (`TaxonomyCentroidRepository.java:147-156`), the same shape that writes
  chunks at `PgVectorRepository.java:1283`, and `TenantImportLoad.java:256`
  inserts into any table by name (T2 `229-research-48`). Inferred, not
  read: the 109 is a lower bound. P3.1's acceptance re-runs the writer
  census over helper-routed and by-name writes and finds every writer of
  `taxonomy_centroids` before its CHECK lands.
- *Index builds follow the PCI reconciler's pattern.* RDR-246 keeps index
  builds out of a move: `on_complete` holds transactional DDL, "never CREATE
  INDEX CONCURRENTLY" (lines 546-550). The per-leaf unique indexes for the
  new chunks and centroid keys (P3.3b), and every per-collection index on
  the id (P3.4), are built the way the PCI reconciler builds today. The
  per-leaf pass is new code: the reconciler builds per-collection HNSW
  indexes, "USING hnsw ... WHERE collection = %L"
  (`PciBuilderSession.java:115-118`; T2 `229-research-44`). An engine pass
  runs in the background after the engine serves and issues one `CREATE
  INDEX CONCURRENTLY` at a time. It reports in `boot_work` (the reconciler's
  entry is `pci-reconcile`, "building search indexes"). A build ended by a
  database stop, or by a restart that applies a schema change, "is left
  INVALID, and a later pass drops it and rebuilds it from the start"
  (`PciBootWork.java`; T2 `229-research-33`). The per-leaf pass (P3.3b)
  starts only once the boot-time completion check (below) reports every
  stamp move and every P3.3a move complete, because those moves'
  chunk-family `on_complete` ends builds in flight
  (`VerifyingFinisher.java:40-41`). The pass is engine code, not a move,
  and it records its completion by registering the swap. Inferred, not
  read: on each boot it reads every leaf, builds any id unique index that
  is missing or INVALID (`PciCatalog` reads `pg_index.indisvalid`,
  `PciCatalog.java:349`), and once every leaf of a table has a valid one
  and no row carries that table's swap label, its last step inserts the
  swap move's row on the admin connection, as the chunks swap registers
  its child moves. So the swap's verification never runs before the
  indexes exist. Inferred, not read: from P3.3b's cut the helper that
  creates a leaf (`nexus.partition_ensure_leaf`) also creates its id unique
  index, so a leaf added later never lacks one (T2 `229-research-51`). The per-collection indexes on
  the id (P3.4) are built after P3.3c's swap and after every foreign-key
  validation move has completed (Switch, above), and a collection's index
  only once none of its rows has a null `collection_id`. That is the
  stamp-before-build order of Sam's RDR-247 ruling (Scope boundary with
  RDR-247). Built after the swap and the validations, they are not ended
  by those moves' chunk-family `on_complete`, which ends builds in flight
  (RDR-246). So an index is built a second time only after a kill.
- *The switch is a move's `on_complete`.* The partitioned key follows
  PostgreSQL's documented route: `ALTER TABLE ONLY` the parent `ADD PRIMARY
  KEY`, a matching key on each leaf, then `ALTER INDEX ... ATTACH
  PARTITION` for each leaf ("The same technique applies to PRIMARY KEY
  constraints", `ddl-partitioning.html`; T2 `229-research-41`). Each leaf's
  key takes the unique index built concurrently above, through `ADD ...
  USING INDEX`, which "is not currently supported on partitioned tables"
  but is on a leaf. So P3.3c has, per table whose key it swaps
  (`nexus.chunks`, `nexus.taxonomy_centroids`), a move that P3.3b's pass
  registers when that table's indexes are all valid (above), whose driving table
  is that table, run per tenant and leaf. Its template re-stamps rows whose
  id is still null, which is a no-op after P3.1. Inferred, not read:
  dml_utils still walks every chunk of the leaf, one chunked pass. Its
  verification query returns any leaf without a valid unique index on the
  new key, and any row with a null id; neither can be found once the pass
  has registered it after the stamp completed. Its `on_complete` drops the old key
  and the three child foreign keys, adds the new key by that route, and
  re-adds the three child foreign keys `NOT VALID` (their referencing
  tables are not partitioned), and registers the three moves that validate
  them (Switch, above). The outbound composite
  foreign key from `nexus.chunks` to `catalog_collections` cannot be added
  `NOT VALID`, "because PostgreSQL does not take NOT VALID for a partitioned
  referencing table", so it is validated directly in the same `on_complete`
  (vectors-030 lines 1013-1016). The `on_complete` runs as one transaction.
  A lock it cannot get "fails fast as a transient `blocked:` (55P03)", and
  a kill before the commit leaves the old keys in place (RDR-246 lines
  545-547, 914-916).
- *Readers during the stamp.* RDR-246 requires this RDR to say it: "Each
  move's RDR or bead states how readers behave while it runs: read both
  shapes, or keep the feature off until `completed_at` is set" (RDR-246
  lines 895-897). The rule: on each install, readers keep the name path
  until every stamp move on that install has `completed_at` set. A move's
  `completed_at` is "set when the move verified and on_complete ran", one
  row per move (`datamoves-001-registry.xml:30`), and a stamp move counts as
  complete when it, or the move that supersedes it, has it set. Bead order
  is not this guard, because an install can boot a release that holds
  P3.1 to P3.5 at once. T2 `229-research-43`.
  - *SQL readers (P3.5b).* A redefining changeset whose body names no
    chunk-family table carries, for each stamp label, "a Liquibase
    precondition on `nexus.data_moves.completed_at` for that label with
    `onFail="CONTINUE"`, so it is skipped and retried on each boot until the
    move completes" (RDR-246 lines 937-940), and joins the lint's CONTINUE
    allowlist. That form is refused for a body that names `nexus.chunks`, a
    leaf, `catalog_document_chunks`, `topic_assignments` or
    `chunk_orphaned_at`: "a later-release changeset that waits with
    `onFail="CONTINUE"` is refused there" (RDR-246 lines 902-906). Both
    named views read `nexus.chunks` and the manifest (vectors-032:65-67,
    vectors-019:1153-1155). A move cannot do the waiting instead, because a
    verification that still finds old-shape rows is a permanent failure
    (`DataMoveFailureClassifier.java:160`). So a SQL reader that names a
    chunk-family table takes the rule's other option, read both shapes:
    P3.5b redefines it to match a row by `collection_id` where both sides
    carry one and by the name where either does not, and P4.1 removes the
    name arm with the name columns. Inferred, not read: that match is right
    at every point of the stamp, because a rename keeps rewriting the name
    columns until the check below passes (P3.7). Inferred, not read: a
    two-key match can lose an index, so P3.5b measures each such reader's
    plan on a large store.
  - *Java readers (P3.5c), the registry (P3.5a), P3.4's builds and P3.7's
    rename.* These wait on a completion check that the engine makes at
    boot. Only the admin connection may read the registry: "on the admin
    connection only: `nexus_svc` holds no grant on it"
    (`DataMoveRegistry.java:17-18`). No read answers the question today.
    `DataMoveRegistry` reads open moves (`:39-46`) and failed moves
    (`:66-79`). `MoveBootWork` publishes a `data-move:<label>` entry only
    for an open or failed move and finishes it when the move leaves both
    (`MoveBootWork.java:277-299`), so a missing entry does not tell a
    completed move from an unregistered or superseded one. P3.1 adds the
    read: after `SchemaMigrator.migrate` (`Main.java:285`), on the admin
    connection resolved at `Main.java:277`, the engine reads `completed_at`
    for every stamp label and its replacement, and keeps the answer for the
    process. Until the answer is yes, `CollectionRegistry` hands readers the
    name, the jOOQ readers read the name columns, the PCI reconciler builds
    no index on the id (for P3.4 the same read also covers every
    foreign-key validation move), and `renameCollectionTxn` keeps rewriting the
    fourteen `COLLECTION_SCOPED_TABLES`. Inferred, not read: the switch
    takes effect at the first boot after the last stamp move completes,
    the same boot at which a CONTINUE changeset "applies at the first boot
    after the move completes" (RDR-246 lines 942-944). Inferred, not read:
    client caches keyed by id are safe from P3.1, because P3.1's changeset
    fills the registry's identity column.
  - *A blocked move.* A BYO or `--no-pg-bundle` PostgreSQL "gets it from
    its DBA ... Until the step has run, moves report `blocked:`" (RDR-246
    lines 801-805), and nx doctor "shows one red row per `blocked:` or
    `failed:` move" (RDR-246 lines 891-892). On such an install the stamp
    never completes. Readers stay on names, no index is built on the id, a
    rename keeps its fourteen-table rewrite, and Phase 4's drops never apply
    (P4.1).

Whether any step needs a maintenance window is unverified (Critical
Assumption 5). Inferred, not read: the swap's DDL holds ACCESS EXCLUSIVE on
the table it alters, and the directly validated composite foreign key scans
`nexus.chunks` while that lock is held. `lock_timeout` bounds the wait for a
lock, not how long it is held. P3.2 times the swap, the foreign-key
validations included, before the move's `statement_timeout_ms` is set.

The history tables (`relevance_log`, `search_telemetry`, `hook_failures`,
`gc_audit`) gain a nullable `collection_id` for joins and keep their name
column as a record of the label at the time of the event. A rename stops
rewriting them; their old rows keep the old label, which is what a log
should say.

Every name column without a foreign key to the registry, from a census of
the included changelogs (24 live name columns, 16 of them in 13 tables
without such a key; T2 `229-research-48`). "Expand" means a `collection_id`
beside the name (one per name column on a two-name table; inferred, not
read) under P3.1's CHECK and stamp. "Rename" says whether
`renameCollectionTxn` rewrites the column today; from P3.7 it rewrites none
of them.

| Column | Reaches the registry | Disposition | Rename today |
| --- | --- | --- | --- |
| `catalog_collections.superseded_by` | no | Expand as `superseded_by_id` (NULL when live, the successor's id on a tombstone), under its own CHECK `(superseded_by = '') = (superseded_by_id IS NULL)`; dropped at P4.1 | written on the tombstone |
| `catalog_document_chunks.collection` | through the chunks key | Expand; child key switched in P3.3c | rewritten |
| `chunk_orphaned_at.collection` | through the chunks key | Expand; child key switched in P3.3c | rewritten by its `ON UPDATE CASCADE` from the chunks key (vectors-030:1047-1051) |
| `catalog_documents.physical_collection` | no | Expand | rewritten |
| `catalog_documents.corpus` | no | History label: set at index time (`corpus=collection_name`, `indexer.py:2852`, or a user `--corpus`), kept as written | no |
| `catalog_reconcile_rows.physical_collection` | no | Transient: a staging table reaped after 24 hours (`catalog-048-reconcile.xml:103`); no id | no |
| `chash_remap.source_collection`, `.target_collection` | no | Expand | no |
| `pdf_pipeline.collection` | no | Expand | no |
| `reembed_jobs.source_collection`, `.target_collection` | no | Expand | no |
| `taxonomy_centroids.collection` | no | Expand; primary key rebuilt in P3.3c | rewritten |
| `relevance_log.collection` | no | History label with a nullable id | rewritten until P3.7 |
| `search_telemetry.collection` | no | History label with a nullable id | rewritten until P3.7 |
| `hook_failures.collection` | no | History label with a nullable id | rewritten until P3.7 |
| `gc_audit.collection` | no | History label with a nullable id | rewritten until P3.7 |

**7. The label and what uniqueness it keeps.** The `name` column becomes
the label: the string a person types and reads, and what the 15 MCP tools,
the 49 CLI options and the 30 positional CLI arguments accept (T2
`229-research-29`). It is no
longer a key. No foreign key targets it, no index is named from it, and no
partition holds it. It keeps one uniqueness rule, for addressing only: **a
label is unique among a tenant's live collections**, enforced by a partial
UNIQUE index on `(tenant_id, name)` over rows that are not superseded. Two
collections of one tenant can no longer answer to the same label, so a
label in a command names one collection without ambiguity. From P4.1, a
superseded (tombstoned) row keeps its label for history and releases it, so
the label can be reused. P3.6's index reads `superseded_by` (`TEXT NOT NULL
DEFAULT ''`, `catalog-001-baseline.xml:208`), and P4.1 drops that column.
PostgreSQL drops "Indexes and table constraints involving the column"
with it (T2 `229-research-45`). So P4.1 re-creates the partial UNIQUE over
`superseded_by_id IS NULL` in the same changeset as the primary-key swap.
Inferred, not read: a predicate column counts as involved. That predicate
selects exactly the live rows, because `superseded_by_id` is NULL on a
live row and set on a tombstone, under the CHECK `(superseded_by = '') =
(superseded_by_id IS NULL)` that the stamp validates (item 6; T2
`229-research-50`). Until P4.1 the name is still the primary key
(`PRIMARY KEY (tenant_id, name)`, `catalog-001-baseline.xml:211`; T2
`229-research-36`), so a superseded row, including one a past rename left,
keeps its name taken. Model siblings no longer collide, because each sibling gets
its own minted label. Labels in different tenants are independent, as
names are today.

The label rule replaces ChromaDB's limits in `validate_collection_name`:
non-empty, valid UTF-8 without control characters, at most 255 bytes, and
not of the minted form (item 8) unless the engine minted it, so a user
cannot claim a label that a later mint would produce. The rule may drop the
conformant shape only because, after P2.9, no site in the widened routing
census (item 4, P1.1) routes by the label's prefix (item 12). P3.6 waits
for it. Whether the alphabet excludes `/` is still open (Contradiction
Check), and no audit yet covers where a label meets a file path. P3.6's
tests exercise one label with a space, `%`, `:` and a non-ASCII character
on Windows (Cross-Cutting, Windows).

In the registry, a rename becomes `UPDATE catalog_collections SET name =
<new>` on one row; the note-URI and aspect-task rewrites below still touch
the collection's notes and queued tasks.
`renameCollectionTxn` stops inserting, rewriting fourteen tables and
tombstoning. It still rewrites the `nx-note://<label>/` source URIs of the
collection's notes, as it does today; moving note identity off the label is
a catalog-identity change outside this RDR. From P3.6, when a prefix-free
label first becomes possible, a rename no longer rewrites `chroma://`
URIs. Today `NoteUri.moveTo` rewrites "{@code chroma://<old>/} ... {@code
chroma://<new>/}", and `namesCollection` selects both schemes (T2
`229-research-40`, `229-research-42`). From P3.6, `moveTo` on the rename
path and `renameCollectionTxn` rewrite only `nx-note://` URIs, so
`chroma://` residue keeps the prefixed segment it was written with, which
keeps the URI-string classifiers' frozen list complete (item 12). A
legacy-spelled note the rename no longer moves becomes a stale note that
the stale-note repair rewrites to `nx-note://<collection>/` (T2
`229-research-42`). It also still rewrites queued
aspect tasks, which name their collection in the task body: "a collection
rename ... has to rewrite or delete the tasks itself, in the same
transaction" (`AspectTaskRefs.java:23-26`, called at
`CatalogRepository.java:11393`; T2 `229-research-37`). The one-row rename needs every
reader off the referencing tables' name columns (P3.5) and every
per-collection index off the name (P3.4). From P3.7 those columns are dead:
unread, and no longer rewritten by a rename, so they hold the label each row
was written under until P4.1 drops them. An engine downgraded to a release
before P3.7 would read them; where a downgrade is safe is still open
(Contradiction Check).

**8. Minting (D2).** When a collection is registered with no label, the
engine takes the next `collection_id` and sets the label to `c` followed by
that id in decimal (`c4812`), in the one insert. It is opaque: it carries
no content type, owner, model or version. Listings show the attributes
beside it (`nx collection list`, `collection_list`, `nx catalog show`).
`nx collection list` already shows content type, owner, model, dimension
and state (`commands/collection.py:91-95`); the id column is new (P3.12). A
user who wants a readable label sets one with `nx collection rename`; a subject or repo collection is found by its attributes
either way (item 5), so a label is never needed to find one. Every client
path that renders a conformant name for a new collection
(`CollectionName.render`, `t3_collection_name`'s write branch, the
indexer's reroute, `collection_for_repo`'s client-side render) stops doing
so and registers by attributes. Existing collections keep their names as
labels. Minting waits for P2.9 and P3.9. P2.9 empties the routing class
that the widened census measures. P3.9 gives the recovery-bundle sites the
bundle's row in place of the recorded name. After both, no site on this
install routes a minted collection by a prefix it does not have (item 12).

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
(Open Item O1; this draft proposes 20, about 25 days at the measured rate
of 24 minor releases in 30 days). From minor release N+1 on it is refused with a message that says to re-export from the source, and,
because a recovery bundle may outlive its source, names the last conexus
version that still imports it.

The tenant backup (RDR-248) is a second serialization of the key, keyed
today on `catalog_collections` `("tenant_id text", "name text")` and the
chunks' foreign key `"tenant_id,name,embedding_model"` (`TenantExport.java:355`,
`:389-390`; T2 `229-research-36`). Its table model changes in the same
engine cut as each schema change it describes: P3.1's columns, P3.3's keys
and P4.1's drops. Otherwise `TenantExportRegistryParityTest` goes red: "A
schema change that the registry does not describe fails here" (T2
`229-research-41`). Until P4.1, `collection_id` is not
`catalog_collections`' primary key. `TenantExport`'s `reassignedId` is "the
primary-key column a restore gives new values from the table's own
sequence", so the id is not a reassigned key there. It is a referenced
column that the restore maps from old values to the values the target
assigns. A backup taken before P3.1 has no `collection_id`. RDR-248 loads
an older file "filling new columns from their defaults". The restore then
fills each row's id from the restored registry row with the same
`(tenant_id, name)`, because P3.1's CHECK refuses a row with a name and no
id; the name columns still exist until P4.1. RDR-248's own rule binds the
Phase 4 column drops: "a migration that drops or retypes an exported column
goes red there and must say what it does to old backups" (lines 580-582).
P4.1 therefore states what it does to a backup taken before P3.1, and
RDR-248's committed fixture is part of its acceptance.

**11. `legacy_grandfathered` goes (D5).** Phase 3 stops writing it: the
client stops deriving it, and `collectionForTuple` drops its
`legacy_grandfathered = false` filter (a grandfathered collection has the
same attributes as any other). Phase 4 drops the column and
`idx_catalog_collections_legacy` with it.

**12. Routing by content type reads the row (closes the fourth class).**
No name parsing comes back for these sites. Each prefix test becomes a test
of the row's `content_type`, read through the source its caller already
has. The routing class is 23 sites plus the `taxonomy__` half of the 12
`_BYPASS_SCHEMA_PREFIXES` tests, 35 client tests, and one engine site,
`NoteUri.isFileRoutedCollection`: 36 in all (T2 `229-research-34`,
`229-research-39`, `229-research-49`):

- *Listing rows in hand.* `health.py:12516` and
  `commands/catalog_cmds/doctor.py:1570` filter `t3.list_collections()`
  rows, which already carry `content_type`; they read it from the row.
- *A collection name on this install.* These read the client row cache
  (`mcp_infra.get_collection_row`): `aspect_readers.uri_for`,
  `_identity_fields_for` and `_legacy_identity_fields_for` (`:254`, `:427`,
  `:440`); `aspect_worker._attributed` and `_is_unattributable`;
  `exporter._stamp_note_keys`; `commands/catalog_cmds/backfill.py:98/103`;
  `aspect_extractor.docs_collection_opted_in`, `extraction_applies_to_source`
  and `select_config` (`:1024`, `:1039`, `:1041`, `:1073`);
  `scoring.apply_quality_boost`; `search_engine._threshold_for_collection`
  and `_overfetch_multiplier` (`:802`, `:906`);
  `metadata_schema.chunk_content_type_for_collection` (`:187`);
  `catalog/manifest_backfill.py:865`; `commands/taxonomy_cmd.py:95`; and
  the `note_tags_array` rung. `exporter.import_collection` reads through its
  `db`'s own row resolver.
- *The `taxonomy__` half of `_BYPASS_SCHEMA_PREFIXES`.* P2.5 moves the
  `quarantine-` half to `lifecycle_state`. P2.9 moves the `taxonomy__` half
  at the same 12 sites to the row. No writer in `src/nexus` creates a
  `taxonomy__` collection, so the half only ever meets legacy names (T2
  `229-research-39`).
- *The shipped default exclude pattern.* `config.py:1391`
  (`"local_exclude_collections": ["code__*"]`) excludes code collections
  from local-mode taxonomy by name. P2.9 replaces that default with a
  content-type rule. User patterns in the same list stay label patterns.
- *Docs opt-in patterns.* `docs_collection_opted_in` keeps matching
  `aspects.docs_collections` patterns against the label through `fnmatch`,
  because a user's patterns name labels. Only its `_DOCS_PREFIX` gate
  (`:1024`) moves to the row.
- *A live collection in the engine's stale-note repair.*
  `NoteUri.isFileRoutedCollection` is called once, at
  `CatalogRepository.java:4399`, with the row's live `physical_collection`,
  not a URI segment (T2 `229-research-42`). It takes the content type from
  `CollectionRegistry.require(ctx, tenant, coll)`. Its caller,
  `classifyStaleNotes`, runs inside `tenantScope.withTenant(tenant, ctx ->
  ...)`, so it holds `ctx` and `tenant`.
- *A bundle entry from another install.* `catalog/recovery_bundle.py:92,
  121, 212`, and its `is_note_uri` call at `:101`, read a name that may
  have no row here. They are migration sources, not routing: they read the
  row from the bundle once P3.9 ships, and `legacy_names` before that,
  under the non-stranding rule (item 9). Minting therefore waits for P3.9
  (item 8).

**URI-string classifiers stay on their frozen list.** The fifth caller
class (item 1) is not repointed. These classify a source_uri string over
RDR-231's frozen prefix list (`rdr__`, `docs__`, `code__`):
- in the engine, `NoteUri.isNote`, its `fileRouted` and `NoteUri.canonical`
  (callers `CatalogRepository.java:4206/4215/4393`);
- on the client, `aspect_readers.is_note_uri`, `canonical_note_uri` and
  `_collection_is_file_routed` (`aspect_readers.py:126`; callers
  `exporter.py:927`, `commands/catalog_cmds/reconcile_stale.py:380`,
  `db/serving_engine_version.py:149`);
- and the SQL in `db/note_uri_pairs.py:69`.

The engine's recorded design is that NoteUri "classifies a source_uri
STRING, not a live collection: a note URI's collection segment can name a
collection that was renamed or never registered, so a CollectionRegistry
lookup cannot answer it" (`CollectionParseGateTest.java:239-247`). The
list stays complete because of two facts and one rule:
- A new collection's file-routed documents get `file://` and its notes
  `nx-note://`, never `chroma://` (`aspect_readers.uri_for`; T2
  `229-research-40`).
- `chroma://` residue exists only for names written before P3.6, which are
  prefixed.
- From P3.6 a rename no longer moves `chroma://` residue (item 7). Residue
  keeps its prefixed segment, and the stale-note repair keeps skipping it
  at `if (!NoteUri.isNote(uri)) continue;` whether or not that segment
  still names a live row (T2 `229-research-42`).

`note-uri-corpus.json` does not gain a content type.

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
| Rename | `CatalogRepository.renameCollectionTxn`, `COLLECTION_SCOPED_TABLES` | Replace: a one-row label update plus the note-URI and aspect-task rewrites (`AspectTaskRefs`); `COLLECTION_SCOPED_TABLES` keeps serving delete and rehome, keyed by id. |
| Per-collection index | `PciBuilderSession`, `PciCatalog` | Change: predicate and name hash on `collection_id`; every existing index rebuilt. |
| Stamp and key swap (Phase 3) | RDR-246: `nexus.data_moves`, `DataMoveWorker`, dml_utils, `MoveBootWork` | Reuse: one registered move per table (per tenant and leaf) for the stamp, one per referencing table for each foreign-key validation (P3.3a, P3.3c), and one whose `on_complete` swaps the keys; no new mover. |
| Per-leaf unique index builds | `PciReconciler`, `PciBuilderSession`, `PciBootWork` | New code in the reconciler's pattern (P3.3b): the reconciler builds per-collection HNSW indexes (`PciBuilderSession.java:115-118`), not per-leaf unique indexes. Background `CREATE INDEX CONCURRENTLY` after serve, `boot_work`, INVALID leftovers dropped and rebuilt. |
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
gates reported done while most of the census's sites were outside them (157
of 203 on develop `020118bf7`; T2 `229-research-31`). The
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
- **Risk**: The chunks re-key and the per-collection index rebuild run for
  hours on a large store. **Mitigation**: none of it runs at boot. The stamp
  is a resumable data move, the builds follow the reconciler's pattern, and
  the swap is one `on_complete` transaction (item 6). Critical Assumption 5
  is timed on a PITR fork and sized on a local store in P3.2 before the
  changesets are final.
- **Risk**: A writer misses the dual write. Under P3.1's `NOT VALID` CHECK
  its insert, or its update of a row the stamp has not reached, fails at
  once (inferred, not read: PostgreSQL checks every new row version), so a
  missed writer is a write failure after P3.1's cut, not a null found at the
  switch. **Mitigation**: P3.1 works from the writer census (109 Java write
  sites, 26 SQL functions; T2 `229-research-41`) and tests each path, and a
  census test counts null ids per table before each switch. That census
  test runs on fixtures. The guard on a user's install is that its readers
  switch only once its own stamp moves have `completed_at` set (item 6,
  "Readers during the stamp").
- **Risk**: The engine change to registration lifecycle reaches a client
  that still relies on the prefix inference. **Mitigation**: the client
  never registers quarantine siblings since nexus-syfes (the engine does,
  from the origin's row), so only engine callers change.
- **Risk**: An old recovery bundle is the only copy of a lost installation
  after its fallback retires. **Mitigation**: the refusal names the last
  conexus version that imports it (item 10).

### Failure Modes

- A strict reader reads a collection with no row: `CollectionNotRegisteredError`
  names the collection and the register call that should have preceded the
  read. Visible, not silent. A write path that finds no row answers with the
  mint-class derivation, as it does today (item 1).
- A write path cannot read the row (a catalog or listing outage): it writes
  with the name's model, flagged, and the unchanged-content skip does not
  re-index a file on that guess. Today's behaviour is kept: "A row read
  that FAILED answers the name too, flagged ``False``: the skip must not
  judge a stored stamp against that guess" (`doc_indexer._target_model`;
  T2 `229-research-36`).
- A new parse site is added outside `legacy_names`: the client census fails
  with the file, line and shape.
- The SQL live-body gate misresolves a function's last definer: the gate's
  non-vacuity assertion requires it to find at least the functions this RDR
  rewrites, so an empty scan fails rather than passing.
- A label clash on a recovery-bundle import: that collection is refused
  with both labels named; the rest of the bundle imports.
- A route receives a label that two live rows hold: impossible in every
  phase. Until P4.1 the name is the primary key. From P4.1 the partial
  UNIQUE on live labels holds it: added in P3.6 over `superseded_by`, and
  re-created by P4.1 over `superseded_by_id IS NULL` in the changeset that
  drops `superseded_by` (item 7). That column is NULL exactly on live rows,
  under its own CHECK (item 6).

## Implementation Plan

### Prerequisites

- [x] Sam's rulings on the open questions (D1 to D5, 2026-10-18 and
  2026-11-08).
- [ ] Critical Assumptions 1 and 6 measured (P1.2).
- [ ] Critical Assumption 5 measured (P3.2) before P3.1's changeset is
  final, and so before the Phase 3 switch changesets: it sets each move's
  `chunk_size`, `sleep_ms` and `statement_timeout_ms`.
- [ ] RDR-246's data-move mechanism active on the cloud before Phase 3
  runs there:
  nexus-f3yqd.2 ("Data moves run as background jobs via dml_utils, never at
  boot") is in progress, with nexus-f3yqd.2.23 ("Phase 2 activation, waiting
  on Sam's go: cloud pg_background install and the first registered move")
  open (T2 `229-research-32`). It gates the first cloud run of P3.1's move,
  not P3.1's registration, so P3.1 can be the first registered move that
  .2.23 waits for. A Phase 3 cut can still reach an install whose moves are
  blocked. There its readers stay on names (item 6, "A blocked move"). A
  BYO or `--no-pg-bundle` PostgreSQL "gets it from its DBA" (RDR-246 lines
  801-802); until then that install is such a case (Cross-Cutting,
  Deployment model).

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
  per site) and re-pin at the measured count. The shapes include every shape
  of the routing census (item 4; T2 `229-research-39`), so the routing
  class is measured, not listed. First live run pasted into the bead.
- P1.2 Read-only census on both production tenants: per live row, row versus
  name for content type, model, version and lifecycle; owner differences
  counted separately as expected; live labels that would clash under a
  partial UNIQUE (Critical Assumption 6); live `(content_type, owner_id,
  embedding_model)` tuples with more than one candidate row, which
  `collectionForTuple` today settles by `NAME ASC` behind a grandfathered
  filter D5 drops; and live names that already match `^c[0-9]+$`, the
  minted form (T2 `229-research-38`); and tombstones whose `superseded_by`
  names no row of their tenant, which the stamp cannot give an id (item 6;
  T2 `229-research-50`). Bounded load, results in T2.
  Blocks P2.2 onward.
- P1.3 Widen the engine Java gate to the `quarantine-` literal; add the SQL
  live-body gate. First live run pasted into the bead.
- P1.4 `http_catalog_client` reads the next version from the `for_tuple`
  row's `model_version`, not by parsing the returned name; `collectionForTuple`
  breaks ties by a stable column, not by name. Test: a row whose
  `model_version` differs from its name's suffix; and a repo with a legacy
  slug collection and a conformant collection on one tuple resolves the same
  collection before and after P1.4, and again after D5's filter drop
  (P3.8).
- P1.R Review gate for Phase 1 (code-review-expert and substantive-critic;
  "Ship when the three gates are green at pins that match a fresh census").

**Phase 2: readers move to the row**

- P2.1 Non-stranding tests first: recovery bundle import, migrate-fallback,
  legacy-collection migration, ETL import, each
  with a row-less legacy name.
  Blocks P2.2 to P2.9.
- P2.2 `resolve_collection`, `find_collection` and the `legacy_names`
  module; move the parsers; classify every remaining caller.
- P2.3 Write paths: chunk metadata model and indexer target model from the
  row (Gap 1), keeping the three outcomes of item 1. Includes the
  per-document HTTP call count test for Critical Assumption 2, on an
  existing collection and on a new one that has no row yet. Test: a
  listing outage during `nx index md` neither fails the write nor
  re-indexes an unchanged file.
- P2.4 Taxonomy thresholds from the row. Score calibration already reads
  the row first and parses the name only when that answers nothing
  (`scoring.py:175-182`), item 1's NoRow outcome, which stays (T2
  `229-research-49`).
- P2.5 Engine: explicit `lifecycle_state` at registration; lifecycle reads
  from the row in `QuarantineOrigin`, `ChunkReaper`, `VectorHandler`,
  `PgVectorRepository`; the `quarantine-` half of the client's
  `_BYPASS_SCHEMA_PREFIXES` tests reads `lifecycle_state` (the `taxonomy__`
  half is P2.9's).
- P2.6 Changeset after `vectors-030` and after each function's last
  definer (`vectors-005`, `vectors-053`, `vectors-056`; T2
  `229-research-23`): `chash_conformance_report`,
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
- P2.9 Routing by content type reads the row (item 12): the 23 routing
  sites and the `taxonomy__` half of the 12 `_BYPASS_SCHEMA_PREFIXES` tests
  (T2 `229-research-39`, listed in item 12), the shipped `"code__*"` exclude
  default, and the engine's `isFileRoutedCollection`, which tests a live
  collection. The URI-string classifiers (`NoteUri.isNote`, `canonical`,
  `is_note_uri`, `canonical_note_uri`, `_collection_is_file_routed`,
  `db/note_uri_pairs.py:69`) are the fifth class and are not moved, and
  `note-uri-corpus.json` is unchanged. The recovery-bundle sites are
  migration sources and wait for P3.9. P1.1's widened census measures the
  routing class at zero. Test, for each content type the routers key on
  (docs, knowledge, rdr, code), with a fixture `c<digits>` label that
  carries no prefix: a docs collection still gets its aspect extractor when
  opted in, its quality boost and its file-routed identity; a knowledge
  collection still gets its base extractor, quality boost and note
  identity; an rdr collection its frontmatter extractor and file-routed
  identity; a code collection its search threshold and over-fetch, its
  chunk content type and the local-mode taxonomy exclusion. On the engine,
  the stale-note repair still tells file residue from notes. Blocks P3.6 and
  P3.8.
- P2.R Review gate for Phase 2. Engine cut for P2.5, P2.6 and P2.9; client
  floor bumps in the release that ships P2.3 onward.

**Phase 3: the surrogate key and the label**

- P3.1 Expand, after P3.2: `catalog_collections.collection_id` (identity)
  with UNIQUE `(tenant_id, collection_id)` and UNIQUE `(tenant_id,
  collection_id, embedding_model)`, the RDR-225 composite's future target
  (item 6), as plain changeset work on a small table; `collection_id` columns on every referencing table (item 6
  list) and, `NOT VALID`, the CHECK `(<name column> IS NULL) =
  (collection_id IS NULL)` on each, except `superseded_by`, whose CHECK is
  `(superseded_by = '') = (superseded_by_id IS NULL)` and whose stamp
  resolves a non-empty name to the successor's id (item 6); every writer (item 6's census: 109 Java
  write sites, 26 SQL functions) sets the id on insert and update from this
  cut, and its acceptance re-runs that census over helper-routed and
  by-name writes and finds every writer of `taxonomy_centroids` before the
  CHECK lands (item 6; `TaxonomyCentroidRepository.java:156` is one); `TenantExport`'s table model in the same cut (P3.11); the stamp
  registered in `nexus.data_moves` per table, run per tenant and leaf, with
  a null-`collection_id` verification query, the CHECK's VALIDATE as its
  `on_complete`, and the `chunk_size`, `sleep_ms` and `statement_timeout_ms`
  P3.2 set; a census test that counts null ids per table; the boot-time
  completion check of the stamp moves (item 6, "Readers during the
  stamp"). Test: an upgrade
  boot registers the move and rewrites no row of a large table; a killed run
  resumes from its last committed batch; an insert and an update without the
  id are refused.
- P3.2 Spike, before P3.1's changeset is final: time the stamp, the chunks
  and centroid key swap (its `on_complete`, with the directly validated
  composite foreign key) and the per-collection index rebuild on a PITR fork
  of the larger production tenant, and size them on a local store of the
  largest size class (Critical Assumption 5). Result in T2 and on the bead;
  it sets each move's `chunk_size`, `sleep_ms` and `statement_timeout_ms`,
  and says whether the swap needs a maintenance window.
- P3.3 Switch the keys, as three beads; "P3.3" elsewhere means all three.
  Walked on a PITR fork before the cloud deploy and on a local store.
  `TenantExport`'s key model changes in P3.3a's and P3.3c's cuts (P3.11).
  - P3.3a Foreign-key repoint: the seven registry foreign keys from tables
    that are not partitioned re-created on `collection_id`, `NOT VALID`, and
    one validation move per referencing table registered in the same
    changeset (driving table that table, template a re-stamp of rows still
    null, the VALIDATE as its `on_complete`; item 6, Switch). Test: each
    move completes and leaves its constraint validated.
  - P3.3b Per-leaf unique-index pass: new engine code in the reconciler's
    pattern builds, per leaf, the unique indexes for the new chunks and
    centroid keys (item 6). It starts once the completion check reports
    every stamp move and every P3.3a move complete, and its last step
    registers each table's P3.3c swap move; the leaf-creating helper
    creates the index on a new leaf. Test: a pass ended mid-build leaves an
    INVALID index that the next pass drops and rebuilds; the swap is never
    registered while a leaf's unique index is missing or INVALID.
  - P3.3c Swap: per table, a move registered by P3.3b's pass (driving table
    that table, template a re-stamp of rows still null) whose `on_complete` swaps the
    primary key by `ALTER TABLE ONLY ... ADD PRIMARY KEY`, per-leaf `ADD ...
    USING INDEX` and `ALTER INDEX ... ATTACH PARTITION`, re-adds the three
    child foreign keys `NOT VALID`, registers the three moves that validate
    them (the chunks swap only), and validates the RDR-225 composite directly (item 6). Test: no
    child validation move exists before its constraint does.
- P3.4 After P3.3: rebuild every per-collection index on `collection_id`
  (predicate and name hash) through the PCI reconciler, which reports in
  `boot_work` and rebuilds a killed build from the start; each collection's
  index is built only once none of its rows has a null id
  (stamp-before-build, Scope boundary with RDR-247), and no build starts
  until every P3.3 validation move has completed, so a chunk-family
  `on_complete` cannot end it (item 6); `PciCatalog` reads the
  id back from the predicate. Test: a collection with a row whose
  `collection_id` is null gets no index on the id. The bounded drop of the
  shared leaf graph that follows is RDR-247's.
- P3.5 Readers switch to the id, as three beads; "P3.5" elsewhere means
  all three. On each install the switch waits for every stamp move there to
  complete (item 6, "Readers during the stamp"): P3.5b by the CONTINUE
  precondition or, on the chunk family, by reading both shapes; P3.5a and
  P3.5c by the boot-time completion check. Test: a store with only half its
  chunks stamped (the stamp move paused part-way) still searches, lists and
  renames correctly after the P3.5 cut, and reads by the id from the first
  boot after the move completes.
  - P3.5a Registry and routes: `CollectionRegistry` keyed by id with a
    label index; every collection-taking route accepts `collection_id` and
    a label; responses carry both. Client caches keyed by id.
  - P3.5b SQL readers: the plpgsql functions, the views keyed on names
    (`collection_vector_stats`, `live_chunks`) and triggers that fire on a
    name column (`catalog-044`'s `AFTER INSERT OR UPDATE OF
    physical_collection, deleted_at ON nexus.catalog_documents`) read the
    id (T2 `229-research-37`).
  - P3.5c Java readers: the jOOQ queries on the name columns read the id.
- P3.6 Label rule, after P2.9: partial UNIQUE on live `(tenant_id, name)`;
  `validate_collection_name` becomes the label rule (item 7). Test: a
  100-character label and a non-conformant label are accepted, a control
  character is refused, a second live row with one label is refused, and a
  user label of the minted form (`c` followed by digits) is refused; on
  Windows, a label holding a space, `%`, `:` and a non-ASCII character is
  registered, listed, searched and renamed (Cross-Cutting, Windows).
  Label reuse after supersession waits for P4.1. The "second live row
  with one label is refused" test stays in the suite through P4.1.
- P3.7 After P3.4 and P3.5: rename becomes a one-row registry update plus the
  note-URI rewrite; history tables and the referencing tables' dead name
  columns stop being rewritten (item 7); queued aspect tasks are still
  rewritten. On each install the one-row rename waits for the boot-time
  completion check, and until it passes the rename keeps its fourteen-table
  rewrite (item 6, "Readers during the stamp"). The rename rewrites only `nx-note://` URIs, never `chroma://`
  residue, a rule in force from P3.6's cut (item 7). Test: a rename leaves
  chunk, manifest and taxonomy rows untouched and the per-collection index
  in place, and a task queued before the rename names the new label; and
  renaming a file-routed collection that has `chroma://` residue to a
  prefix-free label leaves the residue on its old segment, still classed
  file-routed by `NoteUri.isNote` and `is_note_uri`, and skipped by the
  stale-note repair.
- P3.8 Minting (item 8), after P2.9, P3.6 and P3.9: the engine mints `c<id>` when no label is given;
  every client render-for-new-collection path registers by attributes
  instead; the strict conformant guard in `t3.py`, the rename gate and the
  indexer reroute are removed. Registration requires explicit attributes
  (`http_catalog_client.py:3709-3716`; T2 `229-research-29`), and the
  registering call returns the row, which the writer uses for the first
  write because the row cache omits a collection with no chunk yet (T2
  `229-research-41`). Stop writing `legacy_grandfathered`; drop the
  `for_tuple` filter on it. Test: the first `store_put` to a new subject
  stamps the row's model with no name derivation.
- P3.9 Recovery bundle format carries the registry row and `collection_id`;
  import re-maps ids and checks label clashes. The release that ships it
  starts the N-minor-release count (item 10, Open Item O1).
- P3.10 Rewrite `docs/collections.md` Rule 3 and `docs/storage-tiers.md` §
  Collections for labels, minted ids and attribute lookup.
- P3.11 Tenant backup carries the id (item 10), shipped in P3.1's engine
  cut and again with P3.3's key changes: `TenantExport`'s table model gains
  `collection_id` as a referenced column on every table that has it, and
  the restore maps old ids to the target's. A pre-P3.1 backup's rows take
  the id of the restored registry row with the same `(tenant_id, name)`.
  Test: `TenantExportRegistryParityTest` is green at P3.1 and at P3.3; a
  backup taken after P3.1 restores with every chunk on its collection's new
  id; RDR-248's committed fixture, taken before P3.1, still loads with every
  id filled.
- P3.12 Day 2 surfaces, after P3.5a and P3.6: an id column in `nx
  collection list` (it already shows content type, owner, model, dimension
  and state, `commands/collection.py:91-95`), an `nx doctor` census of null
  ids per table and of live labels held by more than one row, and `nx
  collection delete` by label or id (it takes a name today,
  `commands/collection.py:547-550`). T2 `229-research-48`. Test: each
  surface on a fixture with a null id and a minted label.
- P3.R Review gate for Phase 3. Engine cuts for P3.1, P3.3 to P3.5 and P3.11; client
  floor bumps in the release that first depends on each. One release may
  hold several of these cuts, and an install may skip releases, so the
  cut order does not decide when an install's readers switch; its own
  stamp moves' `completed_at` does (item 6, "Readers during the stamp").

**Phase 4: contract and retire**

- P4.1 After P3.11; its acceptance includes RDR-248's committed fixture
  and a stated effect on backups taken before P3.1 (item 10). Primary key
  to `(tenant_id, collection_id)`; drop the name columns on
  referencing tables, the `(tenant_id, name, embedding_model)` UNIQUE and
  `superseded_by` (replaced by `superseded_by_id`). UNIQUE `(tenant_id,
  collection_id, embedding_model)` stays: the RDR-225 composite depends on
  it, and PostgreSQL drops such a constraint only with CASCADE (item 6; T2
  `229-research-44`). Each drop waits for
  this install's stamp moves (item 6, "Readers during the stamp"). On the
  chunk family (`nexus.chunks`, the manifest, `topic_assignments`,
  `chunk_orphaned_at`) a drop is a move's `on_complete`: a destructive
  statement on a table a move drives "must carry a precondition on that
  move's `completed_at` with `onFail="CONTINUE"` ...; on a chunk-family
  table it is refused outright, by the CONTINUE-precondition lint, and the
  step belongs in a move's `on_complete`" (RDR-246 lines 986-992). That
  `on_complete` also removes the name arm of P3.5b's both-shapes readers.
  Inferred, not read: that move's verification, run as the admin role,
  also returns a row for each stamp move without `completed_at`, so it
  fails visibly instead of dropping early. On every other table the
  dropping changeset carries the CONTINUE precondition on every stamp
  label. The changeset that swaps the registry's primary key and drops
  `superseded_by` also re-creates the partial UNIQUE on live labels over
  `superseded_by_id IS NULL` (item 7), which holds because the CHECK
  `(superseded_by = '') = (superseded_by_id IS NULL)` keeps that column NULL
  exactly on live rows. Test: a superseded
  row's label can be reused by a new live collection, and P3.6's test that
  refuses a second live row with one label still passes.
- P4.2 Drop `legacy_grandfathered` and `idx_catalog_collections_legacy`.
- P4.3 Delete every parser outside `legacy_names`; both census gates fall to
  the migration-source and diagnostic floor. The routing class has been
  zero since P2.9, as P1.1's widened census measures it.
- P4.4 At minor release N+1 after P3.9 (Open Item O1): old bundles refused with the
  re-export message naming the last version that imports them;
  `legacy_names` loses its bundle callers.
- P4.G Phase-review gate across all four phases before close.

### Phase dependencies

P1.2 blocks all of Phase 2. P2.1 blocks P2.2 to P2.9. P3.1 waits for Phase
2 and for P3.2; the first cloud run of its move waits for nexus-f3yqd.2.23
(Prerequisites). P3.2 blocks P3.1, P3.3 and P3.4. P3.4 waits for P3.3. Within P3.3, P3.3c waits for P3.3b, whose
indexes its verification requires. P3.12 waits for P3.5a and P3.6. P3.3 blocks P3.5 and P3.7. P3.7 also waits
for P3.4 and P3.5 (item 7). P3.8 waits for P2.8, P2.9, P3.6 (which
enforces the reserved minted form) and P3.9 (the recovery-bundle sites,
item 8); P3.6 waits for P2.9. Phase 4 waits for every Phase 3 bead to be deployed on both
production tenants; P4.4 waits for its release count. These are bead
orders. At run time each install also waits for its own stamp moves:
P3.3b's pass, P3.3c's swap (which that pass registers only once every leaf
has a valid unique index), readers, P3.4's builds, P3.7's rename and P4.1's
drops move only once every stamp move there has `completed_at` set (item 6,
"Readers during the stamp").

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Census pins (client, engine, SQL) | In scope (the tests print their tables) | In scope | N/A | In scope (`-m lint` and the Java gate tests) | N/A |
| `lifecycle_state` at registration | `nx collection list` | `nx catalog show` | Existing lifecycle verbs | `nx doctor` row versus name diagnostic | Recovery bundle |
| `collection_id` and label | `nx collection list` (id, label, attributes; the id column is P3.12's) | `nx catalog show` | `nx collection delete` (by label or id, P3.12) | `nx doctor` null-id and label-clash census (P3.12) | Recovery bundle (carries the row from P3.9); tenant backup, `nx backup` (carries the id from P3.1's cut, P3.11) |

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
- **Scenario**: a docs, a knowledge, an rdr and a code collection, each
  with a minted, prefix-free label, are indexed, searched and repaired
  after P2.9 — **Verify**: each gets the behaviour its content type routes
  to (P2.9's list: extractor, quality boost, search threshold and
  over-fetch, chunk content type, taxonomy exclusion, file-routed or note
  identity), and the engine's stale-note repair does not class file
  residue as notes.
- **Scenario**: a new raw parse site of each shape is added to a fixture
  file — **Verify**: the client census fails, naming the shape.
- **Scenario**: `chash_conformance_report` before and after the rewrite on
  the same seeded data — **Verify**: identical rows.
- **Scenario**: an engine with the P3.5 cut boots on a store with only half
  its chunks stamped (the stamp move paused part-way) — **Verify**: search,
  listings and a rename return what they returned before the cut, and the
  engine reads by the id from the first boot after the move completes.
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
  conexus shipped 36 releases in the 30 days to 2026-11-07, 24 of them
  minor, so 20 minor releases is about 25 days at that rate; a smaller number shortens the window in which a
  recovery bundle from a lost installation restores on a current release,
  and the refusal message names the last version that still imports one
  either way.

## Finalization Gate

### Contradiction Check

The draft's tension with RDR-204's 2026-09-09 ruling is resolved by D1. The
draft's rejection of a surrogate key is reversed by D3, and Alternative 2
now records the rejected name-as-key design. Gate round 1 (2026-11-19) found
these, now resolved in the text:

- Phase 3 written as boot changesets against RDR-246's rule: item 6, "How
  each Phase 3 step runs".
- Sam's 2026-11-12 RDR-247 ruling (stamp before build) against an
  unordered full stamp: Scope boundary with RDR-247.
- nexus-j4gl6, closed into RDR-247, still cited as this RDR's live tracker:
  Problem Statement.
- Label reuse and a one-row rename asserted in Phase 3 while the name is
  still the primary key: item 7 and P4.1.

The round 1 fix check found these, now resolved in the text:

- A routing class counted over one code shape, which P2.9 said it emptied:
  the routing census (T2 `229-research-39`), item 12 and P2.9, measured by
  P1.1.
- Minting allowed before the recovery bundle carries the row: item 8 and
  P3.8 wait for P3.9.
- "No maintenance window" asserted while Critical Assumption 5 was
  unverified: item 6 and Deployment model now leave it to P3.2.
- A `NOT VALID` composite foreign key on the partitioned `nexus.chunks`,
  which PostgreSQL refuses: item 6 validates it directly.

Gate round 2 (2026-11-19) found these, now addressed in the text:

- Readers switched to `collection_id` with no guard that the install's
  stamp had completed, against RDR-246's reader rule: item 6, "Readers
  during the stamp", with P3.1's boot-time completion check, P3.5, P3.7,
  P4.1 and the half-stamped store test (T2 `229-research-43`).
- A composite foreign key with no UNIQUE on its new columns, validations
  named but no moves, and the swap bead holding three kinds of work: P3.1's
  UNIQUE `(tenant_id, collection_id, embedding_model)`, the named
  validation moves, and P3.3 split into P3.3a to P3.3c (T2
  `229-research-44`).
- A label index whose predicate column P4.1 drops: P4.1 re-creates it on
  `superseded_by_id` (item 7; T2 `229-research-45`).
- "At every step the old path still works" against the dead name columns
  from P3.7: the Approach now says "up to P3.6" and points at the open
  downgrade point.
- The RDR-246 prerequisite stated for every install while BYO PostgreSQL
  blocks moves, and no Windows line: Prerequisites and Cross-Cutting (T2
  `229-research-47`).
- Name columns with no disposition, and Day 2 promises with no bead: the
  table in item 6 and P3.12 (T2 `229-research-48`).

Recorded residuals, for Sam at acceptance (gate record
`nexus_rdr/229-gate-latest`; not resolved here):

- "[BLOCKS-PLANNING] chroma:// residue rule covers rename only;
  NoteUri.moveTo's re-home, re-embed and collection-update callers still
  move residue onto prefix-free labels (fix check c1a9d815e8cc) (carried
  from round 1)"
- "[BLOCKS-PLANNING] residue rule stated in force from P3.6 but owned by
  P3.7; P3.6 and P3.8 do not wait for P3.7 (fix check c1a9d815e8cc)
  (carried from round 1)"
- "[BLOCKS-PLANNING] prefix-free labels already possible via
  rename-collection --allow-legacy; "names before P3.6 are prefixed" and
  CA3's Verified rest on it; P1.2 lacks a census of prefix-free chroma://
  segments (fix check c1a9d815e8cc) (carried from round 1)"

The URI-string classifier rule (item 7, item 12, P3.7; T2
`229-research-42`), the P3.7 test that renames a file-routed collection
with `chroma://` residue, and the Test Plan's prefix-free-label row rest on
the rule these residuals question, and are regenerated when Sam
dispositions them.

Still open, for a design call: whether the label alphabet excludes `/`,
which `NoteUri.collectionOf` treats as the end of the collection; what
makes a live `(content_type, owner_id, embedding_model)` tuple unique once
minting removes the name from that job; and the safe engine downgrade
point after P3.3, which matters most from P3.7, when the name columns go
dead (Approach, item 7).

### Assumption Verification

Four Critical Assumptions are unverified, and each has a bead that verifies
it before the work that depends on it (P1.2 for 1 and 6, P2.3 for 2, P3.2
for 5).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `GET /v1/catalog/collections/list` with `content_type` and `lifecycle_state` filters | nexus engine | Source Search (RDR-204 Phase 2) |
| `GET /v1/catalog/collections/for_tuple` | nexus engine | Source Search (`CatalogRepository.java:10500-10553`, `http_catalog_client.py:3845-3891`; T2 `229-research-29`) |
| `catalog_document_chunks.embedding_model` | nexus schema | Source Search (`vectors-030` lines 786-791) |
| `ALTER TABLE ... ADD CONSTRAINT ... NOT VALID` then `VALIDATE CONSTRAINT` on a partitioned referencing table | PostgreSQL 17 | Source Search (fk-002 and vectors-030 already use it on `nexus.chunks`) |

### Scope Verification

The Minimum Viable Validation runs in the local-service gate during Phase 2
and again after Phase 3, and is not deferred. Items that gate round 1 found
with no plan step now have one: routing by content type, 23 sites plus the
`taxonomy__` half of `_BYPASS_SCHEMA_PREFIXES` (P2.9), the recovery-bundle
sites (migration sources, P3.9 before P3.8), the tenant backup (P3.11, in
P3.1's and P3.3's cuts), every writer of the dual write (P3.1), queued
aspect tasks on rename (P3.7), and name-keyed views and triggers (P3.5b).
Items that gate round 2 found with no plan step now have one: the
per-install reader guard (P3.1's completion check, P3.5, P3.7, P4.1), the
foreign-key validation moves (P3.3a, P3.3c), the per-leaf unique-index
pass as new code (P3.3b), the label index after P4.1 (P4.1), blocked moves
on BYO PostgreSQL and the Windows label test (Cross-Cutting, P3.6), the
disposition of all sixteen name columns without a registry foreign key
(item 6's table: `catalog_documents.corpus` is a history label,
`catalog_reconcile_rows.physical_collection` transient), the Day 2
surfaces (P3.12), and the `taxonomy_centroids` writer (P3.1's acceptance;
one is `TaxonomyCentroidRepository.java:156`).
Still without one: the history tables' nullable `collection_id` (item 6)
names no consumer; P1.2 measures the two production tenants, not local
stores; no rule says what a mint does when a live label already has the
minted form (P1.2 only counts such labels); and no audit covers where a
label meets a file path beyond P3.6's Windows test.

### Cross-Cutting Concerns

- **Versioning**: P2.5, P2.6, P2.9 and the Phase 3 engine beads ride engine cuts;
  the client floor bumps in the release that first depends on each, engine
  deployed first. Routes accept both a label and an id from P3.5, so an
  older client keeps working until the floor bump. An install that skips
  releases is guarded by its own stamp moves, not by the cut order: its
  readers switch at the first boot after every stamp move there has
  completed (item 6, "Readers during the stamp").
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: every changeset is walked on a PITR fork rehearsal
  before the cloud deploy, and on a local store. No Phase 3 step rewrites
  the rows of, or builds an index on, a table that grows with user data at
  boot: the stamp and the key swap run as registered data moves, the index
  builds in the PCI reconciler's pattern, each visible in `boot_work` and
  resumable (item 6). Whether the swap's `on_complete` needs a maintenance
  window is unverified until P3.2 times it (Critical Assumption 5).
- **BYO or `--no-pg-bundle` local PostgreSQL**: such an install gets
  pg_background "from its DBA ... Until the step has run, moves report
  `blocked:`" (RDR-246 lines 801-805), and nx doctor shows a red row for
  each (lines 891-892). Per bead: P3.1's changeset still applies (columns,
  `NOT VALID` CHECKs, move registration, the dual write), but every move
  stays blocked (the stamp, P3.3a's and P3.3c's validations and the swap).
  P3.5's readers stay on names, P3.4 builds no index on the id, and P3.7's
  rename keeps its fourteen-table rewrite. P4.1's drops never apply,
  because each waits for the stamp's `completed_at` (item 6, "A blocked
  move"). Inferred, not read: P3.3b's pass is an engine pass, not a move,
  so it still builds; P3.6 and P3.8 touch only the registry and still
  apply. T2 `229-research-47`.
- **Windows**: RDR-246 verified pg_background on the Windows x64 bundle:
  "built for Linux x86_64 and arm64 (manylinux_2_28), macOS arm64 and
  Windows x64. On each, a relocated bundle ran `CREATE EXTENSION` and a
  background worker" (RDR-246 lines 225-227). So a bundled Windows install
  runs the moves. Where a label meets a file path is not audited yet, and
  the label alphabet is open (Contradiction Check). P3.6's tests exercise on Windows one
  label holding a space, `%`, `:` and a non-ASCII character.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phases 1 and 2 change no schema key and no name.
  Each Phase 3 step up to P3.6 leaves the name path working. From P3.7 a
  rename leaves the referencing tables' name columns on the old label, so
  the name path is dead from there until P4.1 drops it (item 7), and the
  safe engine downgrade point is open (Contradiction Check). On each
  install that happens only after its stamp moves have completed, because
  the one-row rename waits for the same check (item 6, "Readers during the
  stamp").
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
- `docs/rdr/rdr-246-data-moves-are-background-jobs.md`, Technical Design.
- `docs/rdr/rdr-247-one-index-per-row.md`, Revision History (2026-11-12
  deferral); bead nexus-f3yqd.8 (Sam's 2026-11-12 ruling);
  T2 `nexus/rdr247-cost-comparison-2026-11-12`.
- `docs/rdr/rdr-236-chunk-row-metadata-normalization.md`, Phase 4.
- `docs/rdr/rdr-248-cloud-tenant-backup.md`, restore and compatibility.
- `docs/rdr/rdr-231-note-source-uri-scheme.md`.
- `docs/collections.md`, Rules 3 and 6.
- T2 `nexus_rdr/229-research-1` to `-49`, and
  `nexus_rdr/229-refresh-2026-11-19` (which records hold, which are
  superseded).
- T2 `nexus/rdr247-measurement-backfill`, `nexus/rdr247-measurement-M4`.

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
- 2026-11-19: Research refreshed on develop `020118bf7` (no design change):
  every census re-run as a script; counts, site lists, file:line citations
  and RDR statuses brought current (the census is 203 sites, was 152); the
  local `collection_id` backfill measurement cited. T2 `229-research-17` to
  `-31` added (`-17` to `-29` supersede `-1` to `-8`, `-10`, `-11` and `-14`
  to `-16`; `-30` measurement; `-31` census total and bead state), with
  `229-refresh-2026-11-19` recording that `-9`, `-12` and `-13` still hold.
- 2026-11-19: Gate round 1 — BLOCKED (2 Critical, 8 Significant, 2 ship-blocker(s)); blob `691241710dec`; critique `nexus_rdr/229-gate-critique-2026-11-19-r1`.
- 2026-11-19: Gate round 1 fixes. C1: Phase 3 runs on RDR-246's data
  moves and the PCI reconciler's build pattern, with no maintenance window.
  C2: a fourth census class, routing by content type, moved to the row by
  P2.9 before P3.6 and P3.8. S1, S3, S5 and S8 fixed; S2 and S6 in part;
  S4 and S7 left for a design call; Observation 3 corrected. The Finalization
  Gate's Contradiction Check and Scope Verification regenerated. T2
  `229-research-32` to `-38` added.
- 2026-11-19: Fix check 1 (`nexus_rdr/229-fix-check-759ec4ba66a9`, FAIL)
  re-fix. The routing census was re-run over every prefix shape (23 sites
  plus the `taxonomy__` half of `_BYPASS_SCHEMA_PREFIXES`). Minting now
  waits for P3.9, and the recovery-bundle sites are migration sources. The
  Phase 3 mechanics are stated as rules: the CHECK's scope and writers, the
  move timeouts, PostgreSQL's partitioned-key route, the direct validation
  of the composite key, the swap move's shape, P3.2 before P3.1 and P3.4
  after P3.3. The "no maintenance window" claim is withdrawn until P3.2.
  The tenant backup ships in P3.1's and P3.3's cuts and is no longer listed
  as a name reader. T2 `229-research-39` to `-41` added.
- 2026-11-19: URI-string classifiers settled (baton's rename rule). From
  P3.6 a rename rewrites only `nx-note://` URIs, so `chroma://` residue
  keeps its prefixed segment. The URI-string classifiers become the fifth
  caller class and stay on RDR-231's frozen list. Round 1's registry route
  and its no-row answer are withdrawn, and `note-uri-corpus.json` is
  unchanged. `isFileRoutedCollection`, which tests a live collection, stays
  in the routing class. T2 `229-research-42` added.
- 2026-11-19: Gate round 2 — BLOCKED (1 Critical, 5 Significant, 1 ship-blocker(s)); blob `c1a9d815e8cc`; critique `nexus_rdr/229-gate-critique-2026-11-19-r2`.
- 2026-11-19: Gate round 2 fixes. C-A: readers keep the name path on each
  install until every stamp move there has `completed_at` set (RDR-246's
  reader rule), by a CONTINUE precondition, a both-shapes read on the chunk
  family, or a boot-time completion check; blocked moves keep readers on
  names and Phase 4's drops unapplied. S-1: UNIQUE `(tenant_id,
  collection_id, embedding_model)` in P3.1, named validation moves, P3.3
  split into P3.3a to P3.3c. S-2: P4.1 re-creates the live-label index on
  `superseded_by_id`. S-3: the Approach says "up to P3.6". S-4: BYO
  PostgreSQL and Windows lines. S-5: a disposition for all sixteen no-FK
  name columns, P3.12 for Day 2, the `taxonomy_centroids` writer in P3.1's
  acceptance. Observations 3, 4 and 6 corrected. The three recorded
  residuals are listed for Sam, not resolved. Contradiction Check and
  Scope Verification regenerated. T2 `229-research-43` to `-49` added.
