---
title: "Multi-hop entity-resolution retrieval: ingest-time entity-linker + traverse resolution hop, gated by a query-time type-mismatch trigger"
id: RDR-147
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-03
accepted_date:
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

- [ ] Entity records are identifiable at ingest by a stable key that appears as a
  token in referencing artifacts (title == key for store_put'd entities) —
  **Status**: Verified (HERB) / Unverified (general corpora) — **Method**: Spike
- [ ] Catalog `link_if_absent` can be called from within a post-store batch hook
  without deadlocking the catalog-behind-daemon path — **Status**: Unverified —
  **Method**: Spike
- [ ] `traverse` can be used as a plan step with `seeds=$stepN.tumblers` and a
  link-type filter, returning the resolution-target records — **Status**:
  Unverified — **Method**: Source Search + Spike
- [ ] A `where=`-backed exact-filter retrieval can be expressed as a plan operator
  fed by an extract step's output list — **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Three coordinated changes, each mapping to a discovery mechanism, each gated:

1. **Mechanism A — ingest-time entity-linker (Gap 1).** A post-store batch hook
   that, for each newly indexed *entity record* (a document whose title/key is an
   identifier), creates `artifact --mentions--> entity-record` catalog links for
   every already-indexed artifact whose text contains that key verbatim (and the
   reverse for artifacts indexed later). This materializes the join graph at
   ingest, discovered from the corpus alone.

2. **Deterministic resolution hop (Gap 2).** A plan step that resolves a set of
   identifiers to their entity records *without fuzzy retrieval* — preferred via
   `traverse` over the Mechanism-A links; fallback via a new `resolve(ids, by=title)`
   operator backed by `search --where title=<id>` per id. Dense retrieval is never
   used for exact-key resolution.

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

**Deterministic resolution hop.** Preferred: a plan sub-DAG
`search(question) [s1] → traverse(seeds=$s1.tumblers, link_types=["mentions"],
direction="out", depth=1) [s2] → extract(inputs=$s2.contents, fields=answer_field)`.
This reuses existing `traverse` + `extract` operators; the only new wiring is
allowing `traverse` as a runner step with `$ref` seeds. Fallback operator
`resolve(ids, by="title")`: for each id, issue a `where=title=<id>` exact-filter
retrieval; return the records. Error contract: ids that resolve to zero records
are reported, not silently dropped (mirror `store_get_many`'s `missing` field).

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
| Resolution-hop traversal | `traverse` MCP tool / catalog BFS | Reuse for the hop; Extend runner to accept `traverse` as a `$ref`-seeded plan step. |
| `resolve(ids, by=title)` operator (fallback) | `nexus.plans.runner` `_OPERATOR_TOOL_MAP` + `search --where` | Add: thin operator over the existing where-filter primitive. |
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
  PR-by-author) in any corpus, from one discovery rule.
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
  `question→affected-customer`. **Mitigation**: Scope MVV to the resolution hop
  only; treat the outer hop as a separate, later concern; don't over-claim.

### Failure Modes

- **Visible**: resolution hop returns empty → answer falls back to single-hop
  (current behavior), logged with the unresolved ids.
- **Silent**: stale `mentions` links after deletion → resolves to a wrong/dead
  record. Diagnose via `link_audit`; mitigate with deletion-cascade on links.
- **Recovery**: feature-flag the entity-linker and the trigger; off = today's behavior.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (link-from-hook safety; traverse-as-plan-step;
  resolve operator).
- [ ] HERB SearchFlow sandbox available as the regression fixture (exists in `assetops-kg`).

### Minimum Viable Validation

On HERB SearchFlow, with the entity-linker enabled and the resolution hop wired,
a company question whose affected customers ARE correctly retrieved in hop 1
resolves their CUST-ids to company names via `traverse` (not fuzzy search),
yielding company recall > 0 on at least that controlled subset — proving the
deterministic join executes end-to-end. (Full company recall is out of scope; it
is gated by the separate hop-1 recursion finding.)

### Phase 1: Code Implementation

#### Step 1: Entity-record classification + `entity_key` stamping
Add an `entity_key` metadata path through store_put/index; default heuristic per collection.

#### Step 2: Entity-linker batch hook
Register in `install_default_hooks`; exact token-match → `link_if_absent(..., link_type="mentions")`. Feature-flagged.

#### Step 3: Resolution hop
Enable `traverse` as a `$ref`-seeded plan step; add `resolve(ids, by=title)` fallback operator over `where=title`.

#### Step 4: Type-mismatch trigger + plan template
Extend the inline-planner contract; `plan_save` the emitted plan keyed on the dimensional signature; gate to fire only on referenced-but-not-present.

### Phase 2: Operational Activation

Feature flags default off; enable on HERB sandbox first; measure link precision
and resolution recall before any default-on.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `mentions` catalog links | `link_query` | `links_from`/`links_to` | deletion-cascade (new) | `link_audit` | catalog backup (existing) |
| `lookup` plan template | `plan list` | `plan show` | `plan` delete | `plan_match` probe | T2 backup (existing) |

Deletion-cascade for `mentions` links is in scope (silent-failure mode above
depends on it).

### New Dependencies

None — reuses catalog, plans runner, hook registry, and the `where` filter.
