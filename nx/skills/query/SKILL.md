---
name: query
description: Use when questions require multi-step retrieval and analysis over nx knowledge collections. Routes through plan-match-first enforcement automatically.
effort: medium
---

# Query

Calls the `nx_answer` MCP tool, which enforces plan-match-first internally:

1. Matches the question against the plan library (threshold 0.40).
2. On hit: executes the plan via `plan_run` (search + traverse + operator steps).
3. On miss: plans inline via `claude -p`, then executes.

No agent spawns. No T1 scratch relay. All coordination is in-process.

## Usage

```
mcp__plugin_nx_nexus__nx_answer(question="<your question>", scope="<corpus or subtree filter>")
```
