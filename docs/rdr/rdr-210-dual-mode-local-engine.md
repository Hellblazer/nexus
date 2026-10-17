---
title: "One Local Engine Serves Both Embedders: bge-768 and Voyage Collections Side by Side"
id: RDR-210
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-09-15
accepted_date:
related_issues: [nexus-ddmfg, nexus-umm29, nexus-r5f3c, nexus-35ok4]
related_rdrs: [RDR-160, RDR-204, RDR-157, RDR-188, RDR-109, RDR-223, RDR-225, RDR-192, RDR-227, RDR-224, RDR-228]
---

# RDR-210: One Local Engine Serves Both Embedders: bge-768 and Voyage Collections Side by Side

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Sam, 2026-09-15: "we allow voyager embedding in local mode. this
is correct. this will not change", then "local can use both and we require that
now". An investigation the same day (T2 `nexus/voyage-local-mode-investigation-2026-09-15`
[25872], read at origin/develop a9551ee21) found that local mode can use Voyage,
but only as a switch for the whole engine, and that one engine cannot serve both
kinds of collection. On 2026-10-17 Sam decided to build this RDR, reversing a
same-day choice of Alternative 1, and answered the draft's six open questions
(T2 `nexus_rdr/210-open-questions-sam-2026-10-17`). Every code citation below was
re-read at develop e34467fdb (drift audit: T2 `nexus_rdr/210-drift-audit-2026-10-17`).

Terms used throughout:

- **Engine**: the Java `nexus-service` process. In local mode the nx client's
  supervisor spawns it on the user's machine against a bundled Postgres.
- **Embedder**: the component that turns text into a vector. Local mode ships
  bge-768 (the bge-base-en-v1.5 model, 768 dimensions, run on-device through
  ONNX runtime). Voyage is a paid remote API; `voyage-code-3` embeds code and
  `voyage-context-3` embeds prose.
- **Family**: bge-768 is one family, the Voyage models are the other.
- **Posture**: which embedders one running engine holds. Today there are two,
  chosen at boot: *keyless* (bge-768 only) and *keyed* (Voyage only). The key is
  the `NX_VOYAGE_API_KEY` environment variable in the engine's process.
- **Mode string**: the one word the engine reports for its posture on
  `GET /version` and `GET /v1/status` (`embedding_mode`): `onnx-local` or `voyage`
  today.
- **Embedding profile**: the engine-written table (RDR-204) that names the model
  each content type's NEW collections get. An existing collection keeps the
  model recorded in its own catalog row.
- **Sibling**: two collections that differ only in the model segment of their
  name, for example `code__1-1__bge-base-en-v15-768__v1` and
  `code__1-1__voyage-code-3__v1`.
- **chash**: the SHA-256 of a chunk's text, which is the chunk's identity in its
  collection.
- **Manifest**: the catalog table `catalog_document_chunks`, one row per
  (document, position), naming the collection and chash of that position's chunk.
- **live(c)**: RDR-192's visibility rule. A chunk is visible to reads only when a
  manifest row in the chunk's OWN collection names it and that row's document is
  not deleted. A chunk with no such row is *ownerless*.
- **Ownership guard**: RDR-223's check that every chunk a write route stores
  already has a live manifest row in the target collection. Local engines run it
  in enforce mode.
- **Reaper**: the engine's periodic job (RDR-192) that moves aged ownerless chunks
  into a quarantine collection and later expires them.

## Problem Statement

A local user who has both bge-768 collections and Voyage collections can have
only one kind searchable at a time. Restarting the engine with a key makes every
bge-768 collection fail with HTTP 422, and the same restart without a key does
the same to every Voyage collection. By default the key never reaches the engine
at all, and re-running `nx init` quietly undoes a Voyage opt-in. Several client
decisions, including one that deletes data, read the engine's single mode string
and would misfire on an engine that holds both. Nothing moves an existing
collection onto a new model without re-indexing it from source.

### Enumerated gaps to close

#### Gap 1: One engine holds one embedder family per boot

`Main.java:238` reads the key once. When it is set, `Main.java:251-273` builds
Voyage-only routers (`:258-259`) and the Voyage reranker (`:267-271`); otherwise
`Main.java:274-331` builds bge-only routers (`:286`, `:310-315`) and the
cross-encoder reranker (`:323-324`). The Voyage router's dispatch table has no bge
entry (`EmbedderRouter.java:149-159`) and the keyless table has no Voyage entry
(`EmbedderRouter.java:87-101`). `resolveEmbedderStrict`
(`EmbedderRouter.java:543-559`) looks up the collection's catalog row by its
`embedding_model`; a miss throws `EmbeddingModelUnavailableException`, which the
vector handler maps to 422 (`VectorHandler.java:256`). So embed, write and search
against the other family's collections are all refused. In a multi-collection
search the client no longer hides this: since nexus-vply6 (e1614fda3,
2026-09-24) any model-unavailable collection raises
`SearchEmbeddingProfileMismatchError` even when every other collection searched
fine (`search_engine.py:1948-1981`). The search is loud, but it still cannot
return the other family's hits.

#### Gap 2: The key does not reach the engine by default, and init reverts the opt-in

The supervisor (`storage_service_daemon.py:1544-1590`) passes a configured key
only when `local.embed_model` names a Voyage model or records no choice at all
(the guard is `:1565`, the credential lookup `:1573`). That guard (nexus-r5f3c)
exists because of Gap 1: a key flipped the engine Voyage-only and broke the
user's bge collections. On develop, `nx init` writes `local.embed_model = bge`
on every local provision (`init.py:96`, inside `_provision_service_embedder_step`
at `init.py:54`, called from `provision_service_stack` at `init.py:765`), and so
does the upgrade ladder's provision leg (`upgrade_ladder/provisioning.py:293,
381-384`), which reaches the same function. A user who opted into Voyage and
later re-runs `nx init` loses the opt-in at the next service restart.

The init half is fixed separately and lands ahead of this RDR: branch
`feature/nexus-ddmfg-init-keeps-voyage` (nexus-ddmfg, c03352a2a) keeps a Voyage
value and writes bge only otherwise, which also covers the ladder. It is not
scope here. The supervisor half (the r5f3c guard) is.

#### Gap 3: The profile has no input for choosing between two available models

The engine seeds the embedding profile from its posture on every boot
(`Main.java:333-350`, calling `seedEmbeddingProfile` at
`EmbedderRouter.java:264-272`, which reads `contentTypeModelTokens` at
`EmbedderRouter.java:227-238` and upserts with ON CONFLICT DO UPDATE at
`:356-378`). A keyed boot names the Voyage models, a keyless boot names bge-768.
Once one engine holds both, "which model do new collections get" becomes a choice
the posture no longer answers, and nothing carries the user's choice to the
engine: the engine reads no profile variable (the engine's `NX_*` reads, by grep
of `getenv("NX_` in `service/src/main/java`, include none for the profile).

#### Gap 4: The single-name collection resolver picks one sibling

`t3_collection_name` (`corpus.py:1213`) returns one name. Once a Voyage sibling
exists it returns that sibling (`corpus.py:1406-1410`), and it probes for the bge
sibling only when the promoted name is missing (`corpus.py:1484-1500`). This is
pinned by `tests/test_rdr_109_phase2_dispatch.py:258`
(`test_read_path_prefers_voyage_over_bge_once_voyage_sibling_exists`).

This gap is narrower than the draft said. A bare content-type corpus
(`--corpus code`) already fans out to every live collection of that content type
(`resolve_corpus` stage 2, `corpus.py:2428-2451`), the search engine groups the
result by model (`search_engine.py:595-625`), and each group goes to the engine
as its own request. What still reads one sibling is every path that goes through
the single-name resolver: an owner-scoped corpus, `--collection`, and the store
and get paths.

#### Gap 5: The engine's outbound calls have no guard, and one of them is not Voyage

RDR-157 and RDR-160 state that the local engine makes no outbound HTTP. Sam
decided on 2026-06-15 (nexus-umm29) that the Voyage embed and rerank call is a
sanctioned exception, bounded by a guard or test so a later change cannot add
other outbound calls under it. The wording landed in f7cae33ec (2026-09-15:
RDR-157, RDR-160, RDR-204, the privacy policy). The guard does not exist, and a
guard written as drafted ("outbound HTTP only in the Voyage classes") would fail
on the day it lands. A grep of `service/src/main/java` for
`HttpClient.newBuilder`, `HttpClient.newHttpClient`, `openConnection(` and
`new Socket(` finds four construction sites: `VoyageEmbedder.java:309`,
`CceEmbedder.java:359`, `VoyageReranker.java:152` and
`HttpsSchemeHandler.java:46`. The fourth fetches any `https://` URI on request
behind `POST /v1/vectors/resolve` and is registered unconditionally
(`NexusService.java:648-649`). `EgressProxy.java` builds no client; it supplies a
proxy selector to the three Voyage classes (`EgressProxy.java:7-35`). (T2
210-research-6. The grep above is this revision's; the full egress census,
covering forms it does not match, is pending 210-research-egress-census.)

#### Gap 6: No test or gate runs a keyed local engine, let alone a dual one

The local-service gate runs keyless and deselects `cloud_mode` tests; its header
records the coverage gap in its own words (`tests/e2e/local-service-gate.sh:61-71`,
"no gate in this repo boots the local service keyed"). The release battery has
three legs, preflight aside: fresh-install, package-upgrade and the local-service
gate (`tests/e2e/release-battery.sh:163-167`). `tests/e2e/rdr195-voyage-mvv.sh`,
the one hand-run keyed script, was deleted in bf54a64c6 (2026-10-03).
`Rdr204Gh1461ProfileRestartJourneyTest` holds both kinds of collection in one
database, but its header (`:44-60`) says it reads the bge one through a separate
bge router and writes the Voyage one through a fake; the production Voyage router
would refuse that read.

#### Gap 7: Nothing moves an existing collection onto the new default

Once a key arrives, every collection the install already holds stays bge-768.
`nx collection re-embed NAME --to MODEL` (`commands/collection.py:1659`) rewrites
vectors in place, 100 chunks per request (`:1444`), and in service mode refuses a
`--to` that differs from the model the name encodes (`:1711-1736`); its `--to`
choices are the three Voyage models only (`:1436`). It cannot move a bge
collection to Voyage or back. The old cross-model path,
`embed_migrate.migrate_collection_safe`, was deleted with RDR-223's dead
chunk-write paths (3ccf25a48, 2026-09-30). What remains is a re-index from source
(`nx index repo`, `nx index pdf`), which cannot move a chunk that has no source
file (MCP-stored notes, DEVONthink content), runs as a client loop that dies with
the terminal, keeps no cursor, and reports nothing while it runs.

#### Gap 8: Client decisions key off one engine-wide mode string

The engine's mode string is `onnx-local` when the router has no Voyage embedder
and `voyage` otherwise (`EmbedderRouter.java:162-169`), reported on `/version`
(`VersionHandler.java:478-490`) and `/v1/status` (`StatusHandler.java:338-347`).
A `git grep` of the non-test, non-doc tree for `"onnx-local"`, `== "voyage"` and
`embedding_mode` finds these client branches on it:

- `http_vector_client.py:1061`: the per-request chunk cap is the ONNX memory cap
  for every collection when the mode is `onnx-local`.
- `http_vector_client.py:1175`: the Voyage byte budget is off when the mode is
  `onnx-local`.
- `http_vector_client.py:6049`: the probe memo accepts only the two known values.
- `search_engine.py:580`: Voyage-calibrated similarity thresholds apply only when
  the mode is `voyage`.
- `commands/collection.py:601-650`: `_active_embedding_dim` turns the mode into
  ONE active dimension (1024 for `voyage`, the local model's for `onnx-local`).
  `_find_dimension_mismatched_collections` (`:664`) lists every collection whose
  name declares another dimension, and `nx collection prune --yes` (`:722`)
  deletes them. `nx doctor` reuses the same finder (`health.py:8224-8244`).
- `http_engine_status.py:154`: a display branch.

The rest are display or log only (`errors.py:252-265`, `commands/daemon.py:1778`,
`commands/init.py:872`, `commands/service_cmd.py:57`,
`storage_service_daemon.py:1582`). On an engine that holds both families, any
single value is wrong for half the collections (T2 210-research-7; the full
census of mode-string consumers is pending 210-research-ca3-census, and the list
above is this revision's own grep until it lands). The pruner is the dangerous one:
under `voyage` it would list every served bge collection for deletion. It is
already wrong today in one case: a keyless engine lists every Voyage collection
as a dimension orphan, although adding the key would serve it.

#### Gap 9: The client's profile check refuses writes to an existing off-profile collection

Before registering, `ensure_collection_registered` compares the name-derived
model with the profile's model for the content type and raises
`EmbeddingProfileMismatchError` on any difference (`corpus.py:2116-2120`). It does
not ask whether the row already exists. The engine already makes that
distinction: `CatalogRepository.upsertCollection` reads the existing row first
(`:7896`) and, for an existing row, refuses only a change of model (`:7906-7930`);
the profile applies to new rows alone (`:7944-7960`). Under a dual engine whose
profile says Voyage, a write to an existing, served bge collection (a re-index of
a file, a `store_put` into an existing note collection) would be refused by the
client although the engine would accept it.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-160 | Origin | Decision 2: "`EmbedderRouter` local mode routes all collections to the bge-768 embedder". Its rationale was a greenfield default for users without a key, and the no-outbound-HTTP invariant (Decision 4). The default still holds; routing *all* collections to bge does not, since Sam requires Voyage in local mode. This RDR revises Decision 2 only; bge-768 stays the keyless default and the parity gate is untouched. |
| RDR-204 | Origin | §1a says reads of a collection whose model differs from the profile "route by the row's own model and are never refused". The code refuses them whenever the running posture lacks that model; the 2026-09-15 amendment (rdr-204:406) records that. A dual engine makes the sentence true for bge-768 and Voyage. |
| RDR-157 | Origin | States the local-engine topology invariant. The nexus-umm29 carve-out amends it (landed f7cae33ec); this RDR adds the guard that keeps the carve-out bounded (Gap 5). |
| RDR-188 | Precedent | Moved all Voyage traffic, embed and rerank, into the engine and made the key engine-bootstrap material only. This RDR keeps that: the client still never calls Voyage. |
| RDR-109 | Origin | Honest local-mode naming introduced the single-name collection resolution that Gap 4 changes. |
| RDR-223 | Precedent and constraint | Its ownership guard, enforced on local engines (`storage_service_daemon.py:1489`), refuses any chunk write whose chash has no live manifest row in the target. It deleted staging. Its combined write (`CombinedWriteService`, `CatalogRepository.writeManifestMany`) writes a document's chunks and manifest rows in one transaction; the re-embed (Phase 6) moves documents through that primitive and needs no exception to the guard. |
| RDR-225 | Constraint | Chunks are partitioned by model, and every seeded model has a partition (vectors-030 `:703-713`). The manifest-to-chunk foreign key includes the model (vectors-030 `:1017-1021`). It states (rdr-225:554): "Cross-model migration registers a new collection under the target model. There is no in-place re-embed, and none is added." The re-embed here copies into a new sibling and so agrees. |
| RDR-192 | Constraint | live(c) decides when moved chunks become visible, and the reaper's grace and floor decide what happens to the source's chunks after the move (Technical Design, re-embed section). |
| RDR-227 | Adjacent (closed) | Its reconciler builds a per-collection index for any live, unsuperseded collection above its row threshold and drops it when `superseded_by` is set (rdr-227:366-370, :418-428). A re-embed target therefore gets its index while the job runs, and the source's index goes at supersede. No change needed. |
| RDR-224 | Adjacent (closed) | Native Windows runs bge on the Windows ONNX runtime (rdr-224:289, :322), with the ORT init gate and temp sweep at boot (`Main.java:67`, `:71`). The dual boot must hold on Windows; the coexistence spike (Phase 1 Step 1) includes the win-x64 image. |
| RDR-228 | Adjacent (closed) | Plugin setup runs `nx init --yes` unattended when no service is provisioned, and reruns an unfinished one (rdr-228:677-683, :1600-1601). The init keep-voyage fix landing ahead covers that caller too; nothing here changes it. |

Searched `docs/rdr/README.md` and the RDR collection for "dual", "local voyage",
"embedder router", "embed_model" and "re-embed"; no other RDR covers serving two
families from one engine.

## Context

### Background

nexus-35ok4 (GH #1461, 2026-08-18) made the client half of local Voyage work: a
user sets `local.embed_model` to a Voyage model and `voyage_api_key`, restarts the
service, and new collections are minted with Voyage names. It filed the engine
half as nexus-ddmfg, whose remedy 2 was this dual-mode router. nexus-ddmfg was
closed on 2026-08-29 as "reopen if a real trigger appears"; Sam's requirement is
that trigger. On 2026-10-17 Sam first chose Alternative 1 (guided migrate on
switch), then reversed it the same day and chose to build this RDR. The
`nx config set` stranding guard that Alternative 1 needed is dropped; only the
init keep-voyage fix goes ahead, alone.

### Technical Environment

- Engine: Java 21, GraalVM native image, `EmbedderRouter` with a model-token
  dispatch table (RDR-103 conformant names, RDR-204 catalog rows as authority).
  Latest engine tag at this revision: `engine-service-v0.1.160` (2026-10-11).
- bge-768: `Bge768Embedder`, ONNX runtime, admission-controlled by one
  process-wide `LocalOnnxAdmission` shared by the document and query routers and
  the cross-encoder reranker (`Main.java:303-324`). Its constructor checks the
  model and tokenizer files and a size floor, and throws `IllegalStateException`
  before touching ONNX runtime (`Bge768Embedder.java:226-255`).
- Voyage: `VoyageEmbedder`, `CceEmbedder`, `VoyageReranker`. Batches up to 1,000
  inputs per request under a token budget (`VoyageEmbedder.java:124`).
- The cloud engine has no ONNX model on disk. Constructing an ONNX session on a
  missing file segfaults rather than throws (conexus-qcn, comment at
  `Main.java:252-257`), so the cloud path must never construct one.
- Wire changes: a change visible on the wire records an entry in
  `docs/wire-contract-pending.md`, checked by `scripts/check_wire_contract_pairing.py`.

## Research Findings

### Investigation

Source reading at a9551ee21 (T2 [25872]) and 9172348a0 (T2
`nexus_rdr/210-research-1` to `-4`), then a 70-row re-verification of every claim
at e34467fdb (T2 `nexus_rdr/210-drift-audit-2026-10-17`), recorded as research
findings `nexus_rdr/210-research-5` (copy against the ownership guard), `-6`
(egress against `HttpsSchemeHandler`), `-7` (mode-string sites, CA-3), `-8`
(cutover does not cascade collection-keyed state) and `-9` (the drift list). For
this revision the cited lines were re-read at e34467fdb; where the audit and the
code disagreed, the code is what is cited. Further research is in progress in
another session; where this RDR depends on a result not yet recorded, the text
says "pending 210-research-<topic>".

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Engine router and boot (`Main`, `EmbedderRouter`) | Yes | Posture from key presence; no router holds bge and Voyage except the test-only `EmbedderRouter(OnnxEmbedder, String, String)` (`:111-127`), typed to the retired MiniLM embedder. |
| Catalog write path (`CatalogRepository`, `CombinedWriteService`) | Yes | Per-document transaction writes chunks then replaces the document's manifest rows (`CatalogRepository.java:5182-5260`). |
| Reaper (`ChunkReaper`, vectors-021, vectors-030) | Yes | 30-day grace; move floor 0.25 from 100 chunks. |
| onnxruntime 1.20.0 with GraalVM native image, alongside the Voyage HTTP clients | No | Coexistence in one native process is Critical Assumption 1; a spike, not a source search, settles it. |
| Voyage API | No (RDR-195 verified the input-count cap via Context7) | Not re-read; the engine's retry loop is `VoyageRetryLoop`. |

### Key Discoveries

- **✅ Verified** (source search) — Server-side embedding on write: when a
  request carries no vectors, `handleUpsertChunks` (`VectorHandler.java:480`,
  `force_re_embed` read at `:500`) routes to the repository, which embeds with
  the collection's model; with `force_re_embed` unset, a chash that already
  carries a vector gets a metadata-only update. Both public write calls pass an
  ownership guard (`VectorHandler.java:520, 528`, built at `:380`), and the
  repository checks ownership before embedding (`PgVectorRepository.java:932-935`,
  `checkOwnership` at `:799-813`, re-checked inside the write transaction at
  `:824-846`). *Source: T2 210-research-1, re-read.*
- **✅ Verified** (source search) — A document cannot be in two collections at
  once. The manifest's primary key is (tenant, doc_id, position)
  (`catalog-001-baseline.xml:181`). So no design can write a document's target
  manifest rows while its source rows still serve.
- **✅ Verified** (source search) — live(c) reads the chunk's own collection:
  `chunk_live_owners` joins manifest rows with `m.collection = c.collection` and a
  non-deleted document (vectors-018 `:65-75`), applied on every read path by
  vectors-019 (`:6`). A copied chunk with no manifest row in its new collection is
  invisible.
- **✅ Verified** (source search) — The ownership guard cannot be bypassed from a
  new engine class. `OwnershipGuardCoverageScan` fails when any main-source file
  other than `VectorHandler` calls one of the four guarded repository methods
  (`:35-36`, rule `:121-145`); its allowed-callers map is empty on purpose
  (`:38-39`) and admits only a caller "it cannot write an ownerless chunk". SQL
  functions that insert chunks are governed separately by
  `_CHUNK_INSERTER_ALLOWLIST` (`tests/test_changelog_chunk_inserter_lint.py:49`).
  Together with the live(c) finding above, this refutes the 2026-09-16 copy
  design. *Source: T2 210-research-5.*
- **✅ Verified** (source search) — The combined write already moves a document.
  `writeManifestRows` (`CatalogRepository.java:5182-5260`), inside one per-document
  transaction under the target collection's sweep gate: upserts the resolved
  chunk vectors into the target (`:5215-5221`), deletes ALL of the document's
  manifest rows by `doc_id`, whatever collection they name (`:5222`), inserts the
  new rows stamped with the target (`:5225-5236`), and refuses a tombstoned
  document (`:5250-5258`). `CombinedWriteService` embeds outside that transaction,
  through the same router, and skips chashes whose identical text is already
  stored (class javadoc `:8-50`). The write's sweep deletes only chunks in the
  target collection (`sweepChunksQuery`, `:6285-6290`). `physical_collection` is
  set by a separate call (`updateDocumentCollection`, `:3365-3370`).
- **✅ Verified** (source search) — The reaper will not drain a fully orphaned
  collection. A chunk becomes reapable 30 days after it was last written or
  orphaned (`chunk_is_reapable`, vectors-021 `:420-439`). The move into quarantine
  is refused when the reapable set is at least 100 chunks and more than a quarter
  of the collection (`ChunkReaper.java:75-77`, defaults `:218-219`, judged again
  inside `reaper_quarantine_chunks`, vectors-030 `:1953`). Exemptions exist only
  as an environment list (`NX_REAPER_FLOOR_EXEMPT_COLLECTIONS`,
  `ChunkReaper.java:133`). A superseded source whose documents all moved is 100%
  reapable after 30 days and is refused every pass.
- **✅ Verified** (source search) — Cross-model rename leaves stores behind. The
  copy branch of `renameCollectionTxn` (`CatalogRepository.java:9124-9165`)
  repoints `catalog_documents.physical_collection` and the manifest's collection
  and model; its own comment (`:9149-9152`) says topic assignments and orphan
  stamps keep naming the source's chunks. The client's cascade for the other
  stores, `remap_collection_references` (`collection_rename.py:174`), has no
  caller outside its module (grep of `src/nexus`). The engine tables it touches
  are listed in `T2Database.rename_collection_cascade`
  (`db/t2/__init__.py:347-355`); one of them, `nexus.chash_index`, no longer
  exists (dropped, `rdr187-001-drop-chash-index.xml:58-60`). *Source: T2
  210-research-8; the full census of collection-keyed state is pending
  210-research-cutover-cascade-census.*
- **✅ Verified** (source search) — `/version` already reports the served model
  set as `embedding_models` beside `embedding_mode` (`VersionHandler.java:478-487`).
- **✅ Verified** (git grep) — No production client calls `POST /v1/vectors/resolve`:
  its one client method, `HttpVectorClient.resolve_content`
  (`http_vector_client.py:4155`), has no caller outside tests, and the client has
  its own `https` reader (`aspect_readers.py:748`). That the handler exists and
  breaks the drafted guard is T2 210-research-6; the full egress census is
  pending 210-research-egress-census.
- **✅ Verified, draft corrected** (source search) — Batch numbers: Voyage batches
  are planned under a token budget up to 1,000 inputs (`VoyageEmbedder.java:124`);
  the 300-row write cap is a client convention (`limits.py:60`) the engine does
  not enforce on `upsert-chunks`; the existing re-embed verb uses 100 so a request
  fits the managed edge's 55-second limit (`commands/collection.py:1438-1444`).
  *Source: T2 210-research-3, re-read.*
- **✅ Verified** (source search) — The engine runs periodic work on one scheduler
  registered at construction (`NexusService.java:686`, `:700`, reaper at `:729`),
  each run stateless or holding state in process; there is no persisted job table.
  *Source: T2 210-research-4, re-read.*
- **✅ Verified** (source search) — Drift since 9172348a0: `embed_migrate.py`
  and `rdr195-voyage-mvv.sh` deleted, the verb renamed and its batch 100, the
  content-type fan-out already spanning siblings, the nexus-vply6 named error,
  and RDR-223, RDR-225 and RDR-192 load-bearing. *Source: T2 210-research-9.*
- **Documented**: posture is chosen once, from key presence (`Main.java:238, 251`).
- **Documented**: the profile is rewritten on every boot from the posture
  (`EmbedderRouter.java:264-272`, `:356-378`).
- **Documented**: a request that names collections of two models is refused by
  the engine (`requireHomogeneousModel`, used at `PgVectorRepository.java:2399` in
  `searchPerCollection`), and the client already splits a search per model
  (`search_engine.py:595-625`).

### Critical Assumptions

- [ ] **CA-1.** A bge-768 embedder and the Voyage embedders and reranker can be
  built in one native-image process without conflict on macOS arm64, Linux x64
  and Windows x64, and the bge files' presence can be checked without creating an
  ONNX session. — **Status**: Unverified, pending 210-research-ca1 — **Method**:
  Spike (Phase 1 Step 1). The presence half is already true in source: the
  constructor's file and size checks throw before ONNX runtime is touched
  (`Bge768Embedder.java:226-255`).
- [ ] **CA-2.** A merged result list from a bge-768 collection and a Voyage
  collection ranks soundly, because every result of a keyed engine passes the
  Voyage reranker, which scores query and text directly, and its scores are
  comparable across the per-model requests the client sends. — **Status**:
  Unverified, pending 210-research-ca2-q2 — **Method**: Source Search (does every mixed-family path carry
  rerank scores to the client merge?), then Spike (measure, Phase 4 Step 3).
  Sam's 2026-10-17 answer (one reranker per engine) narrows it; it does not
  verify it.
- [x] **CA-3.** No other code path assumes "keyed means no local embedder".
  — **Status**: Refuted (T2 210-research-7; the full site census is pending
  210-research-ca3-census) — **Method**: Source Search.
  Gap 8 lists the client branches; the design adds a third mode value and moves
  per-collection decisions to the collection's own model (Technical Design 10),
  shipped before any engine reports the new value (Phase 1).
- [x] **CA-4.** Writing a document's chunks into the target collection through
  the combined write moves the document whole: its manifest rows leave the source
  in the same transaction that writes the target's chunks, so no chunk is
  ownerless at any commit. — **Status**: Verified for the write itself; whether
  it is the mechanism to build is pending 210-research-reembed-copy-options
  — **Method**: Source Search (`CatalogRepository.java:5182-5260`, manifest key
  `catalog-001-baseline.xml:181`).
- [ ] **CA-5.** An engine-internal caller of `CombinedWriteService` runs without
  a request deadline and outside the edge's 55-second limit, so its batch bound is
  the job's own. — **Status**: Unverified — **Method**: Source Search (Phase 6
  Step 1). Inferred, not read: the deadline is installed per request by the HTTP
  layer (`RequestDeadline.java:46`, read from `AuthFilter`).

## Proposed Solution

### Approach

One router per input type holds every embedder the machine can run: bge-768 when
its files are on disk, Voyage when the key is present. Dispatch stays by the
collection row's model, so each collection is embedded by the model its vectors
were made with, and nothing the machine can serve is refused. The user's
`local.embed_model` becomes the profile input that picks the model for new
collections. The engine reports a third mode value, `dual`, and the client stops
making per-collection decisions from the engine-wide mode. The supervisor passes
the key whenever one is configured. Client reads span every model sibling. A
re-embed job moves a collection onto the profile's model document by document,
through the same atomic write the indexers use.

### Technical Design

1. **Engine router.** Generalize the mixed constructor to take any local
   `Embedder` (the admission-wrapped bge) and a nullable key, and delete the
   MiniLM-typed form. Main builds bge when a non-throwing presence probe (the same
   file and size checks the constructor makes) finds the files, and the Voyage
   embedders when the key is set. Neither present is a boot failure naming both
   remedies. A truncated or wrong-size model stays a loud boot failure, not
   "absent". The cloud container has no bge files, so it boots Voyage-only
   exactly as today, with no ONNX session.
2. **Profile policy.** The supervisor passes `NX_EMBED_PROFILE` (`bge` or
   `voyage`), derived from `local_embed_model_is_voyage()` when a choice is
   recorded and left unset when none is. The engine seeds the profile from it and
   refuses to boot on a choice it cannot serve (`voyage` with no key, `bge` with
   no bge files), naming the missing piece. Unset keeps today's rule: keyed means
   Voyage (Sam, 2026-10-17, Q1).
3. **Rerank.** One reranker per engine (Sam, Q2): a keyed engine reranks every
   search with Voyage, including bge-only results; a keyless engine uses the
   cross-encoder. On a keyed engine the cross-encoder is not built.
4. **Supervisor.** Pass the configured key regardless of `local.embed_model` and
   retire the nexus-r5f3c branch (`storage_service_daemon.py:1544-1572`), whose
   reason (Gap 1) is gone.
5. **init and the ladder.** Landing ahead on nexus-ddmfg; not scope here.
6. **Client reads.** The single-name resolver returns every live sibling of the
   owner's corpus, not one; callers that need one name for a write keep the write
   resolver. Searches already split per model and merge client-side.
7. **Egress guard (nexus-umm29).** A main-source scan, shaped like
   `OwnershipGuardCoverageScan`: every construction of an outbound client
   (`HttpClient.newBuilder`, `HttpClient.newHttpClient`, `openConnection(`,
   `new Socket(`, `SocketChannel.open`) must be in a file on an allowlist that
   carries a reason per entry, and the scan asserts it found at least as many
   sites as the allowlist names (non-vacuity). The Voyage entries also pin the
   host: their URL constants must name `api.voyageai.com`. The allowlist is the
   three Voyage classes, subject to pending 210-research-egress-census finding
   no other site. `HttpsSchemeHandler` is Open Question 1: the
   recommendation is that a supervisor-spawned local engine does not register
   the `https` scheme, so the guard's allowlist stays Voyage-only.
8. **Java comments.** The stale router comments are corrected here, since they
   describe the router this RDR changes: `Main.java:35-38` (says an ONNX fallback
   exists when keyed), the class javadoc at `EmbedderRouter.java:33-47`, and the
   rest of T2 [25872] items 1-13 and 22. The non-engine sweep landed in f7cae33ec.
9. **Re-embed to the profile default.** See the next section.
10. **Mode string and per-collection decisions.** `modeName()` returns
    `onnx-local` (bge only), `voyage` (Voyage only) or `dual` (both). The two
    existing values keep their meaning, so a cloud engine and a keyless engine
    report what they report today. The client changes, all landing before any
    engine can report `dual`:
    - The chunk cap and the byte budget follow the target collection's model: a
      bge collection gets the ONNX memory cap, a Voyage collection the Voyage
      rules (`http_vector_client.py:1061, 1175`).
    - Similarity thresholds apply to hits from Voyage collections, whatever the
      engine's mode (`search_engine.py:580`).
    - The probe memo accepts `dual` (`http_vector_client.py:6049`).
    - The dimension-orphan finder compares each collection's declared model with
      the set of models this install can serve: the engine's `embedding_models`
      (`VersionHandler.java:478-487`) plus every Voyage model, which a key would
      serve. A collection is listed only when its model is in neither. A served
      collection can then never be listed, and a Voyage collection on a keyless
      engine is reported as "needs the key", not as an orphan. When
      `embedding_models` is absent (an engine older than that field), the finder
      reports "cannot verify" and lists nothing, as it does for an unknown mode
      today.
    - An old client against a dual engine fails closed: it does not recognise
      `dual`, so `_active_embedding_dim` answers unknown and prune lists nothing
      (`commands/collection.py:635-650`, `:745-760`). Reporting `voyage` instead
      would make an old client list every bge collection for deletion; that is
      why the value is new rather than reused.
11. **Profile seam (Gap 9).** The client's registration check skips the profile
    comparison when the row already exists with the same model; it reads the row
    first, as the implicit path already does for the superseded check
    (`corpus.py:2110-2114`). The engine needs no change: an existing row is never
    re-pointed by the profile (`CatalogRepository.java:7906-7930`). A new
    engine test pins that behaviour so the client fix cannot drift from it.

### Re-embed to the profile default

The job answers one question, "does every live collection carry the vectors the
profile now asks for", and makes the answer yes. A collection is stale when its
row's `embedding_model` differs from the profile's model for its `content_type`.
Quarantine, dormant and disputed rows are never candidates; they drain through
their own lifecycle. The user starts it; a profile change prints a hint and
`nx doctor` reports the drift (Sam, Q4).

**The move is per document, into the model sibling.** The target is the sibling
named for the profile's model (`code__1-1__voyage-code-3__v1` beside
`code__1-1__bge-base-en-v15-768__v1`), registered live if absent; the profile is
what lets that registration through (`CatalogRepository.java:7944-7960`), so the
target model is always the profile's. The job walks the source's live documents
in tumbler order. For each batch it reads, per document, the manifest rows and
the chunk text and metadata they name, and calls `CombinedWriteService` for the
TARGET collection with those chunks and rows. The engine embeds outside any
transaction with the target's model, skipping chashes the target already holds
with identical text, then runs one transaction per document that writes the
target chunks, replaces the document's manifest rows with rows naming the target,
and, new in this RDR, sets the document's `physical_collection` in that same
transaction (a parameter on the job's internal call; the public routes are
unchanged).

That choice follows from three facts in Research Findings. A document has one
manifest row per position, so its rows cannot name both collections. A target
chunk with no target manifest row is invisible and is exactly what the ownership
guard refuses. And the combined write already does chunk-plus-owner in one
transaction. So the job writes nothing ownerless at any commit, needs no entry
in `OwnershipGuardCoverageScan`'s allowed callers, adds no SQL chunk inserter,
and does not touch `renameCollectionTxn`.

**Status of this choice: pending 210-research-reembed-copy-options.** A
comparison of the copy options under the guard is in progress in the research
pass: an engine job writing chunks plus target manifest rows per batch (the
option above is that one, done per document because the manifest key allows one
collection per document), a guard-exempt write followed by a repoint
(Alternative 3), and re-index from source as RDR-225 describes. The rest of this
section assumes the per-document move and is revised if that comparison picks
another option.

**When target chunks become readable.** At the commit of their document's move,
and not before. From that commit the document is in the target only; before it,
in the source only. A reader that spans both siblings (bare content-type corpora
today, every resolver after Phase 4) sees each live document exactly once
throughout. A reader still on the single-name resolver sees whichever sibling it
picks, which is why Phase 4 comes before Phase 6.

**Concurrent writes.** The job's per-document call carries a precondition: the
document's current manifest chashes and collection are what the job read. When
they are not (a client re-indexed or deleted the document since), the job skips
it and counts the skip; the next pass sees it wherever the client put it. A
client re-index in the dual world already writes to the profile's sibling and
moves the document itself, through the same transaction.

**Idempotent.** Documents already in the target have no source manifest rows and
are not candidates. Chunks shared by two documents are embedded once (the
existence partition). Re-running after a crash, a restart or completion
converges, and a second run with nothing stale plans zero documents and writes
nothing.

**What is not moved.**
- *Tombstoned documents* are refused by `writeManifestRows`
  (`CatalogRepository.java:5250-5258`). They stay in the source until purge-trash
  removes them or a restore makes them live, after which the next run moves them.
- *Ownerless chunks* in the source (no live manifest row) are invisible today and
  are not moved. The plan reports their count.

**Cutover and what the move leaves behind.** The list below is from this
revision's reading and T2 210-research-8; the full census of collection-keyed
state is pending 210-research-cutover-cascade-census.
- *Source row.* When the source holds no live document, the job supersedes it
  (`supersedeCollection`, `CatalogRepository.java:8492`). The routing listing
  then leaves it out (`:8213-8217`), and RDR-227's reconciler drops its index.
- *Source chunks.* Each moved document's source chunks become ownerless at that
  document's commit and are stamped orphaned by the manifest triggers. They are
  invisible from that moment. The reaper will not drain them: once all documents
  have moved, the source is entirely reapable and the move floor refuses it every
  pass (Research Findings). The source is therefore removed explicitly through
  the existing delete (`CatalogRepository.deleteCollection`, `:8280`), and only
  once it holds no document row at all, tombstoned ones included, since the
  delete removes documents by `physical_collection` (`:8465`). When that happens
  is Open Question 3.
- *Aspects, the aspect queue and highlights* (`document_aspects`,
  `aspect_extraction_queue`, `document_highlights`, each with a `collection`
  column, `aspects-001-baseline.xml:56-60, 111-116, 153-157`) describe the
  document, not its vectors. The job updates their `collection` for the moved
  document in the same per-document transaction. Inferred, not read: the join
  from a document to its aspect rows is by source path and to its highlights by
  `doc_id`; Phase 6 Step 1 confirms it.
- *Topics and topic assignments* are model-bound (centroids are partitioned by
  model). The source's assignments go with the source's chunks when the source is
  deleted (`topic_assignments_chunk_fk` cascades, vectors-030 `:1033`). The job's
  `done` report names `nx taxonomy discover` (`taxonomy_cmd.py:694`) for the
  target, and the doctor's drift row says so until it is run.
- *Search telemetry and hook failures* are history. They keep naming the source.
- *Client caches.* The registration and collection-row caches already treat a
  superseded row as stale (`corpus.py:2093-2097`).
- *The 30-day reap grace* still protects a job that stops halfway: nothing it
  wrote is ownerless, and the source's documents not yet moved still own their
  chunks.

**Batched.** A batch is whole documents up to 100 chunks; a document with more
than 100 chunks is a batch of its own, because a document moves in one
transaction. 100 matches the existing verb's bound (`commands/collection.py:1444`),
whose reason (the managed edge's 55-second limit) applies when the job runs in
the managed engine; locally the bound only keeps one batch's embed short (CA-5).
Voyage requests are planned by the engine's own batching up to 1,000 inputs.

**Managed.** The job is a row in a new engine table, `reembed_jobs` (tenant,
source, target, state, document cursor, documents moved, documents skipped,
chunks embedded, chunks reused, tokens billed, started, updated, finished, last
error), created through Liquibase. States: `planned`, `running`, `paused`,
`cutover`, `done`, `failed`. A runner on the engine's scheduler, the shape the
tuple sweep and the reaper already use (`NexusService.java:686, 729`), picks up
`running` rows at boot, so a job survives a restart and a closed lid, and no
client is needed once it is started. Two jobs never run against one source; the
row is the lock.

**The verb.** `nx collection re-embed` is extended rather than joined by a second
verb:
- `nx collection re-embed --all` plans every stale collection (chunk count,
  token estimate, ownerless and tombstoned counts) and, with `--no-dry-run --yes`,
  starts one job per collection. Dry run stays the default, as today
  (`commands/collection.py:1666-1667`).
- `nx collection re-embed NAME --to MODEL` keeps its in-place same-model
  behaviour. When MODEL differs from the name's model in service mode, it starts
  the job instead of refusing (`:1711-1736`); MODEL must then be the profile's
  model for the collection's content type, and the choices gain
  `bge-base-en-v15-768` (`:1436`).
- `--status [JOB]`, `--pause JOB`, `--resume JOB` and `--abort JOB` act on job
  rows. Abort stops the runner; documents already moved stay moved, and a later
  run continues from what remains in the source.

**Monitored.** `GET /v1/vectors/reembed/status` returns every job row for the
tenant. `nx doctor` extends the existing off-profile row (`health.py:8436-8458`,
which today suggests `nx collection reindex`) to suggest `re-embed --all`, and
adds one row for running or failed jobs, not-applicable when the table is empty.

**Logged.** The engine logs one event per batch (`reembed_batch`: job, source,
target, cursor, documents moved, skipped, chunks embedded, reused, elapsed,
tokens) and one per state change. The client logs the plan it submitted and each
status poll. A failed batch is logged with the engine's error text and leaves the
job `failed` with the cursor at the last committed document.

**Cost.** The plan states the token estimate before anything runs, from the
source's live chunk text lengths. Each run has a token cap with a default of
10 million; a plan above the cap is refused before any write unless a flag raises
it. bge re-embeds have no cap; the plan states the time estimate instead (Sam, Q5).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Dual router | `EmbedderRouter(OnnxEmbedder, String, String)` (`:111-127`) | Extend: retype to `Embedder`, nullable key; delete the MiniLM-typed form |
| bge presence probe | `Bge768Embedder` constructor checks (`:226-255`) | Extend: the same checks as a non-throwing static probe |
| Profile input | `seedEmbeddingProfile` (`EmbedderRouter.java:264-272`) | Extend: seed from `NX_EMBED_PROFILE` |
| Mode string | `modeName()` (`:162-169`) | Extend: add `dual` |
| Key plumbing | `storage_service_daemon.py:1544-1590` | Replace the r5f3c branch |
| Per-collection caps, budget, thresholds | `http_vector_client.py:1061, 1175`; `search_engine.py:580` | Extend: key on the collection's model |
| Orphan finder | `_active_embedding_dim` / `_find_dimension_mismatched_collections` (`commands/collection.py:601-720`) | Replace the single-dimension rule with the served-model-set rule |
| Profile seam | `ensure_collection_registered` (`corpus.py:2116-2120`) | Extend: skip for an existing same-model row |
| Sibling reads | `t3_collection_name` (`corpus.py:1213`); `resolve_corpus` stage 2 (`:2428-2451`) | Extend the single-name resolver; stage 2 already spans siblings |
| Egress guard | none | New source scan |
| Document move | `CombinedWriteService.writeManyCombined` (`:202`), `CatalogRepository.writeManifestRows` (`:5182`) | Reuse, with a precondition and a `physical_collection` parameter for the job's internal call |
| Cutover | `supersedeCollection` (`:8492`), `deleteCollection` (`:8280`) | Reuse unchanged |
| Cross-model rename | `renameCollectionTxn` copy branch (`:9124-9165`) | Not used by this RDR |
| Client reference cascade | `remap_collection_references` (`collection_rename.py:174`) | Not used: the engine moves the per-document stores in the move transaction |
| Re-embed verb | `nx collection re-embed` (`commands/collection.py:1659`) | Extend: `--all`, cross-model `--to` starts the job, job controls |
| Job table and runner | none; scheduler at `NexusService.java:686-729` | New Liquibase changeset `reembed_jobs`; runner on the existing scheduler |
| Doctor rows | off-profile row (`health.py:8436-8458`) | Extend; one new job row |
| Keyed gate | none (`rdr195-voyage-mvv.sh` deleted) | New script, a fourth battery leg |

### Decision Rationale

Dispatch by the row's model already exists and is the right authority; the
defect is that the table is built from half of what the machine can run. Filling
it fixes Gaps 1 and 3 in the engine. Gaps 2 and 4 then reduce to removing a guard
and a single-name choice that existed to route around Gap 1. Gap 8 has to land
first, because a dual engine reporting either old value would mislead a client
that branches on it, and one of those branches deletes data. For the re-embed,
the per-document move reuses the one write that RDR-223 made atomic, so it keeps
the ownership invariant without an exception and keeps search whole without a
cutover window.

## Alternatives Considered

### Alternative 1: Keep one posture per engine and migrate on switch

**Description**: nexus-ddmfg's remedy 1. Switching `local.embed_model` offers a
guided re-embed of existing collections into the new family.

**Pros**:

- No engine router change.

**Cons**:

- Only one family is served at a time, which is what Sam ruled out.
- A re-embed costs Voyage tokens or hours of on-device time on every switch.

**Reason for rejection**: fails the requirement. Sam chose it on 2026-10-17 and
reversed the choice the same day.

### Alternative 2: The client embeds with Voyage and hands vectors to the engine

**Description**: nexus-umm29's option (b): the engine never leaves the machine.

**Cons**:

- Reverses RDR-188, which moved all Voyage traffic into the engine.
- Sam decided option (a) on 2026-06-15.

**Reason for rejection**: already decided against.

### Alternative 3: Re-embed by copying, then one cross-model rename

**Description**: The 2026-09-16 draft. Copy every live chunk into the target with
no manifest rows, then repoint all manifests at once through
`renameCollectionTxn`'s copy branch, then supersede.

**Cons**:

- Every copied chunk is ownerless until the end, so the copy needs a write that
  the ownership guard refuses and that `OwnershipGuardCoverageScan` forbids
  outside `VectorHandler`. An exception would have to be granted to a caller that
  does write ownerless chunks, which is the one thing the scan's allowed-callers
  rule says an entry may not do.
- The rename repoints tombstoned documents too, so their chunks must also be
  copied (paying to embed the trash) or the foreign key aborts the repoint.
- Target chunks are invisible until the final transaction, and that transaction
  repoints the whole collection at once.

**Reason for rejection**: the per-document move gets the same result through an
existing atomic write with none of these.

### Briefly Rejected

- **Two engine processes, one per family**: two Postgres connection pools and
  two supervisors for one install, to work around a dispatch table.
- **Re-index from source (RDR-225's "registers a new collection")**: cannot move
  a chunk with no source file (notes, DEVONthink content), which is Gap 7's
  point; under evaluation in pending 210-research-reembed-copy-options.
- **A side table of target vectors inserted at cutover**: brings back the staging
  RDR-223 deleted, adds a SQL chunk inserter to the lint allowlist, and inserts a
  whole collection with its index maintenance in one transaction.

## Trade-offs

### Consequences

- A keyed local install keeps serving its bge-768 collections.
- A key the supervisor can find now always reaches the engine. With Sam's rerank
  rule, a user whose profile is bge and who has `VOYAGE_API_KEY` exported for any
  reason gets every search reranked by Voyage, which sends query and result text
  to Voyage. Today the r5f3c guard withholds that key. See Open Question 2.
- The engine holds the bge model in memory on keyed installs that have it on
  disk, even if they use only Voyage.
- A re-embed holds both copies of a moved document's chunks until the source is
  deleted, and spends Voyage tokens once per distinct chunk; the plan says how
  much before the job starts.
- A re-embed leaves the target without topics until `nx taxonomy discover` runs.

### Risks and Mitigations

- **Risk**: results merged across families rank badly.
  **Mitigation**: CA-2, measured in Phase 4 before re-embed ships.
- **Risk**: the cloud engine constructs an ONNX session and segfaults.
  **Mitigation**: the non-throwing presence probe, and a native-image smoke with
  no model on disk (Phase 2 Step 2).
- **Risk**: an old client prunes a dual engine's collections.
  **Mitigation**: the `dual` value fails closed in old clients (Technical Design
  10), and the client fix ships first.
- **Risk**: native image cannot hold ONNX runtime and the Voyage clients together.
  **Mitigation**: Phase 1 Step 1 is the spike; on failure the RDR goes back to
  planning before any other step.

### Failure Modes

A family the engine cannot serve still gets 422 naming the missing piece (the
key, or the bge model files; `EmbedderRouter.java:547-556`). The boot banner names
both families, the profile's choice and the reranker. A profile choice the engine
cannot serve stops the boot with that reason in the service log. A re-embed batch
that fails leaves the job `failed` with the cursor at the last committed document
and the error in the row; the documents not yet moved are still served from the
source, so search is whole. A skipped document (precondition failed) is counted
in the row and in `--status`.

## Implementation Plan

### Prerequisites

- [ ] CA-1 verified (Phase 1 Step 1). CA-2 and CA-5 are verified inside their
  phases, before the phase that depends on them ships.
- [x] Non-engine doc sweep and the nexus-umm29 wording landed (f7cae33ec).
- [ ] The init keep-voyage fix (nexus-ddmfg branch) merged to develop.
- [ ] Open Question 1 decided before Phase 2 Step 6; Open Question 3 before
  Phase 6 Step 4.

### Minimum Viable Validation

A local engine booted with a real key and the bge model, one bge-768 collection
and one Voyage collection: write to and search both, restart, search both again,
and one search over both returns hits from both. Then `re-embed --all` moves the
bge collection (one source-backed document and one `store_put` note) to its
Voyage sibling, search returns the same documents before and after, and a second
`re-embed --all` plans zero documents. This is the Phase 5 gate script's body
plus the Phase 6 scenarios.

### Phase 1: Spike and client prerequisites

#### Step 1: Native-image coexistence spike (CA-1)

Build the native image on macOS arm64, Linux x64 and Windows x64 with a spike
`Main` that constructs `Bge768Embedder` and the three Voyage classes in one
process, the Voyage ones pointed at a loopback stub through their URL-taking
constructors. **Pass**: on all three targets the image builds, boots, returns a
768-dimension bge vector and a 1024-dimension vector from each Voyage class, and
exits 143 on SIGTERM sent during bge initialisation. **Fail**: any build error,
reflection or JNI conflict, crash or wrong exit code on any target; stop and
revise the RDR. Record the result as a research finding.

#### Step 2: Mode string audit and per-collection decisions

Change the client branches in Gap 8 to the rules in Technical Design 10: chunk
cap and byte budget by collection model, thresholds by collection model, the memo
accepts `dual`. **Test**: unit tests with a fake `/version` reporting each of
`onnx-local`, `voyage` and `dual`, asserting the cap, budget and threshold for a
bge collection and a Voyage collection under each.

#### Step 3: Orphan finder by served model set

Replace `_active_embedding_dim`'s single dimension with the served-model-set rule.
**Test**: with `embedding_models` = {bge} and = {bge, voyage-code-3,
voyage-context-3, voyage-3}, the finder lists a minilm-384 collection and never a
bge or Voyage one; with the field absent it lists nothing and says "cannot
verify"; `nx collection prune --yes` against a dual fake deletes nothing served.
Also run `uv run pytest -m lint` on the touched files.

#### Step 4: Profile seam

`ensure_collection_registered` skips the profile comparison for an existing
same-model row. **Test**: a Python test where the profile says Voyage and an
existing bge row is written to (passes), and a new bge name is registered
(refused, as today); a Java test pinning `upsertCollection`'s existing-row branch
against a profile that names another model.

### Phase 2: Engine

#### Step 1: Router and presence probe

Retype the mixed constructor; add the non-throwing probe; Main builds what is
present. **Test**: `EmbedderRouter` unit tests for the three shapes (bge only,
Voyage only, both): `availableModels`, `modeName`, and `resolveEmbedderStrict` for
a bge row and a Voyage row.

#### Step 2: Cloud shape smoke

**Test**: native-image smoke with a key and no bge files on disk boots Voyage-only,
reports `voyage`, and creates no ONNX session; with neither present the boot fails
naming both remedies; with a truncated model file it fails naming the size floor.

#### Step 3: Profile variable

Seed from `NX_EMBED_PROFILE`; refuse an unservable choice at boot. **Test**:
engine integration tests for each value with and without the key and the files,
asserting the seeded rows or the boot refusal.

#### Step 4: Rerank, banner and status

One reranker by posture; banner names both families, the profile and the
reranker; `local_embed_activity` is reported whenever bge is built. **Test**:
`StatusHandler` and `VersionHandler` tests for the dual shape.

#### Step 5: Dual journey test

Rewrite `Rdr204Gh1461ProfileRestartJourneyTest` to use one production dual router
for both collections, removing the separate bge router and the fake. **Test**: the
rewritten test itself.

#### Step 6: Egress guard

Add the source scan with its allowlist and non-vacuity count, and the host pin.
Apply Sam's answer to Open Question 1 (recommended: the supervisor-spawned engine
does not register the `https` scheme). **Test**: the scan, plus a negative test
that a fixture file constructing an `HttpClient` fails it.

#### Step 7: Java comments and the engine tag

Correct the comments in Technical Design 8. Record the `dual` mode value in
`docs/wire-contract-pending.md`. Run every `*GateTest`. Ships in an engine tag
after `engine-service-v0.1.160`; the tag is Sam's.

### Phase 3: Supervisor

#### Step 1: Key and profile plumbing

Retire the r5f3c branch; pass the key whenever the credential chain has one; pass
`NX_EMBED_PROFILE`. **Test**: `tests/daemon/test_storage_service_daemon.py` cases
for a bge choice with a key (key passed, profile `bge`), a Voyage choice without a
key (no key, profile `voyage`, the engine refuses at boot), and no choice (no
profile variable).

#### Step 2: Local-service gate comments

The gate stays keyless; correct its stale comments about the key and one posture
(`local-service-gate.sh:61-71`, `:349-352`, `:940-952`). **Test**: the gate's own
guard test (`tests/e2e/local_service_gate_guard_test.sh`).

### Phase 4: Client reads

#### Step 1: Single-name resolver spans siblings

The read resolver returns every live sibling; write resolution is unchanged.
**Test**: replace the pin at `tests/test_rdr_109_phase2_dispatch.py:258` with one
asserting both siblings are read once a Voyage sibling exists.

#### Step 2: Merge across families

Searches over siblings of two models merge their reranked results. **Test**: a
merge test with hits from a bge and a Voyage collection.

#### Step 3: Ranking measurement (CA-2)

Source search for every mixed-family path to the merge; then a measured run on a
keyed local engine over a fixed query set, comparing the merged list with each
family's own list. Record the result as a research finding before Phase 6.

### Phase 5: Gates

#### Step 1: Dual-embedder gate script

No keyed local script exists to extend. Add `tests/e2e/dual-embedder-gate.sh`: it
provisions a local engine with the bge model and a real Voyage key from the
operator's credential chain, runs the MVV's first half, restarts, and runs it
again. An absent key is a FAIL, not a skip. **Test**: the script's own guard test,
asserting a missing key fails it.

#### Step 2: Release battery leg

Register it as a fourth leg in `tests/e2e/release-battery.sh` beside `:165-167`.
It runs in the release battery and by hand only, never nightly (Sam, Q3).

### Phase 6: Re-embed

#### Step 1: Internal call and precondition

An engine-internal entry to `CombinedWriteService` that takes the precondition
and sets `physical_collection` in the per-document transaction, and moves the
aspect, queue and highlight rows. Confirm CA-5 and the aspect joins by source
reading first. **Test**: engine integration tests: a document moves whole
(chunks, manifest, `physical_collection`, aspects); a document changed after the
read is skipped; a tombstoned document is refused; a chunk shared by two
documents is embedded once.

#### Step 2: Job table and runner

`reembed_jobs` changeset, runner on the scheduler, states and cursor. **Test**:
restart mid-job resumes from the cursor; two starts on one source are refused; a
failed batch leaves `failed` with the cursor at the last committed document.

#### Step 3: Status route and verb

`GET /v1/vectors/reembed/status`; `nx collection re-embed --all`, cross-model
`--to`, and the job controls; token estimate and cap. **Test**: CLI tests for the
plan, the cap refusal, and each control.

#### Step 4: Cutover and source removal

Supersede at zero live documents; remove the source per Open Question 3. **Test**:
after the last move the source is superseded and out of the routing listing; the
source is deleted only when it holds no document row.

#### Step 5: Doctor rows and paired release

Extend the off-profile row; add the job row. Ships in its own engine tag with the
client half paired. **Test**: doctor tests for the not-applicable, running and
failed cases, and the Phase 6 MVV scenarios in the dual gate script.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Embedding profile rows | N/A (existing) | `nx doctor` profile row | N/A | `nx doctor` | N/A |
| Re-embed jobs | `nx collection re-embed --status` | `--status JOB` | `--abort JOB` (the row stays as the record) | a second `re-embed --all` plans zero; `nx doctor` off-profile row | N/A |
| Superseded source collections | `nx collection list` | `nx collection info` | the job's removal step, or `nx collection delete` | `nx catalog verify` | N/A |

### New Dependencies

None.

## Open Questions

1. **The engine's `https` fetcher in local mode.** `HttpsSchemeHandler` is a live
   outbound client (`HttpsSchemeHandler.java:46`, registered at
   `NexusService.java:648-649`) that fetches any public URL a request names. Either
   (a) the egress allowlist names it as a second carve-out, which needs its own
   amendment to RDR-157, RDR-160 and the privacy policy, or (b) a
   supervisor-spawned local engine does not register the `https` scheme, and the
   allowlist is the three Voyage classes. **Recommendation: (b).** No production
   client calls the route (`resolve_content` has no caller outside tests), the
   client already reads `https` sources itself (`aspect_readers.py:748`), and the
   carve-out Sam approved is for Voyage embed and rerank only.
2. **An ambient key and Voyage rerank on a bge profile.** With the key always
   passed and one reranker per engine, a user whose profile is bge but who has
   `VOYAGE_API_KEY` in the environment gets every search reranked by Voyage, so
   local text leaves the machine. Today the r5f3c guard withholds that key.
   Options: (a) accept it and make it visible (the boot banner and an `nx doctor`
   row "Voyage egress: embed for N collections, rerank for every search"); (b) a
   bge-profile engine reranks with the cross-encoder even when keyed, and uses the
   key only to serve existing Voyage collections. **Recommendation: (b).** It
   keeps Sam's "one reranker per engine" rule (the choice follows the profile, not
   the request), sends nothing to Voyage for a user who never chose Voyage, and
   the cross-encoder scores text directly, so merged lists rank the same way.
3. **When the superseded source is deleted.** Its chunks never drain on their own
   (the reaper's move floor). Options: (a) the job deletes it as soon as it holds
   no document row, tombstoned included, with `--keep-source` to opt out; (b) the
   job leaves it superseded and the user deletes it; (c) a waiver of the reaper
   floor for superseded collections. **Recommendation: (a).** Nothing is lost that
   cannot be rebuilt (the target holds every live chunk's text), the wait on the
   trash protects restores, and (c) grows the floor logic Sam has asked not to
   grow.

## Test Plan

- **Scenario**: dual engine, bge and Voyage collections in one database —
  **Verify**: embed, write and search succeed for both; no 422.
- **Scenario**: keyless engine, Voyage collection — **Verify**: 422 naming the key.
- **Scenario**: keyed engine with no bge files (cloud shape) — **Verify**: boots
  Voyage-only, reports `voyage`, no ONNX session constructed.
- **Scenario**: bge model file truncated — **Verify**: boot fails naming the size
  floor, not "absent".
- **Scenario**: `NX_EMBED_PROFILE=voyage` with no key — **Verify**: boot refused
  naming the key.
- **Scenario**: client against fake engines reporting `onnx-local`, `voyage` and
  `dual` — **Verify**: chunk cap, byte budget and thresholds follow each
  collection's model.
- **Scenario**: `nx collection prune --yes` against a dual engine — **Verify**: no
  served collection is listed or deleted; a minilm-384 collection is listed.
- **Scenario**: old client (no `dual` support) against a dual engine — **Verify**:
  prune reports "cannot verify" and deletes nothing.
- **Scenario**: profile says Voyage, write to an existing bge collection —
  **Verify**: accepted by client and engine; a new bge name is refused.
- **Scenario**: corpus with both siblings, read through `--collection` and
  through a bare content type — **Verify**: hits from both.
- **Scenario**: engine source adds a new outbound HTTP client — **Verify**: the
  egress guard fails.
- **Scenario**: dual gate with no key — **Verify**: FAIL, not skip.
- **Scenario**: `re-embed --all` on two stale collections, one holding only
  `store_put` notes — **Verify**: every live document is in its Voyage sibling,
  `physical_collection` names it, the sources are superseded, search returns the
  same documents.
- **Scenario**: search during a re-embed — **Verify**: each live document appears
  once, from whichever sibling holds it.
- **Scenario**: a client re-indexes a document while the job runs — **Verify**:
  the job skips it and counts the skip; the client's version stands.
- **Scenario**: engine restart mid-job — **Verify**: the job resumes from its
  cursor; no document is in both or neither collection.
- **Scenario**: `re-embed --all` twice — **Verify**: the second run plans zero
  documents and writes nothing.
- **Scenario**: a batch fails at Voyage — **Verify**: the job is `failed` with the
  cursor at the last committed document and the error in the row; `--resume`
  continues.
- **Scenario**: plan above the token cap — **Verify**: refused before any write,
  naming the estimate and the cap.
- **Scenario**: a tombstoned document in the source — **Verify**: not moved; the
  source is superseded but not deleted while the document row exists.

## Finalization Gate

### Contradiction Check

Three contradictions were found and resolved in this revision. First, the draft's
re-embed upserted copied chunks through the public write route; Research Findings
show that route's ownership guard refuses a chunk with no manifest row in the
target, and local engines enforce it. The design now moves documents through the
combined write, which writes the chunk and its owner together. Second, the draft
said source chunks drain through the ordinary quarantine sweep; the reaper's move
floor refuses a collection that is more than a quarter reapable, so a fully moved
source never drains. The design removes the source explicitly (Open Question 3).
Third, Critical Assumption 3 was refuted: the client branches on the engine's mode
string in six places, one of which deletes data; Technical Design 10 and Phase 1
answer it. One tension remains open rather than resolved: always passing the key
plus one Voyage reranker per keyed engine sends local text to Voyage for users who
did not choose Voyage, which the 2026-06-15 carve-out did not obviously intend
(Open Question 2). No other contradictions were found between research findings,
design principles, and the proposed solution.

### Assumption Verification

CA-3 is refuted (T2 210-research-7) and the design adjusted; its full site
census is pending 210-research-ca3-census, and Phase 1 Step 2 takes its list
from that census when it lands. CA-4 is verified by source reading as a property
of the write; whether it is the re-embed mechanism is pending
210-research-reembed-copy-options. CA-1 is unverified (pending
210-research-ca1) and is the first step of the plan, with a pass/fail criterion
on three targets; nothing else starts until it passes. CA-2 is unverified
(pending 210-research-ca2-q2); Phase 4 Step 3 measures it before the re-embed,
which is the first feature that puts mixed-family results in front of most
users. CA-5 is unverified and is read in Phase 6 Step 1 before the batch bound is
fixed. This RDR is not ready to pass the gate while those pending results are
open; they are named so the gate can check each one.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `EmbedderRouter.resolveEmbedderStrict`, `modeName`, `availableModels` | nexus engine | Source Search |
| `CombinedWriteService.writeManyCombined` / `CatalogRepository.writeManifestRows` | nexus engine | Source Search |
| `CatalogRepository.supersedeCollection`, `deleteCollection`, `upsertCollection` | nexus engine | Source Search |
| `GET /version` `embedding_mode`, `embedding_models` | nexus engine | Source Search |
| `Bge768Embedder` constructor checks | nexus engine | Source Search |
| ONNX runtime and Voyage clients in one native image | onnxruntime 1.20.0, GraalVM | Spike (Phase 1 Step 1, pending) |
| Voyage rerank score comparability across requests | Voyage API | Spike (Phase 4 Step 3, pending) |

### Scope Verification

The MVV is in scope and executed: its first half is the body of
`tests/e2e/dual-embedder-gate.sh` (Phase 5 Step 1), a release battery leg; its
second half is the Phase 6 Step 5 scenarios added to the same script. The init
keep-voyage fix is out of scope (nexus-ddmfg, landing ahead), and so is any
change to the managed engine's posture: the cloud boots Voyage-only exactly as
today.

### Cross-Cutting Concerns

- **Versioning**: `dual` is a new wire value on `/version` and `/v1/status`,
  recorded in `docs/wire-contract-pending.md`. Old clients fail closed on it
  (Technical Design 10). Local installs run the engine their client was built
  with; the managed engine never reports `dual`.
- **Build tool compatibility**: the native image must carry ONNX runtime and the
  Voyage clients together on three targets (CA-1).
- **Licensing**: N/A; no new dependency.
- **Deployment model**: local engines only change posture; the managed engine is
  unchanged, and the re-embed job runs in both (in the managed engine it moves
  Voyage-to-Voyage only, when a profile changes).
- **IDE compatibility**: N/A.
- **Incremental adoption**: each phase ships alone. Phase 1 is client-only and
  safe against today's engines. A dual engine without Phase 4 still serves both
  families; only single-name reads stay on one sibling.
- **Secret/credential lifecycle**: the key keeps its one source (the credential
  chain, then `NX_VOYAGE_API_KEY` at spawn); what changes is that it is passed
  whenever present. Rotation is a service restart, as today. Open Question 2
  covers the ambient-key consequence.
- **Memory management**: a keyed engine with bge files holds the bge model
  (about 416 MB on disk, `Bge768Embedder.java:243-250`) in addition to the Voyage
  clients. A re-embed batch holds at most one batch of chunk text and vectors, or
  one large document's; the bound is the batch rule above.

### Proportionality

The document is long because it carries two designs: the dual engine (Phases 1
to 5) and the re-embed (Phase 6). They could be two RDRs; they stay together
because the re-embed's correctness depends on the dual engine's reads and
profile, and splitting would duplicate the context section. The Key Discoveries
list is the place to trim before locking: once the gate passes, the bullets that
only correct the 2026-09-16 draft can move to Revision History.

## References

- T2 `nexus/voyage-local-mode-investigation-2026-09-15` [25872]
- T2 `nexus_rdr/210-research-1` to `-9`; `nexus_rdr/210-drift-audit-2026-10-17`;
  `nexus_rdr/210-open-questions-sam-2026-10-17`
- Beads nexus-ddmfg, nexus-umm29, nexus-r5f3c, nexus-35ok4, nexus-vply6
- `service/src/main/java/dev/nexus/service/Main.java:232-350`
- `service/src/main/java/dev/nexus/service/vectors/EmbedderRouter.java`
- `service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java:597-935`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java:5182-5260, 7873-7990, 8280, 8492, 9018-9165`
- `service/src/main/java/dev/nexus/service/db/CombinedWriteService.java`
- `service/src/main/java/dev/nexus/service/ChunkReaper.java`
- `service/src/test/java/dev/nexus/service/OwnershipGuardCoverageScan.java`
- `src/nexus/daemon/storage_service_daemon.py:1479-1590`
- `src/nexus/commands/init.py:54-96, 737-765`
- `src/nexus/corpus.py:1213-1500, 2085-2135, 2373-2451`
- `src/nexus/commands/collection.py:601-760, 1436-1745`
- `src/nexus/db/http_vector_client.py:1050-1262, 3275-3330, 6040-6052`
- `tests/e2e/local-service-gate.sh`, `tests/e2e/release-battery.sh`
- `docs/cli-reference.md` "Local mode with Voyage"

## Revision History

- 2026-09-15: created from Sam's requirement and the investigation above.
- 2026-09-16: Gap 7 and the re-embed contract added on Sam's requirement ("if we
  have Voyage and re-embed, everything gets re-embedded to the new defaults;
  idempotent; batched, managed, monitored, logged"): an engine job that copies
  live chunks into the profile-model sibling with server-side embedding, skips
  chashes already present, cuts over through the existing cross-model rename
  and supersede, with a job table, status route, doctor rows, per-batch events
  and a cost estimate. Phase 5, Open Questions 4 to 6, and six test scenarios.
- 2026-09-16: research pass on the re-embed section (T2 210-research-1 to -4):
  four claims verified against source, two numbers corrected (Voyage batch cap
  1,000 under a token budget; the 300-row cap is the client's convention), the
  enumeration and job-runner sentences rewritten to what the engine has.
- 2026-10-17: revised for the first gate against develop e34467fdb, from the
  drift audit (T2 `nexus_rdr/210-drift-audit-2026-10-17`) and Sam's answers to the
  six open questions (T2 `nexus_rdr/210-open-questions-sam-2026-10-17`). Cites
  moved to current lines; Gap 1 records the nexus-vply6 named error; Gaps 4 to 7
  narrowed or corrected (content-type fan-out exists; wording landed; the keyed
  script and `embed_migrate` deleted; the verb is `re-embed` at batch 100);
  Gaps 8 and 9 added (mode-string consumers, CA-3 refuted; client profile seam).
  The re-embed is redesigned as a per-document move through the RDR-223 combined
  write, with explicit source removal because the reaper's floor would refuse it.
  Answers folded in: profile from `local.embed_model`, one reranker per engine,
  keyed gate in the release battery only, user-started job with automatic
  cutover, a 10M-token default cap per run. Plan rewritten in six phases with a
  test per step; three new open questions (local `https` fetcher, ambient key and
  rerank, source deletion). Related RDRs 223, 225, 192, 227, 224 and 228 added.
  Research findings 210-research-5 to -9 cited; results still pending in the
  research pass are marked "pending 210-research-<topic>" (ca1, ca2-q2,
  ca3-census, egress-census, cutover-cascade-census, reembed-copy-options).
