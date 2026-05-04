# Post-Mortem: RDR-103, Catalog as Collection-Name Authority

**RDR:** [RDR-103](../rdr-103-catalog-collection-name-authority.md)
**Status:** closed (implemented), 2026-05-04
**Epic:** `nexus-yqnr` (9/9 child beads, 100% complete)
**Phases shipped:** 1 (tuple type) -> 6 (plugin docs); irreversible flip at 5 (strict-naming default-on)
**Final landing:** PR #498 close (post #497 release-gate landed RDR-101)

## Outcome

Single-day architectural arc on top of RDR-101's irreversibility commitment. Collection naming authority moved from the indexer family (`registry._collection_name(repo)`) to the catalog (`Catalog.collection_for_repo`). The collection name is now a formal `CollectionName` tuple `(content_type, owner_id, embedding_model, model_version)` rendered to `<ct>__<owner>__<model>__v<n>`. Strict-naming enforcement collapsed from a 30-site flag-threading exercise to a one-line assertion in `T3Database.get_or_create_collection`. Migration handled in a single `_migrate_legacy_collections` pre-pass on first index per content_type.

Phases as shipped:

- **Phase 1** : `CollectionName` tuple type (`nexus-yqnr.1`).
- **Phase 2** : `Catalog.collection_for` + owner-segment helper promoted from `commands/catalog.py` to `nexus.catalog` (`nexus-yqnr.2`).
- **Phase 3a** : `collection_for_repo` convenience + indexer call-site migration (`nexus-yqnr.3`).
- **Phase 3b** : plugin-layer indexers wired to catalog authority (`nexus-yqnr.4`); review fixes (`nexus-yqnr.5`).
- **Phase 4** : automatic legacy-name migration on first index (`nexus-yqnr.6`); second-pass review fixes folded into `ab8bc459`.
- **Phase 5** : strict-naming default-on flip (irreversible) + conformant fallbacks across catalog, registry, indexer, doc_indexer, and MCP/CLI (`nexus-yqnr.7`).
- **Phase 6** : plugin docs reshaped to conformant 4-segment examples (`nexus-yqnr.8`); OpenQ Q2 resolved as "keep per-collection upgrade message" (`nexus-yqnr.9`).

The arc absorbed two RDR-101 carryover beads (`nexus-2r71` strict-flip into Phase 5; `nexus-qpet.4` indexer rewrite into Phase 3a + 3b jointly) and surfaced two operator-visible bugs in sandbox shakedown:
- `nexus-7vuw` : `owners.UNIQUE(name)` schema bug split rdr collections via INSERT OR REPLACE silently obliterating the repo owner; fixed by composite `UNIQUE(name, owner_type)` plus an in-place migration that detects legacy auto-index and rebuilds.
- `nexus-hmxi` : `nx store list --collection knowledge__delos` auto-promoted 2-segment input to conformant; `nx search --corpus knowledge__delos` used the input as-is. Operators got split read/write views of legacy collections. Fixed by threading `t3=` explicitly through MCP/CLI surfaces so `t3_collection_name` can grandfather only collections that actually exist in T3.

## What worked

**Catalog as authority collapsed strict-naming enforcement to one site.** RDR-101 Phase 6 had shipped `T3Database.strict_collection_naming` as an opt-in flag, deliberately threading `strict=False` through ~30 production sites and ~26 test mocks before a future flip. The catalog-as-authority refactor made that threading unnecessary: every new write path constructs names via `Catalog.collection_for*`, which always emits conformant. The Phase 5 flip removed the flag entirely. The single guard at `src/nexus/db/t3.py:547` is now the single point of enforcement.

**`CollectionName` tuple before logic migration.** Phase 1 introduced the tuple type with no behaviour change; subsequent phases moved logic onto it. By Phase 3 every call site that constructed names was rewritten to ask the catalog for one, so the type-system caught most regressions at compile time. The non-conformant escape hatch (`is_conformant_collection_name` / `parse_conformant_collection_name`) preserved a clean shape for legacy-tolerant callers.

**Phase 4 migration on first index.** Operators do not run a separate migration verb. The first `nx index repo` after upgrade detects legacy collections (both pre-RDR-101 2-segment and post-Phase-6 path-derived 4-segment), uses `Catalog.rename_collection` to rename them in place, and emits exactly one `Upgraded legacy collection` line per content_type per repo. Idempotent: re-running the index after migration emits zero migration lines.

**Sandbox shakedown caught the two bugs the unit suite missed.** `nexus-7vuw` and `nexus-hmxi` are both shapes of "two surfaces holding the same name pattern with different policies." Neither was reachable from the unit test suite because both required walking through the operator-visible CLI flow with realistic state. The `release-sandbox.sh` shakedown sequence (HOME isolation + step-by-step nx invocations + the new step 11 release-gate) surfaced both same-day with regression tests pinning the round-trip invariants.

**Pinned decisions held under pressure.** The epic description listed four pinned decisions (migration uses indexer's CURRENT canonical model, model-name change is not a version bump, `knowledge__rdr_postmortem__{repo}` collapses, parse raises on legacy). The implementation hit all four naturally; none of them needed re-litigation during phase work or during the two follow-up bug fixes.

## What surprised me

**`_migration_source_candidates` had to detect TWO legacy shapes, not one.** Initial implementation only enumerated 2-segment legacy names (`code__nexus-8c2e74c0`). Sandbox shakedown showed that the prior strict-naming flag, when off, had also produced path-derived 4-segment names (`code__nexus-8c2e74c0__voyage-code-3__v1`) on greenfield runs that bypassed the catalog. The migration had to rename both shapes onto the conformant tuple-derived name. Fix landed in `nexus-7vuw`: enumerate both candidates per `(repo, content_type)` and let the existing `Catalog.rename_collection` handle whichever exists.

**Eager `make_t3()` in `t3_collection_name` broke unrelated tests.** First cut at grandfathering legacy 1/2-segment input was to probe T3 unconditionally. That hit cloud T3 in CI for `nx index pdf --collection knowledge__delos` (the test seeds a real `knowledge__delos` collection ID), and ran before the credential check in memory promote tests. Fix: opt-in `t3=` keyword on `t3_collection_name`. CLI/MCP surfaces that have a `T3Database` available pass it; tests and tools without one get pure auto-promotion to conformant.

**`UNIQUE(name)` on owners was load-bearing in a wrong way.** The `owners` table predated RDR-101 Phase 4. Adding a Phase-4 path that registered the same owner name under a different `owner_type` (repo vs path-derived synth) silently fired INSERT OR REPLACE and obliterated the existing repo owner. The split-rdr-collections symptom was a downstream consequence: subsequent T3 lookups via owner tumbler returned nothing because the repo owner was gone. Fixed by composite `UNIQUE(name, owner_type)` and an in-place migration that rebuilds the table when the legacy single-column unique index is detected.

**OpenQ Q2 was over-thought.** The original concern was "operator with hundreds of collections sees hundreds of upgrade lines on first run." The migration loop iterates over `("code", "docs", "rdr")`, so a single `nx index repo` invocation emits at most 3 `Upgraded legacy collection` lines, then 0 thereafter. The "hundreds of collections" concern was a cumulative T3-store property, not a per-invocation message count. Resolved as "keep per-collection lines, no `--quiet` flag."

## What didn't ship and why

**Background re-embed on `rename-collection`.** Inherited from RDR-101 Phase 6 design: chunks are NOT moved by rename; they become orphans for `nx t3 gc` to sweep, and the operator re-indexes to repopulate the target. Documented in `nx catalog rename-collection --help`. RDR-103 did not change this contract; a true background re-embed job remains a possible follow-up.

**`registry._collection_name`/`_docs_collection_name`/`_rdr_collection_name` still exist in dormant state.** The Phase 5 flip removed all production callers, but the helpers remain in `registry.py` as the legacy-name synthesizers used by `_migration_source_candidates` to enumerate the prior pre-strict path-derived shape. They are write-side dormant: no production call site invokes them. Removal is gated on the next major when migration completes for all in-the-wild operators.

**Cross-tool grandfathering for non-conformant `--collection` arguments other than `--corpus` and `--collection`.** The fix in `nexus-hmxi` covered `nx store *` and `nx search`. Other CLI surfaces that take a collection name (e.g. `nx t3 gc --collection`) were not audited; they currently rely on the conformant-only path. If a future incident shows an operator-visible split, the fix is to thread `t3=` through that surface, mirroring the `nexus-hmxi` pattern.

## Final shape

Today, `develop` carries:

- One naming authority (`Catalog.collection_for*`); the indexer asks, never constructs.
- One enforcement site (`T3Database.get_or_create_collection`'s strict guard); no flag thread.
- One tuple type (`CollectionName`) used end-to-end; legacy callers gate with `is_conformant_collection_name` before parse.
- One migration entry point (`_migrate_legacy_collections` on first index per content_type); operator reads at most 3 upgrade lines per invocation.
- Two follow-up bug fixes pinned by regression tests so the surfaced asymmetries cannot recur.

The "split authority" Gap 1 from the original Problem Statement is structurally closed: the two writers became one. Gap 2 ("embedding model is implicit") is closed by `canonical_embedding_model(content_type)` being part of the tuple. Gap 3 ("strict-naming enforcement scales linearly with caller count") is closed by collapsing 30 sites to 1. Gap 4 ("migration path constrains the design") is closed by the on-first-index migration absorbing both legacy shapes through the existing `rename_collection` primitive.

`nexus-yqnr` is closed at 9/9; the arc shipped on `develop` alongside the RDR-101 close (PR #498). The conformant 4-segment shape is the only collection name reachable from new writes, and the catalog is the source of those names.
