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
[plan_match]  ─── T1 cosine (plans__session cache) ─► hit → Match(confidence≥0.40)
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
[plan_run]  execute steps: search, query, traverse, store_get_many, operator_*
  │
  ▼
[record]  nx_answer_runs table — duration, cost, step count, final text
  │
  ▼
final text
```

No step here is optional.  The record step runs even on plan-miss + planner-
failure paths so every invocation leaves a trace.

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

## The 9 builtin scenario templates

`nx catalog setup` seeds these from `nx/plans/builtin/*.yml` as
`scope:global` plans:

| Template | Verb | What it does |
|----------|------|--------------|
| `research-default` | research | Walks prose corpus → `traverse` to implementing code → hydrate → summarise with citations |
| `review-default` | review | Resolves changed files → walks `decision-evolution` edges → extracts decisions → compares vs proposed change |
| `analyze-default` | analyze | Gathers prose + code → walks `reference-chain` → ranks by criterion → summarises |
| `debug-default` | debug | Resolves failing path to catalog → hydrates authoring RDRs → summarises design context |
| `document-default` | document | Prose search + code search → walks `documentation-for` → compares doc-coverage |
| `plan-author-default` | plan-author | Fetches authoring guide + dimension registry → surveys prior art → generates plan template |
| `plan-inspect-default` | plan-inspect | Looks up plan metrics (use_count, match_count, success/failure) |
| `plan-inspect-dimensions` | plan-inspect (variant) | Enumerates the dimension registry + usage |
| `plan-promote-propose` | plan-promote | Ranks plans worth promoting from personal → project → global |

The first 5 are the "verb" scenarios — they correspond to the 5 RDR-078
verb skills (`/nx:research`, `/nx:review`, …).  The last 4 are meta-seeds
for the plan-library itself.

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
    budget_usd=0.50,  # reserved for future enforcement
)
```

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

`traverse` is the load-bearing addition from RDR-078 — it makes typed
links between documents first-class operators in a plan.  Before, link
traversal was a post-hoc step after retrieval; now the plan itself can
say "starting from the search hits, walk `implements` edges to depth 2".

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
