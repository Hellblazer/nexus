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
related_rdrs: [RDR-160, RDR-204, RDR-157, RDR-188, RDR-109]
---

# RDR-210: One Local Engine Serves Both Embedders: bge-768 and Voyage Collections Side by Side

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Sam, 2026-09-15: "we allow voyager embedding in local mode. this
is correct. this will not change", then "local can use both and we require that
now". An investigation the same day (T2 `nexus/voyage-local-mode-investigation-2026-09-15`
[25872], read at origin/develop a9551ee21) found that local mode can use Voyage,
but only as a switch for the whole engine, and that one engine cannot serve both
kinds of collection. This RDR designs the engine that can.

Terms used throughout:

- **Engine**: the Java `nexus-service` process. In local mode the nx client's
  supervisor spawns it on the user's machine against a bundled Postgres.
- **Embedder**: the component that turns text into a vector. Local mode ships
  bge-768 (the bge-base-en-v1.5 model, 768 dimensions, run on-device through
  ONNX runtime). Voyage is a paid remote API; `voyage-code-3` embeds code and
  `voyage-context-3` embeds prose.
- **Posture**: which embedders one running engine holds. Today there are two,
  chosen at boot: *keyless* (bge-768 only) and *keyed* (Voyage only). The key is
  the `NX_VOYAGE_API_KEY` environment variable in the engine's process.
- **Embedding profile**: the engine-written table (RDR-204) that names the model
  each content type's NEW collections get. An existing collection keeps the
  model recorded in its own catalog row.

## Problem Statement

A local user who has both bge-768 collections and Voyage collections can have
only one kind searchable at a time. Restarting the engine with a key makes every
bge-768 collection fail with HTTP 422, and the same restart without a key does
the same to every Voyage collection. By default the key never reaches the engine
at all, and re-running `nx init` quietly undoes a Voyage opt-in.

### Enumerated gaps to close

#### Gap 1: One engine holds one embedder family per boot

`Main.java:163-233` builds a Voyage-only router when the key is present and a
bge-only router when it is not. The Voyage router's dispatch table has no bge
entry (`EmbedderRouter.java:149-159`) and the keyless table has no Voyage entry
(`EmbedderRouter.java:87-101`). `resolveEmbedderStrict` (`EmbedderRouter.java:543-559`)
looks up the collection's catalog row by its `embedding_model`; a miss throws
`EmbeddingModelUnavailableException`, which the vector handler maps to 422. So
embed, write and search against the other family's collections are all refused.
In a multi-collection search the client turns that into a per-collection error
row, and results silently come back from one family only.

#### Gap 2: The key does not reach the engine by default, and init reverts the opt-in

The supervisor (`storage_service_daemon.py:922-951`) passes a configured key only
when `local.embed_model` names a Voyage model. That guard (nexus-r5f3c) exists
precisely because of Gap 1: a key flipped the engine Voyage-only and broke the
user's bge collections. `nx init` writes `local.embed_model = bge` on every local
provision (`init.py:73`, via `provision_service_stack` at `init.py:685`), and so
does the upgrade ladder's provision leg (`upgrade_ladder/provisioning.py:293,
381-384`). A user who opted into Voyage and later re-runs `nx init`, which is
documented as safe to re-run, loses the opt-in at the next service restart.

#### Gap 3: The profile has no input for choosing between two available models

The engine seeds the embedding profile from its posture on every boot
(`Main.java:247-254`, `EmbedderRouter.contentTypeModelTokens` at 227-238): a
keyed boot names the Voyage models, a keyless boot names bge-768. Once one engine
holds both, "which model do new collections get" becomes a choice the posture no
longer answers, and nothing carries the user's choice to the engine. The same
holds for rerank: today the posture also picks the reranker (Voyage when keyed,
the ms-marco cross-encoder when keyless, `Main.java:172-226`), and a dual engine
needs a rule for which one a mixed-result search uses (Open question 2).

#### Gap 4: Client reads resolve one physical collection per corpus

When a corpus has both a bge-768 collection and a Voyage sibling, the client
reads only one of them (`corpus.py:1437-1446` and `477-504`, pinned by
`tests/test_rdr_109_phase2_dispatch.py:251-272`). Once a Voyage sibling exists,
the bge collection is never read again. Even a dual engine would leave that
content invisible.

#### Gap 5: The engine's outbound Voyage call has no recorded, bounded carve-out

RDR-157 and RDR-160 state that the local engine makes no outbound HTTP. Sam
decided on 2026-06-15 (nexus-umm29) that the Voyage embed and rerank call is a
sanctioned exception, and that the exception must be bounded: a guard or test so
a later change cannot add other outbound calls under it. The wording is being
amended alongside this draft; the guard does not exist.

#### Gap 6: No test or gate runs a keyed local engine, let alone a dual one

The local-service gate runs the keyless posture and deselects `cloud_mode` tests
(`tests/e2e/local-service-gate.sh:61-77`). `Rdr204Gh1461ProfileRestartJourneyTest`
holds both kinds of collection in one database, but reads the bge one through a
separate bge router (its own header, lines 50-59, says so) and writes the Voyage
one through a fake; the production Voyage router would refuse that read.
`tests/e2e/rdr195-voyage-mvv.sh` runs a keyed local engine by hand, Voyage only.

#### Gap 7: Nothing moves an existing collection onto the new default

Once a key arrives, every collection the install already holds stays bge-768.
`nx collection reembed` is in-place and refuses a cross-model target in service
mode, because the collection name encodes the model, so it cannot move a bge
collection to Voyage at all. The only cross-model path is a re-index from source
(`embed_migrate.migrate_collection_safe`), which drops every chunk that has no
source file (MCP-stored notes, DEVONthink content) unless the caller accepts the
loss, runs as a client loop that dies with the terminal, keeps no cursor, and
reports nothing while it runs. A user who says "I have Voyage now, re-embed
everything" has no command that does that safely.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-160 | Origin | Decision 2: "`EmbedderRouter` local mode routes all collections to the bge-768 embedder". Its rationale was a greenfield default for users without a key, and the no-outbound-HTTP invariant (Decision 4). The default still holds; routing *all* collections to bge does not, since Sam requires Voyage in local mode. This RDR revises Decision 2 only; bge-768 stays the keyless default and the parity gate is untouched. |
| RDR-204 | Origin | §1a says reads of a collection whose model differs from the profile "route by the row's own model and are never refused". The code refuses them whenever the running posture lacks that model. A dual engine makes the sentence true for bge-768 and Voyage. |
| RDR-157 | Origin | States the local-engine topology invariant. The nexus-umm29 carve-out (Gap 5) amends it; this RDR adds the guard that keeps the carve-out bounded. |
| RDR-188 | Precedent | Moved all Voyage traffic, embed and rerank, into the engine and made the key engine-bootstrap material only. This RDR keeps that: the client still never calls Voyage. |
| RDR-109 | Origin | Honest local-mode naming introduced the per-corpus collection resolution that Gap 4 changes from "one physical collection" to "every model sibling". |

Searched `docs/rdr/README.md` and the RDR collection for "dual", "local voyage",
"embedder router" and "embed_model"; no other RDR covers serving two families
from one engine.

## Context

### Background

nexus-35ok4 (GH #1461, 2026-08-18) made the client half of local Voyage work: a
user sets `local.embed_model` to a Voyage model and `voyage_api_key`, restarts the
service, and new collections are minted with Voyage names. It filed the engine
half as nexus-ddmfg, whose remedy 2 was this dual-mode router. nexus-ddmfg was
closed on 2026-08-29 as "reopen if a real trigger appears"; Sam's requirement is
that trigger, and the bead is reopened.

### Technical Environment

- Engine: Java, GraalVM native image, `EmbedderRouter` with a model-token
  dispatch table (RDR-103 conformant names, RDR-204 catalog rows as authority).
- bge-768: `Bge768Embedder`, ONNX runtime, admission-controlled by one
  process-wide `LocalOnnxAdmission` shared by the document and query routers and
  the cross-encoder reranker (`Main.java:196-226`).
- Voyage: `VoyageEmbedder`, `CceEmbedder`, `VoyageReranker`.
- The cloud engine has no ONNX model on disk. Constructing an ONNX session on a
  missing file segfaults the process rather than throwing (conexus-qcn,
  `Main.java:164-169`), so the cloud path must never construct one.

## Research Findings

### Investigation

Source reading at a9551ee21, recorded in T2 [25872]; the engine and supervisor
lines cited above were re-read for this draft.

### Key Discoveries

Re-embed section, 2026-09-16 (T2 `nexus_rdr/210-research-1` to `-4`):

- **✅ Verified** (source search) — The engine embeds server-side on
  `upsert-chunks` when no vectors are sent (`VectorHandler.java:368`), and with
  `force_re_embed` unset a chash that already carries a vector gets a
  metadata-only update (`PgVectorRepository.java:641`), so skipping present
  chashes falls out of the ordinary write.
  *Source: T2 210-research-1*
- **✅ Verified** (source search) — Cutover reuses existing branches: the
  cross-model rename repoints `catalog_documents` and `catalog_document_chunks`
  and needs a live target (`CatalogHandler.java:1963, 2049`); the manifest-to-
  chunk foreign key has no pre-check, so a chash absent from the target aborts
  the repoint rather than skipping; `supersedeCollection`
  (`CatalogRepository.java:6884`) and the resolver's superseded-row exclusion
  (`:6954`) exist; the profile is one row per tenant and content type
  (catalog-036-3).
  *Source: T2 210-research-2*
- **✅ Verified, draft corrected** (source search) — Voyage batches are planned
  under a token budget up to 1,000 inputs (`VoyageEmbedder.MAX_BATCH_TEXTS`),
  not 128, and the 300-row write cap is a client convention
  (`limits.py:60`) the engine does not enforce on `upsert-chunks`.
  *Source: T2 210-research-3*
- **✅ Verified, draft corrected** (source search) — `nexus.live_chunks` is the
  tombstone-filtered view; chash-ordered pagination exists in the repository
  (`PgVectorRepository.list`), while `getAllMetadata` is a single call capped at
  200,000 rows; the engine has a boot-registered periodic scheduler (the tuple
  sweep) but no persisted, resumable job, so the `reembed_jobs` row and cursor
  are new.
  *Source: T2 210-research-4*

- **Documented**: the posture is chosen once, from key presence (`Main.java:150,
  163`).
- **Documented**: an engine constructor that holds a local embedder and Voyage
  together already exists, `EmbedderRouter(OnnxEmbedder, String, String)` at
  `EmbedderRouter.java:111-127`, typed to the retired MiniLM `OnnxEmbedder` and
  used only by tests and the parity gate.
- **Documented**: the profile is rewritten on every boot from the posture
  (`EmbedderRouter.java:227-273`).
- **Documented**: the combined multi-collection query already refuses a mix of
  models (`PgVectorRepository:2240-2248`), and the client already splits a search
  per model.
- **Documented**: rerank follows the posture: `VoyageReranker` when keyed, the
  ms-marco cross-encoder when keyless (`Main.java:172-226`).

### Critical Assumptions

- [ ] A bge-768 embedder and the Voyage embedders can be built in one native
  image process without conflict, and the bge model's presence on disk can be
  checked before any ONNX session is created. — **Status**: Unverified
  — **Method**: Spike
- [ ] Merging results from a bge-768 collection and a Voyage collection into one
  ranked list is sound when the fused rerank stage runs, because the reranker
  scores query and text directly rather than comparing the two families' vector
  distances. — **Status**: Unverified — **Method**: Source Search, then Spike
- [ ] No other code path assumes "keyed means no local embedder" (for example
  `local_embed_activity` on `/v1/status`, `modeName()`, the doctor rows).
  — **Status**: Unverified — **Method**: Source Search

## Proposed Solution

### Approach

One router per input type holds every embedder the machine can run: bge-768 when
its model is on disk, Voyage when the key is present. Dispatch stays by the
collection row's model, so each collection is embedded by the model its vectors
were made with, and nothing is refused that the machine can serve. The user's
`local.embed_model` becomes the profile input that picks the model for new
collections. The supervisor passes the key whenever one is configured, and
`nx init` stops overwriting the choice. Client reads span every model sibling of
a corpus.

### Technical Design

1. **Engine router.** Generalize the existing mixed constructor to take any
   local `Embedder` (the admission-wrapped bge) plus a nullable key. Main builds
   the bge embedder when the resolved ONNX model root holds the bge files, and the
   Voyage embedders when the key is set. Neither present is a boot failure with a
   named cause. The cloud container has no bge files, so it boots Voyage-only
   exactly as today.
2. **Profile policy.** The supervisor passes the user's choice to the engine as
   an environment variable (name decided in Phase 1). The engine seeds the
   profile from it, refusing at boot a choice it cannot serve (for example Voyage
   chosen with no key). With no choice set, the profile keeps today's rule (keyed
   means Voyage).
3. **Rerank.** Keyed engines rerank with Voyage and keyless ones with the
   cross-encoder, as today. Whether a dual engine should pick the reranker per
   request is left open (Open question 2).
4. **Supervisor.** Pass the configured key regardless of `local.embed_model` and
   retire the nexus-r5f3c withholding branch, whose reason (Gap 1) is gone.
5. **init and the ladder.** Write `local.embed_model` only when it is absent.
6. **Client reads.** Resolve every model sibling of a corpus and search each,
   then merge. The engine already refuses a mixed-model combined query, so the
   client issues one query per model.
7. **Egress guard (nexus-umm29).** A test that fails when engine source
   constructs an outbound HTTP client anywhere other than the Voyage embed and
   rerank classes.
8. **Docs.** The Java comments the investigation listed as stale (T2 [25872]
   items 1-13 and 22) are corrected here, since they describe the router this RDR
   changes. The non-engine sweep lands ahead of this RDR.
9. **Re-embed to the profile default.** One engine job moves a collection from
   the model it was embedded with to the model the profile names for its content
   type, and `nx collection reembed --all` runs that job over every collection
   that differs. See the next section for the contract.

### Re-embed to the profile default

`reembed` answers one question, "does every live collection carry the vectors
the profile now asks for", and makes the answer yes. The profile is the input:
a collection is stale when its row's `embedding_model` differs from the profile's
model for its `content_type`. Quarantine, dormant and disputed rows are never
candidates; they drain through their own lifecycle.

**The move is a copy into the model sibling, never an in-place rewrite.** The
target is the sibling named for the profile model
(`code__1-1__voyage-code-3__v1` beside `code__1-1__bge-base-en-v15-768__v1`),
registered live if absent. The job walks the source's live chunks (`nexus.live_chunks`) in chash
order by keyset pagination on the repository (`PgVectorRepository.list`'s
ordering; the unpaged `getAllMetadata` is capped at 200,000 rows and is not the
tool) and upserts each batch's text and metadata into the target with no vectors, so
the engine embeds them with the target's model exactly as an index write would.
Chunks are content-addressed, so a chash already present in the target is done
and is skipped; a batch that was written and not acknowledged is rewritten to
the same rows. That is what makes the job idempotent: re-running it after a
crash, a restart, or completion converges on the same target and writes nothing
new the second time. Nothing is re-indexed from source, so sourceless chunks
(notes, DEVONthink content) move with everything else, which the reindex path
could not do.

**Cutover is the existing catalog branch.** When the target's live chash set
equals the source's, the job repoints manifests through the cross-model rename
branch (`renameCollectionTxn`, which requires the target chunks to exist first:
the manifest-to-chunk foreign key aborts a repoint onto an empty target), then
supersedes the source row. The source's chunks are then unreferenced and drain
through the ordinary quarantine sweep; the job never deletes them itself. Client
reads already span both siblings during the window (design item 6), so search is
whole throughout.

**Batched.** One bounded transaction per batch, each with its own statement
bound, so the job never holds a lock across a Voyage round trip and a cancelled
batch is one batch. The batch size is the job's own bound (300, the client
write convention in `limits.py`; the engine enforces no row count on
`upsert-chunks`). The engine's embed path already plans Voyage batches under a
token budget of up to 1,000 inputs per request and retries 429s with the
server's `Retry-After` budget.

**Managed.** The job is a row in a new engine table, `reembed_jobs` (tenant,
source, target, state, chash cursor, batches done, chunks done, chunks skipped,
tokens billed, started, updated, finished, last error), created through
Liquibase. States: `planned`, `running`, `paused`, `cutover`, `done`, `failed`.
A job runner registered at construction, the shape the tuple sweep already
uses, picks up `running` rows at boot, so a job survives a service restart and
a closed lid, and a client is not needed once the job is started. The persisted
row and the cursor are new: the engine's existing sweeps are stateless per run
or hold their memory in-process. `nx collection reembed --all` plans (lists every stale collection with
its chunk count and an estimated Voyage token spend, and exits with a plan under
`--dry-run`), starts, and then reports; `reembed status`, `pause`, `resume` and
`abort` act on the row. Abort leaves the target in place and unreferenced, so a
later run resumes from what was copied. Two jobs never run against one source.

**Monitored.** `GET /v1/vectors/reembed/status` returns every job row for the
tenant. `nx doctor` gains two rows: collections whose model differs from the
profile (the drift the command exists to close) and running or failed jobs.
Each of those is not-applicable on an install with one model.

**Logged.** The engine logs one event per batch (`reembed_batch`: job, source,
target, cursor, chunks written, chunks skipped, elapsed, tokens) and one per
state change; the client logs the plan it submitted and each status poll. A
failed batch is logged with the engine's error text and leaves the job `failed`
with the cursor at the last good batch.

**Cost.** The plan states the token estimate before anything runs, from the
source's live chunk text lengths, and a spend cap on the command refuses a plan
above it. On-device bge re-embeds cost time, not money, and the estimate says
so.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Dual router | `EmbedderRouter(OnnxEmbedder, String, String)` | Extend: retype to `Embedder`, make the key nullable, delete the MiniLM-typed form |
| Profile input | `seedEmbeddingProfile` | Extend: seed from the passed choice |
| Key plumbing | `storage_service_daemon.py:922-951` | Replace the r5f3c branch |
| Sibling reads | `corpus.py` collection resolution | Extend from one collection to every sibling |
| Egress guard | none | New test |
| Re-embed copy | `nx collection reembed` (in-place, nexus-bw65), `embed_migrate.migrate_collection_safe` (reindex) | Replace both cross-model paths with the engine job; keep the in-place verb for same-model refreshes |
| Cutover | `renameCollectionTxn` cross-model branch, `supersedeCollection` | Reuse unchanged |
| Job table | none | New Liquibase changeset `reembed_jobs` |

### Decision Rationale

Dispatch by the row's model already exists and is the right authority; the only
defect is that the table is built from half of what the machine can run. Filling
the table fixes Gaps 1 and 3 in the engine, and Gaps 2 and 4 then reduce to
removing guards that only existed to route around Gap 1.

## Alternatives Considered

### Alternative 1: Keep one posture per engine and migrate on switch

**Description**: nexus-ddmfg's remedy 1. Switching `local.embed_model` offers a
guided re-embed of existing collections into the new family.

**Pros**:

- No engine change.

**Cons**:

- Only one family is served at a time, which is what Sam ruled out.
- A re-embed costs Voyage tokens or hours of on-device time.

**Reason for rejection**: fails the requirement.

### Alternative 2: The client embeds with Voyage and hands vectors to the engine

**Description**: nexus-umm29's option (b): the engine never leaves the machine.

**Cons**:

- Reverses RDR-188, which moved all Voyage traffic into the engine.
- Sam decided option (a) on 2026-06-15.

**Reason for rejection**: already decided against.

### Briefly Rejected

- **Two engine processes, one per family**: two Postgres connection pools and
  two supervisors for one install, to work around a dispatch table.

## Trade-offs

### Consequences

- A keyed local install keeps serving its bge-768 collections.
- A key in the environment now always reaches the engine, so a user with a key
  exported and a bge choice gets bge collections for new data and Voyage search
  over any Voyage collections they already have.
- The engine holds the bge model in memory even on installs that only use
  Voyage, when the model is on disk.
- A re-embed doubles a collection's storage until cutover and the quarantine
  drain complete, and spends Voyage tokens once per chunk moved; the plan says
  how much before the job starts.

### Risks and Mitigations

- **Risk**: result merging across families ranks badly without a reranker.
  **Mitigation**: Critical Assumption 2; measure before Phase 3 ships.
- **Risk**: the cloud engine constructs an ONNX session and segfaults.
  **Mitigation**: file-presence check before construction, and a native-image
  smoke with no model on disk.

### Failure Modes

A family the engine cannot serve still gets 422 naming the missing piece (the
key, or the bge model files). The boot banner names both families and which one
the profile picks.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified
- [ ] The non-engine doc sweep and the nexus-umm29 wording landed

### Minimum Viable Validation

A local engine booted with a real key and the bge model, one bge-768 collection
and one Voyage collection: write to and search both, restart, search both again,
and a mixed search returns hits from both.

### Phase 1: Engine

Router, profile input, boot banner, egress guard, Java comment corrections,
engine tests. Ships in an engine tag after engine-service-v0.1.120.

### Phase 2: Supervisor and init

Key plumbing, the profile variable, `nx init` and the ladder stop overwriting.

### Phase 3: Client reads

Sibling resolution and merged search.

### Phase 4: Gates

A local-service gate leg with a key, and the MVV above.

### Phase 5: Re-embed

The `reembed_jobs` changeset and job runner, the status route, the
`nx collection reembed --all` plan/start/status/pause/resume/abort verbs, the
two doctor rows, and the cost estimate. Ships in its own engine tag with the
client half paired. The MVV for this phase: a keyed local engine with two bge
collections, one of them sourceless, `reembed --all` moves both to Voyage
siblings, search returns the same documents before and after, a restart mid-job
resumes from the cursor, and a second `reembed --all` writes nothing.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Embedding profile rows | N/A (existing) | `nx doctor` profile row | N/A | `nx doctor` | N/A |
| Re-embed jobs | `nx collection reembed status` | same, per job | `abort` (row stays for the record) | second `reembed --all` is a no-op; `nx doctor` drift row | N/A |
| Superseded source collections | `nx collection list` | `nx collection info` | quarantine sweep | `nx catalog verify` | N/A |

### New Dependencies

None.

## Open Questions

1. With both families available and no choice recorded, which model do new
   collections get?
2. Should a dual engine pick its reranker per request (Voyage for Voyage hits,
   the cross-encoder otherwise), or one per engine?
3. Does the keyed local gate leg run in the nightly (it spends Voyage tokens and
   needs a secret) or only in the release battery?
4. Does a profile change offer the re-embed (a doctor row and a printed hint),
   or start it? Sam's framing is a command the user runs; the draft keeps it
   explicit.
5. What is the default spend cap for `reembed --all`, and is it per run or per
   collection?
6. Does cutover wait for a human `--cutover` confirmation, or follow parity
   automatically inside the job? The draft follows parity automatically, since
   client reads cover both siblings either way.

## Test Plan

- **Scenario**: dual engine, bge and Voyage collections in one database —
  **Verify**: embed, write and search succeed for both; no 422.
- **Scenario**: keyless engine, Voyage collection — **Verify**: 422 naming the key.
- **Scenario**: keyed engine with no bge files (cloud shape) — **Verify**: boots
  Voyage-only, no ONNX session constructed.
- **Scenario**: `nx init` re-run with `local.embed_model` set to Voyage —
  **Verify**: the value is unchanged.
- **Scenario**: corpus with both siblings — **Verify**: search returns hits from
  both.
- **Scenario**: engine source adds a new outbound HTTP client — **Verify**: the
  egress guard fails.
- **Scenario**: `reembed --all` on two stale collections, one sourceless —
  **Verify**: both Voyage siblings hold the source's live chash set, manifests
  point at the siblings, the sources are superseded, search results are the
  same documents.
- **Scenario**: engine restart mid-job — **Verify**: the job resumes from its
  cursor and the target holds no duplicate or missing chash.
- **Scenario**: `reembed --all` twice — **Verify**: the second run plans zero
  chunks and writes nothing.
- **Scenario**: a batch fails at Voyage — **Verify**: the job is `failed` with
  the cursor at the last good batch and the error text in the row; `resume`
  continues.
- **Scenario**: plan above the spend cap — **Verify**: refused before any write,
  naming the estimate and the cap.

## Finalization Gate

To be completed at gate time.

## References

- T2 `nexus/voyage-local-mode-investigation-2026-09-15` [25872]
- Beads nexus-ddmfg, nexus-umm29, nexus-r5f3c, nexus-35ok4
- `service/src/main/java/dev/nexus/service/Main.java:142-254`
- `service/src/main/java/dev/nexus/service/vectors/EmbedderRouter.java`
- `src/nexus/daemon/storage_service_daemon.py:897-951`
- `src/nexus/commands/init.py:30-120, 657-700`
- `src/nexus/corpus.py:352-669, 1437-1460`
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
