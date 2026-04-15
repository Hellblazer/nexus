---
title: "RDR-078: Plan-Centric Retrieval — Semantic Plan Matching, Typed-Graph Traversal, Scenario Plans"
status: accepted
type: feature
priority: P2
created: 2026-04-14
accepted_date: 2026-04-15
related: [RDR-042, RDR-050, RDR-053, RDR-070, RDR-077]
reviewed-by: self
---

# RDR-078: Plan-Centric Retrieval

Pickup of the two explicit deferrals in RDR-042, plus the reasoning mechanism that AgenticScholar attributes its quality delta to but RDR-042 did not ship: **multi-hop typed-link traversal as a first-class plan operator.** That RDR shipped the analytical-operator agent, plan library (`plans` table + FTS5), the `/nx:query` skill, and the self-correction loop. It deferred a semantic layer over the plan library ("Can add T3 semantic layer later if FTS5 matching proves inadequate") and skipped taxonomy-driven planning ("may revisit via lightweight clustering in a future RDR"). RDR-070 shipped HDBSCAN taxonomy discovery across both code and prose corpora; RDR-077 shipped similarity-aware projection quality signals. The blockers RDR-042 cited are now absent.

The center of gravity is the **plan** — a saved query+DAG pair — composed of steps that each operate in one embedding space or walk explicit typed edges between documents. No cross-embedding bridging is attempted; the architecture never requires it. The quality lever is the typed-link traversal step, not the domain-scoped retrieval step.

## Problem Statement

Nexus agents routinely reinvent retrieval DAGs, cannot reuse past plans semantically, cannot compose typed-graph traversal into plans, and cannot scope retrieval steps by domain taxonomy. The machinery to fix each exists in shipped RDRs (RDR-041 T1 HTTP server, RDR-042 plan library + `/nx:query`, RDR-070 HDBSCAN taxonomies, RDR-077 projection quality). What's missing is the architectural glue that binds them into a plan-centric retrieval layer.

### Enumerated gaps to close

*Four gaps, one per problem. Close-skill greps for `^#### Gap \d+:` in this section.*

#### Gap 1: Plan library is write-only for semantic intent

RDR-042's plan library stores `(query, plan_json, outcome, tags, project, ttl, created_at)` and exposes FTS5 search over the query text (`plan_search`). FTS5 matches *tokens*; it does not match *intent*. Two paraphrased queries that ought to resolve to the same plan typically miss — the agent re-decomposes, and the library grows with near-duplicate entries.

AgenticScholar's plan reuse mechanism is specifically semantic vector match over prior `(query, plan)` pairs with a confidence gate. RDR-042 documented this as a known deferral (§Alternatives: "T3 for plan storage (deferred)"). Two conditions were set for pickup: (a) evidence that FTS5 underperforms, and (b) infrastructure to embed queries. Both are met.

The reuse win is explicitly an **efficiency** story — AgenticScholar reports ~40% compute cost reduction at ≥90% match confidence (RF-1). It is not a quality story. That matters for framing: Phase 1 pays for itself in agent thrash avoidance, not in better answers.

#### Gap 2: Plan steps have no typed-graph traversal operator

Current plan JSON encodes retrieval and analytical operators (`search`, `extract`, `summarize`, `rank`, `compare`, `generate`). None of them walk the catalog link graph. Yet the catalog already holds ~16,500 typed edges across `cites`, `implements`, `implements-heuristic`, `supersedes`, `relates` — with BFS traversal already exposed via `query(follow_links=...)` and `nx catalog links`. The machinery exists; it is not composable into a plan DAG.

This is the load-bearing gap for the user's intent. The verb list — **create / debate / collate / relate / integrate** — hinges on "relate" being a real plan operation. The paper's NDCG@3 = 0.606 vs RAG's 0.411 (+47%) gain is qualitatively attributed to multi-hop KG traversal (Tier-3 analytical reasoning), not to plan reuse or taxonomies alone (RF-1). A plan runner that cannot compose a `traverse` step cannot perform the cross-document reasoning that distinguishes analytical retrieval from flat retrieval.

#### Gap 3: Plan steps cannot scope by domain taxonomy

The plan schema encodes tool arguments but has no *domain-scope* specifier. A plan for "find prior art on priming" cannot express "take the topic `vision-language priming` from the prose taxonomy and the topic `LanguagePrimingSignal` from the code taxonomy and return both, each scoped to its native embedding space." It can only express a flat `search` that either hits one corpus set or the other — losing the alignment that the two independently-discovered taxonomies naturally expose.

This is a narrower problem than Problem 2. Taxonomies scope *individual* retrieval steps; typed-graph traversal is what composes *across* steps. Both gaps coexist; the paper leans on traversal for quality, taxonomies for scope coherence.

#### Gap 4: Scenario-shaped reuse is unimplemented

Five canonical scenarios recur across every nexus session: design/planning, critique/review, analysis/synthesis, dev/debug, documentation. Each has a stereotyped retrieval shape. Today each session reinvents the retrieval DAG ad-hoc, even when a prior session three days ago solved the same pattern. Infrastructure exists — `plan_save`, `plan_search`, `/nx:query` — but the library is empty of what scenario-matched queries would need. Agents don't reach for the library because the library does not yet answer to the scenarios.

## Context

### Background

AgenticScholar ("Agentic Data Management with Pipeline Orchestration for Scholarly Corpora", arXiv 2603.13774, indexed in `knowledge__agentic-scholar`) describes a four-layer architecture: taxonomy-anchored knowledge graph, LLM-driven query planner, composable operator library, structured ingestion. Its benchmarks — NDCG@3 = 0.606 vs RAG's 0.411 (+47%) — are qualitatively attributed to the multi-hop KG Traverse operator (Tier-3 analytical reasoning), not to plan reuse or taxonomy construction alone. Plan reuse is explicitly an efficiency story (~40% compute cost reduction at ≥90% match confidence).

RDR-042 ("AgenticScholar-Inspired Enhancements", closed) adopted the composable operator library, the plan library (FTS5-indexed `plans` table), the `/nx:query` skill, and the self-correction loop. It **explicitly deferred two layers**: (a) a T3 semantic layer over the plan library ("Can add T3 semantic layer later if FTS5 matching proves inadequate") and (b) taxonomy-driven planning ("may revisit via lightweight clustering in a future RDR"). RDR-070 shipped HDBSCAN-based lightweight clustering across all corpora; RDR-077 shipped similarity-aware projection quality signals (similarity, assigned_at, source_collection, ICF). The two blockers RDR-042 cited are now absent.

The empirical corner-case of RDR-077 (recorded at 2026-04-14 during the ART backfill) rules out any cross-embedding-model cosine bridge: code uses `voyage-code-3`, prose uses `voyage-context-3`, and cosine between them is approximately noise. Plans must compose per-space steps; the typed-link catalog graph is the only bridge between code and prose and is already exposed via `query(follow_links=...)`.

### Technical Environment

- **Python 3.12+** CLI + MCP server stack. MCP server is LLM-free and deterministic by convention (RDR-042).
- **T2** = SQLite WAL + FTS5 (RDR-042 `plans` table; RDR-063 T2 domain split).
- **T1** = ChromaDB HTTP server per-session (RDR-041), with PPID-based inheritance for spawned subagents.
- **T3** = ChromaDB Cloud or local persistent, with `voyage-code-3` / `voyage-context-3` embedding per-prefix (RDR-059).
- **Catalog** = Git-backed JSONL source of truth + SQLite cache; ~16,500 typed edges across `cites`, `implements`, `implements-heuristic`, `supersedes`, `relates` (RDR-050, RDR-053, RDR-063).
- **Plan library** = `plans` table with FTS5 triggers + 5 builtin template seeds at `nx catalog setup` (RDR-042).
- **Analytical operator agent** = `nx/agents/analytical-operator.md` handles extract / summarize / rank / compare / generate (RDR-042).
- **Query planner agent** = `nx/agents/query-planner.md` dispatched by `/nx:query` skill for novel analytical pipelines (RDR-042).
- **Taxonomy** = HDBSCAN topic discovery + c-TF-IDF labels + centroid ANN (RDR-070), similarity + ICF + hub detection (RDR-077).

## Proposed Design

Six phases. Each builds on RDR-042's shipped substrate. Phase 3 carries the paper's quality lever (typed-graph traversal); Phase 6 carries the shipping-velocity story (scoped plan loading).

### Vocabulary: plans are multi-dimensional templates

A plan is a **template** — a reusable DAG of operator steps with named `$var` placeholders. The plan library is a **template registry** selected by semantic intent against a bag of pinned dimensions, with ranking by description cosine.

**Identity is a pinned dimension set, not a flat name.** A plan's identity is the map of dimension → value that it pins. Two plans with identical pinned sets collide; `name` is a human-facing disambiguator, not part of identity. Scope, verb, strategy, and others are just well-known dimensions — none is structurally privileged.

**Three strings, three jobs:**

- **`description`** — prose authored when the plan is saved, describing *when to use it*. Embedded at SessionStart; what `plan_match` ranks on via cosine. **Persisted in the existing `plans.query` column** (RDR-042 schema, no rename); "description" is the conceptual name used in RDR discourse and in the YAML template — the SQL column stays `query` for compatibility.
- **`intent`** — caller-side phrasing of what they're trying to do. Passed to `plan_match` at call time; never stored on the plan.
- **`name`** — human disambiguator for otherwise-identical dimension sets. Not part of the match.

The match is `cosine(embed(caller.intent), embed(plan.description))`, applied to candidates whose dimensions ⊇ the caller's filter. Dimensions narrow the pool; cosine ranks within it; scope cascade and specificity are tiebreakers; bindings parameterise the chosen template at run time.

**v1 dimension set (extensible via registry):**

| Dimension | Typical values | Required? |
|---|---|---|
| `verb` | research / review / analyze / debug / document / relate / trace / integrate / locate / verify / plan-author / plan-inspect / plan-promote | yes |
| `scope` | personal / rdr-<slug> / project / repo / global | yes |
| `strategy` | default / deep / quick / compliance / ... | no (defaults to `default`) |
| `object` | concept / file / module / rdr / change-set / symbol / commit / ... | no |
| `domain` | security / performance / correctness / documentation / onboarding / ... | no |

Dimensions live in a git-tracked registry: `nx/plans/dimensions.yml` (global, plugin-shipped) with project/repo overrides via the Phase 6 scoped-loader mechanism. Unregistered dimensions on a plan warn at load ("unrecognised dimension `<name>` — register or remove"). Discipline: a dimension earns inclusion only when filtering by it is a *recurring* need — prematurely pinned dimensions retire on disuse.

**Plan template structure:**

```yaml
name: default                             # human disambiguator only
description: |
  Research a concept by walking prior-art documents, then following
  their typed links to implementing code, then summarising the union
  with citations. Use when the user asks to "plan", "design", "extend",
  or "research" a technical subsystem.
dimensions:                               # identity — the pinned set
  verb: research
  scope: global
  strategy: default
  object: concept
parent: null                              # optional currying lineage (see below)
default_bindings: {}                      # pre-filled placeholders (currying)
required_bindings: [concept]
optional_bindings: [module]
plan_json:
  steps:
    - tool: search
      args: {query: "$concept", corpus: "knowledge,rdr,docs"}
      scope: {taxonomy_domain: prose, topic: "$concept"}
    - tool: traverse
      args: {seeds: "$step1.tumblers", purpose: find-implementations, depth: 2}
    - tool: search
      args: {query: "$concept", corpus: "code", subtree: "$module"}
      scope: {taxonomy_domain: code}
    - tool: summarize
      args: {inputs: [$step1, $step2, $step3], cited: true}
```

**Currying via `default_bindings` + `parent`:**

A curried plan is a more-specialised plan that pins more dimensions AND pre-fills some bindings. Lineage is optional but useful for introspection:

```yaml
name: security
description: "Review a code change with a security lens. Walks the authoring
  RDRs, applies security-review extraction, and compares against the
  change."
dimensions:
  verb: review
  scope: global
  strategy: default
  object: change-set
  domain: security                        # NEW pinned dimension — specialisation
parent:                                   # what this specialises
  verb: review
  scope: global
  strategy: default
  object: change-set
default_bindings:                         # pre-filled; caller can still override
  focus: security
required_bindings: [changed_paths]
plan_json: {...}
```

`plan_run(match, bindings)` resolves final bindings as `{**match.default_bindings, **caller.bindings}` — caller wins on conflict. No new mechanism; three lines in the runner.

**Input contract — four axes, now dimensional:**

```
plan_match(
    intent: str,                       # REQUIRED — caller's description of what they're doing
    dimensions: dict = {},             # pin any subset, e.g. {verb: "review", object: "change-set"}
    scope_preference: str = "",        # "rdr-078,project,repo,global" — scope dimension cascade
    context: dict = {},                # optional — {changed_paths, active_rdr, current_topic}
                                       # blended into the match embedding when present
    min_confidence: float = 0.85,
    n: int = 5,
) -> list[Match]

plan_run(
    match: Match,
    bindings: dict = {},               # fills $vars; merged over match.default_bindings
) -> PlanResult
```

Candidate selection: `plan.dimensions ⊇ filter.dimensions`. Ranking: cosine primary, specificity bonus (plans with more dimensions pinned beyond the filter rank slightly higher on ties), scope cascade last. Specificity tiebreak prefers more-specialised plans when confidences are close — the curried `security` variant wins over the general `default` when the caller pins `domain: security`, loses when they don't.

The axes cleanly separate semantic selection (`intent`), multidimensional pool narrowing (`dimensions`, `scope_preference`), situational hints (`context`), and execution parameterisation (`bindings`). Naming kludges collapse: skills become pure verbs (`nx:research`, `nx:review`, `nx:debug`, ...) whose body is one parameterised template: `plan_match(intent, dimensions={verb: <skill_verb>}, n=1)` → `plan_run(match, bindings)`. Five scenario skills share one implementation.

### Phase 1: Semantic plan matching (`plan_match` MCP tool, T1-cached)

Pickup of RDR-042 §Alternatives "T3 for plan storage (deferred)". The original deferral assumed a T3 collection; on review the session-scoped T1 ChromaDB is a better fit — the cache rebuilds on every SessionStart from T2, so there is no sync drift, no TTL coordination, and no dual-write race.

- **T2 remains authoritative.** `plans` table + FTS5 (existing, RDR-042) is the source of truth. All writes go to T2 via `plan_save`. No schema changes.
- **T1 holds the session semantic cache.** New collection `plans__session` (via RDR-041's T1 HTTP server) populated at SessionStart: `SELECT id, query, plan_json, tags, dimensions FROM plans WHERE outcome='success' AND (ttl IS NULL OR julianday('now') - julianday(created_at) <= ttl)` (matches the existing `search_plans` TTL predicate in `plan_library.py:195-197`) → embed the query text → upsert one document per plan with `metadata={plan_id, verb_name, handler_kind, tags, project, ttl, last_used}`.
- **`plan_match`** — signature in the Vocabulary section above. T1 cosine over plan descriptions, returns ranked `Match` objects with `{plan_id, name, description, confidence, dimensions, tags, plan_json, required_bindings, optional_bindings, default_bindings, parent_dims}`. Only `outcome='success'` plans are loaded (TTL-honest per Phase 5 SessionStart SQL), so no explicit failure filter needed at call time.
- **`plan_run`** — new MCP tool (signature in Vocabulary section above). **Execution model: deterministic**. Pure substitution + tool dispatch, no subagent spawning. Steps run in declared order. For each step: (i) resolve `$var` placeholders from `{**match.default_bindings, **caller.bindings}`; (ii) resolve `$stepN.<field>` references from prior step outputs stashed in T1 scratch (RDR-041 pattern, tag `plan_run,step-N`, same mechanism RDR-042's `/nx:query` skill already uses); (iii) dispatch the MCP tool named in `step.tool` with the substituted args; (iv) persist the step result to T1 scratch for downstream `$stepN` resolution. Unresolved required bindings abort with `PlanRunBindingError(missing=[...])`; unresolved `$stepN` references abort with `PlanRunStepRefError`. `plan_run` does NOT dispatch the query-planner or analytical-operator agents — those agents are still available as `step.tool: "extract"` / `"summarize"` / etc., invoked as any other MCP tool. This distinguishes `plan_run` (deterministic DAG execution of a known plan) from `/nx:query` (the planner-dispatching skill for novel analytical pipelines without a matching plan).
- **Retrieval step output contract** (PQ-5 resolved as a design decision, not an open question): every retrieval step (`search`, `query`, `traverse`) emits a result object carrying at least `{tumblers: list[str], ids: list[str], distances: list[float] | None}`, stashed to T1 scratch under the `$stepN` key. Downstream `$stepN.tumblers` / `$stepN.ids` references resolve from this shape. Non-retrieval operators (`extract`, `summarize`, `rank`, `compare`, `generate`) emit `{text: str, citations: list[dict]}`; `$stepN.text` and `$stepN.citations` are the documented reference fields.
- **Fallback path.** When T1 is unavailable (EphemeralClient in tests, or session server failed to start), `plan_match` degrades to `plan_search` FTS5 over T2. The fallback must return `Match` objects compatible with `plan_run`; implement via a constructor `Match.from_plan_row(row: dict) -> Match` that parses `dimensions` JSON into a dict, parses `default_bindings` JSON into a dict, and sets `confidence=None` (sentinel for "FTS5 match; cosine confidence unavailable"). `plan_run(match, bindings)` treats `confidence=None` as an implicit pass — skills that gate on `confidence >= 0.85` must check `confidence is not None` first. Tests assert the fallback round-trip: `plan_match(...)` → `plan_run(...)` succeeds with both T1 available and T1 disabled.
- **Write visibility.** A new plan saved mid-session via `plan_save` is also upserted to T1 by the same commit hook — so the calling session sees it immediately without waiting for the next SessionStart. Subagents sharing the parent T1 (RDR-041 session inheritance) see it too.
- The existing `plan_search` FTS5 tool is untouched — it remains the fast path for exact-token and tag-only lookups.

**What this drops vs. earlier draft:** no new T3 collection; no T3 embedding cost per `plan_save`; no TTL-triggered T3 deletion; no reindex command. T1 rebuilds every session by construction.

**Expected benefit (RF-1):** ~40% compute cost reduction on scenario-matched queries once the library is populated. Efficiency story, not quality.

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

**Implementation** reuses `Catalog.graph()` (`src/nexus/catalog/catalog.py:1440`). No new storage, no new graph *algorithm* — but `Catalog.graph(tumbler, ...)` accepts a **single** `Tumbler`, while plan `traverse.seeds` accepts a list (or `$step_N` reference resolving to multiple tumblers). Phase 3 adds a thin multi-seed wrapper `Catalog.graph_many(seeds: list[Tumbler], ...) -> {nodes, edges}` that:

- Iterates `Catalog.graph(seed, depth, direction, link_type)` per seed.
- Merges `nodes` by node-key = `str(tumbler)` — first-seen wins; per-node `seed_origin: list[str]` metadata records which seed(s) reached it.
- Merges `edges` by edge-key = `(str(from_tumbler), str(to_tumbler), link_type)` — deduplicates across seed traversals.
- Honours the same `_MAX_GRAPH_NODES = 500` cap applied across the merged result, not per-seed. Traversal short-circuits once the merged frontier exceeds the cap.

No SQL changes, no new graph algorithm — just the wrapper. The plan-step binding `{tool: "traverse", args: {seeds, link_types | purpose, depth, direction, return}}` dispatches through `graph_many()` when `seeds` is a list (or resolves to one), and through the existing `graph()` otherwise. Contract tests pin the merge invariants against the catalog's existing 16,500+ edges.

**Composability examples**:

- *Research a concept* — `search(topic=X)` → `traverse(seeds=$step1, link_types=[implements,cites], depth=2)` → `summarize(cited=true)`. Finds seed documents, walks their implementation and citation graph, summarises with provenance.
- *Review a change* — `catalog-links-for-file($changed_paths)` → `traverse(seeds=$step1, link_types=[supersedes,relates], depth=1)` → `extract(template=decision_schema)` → `compare(baseline=$plan_baseline)`. Pulls authoring RDRs, walks decision evolution, extracts the effective decisions, compares to what the change does.
- *Integrate findings* — `search(topic=prose-X)` + `search(topic=code-Y)` + `traverse(seeds=$step1_tumblers ∪ $step2_tumblers, link_types=[implements], depth=1)` → `generate(template=synthesis, cited=true)`. Dual-taxonomy retrieval stitched by typed-link traversal.

**Expected benefit (RF-1):** this is where the paper's +47% NDCG quality delta is attributed. Honest estimate: transfer is *qualitative* — nexus's mixed corpus, non-scholarly taxonomy, and different link type vocabulary mean the magnitude will differ. What should transfer is the *kind* of result: plans that previously required ad-hoc multi-agent orchestration will run as deterministic DAGs, with the cross-document connections the intent list (especially "relate" and "integrate") requires.

#### Purpose abstraction over link types

Hand-picking `link_types: [implements, cites, ...]` in every plan is brittle. Plan authors (human and agent) need to reason about *intent*, not the catalog's edge vocabulary. `traverse` accepts either:

- **`link_types: [...]`** — literal list, pinned at authoring time. Explicit and inspection-friendly.
- **`purpose: <name>`** — resolved via a registry to a link-types set. Preferred; more readable; auto-adapts when the vocabulary extends.

Starter purpose set (shipped in `nx/plans/purposes.yml`, git-tracked; overridable per Phase 6 scope tiers via `.nexus/purposes.yml`):

| Purpose | Resolves to | When |
|---|---|---|
| `find-implementations` | `implements, implements-heuristic` | Doc/RDR → code |
| `decision-evolution` | `supersedes` | Walk decision chains |
| `reference-chain` | `cites` | Evidence / paper citation graph |
| `documentation-for` | `documented-by, cites` | Code → its docs |
| `soft-relations` | `relates` | Sibling / tangential discovery |
| `all-implementations` | `implements, implements-heuristic` (plus `semantic-implements` when that type ships in a future RDR) | Broader net than `find-implementations`; forward-compatible |

Schema validator rejects `link_types` and `purpose` specified together. `purpose` is the recommended form for new plans and scenario seeds.

**Unknown link-type handling.** `purposes_resolve(name, project, scope)` filters the resolved list against `Catalog.registered_link_types()` before returning: unknown link types are dropped with a structured warning (`purpose_unknown_link_type, purpose=<name>, link_type=<token>`). This is the forward-compatibility seam — e.g., `all-implementations` can list `semantic-implements` today; the resolver silently omits it until that link type ships, at which point the same purpose automatically picks it up.

Two companion docs ship with Phase 3: `docs/catalog-link-types.md` (semantic definition of each link type — directionality, source, typical traversal shape, when to use) and `docs/catalog-purposes.md` (purpose registry reference). Both are catalog-indexed so `plan_match("what's the right link type for walking decision history")` can surface them.

### Phase 4: Plan templates, metrics, meta-seeds, authoring guide

Four tightly-coupled pieces: the formal template schema, the scenario template seeds, the operational metrics that feed the future promotion pipeline, and the meta-seeds that teach agents (and humans) how to author more.

#### 4a — Plan template schema

Formal YAML/JSON schema for a plan template (loadable by `.nexus/plans/*.yml`, `docs/rdr/<slug>/plans.yml`, and the global plugin seeds):

```yaml
name: <human-disambiguator>       # optional; distinguishes otherwise-identical dimension sets
description: <prose>              # required; embedded for plan_match cosine
dimensions:                       # required; identity — the pinned set
  verb: <registered-verb>         # required dimension
  scope: <registered-scope>       # required dimension
  strategy: default | ...         # optional (defaults to "default")
  object: ...                     # optional
  domain: ...                     # optional
  # any registered dimension, pinned or omitted
parent:                           # optional currying lineage
  verb: ...
  scope: ...
  strategy: ...
default_bindings:                 # optional pre-filled placeholders
  <var-name>: <value>
required_bindings: [<name>, ...]  # optional; plan_run aborts if missing
optional_bindings: [<name>, ...]  # optional; defaults to null/empty
tags: <comma-separated>           # optional; free-form non-dimensional tags
plan_json:
  steps:
    - tool: <search|query|traverse|extract|summarize|rank|compare|generate>
      args: {<tool-args, may contain $var and $stepN.field refs>}
      scope: {<Phase 2 scope override, optional>}
```

Schema validation lives in a dedicated validator (`src/nexus/plans/schema.py`); loaders reject malformed entries with a named error. **Identity dedup key** = the canonicalised dimension map (sorted, keys-lowercased). Two plans with identical dimension maps at load time are a conflict — loader rejects the later one with a named error that names both sources.

#### 4b — Five scenario templates (seeded at `scope:global`)

Each plan pins `{verb, scope:global, strategy:default}` at minimum. Descriptions are exemplary — good enough to learn from by reading. Four of the five use a `traverse` step; the `verb:debug` scenario is **intentionally flat** (no `traverse`) because dev/debug typically starts from a failing path and the primary link walk is `catalog-links-for-file`, not multi-hop graph traversal — Serena handles symbol-level navigation separately.

| Dimensions | Scenario | DAG sketch |
|---|---|---|
| `verb:research, scope:global, strategy:default` | Design / arch / planning | `search` prose (topic=$concept) → `traverse` (purpose=find-implementations, depth=2) → `search` code (topic=$concept, subtree=$module) → `summarize` with citations. |
| `verb:review, scope:global, strategy:default` | Critique / audit / review | `catalog-links-for-file($changed_paths)` → `traverse` (purpose=decision-evolution, depth=1) → `extract` decisions → `compare` decisions vs. changed code. |
| `verb:analyze, scope:global, strategy:default` | Analysis / synthesis / research | `search` prose (topic=$area) → `search` code (topic=$area) → `traverse` both (purpose=reference-chain, depth=2) → `rank` by criterion → `generate` synthesis with citations. |
| `verb:debug, scope:global, strategy:default` | Dev / debug | `catalog-links-for-file($failing_path)` → `traverse` (purpose=decision-evolution, depth=1) for authoring-RDR history → `summarize` design context. Serena handles symbol-level separately. |
| `verb:document, scope:global, strategy:default` | Documentation | `search` prose (follow_links=cites) for existing references → `search` code (topic=$area) → `traverse` (purpose=documentation-for, depth=1) → `compare` for doc-coverage gaps. |

Each plan's `name` is `default` — the canonical strategy for that verb. Variants (e.g. `verb:review, strategy:security` with `domain:security` pinned) are added later via Tier-B YAML when specialisation need emerges. Skills in Phase 5 are pure verbs (`nx:research`, `nx:review`, `nx:analyze`, `nx:debug`, `nx:document`) and share one template body: `plan_match(intent, dimensions={verb: <skill_verb>}, n=1)` → `plan_run(match, bindings)`.

Seeding via `nx catalog setup` is idempotent (existing plans with the same canonical dimension map are updated, not duplicated).

#### 4c — Operational metrics

Extend the `plans` table (T2 migration at RDR-078 implementation time) with counters that observe the feedback loop without acting on it:

```sql
-- Metrics
ALTER TABLE plans ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN last_used TEXT;
ALTER TABLE plans ADD COLUMN match_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN match_conf_sum REAL NOT NULL DEFAULT 0.0;
ALTER TABLE plans ADD COLUMN success_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;

-- Dimensional identity (indexed axes — others live in `tags`)
ALTER TABLE plans ADD COLUMN verb TEXT;
ALTER TABLE plans ADD COLUMN scope TEXT;
CREATE INDEX IF NOT EXISTS idx_plans_verb ON plans(verb);
CREATE INDEX IF NOT EXISTS idx_plans_scope ON plans(scope);
CREATE INDEX IF NOT EXISTS idx_plans_verb_scope ON plans(verb, scope);

-- Canonical dimension map for identity + dedup
-- JSON string, keys lowercased and sorted, e.g.
--   {"object":"concept","scope":"global","strategy":"default","verb":"research"}
-- Produced by `src/nexus/plans/schema.py:canonical_dimensions_json(dim_map)`.
-- UNIQUE constraint makes (project, dimensions) the identity-dedup key —
-- `plan_save` / loader use INSERT ON CONFLICT(project, dimensions) DO UPDATE
-- to make reseeding idempotent. Same-verb same-scope same-strategy
-- collisions land on the existing row rather than duplicating.
ALTER TABLE plans ADD COLUMN dimensions TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_project_dimensions
    ON plans(project, dimensions)
    WHERE dimensions IS NOT NULL;

-- Currying
ALTER TABLE plans ADD COLUMN default_bindings TEXT;   -- JSON object
ALTER TABLE plans ADD COLUMN parent_dims TEXT;        -- JSON object (parent's dimension map)

-- Optional human disambiguator (not part of identity)
ALTER TABLE plans ADD COLUMN name TEXT;
```

`verb` and `scope` get dedicated indexed columns because they are the high-frequency filters. The canonical `dimensions` JSON carries the full identity bag and is UNIQUE per `(project, dimensions)` — this is the enforcement mechanism Phase 6 SC-14 assumes. Other dimensions (`object`, `domain`, etc.) serialise as `k:v` entries into the existing `tags TEXT` column for FTS5 discoverability, but identity is from `dimensions`, not `tags`. Existing RDR-042 rows migrate with `verb=NULL, scope='personal', dimensions=NULL` and continue to work through the legacy `plan_search` FTS5 path (which ignores `dimensions`) until they're promoted or re-saved under the dimensional schema.

Update points:

- `plan_match` returns → increment `match_count`, add cosine to `match_conf_sum` for every returned plan (so `match_conf_avg = sum/count`).
- `plan_run` starts → increment `use_count`, update `last_used`.
- `plan_run` completes → increment `success_count` or `failure_count` based on outcome.

Per-installation operational data; **not git-tracked**. When a plan graduates to YAML at a higher scope, counters reset in the new scope (the YAML is canonical; stats measure local experience).

**Promotion signal** (derived, not stored): a plan is a reasonable promotion candidate when `use_count ≥ 3 AND match_count/use_count ≥ 0.7 AND success_count/(success_count+failure_count) ≥ 0.8 AND match_conf_sum/match_count ≥ 0.80`. Thresholds are PQs, not hardcoded. **The CLI that acts on this signal is deferred to RDR-079.**

#### 4d — Meta-seeds (also seeded at `scope:global`)

Four seed plans whose purpose is to teach agents and humans how the library works. Each pins `verb`, `scope:global`, and `strategy:default` (or a named variant when multiple strategies exist).

- **`verb:plan-author, strategy:default`** — description: *"Author a new plan template from scratch."* DAG: fetches `docs/plan-authoring-guide.md` → fetches the dimension registry and schema → prompts the caller for dimensions / description / required_bindings → drafts a candidate `plan_json` with per-step scope and `traverse` purpose guidance → calls `plan_save` with the result. Self-referential bootstrap.
- **`verb:plan-promote, strategy:propose`** — description: *"Survey T2 plan counters and rank promotion candidates."* DAG: reads `plans` metrics → applies candidate thresholds (default 4c values) → formats a ranked shortlist with paraphrase-range per plan → emits a markdown report. Primitive form of RDR-079's `nx plan audit` CLI; usable today via `plan_run`.
- **`verb:plan-inspect, strategy:default`** — description: *"Inspect a plan by its dimension map. Render description, dimensions, bindings, lineage, and DAG with annotation."* DAG: loads plan by canonical dimension map → renders fields + step-by-step with arg binding notes + currying parent if any. Agent self-service introspection.
- **`verb:plan-inspect, strategy:dimensions`** — description: *"List registered dimensions with their value sets and usage counts."* DAG: reads `nx/plans/dimensions.yml` (+ scoped overrides) → cross-references T2 plan usage per dimension → emits a markdown catalogue. Vocabulary discovery for agents and humans.

#### 4e — `docs/plan-authoring-guide.md`

Prose companion document shipped in the plugin. Covers:

- The templating vocabulary (template / description / intent / bindings / scope).
- Schema reference.
- What makes a good `description` (keys: verb-action, typical nouns, scope hint).
- Binding naming conventions (lowercase snake, no type suffixes).
- The four-axis `plan_match` contract.
- Lifecycle: personal → rdr → project → repo → global. Promotion path.
- How to run `verb:plan-authoring` if unsure where to start.

Linked from RDR-078's References. Catalog-indexed like any other doc in `docs/`, so `plan_match` can surface it when an agent asks *"how do I write a plan."*

### Phase 5: Plan-first priming (skills + hooks + agent prompts)

The ergonomic change. Agents must reach for `plan_match` **before** decomposing any retrieval task.

- **`nx:plan-first` skill** — the gate skill. Invoked at the top of every retrieval-shaped task. Triggers on verbs like "plan", "design", "review", "analyze", "debug", "document". Instructs: call `plan_match(intent, min_confidence=0.85)` first; if a match exists, present it and execute via `plan_run`; if not, dispatch `/nx:query` (the query-planner) and save the result.
- **Five verb skills**: `nx:research`, `nx:review`, `nx:analyze`, `nx:debug`, `nx:document`. All share one template body — `plan_match(intent, dimensions={verb: <skill_verb>}, n=1)` → if confidence ≥ threshold, `plan_run(match, bindings)`; else defer to `/nx:query`. Skill names are pure verbs; the dimension filter does the namespacing that old compound names (`nx:research-plan`) tried to do by embedding a qualifier.
- **Three plan-management skills**: `nx:plan-author`, `nx:plan-inspect`, `nx:plan-promote`. Same template body pointed at the matching meta-seed verbs.
- **SessionStart hook** (`nx/hooks/scripts/session_start_hook.py`) — two additions:
    1. **Populate T1 `plans__session` semantic cache.** Read
       ```sql
       SELECT id, query, plan_json, tags, dimensions, created_at, ttl
       FROM plans
       WHERE outcome = 'success'
         AND (ttl IS NULL OR julianday('now') - julianday(created_at) <= ttl)
       ```
       (matches the existing `search_plans` TTL predicate in `plan_library.py:195-197`) → embed the `query` text (the description per Vocabulary) → upsert to T1 collection `plans__session` with metadata `{plan_id, verb, scope, strategy, tags, project, ttl, created_at, last_used}`. Skipped gracefully if the T1 server is unavailable (fallback to FTS5 at match time). Log the populated count.
    2. **Inject a "## Plan Library" context block** listing `plan_match` / `plan_save` / `plan_search` and the five scenario/verb names. Extends the existing "## nx Capabilities" section.
- **SubagentStart hook** (`nx/hooks/scripts/subagent-start.sh`) — for the eight retrieval-shaped agents (strategic-planner, architect-planner, code-review-expert, substantive-critic, deep-analyst, deep-research-synthesizer, debugger, plan-auditor), inject a "plan-match-first" preamble: *before decomposing any retrieval task, call `plan_match(query, min_confidence=0.85)`; execute the returned plan if match confidence clears the threshold.*
- **Per-agent `nx/agents/<name>.md`** — each target agent's opening instruction cites the `plan_match`-first pattern independently of the hook, so behavior survives hook-context trimming.

### Phase 6: Scoped plan loader — git as the shipping transport

Extends `nx catalog setup` to load plan templates from scoped locations beyond the plugin-shipped globals. **Every scope above `personal` is a git-tracked YAML path** — sharability, persistence, review, rollback, attribution, offline operation, and CI-gateability come from git for free. No new transport, no server, no auth layer. Five scope tiers coexist in T2 via the `scope:*` tag convention:

| Scope tag | Lives in | Loaded by | Git-tracked |
|---|---|---|---|
| `scope:personal` | T2 only | `plan_save` at runtime | No |
| `scope:rdr-<slug>` | `docs/rdr/<slug>/plans.yml` (peer to the RDR file) | `nx catalog setup` when RDR is `status: accepted` | Yes (with the RDR) |
| `scope:project` | `.nexus/plans/*.yml` or `.nexus/plans.yml` (project root) | `nx catalog setup` | Yes |
| `scope:repo` | umbrella path (e.g. `.nexus/plans/_repo.yml`) | `nx catalog setup` with umbrella detection | Yes |
| `scope:global` | `nx/plans/builtin/*.yml` in the plugin | plugin load at catalog setup | Plugin release |

**Loader contract:**

- `nx catalog setup` scans the four YAML paths in addition to the plugin builtins.
- Each loaded plan is schema-validated (Phase 4a); invalid plans log a named error and skip.
- Each plan's `scope` field is cross-checked against its source path (e.g., a YAML in `.nexus/plans/` must declare `scope: project`); mismatches are logged and the file's path wins.
- Idempotency via the Phase 4c `UNIQUE INDEX idx_plans_project_dimensions ON plans(project, dimensions)` + `INSERT … ON CONFLICT(project, dimensions) DO UPDATE`: re-running `nx catalog setup` updates the existing T2 row rather than duplicating. `name` is a human disambiguator only and plays no role in SQL-level dedup.
- RDR-scoped plans are loaded only when the parent RDR's frontmatter `status` is in `{accepted, closed}` — drafts don't pollute the library. RDRs transitioning to `closed` with a post-mortem keep their plans discoverable for archival purposes; re-activation is manual.

**Scope precedence at lookup time:**

`plan_match(intent, scope_preference="rdr-078,project,global")` ranks candidates by `cosine × specificity_multiplier` where `personal > rdr > project > repo > global`. The multiplier is small (0.05 per step) — semantic match dominates; specificity is the tiebreaker when confidences are close.

**Git is the transport — what that buys us:**

- **Sharability.** Anyone who clones the repo gets every `scope:rdr-*`, `scope:project`, `scope:repo` plan. No server, no auth, no coordination.
- **Persistence.** History is git history. Deleted plans are recoverable. Blame explains why a plan exists.
- **Review.** PR-gated plan changes at the project and repo tiers; RFC-level scrutiny for plugin tier. Drafts live on branches until merged.
- **Rollback.** `git revert` on a bad plan is instant. No DB migration, no data-layer surgery.
- **Attribution.** Every plan has an author, a commit, a message, a PR. Promotion-candidate reasoning becomes auditable.
- **Offline.** Plan library is populated without network. Catalog setup is deterministic from clone state.
- **CI gating.** Plan schema validation runs in CI. Broken plans fail the build before they hit T2. The plugin's shipped scenario plans are smoke-tested the same way.

The catalog already treats YAML files as first-class documents with tumblers; plans get catalog edges for free, participating in Phase 3 traversal without extra wiring.

**What Phase 6 does NOT ship:**

Promotion operations (`nx plan promote`, lifecycle hooks on RDR close, `nx plan audit`) are deferred to RDR-079. Phase 6 delivers *loading* from git-tracked YAML, not mutation of git state. Humans who want to promote today use `git mv` + edit + commit; RDR-079 makes it a one-liner that wraps the same git calls.

## Alternatives Considered

### Alternative 1: Persistent T3 `plans__semantic` collection

Embed plan queries into a permanent T3 ChromaDB collection; sync on every `plan_save`; TTL-coordinated deletion.

**Rejected** because session-scoped T1 (RDR-041) rebuilds the cache from authoritative T2 at every SessionStart — no sync drift, no TTL coordination, no dual-write race. In-session upsert on `plan_save` gives immediate visibility within the calling session; next SessionStart makes it fully authoritative. Persistent T3 added coordination complexity for no semantic gain.

### Alternative 2: Unified `retrieve()` tool replacing `search()` and `query()`

Collapse both MCP tools into `retrieve(intent, mode="chunks"|"documents", ...)` with every feature available in one surface.

**Rejected** as a breaking change with no corresponding clarity win. `search()` chunk-granular and `query()` document-granular distinction is real and usefully separate. RDR-078 keeps both; gaps in feature parity become follow-on tidy-up work. Option 1 (additive alignment) was provisionally proposed, then also rejected as not paper-grounded (research-2 audit) — filed to Out of Scope.

### Alternative 3: Projection-rows as authoritative catalog edges

Promote RDR-077's `topic_assignments` rows (chunk × topic projections with similarity + ICF) into catalog `semantic-implements` edges at every `nx catalog setup`.

**Rejected** because (a) cross-embedding projection is noise between `code__*` and `docs__*`/`rdr__*`/`knowledge__*` corpora (empirical, research-3), and (b) the `--use-icf` bootstrap failure mode surfaces boilerplate hubs faster than domain topics at write time, poisoning the edge set. Same-embedding-space projection (code↔code, prose↔prose) remains a candidate for a future RDR, but is a sidequest to this one.

### Alternative 4: Hand-picked `link_types: [...]` only, no purpose abstraction

Keep plan `traverse` steps authored with literal link-type lists; ship no purpose registry.

**Rejected** because plan authors (especially agents) need to reason in intent terms. Hand-picking `[implements, implements-heuristic]` every time a plan wants "find the code" is brittle, and when `semantic-implements` (or any future type) lands, every plan needs manual update. The `purpose` abstraction trades one layer of indirection for teachability, extension-safety, and readable templates. Both forms coexist in the schema; `purpose` is recommended.

### Alternative 5: Flat (scope, verb, name) identity

Keep the first-draft three-field plan identity with separate top-level fields.

**Rejected** because naming kludges propagate (e.g. `research-plan` conflates verb and qualifier). Dimensional identity (pinned bag of dimensions, `name` as disambiguator) eliminates the kludge, enables organic specialisation via currying, and matches the functional framing of plans as typed templates. Migration is cheap: existing RDR-042 rows keep working with `verb=NULL, scope='personal'`.

### Alternative 6: Automatic plan promotion based on counters

Promote plans automatically once `use_count`, `match_conf_avg`, and `success_rate` clear thresholds.

**Rejected for RDR-078**, deferred to RDR-079. The *signal* (metrics columns + `verb:plan-promote,strategy:propose` meta-seed) is in RDR-078 scope; the *action* (CLI + lifecycle hooks) is in RDR-079 after 4-6 weeks of usage data inform threshold calibration. Human-in-loop promotion via `git mv` + `commit` works today.

## Trade-offs

### Consequences

- **Plan library growth is unbounded by default.** Every `plan_save` creates a row; T1 cache size scales with library size. Mitigation: T1 is ChromaDB cosine-indexed (sub-millisecond lookup at 10k entries); PQ-10 revisits scope (verbs-only vs all-plans) when library exceeds 10k.
- **Dimensional identity means identity collisions are schema-enforceable.** Two plans with the same canonicalised dimension map at load time reject with a named error. This is a hard constraint — intentional.
- **The `description` field becomes load-bearing.** A sloppy description degrades `plan_match` accuracy for that plan. Authoring guide (Phase 4e) and `verb:plan-author` meta-seed (Phase 4d) exist precisely to teach good-description habits. Sloppy descriptions fail silently (low match confidence) rather than loudly (crash), so authoring discipline matters.
- **Git becomes a plan-deployment channel.** Team-scoped plans land via PR; the same review culture that gates code now gates plans. Teams that don't have that culture see the plan library diverge across clones.

### Risks and Mitigations

- **Risk: T1 server unavailable at SessionStart.** `plan_match` falls back to FTS5 over T2 — functional but token-matching, no semantic reuse. **Mitigation**: existing RDR-041 startup robustness; graceful degradation is a first-class contract.
- **Risk: dimension sprawl.** Plans keep adding custom dimensions; registry becomes unwieldy. **Mitigation**: warn-on-unregistered-dimension at load; `verb:plan-inspect,strategy:dimensions` surfaces cold dimensions. RDR-079 can add retirement policy.
- **Risk: scope cascade turns into confusion.** A team member expects their project-scope override but the global default wins due to higher cosine. **Mitigation**: specificity bonus (0.05/dim) reliably breaks ties in favour of specialised plans; `verb:plan-inspect` shows the matched plan's scope explicitly.
- **Risk: plans embed stale assumptions about link types.** A plan authored with `link_types: [implements-heuristic]` hand-picked becomes stale when vocabulary extends. **Mitigation**: `purpose` abstraction auto-adapts; linter warns on literal link_types in new plans.
- **Risk: the `$stepN.field` reference model has undefined behaviour across plan-runner versions.** A plan authored today references a step output shape that a future runner changes. **Mitigation**: plan_json schema version field (unused at v1, present for future migration). Out-of-scope implementation detail but worth reserving the column.

### Failure Modes

- **Plan with broken DAG ships to production.** CI schema validation (Phase 6 SC-15) catches obvious structure errors. Semantic errors (wrong `purpose`, bad binding shape) surface at first execution; `success_count/failure_count` counters expose repeat failures for promotion-candidate scoring.
- **Plan match returns high-confidence but wrong plan.** Description was crisp but misleading. Agent executes it, user pushes back. `plan_run` failure or explicit rejection increments `failure_count`; over time the plan ranks lower or gets explicitly archived. No crash; slow quality degradation trajectory.
- **Dimension registry diverges across forks.** Two teams add the same dimension name with different semantics. **Mitigation**: dimensions live in the plugin by default (`scope:global`); project overrides are additive; name collisions surface at load as duplicate-key warnings.
- **`plan_save` during a long-running session never reaches T1 cache due to server crash mid-session.** Row exists in T2 but T1 upsert failed. Next SessionStart rebuild picks it up. Short-lived inconsistency, auto-recovered.

## Success Criteria

- **SC-1** — `plan_match` MCP tool lands. Given a plan with description *"how does projection quality work"* saved to the library, `plan_match("what's the mechanism for projection quality hub suppression")` returns that plan above the configured `min_confidence` threshold (default 0.85 per PQ-2, calibrated during implementation against a 20-query paraphrase set before SC-1 is claimed met). Exact-token variants return the plan from FTS5 (`plan_search`) as the fallback path (SC-11 covers Match.from_plan_row construction).
- **SC-2** — T1 `plans__session` collection is populated at SessionStart from T2 using the TTL filter in `plan_library.py:195-197`. After SessionStart, `COUNT(T1 plans__session) == COUNT(T2 plans WHERE outcome='success' AND (ttl IS NULL OR julianday('now') - julianday(created_at) <= ttl))`. A `plan_save` during the session upserts to T1 immediately via the commit hook; the new row is visible to `plan_match` within the same session without a restart.
- **SC-3** — Plan step schema accepts the `scope` field with `taxonomy_domain` ∈ {`prose`, `code`} and per-domain `topic=`. The plan runner forwards scope to the correct retrieval tool and corpus set. Cross-embedding cosine is never computed; verifiable by grep.
- **SC-4** — Plan step schema accepts `{tool: "traverse", args: {seeds, link_types, depth, direction, return}}`. Depth is capped at 3. `seeds` accepts both literal tumbler lists and `$step_N` references. The runner resolves both cases and returns the agreed shape.
- **SC-5** — `traverse` operator dispatches through `Catalog.graph_many()` (the thin multi-seed wrapper defined in Phase 3 with node-key = `str(tumbler)` and edge-key = `(from, to, link_type)` dedup invariants, honouring `_MAX_GRAPH_NODES` across the merged frontier) for seed lists, or through `Catalog.graph()` directly for single-seed cases — no new BFS algorithm. Contract tests pin the merge invariants against the catalog's existing link data. Returning `collections` from a traverse step usable as `subtree=` / explicit `corpus=` input to a downstream retrieval step is end-to-end tested.
- **SC-6** — Five scenario plans seed via `nx catalog setup`. Four use at least one `traverse` step; `verb:debug` is the one intentionally-flat scenario (see Phase 4b). Reseeding is idempotent via the `(project, dimensions)` UNIQUE index.
- **SC-7** — Session-start hook injects a "## Plan Library" block listing `plan_match`, `plan_save`, `plan_search`, and the five scenario names. SubagentStart hook injects the plan-match-first preamble for the eight retrieval-shaped agents. Each agent's `nx/agents/<name>.md` cites the pattern independently (verifiable by grep).
- **SC-8** — End-to-end demo on ART repo: fresh session, user asks *"how does vision→language priming work in ART?"*, `nx:plan-first` skill fires, cold-library case runs `/nx:query` planner → plan saved; warm-library case resolves from `plan_match` with `confidence >= min_confidence` (the PQ-2 calibrated value) and executes the saved DAG via `plan_run`. At least one step in the resulting plan is a `traverse` that walks from the RDR to its implementing code via typed links.
- **SC-9** — Zero regressions. `plan_save` / `plan_search` / `/nx:query` unchanged in behavior for existing callers. `search()` / `query()` existing arg sets unchanged in behavior.
- **SC-10** — Cross-embedding boundary is not crossed anywhere in the plan runner. Every retrieval step operates in exactly one embedding space. `traverse` operates on catalog tumblers (no embeddings involved). **Enforced at runtime, not by grep**: (a) `plan_run` asserts that any step carrying `scope.taxonomy_domain` dispatches only to corpora whose embedding model matches the declared domain — mismatch raises `PlanRunEmbeddingDomainError`; (b) unit test `test_plan_runner_rejects_cross_embedding_step` pins the invariant; (c) `traverse` step signature is typed to accept only tumblers/ids/link-types, never embedding vectors, so the type system prevents accidental cosine on traversal output.
- **SC-11** — Plan template contract. `plan_match` accepts the four-axis signature (`intent`, `scope_preference`, `tag_filter`, `context`, plus `min_confidence` / `n`). `plan_run` accepts `(match, bindings)` and resolves `$var` placeholders + `$stepN.<field>` references; unresolved required bindings abort with a named error. Documented in `docs/plan-authoring-guide.md` and round-trip-tested with a small paraphrase set (≥20 intent variants → correct plan match + execution).
- **SC-12** — Metrics columns (`use_count`, `last_used`, `match_count`, `match_conf_sum`, `success_count`, `failure_count`) are added to `plans` via a T2 migration and updated atomically at the right call sites (match on `plan_match`, start/complete on `plan_run`). Counters do not persist across scope promotions.
- **SC-13** — Four meta-seeds ship at `scope:global`: `verb:plan-author, strategy:default`; `verb:plan-promote, strategy:propose`; `verb:plan-inspect, strategy:default`; `verb:plan-inspect, strategy:dimensions`. Each is callable via `plan_match`+`plan_run` and produces its documented output on a freshly-set-up catalog. `docs/plan-authoring-guide.md` exists and is catalog-indexed. (Revised 2026-04-15 per RDR-078 critique audit: prior text said "three" with noun-phrase verb names that did not exist in the dimensions registry; implementation shipped four with `plan-author`/`plan-promote`/`plan-inspect` verbs.)
- **SC-14** — Scoped plan loader covers all four non-personal tiers: `nx/plans/builtin/*.yml` (global), `docs/rdr/<slug>/plans.yml` (rdr-scoped, only for accepted/closed RDRs), `.nexus/plans/*.yml` (project), and an optional umbrella path for `scope:repo`. Schema validation rejects malformed YAML with a named error. Source-path / declared-scope mismatches log a warning and prefer the path. Re-running `nx catalog setup` is idempotent via the Phase 4c `UNIQUE INDEX idx_plans_project_dimensions ON plans(project, dimensions)` + `INSERT ON CONFLICT(project, dimensions) DO UPDATE` — dedup key is the canonical JSON dimension map, not `(name, scope)`.
- **SC-15** — Git-as-transport integrity. All four YAML paths are commit-indexed, plan schema CI check runs on PR, rollback via `git revert` restores prior plan state after a subsequent `nx catalog setup`. No hidden state lives outside T2 + git.
- **SC-16** — Purpose abstraction on `traverse`. Step schema accepts `purpose: <name>` resolving via registry, or `link_types: [...]` literal — specifying both is a validation error. `purposes_resolve(name, project, scope) → list[str]` is a pure function of registry state. Registry loads with the same scope cascade as Phase 6 plans.
- **SC-17** — `docs/catalog-link-types.md` and `docs/catalog-purposes.md` ship in the plugin, are catalog-indexed, and a paraphrase-set test (e.g. *"when do I use supersedes"*, *"what does implements-heuristic mean"*, *"which link type for walking documentation"*) resolves to the correct reference via `plan_match` / `query` with confidence ≥ 0.80.
- **SC-18** — Dimensional identity and currying. A plan's identity is the canonicalised dimension map (sorted, keys-lowercased); two plans with identical maps at load time reject with a named error citing both sources. `plan_run(match, bindings)` merges `match.default_bindings` under `caller.bindings` (caller wins). Currying lineage is inspectable via `verb:plan-inspect, strategy:default`.
- **SC-19** — Dimension registry. `nx/plans/dimensions.yml` ships with at least `verb`, `scope`, `strategy`, `object`, `domain` registered. Unregistered dimensions on a loaded plan emit a warning naming the file and the offending key; load still succeeds (lenient by default, strict mode opt-in via `NX_PLAN_STRICT_DIMENSIONS=1`). `verb:plan-inspect, strategy:dimensions` enumerates registered dimensions with per-dimension usage counts drawn from live T2.

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
- **PQ-5** — *(Resolved — see Phase 1 "Retrieval step output contract". Retrieval steps always emit `{tumblers, ids, distances}`; operators emit `{text, citations}`. The standardised-output option won over the per-step `emit:` hint.)*
- **PQ-6** — Failed-plan filter in `plan_match`. Default `outcome='success'` is correct. Should traverse-heavy plans carry a different success signal (e.g., "at least one typed-link traversal returned a non-empty neighbourhood")? Defer; the existing outcome field starts coarse.
- **PQ-7** — Drift between T2 `plans` and T3 `plans__semantic`. Opt-in periodic reindex via `nx catalog setup --reindex-plans`. Not required for first iteration.
- **PQ-8** — Scenario plan portability across projects. Plans ship project-neutral (templates); agent customises per-project before saving a project-scoped variant. Substitution mechanism (`$var` references in `plan_json`) needs a small runner contract.
- **PQ-9** — Verbs as emergent taxonomy. Today the `verb:*` tag convention is human-curated. Future work: apply HDBSCAN-style clustering to the `plans.query` embeddings themselves, so recurrently-phrased intents surface as candidate verb clusters. Out of scope for this iteration; naming it so the first agent that asks doesn't rediscover the question.
- **PQ-10** — Cache scope: all plans or just verbs. Starts with all `outcome='success'` plans in T1. Revisit if T1 memory footprint becomes an issue at 10k+ plans (unlikely — 1k plans × 1024d float32 embedding ≈ 4 MB).
- **PQ-11** — Cross-subagent visibility. RDR-041's T1 HTTP server lets a spawned subagent inherit the parent's session via PPID walking. The T1 `plans__session` cache persists for the parent session's lifetime; subagents see the same cache without re-populating. Name it so the behaviour is documented before it surfaces as a live-debug confusion.
- **PQ-12** — Promotion threshold calibration. Starting candidate thresholds (`use_count ≥ 3`, `match_count/use_count ≥ 0.7`, `success_count/(success+failure) ≥ 0.8`, `match_conf_avg ≥ 0.80`) are heuristic. Calibrate against 4-6 weeks of real plan usage and adjust in RDR-079 when the promotion CLI lands.
- **PQ-13** — Paraphrase-range ring buffer. Storing the last N distinct intent strings that matched each plan would sharpen the promotion signal ("this plan has handled 8 distinct phrasings, not just 1"). Additional T2 column (JSON array, capped length) is cheap. Scope decision for RDR-079; RDR-078 proves the base signal works first.
- **PQ-14** — Scope precedence specifics. Current model: `personal > rdr > project > repo > global` with a small multiplier (0.05 per step) so cosine dominates. Alternative: strict precedence — a confident match at a more-specific scope fully overrides a slightly-more-confident match at a broader scope. Calibrate with empirical paraphrase-set tests during implementation.
- **PQ-15** — Umbrella / monorepo conventions for `scope:repo`. Monorepos often have nested `.nexus/plans/` at the project level plus a repo-wide location. Default proposal: `.nexus/plans/_repo.yml` or `<repo-root>/.nexus/plans.repo.yml`. Pick one during implementation; document.
- **PQ-16** — RDR lifecycle plan bindings. What happens to `scope:rdr-<slug>` plans when the RDR closes as superseded or rejected? Proposed default: mark archived (soft); hide from `plan_match` unless `include_archived=true`. Still catalog-indexed for audit; promoted plans keep their higher-scope copies. The exact mechanism moves to RDR-079 (lifecycle hooks on RDR status changes); RDR-078 only loads plans for accepted/closed RDRs.
- **PQ-17** — Conflict resolution when the same dimensional identity appears at multiple scopes (e.g. `verb:research, strategy:default, scope:global` AND `verb:research, strategy:default, scope:project`). Default: both coexist because `scope` differs, so their canonical identity maps differ — scope precedence at lookup time settles which wins per-call. `plan_match` can return both if `n>1`. No implicit merge — team override is deliberately distinct from the global default.
- **PQ-18** — Dimension naming convention. Kebab-case vs snake_case (proposing kebab: `change-set`, `plan-author`). Singular vs plural for enum values. Convention should go in `docs/plan-authoring-guide.md`; code enforces only `[a-z0-9-]`.
- **PQ-19** — Multi-verb intent hits. When `plan_match(intent)` with no `verb` pinned returns plans across multiple verbs with close confidence (e.g. *"look at the auth code"* could be `verb:review`, `verb:analyze`, or `verb:explain`), does the skill pick top-1, list candidates, or ask? Current plan: top-1 with specificity tiebreak; if the match presents both verbs within 0.02 cosine, surface both to the caller.
- **PQ-20** — Specificity bonus weight. Proposed 0.05 per extra pinned dimension beyond the caller's filter. May need calibration against a paraphrase test set. Too-high weight overwhelms cosine; too-low weight makes specialised plans invisible.
- **PQ-21** — Purpose composition. Can a plan reference `purpose: implementation-plus-tests` resolving to the union of `find-implementations` and a hypothetical `find-tests`? Useful but more complex than 1:1 resolution; leave until a scenario needs it.
- **PQ-22** — Should `search(follow_links=...)` also accept `purpose=`? Same vocabulary applies; propagation is straightforward. Deferred to the follow-on RDR-079 to avoid Phase 4 scope creep.

## Finalization Gate

Checklist run at RDR acceptance time. Layer 1 is structural (run by `/nx:rdr-gate`); Layers 2-4 are author + reviewer responsibility.

### Contradiction Check

- [ ] No claim in Problem Statement is rebutted by a Research Finding.
- [ ] No SC asserts behaviour that a Trade-off identifies as a known failure mode.
- [ ] The "cross-embedding cosine is noise" invariant (RF-2) is consistent with every retrieval step in every Phase — no phase walks a cosine between code and prose embeddings.
- [ ] `plan_match` / `plan_run` signatures in the Vocabulary section match the arguments referenced in Phase 4 and Phase 5.

### Assumption Verification

- [ ] RDR-042 plan library schema (`plans` table + FTS5) exists and is live at `nexus.db.t2.plan_library` — verified by SQL introspection.
- [ ] T1 HTTP ChromaDB server (RDR-010) is reachable; spawned subagents inherit the parent's session via PPID walking (RDR-041) — verified by existing `tests/test_session*.py` and `tests/test_t1*.py` integration tests where present.
- [ ] `Catalog.graph(tumbler, depth, link_type)` returns `{nodes, edges}` as assumed in Phase 3 — verified by `nx catalog links --help` source in `src/nexus/catalog/catalog.py`.
- [ ] Five retrieval-shaped agents (Phase 5 list) exist in `nx/agents/` — verified by glob.
- [ ] HDBSCAN topic discovery (RDR-070) is live and populates `topics` + `topic_assignments` tables — verified by `nx taxonomy status`.
- [ ] Voyage AI embedding models are accessible at plan-save time — fallback to FTS5 if T1 cannot embed.

#### API Verification

- [ ] `ChromaDB.Collection.query(query_embeddings, n_results, where=...)` supports metadata filter with `tag_filter` glob emulation — confirmed.
- [ ] SQLite FTS5 tokenizer on the `plans` FTS table matches on tags (convention `verb:*`) — confirmed via existing `plan_search` implementation.
- [ ] `nx catalog setup` already seeds 5 builtin plans via `plan_save` — additive seed loop for the new scenarios + meta-seeds does not re-author the mechanism.

### Scope Verification

- [ ] RDR does not implement `nx plan promote` CLI, `nx plan audit` CLI, RDR lifecycle hooks, or `search(purpose=)` / `query(follow_links=purpose)` propagation. All four are explicitly deferred to RDR-079 in the Out of Scope section.
- [ ] RDR does not modify RDR-042's shipped `plan_save` / `plan_search` / `/nx:query` behaviour for existing callers (SC-9).
- [ ] RDR does not introduce a cross-embedding bridge mechanism (SC-10, RF-2).
- [ ] Phases 1-6 count as one shippable unit; no phase is described as optional within this RDR (but individual phases are delivery-orderable as PRs).

### Cross-Cutting Concerns

- [ ] **Security**: plan_json is executed by the plan-runner with caller bindings. Caller bindings are not interpolated as shell/SQL — they parameterise tool args whose own contracts validate input. No new injection surface.
- [ ] **Privacy**: `scope:personal` plans stay in T2; never committed to git. Scope tagging is enforced at load-from-YAML time (Phase 6 source-path check).
- [ ] **Performance**: T1 plan cache size is bounded by `COUNT(plans WHERE outcome='success')` × 1024d float32 ≈ 4 MB at 1000 plans. SessionStart population is O(N) with one batched embed request. Acceptable.
- [ ] **Observability**: metrics counters (SC-12) provide the promotion-candidate signal without new logging infrastructure.
- [ ] **Reversibility**: every Phase is additive. Rollback = `git revert` + `nx catalog setup` + reseed. T2 schema additions (verb/scope indexed columns + metrics + currying fields) are nullable; existing rows continue working.

### Proportionality

- [ ] Scope fits one arc of implementation (2-4 weeks expected). Five phases' worth of work, each 1-5 days.
- [ ] Deferred items (RDR-079) are named and bounded; this RDR does not promise lifecycle ops or surface alignment it cannot deliver.
- [ ] Success is measurable: SC-1 (paraphrase-match > 0.80), SC-4 (traverse seed resolution), SC-8 (ART end-to-end demo) are concrete.

## Out of Scope (deferred, each may spawn its own RDR)

- **Retrieval surface alignment** (`search()` ↔ `query()` parity) — plumbing debt, not paper-grounded. Plans compose tools as-is; agents call whichever tool fits each step. File as a separate tidy-up RDR if the duplication starts hurting.
- **Projection → catalog link promotion.** Within-same-model code↔code bridges are a sidequest. Cross-embedding projection is ruled out by RF-2 regardless.
- **Heuristic linker strengthening** (module/symbol/path extraction from RDR body). Would address the 0.8%/7.9% per-workspace heuristic-linker recall measured during the RDR-078 discovery sweep. Directly improves Phase 3 traversal neighbourhoods, so worth doing — but as its own RDR since it's independently scoped.
- **RDR-077 `--use-icf` bootstrap failure post-mortem.** RF-3 cites it; separate document in `docs/rdr/post-mortem/077-use-icf-bootstrap-amplification.md` should capture the mechanism.
- **Link graph UI.** Out of scope across all iterations.
- **Per-project configuration of hub stopwords** (RDR-077 PQ-3). Orthogonal.
- **New link types beyond the catalog's existing vocabulary.** `semantic-implements`, `documented-by`, `tests` were proposed in earlier drafts; Phase 3 works with the existing `implements` / `cites` / `relates` / `supersedes` / `implements-heuristic` set. Additional types can land later if scenario plans reveal concrete need.

## References

### RDRs

- **RDR-042** — *AgenticScholar-Inspired Enhancements* (closed). Shipped the analytical-operator agent, plan library (T2 `plans` table + FTS5), `/nx:query` skill, self-correction loop. RDR-078 picks up its two explicit deferrals.
- **RDR-010** — *T1 Scratch Persistent Bounded Store*. The T1 HTTP ChromaDB server startup + fallback-to-EphemeralClient warning (`src/nexus/session.py:220-277`).
- **RDR-041** — *T1 Scratch Inter-Agent Context* (closed). Session ID routing + PPID-based inheritance so spawned subagents share the parent's T1 scratch. RDR-078 Phase 1 relies on this for cross-subagent visibility of the plan semantic cache.
- **RDR-050** — *Catalog-First Query Routing*. Establishes `query(follow_links=...)` + `author=` + `content_type=` + `subtree=` routing primitives. RDR-078 Phase 3 wraps the same `Catalog.graph()` BFS into a plan step.
- **RDR-053** — *Xanadu Fidelity*. Link-graph design doctrine — typed edges, `chash:` spans, provenance via `created_by`. RDR-078 relies on the existing vocabulary.
- **RDR-063** — *T2 Domain Split*. `CatalogTaxonomy` / `PlanLibrary` / `MemoryStore` / `Telemetry` stores. RDR-078's plan metrics migration extends `PlanLibrary`.
- **RDR-070** — *HDBSCAN Topic Discovery*. Produces the per-corpus topic taxonomies that Phase 2 scope references.
- **RDR-075** — *Cross-Collection Topic Projection*. The projection pipeline whose bootstrap failure mode (cited as RF-3/RF-4) rules out projection-as-cross-embedding-bridge.
- **RDR-077** — *Projection Quality: Similarity + ICF Hub Detection*. The ICF write-time amplification finding informs the "plans must compose per-embedding-space steps" invariant (SC-10).
- **RDR-079** (planned) — *Plan Lifecycle Operations*. Will carry `nx plan promote`, `nx plan audit`, RDR-accept/close hooks, `search(purpose=...)` propagation, and threshold calibration from live data.

### External

- AgenticScholar — *"Agentic Data Management with Pipeline Orchestration for Scholarly Corpora"*, arXiv 2603.13774. Indexed as nexus corpus `knowledge__agentic-scholar` (172 chunks, 54 pages). Qualitative claims on KG Traverse vs. plan reuse separation cite the research-2 audit (`nexus_rdr/078-research-2`).

### Research Findings (stored in T2)

- `nexus_rdr/078-research-1` — Per-workspace heuristic-linker recall measurement; cross-embedding projection empirical zero-match result.
- `nexus_rdr/078-research-2` — AgenticScholar paper audit: paper's +47% NDCG delta attribution analysis; phase-to-paper mapping; verb-list gap analysis.
- `nexus_rdr/078-research-3` — Verb-registry-as-tagged-plans insight; T1 session cache architecture (replaces the T3 proposal).
- `nexus_rdr/078-research-4` — Templating + four-scope tier model + feedback-loop metrics design.
- `nexus_rdr/078-research-5` — Dimensional plan identity, currying via `default_bindings` + `parent`, purpose abstraction over link types.

### Code and schema anchors

- `src/nexus/db/t2/plan_library.py` — existing RDR-042 plan library implementation.
- `src/nexus/catalog/catalog.py` — existing `Catalog.graph()` BFS used by Phase 3.
- `src/nexus/search_engine.py:152-163` — `search_cross_corpus()` kwargs.
- `src/nexus/mcp/core.py:178` — `query()` MCP tool.
- `src/nexus/mcp/core.py:53` — `search()` MCP tool.
- `nx/agents/query-planner.md`, `nx/agents/analytical-operator.md` — existing RDR-042 agents.
- `nx/hooks/scripts/session_start_hook.py`, `nx/hooks/scripts/subagent-start.sh` — priming integration points (Phase 5).
- `nx/resources/rdr/TEMPLATE.md` — RDR template this document conforms to.

## Revision History

- **2026-04-14** — Initial draft (scope: projection → link promotion).
- **2026-04-14** — Revision 2: scrapped projection-centric framing; reframed around plan-match + typed-graph traversal after the ART live demo surfaced RF-2 (cross-embedding cosine is noise) and RF-3 (--use-icf bootstrap amplification).
- **2026-04-14** — Revision 3: applied paper-audit findings (research-2) — added Phase 3 catalog-Traverse as quality lever; cut Phase 4 (surface alignment); renamed Phase 2 to domain-scoped retrieval steps.
- **2026-04-15** — Revision 4: T1 session cache replaces T3 plans__semantic collection (research-3). Added four scope tiers (personal/rdr/project/repo/global) with git as transport.
- **2026-04-15** — Revision 5: templating vocabulary + `plan_run` named tool (research-4). Phase 4 rewritten into 4a-4e (schema + scenarios + metrics + meta-seeds + authoring guide). Phase 6 added.
- **2026-04-15** — Revision 6: dimensional identity (research-5). Scope/verb/name collapse into pinned dimension map. Currying via `default_bindings` + `parent`. Purpose abstraction over link types. Skills collapse to pure verbs.
- **2026-04-15** — Revision 7: formal structure — Problem Statement with `#### Gap N:` enumeration, `## Context`, `## Alternatives Considered`, `## Trade-offs`, `## Finalization Gate`, `## References`, `## Revision History`. Content unchanged; structure conforms to `nx/resources/rdr/TEMPLATE.md`.
- **2026-04-15** — Revision 8 (this): gate-fix pass against `nexus_rdr/078-critique-gate` findings. Fixes 4 critical (C-1..C-4) and 5 significant (S-1..S-5) issues, all code-vs-design mismatches: (C-1) multi-seed `traverse` wrapper `Catalog.graph_many()` explicitly spec'd with node/edge dedup invariants; (C-2) `description` concept persisted via existing `plans.query` column — no schema rename; (C-3) SessionStart SQL replaced with correct `julianday()`-based TTL filter matching `plan_library.py:195-197`; (C-4) identity-dedup key made concrete via new `dimensions TEXT` column + UNIQUE `(project, dimensions)` index, canonical-JSON serialisation, `ON CONFLICT DO UPDATE` reseeding; (S-1) `Match.from_plan_row()` constructor spec'd for FTS5 fallback with `confidence=None` sentinel; (S-2) `$stepN.<field>` output contract resolved as design — retrieval steps emit `{tumblers, ids, distances}`, operators emit `{text, citations}`; (S-3) SC-10 now enforced at runtime via `PlanRunEmbeddingDomainError` + typed `traverse` signature + unit test; (S-4) `plan_run` execution model declared deterministic (no agent dispatch); (S-5) SC-1/SC-8 thresholds reference PQ-2 calibration rather than hardcoded numbers. Observations (O-3..O-5) addressed: references split correctly across RDR-010 + RDR-041 for T1 server lineage; `purpose:all-implementations` semantics clarified with `purposes_resolve` warn-and-drop on unknown types; `verb:debug` flat-scenario framing made definitive.
