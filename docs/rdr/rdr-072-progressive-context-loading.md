---
title: "RDR-072: Progressive Context Loading"
status: draft
type: feature
priority: P2
created: 2026-04-13
---

# RDR-072: Progressive Context Loading

Inspired by MemPalace's 4-layer memory stack (L0-L3). Reduce cold-start latency for agent sessions by assembling a project context packet at session start.

## Problem

Every agent session starts cold. The agent has no project context until it runs a search query. For the first few interactions, the agent is working blind, often asking questions that the taxonomy, recent activity, or project identity could answer instantly.

MemPalace solves this with a ~600 token wake-up: identity (L0, ~100 tokens) + essential story (L1, ~500 tokens). The agent knows who it is and what matters before the first user message.

Nexus has the raw material (CLAUDE.md, taxonomy labels, recent memory, catalog stats) but no mechanism to assemble and inject it.

## Research

### RF-072-1: MemPalace layer architecture

Source: `mempalace/layers.py`

| Layer | Tokens | Content | When loaded |
|-------|--------|---------|-------------|
| L0 | ~100 | Identity: name, traits, key people, current project | Always (session start) |
| L1 | ~500-800 | Essential story: top moments from the palace, importance-ranked | Always (session start) |
| L2 | ~200-500 each | On-demand: wing/room-specific context when topic comes up | On mention |
| L3 | Unlimited | Deep search: full ChromaDB semantic search | On query |

Wake-up cost: ~600-900 tokens. Leaves 95%+ of context free.

### RF-072-2: Available context sources in nexus

| Source | What it provides | Tokens |
|--------|-----------------|--------|
| CLAUDE.md | Project structure, conventions, commands | Already injected by Claude Code |
| `nx taxonomy status` output | Topic labels with doc counts per collection | ~50-200 tokens |
| Recent T2 memory entries | Project decisions, notes, findings | ~100-300 tokens |
| Catalog stats | Document counts, link counts, content types | ~50 tokens |
| Recent git activity | What changed recently | ~50-100 tokens |

Total potential: ~300-650 tokens on top of CLAUDE.md.

### RF-072-3: Injection mechanism

The SessionStart hook (`nx/hooks/scripts/session_start_hook.py`) already runs at every session start and injects content into the system reminder. It currently injects:
- Ready beads
- nx capabilities summary
- T1 scratch initialization

Adding a project context section is a natural extension. The content would be generated once and cached in T2 memory (refreshed when taxonomy or memory changes).

## Design

### Layer 0: Project identity (~100 tokens)

Extracted from CLAUDE.md (already injected) + catalog stats. No new work needed for L0. CLAUDE.md IS the identity layer.

### Layer 1: Topic map (~200 tokens)

Generated from taxonomy: top 10-15 topic labels per collection, grouped by prefix (code/docs/knowledge/rdr). Cached in T2 memory as `project_context_l1`.

Example output:
```
Project knowledge map:
  code: GPU Kernel Programming (1294), Latency Benchmarking (1272), JUnit Testing (1202)
  knowledge: Organization Member Services (71), Byzantine Consensus (68), Bloom Filters (53)
  rdr: Bead Composition Probe (91), Content-addressed Resolution (75), Catalog Link Graph (62)
```

### Layer 2: On-demand context (existing)

Already served by `search()` and `query()` MCP tools. No change needed. The topic parameter enables scoped retrieval: `search(query="...", topic="Byzantine Consensus")`.

### Layer 3: Deep search (existing)

Already served by `/nx:query` skill for multi-step analytical queries. No change needed.

### Refresh strategy

The L1 context is regenerated when:
- `nx taxonomy discover` runs (topics changed)
- `nx index repo` completes (corpus changed)
- Explicitly via `nx context refresh` (new command)

Cached in T2 memory with key `(project=repo_name, title="__context_l1")`.

## Success Criteria

- SC-1: SessionStart hook injects topic map in < 200 tokens
- SC-2: Agent can answer "what topics exist in this project?" without searching
- SC-3: Context refreshes automatically after discover/index
- SC-4: No measurable latency increase on session start (< 100ms for L1 generation from cache)

## Open Questions

1. Should L1 include recent memory entries (decisions, findings) or just taxonomy? (Proposed: taxonomy only for v1, memory in v2)
2. Should the topic map be per-collection or aggregated? (Proposed: aggregated, grouped by prefix)
3. How many topics per collection in L1? (Proposed: top 5 by doc_count, capped at 200 tokens total)
