---
title: "Catalog Inode Flip: Spaces Replace Name-Encoded Collections, Sets Replace Corpus Routing"
id: RDR-203
type: Architecture
status: draft
priority: high
author: Sam
created: 2026-09-05
reviewed-by: self
related_issues: []
related: [RDR-049, RDR-052, RDR-053, RDR-101, RDR-103, RDR-108, RDR-137, RDR-156, RDR-164, RDR-169, RDR-180, RDR-191, RDR-193, RDR-199]
---

# RDR-203: Catalog Inode Flip

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Design of record: T2 `nexus/design-catalog-inode-flip-spaces-and-sets-2026-09-05` [24459], locked with Sam section by section on 2026-09-05. Research inputs: T2 [24451] (document identity models), [24452] (index and collection decoupling), [24454] (collection-name consumer map).

## Problem Statement

A T3 collection is the unit under which the engine stores embedded chunks. Its name has the shape `<content_type>__<owner_id>__<embedding_model>__v<n>`, for example `code__1-1__voyage-code-3__v1`. That string was designed in the ChromaDB era, when a collection was a physical object that could hold vectors of only one width and carried no metadata of its own, so every fact about it had to be spelled into its name. ChromaDB is gone (RDR-155). The engine now stores chunks in one Postgres table with typed vector columns (RDR-191) and already has a `catalog_collections` registry row holding the same four facts. The name is still the only thing anyone reads.

The name does three unrelated jobs at once:

1. **Physical partition.** The engine picks the embedder and the vector width by splitting the name on `__` and looking the third field up in hard-coded maps (`EmbedderRouter.java:302-323`, `PgVectorRepository.java:409-426`; the Python mirror is `reconcile.py:177-183`). The chunk table's primary key is (tenant, collection name, chash), where the chash is the SHA-256 of the chunk's text and serves as its identity. Fifteen or more tables key on the name string. Seven carry a validated foreign key to `catalog_collections(tenant_id, name)` (`chunks`, `topic_assignments`, `document_aspects`, `aspect_extraction_queue`, `topics`, `taxonomy_meta`, `document_highlights`), and two more reference the chunk key (tenant_id, collection, chash) itself: `catalog_document_chunks` (`catalog-029-manifest-chunk-fk.xml`, NO ACTION, deferrable) and `topic_assignments` (`taxonomy-012-doc-id-chunk-fk.xml`, ON DELETE CASCADE). Nine constraints on eight tables.
2. **Classification.** Content type and owner ride in the first two fields. Search routing (`corpus=` on every MCP search tool, `--corpus` on the CLI) is prefix matching on the same string (`corpus.py:876-905`), and the "all" expansion exists in three non-identical copies in `mcp/core.py` (lines 1915, 2332, 3425 onward; the third appends the raw part at 3439-3440 where the others normalise through `t3_collection_name`).
3. **Schema version.** The trailing `v<n>`.

Ted Nelson's rule for tumblers (the hierarchical addresses nexus gives owners and documents, such as `1.7.42`), which nexus adopted in RDR-049, is that an address is independent of subject and category, and that "categorizing directories" belong to the user, never to the system level. The collection name is a categorising directory at system level. The tumbler already says where a document is; the name repeats the owner in a second, lossy encoding (`1.7.42` becomes `1-7`), adds a category the catalog also holds as a column, and welds both to a storage detail the engine should own.

The user-facing cost is that nexus has no organisation primitive at all. A user who wants "these twelve papers" as a thing has to mint a collection, which means picking a name that also fixes the embedding model and the owner, and a paper can live in exactly one. The `knowledge__` prefix is the whole extent of what a user can say about grouping.

### Enumerated gaps to close

#### Gap 1: The engine routes by parsing a name

Model and width come from `split("__")[2]`. The `catalog_collections` row that holds `embedding_model` is written from the name and never read on a write or search path (consumer map [24454] §4 and §5). A registry that nothing reads is a comment. The fix: a `spaces` registry row is the only source of model and width, and the chunk row points at it by id.

#### Gap 2: Corpus routing is string prefix matching

`resolve_corpus` (`corpus.py:876-905`) does exact match then `startswith(f"{corpus}__")`. Scoping a search to "the papers about consensus" or "everything this repo owns" is expressed by guessing a prefix. The RDR-156 combined-query functions already prove the alternative: filter in the catalog, then rank vectors, in one statement. They exist for four query shapes and are not the default path for plain `search()` and `query()`. The fix: every query shape scopes through catalog predicates; `corpus=` is deleted.

#### Gap 3: No user organisation primitive

There is no way to put one document in two groups, to nest groups, or to name a group without also naming a model and an owner. The fix: sets, which are documents of content type `set`, tumbler-addressed, nestable, with membership as `member-of` rows on the existing links table.

#### Gap 4: The name is a second, lossy copy of catalog facts

`owner_segment_for_tumbler` renders `1.7.42` as `1-7` (`collection_name.py:31-50`); content type is both a catalog column and a name prefix; cross-machine identity was deferred at RDR-103 line 231 because changing the owner hash "would invalidate every existing repo's tumbler", a consequence that exists only because the owner is baked into a storage key. The fix: the name stops being a key anywhere. It survives for one release as a derived display alias and is then gone.

## Relationship to Prior RDRs

Searched the title table for: collection, catalog, corpus, naming, identity, tumbler, chunk, T3, organisation. Read RDR-049, 052, 053, 101, 103, 108, 137, 156, 164, 169, 180, 191, 193, 199 and the post-mortems for 101, 103, 156, 180, 191.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-049 (closed), RDR-053 (closed) | Origin of tumblers and links | Nelson's rule that addresses are independent of category, and RDR-053's decision that links live in the catalog, not in tumbler space, are the doctrine this RDR applies to the last violator. Sets reuse both unchanged. |
| RDR-052 (closed) | Precedent | RF-7 argued in 2026-04 that `subtree=` should replace corpus strings and that "which repo is this from" must never be read off a collection name. The routing half shipped; the name outlived it. This RDR finishes RF-7. |
| RDR-101 (closed) | Origin | Created the conformant name and `doc_id` identity. Its own scope table (line 617) deferred "embedding model selection per collection" and required only that "model + version are encoded in the collection name". That requirement is what this RDR retires; `doc_id` and the manifest are kept whole. |
| RDR-103 (closed) | Origin, superseded in part | Rationale, quoted: "Embedding-model identity becomes part of the collection's surface, matching the user-visible reality that switching embedding models necessarily creates a new collection (the vectors are not compatible)." True on ChromaDB. Expired: RDR-191's table holds three widths side by side and `catalog_collections` already carries the model. RDR-103's strict-naming flip (line 57, irreversible) and its "legacy names break loudly, no shim" rule (lines 134, 251) are superseded for the name and re-applied to spaces: a legacy name is a display alias for one release, then unresolvable. |
| RDR-108 (closed), RDR-180 (closed) | Precedent | Made chunk identity content-addressed and put document structure in the manifest, not in chunk metadata. This RDR is the same move one level up: document organisation in the catalog, not in a storage name. Chash identity and manifest structure (which chashes compose a document, in what order) are untouched; the manifest's `collection` column and its foreign key to the chunk row (catalog-029, an RDR-191 deliverable) are relabelled to the surviving row during the dedup, see Technical Design. |
| RDR-137 (closed) | Precedent | Made the catalog the source of truth for repo-to-collection mapping and deleted `repos.json`. Spaces are the same principle applied to the engine's routing table. |
| RDR-156 (closed) | Precedent | The four combined-query functions are the query shape this RDR makes universal. Their doc_id-vs-source_uri miss on the `knowledge__` family (architecture.md:436, nexus-bocft) must be closed as part of making them the only path. |
| RDR-164 (closed) | Precedent | Collection and document lifecycle cascades are Postgres-native. Space and set lifecycle rides the same cascades; nothing client-side is added. |
| RDR-169 (accepted) | Adjacent | Reference-only chunks add a retention enum on the chunk row. Orthogonal to which partition column the row carries; RDR-169 owns retention, this RDR owns partition and organisation. No sequencing constraint. |
| RDR-191 (closed) | Precedent and constraint | One `nexus.chunks` table, three typed embedding columns, exactly one non-null per row, all three widths required unconditionally (amendment vi, line 290). Line 509 forecloses one chunk row carrying two models. This RDR keeps every word of that: one row per (tenant, space, chash), one vector per row. Partitioning by width was proven impossible on the bundled pgvector; spaces are a plain column, not a partition. |
| RDR-193 (draft) | Adjacent draft | Moves index-time catalog reconciliation and taxonomy compute onto the engine. Scope boundary: RDR-193 owns where reconciliation runs; this RDR owns what the catalog's organisation model is. RDR-193's server-side diff must key documents by doc_id and manifest, never by collection name, which this RDR guarantees. Sequence: this RDR's release A lands first so RDR-193 does not build on the name. |
| RDR-199 (draft) | Adjacent draft | Gives an indexed corpus a nameable git revision. Scope boundary: RDR-199 owns source revision identity on the owner; this RDR owns spaces and sets. Independent; either may land first. |

## Context

### Background

Discovered 2026-09-04 during a catalog census (T2 [24436], [24450]) that tombstoned dead `code__1-7` and `rdr__1-67` collections and found that every organisational question ("which owner", "what type", "which model") was being answered by parsing a name that also served as a storage key. Sam's framing on 2026-09-05: the naming was necessary on ChromaDB and no longer is; flip to an inode model, where identity and metadata live in the catalog and storage is dumb; the corpus concept goes; document organisation becomes a first-class user feature.

An inode, for a reader outside Unix: the filesystem's record of a file's identity and metadata, separate from its names. A directory entry maps a name to an inode; one inode can have many names (hard links); renaming touches the directory, not the file. The research in [24451] found every mature document model (POSIX, git, Perkeep, IPLD, PCDM, JCR, CMIS) converging on the same three rules: storage is dumb and content-addressed, naming is a link row, type is a property. Nexus already follows all three for chunks. The collection name is the last place it does not.

### Technical Environment

Engine: Java, Postgres 17 with pgvector 0.8.2 bundled (the Postgres extension that stores vectors and builds HNSW approximate-nearest-neighbour indexes over them), schema changes only through Liquibase changelogs under `service/src/main/resources/db/changelog/`, queries only through jOOQ's generated Java DSL (no SQL strings, Sam 2026-09-03). Every table is tenant-scoped under Postgres row-level security applied to the service role as well (FORCE RLS). Client: Python 3.12, `src/nexus/`. Catalog tables in `catalog-001-baseline.xml`: `catalog_owners` (line 37), `catalog_documents` (62), `catalog_links` (127), `catalog_document_chunks` (171), `catalog_collections` (199, holding content_type, owner_id, embedding_model, model_version, with a tuple index at 221). Chunks in `vectors-004-unify-chunks.xml:262-276`: (tenant_id, collection, chash bytea, chunk_text, embedding_384, embedding_768, embedding_1024, chunk_tsv, metadata, created_at), primary key (tenant_id, collection, chash). Embedding models the engine knows (`PgVectorRepository.MODEL_DIMS`): voyage-code-3, voyage-context-3 and voyage-3 at 1024, bge-base-en-v15-768 at 768, minilm-l6-v2-384 at 384. Live in the cloud estate per the census [24466]: voyage-code-3, voyage-context-3, and one minilm-l6-v2-384 collection with one chunk. Cloud estate: one environment, 19465 live documents, 286707 chunks, 75 owners.

## Research Findings

### Investigation

Four parallel surveys on 2026-09-05, all read-only:

- Consumer map [24454]: every Python and Java site that parses the name, every corpus resolver, the schema, the engine's routing, the existing registry. About 35 Python consumers across `db/`, `catalog/`, `indexer.py`, `mcp/core.py`, `commands/`; Java routing in `EmbedderRouter`, `PgVectorRepository`, `CombinedWriteService`, `StagingPromoteOps`, `StagingHandler`.
- Settled-decision survey of RDR-101, 102, 103, 108, 156, 180, 191 with irreversibility quotes and a 23-item deferred list. Items answered by this RDR: 101:617 (model per collection), 101:618 (tenancy beyond owner), 103:231 (cross-machine owner identity unblocked, not done), 103 post-mortem (rename re-embed never shipped: spaces make rename a catalog write), 191:509 (one vector per row, preserved).
- Identity models [24451]: eight converging primitives, five already built in nexus.
- Index decoupling [24452]: how Elasticsearch, Vespa, Weaviate, Qdrant, Milvus, Lance, Turbopuffer and Postgres layouts separate logical names from physical indexes; two Postgres options, of which the wide table already shipped by RDR-191 is option 1.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| pgvector 0.8.2 (bundled) | Yes, via RDR-191's spike | An untyped `vector` column cannot carry an HNSW index; typed children cannot attach under an untyped parent. Spaces must be a column with partial indexes, never a partition key. |
| Liquibase (engine changelogs) | Yes, changelog files | `catalog_collections` and the five validated FKs to its name are the migration surface. |
| jOOQ codegen | Yes, `service/` build | Every new table gets generated DSL; no SQL strings. |

### Key Discoveries

- **Verified**: the engine never reads `catalog_collections.embedding_model` on a write or search path; it splits the name (consumer map §4). The registry exists and is inert.
- **Verified**: `catalog_collections` has no dimension column; width is only ever derived from the model name through hard-coded maps in Java and Python that must agree by hand.
- **Verified**: the three "all" expansions in `mcp/core.py` differ: the prefix logic at 3436-3440 is identical across the three sites, but the third appends the raw part (`target.append(part)`, 3441) where the other two pass it through `t3_collection_name(part, t3=t3)`. Three copies of one routing rule is the shape that drifts.
- **Verified**: RDR-156's `search_aspect_scoped` cannot see the `knowledge__` family because the catalog's `source_uri` comes from the title while the aspect extractor's comes from `source_path` (architecture.md:436, nexus-bocft). Making the catalog join universal forces this fix.
- **Verified** (2026-09-05, this tenant's live `collection_list`): the two name parsers already disagree on six live collections. `quarantine-docs__1-1__voyage-context-3__v1`, `quarantine-rdr__1-1__…`, `quarantine-rdr__1-20__…`, `quarantine-rdr__1-41__…` and the two `quarantine-code__` rows fail the client's conformant regex (`corpus.py:102`, whose content-type alternation has no `quarantine-*` entry), so `embedding_model_for_collection` (`corpus.py:573`) falls to prefix inference and reports `voyage-code-3` for names whose third segment says `voyage-context-3`; the engine's `split("__")[2]` reports `voyage-context-3`. Both are 1024 wide, so no vector is mis-sized today, but the same name yields two models depending on which side reads it. This is Gap 1 in the live estate, and it means the census for assumption 1 must group by the registry row, never by either parser.
- **Documented**: Nelson, Literary Machines, as quoted in RDR-049: tumblers are independent of subject and category; two system directories only, author and title; categorising is user business.
- **Documented**: CMIS multi-filing (one document in many folders), PCDM Object-to-File (one work, many representations), OAI-ORE proxies (order lives on the membership row). Sets adopt all three.
- **Assumed**: a partial HNSW index per (space, width column) performs the same as today's per-collection index, since today's collection is a space in all but name. To be measured in the release A battery, per the throughput-before-cut directive.

### Critical Assumptions

- [x] Every existing collection maps to exactly one space with no split or merge. **Status**: Verified with a correction, 2026-09-05 (census T2 `nexus/rdr-203-census-spike-results` [24466], conexus relay, bead nexus-fvxh1, read-only aggregates against the live cloud catalog). No collection carries more than one width (A1c: 0 rows, both tenants) and no chunk lacks a registry row (A1d: 0 orphans), so every collection maps to exactly one space. The correction: `catalog_collections.embedding_model` and `model_version` are blank on 201 of 265 registry rows (42 of 44 in gate-xr789, 159 of 221 in nexus), so a seed grouped by the registry columns as written would collapse every blank row into one pseudo-space per tenant across models. A1b found zero cases of a populated registry model disagreeing with the name; the registry is unpopulated, never wrong. **Consequence**: the release A seed derives the space key from the name's model segment cross-checked against the chunk width, backfills the registry columns in the same changeset, and fails the walk on any row where the two disagree. **Method as run**: Spike, a census query over `catalog_collections` grouped by (tenant, embedding_model, model_version) before the seed changeset is written. Precedent for a cloud-readable census with no substrate access: the `chash_conformance_report(dim)` SQL function behind an engine route (`rdr180-021-chash-conformance-report.xml`, `CatalogRepository.java`), tenant-scoped under FORCE RLS; the local counterpart is the `nexus_diag` aggregate-only probe (`db/diag_connection.py`). Draft queries: scratchpad `rdr-203-census-spike.sql` (session nexus-b6).
- [x] The chunk primary key can move from (tenant, collection, chash) to (tenant, space_id, chash) with identical uniqueness, that is, no two collections that become one space share a chash. **Status**: Falsified 2026-09-05 (same census, A2, keyed on the name's model segment as the backfilled registry would be). 3,369 of 373,881 chunks (0.9%) would be rejected by the new key: gate-xr789 voyage-context-3 1,749 colliding chashes over 1,798 rows, voyage-code-3 93 over 188; nexus voyage-context-3 618 over 835, voyage-code-3 324 over 548; minilm none. A collision is byte-identical chunk text under two names, so the dedup loses only which collection label survives, and the manifest rows keep every document whole. **Consequence**: the release A chunk relabel changeset dedups before adding the key: for each colliding (tenant, space, chash) group it elects one surviving `nexus.chunks` row, first UPDATEs every referrer of the losing rows' (tenant_id, collection, chash) key to the survivor's key (`catalog_document_chunks` via `fk_catalog_chunks_chunk`, NO ACTION and DEFERRABLE INITIALLY IMMEDIATE, so a delete before the repoint aborts the walk; `topic_assignments` via `topic_assignments_chunk_fk`, ON DELETE CASCADE, so a delete before the repoint silently destroys topic rows), then deletes the losers, then asserts the post-dedup chunk count equals the pre-dedup distinct count and that no manifest or topic row was lost (row counts before and after are equal). The migration never fails on a collision; it fails only if the repoint left a dangling reference. **Method as run**: the same census, counting chash collisions across collections that collapse.
- [ ] Partial HNSW indexes keyed on space_id give recall and latency equal to today's per-collection indexes. **Status**: Unverified. **Method**: Spike, timed A/B on the shakedown corpus in the release A battery.
- [x] No client outside this repository reads collection names off the wire. **Status**: Falsified 2026-09-05 (session nexus-6f). **Method**: Source search of the conexus checkout and both plugin surfaces (`conexus/`, `sn/`) for `__` splitting. The plugin surfaces are clean (the one hit, `subagent-stop-writes-scan.py:69`, splits MCP tool names, not collection names). conexus has three readers of the wire name: `deploy/parity/capture_oracle.py:79-95` derives the embedding model from segment 2, `deploy/tests/test_auy1_query_set.py:113` takes the corpus from segment 0, and `deploy/tests/test_doc_count_deadlock_probe.py:33` asserts the four-segment shape. All three are conexus's gate machinery, not an end-user client, but they break at release B. **Consequence**: release A's response must carry `space_id` and `embedding_model` as fields so conexus can stop deriving them from the name before B; a relay to conexus naming those three sites is part of release A's client work.

## Proposed Solution

### Approach

Separate the three jobs the name does.

**Space.** A registry row: `spaces(tenant_id, space_id, embedding_model, dimension, schema_version, created_at)`, opaque id, unique on (tenant, model, version). It is the physical partition and nothing else. The engine reads embedder and width from it. Chunk rows carry `space_id`; the primary key becomes (tenant_id, space_id, chash). Every existing `catalog_collections` tuple becomes one space at migration; chunks are relabelled, never re-embedded. A space never carries owner, content type, or content version. Users never see or name one. A catalog model policy keyed on content type (code to voyage-code-3, prose and papers to voyage-context-3, local mode to bge) picks the space; results show the model as provenance.

**Set.** A document of content type `set`, registered under its owning curator, tumbler-addressed like any document, so it can be linked, cited, superseded, tombstoned and audited by existing machinery. Membership is a `member-of` row on `catalog_links` with an optional integer position. One document in many sets; a set in a set. Nesting is a directed acyclic graph enforced at the links route: a `member-of` write whose target is reachable from its source through existing `member-of` rows is rejected (the same walk `search_graph_hop`'s recursive CTE already does), and `set=` resolution walks `member-of` to a fixed depth of 8 and fails loud past it rather than looping. `--set` on `nx store put` and `nx index pdf`; `set=` on search alongside `subtree=` and `content_type=`, combinable. Sets carry no model policy.

**Scope.** Every query shape resolves its scope in the catalog first: owner subtree, content type, set membership, links, topics, aspects. The result is a doc_id set, joined through the manifest to chash, ranked in the space the query's model requires, in one statement, which is the RDR-156 shape made universal. `corpus=` and the prefix expansions are deleted. Owner remains the tumbler's second segment and says where; a set says what it is about; content type says what it is.

### Technical Design

Engine, release A (additive):

- Liquibase changesets, in this order, each with a count assertion: create `spaces`; backfill `catalog_collections.embedding_model` and `model_version` from the name's model segment where blank (201 of 265 live rows, census [24466]), asserting the width of that collection's chunks matches the model's dimension; seed one row per distinct (tenant, embedding_model, model_version); dedup chunk rows that share (tenant, prospective space, chash) across collapsing collections (3,369 live rows) by repointing `catalog_document_chunks` and `topic_assignments` to the survivor before deleting the losers (Critical Assumption 2); add nullable `space_id` to `nexus.chunks` and to each name-keyed table, populate by join through `catalog_collections`, then set NOT NULL and add the FK; add `space_id` to `catalog_collections` so the old name row points at its space. The name column stays, valid, for the whole release.
- `EmbedderRouter` and `PgVectorRepository` resolve model and width from the `spaces` row for the collection named in the request; the split-on-`__` path is deleted, not kept as a fallback. Fail loud on a name with no space.
- `/v1/vectors` accepts either a collection name (resolved to a space through `catalog_collections`) or a `space_id`; the response carries `space_id`.
- New routes: `POST /v1/catalog/sets`, `member-of` accepted as a link type on the existing links route with a `position` attribute, `GET /v1/catalog/sets/{tumbler}/members`.
- Combined-query functions take a scope struct (owner prefix, content types, set tumblers, model) instead of a collection list; the four existing functions become one function with optional predicates, keeping their partial-HNSW plan shape.

Client, release A:

- `search()` and `query()` MCP tools and `nx search` dispatch every call through the scoped combined query. `corpus=` is accepted, resolved through the catalog to a scope, and warns once per process that it is gone next release.
- `Catalog.space_for(content_type)` replaces `collection_for` as the thing indexers ask; it returns a space and the derived alias name for display.
- `nx catalog set create|add|remove|list`, `--set` on `store put` and `index pdf`.
- `nx catalog convert-collections-to-sets`, one shot, idempotent: every `knowledge__<name>` collection becomes a set named `<name>` under its curator with every document a member; `code__`, `docs__`, `rdr__` collections become nothing.
- Doctor and `store_list` print the alias name and the space id.

Release B (deletion): drop the name column and its FKs, `catalog_collections` in its current form, `corpus=`, `--collection`, the three "all" expansions, `collection_name.py`, `parse_conformant_collection_name`, the Java and Python model-to-width maps. A legacy name anywhere fails loud.

```text
// Illustrative — verify signatures during implementation
Scope := { tenant, owner_prefix?: tumbler, content_types?: [str],
           sets?: [tumbler], model: str, limit }
search_scoped(scope, query_vector) -> [ {tumbler, chash, distance, space_id} ]
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `spaces` registry | `catalog_collections` (catalog-001-baseline.xml:199) | Extend then replace: seed spaces from it in A, delete it in B. |
| Engine routing by space | `EmbedderRouter.java:302-367`, `PgVectorRepository.java:409-426`, `MODEL_DIMS` | Replace: registry lookup, delete the parsers and maps. |
| Scoped search | RDR-156 functions `search_metadata_scoped`, `search_aspect_scoped`, `search_graph_hop`, `search_topic_scoped` | Extend into one function with optional predicates. |
| Set membership | `catalog_links` (catalog-001-baseline.xml:127) | Reuse: new link type `member-of`, add `position`. |
| Set entity | `catalog_documents` | Reuse: content type `set`. |
| Corpus resolution | `corpus.py:876-905`, `mcp/core.py` three expansions | Replace in A (alias), delete in B. |
| Name rendering | `catalog/collection_name.py` | Keep as display-alias renderer in A, delete in B. |
| Model policy | `canonical_embedding_model()` (RDR-103) | Extend into a content-type keyed policy table in the catalog. |

### Decision Rationale

The name cannot be fixed in place because it is three things. Renaming it better, or parsing it more carefully, keeps every consumer coupled to a string that must agree with a registry by hand. Putting the physical fact in a registry the engine actually reads is the only change that lets the two catalog facts (owner, type) become ordinary columns and lets organisation become a user feature. Sets as documents, rather than a new table, cost nothing new in audit, tombstone, link, or tumbler machinery and follow Nelson's rule that links point at anything. Two releases, additive first, because the estate has one environment and the change moves a primary key on 286k rows and fifteen foreign keys.

## Alternatives Considered

### Alternative 1: Facade only

**Description**: Retire `corpus=` in favour of catalog predicates and the RDR-156 join; leave names as physical partitions and display labels.

**Pros**: no schema change, no engine change, closes the routing gap.

**Cons**: the engine still routes by parsing a string; the registry stays inert; users still get no organisation primitive; the owner is still baked into a storage key, so cross-machine identity stays blocked.

**Reason for rejection**: it is phase one of the chosen approach, not an alternative to it. Kept as the first client deliverable of release A.

### Alternative 2: Normalise vectors into a child table

**Description**: `chunk_embeddings(chash, embedding_model, dim, vector)`, one row per (chunk, model), the Vespa and Lance shape; adding a model becomes an insert.

**Pros**: cleanest multi-representation story.

**Cons**: a join on every query; a migration of every embedding column; contradicts RDR-191's just-paid wide-table decision for a model set that is a fixed enum of four.

**Reason for rejection**: deferred until the model set stops being fixed. Spaces make that move a later additive change rather than foreclosing it.

### Briefly Rejected

- **Folder tree instead of sets**: reintroduces the fixed hierarchy the name encoding degenerated into; the tumbler already provides the one hierarchy that means something (where).
- **Sets in their own table**: duplicates identity, audit, and tombstone machinery the document table already has.
- **User-selectable embedding space on search**: a knob whose wrong setting silently returns nothing; provenance in results carries the same information without the failure mode.
- **One-release hard cut**: fifteen FK moves and an engine re-route in one deploy against a single live environment, no staging target.

## Trade-offs

### Consequences

- Positive: the engine's routing table becomes data the engine reads; adding a model is a row, not a code change in two languages.
- Positive: users get sets with multi-filing and nesting; every search scope is a catalog predicate.
- Positive: RDR-103's cross-machine owner identity becomes a catalog-only change.
- Negative: two releases of dual-keyed tables; every name-keyed test fixture is rewritten.
- Negative: `corpus=` callers in skills, plans, and the plan library break at release B; the plan library needs a scope migration.
- Negative: for one release, doctor output shows both a name and a space id, which is more to read.

### Risks and Mitigations

- **Risk**: two collections collapse into one space and share a chash, breaking the new primary key.
  **Mitigation**: measured (census [24466]): 3,369 rows, 0.9% of the estate. A collision is identical chunk text under two names and collapses to one row by RDR-108's own rule, so the relabel changeset dedups with a count assertion, not a failure. The manifest already keys on chash and needs no rewrite.
- **Risk**: partial HNSW on space_id is slower than the per-collection index it replaces.
  **Mitigation**: timed A/B on the shakedown corpus in the release A battery; a regression blocks the cut (throughput-before-cut directive).
- **Risk**: an external client parses names off the wire.
  **Mitigation**: source search of conexus and plugin surfaces before release A; the alias stays valid through A.
- **Risk**: the Liquibase walk on the cloud replays every pending changeset and is sized by the cluster's state, not this change.
  **Mitigation**: PITR-fork rehearsal (`deploy/RESTORE.md`) is mandatory for release A's engine tag, as for any tag carrying a changeset.

### Failure Modes

- A request names a collection with no space row: engine returns 422 naming the collection. Diagnosis: `nx doctor` lists collections without a space.
- A `member-of` link to a tumbler that is not a set: rejected at the links route, same as a link to a missing document today.
- A `member-of` link that would close a cycle: rejected at the links route naming the path.
- A write to a collection minted during the release A window before the client is converted: the engine mints the space from the model policy at first write (`Catalog.space_for`), registers the `catalog_collections` row pointing at it, and the census of collections without a space stays zero by construction; `nx doctor` reports any row that slipped through.
- A `corpus=` value in release A that resolves to no scope: warn and fail loud, never return an empty result silently.
- A search with a model whose space does not exist for the tenant: fail loud with the model named; never fall back to another width.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions verified or falsified with consequences recorded (assumption 3, the HNSW A/B, is verified inside the release A battery by design)
- [x] Census spike results recorded in T2 `nexus/rdr-203-census-spike-results` [24466] (nexus-fvxh1)
- [ ] PITR-fork rehearsal scheduled with conexus for the release A engine tag

### Minimum Viable Validation

On the release A engine against a copy of the shakedown corpus: index one repository and one set of PDFs through the new path, then run one search scoped by `set=` and one scoped by `subtree=` plus `content_type=`, both dispatched through the single scoped function, with no collection name present anywhere in the request or the SQL plan, and results equal to the same searches on the pre-flip engine.

### Phase 1: Release A, engine

#### Step 1: Census spike

Query `catalog_collections` and `nexus.chunks` for the two collapse assumptions; record counts in T2.

#### Step 2: Spaces changesets

Create, seed, add `space_id` everywhere, populate, NOT NULL, FK, new primary key on chunks. jOOQ regenerate.

#### Step 3: Routing

`EmbedderRouter` and `PgVectorRepository` read the space row; delete the parsers and maps; 422 on a name with no space.

#### Step 4: Sets and scoped search routes

Sets route, `member-of` with position, one scoped combined-query function; close the nexus-bocft doc_id-vs-source_uri miss so the `knowledge__` family joins.

#### Step 5: Battery

Full engine suite, timed A/B on the shakedown corpus, PITR-fork walk rehearsal, image smoke, then the tag.

### Phase 2: Release A, client

#### Step 6: Scoped dispatch

All search shapes through the scoped function; `corpus=` alias with a once-per-process warning.

#### Step 7: Sets CLI and MCP

`nx catalog set …`, `--set` on store and index, `set=` on the MCP tools.

#### Step 8: Conversion command

`nx catalog convert-collections-to-sets`, idempotent, run at upgrade.

#### Step 9: Floor bump and paired release

`REQUIRED_ENGINE_VERSION` to the release A engine; every wire-ledger entry `[additive]`; deploy the engine before the client tag.

### Phase 3: Release B, deletion

#### Step 10: Delete the name

Name columns, FKs, `catalog_collections`, `corpus=`, `--collection`, the expansions, the parsers, the maps. Legacy names fail loud. Plan-library scope migration.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Space | `nx doctor` and `nx collection list` show spaces | In scope | Deferred: a space with zero chunks is inert; deletion rides RDR-164 cascades in B | `nx doctor` flags chunks with no space | Existing PG backup |
| Set | `nx catalog set list` | `nx catalog show <tumbler>` | `nx catalog delete` (tombstone, RDR-053) | `nx catalog link-audit` covers `member-of` | Existing PG backup |
| Model policy | `nx doctor` prints it | In scope | N/A (a table with one row per content type) | Doctor | Existing PG backup |

### New Dependencies

None.

## Test Plan

- **Scenario**: two collections with the same (tenant, model, version) seed one space. **Verify**: one `spaces` row, both name rows point at it, every chunk has that `space_id`.
- **Scenario**: a write names a collection with no space. **Verify**: 422 naming the collection; nothing written.
- **Scenario**: a document is added to two sets and one set is nested in another. **Verify**: `set=` on the outer set returns it once; link-audit is clean.
- **Scenario**: `corpus="code"` in release A. **Verify**: resolves to content_type=code across all owners, warns once, results equal the pre-flip search.
- **Scenario**: legacy name in release B. **Verify**: fails loud with the name; no silent empty result.
- **Scenario**: timed A/B of scoped search on the shakedown corpus, pre-flip versus release A. **Verify**: p95 latency and recall within the committed throughput baseline.
- **Scenario**: `knowledge__dt-papers` conversion. **Verify**: set `dt-papers` under owner 1.14 with eight members; rerun is a no-op.
- **Scenario**: PITR-fork Liquibase walk for release A. **Verify**: changeset count as predicted; grants intact.

## Validation

### Testing Strategy

1. **Scenario**: engine unit and integration suites with the name parsers deleted. **Expected**: green with zero name-splitting sites remaining (a lint test asserts none).
2. **Scenario**: release battery per AGENTS.md § Cutting a release, plus the A/B timing. **Expected**: all gates green, the A/B within baseline.
3. **Scenario**: fresh-install MVV on release A. **Expected**: init, index, store put with `--set`, search by set, doctor clean.

### Performance Expectations

Measured, not estimated: the A/B on the shakedown corpus is the number. The expectation from RDR-191's shape is parity, because today's collection is already a space in everything but name.

## Finalization Gate

### Contradiction Check

Gated 2026-09-05 (two rounds, substantive-critic, T2 [24476] and [24478]). Round 1 found one contradiction with RDR-191: the dedup plan said the manifest needed no repoint, but `catalog-029` (an RDR-191 deliverable) makes `catalog_document_chunks` reference the chunk key, and `taxonomy-012` makes `topic_assignments` cascade on it. Resolved by the repoint-then-delete order in Technical Design. No contradiction with RDR-103 (superseded in part, stated), RDR-108 (chash and manifest structure untouched), or RDR-156 (its join shape becomes universal).

### Assumption Verification

Gated 2026-09-05. Assumptions 1, 2 and 4 are settled by the census [24466] and the source search, each with its consequence written into the design. Assumption 3 (partial HNSW parity) stays open by design and is measured in the release A battery, where a regression blocks the cut.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Partial HNSW index on `space_id` predicate | pgvector 0.8.2 | Spike |
| `ADD CONSTRAINT … PRIMARY KEY USING INDEX` on 286k rows | Postgres 17 | Spike on the PITR fork |

### Scope Verification

The MVV runs in release A's battery, not deferred.

### Cross-Cutting Concerns

- **Versioning**: paired release, floor bump in the same client release, all-additive wire ledger for A, engine deployed before the client tag; B is a non-additive break announced in A's warning.
- **Build tool compatibility**: jOOQ regeneration for every new table.
- **Licensing**: N/A
- **Deployment model**: one live environment; PITR-fork rehearsal mandatory for A's tag.
- **IDE compatibility**: N/A
- **Incremental adoption**: release A keeps every name valid; conversion command is idempotent.
- **Secret/credential lifecycle**: N/A
- **Memory management**: N/A

### Proportionality

Judged at gate 2026-09-05. The census confirmed every collection is already one space in all but name (no width splits, no orphans), so the change is a relabel plus a registry the engine reads, not a re-embed; the 0.9% dedup and the nine FK moves are the whole cost of the PK move. Two releases against one live environment is the minimum sequencing for a primary-key change, not ceremony. The Alternatives section stands as written.

## References

- T2 `nexus/design-catalog-inode-flip-spaces-and-sets-2026-09-05` [24459], the locked design of record
- T2 [24451] research-document-identity-models-2026-09-05
- T2 [24452] research-index-collection-decoupling-2026-09-05
- T2 [24454] collection-name-consumer-map-2026-09-05
- T2 [24436], [24450] catalog censuses of 2026-09-04 and 2026-09-05
- Nelson, T.H., Literary Machines (1981/1987), ch. 4, as quoted in RDR-049 and RDR-053
- `docs/exploration/xanadu-in-nexus.md`
- `service/src/main/resources/db/changelog/catalog-001-baseline.xml`, `vectors-004-unify-chunks.xml`
- `src/nexus/corpus.py`, `src/nexus/catalog/collection_name.py`, `src/nexus/mcp/core.py`
- `service/.../EmbedderRouter.java`, `PgVectorRepository.java`

## Revision History

- 2026-09-05: created from the brainstorming gate session with Sam.
- 2026-09-05: assumptions 1, 2 and 4 settled (census [24466], conexus source search); gate round 1 BLOCKED on the manifest-FK dedup contradiction, fixed; round 2 PASSED ([24476], [24478]).
