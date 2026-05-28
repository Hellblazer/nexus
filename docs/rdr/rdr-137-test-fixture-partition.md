---
title: "RDR-137 Test-Fixture Partition (Phase 5.4 deliverable, nexus-tts0d.3)"
parent_rdr: RDR-137
parent_bead: nexus-tts0d.3
related_beads: [nexus-tts0d.19, nexus-tts0d.20, nexus-tts0d.21]
created: 2026-05-28
status: deliverable
---

# RDR-137 Test-Fixture Partition

OQ-9 deliverable. Partitions every `RepoRegistry` /  `repos.json` /
`nexus.registry` test-file touch-point into one of three classes so
the Phase 5 migration (`nexus-tts0d.19` + `.20`) and the Phase 3
consumer cutover (`.6` – `.14`) can act with concrete scope instead
of guessing.

## Classes

- **(a) status-lifecycle — DELETE with module.** The test only exists
  because the registry exists. With `RepoRegistry` and `repos.json`
  gone (`nexus-tts0d.20`), the test has no behavior to assert.
- **(b) path / collection-name parity — MIGRATE to catalog API.** The
  test uses `RepoRegistry` as a setup convenience (seed-then-run-
  indexer pattern) or as a mock target (verify caller behavior).
  After the Phase 3 cutover (`.6` – `.14`) the same assertion has a
  catalog-API equivalent: `cat.register_owner(...)` for the seed
  shape, `patch("nexus.catalog.catalog.Catalog.owner_for_repo")` for
  the mock shape.
- **(c) helper-only — KEEP, follows the helpers.** Tests for
  `_repo_identity`, `_resolve_repo_collection`,
  `_sanitise_owner_segment`, `_safe_collection` (the five pure
  helpers). These tests survive `nexus-tts0d.20` because the helpers
  themselves survive — they relocate to `nexus.repo_identity` per
  `nexus-tts0d.21`. The tests' imports retarget alongside.

`(a)` deletions run inside `nexus-tts0d.20` after the helpers move.
`(b)` migrations run inside each Phase 3 consumer-cutover bead that
owns the corresponding production module. `(c)` retargeted imports
run inside `nexus-tts0d.21`.

## Inventory

17 test files, 139 total `RepoRegistry` / `repos.json` /
`nexus.registry` touch-points (line-count grep of HEAD on
`feature/nexus-tts0d.3-test-fixture-partition`).

| File | Hits | Class | Action owner | Notes |
|------|-----:|:-----:|--------------|-------|
| `tests/test_registry.py` | 22 | mixed | see § below | Splits 14 (a) / 8 (c). Single-file partition described below. |
| `tests/test_indexer_e2e.py` | 28 | (b) | `nexus-tts0d.13` | Pure seed-then-indexer pattern. Migrate to `cat.register_owner(...)` + `cat.register_collection(...)` alongside the indexer read-path cutover. |
| `tests/test_collection_name_migration.py` | 18 | (b) | `nexus-tts0d.16` | Tests legacy → conformant collection-name migration during writer cutover. The RepoRegistry usages are setup; migrate to catalog seeding. |
| `tests/test_indexer_conformant_names.py` | 13 | (b) | `nexus-tts0d.13` | Tests `RepoRegistry.add(repo, cat=cat)` populating `code_collection`. Rewrite against the catalog-backed writer the same bead lands. |
| `tests/test_p0_regressions.py` | 9 | (b) | `nexus-tts0d.13` | Indexer regression suite using `RepoRegistry` as setup. Migrate to catalog seeding alongside indexer cutover. |
| `tests/test_catalog_backfill.py` | 7 | (a) + (b) | `nexus-tts0d.16` + `.20` | The `_backfill_repos` verb itself is the catalog-side migrator that reads `repos.json`. The verb evolves under `.16` (its read side flips) and the tests that lock-in `repos.json` shape disappear with `.20`. |
| `tests/test_prose_indexer_doc_id.py` | 6 | (b) | `nexus-tts0d.13` | Seed pattern. |
| `tests/test_pdf_indexer_doc_id.py` | 6 | (b) | `nexus-tts0d.13` | Seed pattern. |
| `tests/test_code_indexer_doc_id.py` | 6 | (b) | `nexus-tts0d.13` | Seed pattern. |
| `tests/test_indexer_duplicate_content.py` | 5 | (b) | `nexus-tts0d.13` | Seed pattern. |
| `tests/test_config_dir_isolation.py` | 4 | (a) + (c) | `nexus-tts0d.20` | `_default_registry_path` / `_registry_path` assertions delete with the module. Path-isolation infrastructure tests (catalog dir, scratch dir) keep. |
| `tests/test_catalog_e2e.py` | 3 | (b) | `nexus-tts0d.13` | RepoRegistry seeds, catalog assertions. Migrate seed to catalog. |
| `tests/test_doctor_cmd.py` | 3 | (b) | `nexus-tts0d.12` | Mocks `nexus.registry.RepoRegistry`. Doctor reads catalog post-cutover; retarget mock to `nexus.catalog.catalog.Catalog`. |
| `tests/test_pipeline_version.py` | 3 | (b) | `nexus-tts0d.13` | Mocks `nexus.registry.RepoRegistry` to inject pipeline-version state. Retarget to catalog metadata. |
| `tests/test_git_hooks.py` | 2 | (a) | `nexus-tts0d.20` | Asserts the `{repos: {<path>: {...}}}` JSON shape. With `repos.json` deleted the assertion has no subject. The hook's behavior (re-indexing after a commit) is covered elsewhere. |
| `tests/test_indexer_modules.py` | 2 | (b) | `nexus-tts0d.13` | Seed pattern (uses `cfg_dir / "repos.json"` path). |
| `tests/test_silent_error_logging.py` | 2 | (a) | `nexus-tts0d.20` | Tests doctor's resilience when `RepoRegistry.__init__` raises (corrupt `repos.json`). With `repos.json` gone there is no corrupt-file failure mode to test. |

### `tests/test_registry.py` internal partition

The dedicated registry test file is the only one with both (a) and
(c) content. Per-function classification (line numbers from HEAD):

**Class (a) — DELETE with `nexus-tts0d.20`** (14 functions, the
CRUD / persistence / concurrency surface):

- L54 `test_add_and_get`
- L64 `test_persists_to_json`
- L71 `test_survives_reload`
- L77 `test_remove`
- L83 `test_get_missing`
- L94 `test_update` (parametrized over `head_hash` / `status`)
- L103 `test_atomic_write_leaves_no_tmp`
- L108 `test_concurrent_reads_and_writes`
- L141 `test_get_returns_copy`
- L154 `test_dual_collection_names`

**Class (c) — MOVE with helpers under `nexus-tts0d.21`**, retarget
imports to `nexus.repo_identity` (8 functions, the pure-helper
surface):

- L178 `test_repo_identity_fallback_without_git`
- L187 `test_repo_identity_stable_in_git_repo`
- L196 `test_worktree_matches_main`
- L209 `test_collection_names_stable_across_worktrees`
- L223 `test_resolve_repo_collection_synthesises_conformant_for_rdr`
- L233 `test_resolve_repo_collection_owner_segment_shared_across_types`
- L250 `test_truncates_long_basename`
- L261 `test_short_basename_not_truncated`
- L266 `test_truncated_names_still_unique`
- L277-315 `test_sanitise_owner_segment_*` (4 variants)
- L315 `test_resolve_repo_collection_dotted_basename_passes_validation`

Note that the class-(c) functions count as "11 named tests" because
the four `test_sanitise_owner_segment_*` are listed separately even
though they share the same prefix; the 11 functions cover the seven
distinct helper symbols.

When `nexus-tts0d.21` lands, these (c) tests move to a new file
`tests/test_repo_identity.py` (or merge into
`tests/test_worktree_repo_root_contamination.py` if the per-module
file-count economy says so) with imports retargeted to
`nexus.repo_identity`.

When `nexus-tts0d.20` lands, the (a) tests delete; the
`tests/test_registry.py` file itself either disappears entirely (if
all class-(c) tests have already moved) or shrinks to whatever
helper tests remain.

## Counts

| Class | Files (some files span multiple classes) | Distinct functions / blocks |
|------|:---:|---:|
| (a) status-lifecycle — DELETE | 5 (test_registry partial, test_git_hooks, test_silent_error_logging, test_catalog_backfill partial, test_config_dir_isolation partial) | ~20 |
| (b) parity — MIGRATE to catalog API | 12 (every indexer / doctor / pipeline / collection-rename / catalog-e2e seed-style file) | ~80 (rough — most files have 2-6 fixtures each) |
| (c) helper-only — KEEP (move with helpers) | 1 (test_registry partial) | ~11 |

Sum of touch-points ≈ 139 distributes roughly 30 / 95 / 14 across
(a) / (b) / (c).

## Phase-3 / Phase-5 cross-walk

Phase-3 cutover beads each absorb the (b) migrations for the
production module they own:

- `nexus-tts0d.12` (doctor.py cutover) → migrates `test_doctor_cmd`
  + `test_silent_error_logging` mock targets.
- `nexus-tts0d.13` (indexer cutover + status writes drop) → migrates
  ALL indexer / pdf / prose / code / duplicate-content / regression
  test files (10 of the 17, the bulk of class (b)).
- `nexus-tts0d.16` (writer replacement + `--corpus knowledge`
  inference) → migrates `test_collection_name_migration` +
  `test_catalog_backfill` write-side.
- `nexus-tts0d.21` (helper relocation) → moves the 11 class-(c)
  functions from `test_registry.py` to the helpers' new home.
- `nexus-tts0d.20` (RepoRegistry class deletion + lint guard) →
  deletes the residual class-(a) tests; deletes the remaining shell
  of `test_registry.py` if empty; deletes `test_git_hooks.py`'s
  JSON-shape assertion block.

After Phase-3 close (`nexus-tts0d.14`) the (b) migration debt is
zero. After `nexus-tts0d.21` the (c) re-home is done. After
`nexus-tts0d.20` the (a) deletions ship and the CI lint guard
forbids re-introduction.
