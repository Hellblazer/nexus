---
title: "RDR-077: Projection Quality — Similarity Scores and ICF Hub Detection"
status: accepted
type: feature
priority: P2
created: 2026-04-14
accepted_date: 2026-04-14
github_issue: 161
related: [RDR-075, RDR-076]
reviewed-by: self
---

# RDR-077: Projection Quality — Similarity Scores and ICF Hub Detection

Follow-up to RDR-075 surfaced during the v4.2.2 live shakeout. Cross-collection projection now runs across 93 collections with 689,710 assignments at 92.2% chunk coverage, but the projection graph has two correctable quality problems: we throw away the similarity score that drove each assignment, and generic-pattern hub topics dominate the graph and drown out domain-meaningful signals.

## Problem

### Problem 1: Similarity score is not persisted

`topic_assignments` stores `(doc_id, topic_id, assigned_by)` and nothing else. The cosine similarity that produced each projection (already computed in memory by `project_against`) is discarded on write. Downstream this means:

- No confidence filtering at query time — we cannot rank projected assignments or filter low-confidence matches without re-running the full matrix multiply.
- No auditability — we cannot inspect which assignments were borderline vs. high-confidence.
- No drift detection — we cannot observe whether re-projections move chunks toward or away from their current topics.
- No temporal queries — `assigned_at` is also missing, so "what did the last backfill add?" is unanswerable.

The data to fix this exists; the schema doesn't.

### Problem 2: Generic-pattern hubs dominate projection

Top 10 cross-collection hub topics by chunk count after backfill (threshold 0.50):

| Topic | Cross-coll chunks | Source collections | Domain meaningful? |
|---|---:|---:|:---:|
| Unit Testing and Assertions (Luciferase) | 21,307 | 7 | No — generic |
| Unit Test Assertions Mocking (workspace) | 13,884 | 9 | No — generic |
| Java Class and Interface Implementation (Delos) | 13,701 | 7 | No — language pattern |
| Binary Operator Expression Parsing (Kramer) | 13,374 | 6 | No — generic |
| Builder Pattern Implementation (Luciferase) | 11,047 | 6 | No — design pattern |
| JUnit Test Class Structure (Delos) | 10,984 | 7 | No — generic |
| Member Proposal Consensus Import (Luciferase) | 10,151 | 3 | **Yes — Stereotomy/consensus** |
| Variable Declaration and Types (ART) | 9,517 | 7 | No — language pattern |
| GPU Vendor Configuration (Luciferase) | 8,710 | 5 | Possibly |
| Exception Handling in Tests (Luciferase) | 8,219 | 7 | No — generic |

9 of the top 10 hubs are **language / framework boilerplate**. They appear in nearly every Java codebase and project on everything because Java syntax tokens dominate code-corpus centroids. The one genuinely meaningful hub (Member Proposal Consensus, Stereotomy-specific) is buried.

This is the TF-IDF stopword problem applied to topics: high document frequency is a weak discriminator. Users exploring the graph get led to "Unit Testing and Assertions" instead of to Stereotomy, because similarity alone doesn't discount ubiquity.

### Root Cause

1. `project_against` computes similarity in memory then calls `assign_topic(doc_id, topic_id, assigned_by='projection')` — the similarity value has no column to land in.
2. `project_against` filters by raw cosine similarity against a single threshold (default 0.85; 0.50 in the backfill). A language-pattern centroid clears that bar for code chunks in any Java corpus.

## Proposed Design

### Phase 1: Schema migration — similarity + assigned_at + source_collection

```sql
ALTER TABLE topic_assignments ADD COLUMN similarity        REAL;
ALTER TABLE topic_assignments ADD COLUMN assigned_at       TEXT;
ALTER TABLE topic_assignments ADD COLUMN source_collection TEXT;
CREATE INDEX IF NOT EXISTS idx_topic_assignments_source
    ON topic_assignments(source_collection, assigned_by);
```

All nullable. `similarity` populated by `project --persist` and the `taxonomy_assign_hook` post-store projection path. NULL for legacy HDBSCAN assignments — cluster membership is not a centroid distance and conflating the two would mislead downstream consumers.

`assigned_at` stamped at insert time (`datetime('now')`); NULL for rows predating the migration.

`source_collection` is **required for ICF computation** (RF-5) — without it, `DF(topic) = COUNT(DISTINCT source_collection)` is unanswerable from T2 because `doc_id` hashes the collection in an unrecoverable way. Populated at insert time by every `assigned_by='projection'` write path.

One-line registration in `nexus.db.migrations.MIGRATIONS` per RDR-076 pattern:

```python
Migration(introduced="4.3.0", name="add_projection_quality_columns", fn=_add_projection_quality_columns),
```

The migration adds columns + index only; no data backfill. Pre-existing projection rows (from v4.2.x) keep NULL similarity/source_collection and are excluded from ICF/audit outputs until a `nx taxonomy project --backfill` reruns them.

### Phase 2: Populate similarity + source_collection on the write path

`taxonomy.assign_topic()` grows two optional parameters:

```python
def assign_topic(
    self,
    doc_id: str,
    topic_id: int,
    *,
    assigned_by: str = "hdbscan",
    similarity: float | None = None,
    source_collection: str | None = None,
) -> None:
```

**Upsert semantics split by `assigned_by` (RF-8; resolves PQ-6 → prefer-higher)**:

- Projection rows use a portable UPSERT that does not depend on SQLite 3.38+ `ON CONFLICT ... WHERE`:

  ```sql
  INSERT INTO topic_assignments
      (doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection)
  VALUES (?, ?, 'projection', ?, ?, ?)
  ON CONFLICT(doc_id, topic_id) DO UPDATE SET
      similarity        = MAX(topic_assignments.similarity, excluded.similarity),
      assigned_at       = CASE
                              WHEN excluded.similarity > COALESCE(topic_assignments.similarity, -1.0)
                              THEN excluded.assigned_at
                              ELSE topic_assignments.assigned_at
                          END,
      source_collection = CASE
                              WHEN excluded.similarity > COALESCE(topic_assignments.similarity, -1.0)
                              THEN excluded.source_collection
                              ELSE topic_assignments.source_collection
                          END;
  ```

  The `MAX()` + `CASE` pattern is valid on all SQLite versions that support `ON CONFLICT DO UPDATE` (3.24+, Python 3.12 ships 3.39+). No `WHERE` clause on the `DO UPDATE` target, which would require 3.38+.

  **NULL-existing guard**: `COALESCE(topic_assignments.similarity, -1.0)` inside the `CASE` predicate handles pre-migration legacy rows (where `similarity IS NULL`). Without it, `excluded.similarity > NULL` evaluates to NULL (falsy) and the provenance columns (`assigned_at`, `source_collection`) would silently *not* refresh even when `MAX()` promotes `similarity` from NULL to the new value — leaving the row internally inconsistent and invisible to ICF / hub queries that filter on `source_collection IS NOT NULL`. Since cosine similarity is in [0, 1], `-1.0` is safely below any real value.

  **Prefer-higher semantics**: re-projection with a better score refreshes the row and its timestamp; a worse score leaves the stored value untouched (`MAX` preserves it). Rationale: centroid drift is slow, spurious small-delta regressions are common (different sibling set + order-of-assign causes ±0.02 noise), stable-under-re-runs is the desired property.

- HDBSCAN rows keep `INSERT OR IGNORE` — cluster membership is deterministic per discover run.

**Stored vs. adjusted similarity (resolves Observation from Gate 2)**: the `similarity` column always stores the **raw cosine** value. ICF weighting (Phase 4) is applied at *query/ranking/filter* time, not at write time. This keeps the column audit-friendly (Phase 6), prevents re-projection oscillation when the ICF denominator changes as the corpus grows, and means hub-suppressed assignments are simply not written (the threshold filter in Phase 4 applies to the adjusted score; below-threshold rows never call `assign_topic`).

Five write-path updates (all must land in the same commit to keep the tree buildable):

1. **`project_against` (catalog_taxonomy.py:1501)**: change `chunk_assignments.append((doc_id, tid))` to `chunk_assignments.append((doc_id, tid, float(sim[i, idx])))`. The per-chunk similarity is already in memory at line 1497 — the tuple shape change is the whole fix. The `result['chunk_similarities']` key claimed in the v0 draft **does not exist** (RF-2); no code relies on it.

2. **`_persist_assignments` (taxonomy_cmd.py:1121)**: unpack the 3-tuple and pass `similarity=sim, source_collection=source` to `assign_topic`.

2a. **`backfill_projection` (db/migrations.py:352-353)** — **already-executing production code, not a stub**: currently reads `for doc_id, topic_id in assignments:` and calls `taxonomy.assign_topic(doc_id, topic_id, assigned_by='projection')`. Must change to 3-tuple unpack + pass `similarity` and `source_collection` in the same Phase 2 commit, otherwise `nx upgrade` crashes with `ValueError: too many values to unpack` on any deployment re-running the RDR-075 T3UpgradeStep. SC-8 must include a regression test that exercises `backfill_projection` against the Phase-1 schema.

3. **`taxonomy_assign_hook` (mcp_infra.py:321)**: `assign_single` already receives a ChromaDB `results` dict that includes `distances` but discards it at line 1299. Change `assign_single` return type from `int | None` to `AssignResult | None` (NamedTuple with `topic_id: int` + `similarity: float`). **This is a breaking API change.** All call sites must migrate atomically in the same commit:
   - `mcp_infra.py:316` — `topic_id = taxonomy.assign_single(...)` → `result = taxonomy.assign_single(...); topic_id = result.topic_id if result else None`
   - `mcp_infra.py:321` — same pattern; similarity passed to the cross-collection `assign_topic` call at line 325
   - Any internal helpers in `catalog_taxonomy.py` that call `assign_single`

   Using a NamedTuple (not bare tuple) means `if topic_id is not None` on the old scalar path fails-loud at type-check, not silent-true on a non-None tuple. Add explicit mypy/ruff check in the migration commit.

   **Exact distance extraction**: ChromaDB returns `distances` as list-of-lists (`[[d1, d2, ...]]`) even for `n_results=1`. The correct capture is `similarity = 1.0 - float(results["distances"][0][0])` (ChromaDB returns cosine *distance*, not similarity — invert). Add SC-8 test for this extraction shape to guard against `IndexError` on the wrong axis.

4. **`assign_batch(..., cross_collection=True)` (catalog_taxonomy.py:1357/1363)**: same tuple-shape change as `project_against`.

HDBSCAN path (`assign_batch` from `discover()`) continues to pass `similarity=None, source_collection=None` — NULL is semantically correct for cluster membership.

### Phase 3: ICF computation

**Inverse Collection Frequency** for a topic, using **log base 2** (conventional for IDF variants; predictable numeric scale for threshold calibration):

```
ICF(topic) = log2(N_effective / DF(topic))
```

Where:

- `N_effective` = number of distinct `source_collection` values appearing in `topic_assignments` with `assigned_by='projection'` and `source_collection IS NOT NULL`. Pre-migration rows (NULL `source_collection`) are excluded from both numerator and denominator — they carry no collection signal and their inclusion would bias results toward spurious hubs.
- `DF(topic)` = number of distinct `source_collection` values in projection rows for that topic.

**Guards**:
- `DF = 0` is impossible by construction — if a topic has no projection assignments, it does not appear in the aggregation and has no ICF. Audit and hubs commands only query over joined projection rows.
- `DF = N_effective` yields `ICF = log2(1) = 0`. This is the intended behavior for ubiquitous hub topics — similarity contributes nothing after weighting.
- `N_effective < 2` (e.g. a single-collection corpus) disables ICF entirely — return raw similarity. See PQ-1.

**Authoritative SQL** (no placeholder — uses the new `source_collection` column from Phase 1):

```sql
WITH n_eff AS (
    SELECT COUNT(DISTINCT source_collection) AS n
    FROM topic_assignments
    WHERE assigned_by = 'projection' AND source_collection IS NOT NULL
)
SELECT
    ta.topic_id,
    COUNT(DISTINCT ta.source_collection)                        AS df,
    LOG2(CAST((SELECT n FROM n_eff) AS REAL)
         / COUNT(DISTINCT ta.source_collection))                AS icf
FROM topic_assignments ta
WHERE ta.assigned_by = 'projection' AND ta.source_collection IS NOT NULL
GROUP BY ta.topic_id
HAVING COUNT(DISTINCT ta.source_collection) > 0;
```

SQLite has no native `LOG2`; use `LOG(x) / LOG(2)` or register a Python scalar function at connection init (simpler, deterministic across SQLite versions).

**Caching**:
- `taxonomy_assign_hook` path (single-doc, real-time): **no cache**. ICF for one topic is a single index-scan and recomputing per assignment avoids stale-cache hazards from concurrent MCP writes.
- `nx taxonomy project --persist` path (bulk): compute full ICF map once at the start of the command run, hold it for the duration. The corpus is effectively frozen during a single bulk run (MCP writes during the run are rare and their absence from this run's ICF is acceptable — next `--persist` picks them up). Cache lives on the local `CatalogTaxonomy` call object and is garbage-collected when the command returns.
- `nx taxonomy hubs` / `nx taxonomy audit`: per-invocation compute; no cross-invocation cache.

### Phase 4: ICF-weighted projection

New option on `nx taxonomy project` and on `taxonomy_assign_hook`:

```bash
nx taxonomy project SRC --use-icf --threshold 0.50 --persist
```

At assignment time:
```
adjusted_similarity = raw_cosine * ICF(target_topic)
```
Hub topics (DF→N) have ICF→0 and get suppressed. Domain-specific topics (DF=1..3) retain most of their similarity.

Per-corpus-type default thresholds (applied when `--threshold` not explicit):

| Source prefix | Default threshold | Rationale |
|---|---:|---|
| `code__*` | 0.70 | Java/Python syntax inflates raw cosine; stricter cut-off filters pattern noise |
| `knowledge__*` | 0.50 | Dense prose, semantically rich — threshold validated in backfill |
| `docs__*` | 0.55 | Mixed prose + code snippets |
| `rdr__*` | 0.55 | Same as docs |

Threshold calibration process documented in `docs/taxonomy-projection-tuning.md` (new).

### Phase 5: `nx taxonomy hubs` — generic-pattern detector

```bash
nx taxonomy hubs --min-collections 5 --max-icf 1.2
```

Lists topics flagged as likely generic hubs: high cross-collection DF, low ICF, label contains stopword-like tokens (configurable list: `assert`, `junit`, `builder`, `class`, `import`, `exception`, `getter`, `setter`, `variable`, `declaration`, `operator`). Output sorted by `chunks × (1 - ICF)` to surface the worst offenders first.

**Staleness warning (related to PQ-7)**: `hubs` output accuracy degrades after `discover --rebuild` — stored `similarity` values and `assigned_at` timestamps were stamped against pre-rebuild centroids. Add `--warn-stale` flag that compares `max(assigned_at)` for each contributing collection against the most recent `discover --rebuild` timestamp (tracked in `taxonomy_meta.last_discover_at`) and prints a warning when hub evidence is older than the last rebuild. Implementation note: `taxonomy_meta.last_discover_at` already exists from RDR-075 infrastructure.

### Phase 6: `nx taxonomy audit`

```bash
nx taxonomy audit --collection docs__nexus
```

Distribution of similarities for the collection's projected assignments: quantiles (p10/p50/p90), count below threshold (for re-projection detection), top hub topics receiving this collection's chunks, likely pattern-pollution flags.

## Success Criteria

- **SC-1**: `topic_assignments` has `similarity REAL NULL`, `assigned_at TEXT NULL`, and `source_collection TEXT NULL` columns after the 4.3.0 migration, plus the `(source_collection, assigned_by)` index.
- **SC-2**: `project --persist` and `taxonomy_assign_hook` populate `similarity` and `source_collection` for every projection assignment; HDBSCAN assignments keep NULL for both. Re-projection uses **prefer-higher upsert**. Three-case test: (a) re-project with `similarity=0.9` then `similarity=0.7` — stored row reads `0.9`; (b) re-project with `0.7` then `0.9` — stored row reads `0.9` with refreshed `assigned_at` and `source_collection`; (c) re-project a legacy row (pre-migration, `similarity=NULL`, `assigned_at=NULL`, `source_collection=NULL`) with `similarity=0.6` — all three columns populated with the new values (verifies the `COALESCE(-1.0)` NULL guard).
- **SC-3**: `nx taxonomy project --use-icf` applies ICF weighting and produces a measurably reordered top-K ranking vs. default on a test fixture of ≥10 collections. **The ≥10 is a calibration choice** (ensures ICF scores have enough spread to reorder the top-K), not a guard threshold — ICF is only disabled when `N_effective < 2` per PQ-1. A 3-collection fixture would still exercise ICF correctly but might not produce a visible ranking change.
- **SC-4**: `nx taxonomy hubs` detects synthetic pattern-pollution hubs in the test fixture. Fixture construction: 5 fake collections × 100 docs each, half of them assigned to a single "stopword-labeled" topic (label contains `assert` or `junit`), the other half spread across distinct domain topics. The detector must surface the stopword-labeled topic as a hub and exclude single-collection domain topics. Live-corpus validation (real top-10 hubs) is an observational acceptance signal but **not** the SC — it cannot be made reproducible without bundling the live DB.
- **SC-5**: Per-corpus-type default thresholds applied when `--threshold` not specified; documented in CLI help and in `docs/taxonomy-projection-tuning.md`.
- **SC-6**: `nx taxonomy audit` prints similarity distribution (p10/p50/p90), below-threshold count, top receiving hubs, and pattern-pollution flags for a collection.
- **SC-7**: Post-ICF backfill measurement shows domain-meaningful hubs (e.g. Member Proposal Consensus) ranked above language-pattern hubs by `chunks × ICF`.
- **SC-8**: Zero regressions on `tests/test_taxonomy.py`; new tests cover migration, similarity persistence (including prefer-higher upsert invariant and `results["distances"][0][0]` extraction shape), ICF computation (log2 base, N_effective definition, DF=N → 0, N<2 disables), hubs detection against the synthetic fixture, and a `backfill_projection` regression test that exercises the Phase-1 schema end-to-end to catch tuple-shape drift in `db/migrations.py`.

## Research Findings

### RF-1: Schema claim confirmed; no similarity, assigned_at, or source_collection

`src/nexus/db/t2/catalog_taxonomy.py:80-85`:

```sql
CREATE TABLE IF NOT EXISTS topic_assignments (
    doc_id      TEXT NOT NULL,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    assigned_by TEXT NOT NULL DEFAULT 'hdbscan',
    PRIMARY KEY (doc_id, topic_id)
);
```

Three inline migrations on `_init_schema` (lines 181–213: `migrate_topics`, `migrate_assigned_by`, `migrate_review_columns`) — none add the missing columns. `src/nexus/db/migrations.py` has zero hits for `similarity` or `assigned_at`.

Seven INSERT sites total, all three-column: `assign_topic()` at line 250 plus inline copies at lines 469, 745, 1026, 1771, 1807, 1821. Every write path has to grow to accept similarity; can't leave legacy paths writing the narrower tuple because `INSERT OR IGNORE` (see RF-8) will silently win over later updates.

### RF-2: Similarity drop site — RDR's key name was wrong

In `project_against` (`catalog_taxonomy.py:1370`):

- Matrix multiply at line 1481: `sim = src_norm @ ctr_norm.T`
- Per-centroid value read at line 1497: `float(sim[i, idx])` — accumulated into `topic_stats[tid]["total_similarity"]`
- `chunk_assignments` built at line 1501: `chunk_assignments.append((doc_id, tid))` — bare `(str, int)` tuple. **Similarity dropped here.**

Return dict (lines 1536–1542): `matched_topics`, `novel_chunks`, `chunk_assignments`, `total_chunks`, `total_centroids`. **No `chunk_similarities` key — the earlier design note was wrong.** `matched_topics[].avg_similarity` is a per-topic aggregate, not per-chunk. The fix is to change `chunk_assignments` to `list[tuple[str, int, float]]` — the data is already in memory at line 1497.

### RF-3: assign_topic signature and call sites

`assign_topic(doc_id, topic_id, *, assigned_by='hdbscan')` — line 244, no similarity param. Four call sites pass `assigned_by='projection'`, all must migrate atomically in the Phase 2 commit:

- `mcp_infra.py:325` — `_run_taxonomy_assign` cross-collection branch
- `taxonomy_cmd.py:1121` — `_persist_assignments()` (backfill and project commands)
- `db/migrations.py:353` — T3 upgrade step backfill (RDR-075)
- `catalog_taxonomy.py:1357/1363` — `assign_batch` with `cross_collection=True` (both branches of the batch call site count as one migration target)

Additional inline INSERT sites in `catalog_taxonomy.py` not passing `assigned_by='projection'` (lines 469, 745, 1026, 1771, 1807, 1821) also need tuple-shape audit — `INSERT OR REPLACE` at 1807/1821 already partially implements upsert for manual-assignment paths and is compatible with the new column list.

### RF-4: taxonomy_assign_hook — similarity available at zero cost

`_run_taxonomy_assign` (`mcp_infra.py:284-325`) calls `assign_single` (line 321), which queries `taxonomy__centroids` at line 1292 and returns only `int(results["metadatas"][0][0]["topic_id"])` at line 1299. **`results["distances"]` is returned by ChromaDB but never read.** Capturing it is a one-line change in `assign_single`; no extra query, no extra cost.

### RF-5: ICF data path requires a source_collection column — PQ-5 is mandatory

No direct collection lookup from `doc_id` exists:

- `topic_assignments` has only `(doc_id, topic_id, assigned_by)`.
- `topics.collection` is the **target** topic's collection, not the source.
- `doc_id` for T3 chunks is `sha256("{collection}:{title}")[:16]` (`db/t3.py:380`) — collection hashed in, not recoverable.
- No `chunk_metadata` table exists.

**PQ-5 graduates from an open question to a requirement.** Without `topic_assignments.source_collection`, computing `DF(topic) = COUNT(DISTINCT source_collection)` is impossible from T2. T3 reverse-lookup is per-doc and will not scale to 689k rows.

### RF-6: Centroid dimensionality is mode-dependent

Collection name: `taxonomy__centroids`. Comment at `catalog_taxonomy.py:882`: *"Uses embedding_function=None (pre-computed MiniLM 384d vectors)"*. In cloud mode, `_run_taxonomy_assign` fetches the existing T3 Voyage embedding (`mcp_infra.py:299-304`, 1024d) before calling `assign_single`. `_check_centroid_dimension` (lines 111–134) guards against mismatch. **The RDR's "1024d voyage vectors" claim is only true in cloud mode.** All math in this RDR (similarity, ICF) is dimension-agnostic; no design change — just accuracy fix in narrative.

Fetch path in `project_against`: paginated GET at lines 1448–1452, no query embedding, raw matrix multiply.

### RF-7: Hub SQL — current schema cannot distinguish hubs from volume

With `source_collection` column landed (from RF-5 requirement):

```sql
SELECT
    t.id, t.label, t.collection,
    COUNT(*)                                     AS total_projection_chunks,
    COUNT(DISTINCT ta.source_collection)         AS distinct_source_collections
FROM topic_assignments ta
JOIN topics t ON t.id = ta.topic_id
WHERE ta.assigned_by = 'projection'
GROUP BY t.id, t.label, t.collection
ORDER BY distinct_source_collections DESC, total_projection_chunks DESC
LIMIT 20;
```

Today, the best the schema can do is rank topics by projection chunk volume — blind to whether those chunks came from one collection or twenty. **Hub detection is impossible without the schema change.** The "top-10 hubs" table in the Problem section was produced by external SQL joining against separate collection metadata (live backfill); it cannot be reproduced from T2 alone in v4.2.2.

### RF-8: `INSERT OR IGNORE` blocks similarity refresh

`assign_topic` uses `INSERT OR IGNORE` (line 250). After the schema migration, re-projecting a doc that already has an assignment row will silently skip — the similarity column will never update. Need explicit `INSERT ... ON CONFLICT DO UPDATE SET similarity = excluded.similarity, assigned_at = excluded.assigned_at WHERE excluded.similarity > similarity` (or unconditional REPLACE for projection assignments). HDBSCAN rows stay `INSERT OR IGNORE` — cluster membership is deterministic per discover run.

### RF-9: Re-discovery interaction with Phase-3 post-pass

`discover_topics` exits early if any topics already exist for the collection (`catalog_taxonomy.py:963-974`). Docs indexed **after** the initial discover are assigned to existing centroids that were computed without them. Projection assignments from the `taxonomy_assign_hook` path on a newly-indexed doc will use those stale centroids, and there is no mechanism to refresh projection assignments after a later `discover --rebuild`. This is an RDR-075 legacy issue but compounds here: the similarity column will be stamped with a value computed against potentially stale centroids.

**Mitigation option**: `nx taxonomy audit` (Phase 6) can surface the delta by comparing stored `similarity` against a fresh recomputation — falling similarities indicate centroid drift.

### Design impacts from research

| Original design | Revised after research |
|---|---|
| Add `similarity` + `assigned_at` columns | Add `similarity` + `assigned_at` + **`source_collection`** (PQ-5 mandatory) |
| Carry similarity via `result['chunk_similarities']` | Change `chunk_assignments` tuple to `(doc_id, topic_id, similarity)` — key did not exist |
| "1024d voyage centroids" | Dimension-agnostic; cloud=1024d (voyage), local=384d (MiniLM) |
| `assign_topic` INSERT OR IGNORE unchanged | Split by `assigned_by`: projection needs upsert; HDBSCAN keeps ignore |
| taxonomy_assign_hook — no similarity | Similarity is already in ChromaDB response, one-line capture |

## Proposed Questions

- **PQ-1**: Does ICF weighting over-correct for small corpora? With only 3 collections, `log(3/2) ≈ 0.4` squashes even legitimate high-similarity matches. Minimum-collection threshold below which ICF weighting is disabled?
- **PQ-2**: Config-driven per-corpus-type thresholds (`.nexus.yml`) vs. hard-coded, given users' corpora differ?
- **PQ-3**: Stopword token list for `hubs` detection — bundled defaults + user extension, or pure defaults?
- **PQ-4**: How to distinguish "generic hub" (language/framework noise) from "cross-cutting concept" (e.g. "Consensus" legitimately spanning multiple collections in a blockchain workspace)? Label-token heuristic conflates them. **Risk**: `nx taxonomy hubs` will flag legitimate cross-cutting concepts as pollution in domain-specific workspaces. Mitigation: `hubs` output is advisory (not auto-remediation); `--explain` flag shows WHY a topic was flagged so the user can accept or suppress via user-extensible stopword list (PQ-3).
- **PQ-5** (RESOLVED by RF-5 — now mandatory): Adding `source_collection` to `topic_assignments` is required, not optional. Without it, ICF is uncomputable from T2.
- **PQ-6** (RESOLVED — prefer-higher): Upsert semantics for projection rows use prefer-higher (Phase 2, lines 97–102). Rationale: centroid drift is slow, spurious small-delta regressions are common (order-of-assign noise ±0.02), stable-under-re-runs is the desired property. Always-latest rejected.
- **PQ-7** (from RF-9): Should `discover --rebuild` invalidate prior projection assignments for the rebuilt collection (to force re-projection against fresh centroids)? Or leave them and flag divergence via audit?

## Related

- Supersedes GitHub issue **#161** (see issue for the original problem statement and concrete data).
- Builds on RDR-075 (cross-collection projection) and uses the migration registry from RDR-076.
- Tracked by bead `nexus-act`.

### RDR-075 bookkeeping

RDR-075 closed at v4.2.2 implements the assignment *routing* (cross-collection hook fires, matches get written). The quality *metadata* (similarity, source_collection, upsert semantics) was deferred to this RDR. RDR-075 itself does not need amendment — its SCs were scoped to routing — but a close-note linking forward to RDR-077 should be added when this RDR accepts, so future readers understand the v4.2.2 baseline persists nothing about assignment confidence.
