---
name: query
description: Use when questions require multi-step retrieval and analysis (extract, summarize, rank, compare, generate) over nx knowledge collections with plan decomposition and reuse.
effort: medium
---

# Query Skill

Drives multi-step analytical queries by orchestrating the **query-planner** and **analytical-operator** agents. The skill is the loop driver — agents cannot spawn agents, so all dispatch and step sequencing happens here.

## When This Skill Activates

- Complex analytical questions that require both retrieval and reasoning
- Questions like "summarize", "compare", "extract structured data from", "rank by relevance", "explain using evidence from"
- Any question where a single `nx search` call is insufficient and the result needs further analysis
- Repeated analytical workflows that benefit from plan reuse

**Do not use** for simple lookups — if `nx search` alone answers the question, use it directly.


## Key Constraint

**The skill is the loop driver.** Subagents (`query-planner`, `analytical-operator`) cannot spawn other subagents. The skill dispatches them sequentially, resolves step references, and manages T1 scratch between dispatches. Do not ask the planner or operator to dispatch further agents.


## Execution Flow

### Step 0: Receive User Question

Capture the user's natural-language analytical question. This is the `query` that drives the entire flow.

### Step 1: Search T2 Plan Library for Similar Queries

Before planning, check whether a similar query has been executed before. Use the memory_search MCP tool:

```
Use memory_search tool: query="{user question}", project="{project}"
```

Alternatively, use `nx memory search "{user question}" --project {project}` via Bash if MCP is unavailable (degraded mode per CONTEXT_PROTOCOL.md).

If matches are found, collect up to 3 plans with `outcome="success"` as few-shot examples. Include them in the planner relay.

If no matches are found, proceed with an empty `few_shot_plans` list.

### Step 2: Dispatch query-planner

## Agent Invocation

Use the Agent tool to invoke **query-planner** with:

```markdown
## Relay: query-planner

**Task**: Decompose the following analytical question into a step-by-step execution plan.
**Bead**: none

### Input Artifacts
- nx scratch: none
- nx memory: none
- Files: none

### Deliverable
A JSON execution plan with ordered steps (search, extract, summarize, rank, compare, or generate).

### Quality Criteria
- [ ] Plan is valid JSON with "query" and "steps" fields
- [ ] First step is a search operation
- [ ] All step references use $step_N notation
- [ ] Plan has 2-4 steps (avoid over-engineering)

### Context Notes
**Question**: {user question verbatim}

**Few-shot plans** (adapt these patterns if they match):
{JSON array of few_shot_plans, or "none"}
```

Wait for the planner to return. Parse the JSON plan from the response. The planner returns a single fenced JSON block — extract the content between ` ```json ` and ` ``` `.

If the plan cannot be parsed, log the error and ask the user to rephrase the question.

### Step 3: Execute Plan Steps

For each step in `plan["steps"]` in order:

#### If `step["operation"] == "search"`:

Execute via the search MCP tool directly:

```
Use search tool: query="{step.search_query}", corpus="{step.corpus}", n=10
```

If corpus contains multiple values (e.g., `"knowledge,code"`), run one search per corpus and concatenate the results.

Write results to T1 scratch:
```
Use scratch tool: action="put", content="{search results as text}", tags="query-step,step-{N},search"
```

#### For all other operations (extract, summarize, rank, compare, generate):

**Resolve inputs**: Before dispatching the operator, resolve any `$step_N` references by reading from T1 scratch:

```
Use scratch tool: action="search", query="query-step step-{N}"
```

Retrieve the content from the matching scratch entry. If multiple entries match, use the most recent. Substitute the resolved content for the `$step_N` reference in the relay.

For steps with multiple inputs (`["$step_N", "$step_M"]`), resolve each reference separately and pass as an array.

**Dispatch analytical-operator**:

```markdown
## Relay: analytical-operator

**Task**: Execute {operation} operation on the provided inputs.
**Bead**: none

### Input Artifacts
- nx scratch: step-{N} results (resolved and included below)
- Files: none

### Deliverable
Operation result written to T1 scratch with tag "query-step,step-{N+1},{operation}"

### Quality Criteria
- [ ] Operation completed without error
- [ ] Result written to T1 scratch

### Context Notes
**Step number**: {N}

**Operation payload**:
```json
{
  "operation": "{step.operation}",
  "inputs": [{resolved input content}],
  "params": {step.params}
}
```
```

Wait for the operator to complete. Write the operator's output to T1 scratch with tag `query-step,step-{N},{step.operation}`:

```
Use scratch tool: action="put", content="{operator output}", tags="query-step,step-{N},{operation}"
```

**Note**: The analytical-operator also writes to scratch itself. This redundant write by the skill ensures the tag `query-step,step-{N}` is always present for subsequent step resolution, regardless of the operator's exact tag format.

#### Error Handling

If an operator step fails (operator returns `{"error": ...}` or an exception):
1. Log the error message.
2. Write a failure marker to scratch: `Use scratch tool: action="put", content="FAILED: {reason}", tags="query-step,step-{N},error"`
3. Continue executing remaining steps. Steps that reference a failed step's output will receive the failure marker text as input — the operator will surface the absence of real data in its output.
4. Track partial failure: set `outcome = "partial"` for any plan library save at the end.

### Step 4: Collect and Present Final Output

After all steps complete, read the last step's output from T1 scratch:

```
Use scratch tool: action="search", query="query-step step-{last_N}"
```

Present the result to the user with a brief header:

```
**Query**: {original question}

**Result**:
{final step output}
```

### Step 5: Prompt to Save Plan

After presenting results, ask:

> "Save this plan to the library for future reuse? (y/n)"

If the user confirms:
- Serialize the plan JSON as a string.
- Save via memory_put MCP tool (T2 plan library):

```
Use memory_put tool: content="{plan_json}", project="{project}", title="plan-{slug}.md", ttl=90
```

Where `{slug}` is a short snake_case version of the first 5 words of the question.

Additionally, if the T2 database has `save_plan` available via the CLI:

```bash
nx memory put "{plan_json}" --project {project} --title "plan-{slug}.md" --ttl 90
```

Set `outcome="partial"` if any step failed; `outcome="success"` otherwise.

If the user declines or does not respond, skip saving.


## T1 Scratch Usage

T1 scratch is the cross-dispatch persistence mechanism. Every step output is written here so subsequent steps can reference it.

| Tag pattern | Written by | Read by |
|-------------|-----------|---------|
| `query-step,step-{N},search` | Skill (after search MCP call) | Skill (resolving $step_N for next dispatch) |
| `query-step,step-{N},{operation}` | analytical-operator + Skill | Skill (resolving $step_N for next dispatch) |
| `query-step,step-{N},error` | Skill (on operator failure) | Skill (to detect partial failures) |

**This is critical** (per RDR-041 and RDR-042 gate finding C2): without T1 scratch as the inter-dispatch bus, step outputs are lost between agent invocations. Never skip the scratch write after each step.


## Storage Guidance

| Tier | What is stored | When |
|------|---------------|------|
| T1 scratch | Step outputs, tagged `query-step,step-N,{operation}` | After every step execution |
| T2 memory | Serialized plan JSON for reuse | Only when user confirms save |
| T3 store | Not directly used — search MCP tool handles T3 queries | N/A |

Search step results come from T3 via the search MCP tool. The skill does not write to T3.


## Agent-Specific PRODUCE

- **Step outputs**: T1 scratch with `query-step,step-N,{operation}` tags (ephemeral — wiped at session end)
- **Plan library**: T2 memory via memory_put or `nx memory put` — only on user confirmation
- **Final answer**: Presented inline in the conversation; not stored unless the user explicitly requests it

Store using these naming conventions:
- **T2 memory title**: `plan-{slug}.md` (e.g., `plan-caching_strategy_compare.md`)
- **T1 scratch tags**: `query-step,step-N,{operation}` (e.g., `query-step,step-2,summarize`)


## Success Criteria

- [ ] Query-planner agent returns valid JSON plan with at least one step
- [ ] All search steps executed via search MCP tool with results written to T1 scratch
- [ ] All operator steps dispatched to analytical-operator with $step_N references resolved from T1 scratch
- [ ] Final output presented to user with source citations
- [ ] Plan library prompted for save after successful execution
- [ ] Partial failures handled gracefully (outcome="partial" on save)

## Context Protocol

This skill follows the [Shared Context Protocol](../../agents/_shared/CONTEXT_PROTOCOL.md).

T2 memory context is auto-injected by SessionStart and SubagentStart hooks. Use `memory_search` to find similar prior plans before dispatching the planner.
