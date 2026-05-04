# Post-Mortem: RDR-101, Event-Sourced Catalog Migration

**RDR:** [RDR-101](../rdr-101-catalog-t3-metadata-design.md)
**Status:** closed (implemented), 2026-05-04
**Epic:** `nexus-o6aa` (14/14 child beads, 100% complete)
**Phases shipped:** 0 (audit) → 6 (enforcement); irreversible flips at 5b
**Final landing:** Phase 6 release-gate (PR #497) on `develop`

## Outcome

Greenfield event-sourced catalog architecture, replacing field-ownership-with-drift-shims. Append-only `events.jsonl` is canonical truth; SQLite catalog and T3 chunk metadata are deterministic projections of the log; immutable `doc_id` (UUID7-shaped tumblers) is the only join key.

Phases as shipped:

- **Phase 0** : field disposition audit (~30 keys), bib_semantic_scholar_id home, RDR-086 chash_index naming collision, chunk_id generation rule, downstream caller surveys (`nexus-o6aa.1` through `.6`).
- **Phase 1** : event log infrastructure (`nexus-o6aa.7`), write-only at first.
- **Phase 2** : synthesize log from existing JSONL state, T3 chunk doc_id backfill (`nexus-o6aa.8`).
- **Phase 3** : new write path, irreversibility window starts (direct-SQLite-mutation prohibited; `nexus-o6aa.9`).
- **Phase 4** : reader migration + chunk-side prune of deprecated keys (`nexus-o6aa.10`); shipped as RDR-102 + spillover into 4.22.0.
- **Phase 5a/5b/5c** : opt-in flag → irreversible default-on flip → final schema removal (`.11/.12/.13`); shipped in 4.22.0.
- **Phase 6** : `nx catalog doctor --collections-drift` as release gate, collection-naming validation enforced at create time, fallback collection migration verb (`.14`); shipped today as PR #497.

The arc absorbed RDR-102 (Phase 4 completion spike) and RDR-103 (collection-name authority refinement). RDR-103 itself ran Phases 1-6 + 2 follow-up bug fixes in a single day arc on top of RDR-101's irreversibility commitment.

## What worked

**Phased irreversibility was the correct shape.** Phases 0-4 are reversible: the system continues to work if rollback. Phase 5b (default-on flip) is the SOLE irreversible commitment, gated behind an audit + a release window. Phase 5c (final schema removal) bracketed the irreversible cut. Operators got a soak window between Phase 5a (opt-in flag) and Phase 5b (default-on); the migration verbs (`synthesize-log`, `t3-backfill-doc-id`, `prune-deprecated-keys`, `repair-orphan-chunks`, `migrate`) bridged the gap.

**The substantive-critic gate caught the field-ownership rejection at design time.** The original RDR-101 draft proposed assigning each duplicated field to one store as authority and demoting the other to read-through cache. The critic identified three concrete blockers (head_hash idempotency, JSONL replay, RDR-096 `title` collision) : all symptoms of duplication-without-referential-integrity. A phased migration that keeps duplication live during transition keeps the bug class live during transition. The pivot to event-sourcing was the structural fix, not a policy fix.

**Doctor as a deterministic verifier paid off twice.** `--replay-equality` (Phase 1) drives the projector against events.jsonl and diffs the projected SQLite against the live `.catalog.db`; mismatches are bugs, not heuristics. `--collections-drift` (Phase 6, today) enforces the projection ⊇ T3 ⊇ documents.physical_collection invariant as a release gate. Together they make any future drift a deterministic failure rather than an audit finding.

**Migration cleanup at the end was an honest signal.** The five transitional verbs (`migrate`, `synthesize-log`, `t3-backfill-doc-id`, `repair-orphan-chunks`, `prune-deprecated-keys`) were one-shot scaffolding. After Phase 5b shipped and operators had a release window to migrate, deleting the verbs (`nexus-iftc`) removed ~6,100 LOC and ~30% of `commands/catalog.py`. The deletion is itself evidence the transition completed: the scaffolding is no longer load-bearing.

**Sandbox shakedown caught real bugs at the last mile.** Today's tail of the arc surfaced two bugs the unit suite missed: (1) `nexus-7vuw` : `owners.UNIQUE(name)` schema bug producing split rdr collections via INSERT OR REPLACE silently obliterating the repo owner; (2) `nexus-hmxi` : `nx store list` auto-promoting 2-segment `--collection` input but `nx search --corpus` using it as-is, leaving operators with split read/write views of legacy collections. Both fixed same-day with regression tests pinning the round-trip invariants.

## What surprised me

**Phase 6 release-gate wiring exposed an asymmetry.** The drift check excludes `taxonomy__*` from the T3 set; `backfill-collections` registered them in the projection. Net: every greenfield run produced a permanent "projection row whose T3 collection is gone" report no operator could clear. Fix: backfill applies the same `_BYPASS_SCHEMA_PREFIXES` filter the drift check uses. Caught only because the gate ran with `--no-dry-run` (the verb defaults to dry-run, which printed the right thing without actually registering).

**The squash-merge pipeline silently dropped half a PR.** PR #496 was supposed to delete the 5 transitional verbs in `commands/catalog.py` AND their tests. The git_rm of one path raised `fatal: pathspec ... did not match` mid-list and let the subsequent `git add` paths skip silently; only the test-file deletions made it into the squash commit. CI passed because the production code wasn't actually broken (the verbs were still there). The next branch caught it because `nx catalog --help` still listed the deleted verbs. Lesson: trust-but-verify on `git add` exit semantics with multi-path arguments; or stage in smaller batches.

**RDR-103 was a sub-arc this RDR did not anticipate.** RDR-101 committed to `<content_type>__<owner_id>__<embedding_model>__<model_version>` as the conformant collection-name shape. The implementation discovered the catalog needed to be the SOURCE of names, not just a registrar. RDR-103 split out as its own RDR to formalize that, ran 6 phases + 2 surfacing-day bug fixes, closed alongside RDR-101 today. Two RDRs, one architectural commitment.

**Phase 4 and Phase 5c overlapped in 4.22.0.** Phase 4 (reader migration + prune verb) and Phase 5c (final schema removal of `corpus`/`store_type`/`git_meta`) shipped in the same release because the reader migration enabled the schema removal to be safe. The bead split-out (`nexus-o6aa.10` for Phase 4, `.13` for Phase 5c) tracked semantic boundaries; the release boundary was operational. The greenfield smoke (`nexus-e5uw`) caught the only place this overlap leaked: code-indexer write path was still writing pruned keys until Phase B's `make_chunk_metadata` signature hard-removal landed.

## What didn't ship and why

**Cloud-side greenfield smoke** (fresh ChromaDB Cloud DB + index + drift check). Deferred to next-release shakeout per the bead's own deferral. The chunk metadata schema is Python-side (`make_chunk_metadata` + `normalize`) and identical between local and cloud T3 : there is no cloud-specific code path that would re-leak deprecated keys. Local greenfield smoke (sandbox shakedown step 5 + step 11) covers the same write path; the cloud step is operator hygiene, not a missing gate. Chroma's cloud DB creation requires the web UI / their CLI, outside the local CLI smoke flow.

**Background re-embed on `rename-collection`.** The bead text said "rename-collection ... + background re-embed completes". The shipped design is "catalog re-point + orphan + gc + re-index" : T3 chunks are NOT moved by the rename; they become orphans for `nx t3 gc` to sweep, and the operator re-indexes to repopulate the target. Documented in `nx catalog rename-collection --help`. A true background re-embed job is a possible follow-up; today's design avoids the embedding cost on every rename.

**Operator UX for migration upgrade messages** (`nexus-yqnr.9`, the RDR-103 OpenQ). Deliberately resolved as "keep per-collection lines, no `--quiet` flag". The migration loop iterates over `("code", "docs", "rdr")` so a single `nx index repo` invocation emits at most 3 `Upgraded legacy collection` lines, then 0 thereafter (idempotent). The "hundreds of collections" concern was a cumulative T3-store property, not a per-invocation message count.

## Final shape

Today, `develop` carries:

- One catalog, one canonical truth (events.jsonl), two deterministic projections (SQLite, T3 chunk metadata).
- One join key (`doc_id` tumbler) between catalog and T3; no more string-equality joins on `source_path`/`source_uri`.
- One naming grammar (`<ct>__<owner>__<model>__v<n>`) enforced at write time by the strict-naming guard.
- One release gate (`nx catalog doctor --collections-drift`) wired into the release shakedown.
- Zero migration scaffolding : the five transitional verbs are deleted; the schema-divergence remediation has no work left to do.

The "drift between catalog and T3 produces silent empty results" Gap 1 from the original Problem Statement is structurally closed: the duplicated fields no longer exist; the chunks know their `doc_id`; the projection is reproducible from the log. The 2026-04-29 incident (catalog `chunk_count = 0` while T3 held 63,077 chunks) cannot recur because `chunk_count` is no longer a write-divergent field : it is a Document attribute updated by the same event the chunks reference.

`develop` is 60 commits ahead of `main`; the arc is ready for a 4.23.0 release whenever the operator wants to tag.
