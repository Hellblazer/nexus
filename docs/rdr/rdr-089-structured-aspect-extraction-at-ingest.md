---
title: "RDR-089: Structured Aspect Extraction at Ingest"
id: RDR-089
type: Feature
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-17
accepted_date: 2026-04-25
closed_date: 2026-04-26
close_reason: implemented
gap_closures:
  Gap1: src/nexus/db/t2/document_aspects.py:99
  Gap2: src/nexus/operators/aspect_sql.py:203
  Gap3: src/nexus/operators/aspect_sql.py:384
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
attributes. `operator_extract` (`src/nexus/mcp/core.py:1739`) does
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

Since the original draft (2026-04-17), the surrounding picture
shifted in three substantive ways:

- **RDR-088 / RDR-093 (closed 2026-04-24)** shipped the full §D.4
  analytics quartet (`filter`, `groupby`, `aggregate`) plus
  `operator_check` and `operator_verify`, validating the
  `claude_dispatch` substrate at production scale.
- **RDR-095 (closed 2026-04-24)** added a parallel batch-shape
  contract to the post-store hook framework, migrated the
  `taxonomy_assign_batch` hardcoded callers into the chain, and
  via the symmetric-fire follow-up wired both chains into every
  CLI ingest path. That work established the dispatcher pattern
  (synchronous, per-hook failure isolation, T2 `hook_failures`
  persistence) that RDR-089 mirrors for its third chain.
- **Substantive critique of an earlier RDR-089 draft** surfaced
  two structural defects: the proposed registration on the
  existing single-doc chain would have fired at *chunk* grain
  (10–50× cost blowout, semantically wrong aspects, T2 key
  collisions), and the proposed routing through async
  `operator_extract` from a sync dispatcher would have silently
  dropped the coroutine without raising. RDR-089 now owns a new
  per-document hook chain (Phase 0) at the right granularity,
  with a synchronous extractor that calls `subprocess.run`
  directly. Both defects are eliminated by construction.

The aspect store is the remaining piece that turns the analytics
operators from expensive scanners into amortised queries. The
mounting point does not exist yet — RDR-089 builds it as part of
the same RDR that ships the first consumer.

Cost model (Haiku pricing, 2026-04): one LLM call per paper, ~$0.01.
A 500-paper corpus runs in ~$5 for a full extraction pass. Incremental
extraction (new documents only) on steady-state ingest is negligible.

### Technical Environment

- `src/nexus/mcp_infra.py:296` / `:382` — `register_post_store_hook`
  / `fire_post_store_hooks` (single-doc, chunk-grain) and
  `register_post_store_batch_hook` / `fire_post_store_batch_hooks`
  (batch, chunk-batch-grain). Both synchronous; both fire at chunk
  granularity from CLI ingest paths. RDR-089 adds a third chain
  (`register_post_document_hook` / `fire_post_document_hooks`)
  to the same module that mirrors the dispatcher pattern at
  document grain.
- `src/nexus/mcp_infra.py:480` / `:607` — `taxonomy_assign_batch_hook`,
  `chash_dual_write_batch_hook`: live precedents for the
  dispatcher lifecycle (collection-prefix-scoped, idempotent T2
  upsert, per-hook failure isolation, T2 `hook_failures`
  persistence). RDR-089's `aspect_assign_hook` reuses that
  lifecycle on the new chain.
- `src/nexus/pipeline_stages.py:718` — `_catalog_pdf_hook`: the
  only existing per-document fire site. Private + PDF-specific.
  Acts as the natural anchor for `fire_post_document_hooks` in
  the PDF path.
- `src/nexus/mcp/core.py:386` — registration site for the existing
  batch chain. RDR-089 adds one line here:
  `register_post_document_hook(aspect_assign_hook)`. The block
  comment at line 365 will be updated to reflect the new chain.
- `src/nexus/bib_enricher.py` — Semantic Scholar metadata enrichment.
  Per-doc retry pattern reused for the extractor's
  `subprocess.run` path; the storage hook differs (post-document
  vs. inline-during-fetch).
- `src/nexus/indexer.py:812`, `src/nexus/code_indexer.py:437`,
  `src/nexus/prose_indexer.py:202`, `src/nexus/pipeline_stages.py:410`,
  `src/nexus/doc_indexer.py:375/500/902`: CLI ingest paths. RDR-095
  wired the existing chunk-grain chains into all of them. RDR-089
  adds one *new* call site per entry point (six total) for the
  document-grain chain — placed *after* the existing chunk fires
  so the document is fully landed before document-level enrichment
  runs.
- `src/nexus/db/t2/` — T2 domain stores; this RDR adds a new
  `document_aspects` store via the RDR-076 migration registry
  (`src/nexus/db/migrations.py`).
- `src/nexus/mcp/core.py:1739` — `operator_extract`, the canonical
  async structured-extraction operator. Signature
  `async def operator_extract(inputs: str, fields: str, timeout: float = 300.0) -> dict`.
  RDR-089's earlier draft proposed extending it with optional
  `prompt_prefix` and `field_schema` parameters; that extension
  is now dropped from this RDR. Reason: the per-document hook
  chain is synchronous (Phase 0 design constraint — see Approach
  §Async/sync contract) and routing through `async`
  `operator_extract` from a sync dispatcher would silently drop
  the coroutine. The extractor calls `subprocess.run` directly.
- `src/nexus/operators/dispatch.py` — `claude_dispatch` for async
  LLM calls. Not used by the synchronous aspect extractor; the
  RDR-088 Spike A reliability budget (95% fully-stable / 99%
  micro-stable / 0% schema errors) transfers because both paths
  end at the same `claude -p` CLI substrate.

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
| `operator_extract` (`mcp/core.py:1739`) | Yes | Async function decorated with `@mcp.tool()`; body builds a generic `"Extract the following fields from each item: {fields}"` prompt and returns `{"extractions": [{...}]}` via `claude_dispatch`. Has zero Python-level callers in `src/nexus/` (`operator_filter`, `operator_groupby`, `operator_aggregate` all call `claude_dispatch` *directly*, not `operator_extract`); MCP clients call it via the tool dispatch table by string name. RDR-089 does not modify this operator (the earlier draft's `prompt_prefix` / `field_schema` extension was dropped — see Open Questions). |
| `claude_dispatch` (`src/nexus/operators/dispatch.py`) | Yes | `async def`, launches an `asyncio` subprocess. Schema-conformant JSON, timeout-safe, typed errors. RDR-089's hook does NOT route through this — it calls `subprocess.run(["claude", "-p", ...])` synchronously. |
| `fire_post_store_hooks` / `fire_post_store_batch_hooks` (`mcp_infra.py:301` / `:382`) | Yes | Both synchronous dispatchers, zero `asyncio` in `mcp_infra.py`. Both fire at chunk granularity from CLI ingest paths (`_did` is a chunk ID, `_doc` is chunk text). Neither is suitable for document-level enrichment without dedup-by-source-path inside the consumer (rejected as fragile). RDR-089 adds a third chain at document grain. |
| Per-document fire site precedent | Yes | Only `_catalog_pdf_hook` at `pipeline_stages.py:718` fires once per logical document today, and it is private + PDF-specific. No generic per-document hook framework exists. |
| Haiku pricing | Docs Only | $0.25 / 1M input tokens, $1.25 / 1M output. ~$0.01 per paper pass assuming 5K input / 500 output tokens per call. |

### Key Discoveries

- **Verified** (`src/nexus/mcp_infra.py:296`, `:382`): both existing
  post-store hook chains (`fire_post_store_hooks`,
  `fire_post_store_batch_hooks`) are synchronous dispatchers and
  fire at *chunk* granularity from CLI ingest paths. They are
  unsuitable for document-level enrichment without dedup-by-
  source-path inside the consumer (rejected as fragile — see
  Decision Rationale). RDR-089 adds a new third chain at document
  grain; the new chain mirrors the existing dispatcher pattern.
- **Verified** (`src/nexus/mcp_infra.py:480`): `taxonomy_assign_batch_hook`
  (and `chash_dual_write_batch_hook` at line 607) are live
  precedents for the dispatcher lifecycle pattern (prefix-scoped,
  idempotent T2 upsert, per-hook failure isolation, no
  ingest-blocking on failure). RDR-089's chain reuses that
  lifecycle at a third granularity.
- **Verified** (`src/nexus/pipeline_stages.py:718`):
  `_catalog_pdf_hook` is the only existing per-document fire site.
  Private and PDF-specific. Acts as the natural anchor for
  `fire_post_document_hooks` in the PDF path, but a general
  framework chain does not exist today — RDR-089 introduces it.
- **Verified** (`src/nexus/mcp/core.py:1739` and grep across
  `src/nexus/`): `operator_extract` has zero Python-level callers
  inside the package. The `operator_filter`, `operator_groupby`,
  `operator_aggregate` operators all call `claude_dispatch`
  directly. The earlier RDR draft's claim that they "pass
  positionally" to `operator_extract` was wrong. Implication:
  dropping the `operator_extract` extension from this RDR has
  zero blast radius on existing operators.
- **Verified** (`src/nexus/bib_enricher.py`): per-document
  enrichment with retry. Retry pattern reused for the extractor's
  subprocess-call path; the storage hook differs (post-document
  vs inline-during-fetch).
- **Verified** (`src/nexus/db/migrations.py`, RDR-076): additive
  `Migration` entries are routine — `document_aspects` and the
  additive `chain` column on `hook_failures` (TEXT enum
  `'single' | 'batch' | 'document'`, replacing the existing
  `is_batch` boolean) each land as one registry entry. The
  enum-over-boolean shape was chosen on plan-audit recommendation
  (F7) because stacking a third boolean (`is_document`) alongside
  the existing `is_batch` would force every future chain
  consumer to add another boolean — `chain` scales cleanly. The
  migration backfills `chain = CASE WHEN is_batch THEN 'batch'
  ELSE 'single' END` then drops `is_batch`.
- **Assumed**: LLM-extracted aspects are stable across re-runs
  (determinism). Needs validation via spike on 10 papers with 3
  runs each.

### Critical Assumptions

- [x] Haiku produces schema-conformant output with ≥95% success rate across `knowledge__delos` — **Status**: Verified by precedent. RDR-088 Spike A measured 95% fully-stable / 99% micro-stable / 0% schema-validation errors across 100 `operator_check` dispatches; RDR-093 shipped `operator_groupby` and `operator_aggregate` against the same `claude_dispatch` substrate with no schema-conformance regressions in production. Field-level shape for the aspect schema specifically is unconfirmed but the substrate reliability budget is established. **Method**: optional 10-paper spike on `knowledge__delos` if the gate reviewer wants schema-shape confirmation; not blocking.
- [x] Aspect extraction adds &lt;3s per document to ingest time — **Status**: **Invalidated and superseded.** P1.3 spike on `knowledge__delos` (10 papers × 3 runs, `scripts/spikes/spike_rdr089_delos.py`) measured median 26.5 s and p95 38.1 s per document — 11–17× over the &lt;3 s threshold. Synchronous-inline extraction blocking the ingest path is therefore off the table. The redirect (nexus-qeo8): the document-grain hook enqueues to T2 `aspect_extraction_queue` in microseconds (single SQLite INSERT) and a daemon worker thread drains the queue invoking the same synchronous extractor. Ingest cost on the hook path is now microseconds; aspects populate within seconds-to-minutes of ingest depending on queue depth and batch size (`batch_size=5` default; `extract_aspects_batch` amortises one Haiku call across N papers). The implicit replacement assumption — "queue drain latency under non-pathological load is acceptable for read-many analytics use cases" — holds by design: no hard SLA is promised, and operators needing immediate consistency can pass `source="llm"` to bypass T2 entirely. See §Implementation Deviations row 1 and `CHANGELOG.md` `[4.14.2]`. **Method**: spike committed (`spike_rdr089_results.jsonl`, `verdict_pass: false` for the original threshold; the redirect was accepted by user direction on 2026-04-25).
- [x] T2 write contention under concurrent indexing is acceptable (aspect upserts during ingest) — **Status**: Verified by RDR-095 production deployment. `taxonomy_assign_batch_hook` and `chash_dual_write_batch_hook` write per-doc upserts to T2 from the existing batch chain under concurrent indexing without contention issues; WAL mode + per-store lock pattern transfers. The new per-document chain reuses the same `t2_ctx()` connection path. **Method**: closed by RDR-095 acceptance; no separate spike needed.

## Proposed Solution

### Approach

Three pieces. The first is a framework addition that RDR-089 owns;
the next two are the aspect feature proper as the framework's
first consumer.

1. **New `fire_post_document_hooks` chain in the post-store hook
   framework.** The existing single-doc chain
   (`fire_post_store_hooks`) and batch chain
   (`fire_post_store_batch_hooks`) both fire at chunk granularity
   from CLI ingest paths — `_did` is a chunk ID and `_doc` is
   chunk text, never the full document. That granularity is wrong
   for any enrichment that reasons about a whole document
   (aspect extraction, document-level summarisation, citation
   resolution). RDR-089 adds a third chain,
   `fire_post_document_hooks(source_path, collection, content)`,
   that fires exactly once per logical document — after every
   chunk of that document has landed in T3 and after batch hooks
   have run. The chain is synchronous like its siblings (same
   per-hook failure isolation, same persistence to T2
   `hook_failures`); hooks that need to spawn an async LLM
   subprocess use `subprocess.run` directly rather than routing
   through async wrappers (see "Async/sync contract" below). New
   call site in each indexer entry point (six sites total — see
   Phase 0).

2. **Aspect extractor as a sync subprocess caller.** New module
   `src/nexus/aspect_extractor.py` exposes
   `extract_aspects(content, source_path, collection) -> AspectRecord`,
   a synchronous function that builds a collection-scoped prompt
   and field schema, calls `subprocess.run(["claude", "-p",
   prompt, "--json"], timeout=...)` directly, parses the typed
   result, and returns the record. No async path, no
   `operator_extract` routing for the hook path. The synchronous
   shape is mandated by the synchronous hook chain; bypassing
   `operator_extract` for the in-hook path also avoids a critical
   async/sync impedance mismatch (`operator_extract` is `async
   def` and `fire_post_document_hooks` is sync).

3. **`aspect_assign_hook` registered on the new chain.** Hook
   short-circuits when collection prefix is not `knowledge__*` or
   `rdr__*` (Phase 1 limits scope to `knowledge__*`; see Open
   Questions). New T2 store `db/t2/document_aspects.py` with
   idempotent upsert keyed by `(collection, source_path)`.
   Migration added to the RDR-076 registry
   (`src/nexus/db/migrations.py`). One registration in
   `src/nexus/mcp/core.py` near the existing batch-chain
   registrations.

**Async/sync contract** (locked at the framework level so
downstream consumers do not have to redecide it):

- `fire_post_document_hooks` is synchronous, identical signature
  shape to `fire_post_store_hooks`.
- Hooks registered via `register_post_document_hook(fn)` MUST be
  synchronous callables. Async hooks are explicitly unsupported.
- Hooks that need to invoke LLM-backed work shell out via
  `subprocess.run(["claude", "-p", ...], timeout=...)` directly.
  This blocks the calling thread for the duration of the call.
  All call sites — including MCP `store_put` — invoke the chain
  with a plain synchronous call: `fire_post_document_hooks(
  source_path, collection, content)`. `store_put` is itself
  synchronous (`def`, not `async def`); FastMCP wraps sync
  `@mcp.tool()` bodies in a thread pool at the framework level,
  so calling `subprocess.run` from inside the hook chain blocks
  the worker thread, not the asyncio event loop. No `to_thread`
  wrapping is needed (and would be invalid: a sync function body
  cannot use `await`).
- The existing `operator_extract` keeps its `async def` shape and
  is unchanged by RDR-089 (the previously-proposed
  `prompt_prefix` / `field_schema` extension is dropped from this
  RDR — it can come back when an async caller actually needs it).

**Async/sync as the resolution to a real defect.** This contract
exists because the original RDR-089 draft proposed registering
on the existing `register_post_store_hook` chain and routing
through `operator_extract`. Substantive critique surfaced that
`fire_post_store_hooks` is synchronous (zero `asyncio` in
`mcp_infra.py`) and `operator_extract` / `claude_dispatch` are
both `async def`. A sync dispatcher invoking an async hook
silently drops the coroutine — extraction never runs, no
exception is raised, `hook_failures` stays empty. The contract
above eliminates that failure mode by construction: synchronous
all the way down on the hook chain.

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

`confidence` is self-reported by the extractor and stored as
informational metadata only. It is **not** a quality gate. A
model that confidently hallucinates a dataset name reports high
confidence in that hallucination, so the field cannot detect the
failure mode it would seem to detect. Independent validation —
the only signal that actually catches hallucinated extractions
— is a sampled `operator_check` second-pass over a small fraction
of newly-extracted papers (Phase 2, see "Sampled validation
pass" below). The `confidence` column is retained for
distributional inspection (do extractions on survey papers have
visibly different self-reported confidence than on experimental
papers?), not for routing decisions.

**Pluggable extractor** (`src/nexus/aspect_extractor.py`):

One core entry point
`extract_aspects(content: str, source_path: str, collection: str) -> AspectRecord`,
synchronous. Selects a collection-scoped extractor config
(prompt prefix, field schema, fields list, `extras` key set) by
collection prefix and invokes `subprocess.run(["claude", "-p",
prompt, "--json"], timeout=180, capture_output=True, text=True)`
directly. Parses the JSON response, validates against the
configured field schema, returns the typed `AspectRecord`.
Phase 1 ships exactly one extractor:

- `knowledge__*` → scholarly-paper extractor (the five core fields
  plus paper-specific `extras` like `ablations`, `code_release`).

The `rdr__*` extractor is deferred to a follow-up RDR (see Open
Questions): RDR documents already carry structured frontmatter
and labelled sections that a markdown parser handles more
reliably than a forced 5-field schema, and no downstream
analytics use case requires RDR aspects today.

Adding a new domain later (`docs__release-notes`,
`knowledge__conference-proceedings`) is one extractor config
registration; no change to the post-store hook wiring, the T2
store, or consumer operators.

**T2 store** (`src/nexus/db/t2/document_aspects.py`):

```text
CREATE TABLE document_aspects (
  collection TEXT NOT NULL,
  source_path TEXT NOT NULL,
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
  PRIMARY KEY (collection, source_path)
);
```

The key is `(collection, source_path)`, not `(collection, doc_id)`.
`source_path` is the document-level identifier carried in chunk
metadata across every indexer (`prose_indexer.py`,
`code_indexer.py`, `pipeline_stages.py`, `doc_indexer.py`); the
new `fire_post_document_hooks` chain passes it as the canonical
per-document key. Per-chunk `doc_id` is intentionally not in the
schema — aspects are document-level by construction.

`extractor_name` is the registered key (e.g.
`"scholarly-paper-v1"`) — paired with `model_version`, it gives
re-extraction a precise filter (`WHERE extractor_name = ? AND
model_version < ?`) so a schema or prompt change can be rolled
out incrementally rather than all-or-nothing.

**Upsert semantics**: complete idempotent overwrite. Re-extraction
overwrites every column for `(collection, source_path)`; there is
no diff-and-merge, no per-field stability check, no deviation
log. If a future requirement needs change tracking, a separate
`document_aspects_history` table with INSERT-only writes is the
pattern, not in-place merging. (This RDR's earlier draft
described both behaviours and was self-contradictory; this
sentence resolves it.)

No FTS5 index initially — queries go through ad-hoc SQL against
T2, or through `operator_filter` / `operator_groupby` /
`operator_aggregate` (RDR-088 + RDR-093, both shipped) once the
SQL fast path lands (deferred per Open Questions). Add FTS5 if
usage demands keyword search over aspect text.

**Integration via the new per-document hook chain**: register
`aspect_assign_hook(source_path, collection, content)` through
the new `register_post_document_hook` in `src/nexus/mcp_infra.py`
(added by Phase 0). The hook short-circuits when the collection
prefix is not `knowledge__`. Failures are caught by
`fire_post_document_hooks`, logged via structlog, and persisted
to T2 `hook_failures` (reusing the existing capture path from
GH #251); ingest is never blocked. (See Phase 0 Step 2 for the
authoritative wire-site list and counts; the bullet list here
is descriptive prose only.)

**Content-sourcing contract** (load-bearing — pinned by Phase 0
plan audit): the dispatcher signature is
`fire_post_document_hooks(source_path: str, collection: str,
content: str)`, but the available scope at call sites differs:

- **MCP `store_put`** has the full document text in scope and
  passes it through literally as `content`.
- **CLI ingest sites** (the chunk-grain indexers) only have
  chunk text in scope at the point where the per-document hook
  fires; the full document is not accumulated. These sites pass
  `content=""` as the contract signal that "this hook may need
  to read `source_path` itself."
- **Hooks** treat `content` as primary; if `content == ""` and
  the hook needs full document text, it reads the file at
  `source_path` (which is still on disk at hook time — every
  CLI ingest path indexes from existing files). For PDF paths
  where `source_path` points at a `.pdf` whose extracted text
  is what aspects should be extracted from, the hook re-runs
  the extractor's text-extraction step (cheap relative to the
  Haiku call); the alternative — having every indexer
  accumulate full text in memory — was rejected as memory
  pressure at corpus scale.

This contract is asserted by P0.3 unit tests (the
`test_post_document_hooks.py` dispatcher test exercises both
the pass-through and file-read fallback paths) and pinned in
the Phase 0 dispatcher acceptance criteria.

One `register_post_document_hook(aspect_assign_hook)` call covers
all of `nx index repo`, `nx index pdf`, `nx index rdr`, and MCP
`store_put`. Per-chunk firings of the existing
`fire_post_store_hooks` chain are unaffected (the existing chain
remains empty by default, but stays available for any future
genuinely per-chunk consumer).

**CLI**: `nx enrich aspects <collection>` — batch extraction on
an existing collection. Iterates documents via the catalog
(`catalog.list_by_collection`, returning one entry per source
document, not per chunk), calls `extract_aspects` directly
(bypassing the hook), upserts to T2. Dry-run mode reports
document count and cost estimate. This path also covers any
documents indexed before RDR-089 ships.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `fire_post_document_hooks` chain | `fire_post_store_hooks` (single-doc, chunk-grain) and `fire_post_store_batch_hooks` (batch, chunk-batch-grain) at `mcp_infra.py:301` / `:382` | **New chain**: identical lifecycle pattern (synchronous dispatcher, per-hook failure isolation, T2 `hook_failures` persistence) but new granularity — fires once per logical document. The existing two chains are chunk-shaped at every CLI call site (`_did` is a chunk ID, `_doc` is chunk text); document-level enrichment cannot be implemented correctly on either. RDR-089 owns the new chain definition AND its consumer. |
| Per-document fire sites | `_catalog_pdf_hook` at `pipeline_stages.py:718` is the only existing per-document hook; it is private and not a generic mounting point | **New call sites**: six sites (`indexer.py`, `code_indexer.py`, `prose_indexer.py`, `pipeline_stages.py`, `doc_indexer.py`, `mcp/core.py:store_put`). Placed after the chunk loop so document is fully landed before enrichment runs. |
| `aspect_extractor.py` | `bib_enricher.py` | **Reuse pattern**: per-doc enrichment with retry; different schema, different storage tier (T2 not bibliographic metadata). |
| LLM call path | `subprocess.run(["claude", "-p", ...])` directly | **Bypass `operator_extract`**: `operator_extract` is `async def` and the new hook chain is synchronous; routing through it would require an async-bridge that does not exist in `mcp_infra.py`. The aspect extractor calls `subprocess.run` directly and is synchronous all the way down. The `operator_extract` extension previously proposed in this RDR (`prompt_prefix` / `field_schema`) is dropped — it can return when an async caller actually needs it. |
| Schema-conformance reliability budget | RDR-088 Spike A (95% fully-stable / 99% micro-stable / 0% schema errors over `claude_dispatch`) | **Transfers**: same Haiku model, same JSON-schema enforcement at the prompt level. The substrate is `subprocess.run` not `claude_dispatch`, but both end at the same `claude -p` CLI. |
| `document_aspects` T2 store | `catalog_taxonomy.py`, `memory_store.py`, `plan_library.py` | **New store**: per-(collection, source_path) keyspace; same WAL + per-store lock pattern. |
| T2 schema migration | `src/nexus/db/migrations.py` (RDR-076 registry) | **Extend**: add one `Migration` entry — additive `CREATE TABLE`, no downgrade complexity. |
| Hook lifecycle precedent | `taxonomy_assign_batch_hook` (`mcp_infra.py:480`), `chash_dual_write_batch_hook` (`mcp_infra.py:607`) | **Reuse pattern**: prefix-scoped, idempotent T2 upsert, per-hook failure isolation. Different chain shape and granularity but same `t2_ctx()` connection path and same failure persistence. |
| Failure persistence | `mcp_infra.py:_record_hook_failure` + T2 `hook_failures` | **Reuse**: extend the existing per-hook failure capture to the new chain's dispatcher; no new error infrastructure. |
| Drift guard | `tests/test_hook_drift_guard.py` (covers batch-chain hook names) | **Extend in two waves**: Phase 0 ships `test_every_cli_ingest_site_fires_document_hook` (call-site presence + AST walk) plus a unit test for the new chain dispatcher itself in `tests/test_post_document_hooks.py`. Phase 2 ships `DOCUMENT_HOOK_GUARDED_NAMES = frozenset({"aspect_assign_hook"})` alongside the symbol definition (shipping it earlier passes vacuously). The original RDR-070 hook-bypass debt accrued precisely because no enforcement test existed; same mistake not repeated. |
| Downstream consumers | `operator_filter` / `operator_groupby` / `operator_aggregate` (RDR-088 + RDR-093, both shipped) | **Already in place**: aspect store is the missing data layer; the operators currently call `claude_dispatch` directly per dispatch — see Open Questions for the SQL fast path. |

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
calls go through `store_put` directly. RDR-070 designed
`fire_post_store_hooks` and RDR-095 added a parallel batch
chain, but both chains fire at *chunk* granularity from CLI
ingest paths — `_did` is a chunk ID, `_doc` is chunk text. That
granularity is correct for chash dual-write and topic
assignment (per-chunk needs) but wrong for any enrichment that
reasons about a whole document. Aspect extraction is the
canonical document-level case: a `proposed_method` extracted
from a single chunk of a 50-page paper is meaningless; the cost
model assumed in this RDR (~$5 per 500-paper corpus) only holds
if the LLM call fires per document, not per chunk.

The clean fix is a third chain at the right granularity, owned
by this RDR alongside its first consumer. The existing two
chains stay as-is (chunk-shaped consumers like chash and
taxonomy already depend on that shape); the new chain fires
once per logical document at six new call sites placed *after*
the chunk loop completes. Adding the chain costs ~80 lines of
framework code (mirror the existing dispatcher + registration
helpers) and one new drift-guard test. The alternative —
dedup-by-source-path inside `aspect_assign_hook` against the
existing chunk-shaped chain — was rejected as fragile (requires
either T3 round-trip per chunk to recover `source_path` from
`doc_id`, or string-suffix parsing of chunk IDs).

Scoping Phase 1 to `knowledge__*` (dropping `rdr__*` from the
original draft) keeps the surface bounded while targeting the
corpus whose query workload most benefits from pre-extracted
structure. RDR documents already carry structured frontmatter
and labelled sections that a markdown parser handles more
reliably than a forced 5-field schema; that work is a separate
concern. Expanding to additional `knowledge__*` sub-domains
later is cheap: one extractor config registration, no change to
the hook, the T2 schema, or consumer operators. The `extras`
JSON column absorbs domain-specific fields without a migration.

Consumer-readiness: the §D.4 analytics quartet is already shipped
and stable. The aspect store is the data layer those operators
need to hit corpus scale affordably — not a forward-positioned
bet on operators-to-come. The SQL-fast-path wiring (RDR-093
Alternative 2's deferred work) requires planner-side
auto-selection logic that is not designed yet and is split into
its own follow-up rather than landed half-done in this RDR's
Phase 3 (see Open Questions).

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

- ~~Ingest time increases by ~1–3s per paper in-scope (one Haiku call).~~ **Updated by Implementation Deviations**: ingest cost on the hook path is microseconds (single SQLite INSERT into `aspect_extraction_queue`); aspects populate within seconds-to-minutes of ingest depending on queue depth and `batch_size` (default 5). The original synchronous-inline cost was invalidated by the P1.3 spike (median 26.5s).
- Monthly cost: roughly $5 for a 500-paper corpus full extraction; &lt;$1 for steady-state incremental. The Phase D batch path (`extract_aspects_batch`, one Haiku call per N papers) reduces drain wall-time on a 1000-paper corpus from ~7 hours single-paper to ~40–80 minutes batched.
- New T2 tables require migrations (`document_aspects`, `aspect_extraction_queue`, `aspect_promotion_log`, `hook_failures.chain` enum column — handled by RDR-076 migration framework, all four registered as 4.14.2 migrations).
- Aspects are stored as free text within structured fields; downstream operators still need LLM calls for fuzzy comparisons (e.g., "NDCG > 0.7" requires parsing `experimental_results` text). Phase B SQL fast path covers structured equality / token-membership / aggregate queries against fixed columns; non-trivial range / fuzzy queries still flow through the LLM substrate via `source="auto"` fallback.

### Risks and Mitigations

- **Risk**: Haiku extraction quality degrades on papers with non-standard structure (survey papers, position papers without experimental sections), or hallucinates plausible-but-wrong values (a dataset name the paper does not actually use).
  **Mitigation**: Sampled `operator_check` second-pass over a configurable fraction (default 5%) of newly-extracted papers during `nx enrich aspects` runs, comparing extracted aspects against the source text via an independent LLM call. Self-reported `confidence` is stored but not used as a quality gate (the same model that hallucinates also reports high confidence in the hallucination — circular). Graceful degradation for papers that lack the structure: null fields stored, not failure.

- **Risk**: Schema evolution (adding aspect fields later) requires re-extraction of the full corpus.
  **Mitigation**: `model_version` column; re-extraction can be incremental by version filter.

- **Risk**: Concurrent indexing creates T2 write contention on `document_aspects`.
  **Mitigation**: WAL mode (already enabled); per-store lock pattern reused from `catalog_taxonomy.py`; per-doc upsert is idempotent. Precedent from `taxonomy_assign_batch_hook` writing per-doc to T2 `topics` under the parallel batch chain — no contention issues observed in production after RDR-095 acceptance.

- **Risk**: Per-document hook latency (one Haiku call per document)
  slows down bulk ingest perceptibly.
  **Mitigation**: The new chain fires once per document (not per
  chunk), so a 50-page paper costs one call, not 50. Per-hook
  failures don't block ingest. If latency still becomes a
  bottleneck on multi-thousand-paper bulk runs, move aspect
  extraction to an async queue (the `pipeline_buffer` SQLite WAL
  pattern is the obvious vehicle — RDR-048). Treat as a Phase 4
  follow-up, not a Phase 1 blocker; measure during the spike
  before optimising.

- **Risk**: A future contributor adds a new indexer path or a new
  ingest entry point and forgets to call `fire_post_document_hooks`,
  silently dropping aspect extraction for that path. This is the
  exact failure mode that bit RDR-070 (seven hardcoded
  `taxonomy_assign_batch` callers bypassing the hook chain).
  **Mitigation**: extend `tests/test_hook_drift_guard.py` with a
  `test_every_cli_ingest_site_fires_document_hook` test that
  enumerates the expected call sites and pins them by AST walk.
  Ships in Phase 0 alongside the chain itself, not as a follow-up.

- **Risk**: Burst MCP `store_put` calls (an agent loop calling
  the tool 100× rapidly on `knowledge__*` collections) queue up
  many concurrent `claude -p` subprocesses through
  `asyncio.to_thread`. Python's default thread-pool executor is
  unbounded and `subprocess.run` blocks per-thread, so a burst
  could spawn dozens of concurrent extractor subprocesses,
  saturating CPU and the Claude API rate limit.
  **Mitigation**: same Phase 4 escape hatch as the latency risk
  — move extraction to an async work queue if measurement during
  the spike or production usage shows it matters. For Phase 1,
  document the expected usage pattern as "sequential per-doc MCP
  ingest, bulk via `nx enrich aspects`" and treat burst MCP
  ingest as a non-target case.

### Failure Modes

- **Visible**: subprocess call fails or times out → structured log entry + retry with backoff → if all retries fail, store null aspects + extracted_at + extractor_name + model_version, confidence=null. Row exists, downstream operators short-circuit on null fields.
- **Silent**: extraction hallucinates plausible-but-wrong fields (dataset name the paper does not actually use). Mitigated by the sampled `operator_check` validation pass during `nx enrich aspects` (default 5% sample); the self-reported `confidence` field is **not** the detection mechanism (see Risks). Hallucinations on hook-path extractions land in T2 unflagged until the next batch enrichment pass surfaces them.

## Implementation Plan

> **Note**: Symbol names (`aspect_assign_hook`), fire-site counts, and the synchronous-inline extraction path described in this section reflect the spec at acceptance time. The P1.3 spike invalidated the latency assumption and drove a redirect; the shipped artefact differs in several places. The frozen text below is preserved as the historical record. See **§Implementation Deviations** for what shipped: in particular, `aspect_assign_hook` → `aspect_extraction_enqueue_hook`, the addition of T2 `aspect_extraction_queue` plus an async worker, and the operator rename for `--validate-sample` (`operator_check` → `operator_verify`).

### Prerequisites

- [x] **RDR-095 closed (2026-04-24)**: post-store hook framework batch contract plus taxonomy migration plus the symmetric-fire follow-up that wires both existing chains into every CLI ingest path. RDR-089 builds on the same dispatcher pattern but at a new granularity (per-document, not per-chunk).
- [ ] Spike: 10-paper extraction on `knowledge__delos`, three runs each, measuring (a) schema conformance, (b) field stability across runs, (c) end-to-end ingest-time delta with the per-document hook firing synchronously, and (d) per-document vs per-chunk fire counts (proving the new chain fires exactly once per document at every CLI site).
- [ ] T2 migration for `document_aspects` table (one `Migration` entry in `src/nexus/db/migrations.py`).

### Minimum Viable Validation

Run aspect extraction over `knowledge__delos` (small corpus); verify
T2 writes succeed exactly once per source document (not per chunk);
execute a query that filters by `experimental_datasets` and returns
sensible results.

### Phase 0: Per-document hook chain (framework foundation)

This phase is the framework prerequisite for the rest of the RDR.
It ships independently of the aspect feature so the chain can be
exercised by tests before any aspect-specific code lands.

#### Step 1: Add the chain to `mcp_infra.py`

Add `register_post_document_hook(fn)`, the `_post_document_hooks`
list, `fire_post_document_hooks(source_path, collection, content)`,
and `_record_document_hook_failure` to `src/nexus/mcp_infra.py`.
Mirror the existing `_post_store_hooks` and
`fire_post_store_hooks` shape verbatim — synchronous dispatcher,
per-hook try/except, structlog warning, T2 `hook_failures`
persistence tagged `chain='document'` via the new TEXT enum
column (additive migration that also backfills the existing
`is_batch` boolean into the same enum and then drops
`is_batch`). Roughly 80 lines.

#### Step 2: Wire the per-document fire sites

Add `fire_post_document_hooks(source_path, collection, content)`
calls at the *document-boundary* sites listed below. Placement
matters: the existing chunk-grain chains fire from inside chunk
loops (and, for the PDF incremental path, from inside a
300-chunk pagination loop). The per-document chain must fire
*after* those loops complete so each call corresponds to one
finished source document.

- `src/nexus/doc_indexer.py:_index_document` (post-line 382) —
  per-file boundary for single-file markdown / prose ingest. One
  fire after the existing per-chunk loop completes and the upsert
  is done. **`batch_index_markdowns` (line ~1119) is intentionally
  not wired separately** — it is a loop over `_index_document`
  calls and gets coverage transitively. Adding a fire there too
  would double-fire every RDR document.
- `src/nexus/doc_indexer.py:index_pdf` — **two distinct fire
  sites, one per branch tail** (no shared post-branch-join
  landing point exists in the function): one after
  `_index_pdf_incremental` returns (around line 877, after
  `_register_in_catalog`) for large PDFs, and a second after the
  small-document upsert path (around line 909) completes for
  small PDFs. **Do not** add the call inside
  `_index_pdf_incremental` itself — that function's batch loop
  spans lines 470-547 and the document boundary is at the loop
  exit (line 547), not inside the loop body. Adding the fire
  inside the loop would trigger N/300 times per large PDF
  instead of once. (If `index_pdf` is later refactored to a
  shared `try/finally` exit, the two fires can collapse to one;
  for Phase 0 the two-site shape is mandatory.)
- `src/nexus/code_indexer.py` — once per file in `index_file`,
  after the chunk loop.
- `src/nexus/prose_indexer.py` — once per file, same shape as
  `code_indexer`.
- `src/nexus/pipeline_stages.py` — once per PDF, immediately
  after `_catalog_pdf_hook` (line 718). `_catalog_pdf_hook` is
  the existing per-document anchor in this path; the new fire
  rides alongside it.
- `src/nexus/mcp/core.py:store_put` (line ~910) — plain
  synchronous call: `fire_post_document_hooks(source_path,
  collection, content)`, placed after the existing batch fire.
  `content` is already the full document text here. **No
  `await`, no `asyncio.to_thread`** — `store_put` is `def` not
  `async def`; FastMCP wraps sync tool bodies in a worker thread
  at the framework level, so the hook's `subprocess.run` blocks
  the thread, not the event loop.

Total: **seven fire sites in five modules** (`doc_indexer.py` has
three sites: `_index_document` + two in `index_pdf`;
`code_indexer.py`, `prose_indexer.py`, `pipeline_stages.py`,
`mcp/core.py:store_put` contribute one each). The drift-guard
test below pins each site by AST walk so future contributors
cannot accidentally regress to chunk-level firing or
re-introduce the dropped `batch_index_markdowns` site.

#### Step 3: Drift-guard test + chain unit test

Extend `tests/test_hook_drift_guard.py` with two pieces, only the
first of which ships in Phase 0:

- **Phase 0**: `test_every_cli_ingest_site_fires_document_hook` —
  AST-walks each of the six modules and asserts
  `fire_post_document_hooks` is called from the expected function
  (matching the placements named in Step 2 above, including the
  caller-not-callee placement for `index_pdf`). Mirrors the
  existing `test_every_cli_ingest_site_fires_both_chains`. This
  test is meaningful immediately because it pins call-site
  presence regardless of whether any consumer is registered.

Also in Phase 0, add a unit test in `tests/test_post_document_hooks.py`
exercising the chain dispatcher itself: register a synchronous
test hook, call `fire_post_document_hooks(source_path, collection,
content)`, assert the hook was invoked with the correct arguments,
assert exceptions raised by the hook are caught and persisted to
T2 `hook_failures` with `chain='document'`, assert ingest is never
blocked. This validates the framework before Phase 2 registers
a real consumer.

- **Phase 2 (deferred)**: a `DOCUMENT_HOOK_GUARDED_NAMES =
  frozenset({"aspect_assign_hook"})` drift guard mirroring the
  batch-chain `GUARDED_NAMES` pattern. Lands alongside the
  `aspect_assign_hook` symbol definition in Phase 2; shipping it
  earlier would pass vacuously (the AST scanner finds zero
  matches for a non-existent name) and provide no value.

### Phase 1: T2 store + extractor

#### Step 1: `src/nexus/db/t2/document_aspects.py`

SQLite store with `upsert`, `get`, `list_by_collection`, `delete`,
`list_by_extractor_version` methods. Schema as specified in
Technical Design above. Migration in `db/migrations.py` (one
additive `Migration` entry).

#### Step 2: `src/nexus/aspect_extractor.py`

`extract_aspects(content: str, source_path: str, collection: str) -> AspectRecord`,
synchronous. Selects the collection-scoped extractor config by
prefix (Phase 1 ships only `knowledge__*` → scholarly-paper
extractor), builds the prompt + field-schema instruction string,
calls `subprocess.run(["claude", "-p", prompt, "--json"],
timeout=180, capture_output=True, text=True)`, parses and
validates the JSON response, returns the typed `AspectRecord`.
On subprocess failure: retry with backoff (reuse the
`bib_enricher.py` retry pattern); on final failure return a
record with all aspect fields null and confidence=null.

### Phase 2: Hook registration + CLI

#### Step 1: `aspect_assign_hook` in `aspect_extractor.py`

`aspect_assign_hook(source_path: str, collection: str, content: str)`:
short-circuit when collection prefix is not `knowledge__`;
otherwise call `extract_aspects(content, source_path, collection)`
and upsert the result into T2 `document_aspects`. Register via
`register_post_document_hook(aspect_assign_hook)` at MCP server
startup in `src/nexus/mcp/core.py` near line 386, alongside the
existing batch-chain registrations. Add `aspect_assign_hook` to
the Phase 0 drift-guard frozenset.

No CLI ingest code touched in Phase 2 — Phase 0 already wired
the new chain into every ingest path.

#### Step 2: `nx enrich aspects <collection>` CLI

Batch extraction for pre-existing collections. Iterates documents
via the catalog (`catalog.list_by_collection`, returning one
entry per source document not per chunk), invokes
`extract_aspects` directly (bypassing the hook to avoid double
work on docs that already triggered hook-time extraction),
upserts to T2. Flags:

- `--dry-run` — paper count + cost estimate, no API calls.
- `--validate-sample N` — after extraction, run `operator_check`
  on N% of newly-extracted papers (default 5) using the call shape
  `operator_check(claim=json.dumps(extracted_aspects),
  evidence=raw_document_text, timeout=60)` — i.e. the extracted
  aspect record is the claim under test and the source document
  is the evidence. Disagreements (operator returns `verified=false`
  with rationale) emit any
  papers where the check disagrees with the extracted aspects to
  a `validation_failures.jsonl` file in the corpus directory.
- `--re-extract --extractor-version <v>` — re-run on rows whose
  `model_version < v`, supporting incremental schema rollouts.

### Open Questions (deferred to follow-up RDRs)

The following were in scope of earlier drafts but are explicitly
parked here to keep RDR-089 shippable:

- **`rdr__*` extractor**. The original draft mapped the scholarly
  schema onto RDRs (`experimental_results → "acceptance_criteria"`,
  etc.). RDRs already carry structured frontmatter and labelled
  sections that a markdown parser handles more reliably than a
  forced 5-field LLM extraction. A separate RDR should design an
  RDR-native aspect schema if a downstream consumer actually needs
  it; nothing today does.
- **SQL fast path for shipped operators**. Adding
  `source="aspects"` to `operator_filter` / `operator_groupby` /
  `operator_aggregate` requires planner-side auto-selection
  (otherwise no caller learns the path exists and it stays dead
  code). The auto-selection design — which aspect field names
  trigger the SQL path, how the planner discovers the available
  schema, how to fall back when the extracted value is null — is
  not designed yet and warrants its own RDR rather than landing
  half-done here.
- **Batch Haiku per call**. The cost model assumes one Haiku call
  per document. A single `claude -p` call can in principle
  process N papers in one prompt and return N aspect records; if
  the per-call latency dominates at corpus scale (1000+ papers),
  batching could meaningfully change the wall-clock cost. Out of
  scope for Phase 1; revisit if `nx enrich aspects` runtime
  becomes a complaint vector.
- **`extras` → fixed-column promotion mechanic**. When a field
  graduates from `extras` to its own column, the rollout is
  `nx enrich aspects --re-extract --extractor-version <v>` over
  the affected collection. The CLI flag is wired in Phase 2 but
  the workflow (which fields warrant promotion, who decides,
  what marks the version bump) is policy not mechanism and is
  parked for a later operations RDR.
- **Async aspect operators / `operator_extract` extension**. The
  earlier draft proposed extending `operator_extract` with
  `prompt_prefix` and `field_schema` parameters so async callers
  could share the aspect prompt-building logic. Dropped from
  Phase 1 scope (the synchronous hook does not use it). Revisit
  if an async caller actually needs the affordance.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `document_aspects` rows | `nx enrich list <collection>` | `nx enrich info <collection> <source_path>` | `nx enrich delete <collection> <source_path>` | schema check via `nx doctor` | T2 SQLite backup |

### New Dependencies

None. Aspect extraction shells out to the `claude` CLI via
`subprocess.run`, which is already a runtime dependency for
`claude_dispatch` and `operator_extract`.

## Test Plan

- **Scenario**: `fire_post_document_hooks` is called from MCP `store_put` and from each of the six CLI ingest sites — **Verify**: drift-guard test (`tests/test_hook_drift_guard.py::test_every_cli_ingest_site_fires_document_hook`) AST-walks each module and asserts the call is present in the expected function.
- **Scenario**: A 50-page paper indexed via `nx index pdf` — **Verify**: `aspect_assign_hook` is called exactly once for that paper, not once per chunk; T2 `document_aspects` has exactly one row keyed by `(collection, source_path)`.
- **Scenario**: Extraction on a paper with clear problem/method/results structure — **Verify**: all 5 fields populated, JSON parses, schema validates.
- **Scenario**: Extraction on a position paper with no experimental section — **Verify**: dataset/baseline/results fields null, confidence is whatever the model self-reports (not a quality gate).
- **Scenario**: Re-extraction via `nx enrich aspects --re-extract --extractor-version v2` — **Verify**: rows with `model_version < v2` are overwritten in full; `extracted_at` updates; no diff-merge.
- **Scenario**: Post-document hook receives `code__myrepo` collection — **Verify**: `aspect_assign_hook` short-circuits at the prefix check; no subprocess spawned, no T2 write, no log noise.
- **Scenario**: `subprocess.run` times out or returns non-zero during the hook — **Verify**: `fire_post_document_hooks` catches the exception, persists to T2 `hook_failures` with `chain='document'`, and the original ingest returns success.
- **Scenario**: MCP `store_put` invokes the hook (FastMCP sync-tool worker thread context) — **Verify**: plain sync `fire_post_document_hooks(source_path, collection, content)` call runs to completion without `await`; the surrounding asyncio event loop remains responsive during a 1–3s extraction (FastMCP's thread-pool dispatch handles the offloading); a regression test in `tests/test_hook_drift_guard.py` AST-asserts `mcp/core.py:store_put` calls the dispatcher without `asyncio.to_thread` or `await`, blocking the F1 defect from returning.
- **Scenario**: `nx enrich aspects <collection> --dry-run` — **Verify**: reports document count and cost estimate without spawning subprocesses.
- **Scenario**: `nx enrich aspects <collection> --validate-sample 5` — **Verify**: 5% of newly-extracted papers receive an `operator_check` second-pass; disagreements written to `validation_failures.jsonl`.
- **Scenario**: Migration rollback — **Verify**: T2 upgrade + downgrade leaves schema clean (both `document_aspects` table and the `chain` enum column migration on `hook_failures`, including `is_batch` backfill correctness).

## Validation

### Testing Strategy

Unit tests for the new chain dispatcher (`fire_post_document_hooks`)
mirror the existing `test_post_store_hooks` pattern. Unit tests
for the extractor mock `subprocess.run` and assert prompt-shape
+ JSON parsing behavior. Unit tests for the store use ephemeral
SQLite. The drift-guard test ships in Phase 0 and protects the
six CLI ingest sites by AST walk. Integration test runs
end-to-end on a 3-paper fixture corpus through both `nx enrich
aspects` and the per-document hook path. Spike validation on
`knowledge__delos` confirms once-per-document fire counts and
schema conformance.

### Performance Expectations

- Extraction adds ~1-3s per paper during ingest; measured during spike.
- T2 upsert &lt;1ms per paper (negligible next to the LLM call).

## Finalization Gate

_To be completed during /nx:rdr-gate._

## Implementation Deviations

This section captures audit-driven and spike-driven divergences between the spec body above (frozen at acceptance, 2026-04-25) and what actually shipped on `feature/nexus-b9g1-rdr-089-aspect-extraction`. The spec body is preserved as the historical record; this section is the corrigendum.

| Specified | Shipped | Why |
|-----------|---------|-----|
| Synchronous-inline extraction at the post-document hook fire site | Async-queue dispatch (hook enqueues to T2 `aspect_extraction_queue`; daemon worker drains and calls the synchronous extractor) | P1.3 spike on `knowledge__delos` measured median 26.5 s and p95 38.1 s per document — 11–17× over the <3 s threshold from Critical Assumption #2. Synchronous-inline would block ingest for ~25 s per document. Bead `nexus-qeo8` shipped the redirect; the synchronous `extract_aspects` is reused verbatim by the worker. |
| Hook symbol named `aspect_assign_hook` | Named `aspect_extraction_enqueue_hook` and lives in `src/nexus/aspect_worker.py` (not `mcp_infra.py`) | The async redirect changed the hook's job from "compute and persist" to "enqueue and lazy-spawn worker". The new name reflects the new shape; placing it in `aspect_worker.py` keeps `mcp_infra.py` dependency-light. Drift guard `DOCUMENT_HOOK_GUARDED_NAMES` allowlist is `aspect_worker.py` + `mcp/core.py` accordingly. |
| MCP `store_put` hook fire wrapped in `await asyncio.to_thread(...)` | Plain synchronous call | Audit F1: `store_put` is `def`, not `async def`; FastMCP wraps sync `@mcp.tool()` bodies in worker threads at the framework level. The original spec's `await asyncio.to_thread` was a syntax error (await in a sync function). Pinned via the `test_mcp_store_put_calls_document_hook_synchronously` AST parent-walk test. |
| `hook_failures.is_document=1` boolean column (third boolean alongside `is_batch`) | `hook_failures.chain` TEXT enum (`'single'` \| `'batch'` \| `'document'`) | Audit F7: stacking a third boolean would have been brittle as the chain set grows. Enum is forward-compatible. Migration backfills `chain='batch' WHERE is_batch=1` for historical RDR-095 rows; `is_batch` is retained for back-compat with pre-4.14.2 readers and existing write paths dual-write both columns. |
| 7 fire sites in 5 modules (per §Phase 0 Step 2 close) | 8 fire sites in 6 modules (`doc_indexer.py` accounts for 3 of 8 sites for the pdf/markdown/repo entry points) | P0.review caught the omission: `indexer.py:_index_pdf_file` is a CLI ingest boundary that the bead's original site map did not list. Added in P0.review fix-up commit. The "5 modules" → "6 modules" delta is `indexer.py` itself becoming a fire-site bearer; the "7 → 8" delta is the new site within `indexer.py`. CHANGELOG.md and CLAUDE.md cite the shipped count "8 sites in 6 modules". |
| `--validate-sample` calls `operator_check(items, check_instruction)` | Calls `operator_verify(claim, evidence)` | The RDR confused the two operators in the validation context. `operator_check` is 1-claim-to-N-items (consistency across peers); `operator_verify` is 1-claim-to-1-evidence (grounding a single extraction in its source) — which is what `--validate-sample` actually wants. Implementation uses the correct operator; this RDR text references the wrong one in §Implementation Plan Phase 2 Step 2 (the `--validate-sample` flag description) and §Test Plan scenario 7 — both occurrences are in the frozen spec body and remain as the historical record. |
| Hook chain `(source_path, collection, content)` ignores `content` at MCP boundary | Hook persists `content` to the queue row; worker uses `row.content` as the primary input | Substantive critique caught a silent correctness hole: `content` was originally discarded, and the worker tried to re-read `Path(source_path).read_text()` — but at the MCP boundary `source_path` is a 16-char content-hash `doc_id`, not a real filesystem path. Result was a null-fields record for every MCP-path extraction. Queue gained a `content TEXT` column; CLI rows still pass `content=""` and rely on the worker's source-path-read fallback. |
| `claim_next` SELECT-then-UPDATE under a Python `threading.Lock` | Compare-and-swap pattern: UPDATE WHERE includes `AND status='pending'`, retry on `cursor.rowcount == 0` | Substantive critique caught a cross-process race: two concurrent processes (MCP server + CLI ingest) could double-claim the same row. Python lock does not span processes. CAS pattern adds the across-process guarantee that WAL row-locking alone does not provide for SELECT-then-UPDATE sequences across separate connections. |
| `--validate-sample` default 5% | Default 5% (originally raised to 20% then reverted) | The interim raise to 20% was responding to the P1.3 spike's 16.7% strict-equality cross-run stability. That metric is methodology-shaped (does the model paraphrase between runs?), not hallucination-shaped. `operator_verify` is the hallucination guard; raising the sample rate does not improve hallucination coverage. Restored to RDR-original 5%; comment in source documents the methodology gap and the right trigger for revisiting (token-overlap or embedding-similarity stability metrics, not strict equality). |
| `validation_failures.jsonl` written to corpus directory | Written to current working directory | Pragmatic simplification — the catalog-derived corpus directory is not always present (e.g. when `nx enrich aspects` runs in a sandbox or against a remote-only collection). CWD-relative is context-free; users can `cd` into the corpus dir before running. |
| Bead-listed migration version `4.14.2` (chain) | All three RDR-089 migrations (chain enum, document_aspects, aspect_extraction_queue) tagged `4.14.2` | Migrations were initially tagged 4.14.2 / 4.14.3 / 4.14.4 progressively as beads landed. Substantive critique caught that the package version stayed at 4.14.1, so all three migrations were skipped at runtime. Retagged to share `4.14.2` (same-version-multiple-migrations pattern from 4.0.0); `pyproject.toml` bumped 4.14.1 → 4.14.2 in the same release commit. All three migrations now apply atomically as one bump. |

**Out-of-scope follow-ups — initial framing (post-acceptance, pre-full-scope-deliverable):**

The above table captured the divergences as of the substantive-critic-driven Phase A landing. The user's "EVERYTHING we were to originally deliver" directive after the substantive critique closed every Open Question on this branch in Phases A through F:

| Phase | Scope | Status |
|-------|-------|--------|
| A | Day-2 Ops: `nx enrich list` / `info` / `delete` (RDR §Day 2 Operations) | Shipped, 8 contract tests |
| B | SQL fast path: `operator_filter` / `operator_groupby` / `operator_aggregate` with `source="auto"` / `"aspects"` / `"llm"` and `aspect_field` parameters; `nexus.operators.aspect_sql` substrate | Shipped, 36 contract tests |
| C | Benchmark proving the O(1) claim (synthetic 100-paper corpus, mocked LLM at 1.5 s, real SQLite path) | Shipped: 500x speedup on filter/groupby; 47000x on aggregate — committed evidence in `scripts/spikes/bench_rdr089_results.json` |
| D | Batch Haiku per call: `extract_aspects_batch(items)` extracts N papers in one Claude call; worker batches when queue depth ≥ `batch_size` (default 5) | Shipped, 9 contract tests + 2 worker integration tests |
| E | `extras` → fixed-column promotion mechanic: `promote_extras_field()` + `nx enrich aspects-promote-field` CLI + audit log | Shipped, 16 contract tests |
| F | `rdr__*` extractor (deterministic markdown + frontmatter parser, zero API cost): `rdr-frontmatter-v1` config registered; `ExtractorConfig.parser_fn` shortcut for any future deterministic path | Shipped, 21 contract tests |

What still belongs to a separate operations RDR is **policy** (not mechanism): which `extras` keys graduate, who decides, what governance marks a model_version bump. The Phase E mechanism is one operator command away from action; the policy that drives it is the missing piece.

The consistency model is now documented in the operator docstrings and the SQL fast path docstring (`nexus.operators.aspect_sql` module): operators querying `document_aspects` for a queue-pending document treat the missing row as a non-match (filter), `unassigned` (groupby), or excluded (aggregate). Callers needing eventual consistency re-run after the queue drains, or pass `source="llm"` to bypass T2 entirely.

**Second-pass substantive-critique findings (post-Phase F)**:

A second `/nx:substantive-critique` pass after Phases A–F shipped found four critical correctness defects that survived the first review because the reviewer was checking the implementation against itself; this round checked it against the LLM-path contracts and the documented invariants. All four are resolved in-place:

1. `operator_groupby` SQL path on JSON-array columns (`experimental_datasets`, `experimental_baselines`) would unroll a multi-value item across multiple groups, violating the LLM path's one-group-per-item invariant (RDR-093 §C-1). Now: under `source="auto"` the SQL path detects json_array fields and returns `None` so the operator falls back to LLM (which respects the invariant); under `source="aspects"` the divergence is surfaced as a `_meta` group with rationale rather than silently emitting wrong-shape output. Callers who specifically want unrolled multi-membership must construct a different operator.
2. `count distinct` returned `0 distinct item(s)` when input items lacked an explicit `id` field — a misleading-rather-than-obviously-wrong result that would hide silently in any pipeline driven by `operator_groupby`'s SQL path (which does not synthesize `id`). Now: falls back to dedup by `(collection, source_path)` tuple when `id` is absent on every item, with annotation `"id field absent; deduped by (collection, source_path)"`. Truly identity-less items report `len(items) item(s) (no id or identity field; cannot dedup)`.
3. `_query_confidence_aggregate` silently truncated groups larger than 300 to compute `avg/min/max(confidence)` over an arbitrary first-300 subset. Now: paginates in 300-id batches, accumulates `sum`/`count`/`min`/`max` in Python, returns the exact aggregate over the full group.
4. `_parse_simple_yaml` (Phase F RDR frontmatter parser) corrupted real-world RDR frontmatter on two patterns confirmed in this repo: `note: |` was stored as the literal `|` character; `related:` followed by indented `- item` lines was parsed as the empty string and the items silently dropped. Now: block scalar indicators (`|`, `>`, `|-`, `>-`, `|+`, `>+`) store `None` and skip indented continuations; block lists accumulate stripped items into a Python list.

Plus three significant items: `select_config` docstring updated to reflect the Phase F `rdr__*` registration; `aspect_promotion_log` migrated into the registry (was lazy-only, missed by `nx doctor --check-schema` audits); confidence-aggregate pagination now logs nothing extra because the exact path replaces the truncation entirely.

## References

- Paper: `knowledge__agentic-scholar` Algorithm ExtractTemplate, §D.3 (MatrixConstruct consumption)
- Retrospective: `knowledge__nexus` → "AgenticScholar Retrospective 2026-04-17"
- `src/nexus/mcp_infra.py:296` — `register_post_store_hook` / `fire_post_store_hooks` (RDR-070): the existing single-doc chunk-grain chain. RDR-089 adds a parallel `register_post_document_hook` / `fire_post_document_hooks` chain at the same module mirroring this shape but at document grain.
- `src/nexus/mcp_infra.py:480` / `:607` — `taxonomy_assign_batch_hook`, `chash_dual_write_batch_hook`: live precedents on the parallel batch chain. Same lifecycle (prefix-scoped, idempotent T2 upsert, per-hook failure isolation) — RDR-089's new chain mirrors that lifecycle at a third granularity.
- `src/nexus/pipeline_stages.py:718` — `_catalog_pdf_hook`: the only existing per-document fire site (PDF-specific, private). The natural anchor for `fire_post_document_hooks` in the PDF path.
- `src/nexus/mcp/core.py:386` — registration site for the existing batch chain. RDR-089 adds `register_post_document_hook(aspect_assign_hook)` adjacent.
- `tests/test_hook_drift_guard.py` — existing batch-chain drift guard (`GUARDED_NAMES` frozenset + `test_every_cli_ingest_site_fires_both_chains`). RDR-089 extends with parallel guards for the new chain.
- `src/nexus/bib_enricher.py` — enrichment pattern precedent (per-doc retry)
- `src/nexus/db/migrations.py` — RDR-076 migration registry (where the `document_aspects` table migration and the `hook_failures.chain` enum-column migration land — additive create + additive column with `is_batch` backfill)
- `src/nexus/db/t2/` — T2 domain store pattern (`catalog_taxonomy` is the closest analog)
- `src/nexus/operators/dispatch.py` — `claude_dispatch`: existing `async def` LLM call path. RDR-089 deliberately bypasses it for the synchronous hook (uses `subprocess.run` directly); the shared substrate is the underlying `claude -p` CLI, so the RDR-088 reliability budget transfers.
- RDR-042 — original ExtractTemplate call-out (unpicked bead nexus-erim)
- RDR-070 — taxonomy infrastructure that introduced post-store hooks
- RDR-076 — T2 migration framework
- RDR-088 — `operator_filter` (closed/implemented 2026-04-24; future consumer of this store; source of Spike A reliability data verifying Assumption #1)
- RDR-090 — realistic AgenticScholar benchmark (consumer; cites RDR-089 as the ingest-time enrichment under measurement)
- RDR-093 — `operator_groupby` / `operator_aggregate` (closed/implemented 2026-04-24; paired analytical consumers; Alternative 2 deferred a SQL-aspect-store fast path that this RDR also defers — see Open Questions)
- RDR-095 — Post-Store Hook Framework: Batch Contract (closed/implemented 2026-04-24; established the dispatcher pattern that RDR-089's new chain mirrors at document grain)
