---
title: "RDR-137 Test-Fixture Partition (Phase 5.4 deliverable, nexus-tts0d.3)"
parent_rdr: RDR-137
parent_bead: nexus-tts0d.3
related_beads: [nexus-tts0d.19, nexus-tts0d.20, nexus-tts0d.21]
created: 2026-05-28
updated: 2026-05-28
status: revised-after-implementation
---

# RDR-137 Test-Fixture Partition

OQ-9 deliverable. Partitions every `RepoRegistry` / `repos.json` /
`nexus.registry` test-file touch-point so the Phase 5 migration
(`nexus-tts0d.19` + `.20`) and the Phase 3 consumer cutovers
(`.6` – `.14`) can act with concrete scope.

> **Revised 2026-05-28 after Phase 5 implementation shipped.** The
> original plan called for class-(a) and class-(b) tests to be
> deleted or migrated to a catalog-only fixture pattern. The simpler
> approach that actually shipped (`nexus-tts0d.20`) was to **keep
> the `RepoRegistry` class in `nexus.registry` as a test-fixture
> shim** during the deprecation window, with a CI lint guard
> (`tests/test_no_repo_registry_resurrection.py`) preventing
> production re-introduction. Only class-(c) helper imports were
> mechanically retargeted (in `nexus-tts0d.21`). Class-(a) deletions
> and class-(b) migrations are **deferred** until the next major
> release closes the deprecation window and `nexus.registry` itself
> can be deleted.

## What shipped

| Class | Original plan | Shipped | Why |
|------|---------------|---------|-----|
| (a) status-lifecycle | DELETE with `nexus-tts0d.20` | **Kept as fixtures** | The tests still pass against the back-compat shim; deleting them prematurely loses regression coverage of the JSON file shape during the deprecation window. |
| (b) parity | MIGRATE to catalog API per consumer-cutover bead | **Kept as fixtures** | Same reason. Production code is fully cut over (lint guard enforces); tests can continue using `RepoRegistry(...)` as a synthetic file-seeder. |
| (c) helper-only | RETARGET imports in `nexus-tts0d.21` | **DONE** | 7 test files had `monkeypatch.setattr("nexus.registry._repo_identity", ...)` retargeted to `"nexus.repo_identity._repo_identity"` so mocks intercept the relocated function correctly. |

## Lint guard contract

Two `tests/test_no_repo_registry_resurrection.py` checks enforce the
post-shipping invariant:

- `test_no_RepoRegistry_class_definition_outside_legacy_shim` —
  `class RepoRegistry` is allowed in `src/nexus/registry.py` only.
  Re-introducing the class anywhere else under `src/nexus/` is a
  regression.
- `test_no_direct_repos_json_parsing_outside_migration_path` — direct
  `json.loads(repos.json)` reads are allowed in
  `src/nexus/commands/upgrade.py` (migration verb) and `src/nexus/repos.py`
  (the dual-read shim's stdlib helper) only. Production consumers
  that need to mention `repos.json` for path-passthrough are fine;
  they route the read through `nexus.repos.read_dual` / `list_repos_dual`.

## What gets deleted in the next major release

When the deprecation window closes:

- `src/nexus/registry.py` deletes entirely (helpers already live in
  `nexus.repo_identity`).
- The class-(a) tests in `tests/test_registry.py` delete with it.
- The class-(b) test files keep their assertions but rewrite their
  seed-pattern to mint catalog rows directly instead of writing
  `repos.json` files.

The lint guard's `_REPO_REGISTRY_CLASS_ALLOW` whitelist becomes
empty at that point.

## Original inventory (preserved for historical reference)

17 test files, 139 total `RepoRegistry` / `repos.json` /
`nexus.registry` touch-points (line-count grep of HEAD on
`feature/nexus-tts0d.3-test-fixture-partition` 2026-05-28).

| File | Hits | Original class |
|------|-----:|:--------------:|
| `tests/test_registry.py` | 22 | (a) + (c) |
| `tests/test_indexer_e2e.py` | 28 | (b) |
| `tests/test_collection_name_migration.py` | 18 | (b) |
| `tests/test_indexer_conformant_names.py` | 13 | (b) |
| `tests/test_p0_regressions.py` | 9 | (b) |
| `tests/test_catalog_backfill.py` | 7 | (a) + (b) |
| `tests/test_prose_indexer_doc_id.py` | 6 | (b) |
| `tests/test_pdf_indexer_doc_id.py` | 6 | (b) |
| `tests/test_code_indexer_doc_id.py` | 6 | (b) |
| `tests/test_indexer_duplicate_content.py` | 5 | (b) |
| `tests/test_config_dir_isolation.py` | 4 | (a) + (c) |
| `tests/test_catalog_e2e.py` | 3 | (b) |
| `tests/test_doctor_cmd.py` | 3 | (b) |
| `tests/test_pipeline_version.py` | 3 | (b) |
| `tests/test_git_hooks.py` | 2 | (a) |
| `tests/test_indexer_modules.py` | 2 | (b) |
| `tests/test_silent_error_logging.py` | 2 | (a) |

## Class (c) retargeting log

The 7 test files updated in `nexus-tts0d.21` (helper relocation):

- `tests/test_catalog.py` — 4 patch sites
- `tests/test_catalog_cli.py` — 4 patch sites
- `tests/test_catalog_collection_for.py` — `import nexus.registry as reg_mod` → `import nexus.repo_identity as reg_mod`
- `tests/test_indexer_conformant_names.py` — 3 patch sites
- `tests/test_worktree_repo_root_contamination.py` — 2 patch sites
- `tests/test_collection_name_migration.py` — 1 patch site
- `tests/hooks/test_rdr_hook.py` — 3 patch sites

Plus `tests/test_silent_error_logging.py` rewrite (the
`doctor_registry_load_failed` site moved from patching `RepoRegistry`
to patching `nexus.repos.list_repos_dual`, since that's where the
enumeration now happens after the `nexus-tts0d.6` health.py cutover).
