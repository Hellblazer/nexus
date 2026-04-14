---
title: "RDR-078: Unified Context Graph & Retrieval — Projection Link Promotion, Surface Alignment, Scenario Skills"
status: draft
type: feature
priority: P2
created: 2026-04-14
related: [RDR-063, RDR-070, RDR-075, RDR-077, RDR-053]
reviewed-by: self
---

# RDR-078: Unified Context Graph & Retrieval

Follow-up to RDR-077 surfaced during the v4.3.0 live demo on the ART repo. Cross-corpus retrieval is currently a three-legged stool: the catalog link graph, the taxonomy topic graph, and the RDR-077 projection graph. Each is useful in isolation; each is walled off from the others. Agents working on planning, review, analysis, debugging, and documentation write the same composition glue repeatedly, and the glue always degrades at the same joint — the link graph is too sparse where semantic similarity is strong.

This RDR closes three gaps in one iteration: promote projection assignments into the catalog link graph so the existing `query(follow_links=...)` walks a dense graph, align the retrieval-tool surfaces so a single call can express topic-and-link constraints together, and add five thin scenario skills so agents reach for this machinery reflexively.

## Problem

### Problem 1: Catalog link graph is sparse where it matters most

The catalog heuristic linker (`nexus.catalog.link_generator.generate_code_rdr_links`) matches code-file *stem* against RDR *title* tokens. It produces `implements-heuristic` edges with acceptable precision but poor recall:

- **Measured per-workspace heuristic recall** (2026-04-14, via `nexus_rdr/078-research-1`):
  - ART workspace (`code__ART-8c2e74c0`): 4,180 code entries, 35 heuristic edges to sibling RDR/docs — **0.8% of code files have any link**.
  - nexus workspace (`code__nexus-571b8edd`): 280 code entries, 22 heuristic edges — **7.9%**.
- The earlier "25.9%" figure in the pre-gate draft came from `nx catalog coverage` aggregated across all 93 collections in the live DB — it counts any link of any type regardless of endpoint, which is not the recall we need here. Per-workspace, directed-to-authoring-RDRs recall is an order of magnitude worse.
- Live ART demo (RDR-002 "Fix Vision→Language Priming in CrossModalIntegrationTest"): the linker produced exactly one edge — to `CrossModalIntegrationTest.java`. Semantic search surfaced three additional production classes (`LanguagePrimingSignal.java`, `LanguageToVisionPipeline.java`, `Phase4CrossModalPrimingTest.java`) that implement the RDR. None were linked because their filenames share no tokens with the RDR title.

The coverage gap is real, not theoretical — the filename heuristic cannot see through paraphrase. Every richer link generator we might build has to compute *something* that overlaps with raw semantic similarity. That compute already exists: RDR-077's `topic_assignments` table.

### Problem 2: Projection graph and link graph are disconnected

RDR-077 wired a full cross-collection projection pipeline. As of live state, `topic_assignments` holds projection rows with raw cosine `similarity`, `assigned_at`, and `source_collection`. The ICF-weighted threshold filter on write already gates out low-quality matches. `compute_icf_map()` exposes the hub-suppression signal at query time.

But nothing promotes those rows to catalog edges. A code chunk that projects into an RDR topic with adjusted similarity 0.82 is semantic-implements-level evidence and it sits in a SQLite table that `query(follow_links=...)` will never traverse.

The two graphs share the same node identifiers (tumblers, doc_ids, collection names). The bridge is mechanical.

### Problem 3: `search` and `query` MCP tools expose orthogonal subsets of the engine

Both tools call `search_cross_corpus()`. Neither exposes all of its capability.

| Capability | `search` (chunk-level) | `query` (document-level) |
|---|---|---|
| Corpus selection | yes | yes |
| Metadata filter (`where=`) | yes | yes |
| Topic pre-filter (`topic=`, RDR-070) | yes | no |
| Cluster output (`cluster_by="semantic"`) | yes | no |
| Pagination (`offset`) | yes | no |
| Catalog author filter | no | yes |
| Catalog content_type filter | no | yes |
| Catalog subtree filter | no | yes |
| Link traversal (`follow_links`, `depth`) | no | yes |

An agent that wants "RDRs linked to the current module, containing chunks in topic Y" cannot express it in one call. They must: (a) `query(follow_links=..., subtree=...)` to get the linked set, (b) `search(topic=...)` across the result's collection set, (c) intersect client-side. The two-call pattern loses topic-boost ranking fusion because neither tool knows about both constraints.

This is feature bifurcation, not feature redundancy. The tools differ in output granularity (chunks vs best-chunk-per-document); they should *not* differ in the retrieval constraints they accept.

### Problem 4: Agents don't reach for the graph for design, review, or analysis

`nx:query` exists and dispatches the query-planner agent — but by design only for *novel analytical pipelines* (multi-step extract / compare / generate). For the five everyday scenarios the user described — design/planning, critique/review, analysis/synthesis, dev/debug, documentation — there is no skill that tells the agent "call `query(follow_links=..., depth=2, topic=..., subtree=...)` before writing anything."

The primitives are usable today; the *reach* is the gap.

## Proposed Design

Three phases. Each stands alone and each unlocks value independently, but they compose.

### Phase 1: Projection → Catalog link promotion

New link generator `generate_semantic_implements_links(catalog, taxonomy)` in `src/nexus/catalog/link_generator.py`. Runs as part of `nx catalog link-generate`.

**Input:** `topic_assignments` rows where `assigned_by = 'projection'` and `source_collection IS NOT NULL`.

**Filter:** ICF-weighted adjusted similarity above a per-corpus-type floor. Reuses `CatalogTaxonomy.compute_icf_map()` so gate values stay consistent with what the projection writer itself uses. Rows whose target topic has ICF below a minimum threshold (i.e., hubs) are excluded — generic topics should not produce bridging edges.

**Mapping:** each qualifying row `(doc_id, topic_id, similarity, source_collection)` becomes:
- From: catalog entry whose `physical_collection == source_collection` and whose doc-level identity covers `doc_id` (the file the chunk belongs to).
- To: catalog entry that represents the target topic — pick the RDR/doc entry with the highest `doc_count` in the topic's owning collection.
- Link type: `semantic-implements` (new).
- `created_by`: `projection`.
- Metadata: `{raw_similarity, adjusted_similarity, source_chunk_id, topic_id}` — carried for later audit and recomputation.

**Idempotency:** prefer-higher UPSERT on `(from, to, link_type)`. Re-running the generator updates similarity without duplicating edges. Mirrors the RDR-077 Phase 2 write-path prefer-higher invariant.

**Chunk→document aggregation:** many chunks of the same file may project into the same topic. The generator aggregates by `(source_file, target_entry)` and emits one edge carrying `max(adjusted_similarity)` across the chunk set. Raw per-chunk evidence stays in `topic_assignments`; the catalog layer holds the aggregated judgment.

**Why a new link type and not `implements`:** agents and humans judge evidence differently. `implements` is a hand-curated assertion. `implements-heuristic` is filename overlap — cheap signal, high precision, low recall. `semantic-implements` is projection-derived — medium precision, high recall. Keeping them distinct lets `query(follow_links=...)` scope by confidence.

### Phase 2: Retrieval surface alignment

Additive. No deprecation. No breakage.

**Add to `search()` MCP tool** (chunk-level):
- `author: str = ""` — catalog author filter.
- `content_type: str = ""` — catalog content-type filter.
- `subtree: str = ""` — tumbler prefix scope.
- `follow_links: str = ""` + `depth: int = 1` — link graph expansion before semantic search.

**Add to `query()` MCP tool** (document-level):
- `topic: str = ""` — topic label pre-filter / boost (mirrors `search()`).
- `cluster_by: str = ""` — optional Ward/semantic grouping of documents.
- `offset: int = 0` — proper pagination.

**Default corpus alignment:** both tools default to `"knowledge,code,docs,rdr"`. (Currently `search` defaults to three, `query` to one — inconsistent.)

**Shared sub-engine:** factor the catalog-routing logic in `query()` into `nexus.search_engine.resolve_catalog_collections()` so `search()` can reuse it verbatim. The topic pre-filter is already in `search_cross_corpus()`; exposing it on `query()` is a one-line passthrough.

**Fusion ranking** (RDR-070 already has the machinery): when both topic filter and link traversal are set, the engine pre-filters to linked collections, applies topic boost during ranking, and returns results in fused distance order. No new math — the combinations are independent filters over the same chunk set.

**The output-granularity distinction remains the one reason to pick one tool over the other:** `search` for "where are the matching pieces," `query` for "which documents match." Every other retrieval constraint works on both.

### Phase 3: Scenario skills + session/subagent priming

A skill that nobody invokes is dead markdown. Phase 3 has three surfaces: the skills themselves, the session-start hook that primes the main agent, and the subagent-start hook + per-agent system prompts that prime spawned agents for the five scenarios.

#### 3a: Five scenario skills

One markdown file each under `nx/skills/`. Each teaches the agent exactly which `search()` / `query()` shape fits one of the scenarios the user identified. Triggers + templates, not new code.

| Skill | Scenario | Trigger | Core instruction |
|---|---|---|---|
| `nx:research-plan` | Design / arch / planning | User asks to build a plan, design a feature, write an RDR, extend a subsystem | Before writing: `query(question=..., follow_links="semantic-implements", depth=2, subtree=<module>)` to gather prior art + linked code. Then `search(topic=..., corpus="rdr")` for related decisions. |
| `nx:review-context` | Critique / audit / review | User asks to review a PR, audit a design, critique a document | Before critique: `query(question=<summary>, follow_links="implements,semantic-implements,supersedes", depth=2)` to surface authoring decisions, prior art, related incidents. |
| `nx:analyze-corpus` | Analysis / synthesis / research | User asks to analyze across a corpus, compare approaches, synthesize findings | `query(question=..., topic=..., follow_links="cites", depth=2)` with `subtree` scoping; consider dispatching `nx:query` planner for multi-step extract/compare. |
| `nx:debug-context` | Dev / debug | User asks why code looks a certain way, what an error means, how a subsystem works | `catalog links-for-file <path>` → RDRs explaining *why*; Serena for symbol navigation covers *what*. |
| `nx:doc-scope` | Documentation | User asks what code a doc should reference, what docs a code file needs | `query(question=..., follow_links="cites,documented-by", depth=1)` for existing references; `catalog suggest-links` for candidates. |

Each skill is ~50 lines of markdown with triggering-condition block, invocation template, rationale. Query-planner stays for analytical pipelines; these cover the 80% workflow that doesn't need decomposition.

#### 3b: Session-start priming (`nx/hooks/scripts/session_start_hook.py`)

The current "## nx Capabilities" block injected at session start lists `search`, `query`, `/nx:query`, plan library, scratch, catalog, enrichment. Extend with a new **"## Scenario Retrieval"** block listing each scenario skill with a one-line trigger keyword set, so the main agent reaches for the skill reflexively when a request fits the pattern. Example line:

> `nx:research-plan` — triggers on "plan", "design", "extend", "implement RDR". Invokes `query(follow_links="semantic-implements", depth=2)` before writing.

No new tokens-per-session beyond ~6 lines. The point is to put the skill names into the session context so the `using-nx-skills` trigger gate can fire on them.

#### 3c: Subagent priming (`nx/hooks/scripts/subagent-start.sh` + agent system prompts)

Two touch points per scenario agent:

- The grep-dispatched context-injection block in `subagent-start.sh` for the relevant agent gets a "Knowledge-graph-first preamble" — one paragraph telling the agent its first action on a new task is to walk the catalog/topic graph via `query(follow_links=..., depth=2, topic=..., subtree=...)` before reading files.
- The agent's own `nx/agents/<name>.md` frontmatter + opening instruction are edited to cite the same pattern — so the behavior survives when the hook context is trimmed.

Target agents (mapped to scenarios):

| Agent | Scenario served | Edit |
|---|---|---|
| `strategic-planner` | Design / plan (3a: `research-plan`) | "Before decomposing, call `query(follow_links=semantic-implements, depth=2)` to surface prior art + linked code." |
| `architect-planner` | Architecture | Same pattern, scoped to module-level `subtree`. |
| `code-review-expert` | Review (3a: `review-context`) | "Before reviewing, call `catalog links-for-file` on changed paths to load authoring RDRs and prior related decisions." |
| `substantive-critic` | Critique (3a: `review-context`) | Same as code-review-expert plus `follow_links=supersedes` to trace decision evolution. |
| `deep-analyst` | Deep analysis | "Before analysis, walk the graph: `query(follow_links=cites,depth=2, topic=...)`." |
| `deep-research-synthesizer` | Research (3a: `analyze-corpus`) | "First pass is graph-walk + topic pre-filter, not web search." |
| `debugger` | Debug (3a: `debug-context`) | "Before hypothesizing, `catalog links-for-file` on the failing module to load the RDR that shaped its design." |
| `plan-auditor` | Plan review | "Before auditing the plan, walk `follow_links=implements,supersedes` to confirm the plan respects existing decisions." |

Agents not listed (indexer-internal, pdf-processor, etc.) are out of scope — they're not retrieval-shaped.

#### 3d: Plan-library seeding (`plan_save` MCP tool)

Nexus already ships a plan library: `plan_save`/`plan_search` MCP tools over a T2 `plans` table, 5 builtin templates seeded at `nx catalog setup`. It's the right substrate for canonical `query()`/`search()`/`catalog` invocation patterns — tool-agnostic JSON describing retrieval steps, project-scoped, searchable by tags.

Seed one plan per scenario skill — **same names as the skills so `plan_search(scenario_key)` and the skill both resolve to the same pattern**. Each plan is a 2–4 step sequence:

| Plan name | Tags | Steps (abbreviated) |
|---|---|---|
| `research-plan` | `scenario,design,planning,rdr` | 1. `query(follow_links="semantic-implements", depth=2, subtree=<module>)` for prior art. 2. `search(topic=<topic>, corpus="rdr")` for related decisions. 3. `catalog suggest-links` for unlinked candidates. |
| `review-context` | `scenario,review,critique,audit` | 1. `catalog links-for-file` on changed paths. 2. `query(follow_links="implements,semantic-implements,supersedes", depth=2)` on each authoring RDR. 3. `search(topic=<area>, corpus="rdr")` for related incidents. |
| `analyze-corpus` | `scenario,analysis,synthesis,research` | 1. `query(topic=<topic>, follow_links="cites", depth=2, subtree=<scope>)` for the citation network. 2. If multi-step extract/compare needed, dispatch `nx:query` planner. |
| `debug-context` | `scenario,debug,dev` | 1. `catalog links-for-file <failing-path>` for the RDR that shaped the design. 2. Serena `jet_brains_find_symbol` on the offending symbol. 3. `search(topic=<error-domain>, corpus="code")` for sibling implementations. |
| `doc-scope` | `scenario,documentation` | 1. `query(follow_links="cites,documented-by", depth=1)` for existing references. 2. `catalog suggest-links` for unlinked candidates. |

Each plan's steps are the canonical multi-tool sequence — including when to fall through to `nx:query` for decomposition or Serena for symbol-level navigation. Plans are **project-scoped** (`project="nexus"` defaults; overridden per-repo) so per-project overrides can refine the `subtree` or corpus scope.

The `query-planner` agent already consumes `few_shot_plans` from the library (per its relay contract). Seeding these scenario plans makes them available as priors for the planner's decomposition, so even the "novel analytical pipeline" path benefits from the scenario patterns.

One seeding script under `src/nexus/mcp_infra.py` or a new `src/nexus/catalog/scenario_plans.py`, run by `nx catalog setup` alongside the existing 5 builtin seeds. Upgrade-safe — reseed is idempotent via `plan_save` prefer-latest semantics.

## Success Criteria

- **SC-1** — `generate_semantic_implements_links` generator lands in `link_generator.py` with per-corpus-type ICF-adjusted similarity thresholds (code__* 0.60, knowledge__* 0.45, docs__*/rdr__* 0.50; these are starting points — PQ-1).
- **SC-2** — Running `nx catalog link-generate` on a workspace with established projection (run `nx taxonomy project --backfill --persist` first if absent) increases **per-workspace** code-entry→authoring-RDR coverage from the baseline measured in RF-1 (0.8% ART, 7.9% nexus) to ≥ 50% **where projection data exists**, without introducing more than 5% false-positive bridges (measured against a 50-pair hand-labeled audit set). For workspaces with no projection rows yet (ART at the time of this RDR) Phase 1 is a no-op — its precondition is RDR-077-style projection having run.
- **SC-3** — `semantic-implements` edges carry metadata: `raw_similarity`, `adjusted_similarity`, `source_chunk_id`, `topic_id`. `nx catalog show <tumbler>` renders them distinctly from `implements-heuristic`.
- **SC-4** — `search()` accepts `author`, `content_type`, `subtree`, `follow_links`, `depth` with the same semantics as `query()`. Same question against the same collection set with the same catalog filter returns overlapping results between the two tools (chunk subset ⊆ document's chunk pool).
- **SC-5** — `query()` accepts `topic`, `cluster_by`, `offset`. A `topic=` pre-filter on `query()` produces the same document set that `search(topic=...)` followed by doc-level grouping would.
- **SC-6** — Both tools default to `"knowledge,code,docs,rdr"`.
- **SC-7** — Five scenario skills ship in the `nx` plugin. Each has a triggering-condition block, an invocation template, and a worked example. `plugin-dev:skill-reviewer` passes each one.
- **SC-8** — RDR-002 ART live re-demo: `query(question="vision language priming", follow_links="semantic-implements", depth=2, subtree="1.11")` returns RDR-002 **plus** `LanguagePrimingSignal.java`, `LanguageToVisionPipeline.java`, and `Phase4CrossModalPrimingTest.java` in one call. Current live result: RDR-002 and one test file.
- **SC-9** — Prefer-higher UPSERT on `semantic-implements` edges preserved across re-runs. Lowering the ICF gate threshold does not delete existing higher-confidence edges.
- **SC-10** — Zero regressions: full test suite green before tagging v4.4.0.
- **SC-11** — Session and subagent hooks prime the scenario skills. The `session_start_hook.py` "## Scenario Retrieval" block lists all five skills with triggers. The `subagent-start.sh` grep dispatch injects a knowledge-graph-first preamble for each of the 8 target agents (strategic-planner, architect-planner, code-review-expert, substantive-critic, deep-analyst, deep-research-synthesizer, debugger, plan-auditor). Each target agent's `nx/agents/<name>.md` opening instruction cites the pattern independently of the hook — verifiable by grep.
- **SC-12** — Five scenario plans land in the plan library via a seeding script run by `nx catalog setup`. `plan_search(scenario_key)` resolves each of `research-plan`, `review-context`, `analyze-corpus`, `debug-context`, `doc-scope` to a canonical multi-tool invocation sequence. Plans are project-scoped (default `nexus`) and reseed is idempotent. The `query-planner` agent consumes them as `few_shot_plans` priors.

## Research Findings

- **RF-1** — Empirical per-workspace heuristic recall is an order of magnitude worse than the aggregate `nx catalog coverage` number suggested. Measurement at 2026-04-14 (stored as `nexus_rdr/078-research-1`): ART workspace 0.8% (35/4,180 code files with an RDR/docs link); nexus workspace 7.9% (22/280). Projection coverage where it exists is strong — nexus's 402 persisted projection rows from the RDR-077 shakeout cover ~134 unique source files (≈48%). **Precondition gap:** ART has zero projection rows because `nx taxonomy project` has never been run on it. Phase 1 has no effect on a workspace until projection has been populated; the plan-of-record must sequence projection-first-then-link-promote. A 50-pair hand-curated audit set for precision measurement remains Phase 1 pre-work.
- **RF-2** — ICF gating already handles the hub-suppression problem. Topics with `ICF == 0` (DF == N_effective) contribute nothing to `semantic-implements` promotion because `adjusted_similarity = raw * ICF = 0 < threshold` for any `threshold > 0`. The hub problem that motivated RDR-077 is exactly the noise this generator must not amplify, and RDR-077's math does the right thing by construction.
- **RF-3** — `search_cross_corpus()` (`src/nexus/search_engine.py:152-163`) accepts `topic`, `catalog`, `taxonomy`, `cluster_by`, `link_boost` kwargs — the underlying engine is capability-complete for both tool surfaces. However, the "passthrough" shorthand in the earlier draft over-simplified: `topic` carries non-trivial logic in the engine body (`search_engine.py:191-204`) — a `taxonomy.get_doc_ids_for_topic()` lookup, an ID-set build, and a 500-ID pre/post-filter cap. `search()` (`mcp/core.py:109-116`) already wires it; `query()` (`mcp/core.py:330-334`) does not pass `topic` at all. Phase 2 requires an explicit one-line addition in `query()` to forward `topic=topic or None` into `search_cross_corpus`, plus a doc-level result dedup pass over the 500-ID cap boundary. `catalog` and `taxonomy` are genuine passthroughs. `follow_links` on `search()` needs the catalog-collection-resolution logic from `mcp/core.py:240-291` factored into a shared helper; that code is not currently a library function.
- **RF-4** — Link type disambiguation is supported by the existing catalog model. `link_generator.generate_citation_links` and `generate_code_rdr_links` write distinct `link_type` values. Adding `semantic-implements` is additive.
- **RF-5** — The chunk→file aggregation step matters. 63,101 chunks projected in the live demo; a per-chunk link emission would produce thousands of edges per file pair. Per-`(source_file, target_entry)` aggregation with `MAX(adjusted_similarity)` keeps the graph navigable and preserves the evidence pointer to the top chunk.
- **RF-6** — The user's own observation: "the query planner was supposed to be doing this with the knowledge graph." The primitive is already there (`query(follow_links=..., depth=N)`); what makes it feel absent is that the *graph being walked* is sparse. Phase 1 densifies the graph. Phase 2 widens the surface that reaches it. Phase 3 trains the agent layer.
- **RF-7** — Catalog JSONL uses field names `from_t` / `to_t` for link endpoints (`from_tumbler` / `to_tumbler` are the SQLite cache column names only). Implementation note for Phase 1: the link-generator emits through the catalog API, which handles the translation — but any test fixture or audit tool reading `links.jsonl` directly must use the JSONL field names.

## Proposed Questions

- **PQ-1** — Starting per-corpus-type ICF-adjusted similarity thresholds are chosen conservatively. Calibration against RF-1's hand-labeled set in implementation may move them. Threshold table becomes a tunable — possibly `.nexus.yml` exposure in a follow-on RDR (same PQ-2 open question that RDR-077 left unresolved).
- **PQ-2** — Should `semantic-implements` edges decay? Centroid drift after re-discover can leave stale edges. Options: (a) re-run generator on `taxonomy-meta.last_discover_at` change, (b) TTL on the edge, (c) lazy recomputation during `nx catalog link-audit`. Defer to implementation review.
- **PQ-3** — The chunk→file aggregation picks `MAX(adjusted_similarity)` as the edge weight. Alternative: `AVG` (more stable, less sensitive to outliers) or `weighted_sum` (bias toward file pairs with many corroborating chunks). MAX is simplest and easiest to explain; if query-time ranking suffers we revisit.
- **PQ-4** — `documented-by` link type appears in the `nx:doc-scope` skill example but is not proposed here. Do we need it as a distinct type, or is `cites` sufficient? Split on clarity of agent guidance; leave for a follow-up RDR once the skill lands and tells us what shape the gap takes.
- **PQ-5** — Scenario skills may overlap with the existing `nx:research-synthesis` skill. Audit that one during Phase 3 and either absorb or cross-reference. Don't duplicate.
- **PQ-6** — Should the surface alignment deprecate `query()` or `search()` in the long run? This RDR says no; every call site that picks one over the other does so on output granularity, which is a real distinction. A future RDR may revisit if the duplication costs exceed the readability benefit. For now, alignment is cheaper than unification.

## Related

- **RDR-063** — Catalog domain split and link-graph origins.
- **RDR-070** — HDBSCAN topic discovery; `topic` pre-filter and boost machinery this RDR extends to `query()`.
- **RDR-075** — Cross-collection projection plumbing (what RDR-077 added similarity scores to).
- **RDR-077** — Projection quality, similarity storage, ICF computation. This RDR's Phase 1 is the logical continuation — promoting RDR-077's signal into the graph `query()` walks.
- **RDR-053** — Xanadu-in-Nexus link-graph design doctrine.

## Out of Scope (deferred)

- Unified `retrieve()` tool with `mode="chunks"|"documents"` (option 2 from the design discussion). Reviewed; rejected for this iteration because feature alignment (option 1) delivers the same agent capability without breakage.
- `query`-planner agent changes. The existing analytical-pipeline agent stays unchanged; scenario skills cover the 80% workflow that doesn't need decomposition.
- Link graph visualization / UI.
- Per-project configuration of hub stopwords (PQ-3 from RDR-077).
