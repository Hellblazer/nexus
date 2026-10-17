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
(T2 `nexus_rdr/210-open-questions-sam-2026-10-17`). The research findings that
settle the draft's open technical questions are T2 `nexus_rdr/210-research-10` to
`-19` (four of them spikes on chas). Every code citation below was re-read at
4d08d3861, on develop 6cd90aab5 (drift audit: T2
`nexus_rdr/210-drift-audit-2026-10-17`).

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
at all. Several client
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

#### Gap 2: The key does not reach the engine by default

The supervisor (`storage_service_daemon.py:1544-1590`) passes a configured key
only when `local.embed_model` names a Voyage model or records no choice at all
(the guard is `:1565`, the credential lookup `:1573`). That guard (nexus-r5f3c)
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
   Sam ruled it removed in every mode (nexus-3t0jv, landing ahead of this RDR).
- **Paths 2 to 4.** `VoyageEmbedder` (`:309`), `CceEmbedder` (`:359`) and `VoyageReranker`
   (`:152`), each to `api.voyageai.com` (URL constants `VoyageEmbedder.java:101`,
   `CceEmbedder.java:115`, `VoyageReranker.java:63`), built only when keyed.
- **Path 5.** `EgressProxy` (`EgressProxy.java:49`) routes 2 to 4 through an
   operator-configured proxy; it builds no client.
- **Paths 6 and 7.** DJL inside the tokenizer library (used by `Bge768Embedder`,
   `CrossEncoderReranker`, `OnnxEmbedder`): a call to the cloud instance-metadata
   address at every tokenizer build, and a native-library download on GPU hosts.
   Library code, not visible to a main-source scan (210-research-10; not re-read
   here). Both are closed by nexus-17jvq (ba8422171), which is not on develop.
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
  and recommends that prune (`health.py:8224-8275`). Under `voyage`, every served
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
  `health.py:8371`.
- **Gates.** `tests/e2e/cloud-client-path-gate.sh:705` asserts the mode is one
  of the two values.
- **Engine.** `modeName`, the refusal text (`EmbedderRouter.java:507-590`), and
  `local_embed_activity`, wired only in the keyless branch (`Main.java:309`).

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
| RDR-223 | Precedent and constraint | Its ownership guard, enforced on local engines (`storage_service_daemon.py:1489`), refuses any chunk write whose chash has no live manifest row in the target. It deleted staging. Its combined write (`CombinedWriteService`, `CatalogRepository.writeManifestMany`) writes a document's chunks and manifest rows in one transaction; the re-embed (Phase 6) moves documents through that primitive and needs no exception to the guard. |
| RDR-225 | Constraint | Chunks are partitioned by model, and every seeded model has a partition (vectors-030 `:703-713`). The manifest-to-chunk foreign key includes the model (vectors-030 `:1017-1021`). It states (rdr-225:554): "Cross-model migration registers a new collection under the target model. There is no in-place re-embed, and none is added." The re-embed here copies into a new sibling and so agrees. |
| RDR-192 | Constraint | live(c) decides when moved chunks become visible, and the reaper's grace and floor decide what happens to the source's chunks after the move (Technical Design, re-embed section). |
| RDR-227 | Adjacent (closed) | Its reconciler builds a per-collection index for any live, unsuperseded collection above its row threshold and drops it when `superseded_by` is set (rdr-227:366-370, :418-428). A re-embed target therefore gets its index while the job runs, and the source's index goes at supersede. No change needed. |
| RDR-224 | Adjacent (closed) | Native Windows runs bge on the Windows ONNX runtime (rdr-224:289, :322), with the ORT init gate and temp sweep at boot (`Main.java:67`, `:71`). The dual boot must hold on Windows; the coexistence spike (Phase 1 Step 1) includes the win-x64 image. |
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

The spikes ran on local branches (`spike/nexus_911-rdr210-copy-cutover`
0d3ebc1b8, `spike/nexus_911-rdr210-dual-native` a86b4d0d1), not on develop.
Every cited line was re-read at 4d08d3861; where a record and the code disagree,
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
- **Documented**: posture is chosen once, from key presence (`Main.java:238, 251`).
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
  before any engine reports `dual` (Phase 1).
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
the key whenever one is configured. Client reads span every model sibling, and
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
2. **Profile policy.** The supervisor passes `NX_EMBED_PROFILE` (`bge` or
   `voyage`), derived from `local_embed_model_is_voyage()` when a choice is
   recorded and left unset when none is. The engine seeds the profile from it and
   refuses to boot on a choice it cannot serve (`voyage` with no key, `bge` with
   no bge files), naming the missing piece. Unset keeps today's rule: keyed means
   Voyage (Sam, 2026-10-17, Q1).
3. **Rerank.** One reranker per engine, chosen by the profile (Sam,
   2026-10-17, Q2 and its refinement): an engine whose profile names Voyage
   reranks every search with Voyage, including bge-only results; an engine whose
   profile is bge reranks with the cross-encoder even when keyed, and uses the
   key only to serve existing Voyage collections. A keyless engine uses the
   cross-encoder. Nothing is sent to Voyage for a user who did not choose it.
4. **Supervisor.** Pass the configured key regardless of `local.embed_model` and
   retire the nexus-r5f3c branch (`storage_service_daemon.py:1544-1572`), whose
   reason (Gap 1) is gone.
5. **init and the ladder.** Landed on develop (nexus-ddmfg, Gap 2); not scope here.
6. **Client reads.** The single-name resolver returns every live sibling of the
   owner's corpus, not one; callers that need one name for a write keep the write
   resolver. Searches already split per model and merge client-side.
7. **Egress allowlist and guard (nexus-umm29).** The carve-out is stated as an
   allowlist: *the local engine connects only to the Voyage API
   (`api.voyageai.com`) from `VoyageEmbedder`, `CceEmbedder` and
   `VoyageReranker`, and only when keyed, optionally through the operator's proxy
   (`EgressProxy`); and to its database at `NX_DB_URL` over JDBC. Nothing else.*
   That covers census paths 2 to 5 and 8 (Gap 5). Path 1 is removed in every mode
   (Sam, 2026-10-17; nexus-3t0jv, landing ahead). Paths 6 and 7 are closed by
   nexus-17jvq, which must land before the guard. Two tests hold it:
   - **Source census.** A main-source scan, shaped like
     `OwnershipGuardCoverageScan`: strip comments, then every outbound construct
     (`HttpClient.newBuilder`, `HttpClient.newHttpClient`, `openConnection(`,
     `openStream(`, `new Socket(`, `ServerSocket`, `SocketChannel.open`,
     `DriverManager.getConnection`, `ProcessBuilder`, `Runtime.getRuntime().exec`)
     must sit in a file on an allowlist that carries a reason per entry. The scan
     asserts it found at least as many sites as the allowlist names
     (non-vacuity), and that each Voyage class's URL constant names
     `api.voyageai.com`. It also reads `service/pom.xml` and fails on a Liquibase
     version of 4.30 or later unless its analytics are configured off. If
     nexus-17jvq's own scan lands first, this extends it.
   - **Runtime witness.** A child JVM that installs a `URLStreamHandlerFactory`
     refusing any non-`file`/`jar` stream, then builds the bge embedder and the
     cross-encoder (the tokenizer builds) and runs one embed and one rerank. It
     catches library code the source scan cannot see (paths 6 and 7). It does not
     see `java.net.http`, which is why the source census covers `HttpClient`.
8. **Java comments.** The stale router comments are corrected here, since they
   describe the router this RDR changes: `Main.java:35-38` (says an ONNX fallback
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
    before any engine can report `dual`:
    - The chunk cap and the byte budget follow the target collection's model: a
      bge collection gets the ONNX memory cap, a Voyage collection the Voyage
      rules (`http_vector_client.py:1061`, `:1175`, `:1250-1259`).
    - Thresholds and cross-family ranking: Technical Design 12.
    - The probe memo accepts `dual` (`http_vector_client.py:6049`).
    - Remedy and display text names the family the collection needs, not the
      engine's mode (the remedy and display sites in Gap 8).
    - The gate assertion accepts `dual` (`cloud-client-path-gate.sh:705`).
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
reason and counts the skip; the next pass sees it wherever the client put it. A
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
(Key Discoveries), plus the gate's other documented exclusions. REPOINT rows move
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
| `relevance_log`, `search_telemetry`, `hook_failures`, `gc_audit` | LEAVE | History; `deleteCollection` does not touch them (`CatalogRepository.java:8341-8468`) |
| `pdf_pipeline`, `chash_remap` | LEAVE | The gate's documented exclusions (transient queue; permanent ledger, nexus-4nll0) |

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
job to `done` (`--wait`); otherwise the `done` report names the command
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
  delete removes documents by `physical_collection` (`:8465`). By then the
  satellites are re-homed, so the delete takes only the source's taxonomy (being
  rebuilt) and rows no document owned. `--keep-source` opts out (Sam,
  2026-10-17).
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
  run continues from what remains in the source. `--wait` blocks until the job
  is `done` or `failed` and, on `done`, runs taxonomy discover for the target.

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
| Mode string | `modeName()` (`:168-169`) | Extend: add `dual` |
| Key plumbing | `storage_service_daemon.py:1544-1590` | Replace the r5f3c branch |
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
- A key the supervisor can find now always reaches the engine. A bge-profile
  engine still reranks locally (Technical Design 3), so the key causes Voyage
  traffic only to embed for and search existing Voyage collections.
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
  10), and the client fix ships first.
- **Risk**: an old client sends Voyage-sized requests to a dual engine's bge
  collections. **Mitigation**: measured in Phase 1 Step 1; a minimum client
  version on failure (Technical Design 10).
- **Risk**: the dual process fails on a platform the spike did not cover.
  **Mitigation**: Phase 1 Step 1 runs macOS arm64 and Windows x64 first; on
  failure the RDR goes back to planning before any engine step.

### Failure Modes

A family the engine cannot serve still gets 422 naming the missing piece (the
key, or the bge model files; `EmbedderRouter.java:547-556`). The boot banner names
both families, the profile's choice and the reranker. A profile choice the engine
cannot serve stops the boot with that reason in the service log. A re-embed batch
that fails leaves the job `failed` with the cursor at the last committed document
and the error in the row; the documents not yet moved are still served from the
source, so search is whole. A skipped document (reason
`move_precondition_failed`) is counted in the row and in `--status`. When one
model group's rerank degrades and another's does not, the merge falls back to
calibrated order and warns (Technical Design 12).

## Implementation Plan

### Prerequisites

- [x] CA-1 verified on linux-amd64 (210-research-19); the remaining platforms and
  loads are Phase 1 Step 1, which gates every engine step.
- [x] CA-5 verified by source; CA-2 verified where it is used, its residuals
  measured in Phase 4 Step 3 before Phase 6 ships.
- [x] Non-engine doc sweep and the nexus-umm29 wording landed (f7cae33ec).
- [x] The init keep-voyage fix landed on develop (nexus-ddmfg,
  c03352a2a..6cd90aab5).
- [x] Open Questions 1-3 decided (Sam, 2026-10-17).
- [ ] `HttpsSchemeHandler` and `POST /v1/vectors/resolve` removed (nexus-3t0jv,
  claimed by another session) before Phase 2 Step 6.
- [ ] The DJL instance-metadata and native-download paths closed (nexus-17jvq,
  ba8422171, not on develop) before Phase 2 Step 6, whose runtime witness would
  otherwise fail on them.

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
that spike did not, from its branch (`spike/nexus_911-rdr210-dual-native`
a86b4d0d1) or its successor. Each check records VmHWM (or the platform's peak
working set) and is a research finding. Any fail stops the engine phases and
sends the RDR back to planning.

- **Platforms.** Native image on macOS arm64 and Windows x64 (the RDR-224 runtime
  with its ORT init gate and temp sweep). **Pass**: the image builds and boots
  dual, returns a 768-dimension bge vector, a cross-encoder rerank and, through a
  loopback stub on the URL-taking constructors, a 1024-dimension vector from each
  Voyage class; exits 143 on SIGTERM, also during bge initialisation, with no
  crash report; a keyed boot without model files never loads ONNX runtime.
  **Fail**: any build error, reflection or JNI conflict, crash or wrong exit code.
- **Voyage serving in the dual process.** linux-amd64, a real key from the
  operator's credential chain. **Pass**: one embed through each Voyage class and
  one Voyage rerank return results while bge serves in the same process, and peak
  memory stays within the bge-only run's range plus 10%.
- **Concurrent load.** linux-amd64, three concurrent writers to a bge collection
  (`flush_concurrency=3`) with reranks running alongside. **Pass**: the dual
  process's peak is within 10% of a bge-only process under the same load, and
  under the 3.03 GB measured for bge-only at that concurrency
  (`Bge768Embedder.java:101-105`).
- **Old-client request shape.** The same load sent with the Voyage chunk cap and
  byte budget a pre-`dual` client would use (Technical Design 10). **Pass**: the
  same bound as the concurrent-load check. **Fail**: the wire ledger entry for
  `dual` carries a minimum client version instead.
- **Native-library extraction (nexus-8ool4).** If nexus-8ool4 (c62708f91,
  `OrtNativeDir.applyAtBoot` on every boot) has landed by then, a keyed boot with
  no model files. **Pass**: boot memory within 10% of the spike's 161 MB, and
  either nothing extracted or the extraction recorded as accepted in the bead.
  Not applicable while nexus-8ool4 is off develop (no `OrtNativeDir` in main at
  4d08d3861).

#### Step 2: Mode string and per-collection decisions

Change the 22 site groups of Gap 8 to the rules in Technical Design 10: chunk cap
and byte budget by collection model, the memo and the gate accept `dual`, remedy
text by collection family, `embedding_models` as the authority. **Test**: unit
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

Seed from `NX_EMBED_PROFILE`; refuse an unservable choice at boot. **Test**:
engine integration tests for each value with and without the key and the files,
asserting the seeded rows or the boot refusal.

#### Step 4: Rerank, banner and status

One reranker, chosen by the profile (Technical Design 3); the search route's
rerank stage dispatches to it for every group. The banner names both families,
the profile and the reranker; `local_embed_activity` is reported whenever bge is
built, not only in the keyless branch (`Main.java:309`). **Test**:
`StatusHandler` and `VersionHandler` tests for the dual shape; a test that a
bge-profile engine with a key reranks with the cross-encoder and makes no Voyage
rerank call.

#### Step 5: Dual journey test

Rewrite `Rdr204Gh1461ProfileRestartJourneyTest` to use one production dual router
for both collections, removing the separate bge router and the fake. **Test**: the
rewritten test itself.

#### Step 6: Egress guard

After nexus-3t0jv and nexus-17jvq are on develop. Add both tests of Technical
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
again. An absent key is a FAIL, not a skip. **Test**: the script's own guard test,
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

`reembed_jobs` changeset, runner on the scheduler, states and cursor. **Test**:
restart mid-job resumes from the cursor; two starts on one source are refused; a
failed batch leaves `failed` with the cursor at the last committed document.

#### Step 3: Status route and verb

`GET /v1/vectors/reembed/status`; `nx collection re-embed --all`, cross-model
`--to`, and the job controls; token estimate and cap. **Test**: CLI tests for the
plan, the cap refusal, and each control.

#### Step 4: Cutover and source removal

Supersede at zero live documents; the job deletes the source once it holds no
document row, unless `--keep-source`; `--wait` runs taxonomy discover for the
target on `done`. **Test**: after the last move the source is superseded and out
of the routing listing; it is deleted only when it holds no document row, and
kept with `--keep-source`; after the delete, no aspect, highlight or queue row of
a moved document names the source, and the moved documents' aspects are intact;
after `--wait`, the target has a `taxonomy_meta` row and topic assignments for
its chunks; without `--wait`, the doctor's drift row names the discover command.

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
- **Scenario**: `NX_EMBED_PROFILE=voyage` with no key — **Verify**: boot refused
  naming the key.
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
fetcher's removal is out of scope and lands ahead (nexus-3t0jv), and so is any
change to the managed engine's posture: the cloud boots Voyage-only exactly as
today.

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
  unchanged, and the re-embed job runs in both (in the managed engine it moves
  Voyage-to-Voyage only, when a profile changes).
- **IDE compatibility**: N/A.
- **Incremental adoption**: each phase ships alone. Phase 1 is client-only and
  safe against today's engines. A dual engine without Phase 4 still serves both
  families; only single-name reads stay on one sibling.
- **Secret/credential lifecycle**: the key keeps its one source (the credential
  chain, then `NX_VOYAGE_API_KEY` at spawn); what changes is that it is passed
  whenever present. Rotation is a service restart, as today. An ambient key on a
  bge profile causes no rerank traffic (Technical Design 3).
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
- `service/src/main/java/dev/nexus/service/Main.java:232-350`
- `service/src/main/java/dev/nexus/service/vectors/EmbedderRouter.java`
- `service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java:597-935`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java:5182-5260, 6372-6414, 7873-7990, 8280-8468, 8492, 8804-8823, 9018-9168`
- `service/src/main/java/dev/nexus/service/db/CombinedWriteService.java`
- `service/src/main/java/dev/nexus/service/ChunkReaper.java`
- `service/src/main/java/dev/nexus/service/http/RequestContext.java`, `vectors/RequestDeadlineProbe.java`
- `service/src/test/java/dev/nexus/service/OwnershipGuardCoverageScan.java`
- `service/src/main/resources/db/changelog/aspects-001-baseline.xml`, `fk-001-catalog-cross-store.xml`, `taxonomy-014-topics-tenant-unique.xml`, `vectors-030-model-tenant-partition-functions.xml`
- `tests/catalog/test_collection_scoped_tables_schema_parity.py`
- `src/nexus/daemon/storage_service_daemon.py:1479-1590`
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
  "pending" marker replaced, cites re-read at 4d08d3861 (develop 6cd90aab5).
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
- 2026-10-17: Gate round 1 — PASSED (0 Critical, 6 Significant, 0 ship-blocker(s)); commit `924fa8256`; critique `nexus_rdr/210-gate-critique-2026-10-17-r1`.
