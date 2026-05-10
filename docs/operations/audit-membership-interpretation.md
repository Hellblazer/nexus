# Interpreting `nx catalog audit-membership` output

`audit-membership` reports per-collection contamination signals: how many distinct "homes" the collection's documents come from. A clean collection has one home (one repo, one curator import directory, one DEVONthink database). A contaminated collection has 2+, and the verb flags it.

But "contamination" lumps together three structurally different situations. Knowing which axis you're on tells you whether to act, and how.

## The three axes

### Axis 1: true cross-project leak

A document genuinely registered in the wrong collection. The 2026-05-08 prod probe's canonical example: tumbler `1.653.2` (`SOVEREIGN2-Grossberg2019.pdf`, content_type=paper, owner=papers curator) registered into `knowledge__knowledge` instead of `knowledge__art-grossberg-papers__voyage-context-3__v1`. `knowledge__knowledge` is reserved for MCP-stored knowledge notes (content_type=knowledge); a paper row there violates the content-type-vs-collection invariant.

**Detection signal**: a single document whose `physical_collection` doesn't match the curator's owned collection. The audit shows the collection as having 2+ homes when "homes" is computed at the source-URI / repo-root level.

**Action**: `nx catalog show <tumbler>` to confirm; `nx catalog delete <tumbler>` (4.29.1 backup-before-delete makes this reversible); re-index the source into the right collection. Add a regression test asserting the invariant (e.g. `tests/test_catalog_papers_curator_isolation.py` for the papers/knowledge case).

### Axis 2: self-marker noise

A collection-creation artifact row with `chunk_count=0` and empty `source_uri`. These are placeholder documents that the indexer or operator created when the collection was first registered, and they never received any actual chunks. They flip the audit's "distinct homes" count from 1 to 2 because the empty-URI bucket counts as its own home.

**Detection signal**: the second home is the empty-URI bucket; the document has `chunk_count=0`. There are usually 1-3 of these per affected collection.

**Action**: `nx catalog delete <tumbler>` to sweep. The 2026-05-08 shakeout cleaned 13 such rows under bead `nexus-4yfr` (all `chunk_count=0`, all confirmed safe via the bead's pre-audit). Reversible via `nx catalog undelete`.

### Axis 3: multi-source corpus

A collection that is intentionally fed from more than one home. Common shapes:

- A `knowledge__art-grossberg-papers` collection that imports from BOTH a DEVONthink database AND a `~/git/ART/SOVEREIGN-papers/` directory; both are legitimate "homes" for the curator.
- A `code__workspace-XXX` collection that scans multiple repo roots under one workspace umbrella.

**Detection signal**: 2+ homes, all of them legitimate (real source URIs, real `chunk_count > 0`); none match the cross-project-leak shape (wrong content_type, wrong owner).

**Action**: ignore. Optionally suppress by adding the collection to an allow-list once the audit verb supports one (not yet shipped; track under future operator UX work).

## How to triage in practice

For each collection the audit flags:

1. Open the collection's documents:
   ```
   nx catalog list --collection <name>
   ```
2. Check the home of each: count distinct `source_uri` after collapsing DEVONthink UUIDs (per `nexus-n3md` the `_DEVONTHINK_HOME_KEY` sentinel does this) and excluding the empty-URI bucket.
3. If any document has `chunk_count=0` AND empty `source_uri`: axis 2, delete it.
4. If any document's `content_type` doesn't match the curator's expectation (e.g. content_type=paper in knowledge__knowledge): axis 1, delete + re-index into the right collection.
5. Anything left over with 2+ legitimate non-empty homes and `chunk_count > 0`: axis 3, fine.

## Worked example: 2026-05-08 shakeout

The verb flagged 15 collections as contaminated. After applying the three-axis decomposition:

- **Axis 1 (true leak)**: 1 collection (`knowledge__knowledge`, the SOVEREIGN2 misregister). Filed `nexus-frai`, prod data fix run 2026-05-10, regression test added.
- **Axis 2 (self-marker)**: 13 collections, 13 rows total. Filed `nexus-4yfr`, swept 2026-05-10.
- **Axis 3 (multi-source)**: 2 collections (`knowledge__art-grossberg-papers`, `knowledge__rag-papers`). Pre-fix the audit reported 110+ homes for the first because every DEVONthink UUID was a distinct home; `nexus-n3md` (PR #662) collapsed them so the audit now reports the 4 logical roots.

Net: the audit went from "15 contaminated" to "0 contaminated" once each axis was handled with the right verb.

## Cross-references

- [`t3-health.md`](t3-health.md) for the `nx catalog doctor` runbook.
- [`../architecture.md`](../architecture.md) § Metadata field semantics for `content_hash` vs `chunk_text_hash`, `source_uri` vs `source_path`.
- Beads: `nexus-frai` (axis 1 example + regression test), `nexus-4yfr` (axis 2 sweep), `nexus-n3md` (DEVONthink home-key fix), `nexus-fntl` (axis 3 partially-resolved).
