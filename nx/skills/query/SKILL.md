---
name: query
description: Use when questions require multi-step retrieval and analysis (extract, summarize, rank, compare, generate) over nx knowledge collections with plan decomposition and reuse.
effort: medium
---

# Query Skill

Three-path dispatch for analytical queries over nx knowledge. The skill routes each question to the simplest path that can answer it.

## When This Skill Activates

- **Cross-corpus consistency checks**: "do the docs and code agree on error handling?"
- **Structured extraction with comparison**: "extract API patterns from these papers and compare them"
- **Multi-source synthesis**: "search code, docs, and RDRs for authentication patterns, then rank by recency"
- **Evidence-grounded generation**: "generate a summary of distributed consensus approaches with citations"
- **Author/citation/provenance queries**: "what did Fagin write about the chase procedure?"

**Do not use** for:
- Simple lookups — `nx search` alone answers the question
- Single-corpus summarization — dispatch `analytical-operator` directly with `operation=summarize`


## Key Constraint

**The skill is the loop driver.** Subagents (`query-planner`, `analytical-operator`) cannot spawn other subagents. The skill dispatches them sequentially, resolves step references, and manages T1 scratch between dispatches.


## Three-Path Dispatch

### Routing Order

Evaluate **simultaneously**, not sequentially:

1. Does the question have **explicit catalog handles** (author name, content type, subtree prefix, citation/link signals)?
2. Does the question have **analytical signal words** (compare, extract, generate, synthesize, rank, contradictions, differences)?

| Catalog handles | Analytical signals | Route |
|---|---|---|
| Yes | No | **Path 1** — single `query()` call with catalog params |
| Yes | Yes | **Hybrid** — execute catalog-scoped `query()` calls, pass results to `analytical-operator` directly (skip planner) |
| No | No | **Path 1** via catalog probe, or broad search if probe misses |
| No | Yes | **Path 2** (template match) or **Path 3** (planner) |

The hybrid path avoids unnecessary planner dispatch for questions like "compare papers by Fagin and Bernstein" — two deterministic catalog-scoped queries plus one analytical operator.

### Path 1: Single-Tool (most questions)

The enhanced `query` MCP tool handles catalog-aware routing internally. Use this when the question maps to a single scoped retrieval.

**Detection**: The question has explicit catalog handles (author, content type, subtree, citation/link signals) without analytical signals. Or: no catalog handles but a catalog probe returns a match.

**Catalog probe**: Call `mcp__plugin_nx_nexus__catalog_search(query="{question}", limit=1)`. Route to Path 1 only if the result's `title` or `author` field contains a token from the question (not just any FTS5 match).

**Execution**: Call `query()` MCP directly with appropriate catalog params:

```
mcp__plugin_nx_nexus__query(
    question="{question}",
    author="{detected author}",
    content_type="{detected type}",
    subtree="{detected tumbler prefix}",
    follow_links="{detected link type}",
    depth={depth},
    corpus="{corpus}",
    limit=10
)
```

Present results directly. No planner, no T1 scratch, no plan save prompt.

**Examples**:
- "What did Vaswani write about attention?" → `query(question="attention mechanisms", author="Vaswani")`
- "Show me all code in the nexus repo" → `query(question="code patterns", subtree="1.1")`
- "What papers cite this RDR?" → `query(question="related work", follow_links="cites")`
- "Find knowledge about distributed consensus" → `query(question="distributed consensus", content_type="paper")`


### Path 2: Template Match (structured patterns)

Pre-built plan templates handle common multi-step patterns. Use this when the question matches a known template structure.

**Detection**: Check the T2 plan library for a matching template:

```
mcp__plugin_nx_nexus__plan_search(query="{question}", limit=3)
```

If a result with `builtin-template` in tags has a similar structure to the question, adapt and execute its plan.

**Execution**: Parse the template's `plan_json`, substitute parameters from the question, then execute the plan steps using Path 3's Step 3 execution loop below. The template matches when its primary operation type (compare, generate, extract) matches the question's intent.

**Reuse**: If Path 2 called `plan_search` but no template matched, pass those results as few-shot examples to Path 3 Step 1 instead of calling `plan_search` again.


### Path 3: Planner (novel analytical pipelines)

For questions requiring extract, compare, generate, or multi-step synthesis that don't match Path 1 or Path 2.

**Detection**: The question contains analytical signal words (compare, extract, generate, synthesize, rank, contradictions, differences) AND cannot be answered by a single `query()` call.

**Execution**:

#### Step 1: Plan Library Lookup

Search for similar prior plans:

```
mcp__plugin_nx_nexus__plan_search(query="{question}", project="{project}", limit=3)
```

Collect plans with `outcome="success"` as few-shot examples.

#### Step 2: Dispatch query-planner

```markdown
## Relay: query-planner

**Task**: Decompose the following analytical question into a step-by-step execution plan.
**Bead**: none

### Input Artifacts
- nx scratch: none
- nx memory: none
- Files: none

### Deliverable
A JSON execution plan with ordered steps.

### Quality Criteria
- [ ] Plan is valid JSON with "query" and "steps" fields
- [ ] First step is a search or catalog operation
- [ ] All step references use $step_N notation
- [ ] Plan has 2-4 steps

### Context Notes
**Question**: {user question verbatim}

**Few-shot plans** (adapt these patterns if they match):
{JSON array of few_shot_plans, or "none"}
```

Parse the JSON plan from the response. The planner returns a fenced ` ```json ` block — extract the content between the opening ` ```json ` and closing ` ``` ` fences before calling `json.loads()`.

**Single-step guard**: If the returned plan has only 1 step, it should have been Path 1. Execute it via `query()` MCP directly instead of the full pipeline.

#### Step 3: Execute Plan Steps

For each step in `plan["steps"]` in order:

**catalog_search / catalog_links / catalog_resolve**: Execute via the corresponding MCP tool. Write results to T1 scratch with tag `query-step,step-{N},{operation}`. Extract `physical_collection` values into `$step_N.collections`.

**search**: Execute via `mcp__plugin_nx_nexus__search`. Write results to T1 scratch.

**All other operations (extract, summarize, rank, compare, generate)**: Resolve `$step_N` inputs from T1 scratch, then dispatch `analytical-operator`:

```markdown
## Relay: analytical-operator

**Task**: Execute {operation} operation on the provided inputs.
**Bead**: none

### Input Artifacts
- nx scratch: step-{N} results (resolved and included below)

### Deliverable
Operation result written to T1 scratch with tag "query-step,step-{N},{operation}"

### Context Notes
**Step number**: {N}

**Operation payload**:
{JSON with operation, inputs, params}
```

Write the operator's output to T1 scratch.

**Error handling**: If an operator step fails, write a failure marker to scratch (`tags="query-step,step-{N},error"`). Continue executing remaining steps. Set `outcome = "partial"`.

#### Step 4: Present Results

Read the last step's output from T1 scratch. Present to the user:

```
**Query**: {original question}

**Result**:
{final step output}
```

#### Step 5: Auto-Cache Plan

**Only cache fully successful plans.** If any step failed (`outcome = "partial"`), do NOT cache — the next similar query will re-dispatch the planner fresh. Partial plans served from cache would repeat the same failure for up to 30 days.

When `outcome == "success"`:

```
mcp__plugin_nx_nexus__plan_save(
    query="{original question}",
    plan_json="{serialized plan JSON}",
    outcome="success",
    tags="{comma-separated operation types}",
    ttl=30
)
```

Plans are cached with `ttl=30` days. Builtin templates (from `nx catalog setup`) have no TTL.


## T1 Scratch Usage

T1 scratch is the cross-dispatch persistence mechanism for Path 3. Every step output is written here so subsequent steps can reference it.

| Tag pattern | Written by | Read by |
|-------------|-----------|---------|
| `query-step,step-{N},search` | Skill (after search MCP call) | Skill (resolving $step_N) |
| `query-step,step-{N},catalog_search` | Skill (after catalog_search) | Skill ($step_N and $step_N.collections) |
| `query-step,step-{N},catalog_links` | Skill (after catalog_links) | Skill ($step_N and $step_N.collections) |
| `query-step,step-{N},{operation}` | analytical-operator + Skill | Skill (resolving $step_N) |
| `query-step,step-{N},error` | Skill (on operator failure) | Skill (partial failure tracking) |

Path 1 and Path 2 (single-tool) do **not** use T1 scratch — the query MCP tool returns results directly.


## Agent Invocation

Path 3 dispatches two agents via the Agent tool:

1. **query-planner** — receives the question + few-shot plans, returns a JSON execution plan
2. **analytical-operator** — receives operation + inputs, executes extract/summarize/rank/compare/generate

See Path 3 above for the full relay templates.


## Success Criteria

- [ ] Path 1 questions answered with a single `query()` MCP call
- [ ] Path 2 questions matched against builtin templates
- [ ] Path 3 plans executed with all steps, results in T1 scratch
- [ ] Auto-cache: Path 3 plans saved with `ttl=30` after execution
- [ ] No "Save plan?" prompt — auto-cache is silent

## Context Protocol

This skill follows the [Shared Context Protocol](../../agents/_shared/CONTEXT_PROTOCOL.md).

T2 memory context is auto-injected by SessionStart and SubagentStart hooks. Use the `plan_search` MCP tool to find similar prior plans.

## Agent-Specific PRODUCE

- **Step outputs**: T1 scratch with `query-step,step-N,{operation}` tags (ephemeral — wiped at session end)
- **Plan library**: T2 via `plan_save(ttl=30)` — auto-cached on Path 3 success, no user prompt
- **Final answer**: Presented inline in the conversation; not stored unless the user explicitly requests it
