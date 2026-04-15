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

Six phases. Each builds on RDR-042's shipped substrate. Phase 3 carries the paper's quality lever (typed-graph traversal); Phase 6 carries the shipping-velocity story (scoped plan loading).

### Vocabulary: plans are multi-dimensional templates

A plan is a **template** — a reusable DAG of operator steps with named `$var` placeholders. The plan library is a **template registry** selected by semantic intent against a bag of pinned dimensions, with ranking by description cosine.

**Identity is a pinned dimension set, not a flat name.** A plan's identity is the map of dimension → value that it pins. Two plans with identical pinned sets collide; `name` is a human-facing disambiguator, not part of identity. Scope, verb, strategy, and others are just well-known dimensions — none is structurally privileged.

**Three strings, three jobs:**

- **`description`** — prose authored when the plan is saved, describing *when to use it*. Embedded at SessionStart; what `plan_match` ranks on via cosine.
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
- **T1 holds the session semantic cache.** New collection `plans__session` (via RDR-041's T1 HTTP server) populated at SessionStart: `SELECT id, query, plan_json, tags FROM plans WHERE outcome='success' AND (ttl_days = 0 OR expires_at > now)` → embed the query text → upsert one document per plan with `metadata={plan_id, verb_name, handler_kind, tags, project, ttl, last_used}`.
- **`plan_match`** — signature in the Vocabulary section above. T1 cosine over plan descriptions, returns ranked `Match` objects with `{plan_id, name, description, confidence, scope, verb, tags, plan_json, required_bindings, optional_bindings}`. Only `outcome='success'` plans are loaded, so no explicit failure filter needed at call time.
- **`plan_run`** — new MCP tool (signature in Vocabulary section above). Takes a `Match` plus a `bindings` dict, resolves `$var` references in `plan_json`, executes the DAG via the analytical-operator + retrieval-tool stack. Unresolved required bindings abort with a named error; optional bindings default to empty/null. `$stepN.<field>` references resolve from prior step outputs via T1 scratch (the RDR-041 pattern RDR-042 already uses).
- **Fallback path.** When T1 is unavailable (EphemeralClient in tests, or session server failed to start), `plan_match` degrades to `plan_search` FTS5 over T2. Tests get deterministic behavior; production gets the semantic path.
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

**Implementation** reuses `Catalog.graph()` (already in `src/nexus/catalog/catalog.py` per `nx catalog links --help`). No new storage, no new algorithm. The work is exposing the existing BFS as a `{tool: "traverse"}` plan step, plus contract tests against the catalog's existing link data.

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
| `all-implementations` | `implements, implements-heuristic, semantic-implements` | When the semantic tier ships in a future RDR |

Schema validator rejects `link_types` and `purpose` specified together. `purpose` is the recommended form for new plans and scenario seeds.

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

Each plan pins `{verb, scope:global, strategy:default}` at minimum. Descriptions are exemplary — good enough to learn from by reading. All but the last use at least one `traverse` step.

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

-- Currying
ALTER TABLE plans ADD COLUMN default_bindings TEXT;   -- JSON object
ALTER TABLE plans ADD COLUMN parent_dims TEXT;        -- JSON object (parent's dimension map)
```

`verb` and `scope` get dedicated indexed columns because they are the high-frequency filters. All other dimensions serialize as `k:v` entries into the existing `tags TEXT` column. The canonical dimension map is reconstituted at query time from `(verb, scope, tags)`. Existing RDR-042 rows migrate with `verb=NULL, scope='personal'` and continue to work through the legacy `plan_search` FTS5 path until they're promoted or deprecated.

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
    1. **Populate T1 `plans__session` semantic cache.** Read `SELECT id, query, plan_json, tags FROM plans WHERE outcome='success' AND (ttl_days=0 OR expires_at>now)` → embed query text → upsert to T1 collection `plans__session` with metadata `{plan_id, verb_name, handler_kind, tags, project, ttl, last_used}`. Skipped gracefully if T1 server unavailable (fallback to FTS5 at match time). Log the populated count.
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
- Idempotency via `(name, scope)` dedup: re-running `nx catalog setup` updates the existing T2 row rather than duplicating.
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

## Success Criteria

- **SC-1** — `plan_match` MCP tool lands. Given a plan with query *"how does projection quality work"* saved to the library, `plan_match("what's the mechanism for projection quality hub suppression")` returns that plan with cosine confidence > 0.80. Exact-token variants return the plan from FTS5 (`plan_search`) with equivalent or higher confidence.
- **SC-2** — T1 `plans__session` collection is populated at SessionStart from T2. After SessionStart, `COUNT(T1 plans__session) == COUNT(T2 plans WHERE outcome='success' AND (ttl_days=0 OR expires_at>now))`. A `plan_save` during the session upserts to T1 immediately via the commit hook; the new row is visible to `plan_match` within the same session without a restart.
- **SC-3** — Plan step schema accepts the `scope` field with `taxonomy_domain` ∈ {`prose`, `code`} and per-domain `topic=`. The plan runner forwards scope to the correct retrieval tool and corpus set. Cross-embedding cosine is never computed; verifiable by grep.
- **SC-4** — Plan step schema accepts `{tool: "traverse", args: {seeds, link_types, depth, direction, return}}`. Depth is capped at 3. `seeds` accepts both literal tumbler lists and `$step_N` references. The runner resolves both cases and returns the agreed shape.
- **SC-5** — `traverse` operator uses `Catalog.graph()` for BFS; no new graph-walking code. Returning `collections` from a traverse step usable as `subtree=` / explicit `corpus=` input to a downstream retrieval step is end-to-end tested.
- **SC-6** — Five scenario plans seed via `nx catalog setup`. Each uses at least one `traverse` step (except where the scenario is intentionally flat — `debug-context` is the only candidate). Reseeding is idempotent.
- **SC-7** — Session-start hook injects a "## Plan Library" block listing `plan_match`, `plan_save`, `plan_search`, and the five scenario names. SubagentStart hook injects the plan-match-first preamble for the eight retrieval-shaped agents. Each agent's `nx/agents/<name>.md` cites the pattern independently (verifiable by grep).
- **SC-8** — End-to-end demo on ART repo: fresh session, user asks *"how does vision→language priming work in ART?"*, `nx:plan-first` skill fires, cold-library case runs `/nx:query` planner → plan saved; warm-library case resolves from `plan_match` with confidence ≥ 0.80 and executes the saved DAG. At least one step in the resulting plan is a `traverse` that walks from the RDR to its implementing code via typed links.
- **SC-9** — Zero regressions. `plan_save` / `plan_search` / `/nx:query` unchanged in behavior for existing callers. `search()` / `query()` existing arg sets unchanged in behavior.
- **SC-10** — Cross-embedding boundary is not crossed anywhere in the plan runner. Every retrieval step operates in exactly one embedding space. `traverse` operates on catalog tumblers (no embeddings involved). Verifiable by grep.
- **SC-11** — Plan template contract. `plan_match` accepts the four-axis signature (`intent`, `scope_preference`, `tag_filter`, `context`, plus `min_confidence` / `n`). `plan_run` accepts `(match, bindings)` and resolves `$var` placeholders + `$stepN.<field>` references; unresolved required bindings abort with a named error. Documented in `docs/plan-authoring-guide.md` and round-trip-tested with a small paraphrase set (≥20 intent variants → correct plan match + execution).
- **SC-12** — Metrics columns (`use_count`, `last_used`, `match_count`, `match_conf_sum`, `success_count`, `failure_count`) are added to `plans` via a T2 migration and updated atomically at the right call sites (match on `plan_match`, start/complete on `plan_run`). Counters do not persist across scope promotions.
- **SC-13** — Three meta-seeds ship at `scope:global`: `verb:plan-authoring`, `verb:plan-propose-promotion`, `verb:plan-inspect`. Each is callable via `plan_match`+`plan_run` and produces its documented output on a freshly-set-up catalog. `docs/plan-authoring-guide.md` exists and is catalog-indexed.
- **SC-14** — Scoped plan loader covers all four non-personal tiers: `nx/plans/builtin/*.yml` (global), `docs/rdr/<slug>/plans.yml` (rdr-scoped, only for accepted/closed RDRs), `.nexus/plans/*.yml` (project), and an optional umbrella path for `scope:repo`. Schema validation rejects malformed YAML with a named error. Source-path / declared-scope mismatches log a warning and prefer the path. Re-running `nx catalog setup` is idempotent per `(name, scope)`.
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
- **PQ-5** — Traverse `seeds` referencing `$step_N` requires consistent output shape across retrieval operators. What does `search` emit when a downstream `traverse` expects tumblers? Add `emit: "tumblers"` hint on the source step, or standardise the retrieval output to always carry tumblers. Lean toward the latter — the catalog already tracks tumbler-per-chunk via the `chunks` metadata.
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

## Out of Scope (deferred, each may spawn its own RDR)

- **Retrieval surface alignment** (`search()` ↔ `query()` parity) — plumbing debt, not paper-grounded. Plans compose tools as-is; agents call whichever tool fits each step. File as a separate tidy-up RDR if the duplication starts hurting.
- **Projection → catalog link promotion.** Within-same-model code↔code bridges are a sidequest. Cross-embedding projection is ruled out by RF-2 regardless.
- **Heuristic linker strengthening** (module/symbol/path extraction from RDR body). Would address the 0.8%/7.9% per-workspace heuristic-linker recall measured during the RDR-078 discovery sweep. Directly improves Phase 3 traversal neighbourhoods, so worth doing — but as its own RDR since it's independently scoped.
- **RDR-077 `--use-icf` bootstrap failure post-mortem.** RF-3 cites it; separate document in `docs/rdr/post-mortem/077-use-icf-bootstrap-amplification.md` should capture the mechanism.
- **Link graph UI.** Out of scope across all iterations.
- **Per-project configuration of hub stopwords** (RDR-077 PQ-3). Orthogonal.
- **New link types beyond the catalog's existing vocabulary.** `semantic-implements`, `documented-by`, `tests` were proposed in earlier drafts; Phase 3 works with the existing `implements` / `cites` / `relates` / `supersedes` / `implements-heuristic` set. Additional types can land later if scenario plans reveal concrete need.
