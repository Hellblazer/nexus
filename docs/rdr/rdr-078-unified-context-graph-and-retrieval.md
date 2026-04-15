---
title: "RDR-078: Plan-Centric Retrieval — Semantic Plan Matching, Dual-Taxonomy Planning, Scenario Plans"
status: draft
type: feature
priority: P2
created: 2026-04-14
related: [RDR-042, RDR-070, RDR-075, RDR-077]
reviewed-by: self
---

# RDR-078: Plan-Centric Retrieval

Pickup of the two explicit deferrals in RDR-042. That RDR shipped the analytical-operator agent, plan library (`plans` table + FTS5), the `/nx:query` skill, and the self-correction loop. It explicitly deferred a semantic layer over the plan library ("Can add T3 semantic layer later if FTS5 matching proves inadequate") and skipped taxonomy-driven planning ("may revisit via lightweight clustering in a future RDR"). RDR-070 shipped HDBSCAN taxonomy discovery across both code and prose corpora; RDR-077 shipped similarity-aware projection quality signals. The blockers RDR-042 cited are now absent. RDR-078 closes both deferrals in one iteration.

The center of gravity is the **plan** — a saved query+DAG pair — not the link graph, not projection, not cross-embedding similarity. Plans compose steps over each domain's taxonomy independently in its native embedding space. No cross-embedding bridging is attempted; the architecture never requires it.

## Problem

### Problem 1: Plan library is write-only for semantic intent

RDR-042's plan library stores `(query, plan_json, outcome, tags, project, ttl, created_at)` and exposes FTS5 search over the query text (`plan_search`). FTS5 matches *tokens*; it does not match *intent*. Two paraphrased queries that ought to resolve to the same plan typically miss — the agent re-decomposes, and the library grows with near-duplicate entries.

AgenticScholar's 40% cost reduction came specifically from **semantic plan reuse above a confidence threshold**. RDR-042 documented this as a known deferral (§Alternatives: "T3 for plan storage (deferred)"). Two conditions were set for pickup: (a) evidence that FTS5 underperforms, and (b) infrastructure to embed queries. Both are met — FTS5 is an exact-token index by design, and the RDR-070/077 taxonomy stack provides per-query embeddings on demand via the same ChromaDB client that indexes documents.

### Problem 2: Plans have no taxonomy-scope primitive

The current `plan_json` schema encodes ordered operator steps (`search`, `extract`, `summarize`, `rank`, `compare`, `generate`). Each step carries tool arguments but has no *domain-scope* specifier. A plan for "find prior art on priming" can't express "take the topic `vision-language priming` from the prose taxonomy and the topic `LanguagePrimingSignal` from the code taxonomy and return both with their linked context" — it can only express a flat `search` that either hits one corpus set or the other.

This forces the query-planner agent to emit plans that are either (a) prose-only, (b) code-only, or (c) a sequence of two independent flat searches with no shared scope. Option (c) is what gets produced, and it loses the natural alignment that the taxonomies expose.

### Problem 3: Scenario-shaped reuse is unimplemented

Five canonical scenarios recur across every nexus session: design/planning, critique/review, analysis/synthesis, dev/debug, documentation. Each has a stereotyped retrieval shape. Today each session reinvents the retrieval DAG ad-hoc, even when a prior session three days ago solved the same pattern. The infrastructure is in place — `plan_save`, `plan_search`, `/nx:query` — but there are no seed plans that match the five recurring scenarios. Agents don't reach for the library because the library is empty of what they'd need.

### Problem 4: Retrieval surfaces are bifurcated

`search()` MCP tool accepts `topic=` (taxonomy pre-filter, RDR-070) but not `follow_links=`/`subtree=`/`author=` (catalog routing, RDR-050). `query()` accepts the reverse. A plan step that wants "topic=X within collections linked to RDR-Y" can't express it in one tool call. This is additive feature bifurcation over the same engine (`search_cross_corpus`), not intentional separation — and it forces plans into unnecessary multi-step shapes.

## Proposed Design

Five phases. Each builds on RDR-042's shipped substrate.

### Phase 1: Semantic plan matching (`plan_match` MCP tool)

Pickup of RDR-042 §Alternatives "T3 for plan storage (deferred)".

- New T3 collection `plans__semantic`: embed each plan's `query` field via the standard index-time embedding pipeline. One embedding per plan row. Plan metadata (`project`, `tags`, `ttl`) carried as ChromaDB metadata for filter.
- Write path: `plan_save` gains a post-commit side effect — embed the query and upsert to `plans__semantic`. Failure is logged but not fatal (FTS5 remains the source of truth; semantic is a cache).
- Read path: new MCP tool `plan_match(query, project=None, n=5, min_confidence=0.0)` returns `[(plan_id, plan_query, confidence, plan_json), ...]`. Confidence is cosine similarity. The T3 collection uses the default embedding model (`voyage-context-3` for mixed-intent queries — agents' phrasing is prose-like even for code queries).
- Deletion path: `plans.close` TTL purge deletes the matching `plans__semantic` row. Idempotent.
- The existing `plan_search` FTS5 tool is untouched — it remains the fast path for tag/project filters and exact token matches.

### Phase 2: Dual-taxonomy plan DAG

Extend the plan step schema (currently `{tool, args}`) with an optional `scope` field:

```json
{
  "tool": "search",
  "args": {"query": "priming visual-to-language", "corpus": "knowledge,docs,rdr"},
  "scope": {
    "taxonomy_domain": "prose",
    "topic": "vision-language priming",
    "follow_links": "cites",
    "depth": 2
  }
}
```

- `taxonomy_domain`: `prose` | `code`. Selects which HDBSCAN-discovered topic tree's labels are valid for `topic=`. Prose covers `knowledge__*` / `docs__*` / `rdr__*` / `paper__*`; code covers `code__*`. Each operates in its native embedding space; no cross-model arithmetic.
- Steps with `scope.taxonomy_domain = code` forward `topic=` to `search()` / `query()` over code corpora only. Same for prose.
- A plan expresses the dual-taxonomy operation as **two scoped steps** joined by a downstream step (e.g., `compare` or `summarize`), or by document-set intersection at the plan-runner level. No new cross-embedding primitive.

The query-planner agent learns the pattern through its few-shot plan priors (Phase 3 seed plans are the examples).

### Phase 3: Scenario plans seeded at setup

Five plans, seeded by `nx catalog setup` alongside the existing 5 builtin plan templates from RDR-042. Each plan is a 2-5 step DAG spanning both taxonomies where the scenario calls for it.

| Plan name | Scenario | DAG sketch |
|---|---|---|
| `research-plan` | Design / arch / planning | `search` over prose (`topic=<concept>`, `follow_links="cites,implements"`) → `search` over code (`topic=<concept>`, `subtree=<module>`) → `summarize` the union with evidence citations. |
| `review-context` | Critique / audit / review | `catalog links-for-file` on each changed path → `search` over prose (authoring RDRs, `follow_links="supersedes,relates"`) → `extract` key decisions per RDR → `compare` decisions vs. changed code. |
| `analyze-corpus` | Analysis / synthesis / research | `search` over prose (`topic=<area>`) → `search` over code (`topic=<area>`) → `rank` both result sets by `criterion` → `generate` synthesis with citations. |
| `debug-context` | Dev / debug | `catalog links-for-file <failing-path>` → `search` over prose (authoring RDRs) → Serena `jet_brains_find_symbol` over failing code → `summarize` the design context + related RDRs. |
| `doc-scope` | Documentation | `search` over prose (`follow_links="cites"`) for existing references → `search` over code (`topic=<area>`) for candidate subjects → `compare` to find doc-coverage gaps. |

Plan names match the skill names (Phase 5) so `plan_match("research this concept")` and the `nx:research-plan` skill both resolve to the same DAG. Seeding via `nx catalog setup` is idempotent (existing plans with the same `(project, name)` are updated, not duplicated).

### Phase 4: Retrieval surface alignment

Additive, no breakage.

- **`search()`** gains: `author`, `content_type`, `subtree`, `follow_links`, `depth` (all catalog-routing knobs from `query()`).
- **`query()`** gains: `topic`, `cluster_by`, `offset` (all taxonomy/pagination knobs from `search()`).
- Both default corpus: `"knowledge,code,docs,rdr"` (currently inconsistent).
- Factor catalog-routing logic from `mcp/core.py:240-291` into `nexus.search_engine.resolve_catalog_collections()` so both tools share one implementation.
- `topic=` on `query()` requires forwarding the param plus doc-level dedup over the taxonomy's 500-ID cap boundary (RF-3 from prior analysis). Not a pure passthrough.

Output granularity (chunks vs. best-chunk-per-document) remains the reason to pick one tool over the other. Every other retrieval constraint is available on both.

### Phase 5: Plan-first priming (skills + hooks + agent prompts)

The primary ergonomic change. Agents must reach for `plan_match` **before** decomposing any retrieval task.

- **`nx:plan-first` skill** — invoked at the top of every research/review/analyze/debug/doc task. Triggers on verbs like "plan", "design", "review", "analyze", "debug", "document". Instructs: call `plan_match(query, min_confidence=0.85)` first; if a match exists, present it and execute; if not, dispatch `/nx:query` (the query-planner) and save the result.
- **Five scenario skills**: `nx:research-plan`, `nx:review-context`, `nx:analyze-corpus`, `nx:debug-context`, `nx:doc-scope`. Each is a thin wrapper that calls `plan_match("<scenario-name> <user-query>")` with a bias toward the matching scenario plan. Names intentionally match the seeded plan names.
- **SessionStart hook** (`nx/hooks/scripts/session_start_hook.py`) — add a "## Plan Library" block to the injected context listing `plan_match` / `plan_save` / `plan_search` and the five scenario names. Extends the existing "## nx Capabilities" section.
- **SubagentStart hook** (`nx/hooks/scripts/subagent-start.sh`) — for the eight retrieval-shaped agents (strategic-planner, architect-planner, code-review-expert, substantive-critic, deep-analyst, deep-research-synthesizer, debugger, plan-auditor), inject a "plan-match-first" preamble: *before decomposing any retrieval task, call `plan_match(query, min_confidence=0.85)`; execute the returned plan if match confidence clears the threshold.*
- **Per-agent `nx/agents/<name>.md`** — each target agent's opening instruction cites the `plan_match`-first pattern independently of the hook, so behavior survives hook-context trimming.

## Success Criteria

- **SC-1** — `plan_match` MCP tool lands. Given a plan with query *"how does projection quality work"* saved to the library, `plan_match("what's the mechanism for projection quality hub suppression")` returns that plan with cosine confidence > 0.80. Exact-token variants return the plan from FTS5 (`plan_search`) with equivalent or higher confidence.
- **SC-2** — Plan embedding is upserted on `plan_save` and deleted on TTL purge. T3 collection `plans__semantic` stays in sync with the T2 `plans` table (`COUNT(plans) == COUNT(plans__semantic)` after a reindex sweep).
- **SC-3** — Plan step schema accepts the `scope` field with `taxonomy_domain` ∈ {`prose`, `code`} and per-domain `topic=` / `follow_links=` / `subtree=` / `author=`. The plan runner forwards domain scope to the correct retrieval tool and corpus set.
- **SC-4** — Five scenario plans seed via `nx catalog setup`. `plan_search(name="research-plan")`, `plan_search(name="review-context")`, etc. all resolve. Reseeding is idempotent (no duplicates, updated metadata on re-run).
- **SC-5** — `search()` accepts `author`, `content_type`, `subtree`, `follow_links`, `depth`. `query()` accepts `topic`, `cluster_by`, `offset`. Both default corpus to `"knowledge,code,docs,rdr"`. Shared `resolve_catalog_collections()` helper is the single source of routing logic.
- **SC-6** — Session-start hook injects a "## Plan Library" block listing `plan_match` and the five scenario names.
- **SC-7** — SubagentStart hook injects a "plan-match-first" preamble for the eight retrieval-shaped agents. Each agent's `nx/agents/<name>.md` cites the pattern independently (verifiable by grep).
- **SC-8** — End-to-end demo: fresh session, user asks *"how does vision→language priming work in ART?"*, `nx:plan-first` skill fires, `plan_match` resolves (cold library hit) or `/nx:query` runs planner (cold library miss). In the miss case, the successful plan gets saved; the following session with a paraphrased query resolves from `plan_match` with confidence ≥ 0.80.
- **SC-9** — Zero regressions. `plan_save` / `plan_search` / `/nx:query` unchanged in behavior for existing callers. `search()` / `query()` existing arg sets unchanged in behavior.
- **SC-10** — Cross-embedding boundary is not crossed anywhere in the plan runner. Every retrieval step operates in exactly one embedding space. Verifiable: grep for any code that computes cosine between a code-model vector and a prose-model vector — should return zero hits.

## Research Findings

- **RF-1** — RDR-042 §Alternatives explicitly deferred the T3 semantic layer for plan storage, citing "Can add T3 semantic layer later if FTS5 matching proves inadequate." FTS5 matches tokens, not intent; this is inadequate by construction for the plan-reuse use case. Phase 1 is the scheduled pickup.
- **RF-2** — RDR-042 §Alternatives explicitly deferred taxonomy-driven planning, citing "4-stage LLM-based taxonomy construction. Rejected: expensive, tuned for homogeneous scholarly corpora, doesn't generalize to Nexus's mixed content. May revisit via lightweight clustering in a future RDR." RDR-070 (HDBSCAN topic discovery) shipped the lightweight clustering; it's now live on both code and prose corpora with c-TF-IDF labels and hub-detection (RDR-077). The RDR-042 condition for revisit is met.
- **RF-3** — Cross-embedding-model cosine is noise (measured 2026-04-14). ART code projected against ART prose at threshold 0.7 produced zero matches in 63,101 × 736 centroid comparisons; all 12,328 ≥0.7 matches were code↔code. `voyage-code-3` and `voyage-context-3` live in disjoint vector spaces. This rules out any projection-as-cross-corpus-bridge mechanism. Plans must compose per-space steps; no attempt to bridge embeddings belongs in this RDR.
- **RF-4** — `--use-icf` amplification bootstrap failure (measured 2026-04-14). ART backfill with `--use-icf` at threshold 0.7 wrote 189,303 assignments, all into 9 boilerplate Java/TypeScript topics; raw cosine avg 0.50, below the nominal threshold. Mechanism: `ICF = log2(N/DF) = log2(8/1) = 3.0` for DF=1 topics amplifies weak matches past threshold. This is an RDR-077 write-path finding that belongs in a separate post-mortem, but is cited here as part of the rationale for NOT using projection rows as a primary data source for plan-level retrieval.
- **RF-5** — RDR-042 plan library is FTS5 on `plans.query` + `plans.tags` (triggers defined at schema setup). Adding a T3 semantic cache does not modify the FTS5 path; additive. Plan rows have a stable `id` column usable as a ChromaDB document id.
- **RF-6** — `search_cross_corpus()` already accepts `topic`, `catalog`, `taxonomy`, `cluster_by`, `link_boost` kwargs at `src/nexus/search_engine.py:152-163`. `search()` MCP tool wires `topic=`; `query()` MCP tool does not. The underlying engine is capability-complete; only the tool surfaces are bifurcated. Phase 4 alignment is MCP-tool-level wiring plus one shared helper.
- **RF-7** — Five retrieval-shaped agents in `nx/agents/` already have docstring instructions to call `search` or `query`; the edit is to prepend a `plan_match` step before the existing instructions. No structural rewrites needed. Target agents: strategic-planner, architect-planner, code-review-expert, substantive-critic, deep-analyst, deep-research-synthesizer, debugger, plan-auditor.
- **RF-8** — Stale projection-table state was wiped clean at RDR draft time: 633,820 `assigned_by='projection'` rows deleted (633,356 NULL-`source_collection` legacy + 464 session-demo + upgrade-backfill rows). HDBSCAN (238,593) and centroid (748) assignments preserved. This establishes a clean baseline for any future write-path work; `plan_match` implementation never reads `topic_assignments`, so the wipe has no effect on Phase 1-5.

## Proposed Questions

- **PQ-1** — Plan embedding model. Default is `voyage-context-3` (CCE) for the query text. Alternative: use `voyage-3` (non-CCE, smaller cache footprint) for a purely intent-matching use case where the query is a single sentence. Calibrate during implementation; default stands unless measurably worse.
- **PQ-2** — Plan-match confidence threshold. Default 0.85 cosine is the RDR-042-cited 90%-confidence reuse rule converted to cosine distance (0.85 cosine ≈ 90% semantic overlap for short queries). Calibrate against a 20-query paraphrase set during Phase 1 implementation.
- **PQ-3** — Plan scope `taxonomy_domain` vocabulary. Starts with `prose` / `code`. Does `paper` merit its own domain (ChromaDB `paper__*` collections)? Or fold into `prose`? Defer to implementation; `prose` umbrella is probably fine since `paper__*` uses the same CCE model.
- **PQ-4** — Plan reuse when saved plan has failed steps. `plan.outcome` field distinguishes success/failure. `plan_match` must filter on `outcome='success'` by default; implement as a required WHERE clause, not an optional flag.
- **PQ-5** — Cross-project plan portability. Plans are project-scoped by default. A scenario plan written for `nexus` may not fit `ART` without parameter substitution. Phase 3 seed plans ship project-neutral (templates); agent customizes per-project before saving a project-scoped variant. Mechanism: plan runner accepts a `substitutions={name: value}` dict that resolves `$var` references in `plan_json`. Scope of substitution behavior to document.
- **PQ-6** — Drift between T2 `plans` and T3 `plans__semantic`. If a plan row is edited in SQLite bypassing `plan_save`, the T3 cache goes stale. Mitigation: periodic reindex job triggered by `nx catalog setup --reindex-plans`, opt-in. Not required for first iteration.
- **PQ-7** — Relationship to `nx:query` planner dispatch. `/nx:query` currently dispatches the query-planner agent for any novel analytical pipeline. After Phase 5, `nx:plan-first` runs before `/nx:query`. Does `/nx:query` become redundant, or does it remain as the "no match, decompose novel" path? Answer: it remains — `nx:plan-first` delegates to `/nx:query` on match miss. Explicit delegation chain documented in the skill.

## Out of Scope (deferred, may spawn separate RDRs)

- **Projection → catalog link promotion.** Within-same-model code↔code projection bridges are a sidequest to the primary ask (code↔prose via explicit graph). Useful later; not load-bearing here. Cross-embedding projection is ruled out by RF-3 regardless.
- **Heuristic linker strengthening** (module/symbol/path extraction from RDR body). Would address the 0.8%/7.9% per-workspace heuristic-linker recall measured in the prior draft. Valuable independently; not required for plan-centric retrieval since plans route via the link graph that exists.
- **RDR-077 `--use-icf` bootstrap failure post-mortem.** RF-4 cites it; a separate document in `docs/rdr/post-mortem/077-use-icf-bootstrap-amplification.md` should capture the mechanism and the three-gate filter proposal.
- **`context_graph` MCP tool.** The composed operation IS a saved plan, not a new tool. `plan_match` + plan runner is the composition layer.
- **Link graph UI.** Out of scope across all iterations.
- **Per-project configuration of hub stopwords** (RDR-077 PQ-3). Orthogonal.
