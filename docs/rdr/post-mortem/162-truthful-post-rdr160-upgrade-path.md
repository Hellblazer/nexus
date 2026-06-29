# RDR-162 post-mortem — Truthful Post-RDR-160 Upgrade Path

**Closed:** 2026-06-19 (accepted 2026-06-18). **Epic:** `nexus-i055c`. **Outcome:** implemented, rehearsal-proven green end-to-end.

## What shipped

- **P1 — truthful classifier.** `detection._ONNX_MODEL` → `bge-base-en-v15-768`; the `Support` literal `supported-onnx-384` → `supported-onnx` (dim-agnostic); a legacy `minilm-384` collection is `unsupported` with a re-index diagnostic. Migration test fixtures inverted to the post-RDR-160 reality.
- **P2 — cross-model migrate.** Single-stage **stored-text re-embed**: `nx migrate-to-service` reads a legacy collection's stored chunk text and upserts it into a model-remapped `bge-768` target (`cross_model_target_name`), the service re-embeds, and the catalog/topic `source_collection` references are re-pointed source→target after the target verifies (`remap_collection_references`). Copy-not-move, idempotent on `(tenant, target_collection, chash)`, remap-after-verify, leg-demotion on remap failure. Covers sourceless `store_put` notes `embed_migrate` cannot. Cross-model-aware dry-run + validation (`verify_counts` / `verify_taxonomy_consistency` resolve through `target_names`).
- **P3 — contract.** Pinned as primitive 7 in `rdr-157-handoff-to-conexus-rdr-001.md`; relay staged for the conexus RDR-001 instance.

## The rehearsal earned its keep

The `tests/e2e/migration-rehearsal` Docker harness — driving the real native service, real Postgres, real cross-model migrate — surfaced **four latent bugs that green unit tests and three stacked reviews did not**:

1. **Dry-run preview** classified legacy collections as BLOCKED — the cross-model path existed only in the live driver, not the dry-run. (nexus-side, fixed.)
2. **`nexus-qke1e` supervisor lifecycle** — `nx init --service` published a discovery lease with no heartbeating supervisor, so it aged out by TTL before the migrate ran. Routed both init and `daemon service start` through a shared `ensure_storage_supervisor`. (nexus-side, fixed.)
3. **`nexus-pqatt` native crash** — the GraalVM native binary SIGABRTed on the first bge-768 embed (missing JNI registrations for the DJL tokenizers / onnxruntime path). Fixed engine-side, shipped as `engine-service-v0.1.4`.
4. **Service-mode rename contract mismatches** — `TaxonomyHandler` was the lone `rename_collection` endpoint expecting `old_collection`/`new_collection` (every other handler + client uses `old`/`new`); the catalog rename 500s for cross-model (the upsert pre-registers the bge target, so the registry rename collides under the RDR-156 `ON UPDATE CASCADE` FKs). The taxonomy fix shipped; the catalog 500 is fail-open and handed to engine-service.

**Lesson:** the ref-remap was the *first real caller* of the service-mode rename cascade. A whole class of client/server contract bugs sat dormant until an end-to-end harness exercised them. Unit tests + reviews validated the design; only the rehearsal validated the *integration*.

## Process note: duplicate planning tree

Two RDR-162 bead trees existed (both planned 2026-06-18): the canonical `nexus-i055c` (single-stage stored-text design, executed) and a stale `nexus-0xdfo` (the **rejected two-stage `embed_migrate` → `migrate-to-service` chain**, pre-design-refinement). The stale tree (epic + ~13 beads) was superseded wholesale at close. Cause: re-planning after Hal's single-stage design refinement created a fresh tree without retiring the first. Watch for duplicate epics when a design pivots mid-planning.

## Deferred (tracked, non-blocking)

- `nexus-0ng3v` (P2) — rehearsal assert catalog `physical_collection` repoint, blocked on the engine catalog-rename-500 fix.
- `nexus-5yn9c` (P3) — thread `config_dir` through `init._start_service_step`.
- `nexus-8py1k` (P3) — dry-run preview mixed-bucket display.
- Engine-side: catalog-rename-500 (handoff `engine_service/HANDOFF-catalog-rename-500-cross-model-collision`).

## Boundary clarified

`nexus.db.embed_migrate` (under `db/`) is the **LOCAL-only** 384→768 re-index for users staying on Chroma; it re-reads source files and cannot upgrade sourceless notes. The RDR-162 service path re-embeds stored text and is what conexus RDR-001 orchestration uses. Not to be confused.

## Relation to the release gate

Feeds `nexus-luxe6` prerequisite (2) (upgrade orchestration) by making the nexus cross-model primitive truthful + composable. Does not by itself lift luxe6.
