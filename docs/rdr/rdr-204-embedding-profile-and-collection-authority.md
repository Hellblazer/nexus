---
title: "Collections Stop Encoding Metadata in Their Names: An Install-Scoped Embedding Profile and catalog_collections as the Authority"
id: RDR-204
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-06
accepted_date:
related_issues: []
related_rdrs: [RDR-101, RDR-103, RDR-109, RDR-137, RDR-160, RDR-162, RDR-164, RDR-191, RDR-194]
---

# RDR-204: Collections Stop Encoding Metadata in Their Names

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

A collection (a named set of chunks that share one embedding model, the
unit `search` and `query` route over) carries four facts about itself in
its name: `<content_type>__<owner_id>__<embedding_model>__v<n>`, for
example `code__1-1__voyage-code-3__v1`. The database also has a table for
exactly these facts, `nexus.catalog_collections`, with a column for each
segment and a foreign key from every chunk row to it. Both exist. Only the
name is trusted.

Read on develop at `d77f9969c` (2026-09-06):

- The engine parses the name in six places. Embedder routing takes
  `segments[2]` as the model (`EmbedderRouter.resolveEmbedderStrict`);
  the vector repository derives the vector dimension from the same segment
  (`PgVectorRepository.dimForCollection`) and parses content type in three
  more methods; the combined-write path and staging promote parse the
  prefix.
- The client parses it in six more: corpus resolution (`corpus.py`
  `resolve_corpus`, `embedding_model_for_collection*`), the MCP corpus
  fan-out (`core.py` `_resolve_corpus_target`, `_group_collections_by_model`),
  the exporter, the reconciler, the recovery bundle, and context loading.
- The table is written with blanks. The aspect and taxonomy repositories
  insert a stub row with empty `content_type`, `owner_id`, and
  `embedding_model` when a collection is missing, so that their own
  foreign keys can be satisfied. A row's presence therefore proves nothing
  about its columns, and nobody reads them.

The cost is not theoretical. GH #667 (RDR-109 gap 1) was local-mode
collections named `voyage-*` while embedded by a local ONNX model, because
the name was composed from a default rather than from what the embedder
did. RDR-160 and RDR-162 then spent two RDRs making the name tell the
truth again. The fact the name lies about, which model embedded these
vectors, is the one fact every search depends on.

The reason the name carries this load has expired. RDR-101 and RDR-103
put the tuple in the name because ChromaDB (the vector store retired at
RDR-155) had no collection metadata worth relying on and constrained
names to a regex; the name was the only durable place. Postgres is the
store now, the table exists, and the FK is in place (`fk-002`).

The premise this RDR rests on, and the thing that keeps it small: **no
installation today chooses an embedding model per collection.** Local
mode embeds every content type with bge-768 (RDR-160). Cloud mode embeds
code with voyage-code-3 and everything else with voyage-context-3. The
model is a function of the install and the content type. The name makes
it look like a free per-collection choice, and that appearance is what
GH #667 exploited.

### Enumerated gaps to close

#### Gap 1: Two sources of truth for a collection's attributes, and the wrong one is read

`catalog_collections` has the columns; twelve code sites parse the name
instead. The fix makes the table the only source and deletes the parsers,
enforced by a census gate that only shrinks.

#### Gap 2: The table cannot be trusted because stub rows carry blanks

Foreign-key satisfaction inserts rows with empty attribute columns. The
fix forbids blanks by constraint, backfills every existing row once, and
turns the stub path into "register properly or fail loud".

#### Gap 3: The embedding model is modelled as a per-collection choice it has never been

Nothing records that an install has one model per content type, so the
schema cannot refuse a collection whose recorded model disagrees with
what the install can embed. The fix adds an install-scoped embedding
profile that collections inherit from and are checked against.

#### Gap 4: Lifecycle state is encoded in the name too

The orphan GC parks chunks in `quarantine-<content_type>__...` siblings
(nexus-xukbj) so that the `quarantine-` prefix falls outside every search
corpus. That is a lifecycle state spelled as a content-type prefix. The
fix records it as a column on the same table and lets corpus resolution
exclude by column.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-101 (event-sourced catalog) | Origin | Created `catalog_collections` as a "first-class collections projection" (changeset catalog-001-5) and defined the four-segment name. Its rationale for the name was ChromaDB's metadata poverty and name regex. Both are gone with RDR-155. The projection it created is what this RDR promotes to authority. |
| RDR-103 (catalog as collection-name authority) | Origin | Made the catalog the only place that *renders* a name, via `CollectionName` and `collection_for`. It kept parsing as the read path ("`CollectionName.parse` is strict"). Its rendering discipline stays; its parse-as-read contract is what this RDR retires. |
| RDR-109 (honest local-mode naming) | Precedent | Diagnosed GH #667: local collections labelled `voyage-*`. Fixed by widening the token set so a local model gets a local-shaped token. It repaired the label; this RDR removes the label's authority so the class cannot recur. |
| RDR-160 / RDR-162 (bge-768 local embedder; truthful post-160 upgrade path) | Precedent | Established that local mode has exactly one model for every content type and built a parity gate against it. The "one model per install per content type" fact this RDR encodes is theirs, verified in production. |
| RDR-137 (eliminate repos.json) | Precedent | Same move one level up: retired a side file that duplicated a catalog fact and made the catalog canonical. The census-and-delete method it used is reused here. |
| RDR-164 (lifecycle cascade consolidation) | Precedent | Moved cross-store cascades onto the engine. The rename and delete cascades it built are the reason no collection needs renaming here: the name can stay a stable handle. |
| RDR-191 (unify chunk tables) | Precedent | Made `nexus.chunks` one table with three typed vector columns and `exactly_one_embedding`. That column, not the name, is the ground truth for a collection's dimension, and it is what the backfill verifies against. |
| RDR-194 (FK census) | Precedent | Enforced every expressible relationship, including `chunks_collection_fk` in `fk-002`. The FK is why "name as opaque handle" already holds structurally. |
| RDR-144 (guided embedder provisioning) | Adjacent, closed | Made the local 384-vs-768 choice explicit at `nx init`. The embedding profile is where that choice is now recorded, so `nx init` writes the profile rather than only picking a model. |

Searched the table for "collection", "naming", "embedder", "embedding",
"catalog", "quarantine", "corpus". No draft RDR overlaps.

## Context

### Background

Every consumer that needs a collection's model, dimension, content type,
or owner gets it from `name.split("__")`. RDR-103 made rendering
consistent so that parsing would be safe; it did not make parsing
unnecessary. Meanwhile the table those facts belong in is written by five
paths, two of which write blanks, and read by none of the hot paths.

### Technical Environment

- Engine: Java service, jOOQ generated DSL only (no SQL strings; nexus-zrcj7),
  Liquibase changelog under `service/src/main/resources/db/changelog`.
- `nexus.catalog_collections(tenant_id, name, content_type, owner_id,
  embedding_model, model_version, display_name, legacy_grandfathered,
  superseded_by, superseded_at, created_at)`, PK `(tenant_id, name)`.
- `nexus.chunks(tenant_id, collection, chash, embedding_384, embedding_768,
  embedding_1024, ...)` with `exactly_one_embedding` and
  `chunks_collection_fk -> catalog_collections`.
- `CollectionRegistry` (engine) caches "this name has a row" per tenant.
- Client: `nexus.corpus`, `nexus.catalog.collection_name.CollectionName`,
  `HttpCatalogClient` with `/v1/catalog/collections/{list,get,for_tuple,...}`.

## Research Findings

### Investigation

Source search on develop `d77f9969c`, 2026-09-06. Live census on this box
(cloud mode, tenant `default`) on 2026-09-06 ~23:30Z: `catalog_collections`
rows read through `HttpCatalogClient.list_collections()`, joined to the
per-collection chunk counts from `nx collection list`. Full numbers in T2
`nexus/rdr-204-catalog-collections-census-2026-09-06` [24785].

### Key Discoveries

- **Verified**: 12 name-parse sites, 6 engine and 6 client (listed in the
  Problem Statement). The client also carries three `embedding_model_for_*`
  helpers in `corpus.py` that are parsers by another name.
- **Verified**: `AspectRepository` (RDR-164 P1a) and `TaxonomyRepository`
  (RDR-156 P0.2) insert `catalog_collections` stub rows with empty
  attribute columns to satisfy their FKs.
- **Verified**: `chunks_collection_fk` exists (`fk-002-collection-registry.xml`),
  so a chunk cannot reference a collection without a row. The name is
  already a key; it is only its *content* that is still read.
- **Verified**: 70 live collections (at least one chunk) against 223
  `catalog_collections` rows. The 153 rows with zero chunks are ghosts:
  106 two-segment Chroma-era names, 45 four-segment, 2 three-segment. All
  22 `legacy_grandfathered` rows are ghosts. Ghosts have no vector column to
  verify against, which changes the backfill (see Technical Design step 3).
- **Verified**: 158 of 223 rows carry blank `content_type`, `owner_id`, and
  `embedding_model`. Twelve of those blank rows belong to LIVE collections
  (`code__1-{2,3,5}`, `docs__1-{2,3,4,5}`, `docs__default`, `rdr__1-{2,3}`
  and two more): the stub inserts landed on real collections, so Gap 2 is
  live, not hypothetical.
- **Verified**: among the 70 live collections the model is a function of
  content type with zero exceptions: `code` and `quarantine-code` are
  voyage-code-3; `docs`, `rdr`, `knowledge` and their quarantines are
  voyage-context-3. The only `minilm-l6-v2-384` rows on the tenant are four
  ghosts with zero chunks (`code|docs|rdr__1-2188__...` and
  `knowledge__ingestgate__...`), which is the 384-era relic class RDR-144
  and RDR-162 retired.
- **Verified**: where a row has a model, it agrees with the name's model
  segment in every case (0 disagreements). The table is wrong by omission,
  not by contradiction.
- **Verified**: `GET /v1/catalog/collections/list` takes no filter
  parameters today (`CatalogHandler.handleCollectionList` calls
  `repo.listCollections(tenant)` unconditionally); the `content_type` and
  `lifecycle_state` filters are new work in Phase 3.
- **Verified**: the vector dimension ground truth already exists as a view.
  `nexus.collection_vector_stats` (vectors-005-1) derives `dim` per
  `(tenant, collection)` from the stored column (`CASE WHEN embedding_384 IS
  NOT NULL THEN 384 ...`) and groups by `(collection, dim)`. Read live
  through `GET /v1/vectors/collections`: 70 rows for 70 collections, every
  one at 1024, no collection with two dimensions. The backfill's verify
  step is a join against this view, not new SQL.
- **Verified**: the client already carries the profile as code.
  `corpus.effective_embedding_model_for_writes(content_type)` (RDR-109 P2)
  returns the local token in local mode, the per-content-type Voyage token
  in cloud mode, and mirrors cloud when `local.embed_model=voyage-*` is
  configured (nexus-35ok4, GH #1461); the engine mirrors it with a
  pure-Voyage or pure-ONNX router. The profile table is that function as
  data; the GH #1461 opt-in becomes a profile write.
- **Verified**: registration derives a collection's attributes by parsing
  the name it has just rendered (`indexer.py:785-800`), and one path
  hardcodes `embedding_model="voyage-context-3"` (`commands/index.py:100`).
  Phase 1 replaces both with a profile read.
- **Verified**: six tables carry FKs into `catalog_collections`
  (`fk-002/003/004`): `chunks`, `document_aspects`, `document_highlights`,
  `aspect_extraction_queue`, `taxonomy_meta`, `topics`, plus the
  `document_chunks` manifest by collection column. The ghost sweep checks
  all of them; the `chunks` FK is `ON DELETE RESTRICT`, so a mistaken
  delete fails loud regardless.
- **Verified**: the model token vocabulary is five tokens on the engine
  (`MODEL_DIMS`: the three Voyage tokens at 1024, `bge-base-en-v15-768`,
  `minilm-l6-v2-384`) and four on the client (no `voyage-3`). The
  `embedding_models` seed is the four shared tokens; `voyage-3` has no
  client token and no live rows and is not seeded.
- **Verified**: `CollectionRegistry` caches only `(tenant, name)` presence:
  process-local, unbounded, marked after commit, evicted on delete and on
  the canonical branch of rename. Holding the row instead of a boolean is
  an additive change to the same class with the same invalidation points,
  plus one new eviction on profile write.
- **Verified**: `_group_collections_by_model` (nexus-3l6gz) groups a corpus
  fan-out by parsed model so a mixed-model query is embedded once per
  model. It needs the model per collection, not the name.
- **Documented**: RDR-160 fixed local mode to bge-768 for all content types;
  RDR-162's classifier and the `test_embed_parity.py` gate depend on that.

### Critical Assumptions

- [x] Every install has exactly one embedding model per content type, and it
  is a function of mode alone. **Status**: Verified on this tenant (70 live
  collections, zero exceptions) and by RDR-160's design for local mode.
  **Method**: Spike (live census) + Source Search. Still to run before the
  backfill ships: the same grouping across every cloud tenant, read-only,
  from conexus's side (a relay to Sam; this box sees one tenant).
- [x] For every collection that owns chunks, the non-null vector column in
  `nexus.chunks` is the same for all its rows and matches the profile.
  **Status**: Verified on this tenant through `collection_vector_stats`
  (70 collections, one dimension each, all 1024). **Method**: Spike. The
  backfill re-runs the same check on every install as its precondition;
  ghost rows are outside it by construction.
- [x] No consumer needs a fact from the name that the table cannot carry.
  **Status**: Verified by the parse-site census: every extracted value is
  one of content_type, owner_id, embedding_model, or the quarantine prefix.
  **Method**: Source Search.
- [x] `CollectionRegistry`'s cache can hold the row, not only the name,
  without a correctness change to its invalidation. **Status**: Verified by
  reading the class: its invalidation points are delete and rename, both
  post-commit; profile write is the one new point. **Method**: Source Search.

## Proposed Solution

### Approach

Make the table true, then make everything read it, then stop parsing.
Nothing is renamed and no chunk moves.

### Technical Design

**1. Install-scoped embedding profile.** This is the client's existing
`effective_embedding_model_for_writes` turned into data. New table
`nexus.embedding_profile(tenant_id, content_type, embedding_model,
dimension, PRIMARY KEY (tenant_id, content_type))`, plus a reference table
`nexus.embedding_models(embedding_model PRIMARY KEY, dimension, provider)`
seeded with the four models the engine can serve. `nx init` writes the
profile for the chosen mode; a cloud tenant's profile is written at tenant
mint. The profile is the only place a model is chosen.

**2. Collections inherit.** `catalog_collections.embedding_model` and a new
`dimension` column stay as denormalised facts for joins, but they are
written from the profile at registration and constrained: NOT NULL and
non-empty on `content_type`, `owner_id`, `embedding_model`; a FK to
`embedding_models`; and a trigger-or-check that the row's model equals
`embedding_profile(tenant_id, content_type)`. A register call naming a
different model is a 422 with the profile's value in the message. A new
`lifecycle_state` column (`live` | `quarantine`) replaces the
`quarantine-` prefix as the thing corpus resolution excludes.

**3. The one-time backfill.** Two changesets, in order. The first sweeps
ghosts: a `catalog_collections` row with zero `nexus.chunks` rows, zero
`document_chunks` manifest rows, and zero aspect or highlight references is
deleted (153 of 223 rows on this tenant; a row still referenced by a
manifest or an aspect is kept and reported, never guessed at). The second
walks every surviving row. Ground truth is `nexus.collection_vector_stats`
(one row per `(collection, dim)` derived from the stored column): exactly
one row per collection gives the dimension, two rows is a disagreement;
the profile gives the model for that content type; the two must agree. On
agreement the row is written from the profile and the name is never read.
On disagreement, or on a collection whose rows use more than one column,
the changeset fails naming the collection and the remedy (re-embed under
the profile). Content type and owner are taken from the name exactly once
here, as the last parse; `quarantine-` becomes `lifecycle_state =
'quarantine'` with the base content type. Grandfathered two-segment names
are backfilled the same way and keep their flag.

**4. Engine reads the row.** `CollectionRegistry` caches the row. The six
engine parse sites resolve model, dimension, and content type from it.
`EmbedderRouter` resolves by content type through the profile; the
"unavailable model" 422 becomes "this install's profile names a model this
mode cannot serve", which is the true condition. The stub-insert paths in
`AspectRepository` and `TaxonomyRepository` are deleted; a write against an
unregistered collection fails loud.

**5. Client resolves through the catalog.** `corpus="code"` becomes
`GET /v1/catalog/collections/list?content_type=code&lifecycle_state=live`.
The six client parse sites and the three `embedding_model_for_*` helpers
read the row. `_group_collections_by_model` groups by the column.
`CollectionName.render` stays as the way a new name is minted; `parse`
survives only inside the backfill and the census gate.

**6. Optional, last, and out of the accepted scope:** once nothing parses,
a new collection may be given an opaque name. Existing names never change.

### Existing Infrastructure Audit

- `catalog_collections` and its tuple index `(tenant_id, content_type,
  owner_id, embedding_model)` exist and are reused.
- `chunks_collection_fk` exists; nothing structural is added on `chunks`.
- `CollectionRegistry` exists and is extended, not replaced.
- `/v1/catalog/collections/list`, `get`, `for_tuple` exist; `list` gains
  two filter parameters.
- The RawSqlGateTest pattern exists and is copied for the parse census.

### Decision Rationale

The alternative that kept the name authoritative and merely repaired the
table was rejected because it leaves two sources of truth, which is the
condition GH #667 came from. The alternative that renamed every collection
to an opaque id was rejected because it moves nothing worth moving: the FK
already makes the name a key, and a rename cascade across 61 collections
and every manifest is the largest, riskiest part of any design here for
zero information gain.

## Alternatives Considered

### Alternative 1: Repair the table, keep parsing

Backfill the columns and constrain them, but leave the twelve parsers.
Rejected: the parsers are the read path, so a future write that gets the
name wrong is still believed. This is the status quo with better decor.

### Alternative 2: Rename every collection to an opaque handle now

Rejected as the first move for the reason above. Kept as the optional
final phase, when it costs nothing.

### Alternative 3: Per-collection model choice, table-backed

Model the model as a real per-collection attribute with no profile.
Rejected: it preserves a freedom no install uses, and it is the freedom
that let a default mislabel every local collection. The profile encodes
the fact and lets the constraint refuse the lie.

### Briefly Rejected

- A `collections` view over `chunks` deriving the dimension live: correct
  but an aggregate over every chunk on every routing decision.
- Storing the profile in `nexus.catalog_meta` as JSON: unconstrainable.

## Trade-offs

### Consequences

- A collection can no longer exist with unknown or blank attributes. Paths
  that relied on implicit creation must register first.
- Corpus resolution does a catalog list call instead of a string prefix
  test. The list is already fetched and cached for counts
  (`get_collection_names`), so this is a field read on a call already made.
- The `quarantine-` corpus exclusion becomes a column filter; the GC's
  sibling-collection design otherwise stands.

### Risks and Mitigations

- **A collection's stored vectors disagree with the profile.** This is the
  GH #667 class surfacing at upgrade time. Mitigation: the backfill refuses
  and names the collection; it never guesses from the name. The remedy is
  re-embedding, which the RDR-185 ladder already knows how to sequence.
- **Cloud estate has a tenant with two models for one content type.**
  Would falsify the premise. Mitigation: the read-only grouping query in
  Critical Assumptions runs against the cloud before the changeset is
  cut; a violation stops the RDR at the gate, not in production.
- **`CollectionRegistry` cache staleness after a supersede or rename.**
  Mitigation: the existing evict path fires on rename and delete
  (RDR-164 cascades); extend it to profile writes, and cover it in the
  engine suite.

### Failure Modes

- Register with a wrong model: 422, message carries the profile's model.
- Write against an unregistered collection: 4xx, no stub row.
- Backfill disagreement: changeset fails, engine does not boot on that
  tree, error names the collection. This is deliberate: silently
  proceeding is the fiasco.

## Implementation Plan

### Prerequisites

- Cloud grouping query run and recorded in T2 (assumption 1).
- Engine tip at or past `4fa8eb03c` (peer's nexus-hxrcm changes) so the
  changeset numbering does not collide.

### Minimum Viable Validation

A local-mode install with a `code` and a `docs` collection, after the
changeset: both rows carry `bge-768`/`768` from the profile; a register
call naming `voyage-code-3` is refused with a 422; `nx search
--corpus code` resolves through the catalog with the parse census at zero
on the client; the engine parse census is at zero. This runs inside
`tests/e2e/migration-rehearsal/run.sh --candidate-migration`, because it
walks the tree's own changeset over a populated store.

### Phase 1: Schema and backfill (engine)

1. Changesets: `embedding_models`, `embedding_profile`, new columns and
   constraints on `catalog_collections`, the backfill with its refusal
   precondition. Liquibase only; no Python DDL.
2. Delete the stub inserts in `AspectRepository` and `TaxonomyRepository`.
3. `register_collection` writes from the profile; `nx init` writes the
   profile.
4. Engine suite: profile write, refused register, backfill agree and
   refuse cases, each against a real PG.

### Phase 2: Engine reads the row

1. `CollectionRegistry` caches the row; evict on profile write.
2. Replace the six parse sites; `dimForCollection` and
   `resolveEmbedderStrict` read the registry.
3. `CollectionParseGateTest` in the RawSqlGateTest style: pins the count
   of `split("__")` on collection names; only shrinks.

Phases 1 and 2 ride one engine cut.

### Phase 3: Client reads the catalog

1. `list` route gains `content_type` and `lifecycle_state` filters.
2. `resolve_corpus`, `_resolve_corpus_target`,
   `_group_collections_by_model`, `embedding_model_for_*`, exporter,
   reconciler, recovery bundle, context loader read the row.
3. A lint-bucket census pins client `split("__")` on collection names;
   only shrinks. `CollectionName.parse` callers reduce to the census gate
   and the backfill.

Client-only release.

### Phase 4 (deferred, not accepted here): opaque names for new collections.

### Day 2 Operations

- `nx doctor` gains a row: profile present, every collection agrees with
  it. A disagreement is a red, never a warning.
- `nx collection list` prints the columns, not a parsed name.

### New Dependencies

None.

## Test Plan

- Engine: changeset agree/refuse cases; register 422; registry eviction;
  parse census at target count.
- Client: corpus resolution by content type and lifecycle state; model
  grouping by column; census at target count; exporter and reconciler
  round-trips.
- E2E: `--candidate-migration` rehearsal walks the changeset over a
  populated store and ends green; `test_embed_parity.py` unchanged and
  green.

## Validation

### Testing Strategy

The two census gates are the acceptance signal: each phase lowers its pin
and the gate refuses a regression. The backfill's refusal branch is tested
with a deliberately mis-embedded fixture collection, not asserted from the
happy path.

### Performance Expectations

No new query on the hot path. Routing reads a cached row where it used to
split a string; corpus resolution reads fields of a list already fetched.

## Finalization Gate

### Contradiction Check

Pending gate.

### Assumption Verification

Pending gate. Assumption 1 requires the cloud grouping query; assumption 2
is the backfill precondition itself; assumption 4 is a Phase 2 source
search.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `/v1/catalog/collections/list` filters | nexus engine | Source Search |
| Liquibase `precondition` failing a changeset | Liquibase | Docs Only, to be upgraded by the rehearsal spike |

### Scope Verification

The MVV runs inside the candidate-migration rehearsal during Phase 1 and
is not deferred.

### Cross-Cutting Concerns

- **Versioning**: engine cut carries Phases 1 and 2; the client floor bumps
  to it in the release that ships Phase 3 (paired-release choreography).
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: the changeset walks on the cloud via the PITR fork
  rehearsal first (`deploy/RESTORE.md`), because it can refuse.
- **IDE compatibility**: N/A.
- **Incremental adoption**: phases are independently shippable; Phase 3
  tolerates a pre-Phase-1 engine by failing loud on the missing filter.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: N/A.

### Proportionality

Right-sized: one new table pair, columns on an existing one, twelve
parse-site deletions, two census gates. Phase 4 is held out of scope on
purpose.
