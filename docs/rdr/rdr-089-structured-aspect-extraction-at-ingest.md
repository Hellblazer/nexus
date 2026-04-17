---
title: "RDR-089: Structured Aspect Extraction at Ingest"
id: RDR-089
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-17
accepted_date:
related_issues: []
related_tests: [test_aspect_extractor.py]
related: [RDR-042, RDR-044, RDR-078, RDR-088]
---

# RDR-089: Structured Aspect Extraction at Ingest

Nexus indexes scholarly and RDR corpora as free text plus a handful of
bibliographic fields from Semantic Scholar. The AgenticScholar paper
extracts structured per-document aspects at ingest time
(`problem_formulation`, `proposed_method`, `experimental_datasets`,
`experimental_baselines`, `experimental_results`) and uses them as
first-class queryable attributes. Without this layer, queries like
"which papers report NDCG &gt; 0.7 on dataset X?" degrade to semantic
search over free text and cannot be answered reliably. This RDR
proposes implementing that extraction pass for `knowledge__*` and
`rdr__*` collections.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Structured attributes are not indexed at ingest time

`bib_enricher.py` fetches author/venue/year/abstract from Semantic
Scholar — a handful of bibliographic fields, no content attributes.
`operator_extract` (`src/nexus/mcp/core.py:1318`) does structured
extraction at *query time*, one call per query per paper. This is
slow (N×M calls where N=papers, M=queries) and non-cumulative (each
query re-extracts rather than consulting a shared index).

#### Gap 2: Composable filters cannot operate on aspect content

RDR-088 adds `operator_filter`, `operator_groupby`, `operator_aggregate`
(GroupBy/Aggregate scoped as future work). Without indexed aspects
these operators must LLM-extract on each call — prohibitive cost for
operations over hundreds of documents. A structured attribute store
turns a 500-paper group-by from a 500-LLM-call scan into a SQL query.

#### Gap 3: `MatrixConstruct` is not supportable

Paper §5 flagship discovery pipeline: `FindNode → Traverse → MatrixConstruct → Generate`.
MatrixConstruct builds a problem×method matrix and identifies empty
cells (unexplored research directions). Requires indexed
(problem, method) tuples per paper. Without aspect extraction, this
operator cannot be implemented.

## Context

### Background

The AgenticScholar retrospective (2026-04-17) identified ingest-time
structured extraction as the single highest-value dependency for the
paper's compositional analytics operators. RDR-042 (accepted
2026-03-29) called out `ExtractTemplate` as a gap and scoped a bead
for it (`nexus-erim`) that was never picked up. This RDR restates the
gap with post-maturation design and a concrete persistence plan.

Cost model (Haiku pricing, 2026-04): one LLM call per paper, ~$0.01.
A 500-paper corpus runs in ~$5 for a full extraction pass. Incremental
extraction (new documents only) on steady-state ingest is negligible.

### Technical Environment

- `src/nexus/bib_enricher.py` — Semantic Scholar metadata enrichment.
  Precedent for ingest-time enrichment.
- `src/nexus/indexer.py` + `src/nexus/pipeline_stages.py` — current
  ingest pipeline (classify → chunk → embed → store).
- `src/nexus/db/t2/` — T2 domain stores; this RDR adds a new
  `document_aspects` store.
- `src/nexus/operators/dispatch.py` — `claude_dispatch` for LLM calls.

## Research Findings

### Investigation

Paper `knowledge__agentic-scholar`, Algorithm ExtractTemplate: the
extraction schema is explicit — 5 aspect fields, free-text per field.
Paper uses gpt-4.1; Nexus would use Haiku (lower cost, sufficient
structure quality for this task).

Scope determination: code (`code__*`) and general docs (`docs__*`)
do not benefit from the paper's aspect schema. Code has different
structured attributes (function signatures, test coverage). Docs
vary wildly. `knowledge__*` (external papers) and `rdr__*` (project
decision documents) are the natural fit — both contain
problem-statement + method + evaluation structure.

### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `claude_dispatch` | Yes | Returns schema-conformant JSON; timeout-safe; typed errors. Fit for purpose. |
| Haiku pricing | Docs Only | $0.25 / 1M input tokens, $1.25 / 1M output. ~$0.01 per paper pass assuming 5K input / 500 output tokens per call. |

### Key Discoveries

- **Verified** (`src/nexus/bib_enricher.py`): ingest-time enrichment
  pattern already exists with metadata upsert and per-document retry.
  Aspect extractor adopts the same pattern.
- **Verified** (`src/nexus/pipeline_stages.py`): indexing has a
  post-embedding stage where aspect extraction naturally fits —
  after the document is chunked but before final metadata commit.
- **Assumed**: LLM-extracted aspects are stable across re-runs
  (determinism). Needs validation via spike on 10 papers with 3 runs each.

### Critical Assumptions

- [ ] Haiku produces schema-conformant output with ≥95% success rate across `knowledge__delos` (1397 chunks, ~200 papers) — **Status**: Unverified — **Method**: Spike
- [ ] Aspect extraction adds &lt;2s per paper to ingest time — **Status**: Unverified — **Method**: Spike
- [ ] T2 write contention under concurrent indexing is acceptable (aspect upserts during ingest) — **Status**: Unverified — **Method**: Source Search + spike

## Proposed Solution

### Approach

New module `src/nexus/aspect_extractor.py` — one `claude_dispatch` per
paper with fixed schema. Hook into indexing pipeline for
`knowledge__*` and `rdr__*` collections only. New T2 store
`document_aspects.py` with append-only writes keyed by `(collection, doc_id)`.

### Technical Design

**Extraction schema**:

```text
{
  "problem_formulation": "string (1-3 sentences)",
  "proposed_method": "string (method name + 1-sentence mechanism)",
  "experimental_datasets": ["string", ...],
  "experimental_baselines": ["string", ...],
  "experimental_results": "string (key metric + value + any delta)",
  "confidence": "number in [0,1]"
}
```

`confidence` is self-reported by the extractor; low-confidence
extractions are stored but flagged for human review.

**T2 store** (`src/nexus/db/t2/document_aspects.py`):

```text
CREATE TABLE document_aspects (
  collection TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  problem_formulation TEXT,
  proposed_method TEXT,
  experimental_datasets TEXT,  -- JSON array
  experimental_baselines TEXT, -- JSON array
  experimental_results TEXT,
  confidence REAL,
  extracted_at TEXT NOT NULL,
  model_version TEXT NOT NULL,
  PRIMARY KEY (collection, doc_id)
);
```

No FTS5 index initially — queries go through `operator_filter` and
`operator_groupby` (RDR-088 Phase 1 + future). Add FTS5 if usage
demands keyword search over aspect text.

**Integration with indexer**: new stage
`AspectExtractionStage` in `src/nexus/pipeline_stages.py`, runs
after embedding for `knowledge__*` and `rdr__*` collections.
Failures logged but do not block indexing (aspect data is enrichment,
not core).

**CLI**: `nx enrich aspects <collection>` — run extraction on an
existing collection. `nx index repo .` and `nx index pdf` run
extraction inline when collection is in-scope.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `aspect_extractor.py` | `bib_enricher.py` | **Reuse pattern**: per-doc enrichment with retry; different schema and storage. |
| `document_aspects` T2 store | `memory_store`, `plan_library`, `catalog_taxonomy` | **New store**: different keyspace (per-doc, not per-project); different write rhythm. |
| Ingest pipeline hook | `pipeline_stages.py` | **Extend**: add `AspectExtractionStage` as an optional post-embedding stage. |

### Decision Rationale

Ingest-time extraction is the only approach that amortizes cost
across many queries. Per-query extraction (`operator_extract`) costs
O(N×M) LLM calls; ingest-time is O(N). Storing in T2 (not T3) because
aspects are structured metadata, not semantic content — queried by
field equality, not similarity.

Scoping to `knowledge__*` and `rdr__*` keeps the change bounded.
Expanding to `docs__*` later is cheap (same pipeline, different
collection filter).

## Alternatives Considered

### Alternative 1: Keep extraction at query time (operator_extract)

**Description**: Don't persist; extract on demand.

**Pros**: Simpler; no new T2 table; extractions always use latest model.

**Cons**: Cost scales as O(N×M); GroupBy over 500 papers = 500 LLM
calls per query; MatrixConstruct impossible.

**Reason for rejection**: The paper's flagship queries are precisely
the ones where this cost matters.

### Alternative 2: Store aspects in T3 as structured metadata

**Description**: Use ChromaDB's `metadata` field for aspect storage.

**Pros**: Single store; queried via the same ChromaDB client.

**Cons**: ChromaDB metadata has size limits (~64KB per record) and
weak support for JSON-array predicates; SQLite handles these
natively.

**Reason for rejection**: Query shape is relational (filter/groupby
over aspect fields), not similarity-based.

### Briefly Rejected

- **Extract during search**: couples unrelated concerns (retrieval and enrichment); same O(N×M) cost as operator_extract.
- **Use GPT-4.1 for parity with paper**: cost blowout (~10x Haiku); Haiku sufficient for structured extraction at this schema granularity.

## Trade-offs

### Consequences

- Ingest time increases by ~1–3s per paper in-scope (one Haiku call).
- Monthly cost: roughly $5 for a 500-paper corpus full extraction; &lt;$1 for steady-state incremental.
- New T2 table requires a migration (handled by RDR-076 migration framework).
- Aspects are stored as free text within structured fields; downstream operators still need LLM calls for fuzzy comparisons (e.g., "NDCG > 0.7" requires parsing `experimental_results` text).

### Risks and Mitigations

- **Risk**: Haiku extraction quality degrades on papers with non-standard structure (survey papers, position papers without experimental sections).
  **Mitigation**: Self-reported `confidence` field; human-review queue for low-confidence extractions; graceful degradation (null fields stored, not failure).

- **Risk**: Schema evolution (adding aspect fields later) requires re-extraction of the full corpus.
  **Mitigation**: `model_version` column; re-extraction can be incremental by version filter.

- **Risk**: Concurrent indexing creates T2 write contention on `document_aspects`.
  **Mitigation**: WAL mode (already enabled); per-collection lock at stage boundary; per-doc upsert is idempotent.

### Failure Modes

- **Visible**: extraction API failure → structured log entry + retry with backoff → if all retries fail, store null aspects + confidence=0.
- **Silent**: extraction hallucinates plausible-but-wrong fields (dataset name the paper does not actually use). Mitigated by spot-checking during spike and occasional human audit of low-confidence extractions.

## Implementation Plan

### Prerequisites

- [ ] Spike: 10-paper extraction on `knowledge__delos` with 3 runs each; measure schema conformance and field stability.
- [ ] T2 migration for `document_aspects` table.

### Minimum Viable Validation

Run aspect extraction over `knowledge__delos` (small corpus); verify
T2 writes succeed; execute a query that filters by `experimental_datasets`
and returns sensible results.

### Phase 1: Module + T2 store

#### Step 1: `src/nexus/aspect_extractor.py`

`extract_aspects(doc_text: str, doc_id: str, collection: str) → AspectRecord`.
Uses `claude_dispatch` with the schema above.

#### Step 2: `src/nexus/db/t2/document_aspects.py`

SQLite store with `upsert`, `get`, `list_by_collection`, `delete` methods.

### Phase 2: Pipeline integration

#### Step 1: `AspectExtractionStage` in `pipeline_stages.py`

Post-embedding stage; filters by collection prefix (`knowledge__` or
`rdr__`); invokes extractor; stores result.

#### Step 2: `nx enrich aspects <collection>` CLI

Batch extraction for pre-existing collections. Dry-run mode shows
count and cost estimate.

### Phase 3: Downstream consumption

#### Step 1: Wire into `operator_filter`

Add `source="aspects"` path that reads from T2 instead of LLM-on-demand.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `document_aspects` rows | `nx enrich aspects --list` | `nx enrich aspects --info <doc>` | `nx enrich aspects --delete <doc>` | schema check via `nx doctor` | T2 SQLite backup |

### New Dependencies

None (Haiku via existing `claude_dispatch`).

## Test Plan

- **Scenario**: Extraction on a paper with clear problem/method/results structure — **Verify**: all 5 fields populated with confidence ≥0.7.
- **Scenario**: Extraction on a position paper with no experimental section — **Verify**: dataset/baseline/results fields null, confidence &lt;0.5.
- **Scenario**: Re-extraction of a paper whose content hasn't changed — **Verify**: upsert updates extracted_at but leaves other fields stable (or diff-logs deviations).
- **Scenario**: `nx enrich aspects <collection> --dry-run` — **Verify**: estimates paper count and cost without making API calls.
- **Scenario**: Migration rollback — **Verify**: T2 upgrade + downgrade leaves schema clean.

## Validation

### Testing Strategy

Unit tests for extractor (mocked `claude_dispatch`) and store
(ephemeral SQLite). Integration test runs end-to-end on a 3-paper
fixture corpus. Spike validation on `knowledge__delos` establishes
confidence distribution.

### Performance Expectations

- Extraction adds ~1-3s per paper during ingest; measured during spike.
- T2 upsert &lt;1ms per paper (negligible next to the LLM call).

## Finalization Gate

_To be completed during /nx:rdr-gate._

## References

- Paper: `knowledge__agentic-scholar` Algorithm ExtractTemplate, §D.3 (MatrixConstruct consumption)
- Retrospective: `knowledge__nexus` → "AgenticScholar Retrospective 2026-04-17"
- `src/nexus/bib_enricher.py` — enrichment pattern precedent
- `src/nexus/pipeline_stages.py` — ingest pipeline integration point
- `src/nexus/db/t2/` — T2 domain store pattern
- RDR-042 — original call-out (unpicked bead nexus-erim)
- RDR-076 — T2 migration framework
- RDR-088 — operator_filter (consumer of this store)
