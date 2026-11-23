---
title: "The Collection Name Becomes a Label: Retiring What RDR-204 Left Reading the Name"
id: RDR-229
type: Architecture
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-10-17
accepted_date: 2026-11-19
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
content-type prefix. There are 23 client sites (the census measures 24, with `topic_route.py:46`): tests against prefix
constants, loops over prefix-keyed tables (`_REGISTRY`, `_PREFIX_TO_KEY`,
`_PREFIX_CONTENT_TYPES`, `CHROMA_IDENTITY_FIELD`), a literal prefix tuple,
and the shipped default taxonomy exclude pattern `"code__*"`. The
`taxonomy__` half of `_BYPASS_SCHEMA_PREFIXES` adds 12 more sites, and the
engine's `NoteUri.isFileRoutedCollection` tests a live collection at one
site (T2 `229-research-34`, `229-research-39`, `229-research-42`). Apart
from these, `aspect_readers.is_note_uri` and the engine's `NoteUri.isNote`
classify a URI string over a frozen list. They stay as they are once
`NoteUri.moveTo` and `catalog_reconcile_commit` stop moving `chroma://`
residue on every path and the
rows they have already moved onto a prefix-free label are repaired (item 7,
item 12, P2.10). A minted label carries no prefix. A
minted collection would therefore lose its aspect extractor, its quality
boost, its search threshold and over-fetch, its chunk content type and its
file-routed identity.

#### Gap 2: Lifecycle state is still read from the `quarantine-` name prefix

RDR-204 Gap 4 replaced the `quarantine-` prefix with a `lifecycle_state`
column for corpus resolution. The engine still infers `lifecycle_state` from
the prefix when it registers a row, still guards quarantine operations by the
prefix (`QuarantineOrigin.isQuarantineName`), routes reaper work by it, and
two live SQL functions refuse or select by it (the Java reads it in 12
places at the draft; the gates now pin 6 `quarantine-` literals plus the one
exempt constant and 35 reads, `CollectionParseGateTest.java:329`, `:367`, the
35 adding the `requireNotQuarantine` callers and the bypass-set readers, and the
duplicate `ChunkReaper` constant counts; T2 `229-research-22`, `-23`). The client skips quarantine collections by a
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
and restore paths. A fifth migration source is `exporter.import_collection`
(`exporter.py`; the census ledgers its sites as routing and mint today): its
row-less legacy-name test is owed after P2.1 lands (the P1.R critique; inferred,
not read in the bead's note). The bead names this hazard directly: the reader cutover
must not be able to strand a migration path.

#### Gap 6: The name is the reference key of the vector and taxonomy schema

`catalog_collections` is keyed `(tenant_id, name)`. Eight live foreign keys
point at that key or at the RDR-225 composite `(tenant_id, name,
embedding_model)`. The name also sits inside two primary keys, the chunks
table's `(tenant_id, collection, chash, embedding_model)` and
`taxonomy_centroids`', and three more foreign keys reach the name through the
chunks key (the manifest, `topic_assignments`, `chunk_orphaned_at`). Fourteen
further columns in eleven tables hold a name with no foreign key at all
(T2 `229-research-27`, `229-research-48`, `229-research-54`). The
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
| RDR-227 (per-collection HNSW above the router threshold) | Adjacent, closed 2026-10-12 (T2 `229-research-25`) | Builds each per-collection index as a partial index `WHERE collection = '<name>'` and names it from a hash of model, tenant and name. Phase 3 builds each such index again on `collection_id`, beside the name index, which retires once gate 3 has latched and the readers select by id. |

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
index build on the id (P3.4). A collection's rows are stamped before its
index is built on the id, so no index is built twice. RDR-247 owns the
bounded drop of the shared leaf graph, which is not in this RDR. The stamp
cannot stop at new rows, the cost comparison's option (c): D3's chunks
key holds `collection_id`, so every row must carry one before P3.3b
adds that key. What (c) adds, stamping a collection's old rows just before
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
| nexus engine (`service/src/main`) | Yes | Three `__` literals remain, RDR-231's `NoteUri.FILE_ROUTED_PREFIXES`, read as a content-type prefix test in two places that route note handling (T2 `229-research-34`); the `quarantine-` prefix is read in 12 places (the draft's count; 6 literals and 35 reads under the P1.3 gate, Gap 2); three live SQL functions parse names (T2 `229-research-22`, `-23`). |
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
  (A dated count: the client census pin is now 265, P1.1's 243 plus the P1.2
  one-off and P1.R's cross-module roots; `scripts/` stays outside `SRC`, so
  outside the pin.)
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
     `health.py:11236`, `mcp/core.py:4298`, `search_engine.py:914`.
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
     `db/t2/http_taxonomy_store.py:2667`; the engine's prefix-matched prune is
     the matching engine site, P2.4's engine half), and the next version computed by
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
     (`aspect_extractor.py:1025/1041`, `scoring.py:533`, `health.py:12645`,
     `commands/catalog_cmds/doctor.py:1570`,
     `upgrade_ladder/rungs/note_tags_array.py:131`). The routing class
     (item 1) is wider than these shapes. The routing census (T2
     `229-research-39`) also scans loops over prefix tables, literal prefix
     tuples, default glob patterns and client SQL. It finds 23 routing sites
     plus the `taxonomy__` half of the 12 `_BYPASS_SCHEMA_PREFIXES` tests (the
     P1.1 census measures 24: the 23 and `topic_route.is_prose`,
     `topic_route.py:46`, `return collection.startswith(PROSE_PREFIXES)`).
     The three `FILE_ROUTED_PREFIXES` tests in `catalog/recovery_bundle.py`
     are migration sources.
     In `scripts/`:
     `rdr-108-je0b-backfill.py:92`, `upgrade_boot_seed.py:67/86`, and seven
     in `bench/`, `experiments/` and `spikes/`. *T2 `229-research-21`.*
  4. *Engine Java* (14 sites; the P1.3 gate pins 6 `quarantine-` literals and
     35 reads, and the SQL gate 11 name-parsing expressions in 3 functions over
     119 live functions, `CollectionNameSqlLiveBodyGateTest.java:138`). The
     `quarantine-` prefix (12):
     `CatalogRepository.java:9860-9866` (a new row's lifecycle in
     `upsertCollection`, which since P2.5 no longer reads the prefix; the
     ETL import keeps it as its fallback; T2 `229-research-314`), `:10241`, `:13528-13529`
     (`lifecycleStateForImport`); `QuarantineOrigin.java:85/162/171` (the
     predicate itself at `:73`); `VectorHandler.java:2592`;
     `PgVectorRepository.java:7419/7438`; `ChunkReaper.java:280/877/879`.
     Names are rendered with the prefix at `ChunkReaper.java:1043` and
     `PgVectorRepository.java:7549`. A content-type prefix (2):
     `NoteUri.java:178` (`isFileRoutedCollection`, called at
     `CatalogRepository.java:4399`) and `:194`, over
     `FILE_ROUTED_PREFIXES` (`NoteUri.java:22`, RDR-231); both route by
     content type (`NoteUri.isNote` is called at
     `CatalogRepository.java:4242/4251/4456/4463`).
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
  `229-research-9`.* Superseded 2026-11-21 by owner is the source (item 5):
  an owner set at registration is never re-derived from the documents, and
  catalog-044's rule now applies only to a legacy name-derived owner.
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
  (`CatalogRepository.java:11461-11772`) never updates a registry name in
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
  `legacy_grandfathered` (as of 2026-11-19: P1.4 since moved the tie to
  `created_at` then name, and the filter drop to 0.1.200, item 11). Fifteen MCP tools, 49 CLI options and 30 positional
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
  refactor. — **Status**: Measured 2026-11-20, false for `model_version` only:
  `content_type`, `embedding_model` and lifecycle agree on every live row of both tenants (171 and 43 rows);
  `model_version` is empty on 53 rows after D3 (12 on nexus, 41 on
  gate-xr789), each a conformant `__v1` name. Sam decided D1 to D4 (T2
  `nexus/sam-ruling-rdr229-p12-row-decisions-2026-11-20`): D1 refuses a blank,
  D2 sets it from the conformant name by a registered data move, whose template
  therefore reads a name (inert once complete). Any reader of the row's
  `model_version` sees `''` on those rows until D2 completes in the cloud, and
  only `collection_for` has the fallback. — **Method**: Spike (P1.2's census on
  both tenants; T2 `nexus_rdr/229-p1.2-census-2026-11-20`).
- [x] The row cache (`mcp_infra.get_collection_row`) is populated for every
  collection a write path names, so a row-first read on the write path costs
  no extra round trip. That holds for the process-default tenant's client. A
  client for another tenant has no shared cache and lists once per `nx index
  md` call (`db/http_vector_client.py:3662`, `_resolve_collection_row` case
  3), as it has since nexus-7jy5r; P3.8's registering call that returns the
  row removes that read. A collection with no row yet, or a failed read,
  answers as item 1 says, not with an error. The cache cannot hold a
  collection with no chunk yet: "The listing omits a registered collection
  that holds no chunk yet" (`corpus.embedding_model_row_preferred`; T2
  `229-research-41`). So from P3.8 the registering call returns the row and
  the writer uses it. The name fallback applies only to a legacy name
  registered before P3.8. — **Status**: Verified for the process-default
  tenant (P2.3, nexus-p2m1x.8): `nx index md` over 2 files and over 6 makes
  1 and 1 listings into an existing collection, 2 and 2 into a new one with
  no row, and a repo run adds at most 3 for 4 more documents
  (`tests/integration/test_rdr229_p23_write_path_gaps.py`,
  `tests/integration/test_rdr229_p23_repo_model_from_row.py`) —
  **Method**: Source Search of `get_collection_row`'s refresh path plus a
  test that counts HTTP calls per indexed document before and after, on an
  existing collection and a new one (P2.3).
- [ ] No code path needs a fact from the name that the row cannot carry,
  except the migration sources in Gap 5 and the URI-string classifiers
  (item 1's fifth class), which read a URI's segment, not a live
  collection. — **Status**: Verified for the parse sites; Unverified for
  the URI-string classifiers until P1.2's count and P2.10's repair move
  (below) —
  **Method**: Source Search (the parse-site census: every extracted value is
  content type, owner, model, version or the quarantine prefix, each a
  column). The census also holds sites that extract nothing and route by the
  content-type prefix (T2 `229-research-34`, widened in `229-research-39`); the row carries what they
  need too (`content_type`), but they are not repointed by moving a parse
  (item 12, P2.9). The URI-string classifiers (the fifth class) take
  nothing from a row. They read a URI's segment against RDR-231's frozen
  list, which is complete only while no `chroma://` residue carries a
  segment without a file-routed prefix. That does not hold today:
  `nx catalog rename-collection --allow-legacy` already renames to a
  prefix-free label (`collections.py:287-293`), and `moveTo` and
  `catalog_reconcile_commit` have moved residue onto any such label (T2
  `229-research-57`, `229-research-62`). It holds on an
  install once P2.10's change to both ships and its repair move has
  completed there, apart from the two shapes item 12 leaves unrepaired,
  which P1.2 counts (item 7, item 12; T2 `229-research-42`,
  `229-research-55`). **Method** for that half: Spike (P1.2's count on
  every install) and P2.10's repair move. The residue half is still
  unmeasured on every install: the route needs engine 0.1.200, and the P1.2
  census did not run the `uri-residue` doctor row (T2
  `nexus_rdr/229-p1.2-census-2026-11-20`). The count is read on both tenants
  after the 0.1.200 deploy and before Sam's go for the first cloud run of D2 and
  P2.10's moves (P1.2).
- [x] Nothing in Postgres requires a collection name to fit ChromaDB's
  limits. — **Status**: Verified — **Method**: Source Search (no object is
  named from a collection name; the per-collection index name is a hash).
- [ ] Re-keying `nexus.chunks` and `nexus.taxonomy_centroids` on
  `collection_id` (a new column backfilled on every row, a new id key per
  leaf beside the name key, three child id keys, ten foreign-key
  validation moves) and building each per-collection index on `collection_id` beside its name index completes as
  background work (item 6, "How each Phase 3 step runs") in a time the
  operator can see and accept, both on the larger production tenant and on
  a large local store. Each move's `on_complete` (the key swap's, with the
  directly validated composite foreign key; each validation's; P3.7's
  name-key drop; P4.1's chunk-family drop) holds its locks for a time the operator accepts, and
  the P3.3b index pass finishes in such a time. Until
  this is measured, the RDR does not claim that no maintenance window is
  needed. — **Status**: Unverified — **Method**: Spike (P3.2, a timed run
  on a PITR fork of that tenant and a sizing on a local store). P3.2 runs
  before P3.1's changeset is final, because the registering INSERT carries
  each move's `chunk_size`, `sleep_ms` and `statement_timeout_ms`. A local
  measurement bounds the backfill and the index build beside the name index per 1M rows (T2
  `229-research-30`); it does not time the key swap, the validations, the
  P3.3b pass, P3.7's name-key drop or P4.1's drop on that tenant (T2
  `229-research-54`, `229-research-67`).
- [ ] No two live rows of one tenant share a label once renames stop
  tombstoning, so the partial UNIQUE on live labels (item 7) can be
  created without data repair. — **Status**: Measured 2026-11-20, true: 0 label
  clashes and 0 names matching `^c[0-9]+$` on both tenants (T2
  `nexus_rdr/229-p1.2-census-2026-11-20`) — **Method**: Spike (folded into P1.2's
  census).

## Proposed Solution

### Approach

Fix what reads the name first, then change what the name is. Phases 1 and 2
close Gaps 1, 2, 4, 5 and 7 by moving every reader onto the registry row,
and Phase 2 also stops note-URI moves from carrying `chroma://` residue
and repairs residue already moved (P2.10), a row repair that touches no
key; they change no schema key and rename nothing. Phase 3 adds the surrogate key
and moves every reference onto it in the strangler order (expand, switch,
contract): new id columns are added and backfilled beside the name columns,
writers fill both, readers and constraints switch to the id, and only then
does Phase 4 drop the name columns and the parsers. Up to P3.4 the old
path still works, so each of those steps ships on its own and can stop.
P3.5 and P3.7 ship in one engine cut, because P3.5a's rename refusal
ends only when the name-key drop move that P3.7 ships and registers has
completed and every P3.5b changeset has applied, which needs a restart
(`RenameReadiness.java:28-39`; T2 `229-research-301`; P3.7);
P3.6 ships on its own. From P3.7 the referencing tables' name columns are dead until P4.1 drops
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
Any other site is a defect the census refuses, except the interim `holdover`
the census carries until Phase 2 moves it: a site no final class fits yet,
whose ledger note names the P2.n step that moves it (45 at the P1.R re-pin: P2.2
19, P2.3 11, P2.7 11, P2.4 4; `tests/_collection_name_census_ledger.py`; P2.3
and P2.7 have since landed and no holdover names either, so the live total was
23: P2.2 19, P2.4 4; at the Phase 2 integration re-pin, 7817cd3251, P2.2 takes
it 23 -> 4 and P2.4 4 -> 0, so the class is empty).
Two non-parse labels cover `fnmatch` sites, `label-pattern` and `file-path`
(5), with one exception. `label-pattern` is 10 since P2.7: nine `fnmatch` sites and one that is not, the
`resolve_corpus` stage-3 fallback, a prefix match on the label the user typed
that decomposes nothing; the census accepts a non-`fnmatch` `label-pattern` site
only with that one note (`LABEL_TYPED`). The client census is a content-anchored per-site ledger pinned by total,
class and shape (`tests/test_collection_name_parse_census.py`,
`PARSE_SITE_PIN`, `CLASS_PIN`, `SHAPE_PIN`): 243 at P1.1 (mint 107, holdover 40,
routing 36, diagnostic 30, migration-source 14), 244 with the one-off P1.2
collections-census parse (a diagnostic, deleted before Phase 4), and 265 at the
P1.R fix round (mint 111, holdover 45, routing 36, diagnostic 40,
migration-source 17): the helper set now takes roots across modules
(`tests/_collection_name_census_cross.py`), which added 21 sites. RDR-204's
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
prefix as a migration source. On this path the prefix evidence is the
name or the content type carrying `quarantine-`, the same two tests the
fallback reads. A new imported row whose effective state (its own, or the
fallback) is `'quarantine'` with no such evidence refuses the whole batch
with a message that counts and names the rows; a row with the evidence
imports as `'quarantine'`, named when it carried `'live'` (see the
residual below). Rows already registered are skipped first.

Every new row except the import residual below keeps the `quarantine-`
prefix and the `'quarantine'` state together. A name and a state disagree
in exactly two cases: a `quarantine-`
name with `'live'`, or any other name with `'quarantine'`. `'dormant'` and
`'disputed'` are admin states independent of the name (hygiene-002 marks
rows `'disputed'`), so they never disagree. `upsertCollection` refuses,
for every caller of it, a new row whose name and state disagree. So the engine's
quarantine mint passes `'quarantine'`, and a re-embed target cannot carry
the prefix. The SQL movers register the sibling as `'quarantine'`
themselves, and `POST /v1/vectors/gc/quarantine-orphans` refuses a
`quarantine_collection` without the prefix. The ETL import does not go
through `upsertCollection`, and it is the one exemption from that pairing
refusal: it passes its own prefix-evidence check instead (above, and the
import residual below), which refuses only an explicit `'quarantine'` with
no prefix evidence and lands a row with evidence as `'quarantine'`, a
plain-named one included.
The production census found no existing row that breaks the pairing (T2
`nexus_rdr/229-p1.2-census-2026-11-20`).

These name reads stay, as named residuals (P2.5, nexus-p2m1x.10):

- *The row-less fallback of `QuarantineOrigin.isQuarantine`.* Every guard
  and router that asks whether an existing collection is quarantine reads
  its row, and judges a name with no row by its `quarantine-` prefix. The
  callers are store-delete, the restore origin and sibling checks, the
  reapable and manifest-less census routes, reaper routing,
  `holdsRowsOf`, the collection delete cascade, and both ends of rename
  and rehome (a new target has no row yet). Whether the fallback is still
  needed once names are minted labels is decided at P3.8 (minting,
  nexus-p2m1x.28), which registers every new collection with a row.
- *The import's prefix evidence.* An imported legacy row with a plain
  name, a `quarantine-` content type and no `lifecycle_state` lands as
  `'quarantine'`. It is a migration source, which item 9 forbids
  stranding, and P2.1's `CatalogEtlImportNonStrandingTest` is the
  pre-declared check of that shape. It is the one way a plain-named
  quarantine row enters. It stays safe because lifecycle is read from the
  row, so such a row is treated as quarantine everywhere and the guards
  refuse it as a target. A row that carries `'live'` with a `quarantine-`
  name or content type also imports as `'quarantine'`, and the import
  names it (`state_corrected` and a warning). The real case is a sibling
  that the ghost sweep revived to `'live'` between engine v0.1.109 and
  v0.1.111; hygiene-005-3 promoted back only rows still `'dormant'`.
  A plain name with an explicit `'quarantine'` and a plain content type
  has no legacy evidence behind it and is refused.
  A `quarantine-` name with an explicit `'dormant'` or `'disputed'`
  imports as given and resolves, because those states are orthogonal to
  the name.
- *The registration-side `quarantine-` prefix refusal: the engine mints the
  `quarantine-` name; this guard stops a client minting one.* The client
  route (`POST /v1/catalog/collections/upsert`) also registers `live` only,
  and refuses a new row named `quarantine-...` with a 400. The engine's
  own callers of `upsertCollection` and the SQL movers are not subject to
  these two client rules, and neither is the ETL import. The engine's own
  callers are subject to the pairing check above, and the SQL movers
  register only prefixed siblings; the ETL import passes its
  prefix-evidence check instead (the import residual). Two refusals of the same class read
  the prefix: re-embed refuses a `quarantine-` target before registering
  a new row, and the gc quarantine-orphans route refuses a mover sibling
  without it.
- *The engine's own mint and the lookups of what it minted.* The mint is
  `ChunkReaper`'s sibling label (`QuarantineOrigin.PREFIX + name`, passed
  to the move) and the SQL movers that register that sibling as
  `'quarantine'` (`vectors-024`, `hygiene-008`). Two lookups build the
  same label to find a sibling: `PgVectorRepository`'s sibling-name lookup
  and `QuarantineOrigin`'s untagged-sibling search, which also matches the
  `quarantine-` content type. `ChunkReaper`'s boot-time floor-exempt
  warning reads a configured name, when no row is available. All of these
  use the one constant, `QuarantineOrigin.PREFIX`.
- *A version read from a conformant name.* One helper,
  `CollectionRegistration.versionOfConformantName`, takes `vN` from a
  conformant name for the ETL import fallback and for the re-embed
  target's minted name (Sam's D1, T2
  `nexus/sam-ruling-rdr229-p12-row-decisions-2026-11-20`). It is a
  migration source and goes away at P4 with the name parsers.
- *The chunk-key allow-list bypass set.* `ChunkKeyPlan.BYPASS_PREFIXES`
  and its readers (`ChunkConstraintAdmin`'s `chunks_allow_list` CHECK,
  `ChunkKeyAllowlistCheck`, `ChunkKeySweep`) still exempt a `quarantine-`
  name from the allow-list. A CHECK constraint cannot read
  `catalog_collections`, so reading the row here would mean a trigger.
  It is a permanent residual (the RDR author's ruling, 2026-11-20,
  nexus-p2m1x.10's notes), with no trigger: the prefix is engine-minted,
  and the pairing rule makes it carry the `'quarantine'` state on every new
  row. The exception is the import's plain-named quarantine row (the import
  residual above): the CHECK reads the name, so it does not exempt that
  row, and its chunks must satisfy the allow-list. That is accepted rather
  than refused at import. No engine path moves chunks into such a row (the
  reaper mints only `quarantine-` siblings), and RDR-236's sweep strips
  retired document keys from `quarantine-` rows too, because a restore
  copies them into a live collection where the CHECK applies. So a
  plain-named quarantine row loses nothing a prefixed one keeps after the
  sweep. `ImportedPlainNamedQuarantineAllowListTest` pins it.
- *The client's row-less fallback.* `nexus.db.rows.lifecycle`
  judges a name with no row, or a row with no state, by its prefix, at
  each of the client's bypass checks (census note `QUAR_NOROW`, a
  migration source). `chunk_quarantine.is_quarantine_sibling_name` stays a
  name read outside P2.5's scope: its callers are row-less by construction
  (the backfill of names missing from the registry, the registration
  retry) or only shape a message. `doctor_edge_probe` tests the prefix
  behind its own lifecycle filter.

**3. SQL reads columns.** `chash_conformance_report(dim)` filters manifest
rows by the manifest's `embedding_model` column (the `vectors-030`
changeset adds it, lines 786-791) joined to
`nexus.embedding_models.dimension`, instead of nine `split_part` IN-lists
that also hard-code the model list. The two quarantine functions test
`lifecycle_state` instead of the prefix. As built (`vectors-063`), a
manifest row whose collection has a catalog row is counted through
`catalog_collections.embedding_model`, which the manifest's own column
equals by the `fk_catalog_chunks_chunk` and `chunks_collection_fk` foreign
keys. A row with no catalog row is counted by its own `embedding_model` on
a separate "no catalog row" line, shown only when non-empty. Those foreign
keys make such a row unreachable unless they are set aside.

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
by `created_at` ascending, then name, until `collection_id` replaces it. As
shipped by P1.4: `.orderBy(COL_VERSION_NUM.desc(), F_COL_CRTAT.asc(),
CATALOG_COLLECTIONS.NAME.asc())` (`CatalogRepository.java:10560`), whose
comment at `:10548` says "P3.1's collection_id (identity) replaces created_at
here with the same meaning". P3.5c owns that switch (Java readers and the
two upserts read the id). The
name-rendering branch of
`t3_collection_name` stays only as a migration source for a legacy
two-segment knowledge name that has no row.

Owner is the source (Sam, 2026-11-21, T2
`nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`). A collection's
`owner_id` names its source and is set once, at registration; nothing
re-derives it from the collection's documents. `catalog-044`'s rule that
takes the owner from the documents now applies only to a legacy
name-derived owner (a slug, `''` or a placeholder): a catalog amendment
removes its curator clause, so a curator owner a registration set stands.
Its second half (`catalog-051-2`) stops catalog-044's two triggers from
rewriting a legacy owner onto a tuple (tenant, content type, owner, model,
model version) that another `code`, `docs` or `rdr` collection already
holds: the row keeps its old owner and the trigger raises a NOTICE naming
both collections. A row holds its tuple when it is unsuperseded and not in
quarantine (`superseded_by = '' AND lifecycle_state <> 'quarantine'`), the
predicate of P3.8's partial UNIQUE index (T2
`nexus/sam-ruling-rdr229-p38-design-gate-v2-2026-11-21`, Q1), so a dormant
or disputed holder holds like a live one. The guard compares the stored
`model_version`; a row whose D2 repair is still pending (`''`) is left to
the re-own move, which skips it. Before that check each trigger takes a
transaction advisory lock on the tuple's key,
`nexus.collection_tuple_lock_key` (`catalog-051-1a`, made a striped key by
`collections-003-1`), the SQL function that is the key's one derivation (the
engine's `CollectionTupleLock` selects it and does not re-implement it). The
key is one of 256 stripes in one fixed namespace (the high 56 bits of
`md5('18:catalog_tuple_lock')`; the stripe is the low 8 bits of the tuple's
md5), so a batch takes at most 256 locks, deduplicated and sorted before it
locks; unrelated tuples that share a stripe wait on each other briefly, which
is accepted by design. A transaction deadlocked on that lock gets
40P01, and only the paths through `CatalogRepository`'s `DeadlockRetry`
re-run it (deletes, renames, re-homes, purges, manifest writes and chunk
imports); a plain document registration, `upsertCollection` and
`supersedeCollection` surface it to their caller, and the data-move worker
retries the chunk.
An import resolves the owner from the collection's existing row, then the
bundle's optional `owner` header, then a `<slug>-<hash8>` stem, taken as
the repo's when the collection is itself `code`, or is `docs` or `rdr` and
the target already holds a repo owner with that hash or a live `code`
collection of that stem (`nxexp_owner.resolve_import_owner_tumbler`), then
the corpus curator for a `docs` corpus, then the knowledge curator with a
warning. A repo's collections that an old bulk import left under a curator
are re-owned by the registered data move
`rdr229-reown-repo-stems-from-curator` (`vectors-067`), which skips a row
whose destination tuple another row holds (the same held predicate) and
records each skip in `nexus.gc_audit` (operation `rdr229_reown_skipped`; read it
with `SELECT collection, details FROM nexus.gc_audit WHERE operation =
'rdr229_reown_skipped'` under the tenant). It compares tuples on the
effective version, the stored `model_version` or else the name's trailing
`__vN`, so a row D2 has not yet repaired is seen as its v1 twin's and
skipped, not moved. It takes the same tuple lock as the triggers, computed
from the stored version, for only the tuples its chunk moves rows onto, in
ascending key order (the order `CollectionTupleLock` uses), so a chunk holds
at most `chunk_size` locks. Two named residuals, both
accepted by Sam:

- An old export file (from before the `owner` header) of a repo's
  `docs__` collection, imported before its `code__` sibling onto a target
  with no repo owner for that hash, takes its corpus curator and keeps it,
  because an existing owner wins on re-import. The import prints a hint
  naming the remedy: import the repo's `code__` bundle, then remove the
  collection and import it again. No new route closes it (T2
  `nexus/sam-ruling-owner-is-source-old-bundle-residual-2026-11-21`).
- An install that has not yet applied `catalog-044-3` runs that one-off
  repair on upgrade before the amendment, under the old rule, so a
  docs-only curator corpus whose documents carry a repo tumbler is re-owned
  to that repo. Only installs from before `catalog-044-3` are affected; no
  heuristic repair (T2 `nexus/sam-ruling-batch-2026-11-21-late`, item 2).

**6. The surrogate key (D3).** `catalog_collections` gains `collection_id
bigint GENERATED BY DEFAULT AS IDENTITY`, unique across the installation, with a
UNIQUE `(tenant_id, collection_id)` that every reference targets, so
`tenant_id` still leads every foreign key (row-level security and partition
pruning on tenant leaves are unchanged). The RDR-225 composite foreign key
needs one more: it now references "UNIQUE (tenant_id, name,
embedding_model)" (`vectors-030:126-127`, `:1062-1066`), and PostgreSQL
requires that "the refcolumn list must refer to the columns of a
non-deferrable unique or primary key constraint or be the columns of a
non-partial unique index" (T2 `229-research-44`). So P3.1 also adds
UNIQUE `(tenant_id, collection_id, embedding_model)`, and it stays after
Phase 4 as that key's target. At Phase 4 the primary key becomes
`(tenant_id, collection_id)` and `(tenant_id, name)` stops being a key.

The identity is BY DEFAULT and not ALWAYS (T2
`nexus_rdr/229-lead-ruling-collection-id-by-default-2026-11-21`), so a writer
may supply an id, but an explicit id comes from the column's own sequence, or
the writer runs `setval(pg_get_serial_sequence('nexus.catalog_collections',
'collection_id'), max)` afterwards. The identity fill is ordinary changeset
work in `vectors-068-1`, which fills every existing row of this small table
inside the DDL (171 rows on one tenant and 42 on the other at the P3.2
census). It is the one row rewrite P3.1 makes at boot, and no stamp move
fills the registry's own id.

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
  `origin_collection` metadata tag gains an `origin_collection_id` beside it:
  the origin's registry id as a JSON number, left out when the origin has no
  registry row. The three SQL functions that write the tag and the Java
  retag (`QuarantineOrigin.ORIGIN_ID_KEY`) write it alike.
- **Switch.** Each foreign key gets an id twin on `(tenant_id, collection_id)`
  (or `(tenant_id, collection_id, embedding_model)` for the RDR-225
  composite and for `canary_query_ring`'s, which also references the name
  UNIQUE today, vectors-054:90-93) beside its name key, `NOT VALID` first and validated after by a data move's
  `on_complete` (P3.3a). The exception is the RDR-225 composite from the
  partitioned `nexus.chunks`, which PostgreSQL validates directly (below).
  Each validation is its own move, one per foreign key (so
  `topic_assignments` has two): the seven
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
  chunks and centroid tables gain an id key per leaf, `(tenant_id,
  collection_id, chash, embedding_model)` and `(tenant_id, collection_id,
  topic_id, embedding_model)`, beside their name primary keys, which stay
  until P4.1. P3.3b's pass builds that key, and P3.3c's swap checks it
  before it adds the three child foreign keys' id twins (P3.3c, below).

  Not every name key to the registry can stay until P4.1. P3.7's one-row
  rename updates `catalog_collections.name`, and PostgreSQL's default `ON
  UPDATE` action, NO ACTION, is to "Produce an error if the deletion or
  update would create a foreign key constraint violation" (sql-createtable;
  T2 `229-research-58`). Of the eight name keys:
  - `chunks_collection_fk` (vectors-030:1062-1066),
    `canary_query_ring_collection_fk` (vectors-054:90-93), and
    `document_aspects`', `topics`', `taxonomy_meta`' and
    `document_highlights`' keys (fk-003:114-119, :152-157, :171-176,
    :192-197) have no `ON UPDATE`, so each refuses the rename of a
    collection that has a row in its table;
  - `topic_assignments_collection_fk` is `ON UPDATE CASCADE`
    (fk-002:240-246). Inferred, not read: the label it copies into
    `source_collection` then fails `topic_assignments_chunk_fk`, which
    references the chunks' name key (vectors-030:1032-1036), because the
    chunks keep the old name; so it refuses the rename too;
  - `aspect_extraction_queue_collection_fk` is `ON DELETE CASCADE ON
    UPDATE CASCADE` (aspects-005:103-107), and the queue's only other key
    references `catalog_documents`, so it refuses nothing.

  So P3.3a drops none of the name keys, and the seven that refuse the
  rename are dropped before P3.7's one-row rename by one move, the name-key
  drop move. Two of the seven sit on chunk-family tables, where "A later
  finishing step ... is therefore a new move whose on_complete does it"
  (RDR-246 lines 911-913), and `ChunkFamily` counts a move as chunk-family
  "over the driving table's name ... and over the `on_complete` text"
  (lines 917-920). So its driving table is the small
  `nexus.catalog_collections`, which keeps the move short (T2
  `229-research-63`).
  The completion check registers it once gate 3 holds on the install, as
  it registers P4.1's drop move (below). Its template is the no-op
  re-stamp, its verification returns the table's null ids, and its
  `on_complete` drops the seven keys and, where `aspect_extraction_queue`
  still exists, the queue's key too, under the guard the engine already
  uses for a table that may be gone (`IF to_regclass(...) IS NOT NULL
  THEN ... DROP CONSTRAINT IF EXISTS`, fk-002:217-223), because P4.1's
  primary key swap cannot drop `(tenant_id, name)` while a key references
  it (T2 `229-research-61`). P3.7's one-row rename waits for its
  `completed_at`. Every
  per-collection index is built with `WHERE collection_id = <id>` beside its name index, and a
  name hashed from model, tenant and id, so a label change never strands
  one. Readers (engine repositories, SQL functions, client caches) key on
  the id, on each install only once the completion check's gates pass
  ("Readers during the stamp", below; T2 `229-research-43`,
  `229-research-52`). Every collection-taking route in the census accepts
  `collection_id` or a label. The route boundary replaces an id with its
  label once, before the handler runs, so a handler reads the label; the
  delete's handler also reads the resolved id, to remove the one row of an
  id when a label is held by several (`CollectionSelectors.java:21-22`,
  `:58-61`). A route that writes under the collection it names reads a
  named id inside the write's own transaction; the retiring verbs
  (supersede, rename, delete) lock the row instead, the read-only routes
  need no guard, and the code's exempt routes have nothing to guard (`CollectionSelectors.java:62-72`,
  `:89-102`, `:110-128`; T2 `229-research-311`).
  `CollectionRegistry` is keyed by id with a label index beside it and has
  no gate of its own (`CollectionRegistry.java:99-104`; T2
  `229-research-308`). The rename answer carries the label and, when it is
  above 0, the id (`CatalogHandler.java:2989-3001`; T2
  `229-research-306`). The client does not cache by the id until P3.7's
  name-key drop and the answer's `ids_stable` both say it is stable; no
  client code reads `ids_stable` yet (`http_catalog_client.py:518-524`;
  T2 `229-research-307`, `229-research-314`).
- **Contract (Phase 4).** All eight name keys to the registry, the two
  composites and the six plain ones, are already gone, dropped before
  P3.7's one-row rename by the name-key drop move (Switch). Before any of
  the drops below, every name-holding primary key and UNIQUE has a valid
  id-keyed twin (P4.1; bead nexus-p2m1x.35, comment 17:42; T2
  `229-research-72`). Views that read the name columns are redefined first
  (comment 09:24); then the three name child keys go. Then the name
  primary keys of the chunks and centroids swap for their id keys in one
  transaction, with the method left open for P4.1's design (bead
  nexus-p2m1x.35, comment 23:02; T2 `229-research-105`). The name columns
  on referencing tables are then dropped with RESTRICT; then
  `superseded_by` and the `(tenant_id, name, embedding_model)` UNIQUE, and
  last the registry's primary key swap. The registry changeset runs only
  once a check finds no foreign key on the name, and once the name-key
  drop move has completed, so the drops cannot meet a key that still
  depends on them (P4.1; T2 `229-research-61`). Dropping
  `superseded_by` drops the label index whose predicate reads it, so P4.1
  re-creates that index on `superseded_by_id` (item 7). Before any of
  that, an engine release retires the Java name path (P4.0): the name
  readers, the rename's fourteen-table rewrite and the name-based arms of
  `COLLECTION_SCOPED_TABLES` (`CatalogRepository.java:11060`). Until
  that release the dual path stays in Java. That engine carries a boot
  floor, because a release reaches an install whatever its moves have
  done: it serves migration-only until gate 3 of the completion check
  holds on the install, P3.7's name-key drop move has `completed_at`
  set and every P3.5b changeset has applied, the state in which P3.7's
  one-row rename runs (T2 `229-research-59`, `229-research-64`).

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
no exemption line is needed (T2 `229-research-41`; `vectors-068-1` fills
every existing row inside the DDL, once, and no other user-data table is
touched). Three kinds of work follow, each
while the engine serves:

- *The stamp is a data move.* P3.1's changeset adds, `NOT VALID`, on every
  table in the Expand list that carries a name, a CHECK that the id is set
  wherever the name is set: `(NULLIF(<name column>, '') IS NULL) = (<id
  column> IS NULL)`. An empty name means unset, as `superseded_by` already
  did: the P3.2 fork run found `''` in `catalog_documents.physical_collection`,
  `gc_audit.collection` and `hook_failures.collection`, and a row that holds
  `''` needs no id, so the stamp never touches it. The id column is
  `collection_id` on most tables. The others are `superseded_by_id`,
  `physical_collection_id` on `catalog_documents`, `source_collection_id` on
  `topic_assignments`, and `source_collection_id` and `target_collection_id`
  on `chash_remap` and `reembed_jobs`. The four history tables take a bare
  nullable id and no CHECK. P3.1 adds 20 `NOT VALID` CHECKs in all, counting
  the two `collection_id IS NOT NULL` guards below, each with a stable name.
  One list, `service/src/main/resources/db/rdr229/p31-checks.tsv`, holds the
  tables, name and id columns and constraint names that the changesets, the
  operator down-step and its tests all read. The exception to the name rule
  is `superseded_by`, which is `TEXT NOT NULL DEFAULT ''`
  (`catalog-001-baseline.xml:208`) and empty on a live row: the engine
  writes `.set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "")` to make a row current
  (`CatalogRepository.java:10089`) and the new name on a rename's tombstone
  (`:11477`). Its CHECK is `(superseded_by = '') = (superseded_by_id IS
  NULL)`, added `NOT VALID` in P3.1 and validated with the stamp. The
  stamp fills it from the text column: a non-empty `superseded_by`
  resolves to the `collection_id` of the row with the same `tenant_id` and
  name, and `''` stays NULL. A tombstone whose successor was later deleted
  names no row, so it takes its own `collection_id`, which reads as retired
  with no successor (R7; P1.2 counts those, T2 `229-research-50`). The
  invariant: `superseded_by_id` is the id of the row its `superseded_by`
  names in the same tenant, and the row's own `collection_id` when none
  does; a live row keeps NULL. Three writers move a tombstone's id: the
  stamp; the successor-delete repair
  (`CollectionTombstones.deleteRegistryRows`), which gives a surviving tombstone its own id when its successor is
  deleted; and the registration of a name, which re-points every tombstone
  naming it to the new row's id (`repointTombstonesNaming`). A supersede, a
  rename and a registry upsert set the id in their own statement (the
  engine's `CatalogIdStamps`, and the SQL functions of vectors-069). The
  column is `vectors-068-2`, which never waits, because the
  dual write sets it whether or not the CHECK exists. The CHECK is its own
  changeset, `vectors-068-19`, behind an `onFail="CONTINUE"` precondition on
  data and not on D2's state: it waits, and retries each boot, while a
  registry row has `superseded_by <> ''`, no `superseded_by_id` and a blank
  `model_version`, because D2's repair move updates exactly those rows and a
  `NOT VALID` CHECK would refuse the update with 23514. That wait ends when
  D2's move completes or when the stamp fills the column. When no row
  violates it the changeset validates the CHECK itself, with the VALIDATE
  trapped on `check_violation`; a violator the guard could not see leaves
  the CHECK `NOT VALID` for the stamp's `on_complete`. On a single-role
  install the guard reads no registry row it cannot see and so counts the
  registry clean. That is sound for one stated reason: `MovePreflight`
  refuses every move there, so no UPDATE ever meets the CHECK (Risks, the
  single-role entry). Gate 1 below refuses while this CHECK is missing. The
  partitioned `nexus.chunks` already carries a `NOT VALID` CHECK this way
  (`chunks_chash_octet_check`; T2 `229-research-41`). P3.1 then registers one
  move per table, sixteen in all, fifteen where the retired queue table is
  absent (the label is `rdr229-stamp-<table>`). The template is `UPDATE
  <driving_table> SET <id> = COALESCE(t.<id>, (SELECT cc.collection_id FROM
  nexus.catalog_collections cc WHERE cc.tenant_id = t.tenant_id AND cc.name
  = t.<name>)) WHERE <pending> AND <chunking_clause>`. A row is pending when
  its name is set (`''` is unset), its id is NULL and its name resolves to a
  registry row of the same tenant. A row the dual write stamped is not
  pending, so a re-run, a resumed run and a row a new engine wrote are not
  written again. `DataMoveWorker` runs it through dml_utils in primary-key
  chunks, per tenant, and per leaf on the partitioned tables (runs named
  `<label>/<tenant>/<leaf>`). Its verification query is the same pending
  predicate, so it returns the tenant's rows that are still unstamped. Its
  `on_complete` validates each CHECK of the table only if `pg_constraint`
  has it, and, on a table whose CHECK can meet an orphan (every one but the
  registry and `nexus.chunks`), only when no orphan row remains (below). Its
  `statement_timeout_ms` bounds each verification run and the
  `on_complete`, and RDR-246 sets the `on_complete`'s `lock_timeout`
  to "min(5 s, `statement_timeout_ms` / 2)". P3.2 sizes the timeout, with
  `chunk_size` and `sleep_ms`: `nexus.chunks` 100 rows and a 20 s pause,
  `taxonomy_centroids` 100 rows and 2 s, every other table 1000 rows and 4
  s, and 60 s for the timeout on all of them. The move reports in
  `boot_work` as `data-move:<label>`, with `on_stop: "resumes from the last
  committed batch"`, so a killed engine resumes at the first unprocessed
  chunk (RDR-246 lines 867-868).
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
  (`TaxonomyCentroidRepository.java:149`, the `ct` table, and `:166`, the insert), the same shape that writes
  chunks at `PgVectorRepository.java:1308`, and `TenantImportLoad.java:256`
  inserts into any table by name (T2 `229-research-48`). The 109 was a lower
  bound. P3.1's acceptance re-runs the writer census over helper-routed and
  by-name writes and finds every writer of `taxonomy_centroids` before its
  CHECK lands. The re-run is `scripts/rdr229_p31_writer_census.py`, which
  writes `service/src/test/resources/rdr229/p31-writer-census.tsv` and has a
  drift check (`--check`). It lists 183 Java write rows (140 on a literal
  table, 34 through a helper, 9 by name), 48 statements in 25 SQL functions
  (vectors-069 redefines 24 of them; `purge_trash` writes `''` and needs no
  id) and 26 registered move templates.
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
  and it records its completion by registering the swap. Read
  (`LeafIndexPass.java:61-67`, origin/integration/rdr229-phase3-stack-r2
  at 6af903a5e3): the pass builds one valid unique index per leaf on the id
  key, "`CREATE UNIQUE INDEX CONCURRENTLY`, one at a time", dropping an
  INVALID leftover first. Only "once every leaf of the table has a valid
  index" does it add the key to the parent and attach the leaves: "`ALTER
  TABLE ONLY` the parent `ADD CONSTRAINT UNIQUE`", then per leaf "`ADD
  CONSTRAINT ... UNIQUE USING INDEX`" and "`ALTER INDEX ... ATTACH
  PARTITION`". Its last step registers the swap move (`registerSwap`,
  `LeafIndexPass.java:510`), so the swap's verification never runs before
  the indexes exist. The helper that creates a leaf
  (`nexus.partition_ensure_leaf`, `vectors-073-1`, lines 98-101) branches
  on the parent's constraint in `pg_constraint`, never on an index name.
  While the parent lacks the id `UNIQUE`, the helper builds a plain
  `CREATE UNIQUE INDEX` on the new, empty leaf, named `<leaf>_cid_uq`,
  which the pass adopts. Once the parent carries it, PostgreSQL makes the
  leaf's index itself and the helper makes none, so a leaf added later
  never lacks one and never gets a duplicate (T2 `229-research-100` and
  `229-research-101`; the ruling is linda's, nexus-p2m1x.21 comment
  2026-11-22 08:28). The per-collection indexes on
  the id (P3.4) are built after P3.3c's swap and after every foreign-key
  validation move has completed (Switch, above), and a collection's index
  only once none of its rows has a null `collection_id`. That is the
  stamp-before-build order of Sam's RDR-247 ruling (Scope boundary with
  RDR-247). Built after the swap and the validations, they are not ended
  by those moves' chunk-family `on_complete`, which ends builds in flight
  (RDR-246). So an index is built a second time only after a kill. The
  id-keyed unique arbiters are built at run time as well, after the engine
  serves and once gate 2 holds: `IdUniqueIndexPass` builds
  the twin index of each `ArbiterTwins` entry that names one, eight of the ten
  (`ArbiterTwins.java:48-72`; `builtByThePass` at `75-77`; the other two,
  `chunks` and `taxonomy_centroids`, get their UNIQUE from P3.3b/c's parent),
  and the plain `(tenant_id, <id column>)` supporting indexes. The code says
  these support P3.3a's id-twin foreign keys except
  `idx_catalog_chunks_collection_id`, the id twin of an index the R2 anti-join
  reads (`IdUniqueIndexPass.java:50-52`, `:155`; `Main.java:944`; T2
  `229-research-266`, `229-research-289`). They are not in the boot walk.
  The P3.4 changelog adds only the latch table, `vectors-080-1` (T2
  `229-research-265`). `IdUniqueIndexPass.java:51-52` gives the reason: "an upgrade boot may write
  at most 64 MiB and the RDR builds no index on a user-data-growing table
  inside it." Gate 3 requires each of them VALID, and gate 3 implies gate 2,
  since no id index builds before gate 2 holds (T2 `229-research-267`). A
  table found corrupt (a duplicate on an id key), or an index of a pass name
  that stands with another definition, is held: its `boot_work` entry,
  `id-unique-index-held`, stays visible and safe to stop, is not work, and
  never defers `nx upgrade`; gate 3 stays false while it holds
  (`IdUniqueIndexPass.java:74-80`; T2 `229-research-282`).
- *The switch is P3.3b's pass and P3.3c's `on_complete`.* The id key is added beside the
  name primary key, not in its place. P3.3b's pass adds it, by the route
  PostgreSQL documents for a partitioned table: `ALTER TABLE ONLY` the
  parent `ADD UNIQUE`, a matching key on each leaf, then `ALTER INDEX ...
  ATTACH PARTITION` for each leaf ("ALTER TABLE ONLY measurement ADD UNIQUE
  (city_id, logdate); ... ALTER INDEX measurement_city_id_logdate_key
  ATTACH PARTITION ...", `ddl-partitioning.html`; T2 `229-research-41`).
  Each leaf's key takes the unique index built concurrently above, through
  `ADD ... USING INDEX`, which "is not currently supported on partitioned
  tables" but is on a leaf. P3.3c's swap does not add the key. Its
  `on_complete` refuses unless a valid unique key on the id columns is
  already there (`Rdr229SwapMove.java:223-236`, SQLSTATE 55000; T2
  `229-research-102`). So P3.3c has, per table (`nexus.chunks`,
  `nexus.taxonomy_centroids`), a move that P3.3b's pass registers when
  that table's indexes are all valid and its parent key is attached (above), whose driving table is that
  table, run per tenant and leaf. Its template re-stamps rows whose id is
  still null, which is a no-op after P3.1. Inferred, not read: dml_utils
  still walks every chunk of the leaf, one chunked pass. Its verification
  query returns any leaf without a valid unique index on the new key, and
  nothing else. It does not look for a null id: gate 1 needs the stamp's
  NOT NULL CHECK present and validated before the pass registers the move,
  so no null id can be there (`Rdr229SwapMove.java:36-40`; T2
  `229-research-102`). Its `on_complete`, in one transaction, first
  waits, on the chunks swap only, until the child index
  `idx_chunk_orphaned_at_tenant_chash` is built. That index is a boot
  changeset, `vectors-075-1`, which heals an INVALID leftover and builds
  the index at the next boot (`vectors-075-chunk-orphaned-at-chash-index.xml`;
  T2 `229-research-103`). The wait raises SQLSTATE 55P03, which is
  transient. On the chunks swap only, it then adds the three child id keys
  `NOT VALID` with `ON UPDATE CASCADE` (their referencing tables are not
  partitioned; the centroids swap adds none; T2 `229-research-115`). The
  manifest's key, `fk_catalog_chunks_chunk_id`, is `DEFERRABLE INITIALLY
  DEFERRED`, not its name key's `INITIALLY IMMEDIATE`, because purge_trash
  and its kin defer the name key by name, and an immediate id twin would
  raise 23503 there (`Rdr229SwapMove.java:89-100`; the ruling is linda's,
  nexus-p2m1x.21 comments 2026-11-22 09:41 and 09:51). On the chunks move
  only, it then registers the three moves that validate them (Switch,
  above). It drops nothing: the name primary key and the three
  name child keys stay until P4.1. The outbound composite
  foreign key from `nexus.chunks` to `catalog_collections` on the id cannot be added
  `NOT VALID`, "because PostgreSQL does not take NOT VALID for a partitioned
  referencing table", so it is validated directly in the same `on_complete`
  (vectors-030 lines 1013-1016); the old `chunks_collection_fk` stays until
  the name-key drop move (Switch, P3.7). The `on_complete` runs as one transaction.
  A lock it cannot get "fails fast as a transient `blocked:` (55P03)", and
  a kill before the commit leaves the table as it was (RDR-246 lines
  545-547, 914-916).
- *The name path keeps working after P3.3c.* Both writers name the name
  key as their conflict target: `insert.onConflict(ch.tenantId(),
  ch.collection(), ch.chash(), ch.embeddingModel())`
  (`PgVectorRepository.java:1324`) and `.onConflict(ct.tenantId(),
  ct.collection(), ct.topicId(), ct.embeddingModel())`
  (`TaxonomyCentroidRepository.java:172`). PostgreSQL infers "All
  table_name unique indexes that ... contain exactly the conflict_target"
  columns, and "If an attempt at inference is unsuccessful, an error is
  raised" (T2 `229-research-53`). Keeping the name primary key keeps an
  arbiter for both until P4.1. Both writers move their conflict target to
  the id key with P3.5c, when gate 3 passes on the install (below); both
  keys exist from P3.3b's pass to P4.1, so either target has an arbiter.
  While both keys hold two unique indexes over the same rows, a targeted
  upsert can lose a race: a second writer's insert passes its arbiter's
  check and then trips the other key with a plain 23505 (measured by
  `SuppliedVectorsTest`, 3 of 3 red on the stack before the fix). Until
  P4.1, each Java upsert on a pair in `TwinKeyUpsert.pairs()` (the ten
  `ArbiterTwins` entries, `TwinKeyUpsert.java:57-61`) runs through
  `TwinKeyUpsert.run`, which runs the statement under a savepoint and retries
  it once when the 23505 names the non-arbiter member of its own name/id pair
  (`TwinKeyUpsert.java:26-31`; fixed at 70a97c92bc on
  origin/integration/rdr229-phase3-stack-r2; T2 `229-research-107`). An upsert
  in a plpgsql body takes a column-matched plpgsql retry instead; that retry
  is not on the stack yet (T2 `229-research-106`). The census of upserts on
  a twin-key table is on the stack (`TwinKeyUpsertCoverageCensusTest`, commits
  62d40cc1db and 8e7aa44ad7, bead nexus-p2m1x.20; T2 `229-research-275`). The arbiter is the caller's, and it flips at P3.5c. A
  `DO NOTHING` site with no conflict target needs none of this. The
  child keys cascade names only (`ON UPDATE CASCADE`,
  vectors-030:1017-1051), and `chunk_orphaned_at` is not among the
  `COLLECTION_SCOPED_TABLES`, so a rename reaches its name only through
  that cascade. Inferred, not read: from P3.1 the name-path rename, which
  inserts a new registry row and so a new id, sets that id wherever it
  rewrites a name and in `chunk_orphaned_at`; from P3.3c the child id
  keys' `ON UPDATE CASCADE` carries it too. Inferred, not read: from P3.3b
  until P4.1's swap makes the id key a primary key (method open, see P4.1),
  the id key is a UNIQUE, and a UNIQUE admits
  "duplicate rows that contain a null value", so P3.1 also adds `CHECK
  (collection_id IS NOT NULL)` `NOT VALID` on `nexus.chunks` and
  `nexus.taxonomy_centroids`, validated with the stamp.
- *Readers during the stamp.* RDR-246 requires this RDR to say it: "Each
  move's RDR or bead states how readers behave while it runs: read both
  shapes, or keep the feature off until `completed_at` is set" (RDR-246
  lines 895-897). The rule: on each install, readers keep the name path
  until every stamp move on that install has `completed_at` set, and the
  engine's id readers and the one-row rename also wait for the gates of
  the completion check below. A move's
  `completed_at` is "set when the move verified and on_complete ran", one
  row per move (`datamoves-001-registry.xml:30`), and a move counts as
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
    read: after `SchemaMigrator.migrate` (`Main.java:285`), on its own admin
    session (`CompletionBootWork`), the engine reads the rows of its read
    set, the catalog and the CHECK constraints and keeps the latest answer
    for the process. The session is opened `default_transaction_read_only =
    on`, so a write would raise at the server: Sam's rule is no boot-time
    data migrations. P3.1's check therefore reads and reports and registers
    nothing; the registrations at gate 3 below belong to the cut that ships
    each move. Each release adds
    its own labels to the set: the stamp labels (P3.1), P3.3a's seven
    validation labels, P3.3c's two swap labels and three child
    validation labels, and P3.7's name-key drop label. A label with no row
    counts as incomplete, because the swap, child and drop rows are
    registered at run time. A move whose row
    has `failed_at` set and whose driving table no longer exists
    (`to_regclass` of its `driving_schema` and `driving_table` is NULL)
    counts as complete. That is how `aspect_extraction_queue` ends: once
    emptied, "the next boot's guard drops the emptied table"
    (`Main.java:705`), and a move on a missing table fails with 42P01
    as "permanent" (`MovePreflight.java:67-71`,
    `DataMoveFailureClassifier.java:153-154`); nx doctor still shows its
    `failed:` row. Inferred, not read: the check decides it from those two
    columns (T2 `229-research-52`). One label is exempt from the no-row
    rule: the retired queue's stamp, `rdr229-stamp-aspect_extraction_queue`,
    is not applicable, whether it has no row or a failed one, while its
    table or its `collection_id` column is absent
    (`StampLabels.NOT_APPLICABLE_WHEN_ABSENT`; `vectors-068-18` is skipped,
    unrecorded, on an install whose kept queue another role owns). Every
    other label with no row stays incomplete. A stamp label is complete only
    while its table's CHECKs are present and validated. A P3.3c validation
    label is complete only while its child's id key is present and
    validated (`CompletionCheck.java:61-64`; linda, nexus-p2m1x.21 comment
    2026-11-22 10:44; T2 `229-research-104`). A move whose
    `on_complete` found an orphan (a name set, an id NULL, and no registry
    row of the row's tenant) completes with the CHECK left `NOT VALID`, so
    its label stays incomplete and the VALIDATE waits instead of failing a
    boot; `nx doctor` names the table. An orphan row is write-frozen: any
    UPDATE of it fails 23514 while its id is NULL, the engine's own writes
    included. The orphan guard is on every table whose CHECK can meet one.
    `nexus.chunks` has none: its composite foreign key to the registry means
    a row whose name has no registry row cannot exist, so the guard could
    only double the scan of the largest table. The centroids table has no
    such key, so it keeps the guard. A move that completed does not run
    again, so registering the missing collection stamps nothing. The way
    back is the operator's: register or delete the orphans, then
    `SELECT nexus.rdr229_restamp('<table>')` (`vectors-071-1`). It
    registers `<label>-rerun-N`, a new row, since a label is the registry's
    primary key, that supersedes the chain's tail and copies its template,
    verification, `on_complete` and throttle. It registers only when that
    tail is complete, a CHECK of the table is `NOT VALID` and no orphan row
    remains; otherwise it returns text and writes nothing. The down-step
    pause (Risks) covers the reruns, since their labels start
    `rdr229-stamp-`. The runbook (`docs/runbooks/rdr-229-p31-rollback.md`,
    "Orphan rows") and the doctor name this step. The gates, in order (a gate holds only if the one before it does, so gate 3
    implies gate 2; T2 `229-research-260`):
    1. Every stamp move and every P3.3a move complete: P3.3b's pass
       starts.
    2. Also both swap moves and the three child validation moves complete:
       P3.4's builds start.
    3. Also P3.4 complete on the install. The PCI clause is read from the
       database, never from the reconciler's in-process plan, so a standby
       computes the same answer (T2 `229-research-261`). Each live,
       unsuperseded collection that the planner would index needs a VALID
       id-predicate index if it holds a name-predicate index or an
       id-predicate index of any validity, or if it has at least `B` rows
       (`B` is the planner's build threshold, `NX_SEARCH_PCI_BUILD_MIN_ROWS`)
       and no index of either kind; of those, only as many as the leaf has free id
       slots (`NX_SEARCH_PCI_MAX_PER_LEAF` less the collections that already hold
       a required index) are required, in the planner's order
       (`PciGate3Coverage.java:43-52`; T2 `229-research-263`, `229-research-281`). A collection
       whose name index is gone but whose id index is VALID stays covered, so
       the retire of name indexes does not flip the clause. Validity is
       `pg_index.indisvalid`, read through `PciCatalog` (`PciCatalog.java:356-357`).
       Coverage is decided by the id index, never by a name index's existence,
       because the planner drops a name index before it builds (residual S-2). With `NX_SEARCH_PCI=0` the
       clause is not applicable (T2 `229-research-262`). An empty plan is not a
       pass while live collections are registered: "no collection requires an
       id-predicate index while live collections are registered, so nothing
       says they are covered" (`CompletionCheck.java:531`; T2
       `229-research-264`). The one exception is `nothingToIndex`: when no live
       collection has an index or reaches `B` rows, the clause passes
       (`PciGate3Coverage.java:66-69`, `CompletionCheck.java:177-195`; T2
       `229-research-285`). The source is `PciGate3Coverage`, wired as
       `PciReconciler.gate3Source()`; `Gate3Source.NOT_INSTALLED` answers
       nothing. Gate 3 also needs every runtime id index VALID
       (`IdUniqueIndexPass.allValid`, T2 `229-research-267`). The canary twin
       waits for changeset `vectors-078-8`, and gate 3 waits with it
       (`ArbiterTwins.java:45-46`; T2 `229-research-268`). That changeset is
       not in this tree: `ArbiterTwins.java:39-41` ties the replacing writer to
       P3.5b's form A, and vectors-078 comes with P3.5b (T2 `229-research-269`). Then
       the readers move to the id: `CollectionRegistry` is keyed by id
       (P3.5a; T2 `229-research-308`), and the jOOQ readers switch with the
       latched gate 3 (P3.5c). The check registers P3.7's name-key drop move (Switch), the
       rename becomes one row (P3.7) once that move also has `completed_at`
       set, and the check registers P4.1's drop move once P4.1's release
       is installed.
    Until gate 3, the readers that hold a `Gate3` take the name key from
    it (in this tree `CatalogRepository`'s four reads: the graph walk's
    scope, the purge, the re-home and its status,
    `:5366`, `:10424`, `:11961`, `:12030`; the registry holds no gate; T2
    `229-research-300`, `229-research-308`, `229-research-314`), the jOOQ
    readers read the name columns, and
    `renameCollectionTxn` keeps
    rewriting the fourteen `COLLECTION_SCOPED_TABLES`. From gate 3 until
    the one-row rename can run, that is until P3.7's name-key drop move
    has `completed_at` set and every P3.5b changeset has applied (below),
    readers are on the
    id while the rename is still the name path, which gives the collection
    a new id that its index on the old id does not select (T2
    `229-research-53`, `229-research-63`). The engine therefore refuses a
    rename in that window: gate 3 holds and not both of those. The typed
    error is 409 `rename_refused` with `missing` `name_key_drop_move` while
    the move has no `completed_at`, else `restart`, for the P3.5b changesets
    that have not applied (`RenameRefusedException.java:44-53`,
    `HttpUtil.java:643-647`; T2 `229-research-301`, `229-research-303`).
    A third value, `latch_row_missing`, is the guard's own refusal outside
    that window: readiness is complete (the move done and every P3.5b
    changeset applied) but the install has no latch row,
    an inconsistency such as a partial restore (`RenameReadiness.java:35-39`,
    `RenameGuard.java:156-164`; T2 `229-research-313`).
    The latch row decides the path, not the in-process signal
    (`RenameGuard.java:74-76`; T2 `229-research-302`). The
    window is that move's run, a short pass over the registry and its
    `on_complete`, and, where a P3.5b changeset has not applied, the time
    to that restart (inferred, not read). The refusal, the drop move's
    registration at gate 3 and the one-row rename ship in one engine cut,
    P3.5's and P3.7's: the drop label joins the read set only with the
    release that ships the move, and a label with no row counts as
    incomplete, so a release with the refusal and not the move refuses
    every rename from gate 3 on, and one with the move and not the
    refusal lets a name-path rename give an id-keyed collection a new id
    (P3.7; T2 `229-research-69`). A rename between gate 2 and gate 3
    costs only an index P3.4 builds again for the new id before gate 3 can
    hold, while readers are on names (inferred, not read). So no search
    reads by an id before that collection's index on it exists.

    The check reports in its own `/v1/status` field, `rdr229_completion`,
    and never in `boot_work`. `boot_work` is work in progress: `nx upgrade`'s
    bundle swap and the boot-write harness count every entry in it that is
    not a parked data move as work. The gates wait by design (gate 3 cannot
    hold until P3.4 builds its id indexes), so a `boot_work` entry for them
    would stay on every install, and on a bundle without pg_background it
    would defer the very swap that lets the stamp moves run. The moves
    themselves, which are work, stay `boot_work` entries. The field holds
    the three gates, the labels still incomplete, the CHECKs missing and the
    tables whose VALIDATE waits, each with operator text that names the
    `nexus.rdr229_restamp` step. `/v1/status` is unauthenticated, so the
    field names no collection or tenant and carries no number that describes
    tenants; the counts come from the authenticated, tenant-scoped
    `GET /v1/catalog/rdr229_completion_counts`. An absent field means "not
    known yet", never "every gate holds". R7's pre-flight is among those
    counts: the caller's tenant's registry rows read, its dangling
    tombstones in total and by content type, and its self-id tombstones.
    A count of 0 with no registry rows read means the registry was not
    visible to the admin role, not that it is clean. Names come only from
    read-only listing queries that `nx doctor` prints for `nexus_admin` to
    run. P3.1 builds the counts; the reader contract for a tombstone that
    names no successor (`RETIRED_NO_SUCCESSOR` and a test per reader) waits
    for P3.5c.

    The check is not read only at boot. The move worker knows in process
    when a move completes: the completion transaction sets `completed_at`
    and returns `Result.completed()` (`VerifyingFinisher.java:244-287`),
    and the worker acts on it (`case COMPLETED -> ...`,
    `DataMoveWorker.java:562`). So the engine re-reads the check when the
    worker records a `completed_at`, and when a reconciler pass ends
    (`Main.java:721`; T2 `229-research-274`), and a 60 s safety
    poll reads again when no event came in that time, in case one was
    missed. The count of rows that keep a CHECK `NOT VALID` is a bounded
    scan, so it is cached for 10 minutes per table. The gates the
    engine's Java acts on (P3.3b's pass, P3.4's builds, registering the
    name-key and P4.1 drop moves, the Java readers, the one-row rename and
    P4.0's floor) take effect with no restart (T2 `229-research-64`). A
    CONTINUE changeset still "applies at the first boot after the move
    completes" (RDR-246 lines 942-944): P3.5b's SQL readers on tables
    outside the chunk family, and P4.1's changesets. Until P3.5b's apply,
    those readers read the name columns, which a one-row rename no longer
    rewrites, so the one-row rename also waits for every P3.5b changeset
    to be recorded as applied (inferred, not read: read from Liquibase's
    `databasechangelog`). When every stamp move is complete and a P3.5b
    changeset is not yet applied, the check asks for a restart in its
    `rdr229_completion` field, which nx doctor shows, and not in `boot_work`
    (inferred, not read: P3.5b ships that text). That is the one restart the
    chain from a half-stamped store needs. A client cache
    keyed by id holds a stable id only from the name-key drop move's
    `completed_at` on the install, when the
    rename becomes one row: before it, the name-path rename inserts a new
    registry row with a new id (T2 `229-research-53`), so until then the
    client refreshes an id-keyed entry on a rename and keys its lookups by
    label.
  - *A blocked move.* A BYO or `--no-pg-bundle` PostgreSQL "gets it from
    its DBA ... Until the step has run, moves report `blocked:`" (RDR-246
    lines 801-805), and nx doctor "shows one red row per `blocked:` or
    `failed:` move" (RDR-246 lines 891-892). On such an install the stamp
    never completes. Readers stay on names, no index is built on the id, a
    rename keeps its fourteen-table rewrite, and Phase 4's drops never apply
    (P4.1). P4.0's floor never holds there either, so a P4.0 engine would
    serve that install migration-only; it stays on a release before P4.0
    until its moves run (P4.0; T2 `229-research-59`).

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
| `catalog_document_chunks.collection` | through the chunks key | Expand; child id key added in P3.3c, name key dropped at P4.1 | rewritten |
| `chunk_orphaned_at.collection` | through the chunks key | Expand; child id key added in P3.3c, name key dropped at P4.1; the name-path rename sets its id (item 6) | rewritten by its `ON UPDATE CASCADE` from the chunks key (vectors-030:1047-1051) |
| `catalog_documents.physical_collection` | no | Expand | rewritten |
| `catalog_documents.corpus` | no | History label: set at index time (`corpus=collection_name`, `indexer.py:2852`, or a user `--corpus`), kept as written | no |
| `catalog_reconcile_rows.physical_collection` | no | Transient: a staging table reaped after 24 hours (`catalog-048-reconcile.xml:103`); no id | no |
| `chash_remap.source_collection`, `.target_collection` | no | Expand | no |
| `pdf_pipeline.collection` | no | Expand | no |
| `reembed_jobs.source_collection`, `.target_collection` | no | Expand | no |
| `taxonomy_centroids.collection` | no | Expand; id key added beside the name primary key in P3.3b | rewritten |
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
non-empty, valid UTF-8 without control characters, at most 255 bytes, no
`/`, no `,`, no whitespace at either end, and not of the minted form (item 8) unless the engine minted it, so a
user cannot claim a label that a later mint would produce. Sam ruled out
`/` on 2026-11-21 (T2 `nexus/sam-ruling-rdr229-label-slash-2026-11-21`),
because a note URI is `nx-note://<label>/<title>` and
`NoteUri.collectionOf` ends the label at the first `/`. The rule can be
relaxed later, behind a URI-encoding design. Sam ruled out `,` and
whitespace at either end on 2026-11-21 (ask `rdr229-label-comma-space`),
because MCP search and plan confinement split a corpus list at `,` and
strip each part, so such a label could be registered but never named
there; it can be relaxed later, behind quoting in those lists.
"Control characters" means
Unicode category Cc, plus the line and paragraph separators U+2028 and
U+2029, which break lines in logs and terminals. Format characters (Cf,
such as ZWJ) are allowed, because scripts and emoji sequences need them.
"Whitespace" is what Python's `str.strip()` removes; with the control
characters refused, that is Unicode category Zs.
Labels are compared as exact strings, with no Unicode normalization, so
the NFC and NFD spellings of one word are two labels. The engine enforces
the rule (`CollectionLabel`) on a new collection's registration, on a
rename target that does not exist yet, on a re-embed target that does not
exist yet, and on a new row in a catalog import. An existing row keeps its
label even if the rule refuses it, as item 8 says existing collections keep
their names. The client checks it at the rename target and leaves
registration to the engine. The rule may drop the conformant shape only
because, after P2.9, no site in the widened routing census (item 4, P1.1)
routes by the label's prefix (item 12). P3.6 waits for it. P3.6's review
found the sites where a label meets a file path: the export default file
name, the orphan-backfill output directory and the legacy checkpoint
file. It now encodes each label as a safe path component
(`safe_label_filename`), so `.`, `..`, `\`, `:` and Windows device names
need no rule clause. P3.6's tests exercise one label with a space, `%`,
`:` and a non-ASCII character on Windows (Cross-Cutting, Windows).

In the registry, a rename becomes `UPDATE catalog_collections SET name =
<new>` on one row; the note-URI and aspect-task rewrites below still touch
the collection's notes and queued tasks.
`renameCollectionTxn` stops inserting, rewriting fourteen tables and
tombstoning. It still rewrites the `nx-note://<label>/` source URIs of the
collection's notes, as it does today; moving note identity off the label is
a catalog-identity change outside this RDR. From P2.10, a Phase 2 engine
bead that P3.6 and P3.8 wait for, `NoteUri.moveTo` itself rewrites only
`nx-note://` URIs that `isNote` classes as notes, and `namesCollection`
selects exactly what `moveTo` rewrites. Today `moveTo` rewrites "{@code
nx-note://<old>/} becomes {@code nx-note://<new>/} and {@code
chroma://<old>/} becomes {@code chroma://<new>/}", and `namesCollection`
selects both schemes with no `isNote` test (T2 `229-research-40`,
`229-research-55`). The rule sits in `moveTo`, not on the rename path,
because four of the five operations that move a document's URI go
through it, and each one's
collision pre-check also goes through it (`CatalogRepository.java`; T2
`229-research-55`):

- the collection update (`:2346`; pre-check `:2200`);
- the re-embed move (`completeMove`, `:7336`, `:7356`, `:7363`; pre-check
  `moveUriCollisions`, `:7401`);
- re-home (`moveScopedTable`, `:11850`; pre-check `:11127`);
- rename (`:11464`, `:11468`, `:11472`, its rows selected by
  `namesCollection` at `:11466`, `:11470`, `:11474`; pre-check `:11127`).

A fifth path copies `moveTo` in plpgsql: `nexus.catalog_reconcile_commit`
(`catalog-048-reconcile.xml`, defined once at `:151`) rewrites a moved
document's URI in its collision pre-check (`:433-441`) and its two
document UPDATEs (`:527-535`, `:545-551`), each with a `WHEN
starts_with(d.source_uri, 'chroma://' || d.physical_collection || '/')`
arm and no file-routed test. A census of every SQL splice of a note scheme
onto a collection finds only these and the run-once `note-uri-001`
backfill (T2 `229-research-62`). P2.10 redefines the function by
changeset with `moveTo`'s rule.

So on all five, `chroma://` residue keeps the segment it was written with,
and so does an `nx-note://` value whose segment is file-routed, which the
shared corpus classes as no note (`"nx-note://docs__1-1__voyage-context-3__v1/docs/catalog.md",
"is_note": false`, `note-uri-corpus.json:11`). Moving either onto a
prefix-free label would turn it into a note, because `isNote` reads only
the segment. That keeps the URI-string classifiers' frozen list complete
(item 12). P2.10 does not come before every prefix-free label: `nx catalog
rename-collection --allow-legacy` makes one today (`collections.py:287-293`;
T2 `229-research-57`), so residue may already sit on such a segment, and
P2.10 also repairs those rows (item 12). A legacy-spelled note that `moveTo` no longer moves becomes a
stale note that the stale-note repair rewrites to `nx-note://<collection>/`
(T2 `229-research-42`). It also still rewrites queued
aspect tasks, which name their collection in the task body: "a collection
rename ... has to rewrite or delete the tasks itself, in the same
transaction" (`AspectTaskRefs.java:23-26`, called at
`CatalogRepository.java:11663`; T2 `229-research-37`, `229-research-314`). The one-row rename needs every
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
labels. Minting waits for P2.9, P2.10 and P3.9. P2.10 stops residue
moving onto a prefix-free label (item 7). P2.9 empties the routing class
that the widened census measures. P3.9 gives the recovery-bundle sites the
bundle's row in place of the recorded name. After both, no site on this
install routes a minted collection by a prefix it does not have (item 12).

A live collection's tuple is unique (Sam, P3.8 design gate v2, T2
`nexus/sam-ruling-rdr229-p38-design-gate-v2-2026-11-21`, on "owner is the
source", T2 `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`): a
partial UNIQUE index on `(tenant_id, content_type, owner_id,
embedding_model, model_version)` over `collectionForTuple`'s candidates
(not superseded, not quarantine). It waits, through an `onFail="CONTINUE"`
precondition, for the re-own move and for no duplicate to remain, and
`nx doctor` names a duplicate it waits on. A mint is `INSERT ... ON
CONFLICT DO NOTHING` and a re-read, or, before the index exists, a
striped advisory lock on the tuple (`nexus.collection_tuple_lock_key`, one
of 256 stripes; `collections-003-1`) and a re-read; a conflicting import, restore,
rename or named registration is a 409 naming both collections. A minted
label that is already live takes the next id, bounded at 16 draws, then
409, so `label == "c" + collection_id` holds for every minted row. The id
comes only from the column's own sequence; a drawn id that is already a
row draws forward once, never resetting the sequence backward, and an id
is unique across the installation where D2's owner-read policy lets the
checks read every tenant. No route accepts a client-supplied
`collection_id`.

**9. The non-stranding rule.** Phase 2 may repoint a reader only after every
migration source it serves has a test that runs a row-less legacy name
through it and passes. Those tests are written first (P2.1) and stay in the
suite until the fallback they guard is retired (item 10).

**10. Recovery bundles carry the row (D4).** From Phase 3 the recovery
bundle and export format record each collection's registry row, including
`collection_id`, label and attributes, and import registers from the row.
Import re-maps the source installation's `collection_id` to the id the
target mints, and keeps the label (or reports a clash with a live label on
the target and refuses that collection). A label of the minted form
(`c<digits>`, item 8) is the source installation's own rendering, and the
same string on the target names an unrelated collection. So import does not
keep it and never treats it as a clash: it registers the row by its
attributes, takes the label the target mints, and reports the pair as
`collections_relabelled` (P3.9). An old bundle, which carries names
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
`229-research-41`). The test reads a booted install's schema, not the
Liquibase walk alone: `BootedSchema` boots the install and runs the data-move
worker and P3.3b's leaf index pass until no move is open, because P3.3b's id
keys are made by the pass and P3.3c's child id keys by data-move
`on_complete` statements (T2 `229-research-113`, `229-research-194`; bead
nexus-p2m1x.31, 2026-11-22). Until P4.1, `collection_id` is not
`catalog_collections`' primary key. `TenantExport`'s `reassignedId` is "the
primary-key column a restore gives new values from the table's own
sequence", so the id is not a reassigned key there. The restore never
writes a file's id as it is. A backup taken before P3.1 has no `collection_id`, and
RDR-248 loads an older file "filling new columns from their defaults", which
P3.1's CHECK would refuse. So each id twin, the tombstones' `superseded_by_id`
included, is filled on the restore's stage (`RestoreIdFill`; the option-(c)
ruling on bead nexus-p2m1x.17, 2026-11-21). A non-null twin is mapped by id
through the restore's source-to-target map (`CollectionIdMap`, a session temp
table). A NULL twin, or a file with no id columns, is filled by name from the
restored registry row with the same `(tenant_id, name)`. The by-id mapping
ships in the P3.5+P3.7 cut, not P4.1, because from P3.7 a rename changes one
registry row and the name columns go stale until P4.1. The name fill ships
in P3.1's cut (T2 `229-research-190`). The registry's own `collection_id` comes from
the target's sequence. A tombstone is inserted
with its own id and re-pointed to its successor's mapped id in a second pass,
after every registry row is in and before any child table loads, and one
whose successor is not in the backup keeps the self id (R7). `TenantExport`
declares the registry's `collection_id`, `superseded_by_id` and every twin as
`importComputed`, so they are maintained on restore and not copied. A file's
id that the backup's registry does not hold, or a NULL-twin name with no
registry row in the backup, is an orphan: the restore refuses it before it
writes anything, with the code `restore_orphan_collection_id` or
`restore_orphan_collection` naming each table, the count and an example, so a
backup that holds one orphan row in a CHECK-guarded table is refused whole
where it used to restore. A history table (`relevance_log`,
`search_telemetry`) has no CHECK, so an unmapped non-null twin there gives
NULL and is not refused. An id that the backup's registry holds on two rows
is left out of the map, so in a CHECK-guarded table it is refused as an
orphan and in a history table it gives NULL (`CollectionIdMap`; T2
`229-research-114`). A NULL-twin name that another row of the table
carries with a different collection's id is refused as
`restore_ambiguous_collection_name` (T2 `229-research-190`). The quarantine tag's
`origin_collection_id` is the source installation's id: an id the map holds
is mapped, and any other is stripped. It is never resolved by the tag's
`origin_collection` name, which may be stale (T2 `229-research-192`). The by-id map ships in the
P3.5+P3.7 cut, not P4.1: from P3.7 a rename changes one registry row, the name
columns go stale until P4.1 drops them, and a name cannot be trusted to join.
RDR-248's own rule binds the
Phase 4 column drops: "a migration that drops or retypes an exported column
goes red there and must say what it does to old backups" (lines 580-582).
P4.1 therefore states what it does to a backup taken before P3.1, and
RDR-248's committed fixture is part of its acceptance.

A tenant restore relabels a minted-form label as the bundle import does
above (P3.9). The registry draws its ids first and builds the map; the
minted-form labels then become `c` plus the new id, and the same old-to-new
pairs go into every name column, into a chunk's `origin_collection` tag and
into the `nx-note://` prefix of a document's `source_uri`. Every other table
takes this relabel before its ids are filled. Each changed label is reported
as `collections_relabelled`; the restore marker lists the first 1000 and
carries the whole count as `collections_relabelled_count` (T2 `229-research-191`).

A tenant restore also changes that tenant's catalog epoch, a 16-hex value
that a client may key its cached ids by. The epoch is the first 16 hex
characters of a SHA-256 over the cluster's system identifier, its checkpoint
timeline, its postmaster start time and the tenant's restore marker. It is
read on each call. It is empty unless the session is scoped to the tenant and
the engine's role can execute `pg_control_system()`,
`pg_control_checkpoint()` and `pg_postmaster_start_time()`, and it is also
empty when one of those functions answers NULL (T2 `229-research-111`).
`CatalogEpoch.of` computes it; the code is reviewed and waits on its own
branch (`feature/nexus-p2m1x.31-catalog-epoch-r2`, 9c6381d479), not yet on
the stack. No response carries the epoch yet; the response that does omits
it when empty (bead nexus-p2m1x.31, comment 11:59) and ships in the
P3.5+P3.7 cut (comment 08:24; T2 `229-research-119`). `/v1/status`
reports `catalog_epoch_available` as a boolean, absent when the probe cannot
run; the engine logs a WARN each time that state moves into "unavailable" or
"cannot tell". No main-resources changeset grants the functions; a test
changelog grants them to PUBLIC (T2 `229-research-111`). A PostgreSQL
restart changes it too, so a client that keys its cached ids by the epoch re-resolves them by label once after each
restart (T2 `229-research-193`, `229-research-195`).

**11. `legacy_grandfathered` goes (D5).** Phase 3 stops writing it: the
client stops deriving it, and `collectionForTuple` drops its
`legacy_grandfathered = false` filter (a grandfathered collection has the
same attributes as any other). Phase 4 drops the column and
`idx_catalog_collections_legacy` with it.

*Amended 2026-11-20 (lead ruling, T2
`nexus_rdr/229-lead-ruling-for-tuple-filter-drop-to-phase-2-2026-11-20`).* The
engine half, the filter drop, moves from P3.8 to the Phase 2 engine cut,
0.1.200 (P2.8's engine half): with the filter in place
(`.and(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED.eq(false))`,
`CatalogRepository.java:10509`), `for_tuple` answers 404 for every grandfathered
row, so P2.8's acceptance cannot pass against a real engine. The reembed
target selection's mirror filter, `.filter(r -> !r.legacyGrandfathered())`
(`TargetSelection.java:151`), goes in the same cut, so the two keep the same
winner, as P1.4's tie order made them. The client keeps deriving the flag until
P3.8, and Phase 4 drops the column; both are unchanged. Sam sees the drop as a
named behaviour change in the 0.1.200 go, and can strike it: the census reads
D5 winner changes 0 on both tenants, and 4 nexus tuples gain a winner (code and
docs for owners 1-3 and 1-5, T2 `nexus_rdr/229-p1.2-census-2026-11-20`). Its
consequences (the P1.R critique): those 4 tuples' rows carry an empty
`model_version`, so `_tuple_registered` (`http_catalog_client.py:3924`) flips and
`collection_for(bump=True)` returns v2 where it returned v1; and a grandfathered
non-conformant row with an empty `model_version` can now win a tuple, where
`version_from_for_tuple_row` raises (`collection_name.py:182`), which P2.8
decides. Inferred, not read: the P2.8 test for that row is not yet written.

**12. Routing by content type reads the row (closes the fourth class).**
No name parsing comes back for these sites. Each prefix test becomes a test
of the row's `content_type`, read through the source its caller already
has. The routing class is 24 sites plus the `taxonomy__` half of the 12
`_BYPASS_SCHEMA_PREFIXES` tests, 35 client tests, and one engine site,
`NoteUri.isFileRoutedCollection`: 36 in all (the census counts 24 and 12, so
36 client tests and 37 with the engine site, by adding `topic_route.is_prose`;
`test_the_routing_class_is_measured_not_listed` pins the 36) (T2 `229-research-34`,
`229-research-39`, `229-research-49`):

- *Listing rows in hand.* `health.py:12642`, `:12645` and
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
  `metadata_schema.chunk_content_type_for_collection` (`:187`); and
  the `note_tags_array` rung. `exporter.import_collection` reads through its
  `db`'s own row resolver. A router that writes, or reads for a write, takes
  item 1's three outcomes through one helper, `corpus.routing_content_type`:
  a Row answers with its `content_type`; NoRow (after one cache re-read,
  bounded to one extra listing per interval) and Unreadable answer with the
  candidate name's content type (`corpus.candidate_content_type`, the one
  parse, which P2.2 points at `legacy_names`), one mint entry per caller, all
  empty at P3.8. A prefix-free label with no row derives nothing and stays
  unrouted until P3.8's writer uses the registering call's row. The
  `note_tags_array` rung reads strictly, so a failed listing is not "no
  strings". A read-only router that sees only a search hit's collection
  (`scoring._is_code_collection`, `scoring.apply_quality_boost`) reads the
  row alone: a hit's collection holds chunks, which a row always backs (T2
  `229-research-36`), so its NoRow is a stale cache and answers "not that
  type" until the next refresh. `topic_route.is_prose`, which sees the
  request's collections, takes the NoRow fallback.
- *The `taxonomy__` half of `_BYPASS_SCHEMA_PREFIXES`.* P2.5 moves the
  `quarantine-` half to `lifecycle_state`, and in doing so routes all 12
  sites through one helper, `nexus.db.rows.lifecycle.bypasses_canonical_schema`
  (P2.5's client commit). The client census's routing class went from 36 to
  25 in that commit (`tests/test_collection_name_parse_census.py`). No writer in `src/nexus` creates a
  `taxonomy__` collection, so the half only ever meets legacy names (T2
  `229-research-39`), and no row attribute says "taxonomy": a legacy name has
  no row, and hygiene-002 would give it `content_type='unknown'`. So P2.9 does
  not move it to the row. That helper's `taxonomy__` test,
  `catalog/manifest_backfill.py`'s carve-out (`:865`) and
  `commands/taxonomy_cmd.py:109` are migration-source (a name from an older era
  with no row), and since P2.2 all three read it through
  `legacy_names.is_taxonomy_collection_name`; the engine's
  `ChunkKeyPlan.TAXONOMY_PREFIX` is the same class. P4.3 retires them once a
  census shows no install holds a `taxonomy__` collection. With them
  reclassified and the other sites moved, P2.9 measures the routing class at
  zero (client census 240 -> 229 at develop 0d9e53391 with P2.8). On the
  Phase 2 integration tree the client census is 217 (7817cd3251, then
  a92f88c6a0: P2.2 -11, P2.4 -3, owner-is-source +2 migration-source, which
  was +3 until `owner_stem` moved into `legacy_names`; migration-source 22,
  `split` 2). P3.1 leaves 215. Its register-before-write seams (`put_note`,
  the `.nxexp` import pre-pass, the `doc_indexer` pre-flight, the 422 retry
  and `HttpCatalogClient`'s three manifest writes) are counted once, as mint
  seams, through one primitive,
  `registration_retry.register_collection_for_write`. Its one call to
  `ensure_collection_registered` is the one new site, a wrapper's callers
  are not sites, and `HttpCatalogClient`'s three registrations become it,
  so 217 falls to 215. That is a count of seams, not of callers that
  stopped passing a name: seven call sites still pass one, by design,
  because registration is the name-to-id mint and P3.8 replaces one body.
  Then P3.8 (-2) leaves 213, and P3.6 211:
  `validate_collection_name` no longer
  reads any part of a name, so it left the derived helper set, and its two
  `T3Database` callers left the ledger (mint 131 -> 129, primitive_call
  179 -> 177).
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
  not a URI segment (T2 `229-research-42`). P2.9 deletes it. Its caller,
  `classifyStaleNotes`, runs inside `tenantScope.withTenant(tenant, ctx ->
  ...)`, so it holds `ctx` and `tenant`, and reads each candidate
  collection's `content_type` from `catalog_collections` in that transaction,
  not through `CollectionRegistry.require`, whose cache is evicted only
  after a writer commits (an upsert evicts since nexus-afkjy), so it cannot
  see a change inside this transaction. Only a `knowledge` row is a rewrite candidate; docs,
  rdr and code refuse as `file_routed_collection`, a missing row as
  `unregistered_collection` and any other type as `unproven_content_type`,
  and the rewrite re-reads the type under a share lock on the collection
  row, so a retype cannot land between the census and the UPDATE.
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
  (callers `CatalogRepository.java:4242/4251/4456/4463` for `isNote`,
  `:934/2351/3475/4416/14778` for `canonical`; T2 `229-research-314`);
- on the client, `aspect_readers.is_note_uri`, `canonical_note_uri` and
  `_collection_is_file_routed` (`aspect_readers.py:126`; callers
  `exporter.py:927`, `commands/catalog_cmds/reconcile_stale.py:380`,
  `db/serving_engine_version.py:149`);
- and the SQL in `db/note_uri_pairs.py:69`.

The engine's recorded design is that NoteUri "classifies a source_uri
STRING, not a live collection: a note URI's collection segment can name a
collection that was renamed or never registered, so a CollectionRegistry
lookup cannot answer it" (`CollectionParseGateTest.java:239-247`). The
list stays complete because of one fact, one repair and one rule:
- A new collection's file-routed documents get `file://` and its notes
  `nx-note://`, never `chroma://` (`aspect_readers.uri_for`; T2
  `229-research-40`).
- Residue that already sits on a prefix-free segment is repaired. Such
  labels exist today: `nx catalog rename-collection --allow-legacy` skips
  "the is_conformant_collection_name gate on the new name"
  (`collections.py:236`, gate at `:287-293`), and `moveTo` has moved the
  collection's residue onto the new label, and onto any such label a
  re-home, re-embed move or collection update targets, as
  `catalog_reconcile_commit` has for a reconcile repoint (T2
  `229-research-57`, `229-research-62`). How many rows that touched is unmeasured; P1.2
  counts them on every install. P2.10 registers one RDR-246 data move per
  table that holds a source URI (`catalog_documents`, `document_aspects`,
  `document_highlights`) that restores a file-routed segment: a
  `chroma://S/rest` value whose segment `S` has no `rdr__`, `docs__` or
  `code__` prefix, on a row filed under `S` whose registry row has content
  type `code`, `docs` or `rdr`, becomes `chroma://<content_type>__S/rest`.
  A content type's name prefix is `<content_type>__` (`CollectionName`
  renders `f"{self.content_type}__{self.owner_id}..."`,
  `collection_name.py:90-93`), so `isNote` classes the value as residue
  again and the stale-note repair skips it. Inferred, not read: every
  URI-move path leaves a row's URI segment equal to its new collection
  when its transaction commits (the rename in two UPDATEs, the URI's
  selected by the new collection, `CatalogRepository.java:11412-11414`,
  `:11463-11466`; the others in one; T2 `229-research-67`), so
  moved residue names its own collection; a row whose new value a live
  document already holds (catalog-016) is skipped, stays counted, and
  leaves the move's verification empty. The `file://` identity is not
  the target: `uri_for` returns `"file://" + os.path.normpath(source_path)`,
  which a SQL template does not reproduce (inferred, not read), and a
  `file://` value can collide with the document a later re-index made for
  the same path. No safe rule covers two shapes, which P1.2 counts and
  this RDR leaves unrepaired: residue whose segment no longer names its
  collection, and residue that `note-uri-001` rewrote to `nx-note://<label>/`
  because the rename came before that changeset (inferred, not read: it
  rewrote every `chroma://` value whose segment is not file-routed,
  `note-uri-001-backfill.xml:24`), which no string rule tells from a note.
- From P2.10 no path moves `chroma://` residue, or an `nx-note://` value
  with a file-routed segment: not `NoteUri.moveTo` on rename, re-home, the
  re-embed move or the collection update, and not
  `catalog_reconcile_commit` (item 7; T2 `229-research-55`,
  `229-research-62`). Residue
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
| Per-collection index | `PciBuilderSession`, `PciCatalog` | Change: a second build on `collection_id` (predicate and name hash) beside the name index. The name index retires only once gate 3 has latched and the readers select by id (P3.5c). |
| Stamp and key swap (Phase 3) | RDR-246: `nexus.data_moves`, `DataMoveWorker`, dml_utils, `MoveBootWork` | Reuse: one registered move per table (per tenant and leaf) for the stamp, one per foreign key for each validation (P3.3a, P3.3c), and one per table whose P3.3c `on_complete` checks the id key beside the name key; no new mover. |
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
  the collection and builds its per-collection index beside the name index; the label is not
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
- **Risk**: The chunks re-key and the per-collection index build beside the name index runs for
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
- **Risk**: An engine rolled back past P3.1's cut cannot write. Its `NOT
  VALID` CHECKs are enforced on every new row version, and an engine from
  before P3.1 knows no id column, so its chunk INSERT, its supersede UPDATE
  and its carrier INSERTs fail with SQLSTATE 23514. P3.1's cut is the
  downgrade boundary, and a tag flip alone is no longer a rollback
  (`docs/runbooks/rdr-225-cloud-deploy.md` section 6.2 stops being true
  there). **Mitigation**: the down-step is supported only while no stamp
  move of a data table has `completed_at` and before P3.7's name-key drop
  move completes (Sam, 2026-11-20, "Only before a stamp completes"). The
  registry's own stamp, `rdr229-stamp-catalog_collections`, does not close
  the window (Sam, 2026-11-21): it registers first, is small, and completes
  about a minute after the first boot wherever the moves run, and what a
  previous engine writes to the registry meanwhile is repaired by the
  re-add and `nexus.rdr229_restamp('catalog_collections')`. Outside the
  window a downgrade is PITR only. In practice the window lasts until the
  first data-table stamp completes: minutes on a small install, hours on a
  large one, so the operator reads `nexus.data_moves`, not the clock. It
  stays open while the moves cannot run, which is until the DBA installs
  pg_background on a BYO PostgreSQL or a bundle without it, and on the
  cloud until pg_background is installed on the cloud's PostgreSQL
  (nexus-f3yqd.2.23). The order is fixed. Pause the open stamp moves (the
  `rdr229-stamp-` rows, reruns included), drop the CHECKs, flip the tag.
  After the next P3.1 deploy, re-add every CHECK `NOT VALID` before the pause
  is released: `DATABASECHANGELOG` still records vectors-068 as run, so the
  redeploy adds none, and a stamp move would otherwise fail in its
  `on_complete` with 42704 and gate 1 would never hold. Both scripts are
  generated from `p31-checks.tsv`. The down-step leaves vectors-069's 24
  function bodies in place. `canary_query_ring_upsert` then raises 23503 for
  a name with no registry row, where it used to skip the entry, and the
  previous engine's ring writer drops a whole batch when one entry raises;
  `catalog_reconcile_commit` refuses an unregistered `physical_collection`.
  The cloud deploy stays stop-start on one replica. The operator text is
  `docs/runbooks/rdr-229-p31-rollback.md`, a conexus section that nexus
  does not run.
- **Risk**: A single-role install, where `nexus_svc` is or inherits the
  migrating role (RDR-246's supported BYO posture), boots P3.1 and then
  cannot stamp. P3.1's owner-read policy changesets (`<table>_owner_read FOR
  SELECT TO CURRENT_USER USING (true)` on `topics`, `topic_assignments`,
  `taxonomy_centroids`, `taxonomy_meta`, `catalog_document_chunks`,
  `chunk_orphaned_at`, `chash_remap`, `reembed_jobs`, `canary_query_ring`,
  `pdf_pipeline` and `aspect_extraction_queue`, following RDR-246's design
  and the vectors-029 and catalog-050 precedents) are MARK_RAN there, and the
  queue's is skipped and retried, because a policy for the migrating role
  would also let the serving role read every tenant's rows. So
  `MovePreflight` refuses every stamp move on that install: row-level
  security narrows the admin view and nothing can lift it. Gate 1 never
  holds there, and readers stay on names, as for a blocked move (item 6, "A
  blocked move"). Two things follow. `vectors-068-19`'s guard reads no
  registry row it cannot see and so counts the registry clean, which is
  sound only because no move can run an UPDATE that meets the CHECK. And a
  DBA who later adds an owner-read policy by hand lets D2's repair move run,
  and a tombstone with a blank `model_version` then fails its UPDATE with
  23514 until its `superseded_by_id` is stamped; the completion check and
  `nx doctor` name the wait. **Mitigation**: the single-role boot test
  (`Rdr229P31SingleRoleSupersededByCheckTest`) shows the boot completes with
  the CHECK present and `NOT VALID`. P3.5 decides what a single-role install
  does before it relies on the gates, because there the gates never hold.

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
- A pre-P3.1 engine runs after P3.1's CHECKs and the down-step was not run:
  it serves reads and fails every write that touches a guarded table with
  SQLSTATE 23514, naming a constraint from `p31-checks.tsv`
  (`chunks_collection_id_check` on an index or upsert,
  `catalog_collections_superseded_by_id_check` on a supersede). A refused
  write wrote nothing. The fix is the down-step, with the pause first if a
  stamp move is open, and needs no restart (Risks).
- A stamp move finds a row whose name has no registry row in its tenant (an
  orphan): the move completes, the table's CHECK stays `NOT VALID`, and the
  row is write-frozen, since any UPDATE of it fails 23514 while its id is
  NULL. `nx doctor` names the table, and `nexus.rdr229_restamp(table)`
  finishes the stamp once the orphans are registered or deleted (item 6).
  A tenant restore refuses a backup that holds such a name, with
  `restore_orphan_collection`, before it writes (item 10).
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
- [ ] Critical Assumptions 1 and 6 measured (P1.2; done 2026-11-20), and
  Critical Assumption 3's residue rows counted there on every install (open: it
  needs the 0.1.200 route; read on both tenants after that deploy, as a
  post-deploy confirmation and not a move gate: the deploy go is the go for
  the moves' first cloud run, T2 `nexus/sam-ruling-0200-move-gate-2026-11-21`).
- [ ] Critical Assumption 5 measured (P3.2) before P3.1's changeset is
  final, and so before the Phase 3 switch changesets: it sets each move's
  `chunk_size`, `sleep_ms` and `statement_timeout_ms`.
- [ ] RDR-246's data-move mechanism active on the cloud before the Phase 2
  moves and Phase 3 run there (the first registered move is D2's
  `model_version` repair, which rides the Phase 2 engine cut: "the first
  registered data move (the unlock for RDR-246 Step 2)", T2
  `nexus/sam-ruling-rdr229-p12-row-decisions-2026-11-20`):
  nexus-f3yqd.2 ("Data moves run as background jobs via dml_utils, never at
  boot") is in progress, with nexus-f3yqd.2.23 ("Phase 2 activation, waiting
  on Sam's go: cloud pg_background install and the first registered move")
  open (T2 `229-research-32`). It gates the first cloud run of the Phase 2
  moves and of P3.1's move, not their registration (T2 `229-research-57`).
  The first registered moves .2.23 waits for are D2's (`vectors-064`),
  owner-is-source's re-own (`rdr229-reown-repo-stems-from-curator`,
  `vectors-067`) and P2.10's three repair moves, all in 0.1.200; the 0.1.200
  deploy go is the go for their first cloud runs, each named with its
  measured count (T2 `nexus/sam-ruling-0200-move-gate-2026-11-21`,
  `nexus/engine-cut-0.1.200`). A Phase 3 cut can still reach an install whose moves are
  blocked. There its readers stay on names (item 6, "A blocked move"). A
  BYO or `--no-pg-bundle` PostgreSQL "gets it from its DBA" (RDR-246 lines
  801-802); until then that install is such a case (Cross-Cutting, "BYO
  or `--no-pg-bundle` local PostgreSQL").

### Minimum Viable Validation

On a substrate-backed engine (the local-service gate), a fixture tenant with
one collection whose row was corrected so its `owner_id` and
`embedding_model` differ from its name: `nx index md` into it stamps the
row's model into chunk metadata, `nx search` calibrates with the row's
model, and a quarantine of one of its chunks registers a sibling whose
`lifecycle_state` is `quarantine` without the engine reading the prefix. In
the same run, a recovery bundle recorded under a legacy name restores. Both
census gates are at their Phase 2 pins: on the client, `holdover` 0, `routing`
0 and `uri-classifier` 2; on the engine, the collection-split and
quarantine-literal ceilings 0 (`CollectionParseGateTest`). After Phase 3, the same fixture is
renamed and searched: the rename touches one registry row, its chunks and
per-collection index are unchanged, and `store_put` to a new subject
registers a collection with a minted label that `nx search --corpus
<subject>` finds.

### Proposed beads

Not created; this list is for planning at acceptance. Each implementation
bead is test-first: its failing test lands first in the same branch, and
its acceptance is that test green plus the census pin it lowers.

**Phase 1 (CLOSED, 2026-11-20): the census and the measurement**

- P1.1 Widen the client census (new shapes, derived helper set, class label
  per site, an interim `holdover` class, and the non-parse labels `label-pattern`
  and `file-path`) and re-pin at the measured count (243; 265 after P1.R's
  cross-module roots). The shapes include every shape
  of the routing census (item 4; T2 `229-research-39`), so the routing
  class is measured, not listed. First live run pasted into the bead.
- P1.2 Read-only census on both production tenants: per live row, row versus
  name for content type, model, version and lifecycle; owner differences
  counted separately as expected; live labels that would clash under a
  partial UNIQUE (Critical Assumption 6); live `(content_type, owner_id,
  embedding_model)` tuples with more than one candidate row, which
  `collectionForTuple` settled by `NAME ASC` behind a grandfathered
  filter D5 drops (P1.4 moved the tie to `created_at` then name; the filter
  drops at 0.1.200, item 11); and live names that already match `^c[0-9]+$`, the
  minted form (T2 `229-research-38`); and tombstones whose `superseded_by`
  names no row of their tenant, which the stamp cannot give an id (item 6;
  T2 `229-research-50`). Bounded load, results in T2.
  Also, on every install as a pre-flight and not only on the two tenants,
  the rows of `catalog_documents`, `document_aspects` and
  `document_highlights` whose `chroma://` segment has no `rdr__`,
  `docs__` or `code__` prefix, split by whether the row's collection has content type `code`,
  `docs` or `rdr`; among those, the rows whose segment is not their own
  collection; and the `nx-note://` rows of such a collection whose segment
  has no `rdr__`, `docs__` or `code__` prefix (Critical Assumption 3,
  item 12; T2 `229-research-57`). As shipped: a new engine route,
  `GET /v1/catalog/uri_segment_census` (`UriSegmentCensusReads.java`; engine
  0.1.200, announced by `catalog_uri_segment_census_supported` on `/version`),
  read by a non-gating `nx doctor --uri-residue` row
  (`catalog/rdr229/uri_residue_doctor.py`, `commands/doctor.py:2978`) that
  reads "not checked" on an older engine; and `nx catalog collections-census`
  for the row-versus-name measurement. The residue count is read on both
  tenants after the 0.1.200 deploy and before Sam's go for the first cloud run
  of D2's and P2.10's moves.
  Blocks P2.2 onward.
- P1.3 Widen the engine Java gate to the `quarantine-` literal; add the SQL
  live-body gate. First live run pasted into the bead.
- P1.4 `http_catalog_client` reads the next version from the `for_tuple`
  row's `model_version`, not by parsing the returned name; `collectionForTuple`
  breaks ties by a stable column, not by name. As shipped:
  `version_from_for_tuple_row` (`catalog/collection_name.py:147`) raises when
  the row's `(content_type, owner_id, embedding_model)` differ from the tuple
  asked for (`:171-175`) or its `model_version` is malformed, and for an empty
  `model_version` falls back to the returned conformant name with a
  `collection_for_row_model_version_empty` warning (`:177-188`). The fallback is
  temporary, removed once P1.2's census counts zero empty rows; no bead owns
  the removal, and D2's cloud run (below) is its trigger. The tie is
  `created_at` ascending then name (`CatalogRepository.java:10560`), gated on
  P1.2's census, which read `created_at_winner_differs` 0 on both tenants. P1.4
  lowered no census pin: the name parse it removed
  (`http_catalog_client.py:3875`) was never a counted site, and the fallback adds
  the counted caller `collection_name.py:180`. Test: a row whose
  `model_version` differs from its name's suffix; and a repo with a legacy
  slug collection and a conformant collection on one tuple resolves the same
  collection before and after P1.4 where no legacy slug is older than its
  conformant sibling, which is what the tie order is gated on
  (`collectionForTuple_aLegacySlugOlderThanItsConformantSiblingNowWinsTheTie`,
  `CatalogRepositoryTest.java:2297`, pins the flip when one is); and again after
  D5's filter drop, which the lead ruling of 2026-11-20 moved from P3.8 to the
  Phase 2 engine cut, 0.1.200 (item 11).
- P1.R Review gate for Phase 1 (code-review-expert and substantive-critic;
  "Ship when the three gates are green at pins that match a fresh census").

**Phase 2: readers move to the row, and residue stops moving**

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
  *Amended 2026-11-20 (bead nexus-p2m1x.9; the lead's ruling answered S4 of
  the client critique, T2 `nexus/critique-p2m1x9-2026-11-20-r1`, and this text
  comes from S1 of the engine critique, T2
  `nexus/critique-p2m1x9-engine-2026-11-20-r1`).* With the threshold read
  from the row, the engine's `POST /v1/taxonomy/assignments/prune_projection`
  (`TaxonomyRepository.pruneProjectionBelow`, `startsWith` on
  `source_collection`) over-deletes: pruning `..__v1` at its threshold also
  reaches `..__v10`, and `docs__art` also reaches `docs__art-x`, each at the
  wrong threshold. P2.4 therefore owns a small engine half in the Phase 2
  engine cut, 0.1.200: an optional request field `match` (`"prefix"`, the
  default when absent or null, keeps `startsWith`; `"exact"` deletes only the
  named `source_collection`; any other value is a 400 that deletes nothing),
  a reply that echoes the mode applied (`{"removed": N, "match":
  "prefix"|"exact"}`), and `taxonomy_prune_exact_supported: true` on
  `/version`. The client sends `match: "exact"` only when the flag reads true,
  and then only after one verification prune (`match: "exact"`, for a name no
  collection has: the fixed prefix `nexus-exact-probe-` and a random 128-bit
  suffix, so no collection name starts with it except by a collision of
  negligible chance, and even a prefix match therefore reaches no rows; the
  guarantee is the name, not the threshold) whose
  reply echoes `"exact"`; a missing or `"prefix"` echo, or a failed call, runs
  the whole sweep guarded, and a real reply that stops echoing `"exact"` ends
  the sweep at once. Without the flag the client keeps its guard (it prunes a
  prefix name down to its extensions' lowest threshold and prints the skipped
  count). No client floor bump: the flag and the echo are the gate. The
  route's field keeps its name, `source_collection_prefix`; the P3 id form
  (P3.5c) is not part of this change. The cloud leg proves the body field
  crosses the edge (`match: "bogus"` on a name that matches nothing must
  answer 400). Tests: a `..__v1`/`..__v10` pair and a `docs__art`/`docs__art-x`
  pair, each in prefix and in exact mode. [additive] wire entry.
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
  `resolve_corpus` stage 3 to the row tuple. Its fallback, a prefix match on
  the label the user typed, stays as one `label-pattern` site with the
  `LABEL_TYPED` note (item 1).
- P2.8 Subject and repo resolution by attributes (item 5):
  `t3_collection_name` finds a knowledge subject through `find_collection`;
  `collection_for_repo` uses the returned row instead of rendering. Test: a
  knowledge collection whose label is not of the conformant shape is found
  by its subject.
- P2.9 Routing by content type reads the row (item 12): the 24 routing
  sites (23 and `topic_route.is_prose`) and the `taxonomy__` half of the `_BYPASS_SCHEMA_PREFIXES` tests
  (T2 `229-research-39`, listed in item 12; since P2.5 the 12 tests are one
  helper, `nexus.db.rows.lifecycle.bypasses_canonical_schema`, so this half is
  one site, and it is migration-source, not routing: item 12), the shipped `"code__*"` exclude
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
- P2.10 Engine and client: `NoteUri.moveTo` rewrites only `nx-note://` URIs that
  `isNote` classes as notes, and `namesCollection` selects only those; a
  changeset redefines `nexus.catalog_reconcile_commit` with the same rule
  in its three URI rewrites; so no path moves `chroma://` residue or an
  `nx-note://<file-routed>` value (item 7; T2 `229-research-55`,
  `229-research-56`, `229-research-62`). It changes no schema. Its
  changeset also registers the repair of
  residue already on a prefix-free segment, one data move per table
  (`catalog_documents`, `document_aspects`, `document_highlights`), with
  item 12's rule as the template and, as the verification, the rows that
  still match it with a free new value; never a boot rewrite (RDR-246).
  The `catalog_documents` move runs first and its `on_complete` registers
  the two satellite moves, as the chunks swap registers its child moves
  (item 6). Only documents can meet a held value: highlights are unique
  on `(tenant, doc_id)` and aspects on `(tenant, collection,
  source_path)` (`CatalogRepository.java:7319-7322`). So a satellite row
  is repaired only where its document was, and one whose document was
  skipped for a held value is skipped too and counted. From P3.1's cut,
  P3.1's CHECK would refuse a repair UPDATE of a row the stamp has not
  reached, so P3.1's writer census includes the move templates open at
  its cut, and P3.1's changeset supersedes any P2.10 repair move not yet
  complete with one whose template also sets `collection_id` where the
  row has none ("a new move with a new label" that "names the old label
  in `supersedes`", RDR-246 lines 961-963; T2 `229-research-67`).
  The replacements are `catalog-052`, one changeset per registry state, taken
  when the walk reaches it. While the documents move is open or failed it is
  superseded by `rdr229-p31-residue-catalog-documents`, whose `on_complete`
  registers the replacement satellites (`rdr229-p31-residue-document-aspects`
  and `-highlights`) under new labels, because a superseded move never runs
  its own `on_complete`. An old satellite that is registered and not
  complete is superseded by its own replacement. When all three are
  complete, nothing is registered, and an unstamped row is the stamp's work.
  A replacement's template is the old one with the id added to the SET,
  `<id> = COALESCE(t.<id>, c.collection_id)`, so the match rule, the
  verification, the sizing and the held-value rule stay P2.10's, and a row
  whose name resolves to no registry row is not matched. A pause carries
  over: `catalog-052-4` copies the pause of a superseded documents move onto
  its replacement, and a superseded satellite's pause goes to its own
  replacement. The replacement satellites that the replacement documents
  move registers take the replacement's own pause at the moment its
  `on_complete` runs, not the old row's, which keeps its pause after the
  supersede; normally the replacement is not paused then, because the
  operator resumed it, so they arrive unpaused and run. P3.1's writer census
  covers the registered move templates, and its census test flags the three
  P2.10 moves when the supersede is left out.
  The first cloud run of those moves waits for nexus-f3yqd.2.23, as
  P3.1's does (Prerequisites). RDR-246 sets no default to borrow: a
  move's chunk size and `sleep_ms` are "the value measured in the Minimum
  Viable Validation, written into the registering INSERT before the
  release" (RDR-246 lines 395-398), and P3.2 times Phase 3 only. So P2.10
  carries its own sizing step before its changeset is final: it times the
  three moves on a PITR fork of the larger production tenant and sizes
  them on a local store, starting from the cloud `sleep_ms` RDR-246's
  validation set, and writes `chunk_size`, `sleep_ms` and
  `statement_timeout_ms` into the INSERT. Inferred, not read: dml_utils
  walks every row of the driving table, so the cost follows the table's
  size, not the residue count (T2 `229-research-66`). On the client, `nx catalog
  backfill-source-uri` today refuses residue that does not start
  `chroma://<physical_collection>/` as "malformed" (`backfill.py:109-111`),
  which after P2.10 is all moved and repaired residue. P2.10 relaxes the
  check to the residue's own segment: a `chroma://` row is a candidate
  when its own segment has a `rdr__`, `docs__` or `code__` prefix or its
  collection's row has content type `code`, `docs` or `rdr`; the path is
  the text after `chroma://<own segment>/`, still required absolute; and
  the new value is `uri_for`'s `file://` result, which does not depend on
  the collection's name. So the verb can still retire residue to its
  `file://` identity, whatever segment it carries (T2 `229-research-65`).
  Test, for the repair: on a fixture, a
  docs collection renamed with `--allow-legacy` to a prefix-free label
  before P2.10 has its residue rewritten to a `docs__` segment and classed
  file-routed; a knowledge collection's `chroma://` note on a prefix-free
  label is left alone; a row whose new value a live document holds is
  skipped and counted. Test, for the rule: moving the documents of a file-routed
  collection that carry `chroma://` residue and an `nx-note://<file-routed>`
  value onto a prefix-free label, by a rename, a re-home, a re-embed move,
  a collection update and a reconcile commit that repoints them, leaves
  both on their old segment, still classed
  file-routed by `NoteUri.isNote` and `is_note_uri`, and skipped by the
  stale-note repair; `NoteUriCorpusTest`'s `moveTo` assertions and
  `NoteUriMoveTest`'s (`:108`, `:187`, `:225`), which pin the `chroma://`
  arm today, are inverted. Test, for the verb: moved
  residue and repaired residue are re-derived to `file://`, not refused.
  Blocks P3.6 and P3.8.
- P2.R Review gate for Phase 2. Engine cut for P2.4 (prune `match`), P2.5, P2.6,
  P2.9 and P2.10 (and P2.8's filter drop, per its amendment); client
  floor bumps in the release that ships P2.3 onward. The same cut, 0.1.200,
  carries D2's move and owner-read policy (`vectors-064`, `vectors-065`),
  owner-is-source's `catalog-051` and re-own move (`vectors-067`), the
  re-embed 409 refusals on a shared or foreign-held tuple (`ReembedHandler`),
  and the RDR-254 aspect upsert deadlock retry (`AspectRepository`).

**Phase 3: the surrogate key and the label**

- P3.1 Expand, after P3.2: `catalog_collections.collection_id` (identity,
  GENERATED BY DEFAULT) with UNIQUE `(tenant_id, collection_id)` and UNIQUE
  `(tenant_id, collection_id, embedding_model)`, the RDR-225 composite's
  future target (item 6), as plain changeset work on a small table; id
  columns on every referencing table (item 6 list, with the column named per
  table), the four history tables (`relevance_log`, `search_telemetry`,
  `hook_failures`, `gc_audit`) among them, which take a bare nullable id and
  no CHECK or stamp; and, `NOT VALID`, the CHECK `(NULLIF(<name column>, '')
  IS NULL) = (<id column> IS NULL)` on each of the others, `''` meaning
  unset, except `superseded_by`, whose CHECK is `(superseded_by = '') =
  (superseded_by_id IS NULL)` and whose stamp resolves a non-empty name to
  the successor's id, or to the row's own id when no successor exists
  (item 6); `CHECK (collection_id IS NOT NULL)` `NOT VALID` on
  `nexus.chunks` and `nexus.taxonomy_centroids`, the id key's guard once
  P4.1 drops the name key (item 6). The 20 CHECKs carry stable names from
  one list, `p31-checks.tsv`, which the operator down-step and re-add SQL
  are generated from (`docs/runbooks/rdr-229-p31-rollback.md`; Risks). Every
  writer (item 6's census: 109 Java write sites, 26 SQL functions) sets the
  id on insert and update from this cut. The 24 SQL functions that write a
  guarded table are redefined by changeset (`vectors-069`) to set it from the
  registry in their own statement. The client registers a collection before
  the first document under it, through one seam,
  `registration_retry.register_collection_for_write` (`put_note`, the
  `.nxexp` import pre-pass, the `doc_indexer` pre-flight, the 422 retry and
  `HttpCatalogClient`'s three manifest writes), because the engine now
  refuses a document under an unregistered collection with a typed 422
  (`unregistered_collection`) before it writes. A write the engine refuses
  that way, for a name this process cached, is registered again and sent
  once more, never twice. `put_note` logs a warning when its early
  registration fails for a reason other than a client-side refusal. The
  acceptance re-runs the census over helper-routed and by-name writes and
  the move templates open at the cut, finds every writer of
  `taxonomy_centroids` before the CHECK lands (item 6;
  `TaxonomyCentroidRepository.java:156` is one), and supersedes any P2.10
  repair move not yet complete (P2.10); `TenantExport`'s table model and
  the restore's by-name id fill in the same cut; the by-id map follows in
  the P3.5+P3.7 cut (item 10, P3.11; T2 `229-research-196`). A stamp move
  over a row-secured table needs the migrating role to read every tenant's
  rows, so P3.1 adds the owner-read policies `<table>_owner_read FOR SELECT
  TO CURRENT_USER USING (true)`, following RDR-246's design and the
  vectors-029 and catalog-050 precedents, one changeset each in `vectors-070`
  for `topics`, `topic_assignments`, `taxonomy_centroids`, `taxonomy_meta`,
  `catalog_document_chunks`, `chunk_orphaned_at`, `chash_remap`,
  `reembed_jobs`, `canary_query_ring`, `pdf_pipeline` and, where the retired
  queue is kept, `aspect_extraction_queue`. `nexus.chunks` has its policy
  already (`chunks_gate_probe_owner_read`), and `vectors-065` and
  `catalog-050` own the registry's, documents', aspects' and highlights'.
  Each is skipped (MARK_RAN) when `nexus_svc`, or another login role subject
  to row-level security, is or inherits the migrating role (Risks, the
  single-role entry); the queue's is skipped unrecorded and retried each
  boot. The stamp is
  registered in `nexus.data_moves` per table, run per tenant and leaf, with
  a pending-row verification query (item 6), the CHECK's VALIDATE as its
  `on_complete`, and the `chunk_size`, `sleep_ms` and `statement_timeout_ms`
  P3.2 set; a census test that counts null ids per table; the boot-time
  completion check, which reports in its own `rdr229_completion` field,
  with the read set and gates later releases extend (item 6, "Readers
  during the stamp"); and R7's pre-flight counts, with the reader contract
  left to P3.5c. Test: an upgrade
  boot registers the move and rewrites no row of a large table; a killed run
  resumes from its last committed batch; an insert and an update without the
  id are refused; a name-path rename gives every rewritten row, and the
  collection's `chunk_orphaned_at` rows, the new registry row's id.
- P3.2 Spike, before P3.1's changeset is final: time the stamp, the chunks
  and centroid key swap (its `on_complete`, with the directly validated
  composite foreign key), the ten foreign-key validation moves, the P3.3b
  unique-index pass, the per-collection index build beside the name index, P3.7's name-key drop
  move and P4.1's
  chunk-family drop on a PITR fork of the larger production tenant, and
  size them on a local store of the largest size class (Critical
  Assumption 5), including the extra id-key index from P3.3b to P4.1.
  Result in T2 and on the bead; it sets each move's `chunk_size`,
  `sleep_ms` and `statement_timeout_ms` ("the bound on each verification
  run and on `on_complete`", RDR-246 line 403), and says whether any
  `on_complete` needs a maintenance window.
- P3.3 Switch the keys, as three beads; "P3.3" elsewhere means all three.
  Walked on a PITR fork before the cloud deploy and on a local store.
  `TenantExport`'s key model changes in P3.3a's and P3.3c's cuts (P3.11).
  - P3.3a Foreign-key repoint: the seven registry foreign keys from tables
    that are not partitioned gain an id twin on `collection_id`, `NOT VALID`,
    beside the name key, which P3.3a does not drop (the name-key drop move
    of P3.7 drops all seven; item 6, Switch), and
    one validation move per referencing table registered in the same
    changeset (driving table that table, template a re-stamp of rows still
    null, the VALIDATE as its `on_complete`; item 6, Switch). Test: each
    move completes and leaves its constraint validated. After P3.1.
  - P3.3b Per-leaf unique-index pass: new engine code in the reconciler's
    pattern builds, per leaf, the unique indexes for the new chunks and
    centroid keys (item 6). It starts once the completion check reports
    every stamp move and every P3.3a move complete. It builds, per leaf,
    the unique index on the new chunks and centroid keys, and then adds
    each table's id key beside the name primary key: the parent's
    constraint and the leaf attachments, once every leaf of that table has
    a valid index (item 6). Its last step registers each table's P3.3c
    swap move. The leaf-creating helper creates a leaf's index only while
    the parent lacks the id key (item 6). Test: a pass ended mid-build
    leaves an INVALID index that the next pass drops and rebuilds; the swap
    is never registered while a leaf's unique index is missing or INVALID,
    or while the parent's id key is not attached; a new leaf made after the
    key is in place gets exactly one valid id index, and one made before it
    gets the plain index the pass adopts; with `NX_SEARCH_PCI=0` the pass
    still builds the indexes and registers the swap; with moves blocked it
    builds no index and registers no swap.
  - P3.3c Swap: per table, a move registered by P3.3b's pass (driving table
    that table, template a re-stamp of rows still null) whose `on_complete`
    refuses unless the id key is already present and valid (it adds none),
    on the chunks swap only, adds the three child id keys `NOT VALID` with `ON UPDATE CASCADE` (the
    manifest's `DEFERRABLE INITIALLY DEFERRED`), registers the three moves
    that validate them (the chunks swap only; the centroids swap adds no child key, T2 `229-research-115`), and validates the RDR-225
    composite on the id directly (item 6). On the chunks swap, the
    `on_complete` also waits for the child index that the boot changeset
    `vectors-075-1` builds. It drops nothing; the name primary key and the
    three name child keys stay until P4.1, and `chunks_collection_fk` until
    P3.7's name-key drop move. Test: no child validation move exists before
    its constraint does; a swap whose id key is missing is refused; after
    the swap, a chunk and a centroid upsert on the name target still succeed,
    and a name-path rename leaves every child row with the new id; EXPLAIN
    of a chunk DELETE after the swap uses `idx_chunk_orphaned_at_tenant_chash`.
- P3.4 After P3.3: build every per-collection index on `collection_id`
  (predicate and name hash) beside its name index, through the PCI
  reconciler, which reports in `boot_work` and rebuilds a killed build from
  the start (a failed build is left INVALID, `PciCatalog.java:356-357`; T2
  `229-research-273`). Each collection's index is built only once none of
  its rows has a null id (stamp-before-build, Scope boundary with RDR-247),
  and no build starts until gate 2 of the completion check (both swaps and
  every validation move complete), so a chunk-family `on_complete` cannot
  end it (item 6; `Main.java:712-716`; T2 `229-research-276`). The step also
  builds the eight id-keyed arbiters and their supporting indexes at run
  time, in `IdUniqueIndexPass`, after the engine serves (item 6; T2
  `229-research-266`). `PciCatalog` reads the id back from the predicate, and
  its records are what gate 3's PCI clause reads from the database to tell
  that P3.4 is complete on the install.
  The name indexes retire (`REPLACED_BY_ID`) only once gate 3 has latched,
  `IdReaders.PRESENT` is true, and the engine holds its singleton lock
  (`Main.java:265`; `PciReconciler.java:643-650`). It is `false` in this release, and P3.5c flips
  it (`IdReaders.java:18`; T2 `229-research-270`). Until the retire ends, the
  id indexes get a budget of their own, the same `NX_SEARCH_PCI_MAX_PER_LEAF`,
  so a leaf at the cap holds the cap's worth of name indexes and the cap's
  worth of id indexes, one build at a time. That is a stated exception to the
  cap, and it is bounded: if the peak extra index size per tenant, taken from
  P3.2's fork sizes, is over the cloud disk headroom or cannot be sized
  against it, the exception goes to Sam (linda, bead nexus-p2m1x.22; T2
  `229-research-271`, `229-research-286`).
  The gate 3 latch is one row, `nexus.rdr229_gate_latch` (`vectors-080-1`).
  The completion check writes it the first time gate 3 holds, with
  `INSERT ... ON CONFLICT DO NOTHING`, and the row carries `pci_off`
  (`vectors-080-rdr229-gate-latch.xml:37`, `CompletionCheck.java:407-415`; T2
  `229-research-255`). The engine reads it at boot, on a short read-only admin
  session, before the listener binds. Each attempt is bounded at 10 s
  (`lock_timeout` and `statement_timeout`). A failed attempt is retried with
  backoff, 500 ms doubling to 30 s, and after 5 minutes in all without an
  answer the engine exits with status 1 rather than serve on a guess
  (`Gate3Latch.java:28-32, 69-70, 78-94`, `Main.java:612-625`; T2
  `229-research-257`, `229-research-280`). The latch stays held for P3.5's
  readers and for P3.7's one-row rename and its refusal (P3.7). P3.5's readers,
  that rename and the name-index retire go by the latched value, never by the
  live gate 3 (`CompletionBootWork.java:165-166`; T2 `229-research-256`,
  `229-research-284`). A later INVALID id index is reported as
  `id_index_invalid` and does not unlatch (T2 `229-research-258`). A restore from a
  backup taken before the latch reads "not latched", because the row rewinds
  with the data, and the install latches again once gate 3 holds
  (`vectors-080-rdr229-gate-latch.xml:19-20`; T2 `229-research-259`,
  `229-research-292`). An install that latched while `NX_SEARCH_PCI=0` has
  `pci_off` set; when PCI is turned on, the reconciler builds the id indexes
  first, as if gate 2 held, and P3.5's readers select by the id either way
  (`PciIdBuilds.java:32-34`, `Main.java:721`; T2 `229-research-272`,
  `229-research-290`).
  Test: a collection with a row whose `collection_id` is null gets no index on
  the id; readers and the rename stay on the name path, before the latch, while any collection
  the planner would index lacks a valid id-predicate index (item 6, gate 3). The bounded drop of the
  shared leaf graph that follows is RDR-247's.
- P3.5 Readers switch to the id, as three beads; "P3.5" elsewhere means
  all three. P3.5 ships in one engine cut with P3.7, which holds the
  rename refusal P3.5a states (P3.7). On each install the switch waits for the completion check
  (item 6, "Readers during the stamp"): P3.5b for every stamp move, by the
  CONTINUE precondition or, on the chunk family, by reading both shapes;
  P3.5a and P3.5c for gate 3, which includes P3.4 on that install. Test: a store with only half its
  chunks stamped (the stamp move paused part-way) still searches, lists and
  renames correctly after the P3.5 and P3.7 cut while gate 3 does not
  hold, and reads by the id once the check sees gate 3 hold, with no
  restart; from gate 3 a rename is refused until P3.7's one-row rename
  can run (P3.7).
  - P3.5a Registry and routes: `CollectionRegistry` keyed by id with a
    label index; every collection-taking route in the census accepts
    `collection_id` or a label, and a request carrying both for one
    collection is a 400 (`CollectionSelectorFilter.java:50-53`; T2
    `229-research-312`); the rename answer carries the label and, when it
    is above 0, the id (`CatalogHandler.java:2989-3001`; T2
    `229-research-306`). Client caches keyed by id, which they
    trust as stable only once P3.7's name-key drop move has `completed_at`
    set on the install and the rename answer's `ids_stable` says so (item
    6; `http_catalog_client.py:518-524`): until then a rename gives the collection a
    new id, and from gate 3 until P3.7's one-row rename can run (that move
    complete and every P3.5b changeset applied) the engine refuses a
    rename, with a typed error that names the missing condition, the move
    or the restart. The refusal ships in P3.7's engine cut, which is
    P3.5's (P3.7). In this tree `ids_stable` is false
    (`RenameGuard.java:239`), so a rename still gives a new id, and the
    client keys its caches by label (`http_catalog_client.py:524`; T2
    `229-research-307`). The boundary's typed answers to a request by id
    are 400 for an id with a label or a non-integer id; 404
    `unknown_collection_id`; and 409 `collection_superseded` for a retired
    id, naming its successor by `superseded_by_id`
    (`CollectionSelectorFilter.java:50-72`; T2 `229-research-312`). The 409
    `collection_id_mismatch` for an id and a label that no longer agree is
    ready in the engine but no request reaches it while the boundary
    answers 400 for both; a 500 `collection_id_mismatch` marks a registry
    inconsistency. A rename or supersede that cannot lock its registry row
    within 10 s answers 503 `rename_busy`, with nothing changed
    (`CollectionTombstones.java:36,63`; `HttpUtil.java:603-657`; T2
    `229-research-304`, `229-research-305`).
  - P3.5b SQL readers: the plpgsql functions, the views keyed on names
    (`collection_vector_stats`, `live_chunks`) and triggers that fire on a
    name column (`catalog-044`'s `AFTER INSERT OR UPDATE OF
    physical_collection, deleted_at ON nexus.catalog_documents`) read the
    id (T2 `229-research-37`).
  - P3.5c Java readers: the jOOQ queries on the name columns read the id,
    and the two upserts (`PgVectorRepository.java:1324`,
    `TaxonomyCentroidRepository.java:172`) take the id key as their
    conflict target, from gate 3 on the install (item 6). It also owns the
    reader contract for a tombstone that names no successor, whose
    `superseded_by_id` is its own id (R7): a `RETIRED_NO_SUCCESSOR` marker
    on the wire, `collection_superseded` read as "retired, no successor" and
    not retried, the retargeting guard's message, `TargetSelection`, and one
    test per reader class. P3.1 builds only R7's pre-flight counts and the
    doctor's listing queries (item 6).
- P3.6 Label rule, after P2.9 and P2.10: partial UNIQUE on live `(tenant_id, name)`;
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
  rewritten. On each install the one-row rename waits for gate 3 of the
  completion check, which includes P3.4's index builds there, and until it
  passes the rename keeps its fourteen-table rewrite (item 6, "Readers during the stamp"). P3.7 also ships the name-key drop move: the
  completion check registers it once gate 3 holds, its `on_complete` drops
  the seven name foreign keys that refuse a one-row rename and the queue's
  key where that table exists, and the
  one-row rename waits for its `completed_at` as well, and for every
  P3.5b changeset to have applied (item 6, Switch; T2
  `229-research-58`, `229-research-61`, `229-research-64`). From gate 3
  until the one-row rename can run, that is while the move has no
  `completed_at` or a P3.5b changeset has not applied, the engine refuses
  a rename with a typed error that names the missing condition, the move
  or the restart that applies P3.5b's changesets, because
  readers are on the id and the name-path rename would give the
  collection a new id (item 6; T2 `229-research-63`). P3.7 ships in one
  engine cut with P3.5: the rename refusal, the name-key drop move's
  registration at gate 3 and the one-row rename are all in that cut,
  because the drop label joins the completion check's read set only with
  the release that ships the move (item 6; T2 `229-research-69`). The rename, like every other URI-move path, rewrites only
  `nx-note://` URIs that `isNote` classes as notes, never `chroma://`
  residue, the rule P2.10 ships (item 7). Test: a rename leaves
  chunk, manifest and taxonomy rows untouched and the per-collection index
  in place, and a task queued before the rename names the new label; a
  one-row rename of a collection that has chunk rows and
  `canary_query_ring` rows succeeds, and so does one of a collection with
  topic assignments, aspects, topics, `taxonomy_meta` and highlights rows;
  and the one-row rename of a file-routed collection that has `chroma://`
  residue to a prefix-free label leaves the residue on its old segment
  (P2.10's test covers the other four paths); a rename after gate 3 and
  before the name-key drop move completes is refused with the move named;
  and a rename after that move completes and before every P3.5b changeset
  has applied is refused with the restart named.
- P3.8 Minting (item 8), after P2.9, P2.10, P3.6 and P3.9: the engine mints `c<id>` when no label is given;
  every client render-for-new-collection path registers by attributes
  instead; the strict conformant guard in `t3.py`, the rename gate and the
  indexer reroute are removed. Registration requires explicit attributes
  (`http_catalog_client.py:3709-3716`; T2 `229-research-29`), and the
  registering call returns the row, which the writer uses for the first
  write because the row cache omits a collection with no chunk yet (T2
  `229-research-41`). Stop writing `legacy_grandfathered`. (The `for_tuple`
  filter drop moved to the Phase 2 engine cut, 0.1.200, by lead ruling of
  2026-11-20; item 11.) Test: the first `store_put` to a new subject
  stamps the row's model with no name derivation.
- P3.9 Recovery bundle format carries the registry row and `collection_id`;
  import re-maps ids and checks label clashes. The release that ships it
  starts the N-minor-release count (item 10, Open Item O1).
- P3.10 Rewrite `docs/collections.md` Rule 3 and `docs/storage-tiers.md` §
  Collections for labels, minted ids and attribute lookup.
- P3.11 Tenant backup carries the id (item 10), shipped in P3.1's engine
  cut and again with P3.3's key changes: `TenantExport`'s table model
  declares `collection_id` and its twins as `importComputed` on every table
  that has them. The restore fills each id by name from the restored
  registry for a NULL twin or a file with no ids, which P3.1 ships. The
  by-id restore, which maps a file's id through the restore's source-to-target
  map, ships in the P3.5+P3.7 cut, not P4.1 (item 10). That cut also carries
  the catalog epoch. The restore's minted-label relabel (`collections_relabelled`)
  lands in the cut that ships P3.8's minting (bead nexus-p2m1x.31, comment
  09:18; T2 `229-research-112`, `229-research-119`).
  Refusals: `restore_orphan_collection`,
  `restore_orphan_collection_id` and `restore_ambiguous_collection_name`, all
  before the first write. P3.11 is left with what a name cannot settle: a name
  clash with an existing collection in the target (T2 `229-research-190`,
  `229-research-191`, `229-research-193` to `229-research-195`).
  Test: `TenantExportRegistryParityTest` reads a booted install's schema
  (`BootedSchema`) and is green at P3.3 (its expected labels include P3.3b's swap moves, so it cannot be green at P3.1; T2 `229-research-113`); a backup taken after P3.1
  restores with every chunk on its collection's new id; after a rename of one
  row, a backup restores every row on the renamed collection's restored id, and
  a new collection holding the old label receives none of them; a restored
  minted row's label equals `c` plus its new id, and a later mint at the old
  number succeeds; RDR-248's committed fixture, taken before P3.1, still loads
  with every id filled.
- P3.12 Day 2 surfaces, after P3.5a and P3.6: an id column in `nx
  collection list` (it already shows content type, owner, model, dimension
  and state, `commands/collection.py:91-95`), an `nx doctor` census of null
  ids per table and of live labels held by more than one row, and `nx
  collection delete` by label or id (it takes a name today,
  `commands/collection.py:547-550`). T2 `229-research-48`. Test: each
  surface on a fixture with a null id and a minted label.
- P3.R Review gate for Phase 3. The rest of Phase 3 ships as one engine
  cut: the -r13 reland and P3.3 through P3.12 (P3.7's rename refusal, the
  name-key drop move's registration and the one-row rename included) are
  stacked on one branch with one verification pass, not a reland and a cut
  per step (Sam's ruling, T2 `nexus/sam-ruling-rdr229-compress-phase3-2026-11-22`;
  T2 `229-research-120`). The engine cut and the client release are each
  Sam's go; client floor bumps go in the release that first depends on
  the cut. One release may
  hold more than one Phase 3 cut, and an install may skip releases, so the
  cut order does not decide when an install's readers switch; its own
  stamp moves' `completed_at` does (item 6, "Readers during the stamp").

**Phase 4: contract and retire**

- P4.0 Engine release that retires the Java name path: the jOOQ readers
  of the name columns that P3.5 switched at gate 3, `renameCollectionTxn`'s
  fourteen-table rewrite (the one-row rename of P3.7 is the only rename
  left), and the name-based arms of `COLLECTION_SCOPED_TABLES`
  (`CatalogRepository.java:11060`), which keep serving delete and
  re-home by id. Until this release the dual path stays in Java. It keeps
  P3.1 to P3.7's changesets, moves, completion check and index passes, so
  an install that jumps from before P3.1 to this release still reaches
  its floor. The floor: the completion check reads gate 3, the
  `completed_at` of P3.7's name-key drop move and whether every P3.5b
  changeset has applied; until all three hold, the engine
  serves migration-only. It starts, applies its changesets and runs the
  move worker and the index passes, and refuses every collection-scoped
  read and write with a typed error that names the floor. It does not
  exit at boot, as the engine's other refusals do (`ChunksIsolationCheck`
  "refuses to serve"), because the move worker and the reconciler start
  only after `service.start()` (`Main.java:652`, `:678`, `:710-711`), so an
  exiting engine would never run the moves its floor waits for (inferred,
  not read; T2 `229-research-59`). The check is re-read when the move
  worker records a `completed_at` and when a reconciler pass ends, so the
  floor lifts in the running process once it holds (item 6). From a
  half-stamped store the chain needs one restart: the boot after the
  stamp moves complete, which applies P3.5b's CONTINUE changesets, asked
  for by a `boot_work` entry (item 6; T2 `229-research-64`). Test, over
  the whole chain: the P4.0 engine booted on a half-stamped store (the
  stamp move paused part-way) refuses collection-scoped routes naming the
  floor and reports the stamp move in `boot_work`; when the stamp
  completes it asks for a restart; after that one restart, gates 1 to 3,
  the name-key drop move and the floor follow in one process with no
  further restart, and it then serves by the id.
- P4.1 After P3.11 and P4.0; its acceptance includes RDR-248's committed fixture
  and a stated effect on backups taken before P3.1 (item 10). Before any
  drop, every name-holding primary key and UNIQUE has an id-keyed twin
  that exists and is VALID, and P4.1 refuses to drop while one is
  missing (bead nexus-p2m1x.35, comment 2026-11-22 17:42; T2
  `229-research-72`, `229-research-109`). Where those twins are built is not yet decided: in
  P3.4's concurrent pass, or as P4.1's first step, depending on whether
  P3.5c's upsert switch needs them as conflict targets (same comment).
  Drops come next, in this order, and every one is RESTRICT, never
  CASCADE, so a missed dependent object fails the move. First, the form-B
  views `live_chunks` and `collection_vector_stats` are redefined without
  their name arm, with CREATE OR REPLACE, so all five census views (also
  `collection_health_meta`, `collection_doc_counts` and
  `topics_with_counts`) survive (comment 09:24; T2 `229-research-73`).
  Then the three name child keys go, and
  `fk_catalog_chunks_chunk_id`, which is INITIALLY DEFERRED from P3.3c,
  is renamed `fk_catalog_chunks_chunk` and made DEFERRABLE INITIALLY
  IMMEDIATE in the same transaction. The two live sites that defer the
  name key by name, `purge_trash` and `CatalogRepository#deferManifestChunkFk`,
  then reach the id key under the old name, so neither changes (bead notes,
  "linda's rulings on the P4.1 draft", item 3; comments 09:41, 09:51 and
  17:42; T2 `229-research-71`). Then the name primary keys of the chunks
  and centroids swap for their id keys, one primary key per table, in one
  transaction. A table holds one primary key, and `ADD ... USING INDEX` is
  not supported on a partitioned table, so the method (per leaf, or a plain
  `ADD PRIMARY KEY` on the parent) is open for P4.1's design (bead
  nexus-p2m1x.35, comment 23:02, which corrects the 17:42 ruling; T2
  `229-research-105`). The leaf-creating helper `partition_ensure_leaf`
  checks for the parent's id key with `contype = 'u'` and a definition equal
  to `UNIQUE (cols)` (`vectors-073-leaf-id-unique-index.xml:98-100`); once
  the key is a primary key both must also match it (`contype IN ('u', 'p')`
  and `PRIMARY KEY (cols)`), or each new leaf gets a duplicate
  `<leaf>_cid_uq` (bead nexus-p2m1x.35, linda's G7 correction; T2
  `229-research-118`, `229-research-119`). P3.3b's pass reads the id key as
  a UNIQUE the same way (`LeafIndexCatalog.java:106-107`), so it must match
  the promoted key too, or the pass plans new builds after the swap; and the
  three child id foreign keys depend on the id UNIQUE, so the swap moves or
  recreates them in the same transaction (inferred, not read, until P4.1's
  design reads it; bead nexus-p2m1x.35, comment 23:20; T2 `229-research-121`).
  The `vectors-079` trigger
  that sets a child row's name from its chunk goes with the name columns,
  and the two `chunk_orphaned_at` stamp triggers switch to the id twin
  (comments 17:55 and 18:13; T2 `229-research-110`). Then the name columns
  on referencing tables are dropped (the name primary keys of the chunks and
  centroids went in the swap above), and `hybrid_search_<dim>`, which has
  no `_by_id` twin, is dropped (comment 20:10; T2 `229-research-110`). Then,
  in the registry changeset, `superseded_by` (replaced by
  `superseded_by_id`) and the `(tenant_id, name, embedding_model)` UNIQUE;
  and last in that changeset, the primary key swap to `(tenant_id,
  collection_id)` (T2 `229-research-53`). No name key to the registry is
  P4.1's: P3.7's name-key drop move has dropped all eight, the queue's
  where that table exists (item 6, Switch; T2 `229-research-58`,
  `229-research-61`). PostgreSQL refuses to drop "a constraint that
  something else depends on", such as "a unique or primary key constraint"
  a foreign key references, without CASCADE (ddl-alter), so the registry
  changeset carries `onFail="CONTINUE"` preconditions on the
  `completed_at` of the `catalog_collections` stamp move (P3.1), which is
  the move that drives that table and so the label RDR-246's rule 3
  requires for its `DROP COLUMN`, of P3.7's name-key drop move and of
  P4.1's drop move, and a check that no foreign key references
  `catalog_collections` but the id keys (inferred, not read: read from
  `pg_constraint`). It is skipped and retried at each boot until all
  hold. UNIQUE `(tenant_id,
  collection_id, embedding_model)` stays: the RDR-225 composite depends on
  it, and PostgreSQL drops such a constraint only with CASCADE (item 6; T2
  `229-research-44`). Each drop waits for this install's stamp, swap and
  validation moves and for P3.4 (gate 3 of the completion check, item 6,
  "Readers during the stamp"). On the
  chunk family (`nexus.chunks`, the manifest, `topic_assignments`,
  `chunk_orphaned_at`) a drop is a move's `on_complete`: a destructive
  statement on a table a move drives "must carry a precondition on that
  move's `completed_at` with `onFail="CONTINUE"` ...; on a chunk-family
  table it is refused outright, by the CONTINUE-precondition lint, and the
  step belongs in a move's `on_complete`" (RDR-246 lines 986-992). That
  `on_complete` also removes the name arm of P3.5b's both-shapes readers.
  The boot-time completion check registers that move once gate 3 holds,
  so the move never exists before the moves it depends on have
  completed, and its verification only checks its own table (T2
  `229-research-52`). On every other table the dropping changeset carries
  CONTINUE preconditions on that table's own stamp move's `completed_at`,
  the label of a move that drives the table as rule 3 requires (RDR-246
  lines 986-999), and on that drop move's, so it waits for the same gate
  (T2 `229-research-61`). The registry changeset, after it drops
  `superseded_by`, also re-creates the partial UNIQUE on live labels over
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
indexes its verification requires. P3.12 waits for P3.5a and P3.6. P3.3a waits for P3.1. P3.3 blocks P3.5 and P3.7. P3.7 also waits
for P3.4 and P3.5 (item 7), and P3.5 and P3.7 ship in one engine cut,
with the rename refusal and the name-key drop move's registration (P3.7). P3.8 waits for P2.8, P2.9, P2.10 (the
residue rule, item 7), P3.6 (which
enforces the reserved minted form) and P3.9 (the recovery-bundle sites,
item 8); P3.6 waits for P2.9 and P2.10. Phase 4 waits for every Phase 3 bead to be deployed on both
production tenants; P4.4 waits for its release count. These are bead
orders. At run time each install also passes the completion check's gates
(item 6, "Readers during the stamp"): P3.3b's pass waits for the stamp and
P3.3a's moves; P3.3c's swap is registered by that pass only once every leaf
has a valid unique index and the parent's id key is attached; P3.4's builds wait for the swaps and every
validation move; the engine's id readers, P3.7's name-key drop move and
one-row rename and P4.1's
drop move wait also for P3.4 on that install; the one-row rename waits
also for the name-key drop move's `completed_at` and for every P3.5b
changeset to have applied, and from gate 3 until then the engine refuses
a rename. P3.5b's SQL readers wait for
the stamp moves, and apply at the first boot after them. P4.0's engine
serves migration-only until gate 3, the
name-key drop move's `completed_at` and P3.5b's changesets hold on the
install (P4.0). Every other gate takes effect in the running engine when
the check is re-read (item 6). P4.1
waits for P4.0 in bead order. On each install P4.1's registry changeset
waits for the `catalog_collections` stamp move, P3.7's name-key drop move
and P4.1's drop move to have `completed_at` set, and for no foreign key to
reference the registry's name (P4.1).

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Census pins (client, engine, SQL) | In scope (the tests print their tables) | In scope | N/A | In scope (`-m lint` and the Java gate tests) | N/A |
| `nx taxonomy project --prune-below-threshold` | Prints the removed count per collection | Prints "N skipped" when the engine lacks exact mode (cloud, until the conexus edge allowlists the flag), "did not confirm exact matching; using the guarded prune" when the verification prune is not echoed, and "N refused" for a collection whose row cannot be read | N/A | `GET /version` reads `taxonomy_prune_exact_supported` | N/A |
| `lifecycle_state` at registration | `nx collection list` | `nx catalog show` | Existing lifecycle verbs | `nx doctor` row versus name diagnostic | Recovery bundle |
| RDR-229 completion state and the P3.1 down-step | `nx doctor` rows: the three gates, labels incomplete, CHECKs missing, VALIDATE waits, dangling and self-id tombstones | `/v1/status` `rdr229_completion` (no tenant numbers); the caller's own counts on `GET /v1/catalog/rdr229_completion_counts` | The down-step and re-add SQL, conexus-run inside the window (`docs/runbooks/rdr-229-p31-rollback.md`) | The runbook's verify query; `nexus.rdr229_restamp(table)` finishes an orphaned table | PITR outside the window |
| `collection_id` and label | `nx collection list` (id, label, attributes; the id column is P3.12's) | `nx catalog show` | `nx collection delete` (by label or id, P3.12) | `nx doctor` null-id and label-clash census (P3.12) | Recovery bundle (carries the row from P3.9); tenant backup, `nx backup` (exports the id from P3.1's cut; restores by id from P3.5+P3.7; P3.11) |

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
- **Scenario**: an engine with the P3.5 and P3.7 cut boots on a store with only half
  its chunks stamped (the stamp move paused part-way) — **Verify**: search,
  listings and a rename return what they returned before the cut while
  gate 3 does not hold, and the
  engine reads by the id once the check sees gate 3 hold, with no restart.
- **Scenario**: an engine with P4.0 boots on a store with only half its
  chunks stamped — **Verify**: it serves migration-only, refusing
  collection-scoped routes with an error that names the floor, the stamp
  move keeps running in `boot_work`, it asks for one restart once the
  stamp completes, and after that restart it reaches gate 3, the
  name-key drop move and the floor in one process and serves by the id.
- **Scenario**: rename a collection that has chunk and `canary_query_ring`
  rows after P3.7 — **Verify**: one registry
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
key and its children get smaller from P4.1. From P3.3b to P4.1 both the
name primary key and the id key exist, so those indexes are larger for that
window (inferred, not read); the one-off cost is the re-key, the window's
extra index and the index build beside the name index, measured in P3.2.

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
  from P3.7: the Approach now says "up to P3.4", with P3.5 and P3.7 one
  cut and P3.6 on its own, and points at the open downgrade point.
- The RDR-246 prerequisite stated for every install while BYO PostgreSQL
  blocks moves, and no Windows line: Prerequisites and Cross-Cutting (T2
  `229-research-47`).
- Name columns with no disposition, and Day 2 promises with no bead: the
  table in item 6 and P3.12 (T2 `229-research-48`).

The round 2 fix check (`nexus_rdr/229-fix-check-1b112f9c770c`, FAIL) found
these in the round 2 fixes, now addressed in the text:

- A null-parity CHECK on `superseded_by`, which is `''` on a live row, so
  `superseded_by_id` could never be NULL and P4.1's live-label index would
  match nothing: `superseded_by` has its own CHECK `(superseded_by = '') =
  (superseded_by_id IS NULL)` (item 6, item 7, P3.1, P4.1; T2
  `229-research-50`).
- A swap move whose verification needs P3.3b's indexes, registered before
  they exist: P3.3b's pass registers it once every leaf's index is valid
  and the parent's id key is attached (item 6, P3.3b, P3.3c; T2
  `229-research-51`).
- P4.1's drop using a verification as a wait, an undefined label set, and
  readers and the one-row rename not waiting for P3.4: the completion
  check's read set and three gates, with P4.1's drop move registered at
  gate 3 (item 6, P3.4, P3.5, P3.7, P4.1; T2 `229-research-52`).
- Name-keyed upsert targets and name-only child cascades after a swap that
  dropped the name key, and client caches trusting ids a name-path rename
  replaces: P3.3b's pass adds the id key beside the name keys and P3.3c
  adds the child id keys, all until P4.1; the writers move at gate 3, the
  rename sets the new id, and caches trust ids
  from gate 3 (item 6, P3.3c, P3.5, P4.1; T2 `229-research-53`).
- P3.2 timing only the stamp, the swap and the index build beside the name index: it times every
  move and pass (P3.2, Critical Assumption 5; T2 `229-research-54`).

Gate round 3 (2026-11-19, PASSED) recorded twelve residuals in
`nexus_rdr/229-gate-latest`. Sam chose on 2026-11-19 to fix the five
classed BLOCKS-PLANNING in the text and to carry the seven
DISCOVER-AT-IMPLEMENTATION ones as beads at accept (ask
`baton/rdr229-accept-route`, option 1). The five, now resolved in the text:

- The `chroma://` residue rule covered the rename only: the rule is in
  `NoteUri.moveTo`, for all four callers (item 7, item 12, P2.10; T2
  `229-research-55`).
- The rule was stated in force from P3.6 but owned by P3.7: P2.10 owns
  it, and P3.6 and P3.8 wait for it (item 7, P2.10, P3.6, P3.8, Phase
  dependencies; T2 `229-research-56`).
- Prefix-free labels are possible today through `--allow-legacy`: the
  "prefixed before P3.6" premise is withdrawn, Critical Assumption 3 is
  reopened for the URI-string classifiers, P1.2 counts the rows on every
  install, and P2.10 repairs residue already moved (item 12, Critical
  Assumption 3, P1.2, P2.10; T2 `229-research-57`).
- The name composite foreign keys refused P3.7's one-row rename: seven of
  the eight name keys refuse it, and P3.7's name-key drop move drops them
  once gate 3 holds, before the rename (item 6 Switch and Contract, P3.3a,
  P3.3c, P3.7, P4.1; T2 `229-research-58`).
- S-1, Phase 4 retired the engine's name path with no per-install guard:
  P4.0, behind a boot floor (Contract, P4.0, Phase dependencies,
  Cross-Cutting; T2 `229-research-59`).

The accept-time fix check (`nexus_rdr/229-fix-check-f58bc168420c`, FAIL)
found these in those fixes, now resolved in the text:

- P4.1's registry primary key swap and UNIQUE drop were not ordered after
  the name keys that depend on them: the name-key drop move also drops
  the queue's key, and the registry changeset drops before it swaps and
  waits on the `catalog_collections` stamp move, the name-key drop move,
  its own drop move and a check that no key references the name (item 6,
  Contract, P4.1, Phase dependencies; T2 `229-research-61`).
- `catalog_reconcile_commit` is a fifth URI-move path: P2.10 redefines it
  (item 7, item 12, P2.10; T2 `229-research-62`).
- Readers on the id from gate 3 while a rename still minted a new id: the
  engine refuses a rename until the name-key drop move completes and
  every P3.5b changeset has applied (item 6, P3.5a, P3.7, Versioning; T2
  `229-research-63`, `229-research-69`).
- P4.0's floor waited on restarts with no trigger: the check is re-read
  in process, and the one restart the chain needs is asked for (item 6,
  P3.7, P4.0, Test Plan; T2 `229-research-64`).
- `backfill-source-uri` refused moved and repaired residue: P2.10 relaxes
  its check to the residue's own segment (P2.10; T2 `229-research-65`).
- P2.10's repair moves had no sizing owner: P2.10 sizes them (T2
  `229-research-66`).
- The repair's order across its three tables, its meeting with P3.1's
  CHECK, `NoteUriMoveTest`, and seven wording rows (P2.10, P3.1, item 8,
  item 12, P1.2, CA5, Prerequisites, Approach; T2 `229-research-67`).

The accept fix check (`nexus_rdr/229-fix-check-3c7aa425d443`, FAIL) found
this in those fixes, recorded as the last residual of
`nexus_rdr/229-gate-latest`. Sam chose on 2026-11-19 to fix it in the text
(ask `baton/rdr229-refusal-owner`, option 1). Now resolved in the text:

- The gate-3 rename refusal had no owner before P3.7: it ended on a move
  only P3.7 ships and registers, so a release with P3.5 and not P3.7
  refused every rename or reopened the new-id hazard, and the Approach
  and Incremental adoption said each step up to P3.6 could stop alone.
  P3.5 and P3.7 now ship in one engine cut with the refusal and the
  name-key drop move's registration; the refusal holds from gate 3 until
  that move has `completed_at` and every P3.5b changeset has applied, and
  names the missing condition, which also covers the check's DISCOVER row
  on the state between those two (item 6, Approach, P3.5, P3.5a, P3.7,
  P3.R, Phase dependencies, Versioning, Incremental adoption;
  `9ea4521a4`; T2 `229-research-69`).

To beads at accept (DISCOVER-AT-IMPLEMENTATION; critique
`nexus_rdr/229-gate-critique-2026-11-19-r3`), not resolved here:

- S-2, P3.4's planner transition and gate 3's "P3.4 complete" test.
- S-3, P3.3b and P3.3c against the two-level partitioning, installs with
  PCI off, and the `changeset_id` of a move registered at run time.
- S-4, `aspect_extraction_queue` absent on most installs.
- S-5, the BYO paragraph's "P3.3b's pass ... still builds", which
  contradicted gate 1. The paragraph now says the pass does not start on a
  blocked install (2026-11-20); the bead stays open until P3.3b is built
  to gate 1 and tested there.
- S-6, the downgrade boundary, which P3.1's CHECK places at P3.1, not
  after P3.3 as "Still open" below says.
- S-7, the client's trust in an id, which now starts at the name-key
  drop move's `completed_at`, with no engine signal.
- S-8, a tombstone whose successor was deleted, which the stamp cannot
  give an id.

Still open, for a design call (Sam settled the `/` question on
2026-11-21: the rule refuses it, item 7): the safe engine downgrade
point after P3.3, which matters most from P3.7, when the name columns go
dead (Approach, item 7; S-6 above); a repair for residue whose segment no
longer names its collection and for residue `note-uri-001` rewrote to
`nx-note://` (P1.2 counts both; item 12); and a note that `moveTo` moves
onto a label that starts with `rdr__`, `docs__` or `code__`, which
`isNote` then classes as no note, because P2.10's rule tests only the old
segment (`NoteUri.java:110`, `:191-200`; T2 `229-research-60`).

### Assumption Verification

Five Critical Assumptions are unverified, Critical Assumption 3 in part,
and each has a bead that verifies it before the work that depends on it
(P1.2 for 1 and 6, P1.2 and P2.10 for 3, P2.3 for 2, P3.2 for 5).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `GET /v1/catalog/collections/list` with `content_type` and `lifecycle_state` filters | nexus engine | Source Search (RDR-204 Phase 2) |
| `GET /v1/catalog/collections/for_tuple` | nexus engine | Source Search (`CatalogRepository.java:10793`, `http_catalog_client.py:3911`; T2 `229-research-29`) |
| `catalog_document_chunks.embedding_model` | nexus schema | Source Search (`vectors-030` lines 786-791) |
| `ALTER TABLE ... ADD CONSTRAINT ... NOT VALID` then `VALIDATE CONSTRAINT` on a partitioned referencing table | PostgreSQL 17 | Source Search (fk-002 and vectors-030 already use it on `nexus.chunks`) |

### Scope Verification

The Minimum Viable Validation runs in the local-service gate during Phase 2
and again after Phase 3, and is not deferred. Items that gate round 1 found
with no plan step now have one: routing by content type, 23 sites plus the
`taxonomy__` half of `_BYPASS_SCHEMA_PREFIXES` (P2.9), the recovery-bundle
sites (migration sources, P3.9 before P3.8), the tenant backup (P3.11, in
P3.1's and P3.3's cuts, its by-id restore in P3.5+P3.7), every writer of the dual write (P3.1), queued
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
one is `TaxonomyCentroidRepository.java:156`). Items the round 2 fix
check found now have one too: `superseded_by_id`'s own CHECK (P3.1), the
swap's registration by the P3.3b pass (P3.3b), the completion check's read
set and gates (P3.1, P3.4, P3.5, P3.7, P4.1), the name-keyed upserts and
child cascades until P4.1 (P3.3c, P3.5c), the old composites' drop
(P3.7's name-key drop move), and the timing of every move and pass (P3.2).
Items the round 3 residuals found now have one: the residue rule on every
`moveTo` caller (P2.10), its order before P3.6 and P3.8 (P2.10), the count
of residue on a prefix-free segment on every install (P1.2) and its repair
(P2.10's moves), the drop of the name keys that refuse the one-row rename
(P3.7's name-key drop move), and the retirement of the Java name path
behind a boot floor (P4.0). Items the accept-time fix check found now
have one: the order of P4.1's registry drops (P4.1), the reconcile path
(P2.10), the rename refused in the gate 3 window (P3.7), the in-process
re-read of the check and P4.0's one restart (item 6, P4.0), the
backfill verb (P2.10), the repair moves' sizing and order (P2.10), and
the repair against P3.1's CHECK (P3.1). The seven DISCOVER residuals,
S-2 to S-8, go to beads at accept (Contradiction Check).
Still without one: P3.5b's plan measurement of each both-shapes reader
has no bead of its own beyond P3.5b's acceptance; the history tables' nullable `collection_id` (item 6)
names no consumer; P1.2 measures the two production tenants, not local
stores, apart from its residue count, which runs on every install; and no repair covers
the two residue shapes item 12 leaves unrepaired (P1.2 counts them).

### Cross-Cutting Concerns

- **Versioning**: P2.4 (the prune `match` field; engine first, and an older engine reads as
  unsupported, so the client keeps its guard), P2.5, P2.6, P2.9, P2.10 (in 0.1.200 with D2's `vectors-064` and
  `vectors-065`, `catalog-051`, `vectors-067`, the re-embed 409 refusals and the RDR-254 aspect fix), the Phase 3
  engine beads and P4.0 ride engine cuts,
  P3.5 and P3.7 one cut together with the rename refusal and the name-key
  drop move's registration (P3.7);
  the client floor bumps in the release that first depends on each, engine
  deployed first. Routes accept both a label and an id from P3.5, so an
  older client keeps working until the floor bump. An install that skips
  releases is guarded by its own moves, not by the cut order: its readers
  switch once the completion check sees its gates pass there
  (item 6, "Readers during the stamp"). A client trusts a collection's
  id as stable only from P3.7's name-key drop move's `completed_at`
  there; from gate 3 until that point and every P3.5b changeset has
  applied, the engine refuses a rename (item 6). The same holds at Phase 4: an
  install that skips from before P3.1 to a release with P4.0 meets P4.0's
  boot floor, which serves it migration-only until gate 3, P3.7's
  name-key drop move and P3.5b's changesets hold there (P4.0; T2
  `229-research-59`, `229-research-64`).
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
  move"). P4.0's floor never holds either, so a P4.0 engine would serve
  such an install migration-only; it stays on a release before P4.0 until
  its moves run (P4.0). P3.3b's pass does not start either: it is an
  engine pass, not a move, but it starts only at gate 1, once every stamp
  move and every P3.3a move is complete (item 6), and on such an install
  none is. Inferred, not read: P3.6 and P3.8 touch only the registry and
  still apply. T2 `229-research-47`.
- **Windows**: RDR-246 verified pg_background on the Windows x64 bundle:
  "built for Linux x86_64 and arm64 (manylinux_2_28), macOS arm64 and
  Windows x64. On each, a relocated bundle ran `CREATE EXTENSION` and a
  background worker" (RDR-246 lines 225-227). Inferred, not read: so a
  bundled Windows install runs the moves; RDR-246 verified the extension
  and a background worker there, not a data move. The sites where a label meets a file path encode it as a
  safe path component, and the rule refuses `/` (item 7). P3.6's tests exercise on Windows one
  label holding a space, `%`, `:` and a non-ASCII character.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phases 1 and 2 change no schema key and no name.
  Each Phase 3 step up to P3.4 leaves the name path working and can ship
  and stop alone, and so can P3.6. P3.5 and P3.7 are one engine cut:
  P3.5a's rename refusal ends only when P3.7's name-key drop move
  completes and every P3.5b changeset has applied, and only P3.7 ships
  and registers that move (P3.7). From P3.7 a
  rename leaves the referencing tables' name columns on the old label, so
  the name path is dead from there until P4.1 drops it (item 7), and the
  safe engine downgrade point is open (Contradiction Check). On each
  install that happens only after its stamp moves have completed, because
  the one-row rename waits for the same check (item 6, "Readers during the
  stamp"). The Java name path stays in the engine until P4.0, whose boot
  floor serves an install migration-only until that install's gate 3,
  name-key drop move and P3.5b changesets hold, so an id-only engine never serves a
  half-stamped store (P4.0).
- **Secret/credential lifecycle**: N/A.
- **Memory management**: N/A.

### Proportionality

The schema work is now large: a new key on the registry, eight foreign keys
and two partitioned tables given an id key beside their name keys until
P4.1, ten foreign-key validation moves, every per-collection index built beside its name index,
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
- T2 `nexus_rdr/229-research-1` to `-68`, and
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
  acceptance. Observations 3 and 4 corrected, and Observation 6's rule
  written into P4.1. The three recorded
  residuals are listed for Sam, not resolved. Contradiction Check and
  Scope Verification regenerated. T2 `229-research-43` to `-49` added.
- 2026-11-19: Fix check 1 of round 2 (`nexus_rdr/229-fix-check-1b112f9c770c`,
  FAIL) re-fix. `superseded_by` gets its own CHECK, so `superseded_by_id`
  is NULL exactly on live rows. P3.3b's pass registers the swap once every
  leaf's index is valid. The completion check has a read set (a label with
  no row is incomplete; a failed move on a dropped table is complete) and
  three gates; readers, the one-row rename and P4.1's drop move wait for
  P3.4. P3.3c adds the id key beside the name keys until P4.1, and the
  upserts move to it at gate 3. P3.2 times every move and pass. Wording
  rows corrected. Contradiction Check and Scope Verification regenerated,
  with the three residuals still recorded for Sam. T2 `229-research-50` to
  `-54` added.
- 2026-11-19: Gate round 3 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); blob `2e06cb126e67`; critique `nexus_rdr/229-gate-critique-2026-11-19-r3`.
- 2026-11-19: Round 3 residuals dispositioned (Sam, ask `baton/rdr229-accept-route`, option 1). Fixed: residual 1 `f0d64bc6a`, residual 2 `a59ef5325`, residual 3 `4ce3d0def` and `e9684ddca`, residual 4 `1d2e07f8d`, S-1 `bed7b07a7`, sweep `0947955d2`; S-2 to S-8 to beads at accept. Contradiction Check and Scope Verification regenerated. T2 `229-research-55` to `-60` added.
- 2026-11-19: Accept-time fix check (`nexus_rdr/229-fix-check-f58bc168420c`, FAIL) re-fix. P4.1 ordering `107b5dd67`; reconcile path `e2ad1c74b`; gate 3 window `3933d47df`; P4.0 liveness `7ee538cbf`; backfill verb `7341f2511`; P2.10 sizing `25bb765b8`; named single-critic rows `e3a1aebf1`. Contradiction Check and Scope Verification regenerated. T2 `229-research-61` to `-68` added.
- 2026-11-19: Last residual of `nexus_rdr/229-gate-latest` (the gate-3 rename refusal had no owner before P3.7; accept fix check `3c7aa425d443`) fixed in the text (Sam, ask `baton/rdr229-refusal-owner`, option 1): P3.5 and P3.7 ship in one engine cut with the refusal and the name-key drop move's registration, and the refusal holds until every P3.5b changeset has applied, `9ea4521a4`. Contradiction Check regenerated. T2 `229-research-69` added.
- 2026-11-19: Accepted (Sam, "accept RDR-229", T2 `nexus/sam-ruling-rdr229-accept-2026-11-19`); epic nexus-p2m1x. Residual dispositions: the five BLOCKS-PLANNING residuals of `229-gate-latest` fixed in 00f6de8fe..55f50376b; the rename-refusal-owner residual fixed in 9ea4521a4 (confirming check `229-fix-check-50164fafab10`); S-2 to S-8 to beads nexus-p2m1x.40 to .46; the accept-check DISCOVER rows to nexus-p2m1x.47 (P3.5 and P3.7 close together) and .48 (fresh-install rename refusal until a second boot).
- 2026-11-20: residual S-5 (nexus-p2m1x.43): the Cross-Cutting BYO paragraph said P3.3b's pass still builds on a blocked install, contradicting gate 1 (item 6), P3.3b and the phase dependencies; it now says the pass does not start, matching the RDR's own gate. The Contradiction Check's S-5 entry is updated; the bead closes when P3.3b is built and tested to gate 1.
- 2026-11-21: P2.9's engine half landed on develop (c7ace16dd onward): `isFileRoutedCollection` is deleted, and the stale-note repair reads `content_type` in its transaction, admits only `knowledge` rows, and share-locks the collection row under the rewrite (item 12, engine paragraph). This supersedes the 2026-11-19 line that kept `isFileRoutedCollection`. The client half's text follows its reland (grant 3, nexus-p2m1x.14).
- 2026-11-21: Owner is the source (Sam, T2 `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`) written into item 5 and the `229-research-9` finding, with its two accepted residuals (T2 `nexus/sam-ruling-owner-is-source-old-bundle-residual-2026-11-21`, `nexus/sam-ruling-batch-2026-11-21-late`), matching the reviewed code on `feature/rdr229-owner-is-source` (grant 3).
- 2026-11-21: the P2.7 step names the `resolve_corpus` stage-3 fallback that item 1 already records (one `label-pattern` site, `LABEL_TYPED`), so the step and item 1 agree (grant 3, nexus-p2m1x.12; P2.R cross-walk F1, nexus-p2m1x.16).
- 2026-11-21: item 1's holdover total and item 12's `taxonomy__` sites match the Phase 2 integration tree: holdover empty (P2.2 23 -> 4, P2.4 4 -> 0), the three taxonomy tests read through `legacy_names.is_taxonomy_collection_name`, client census 218, all measured at 7817cd3251 (grant 3; P2.R author read, nexus-p2m1x.16).
- 2026-11-21: recorded late, P2.4's two amendments of 2026-11-20 and 2026-11-21. P2.4 owns a small engine half: the prune route's `match` field (`exact` or `prefix`), echoed in the answer, with `/version`'s `taxonomy_prune_exact_supported` flag, which the client checks before any exact prune; the P2.R step, Versioning, item 1 and Day 2 follow (b7e8f448e3, grant 3, nexus-p2m1x.9); and the verification sentinel is a name no collection has, and the guarantee is the name, not the threshold (20baca2d8f, nexus-p2m1x.9).
- 2026-11-21: the 0.1.200 contents (P2.R step, Versioning), item 5's stem rule and `catalog-051-2` tuple guard with its lock key, the Prerequisites' first registered moves and their deploy-go gate (T2 `nexus/sam-ruling-0200-move-gate-2026-11-21`), and the MVV's numeric Phase 2 pins match the integration tree eb1e4cd42b (grant 3; P2.R critique T2 `nexus/critique-eb1e4cd42b-nexus-p2m1x.16-2026-11-21-r1`, nexus-p2m1x.16).
- 2026-11-21: item 5's tuple guard and re-own move match a92f88c6a0: one held predicate (unsuperseded and not in quarantine, P3.8's index predicate), the guard on the stored version and the move on the effective one, the move taking the tuple lock for its chunk's tuples in key order, and which paths re-run a 40P01; item 12's client census is 217 with `owner_stem` in `legacy_names` (grant 3; P2.R critique S2, S3, S5, nexus-p2m1x.16).
- 2026-11-21: P3.8 amendment (grant 3, matching reviewed code, nexus-p2m1x.28): item 8 states the live-tuple UNIQUE and the minted-label collision rule; the two open questions on tuple uniqueness and minted-form collisions (Contradiction Check, Scope Verification) are closed by Sam's ruling T2 `nexus/sam-ruling-rdr229-p38-design-gate-v2-2026-11-21`.
- 2026-11-21: item 7's label rule matches P3.6 as reviewed: no `/` (Sam's ruling), Cc plus U+2028 and U+2029 refused, Cf allowed, no normalization, the engine as the authority with existing rows keeping their labels, and the label-to-file-path sites encoded rather than audited-open; the client census goes 213 -> 211; the `/` question leaves the Contradiction Check (grant 3; P3.6 review r1 and r2, nexus-p2m1x.26).
- 2026-11-21: item 7's label rule also refuses `,` and whitespace at either end (Sam's ruling on ask `rdr229-label-comma-space`; grant 3, matching reviewed code 26359a5b5b and 71883ae2a5, nexus-p2m1x.26): MCP search and plan confinement split a corpus list at `,` and strip each part; whitespace is category Zs once controls are refused; the import report's `rule` gains `no_comma` and `no_edge_whitespace`.
- 2026-11-22: P3.1's landing amendments, matching the reviewed code (grant
  3; T2 `nexus_rdr/229-p31-confirmation-critique`, nexus-p2m1x.17). Item 6:
  the identity is BY DEFAULT; the CHECK shape, `''` as unset, the pending
  predicate and the id column per table; the `superseded_by` rules and the
  split of `vectors-068-2` from `vectors-068-19`; the completion check's 60 s
  poll, its own `rdr229_completion` field in place of a `boot_work` entry,
  the queue exemption, orphans and `nexus.rdr229_restamp`, gate 3's source
  and its refusal of an empty plan, and R7's counts. Item 10: the restore
  refills every id by name. Item 12: the census goes 217 to 215 as mint
  seams. P2.10, P3.1, P3.5c and P3.11: the residue supersede (`catalog-052`),
  the client seam, the owner-read policies, the R7 reader contract and the
  restore split. Risks, Failure Modes and Day 2: the downgrade boundary at
  P3.1's cut and the single-role install. No decision or criterion changed.
- 2026-11-22: Phase 3 wording, matching reviewed stacked code and linda's rulings (grant 3; nexus-p2m1x.20, .21, .31, .35): P3.3b's pass adds the id key and P3.3c's `on_complete` checks it and adds the child keys, the manifest child key is DEFERRABLE INITIALLY DEFERRED, the chunk_orphaned_at index is vectors-075's self-healing boot changeset, and TwinKeyUpsert is the retry rule for name/id unique pairs until P4.1; item 10 and P3.11: the by-id restore ships in the P3.5+P3.7 cut, not P4.1, with the minted-label relabel, the stripped unmapped quarantine origin id, catalog_epoch's derivation and the booted-schema parity; P4.1's design notes (G1, G7, views first, RESTRICT drops, the deferral and trigger notes). P3.R: the rest of Phase 3 is one engine cut (Sam's ruling, T2 `nexus/sam-ruling-rdr229-compress-phase3-2026-11-22`). P3.4, P3.5 and P3.7 follow when their code is reviewed and stacked. Two decisions moved on linda's rulings: the by-id restore from P4.1 to the P3.5+P3.7 cut, and the catalog epoch placed in the P3.5+P3.7 cut (it replaces the 11-20 cut-C1 timing; the RDR had placed it nowhere) (bead nexus-p2m1x.31, comments 08:24 and 08:49; bead nexus-p2m1x.35, comment 17:42; T2 `229-research-116`). No test criterion changed in this entry.
- 2026-11-22: test lists (Sam's ruling, T2 `nexus/sam-ruling-rdr229-test-lists-and-stack-wal-2026-11-23`, answering linda's ask rdr229-rdr-test-lists): the P3.3b, P3.3c and P3.11 Test sentences gain the tests the reviewed code carries (an unattached parent key, a new leaf, `NX_SEARCH_PCI=0`, moves blocked; a swap refused without the id key, the EXPLAIN of a chunk DELETE; the booted-schema parity, the rename and minted-label restores), and the parity test is green at P3.3, not P3.1 (T2 `229-research-113`; tests `LeafIndexPassIntegrationTest`, `Rdr229SwapMovesIntegrationTest`, `Rdr229SwapMovesAfterTheSwapIntegrationTest`, `TenantExportRegistryParityTest`, `Rdr229RestoreByIdTest`, `Rdr229RestoreMintedRelabelTest`).
- 2026-11-23: P3.4 folded from the stacked code (grant 3, nexus-p2m1x.22): the gate 3 latch row (`vectors-080-1`) and its boot read, the PCI clause read from the database with the PCI-off case and the B-row coverage, the runtime id arbiters after gate 2 and not in the boot walk, the name-index retire behind the latch, `IdReaders.PRESENT` and the singleton lock, the held entries (`id-unique-index-held`), gate 3 implying gate 2, the PCI clause's B-row cap per leaf, the latch's hold for P3.5's readers and P3.7's one-row rename and its refusal, the per-leaf budget with its bound, the canary twin's wait for `vectors-078-8`, and linda's rulings on nexus-p2m1x.22. Builds run beside the name index, not as rebuilds. P3.4's Test sentence gains "before the latch" (Sam, ask rdr229-p34-test-latch, 2026-11-23).
- 2026-11-23: P3.5a folded from the stacked code (grant 3, nexus-p2m1x.23): the refusal's wire values (`rename_refused` with `name_key_drop_move`, `restart`, or the guard's own `latch_row_missing`), the latch row deciding the path, the registry keyed by id with no gate, the route boundary's id-to-label turn and the delete's resolved id, the boundary's typed 400, 404 and 409 answers, the unreachable 409 `collection_id_mismatch`, the 503 `rename_busy`, the four readers that hold a `Gate3`, the client's id trust waiting on both the drop move and `ids_stable`, the incremental-adoption refusal also waiting on P3.5b, `ids_stable` false in this tree, and the citations moved by the stack. Not changed: the P3.5 Test sentence's "the check sees gate 3 hold", which is shared with P3.5b and P3.5c (T2 `229-research-300` to `-314`; fix checks `229-fix-check-27fcc5dbb5`, `229-fix-check-f0562ed03e`).
