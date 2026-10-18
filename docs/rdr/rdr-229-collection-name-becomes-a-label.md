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
related_rdrs: [RDR-101, RDR-103, RDR-155, RDR-156, RDR-176, RDR-204, RDR-210, RDR-225]
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
on 2026-09-09. This RDR is therefore about what RDR-204 left behind, and it
starts from that ruling rather than around it (Open Question 1).

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

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-101 (event-sourced catalog) | Origin | Defined the four-segment name and created `catalog_collections`. Its rationale for the name was ChromaDB's metadata poverty and name regex; both expired with RDR-155. |
| RDR-103 (catalog as collection-name authority) | Origin | Made the catalog the only place that renders a name (`CollectionName.render`). Its surviving rationale, "switching embedding models necessarily creates a new collection", is kept: a model switch still mints a sibling collection. Only parse-as-read is retired here, as RDR-204 already said. |
| RDR-155 / RDR-156 (pgvector consolidation; capability leverage) | Precedent | Moved T3 onto Postgres and kept collection names verbatim. RDR-156 turned the "names verbatim" prose contract into `ON UPDATE CASCADE`, which is why a label can change without a manual cascade. |
| RDR-176 (survivable managed migration) | Precedent, closed | Shipped nexus-zvcou (one config-first service URL and token chain for every storage command) as nexus-t9rmg.8. The bead asked to fold zvcou in or hard-depend on it; it is satisfied and carries no work here. |
| RDR-204 (embedding profile; `catalog_collections` as the authority) | Origin, closed | Shipped most of nexus-j4gl6 steps 1 and 2. Rejected step 3 (opaque names) on 2026-09-09 with a reopen trigger. This RDR takes over its two census gates, repairs their blind spots, and finishes the reader move. It does not restate RDR-204's schema; it builds on it. |
| RDR-210 (one local engine serves both embedders) | Adjacent draft, under gate in another session | Resolves model siblings and its re-embed target by name (design item 9, Gap 7). Scope boundary and sequencing below. Read only. |
| RDR-225 (one vector table per embedding model) | Adjacent, accepted and deployed | Keys every chunk on `(tenant_id, collection, embedding_model)` through a composite foreign key to `catalog_collections`, written from the row, never the name. It makes the name a pure key in the vector schema. Sequencing below. |

Searched `docs/rdr/README.md` and the RDR tree for "collection name",
"naming", "conformant", "opaque", "quarantine", "segment" and "sibling". No
other draft overlaps.

**Scope boundary with RDR-210.** RDR-210 owns how a dual-mode engine finds
and fills model siblings. RDR-229 owns where any code learns a collection's
attributes. The interface between them is one rule: a sibling is found by
the row tuple `(content_type, owner_id)` through the catalog list route's
filters, never by rendering or matching a name. If RDR-210 lands first and
resolves siblings by name, RDR-229 Phase 2 repoints that resolution like any
other reader. Order is Open Question 6.

**Sequencing with RDR-225.** No blocking dependency. RDR-225 is deployed
(nexus-3wh8d, 29 of 31 beads closed; open are the 14-day drop of
`chunks_retired_225` and its phase-review gate). Two ordering rules apply:
every changeset this RDR adds is ordered after `vectors-030`, and a function
`vectors-030` defines (for example `quarantine_restore_chunks`) is redefined
with its four-column write sites intact and stays under
`PlpgsqlCheckGateTest`. The manifest's new `embedding_model` column
(`vectors-030-model-tenant-partition-functions.xml` lines 786-791) is what lets Phase 2 rewrite `chash_conformance_report` without
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
  legacy_grandfathered, ...)`, primary key `(tenant_id, name)`; since RDR-225
  also UNIQUE `(tenant_id, name, embedding_model)` as the target of the chunk
  foreign key.

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
- **✅ Verified** (source search) — Of the three load-bearing sites the bead
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
     `229-research-4` for the full list by helper. The load-bearing ones are
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
  named from a collection name).

## Proposed Solution

### Approach

Fix what reads the name before deciding what a new name looks like. Phases 1
and 2 close Gaps 1, 2, 4 and 5 and do not depend on Open Question 1: they
are reads of facts the row already owns, and the reopen condition Sam set for
RDR-204 Phase 4 is about mint-time names. Phases 3 and 4 change what a name
must be, and they wait for Sam's answers to Open Questions 1 and 2.

Nothing is renamed by any phase. Existing names stay exactly as they are.

### Technical Design

**1. One resolver, one parser, three classes of caller.** The client gets a
single read path for a collection's attributes and a single place where a
name may still be parsed.

```text
// Illustrative; verify against corpus.py and mcp_infra.py during implementation
resolve_collection(name: str) -> CollectionAttributes
    # reads the registry row (cached); raises CollectionNotRegisteredError
CollectionAttributes: content_type, owner_id, embedding_model, model_version,
    dimension, lifecycle_state
legacy_names.candidate_attributes(name: str) -> CandidateAttributes | None
    # the only name parser left; callers must be classified
```

Every caller of `legacy_names` is one of three classes, recorded in the
census beside the site: **mint** (the name is the user's own input or one
`nx` is about to register, so no row exists yet), **migration source** (the
name comes from another install or an older era: recovery bundle import,
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
`quarantine-` prefix stays on the names that already have it and on new
quarantine siblings as a human-readable label. The ETL import path, which
receives rows from another install, takes `lifecycle_state` from the
imported row when present and falls back to the prefix as a migration
source.

**3. SQL reads columns.** `chash_conformance_report(dim)` filters manifest
rows by the manifest's `embedding_model` column (the `vectors-030`
changeset adds it, lines 786-791) joined to `nexus.embedding_models.dimension`, instead of nine
`split_part` IN-lists that also hard-code the model list. The two
quarantine functions test `lifecycle_state` instead of the prefix.

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

**5. A name becomes a label (Phases 3 and 4, pending Sam).** Writes stop
requiring the conformant shape: the strict guard in `t3.py`, the rename
gate, the indexer's reroute, and the ChromaDB limits in
`validate_collection_name` are replaced by one label rule. The label must be
unique per tenant (it is the primary key, and RDR-225's foreign key target),
non-empty, valid UTF-8 without control characters, and within a byte limit
Sam picks. Registration requires explicit attributes and never derives them
from the name. What label `nx` mints for a new collection is Open Question 2.
With the shape no longer required, the parsers outside `legacy_names` are
deleted and both census gates fall to their floors.

**6. The non-stranding rule.** Phase 2 may repoint a reader only after every
migration source it serves has a test that runs a row-less legacy name
through it and passes. Those tests are written first (Phase 2 Step 1) and
stay in the suite through Phase 4. A migration source keeps its
`legacy_names` fallback until Open Question 4 retires it.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `resolve_collection` | `corpus.collection_content_type` / `collection_owner` / `collection_model`, `mcp_infra.get_collection_row` | Extend: one function returning the whole row, with the three helpers kept as thin wrappers until their callers move. |
| `legacy_names` module | `corpus.split_candidate_collection_name`, `embedding_model_for_collection_name`, `model_version_for_collection_name`, `parse_conformant_collection_name` | Replace: move them into one module whose callers are classified; delete the rest. |
| Client census | `tests/test_collection_name_parse_census.py`, `tests/test_parse_conformant_collection_name_caller_census.py` | Extend: new shapes, derived helper set, class labels per site. |
| Engine census | `CollectionParseGateTest` | Extend with the `quarantine-` literal; add a SQL live-body gate beside `PlpgsqlCheckGateTest`. |
| Lifecycle at registration | `CatalogRepository.upsertCollection`, `lifecycleStateForImport` | Extend: explicit `lifecycle_state` input; prefix only as the import fallback. |
| Rename and delete cascades | `CatalogRepository.renameCollectionTxn`, `COLLECTION_SCOPED_TABLES`, RDR-156 `ON UPDATE CASCADE` | Reuse unchanged. |

### Decision Rationale

Splitting the work at the reader/writer line keeps the part Sam has not
ruled on (what a name looks like) separate from the part that is a defect
today (chunk metadata and lifecycle read from a string the row overrides).
The census-first order follows RDR-204's own method, which worked; the
widened gates exist because RDR-204's gates reported done while 106 of the
census's sites (the second ring, the raw shapes, the engine prefix reads and
the SQL) were outside them.

## Alternatives Considered

### Alternative 1: Close the bead as done by RDR-204

**Description**: Treat RDR-204 as the bead's resolution and record the
second ring as follow-up beads.

**Pros**:

- No new record; RDR-204's Phase 4 ruling stands untouched.

**Cons**:

- Leaves the write-path model read (Gap 1) and the lifecycle prefix (Gap 2)
  as known defects behind green gates (Gap 4).

**Reason for rejection**: not rejected; it is Open Question 1's "no" branch
for Phases 3 and 4. Phases 1 and 2 are still needed in that branch.

### Alternative 2: A surrogate collection id as the primary key

**Description**: Add `collection_id` (a generated key) and make every table
that names a collection reference it, so the name is free to change or
repeat.

**Pros**:

- The name becomes a pure display field.

**Cons**:

- Touches every entry in `COLLECTION_SCOPED_TABLES`, RDR-225's partition
  keys and composite foreign keys, the manifest, and every route that takes
  a collection name, for a property `ON UPDATE CASCADE` already gives.

**Reason for rejection**: proposed as rejected, but it is a schema decision
and is listed as Open Question 3.

### Alternative 3: Rename every collection to an opaque id now

Rejected for the reason RDR-204 gave: the foreign key already makes the name
a key, and a rename cascade across every collection and manifest moves no
information.

### Briefly Rejected

- **Keep parsing, but check the parse against the row at every read**:
  doubles the work and keeps two sources of truth.
- **Drop the `quarantine-` prefix from new quarantine names**: the prefix is
  harmless once nothing reads it, and operators recognise it.

## Trade-offs

### Consequences

- Chunk metadata, scoring and taxonomy use the model and content type the
  engine actually stored, which can differ from the name only where the row
  was corrected, which is where it matters.
- Every reader of a registered collection depends on the row cache being
  current. A stale cache was already a dependency of corpus resolution since
  RDR-204.
- Phases 3 and 4 make collection names a free-form label; scripts and docs
  that build names by string concatenation stop being a supported
  interface.

### Risks and Mitigations

- **Risk**: A row and its name disagree on a production tenant, so a
  repoint changes results. **Mitigation**: Phase 1 Step 2 measures it first;
  each disagreeing row is listed and decided before Phase 2 starts.
- **Risk**: A migration source loses its only way to learn attributes.
  **Mitigation**: the non-stranding rule (Design item 6) and its
  tests-first step.
- **Risk**: RDR-210 lands with name-based sibling resolution. **Mitigation**:
  the census refuses an unclassified new parse site, so RDR-210's code
  surfaces in review either way; Open Question 6 picks the order.
- **Risk**: The engine change to registration lifecycle reaches a client
  that still relies on the prefix inference. **Mitigation**: the client
  never registers quarantine siblings since nexus-syfes (the engine does,
  from the origin's row), so only engine callers change.

### Failure Modes

- A write path reads a collection with no row: `CollectionNotRegisteredError`
  names the collection and the register call that should have preceded the
  write. Visible, not silent.
- A new parse site is added outside `legacy_names`: the client census fails
  with the file, line and shape.
- The SQL live-body gate misresolves a function's last definer: the gate's
  non-vacuity assertion requires it to find at least the functions this RDR
  rewrites, so an empty scan fails rather than passing.

## Implementation Plan

### Prerequisites

- [ ] Sam answers Open Questions 1 and 2 before Phase 3 starts. Phases 1 and
  2 do not wait for them.
- [ ] Critical Assumption 1 measured (Phase 1 Step 2).

### Minimum Viable Validation

On a substrate-backed engine (the local-service gate), a fixture tenant with
one collection whose row was corrected so its `owner_id` and
`embedding_model` differ from its name: `nx index md` into it stamps the
row's model into chunk metadata, `nx search` calibrates with the row's
model, and a quarantine of one of its chunks registers a sibling whose
`lifecycle_state` is `quarantine` without the engine reading the prefix. In
the same run, a recovery bundle recorded under a legacy name restores. Both
census gates are at their Phase 2 pins.

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
  counted separately as expected. Bounded load, results in T2. Blocks P2.2
  onward.
- P1.3 Widen the engine Java gate to the `quarantine-` literal; add the SQL
  live-body gate. First live run pasted into the bead.
- P1.4 `http_catalog_client` computes the next version from the row's
  `model_version`, not by parsing the `for_tuple` result name. Test: a row
  whose `model_version` differs from its name's suffix.
- P1.R Review gate for Phase 1 (code-review-expert and substantive-critic;
  "Ship when the three gates are green at pins that match a fresh census").

**Phase 2: readers move to the row**

- P2.1 Non-stranding tests first: recovery bundle import, migrate-fallback,
  legacy-collection migration, ETL import, each with a row-less legacy name.
  Blocks P2.2 to P2.6.
- P2.2 `resolve_collection` and the `legacy_names` module; move the parsers;
  classify every remaining caller.
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
- P2.7 `repo_identity` sibling lookup and `resolve_corpus` stage 3 by row
  tuple.
- P2.R Review gate for Phase 2. Engine cut for P2.5 and P2.6; client floor
  bumps in the release that ships P2.3 onward.

**Phase 3: names become labels (after Open Questions 1 and 2)**

- P3.1 Replace `validate_collection_name`'s ChromaDB limits with the label
  rule. Test: a 100-character and a non-conformant label are accepted, a
  control character is refused.
- P3.2 Remove the strict conformant guard in `t3.py`, the rename gate, and
  the indexer reroute. Test: a non-conformant label registers with explicit
  attributes and is searchable by corpus.
- P3.3 Registration requires explicit attributes; the client never derives
  them from the name (`http_catalog_client.py:3145-3151`).
- P3.4 Minting per Open Question 2.
- P3.5 Rewrite `docs/collections.md` Rule 3 and `docs/storage-tiers.md` §
  Collections.
- P3.R Review gate for Phase 3.

**Phase 4: retire the parsers**

- P4.1 Delete every parser outside `legacy_names`; both census gates fall to
  the migration-source and diagnostic floor.
- P4.2 `legacy_grandfathered` per Open Question 5.
- P4.G Phase-review gate across all four phases before close.

### Phase dependencies

P1.2 blocks all of Phase 2. P2.1 blocks P2.2 to P2.7. Phase 3 waits for
Phase 2 and for Open Questions 1 and 2. Phase 4 waits for Phase 3 and Open
Question 4.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Census pins (client, engine, SQL) | In scope (the tests print their tables) | In scope | N/A | In scope (`-m lint` and the Java gate tests) | N/A |
| `lifecycle_state` at registration | `nx collection list` | `nx catalog show` | Existing lifecycle verbs | `nx doctor` row versus name diagnostic | Recovery bundle |

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
  four-segment name is restored after each Phase 2 bead — **Verify**: it
  restores with the same attributes as before Phase 2.
- **Scenario**: a new raw parse site of each shape is added to a fixture
  file — **Verify**: the client census fails, naming the shape.
- **Scenario**: `chash_conformance_report` before and after the rewrite on
  the same seeded data — **Verify**: identical rows.

## Validation

### Testing Strategy

The census gates are the acceptance signal for every phase: each bead lowers
a pin and the gate refuses a regression. Behaviour is pinned by the
scenarios above, all against a real engine substrate (no mocks of the
registry).

### Performance Expectations

No new query on a hot path is intended: the row cache already serves corpus
resolution. Critical Assumption 2 states the one place this could fail (a
write path naming a collection the cache does not hold) and P2.3 measures
it.

## Open Questions for Sam

1. **Does the June direction reopen RDR-204 Phase 4?** You rejected opaque
   names for new collections on 2026-09-09, with the reopen trigger "a field
   report of a mint-time name whose segments disagree with the row it
   registered". That trigger has not fired as far as this research found.
   Phases 1 and 2 here do not depend on the answer. If the answer is no,
   this RDR should end at Phase 2 and Phases 3 and 4 move out.
2. **What label does `nx` mint for a new collection?** (a) Keep rendering
   the four-segment shape as a label that nothing reads; (b) a subject or
   repo label, with a disambiguator only when a model sibling exists; (c) an
   opaque generated id with the attributes shown beside it. The label must be
   unique per tenant, because it is the primary key and RDR-225's foreign
   key target, and because RDR-210 siblings of one subject coexist.
3. **Surrogate key: confirm the rejection?** This draft keeps
   `(tenant_id, name)` as the key (Alternative 2).
4. **How long does the migration-source fallback live?** The recovery bundle
   format carries names only. Options: keep `legacy_names` indefinitely for
   bundles and old exports; add the registry row to the bundle format and
   retire the fallback after a stated number of releases; or retire it at
   Phase 4 and refuse old bundles.
5. **`legacy_grandfathered`**: once no shape is required, the flag means
   nothing new. Drop the column, or freeze it as history?
6. **Order with RDR-210**: should RDR-210's sibling resolution adopt the row
   tuple rule now, or land first and be repointed by Phase 2 here?

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. The one tension, with RDR-204's 2026-09-09 ruling, is
stated as Open Question 1 rather than resolved.

### Assumption Verification

Two Critical Assumptions are unverified and each has a bead that verifies
it before the work that depends on it (P1.2 and P2.3).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `GET /v1/catalog/collections/list` with `content_type` and `lifecycle_state` filters | nexus engine | Source Search (RDR-204 Phase 2) |
| `catalog_document_chunks.embedding_model` | nexus schema | Source Search (`vectors-030` lines 786-791) |

### Scope Verification

The Minimum Viable Validation runs in the local-service gate during Phase 2
and is not deferred.

### Cross-Cutting Concerns

- **Versioning**: P2.5 and P2.6 ride an engine cut; the client floor bumps in
  the release that first depends on them, engine deployed first.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: the P2.6 changeset only redefines functions; it is
  walked on a PITR fork rehearsal before the cloud deploy, like every
  changeset.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phases 1 and 2 are independently shippable and
  change no name.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: N/A.

### Proportionality

The schema work is small (no new table; one input on registration; three
function redefinitions). The size is in the client, which is why the plan
widens the census first and moves sites under a ratchet.

## References

- Bead nexus-j4gl6 (description and notes, 2026-06-29); nexus-zvcou (closed).
- `docs/rdr/rdr-204-embedding-profile-and-collection-authority.md`, Phase 3
  and Phase 4 sections.
- `docs/rdr/rdr-225-vector-tables-per-embedding-model.md`, Technical Design.
- `docs/rdr/rdr-210-dual-mode-local-engine.md`, Gap 7 and design item 9.
- `docs/collections.md`, Rules 3 and 6.
- T2 `nexus_rdr/229-research-1` to `-13`.

## Revision History

- 2026-10-17: Drafted from bead nexus-j4gl6 on develop `2ad1f90c9`.
