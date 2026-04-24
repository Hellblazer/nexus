# MCP tools vs agents

When should a capability ship as an **MCP tool** versus a **Claude Code agent**?
This page encodes the boundary rule from RDR-080 and the practical patterns
that followed.

## The rule

> If an operation can be expressed as a deterministic function of its inputs
> and completes in under one API call, it is an MCP tool.  If it requires
> multi-turn reasoning, tool selection, or context accumulation across turns,
> it is an agent.

Single-shot and single-purpose → MCP tool.  Multi-turn with judgement calls →
agent.

## Why it matters

Agents are expensive to invoke — a sub-agent spawn loads a full system prompt,
pulls plugin context, and runs multiple LLM turns before returning.  If the
underlying operation is really a structured call with a fixed schema (extract
these fields, rank these items, summarise this text), wrapping it in an agent
burns tokens and latency for no benefit.

Conversely, some operations really do need multi-turn reasoning: deciding
which file to look at next based on what the last file said, revising a
hypothesis after gathering evidence, or planning a multi-step retrieval from
an ambiguous intent.  Those don't compress into a single `claude -p`
invocation without losing their essence.

**Operator bundling is the middle ground** (v4.10.0). A fixed-shape
operator *pipeline* — extract → rank → summarise, where each step has
a known contract and the downstream step feeds on the upstream step's
output — *does* compress into one `claude -p` call. The LLM carries
the intermediate state in its reasoning window; the host side only
sees the terminal output. This isn't multi-turn reasoning getting
crushed into one turn; it's a deterministic DAG running in one
subprocess instead of N. Measured win on real corpora: 55-72% latency
reduction on plans with multiple consecutive operators. See
[plan-centric-retrieval.md §Operator bundling](plan-centric-retrieval.md#operator-bundling-v4100).

## Classification table (RDR-080)

| Capability | Before RDR-080 | After RDR-080 |
|------------|---------------|---------------|
| Knowledge consolidation | `knowledge-tidier` agent | `mcp__plugin_nx_nexus__nx_tidy` |
| Plan audit | `plan-auditor` agent | `mcp__plugin_nx_nexus__nx_plan_audit` |
| Bead enrichment | `plan-enricher` agent | `mcp__plugin_nx_nexus__nx_enrich_beads` |
| Multi-step retrieval | `query-planner` + `analytical-operator` agents | `mcp__plugin_nx_nexus__nx_answer` |
| PDF indexing | `pdf-chromadb-processor` agent | `nx index pdf` CLI / direct ingest |
| Code review | `code-review-expert` agent | (kept) — multi-turn inspection with judgement |
| Debugging | `debugger` agent | (kept) — hypothesis → evidence → revise loop |
| Research synthesis | `deep-research-synthesizer` agent | (kept) — cross-source comparison + synthesis |
| Strategic planning | `strategic-planner` agent | (kept) — multi-phase decomposition, tradeoffs |
| Architecture design | `architect-planner` agent | (kept) — design alternatives, phased plans |
| Code analysis | `codebase-deep-analyzer` agent | (kept) — exploration + dependency mapping |
| Substantive critique | `substantive-critic` agent | (kept) — multi-axis review |

Anything in the "kept" column fundamentally needs multi-turn reasoning —
hypothesis testing, alternative comparison, or exploration-with-backtracking.
Everything that was moved to MCP is a structured-output call disguised as
an agent.

## The stub-agent pattern

For the three agents that were demoted to MCP tools, the plugin keeps a
40-line stub agent file (`nx/agents/{knowledge-tidier,plan-auditor,plan-enricher}.md`)
so legacy dispatch references don't break:

```markdown
---
name: knowledge-tidier
description: "STUB — superseded by mcp__plugin_nx_nexus__nx_tidy MCP tool
             (RDR-080 P3).  Call mcp__plugin_nx_nexus__nx_tidy instead of
             dispatching this agent."
---

# knowledge-tidier (STUB)

This agent is a redirector.  The real work lives in the `nx_tidy` MCP tool.

## Usage

Call the MCP tool directly:

    mcp__plugin_nx_nexus__nx_tidy(topic="...", collection="knowledge")
```

When a caller dispatches the stub agent via the `Agent` tool, Claude reads
the stub body as the system prompt, recognises the redirect, and invokes
the MCP tool on the caller's behalf.  The runtime validation harness
(`scripts/validate/07-agent-behavior.py` and `09-plugin-runtime.py`) verifies
this routing actually happens.

**Do not add new stub agents**.  Once the six months of compatibility
headroom elapses, these three will be deleted — new callers should go to
the MCP tool directly.

## When you're authoring a new capability

1. Can it be a schema-conforming single call?  → MCP tool.  Done.
2. Does it need cross-turn state (revising based on what you find)?  → Agent.
3. Does it spawn one LLM call to decide + one LLM call to act?  → Agent with
   a narrower system prompt; or two MCP tools chained by a skill.
4. Is the "multi-turn" really just "large output split across turns"?  →
   MCP tool with a larger timeout.

Most new capabilities fit in bucket 1 or 4.  Be suspicious of the second and
third buckets — RDR-080 argues that most "agent" capabilities were actually
structured calls in disguise.

## How the MCP tools run under the hood

The RDR-080 tools (`nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`) and the
eight operator tools (`operator_extract`, `operator_rank`, `operator_compare`,
`operator_summarize`, `operator_generate`, `operator_filter`,
`operator_check`, `operator_verify`) use a single primitive:
`nexus.operators.dispatch.claude_dispatch`.

`claude_dispatch` spawns `claude -p --output-format json --json-schema <schema>`,
feeds the prompt via stdin, times out at a configurable limit (default 120s),
and unwraps `structured_output` from claude's JSON result wrapper.  The
subprocess authenticates via the caller's `~/.claude` and `~/.claude.json` —
nothing in the MCP server needs API keys.

Tools that need to reach Nexus storage during their reasoning (e.g.
`nx_enrich_beads` searches the codebase) get the same set of nexus MCP
tools via the subprocess inheriting `~/.claude`.

## See also

- [RDR-080](rdr/rdr-080-retrieval-layer-consolidation.md) — the architectural decision
- [MCP Servers](mcp-servers.md) — full tool catalog
- [Querying Guide](querying-guide.md) — the `nx_answer` retrieval trunk
- [Plan Authoring Guide](plan-authoring-guide.md) — for capabilities that
  compose multiple MCP tools into a reusable plan
