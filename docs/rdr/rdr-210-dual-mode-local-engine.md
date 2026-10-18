---
title: "One Local Engine Serves Both Embedders: bge-768 and Voyage Collections Side by Side"
id: RDR-210
type: Architecture
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-09-15
accepted_date: 2026-10-18
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
(T2 `nexus_rdr/210-open-questions-sam-2026-10-17`). The research findings that
settle the draft's open technical questions are T2 `nexus_rdr/210-research-10` to
`-19` (four of them spikes on chas). Every code citation below was re-read in a
tree whose code is that of 42382fa3d, on develop 6cd90aab5 (drift audit: T2
`nexus_rdr/210-drift-audit-2026-10-17`): the first pass at 42382fa3d, the gate
round 1 fixes at ad64a703b (T2 `nexus_rdr/210-research-20`) and the fix-check
round at 4bc078be1 (T2 `nexus_rdr/210-research-21`). `git diff 42382fa3d..4bc078be1`
over `src`, `service` and `tests` is empty.

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
- **Sibling**: two collections whose registry rows carry the same
  `(content_type, owner_id)` and differ in `embedding_model`, for example
  `code__1-1__bge-base-en-v15-768__v1` and `code__1-1__voyage-code-3__v1`. The
  names are examples only. Siblings are found by the row tuple, never by
  rendering or matching a name (Sam, 2026-10-18, RDR-229 Q6): RDR-229 gives a new
  collection an opaque generated id, so a name match would not find a new sibling.
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
at all. Several client
decisions, including one that deletes data, read the engine's single mode string
and would misfire on an engine that holds both. Nothing moves an existing
collection onto a new model without re-indexing it from source.

### Enumerated gaps to close

#### Gap 1: One engine holds one embedder family per boot

`Main.java:243` reads the key once. When it is set, `Main.java:256-278` builds
Voyage-only routers (`:263-264`) and the Voyage reranker (`:273-276`); otherwise
`Main.java:279-342` builds bge-only routers (`:297`, `:321-326`) and the
cross-encoder reranker (`:334-335`). The Voyage router's dispatch table has no bge
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

#### Gap 2: The key does not reach the engine by default

The supervisor (`storage_service_daemon.py:1538-1595`) passes a configured key
only when `local.embed_model` names a Voyage model or records no choice at all
(the guard is `:1575`, the credential lookup `:1583`). That guard (nexus-r5f3c)
exists because of Gap 1: a key flipped the engine Voyage-only and broke the
user's bge collections.

The init half is fixed and on develop (nexus-ddmfg, c03352a2a..6cd90aab5):
`_provision_service_embedder_step` (`init.py:54`, called from
`provision_service_stack` at `init.py:787`) keeps a Voyage `local.embed_model`
(`init.py:100-102`) and writes bge only otherwise (`init.py:118`). The upgrade
ladder's provision leg (`upgrade_ladder/provisioning.py:293, 381-384`) reaches
the same function, so it is covered too. It is not scope here. The supervisor
half (the r5f3c guard) is.

#### Gap 3: The profile has no input for choosing between two available models

The engine seeds the embedding profile from its posture on every boot
(`Main.java:345-361`, calling `seedEmbeddingProfile` at
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
sibling only when the promoted name is missing (`corpus.py:1483-1499`). This is
pinned by `tests/test_rdr_109_phase2_dispatch.py:258`
(`test_read_path_prefers_voyage_over_bge_once_voyage_sibling_exists`).

This gap is narrower than the draft said. A bare content-type corpus
(`--corpus code`) already fans out to every live collection of that content type
(`resolve_corpus` stage 2, `corpus.py:2427-2452`), the search engine groups the
result by model (`search_engine.py:595-625`), and each group goes to the engine
as its own request. What still reads one sibling is every path that goes through
the single-name resolver: an owner-scoped corpus, `--collection`, and the store
and get paths.

The fix finds siblings by the row tuple. `GET /v1/catalog/collections/list`
filters on `content_type` and `lifecycle_state` today
(`CatalogHandler.java:2103-2130`, no `owner_id` filter), so the resolver asks for
`content_type=<ct>&lifecycle_state=live` and keeps the rows whose `owner_id`
equals the owner's; whether the engine route gains an `owner_id` filter instead
is decided at implementation (Phase 4 Step 1). It never splits, renders or
prefix-matches a collection name.

#### Gap 5: The engine's outbound calls have no guard, and one of them is not Voyage

RDR-157 and RDR-160 state that the local engine makes no outbound HTTP. Sam
decided on 2026-06-15 (nexus-umm29) that the Voyage embed and rerank call is a
sanctioned exception, bounded by a guard or test so a later change cannot add
other outbound calls under it. The wording landed in f7cae33ec (2026-09-15:
RDR-157, RDR-160, RDR-204, the privacy policy). The guard does not exist, and no
scan covers outbound networking today. The egress census (T2 210-research-10;
patterns: `java.net.http`, `URL.openConnection`/`openStream`,
`Socket`/`ServerSocket`/`SocketChannel`, `ProxySelector`, `DriverManager`,
`ProcessBuilder`/`exec`, DJL and ONNX runtime, `https` literals) finds eight
outbound paths in the engine:

- **Path 1.** `HttpsSchemeHandler` (`HttpsSchemeHandler.java:45-48`, redirects followed),
   registered unconditionally for `POST /v1/vectors/resolve`
   (`NexusService.java:648`, `:654`): fetches any caller-supplied `https` URI.
   Sam ruled it removed in every mode (nexus-3t0jv, c143a27ab, on develop).
- **Paths 2 to 4.** `VoyageEmbedder` (`:309`), `CceEmbedder` (`:359`) and `VoyageReranker`
   (`:152`), each to `api.voyageai.com` (URL constants `VoyageEmbedder.java:101`,
   `CceEmbedder.java:115`, `VoyageReranker.java:63`), built only when keyed.
- **Path 5.** `EgressProxy` (`EgressProxy.java:49`) routes 2 to 4 through an
   operator-configured proxy; it builds no client.
- **Paths 6 and 7.** DJL inside the tokenizer library (used by `Bge768Embedder`,
   `CrossEncoderReranker`, `OnnxEmbedder`): a call to the cloud instance-metadata
   address at every tokenizer build, and a native-library download on GPU hosts.
   Library code, not visible to a main-source scan (210-research-10; not re-read
   here). Both are closed by nexus-17jvq (2416ce891, on develop).
- **Path 8.** JDBC to `NX_DB_URL`: the pool, `PciBuilderSession.java:243`,
   `BackendReaper.java:122`, `PoolerModeCheck.java:131`.

No raw socket, `openConnection`, `openStream` or `ProcessBuilder` appears in main
source. Liquibase is 4.29.0 (`service/pom.xml:29`), older than its analytics
beacon; a bump to 4.30 or later would add an outbound path.

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
and `voyage` otherwise (`EmbedderRouter.java:168-169`), reported on `/version`
beside the served model set `embedding_models` (`VersionHandler.java:479-489`)
and on `/v1/status` (`StatusHandler.java:338`), and used in the 422 refusal text
(`EmbedderRouter.java:552`, `:588`). The census (T2 210-research-15, extending
-7) examined 29 site groups, about 70 line cites; 22 need a change and 7 do not.
No telemetry field carries the mode, and `conexus/` has no code consumer. The
groups that decide something, by consequence:

- **Deletes data.** `_active_embedding_dim` (`commands/collection.py:601-649`)
  turns the mode into ONE active dimension (1024 for `voyage`, `:638-639`).
  `_find_dimension_mismatched_collections` (`:664`) lists every collection whose
  name declares another dimension, `nx collection prune --yes` (`prune_cmd`,
  `:725`) deletes them, and the doctor's dimension-orphan row reuses the finder
  and recommends that prune (`health.py:8259-8311`). Under `voyage`, every served
  bge collection would be listed and deleted. It is already wrong today in one
  case: a keyless engine lists every Voyage collection as an orphan, although a
  key would serve it.
- **Memory and request shape.** `http_vector_client.py:1061` applies the ONNX
  memory cap (nexus-33hpq) to every collection when the mode is `onnx-local`;
  `:1175` turns the Voyage byte budget off on the same test; `:1250-1259`
  (`_serving_embedding_mode`) feeds both.
- **Ranking.** `search_engine.py:552-580` applies Voyage-calibrated thresholds
  only when the mode is `voyage`.
- **Memo and probe.** `http_vector_client.py:6049` accepts only the two known
  values; `:3277` and `:5777` carry the single value.
- **Write model.** The client picks the write model from `local.embed_model`
  (`local_embed_model_is_voyage`, `config.py:708`, read at `corpus.py:420-443`,
  `:546`, `:689`, `:1488`), the second one-family assumption.
- **Remedy and display text.** `errors.py:258-261`, `search_engine.py:1972-1974`,
  `mcp/core.py:3928`, `http_vector_client.py:1592`,
  `db/http_engine_status.py:134`, `commands/doctor.py:3745-3800`,
  `health.py:8406`.
- **Gates.** `tests/e2e/cloud-client-path-gate.sh:705` asserts the mode is one
  of the two values.
- **Engine.** `modeName`, the refusal text (`EmbedderRouter.java:507-590`), and
  `local_embed_activity`, wired only in the keyless branch (`Main.java:320`).

A pattern that already works for two families exists:
`verify_voyage_capability` (`upgrade_ladder/provisioning.py:171-218`) reads
`embedding_models`, not the mode.

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
| RDR-223 | Precedent and constraint | Its ownership guard, enforced on local engines (`storage_service_daemon.py:1494`), refuses any chunk write whose chash has no live manifest row in the target. It deleted staging. Its combined write (`CombinedWriteService`, `CatalogRepository.writeManifestMany`) writes a document's chunks and manifest rows in one transaction; the re-embed (Phase 6) moves documents through that primitive and needs no exception to the guard. |
| RDR-225 | Constraint | Chunks are partitioned by model, and every seeded model has a partition (vectors-030 `:703-713`). The manifest-to-chunk foreign key includes the model (vectors-030 `:1017-1021`). It states (rdr-225:554): "Cross-model migration registers a new collection under the target model. There is no in-place re-embed, and none is added." The re-embed here copies into a new sibling and so agrees. |
| RDR-192 | Constraint | live(c) decides when moved chunks become visible, and the reaper's grace and floor decide what happens to the source's chunks after the move (Technical Design, re-embed section). |
| RDR-227 | Adjacent (closed) | Its reconciler builds a per-collection index for any live, unsuperseded collection above its row threshold and drops it when `superseded_by` is set (rdr-227:366-370, :418-428). A re-embed target therefore gets its index while the job runs, and the source's index goes at supersede. No change needed. |
| RDR-224 | Adjacent (closed) | Native Windows runs bge on the Windows ONNX runtime (rdr-224:289, :322), with the ORT init gate and temp sweep at boot (`Main.java:67`, `:71`). The dual boot must hold on Windows; the coexistence spike (Phase 1 Step 1) includes the win-x64 image. |
| RDR-229 | Constraint (draft) | Collections become opaque ids with the attributes in the row (Sam, 2026-10-18, Q2, Q3, Q6). This RDR finds siblings and the re-embed target by the row tuple `(content_type, owner_id)`, never by a name, so it stands whichever name shape ships. |
| RDR-228 | Adjacent (closed) | Plugin setup runs `nx init --yes` unattended when no service is provisioned, and reruns an unfinished one (rdr-228:677-683, :1600-1601). The init keep-voyage fix, now on develop, covers that caller too; nothing here changes it. |

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
  the cross-encoder reranker (`Main.java:314-335`). Its constructor checks the
  model and tokenizer files and a size floor, and throws `IllegalStateException`
  before touching ONNX runtime (`Bge768Embedder.java:226-255`).
- Voyage: `VoyageEmbedder`, `CceEmbedder`, `VoyageReranker`. Batches up to 1,000
  inputs per request under a token budget (`VoyageEmbedder.java:124`).
- The cloud engine has no ONNX model on disk. Constructing an ONNX session on a
  missing file segfaults rather than throws (conexus-qcn, comment at
  `Main.java:257-262`), so the cloud path must never construct one.
- Wire changes: a change visible on the wire records an entry in
  `docs/wire-contract-pending.md`, checked by `scripts/check_wire_contract_pairing.py`.

## Research Findings

### Investigation

Source reading at a9551ee21 (T2 [25872]) and 9172348a0 (T2
`nexus_rdr/210-research-1` to `-4`), then a 70-row re-verification of every claim
at e34467fdb (T2 `nexus_rdr/210-drift-audit-2026-10-17`), recorded as research
findings `nexus_rdr/210-research-5` (copy against the ownership guard), `-6`
(egress against `HttpsSchemeHandler`), `-7` (mode-string sites, CA-3), `-8`
(cutover does not cascade collection-keyed state) and `-9` (the drift list).
A second pass settled the questions the first left open:

| Record | Method | Subject | Result |
| --- | --- | --- | --- |
| 210-research-10 | Source search | Egress census | Eight outbound paths (Gap 5); the guard needs a source scan and a runtime witness |
| 210-research-11 | Source search | CA-1, source half | Both families are in today's binary; no router can hold both |
| 210-research-12 | Source search | Re-embed options | A guard-exempt copy then cross-model rename is possible; per-document manifests are one-collection |
| 210-research-13 | Source search | CA-2, which surfaces rerank | Rerank runs on two surfaces only; the rest merge by distance or `hybrid_score` |
| 210-research-14 | Source search | Cutover cascade | The cross-model rename repoints 2 of the collection-scoped tables |
| 210-research-15 | Source search | CA-3 census | 29 site groups, 22 to change; the wire value must be `dual` |
| 210-research-16 | Spike | Voyage rerank across requests | Comparable (Kendall tau 0.991-0.995 against a noise floor of 0.9955) |
| 210-research-17 | Spike | bge calibration | Factor about 1.38, not the 0.818 default; per-collection thresholds required |
| 210-research-18 | Spike, 17 of 17 | Copy versus per-document move | The move holds under the guard; the copy has a race; satellite tables move with neither |
| 210-research-19 | Spike, linux-amd64 | CA-1 in JVM and native image | One process holds both families; memory within noise of bge-only |

The spikes ran on local branches (`spike/nexus_911-rdr210-copy-cutover` and
`spike/nexus_911-rdr210-dual-native`), not on develop.
The cited lines were re-read at the Phase 1 Step 1 base (develop 0b2ec059b, after the
rebase that moved them; Provenance names the earlier passes); where a record and the code disagree,
the code is cited and the disagreement is noted.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Engine router and boot (`Main`, `EmbedderRouter`) | Yes | Posture from key presence; no router holds bge and Voyage except the test-only `EmbedderRouter(OnnxEmbedder, String, String)` (`:111`), typed to the retired MiniLM embedder. |
| Catalog write path (`CatalogRepository`, `CombinedWriteService`) | Yes | Per-document transaction writes chunks then replaces the document's manifest rows (`CatalogRepository.java:5182-5260`). |
| Reaper (`ChunkReaper`, vectors-021, vectors-030) | Yes | 30-day grace; move floor 0.25 from 100 chunks. |
| onnxruntime 1.20.0 with GraalVM native image, alongside the Voyage HTTP clients | Spike (210-research-19) | One native process holds both on linux-amd64; other platforms are Phase 1 Step 1. |
| Voyage API | Spike for rerank (210-research-16); RDR-195 verified the input-count cap via Context7 | Rerank scores are pointwise and comparable across requests. |

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
  (`:38-39`) and admits only a caller with "a reason it cannot write an ownerless
  chunk" (`:30-31`). SQL functions that insert chunks are governed separately by
  `_CHUNK_INSERTER_ALLOWLIST` (`tests/test_changelog_chunk_inserter_lint.py:49`).
  A guard-exempt copy is therefore possible only by an entry the rule's own text
  forbids (210-research-12 proposed one; the spike's stub caller turned
  `OwnerlessWriteRefusalTest` red, 210-research-18). *Source: T2 210-research-5.*
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
  reapable after 30 days and is refused every pass; the spike confirmed the
  refusal at floor 0.25 (210-research-18). Orphan stamps are written only for
  chunks last written more than an hour ago (the trigger's guard,
  vectors-030 `:3024`, `:3040`, `:3044`), so a source chunk written within the
  hour gets no stamp when its document moves; its reap clock then runs from
  `last_written_at` (`chunk_is_reapable` takes the later of the two,
  vectors-021 `:26`, `:432`), at most an hour earlier.
- **✅ Verified** (source search, then spike) — Collection-scoped state, census
  complete. `COLLECTION_SCOPED_TABLES` registers 14 tables
  (`CatalogRepository.java:8804-8823`); `chunk_orphaned_at` is a documented
  exclusion carried by its chunk foreign key's cascade
  (`tests/catalog/test_collection_scoped_tables_schema_parity.py:139-152`), and
  that gate asks `information_schema` for every collection column, so the 15 are
  all of them. The same-model rename re-homes all 14; the cross-model branch of
  `renameCollectionTxn` (`:9122-9168`) updates only `catalog_documents` and
  `catalog_document_chunks`, and its own comment (`:9149-9152`) says topic
  assignments and orphan stamps stay. The spike found the same of the
  per-document move: aspects, highlights, the aspect queue and topic rows all
  still name the source after it (210-research-18). `deleteCollection(source)`
  then deletes them by collection (`CatalogRepository.java:8369` topic
  assignments, `:8430-8445` topics, meta and centroids, `:8448-8450` aspects,
  highlights and queue). The client cascade `remap_collection_references`
  (`collection_rename.py:174`) has no production caller (210-research-14).
  *Source: T2 210-research-8, -14, -18.*
- **✅ Verified** (source search) — How each satellite joins its document.
  `document_aspects` and `aspect_extraction_queue` are unique on (tenant,
  collection, source_path) and carry a nullable `doc_id` tumbler
  (`aspects-001-baseline.xml:56-75`, `:153-167`; nullable from
  `fk-001-catalog-cross-store.xml:148-149`, `:239-240`); `document_highlights` is
  unique on (tenant, doc_id) with a nullable `collection`
  (`aspects-001-baseline.xml:111-121`). All three cascade from
  `catalog_documents` on `doc_id` (`fk-001-catalog-cross-store.xml:158-162`,
  `:199-203`, `:249-253`). `topic_assignments.doc_id` is a chunk chash; the row
  references the source chunk on (tenant, source_collection, doc_id,
  embedding_model) with ON DELETE CASCADE (vectors-030 `:1032-1036`) and its
  topic on (tenant, topic_id) with ON DELETE CASCADE
  (`taxonomy-014-topics-tenant-unique.xml:270-273`).
- **✅ Verified** (source search) — `/version` already reports the served model
  set as `embedding_models` beside `embedding_mode` (`VersionHandler.java:479-489`).
- **✅ Verified** (git grep) — No production client calls `POST /v1/vectors/resolve`:
  its one client method, `HttpVectorClient.resolve_content`
  (`http_vector_client.py:4155`), has no caller outside tests, and the client has
  its own `https` reader (`aspect_readers.py:748`).
- **✅ Verified** (source search) — Batch numbers: Voyage batches are planned
  under a token budget up to 1,000 inputs (`VoyageEmbedder.java:124`); the
  existing re-embed verb sends 100 chunks per request so one fits the managed
  edge's 55-second limit (`commands/collection.py:1438-1444`). *Source: T2
  210-research-3.*
- **✅ Verified** (source search) — The engine runs periodic work on one scheduler
  registered at construction (`NexusService.java:686`, `:700`, reaper at `:729`);
  there is no persisted job table. *Source: T2 210-research-4.*
- **✅ Verified** (source search) — An engine-internal caller has no request
  deadline. The deadline is a thread-local the HTTP filter sets per request
  (`RequestContext.java:62`, `setDeadlineNanos` `:82-83`), and the embed path's
  probe reads "never expires" when none is set (`RequestDeadlineProbe.java:27`,
  `:33-36`). The request default is 300 s, capped at 900 s
  (`RequestDeadline.java:64`, `:83`). A job on the scheduler sets its own.
- **✅ Verified** (source search) — A precondition failure inside the batched
  write has no typed reason today. The per-document catch classifies SQL errors
  and passes through only `IllegalArgumentException`,
  `TombstonedDocumentException` and `DocumentNotFoundException`; anything else
  becomes "internal error (ClassName)" (`failureDetail`,
  `CatalogRepository.java:6372-6414`, allowlist `:6406-6407`). The spike's
  `MovePreconditionFailed` came back that way (210-research-18).
- **✅ Verified** (source search) — Which surfaces rerank (210-research-13, cites
  re-read). Collections are grouped by model (`search_engine.py:595-624`), one
  engine request per group, reranked inside that request
  (`RerankStage.java:127`). `nx search` reranks only with more than one target
  collection or `--lexical` (`search_cmd.py:462-466`); MCP `search` only with
  `lexical=true` (`mcp/core.py:3049`). On both, rows with a rerank score lead and
  rows of a degraded group follow them (`search_cmd.py:658-680`,
  `mcp/core.py:3089-3093`), with a warning (`search_cmd.py:646-657`,
  `mcp/core.py:3094-3097`), so a rate limit on one group sinks that whole
  family. MCP `query`'s plain path orders by `hybrid_score`
  (`apply_ranking_boosts`, `mcp/core.py:5111-5116`), a min-max over calibrated
  distance whose factor table knows only the two Voyage models
  (`scoring.py:76-79`); any other model gets 0.45/0.55 = 0.818 (`:81`, `:84-104`).
  The catalog-routed tools (`search_metadata_scoped`, `search_graph_hop`, the
  catalog branch of `query`) merge groups by raw distance
  (`_grouped_combined_query`, `mcp/core.py:3881-3946`), and `nx_answer` retrieves
  through them. Thresholds are keyed by name prefix (`search_engine.py:541-549`)
  and gated on the engine mode (`:552-580`), though the request already carries
  them per collection (`:1637-1646`).
- **✅ Verified** (spike, 210-research-16) — Voyage rerank scores are comparable
  across requests. Split-merge against one request over 60-chunk pools, 20
  known-item queries, rerank-2.5 with the engine's request shape: Kendall tau
  0.991-0.995 (min 0.980), top-10 overlap 0.99-1.0, mean absolute score
  difference 3.2e-4 to 4.5e-4, against a noise floor (one request against itself
  shuffled) of tau 0.9955 and 3.4e-4. Top-1 identical 20 of 20. Scoring is
  pointwise. Not measured: through the engine route, under a 429, other corpora.
- **✅ Verified** (spike, 210-research-17) — bge-768's calibration factor is about
  1.38 (bootstrap 1.36-1.48), from the method behind the existing factors
  (0.45 over the model's best-F1 threshold; the method reproduces
  voyage-context-3's recorded 0.65 as 0.641, and gives bge 0.325). With the 0.818
  default, an irrelevant chunk of the other family outranked the best relevant
  chunk in 12 of 20 queries one way (9 of 10 inversions sink a relevant Voyage
  chunk) and 5 of 20 the other; factor 1.0 gave 7 and 2; factors 1.15 to 1.30
  gave 3 and 1. The Voyage 0.65 threshold applied to bge distances admits every
  bge result (off-topic bge top-10 minimum distance 0.369). Not measured:
  voyage-code-3 and code collections, other corpora; n = 20.
- **✅ Verified** (spike, 210-research-18) — The per-document move works under
  the guard with no exemption: the composite foreign key held, the ownership
  guard, its coverage scan and the chunk-inserter lint stayed green; mid-move the
  source held 8 and the target 8 of 16 documents with no overlap; a document a
  client changed between read and move was skipped and the next pass moved it;
  `store_put` notes moved. One search request naming both siblings is refused
  (mixed dimensions 768 and 384, `requireHomogeneousModel`,
  `PgVectorRepository.java:2399`). Median throughput 130.7 chunks/s (123.2 to
  146.0) against 177.9 (139.8 to 192.1) for the guard-exempt copy, about 25%
  slower, on 500 chunks with a MiniLM target that dominates the time.
- **✅ Verified** (spike, 210-research-19, linux-amd64, GraalVM 25.0.3) — One
  engine process, JVM and native image, builds bge-768, the cross-encoder, a dual
  router, the three Voyage embedders and the Voyage reranker (dummy key, no
  Voyage call), serves bge embeds and a cross-encoder rerank, and exits 143 on
  SIGTERM with no SEGV, also during bge initialisation (exit deferred 1.6 to
  3.2 s by `OrtInitGate`). Native peak VmHWM, median of 3: bge-only 1796 MB, dual
  1901 MB after one 64-chunk embed and one 60-candidate rerank, within
  run-to-run noise (bge-only range 1688 to 1867). A keyed boot with no bge files
  skips bge, never loads ONNX runtime, and boots at 161 MB native.
- **Documented**: posture is chosen once, from key presence (`Main.java:243, 256`).
- **Documented**: the profile is rewritten on every boot from the posture
  (`EmbedderRouter.java:264-272`, `:356-378`).
- **Documented**: a request that names collections of two models is refused by
  the engine (`requireHomogeneousModel`, used at `PgVectorRepository.java:2399` in
  `searchPerCollection`), and the client already splits a search per model
  (`search_engine.py:595-625`).

### Critical Assumptions

- [x] **CA-1.** A bge-768 embedder, the cross-encoder, and the Voyage embedders
  and reranker can be built and served in one engine process without conflict,
  and the bge files' presence can be checked without creating an ONNX session.
  — **Status**: Verified for linux-amd64, JVM and native image (T2
  210-research-19); the presence half by source (the constructor's file and size
  checks throw before ONNX runtime is touched, `Bge768Embedder.java:226-255`).
  Unmeasured, and checked in Phase 1 Step 1 with pass/fail criteria: macOS arm64
  and Windows x64, Voyage serving in the dual process, peak memory under
  concurrent load, and nexus-8ool4's native-library extraction on keyed boots.
  — **Method**: Spike.
- [x] **CA-2.** A merged result list from a bge-768 collection and a Voyage
  collection ranks soundly. — **Status**: Verified on rerank-on surfaces of a
  Voyage-profile engine, because Voyage rerank scores are comparable across the
  per-model requests (210-research-16). Refuted for rerank-off surfaces as the
  code stands: MCP `query`, the catalog-routed tools and `nx_answer` merge by
  distance or `hybrid_score`, and bge's default factor inverts results in 12 of
  20 queries (210-research-13, -17). The design adds a measured bge factor and
  per-collection thresholds (Technical Design 12). Inferred, not measured: the
  cross-encoder's scores on a bge-profile engine are comparable across requests
  too (raw logits per document, no per-batch normalisation,
  `CrossEncoderReranker.java:32-33`, `:304-307`); Phase 4 Step 3 measures it.
  — **Method**: Source Search, then Spike.
- [x] **CA-3.** No other code path assumes "keyed means no local embedder".
  — **Status**: Refuted, census complete (T2 210-research-7, -15): 22 of 29 site
  groups change (Gap 8). — **Method**: Source Search. The design reports a new
  mode value `dual`, makes `embedding_models` the authority for every
  per-collection decision (Technical Design 10), and ships the client half
  (Phase 1) no later than the client release that first pins a dual engine
  (Phase 3; Cross-Cutting Concerns, incremental adoption).
- [x] **CA-4.** Writing a document's chunks into the target collection through
  the combined write moves the document whole: its manifest rows leave the source
  in the same transaction that writes the target's chunks, so no chunk is
  ownerless at any commit. — **Status**: Verified by source
  (`CatalogRepository.java:5182-5260`, manifest key
  `catalog-001-baseline.xml:181`) and by spike (210-research-18); it is the
  re-embed mechanism. It does not move the document's satellite rows; the design
  adds that (re-embed section). — **Method**: Source Search, then Spike.
- [x] **CA-5.** An engine-internal caller of `CombinedWriteService` runs without
  a request deadline and outside the edge's 55-second limit, so its batch bound is
  the job's own. — **Status**: Verified (source search): the deadline is a
  per-request thread-local and reads "never expires" when unset (Key
  Discoveries). The job therefore sets a per-batch deadline itself. — **Method**:
  Source Search.

## Proposed Solution

### Approach

One router per input type holds every embedder the machine can run: bge-768 when
its files are on disk, Voyage when the key is present. Dispatch stays by the
collection row's model, so each collection is embedded by the model its vectors
were made with, and nothing the machine can serve is refused. The user's
`local.embed_model` becomes the profile input that picks the model for new
collections. The engine reports a third mode value, `dual`, and the client stops
making per-collection decisions from the engine-wide mode. The supervisor passes
the key whenever one is configured and the engine it spawns is dual-capable.
Client reads span every model sibling, and
results from two families merge by rerank score where a surface reranks and by
calibrated distance where it does not. A re-embed job moves a collection onto the
profile's model document by document, through the same atomic write the indexers
use, carrying the document's aspects and highlights with it.

### Technical Design

1. **Engine router.** Generalize the mixed constructor to take any local
   `Embedder` (the admission-wrapped bge) and a nullable key, and delete the
   MiniLM-typed form. Main builds bge when a non-throwing presence probe (the same
   file and size checks the constructor makes) finds the files, and the Voyage
   embedders when the key is set. Neither present is a boot failure naming both
   remedies. A truncated or wrong-size model stays a loud boot failure, not
   "absent". The cloud container has no bge files, so it boots Voyage-only
   exactly as today, with no ONNX session.
2. **Profile policy.** One NEW client function, `local_embed_profile()` in
   `config.py`, decides the profile: `voyage` when `local.embed_model` names a
   Voyage model, or when no choice is recorded and `config.yml` holds a key;
   `bge` otherwise. `local_embed_model_is_voyage()` keeps its choice-only meaning,
   which `init.py:100` relies on for its "Kept:" line and which seven test files
   reference (`grep -l` over `tests`; author's decision). The new function
   reads the key from the persisted file only, as `persisted_credentials` does
   (`config.py:1342`; "It consults NO
   environment variable", `:1354`), not through `get_credential`, whose
   environment leg comes first ("Precedence: environment variable >
   ``~/.config/nexus/config.yml``", `config.py:1325`) and so can differ between
   the supervisor's process and a client's (author's decision). Client and
   supervisor then read one input from one config dir; the foreground supervisor
   exports `NEXUS_CONFIG_DIR` "so every config read below (local.embed_model, the
   Voyage key handed to the engine) and the engine itself use this dir"
   (`commands/daemon.py:966-969`). An env-only key still reaches the engine
   (Technical Design 4) and serves Voyage collections, but does not choose the
   profile: with no choice recorded the profile is `bge`, and a user who wants
   Voyage for new collections records `local.embed_model` or persists the key
   (`nx config set voyage_api_key --stdin`). That keeps Sam's Q1 rule (unset keeps
   keyed means Voyage; 2026-10-17) for a persisted key; narrowing "keyed" to a
   persisted key is Sam's ruling (2026-10-17, gate round 2). It closes a split:
   `local_embed_model_is_voyage()` is False when no choice is recorded
   (`config.py:725-726`), so today a no-choice client with a key mints bge names
   while a keyed engine seeds the Voyage profile. The supervisor always passes
   `NX_EMBED_PROFILE` from `local_embed_profile()`, and the client's write model
   reads the same function (Technical Design 10). An engine started with the
   variable unset (not by the supervisor) keeps keyed means Voyage. The engine
   seeds the profile from the variable, which needs a change beyond reading it:
   `contentTypeModelTokens` takes every token from a built embedder
   (`EmbedderRouter.java:228`: "`(voyageCodeEmbedder == null) ?
   localEmbedder.modelToken() : cceEmbedder.modelToken()`"), so a `voyage`
   profile on an engine with no key would seed bge tokens. The seed maps each
   profile to its per-content-type tokens by constant instead (Phase 2 Step 3).
   On a choice it cannot serve (`voyage` with no key, `bge` with no bge files)
   the engine boots degraded, not refused (Sam, 2026-10-17, gate round 1: it
   boots with the family it can serve, shows a loud banner and an `nx doctor`
   row, and 422s only the unservable family's collections, as a keyless engine
   does to Voyage collections today). The author's details: it still seeds the
   chosen profile, the banner names the missing piece, and the doctor row reads
   `/version`'s `embedding_models` against the profile's family. Neither family
   present stays a boot failure (Technical Design 1).
3. **Rerank.** One reranker per engine, chosen by the profile (Sam,
   2026-10-17, Q2 and its refinement): an engine whose profile names Voyage
   reranks every search with Voyage, including bge-only results; an engine whose
   profile is bge reranks with the cross-encoder even when keyed, and uses the
   key only to serve existing Voyage collections. A keyless engine uses the
   cross-encoder. Under a degraded boot (Technical Design 2) the reranker still
   follows the profile: a `voyage` profile with no key reranks with the
   cross-encoder, and a `bge` profile never sends text to Voyage, with or without
   the bge files (Sam, 2026-10-17, gate round 2). The engine builds the
   cross-encoder whether or not it built the bge embedder: the cross-encoder
   takes no embedder argument (`Main.java:334-335`: "new
   dev.nexus.service.vectors.CrossEncoderReranker()"), its construction touches
   no I/O (`CrossEncoderReranker.java:38-46`: "construction touches no I/O"), and
   its files sit apart from the bge files (`CrossEncoderReranker.java:66`:
   "/ms-marco-minilm-l6-v2/onnx/model.onnx"). Keyless with no bge files is
   Technical Design 1's boot failure. A cross-encoder whose own model files are
   missing, or that cannot load, skips the rerank with a warning, as today ("a
   not-yet-provisioned model degrades the fused stage LOUD per request instead
   of failing boot", `Main.java:328-330`). The admission gate the cross-encoder
   shares is built at `Main.java:314` after the bge embedder; that it needs no
   bge embedder is inferred, not read, and Phase 2 Step 4's test pins it.
   Nothing is sent to Voyage for a user who did not choose it, and a `bge`-profile
   engine sends no rerank text to Voyage.
4. **Supervisor.** Pass the configured key regardless of `local.embed_model`
   when the engine binary the supervisor is about to spawn is at least the first
   dual tag, read from its install receipt (`read_installed_provenance`,
   `binary_lifecycle.py:86-89`, "Reads the sidecar written by ``install_binary``
   (version, tag, sha256"; the tag is written at `binary_install.py:870`), which
   the supervisor does not read today. Below that tag, or with no receipt, the
   nexus-r5f3c rule (`storage_service_daemon.py:1554-1582`) stands, because
   against an older engine the key still flips it Voyage-only (author's
   decision; inferred, not read: a `NEXUS_SERVICE_JAR` launch has no receipt).
   The pin alone does not hold this: raising `REQUIRED_ENGINE_VERSION`
   (`engine_version.py:667`, `(0, 1, 160)` today) moves fresh installs, whose
   `PINNED_SERVICE_TAG` is "DERIVED from" it (`binary_install.py:106`), and
   existing boxes only at their next "convergence on an existing box"
   (`engine_version.py:56`), so between a client upgrade and that convergence
   the supervisor may spawn an older binary. The change still lands in the client
   release that raises `REQUIRED_ENGINE_VERSION` to the Phase 2 engine tag, never
   before; the local supervisor runs "the engine this client was built and gated
   with" (`storage_service_daemon.py:1488`). That constant is also the managed
   floor ("Only the managed-cloud handshake ... still reads this as a floor
   (``deployed >= this``)", `engine_version.py:57-59`; a lower deployed version
   raises `ManagedServiceIncompatible`, `managed_endpoint.py:244`), so the dual
   tag is deployed to the cloud engine before that client release ships.
5. **init and the ladder.** Landed on develop (nexus-ddmfg, Gap 2); not scope here.
6. **Client reads.** The single-name resolver returns every live sibling of the
   owner's corpus, not one, found by the row tuple `(content_type, owner_id)`
   through the catalog list route's filters (Gap 4), never by a name; callers that need one name for a write keep the write
   resolver. Searches already split per model and merge client-side.
7. **Egress allowlist and guard (nexus-umm29).** The carve-out is stated as an
   allowlist: *the local engine connects only to the Voyage API
   (`api.voyageai.com`) from `VoyageEmbedder`, `CceEmbedder` and
   `VoyageReranker`, and only when keyed, optionally through the operator's proxy
   (`EgressProxy`); and to its database at `NX_DB_URL` over JDBC. Nothing else.*
   That covers census paths 2 to 5 and 8 (Gap 5). Path 1 is removed in every mode
   (Sam, 2026-10-17; nexus-3t0jv, on develop). Paths 6 and 7 are closed by
   nexus-17jvq (on develop). Two tests hold it:
   - **Source census.** A main-source scan, shaped like
     `OwnershipGuardCoverageScan`: strip comments, then every outbound construct
     (`HttpClient.newBuilder`, `HttpClient.newHttpClient`, `openConnection(`,
     `openStream(`, `new Socket(`, `ServerSocket`, `SocketChannel.open`,
     `DriverManager.getConnection`, `ProcessBuilder`, `Runtime.getRuntime().exec`)
     must sit in a file on an allowlist that carries a reason per entry. The scan
     asserts it found at least as many sites as the allowlist names
     (non-vacuity), and that each Voyage class's URL constant names
     `api.voyageai.com`. It also reads `service/pom.xml` and fails on a Liquibase
     version of 4.30 or later unless its analytics are configured off. This
     extends nexus-17jvq's own scan, which is on develop.
   - **Runtime witness.** A child JVM that installs a `URLStreamHandlerFactory`
     refusing any non-`file`/`jar` stream, then builds the bge embedder and the
     cross-encoder (the tokenizer builds) and runs one embed and one rerank. It
     catches library code the source scan cannot see (paths 6 and 7). It does not
     see `java.net.http`, which is why the source census covers `HttpClient`, and
     the census scans only `service/src/main/java`, so `java.net.http`, raw
     sockets, DNS or JNI inside a dependency are covered by neither test. The
     witness runs on a JVM, while the engine ships as a GraalVM native image; the
     DJL tokenizers library does its own network I/O in native code that a URL
     factory cannot see. The guard is therefore a source-level check plus a
     tokenizer-build check, and the documents that cite it say that and no more.
     The follow-up gaps are bead nexus-3va7p.
8. **Java comments.** The stale router comments are corrected here, since they
   describe the router this RDR changes: `Main.java:37-41` (says an ONNX fallback
   exists when keyed), the class javadoc at `EmbedderRouter.java:33-47`, and the
   rest of T2 [25872] items 1-13 and 22. The non-engine sweep landed in f7cae33ec.
9. **Re-embed to the profile default.** See the next section.
10. **Mode string and per-collection decisions.** `modeName()` returns
    `onnx-local` (bge only), `voyage` (Voyage only) or `dual` (both). The two
    existing values keep their meaning, so a cloud engine and a keyless engine
    report what they report today. `embedding_models` (`VersionHandler.java:479-489`)
    becomes the authority for every per-collection decision, read the way
    `verify_voyage_capability` already reads it; `embedding_mode` is kept for
    display. The client changes cover the 22 site groups of Gap 8 and all land
    (Phase 1) in a client release no later than the one whose pin first names a
    dual engine (Phase 3), so no supervisor spawns an engine that can report
    `dual` for a client without them:
    - The chunk cap and the byte budget follow the target collection's model: a
      bge collection gets the ONNX memory cap, a Voyage collection the Voyage
      rules (`http_vector_client.py:1061`, `:1175`, `:1250-1259`).
    - Thresholds and cross-family ranking: Technical Design 12.
    - The probe memo accepts `dual` (`http_vector_client.py:6049`).
    - Remedy and display text names the family the collection needs, not the
      engine's mode (the remedy and display sites in Gap 8).
    - The gate assertion accepts `dual` (`cloud-client-path-gate.sh:705`).
    - The write model (`corpus.py:420-421`, `:545-546`, `:688-689`,
      `:1487-1488`, each reading `local_embed_model_is_voyage()`) reads
      `local_embed_profile()` (Technical Design 2) instead, so the names the client
      mints and the profile the supervisor passes come from one function and
      agree when no choice is recorded.
    - The dimension-orphan finder compares each collection's declared model with
      the set of models this install can serve: the engine's `embedding_models`
      plus every Voyage model, which a key would
      serve. A collection is listed only when its model is in neither. A served
      collection can then never be listed, and a Voyage collection on a keyless
      engine is reported as "needs the key", not as an orphan. When
      `embedding_models` is absent (an engine older than that field), the finder
      reports "cannot verify" and lists nothing, as it does for an unknown mode
      today.
    - An old client against a dual engine is safe for deletion: it does not
      recognise `dual`, so `_active_embedding_dim` answers unknown and prune
      lists nothing (any value but the two returns `(None, "unknown")`,
      `commands/collection.py:632-643`; prune skips, `:744-753`). Reporting
      `voyage` would make an old client delete every bge collection, and
      `onnx-local` would flag every Voyage one; that is why the value is new.
      It is not safe for the chunk cap: an old client treats anything but
      `onnx-local` as Voyage and sends Voyage-sized requests to bge collections
      (210-research-15). Inferred, not read: the engine's own per-group bound on
      padded token area (`Bge768Embedder.java:92-99`) holds whatever the request
      size, so the cost is latency, not the nexus-33hpq memory blowup. Phase 1
      Step 1 measures it; if it fails, the `dual` entry in the wire ledger
      carries a minimum client version and the engine refuses older clients.
11. **Profile seam (Gap 9).** The client's registration check skips the profile
    comparison when the row already exists with the same model; it reads the row
    first, as the implicit path already does for the superseded check
    (`corpus.py:2100-2112`, refusal `:2115-2119`). The engine needs no change: an
    existing row is never re-pointed by the profile
    (`CatalogRepository.java:7906-7930`). A new engine test pins that behaviour so
    the client fix cannot drift from it.
12. **Cross-family ranking (CA-2).** Each surface gets one rule:
    - *Rerank-on surfaces* (`nx search` with more than one collection or
      `--lexical`, MCP `search` with `lexical=true`) keep merging per-model groups
      by `rerank_score`. That is sound on a Voyage-profile engine
      (210-research-16) and inferred sound on a bge-profile one (CA-2). When some
      groups are reranked and others degrade, the merge stops sorting scored rows
      ahead of unscored ones (`search_cmd.py:658-680`, `mcp/core.py:3089-3093`)
      and orders every row by calibrated `hybrid_score` instead, keeping the
      warning; otherwise one rate-limited group sinks a whole family.
    - *Rerank-off surfaces* (MCP `query`'s plain path, the catalog-routed tools,
      and so `nx_answer`) merge by calibrated distance. `scoring.py`'s table gains
      `bge-base-en-v15-768` at the measured 0.325, a factor of 0.45/0.325 = 1.38
      (`scoring.py:76-79`); `_grouped_combined_query` (`mcp/core.py:3881-3946`)
      sorts across groups by distance times that factor, not raw distance. An
      unmeasured model keeps the default and logs that it did. The record
      reports inversion counts for 1.15 to 1.30 but not for the derived 1.38,
      so Phase 1 Step 5 counts them at 1.38 before the entry is set.
    - *Thresholds* are chosen per collection by its model, not by the engine's
      mode (`search_engine.py:552-580`): a Voyage collection gets its Voyage
      threshold on any engine, and a bge collection gets none, as on a keyless
      engine today, because the Voyage value admits every bge result and the
      measured bge threshold's absolute F1 (0.47) is too low to filter on
      (210-research-17). The request already carries thresholds per collection
      (`search_engine.py:1637-1646`).

### Re-embed to the profile default

The job answers one question, "does every live collection carry the vectors the
profile now asks for", and makes the answer yes. A collection is stale when its
row's `embedding_model` differs from the profile's model for its `content_type`.
Quarantine, dormant and disputed rows are never candidates; they drain through
their own lifecycle. A superseded row is not a candidate either, with one
exception (author's decision): a source that a `kept_tombstoned` job left
superseded is listed again, as that job's re-entry and not a new job, once its
tombstones are gone (state table). A `kept_by_flag` or `kept_residue` source is
never re-listed. The user starts it; a profile change prints a hint and
`nx doctor` reports the drift (Sam, Q4). The hint prints at three sites (the
sites are the author's decision). `nx config set` and `nx config unset` already
print a restart line for the two keys the engine reads at spawn
(`RESTART_HINT_KEYS = {"local.embed_model", "voyage_api_key"}`,
`commands/config_cmd.py:27`; printed at `:159-160` for a set and at `:184-185`
for an unset, "if removed and key in RESTART_HINT_KEYS"); when the change flips
`local_embed_profile()`'s answer (Technical Design 2), each prints a second
line naming `nx collection re-embed --all`. `nx daemon service start` prints the
same line when any live collection is off the profile, by the doctor row's
rule, in each of its three output branches: after the "Storage service running
on" line in the plain branch (`commands/daemon.py:1010`); on stderr in the
`--announce-stdout` branch (`:995`), whose stdout carries one JSON object
(`:997-1006`) and stays parseable; and from the supervisor once the engine is up
in the `--foreground` branch (`:965`), which prints no running line. Inferred,
not read: the cost of that catalog read at start, and where in
`run_storage_supervisor` the engine is known to be up.

**The move is per document, into the model sibling.** The target is the sibling
whose row carries the profile's model for the source's `(content_type, owner_id)`,
looked up by that tuple through the list route
(`GET /v1/catalog/collections/list?content_type=<ct>` with no `lifecycle_state`,
which returns superseded rows; `CatalogRepository.java:8213-8218`), keeping the
rows whose `owner_id` and `embedding_model` equal the source's owner and the
profile's model, never by rendering a name. `for_tuple` is not used here: it
returns no superseded row (`CatalogRepository.java`, `collectionForTuple`). The
target is registered live if absent, through the registration path that mints the name (an opaque generated
id once RDR-229 lands, the conformant name before); the profile is
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

The spike ran this mechanism under the guard in enforce mode with no exemption
(210-research-18, Key Discoveries), so it is the design. The alternatives are a
guard-exempt copy followed by a cross-model rename (Alternative 3, rejected for a
race) and re-index from source (Briefly Rejected). What the job's internal call
adds to today's `writeManifestMany`, as the spike's overloads did
(`CatalogRepository.writeManifestMany` with a move-from collection and expected
chashes, `CombinedWriteService.moveDocsCombined`; local branch, not on develop):
a precondition, `physical_collection` in the same transaction, the satellite
re-home below, and a typed refusal. The refusal is a new exception added to
`failureDetail`'s pass-through list (`CatalogRepository.java:6406-6407`) with the
reason `move_precondition_failed`; the job counts that reason as a skip and any
other as a failed batch. The move takes the sweep gate on the target only.

**When target chunks become readable.** At the commit of their document's move,
and not before. From that commit the document is in the target only; before it,
in the source only. One search request naming both siblings is refused for mixed
dimensions, so a reader queries each sibling in its own request and takes the
union, which the per-model grouping (`search_engine.py:595-624`) already does for
content-type corpora and Phase 4 extends to the single-name resolver. Such a
reader sees each live document exactly once throughout. A reader still on the
single-name resolver sees whichever sibling it picks, which is why Phase 4 comes
before Phase 6.

**Concurrent writes.** The precondition is that the document's current manifest
chashes and collection are what the job read. When they are not (a client
re-indexed or deleted the document since), the job skips it with the typed
reason and counts the skip; the next pass, a new user-started job (state table,
`done_with_skips`), sees it wherever the client put it. A
client write in flight that holds the index-run lock blocks that document's move,
which then skips it (the spike measured the block at over 5 s). A client
re-index in the dual world writes to the profile's sibling and moves the document
itself, through the same transaction. A client that writes an already-moved
document back into the source (an old client, or a `--collection` pin) moves its
manifest back, and the next pass moves it again. Inferred, not read or measured:
whether that client write also sets `physical_collection` back, since today it
is set by a separate call (`updateDocumentCollection`,
`CatalogRepository.java:3365-3370`); Phase 6 Step 1 tests it.

**Throughput.** The move is about 25% slower than the bulk copy of Alternative 3
(Key Discoveries), the price of one transaction per document. The spike's
absolute rates came from a MiniLM target and do not transfer to bge-768 or
Voyage; the Phase 6 gate records the real rate.

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

**Every collection-scoped table's disposition.** The 15 tables of the census
(Key Discoveries), plus the gate's other documented exclusions and the two job
tables this RDR adds. REPOINT rows move
to the target inside the document's move transaction; REBUILD rows are made
again for the target after the job; LEAVE rows stay naming the source.

| Table | Disposition | Reason |
| --- | --- | --- |
| `catalog_documents` | REPOINT `physical_collection` | New in the move transaction; today a separate call (`updateDocumentCollection`, `CatalogRepository.java:3365-3370`) |
| `catalog_document_chunks` | REPOINT (rows replaced) | The combined write already does it (`:5222-5236`) |
| `chunks` | Target written by the move; source LEAVE until the job deletes the source | The reaper floor refuses a fully reapable source |
| `chunk_orphaned_at` | LEAVE | Carried by the chunk foreign key's cascade; goes with the source's chunks |
| `document_aspects` | REPOINT | Otherwise deleted with the source (`:8448`) and re-extracted at LLM cost |
| `aspect_extraction_queue` | REPOINT | Otherwise deleted (`:8450`), or a pending item extracts against a superseded name |
| `document_highlights` | REPOINT | Otherwise deleted (`:8449`) and lost |
| `topic_assignments` | REBUILD | See below |
| `topics`, `taxonomy_meta`, `taxonomy_centroids` | REBUILD | Centroids are vectors in the source model's space |
| `relevance_log`, `search_telemetry`, `hook_failures`, `gc_audit` | LEAVE | History; `deleteCollection` deletes none of them (`CatalogRepository.java:8341-8468`). It adds `gc_audit` rows: "one `collection_delete_quarantine` `gc_audit` row per sibling that lost rows" (`:8268-8269`) |
| `pdf_pipeline`, `chash_remap` | LEAVE | The gate's documented exclusions (transient queue; permanent ledger, nexus-4nll0) |
| `reembed_jobs`, `reembed_job_moves` (new, Managed) | LEAVE | The job's own record, kept after the source delete |

The three REPOINT satellites are found per document: `document_highlights` by
`doc_id`; `document_aspects` and `aspect_extraction_queue` by `doc_id` equal to the
document's tumbler, or by an empty `doc_id` and a `source_path` equal to the
document's path (inferred, not read: that `source_path` is the path of the
document's `source_uri`; Phase 6 Step 1 confirms it). Where the target already
holds a row with the same unique key, the target row stands and the source row
is deleted. A parity test beside `COLLECTION_SCOPED_TABLES` lists a cross-model
disposition for every registered table and fails on a new table without one, as
the schema-parity gate does for the registry itself; `remap_collection_references`,
which has no production caller, is deleted.

**topic_assignments: rebuilt, not copied.** 210-research-14 recommended copying
the assignments by chash under the target's model. A copied row keeps its
`topic_id`, which names one of the source's topics, and that foreign key cascades
on delete (`taxonomy-014-topics-tenant-unique.xml:270-273`); the job's source
delete removes the source's topics (`CatalogRepository.java:8430`) and with them
every copy. Keeping the copies alive would mean re-homing the topics as well, and
their centroids cannot follow: they place vectors of the source model, not the
target's, so the re-homed taxonomy could not assign one new target chunk. The
spike found the topic rows unmoved under both mechanisms (210-research-18), so
nothing today carries them either. `nx taxonomy discover` on the target builds
topics, meta, centroids and assignments together in the target's space, and it is
what `nx index` already runs after indexing (`commands/index.py:674-690`). The
cost is a window: from a document's move until discover runs, topic-scoped search
and topic boosts miss it, which they would do anyway, since its source chunks are
invisible from the move. The verb runs discover for the target when it waits on a
job to `done` or `done_with_skips` (`--wait`); otherwise the `done` report names the command
(`taxonomy_cmd.py:694`) and the doctor's drift row says so until a discover has
run.

**Cutover.**
- *Source row.* When the source holds no live document, the job supersedes it
  (`supersedeCollection`, `CatalogRepository.java:8492`). The routing listing
  then leaves it out (`:8213-8217`), and RDR-227's reconciler drops its index.
- *Source chunks and the source delete.* Each moved document's source chunks are
  ownerless and invisible from its commit. The reaper will not drain them: a
  fully moved source is entirely reapable and the move floor refuses it every
  pass, which agrees with Sam's ruling that the job deletes it. The job removes
  it through the existing delete (`CatalogRepository.deleteCollection`, `:8280`)
  once it holds no document row at all, tombstoned ones included, since the
  delete removes documents by `physical_collection` (`:8465`). `--keep-source`
  opts out (Sam, 2026-10-17).
- *What that delete removes.* `deleteCollection(tenant, name)` calls the
  three-argument form with `keepQuarantine` false (`:8281`). On a fully moved
  source it removes:
  1. the source's chunks (`:8383`);
  2. the source's rows in its quarantine siblings, unregistering a sibling it
     empties (`:8386-8390`) and writing "one `collection_delete_quarantine`
     `gc_audit` row per sibling that lost rows" (`:8268-8269`). Decision: they
     die with the source. Passing
     `keepQuarantine` true would strand them, by the method's own comment
     ("Without this the sibling was stranded: nothing expires rows whose origin
     is gone", `:8386`). The plan reports their count; `--keep-source` keeps them
     restorable;
  3. topic assignments of OTHER collections that point at the source's topics,
     through the topic delete's cascade (`:8427-8430`: "a topic_assignments row
     from ANOTHER collection but pointing at one of THIS collection's topics can
     still exist here"), and the source topics' `topic_links` through the same
     cascade (`:8432`). Decision: accepted, since those topics sit in the source
     model's space; the plan and `--status` report both counts;
  4. the source's own topic assignments (`:8369`), topics, `taxonomy_meta` and
     centroids (`:8430`, `:8433`, `:8445`), being rebuilt for the target;
  5. any `document_aspects`, `document_highlights` or `aspect_extraction_queue`
     row still naming the source (`:8448-8450`), such as a doc-less aspect row
     whose `source_path` matched no document. Decision: the plan counts them,
     and the job refuses the delete while any remain unless the job row's
     `force_delete` is set (from `--force-delete` at start, Managed); the job
     then ends `done` with disposition `kept_residue` and the count in last
     error;
  6. the registry row (`:8467`). Manifest rows and document rows (`:8465`) and
     the `catalog_links` that cascade off documents (`:8458`) would go too; the
     parity check below requires that none remain.
- *Order inside cutover* (author's decision). Each step is safe to repeat, so a
  restart in `cutover` re-runs it from the first step: (1) if a live document
  remains in the source (a skip), the job ends `done_with_skips` and supersedes
  nothing; (2) supersede the source, a no-op when this job already superseded
  it; (3) if any document row remains (a tombstoned one), disposition
  `kept_tombstoned` and `done`, with no parity check and no delete; (4) if the
  row's `keep_source` is set, disposition `kept_by_flag` and `done`; (5) the
  parity check; (6) the residue check of item 5, against the row's
  `force_delete`; (7) the delete, skipped when the source's registry row is
  already gone, which a restart after a committed delete finds; disposition
  `deleted` and `done`. Inferred, not read: that `deleteCollection` commits its
  effects in one transaction, so a restart never finds half a delete.
- *Parity before the delete.* The delete is irreversible, so the job checks
  first, at step 5: no manifest row names the source; no live document row has
  `physical_collection` naming the source (step 3 has already sent a source
  with any tombstoned row to `kept_tombstoned`, so none reaches this check);
  and every document in this job's move record has `physical_collection`
  naming the target and manifest rows naming the target, with that count equal
  to the row's documents moved. If any check fails, the job deletes nothing,
  keeps the source superseded, and goes to `failed` with reason
  `parity_mismatch` and the three counts in last error. A document purged since
  its move makes the counts differ and fails the check, which errs toward
  keeping the source. The way out of `parity_mismatch` is `--abort` (author's
  decision): `--resume` re-runs the pass and cutover and fails the same check
  while its cause stands, and an abort leaves the source superseded and
  undeleted, for `nx collection delete` by hand.
- *Client caches.* The registration and collection-row caches already treat a
  superseded row as stale (`corpus.py:2090-2095`).
- *The 30-day reap grace* still protects a job that stops halfway: nothing it
  wrote is ownerless, and the source's documents not yet moved still own their
  chunks.

**Batched.** A batch is whole documents up to 100 chunks; a document with more
than 100 chunks is a batch of its own, because a document moves in one
transaction. 100 matches the existing verb's bound (`commands/collection.py:1444`),
whose reason (the managed edge's 55-second limit) does not reach an engine-internal
job (CA-5); the bound keeps one batch's embed short. The job sets each batch's
deadline itself (`RequestContext.setDeadlineNanos`, `RequestContext.java:82-83`)
to the request default of 300 s (`RequestDeadline.java:64`), so a stuck Voyage
call fails the batch instead of hanging the runner. Voyage requests are planned by
the engine's own batching up to 1,000 inputs.

**Managed.** The job is a row in a new engine table, `reembed_jobs` (tenant,
`source_collection`, `target_collection`, state, source disposition, document
cursor, documents moved, documents skipped, chunks embedded, chunks reused,
tokens billed, `token_cap`, `keep_source`, `force_delete`, started, updated,
finished, last error), created through Liquibase, with a per-job move record,
`reembed_job_moves` (job, document), written inside each document's move
transaction, which the cutover's parity check reads (inferred, not read: its
exact shape; Phase 6 Step 2 settles it). The cap and the two flags are columns
because the runner and a later re-entry act on them with no client (author's
decision). A runner on the engine's scheduler, the shape the tuple sweep and the
reaper already use (`NexusService.java:686, 729`), picks up `planned`, `running`
and `cutover` rows at boot, so a job survives a restart and a closed lid, and no
client is needed once it is started; a `cutover` row re-runs Cutover's ordered
steps from the first. Two jobs never run against one source: a row in
`planned`, `running`, `paused`, `cutover` or `failed` is the lock, held by a
partial unique index on (tenant, `source_collection`) over those five states
(author's decision). `failed` holds it because `--resume` takes a failed job
back to `running`. `--resume` and a `kept_tombstoned` re-entry from `done` both
write a lock state and so take the index's check: `--resume` keeps the lock its
own row already holds, and a re-entry while another job holds the source is
refused with 409 naming that job.

Both tables are tenant tables in the shared Liquibase chain, so the changeset
carries what every new tenant table does, although only a local engine runs the
job: ENABLE and FORCE row level security with the tenant isolation policy (the
shape of `tuples-001-baseline.xml:102`, "ALTER TABLE nexus.tuples FORCE ROW
LEVEL SECURITY;"); entries in the doctor's RLS canary (`_RLS_TENANT_TABLES`,
`health.py:3699`) and its fixture ("Must be kept in sync with
_RLS_TENANT_TABLES in health.py", `tests/test_health_service_checks.py:1062`);
the native-image jOOQ record count (`EXPECTED_RECORD_TYPES = 113`,
`JooqRecordReflectionFeatureTest.java:270`); the changelog family's Liquibase
schema test; and, because the collection columns match the schema-parity
gate's name pattern ("decided by a name pattern (``LIKE '%collection%'``)",
`tests/catalog/test_collection_scoped_tables_schema_parity.py:46-47`), a
`COLLECTION_SCOPED_TABLES` entry or a recorded exclusion with a reason, plus
the cross-model disposition above. Only a changeset that does row DML on a
FORCE-RLS table needs a seed in `SchemaUpgradeRehearsalIntegrationTest`'s
SEED-COVERAGE block (`:490`); creating the tables does none (inferred, not read:
that no backfill is wanted).

The job is local only (Sam, 2026-10-17, gate round 1). The runner starts and the
job routes accept requests only when the supervisor marks the engine local at
spawn, with a variable it sets the way it sets `NX_SERVICE_PARENT_DEATH_EXIT`
(`storage_service_daemon.py:1484`, read at `Main.java:434`); otherwise the routes
refuse, naming local mode. The verb refuses unless `is_local_mode()`
(`config.py:939`). Inferred, not read: that no managed deployment sets that
variable; Phase 6 Step 2 names it and checks.

| State | Meaning | Leaves to |
| --- | --- | --- |
| `planned` | Started; the runner has not picked it up | `running`; `aborted` |
| `running` | The runner walks the cursor over the source's live documents | `paused`; `failed`; `aborted`; `cutover` when a pass reaches the end |
| `paused` | Stopped by `--pause`; cursor kept | `running` (`--resume`); `aborted` |
| `failed` | A batch failed for a reason other than a skip, the profile changed, the token cap was reached, or the parity check refused the delete; cursor at the last committed document, reason in last error | `running` (`--resume`); `aborted` |
| `aborted` | Stopped by `--abort`; moved documents stay moved | none; a later run is a new job |
| `cutover` | The pass is complete; Cutover's ordered steps run | `done`; `done_with_skips`; `failed`; `aborted` (only before the delete step starts) |
| `done` | The source holds no live document and is superseded; disposition `deleted`, `kept_by_flag` (`--keep-source`), `kept_tombstoned`, `kept_residue` (satellite rows the delete would take, Cutover) or `revived` (a later job the other way took the source back as its target) | none, except that a later `--all` re-enters `running` or `cutover` for `kept_tombstoned` (below) |
| `done_with_skips` | The pass ended with skipped documents still live in the source; the source is neither superseded nor deleted | none; the source is still stale, so the next user-started run is a new job that moves the skips |

The lock states are `planned`, `running`, `paused`, `cutover` and `failed`
(Managed). The self-transitions are idempotent (author's decision): `--pause` on
a `paused` job, `--resume` on a `running` one and `--abort` on an `aborted` one
answer with the current state and change nothing, and Cutover's supersede is a
no-op on a source this job already superseded. An abort from `paused` or
`failed` leaves moved documents moved and the source as it stands; an abort in
`cutover` before the delete leaves the source superseded and undeleted, as
`--keep-source` would.

`done` guarantees that every document live in the source when the job reached it
is in the target and that the source serves nothing. `done_with_skips`
guarantees that only for the documents it moved; the skip count is in the row
and `--status`. `--wait` returns when the job reaches `done`, `done_with_skips`,
`aborted`, `failed` or `paused`, naming the state, and runs taxonomy discover for
the target on the first two (author's decision).

- *Tombstoned documents.* A source kept as `kept_tombstoned` is re-listed by
  `re-embed --all` once its tombstones are gone, which the author reads as one
  of two things: purge-trash has left it with no document row, and the job
  re-enters `cutover`; or a restore has made a document live in it (the routing
  listing leaves a superseded source out, so nothing else would serve that
  document), and the job re-enters `running`, whose pass moves it, then
  `cutover`. Either re-entry takes the lock check (Managed), and the entry into
  `running` takes the profile re-check below. Inferred, not read: that the job's
  read of a superseded source's manifest rows is not refused.
- *Profile change.* Every entry into `running` (boot pickup, `--resume`,
  a `kept_tombstoned` re-entry) re-checks that the target's model is the
  profile's model for the source's content type. If not, the job goes to
  `failed` with reason `profile_changed` and moves nothing more; moved
  documents stay in the target, which the dual engine serves, and the next
  `--all` plans by the new profile. A failed job keeps its source's lock until
  it is resumed or aborted, so `--all` names that job instead of planning its
  source.
- *A superseded target of the same tuple.* The target is registered through
  `upsertCollection`, whose update clears `superseded_by`
  (`CatalogRepository.java:8076-8077`; the client's comment at
  `corpus.py:2081-2082`: "The upsert below clears ``superseded_by``"). So the
  plan finds the target's row by the tuple first (above) and branches on that
  row. Absent, the job registers it; live, it uses it;
  superseded with `superseded_by` naming this job's source (an earlier re-embed
  the other way left it under `--keep-source` or `kept_tombstoned`), the job
  revives it through that upsert, the move reuses its chunks where the text
  matches, and the earlier job's disposition becomes `revived` so a later
  `--all` never deletes it; superseded by anything else, the plan refuses,
  naming the row and its successor.

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
- `--keep-source` and `--force-delete` apply to every job a start creates and
  are stored on each job row (`keep_source`, `force_delete`), with the token cap
  (`token_cap`), so the runner and a later re-entry honour them with no client
  (author's decision).
- `--status [JOB]`, `--pause JOB`, `--resume JOB` and `--abort JOB` act on job
  rows. Abort stops the runner; documents already moved stay moved, and a later
  run continues from what remains in the source. `--wait` returns on the states
  listed under the state table, naming the state.

**Routes.** `POST /v1/vectors/reembed/plan` (computes the plan, writes nothing),
`POST /v1/vectors/reembed/jobs` (start: writes `planned` rows; its body carries
`keep_source`, `force_delete` and the token cap for those rows),
`POST /v1/vectors/reembed/jobs/{id}/pause`, `/resume` and `/abort`, and
`GET /v1/vectors/reembed/status`. Each is an `[additive]` entry under
`## Unshipped` in `docs/wire-contract-pending.md`, whose rules say every
both-halves commit "MUST be listed under `## Unshipped`" and that a non-empty
section blocks a deploy unless every entry is `[additive]`. RDR-222's request
identity for mutating routes is a draft and not built; these routes do not need
it. A resent start finds the lock row and gets 409 naming the existing job, which
the verb reports as that job; pause, resume and abort set a state, and their
self-transitions are idempotent (state table), so a resend converges.

**Monitored.** `GET /v1/vectors/reembed/status` returns every job row for the
tenant. `nx doctor` extends the existing off-profile row (`health.py:8471-8493`,
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
The cap is stored on the job row, and the runner goes to `failed` with reason
`token_cap` before a batch that would take tokens billed past it; `--resume`
takes the same flag to raise it (author's decision).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Dual router | `EmbedderRouter(OnnxEmbedder, String, String)` (`:111-127`) | Extend: retype to `Embedder`, nullable key; delete the MiniLM-typed form |
| bge presence probe | `Bge768Embedder` constructor checks (`:226-255`) | Extend: the same checks as a non-throwing static probe |
| Profile input | `seedEmbeddingProfile` (`EmbedderRouter.java:264-272`), `contentTypeModelTokens` (`:227-238`) | Extend: seed from `NX_EMBED_PROFILE`, with per-family token constants in place of tokens read from built embedders |
| Profile predicate | `local_embed_model_is_voyage` (`config.py:708`), `persisted_credentials` (`config.py:1342`) | New: `local_embed_profile()`; the old predicate keeps its choice-only meaning |
| Mode string | `modeName()` (`:168-169`) | Extend: add `dual` |
| Key plumbing | `storage_service_daemon.py:1544-1590`; install receipt (`read_installed_provenance`, `binary_lifecycle.py:86`) | Extend: the r5f3c branch applies only below the first dual tag of the spawned binary |
| Per-collection caps and budget | `http_vector_client.py:1061`, `:1175` | Extend: key on the collection's model |
| Thresholds and cross-family merge | `search_engine.py:552-580`; `scoring.py:76-79`; `_grouped_combined_query` (`mcp/core.py:3881-3946`); scored-first merge (`search_cmd.py:658-680`, `mcp/core.py:3089-3093`) | Extend: per-collection thresholds, a bge factor, calibrated cross-group sort, degrade fallback |
| Orphan finder | `_active_embedding_dim` / `_find_dimension_mismatched_collections` (`commands/collection.py:601-720`) | Replace the single-dimension rule with the served-model-set rule |
| Profile seam | `ensure_collection_registered` (`corpus.py:2115-2119`) | Extend: skip for an existing same-model row |
| Sibling reads | `t3_collection_name` (`corpus.py:1213`); `resolve_corpus` stage 2 (`:2427-2452`) | Extend the single-name resolver; stage 2 already spans siblings |
| Egress guard | none | New source census and runtime witness |
| Document move | `CombinedWriteService.writeManyCombined` (`:202`), `CatalogRepository.writeManifestRows` (`:5182`), `failureDetail` (`:6372`) | Extend for the job's internal call: precondition, `physical_collection`, satellite re-home, typed refusal |
| Cross-model dispositions | `COLLECTION_SCOPED_TABLES` (`CatalogRepository.java:8804`) | New: a disposition per table, with a parity test |
| Cutover | `supersedeCollection` (`:8492`), `deleteCollection` (`:8280`) | Reuse unchanged |
| Taxonomy rebuild | `discover_for_collection` (`taxonomy_cmd.py:274`), as `nx index` calls it (`commands/index.py:674-690`) | Reuse from `re-embed --wait` |
| Cross-model rename | `renameCollectionTxn` copy branch (`:9122-9168`) | Not used by this RDR |
| Client reference cascade | `remap_collection_references` (`collection_rename.py:174`) | Delete: no production caller, and it would violate `topic_assignments_chunk_fk` (210-research-14) |
| Re-embed verb | `nx collection re-embed` (`commands/collection.py:1659`) | Extend: `--all`, cross-model `--to` starts the job, job controls |
| Job table and runner | none; scheduler at `NexusService.java:686-729` | New Liquibase changeset for the tenant tables `reembed_jobs` and `reembed_job_moves`, with the new-tenant-table obligations (Managed); runner on the existing scheduler |
| Doctor rows | off-profile row (`health.py:8471-8493`) | Extend; one new job row |
| Keyed gate | none (`rdr195-voyage-mvv.sh` deleted) | New script, a fourth battery leg |

### Decision Rationale

Dispatch by the row's model already exists and is the right authority; the
defect is that the table is built from half of what the machine can run. Filling
it fixes Gaps 1 and 3 in the engine. Gaps 2 and 4 then reduce to removing a guard
and a single-name choice that existed to route around Gap 1. Gap 8 has to land
before any supervisor spawns a dual engine for a client (Cross-Cutting Concerns,
incremental adoption), because a dual engine reporting either old value would mislead a client
that branches on it, and one of those branches deletes data. For the re-embed,
the per-document move reuses the one write that RDR-223 made atomic, so it keeps
the ownership invariant without an exception, keeps search whole without a
cutover window, and its per-document precondition turns a concurrent writer into
a skipped document rather than the split state the bulk rename leaves (the race
that rules out the faster copy).

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

### Alternative 3: Guard-exempt copy, then one cross-model rename

**Description**: The 2026-09-16 draft, in the form 210-research-12 made workable.
An engine job calls `upsertChunks` with no guard (an `ALLOWED_CALLERS` entry in
`OwnershipGuardCoverageScan`), copying every live chunk into the target, then
repoints all manifests at once through `renameCollectionTxn`'s cross-model branch
(`CatalogRepository.java:9122-9168`) and supersedes the source. The composite
foreign key aborts the repoint if the source gained a chash after the copy, so it
doubles as the parity check: copy the delta and retry. The spike confirmed all of
that (210-research-18).

**Pros**:

- About 36% faster at the median than the per-document move (177.9 against
  130.7 chunks/s), and the cutover itself took about 0.07 s.

**Cons**:

- A race. A source writer paused mid-transaction holds only the shared sweep gate
  and the index-run lock, neither of which blocks the rename (15 ms in the
  spike). When it resumes it rewrites its manifest naming the source, leaving the
  document's `physical_collection` on the target and its new chunk absent from
  the target. Exclusive gates or row locks close the race but block the rename
  with no timeout.
- The copy writes ownerless chunks, which is the one thing the coverage scan's
  allowed-callers rule says an entry may not do (`OwnershipGuardCoverageScan.java:30-31`).
- The rename repoints tombstoned documents too, so their chunks must also be
  copied or the foreign key aborts the repoint.
- Target chunks are invisible until the final transaction, which repoints the
  whole collection at once.
- It moves no satellite rows either, so it saves none of the per-table work.

**Reason for rejection**: the race. The per-document move pays about a quarter
of the throughput for having none of these.

### Briefly Rejected

- **Two engine processes, one per family**: two Postgres connection pools and
  two supervisors for one install, to work around a dispatch table.
- **Re-index from source (RDR-225's "registers a new collection")**: cannot move
  a chunk with no source file (notes, DEVONthink content), which is Gap 7's
  point.
- **A side table of target vectors inserted at cutover**: brings back the staging
  RDR-223 deleted, adds a SQL chunk inserter to the lint allowlist, and inserts a
  whole collection with its index maintenance in one transaction.

## Trade-offs

### Consequences

- A keyed local install keeps serving its bge-768 collections.
- A key the supervisor can find now always reaches an engine at or above the
  first dual tag (Technical Design 4). A bge-profile engine reranks
  locally (Technical Design 3), with or without its bge files, so the key causes
  Voyage traffic only to embed for and search existing Voyage collections.
- The engine holds the bge model in memory on keyed installs that have it on
  disk, even if they use only Voyage (measured cost: Cross-Cutting Concerns,
  memory).
- A re-embed holds both copies of a moved document's chunks until the source is
  deleted, spends Voyage tokens once per distinct chunk (the plan says how much
  before the job starts), and moves about a quarter slower than a bulk copy
  would.
- A re-embed leaves the target without topics until taxonomy discover runs.

### Risks and Mitigations

- **Risk**: results merged across families rank badly.
  **Mitigation**: per-surface rules and a measured bge factor (Technical Design
  12); Phase 4 Step 3 re-measures before the re-embed ships.
- **Risk**: the cloud engine constructs an ONNX session and segfaults.
  **Mitigation**: the non-throwing presence probe, and a native-image smoke with
  no model on disk (Phase 2 Step 2). The spike's keyed boot without model files
  never loaded ONNX runtime (CA-1).
- **Risk**: an old client prunes a dual engine's collections.
  **Mitigation**: the `dual` value fails closed in old clients (Technical Design
  10), and the client fix ships no later than the client release whose pin first
  names a dual engine (Cross-Cutting Concerns, incremental adoption).
- **Risk**: an old client sends Voyage-sized requests to a dual engine's bge
  collections. **Mitigation**: measured in Phase 1 Step 1; a minimum client
  version on failure (Technical Design 10).
- **Risk**: the supervisor passes the key to an engine older than the dual tag,
  which then 422s every bge collection (Gap 2). **Mitigation**: the supervisor
  passes the key past the r5f3c rule only when the binary it spawns is at least
  the first dual tag, by its install receipt, because the pin moves an existing
  box only at convergence (Technical Design 4); Phase 3 also ships only with the
  pin raised to the dual tag, held by a test, after that tag is deployed to the
  cloud engine (Phase 3 Step 1).
- **Risk**: the dual process fails on a platform the spike did not cover.
  **Mitigation**: Phase 1 Step 1 runs macOS arm64 and Windows x64 first; on
  failure the RDR goes back to planning before any engine step.

### Failure Modes

A family the engine cannot serve still gets 422 naming the missing piece (the
key, or the bge model files; `EmbedderRouter.java:547-556`). The boot banner names
both families, the profile's choice and the reranker. A profile choice the engine
cannot serve boots degraded: the banner and an `nx doctor` row name the missing
piece, and only that family's collections get 422 (Technical Design 2). A re-embed batch
that fails leaves the job `failed` with the cursor at the last committed document
and the error in the row; the documents not yet moved are still served from the
source, so search is whole. A skipped document (reason
`move_precondition_failed`) is counted in the row and in `--status`. When one
model group's rerank degrades and another's does not, the merge falls back to
calibrated order and warns (Technical Design 12).

## Implementation Plan

### Prerequisites

- [x] CA-1 verified on linux-amd64 (210-research-19); the remaining platforms and
  loads passed in Phase 1 Step 1 on 2026-10-18 (210-research-25 to -28).
- [x] CA-5 verified by source; CA-2 verified where it is used, its residuals
  measured in Phase 4 Step 3 before Phase 6 ships.
- [x] Non-engine doc sweep and the nexus-umm29 wording landed (f7cae33ec).
- [x] The init keep-voyage fix landed on develop (nexus-ddmfg,
  c03352a2a..6cd90aab5).
- [x] Open Questions 1-3 decided (Sam, 2026-10-17).
- [x] `HttpsSchemeHandler` and `POST /v1/vectors/resolve` removed (nexus-3t0jv,
  c143a27ab, on develop; the bead stays open until the next engine tag).
- [x] The DJL instance-metadata and native-download paths closed (nexus-17jvq,
  2416ce891, on develop; the bead stays open until the next engine tag), so
  Phase 2 Step 6's runtime witness does not fail on them.

### Minimum Viable Validation

A local engine booted with a real key and the bge model, one bge-768 collection
and one Voyage collection: write to and search both, restart, search both again,
and one search over both returns hits from both. Then `re-embed --all` moves the
bge collection (one source-backed document and one `store_put` note) to its
Voyage sibling, search returns the same documents before and after, the
source-backed document's aspects survive the source delete, and a second
`re-embed --all` plans zero documents. This is the Phase 5 gate script's body
plus the Phase 6 scenarios.

### Phase 1: Spike and client prerequisites

#### Step 1: CA-1 residual checks

linux-amd64 passed in JVM and native image (210-research-19). This step runs what
that spike did not, from its branch (`spike/nexus_911-rdr210-dual-native`)
or its successor. Each check records VmHWM (or the platform's peak
working set) and is a research finding. Any fail stops the engine phases and
sends the RDR back to planning.

- **Platforms.** Native image on macOS arm64 and Windows x64 (the RDR-224 runtime
  with its ORT init gate and temp sweep). **Pass**: the image builds and boots
  dual, returns a 768-dimension bge vector, a cross-encoder rerank and, through a
  loopback stub on the URL-taking constructors, a 1024-dimension vector from each
  Voyage class; exits 143 on SIGTERM (POSIX) or 149 on CTRL_BREAK (Windows, the
  RDR-224 clean-stop code), also during bge initialisation, with no
  crash report; a keyed boot without model files never loads ONNX runtime.
  **Fail**: any build error, reflection or JNI conflict, crash or wrong exit code.
- **Voyage serving in the dual process.** linux-amd64, a real key from the
  operator's credential chain. **Pass**: one embed through each Voyage class and
  one Voyage rerank return results while bge serves in the same process, and peak
  memory stays within the bge-only run's range plus 10%.
- **Concurrent load.** linux-amd64, three concurrent writers to a bge collection
  (`flush_concurrency=3`) with a rerank loop running alongside. **Pass** (Sam,
  2026-10-18): the dual process's peak is within 10% of a bge-only process under the
  same load. The 3.03 GB figure measured for bge-only at that concurrency
  (`Bge768Embedder.java:101-105`) came from a shakedown with no rerank loop and does
  not bound this load: bge-only itself peaks at 4.5 to 6.9 GB here, so the absolute
  peaks are recorded, not gated.
- **Old-client request shape.** The same load sent with the Voyage chunk cap and
  byte budget a pre-`dual` client would use (Technical Design 10). **Pass**: the
  same relative bound as the concurrent-load check. **Fail**: the wire ledger entry
  for `dual` carries a minimum client version instead.
- **Native-library extraction (nexus-8ool4).** nexus-8ool4 (962a687bd, 7467e0b74)
  is on develop. `OrtNativeDir.applyAtBoot` (`Main.java:294`) extracts about 16 MB
  of ONNX runtime libraries under the cache tree and sets `onnxruntime.native.path`,
  but it runs only in the keyless branch. A keyed bge-profile engine builds the
  cross-encoder (Technical Design 3) with no such call, so its first rerank would
  unpack ORT into a random `/tmp` directory. Placement rule, checked here and
  built in Phase 2 Steps 1 and 4: `applyAtBoot` runs once, before bge or the
  cross-encoder can touch ORT, in every boot that can build either, and never on
  the cloud shape (key, no model files). **Pass**: a keyed boot with no model
  files has boot memory within 10% of the spike's 161 MB and extracts nothing; a
  keyed boot with the bge files extracts once under the cache tree, a second boot
  reuses it, and a bge-profile rerank leaves no `onnxruntime-java*` directory in
  `/tmp`. **Fail**: any extraction on the cloud shape, or an ORT load outside the
  cache tree.

**Run 2026-10-18 (nexus-6nhy3), all pass.** macOS arm64 native: T2
`nexus_rdr/210-research-25`. Native-library extraction placement: `-26`. Windows
x64 native, clean stop is exit 149 (CTRL_BREAK), not 143: `-27`. linux-amd64
concurrent load and old-client shape, 18 serial runs, dual within 2.2% of bge-only
at worst: `-28`, so the `dual` wire entry needs no minimum client version. The
Voyage loopback stub was not used; a real key covered the 1024-dimension check on
macOS and Windows. The real-key Voyage check ran on those two platforms, not on
linux-amd64.

#### Step 2: Mode string and per-collection decisions

Change the 22 site groups of Gap 8 to the rules in Technical Design 10: chunk cap
and byte budget by collection model, the memo and the gate accept `dual`, remedy
text by collection family, `embedding_models` as the authority, and the write
model read from `local_embed_profile()` (Technical Design 2), added here;
`local_embed_model_is_voyage()` and its callers outside the write model
(`init.py:100`, `storage_service_daemon.py:1575`) are unchanged. The new
function is safe against today's engines: with no choice recorded and a
persisted key, today's supervisor already passes the key
(`storage_service_daemon.py:1575` skips it only when a non-Voyage choice is
recorded, and `get_credential` falls back to `config.yml`, `config.py:1336-1339`),
so the engine is Voyage-only and Voyage names are what it serves. With an
env-only key and no choice it answers `bge`, which is what today's write model
already answers there, so nothing changes for that case. **Test**: a unit test
of the function's five cases (Voyage choice, bge choice, no choice with a
persisted key, no choice with an env-only key, no choice and no key), and that
`local_embed_model_is_voyage()` still answers False with no choice recorded; unit
tests with a fake `/version` reporting each of `onnx-local`, `voyage` and `dual`,
asserting the cap and budget for a bge collection and a Voyage collection under
each; a test that enumerates the Gap 8 sites by the census patterns
(`"onnx-local"`, `== "voyage"`, `embedding_mode`) over `src/nexus` and fails on a
site outside the reviewed list, with a non-vacuity floor of the sites it expects.

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

#### Step 5: Cross-family ranking

Implement Technical Design 12. It is client-only and lands here, before any
engine can return two families to one search. **Tests**:
- `scoring.py`: the bge factor is 0.45/0.325 within 1e-6; an unlisted model
  still gets the default and logs it.
- `apply_hybrid_scoring` over a fixture of one bge and one Voyage group (the
  210-research-17 distances, recorded as test data): count the queries where an
  irrelevant chunk of one family outranks the best relevant chunk of the other.
  At 1.38 the count must be below the 0.818 default's (12 and 5 of 20) in both
  directions, and the test pins the observed count as its regression bound. The
  record measured 1.15 to 1.30 at 3 and 1 but did not report 1.38; if 1.38 counts
  more than that on the fixture, the step stops and Phase 4 Step 3 picks the
  value before the table entry is set.
- `_grouped_combined_query`: rows from two groups interleave by calibrated
  distance, not raw distance.
- Rerank-on merge: two groups both scored merge by `rerank_score`; one scored
  and one degraded fall back to calibrated order and carry the warning, on both
  `nx search` and MCP `search`.
- Thresholds: on fake engines reporting `voyage` and `dual`, a Voyage collection
  is filtered by its threshold and a bge collection is not; on `onnx-local`
  neither is, as today.

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

Seed from `NX_EMBED_PROFILE`; boot degraded on an unservable choice (Technical
Design 2). Replace `contentTypeModelTokens`' instance reads
(`EmbedderRouter.java:227-238`) with per-family token constants for each
content type, so the seeded tokens follow the profile and not which embedders
were built. **Test**: engine integration tests for each value with and without the
key and the files, asserting the seeded rows; among them, a `voyage` profile with
no key seeds the Voyage tokens and a `bge` profile with a key seeds bge-768 for
every content type; for an unservable choice, that the
engine boots, the banner names the missing piece, the servable family's
collections serve and the other family's get 422 naming it.

#### Step 4: Rerank, banner and status

One reranker, chosen by the profile (Technical Design 3); the search route's
rerank stage dispatches to it for every group. The banner names both families,
the profile and the reranker; `local_embed_activity` is reported whenever bge is
built, not only in the keyless branch (`Main.java:320`). **Test**:
`StatusHandler` and `VersionHandler` tests for the dual shape; a test that a
bge-profile engine with a key reranks with the cross-encoder and makes no Voyage
rerank call; for the degraded boots, that a `voyage` profile with no key reranks
with the cross-encoder; a keyed `bge` profile with no bge files builds the
cross-encoder and no bge embedder, and makes no Voyage rerank call; the same
engine with the cross-encoder files removed skips the rerank with a warning and
still makes no Voyage rerank call; the shared admission gate is built with no bge
embedder present.

#### Step 5: Dual journey test

Rewrite `Rdr204Gh1461ProfileRestartJourneyTest` to use one production dual router
for both collections, removing the separate bge router and the fake. **Test**: the
rewritten test itself.

#### Step 6: Egress guard

nexus-3t0jv and nexus-17jvq are on develop. Add both tests of Technical
Design 7: the source census (allowlist with a reason per entry, non-vacuity
count, Voyage host pin, Liquibase version check) and the child-JVM runtime
witness. Amend the carve-out wording in RDR-157, RDR-160 and the privacy policy
to the allowlist sentence. **Test**: the two tests themselves, plus negative
fixtures: a file constructing an `HttpClient` outside the allowlist fails the
census; a Voyage URL constant naming another host fails it; a `liquibase.version`
of 4.30 without analytics off fails it; a tokenizer build that opens an `http`
URL stream fails the witness. Paste the census's first live run (sites found,
allowlist entries, files scanned) into the bead before close.

#### Step 7: Java comments and the engine tag

Correct the comments in Technical Design 8. Record the `dual` mode value in
`docs/wire-contract-pending.md`. Run every `*GateTest`. Ships in an engine tag
after `engine-service-v0.1.160`; the tag is Sam's. That tag is the first dual
engine, and Phase 3 waits for it and for its deployment to the cloud engine
(Technical Design 4).

### Phase 3: Supervisor

#### Step 1: Key and profile plumbing

Read the spawned binary's tag from its install receipt; at or above a named
constant for the first dual tag, pass the key whenever the credential chain has
one, and below it, or with no receipt, keep the r5f3c rule (Technical Design 4).
Pass `NX_EMBED_PROFILE` from `local_embed_profile()` (Technical Design 2), which
the write-model sites already read (Phase 1 Step 2). This step lands in the client release that
raises `REQUIRED_ENGINE_VERSION` (`engine_version.py:667`) to the Phase 2 Step 7
tag, never before, and that release ships only after the tag is deployed to the
cloud engine, since the constant is also the managed floor (Technical Design 4).
Add the degraded-boot doctor row
(Technical Design 2). **Test**: `tests/daemon/test_storage_service_daemon.py`
cases, with a receipt at the first dual tag, for a bge choice with a key (key
passed, profile `bge`), a Voyage choice without a key (no key, profile `voyage`;
the engine boots degraded), no choice with a persisted key (key passed, profile
`voyage`), no choice with an env-only key (key passed, profile `bge`) and no
choice without one (profile `bge`); a bge choice with a key and a receipt below
the first dual tag, or none, withholds the key as r5f3c does today; a test that
the profile the supervisor passes and the write model the client mints agree
for all five; a test that fails while
`REQUIRED_ENGINE_VERSION` is below a named constant for the first dual tag, the
shape `health.py:6868` already uses for a route floor
(`REQUIRED_ENGINE_VERSION < _TUPLE_ROUTE_FIRST_ENGINE_VERSION`), so the guard's
relaxation cannot merge ahead of the pin; a doctor test for the degraded-boot row
against a fake `/version` whose `embedding_models` lacks the profile's family.

#### Step 2: Local-service gate comments

The gate stays keyless; correct its stale comments about the key and one posture
(`local-service-gate.sh:61-71`, `:349-352`, `:940-952`). **Test**: the gate's own
guard test (`tests/e2e/local_service_gate_guard_test.sh`).

### Phase 4: Client reads

#### Step 1: Single-name resolver spans siblings

The read resolver returns every live sibling; write resolution is unchanged.
This step reaches users in or after the Phase 3 client release, never before: on
a single-family engine a read that also names the unserved sibling raises the
named error although the served one answered ("fires even when every OTHER
targeted collection searched fine", `search_engine.py:1951-1952`).
**Test**: replace the pin at `tests/test_rdr_109_phase2_dispatch.py:258` with one
asserting both siblings are read once a Voyage sibling exists, and a second test
that registers the Voyage sibling under a name that is not the four-segment shape
and asserts it is still found, so a name match cannot pass.

#### Step 2: Single-name reads merge by the same rules

Reads that now span siblings through the single-name resolver merge by
Technical Design 12, shipped in Phase 1 Step 5. **Test**: a `--collection` read
over a bge and a Voyage sibling returns hits from both in calibrated order.

#### Step 3: Ranking measurement (CA-2 residuals)

On a dual local engine, through the engine's search route (not the spike's direct
Voyage calls), over a fixed query set that includes a code collection: re-derive
the bge factor with the 210-research-17 method; measure cross-request rank
stability of the cross-encoder on a bge-profile engine the way 210-research-16
measured Voyage's (split-merge against one request, Kendall tau against the
noise floor); and count cross-family inversions on each surface. **Pass**: the
bge factor's bootstrap interval overlaps 1.36 to 1.48; the cross-encoder's
split-merge tau is within 0.01 of its noise floor; inversions on every surface no
worse than the Phase 1 Step 5 fixture bound. **Fail**: revise Technical Design 12 before
Phase 6. Record the result as a research finding.

### Phase 5: Gates

#### Step 1: Dual-embedder gate script

No keyed local script exists to extend. Add `tests/e2e/dual-embedder-gate.sh`: it
provisions a local engine with the bge model and a real Voyage key from the
operator's credential chain, runs the MVV's first half, restarts, and runs it
again. An absent key is a FAIL, not a skip. It needs a dual engine under the
supervisor, so it lands with Phase 3. **Test**: the script's own guard test,
asserting a missing key fails it.

#### Step 2: Release battery leg

Register it as a fourth leg in `tests/e2e/release-battery.sh` beside `:165-167`.
It runs in the release battery and by hand only, never nightly (Sam, Q3).

### Phase 6: Re-embed

#### Step 1: Internal call and precondition

An engine-internal entry to `CombinedWriteService` that takes the precondition,
sets `physical_collection` and re-homes the aspect, queue and highlight rows in
the per-document transaction, and refuses with the typed reason. Confirm the
aspect join on `source_path` by source reading first. Add the cross-model
disposition list beside `COLLECTION_SCOPED_TABLES` and its parity test; delete
`remap_collection_references`. **Test**: engine integration tests on real
Postgres: a document moves whole (chunks, manifest, `physical_collection`, its
aspects, queue item and highlight, including a doc-less aspect row matched by
path); a target row with the same unique key stands and the source row goes; a
document changed after the read is skipped with `move_precondition_failed`, not
"internal error"; a tombstoned document is refused; a chunk shared by two
documents is embedded once; a client write of a moved document back into the
source leaves manifest and `physical_collection` naming the same collection, and
the next pass moves it; a mid-move search through the client returns each
document once, one request per sibling; the disposition parity test fails on a
fixture table added to the registry without a disposition.

#### Step 2: Job table and runner

The changeset for `reembed_jobs` (with the `token_cap`, `keep_source` and
`force_delete` columns) and `reembed_job_moves`, carrying the new-tenant-table
obligations of the Managed section; the partial unique index over the lock
states; runner on the scheduler, picking up `planned`, `running` and `cutover`
rows at boot; the states, transitions and idempotent self-transitions of the
state table; cursor. Name the supervisor's
local-engine variable and check that no managed deployment sets it. **Test**:
restart mid-job resumes from the cursor; restart in `cutover`, both before and
after the delete committed, ends the job `done` with the right disposition and
no second delete; two starts on one source are refused, and a resent start gets
409 naming the existing job; a second start while a job on that source is
`failed` is refused naming it; a `kept_tombstoned` re-entry while another job
holds the source's lock is refused naming that job, and `--resume` of a failed
job succeeds because its own row holds the lock; abort is accepted from
`paused`, `failed` and `cutover` before the delete step and refused after it;
`--pause`, `--resume` and `--abort` resent on a job already in that state
change nothing; the cap and flags survive a restart (a job started with
`--keep-source` and restarted in `cutover` ends `kept_by_flag`); a failed batch leaves
`failed` with the cursor at the last committed document; without the
local-engine variable the runner does not start and the routes refuse; a
profile changed before `--resume` leaves the job `failed` with
`profile_changed`; a superseded target row of the same tuple, whatever its name, whose `superseded_by` names this job's source is revived and
the earlier job's disposition becomes `revived`, and one superseded by another
name is refused at plan time; each state's allowed transitions, and a refused
one, as a table-driven test.

#### Step 3: Status route and verb

The routes of the re-embed section, each an `[additive]` entry in
`docs/wire-contract-pending.md`; `nx collection re-embed --all`, cross-model
`--to`, `--keep-source` and `--force-delete` sent in the start route's body, and
the job controls; token estimate and cap; the plan's counts of
quarantine rows, cross-collection topic assignments and links, and unmatched
satellite rows (Cutover); `--all` candidacy, which lists a superseded source
only as a `kept_tombstoned` job's re-entry; the hint at its three sites
(`nx config set`, `nx config unset`, and each output branch of
`nx daemon service start`). **Test**: CLI tests for the plan and its counts, the
cap refusal, and each control; the verb refuses when `is_local_mode()` is false;
`--all` lists a `kept_tombstoned` source once it holds no document row or a
restored live one, and never a `kept_by_flag` or `kept_residue` one;
`nx config set local.embed_model` from a bge to a Voyage model prints the
re-embed line, and a set that leaves `local_embed_profile()`'s answer unchanged
does not; `nx config unset voyage_api_key` with no choice recorded prints it;
`nx daemon service start` prints it with an off-profile collection present and
not without one, after the running line in the plain branch, on stderr with
stdout still one JSON object under `--announce-stdout`, and from the supervisor
under `--foreground`.

#### Step 4: Cutover and source removal

Cutover's ordered steps: supersede at zero live documents; `kept_tombstoned`
while any document row remains, then `keep_source`, then the parity check
(live document rows only), then the residue check against `force_delete`; the
job deletes the source once it holds no document row and the check passes;
`--wait` returns on `done`, `done_with_skips`, `aborted`, `failed` and `paused`,
naming the state, and runs taxonomy discover for the target on the first two.
**Test**: a source holding one tombstoned document ends `kept_tombstoned`
without a parity check; a `parity_mismatch` job fails the same way on
`--resume` and leaves the source superseded and undeleted on `--abort`;
`--wait` returns on each of its five states with the state named;
after the last move the source is superseded and out of the routing listing; it
is deleted only when it holds no document row, and kept with `--keep-source`;
a fixture that leaves one manifest row naming the source, or a moved document
homed back in the source, fails the parity check with `parity_mismatch` and
deletes nothing; an unmatched doc-less aspect row blocks the delete
(`kept_residue`) and `--force-delete` lets it through; a skipped document ends
the job `done_with_skips` with the source neither superseded nor deleted; after
the delete, no aspect, highlight or queue row of a moved document names the
source, and the moved documents' aspects are intact; the quarantine-sibling and
cross-collection topic counts in `--status` match what the delete removed;
after `--wait`, the target has a `taxonomy_meta` row and topic assignments for
its chunks; without `--wait`, the doctor's drift row names the discover command.

#### Step 5: Doctor rows and paired release

Extend the off-profile row; add the job row. Ships in its own engine tag with the
client half paired, after Phase 4 and its Step 3 measurement; the client half
raises `REQUIRED_ENGINE_VERSION` to that tag, so the tag is deployed to the cloud
engine first (Technical Design 4), although its routes refuse there. **Test**: doctor tests for the not-applicable, running and
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

None open. Resolved by Sam on 2026-10-17 (T2
`nexus_rdr/210-open-questions-sam-2026-10-17`):

1. **The engine's `https` fetcher.** `HttpsSchemeHandler` and
   `POST /v1/vectors/resolve` are removed in every mode, with no hardening
   (nexus-3t0jv). No production client calls the route and the client reads
   `https` sources itself (`aspect_readers.py:748`).
2. **An ambient key on a bge profile.** The reranker follows the profile: a
   bge-profile engine reranks with the cross-encoder even when keyed
   (Technical Design 3).
3. **The superseded source.** The re-embed job deletes it once it holds no
   document row, tombstoned included; `--keep-source` opts out. No reaper floor
   waiver.

## Test Plan

- **Scenario**: dual engine, bge and Voyage collections in one database —
  **Verify**: embed, write and search succeed for both; no 422.
- **Scenario**: keyless engine, Voyage collection — **Verify**: 422 naming the key.
- **Scenario**: keyed engine with no bge files (cloud shape) — **Verify**: boots
  Voyage-only, reports `voyage`, no ONNX session constructed.
- **Scenario**: bge model file truncated — **Verify**: boot fails naming the size
  floor, not "absent".
- **Scenario**: `NX_EMBED_PROFILE=voyage` with no key — **Verify**: the engine
  boots degraded, the banner and the doctor row name the key, bge collections
  serve, Voyage collections get 422 naming the key, and search reranks with the
  cross-encoder.
- **Scenario**: `NX_EMBED_PROFILE=bge` with a key and no bge files — **Verify**:
  the engine boots degraded naming the bge files, builds no bge embedder, serves
  Voyage collections, and search reranks with the cross-encoder and sends no
  rerank text to Voyage; with the cross-encoder files removed it skips the rerank
  with a warning and still sends none.
- **Scenario**: no `local.embed_model` recorded, a key persisted in
  `config.yml` — **Verify**: the supervisor passes profile `voyage` and the
  client mints Voyage names; no `EmbeddingProfileMismatchError`.
- **Scenario**: no `local.embed_model` recorded, a key only in the environment —
  **Verify**: the key reaches the engine, the supervisor passes profile `bge`,
  and the client mints bge names.
- **Scenario**: a bge choice and a key, the installed engine's receipt below the
  first dual tag — **Verify**: the supervisor withholds the key, as the r5f3c
  rule does today.
- **Scenario**: client against fake engines reporting `onnx-local`, `voyage` and
  `dual` — **Verify**: chunk cap, byte budget and thresholds follow each
  collection's model.
- **Scenario**: a merged list from a bge and a Voyage collection on a rerank-off
  surface (MCP `query`, a catalog-routed tool) — **Verify**: ordered by calibrated
  distance with the bge factor; cross-family inversions within the Phase 1 Step 5
  bound.
- **Scenario**: one model group's rerank degrades, the other's does not —
  **Verify**: calibrated order with the warning; the degraded family is not sunk
  below every scored row.
- **Scenario**: `nx collection prune --yes` against a dual engine — **Verify**: no
  served collection is listed or deleted; a minilm-384 collection is listed.
- **Scenario**: old client (no `dual` support) against a dual engine — **Verify**:
  prune reports "cannot verify" and deletes nothing.
- **Scenario**: profile says Voyage, write to an existing bge collection —
  **Verify**: accepted by client and engine; a new bge name is refused.
- **Scenario**: corpus with both siblings, read through `--collection` and
  through a bare content type — **Verify**: hits from both.
- **Scenario**: engine source adds a new outbound HTTP client, or a library opens
  an `http` URL stream during a tokenizer build — **Verify**: the source census or
  the runtime witness fails.
- **Scenario**: dual gate with no key — **Verify**: FAIL, not skip.
- **Scenario**: `re-embed --all` on two stale collections, one holding only
  `store_put` notes — **Verify**: every live document is in its Voyage sibling,
  `physical_collection` names it, the sources are superseded, search returns the
  same documents.
- **Scenario**: search during a re-embed — **Verify**: each live document appears
  once, from whichever sibling holds it.
- **Scenario**: a client re-indexes a document while the job runs — **Verify**:
  the job skips it with `move_precondition_failed` and counts the skip; the
  client's version stands; the next pass finds nothing left to move for it.
- **Scenario**: a moved document with aspects, a queued extraction and a
  highlight, then the source delete — **Verify**: all three name the target and
  survive; the target's topics exist after `--wait`.
- **Scenario**: engine restart mid-job — **Verify**: the job resumes from its
  cursor; no document is in both or neither collection.
- **Scenario**: engine restart while a job is in `cutover`, before and after the
  delete — **Verify**: the runner picks the row up and the job ends `done` with
  its disposition; the source is deleted at most once.
- **Scenario**: a second start on a source whose job is `failed`, and a
  `kept_tombstoned` re-entry while another job holds the source — **Verify**:
  each is refused with 409 naming the holding job; `--resume` of the failed job
  is accepted.
- **Scenario**: `re-embed --all` twice — **Verify**: the second run plans zero
  documents and writes nothing.
- **Scenario**: a batch fails at Voyage — **Verify**: the job is `failed` with the
  cursor at the last committed document and the error in the row; `--resume`
  continues.
- **Scenario**: plan above the token cap — **Verify**: refused before any write,
  naming the estimate and the cap.
- **Scenario**: a tombstoned document in the source — **Verify**: not moved; the
  source is superseded but not deleted while the document row exists
  (`kept_tombstoned`) and the parity check does not run; after purge-trash
  removes the row, the next `re-embed --all` deletes it; after a restore
  instead, the next `re-embed --all` moves the restored document first.
- **Scenario**: the parity check finds a manifest row still naming the source —
  **Verify**: nothing deleted, job `failed` with `parity_mismatch`; `--resume`
  fails the same way; `--abort` leaves the source superseded and undeleted.
- **Scenario**: profile changed in `nx config set`, or by `nx config unset
  voyage_api_key` with no choice recorded — **Verify**: the re-embed line
  prints; `nx daemon service start` prints it while a collection is
  off-profile, in its plain, `--announce-stdout` (stderr, stdout still JSON) and
  `--foreground` branches.
- **Scenario**: `--wait` on a job that is paused, aborted or failed — **Verify**:
  it returns naming the state, without running taxonomy discover.
- **Scenario**: re-embed against an engine the supervisor did not mark local —
  **Verify**: the verb and the routes refuse; no job row is written.

## Finalization Gate

### Contradiction Check

Five contradictions between the drafts and the research were found and resolved.

1. The 2026-09-16 re-embed upserted copied chunks through the public write
   route, which the ownership guard refuses on local engines. The design moves
   documents through the combined write, which writes the chunk and its owner
   together; the spike ran it in enforce mode (210-research-18).
2. That draft said source chunks drain through the quarantine sweep. The
   reaper's move floor refuses a fully reapable source; the job deletes it (Sam,
   2026-10-17), which the spike's reaper refusal confirms.
3. CA-3 was refuted: 22 client and engine site groups read the single mode
   string, one of which deletes data. Technical Design 10 and Phase 1 answer it.
4. The previous revision assumed aspects, highlights and the queue could ride the
   move, and that topic assignments needed only a discover. The spike refuted the
   first: no satellite row moves with the document, and the source delete
   destroys them. The design re-homes three tables in the move transaction and
   rebuilds the taxonomy (the disposition table). 210-research-14's own
   recommendation to copy topic assignments by chash contradicts the
   topic foreign key's cascade and is not followed (re-embed section).
5. CA-2 as first written assumed every keyed-engine result passes the reranker.
   Two surfaces rerank; the rest merge by distance with an uncalibrated bge
   factor that inverts results (210-research-13, -17). Technical Design 12 sets a
   rule per surface.

The tension between always passing the key and one Voyage reranker per keyed
engine is resolved by choosing the reranker from the profile (Sam, 2026-10-17;
Technical Design 3). No other contradictions were found between research
findings, design principles, and the proposed solution.

### Assumption Verification

- **CA-1**: verified for linux-amd64 by spike (210-research-19). The untested
  platforms and loads are Phase 1 Step 1, each with a pass/fail criterion, and
  gate every engine step.
- **CA-2**: verified for rerank-on surfaces of Voyage-profile engines by spike
  (210-research-16); refuted for rerank-off surfaces as the code stands
  (210-research-13, -17) and answered by Technical Design 12, whose factor is a
  measurement. The cross-encoder's comparability is inferred from its pointwise
  scoring and measured in Phase 4 Step 3 before Phase 6.
- **CA-3**: refuted, census complete (210-research-15); the design is built on
  the census.
- **CA-4**: verified by source and spike (210-research-18); it is the mechanism.
- **CA-5**: verified by source (`RequestContext.java:62`,
  `RequestDeadlineProbe.java:27-36`).

No assumption the design rests on is open without a step that measures it and a
criterion that stops the plan on failure.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `EmbedderRouter.resolveEmbedderStrict`, `modeName`, `availableModels` | nexus engine | Source Search |
| `CombinedWriteService.writeManyCombined` / `CatalogRepository.writeManifestRows`, `failureDetail` | nexus engine | Source Search; move overloads by spike (210-research-18) |
| `CatalogRepository.supersedeCollection`, `deleteCollection`, `upsertCollection` | nexus engine | Source Search |
| `GET /version` `embedding_mode`, `embedding_models` | nexus engine | Source Search |
| `Bge768Embedder` constructor checks | nexus engine | Source Search |
| ONNX runtime, cross-encoder and Voyage clients in one process | onnxruntime 1.20.0, GraalVM 25.0.3 | Spike, linux-amd64 (210-research-19); other platforms Phase 1 Step 1 |
| Voyage rerank score comparability across requests | Voyage rerank-2.5 | Spike (210-research-16) |
| `apply_hybrid_scoring` with a bge factor | nexus client | Spike through the real function (210-research-17) |

### Scope Verification

The MVV is in scope and executed: its first half is the body of
`tests/e2e/dual-embedder-gate.sh` (Phase 5 Step 1), a release battery leg; its
second half is the Phase 6 Step 5 scenarios added to the same script. The init
keep-voyage fix is out of scope and already on develop (nexus-ddmfg), the https
fetcher's removal is out of scope and already on develop (nexus-3t0jv), and so is any
change to the managed engine's posture: the cloud boots Voyage-only exactly as
today. Re-embed on the managed engine is out of scope (Sam, 2026-10-17, gate
round 1).

### Cross-Cutting Concerns

- **Versioning**: `dual` is a new wire value on `/version` and `/v1/status`,
  recorded in `docs/wire-contract-pending.md`. Old clients fail closed on it for
  deletion; the chunk-cap case is measured in Phase 1 Step 1, with a minimum
  client version as the fallback (Technical Design 10). The managed engine never
  reports `dual`.
- **Build tool compatibility**: one process holds ONNX runtime and the Voyage
  clients on linux-amd64 (CA-1); macOS arm64 and Windows x64 are Phase 1 Step 1.
  The spike's build needed zlib static libraries that chas lacked, worked around
  locally and not committed (210-research-19); the CI image is unaffected
  (inferred, not read: CI builds native images today).
- **Licensing**: N/A; no new dependency.
- **Deployment model**: local engines only change posture; the managed engine is
  unchanged. The re-embed job is local only (Sam, 2026-10-17, gate round 1): the
  runner and the verb refuse when the engine is not local. Managed re-embed
  (tenant enumeration, RLS, who pays) is a separate future decision. The job's
  two tables still enter the shared Liquibase chain on every engine, the managed
  one included, and so carry row level security and the hand-kept tenant-table
  enumerations (Managed).
- **IDE compatibility**: N/A.
- **Incremental adoption**: each phase has its own ordering. Phase 1 is
  client-only, ships first and alone, and is safe against today's engines
  (Phase 1 Step 2). Phase 2 is an engine tag that can be cut alone: on the cloud
  it boots Voyage-only and reports `voyage` as today, and it reaches a local box
  only through a client release whose pin names it (`PINNED_SERVICE_TAG`,
  `binary_install.py:106`, or convergence), which is Phase 3's, so Phase 1 must
  be in that release or an earlier one. Phase 3 is the client release that
  raises `REQUIRED_ENGINE_VERSION` to the Phase 2 tag, after that tag is deployed
  to the cloud engine (the managed floor, Technical Design 4); against an engine
  older than the dual tag, passing the key recreates the regression nexus-r5f3c
  fixed, which the spawned-binary check and a test hold (Phase 3 Step 1).
  Phase 4 needs Phase 1 Step 5's merge rules and reaches users in or after the
  Phase 3 release, since a sibling-spanning read against a single-family engine
  errors on the unserved sibling (Phase 4 Step 1). Phase 5 needs a dual engine
  under the supervisor and lands with Phase 3. Phase 6 is an engine tag paired
  with its client half, after Phase 4 and its Step 3 measurement, with the same
  cloud-first ordering for its pin (Phase 6 Step 5). A dual engine without
  Phase 4 still serves both families; only single-name reads stay on one
  sibling.
- **Secret/credential lifecycle**: the key keeps its one source (the credential
  chain, then `NX_VOYAGE_API_KEY` at spawn); what changes is that it is passed
  whenever present to an engine at or above the first dual tag (Technical
  Design 4). Only a persisted key chooses the profile (Technical Design 2).
  Rotation is a service restart, as today. An ambient key on a bge profile
  causes no rerank traffic (Technical Design 3).
- **Memory management**: a keyed engine with bge files holds the bge model
  (about 416 MB on disk, `Bge768Embedder.java:243-250`) and the cross-encoder
  (about 87 MB, 210-research-11) beside the Voyage clients. The dual process's
  measured peak is within noise of bge-only (CA-1, Key Discoveries);
  concurrent-load peaks are Phase 1 Step 1. A re-embed
  batch holds at most one batch of chunk text and vectors, or one large
  document's; the bound is the batch rule above.

### Proportionality

The document carries two designs: the dual engine (Phases 1 to 5) and the
re-embed (Phase 6). They stay together because the re-embed's correctness
depends on the dual engine's reads, profile and merge rules, and splitting would
duplicate the context section. This revision cut what the research superseded:
the "pending" markers, the draft's provisional mode-string list (replaced by the
census), the copy-options placeholder, and Key Discovery bullets that only
re-stated drift. What remains long is evidence the gate needs: the census lists
and the per-table dispositions.

## References

- T2 `nexus/voyage-local-mode-investigation-2026-09-15` [25872]
- T2 `nexus_rdr/210-research-1` to `-19`; `nexus_rdr/210-drift-audit-2026-10-17`;
  `nexus_rdr/210-open-questions-sam-2026-10-17`;
  `nexus_rdr/210-ca2-spikes-rerank-and-bge-calibration-2026-10-17` (raw spike data)
- Beads nexus-ddmfg, nexus-umm29, nexus-r5f3c, nexus-35ok4, nexus-vply6,
  nexus-3t0jv, nexus-17jvq, nexus-8ool4
- `service/src/main/java/dev/nexus/service/Main.java:237-361`
- `service/src/main/java/dev/nexus/service/vectors/EmbedderRouter.java`
- `service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java:597-935`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java:5182-5260, 6372-6414, 7873-7990, 8280-8468, 8492, 8804-8823, 9018-9168`
- `service/src/main/java/dev/nexus/service/db/CombinedWriteService.java`
- `service/src/main/java/dev/nexus/service/ChunkReaper.java`
- `service/src/main/java/dev/nexus/service/http/RequestContext.java`, `vectors/RequestDeadlineProbe.java`
- `service/src/test/java/dev/nexus/service/OwnershipGuardCoverageScan.java`
- `service/src/main/resources/db/changelog/aspects-001-baseline.xml`, `fk-001-catalog-cross-store.xml`, `taxonomy-014-topics-tenant-unique.xml`, `vectors-030-model-tenant-partition-functions.xml`
- `tests/catalog/test_collection_scoped_tables_schema_parity.py`
- `src/nexus/daemon/storage_service_daemon.py:1484-1595`
- `src/nexus/commands/init.py:54-118, 759-787`
- `src/nexus/corpus.py:1213-1500, 2085-2135, 2372-2452`
- `src/nexus/commands/collection.py:601-760, 1436-1745`
- `src/nexus/db/http_vector_client.py:1050-1262, 3275-3330, 6040-6052`
- `src/nexus/scoring.py:60-104`, `src/nexus/search_engine.py:536-624, 1625-1650`, `src/nexus/mcp/core.py:3030-3120, 3881-3946, 5100-5120`, `src/nexus/commands/search_cmd.py:455-680`
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
- 2026-10-17: Sam resolved the revised draft's three Open Questions: remove the engine https fetcher and resolve route in every mode (nexus-3t0jv), a bge-profile engine reranks with the cross-encoder even when keyed, and the re-embed job deletes the superseded source (`--keep-source` opts out). Record: T2 nexus_rdr/210-open-questions-sam-2026-10-17.
- 2026-10-17: research findings 210-research-10 to -19 folded in, every
  "pending" marker replaced, cites re-read at 42382fa3d (develop 6cd90aab5).
  Gap 2's init half marked landed; Gap 5 carries the eight-path egress census and
  Technical Design 7 an allowlist with a source census and a runtime witness
  (nexus-17jvq now a prerequisite); Gap 8 carries the CA-3 census. CA-1 verified
  on linux-amd64, its residuals turned into Phase 1 Step 1 checks with pass/fail
  criteria; CA-2 split by surface, with Technical Design 12 (bge factor 1.38,
  per-collection thresholds, degrade fallback) and Phase 4 tests; CA-4 and CA-5
  verified. The per-document move is confirmed as the design, with a typed
  precondition reason and a disposition for every collection-scoped table: the
  aspect tables re-homed in the move transaction, topic assignments rebuilt by
  discover rather than copied. Alternative 3 rewritten around the spike's race.
  Finalization Gate answered against this content.
- 2026-10-17: Gate round 1 — PASSED (0 Critical, 6 Significant, 0 ship-blocker(s)); commit `c62709e9e`; critique `nexus_rdr/210-gate-critique-2026-10-17-r1`.
- 2026-10-18: Gate round 2 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); commit `90a51e69d`; critique `nexus_rdr/210-gate-critique-2026-10-18-r2`.
- 2026-10-18: Sam's ruling on RDR-229 Q6 (recorded on nexus-j4gl6): the row rule is adopted now. Sibling, Gap 4, Technical Design 6, the re-embed target and Phase 4 Step 1 find siblings by the row tuple through the catalog routes, never by rendering or matching a name; the sibling test registers a non-conformant name. The list route has no `owner_id` filter today, noted in Gap 4.
- 2026-10-18: Fix check on 7d26a0a93 failed (3 of 3, one BLOCKS-PLANNING: the superseded-target branch and its test still found the target by name, and `for_tuple` cannot see a superseded row). The target lookup now reads the list route without `lifecycle_state`, and the revive branch and its Phase 6 Step 2 test key on the row tuple. The three DISCOVER-AT-IMPLEMENTATION items every critic raised are for Phase 4 Step 1 and Phase 6 Step 2: the client `list_collections` gains a `content_type` parameter, the Phase 4 fixture registers a non-conformant row past the strict guard, and the `--to` source model is read from the row, not the name.
- 2026-10-18: Gate round 3 — PASSED (0 Critical, 0 Significant, 0 ship-blocker(s)); commit `ecd51d2c3`; critique `nexus_rdr/210-gate-critique-2026-10-18-r3`.
- 2026-10-18: Accept dispositions (residuals by bead): S-R3-1 nexus-b4w2n and nexus-a0814; S-R3-2 nexus-lard5; S-R3-3 nexus-noayy; S-R3-4 nexus-zyu6n; S-R2-2 nexus-6grho; S-R2-3 nexus-hx3c8; S-R2-4 nexus-q6t89; S-R2-5 nexus-n6mh0; target row selection nexus-5ki2n; client parameter and fixture nexus-dxifx; admission-gate order nexus-e81g7; native-load errors nexus-lt84r.
