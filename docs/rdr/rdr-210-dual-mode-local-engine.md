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
longer answers, and nothing carries the user's choice to the engine.

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

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Dual router | `EmbedderRouter(OnnxEmbedder, String, String)` | Extend: retype to `Embedder`, make the key nullable, delete the MiniLM-typed form |
| Profile input | `seedEmbeddingProfile` | Extend: seed from the passed choice |
| Key plumbing | `storage_service_daemon.py:922-951` | Replace the r5f3c branch |
| Sibling reads | `corpus.py` collection resolution | Extend from one collection to every sibling |
| Egress guard | none | New test |

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

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Embedding profile rows | N/A (existing) | `nx doctor` profile row | N/A | `nx doctor` | N/A |

### New Dependencies

None.

## Open Questions

1. With both families available and no choice recorded, which model do new
   collections get?
2. Should a dual engine pick its reranker per request (Voyage for Voyage hits,
   the cross-encoder otherwise), or one per engine?
3. Does the keyed local gate leg run in the nightly (it spends Voyage tokens and
   needs a secret) or only in the release battery?

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
