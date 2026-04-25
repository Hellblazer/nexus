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
related: [RDR-042, RDR-044, RDR-070, RDR-076, RDR-078, RDR-088, RDR-090, RDR-093, RDR-095]
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
Scholar, a handful of bibliographic fields with no content
attributes. `operator_extract` (`src/nexus/mcp/core.py:1713`) does
structured extraction at *query time*, one call per query per
paper: slow (N×M calls where N=papers, M=queries) and
non-cumulative (each query re-extracts rather than consulting a
shared index). The 4.10.0 operator-bundling path fuses consecutive
operator steps within a single query into one dispatch, which
amortises operator overhead inside a query but leaves the per-query
extraction count unchanged. The O(N×M) asymptote across queries
remains.

#### Gap 2: Composable analytics LLM-extract on every call

The §D.4 analytics quartet has shipped: `operator_filter`
(`src/nexus/mcp/core.py:1932`, RDR-088, closed 2026-04-24),
`operator_groupby` (`core.py:2101`) and `operator_aggregate`
(`core.py:2186`) (RDR-093, closed 2026-04-24). Each call still
LLM-extracts per-paper content from raw documents on every
dispatch — the `claude -p` substrate has no shared aspect index to
read from. Without an ingest-side store, a 500-paper group-by is a
500-LLM-call scan; with one, it collapses to a SQL query plus a
single reducer call. The operators are in place; the data layer
that makes them affordable at corpus scale is not.

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

Since the original draft (2026-04-17), RDR-088 and RDR-093 have
shipped the full §D.4 analytics quartet (`filter`, `groupby`,
`aggregate`) plus `operator_check` and `operator_verify`, validating
the `claude_dispatch` substrate at production scale. The aspect
store is the remaining piece that turns those operators from
expensive scanners into amortised queries. The post-store hook
infrastructure that RDR-070 introduced (`register_post_store_hook`
in `src/nexus/mcp_infra.py:296`, with `taxonomy_assign_hook` as
the live precedent) is the natural mounting point — no new pipeline
plumbing required.

Cost model (Haiku pricing, 2026-04): one LLM call per paper, ~$0.01.
A 500-paper corpus runs in ~$5 for a full extraction pass. Incremental
extraction (new documents only) on steady-state ingest is negligible.

### Technical Environment

- `src/nexus/mcp_infra.py:296` — `register_post_store_hook` and
  `fire_post_store_hooks`. Generic post-`store_put` hook framework
  (RDR-070, nexus-7h2). Failures are caught per-hook, logged, and
  persisted to T2 `hook_failures`; ingest is never blocked. This
  is the mounting point for aspect extraction.
- `src/nexus/mcp_infra.py:365` — `taxonomy_assign_hook`, the live
  precedent: post-store, collection-prefix-scoped, idempotent
  upsert into a T2 store. Aspect extraction adopts the same shape.
- `src/nexus/bib_enricher.py` — Semantic Scholar metadata enrichment.
  Per-doc retry pattern reused; the storage hook differs (post-store
  vs. inline-during-fetch).
- `src/nexus/indexer.py`, `src/nexus/code_indexer.py`,
  `src/nexus/prose_indexer.py`, `src/nexus/pipeline_stages.py`,
  `src/nexus/doc_indexer.py`: current CLI ingest paths. Seven
  hardcoded `taxonomy_assign_batch` call sites bypass the
  `fire_post_store_hooks` chain entirely. RDR-095 fixes this at
  the framework level by adding a batch contract and migrating
  the seven callers into the chain. RDR-089 consumes that fix
  and adds nothing of its own to these files.
- `src/nexus/db/t2/` — T2 domain stores; this RDR adds a new
  `document_aspects` store via the RDR-076 migration registry
  (`src/nexus/db/migrations.py`).
- `src/nexus/mcp/core.py:1713` — `operator_extract`, the canonical
  structured-extraction operator. Aspect extraction routes through
  this rather than wrapping `claude_dispatch` directly. RDR-089
  extends it with optional `prompt_prefix` and `field_schema`
  parameters.
- `src/nexus/operators/dispatch.py` — `claude_dispatch` for LLM
  calls. Reached transitively via `operator_extract`. Schema-
  conformance verified at production scale by RDR-088 Spike A
  (95% fully-stable / 99% micro-stable / 0% schema errors) and
  reinforced by shipped RDR-093 operator usage.

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
| `operator_extract` (`mcp/core.py:1713`) | Yes | Async function decorated with `@mcp.tool()`; body builds a generic `"Extract the following fields from each item: {fields}"` prompt and returns `{"extractions": [{...}]}` via `claude_dispatch` with a per-extraction schema of `{"type": "object"}`. Two new optional kwargs (`prompt_prefix`, `field_schema`) cleanly extend the body; existing callers (`operator_filter`, `plan_run`) pass positionally and are unaffected. |
| `claude_dispatch` | Yes | Returns schema-conformant JSON; timeout-safe; typed errors. Reached transitively via `operator_extract`. |
| Haiku pricing | Docs Only | $0.25 / 1M input tokens, $1.25 / 1M output. ~$0.01 per paper pass assuming 5K input / 500 output tokens per call. |

### Key Discoveries

- **Verified** (`src/nexus/mcp_infra.py:296`): `register_post_store_hook`
  / `fire_post_store_hooks` already exists from RDR-070, fires after
  every `store_put` regardless of ingest path, catches per-hook
  failures and persists them to T2 `hook_failures`. Aspect extraction
  mounts here without any ingest-pipeline changes.
- **Verified** (`src/nexus/mcp_infra.py:365`): `taxonomy_assign_hook`
  is the live precedent — post-store, collection-prefix-scoped,
  idempotent T2 upsert. Same shape as the proposed `aspect_assign_hook`.
- **Verified** (`src/nexus/bib_enricher.py`): per-document enrichment
  with retry. Pattern reused for the extractor's API-call path; the
  storage hook differs (post-store, not inline-during-fetch).
- **Verified** (`src/nexus/db/migrations.py`, RDR-076): additive
  `Migration` entries are routine — `document_aspects` lands as one
  registry entry.
- **Assumed**: LLM-extracted aspects are stable across re-runs
  (determinism). Needs validation via spike on 10 papers with 3 runs each.

### Critical Assumptions

- [x] Haiku produces schema-conformant output with ≥95% success rate across `knowledge__delos` — **Status**: Verified by precedent. RDR-088 Spike A measured 95% fully-stable / 99% micro-stable / 0% schema-validation errors across 100 `operator_check` dispatches; RDR-093 shipped `operator_groupby` and `operator_aggregate` against the same `claude_dispatch` substrate with no schema-conformance regressions in production. Field-level shape for the aspect schema specifically is unconfirmed but the substrate reliability budget is established. **Method**: optional 10-paper spike on `knowledge__delos` if the gate reviewer wants schema-shape confirmation; not blocking.
- [ ] Aspect extraction adds &lt;2s per paper to ingest time — **Status**: Unverified. The post-store hook fires synchronously after `store_put`; one Haiku call (~1–3s) lands on the indexing critical path. **Method**: Spike — measure end-to-end ingest delta on a 10-paper batch.
- [ ] T2 write contention under concurrent indexing is acceptable (aspect upserts during ingest) — **Status**: Verified by precedent. `taxonomy_assign_hook` writes per-doc upserts to T2 `topics` from the same post-store hook chain under concurrent indexing without contention issues; WAL mode + per-store lock pattern transfers. **Method**: confirm by reading `mcp_infra.py:_post_store_hooks` chaining behavior; no separate spike needed.

## Proposed Solution

### Approach

Two pieces, gated on RDR-095:

1. **Aspect extractor as a thin orchestrator over `operator_extract`.**
   New module `src/nexus/aspect_extractor.py` exposes
   `extract_aspects(doc_text, doc_id, collection)` which routes
   through the existing `operator_extract` (`src/nexus/mcp/core.py:1713`,
   the canonical structured-extraction operator). The extractor
   builds collection-specific `prompt_prefix` and `field_schema`
   arguments based on collection prefix, calls `operator_extract`,
   parses the typed result, and writes to T2. No new
   `claude_dispatch` wrapper. `operator_extract` gains two
   optional parameters (`prompt_prefix`, `field_schema`) so future
   structured-extraction callers get the same affordance through
   one operator.

2. **One registration.** `aspect_assign_hook` registered via
   `register_post_store_hook` at MCP server startup. Self-filters
   by collection prefix to `knowledge__*` and `rdr__*`; no-op for
   everything else. New T2 store `document_aspects.py` with
   idempotent upsert keyed by `(collection, doc_id)`. Migration
   added to the RDR-076 registry (`src/nexus/db/migrations.py`).

**Dependency on RDR-095.** RDR-089 requires the post-store hook
framework to fire from every ingest path, not just MCP `store_put`.
RDR-095 (Post-Store Hook Framework: Batch Contract) lands that fix
by adding a batch-shape contract and migrating the
`taxonomy_assign_batch` hardcoded callers into the chain. Once
RDR-095 is implemented, the single registration above
automatically covers `nx index repo`, `nx index pdf`,
`nx index rdr`, and MCP `store_put`. Aspects are single-shape (one
Haiku call per document, no batch dependency optimisation
available), so RDR-089 registers via `register_post_store_hook`,
not the new batch contract.

### Technical Design

**Extraction schema** (core five, open-ended extras):

```text
{
  "problem_formulation": "string (1-3 sentences)",
  "proposed_method": "string (method name + 1-sentence mechanism)",
  "experimental_datasets": ["string", ...],
  "experimental_baselines": ["string", ...],
  "experimental_results": "string (key metric + value + any delta)",
  "extras": {                                 # optional, extractor-specific
    "<aspect_name>": <string | array | object>
  },
  "confidence": "number in [0,1]"
}
```

The five core fields track the paper's ExtractTemplate algorithm
verbatim so MatrixConstruct-class consumers get the same structure
AgenticScholar assumes. `extras` is an extensibility anchor — new
aspect fields ("ablations", "code_release", "benchmark_suite",
"datasheet_issues") land in the JSON blob without a schema
migration. When a new field promotes to "queryable enough to
warrant SQL indexing", it moves from `extras` into a fixed column
via an additive migration and the extractor prompt is updated; old
rows stay valid until re-extracted against the new `model_version`.

`confidence` is self-reported by the extractor; low-confidence
extractions are stored but flagged for human review.

**Pluggable extractor** (`src/nexus/aspect_extractor.py`):

One core entry point `extract_aspects(doc_text, doc_id, collection)`
selects a collection-scoped extractor config (prompt prefix, field
schema, fields list, `extras` key set) by collection prefix and
invokes `operator_extract` with those arguments. Initial
registrations:

- `knowledge__*` → scholarly-paper extractor (the five core fields
  plus paper-specific `extras` like `ablations`, `code_release`).
- `rdr__*` → decision-doc extractor (mapped: `problem_formulation`,
  `proposed_method` → "approach", `experimental_datasets` → null,
  `experimental_results` → "acceptance_criteria"; `extras` carries
  RDR-specific fields like `rdr_number`, `supersedes`,
  `dependencies`).

Adding a new domain (`docs__release-notes`, `knowledge__conference-
proceedings`) is one extractor config registration; no change to
`operator_extract`, the post-store hook wiring, the T2 store, or
consumer operators.

**`operator_extract` extension** (`src/nexus/mcp/core.py:1713`):

Two new keyword-only optional parameters:

```python
async def operator_extract(
    inputs: str,
    fields: str,
    timeout: float = 300.0,
    *,
    prompt_prefix: str = "",
    field_schema: dict | None = None,
) -> dict:
```

- `prompt_prefix`: prepended to the existing extraction prompt.
  Empty string preserves current behavior. Aspect extractors set
  it to a domain-context line (e.g. "This is a peer-reviewed
  scholarly paper. Identify the proposed method, datasets, and
  reported results.").
- `field_schema`: when supplied, replaces the loose
  `{"type": "object"}` per-extraction schema with the caller's
  per-field types. Aspect extractors pass a JSON Schema fragment
  encoding `experimental_datasets: array<string>`,
  `confidence: number ∈ [0,1]`, etc. When `None`, the existing
  generic schema applies.

Both parameters are additive. Existing `operator_extract` callers
(`operator_filter`, `plan_run`, ad-hoc dispatches) keep their
current shape. The change is roughly 15 lines in
`mcp/core.py:1713`.

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
  extras TEXT,                 -- JSON object; extensibility anchor
  confidence REAL,
  extracted_at TEXT NOT NULL,
  model_version TEXT NOT NULL,
  extractor_name TEXT NOT NULL,  -- which registered extractor ran
  PRIMARY KEY (collection, doc_id)
);
```

`extractor_name` is the registered key (e.g. `"scholarly-paper-v1"`,
`"rdr-decision-v1"`) — paired with `model_version`, it gives
re-extraction a precise filter (`WHERE extractor_name = ? AND
model_version < ?`) so a schema or prompt change can be rolled out
incrementally rather than all-or-nothing.

No FTS5 index initially — queries go through `operator_filter`,
`operator_groupby`, and `operator_aggregate` (RDR-088 + RDR-093,
both shipped) once the SQL fast path lands (Phase 3), or through
ad-hoc SQL against T2 in the meantime. Add FTS5 if usage demands
keyword search over aspect text.

**Integration via post-store hook**: register
`aspect_assign_hook(doc_id, collection, content)` through
`register_post_store_hook` in `src/nexus/mcp_infra.py`. The hook
short-circuits when the collection prefix is not `knowledge__` or
`rdr__`. Failures are caught by `fire_post_store_hooks`, logged via
structlog, and persisted to T2 `hook_failures` (already wired,
GH #251); ingest is never blocked.

For this to actually cover every ingest path, RDR-095 must land
first. Today the hook chain fires from one site only
(`mcp/core.py:887`, MCP `store_put`). CLI ingest paths bypass the
chain via seven hardcoded `taxonomy_assign_batch` calls in
`indexer.py`, `code_indexer.py`, `prose_indexer.py`,
`pipeline_stages.py`, and `doc_indexer.py`. RDR-095 adds a
batch-shape contract to the hook framework, migrates those
callers into the chain, and gives every CLI ingest path a fire
site. After RDR-095, a single `register_post_store_hook` call
covers `nx index repo`, `nx index pdf`, `nx index rdr`, and MCP
`store_put`.

**CLI**: `nx enrich aspects <collection>` — batch extraction on an
existing collection. New ingest paths (`nx index repo`, `nx index pdf`,
`nx index rdr`, MCP `store_put`) get extraction automatically via
the post-store hook when the collection prefix is in-scope; no
per-command flag required.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `aspect_extractor.py` | `bib_enricher.py` | **Reuse pattern**: per-doc enrichment with retry; different schema, different storage tier (T2 not bibliographic metadata). |
| Extraction call path | `operator_extract` (`mcp/core.py:1713`) | **Reuse + extend**: aspect_extractor calls `operator_extract` with new `prompt_prefix` and `field_schema` parameters. No new dispatch wrapper. The existing `claude_dispatch` substrate (which `operator_extract` already wraps) is the canonical LLM-call path; reliability budget verified by RDR-088 / RDR-093. |
| `document_aspects` T2 store | `catalog_taxonomy.py`, `memory_store.py`, `plan_library.py` | **New store**: per-(collection, doc_id) keyspace; same WAL + per-store lock pattern. |
| T2 schema migration | `src/nexus/db/migrations.py` (RDR-076 registry) | **Extend**: add one `Migration` entry — additive `CREATE TABLE`, no downgrade complexity. |
| Ingest hook | `mcp_infra.py:register_post_store_hook` (RDR-070) | **Reuse**: register `aspect_assign_hook` alongside `taxonomy_assign_hook`. RDR-095 (precondition) extends the framework with a batch contract and ensures the chain fires from every ingest path. |
| Failure persistence | `mcp_infra.py:_record_hook_failure` + T2 `hook_failures` | **Reuse**: existing per-hook failure capture; no new error infrastructure. |
| CLI ingest coverage | `indexer.py:814`, `code_indexer.py:442`, `prose_indexer.py:207`, `pipeline_stages.py:415` | **Out of scope (RDR-095 owns)**: framework-level fix. RDR-089 contributes nothing to these files; depends on RDR-095 to give the hook chain coverage. |
| Downstream consumers | `operator_filter` / `operator_groupby` / `operator_aggregate` (RDR-088 + RDR-093, both shipped) | **Already in place**: aspect store is the missing data layer; consumers exist. |

### Decision Rationale

Ingest-time extraction is the only approach that amortizes cost
across many queries. Per-query extraction (`operator_extract`,
`operator_filter`, `operator_groupby`, `operator_aggregate`) costs
O(N×M) LLM calls; ingest-time is O(N). The 4.10.0 operator-bundling
path fuses consecutive operators within a single query into one
dispatch, amortising operator overhead inside a query but not
changing the per-query extraction count. Aspects live in T2 (not
T3) because the access pattern is field equality / range / partition
— relational, not similarity-based.

The post-store hook mounting choice is structural: PDFs flow
through `pipeline_stages.py`, RDR markdown flows through
`indexer.py → batch_index_markdowns`, code goes through
`code_indexer.py`, prose through `prose_indexer.py`, and MCP tool
calls go through `store_put` directly. RDR-070 RF-070-6 designed
`fire_post_store_hooks` as the canonical per-doc post-store
mounting point so any future enrichment registers once and works
for every ingest path. RDR-095 extends that framework with a
batch contract, migrates the `taxonomy_assign_batch` hardcoded
callers into the chain, and restores the intended coverage. RDR-089
then registers cleanly against a framework that actually does what
it claims; we do not absorb the duplication and we do not make
the existing mess worse.

Scoping to `knowledge__*` and `rdr__*` keeps the change bounded
while targeting the two corpora whose query workloads (general
research and design-intent respectively) most benefit from
pre-extracted structure. Expanding to `docs__*` or additional
`knowledge__*` sub-domains later is cheap: one extractor
registration in the pluggable layer, no change to the hook, the
T2 schema, or consumer operators. The `extras` JSON column absorbs
domain-specific fields without a migration, so each new extractor
can evolve its own schema without disturbing existing rows.

Consumer-readiness: the §D.4 analytics quartet is already shipped
and stable. The aspect store is the data layer those operators
need to hit corpus scale affordably — not a forward-positioned bet
on operators-to-come. RDR-093 Alternative 2 explicitly deferred a
SQL-aspect-store fast path under the same operator names; this RDR
delivers the data layer that fast path requires (Phase 3).

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
  **Mitigation**: WAL mode (already enabled); per-store lock pattern reused from `catalog_taxonomy.py`; per-doc upsert is idempotent. Precedent from `taxonomy_assign_hook` writing per-doc to T2 `topics` under the same hook chain — no contention issues observed in production.

- **Risk**: Post-store hook latency (one Haiku call per `store_put`)
  slows down bulk ingest perceptibly.
  **Mitigation**: Hook is synchronous in `fire_post_store_hooks` but
  per-hook failures don't block ingest. If latency becomes a
  bottleneck, move aspect extraction to an async queue (the
  `pipeline_buffer` SQLite WAL pattern is the obvious vehicle —
  RDR-048). Treat as a Phase 4 follow-up, not a Phase 1 blocker;
  measure during the spike before optimising.

### Failure Modes

- **Visible**: extraction API failure → structured log entry + retry with backoff → if all retries fail, store null aspects + confidence=0.
- **Silent**: extraction hallucinates plausible-but-wrong fields (dataset name the paper does not actually use). Mitigated by spot-checking during spike and occasional human audit of low-confidence extractions.

## Implementation Plan

### Prerequisites

- [ ] **RDR-095 implemented**: post-store hook framework batch contract plus taxonomy migration. Without this, registering `aspect_assign_hook` only covers MCP `store_put` and misses the dominant CLI ingest paths.
- [ ] Spike: 10-paper extraction on `knowledge__delos`, three runs each, measuring (a) schema conformance, (b) field stability across runs, and (c) end-to-end ingest-time delta with the post-store hook firing synchronously.
- [ ] T2 migration for `document_aspects` table (one `Migration` entry in `src/nexus/db/migrations.py`).
- [ ] Confirm hook-registration order during MCP server startup: `aspect_assign_hook` should register after `taxonomy_assign_hook` so taxonomy assignment runs first (cheaper, doesn't need network).

### Minimum Viable Validation

Run aspect extraction over `knowledge__delos` (small corpus); verify
T2 writes succeed; execute a query that filters by `experimental_datasets`
and returns sensible results.

### Phase 1: Extend `operator_extract` and add the T2 store

#### Step 1: Add `prompt_prefix` and `field_schema` to `operator_extract`

Modify `src/nexus/mcp/core.py:1713` to accept the two new
keyword-only optional parameters. When `prompt_prefix` is non-
empty, prepend it to the existing prompt. When `field_schema` is
provided, use it as the per-extraction schema instead of the
default `{"type": "object"}`. Default behavior is unchanged.

Unit test: existing `operator_extract` calls return the same
shape; new calls with `field_schema` enforce per-field types and
fail validation when the LLM returns the wrong shape.

#### Step 2: `src/nexus/aspect_extractor.py`

`extract_aspects(doc_text: str, doc_id: str, collection: str) → AspectRecord`.
Selects the collection-scoped extractor config (prompt prefix,
field schema, fields list) by prefix, calls
`await operator_extract(inputs=doc_text, fields=..., prompt_prefix=...,
field_schema=...)`, parses the typed result, and returns an
`AspectRecord`. No direct `claude_dispatch` call.

#### Step 3: `src/nexus/db/t2/document_aspects.py`

SQLite store with `upsert`, `get`, `list_by_collection`, `delete` methods.

### Phase 2: Hook registration

#### Step 1: `aspect_assign_hook` in `aspect_extractor.py`

`aspect_assign_hook(doc_id: str, collection: str, content: str)`:
short-circuit when collection prefix is not `knowledge__` or
`rdr__`; dispatch to the registered extractor for the prefix;
upsert into T2 `document_aspects`. Register via
`register_post_store_hook` at MCP server startup, alongside
`taxonomy_assign_hook`. No CLI ingest code touched here because
RDR-095 already plumbed the hook chain into those paths.

#### Step 2: `nx enrich aspects <collection>` CLI

Batch extraction for pre-existing collections. Iterates documents
via the catalog (`catalog.list_by_collection`), invokes the
extractor directly (bypassing the post-store hook to avoid double
work), and writes to T2 with the same upsert path. Dry-run mode
shows paper count and cost estimate.

### Phase 3: SQL fast path for shipped operators

#### Step 1: Wire `source="aspects"` into the §D.4 quartet

Add a `source="aspects"` argument to `operator_filter`,
`operator_groupby`, and `operator_aggregate`. When set, the
operator routes the input through a SQL query against
`document_aspects` instead of the `claude -p` substrate. This is
the optimisation path RDR-093 Alternative 2 explicitly deferred
("can be added later as an optional optimisation under the same
operator name when the aspect store warrants it"). Default remains
LLM-backed; callers opt in for partition keys that map to aspect
columns. The plan runner can pick the SQL path automatically when
the planner emits a key matching a known aspect field — defer the
auto-selection until usage data shows it pays off.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `document_aspects` rows | `nx enrich aspects --list` | `nx enrich aspects --info <doc>` | `nx enrich aspects --delete <doc>` | schema check via `nx doctor` | T2 SQLite backup |

### New Dependencies

None. Aspect extraction reaches Haiku transitively through the existing `operator_extract` operator, which wraps the existing `claude_dispatch`. The `prompt_prefix` and `field_schema` extensions to `operator_extract` are additive (no new external dependencies).

## Test Plan

- **Scenario**: `operator_extract` called without `prompt_prefix` or `field_schema` — **Verify**: returns the same shape as before the extension; no regression for existing callers.
- **Scenario**: `operator_extract` called with `field_schema` enforcing `experimental_datasets: array<string>` — **Verify**: schema-conformance failures raise `OperatorOutputError` typed exceptions, not silent dict shape errors.
- **Scenario**: Extraction on a paper with clear problem/method/results structure — **Verify**: all 5 fields populated with confidence ≥0.7.
- **Scenario**: Extraction on a position paper with no experimental section — **Verify**: dataset/baseline/results fields null, confidence &lt;0.5.
- **Scenario**: Re-extraction of a paper whose content hasn't changed — **Verify**: upsert updates extracted_at but leaves other fields stable (or diff-logs deviations).
- **Scenario**: Post-store hook receives `code__myrepo` collection — **Verify**: `aspect_assign_hook` short-circuits with no `operator_extract` call, no T2 write, no log noise.
- **Scenario**: `operator_extract` raises during the hook (network error, schema-validation failure) — **Verify**: `fire_post_store_hooks` catches the exception, persists to T2 `hook_failures`, and the original `store_put` returns success.
- **Scenario**: `nx enrich aspects <collection> --dry-run` — **Verify**: estimates paper count and cost without making API calls.
- **Scenario**: Migration rollback — **Verify**: T2 upgrade + downgrade leaves schema clean.

## Validation

### Testing Strategy

Unit tests for extractor (mocked `operator_extract`) and store
(ephemeral SQLite). A separate unit test covers the
`operator_extract` extension itself: existing callers see no
behavior change; new `field_schema` callers get typed validation.
Integration test runs end-to-end on a 3-paper fixture corpus.
Spike validation on `knowledge__delos` establishes confidence
distribution.

### Performance Expectations

- Extraction adds ~1-3s per paper during ingest; measured during spike.
- T2 upsert &lt;1ms per paper (negligible next to the LLM call).

## Finalization Gate

_To be completed during /nx:rdr-gate._

## References

- Paper: `knowledge__agentic-scholar` Algorithm ExtractTemplate, §D.3 (MatrixConstruct consumption)
- Retrospective: `knowledge__nexus` → "AgenticScholar Retrospective 2026-04-17"
- `src/nexus/mcp_infra.py:296` — `register_post_store_hook` / `fire_post_store_hooks` (RDR-070, nexus-7h2): the hook framework this RDR mounts on
- `src/nexus/mcp_infra.py:365` — `taxonomy_assign_hook`: live precedent for post-store, prefix-scoped, idempotent T2 upsert
- `src/nexus/bib_enricher.py` — enrichment pattern precedent (per-doc retry)
- `src/nexus/db/migrations.py` — RDR-076 migration registry (where the `document_aspects` migration lands)
- `src/nexus/db/t2/` — T2 domain store pattern (`catalog_taxonomy` is the closest analog)
- `src/nexus/mcp/core.py:1713` — `operator_extract`, the canonical structured-extraction operator; aspect_extractor routes through this rather than wrapping `claude_dispatch` directly. Extended in this RDR with optional `prompt_prefix` and `field_schema` parameters.
- `src/nexus/operators/dispatch.py` — `claude_dispatch`, reached transitively via `operator_extract`. Schema-conformance verified by RDR-088 + RDR-093.
- RDR-042 — original ExtractTemplate call-out (unpicked bead nexus-erim)
- RDR-070 — taxonomy infrastructure that introduced post-store hooks
- RDR-076 — T2 migration framework
- RDR-088 — `operator_filter` (closed/implemented 2026-04-24; consumer of this store; source of Spike A reliability data verifying Assumption #1)
- RDR-090 — realistic AgenticScholar benchmark (consumer; cites RDR-089 as the ingest-time enrichment under measurement)
- RDR-093 — `operator_groupby` / `operator_aggregate` (closed/implemented 2026-04-24; paired analytical consumers; Alternative 2 explicitly deferred the SQL-aspect-store fast path that this RDR's Phase 3 delivers)
- RDR-095 — Post-Store Hook Framework: Batch Contract (precondition; gives the hook chain coverage of every ingest path so RDR-089's single registration actually fires for `nx index pdf` / `nx index rdr` / `nx index repo` and MCP `store_put`)
