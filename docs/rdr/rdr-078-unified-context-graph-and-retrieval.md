---
title: "RDR-078: Plan-Centric Retrieval — Semantic Plan Matching, Typed-Graph Traversal, Scenario Plans"
status: draft
type: feature
priority: P2
created: 2026-04-14
related: [RDR-042, RDR-050, RDR-053, RDR-070, RDR-077]
reviewed-by: self
---

# RDR-078: Plan-Centric Retrieval

Pickup of the two explicit deferrals in RDR-042, plus the reasoning mechanism that AgenticScholar attributes its quality delta to but RDR-042 did not ship: **multi-hop typed-link traversal as a first-class plan operator.** That RDR shipped the analytical-operator agent, plan library (`plans` table + FTS5), the `/nx:query` skill, and the self-correction loop. It deferred a semantic layer over the plan library ("Can add T3 semantic layer later if FTS5 matching proves inadequate") and skipped taxonomy-driven planning ("may revisit via lightweight clustering in a future RDR"). RDR-070 shipped HDBSCAN taxonomy discovery across both code and prose corpora; RDR-077 shipped similarity-aware projection quality signals. The blockers RDR-042 cited are now absent.

The center of gravity is the **plan** — a saved query+DAG pair — composed of steps that each operate in one embedding space or walk explicit typed edges between documents. No cross-embedding bridging is attempted; the architecture never requires it. The quality lever is the typed-link traversal step, not the domain-scoped retrieval step.

## Problem

### Problem 1: Plan library is write-only for semantic intent

RDR-042's plan library stores `(query, plan_json, outcome, tags, project, ttl, created_at)` and exposes FTS5 search over the query text (`plan_search`). FTS5 matches *tokens*; it does not match *intent*. Two paraphrased queries that ought to resolve to the same plan typically miss — the agent re-decomposes, and the library grows with near-duplicate entries.

AgenticScholar's plan reuse mechanism is specifically semantic vector match over prior `(query, plan)` pairs with a confidence gate. RDR-042 documented this as a known deferral (§Alternatives: "T3 for plan storage (deferred)"). Two conditions were set for pickup: (a) evidence that FTS5 underperforms, and (b) infrastructure to embed queries. Both are met.

The reuse win is explicitly an **efficiency** story — AgenticScholar reports ~40% compute cost reduction at ≥90% match confidence (RF-1). It is not a quality story. That matters for framing: Phase 1 pays for itself in agent thrash avoidance, not in better answers.

### Problem 2: Plan steps have no typed-graph traversal operator

Current plan JSON encodes retrieval and analytical operators (`search`, `extract`, `summarize`, `rank`, `compare`, `generate`). None of them walk the catalog link graph. Yet the catalog already holds ~16,500 typed edges across `cites`, `implements`, `implements-heuristic`, `supersedes`, `relates` — with BFS traversal already exposed via `query(follow_links=...)` and `nx catalog links`. The machinery exists; it is not composable into a plan DAG.

This is the load-bearing gap for the user's intent. The verb list — **create / debate / collate / relate / integrate** — hinges on "relate" being a real plan operation. The paper's NDCG@3 = 0.606 vs RAG's 0.411 (+47%) gain is qualitatively attributed to multi-hop KG traversal (Tier-3 analytical reasoning), not to plan reuse or taxonomies alone (RF-1). A plan runner that cannot compose a `traverse` step cannot perform the cross-document reasoning that distinguishes analytical retrieval from flat retrieval.

### Problem 3: Plan steps cannot scope by domain taxonomy

The plan schema encodes tool arguments but has no *domain-scope* specifier. A plan for "find prior art on priming" cannot express "take the topic `vision-language priming` from the prose taxonomy and the topic `LanguagePrimingSignal` from the code taxonomy and return both, each scoped to its native embedding space." It can only express a flat `search` that either hits one corpus set or the other — losing the alignment that the two independently-discovered taxonomies naturally expose.

This is a narrower problem than Problem 2. Taxonomies scope *individual* retrieval steps; typed-graph traversal is what composes *across* steps. Both gaps coexist; the paper leans on traversal for quality, taxonomies for scope coherence.

### Problem 4: Scenario-shaped reuse is unimplemented

Five canonical scenarios recur across every nexus session: design/planning, critique/review, analysis/synthesis, dev/debug, documentation. Each has a stereotyped retrieval shape. Today each session reinvents the retrieval DAG ad-hoc, even when a prior session three days ago solved the same pattern. Infrastructure exists — `plan_save`, `plan_search`, `/nx:query` — but the library is empty of what scenario-matched queries would need. Agents don't reach for the library because the library does not yet answer to the scenarios.

## Proposed Design

Five phases. Each builds on RDR-042's shipped substrate. Phase 3 is new to this revision and carries the paper's quality lever.

### Phase 1: Semantic plan matching (`plan_match` MCP tool)

Pickup of RDR-042 §Alternatives "T3 for plan storage (deferred)".

- New T3 collection `plans__semantic`: embed each plan's `query` field via the standard index-time embedding pipeline. One embedding per plan row. Plan metadata (`project`, `tags`, `ttl`) carried as ChromaDB metadata for filter.
- Write path: `plan_save` gains a post-commit side effect — embed the query and upsert to `plans__semantic`. Failure is logged but not fatal (FTS5 remains the source of truth; semantic is a cache).
- Read path: new MCP tool `plan_match(query, project=None, n=5, min_confidence=0.0, outcome='success')` returns `[(plan_id, plan_query, confidence, plan_json), ...]`. Confidence is cosine similarity. Default filter excludes failed plans.
- Deletion path: `plans.close` TTL purge deletes the matching `plans__semantic` row. Idempotent.
- The existing `plan_search` FTS5 tool is untouched — it remains the fast path for tag/project filters and exact token matches.

**Expected benefit:** ~40% compute cost reduction on scenario-matched queries once the library is populated (RF-1, efficiency not quality).

### Phase 2: Domain-scoped retrieval steps

Extend the plan step schema (currently `{tool, args}`) with an optional `scope` field:

```json
{
  "tool": "search",
  "args": {"query": "priming visual-to-language", "corpus": "knowledge,docs,rdr"},
  "scope": {
    "taxonomy_domain": "prose",
    "topic": "vision-language priming"
  }
}
```

- `taxonomy_domain`: `prose` | `code`. Selects which HDBSCAN-discovered topic tree's labels are valid for `topic=`. Prose covers `knowledge__*` / `docs__*` / `rdr__*` / `paper__*`; code covers `code__*`. Each operates in its native embedding space; no cross-model arithmetic.
- Steps with `scope.taxonomy_domain = code` forward `topic=` to `search()` / `query()` over code corpora only. Same for prose.
- A plan expresses dual-taxonomy operations as **two separately-scoped steps** joined by a downstream step (`compare`, `summarize`, or a `traverse` — see Phase 3) or by document-set intersection at the plan-runner level.

**Framing** (per RF-2): this phase is a scope primitive, not a reasoning primitive. It prevents cross-embedding-model cosine from sneaking in and keeps each retrieval step in its own well-defined space. The reasoning happens in the operators that consume the scoped results (Phase 3, plus RDR-042's analytical operators).

### Phase 3: Catalog-Traverse as a first-class plan operator

The quality lever. Exposes the catalog's existing typed-edge BFS as a composable plan step.

New plan tool `traverse` with args:

```json
{
  "tool": "traverse",
  "args": {
    "seeds": ["1.11.2"] | {"tool": "search", "args": {...}, "emit": "tumblers"},
    "link_types": ["implements", "implements-heuristic", "cites"],
    "depth": 2,
    "direction": "out|in|both",
    "return": "entries|collections|both"
  }
}
```

- **Seeds** — either a literal list of tumblers or a reference to a prior step's output (by `step_N` variable, same pattern RDR-042 already uses for multi-step plans).
- **Link types** — one or many of the catalog's typed-edge vocabulary. Filtering at traversal time produces meaningfully different neighbourhoods: `implements` for code-implementation hops, `cites` for reference chains, `supersedes` for decision-evolution, `relates` for soft connections.
- **Depth** — BFS depth. Capped at 3 to bound cost; typical usage is 1 or 2 (RF-1 attributes the paper's Tier-3 reasoning to 2-hop traversal).
- **Direction** — follow outbound, inbound, or both. Defaults to `both` for the common "what's in this node's neighbourhood" intent.
- **Return shape** — `entries` (catalog rows), `collections` (deduped physical_collections, for use as scope in a downstream `search`), or `both`.

**Implementation** reuses `Catalog.graph()` (already in `src/nexus/catalog/catalog.py` per `nx catalog links --help`). No new storage, no new algorithm. The work is exposing the existing BFS as a `{tool: "traverse"}` plan step, plus contract tests against the catalog's existing link data.

**Composability examples**:

- *Research a concept* — `search(topic=X)` → `traverse(seeds=$step1, link_types=[implements,cites], depth=2)` → `summarize(cited=true)`. Finds seed documents, walks their implementation and citation graph, summarises with provenance.
- *Review a change* — `catalog-links-for-file($changed_paths)` → `traverse(seeds=$step1, link_types=[supersedes,relates], depth=1)` → `extract(template=decision_schema)` → `compare(baseline=$plan_baseline)`. Pulls authoring RDRs, walks decision evolution, extracts the effective decisions, compares to what the change does.
- *Integrate findings* — `search(topic=prose-X)` + `search(topic=code-Y)` + `traverse(seeds=$step1_tumblers ∪ $step2_tumblers, link_types=[implements], depth=1)` → `generate(template=synthesis, cited=true)`. Dual-taxonomy retrieval stitched by typed-link traversal.

**Expected benefit (RF-1):** this is where the paper's +47% NDCG quality delta is attributed. Honest estimate: transfer is *qualitative* — nexus's mixed corpus, non-scholarly taxonomy, and different link type vocabulary mean the magnitude will differ. What should transfer is the *kind* of result: plans that previously required ad-hoc multi-agent orchestration will run as deterministic DAGs, with the cross-document connections the user's verb list (especially "relate" and "integrate") requires.

### Phase 4: Scenario plans seeded at setup

Five plans, seeded by `nx catalog setup` alongside the existing 5 builtin plan templates from RDR-042. Each plan is a 2-5 step DAG exercising Phase 2 scoping and Phase 3 traversal where the scenario calls for it.

| Plan name | Scenario | DAG sketch |
|---|---|---|
| `research-plan` | Design / arch / planning | `search` prose (topic=concept) → `traverse` (link_types=[implements,cites], depth=2) → `search` code (topic=concept, subtree=returned_collections) → `summarize` with citations. |
| `review-context` | Critique / audit / review | `catalog-links-for-file(changed_paths)` → `traverse` (link_types=[supersedes,relates], depth=1) → `extract` decisions → `compare` decisions vs. changed code. |
| `analyze-corpus` | Analysis / synthesis / research | `search` prose (topic=area) → `search` code (topic=area) → `traverse` both (link_types=[implements,cites], depth=2) → `rank` by criterion → `generate` synthesis with citations. |
| `debug-context` | Dev / debug | `catalog-links-for-file(failing_path)` → `traverse` (link_types=[supersedes], depth=1) for authoring-RDR history → `summarize` design context. Serena handles symbol-level separately. |
| `doc-scope` | Documentation | `search` prose (follow_links=cites) for existing references → `search` code (topic=area) → `traverse` (link_types=[cites,documented-by], depth=1) → `compare` for doc-coverage gaps. |

Plan names match the skill names (Phase 5) so `plan_match("research this concept")` and the `nx:research-plan` skill both resolve to the same DAG. Seeding via `nx catalog setup` is idempotent (existing plans with the same `(project, name)` are updated, not duplicated).

### Phase 5: Plan-first priming (skills + hooks + agent prompts)

The ergonomic change. Agents must reach for `plan_match` **before** decomposing any retrieval task.

- **`nx:plan-first` skill** — invoked at the top of every research/review/analyze/debug/doc task. Triggers on verbs like "plan", "design", "review", "analyze", "debug", "document". Instructs: call `plan_match(query, min_confidence=0.85)` first; if a match exists, present it and execute; if not, dispatch `/nx:query` (the query-planner) and save the result.
- **Five scenario skills**: `nx:research-plan`, `nx:review-context`, `nx:analyze-corpus`, `nx:debug-context`, `nx:doc-scope`. Each is a thin wrapper that calls `plan_match("<scenario-name> <user-query>")` with a bias toward the matching scenario plan. Names intentionally match the seeded plan names.
- **SessionStart hook** (`nx/hooks/scripts/session_start_hook.py`) — add a "## Plan Library" block to the injected context listing `plan_match` / `plan_save` / `plan_search` and the five scenario names. Extends the existing "## nx Capabilities" section.
- **SubagentStart hook** (`nx/hooks/scripts/subagent-start.sh`) — for the eight retrieval-shaped agents (strategic-planner, architect-planner, code-review-expert, substantive-critic, deep-analyst, deep-research-synthesizer, debugger, plan-auditor), inject a "plan-match-first" preamble: *before decomposing any retrieval task, call `plan_match(query, min_confidence=0.85)`; execute the returned plan if match confidence clears the threshold.*
- **Per-agent `nx/agents/<name>.md`** — each target agent's opening instruction cites the `plan_match`-first pattern independently of the hook, so behavior survives hook-context trimming.

## Success Criteria

- **SC-1** — `plan_match` MCP tool lands. Given a plan with query *"how does projection quality work"* saved to the library, `plan_match("what's the mechanism for projection quality hub suppression")` returns that plan with cosine confidence > 0.80. Exact-token variants return the plan from FTS5 (`plan_search`) with equivalent or higher confidence.
- **SC-2** — Plan embedding is upserted on `plan_save` and deleted on TTL purge. T3 collection `plans__semantic` stays in sync with the T2 `plans` table (`COUNT(plans) == COUNT(plans__semantic)` after a reindex sweep).
- **SC-3** — Plan step schema accepts the `scope` field with `taxonomy_domain` ∈ {`prose`, `code`} and per-domain `topic=`. The plan runner forwards scope to the correct retrieval tool and corpus set. Cross-embedding cosine is never computed; verifiable by grep.
- **SC-4** — Plan step schema accepts `{tool: "traverse", args: {seeds, link_types, depth, direction, return}}`. Depth is capped at 3. `seeds` accepts both literal tumbler lists and `$step_N` references. The runner resolves both cases and returns the agreed shape.
- **SC-5** — `traverse` operator uses `Catalog.graph()` for BFS; no new graph-walking code. Returning `collections` from a traverse step usable as `subtree=` / explicit `corpus=` input to a downstream retrieval step is end-to-end tested.
- **SC-6** — Five scenario plans seed via `nx catalog setup`. Each uses at least one `traverse` step (except where the scenario is intentionally flat — `debug-context` is the only candidate). Reseeding is idempotent.
- **SC-7** — Session-start hook injects a "## Plan Library" block listing `plan_match`, `plan_save`, `plan_search`, and the five scenario names. SubagentStart hook injects the plan-match-first preamble for the eight retrieval-shaped agents. Each agent's `nx/agents/<name>.md` cites the pattern independently (verifiable by grep).
- **SC-8** — End-to-end demo on ART repo: fresh session, user asks *"how does vision→language priming work in ART?"*, `nx:plan-first` skill fires, cold-library case runs `/nx:query` planner → plan saved; warm-library case resolves from `plan_match` with confidence ≥ 0.80 and executes the saved DAG. At least one step in the resulting plan is a `traverse` that walks from the RDR to its implementing code via typed links.
- **SC-9** — Zero regressions. `plan_save` / `plan_search` / `/nx:query` unchanged in behavior for existing callers. `search()` / `query()` existing arg sets unchanged in behavior.
- **SC-10** — Cross-embedding boundary is not crossed anywhere in the plan runner. Every retrieval step operates in exactly one embedding space. `traverse` operates on catalog tumblers (no embeddings involved). Verifiable by grep.

## Research Findings

- **RF-1** — AgenticScholar paper attribution (via `nexus_rdr/078-research-2` audit against `knowledge__agentic-scholar`, 172 chunks). The paper's +47% NDCG@3 gain over RAG has no clean ablation. Qualitatively, the paper attributes its Tier-3 analytical reasoning quality to the **KG Traverse operator** over taxonomy-anchored typed edges — not to plan reuse or taxonomy construction alone. Plan reuse is documented explicitly as an *efficiency* story (~40% compute-cost reduction at ≥90% match confidence), not a quality story. Direct implication for this RDR: Phase 1 delivers cost, Phase 3 delivers quality. Framing Phase 2 as the quality layer overclaims.
- **RF-2** — Cross-embedding-model cosine is noise (measured 2026-04-14). ART code projected against ART prose at threshold 0.7 produced zero matches in 63,101 × 736 centroid comparisons; all 12,328 ≥0.7 matches were code↔code. `voyage-code-3` and `voyage-context-3` live in disjoint vector spaces. This rules out any projection-as-cross-corpus-bridge mechanism. Plans must compose per-space steps; no attempt to bridge embeddings belongs in this RDR. Phase 2's scope primitive is what prevents this from sneaking in.
- **RF-3** — `--use-icf` amplification bootstrap failure (measured 2026-04-14). ART backfill with `--use-icf` at threshold 0.7 wrote 189,303 assignments, all into 9 boilerplate Java/TypeScript topics; raw cosine avg 0.50, below the nominal threshold. Mechanism: `ICF = log2(N/DF) = log2(8/1) = 3.0` for DF=1 topics amplifies weak matches past threshold. This is an RDR-077 write-path finding that belongs in a separate post-mortem, but is cited here as part of the rationale for NOT using projection rows as a primary data source for plan-level retrieval.
- **RF-4** — RDR-042 plan library is FTS5 on `plans.query` + `plans.tags` (triggers defined at schema setup). Adding a T3 semantic cache does not modify the FTS5 path; additive. Plan rows have a stable `id` column usable as a ChromaDB document id.
- **RF-5** — `Catalog.graph(tumbler, depth, link_type)` already exists and drives `nx catalog links --from/--to`. Returns `{nodes, edges}`. Phase 3 wraps this in a plan-operator contract; no new graph code. The catalog SQLite cache (`.catalog/catalog.sqlite3`) makes BFS O(depth × edges) on modest link counts — 16,538 current edges traverse fast.
- **RF-6** — Stale projection-table state was wiped clean at RDR draft time: 633,820 `assigned_by='projection'` rows deleted (633,356 NULL-`source_collection` legacy + 464 session-demo + upgrade-backfill rows). HDBSCAN (238,593) and centroid (748) assignments preserved. This establishes a clean baseline for future write-path work; `plan_match` and `traverse` implementations never read `topic_assignments`, so the wipe has no effect on Phase 1-5.
- **RF-7** — Verb-list coverage check against the paper. The user's verbs are **create / debate / collate / relate / integrate**. Mapping:
  - *create* — emerges from `generate` (RDR-042 operator) consuming traversal results. Supported.
  - *debate* — the self-correction loop (RDR-042) plus the `substantive-critic` agent cover adversarial examination; not new in this RDR but available.
  - *collate* — `rank` + `compare` + `summarize` analytical operators (RDR-042). Already supported.
  - *relate* — **requires Phase 3 traverse**. Without it, "relate" degrades to co-retrieval within a scope (a DAG-level join that loses typed-edge semantics).
  - *integrate* — emerges from a multi-step plan: dual scoped retrieval + traverse + `generate` with citations. The composition is what integrates; no new primitive needed.
  The verb list is fully covered only with Phase 3 present.

## Proposed Questions

- **PQ-1** — Plan embedding model. Default is `voyage-context-3` (CCE) for the query text. Alternative: `voyage-3` (non-CCE, smaller cache footprint) for single-sentence intent matching. Calibrate during implementation.
- **PQ-2** — Plan-match confidence threshold. Default 0.85 cosine is the RDR-042-cited 90%-confidence reuse rule. Calibrate against a 20-query paraphrase set during Phase 1 implementation.
- **PQ-3** — Plan scope `taxonomy_domain` vocabulary. Starts with `prose` / `code`. Does `paper` merit its own domain, or fold into `prose`? Likely fold — same CCE model — but leave open.
- **PQ-4** — Traverse depth cap. Hardcoded 3 is a safety rail. Should it be configurable per-plan or per-project? Start hardcoded; revisit if a scenario needs more.
- **PQ-5** — Traverse `seeds` referencing `$step_N` requires consistent output shape across retrieval operators. What does `search` emit when a downstream `traverse` expects tumblers? Add `emit: "tumblers"` hint on the source step, or standardise the retrieval output to always carry tumblers. Lean toward the latter — the catalog already tracks tumbler-per-chunk via the `chunks` metadata.
- **PQ-6** — Failed-plan filter in `plan_match`. Default `outcome='success'` is correct. Should traverse-heavy plans carry a different success signal (e.g., "at least one typed-link traversal returned a non-empty neighbourhood")? Defer; the existing outcome field starts coarse.
- **PQ-7** — Drift between T2 `plans` and T3 `plans__semantic`. Opt-in periodic reindex via `nx catalog setup --reindex-plans`. Not required for first iteration.
- **PQ-8** — Scenario plan portability across projects. Plans ship project-neutral (templates); agent customises per-project before saving a project-scoped variant. Substitution mechanism (`$var` references in `plan_json`) needs a small runner contract.

## Out of Scope (deferred, each may spawn its own RDR)

- **Retrieval surface alignment** (`search()` ↔ `query()` parity) — plumbing debt, not paper-grounded. Plans compose tools as-is; agents call whichever tool fits each step. File as a separate tidy-up RDR if the duplication starts hurting.
- **Projection → catalog link promotion.** Within-same-model code↔code bridges are a sidequest. Cross-embedding projection is ruled out by RF-2 regardless.
- **Heuristic linker strengthening** (module/symbol/path extraction from RDR body). Would address the 0.8%/7.9% per-workspace heuristic-linker recall measured during the RDR-078 discovery sweep. Directly improves Phase 3 traversal neighbourhoods, so worth doing — but as its own RDR since it's independently scoped.
- **RDR-077 `--use-icf` bootstrap failure post-mortem.** RF-3 cites it; separate document in `docs/rdr/post-mortem/077-use-icf-bootstrap-amplification.md` should capture the mechanism.
- **Link graph UI.** Out of scope across all iterations.
- **Per-project configuration of hub stopwords** (RDR-077 PQ-3). Orthogonal.
- **New link types beyond the catalog's existing vocabulary.** `semantic-implements`, `documented-by`, `tests` were proposed in earlier drafts; Phase 3 works with the existing `implements` / `cites` / `relates` / `supersedes` / `implements-heuristic` set. Additional types can land later if scenario plans reveal concrete need.
