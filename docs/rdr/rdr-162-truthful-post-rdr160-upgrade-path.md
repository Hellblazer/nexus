---
title: "Truthful Post-RDR-160 Upgrade Path: Fix the Migration Model Classifier and Make the Legacy 384→768 Re-Index→Migrate Chain a Rehearsal-Proven Primitive"
id: RDR-162
type: Architecture
status: closed
accepted_date: 2026-06-18
closed_date: 2026-06-19
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
same-model by contract: `vector_etl` re-embeds the stored chunk text but
preserves the source collection name byte-for-byte, so the service refuses a
`minilm-l6-v2-384` name (RDR-109 cross-model-contamination guard). Today there
is **no cross-model migration path** — nothing remaps the target to the
service's model — and the rehearsal does not exercise one; it feeds a raw
minilm-384 source straight to the service and hits the 422. (The §Decision
resolves this with a single-stage stored-text re-embed + target-model remap,
which needs no source files; see §Decision 2.)

The user-facing single-command orchestration (`nx upgrade` detect→guide) is
**conexus RDR-001**, which already exists as the consumer. This RDR is the
**nexus-side** half: make the classifier truthful and make the cross-model
migrate a composable, rehearsal-proven primitive that conexus RDR-001 drives.

## Decision

1. **Fix the classifier to mirror the post-RDR-160 service.** `_ONNX_MODEL`
   becomes `bge-base-en-v15-768`; the `Support` literal `supported-onnx-384`
   becomes the dim-agnostic `supported-onnx`. Consequence: a bge-768 collection
   is `supported-onnx` (migrates); a legacy minilm-384 collection is
   `unsupported` with the existing "re-index required" diagnostic — the truthful
   answer that points the user at the local re-index step instead of marching
   them into a 422.

2. **Re-embed the STORED CHUNK TEXT into a model-remapped target — no source
   files, single stage.** The chunk text is already in the source collection
   (`documents`), and `vector_etl` already re-embeds *that* server-side
   (`iter_collection_chunks` reads the stored text; source vectors are never
   read). The ONLY thing blocking cross-model migration is that `vector_etl`
   preserves the source collection name byte-for-byte, so the bge-768 service
   rejects a `minilm-l6-v2-384` name. The fix is a **cross-model mode on
   `migrate-to-service`**: remap the target collection's model segment to the
   service's model (bge-768), re-embed the stored text under the new name, and
   update the catalog/topic `source_collection` references that the byte-for-byte
   preservation existed to keep valid. This is strictly better than a two-stage
   `embed_migrate` → `migrate-to-service` chain: it needs **no source files**, so
   it also migrates `sourceless` collections (manual `store_put` notes) that
   `embed_migrate` explicitly cannot re-index. `embed_migrate` (RDR-144) remains
   the LOCAL-only 384→768 upgrade path (user staying on Chroma); it is not the
   service-migration path. No new user-facing command is added here (that is
   conexus RDR-001's surface).

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
   assertions (a bge-768 collection migrates clean; a legacy minilm-384
   collection is migrated cross-model via the single-stage stored-text re-embed
   into a bge-768-remapped target). This is correct supersession, not scope
   reduction; the RDR-159 tests encoding the old model are deleted and replaced,
   not edited.

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

**Phase 2: Cross-model migrate (stored-text re-embed + model remap)**

- Add a cross-model mode to `migrate-to-service` / `vector_etl`: when a source
  collection's model segment is unsupported by the service but its chunk text is
  present, re-embed the stored text into a target collection whose model segment
  is the service's model (bge-768), and update the catalog/topic
  `source_collection` references to the remapped name (the references the
  byte-for-byte preservation protected). Idempotent on `(tenant, target_collection,
  chash)`. Covers `sourceless` collections (manual notes) that `embed_migrate`
  cannot.
- Rehearsal proves it: `tests/e2e/migration-rehearsal` seeds a legacy minilm-384
  collection (incl. a sourceless `store_put`-style note) and asserts it migrates
  to a bge-768 target via the stored-text re-embed, lands in pgvector, and the
  catalog references resolve to the new name. Phase A (native serve) + Phase C
  (rollback safety) already pass.

**Phase 3: Contract + conexus RDR-001 coordination**

- Pin the composition contract the conexus RDR-001 orchestration consumes:
  the cross-model `migrate-to-service` mode (target model = service model,
  stored-text re-embed, reference remap), its idempotency/partial-failure
  disposition (per §Open Questions), and the boundary with `embed_migrate`
  (`nexus.db.embed_migrate`, under `db/`, is LOCAL-only upgrade — not the service
  path). Update the RDR-157 P5 handoff doc if the contract shifts; relay to the
  conexus RDR-001 instance. **Pinned (2026-06-19):** the contract lives in
  [`rdr-157-handoff-to-conexus-rdr-001.md`](rdr-157-handoff-to-conexus-rdr-001.md)
  §7 "Cross-model migrate" (the consumer-facing handoff RDR-001 reads).
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
- **Rollback boundary — DISPOSITION (confirm at P3, not genuinely open):** the
  single-stage stored-text re-embed is copy-not-move — the source Chroma
  collection is never mutated (RDR-155 RF-5), so a failed cross-model migrate is
  cleanly re-runnable from the untouched 384 source. `migrate-to-service` is
  idempotent on `(tenant, target_collection, chash)`, so a partial target is
  resumed, not duplicated. The reference remap (catalog/topic
  `source_collection`) is the one mutation that must be ordered AFTER the target
  is verified-populated (mirror RDR-144's reindex-first/delete-after-verify
  ordering) so a mid-migrate failure never leaves dangling references. P3
  codifies this and the rehearsal asserts the partial-failure path.

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
- **CA-3 VERIFIED — the guard stays; the target-name remap is the fix.** The
  service's `EmbedderRouter` model-identity guard (RDR-109 / nexus-pebfx.2)
  refuses to embed a collection with an embedder other than its name's model
  segment, by design. The cross-model migrate satisfies it by remapping the
  TARGET collection's model segment to bge-768 (so the name matches the service
  embedder), never by relaxing the guard.
- **DESIGN REFINEMENT (2026-06-18, Hal) — re-embed from stored chunk text, not
  source files.** `vector_etl` already re-embeds the STORED chunk `documents`
  (`iter_collection_chunks`), so the cross-model migrate needs **no original
  file/web source** — only a target-name model remap + reference update. This is
  strictly better than a two-stage `embed_migrate` → `migrate-to-service` chain:
  `embed_migrate` re-indexes from `source_paths` and has a `sourceless` category
  (manual `store_put` notes) it explicitly cannot re-index, whereas the
  stored-text re-embed covers those too. `embed_migrate` stays the LOCAL-only
  384→768 upgrade primitive; it is not the service-migration path.
