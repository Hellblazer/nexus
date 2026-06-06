---
title: "Multi-hop entity-resolution retrieval: ingest-time entity-linker + traverse resolution hop, gated by a query-time type-mismatch trigger"
id: RDR-147
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-03
accepted_date: 2026-06-05
related_issues: []
---

# RDR-147: Multi-hop entity-resolution retrieval (Mechanism A + B)

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

nexus cannot answer questions whose answer entity type is *referenced* in the
evidence but not *present* in it — the canonical "deep search" multi-hop join.
Concretely (HERB SearchFlow, see `assetops-kg` benchmark): "which companies were
affected by issue X?" retrieves issue/chat artifacts that name **customer IDs**
(`CUST-0018`), but the answer requires **company names** (`FusionTech`). The
resolution `CUST-0018 → FusionTech` lives in a separate customer record. nexus's
single-pass retrieve-then-synthesize plan never performs the dependent second
retrieval, so it emits raw IDs or hallucinates. Company recall measured 0.000.

The failure decomposes into a discovery problem and an execution problem, and
both are unsolved in nexus today.

### Enumerated gaps to close

#### Gap 1: No ingest-time entity linking — the join graph is never built

Entity records whose key (e.g. a customer record titled `CUST-0018`) appears
verbatim as a token inside other artifacts imply a foreign-key-like reference.
nexus indexes both the entity records and the referencing artifacts but creates
**no catalog edge** between them. With no `artifact --mentions--> entity-record`
link, `traverse` has nothing to walk, so the resolution hop cannot be executed
as a deterministic graph step. The referential structure is discoverable at
index time and is currently discarded.

#### Gap 2: Fuzzy retrieval cannot execute the resolution hop — embedding collision

Even with the entity records indexed and retrievable, a semantic re-query for an
exact key fails: the 120 SearchFlow customer records are near-identical sentences
(`Customer CUST-xxxx: contact N, R at company C`), so they collide in embedding
space. **Verified**: `search("CUST-0035")` returns `CUST-0097`, `CUST-0046`,
`CUST-0079` — never `CUST-0035`. `store_get`-by-title also failed. Only a
metadata exact-filter (`search --where title=CUST-0035`) resolves deterministically.
nexus has no first-class deterministic key-resolution retrieval operator that a
plan can target; the only working path is an undocumented `where=title` filter.

#### Gap 3: No type-mismatch trigger — the planner cannot discover the resolution hop

The signal that a resolution hop is needed — "the answer entity type is
referenced-but-not-present in hop-1 evidence" — is internal and available a
priori from `(question, hop-1 result)`. **Verified**: with `force_dynamic` and a
structural-rule context, the inline planner *correctly states* "these are
customer accounts referenced only by ID, not company names" — discovery fires —
but there is no standing mechanism that types `answer_shape` vs `evidence_shape`
and inserts the resolve hop. The capability exists only when hand-prompted.

#### Gap 4: No gating — blanket iteration harms simple questions

Literature (Beyond Static Retrieval, arXiv 2509.25530; **Documented**) shows
iterative/multi-hop retrieval *harms* single-hop and simple-comparison questions
(added documents dilute precision; gold "bridge" docs get buried beyond leading
positions). A resolution hop must fire only on the type-mismatch signal, never
unconditionally.

## Context

### Background

Discovered while benchmarking nexus retrieval against the HERB benchmark
(Salesforce AI Research, "Benchmarking Deep Search over Heterogeneous Enterprise
Data", arXiv 2506.23139) in an isolated local sandbox. nexus's composed
`nx_answer` arm matched GPT-4o agentic ReAct on People search (~34 vs ~32, local
bge-768, zero metered API) but scored 0 on Customer/company. Root-causing that
single number produced the finding chain in T2/T3
(`herb-nexus-comparison-results`, `discovering-multihop-query-plans-from-structure`,
`multihop-retrieval-literature-map-for-nexus`). The design here is deliberately
derived from the *structure* of the task and corpus, not reverse-engineered from
a higher-scoring competitor.

The literature (StepChain GraphRAG 2510.02827; ByoKG-RAG 2507.04127; PRISM
2510.14278; PAR-RAG 2504.16787; Self-RAG 2310.11511; all indexed in T3
`knowledge`, DT-sourced) independently confirms the approach: the GraphRAG family
exists *precisely because* dense retrieval cannot do structured exact-key joins
(PRISM, citing Weller 2025: "no single embedding can represent all query–document
relevance patterns as candidate sets grow combinatorially"). StepChain's
`Extract(chunk)` + `Link(entity_a, entity_b, chunk)` + deterministic BFS is this
RDR's Mechanism A under a different name.

### Technical Environment

- nexus package: `src/nexus/` (repo `git/nexus`). Catalog behind the T2/T3
  daemons.
- Catalog graph: `src/nexus/catalog/catalog_links.py` (`link` 293,
  `link_if_absent` 317, `links_from` 514, `links_to` 537, `link_query` 560);
  `src/nexus/catalog/catalog.py` (`link` 1870). `traverse` exposed as an MCP tool
  (catalog BFS with depth cap, direction, link-type / purpose filter, cycle
  detection).
- Plan runner: `src/nexus/plans/runner.py` — operator registry `_OPERATOR_TOOL_MAP`
  (680), step-ref resolution (`$stepN.<field>` → ids/contents/extractions/...),
  auto-hydration of `ids`, `_OPERATOR_MAX_INPUTS=100` cap (695).
- Ingest seam: `src/nexus/hook_registry.py` — `HookRegistry`
  (`register_single`/`register_batch`/`register_document`, `fire_store_chains`
  271) and `install_default_hooks` (320), where post-store chains (chash index,
  taxonomy, aspect queue) are wired. This is where an entity-linker hook attaches.
- Planner: `src/nexus/mcp/core.py` `nx_answer` (plan-match-first; inline `claude -p`
  planner on miss); `src/nexus/plans/match.py` / `matcher.py` (plan_match).
- Deterministic primitive: `search --where KEY=VALUE` metadata exact-filter.

## Research Findings

### Investigation

Benchmarked nexus on HERB SearchFlow (1559 artifacts incl. customer/employee
entity records, bge-768 local, isolated sandbox). Implemented and ran a
deterministic multi-hop resolver (`assetops-kg benchmark/multihop_resolve.py`).
Probed the inline planner with a structural-rule context + `force_dynamic`.
Surveyed the 2024–2025 multi-hop / GraphRAG literature (6 papers indexed in T3).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `nexus.catalog.catalog_links` | Yes | `link`/`link_if_absent` create typed edges between tumblers; BFS via `links_from`/`links_to`; `traverse` MCP tool already does depth-capped, link-type-filtered, cycle-safe walks. |
| `nexus.plans.runner` | Yes | Operators dispatched via `_OPERATOR_TOOL_MAP`; steps reference prior outputs `$stepN.ids/.contents/...`; ids auto-hydrated; 100-input cap. No `resolve`/exact-key operator exists. |
| `nexus.hook_registry` | Yes | `install_default_hooks` is the single wiring point for post-store chains; `register_batch` accepts `fn(doc_ids, collection, contents, ...)` — the correct seam for an entity-linker. |
| `search --where` | Yes (spike) | Metadata exact-filter returns the exact-title record where semantic search and `store_get`-by-title both fail. |

### Key Discoveries

- **Verified** — Embedding collision: `search("CUST-0035")` returns other CUST
  records, never the exact one; dense retrieval cannot do exact-key resolution.
- **Verified** — `search --where title=<id>` resolves the exact record
  deterministically; this is the only working key-resolution primitive today.
- **Verified** — The inline planner, when prompted with the structural rule,
  correctly *detects* the type mismatch ("ids, not names") — discovery is feasible.
- **Verified** — Even with a working resolution hop wired, company recall stayed
  0 because hop-1 surfaced the wrong customers: the bottleneck **recurses** to
  `question → affected-issue → affected-customer`. Resolution is necessary, not
  sufficient.
- **Documented** — StepChain/ByoKG/HippoRAG2/LEGO-GraphRAG build explicit entity
  graphs for exactly this join; PRISM/PAR-RAG decompose with precision filtering;
  Self-RAG/FLARE gate retrieval reflectively; Beyond-Static documents that
  blanket iteration hurts simple questions.

### Critical Assumptions

- [x] Entity records are identifiable at ingest by a stable key that appears as a
  token in referencing artifacts (title == key for store_put'd entities) —
  **Status**: Verified (HERB) / Refuted (general corpora) — **Method**: Spike +
  Source Search (2026-06-05). Codebase mechanism confirmed: `store_put` stores
  `title` verbatim in T3 chunk metadata (`metadata_schema.normalize()` applies no
  transformation; title is in `ALLOWED_TOP_LEVEL`), and `where=title=<key>`
  exact-matches via ChromaDB equality (`filters._OP_MAP`, `t3.find_ids_by_title`).
  BUT title==key holds **only** on the MCP/CLI `store_put(title=<token>)` path,
  where the caller explicitly chose the token. Every batch-index path mangles or
  reshapes the key: `indexer_utils.derive_title()` title-cases filename tokens
  (`cust-0035.md` → "Cust 0035"); code chunks title is `rel_path:Lstart-Lend`
  (never a key); PDF/markdown titles are human-readable. For general corpora
  there is **no stable entity-key field at all** — the assumption is structurally
  inapplicable, not merely untested, unless the corpus was purpose-built with the
  `store_put(title=<token>)` pattern (as HERB's 120 CUST records were). Additional
  preconditions baked in: entity records must be single-chunk (multi-chunk →
  ambiguous `find_ids_by_title`), and the target collection must be known a priori
  (resolution is per-collection).
- [x] Catalog `link_if_absent` can be called from within a post-store batch hook
  without deadlocking the catalog-behind-daemon path — **Status**: Verified —
  **Method**: Source Search (2026-06-05). `fire_batch` fires LAST in `store_put`
  (`mcp/core.py`), after `catalog_store_hook`, `t3.put` (ChromaDB-only, no catalog
  lock), and `_catalog_auto_link` have all unwound and closed their `CatalogWriter`
  s in `finally`. The hook gets a fresh `CatalogWriter`/socket. Daemon serializes
  catalog writes with a cooperative `asyncio.Lock` (`daemon/t2_daemon._dispatch`),
  not an OS mutex — no blocking across RPCs. The directory `fcntl.flock`
  (`catalog_links.link_if_absent → _acquire_lock`) is acquired/released atomically
  INSIDE the daemon process per op. **Live precedent**: `manifest_write_batch_hook`
  (`mcp_infra.py`) already calls `get_catalog_writer()` → daemon RPC (multi-step
  manifest writes) from inside `fire_batch` on every store_put in production
  without deadlock. Residual: throughput/latency at high concurrent ingest (one
  more RPC in the serialization queue), not a correctness concern.
- [x] `traverse` can be used as a plan step with `seeds=$stepN.tumblers` and a
  link-type filter, returning the resolution-target records — **Status**: Verified
  (with precondition) — **Method**: Source Search + Spike (2026-06-05). `traverse`
  is a first-class non-embedding plan-runner step (`runner._NON_EMBEDDING_TOOLS`,
  isolated-step path via `bundle.py`), returns the standard
  `{tumblers, ids, collections}` retrieval contract. `$stepN.tumblers` resolves via
  `runner._resolve_value` (`_STEPREF_RE`, list-flatten); `search`/`query` emit
  `tumblers` in structured mode. Three production builtin plans already chain this
  (`citation-traversal.yml`, `hybrid-factual-lookup.yml`, `traverse-then-generate.yml`)
  plus dedicated coverage in `tests/test_traverse_step.py`. **Precondition**: the
  seed step's `tumblers` are populated from chunk metadata field `tumbler`, which is
  non-empty only for catalog-registered documents; unparseable seeds are silently
  dropped (`Tumbler.parse` try/except). For RDR-147's ingest-of-entity-records
  scenario this is the intended baseline, but it COMPOUNDS assumption #1's boundary:
  resolution needs entity records both store_put-titled AND catalog-registered with
  a tumbler.
- [ ] A `where=`-backed exact-filter retrieval can be expressed as a plan operator
  fed by an extract step's output list — **Status**: REFUTED — **Method**: Source
  Search + Spike (2026-06-05). Not expressible with today's runner; needs new
  infrastructure. Three independent gaps, each sufficient to block: (A) **No
  `resolve` operator** — `_OPERATOR_TOOL_MAP` (`runner.py`) has no `resolve`/exact-
  key entry; the RDR's own §Technical Design already describes `resolve(ids,
  by="title")` as new code. (B) **No fan-out primitive** — `plan_run` iterates over
  STEPS, not items within a step output; no `foreach`/`map_over_items`; `_STEPREF_RE`
  has no indexing syntax, so resolving N extracted tokens (each needing one
  `where=title=<token>` call) is not wireable. (C) **No `$in` in the MCP `where=`
  surface** — `filters.parse_where_str` accepts only `KEY{>=,<=,!=,>,<,=}VALUE`;
  `$in` exists internally (`search_engine` catalog prefilter on `doc_id`) but is
  unreachable from a plan step's `where=` string. `operator_filter` is NL post-
  retrieval filtering, NOT key-resolution retrieval. Closing #4 requires at minimum
  a new `resolve` operator that internally loops over its input id-list (collapses
  Gaps A+B), or adding `$in` support to `where=` (collapses to one search step).

## Proposed Solution

### Approach

Three coordinated changes, each mapping to a discovery mechanism, each gated:

1. **Mechanism A — ingest-time entity-linker (Gap 1).** A post-store batch hook
   that, for each newly indexed *entity record* (a document whose title/key is an
   identifier), creates `artifact --mentions--> entity-record` catalog links for
   every already-indexed artifact whose text contains that key verbatim (and the
   reverse for artifacts indexed later). This materializes the join graph at
   ingest, discovered from the corpus alone.

2. **Deterministic resolution hop (Gap 2).** Two complementary stages, both
   required: a new `resolve(ids, by="title")` operator turns extracted entity
   *tokens* into records via `search --where title=<id>` per id (the `mentions`
   graph cannot — it is artifact→entity, not token→entity); then `traverse` over
   the Mechanism-A links optionally expands resolved records to related artifacts.
   An `operator_filter` precision step precedes resolution to narrow hop-1 seeds —
   the benchmark proved resolution alone leaves recall at 0 without it. Dense
   retrieval is never used for exact-key resolution.

3. **Mechanism B — type-mismatch trigger, gated (Gaps 3, 4).** The planner types
   the requested `answer_shape`; after hop 1 it types `evidence_shape`; if
   `answer_shape` is referenced-but-not-present (identifiers for the answer type
   appear, values do not), it inserts the resolution hop. The trigger fires only
   on that signal — simple/single-hop questions skip it (Beyond-Static guidance).
   The discovered plan is `plan_save`'d as a template keyed on a dimensional
   signature so `plan_match` reuses it.

### Technical Design

**Mechanism A — entity-linker hook.** Register in `install_default_hooks`
(`hook_registry.py:320`) via `register_batch`. Interface (illustrative — verify
during implementation):

```text
// fn(doc_ids: list[str], collection: str, contents: list[str]) -> None
// 1. classify which of doc_ids are ENTITY records (title matches an entity-key
//    pattern, or carries an `entity_key` metadata flag set by the indexer)
// 2. for each entity record E with key K:
//      find already-indexed artifacts whose text contains token K
//      (FTS / token-index lookup, NOT embedding search)
//      Catalog.link_if_absent(from=artifact.tumbler, to=E.tumbler,
//                             link_type="mentions", created_by="entity-linker")
// 3. symmetric pass: when a non-entity artifact is indexed, link it to any
//    existing entity records whose key it mentions
```

Entity-record classification is the load-bearing design choice. Options (decide
in spike): (a) explicit — the indexer/store_put stamps `entity_key=<K>` metadata
on records the caller marks as entities; (b) heuristic — title matches a
configured key regex per collection. Prefer (a) for generality; (b) as a
zero-config default. Key→artifact matching MUST be exact token match (FTS or a
key-token index), never semantic.

**Deterministic resolution hop — two complementary stages, not primary/backup.**
The resolution hop has two distinct sub-problems that operate at different
pipeline positions and are BOTH required:

- **Stage R1 — token→record resolution (`resolve` operator, always required).**
  Hop-1 evidence yields entity *tokens* as text (e.g. `extract` pulls
  `CUST-0035`, `CUST-0018` from issue bodies). Turning a token string into an
  entity record requires exact-key lookup: the new `resolve(ids, by="title")`
  operator issues a `where=title=<id>` exact-filter retrieval per id (collapsing
  the three Assumption-#4 gaps: no operator / no fan-out / no `$in`). The
  `mentions` graph does NOT cover this case — `mentions` is artifact→entity, not
  token→entity, so `traverse` cannot turn a free-floating extracted token into a
  record. Error contract: ids that resolve to zero records are reported in a
  `missing` field, never silently dropped (mirror `store_get_many`).
- **Stage R2 — graph expansion (`traverse`, optional depth-extension).** Given
  resolved entity-record tumblers, `traverse(seeds=$ref.tumblers,
  link_types=["mentions"], direction="in/out", depth=1)` walks the Mechanism-A
  links to reach related artifacts (e.g. entity-record → the company record it
  belongs to). Reuses the existing `traverse` step (Assumption #3 VERIFIED).

**Hop-1 precision is load-bearing, not deferred.** The benchmark proved that
resolution alone leaves company recall at 0 because hop-1 surfaces the *wrong*
seeds. An `operator_filter` step (existing operator, NL predicate over hop-1
results) sits BEFORE resolution and narrows the candidate set to entities the
question actually constrains (PRISM Selector pattern; T3 synthesis
`research-rdr147-multihop-synthesis-2026-06-05`). The canonical end-to-end plan
template is therefore:

```text
[decompose] → search(question) [s1]
  → operator_filter(inputs=$s1, criterion="<entity the question constrains>") [s2]
  → extract(inputs=$s2.contents, fields=entity_token) [s3]
  → resolve(ids=$s3.extractions, by="title") [s4]     # token → entity record
  → traverse(seeds=$s4.tumblers, link_types=["mentions"], depth=1) [s5]  # optional R2
  → extract/generate(answer_field)
```

`operator_filter` between hop-1 and resolution is the difference between
real-HERB company recall > 0 and recall ≈ 0; it is in scope, not a later concern.

**Mechanism B — type-mismatch trigger.** Extend the inline-planner decomposition
contract (the `claude -p` planner prompt in the `nx_answer` flow) to: (1) name
the `answer_shape`; (2) after hop 1, decide whether evidence carries
identifier-references to `answer_shape` rather than its values; (3) if so, emit
the resolution sub-DAG; (4) otherwise proceed single-hop. Gating is intrinsic:
the trigger is a positive test, so simple questions never reach it. Persist the
emitted plan via `plan_save` with dimensions
`{verb: "lookup", answer_shape: "entity-name", evidence_shape: "entity-ref"}` so
`plan_match` reuses it for company / person-by-role / PR-by-author alike.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Entity-linker hook | `nexus.hook_registry` (`register_batch`, `install_default_hooks`) | Extend: add one batch hook; no new framework. |
| `mentions` links | `nexus.catalog.catalog_links.link_if_absent` | Reuse: existing typed-edge API. |
| Resolution R2 (graph expansion) | `traverse` MCP tool / catalog BFS | Reuse for the hop; Extend runner to accept `traverse` as a `$ref`-seeded plan step. |
| Resolution R1 `resolve(ids, by=title)` (token→record, always required) | `nexus.plans.runner` `_OPERATOR_TOOL_MAP` + `search --where` | Add: thin operator over the existing where-filter primitive, internal fan-out. |
| Hop-1 precision filter | `operator_filter` (existing runner operator) | Reuse: insert as plan-template step before resolution; no new code. |
| Type-mismatch trigger | `nx_answer` inline planner contract | Extend: decomposition-prompt discipline + plan template. |
| Key→artifact token match | FTS / catalog | Reuse FTS; do NOT use embedding search. |

### Decision Rationale

Mechanism A is the most defensible execution path: the join is implied by corpus
structure, discovered once at ingest, and executed deterministically by graph
traversal — immune to the embedding collision that defeats every fuzzy approach,
and validated by the entire GraphRAG subfield. Mechanism B makes the capability
*self-discovering* from query structure rather than per-corpus hand-coding, and
the gating respects the documented harm of blanket iteration. Reusing
catalog/traverse/hooks keeps the surface small.

## Alternatives Considered

### Alternative 1: Bigger / better embedding model

**Description**: Replace bge-768 with a larger embedder to separate near-identical
entity records.

**Pros**: No architecture change.

**Cons**: Theoretically cannot work — PRISM/Weller 2025 show no single embedding
represents all relevance patterns as candidate sets grow combinatorially; our
collision is an instance, not a model-quality artifact.

**Reason for rejection**: Attacks a structural limit with a parameter knob.

### Alternative 2: Pure-LLM iterative re-query (no graph, no exact-filter)

**Description**: Let the agent re-query semantically for each extracted id.

**Pros**: No ingest-time work.

**Cons**: Verified to fail — semantic re-query for `CUST-0035` returns other
records. Also incurs the error-propagation Beyond-Static documents.

**Reason for rejection**: The exact failure we measured.

### Briefly Rejected

- **Per-corpus hardcoded joins** (e.g. a SearchFlow-specific CUST→company step):
  failure-derived, non-general, the trap this RDR explicitly avoids.
- **Index entity records only, no links** (what we tried): necessary but proven
  insufficient — resolution hop still can't find the exact record by fuzzy search.

## Trade-offs

### Consequences

- (+) Generalizes to any entity-resolution join (company, person-by-role,
  PR-by-author) across corpora **where entity records carry a stable extractable
  key** — i.e. `store_put(title=<token>)` records, or corpora where Step 1's
  `entity_key` extraction pipeline is configured (regex for structured IDs, NLP
  for natural entities). This is NOT unconditional: per Assumption #1, batch-index
  paths have no stable key by default — code chunks (titled `rel_path:Lstart`) are
  explicitly out of scope for entity-key resolution; PDF/markdown require the
  Step-1 extractor. The "one discovery rule" is the linker hook; the precondition
  is that an entity key exists for it to match.
- (+) Deterministic resolution — immune to embedding collision; auditable graph paths.
- (−) Ingest cost: an extra token-match + link-write pass per entity record.
- (−) Link-graph maintenance: re-index / deletion must keep `mentions` links consistent.
- (−) Planner complexity: the type-mismatch trigger adds a branch to decomposition.

### Risks and Mitigations

- **Risk**: Entity-record misclassification floods the graph with spurious links.
  **Mitigation**: Prefer explicit `entity_key` stamping; cap/gate the heuristic;
  measure link precision on HERB before enabling by default.
- **Risk**: Calling `link_if_absent` inside a store hook deadlocks the
  catalog-behind-daemon path. **Mitigation**: Spike first; if blocking, enqueue
  links to an async post-commit pass.
- **Risk**: The recursion finding — even perfect resolution doesn't close
  `question→affected-customer` because hop-1 surfaces wrong seeds.
  **Mitigation**: the hop-1 precision step (`operator_filter`, existing operator)
  is IN SCOPE in the plan template (Step 4), not deferred — it is the load-bearing
  difference between real-HERB recall > 0 and recall ≈ 0. Residual risk: for join
  conditions requiring cross-artifact reasoning, the filter criterion depends on
  the `mentions` typed link existing from ingest (Step 2); corpora without that
  link seeded will still surface imprecise seeds. That residual is the honest
  boundary, not the whole outer hop.

### Failure Modes

- **Visible**: resolution hop returns empty → answer falls back to single-hop
  (current behavior), logged with the unresolved ids.
- **Silent**: stale `mentions` links after deletion → resolves to a wrong/dead
  record. Diagnose via `link_audit`; mitigate with deletion-cascade on links.
- **Recovery**: feature-flag the entity-linker and the trigger; off = today's behavior.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions resolved (2026-06-05): #2 link-from-hook safety and
  #3 traverse-as-plan-step VERIFIED; #1 boundary documented (title==key only on the
  `store_put` path); #4 REFUTED → `resolve` operator must be IMPLEMENTED (not
  verified — it does not exist today).
- [ ] HERB SearchFlow sandbox available as the regression fixture (exists in `assetops-kg`).

### Minimum Viable Validation

On HERB SearchFlow, with the entity-linker enabled and the FULL plan template
wired (`search → operator_filter → extract → resolve → traverse`), **real HERB
company questions yield company recall > 0** — measured against the same
benchmark harness that produced the 0.000 baseline. This is the observable
success criterion: the headline metric the Problem Statement declared broken must
move. The `operator_filter` precision step is the load-bearing addition — without
it, hop-1 surfaces wrong seeds and recall stays ≈ 0 even with a perfect resolution
hop (the benchmark already proved this). MVV is NOT scoped to a synthetic
hop-1-correct fixture; that would pass while company recall stayed 0, which is the
exact silent-scope-reduction this RDR must avoid. A secondary diagnostic assertion
(resolution executes deterministically on a hop-1-correct subset) may be used to
isolate the resolve operator during development, but it does not satisfy the MVV.

### Benchmark Ladder — per-phase observable metrics

The single end-to-end metric (company recall) only moves when all three phases
land, so it gives no intermediate signal. To make the HERB SearchFlow benchmark
an *iterative* driver, decompose it into a ladder of isolated sub-metrics — each
phase turns its own metric off zero even while company recall is still 0. The
benchmark harness (`assetops-kg`) emits a four-line `herb-scorecard.json`; a copy
is checked into nexus as the regression baseline and updated per phase PR. Each
metric is asserted as an EXACT value (`== N`, never `>= N`) so a silent-corruption
regression cannot pass (see `feedback_exact_assertions_for_fixture_regression`).

| Phase | Isolated metric | Graded against | Independent of | Baseline → target |
| --- | --- | --- | --- | --- |
| P1 (Step 2 linker) | `mentions`-link precision + recall | gold CUST→company edge truth | retrieval entirely | 0 → link-recall 1.0, precision ≥ threshold |
| P2 (Step 3 resolve) | resolution accuracy: gold CUST-id → correct record | fed GOLD ids | hop-1 quality | 0 → 1.0 |
| P3 (Step 4 filter) | hop-1 precision: surfaced customers actually affected / surfaced | gold question→affected-customer truth | resolve/linker | the recursing-bottleneck number; moves as the filter tightens |
| E2E (MVV) | company recall on real HERB questions | benchmark harness | nothing | 0.000 → > 0 |

**TDD contract.** Write all four assertions FIRST (RED). Each phase flips exactly
one to GREEN; the downstream assertions stay RED with their recorded number, so a
phase cannot close as "progress" while its successors are unverified. The
phase-review-gate for each phase asserts (a) this phase's metric moved to its
exact target AND (b) the E2E number is honestly reported, not faked. This
operationalizes the load-bearing-jointness: shipping P2 alone flips only
resolution-accuracy; company recall stays RED and visible in every subsequent
review.

**Gold-truth fixtures** (built once, Step 0 of Phase 1): `cust_to_company.json`
(120 CUST→company pairs) and `question_to_affected_customers.json` (the company
questions with their ground-truth affected-customer sets). Both derive
deterministically from the HERB SearchFlow source; seeded, no API.

### Phase 1: Code Implementation

#### Step 0: Benchmark ladder fixtures + RED assertions
Build the gold-truth fixtures (`cust_to_company.json`, `question_to_affected_customers.json`) from HERB SearchFlow and the four scorecard assertions (P1/P2/P3/E2E), all RED. This is the regression oracle every subsequent step is graded against — see §Benchmark Ladder.

#### Step 1: Entity-record classification + `entity_key` stamping
Add an `entity_key` metadata path through store_put/index. Per-indexer behavior
(closes Assumption #1's general-corpus gap explicitly):
- `store_put`: caller-set `entity_key` (explicit), else title if it matches the
  collection's key regex.
- `nx index` (markdown/RDR): regex extraction of structured IDs from frontmatter /
  H1 / body (e.g. `CUST-\d+`, `TICKET-\d+`); optional NLP pass for natural entities.
- `nx index pdf`: regex over extracted text; entity records here are uncommon —
  config-gated per collection.
- `nx index repo` (code): **explicitly excluded** — `rel_path:Lstart` titles are
  not entity keys; no extraction attempted.
Mechanism for matching keys in referencing artifacts MUST be exact token match
(FTS / key-token index), never semantic.

#### Step 2: Entity-linker batch hook
Register in `install_default_hooks`; exact token-match → `link_if_absent(..., link_type="mentions")`. Feature-flagged.

#### Step 3: Resolution hop (both stages)
Add the `resolve(ids, by="title")` operator to `_OPERATOR_TOOL_MAP` (R1, token→record exact-filter, internal fan-out over `where=title`); enable `traverse` as a `$ref`-seeded plan step (R2, graph expansion). Resolve is always-required, not a fallback to traverse — see §Technical Design.

#### Step 4: Type-mismatch trigger + plan template (incl. `operator_filter`)
Extend the inline-planner contract to type `answer_shape`/`evidence_shape` and gate on referenced-but-not-present. `plan_save` the FULL template keyed on the dimensional signature: `search → operator_filter(hop-1 precision) → extract → resolve → traverse → synthesize`. The `operator_filter` step is mandatory and load-bearing — omitting it reproduces the hop-1 precision failure and leaves company recall ≈ 0 (the MVV will fail). `operator_filter` already exists in the runner; this step is plan-template YAML, not new code.

#### Step 5: Deletion-cascade for `mentions` links
Wire `mentions`-link removal into the entity-record deletion path so a deleted entity record cascades to its inbound/outbound `mentions` edges (closes the silent stale-link failure mode in §Failure Modes). Without this, resolution silently returns dead records.

### Phase 2: Operational Activation

Feature flags default off; enable on HERB sandbox first. Gate default-on on the
`herb-scorecard.json` ladder: P1 link precision/recall and P2 resolution accuracy
at their exact targets, P3 hop-1 precision above its threshold, and E2E company
recall > 0 — all GREEN before any default-on.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `mentions` catalog links | `link_query` | `links_from`/`links_to` | deletion-cascade (new) | `link_audit` | catalog backup (existing) |
| `lookup` plan template | `plan list` | `plan show` | `plan` delete | `plan_match` probe | T2 backup (existing) |

Deletion-cascade for `mentions` links is implemented in Phase 1 Step 5 (the
silent-failure mode above depends on it).

### New Dependencies

No new *external* packages. New *components* built in this RDR (Assumption #4
confirmed new infrastructure is required):

- `resolve(ids, by="title")` operator — new `_OPERATOR_TOOL_MAP` entry, ~50 lines,
  internal fan-out over the existing `where=title` filter.
- Entity-linker batch hook — new logic in `install_default_hooks` (FTS token scan
  + `link_if_absent` writes), ~100–200 lines.
- `entity_key` extraction path through the indexer for non-`store_put` paths
  (Step 1) — regex/NLP extractor + metadata stamp.
- Deletion-cascade for `mentions` links (Step 5).

Reused unchanged: catalog typed-link API, `traverse`, `operator_filter`, the plan
runner, hook registry, and the `where=` exact-filter primitive.
