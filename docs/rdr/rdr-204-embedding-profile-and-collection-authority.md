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

- The engine parses the name in eight places (full grep, 2026-09-06, T2
  `204-research-4`): embedder routing takes `segments[2]` as the model
  (`EmbedderRouter.resolveEmbedderStrict`); the vector repository derives
  the dimension from the same segment (`PgVectorRepository.dimForCollection`)
  and splits the name in three more methods; the combined-write path and
  staging promote take the content type from the prefix.
- The client parses it in about sixty raw string sites (`split("__")`,
  `partition("__")`, `startswith("code__")` and the like, across
  `corpus.py`, `core.py`, `commands/*`, `db/*`, `catalog/*`, `scoring.py`,
  `exporter.py`, `context.py`) plus thirty-four callers of the three
  helpers that derive a model from a name
  (`embedding_model_for_collection*`, `voyage_model_for_collection`,
  `parse_conformant_collection_name`). An earlier cut of this RDR counted
  twelve; the gate critique caught the undercount, and the number is now a
  mechanized census, not a hand count.
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

`catalog_collections` has the columns; roughly a hundred code sites parse
the name instead. The fix funnels every raw string site through three
helpers, repoints the helpers at the table, and deletes what is left,
enforced on both sides by a census gate that only shrinks to zero.

#### Gap 2: The table cannot be trusted because stub rows carry blanks

Foreign-key satisfaction inserts rows with empty attribute columns. The
fix forbids blanks by constraint, backfills every existing row once, and
turns the stub path into "register properly or fail loud".

#### Gap 3: The embedding model is modelled as a per-collection choice it has never been

Nothing records that an install has one model per content type, so the
schema cannot refuse a new collection whose model disagrees with what the
install embeds with. The fix adds an install-scoped embedding profile that
new collections inherit from. The profile is a setting, not a constant:
`local.embed_model` is already user-mutable (GH #1461), so the profile
must be updatable, and existing collections keep the model they were
embedded with rather than being refused when the profile moves.

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
| RDR-103 (catalog as collection-name authority) | Origin | Made the catalog the only place that *renders* a name, via `CollectionName` and `collection_for`, and kept parsing as the read path ("`CollectionName.parse` is strict"). One of its rationales survives ChromaDB and this RDR keeps it: "switching embedding models necessarily creates a new collection (the vectors are not compatible)", so a collection stays the unit of embedding identity and a profile switch mints a new sibling, never re-embeds in place. The rendering discipline and the unique tuple index stay; only parse-as-read is retired. |
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

### Two facts about the grain that this RDR does not change

A document belongs to exactly one collection: `catalog_documents.
physical_collection` is a single column, the manifest inherits it, and a
chunk row is keyed by `(tenant, collection, chash)`. A repo indexed under
two models is therefore two documents with the same source URI and no link
between them, not one document with two embeddings. The profile design
keeps this: a switch mints a sibling collection.

The collection is also the grain of nearly every quality decision: the
corpus fan-out and its thin-collection floor, the per-collection top-N
then merge, the dedup boundary (identical text in two repos is two rows),
per-collection taxonomy (RDR-075 exists to project topics across it),
aspects and highlights, and code-versus-prose scoring by prefix. Those
mechanisms were written against a bundle of three unrelated things: the
embedding space physics requires, the owner lifecycle wants, and the
content type scoring wants. This RDR unbundles the attributes into
columns and stops there. Moving each mechanism to the grain it actually
wants (fan-out and ranking per model space, dedup per model per tenant,
taxonomy per tenant per model with owner and content type as filters,
scoring by document content type) is a follow-on RDR whose problem
statement this paragraph is.

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

- **Verified**: the parse surface, by grep on develop `b604a4a5b`
  (T2 `204-research-4`): engine 8 sites (`EmbedderRouter:327`,
  `PgVectorRepository:414/712/945/2180`, `CombinedWriteService:323`,
  `StagingPromoteOps:317-318`); client about 60 raw string sites and 34
  callers of the three model-deriving helpers. Load-bearing client
  examples the earlier count missed: `commands/collection.py:422-423`
  (rename validity by prefix), `db/http_vector_client.py:835/868`
  (write-path batch sizing by prefix), `catalog/orphan_backfill.py:322`
  (backfill content type), `scoring.py:257/279/305` (code-versus-prose
  scoring by prefix), `db/t3.py:77/354/388` (model for a write). Matches
  on `mcp__` tool names and `rdr-` document ids are excluded; they are not
  collection names.
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
- **Verified**: a failed changeset stops the engine (`SchemaMigrator` wraps
  `LiquibaseException` in `MigrationException`). With the 2026-08-16
  never-wedge directive this rules out a refusing backfill; see Technical
  Design step 3 for the disputed-row design that replaced it.
- **Verified**: the grandfather-or-raise switch table already exists
  (`corpus.resolve_write_embedding_model`, nexus-o5x2c, GH #1461) and is
  documented in `docs/cli-reference.md` under "Local mode with Voyage";
  its probe is what the profile design repoints at `for_tuple`.
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

- [x] Every install has exactly one embedding model per content type among
  the collections that carry vectors, and it is a function of the install.
  **Status**: Verified on both active production tenants (T2
  `204-research-6`, read by the conexus session on the live cluster,
  2026-09-07 00:35Z): zero exceptions among collections with vector stats;
  the single catalog-row exception is the zero-chunk
  `knowledge__ingestgate__minilm-l6-v2-384__v1` ghost the sweep removes.
  The second tenant embeds `knowledge` with voyage-code-3, which is why the
  profile is keyed per tenant and not a global constant per content type.
  **Method**: Spike (two live censuses) + Source Search.
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
- [x] No two-segment (model-less) collection carries vectors on any
  censused tenant. **Status**: Verified on both tenants: all 106 two-segment
  rows have zero chunks. **Method**: Spike. The walk still has a rule for
  the case (Technical Design step 3) so a future tenant that falsifies this
  gets `disputed` or a profile-derived model, never a wedge.
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
`effective_embedding_model_for_writes` turned into data, including its
`local.embed_model` opt-in (which is why 1a below exists). New table
`nexus.embedding_profile(tenant_id, content_type, embedding_model,
dimension, PRIMARY KEY (tenant_id, content_type))`, plus a reference table
`nexus.embedding_models(embedding_model PRIMARY KEY, dimension, provider)`
seeded with the four models the engine can serve. The engine is the only
writer: it upserts the local tenant's rows at boot from its own mode
decision (1a), and `nx init` reaches the same outcome only because it
starts the service, not because it writes anything itself. The engine has
no tenant-mint route (tenants exist through data-token mint at the edge),
so a cloud tenant's profile is seeded lazily and idempotently by the engine
on the first registration for a content type, from the same mode decision.
The profile is the only place a model is chosen.

**1a. The profile is updatable, and a switch mints a sibling.**
`local.embed_model` can change at any time through `nx config set`
(GH #1461), and that is the feature, not a bug. The switch semantics
already exist as code: `corpus.resolve_write_embedding_model` implements
the documented grandfather-or-raise table (key absent and an existing
local-token collection: write there; key absent and nothing: raise; key
present: the canonical Voyage token, a NEW sibling collection). This RDR
keeps that table verbatim and changes only where the "does a collection
exist for this content type and owner under token X" probe looks: the
catalog's `for_tuple` lookup instead of a rendered-name test.

Who writes the profile, and when: the ENGINE, at boot, from its own mode,
and nobody else. `Main.java` already decides the mode once per process
(`NX_VOYAGE_API_KEY` present: pure Voyage routing; absent: the ONNX local
embedder), and that decision is the profile. At boot the engine upserts
one row per content type for the local tenant (cloud tenants get theirs
lazily, see step 1), idempotent, from the same branch that constructs the
router. This is the trigger the GH #1461 recipe already has: the
documented switch is `nx config set local.embed_model ...`, `nx config set
voyage_api_key ...`, then a service restart, and the restart is the boot.
`nx config set` writes nothing to the profile. Today it prints no restart
hint for this flow either (gate critique [24806]; the restart is documented
only in the CLI reference), so Phase 3 gives `nx config set` a one-line
hint when the key is `local.embed_model` or `voyage_api_key`: restart the
service for the engine to adopt it. Until the restart, the engine's profile
and the client's intended model differ, and `nx doctor`'s profile row
names that (Day 2), which turns today's invisible "did you restart?" state
into a visible one. `nx upgrade` is not involved; the earlier draft's
claim that it was is withdrawn (gate critique [24800]). A profile change
never touches an
existing collection's row: the row records the model its vectors were
embedded with, which is a fact about stored bytes, and the profile
records what new collections get. `nx doctor` reports every live
collection whose model differs from the current profile as needing
re-embedding under the new profile, the same re-index GH #1461 already
requires after a switch; reads of such a collection continue to route by
the row's own model and are never refused.

**2. Collections inherit at registration.** `catalog_collections.
embedding_model` and a new `dimension` column stay as denormalised facts
for joins. They are constrained: NOT NULL and non-empty on
`content_type`, `owner_id`, `embedding_model`, and a FK to
`embedding_models`. Registration of a NEW collection writes the model
from the current profile; a register call that names a different model is
a 422 carrying the profile's value. There is deliberately no constraint
tying an existing row to the current profile (see 1a). A new
`lifecycle_state` column (`live` | `quarantine` | `dormant` | `disputed`)
replaces the `quarantine-` prefix as the thing corpus resolution excludes;
only `live` rows take part in a bare-corpus fan-out.

**3. The one-time backfill, which never wedges an upgrade.** Two
changesets, in order, and neither can fail on data: the engine wraps any
Liquibase failure in `MigrationException` and refuses to boot, which on
the cloud is the single live environment and on a laptop is a bricked
`nx upgrade`. The standing rule for cleanup migrations (2026-08-16) is
warn loudly, delete garbage, constrain after, never abort. An earlier
cut of this design refused on disagreement; that was wrong.

The first changeset sweeps ghosts: a `catalog_collections` row with zero
`nexus.chunks` rows, zero `document_chunks` manifest rows, and zero rows in
each of `document_aspects`, `document_highlights`,
`aspect_extraction_queue`, `taxonomy_meta`, and `topics` (the six tables
whose `ON DELETE RESTRICT` FKs point at it, `fk-002/003/004`; the same set
RDR-164's `deleteCollectionTxn` deletes in order) is deleted, counts reported
with `RAISE NOTICE`. The live census counted chunk-emptiness only (153 of
223 rows on this tenant); the sweep's condition is stricter, so the
deleted count will be at most 153 and the difference is the next class.
A row still referenced anywhere but owning no chunks is kept, gets
`lifecycle_state = 'dormant'`, takes its `content_type`, `owner_id` and
`embedding_model` from the name with `dimension` NULL (there are no
vectors to read), and is reported by notice and by `nx doctor` as
"referenced but empty: re-index it or remove the references". Dormant
rows are outside the disputed rule by construction, since there is no
stored dimension to disagree with.

The second walks every surviving row. `content_type` and `owner_id` come
from the name, the last parse this codebase performs. `embedding_model`
comes from the name's token when that token's dimension equals the
collection's stored dimension in `nexus.collection_vector_stats` (one row
per collection, derived from the non-null vector column). When they
disagree, or a collection has two dimensions, the row keeps the name's
attributes, gets `lifecycle_state = 'disputed'`, and is reported; a
disputed collection is excluded from bare-prefix corpus fan-out and shown
red by `nx doctor` with the remedy (re-index under the current profile).
Measured today, zero live rows would be disputed. NOT NULL, the FK to
`embedding_models`, and the CHECKs are added after this rewrite, so the
constraining step cannot fail. `quarantine-` prefixes become
`lifecycle_state = 'quarantine'` with the base content type. Grandfathered
two-segment names (`<content_type>__<owner>`, no model token) take
`content_type` and `owner_id` from the name and, when they own chunks,
their model from the profile for that content type if the profile model's
dimension equals the stored dimension, else `disputed`; with no chunks they
are ghosts or dormant like any other row. They keep their flag. On both
censused tenants every two-segment row is a ghost (T2 `204-research-1`,
`-6`), so this branch is expected to write nothing; it exists so the walk
has an answer for every row rather than an assumption.

**4. Engine reads the row.** `CollectionRegistry` caches the row. The six
engine parse sites resolve model, dimension, and content type from it.
`EmbedderRouter` resolves by content type through the profile; the
"unavailable model" 422 becomes "this install's profile names a model this
mode cannot serve", which is the true condition. The stub-insert paths in
`AspectRepository` and `TaxonomyRepository` are deleted; a write against an
unregistered collection fails loud.

**5. Client resolves through the catalog, in two moves.** The client's
collection cache (`mcp_infra.get_collection_names`) is fed by
`/v1/vectors/stats` (name, dim, count). Decision: join the catalog
attributes (`content_type`, `owner_id`, `embedding_model`,
`lifecycle_state`) into that response, server side, and leave the client
cache untouched: same one round trip, same TTL, the cached rows simply
carry four more fields that the three helpers read. Switching the cache
to the catalog list was considered and rejected because it would change
the cache's population (the catalog knows ghosts the stats route does
not) and its refresh path at the same time as the repoint. The catalog
list route still gains `content_type` and `lifecycle_state` filters for
the CLI verbs that read it directly. First the
funnel: every raw string site (about sixty) is rewritten to call one of
three helpers, `collection_content_type(name)`, `collection_model(name)`,
`collection_owner(name)`, which at that point still parse; this is
mechanical, reviewable per file, and leaves behaviour byte-identical.
Then the repoint: the three helpers read the catalog row (the list call
is already fetched and cached for collection counts) and
`corpus="code"` becomes
`GET /v1/catalog/collections/list?content_type=code&lifecycle_state=live`.
`_group_collections_by_model` groups by the column. `CollectionName.render`
stays as the way a new name is minted; `parse` survives only inside the
backfill and the census gate. The census gate pins the raw-site count at
each step and only shrinks; the repoint is one change, not sixty.

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

Backfill the columns and constrain them, but leave the parsers.
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

- **A collection's stored vectors disagree with its name's model.** This
  is the GH #667 class surfacing at upgrade time. Mitigation: the backfill
  marks the row `disputed`, keeps it out of fan-out, and names it in
  doctor with the re-index remedy; it never guesses a model from a
  dimension (1024 maps to three Voyage tokens) and never wedges the walk.
- **Cloud estate has a tenant with two models for one content type.**
  Would falsify the premise. Mitigation: the read-only grouping query in
  Critical Assumptions runs against the cloud before the changeset is
  cut; a violation stops the RDR at the gate, not in production.
- **A user changes `local.embed_model` after init.** An earlier cut of this
  design froze the profile and would have refused every later registration
  with a 422 (gate critique, Critical 2). Mitigation is structural (1a): the
  profile is updatable, existing rows are never checked against it, and
  doctor names the collections that now need re-embedding.
- **`CollectionRegistry` cache staleness after a supersede or rename.**
  Mitigation: the existing evict path fires on rename and delete
  (RDR-164 cascades); extend it to profile writes, and cover it in the
  engine suite.

### Failure Modes

- Register with a wrong model: 422, message carries the profile's model.
- Write against an unregistered collection: 4xx, no stub row.
- Backfill disagreement: the row is marked `disputed`, reported by
  `RAISE NOTICE` and by `nx doctor`, excluded from fan-out, and the
  engine boots. The walk never fails on data (2026-08-16 directive);
  "loud" is the notice plus the doctor red, not a refused upgrade.

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
   constraints on `catalog_collections`, the ghost sweep, the walk with
   its disputed and dormant outcomes, constraints added last. Liquibase
   only; no Python DDL; no failing precondition anywhere.
2. Engine boot writes the profile from its mode decision in `Main.java`
   (local tenant at boot; cloud tenants lazily at first registration),
   idempotent upsert, and `CollectionRegistry` evicts on that write.
3. Delete the stub inserts in `AspectRepository` and `TaxonomyRepository`.
4. `register_collection` writes the model from the profile; a different
   model in the request is a 422 naming the profile's value. `nx config
   set` is unchanged: it does not write the profile, it reminds the user
   to restart, which is the write.
5. Engine suite, each against a real PG: boot in ONNX mode yields the
   bge rows and boot with a Voyage key yields the Voyage rows; a second
   boot changes nothing; refused register; backfill agree, disputed
   (a fixture collection whose stored dimension disagrees with its name)
   and dormant (a row referenced by a manifest with no chunks) cases;
   ghost sweep deletes the unreferenced and keeps the referenced.
6. The GH #1461 recipe, end to end on the engine substrate: bge profile,
   set a Voyage key, restart, profile now Voyage, existing bge collection
   still readable and still registered under bge, a new write mints the
   Voyage sibling.

### Phase 2: Engine reads the row

1. `CollectionRegistry` caches the row; evict on profile write.
2. Replace the six parse sites; `dimForCollection` and
   `resolveEmbedderStrict` read the registry.
3. `CollectionParseGateTest` in the RawSqlGateTest style: pins the count
   of `split("__")` on collection names; only shrinks.

Phases 1 and 2 ride one engine cut.

### Phase 3: Client reads the catalog

1. Census gate first: a lint-bucket test that greps the raw patterns
   (`split("__")`, `partition("__")`, `startswith("<type>__")`,
   `"__" in`) on collection names under `src/nexus`, with an explicit
   exclusion list for `mcp__` tool names and `rdr-` ids, pinned at the
   measured count. Every later step lowers the pin.
2. Funnel: rewrite the raw sites to the three helpers, file by file,
   behaviour unchanged; the gate falls to the helpers' own internals.
3. `list` route gains `content_type` and `lifecycle_state` filters; the
   stats route gains the joined catalog attributes.
4. `nx config set` prints the restart hint for `local.embed_model` and
   `voyage_api_key` (the client's only touch on the profile story).
5. Repoint the three helpers and `resolve_corpus` / `_resolve_corpus_target`
   / `_group_collections_by_model` at the row; the gate falls to zero
   outside the backfill.
6. `CollectionName.parse` callers reduce to the census gate and the
   backfill.

Client-only release.

### Phase 4 (deferred, not accepted here): opaque names for new collections.

### Day 2 Operations

- `nx doctor` gains a profile row: the engine's profile per content type,
  and whether the client's intended model (`local.embed_model` and key
  state) agrees with it. A disagreement means the service has not been
  restarted since the config change, and the row says so; this is the
  GH #1461 "did you restart?" blind spot made visible. Live collections
  under a model other than the profile's are listed as needing
  re-embedding, informational; `disputed` and `dormant` rows are listed
  red with their remedies. `nx collection shape` already reports the
  pre-RDR view of the same population and stays the curation tool.
- `nx collection list` prints the columns, not a parsed name.

### New Dependencies

None.

## Test Plan

- Engine: boot writes the profile in both modes and is idempotent;
  changeset agree, disputed, dormant and ghost cases; register 422;
  registry eviction on profile write; the GH #1461 restart journey; parse
  census at target count.
- Client: corpus resolution by content type and lifecycle state; model
  grouping by column; census at target count; exporter and reconciler
  round-trips.
- E2E: `--candidate-migration` rehearsal walks the changeset over a
  populated store and ends green; `test_embed_parity.py` unchanged and
  green.

## Validation

### Testing Strategy

The two census gates are the acceptance signal: each phase lowers its pin
and the gate refuses a regression. The backfill's disputed branch is tested
with a deliberately mis-embedded fixture collection, not asserted from the
happy path, and the walk is asserted to complete on that fixture.

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
  rehearsal first (`deploy/RESTORE.md`) to read the ghost and disputed
  counts before they run live; the walk itself cannot fail.
- **IDE compatibility**: N/A.
- **Incremental adoption**: phases are independently shippable; Phase 3
  tolerates a pre-Phase-1 engine by failing loud on the missing filter.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: N/A.

### Proportionality

Right-sized for the engine (one new table pair, columns on an existing
one, eight parse sites, one gate). The client half is larger than the
first draft admitted, about sixty raw sites plus thirty-four helper
callers, which is why Phase 3 is a funnel then a single repoint under a
shrinking gate rather than a site-by-site rewrite. Phase 4 is held out of
scope on purpose.
