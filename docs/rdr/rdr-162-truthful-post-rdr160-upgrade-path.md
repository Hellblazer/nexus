---
title: "Truthful Post-RDR-160 Upgrade Path: Fix the Migration Model Classifier and Make the Legacy 384→768 Re-Index→Migrate Chain a Rehearsal-Proven Primitive"
id: RDR-162
type: Architecture
status: accepted
accepted_date: 2026-06-18
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-18
related_issues: [nexus-nu5mp, nexus-luxe6]
related: [RDR-160, RDR-159, RDR-155, RDR-144, RDR-152]
---

## Problem Statement

The migration-rehearsal Phase B failed on 2026-06-18 with an HTTP 422 from the
storage service:

```
service (embedding mode onnx-local) has no embedder for model
'minilm-l6-v2-384' ... Available models: [bge-base-en-v15-768]
```

Diagnosis surfaced two distinct defects, one a bug and one a missing capability.

#### Gap 1: Stale migration model classifier (RDR-160 fallout — a bug)

`src/nexus/migration/detection.py` mirrors the service's wired embedders so the
guided upgrade can classify a user's Chroma footprint *before* moving data.
RDR-160 swapped the service's local ONNX embedder MiniLM-384 → bge-768, but
`detection.py` was never updated: `_ONNX_MODEL = "minilm-l6-v2-384"`. The
classifier therefore **inverts reality** — it marks a `minilm-l6-v2-384`
collection `supported-onnx` (the migration proceeds, the live bge-768 service
then 422-refuses it) and marks a `bge-base-en-v15-768` collection `unsupported`.
This also corrupts the RDR-159 pre-gate's offline fallback floor (the path taken
when the live service is unreachable), so a real upgrading user is misclassified
either way.

#### Gap 2: No end-to-end legacy upgrade chain (a missing capability)

A legacy user's local Chroma is minilm-384 (the pre-RDR-160 default). The
bge-768 service cannot serve 384-dim vectors, and `migrate-to-service` is
same-model by contract (`vector_etl` sends chunk text, preserves the source
collection name byte-for-byte, and the service re-embeds with the embedder
matching the name's model segment — RDR-109 cross-model-contamination guard).
So the only correct legacy path is a **two-stage chain**: `embed_migrate`
(RDR-144, local 384→768 re-index, which remaps the collection's model segment)
**then** `migrate-to-service` (now same-model bge-768 → service). Today nothing
composes these two primitives, and the rehearsal does not exercise the chain —
it feeds a raw minilm-384 source straight to the service.

The user-facing single-command orchestration (`nx upgrade` detect→guide) is
**conexus RDR-001**, which already exists as the consumer. This RDR is the
**nexus-side** half: make the classifier truthful and make the
re-index→migrate chain a composable, rehearsal-proven primitive set that
conexus RDR-001 drives.

## Decision

1. **Fix the classifier to mirror the post-RDR-160 service.** `_ONNX_MODEL`
   becomes `bge-base-en-v15-768`; the `Support` literal `supported-onnx-384`
   becomes the dim-agnostic `supported-onnx`. Consequence: a bge-768 collection
   is `supported-onnx` (migrates); a legacy minilm-384 collection is
   `unsupported` with the existing "re-index required" diagnostic — the truthful
   answer that points the user at the local re-index step instead of marching
   them into a 422.

2. **Make the legacy 384→768 re-index→migrate chain a composable, proven
   primitive set.** The rehearsal drives `embed_migrate` (384→768) then
   `migrate-to-service` (768→service) and asserts the chain lands the data in
   pgvector; it also asserts a bare minilm-384 source is correctly *blocked*
   (not silently half-migrated). The chaining ORDER, idempotency, and
   partial-failure/rollback semantics across the two stages are the nexus
   contract conexus RDR-001 consumes; no new user-facing command is added here
   (that is RDR-001's surface).

3. **Do not weaken the service's model-identity guard.** The bge-768 service
   correctly refuses to embed a minilm-384-named collection (RDR-109 /
   nexus-pebfx.2). The fix is upstream classification + re-index, never relaxing
   the guard.

4. **Supersede RDR-159's stale model enumeration.** RDR-159 §Two upgrade paths
   classified minilm-384 as `supported-onnx` (Path 2) and bge-768 as
   `unsupported`/blocked (Path 3), and its test plan item (d) asserts
   "unsupported-model (bge-768): detected and BLOCKED". RDR-160 + this RDR
   invert that: post-RDR-162, **bge-768 is `supported-onnx`** (the service's
   wired model) and **minilm-384 is `unsupported`** with the re-index-required
   diagnostic. The RDR-159 test-plan item (d) is replaced by the P2 rehearsal
   assertions (bge-768 migrates clean; minilm-384 is blocked, then chained via
   `embed_migrate`). This is correct supersession, not scope reduction; the
   RDR-159 tests encoding the old model are deleted and replaced, not edited.

## Approach (phased)

**Phase 1: Truthful classifier + migration test-suite alignment**

- `detection.py`: `_ONNX_MODEL` → `bge-base-en-v15-768`; rename the
  `supported-onnx-384` literal → `supported-onnx`; correct the docstrings and
  the now-inverted dim examples. (Working-tree seed already drafted 2026-06-18.)
- Invert the migration test fixtures that encode the old assumption. P1 starts
  from an already-RED suite (the `detection.py` seed landed on the working tree
  ahead of the tests). Files carrying the stale literal/model:
  `tests/migration/test_detection.py` (lines ~53, 133-134, 146, 170-175, 216,
  293-296, 416-430), `test_sequencer.py:56`, `test_driver.py:51`. bge-768 is the
  supported-ONNX model; a minilm-384 collection is `unsupported` with the
  re-index diagnostic. Tests encoding the old model are deleted and replaced,
  not edited. Confirm whether `test_pregate.py` actually keys on the renamed
  literal (it may only branch on `== "unsupported"` / `== "supported-voyage-1024"`,
  both unchanged) before touching it. Assertions stay exact, never inequalities.

**Phase 2: Rehearsal proves both paths**

- `tests/e2e/migration-rehearsal`: seed BOTH a bge-768 collection (migrates
  clean) AND a legacy minilm-384 collection; assert the minilm-384 one is
  classified `unsupported` / blocked with the re-index diagnostic, then drive
  `embed_migrate` (384→768) and re-run `migrate-to-service` to prove the chained
  legacy path lands in pgvector. Phase A (native serve) and Phase C (rollback
  safety) already pass.

**Phase 3: Chain contract + conexus RDR-001 coordination**

- Pin the composition contract (order, idempotency, partial-failure/rollback
  per the §Open Questions disposition) across `nexus.db.embed_migrate` (the
  RDR-144 re-index primitive; note it lives under `db/`, not `migration/`) →
  `migrate-to-service` that the conexus RDR-001 orchestration consumes; update
  the RDR-157 P5 handoff doc if the contract shifts. Relay the nexus-side
  contract to the conexus RDR-001 instance.
- Phase-review-gate cross-walk + stacked review (behavioral change in the
  migration layer).

## Alternatives considered

- **Relax the service model-identity guard to re-embed any collection with its
  own model.** Rejected: that is the exact same-dim cross-model contamination
  RDR-109 / pebfx.2 exist to prevent; it would silently corrupt recall.
- **Switch the rehearsal fixture to bge-768 only.** Rejected: it greens the test
  by avoiding the legacy path, hiding the real gap (the rehearsal's stated job
  is the legacy pre-cutover footprint).
- **Build the user-facing `nx upgrade` command here.** Rejected: that surface is
  conexus RDR-001 (already exists); nexus owns the primitives + contract.

## Consequences

- Freeze-sensitive: this is in the RDR-155/159 migration layer that
  `nexus-luxe6` gates. It feeds luxe6 prerequisite (2) (the upgrade
  orchestration) by making the nexus primitives truthful and composable, but
  does not by itself lift luxe6.
- A real legacy minilm-384 user is now told the truth (re-index first) instead
  of hitting a 422 mid-migration.
- The classifier change is behavioral; the migration test suite is rewritten to
  the post-RDR-160 reality.

## Open Questions

- Does any consumer branch on the exact `supported-onnx-384` literal beyond the
  `== "unsupported"` / `== "supported-voyage-1024"` checks? **Resolved (CA-1):**
  no — production code is clean; only tests carry the literal.
- **Rollback boundary — DISPOSITION (confirm at P3, not genuinely open):** when
  `embed_migrate` succeeds and `migrate-to-service` fails, the user is in a
  re-runnable **768-local** state; there is no unwind to 384 and none is needed.
  `embed_migrate` is reindex-first/delete-after-verify (RDR-144) and
  `migrate-to-service` is idempotent on `(tenant, collection, chash)` (RDR-155
  RF-5), so re-running `migrate-to-service` from the 768-local Chroma is the
  clean recovery path. P3 codifies this as the contract conexus RDR-001 consumes
  and verifies it with the rehearsal's partial-failure assertion.

## Research Findings

- Live service `/version` (2026-06-18 rehearsal): `embedding_mode: onnx-local`,
  `embedding_models: [bge-base-en-v15-768]`, schema migrated (120 changesets) —
  confirms minilm-384 is wired by no service embedder post-RDR-160.
- `vector_etl.py` (lines 22–35): the ETL sends chunk text (no source vectors),
  preserves the source collection name byte-for-byte, re-embeds server-side by
  the name's model segment — confirms same-model-by-contract.
- `embed_migrate.py` (RDR-144): local 384→768 re-index with no-data-loss
  ordering (reindex-first, delete-after-verify) — the missing first stage of the
  legacy chain.
- Native conversion of the rehearsal (nexus-bneym) validated Phase A: the linux
  native binary boots, migrates the schema, and serves /health.

### Critical-assumption verification (2026-06-18, pre-gate)

- **CA-1 VERIFIED — classifier is the sole stale hardcode + literal rename is
  logic-safe.** `detection.py` is the only place asserting minilm-384 is the
  *service-wired* ONNX model (`init.py:295` is a CLI `--embedder` advisory;
  `local_ef`/`corpus` minilm tokens are the CLI local-embedder identity, a
  distinct concern). Every consumer branches on `support == "unsupported"`
  (pregate.py:157, detection.py:174/372/416) or `== "supported-voyage-1024"`
  (detection.py:349); none keys on the `supported-onnx-384` literal, so renaming
  it to `supported-onnx` changes no control flow.
- **CA-2 VERIFIED — the re-index→migrate chain composes.**
  `embed_migrate._target_name(old, active_token)` swaps the collection's model
  segment, and `detect_stale_local_collections` defaults
  `active_token="bge-base-en-v15-768"`. So the RDR-144 re-index produces a
  bge-768-named collection (768-dim vectors, reindex-first/delete-after-verify),
  which `migrate-to-service` then sees as a same-model bge-768 collection the
  service accepts — no cross-model upsert, no 422.
- **CA-3 VERIFIED — the guard stays; re-index is the upstream fix.** The
  service's `EmbedderRouter` model-identity guard (RDR-109 / nexus-pebfx.2)
  refuses to embed a collection with an embedder other than its name's model
  segment, by design. `embed_migrate` IS that upstream re-index (it remaps the
  segment to bge-768), so the correct path never weakens the guard.
