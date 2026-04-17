---
title: "RDR-087: Collection Observability and Curation Surfaces"
id: RDR-087
status: draft
type: Feature
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-17
related_issues: [154, 161]
related: [RDR-070, RDR-075, RDR-077]
---

# RDR-087: Collection Observability and Curation Surfaces

The substrate for cross-collection projection, topic assignment with
calibrated similarity, and threshold tuning has been landing steadily
since March 2026 (RDR-070 taxonomy, RDR-075 projection, RDR-077
threshold tuning, issues #154 and #161 closing with schema and quality
improvements). What never landed is the *user-facing and agent-facing
surfaces* that compose these primitives into comprehension and
curation.

nexus-rc45 (2026-04-17) is the exemplar of what that gap costs. A
collection returned zero results to a specific query because the
default `docs` distance threshold (0.65) filtered every candidate
from a scholarly-PDF collection whose natural cosine-distance floor
is ~0.81. The primitives to diagnose this existed — per-query raw
distances, per-collection distribution history, cross-collection
siblings with better-matching content — but no command exposed them.
A human took ~30 minutes of direct ChromaDB-API probing to identify
the cause; a user without that access would conclude the collection
was broken and move on.

This RDR names the surfaces, maps each to already-present substrate,
and treats new persistence as the exception (query telemetry is the
one addition justified). It is intentionally scoped to the reporting
layer — substrate changes (naming scheme, collection rename/merge
semantics) are deferred to RDR-088 and must follow the curation
workflow this RDR enables, not precede it.

## Problem Statement

Four symptoms, one cause. The cause is missing composition surfaces.

#### Gap 1: Silent threshold-drop failures

`nx search` returns "No results." whether the collection has zero raw
candidates or whether all candidates were filtered by threshold.
`search_engine.py:226-244` logs `dropped` count to structlog at DEBUG
level but does not surface the information to the user or to a
queryable telemetry store. The user's failure model conflates "empty"
and "filtered" with no way to distinguish.

#### Gap 2: No collection health surface

`nx collection list` shows name + chunk count. That is the complete
surface. A collection can be: stale (source mtime < index mtime by
months), orphaned (no catalog entries), near-duplicate of another
collection (>N% cross-projection overlap), dominated by generic-hub
topics (issue #161), or distance-distribution-anomalous (nexus-rc45).
None of these are queryable today. `nx doctor` covers
configuration/schema correctness but does not probe collection
retrieval quality.

#### Gap 3: No merge-candidate detection despite the data

`topic_assignments.source_collection` (populated after #154 and
RDR-075) records, for every projection assignment, which collection
was the source. A simple SQL aggregate produces a cross-collection
co-assignment matrix: pairs of collections sharing ≥N topics with
≥S similarity form merge candidates. The aggregate has never been
exposed as a command or surfaced in the catalog. The ART corpus has
`docs__ART-8c2e74c0` and `docs__art-grossberg-papers` that almost
certainly overlap heavily (both indexed from the ART repo history),
but the data to confirm is unreachable to a user without hand-rolled
SQL.

#### Gap 4: No distance-distribution visibility

Default distance thresholds are per-prefix constants in
`config.py`. They assume a roughly uniform cosine distribution per
prefix, which is empirically wrong — scholarly PDF chunks (dense
prose) have a natural floor of ~0.80+ while markdown/README chunks
routinely match at ~0.45–0.55. Users with PDF-heavy collections hit
nexus-rc45-class failures and have no command that would have
identified the mismatch. Per-collection calibrated thresholds are the
correct long-term answer; a distance-distribution reporter is the
immediate unblocker.

#### Gap 5: Silent name-resolution failures (same class, three incidents)

Three bugs filed in a single week's ART work — nexus-51j (RDR-resolver
case-sensitive glob), nexus-7ay (validate-refs proximity false
positives), nexus-rc45 (multi-hyphen corpus-name parsing) — are the
same bug shape: name/path/identifier resolution failing silently on
non-default input, returning "no results" or "no match" with no
indication that the name failed to route. Distance-distribution
probes would not have caught any of them; they never reach the
embedding layer. The observability surfaces must include a
name-resolution canary: a fixture of known collection names
(multi-hyphen, mixed-case, hash-suffixed) that `nx doctor` verifies
route correctly through every relevant surface (corpus resolver,
RDR-file glob, validate-refs proximity). An observability layer that
measures query quality but ignores identifier routing is
incomplete.

## Context

### Background

Two prior issues — both closed — landed the substrate:

- **#154** (closed 2026-04-14) "Collections are semantic islands"
  shipped cross-collection topic projection. `CatalogTaxonomy.project()`
  walks every collection, assigns chunks to the nearest topic centroid,
  and records the assignment with `source_collection` provenance.
- **#161** (closed 2026-04-15) "Store similarity, detect generic-pattern
  hubs" added `topic_assignments.similarity REAL` and
  `assigned_at TEXT` columns, making every projection auditable
  post-hoc and enabling confidence-weighted ranking.

RDR-070 (taxonomy), RDR-075 (cross-collection projection), RDR-077
(projection-threshold tuning) are the RDRs that own those systems.

### Technical Environment

- T2 SQLite at `~/.config/nexus/catalog/catalog.db`.
- `topic_assignments(doc_id, topic_id, assigned_by, source_collection,
  similarity, assigned_at)` — fully populated for recent projections.
- `topics(id, label, centroid_hash, collection, cross_doc_count, ...)`
  — topic-level metadata including hub-ness signals.
- `search_engine.search_cross_corpus()` — emits structlog at DEBUG
  for threshold drops. No durable telemetry sink.
- `nx doctor` — has `--check=schema` and related subcommands; no
  `--check=search`.
- `nx taxonomy status` / `nx taxonomy discover` — topic-level surfaces
  exist; no collection-level health view.

## Research Findings

### Critical Assumptions

- [x] **A-1 — The projection substrate is actually populated across
  the ART corpus and other user corpora.** **Status**: **VERIFIED**
  2026-04-17 (T2: `087-research-1`). 433,321 total `topic_assignments`
  rows; 205,975 (47.5%) have `source_collection`; 118 distinct source
  collections. Slightly under the 50% target, clearable with one
  backfill pass before Phase 4. Hub-domination is empirically real
  (topic 2024 "Database and Programming Research" spans 23 collections
  / 1166 chunks; topic 1494 spans 14 collections / 1131 chunks).
- [x] **A-2 — Distance-distribution histograms for per-collection
  calibration are cheaply computable.** **Status**: **VERIFIED**
  2026-04-17 (T2: `087-research-2`). ~400ms per query on ChromaDB
  Cloud; N=25 sample completes in ~10s, N=100 in ~40s. Sub-
  interactive-command budget. Secondary finding: software-engineering
  queries against scholarly-PDF collections return distances 1.6–1.8
  (effectively orthogonal) — the threshold mismatch is even worse
  than initially measured for the `docs__art-grossberg-papers` case.
- [x] **A-3 — Query telemetry can be persisted without measurable
  overhead on the search hot path.** **Status**: **VERIFIED**
  2026-04-17 (T2: `087-research-3`). 0.08ms median insert, 0.14ms
  p95 on WAL-mode SQLite; 0.02% overhead vs the ~400ms ChromaDB
  round-trip. Two orders of magnitude inside the <5% target.
  Synchronous insert is safe; no batching needed for v1.
- [x] **A-4 — Name-canary fixture routes through existing APIs.**
  **Status**: **VERIFIED** 2026-04-17 (T2: `087-research-4`).
  Canaries 1 (multi-hyphen corpus) and 3 (prefix broadcast) routed
  correctly through `resolve_corpus`; canary 2 (uppercase RDR)
  feasibility confirmed via `RdrResolver` API reachability. Fixture
  is a lightweight `tests/fixtures/name_canaries.py` module, no new
  primitives. `nexus-rc45` confirmed as the exemplar of needing
  both probes: routing is fine, threshold-filter is the actual bug.

### Investigation items

- **Verify — `topic_assignments` row population.** Run the assumption
  A-1 query on the dev machine and on a representative user DB.
  Report `total_rows`, `null_source`, `non_null_source`,
  `distinct_source_collections`, and median similarity.
- **Verify — hub-topic domination on real corpora.** Issue #161
  identified generic patterns like "Unit Testing and Assertions"
  dominating 21,307 chunks. Re-measure today to confirm the top-10
  hubs and whether any post-#161 filtering already fires.
- **Measure — distance-distribution variance per prefix.** For each
  distinct prefix (`code__`, `docs__`, `knowledge__`, `rdr__`), sample
  5 random collections and 20 random queries per collection. Report
  the p50/p90/p99 cosine distance per prefix. Expected: `docs__`
  variance is large; `code__` is tighter.
- **Verify — catalog orphan rate.** Collections present in T3 but
  absent from the catalog JSONL/SQLite. Report count on dev DB.

## Proposed Solution

### Surfaces (the deliverable)

Six surfaces. Each maps to existing substrate except where explicitly
noted.

**1. `nx search --threshold VALUE` override** (and `--no-threshold`
shortcut for `--threshold 1.0`). Bypasses the per-prefix default.
Substrate: one kwarg plumbed through `search_cross_corpus`. Zero new
state. Ships standalone as the immediate user workaround for
nexus-rc45.

**2. Silent-zero telemetry.** When `search_cross_corpus` returns
zero post-threshold results but had ≥1 raw candidate, emit one stderr
line: `note: N candidates dropped by docs threshold 0.65 (top
distance 0.81). Use --threshold 0.85 to surface.` Substrate: already
tracked in the `dropped` counter at `search_engine.py:230-249`. The
addition is stderr emission + opt-out via `--quiet`.

**3. `nx doctor --check=search`.** Two probes, not one.

*Probe 3a — name-resolution canary.* A fixture of known-shape
collection names (multi-hyphen, mixed-case, hash-suffixed,
dot-bearing, long-name) is run through every name-routing surface:
`corpus.resolve_corpus`, `doc.resolvers.RdrResolver`, catalog
`resolve_span`. Each surface returns `(matched, unmatched)`;
unmatched canaries with valid shape are routing bugs. This is the
probe that would have caught nexus-51j, nexus-7ay, and nexus-rc45
as a class, not individual one-off filings.

*Probe 3b — retrieval quality.* Every registered collection is
queried with a canned minimal query ("example test probe"); report
collections returning raw==0 (genuinely empty or corrupt),
collections returning raw>0 with all dropped (threshold mismatch),
collections where the registered `embedding_model` metadata mismatches
the resolver's expected model.

Substrate: `search_cross_corpus` + `corpus.voyage_model_for_collection`
for 3b; a new `tests/fixtures/name_canaries.py` + existing resolver
entry points for 3a.

**4. `nx collection health`.** Per-collection composite report.
Columns: `name`, `chunk_count`, `last_indexed`, `zero_hit_rate_30d`,
`median_query_distance_30d`, `cross_projection_rank`,
`orphan_catalog_rows`, `stale_source_ratio`, `hub_domination_score`.
One row per collection, sortable by any column. Substrate:
`topic_assignments` JOIN `topics` JOIN `catalog.documents`;
`zero_hit_rate` requires the new `search_telemetry` table (new
state — see below). `hub_domination_score` is the ratio of a
collection's chunks assigned to top-10 cross-collection hub topics.

**5. `nx collection audit <name>`.** Deep-dive on one collection.
Outputs: distance histogram (buckets 0.0–0.1, 0.1–0.2, ..., 1.9–2.0),
top-5 cross-projections (ranked by shared-topic count × similarity),
orphan chunks (no incoming `cites` or `implements` links after 30d),
hub-topic assignments (which of this collection's chunks are being
dragged into "Unit Testing" and similar). Substrate: SQL aggregates
over existing tables; histogram computed from the (optional) query
telemetry or from a sampled probe.

**6. `nx collection merge-candidates`.** Pair-wise cross-collection
overlap above configurable thresholds. For each pair, show: shared
topic count, mean similarity, sample overlapping chunks. Substrate:
`topic_assignments` GROUP BY `(source_collection, collection)`
with HAVING count ≥ N.

### New state (minimal)

One new T2 table:

```sql
CREATE TABLE search_telemetry (
    ts TEXT NOT NULL,              -- ISO 8601
    query_hash TEXT NOT NULL,      -- sha256(query) — don't store raw
    collection TEXT NOT NULL,
    raw_count INTEGER NOT NULL,    -- candidates before threshold
    kept_count INTEGER NOT NULL,   -- candidates after threshold
    top_distance REAL,             -- NULL if raw_count=0
    threshold REAL,                -- NULL if not applied
    PRIMARY KEY (ts, query_hash, collection)
);
CREATE INDEX idx_search_tel_collection ON search_telemetry(collection);
CREATE INDEX idx_search_tel_ts ON search_telemetry(ts);
```

TTL'd via a maintenance cron or manual `nx doctor --trim-telemetry`
(30d retention default). Query hash is sha256 so the table is safe
to ship across users without leaking query content. Config toggle
in `.nexus.yml`: `telemetry.search_enabled` (default: `true`,
disabled logs to local only). Hot-path cost: one fire-and-forget
`INSERT OR IGNORE` per `search_cross_corpus` call. A-3 measures the
actual overhead.

### What this RDR explicitly does NOT change

- Collection naming convention (`{type}__{name}[-{hash8}]`). That's
  RDR-088 territory; changing it touches every indexer, resolver,
  and search path. The observability this RDR delivers is the
  evidence base that decision needs.
- Distance-threshold values themselves. This RDR makes the mismatch
  visible and overridable; calibrated-per-collection thresholds are
  a follow-up once `search_telemetry` has 30 days of real data.
- Chunk-level migration between collections. The ART session
  hand-rolled two direct-ChromaDB scripts (`copy_papers.py`,
  `move_fixtures.py`) to move chunks between collections without
  re-embedding. This is a (c) curation primitive — merge-candidates
  surfaces the pairs that would benefit, but the migration command
  itself is deferred to a follow-up RDR paired with naming-scheme
  work. Mentioned here so the merge-candidates output is
  action-connected rather than purely informational.
- First-class bridge links between overlapping collections. The
  link-graph primitive already supports typed edges; the merge-
  candidates surface should emit an explicit `--create-link` option
  for human/agent confirmation, but the workflow for
  *automatically*-proposed bridges is deferred.
- Role tags at collection level (primary-source / working /
  archive). The ART instance substituted prose tiers in
  `primary-sources.md` for this. First-class role attribute is (c)
  substrate work; this RDR does not introduce it.
- Path-form normalization before ingestion. The ART ingestion
  hand-rolled absolute vs relative dedup. Deduplication at ingest
  is indexing-layer concern, not observability.
- Automatic collection merge or rename. Merge-candidate *detection*
  only — the action remains manual until the workflow is known.
- Agent-facing MCP surfaces. Defer until the CLI surfaces prove
  their value; then expose `collection_health()`, `collection_audit()`,
  `merge_candidates()` as MCP tools.

### Existing Infrastructure Audit

| Proposed Surface | Existing Module | Decision |
|---|---|---|
| `nx search --threshold` | `src/nexus/commands/search_cmd.py` | **Extend** — add click option, thread through |
| Silent-zero telemetry | `src/nexus/search_engine.py:226-249` | **Extend** — add stderr emit + optional DB write |
| `nx doctor --check=search` | `src/nexus/commands/doctor.py` | **Extend** — new subcheck |
| `nx collection health` | `src/nexus/commands/collection.py` | **Extend** — new subcommand |
| `nx collection audit` | `src/nexus/commands/collection.py` | **Extend** — new subcommand |
| `nx collection merge-candidates` | `src/nexus/commands/collection.py` | **Extend** — new subcommand |
| `search_telemetry` table | `src/nexus/db/migrations.py` | **Add** — new migration |

### Decision Rationale

Six surfaces, one new table, zero new primitives. Each surface is a
SQL/stdlib composition of data that already lives in T2 or is
transiently computed by `search_engine`. The new state (search
telemetry) is the minimum needed to answer "was this query dropped
by threshold?" at a later date without re-running it.

The decision to defer MCP integration is load-bearing: if the
CLI surfaces turn out to be wrong-shaped for curation, exposing them
to agents just multiplies the wrongness. One round of human use
informs shape before agent consumers land.

### Priority framing: effort vs risk

ART-instance feedback (2026-04-17) ranked the same four symptom
classes by two different criteria:

| # | Symptom                          | Effort consumed | Risk to correctness |
|---|----------------------------------|----------------:|--------------------:|
| 1 | Signal-vs-noise inside coll.     | highest         | second              |
| 2 | Duplicate / mixed collections    | second          | fourth              |
| 3 | Silent zero-results (rc45)       | third           | **highest**         |
| 4 | Prefix model / role ambiguity    | lowest          | third               |

This RDR's surfaces address these asymmetrically by design:

- `search_telemetry` + silent-zero stderr + `--threshold` override →
  symptom #3 (highest-risk: false "no results" corrupts downstream
  research passes; a deep-research-synthesizer returned "No CogEM
  material in paper corpus" on 19,417 real chunks).
- `nx collection audit` + `hub_domination_score` →
  symptom #1 (most-effort: ART spent a full day rebuilding the
  `code__ART-8c2e74c0` taxonomy and registering a 40-term glossary
  via RDR-085 because "Float Literal Syntax" 868 and "Unit Test
  Assertions" 3× dominated the domain terms).
- `nx collection merge-candidates` →
  symptom #2 (second-most-effort, second-highest structural cost:
  ART hand-migrated chunks between three overlapping collections
  with `/tmp/copy_papers.py` and `/tmp/move_fixtures.py` because
  no detection surface existed).
- Symptom #4 is deferred to RDR-088; this RDR's output is the
  evidence base the naming-substrate decision needs.

## Alternatives Considered

### Alternative 1: Ship per-collection calibrated thresholds directly

**Pros**: Fixes nexus-rc45-class failures at the root.
**Cons**: Requires a calibration methodology and enough history per
collection to be statistically valid. With no query telemetry today,
there is no history. This RDR's telemetry table is the prerequisite.

### Alternative 2: Build an interactive TUI for curation

**Pros**: Better UX for browsing cross-projection candidates.
**Cons**: 3× the implementation cost for a workflow we haven't
validated. Six CLI subcommands answer the "can we even see what we
have" question; a TUI can follow.

### Alternative 3: Push everything to the console (web UI)

**Pros**: Visual distribution histograms, clickable graphs.
**Cons**: The curation workflow is agent-driven as much as
human-driven. Agents can't click. CLI + MCP is the right shape.

## Trade-offs

### Consequences

- One new SQLite table with bounded growth (30d retention).
- One new hot-path write per search call (fire-and-forget insert).
- Six new CLI subcommand surfaces to maintain.
- Hub-domination scoring depends on topic quality staying current; if
  taxonomy drift creates low-quality hubs, the score misleads.

### Risks and Mitigations

- **Risk**: Telemetry table grows unboundedly on a heavy-search
  install. **Mitigation**: default 30d TTL via cron; documented.
- **Risk**: Collection-audit distance histogram takes minutes on a
  large collection. **Mitigation**: sample to ≤1000 chunks;
  histogram is statistical, not exhaustive.
- **Risk**: Silent-zero stderr becomes too noisy. **Mitigation**:
  fires only on (post-threshold-zero AND raw>0); `--quiet` opt-out.
- **Risk**: Merge-candidates surface proposes false positives on
  legitimately-distinct collections that happen to share hub topics
  (generic patterns). **Mitigation**: filter on similarity-weighted
  shared-topic count, exclude top-N hub topics, and expose the
  score so users judge.

### Failure Modes

- Empty `search_telemetry` on a fresh install: health report shows
  `—` in telemetry-derived columns and the doctor check runs a
  live probe instead.
- `topic_assignments.source_collection IS NULL` for legacy rows:
  merge-candidates surface reports them as "uncategorised" and
  excludes from the aggregate. Backfill is a separate concern.

## Implementation Plan

### Prerequisites

- RF A-1 verified (source_collection population rate).
- RF A-3 verified (telemetry hot-path cost).

### Minimum Viable Validation

1. Run `nx search` on a live nexus-rc45 repro before the fix and
   after: silent-zero output shows the drop count and threshold.
2. `nx doctor --check=search` identifies `docs__art-grossberg-papers`
   as "all candidates filtered at default threshold."
3. `nx collection merge-candidates` lists `docs__ART-8c2e74c0` ↔
   `docs__art-grossberg-papers` as a high-overlap pair.

### Phase 1: Unblock nexus-rc45-class failures

- `nx search --threshold` override.
- Silent-zero stderr telemetry.

### Phase 2: Telemetry persistence

- T2 migration for `search_telemetry`.
- Hot-path insert in `search_cross_corpus`.
- Opt-out config key.
- Trim cron/command.

### Phase 3: Doctor and health

- `nx doctor --check=search` — live probe across registered
  collections.
- `nx collection health` — composite per-collection report.

### Phase 4: Audit and merge-candidates

- `nx collection audit <name>` — distance histogram + cross-projection
  top-N + orphan list + hub assignments.
- `nx collection merge-candidates` — pair-wise overlap ranking.

### Phase 5: Agent surfaces (deferred until CLI shapes proven)

- Promote the four non-trivial surfaces to MCP tools.

## References

- Issue #154 — Taxonomy: cross-collection topic projection (closed
  2026-04-14, shipped the projection substrate this RDR consumes)
- Issue #161 — Taxonomy projection: store similarity, detect
  generic-pattern hubs (closed 2026-04-15, shipped the auditable
  `similarity` column and the hub signals)
- RDR-070 — Taxonomy with calibrated topic assignments
- RDR-075 — Cross-collection projection
- RDR-077 — Projection-threshold tuning
- RDR-085 — Glossary-aware labeler (shipped substrate that makes
  `hub_domination_score` meaningful — without glossary-driven labels,
  hub detection would fire on legitimate domain terms)
- nexus-rc45 — triggering incident (silent threshold-drop on
  scholarly PDF collection)
- nexus-51j — case-sensitive RDR glob (same name-resolution class,
  informs Gap 5 + Probe 3a)
- nexus-7ay — validate-refs proximity false-positives (same class)
- nexus-1uf — closed epic with latent authoring-workflow gaps;
  subset informs deferred (c) scope

## Revision History

- 2026-04-17 — Draft authored after nexus-rc45 investigation revealed
  that the substrate for collection observability exists but no
  user-facing surfaces compose it. Scope: surfaces only; naming and
  threshold-calibration changes deferred until this RDR's telemetry
  is in place.
- 2026-04-17 (rev 2) — ART-instance feedback integrated. Added Gap 5
  (silent name-resolution class — three incidents this week).
  Expanded Probe 3 into 3a (name-resolution canary) + 3b (retrieval
  quality). Priority-framing section added with effort-vs-risk
  asymmetry; surfaces mapped to specific ART session pain points.
  Explicit non-scope additions: chunk-level migration (deferred to
  follow-up RDR paired with naming work), bridge-link workflow,
  role tags, path-form normalization at ingest.
