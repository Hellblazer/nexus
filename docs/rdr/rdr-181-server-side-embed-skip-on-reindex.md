---
title: "Server-Side Embed-Skip on Re-Index: Stop Re-Embedding Chunks Whose Vector the Store Already Holds"
id: RDR-181
type: Technical Debt
status: closed
closed_date: 2026-07-07
postmortem_waiver: "Clean single-phase arc, no divergences to learn from: accepted and fully implemented same-day (2026-07-05, epic nexus-f0r8p 13/13), phase-review-gate clean, shipped engine-service-v0.1.27 cloud-gated GREEN, live MVV proof 89.4% Voyage token reduction (T2 nexus/rdr181-mvv-live-proof-2026-07-05). Implementation matched the accepted design exactly."
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-07-05
accepted_date: 2026-07-05
related_issues: [nexus-duoak, nexus-duoak.11]
related: [RDR-155, RDR-156, RDR-108, RDR-160]
---

# RDR-181: Server-Side Embed-Skip on Re-Index

> Revise during planning; lock at implementation.

## Problem Statement

Re-indexing a repository re-embeds chunks the vector store already holds. Chunk
IDs are content hashes (`chash[:32]`, RDR-108), so an unchanged chunk re-ingested
on a later run is byte-identical — its embedding is already stored, yet the engine
calls Voyage again to recompute the same vector. On the common real-world path
(edit a few files, re-index the repo) the overwhelming majority of chunks are
unchanged, so most of the embedding spend (latency + Voyage token cost) is pure
waste. This is the residual, server-bound cost behind the duoak.11 throughput
work once the serial catalog-registration (RDR — nexus-8c0uv) and prune
(nexus-yz8bt) wall sinks were removed.

### Enumerated gaps to close

#### Gap 1: The engine embeds before the conflict check, so a known chash re-embeds

`PgVectorRepository.upsertChunksInternal` (`service/.../vectors/PgVectorRepository.java:307-518`)
dedups **within the request only** (:326-353), then calls the embedder on the whole
deduped list (:398-402), and only afterward runs `INSERT … ON CONFLICT (tenant_id,
collection, chash) DO UPDATE` (:507-512). The embed is unconditional and happens
*before* Postgres ever sees that the row already exists with a stored vector. So a
re-upsert of an existing `(tenant, collection, chash)` pays the full Voyage embed
cost and then overwrites the identical vector. There is no server-side
chash→vector reuse.

#### Gap 2: The CCE path pays this per-chunk and sequentially

For CCE collections (`docs__`/`rdr__`/`knowledge__`, voyage-context-3),
`CceEmbedder` issues one Voyage call **per chunk, sequentially**
(`service/.../vectors/CceEmbedder.java:110-138`). So the redundant-embed waste in
Gap 1 is N sequential round-trips for N unchanged prose chunks — the dominant
re-index latency sink for prose-heavy corpora.

#### Gap 3: The only existing avoidance is client-side, opt-in, and lossy

`HttpVectorClient.upsert_chunks` has `skip_existing` / `NX_UPSERT_SKIP_EXISTING`
(`src/nexus/db/http_vector_client.py:618-650`) which pre-filters IDs via an
`existing_ids` store-get probe before sending. It is **opt-in** (off by default),
**disabled under `--force`**, costs an extra probe round-trip per batch, and
**skips the `ON CONFLICT` metadata refresh** for existing chunks — so line-number
metadata can drift stale for identical chunk text after edits elsewhere in a file.
The default OOTB re-index therefore pays full re-embed.

## Context

### Background

Discovered during the duoak.11 throughput investigation (2026-07-05, T2
`nexus/duoak11-wall-decomposition-2026-07-05` and
`nexus/research-server-side-embed-reduction-2026-07-05`). After removing the two
serial-phase wall sinks (catalog registration 333s, prune 226s), the residual
upload wall is server-bound Voyage embedding. A `--force` first index (all-new
chunks) cannot benefit from any skip — but that is an artificial worst case. The
common real path is a **re-index of a mostly-unchanged repo**, where redundant
re-embedding is the real, recurring waste. This RDR targets that case.

**Scope disclaimer (gate-required):** RDR-181 does **not** move the
`nexus-duoak.11` P1 wall-clock gate, which measures a from-scratch ~1200-file index
(a `--force`/first-index workload this RDR gives zero benefit to — the existence
SELECT is gated off there). duoak.11's own tracked levers (CCE paging/concurrency,
the hooks slice) are separate. Landing RDR-181 must **not** be cited as progress
against `nexus-duoak.11`; it is a distinct steady-state-re-index optimization that
happens to share the same investigation origin.

### Technical Environment

- Engine: Java service (`service/`), pgvector T3 (RDR-155), embed routed by
  collection model segment (`EmbedderRouter.java:294`): voyage-code-3 (1 call per
  request) vs voyage-context-3 / CCE (1 call per chunk, sequential).
- Chunk identity: `chash[:32]` content hash is the Chroma/pgvector natural ID
  (RDR-108). Unchanged chunk ⇒ identical chash ⇒ identical stored vector.
- Vector table has a unique key on `(tenant_id, collection, chash)` (the ON
  CONFLICT target).

## Research Findings

### Investigation

Engine embed path mapped end-to-end (Explore agent, 2026-07-05, file:line evidence
in T2 `nexus/research-server-side-embed-reduction-2026-07-05`). Key confirmations:
no server-side chash→vector cache; embed is unconditional and pre-conflict; CCE is
per-chunk sequential; no proactive Voyage rate governor (only reactive 429 retry).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Engine `PgVectorRepository` | Yes | Embed at :398 precedes ON CONFLICT at :507; intra-request dedup only |
| Engine `CceEmbedder` / `VoyageEmbedder` | Yes | CCE = per-chunk sequential; code = 1 batched call; no cache |
| `HttpVectorClient` | Yes | `skip_existing` opt-in, probe-based, skips metadata refresh |

### Key Discoveries

- **Documented** — The vector store already *is* the cache: an existing
  `(tenant, collection, chash)` row holds the exact vector a re-embed would
  recompute. The fix is to read it, not to add a new cache.
- **Documented** — A server-side existence query (`SELECT chash FROM <table>
  WHERE tenant_id=? AND collection=? AND chash = ANY(?)`) partitions the deduped
  batch into have-vector vs need-embed in one round-trip, before the embed call.
- **Documented** — Metadata still needs refreshing for existing chunks (the
  RDR-108 manifest position / line numbers can change), so existing chashes take a
  **metadata-only UPDATE** (not a skip), preserving the stored vector — which
  fixes the Gap-3 staleness caveat the client `skip_existing` has.

### Critical Assumptions

- [x] Reusing the stored vector for an existing chash is correctness-neutral —
  **Status**: **Verified (by construction)** — **Method**: Source Search.
  `chash = sha256(chunk_text)[:32]` is a content hash (RDR-108), and the stored
  vector for `(tenant, collection, chash)` was produced by embedding that exact
  text under that collection's model (which is invariant — see below). So the
  stored vector *is* the canonical embedding for that (content, model) pair;
  reusing it is identical to what a re-embed would produce. We reuse, not
  recompute, so Voyage determinism is not load-bearing (it is separately
  Documented: embedding APIs are deterministic for identical input — no
  sampling). A truncated-input chunk stores a truncated-input embedding either
  way, so equivalence holds at the edge too.
- [x] The existence SELECT is cheap relative to the embed calls it avoids —
  **Status**: **Verified** — **Method**: Source Search. `chunks_384/768/1024`
  carry `PRIMARY KEY (tenant_id, collection, chash)`
  (`vectors-001-baseline.xml:80/110/140`) — the SAME tuple the SELECT keys on
  (and the ON CONFLICT target). A batch `chash = ANY(?)` over ≤ a few hundred
  keys is a PK-index lookup (ms); the embed calls avoided are 100s ms–seconds
  each (CCE = N sequential Voyage round-trips). Orders of magnitude cheaper.
- [x] Same collection ⇒ same embed model ⇒ stored vector is valid — **Status**:
  **Verified** — **Method**: Source Search.
  `EmbedderRouter.resolveEmbedderStrict` (`EmbedderRouter.java:294`) routes by the
  collection name's model segment `segments[2]` and **throws**
  `EmbeddingModelUnavailableException` rather than embed a collection with a
  different model. The model is encoded in the collection name (RDR-103 shape
  `<content_type>__<owner>__<model>__v<n>`), so changing the model is a different
  collection name; within one collection the model is invariant.

All three Critical Assumptions are Verified by source search. No live spike is
load-bearing: the design reuses the stored (already-correct) vector rather than
recomputing it, and the added cost is a primary-key lookup. A live embed-
determinism check and a measured SELECT-vs-embed timing remain available as cheap
belt-and-suspenders confirmations at implementation, but neither gates Accept.

**Concurrency-safety (gate round 1):** the assumptions above establish *vector
equivalence* — that a present chash's stored vector is correct to reuse. They do
NOT by themselves make the read-then-write *safe under concurrency*: a separate,
unconditional orphan-GC hard-delete (`_prune_deleted_files` →
`PgVectorRepository.delete`) can delete a shared chash between this batch's
existence SELECT and its metadata UPDATE (RDR-108: shared chashes are normal).
That is a write-safety concern, not a vector-equivalence one, and is addressed in
the design by the 0-row-count fallback (the have-vector UPDATE that matches nothing
re-routes the chash to embed+insert) plus an explicit regression test — see Risks
and Failure Modes.

## Proposed Solution

### Approach

Make the engine skip embedding chunks whose vector it already holds, in one
place — `upsertChunksInternal` — so **every** caller (indexer, exporter, reindex,
migration) inherits it with no client change and no opt-in:

The ordering is load-bearing — the have-vector reroute must resolve **before** the
embed call, so a self-healed chash joins the single embed batch:

1. After intra-request dedup, run one existence SELECT: which of the batch's
   chashes already exist for `(tenant, collection)`. **Skipped under a
   force-re-embed request** (see below): on a first index / `--force` the answer is
   "none present", so the SELECT is pure overhead on the one path that has no
   offsetting benefit — gate it off there. **Fail-safe:** a SELECT error is treated
   as "none present" (embed everything) — never skip on an indeterminate result.
2. Partition: **need-embed** (absent) vs **have-vector** (present).
3. For each have-vector chash, issue a **metadata-only UPDATE** (refresh
   metadata/position, keep the stored vector) — preserving the metadata refresh the
   client `skip_existing` drops (closes Gap 3's caveat). **The UPDATE checks its
   affected-row count; if 0 rows matched, the chash was hard-deleted between the
   existence SELECT and the UPDATE (a concurrent orphan-GC pass — see Risks), so the
   chash is moved into the need-embed set.** This makes the have-vector branch
   self-healing against the check-then-write race: the vector is never permanently
   lost. Steps 1 + 3 are one short transaction that **commits before** step 4.
4. Embed only the (now-finalized) need-embed set — the redundant Voyage calls are
   skipped (the whole win). This runs **outside any open transaction**, exactly as
   the code embeds today.
5. Insert the need-embed rows via the existing `DeadlockRetry`-wrapped `INSERT …
   ON CONFLICT DO UPDATE`.
6. Preserve current semantics when embeddings are client-supplied (the migration
   passthrough) — that path already skips embed.

**Transaction scoping (do NOT collapse into one txn):** steps 1+3 (existence SELECT
+ have-vector UPDATE-with-reroute) commit *before* embedding; `embed()` (step 4)
runs outside any transaction; the `INSERT` (step 5) keeps its existing
`DeadlockRetry` wrapper (nexus-ps9wb). This mirrors today's code, which already
embeds *outside* the DeadlockRetry-wrapped write — because `DeadlockRetry` forbids
wrapping external side effects (`DeadlockRetry.java:31-32`: a retry would re-invoke
`embed()` and re-bill Voyage) and holding a transaction open across the Voyage
round-trip would reintroduce the lock-hold / connection-pool-exhaustion class
already fixed at `PgVectorRepository.java:438-451`. The 0-row fallback's correctness
is independent of transaction boundaries under READ COMMITTED — the vulnerable
SELECT→UPDATE window is fully contained in the step-1+3 transaction, and a delete
that commits inside that window is caught by the 0-row check — so the split does not
reopen the race.

Retire / demote the client `skip_existing` probe: with the server doing this
unconditionally, the extra client probe round-trip becomes redundant. Keep the env
flag as a no-op alias for one deprecation cycle, or repoint it to force-embed for
the rare "recompute everything" case.

### Technical Design

Interface (engine, illustrative — verify during implementation):

```text
// PgVectorRepository.upsertChunksInternal, after dedup:
// --- short txn (commits BEFORE embed) --------------------------------------
Set<String> present = forceReEmbed ? emptySet()          // --force: skip the SELECT
                                   : selectExistingChashes(tenant, collection, dedupChashes);
List<Doc> needEmbed = dedup where chash NOT in present;
List<Doc> haveVector = dedup where chash IN present;
for (Doc d : haveVector) {         // metadata-only, RACE-SAFE vs concurrent orphan-GC delete
    int n = updateMetadataOnly(tenant, collection, d);   // UPDATE ... WHERE (t,c,chash)
    if (n == 0) needEmbed.add(d);  // hard-deleted between SELECT and UPDATE -> re-embed
}
// --- commit ----------------------------------------------------------------

embed(needEmbed);                  // OUTSIDE any txn (as today) — only these hit Voyage (the win)

// --- DeadlockRetry-wrapped write (unchanged from today) --------------------
insertOnConflict(needEmbed);       // vector + metadata
```

Key contract points (verify at implementation):
- `updateMetadataOnly` MUST return its affected-row count; a 0-count is the race
  signal, not a no-op. The existing `updateMetadata` shape
  (`PgVectorRepository.java:1752-1774`) is a bare per-id UPDATE with no row-count
  check — this RDR makes the row-count check + fallback an explicit requirement of
  the have-vector branch, not an inherited assumption.
- `selectExistingChashes` errors ⇒ `present = emptySet()` (fail-safe embed-all).
- **Transaction scoping**: the existence SELECT + have-vector UPDATE-with-reroute
  run in a short transaction that **commits before** `embed()`; `embed()` runs
  outside any transaction (as today, `PgVectorRepository.java:394-403`); the final
  `INSERT` keeps its existing `DeadlockRetry` wrapper (nexus-ps9wb). Do NOT wrap
  `embed()` in the DeadlockRetry transaction — `DeadlockRetry.java:31-32` forbids
  external side effects (a retry re-bills Voyage), and holding a txn open across the
  Voyage round-trip reintroduces the lock-hold/pool-exhaustion class fixed at
  `:438-451`. The 0-row fallback covers the *cross-transaction* GC delete that
  commits between the SELECT and the UPDATE under READ COMMITTED — its correctness
  does not depend on SELECT and UPDATE sharing a transaction with the embed/insert.
- A "force re-embed" request (`forceReEmbed`, wired from the client `--force` /
  the deprecated `NX_UPSERT_SKIP_EXISTING=0` escape) bypasses the partition
  entirely — for the rare model-drift-within-a-collection recompute and to keep the
  first-index path free of the (0%-hit) existence SELECT.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Existence-partition + metadata-only update | `PgVectorRepository.upsertChunksInternal` | Extend — add the partition before the existing embed/upsert |
| Metadata-only update | `PgVectorRepository.updateMetadata` (`:1752-1774`) | Extend — reuse the SQL shape but ADD an affected-row-count return + 0-count fallback to embed+insert (the current form has no row-count check; reusing it as-is is what the gate flagged) |
| Orphan-GC hard-delete | `_prune_deleted_files` (`indexer.py:2187-2237`) + `PgVectorRepository.delete` | Unchanged — it runs unconditionally per re-index; the have-vector branch is made race-safe against it rather than reordering GC |
| Client `skip_existing` probe | `http_vector_client.upsert_chunks` + `existing_ids` | Demote/deprecate — server-side subsumes it, without the probe or the metadata caveat |

### Decision Rationale

Server-side is strictly better than the client probe: no extra round-trip, no
opt-in, universal across callers, and it *keeps* the metadata refresh the client
path drops. It reuses the store as the cache rather than introducing a new cache
with its own invalidation. It composes with (does not require) the separate
first-index levers (CCE concurrency + a Voyage rate governor), which are out of
scope here and can be a later RDR.

## Alternatives Considered

### Alternative 1: Default-on client `skip_existing`

**Description**: Flip `NX_UPSERT_SKIP_EXISTING` to default-on.

**Pros**: Client-only, no engine change; ships fast.

**Cons**: Extra probe round-trip per batch; still drops the ON CONFLICT metadata
refresh (staleness caveat); per-caller opt-in surface; doesn't help non-indexer
callers uniformly.

**Reason for rejection**: The server-side approach dominates it on every axis
(no probe, no staleness, universal). Keep the client flag only as a deprecation
shim.

### Alternative 2: A separate chash→vector cache table/store

**Description**: Add a dedicated embedding cache keyed by chash.

**Reason for rejection**: The vector table already holds the vector for the chash
in that collection — a separate cache is redundant state with its own invalidation
burden.

### Briefly Rejected

- **Skip the row entirely for existing chashes (no metadata update)**: reintroduces
  the client path's metadata-staleness bug.

## Trade-offs

### Consequences

- Re-index of a mostly-unchanged repo pays Voyage only for genuinely new/changed
  chunks — large latency + token-cost reduction on the common path (positive).
- One extra existence SELECT per upsert batch on re-index (small, PK-indexed
  lookup) (minor cost).
- First index (`--force`, all-new) is unaffected: the existence SELECT is **gated
  off** under `forceReEmbed`, so no added latency and no benefit on that path — it
  needs the separate CCE-concurrency/governor levers (out of scope).

### Risks and Mitigations

- **Risk (CRITICAL, gate-surfaced)**: The existence-check-then-conditional-write
  races the pre-existing, unconditional orphan-GC hard-delete (`_prune_deleted_files`
  → `PgVectorRepository.delete`), which runs on every re-index and deletes chashes
  orphaned by content-superseding edits. For a chash **shared across two documents**
  (normal per RDR-108: identical text collapses to one row), a concurrent index of
  doc B can see chash H present, skip re-embed, while doc A's GC deletes H between
  B's SELECT and B's metadata UPDATE — B's UPDATE then matches 0 rows and H is
  permanently lost, making B's document unfetchable.
  **Mitigation**: The have-vector UPDATE checks its affected-row count; a 0-count
  re-routes the chash into the need-embed path (embed + insert). H is re-created, not
  lost. The design is self-healing against the race; no GC reordering or new lock is
  required. A regression test exercises exactly this interleaving.
  **Common case fully safe:** for a re-index of *unchanged* content (the path this
  RDR targets), the document's manifest reference to a shared chash is never
  transiently absent — `append_manifest_chunks` is UPSERT-on-`(doc_id, position)`
  (no delete-first) and `write_manifest` is an atomic DELETE-then-INSERT
  (`catalog_writes.py:750-989`) — so a concurrent GC never observes the reference
  missing and cannot legitimately delete a chash a live re-index still needs.
  **Accepted residual (pre-existing, out of scope)**: the 0-row fallback closes the
  window where a GC delete beats the UPDATE. The complementary window — a GC delete
  landing *after* a successful UPDATE commits — remains, but only for a **first-time**
  shared-chash reference (doc C content-collides with doc A's chash and A drops its
  reference in the gap between C's T3 write committing and C's manifest hook firing,
  `mcp_infra.py:955-999`). This is an inherent property of the T3-write/manifest-hook
  ordering, **identical in today's code** (which embeds+inserts H unconditionally and
  is deleted just the same), so RDR-181 neither introduces nor worsens it. It fails
  **loud** (`IllegalStateException` from `fetchDocumentChunks`) and self-heals on the
  affected document's next re-index. Closing it belongs to a T3-write/manifest
  coordination invariant (a future RDR), not to this embed-skip change.
- **Risk**: Reusing a stored vector when the embed model silently changed within a
  collection would serve a stale vector.
  **Mitigation**: The model is encoded in the collection name and the router refuses
  cross-model embed (`EmbedderRouter.resolveEmbedderStrict:294`, RDR-103), so same
  collection ⇒ same model; a force-re-embed escape covers any deliberate recompute.
- **Risk**: The metadata-only update path diverges from the insert path's metadata
  handling.
  **Mitigation**: Reuse the `updateMetadata` SQL shape (extended with the row-count
  check); test metadata parity against the insert path.

### Failure Modes

- Visible & self-healing: a chash hard-deleted between the SELECT and the UPDATE
  yields a 0-row UPDATE → the fallback embeds+inserts it. A developer sees the chunk
  present and fetchable; the only observable is one extra embed for that chash.
- Visible: if the existence SELECT errored and were treated as "all present", new
  chunks would never embed — so a SELECT failure fail-safes to **embed everything**,
  never skip.
- Silent (guarded): a stale-vector-on-model-drift is prevented by the
  collection-name model invariant + force-re-embed escape; a metadata UPDATE that
  silently affected 0 rows is now caught by the row-count check (was the gate's
  critical). Without the row-count check the failure would be silent permanent chunk
  loss surfacing later as an `IllegalStateException` from `fetchDocumentChunks`
  ("never a silently partial document", `PgVectorRepository.java:1789-1794`).

## Implementation Plan

### Prerequisites

- [ ] Critical Assumptions verified (spike: stored-vs-recomputed vector equality;
  existence-SELECT cost; collection⇒model invariant)

### Minimum Viable Validation

Index a repo, edit ONE file, re-index against the live engine; assert the second
run issues Voyage embed calls only for the changed file's chunks (0 for unchanged),
that unchanged chunks' metadata **still refreshes** (assert an updated
position/line-number lands on a present chash — proving the metadata-only UPDATE
path, not just a skip), and that search results are unchanged. The concurrent-GC
race (Critical) is proven separately by the engine regression test below, not the
MVV (which is single-writer).

### Phase 1: Engine

#### Step 1: Existence partition in upsertChunksInternal
Add `selectExistingChashes(tenant, collection, chashes)` (a PK-indexed `chash =
ANY(?)` lookup); partition into need-embed / have-vector; embed only need-embed.
Fail-safe: SELECT error ⇒ `present = ∅` (embed everything).

#### Step 2: Race-safe metadata-only update for have-vector chashes
Extend `updateMetadata` to return its affected-row count. For each have-vector
chash, UPDATE metadata; **if 0 rows matched, add the chash to need-embed and
embed+insert it** (self-heals the existence-check-vs-concurrent-GC-delete race).

#### Step 3: Force-re-embed escape / first-index gate
A `forceReEmbed` request skips the existence SELECT entirely and embeds all — for
model-drift recompute AND to keep the `--force`/first-index path free of the
0%-hit SELECT. Wire it from the client `--force` path and the deprecated
`NX_UPSERT_SKIP_EXISTING` escape.

#### Step 4: Concurrency regression test
Add the shared-chash GC-race test (Test Plan Critical scenario): assert the 0-row
UPDATE falls back to embed+insert and the document stays fetchable.

### Phase 2: Client

#### Step 1: Demote skip_existing
Make the server-side path authoritative; keep `NX_UPSERT_SKIP_EXISTING` as a
deprecation shim (no-op or force-embed), documented.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| (no new persistent resource) | N/A | N/A | N/A | MVV re-index proof | N/A |

## Test Plan

- **Scenario**: Re-index unchanged repo — **Verify**: 0 Voyage embed calls (all
  chashes present); rows' metadata still updated (non-zero UPDATE counts).
- **Scenario**: Re-index with one changed file — **Verify**: embed calls only for
  that file's new chashes; unchanged chunks keep their vector, refresh metadata.
- **Scenario (Critical — GC race)**: A chash shared by docs A and B; simulate the
  interleave where the existence SELECT sees it present, then a concurrent GC
  `delete` of that chash commits, then the metadata UPDATE runs — **Verify**: the
  UPDATE returns 0 rows, the chash falls back to embed+insert, the row exists
  afterward, and `fetchDocumentChunks(B)` succeeds (no permanent loss, no
  `IllegalStateException`).
- **Scenario (force / first-index)**: `forceReEmbed` request — **Verify**: no
  existence SELECT is issued and every chunk is embedded (first-index path pays no
  added latency).
- **Scenario**: Existence SELECT errors — **Verify**: fail-safe embeds everything
  (no silent skip / dropped chunks).
- **Scenario**: Migration passthrough (client-supplied vectors) — **Verify**:
  unchanged behavior (no embed, vectors stored verbatim).
- **Scenario**: CCE collection re-index — **Verify**: the per-chunk sequential
  embed loop runs only for changed chunks.

## Validation

### Testing Strategy

Engine JUnit (existence-partition unit + Testcontainers pgvector integration for
the metadata-only path, the 0-row-count → embed+insert fallback, the concurrent
shared-chash GC-race interleave, the `forceReEmbed` SELECT-skip, and the
SELECT-error fail-safe) and a live re-index MVV counting embed calls. Performance is
measured at a re-index gate (edit-few-files scenario), not the `--force` all-new
gate.

### Performance Expectations

Empirical only — measured at the re-index gate. Expected: embed calls drop to
~(changed chunks / total), latency and Voyage token cost drop proportionally on the
common re-index path.

## Finalization Gate

### Contradiction Check

Checked against Research Findings and the related RDRs (155, 156, 108, 160, and the
superseded 106/107):

- **RDR-108 (content-hash chunk identity, closed)**: consistent, and load-bearing —
  `chash = sha256(text)[:32]` is why the stored vector is the canonical embedding
  (A1). RDR-108 also establishes that identical text across documents collapses to
  one shared row; this RDR now explicitly handles that shared-chash case in the
  GC-race mitigation rather than treating it as an edge case.
- **RDR-106/107 (tombstone soft-delete, superseded)**: no contradiction — those were
  superseded by RDR-108's content-hash model. Confirmed `chunks_<dim>` has **no**
  tombstone column, so "presence" is a true liveness signal *except* against the
  live hard-delete GC — which is exactly the race this RDR now closes.
- **RDR-155 (pgvector T3)**: consistent — this operates within the pgvector chunk
  tables it created; the existence SELECT keys on their PK.
- **RDR-156 (vector-store capability leverage)**: no overlap/conflict — RDR-156's
  specialized combined-query functions are read-path retrieval; this is a write-path
  embed-skip. Neither defines the other's behavior.
- **RDR-160 (bge-768 local embedder)**: no contradiction — reuse-not-recompute makes
  embed determinism moot regardless of local ONNX vs Voyage, and the model-in-
  collection-name invariant (A3) is embedder-agnostic.

No contradictions remain between research findings, design principles, and the
proposed solution. (Gate round 1 surfaced a real omission — not a contradiction —
the concurrent-GC race; the design was extended to close it.)

### Assumption Verification

All three Critical Assumptions are **Verified via Source Search** (see Research
Findings § Critical Assumptions and T2 `nexus_rdr/181-research-findings`). No spike
is load-bearing: the design reuses the already-correct stored vector rather than
recomputing it, and the added cost is a PK lookup. Gate round 1 correctly noted that
A1's source-search covered the *static* write path only; the *concurrency-safety*
dimension (existence-check vs concurrent hard-delete) is now addressed by design (the
0-row-count fallback) and by an explicit regression test, not by a further
assumption.

### Scope Verification

The MVV (edit-one-file re-index, assert embed calls only for changed chunks AND
metadata refresh on a present chash) is in scope, not deferred. The Critical GC-race
proof is an in-scope engine regression test (Phase 1 Step 4), not deferred.

### Cross-Cutting Concerns

- **Versioning**: relies on collection `__vN` ⇒ model invariant; force-re-embed
  escape for exceptions.
- **Deployment model**: engine change ⇒ a new `engine-service` tag + cloud deploy.
- **Incremental adoption**: server-side is transparent; client flag deprecates
  over one cycle.
- Others: N/A.

### Proportionality

Right-sized: one engine hot-path change + a client deprecation. No new persistent
resource.

## References

- T2 `nexus/research-server-side-embed-reduction-2026-07-05` (engine embed-path map)
- T2 `nexus/duoak11-wall-decomposition-2026-07-05` (corrected sink map)
- `service/.../vectors/PgVectorRepository.java:307-518`, `CceEmbedder.java:110-138`,
  `VoyageEmbedder.java:94-209`, `EmbedderRouter.java:294`
- `src/nexus/db/http_vector_client.py:589-1135`
- RDR-108 (content-addressed chunks), RDR-155 (pgvector), RDR-160 (embedder)

## Revision History

- 2026-07-05: Created (draft) from the duoak.11 embed-reduction research.
- 2026-07-05 (gate round 1 — BLOCKED, remediated): substantive-critic surfaced two
  criticals. (1) The existence-check-then-conditional-write raced the pre-existing
  unconditional orphan-GC hard-delete for chashes shared across documents (normal
  per RDR-108), risking silent permanent chunk loss (0-row metadata UPDATE). Fixed:
  the have-vector UPDATE now checks its affected-row count and falls back to
  embed+insert on 0 rows (self-healing), with an explicit concurrency regression
  test. (2) The Finalization Gate section contradicted the (Verified) Research
  Findings and was unfilled. Fixed: Contradiction Check completed against
  RDR-106/107/108/155/156/160; Assumption Verification reconciled to Verified with a
  concurrency-safety note. Significants also addressed: existence SELECT gated off
  under `forceReEmbed` (no first-index latency); explicit duoak.11 non-progress
  disclaimer; `updateMetadata` row-count check made an explicit requirement.
- 2026-07-05 (gate round 2 — BLOCKED, remediated): both round-1 criticals confirmed
  closed, but the remediation text over-specified transaction scope ("one
  transaction under the DeadlockRetry belt"), which violates
  `DeadlockRetry.java:31-32` (never wrap embedding calls — a retry re-bills Voyage)
  and would hold a txn open across the Voyage round-trip (lock-hold/pool-exhaustion,
  the class fixed at `PgVectorRepository.java:438-451`). Fixed: split into a short
  SELECT+UPDATE-with-reroute txn that commits before `embed()`, `embed()` outside any
  txn (as today), and the existing `DeadlockRetry`-wrapped INSERT after — the 0-row
  fallback's correctness is txn-boundary-independent under READ COMMITTED, so the
  split does not reopen the race. Also reconciled the Approach step ordering
  (have-vector reroute now precedes embed, matching the pseudocode).
