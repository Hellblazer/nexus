# Plan-centric retrieval

Nexus's retrieval layer is organised around **plans** — saved query+DAG pairs
that capture how to answer a class of questions.  When you ask a question,
the system tries to match it to an existing plan first, executes the plan,
and only falls through to ad-hoc decomposition when no plan fits.

This is the user-facing explainer.  The architectural decision is in
[RDR-078](rdr/rdr-078-unified-context-graph-and-retrieval.md); the schema
for authoring plan templates is in [plan-authoring-guide.md](plan-authoring-guide.md).

## The retrieval trunk

Every call to `mcp__plugin_nx_nexus__nx_answer(question, ...)` runs this
sequence:

```
question
  │
  ▼
[plan_match]  ─── T1 cosine (plans__session cache) ─► hit → Match(confidence≥min)
  │           └── FTS5 fallback (when T1 empty) ────► hit → Match(confidence=None)
  │
  │  (no match)
  │
  ▼
[inline planner]  claude -p with DAG schema → synthetic Match
  │
  ▼
[classify]  ─ single_query? / needs_operators? / multi-step?
  │
  ▼
[plan_run]  execute steps: search / query / traverse / store_get_many
            isolated; contiguous operator runs (extract/rank/compare/
            summarize/generate) bundled into one claude -p call
  │
  ▼
[record]  nx_answer_runs table — duration, cost, step count, final text
          plus plans.use_count / success_count / failure_count on
          matched library plans
  │
  ▼
final text
```

No step here is optional.  The record step runs even on plan-miss + planner-
failure paths so every invocation leaves a trace.

### Operator bundling (v4.10.0)

Contiguous runs of ≥2 operator steps collapse into a **single**
`claude -p` subprocess instead of N. Measured wins on real corpora:
`extract → summarize` ~55% faster, `extract → rank` on real RDRs ~28%
faster and with materially better ranking quality, 4-op cross-repo
compare 72% faster (192s → 54s). Retrieval steps stay isolated.

Key mechanics:

- **Contract**: `nexus.plans.bundle.segment_steps` is the sole bundle-
  boundary detector. `compose_bundle_prompt` builds a single composite
  prompt describing all N steps; `dispatch_bundle` issues one
  `claude_dispatch`.
- **Intra-bundle refs**: `$stepN.<field>` references that point inside
  the bundle return a deferred-ref sentinel from `_resolve_value`; the
  composer renders them as `"the <field> output from STEP M"` prose
  using bundle-local step numbering.
- **Parallel branches**: when a bundle has two extracts that hydrate
  from different retrieval steps, the prompt emits a `source:` line
  per extract with the pre-hydration collection name so the LLM can
  attribute cross-corpus extractions correctly.
- **Size guard**: `MAX_BUNDLE_PROMPT_CHARS = 200_000`. Oversized
  bundles fall back to per-step dispatch.
- **Opt-out**: `plan_run(..., bundle_operators=False)` recovers per-
  step isolation for debugging.
- **Dispatcher opt-in**: custom dispatchers need `supports_bundling=True`
  as an attribute to be routed through the bundle path — the default
  dispatcher carries it; wrappers inherit or declare their own.
- **Eligibility**: only operators in `BUNDLEABLE_OPERATORS` bundle;
  new operators opt in explicitly only if pure, cost-bounded, and
  failure-meaningful at bundle granularity.

Both lanes of `plan_match` (T1 cosine, T2 FTS5) key off the same
`match_text` payload synthesised at save time; see
[§match_text synthesis](#match_text-synthesis) below.

The default `min_confidence` floor is `0.40` (RDR-079 P5 calibration);
callers can pass `min_confidence=<float>` to `nx_answer` for a
per-call override (RDR-092 Phase 2 Option A). Verb skills that
validated a stricter precision-first floor (0.50 per R9's 5+5 probe
corpus) opt in this way without moving the global knob.

## Plans have dimensions

Every plan in the library pins a **dimensional identity**:

| Dimension | Required | Example values |
|-----------|----------|----------------|
| `verb` | yes | `research`, `review`, `analyze`, `debug`, `document`, `plan-author`, `plan-inspect`, `plan-promote` |
| `scope` | yes | `global`, `project`, `rdr-<slug>`, `personal` |
| `strategy` | default `"default"` | `default`, `security`, `performance`, `propose`, `dimensions` |
| `object` | optional | `change-set`, `rdr`, `module`, `test-suite` |
| `domain` | optional | `security`, `compliance`, `ml-systems`, `frontend` |

The `(project, dimensions)` pair is UNIQUE — two plans with the same
dimensions in the same project will collide at seed time.  This is how
the seed loader stays idempotent: a second run of `nx catalog setup` with
unchanged templates produces zero inserts.

## The 12 builtin scenario templates

`nx catalog setup` seeds these from `nx/plans/builtin/*.yml` as
`scope:global` plans:

| Template | Verb + strategy | What it does |
|----------|-----------------|--------------|
| `research-default` | research / default | Walks prose corpus → `traverse` to implementing code → hydrate → summarise with citations |
| `review-default` | review / default | Resolves changed files → walks `decision-evolution` edges → extracts decisions → compares vs proposed change |
| `analyze-default` | analyze / default | Gathers prose + code → walks `reference-chain` → ranks by criterion → summarises |
| `debug-default` | debug / default | Resolves failing path to catalog → hydrates authoring RDRs → summarises design context |
| `document-default` | document / default | Prose search + code search → walks `documentation-for` → compares doc-coverage |
| `plan-author-default` | plan-author / default | Fetches authoring guide + dimension registry → surveys prior art → generates plan template |
| `plan-inspect-default` | plan-inspect / default | Looks up plan metrics (use_count, match_count, success/failure) |
| `plan-inspect-dimensions` | plan-inspect / dimensions | Enumerates the dimension registry + usage |
| `plan-promote-propose` | plan-promote / propose | Ranks plans worth promoting from personal → project → global |
| `find-by-author` | research / find-by-author | Catalog author-index lookup → hydrate → summarise an author's contribution surface |
| `citation-traversal` | research / citation-traversal | Resolve seed → walk `reference-chain` both directions → hydrate → summarise |
| `type-scoped-search` | research / type-scoped | Catalog content-type filter → semantic query within that bucket → summarise |

The first 5 are the "verb" scenarios: they correspond to the 5
RDR-078 verb skills (`/nx:research`, `/nx:review`, …). The next 4 are
meta-seeds for the plan-library itself. The last 3 (RDR-092 Phase 0a
migrations) replace the legacy `_PLAN_TEMPLATES` array retired from
`src/nexus/commands/catalog.py`; two further legacy shapes (provenance
and cross-corpus compare) were retired as redundant with
`research-default` and `analyze-default` respectively.

## match_text synthesis

Both lanes of `plan_match` key off a single payload:

- **T1 cosine:** the `plans__session` ChromaDB collection embeds each
  plan's `match_text` via the local ONNX MiniLM function.
- **T2 FTS5:** the `plans_fts` virtual table indexes `match_text`,
  `tags`, and `project`.

`match_text` is a hybrid string built from the plan's dimensional
identity (RDR-092 Phase 1 + Phase 3):

```
<description>. <verb> <name> scope <scope>
```

Examples:

| Row | match_text |
|-----|------------|
| `find-by-author` builtin | `Find documents attributed to a specific author... research find-by-author scope global` |
| A grown research plan named `compare-ranker-outputs` | `compare ranker outputs. research compare-ranker-outputs scope personal` |
| A legacy row whose `dimensions IS NULL` | raw `query` text only |

Design rationale, in order of decreasing weight:

1. **description-first keeps verb-accuracy honest.** The natural
   language prefix is what R10 verified against a baseline of
   raw-description embeddings: adding the suffix produced zero
   verb-accuracy regression while lifting the cosine score for
   queries that actually know the dimensional identity.
2. **dimensional suffix gives the cosine lane a reliable hook.** A
   caller that asks for `research find-by-author` hits the matching
   plan even when their phrasing does not overlap the description.
3. **scope appears only when populated.** `scope:personal` rows whose
   scope column is set get the full suffix; `scope IS NULL` rows drop
   `scope <…>` from the tail rather than emitting the literal word
   `None`.
4. **legacy fallback never breaks.** A row with `verb IS NULL` or
   `name IS NULL` still embeds its raw `query` text, so no signal is
   ever lost to an empty suffix.

The synthesiser lives at `nexus.db.t2.plan_library._synthesize_match_text`
and is called both from `save_plan` (so T2 FTS indexes the hybrid
form on every insert) and from the T1 session cache's `_upsert_row`
(so the cosine lane sees the same payload). Existing rows picked up
from pre-RDR-092 databases are backfilled by the 4.9.13 migration
`_add_plan_match_text_column`.

## Invocation patterns

### From a skill (verb-scoped)

Each verb skill dispatches `plan_match` scoped to its verb:

```
/nx:research "how does projection quality work?"
    │
    ▼  (under the hood)
plan_match(intent="...", dimensions={verb: "research"}, n=1, min_confidence=0.40)
    │
    ▼
plan_run(match, bindings={concept: "projection quality", limit: 10})
```

### Directly via nx_answer

For open-ended questions where you don't want to pre-commit to a verb:

```
mcp__plugin_nx_nexus__nx_answer(
    question="Compare our retrieval approach to Delos and AgenticScholar.",
    scope="global",
    context="recently changed: src/nexus/plans/, RDR-078",
    max_steps=6,
    budget_usd=0.50,       # reserved for future enforcement
    min_confidence=0.50,   # RDR-092 Phase 2 Option A, optional precision-first floor
)
```

The `min_confidence` kwarg defaults to `None`, which routes through
the global `_PLAN_MATCH_MIN_CONFIDENCE` constant (`0.40`, set by
RDR-079 P5). Passing an explicit float overrides both the `plan_match`
gate and the `_nx_answer_match_is_hit` check so a tighter precision
floor is honoured consistently.

### Via /nx:query

The `/nx:query` slash command is now a thin pointer to `nx_answer` —
convenience shortcut when you're in Claude Code.

## Plan operators (what plan_run can call)

Plans are DAGs of steps.  Each step has a `tool` and `args`.  Step N can
reference earlier output via `$stepK.<field>`.

| Tool | Step output contract |
|------|---------------------|
| `search` | `{ids, tumblers, distances, collections}` |
| `query` | `{ids, tumblers, collections}` |
| `traverse` | `{tumblers, ids, collections}` — walks the catalog link graph |
| `store_get_many` | `{contents, missing}` — batch hydration past the ChromaDB 300-record cap |
| `operator_extract` | schema-conforming dict (e.g. `{extractions: [...]}`) |
| `operator_rank` | schema-conforming dict (e.g. `{ranked: [...]}`) |
| `operator_compare` | schema-conforming dict |
| `operator_summarize` | schema-conforming dict (or `{summary: str}`) |
| `operator_generate` | schema-conforming dict (or `{text: str}`) |
| `operator_filter` | `{items: [...], rationale: [{id, reason}]}` — items is a subset of the input, one rationale row per input |
| `operator_check` | `{ok: bool, evidence: [{item_id, quote, role}]}` — role ∈ `supports` / `contradicts` / `neutral` |
| `operator_verify` | `{verified: bool, reason: str, citations: [str]}` — citations are span anchors in the evidence |

`traverse` is the load-bearing addition from RDR-078 — it makes typed
links between documents first-class operators in a plan.  Before, link
traversal was a post-hoc step after retrieval; now the plan itself can
say "starting from the search hits, walk `implements` edges to depth 2".

## Scope-aware matching

The matcher honors a `scope_preference` argument passed by
`nx_answer` (and any direct `plan_match` caller). It shapes which plans
clear the candidate gate and how the surviving plans rank. The mechanism
lives on a per-plan `scope_tags` column populated either by inference
from retrieval-step args or by an explicit kwarg to `plan_save`; see
[Plan Authoring Guide §`scope_tags`](plan-authoring-guide.md#scope_tags-matcher-routing)
for authoring specifics.

### Filter, boost, tie-break

After the existing T1 cosine + dimension post-filter, the matcher:

1. **Drops scope-conflicting plans.** A plan whose `scope_tags` is non-empty and none of whose tags prefix-match the caller scope is removed from the candidate pool before scoring.
2. **Boosts scope-matching plans.** Each surviving plan's rank score is `final_score = base_confidence * (1 + scope_fit_weight * scope_fit)` where `scope_fit ∈ {0.0, 1.0}` and `scope_fit_weight = 0.15`. `Match.confidence` keeps the raw cosine so `min_confidence` and downstream thresholds are unaffected.
3. **Tie-breaks on specificity.** When two plans land at the same `final_score`, the plan with fewer entries in `scope_tags` wins. Ties are uncommon in practice (cosine scores are near-continuous); the tie-break is mostly defensive.

Agnostic plans (`scope_tags=""`) remain in the pool with `scope_fit=0.0`
and compete on base cosine alone.

### Zero-candidate fallback

When every plan in the pool conflicts with the caller's scope, the
matcher returns `[]`. Upstream `nx_answer` treats this exactly like
"no plan matched at all" and falls through to `_nx_answer_plan_miss`,
which invokes the inline planner with the caller's `scope` as a prompt
hint. Saved plans that don't fit the caller's scope never degrade the
answer; they are simply absent from the selection.

### Prefix semantics

Scope strings are normalized before comparison (hash suffixes stripped
at save time, trailing globs stripped at match time), then matched with
`startswith` in either direction. Bare-family tags serve narrower
queries: tag `rdr__` matches caller scope `rdr__arcaneum`. Specific
tags are also reached by bare-family callers: tag `rdr__arcaneum`
matches caller scope `rdr__`.

Multi-corpus plans use intersect semantics: a plan tagged
`knowledge__delos,knowledge__arcaneum` passes when the caller scope
prefix-matches either tag, so bridging plans survive when the caller
picks one of them.

## When to author a new plan

Three signals that a class of questions needs its own plan template:

1. **FTS5 keeps missing**: the same intent in paraphrased form doesn't
   match an existing plan's description, so callers re-decompose the same
   DAG.  The `plan_match` metrics (`match_count`, `match_conf_sum`) will
   show this — high `match_count` with many calls falling through to the
   inline planner means you're re-inventing.

2. **Inline planner returns structurally identical DAGs**: if the
   `_nx_answer_plan_miss` path produces the same step list for 5 similar
   questions, capture it as a template.  The planner is doing work a saved
   plan could do for free.

3. **The DAG needs a specialised step ordering** that the generic planner
   won't intuit (e.g. "search code first, then walk `supersedes` edges
   back to the originating RDR, THEN hydrate").

Author a template in `nx/plans/builtin/<verb>-<strategy>.yml` for global
scope, or `.nexus/plans/*.yml` / `docs/rdr/<slug>/plans.yml` for
project / per-RDR scope.  The CI gate (`.github/workflows/plan-schema-check.yml`)
validates every plan template on PR.

## See also

- [RDR-078](rdr/rdr-078-unified-context-graph-and-retrieval.md) — architectural decision
- [Plan Authoring Guide](plan-authoring-guide.md) — template schema, dimensions, bindings
- [Catalog Link Types](catalog-link-types.md) — what edges `traverse` can walk
- [Catalog Purposes](catalog-purposes.md) — named link-type bundles
- [Querying Guide](querying-guide.md) — when to use each retrieval interface
- [MCP Servers](mcp-servers.md) — full tool catalog
