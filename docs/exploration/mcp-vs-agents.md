# MCP tools vs agents

> **Note**: despite living under `exploration/`, this is a LIVE normative rule:
> the "See also" sections of architecture.md and querying-guide.md delegate
> to it. Not a candidate for archive.

When should a capability ship as an **MCP tool** versus a **Claude Code agent**?
This page encodes the boundary rule from RDR-080.

## The rule

> If an operation can be expressed as a deterministic function of its inputs
> and completes in under one API call, it is an MCP tool.  If it requires
> multi-turn reasoning, tool selection, or context accumulation across turns,
> it is an agent.

## Why it matters

Agents are expensive to invoke. A sub-agent spawn loads a full system prompt,
pulls plugin context, and runs multiple LLM turns before returning.  If the
underlying operation is a structured call with a fixed schema (extract
these fields, rank these items, summarise this text), wrapping it in an agent
burns tokens and latency.

Conversely, some operations do need multi-turn reasoning: deciding
which file to look at next based on what the last file said, revising a
hypothesis after gathering evidence, or planning a multi-step retrieval from
an ambiguous intent.  Those don't compress into a single `claude -p`
invocation without losing their essence.

**Operator bundling is the middle ground** (v4.10.0). A fixed-shape
operator *pipeline* (extract → rank → summarise, where each step has
a known contract and the downstream step feeds on the upstream step's
output) *does* compress into one `claude -p` call. The LLM carries
the intermediate state in its reasoning window; the host side only
sees the terminal output. This isn't multi-turn reasoning getting
crushed into one turn. It's a deterministic DAG running in one
subprocess instead of N. Measured win on real corpora: 55-72% latency
reduction on plans with multiple consecutive operators. See
[plan-centric-retrieval.md §Operator bundling](../plan-centric-retrieval.md#operator-bundling-v4100).

## Classification table (RDR-080)

| Capability | Before RDR-080 | After RDR-080 |
|------------|---------------|---------------|
| Knowledge consolidation | `knowledge-tidier` agent | `mcp__plugin_conexus_nexus__nx_tidy` |
| Plan audit | `plan-auditor` agent | `mcp__plugin_conexus_nexus__nx_plan_audit` |
| Bead enrichment | `plan-enricher` agent | `mcp__plugin_conexus_nexus__nx_enrich_beads` |
| Multi-step retrieval | `query-planner` + `analytical-operator` agents | `mcp__plugin_conexus_nexus__nx_answer` |
| PDF indexing | `pdf-chromadb-processor` agent | `nx index pdf` CLI / direct ingest |
| Code review | `code-review-expert` agent | (kept): multi-turn inspection with judgement |
| Debugging | `debugger` agent | (kept): hypothesis → evidence → revise loop |
| Research synthesis | `deep-research-synthesizer` agent | (kept): cross-source comparison + synthesis |
| Strategic planning | `strategic-planner` agent | (kept): multi-phase decomposition, tradeoffs |
| Architecture design | `architect-planner` agent | (kept): design alternatives, phased plans |
| Code analysis | `codebase-deep-analyzer` agent | (kept): exploration + dependency mapping |
| Substantive critique | `substantive-critic` agent | (kept): multi-axis review |

Anything in the "kept" column needs multi-turn reasoning:
hypothesis testing, alternative comparison, or exploration-with-backtracking.
Everything that was moved to MCP is a structured-output call disguised as
an agent.

## The stub-agent pattern (retired nexus-cnzei.4)

For the three agents demoted to MCP tools, the plugin used to keep a
40-line stub agent file (`conexus/agents/{knowledge-tidier,plan-auditor,plan-enricher}.md`)
so legacy dispatch references wouldn't break:

```markdown
---
name: knowledge-tidier
description: "STUB — superseded by mcp__plugin_conexus_nexus__nx_tidy MCP tool
             (RDR-080 P3).  Call mcp__plugin_conexus_nexus__nx_tidy instead of
             dispatching this agent."
---

# knowledge-tidier (STUB)

This agent is a redirector.  The real work lives in the `nx_tidy` MCP tool.

## Usage

Call the MCP tool directly:

    mcp__plugin_conexus_nexus__nx_tidy(topic="...", collection="knowledge")
```

When a caller dispatched the stub agent via the `Agent` tool, Claude read
the stub body as the system prompt, recognised the redirect, and invoked
the MCP tool on the caller's behalf.

**All three stub files were deleted at nexus-cnzei.4**: they were 40 lines of indirection for a
one-line call, and the corresponding pointer skill (`knowledge-tidying`,
`plan-validation`, `enrich-plan`) already documents the same MCP-tool call. New callers go to the MCP tool, or the
pointer skill, directly.

## When you're authoring a new capability

1. Can it be a schema-conforming single call?  → MCP tool.  Done.
2. Does it need cross-turn state (revising based on what you find)?  → Agent.
3. Does it spawn one LLM call to decide + one LLM call to act?  → Agent with
   a narrower system prompt, or two MCP tools chained by a skill.
4. Is the "multi-turn" really just "large output split across turns"?  →
   MCP tool with a larger timeout.

Most new capabilities fit in bucket 1 or 4.  Be suspicious of the second and
third buckets: RDR-080 argues that most "agent" capabilities were actually
structured calls in disguise.

## How the MCP tools run under the hood

The RDR-080 tools (`nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`) and the
ten operator functions, plan steps rather than MCP tools since nexus-ivi4s (`operator_extract`, `operator_rank`, `operator_compare`,
`operator_summarize`, `operator_generate`, `operator_filter`,
`operator_groupby`, `operator_aggregate`, `operator_check`,
`operator_verify`) use a single primitive:
`nexus.operators.dispatch.claude_dispatch`. Three of the ten
(`operator_filter`, `operator_groupby`, `operator_aggregate`) also have a
SQL fast path ahead of the LLM dispatch (see `src/nexus/operators/aspect_sql.py`;
the prompt and schema builders are in `src/nexus/mcp/operator_requests.py`).

`claude_dispatch` is a thin front over a pluggable `LLMBackend`
(`src/nexus/operators/llm_backend.py`). The setting is `NX_LLM_BACKEND`, then
`llm.backend` in the global `~/.config/nexus/config.yml`, then `auto`. Three
backends exist:

- `cli` (the default): the `claude -p` subprocess, authenticated by Claude Code's
  own login, so nothing in the MCP server needs an API key.
- `anthropic`: the Messages API, which needs an `anthropic_api_key`.
- `openai_compat`: Chat Completions for Ollama, vLLM or OpenRouter, which needs an
  endpoint and, for hosted services, a key.

`auto` picks `cli` when `claude` is on PATH, otherwise `anthropic` when a key
resolves. The API backends serve tool-free calls only: a call that grants tools
always goes to `cli`. The per-repo `.nexus.yml` is not read for this setting.

On the `cli` backend, `claude_dispatch` spawns
`claude -p --output-format stream-json --json-schema <schema>` (plus `--verbose`,
`--no-session-persistence` and `--strict-mcp-config`), feeds the prompt via stdin,
times out at a configurable limit (default 300s), and reads `structured_output`
from the final result event of the stream.

By default the child loads no MCP servers (`--strict-mcp-config` with no
`--mcp-config`) and no settings-supplied hooks (`--setting-sources ""`), and it is
started with `--tools ""`, so an operator call is tool-free. A tool that must reach
Nexus storage during its reasoning (`nx_enrich_beads`, `nx_plan_audit`) gets an
explicit grant instead: the `nx-mcp` and `nx-mcp-catalog` servers passed inline
through `--mcp-config`, plus read-only `Read`, `Grep` and `Glob`
(`_subprocess_tool_grant` in `src/nexus/mcp/core.py`).

## See also

- [RDR-080](../rdr/rdr-080-retrieval-layer-consolidation.md): the architectural decision
- [MCP Servers](../mcp-servers.md): full tool catalog
- [Querying Guide](../querying-guide.md): the `nx_answer` retrieval trunk
- [Plan Authoring Guide](../plan-authoring-guide.md): for capabilities that
  compose multiple MCP tools into a reusable plan
