---
title: "T2 Session Rollup Summaries (MemTree-Lite): Recency-Windowed Memory Consolidation for Compact Context Injection"
id: RDR-131
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date:
related_issues: []
related_rdrs: [RDR-057, RDR-063, RDR-089]
related_tests: []
implementation_notes: ""
---

# RDR-131: T2 Session Rollup Summaries (MemTree-Lite)

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> **STUB** captured 2026-05-27 from the MemForest research synthesis
> (T3: `research-memforest-nexus-leverage-2026-05-27`, idea #5; MemForest
> paper at catalog tumbler `1.14.4`). Problem Statement and Approach are
> sketched; deeper gate sections await `/conexus:rdr-research`.

## Problem Statement

T2 project memory accumulates as a flat, unbounded list of `(project, title)`
entries. As a project ages across many sessions (the RDR-112 arc spans 20+),
`memory_get` and the SubagentStart context-injection hook must scan an
ever-growing entry set, and the token budget for injected context degrades.
nexus has no mechanism to consolidate aged entries into compact, navigable
summaries while preserving drill-down to the raw entries.

### Enumerated gaps to close

#### Gap 1: Flat T2 memory has no recency-windowed consolidation

Today every promoted entry sits at the same granularity forever. There is no
"coarse summary over the last N days" layer. Retrieval and context injection
pay the full flat-scan cost. MemForest's MemTree internal nodes store interval
summaries with O(log n) height-bounded updates; the lite analog here is a
single rollup layer over recency windows.

#### Gap 2: SubagentStart context injection cannot bound its own size

The injected project context grows with project age because there is no
compact representation to inject instead of (or ahead of) raw entries. A
rollup-first read with explicit drill-down would let injection stay compact.

## Context

### Background

MemForest (Chen & He, NUS; tumbler `1.14.4`) frames long-context agent memory
as a write-efficient temporal data-management problem. Its MemTree materializes
each temporal scope as a tree: leaves preserve time-stamped evidence, internal
nodes are coarse interval summaries, and updates touch only a height-bounded
path (siblings re-summarize in parallel once children settle). The research
synthesis flagged a "lite" port: nexus does not need the full hierarchy or the
per-leaf LLM re-summarization (see the TRAP analysis in the synthesis), but a
single rollup-summary layer over recency windows captures most of the benefit
for nexus's project-context domain.

### Technical Environment

- `src/nexus/hooks.py` `session_end_flush()` (promotes T1 to T2, expires stale).
- `src/nexus/db/t2/memory_store.py` (the `memory` domain store; heat-weighted TTL).
- Aspect-extraction async worker (RDR-089) is the precedent for keeping
  LLM-bearing work OFF the hot path.

## Research Findings

### Investigation

[To be completed during `/conexus:rdr-research`: measure flat-scan retrieval
degradation as a function of entry count; characterize the SubagentStart
injection token budget; confirm `session_end_flush` is the right hook point.]

### Key Discoveries

- **Documented**: MemForest interval-summary nodes + height-bounded updates
  (paper §3, tumbler `1.14.4`).
- **Assumed**: a single rollup layer (not a full tree) suffices for nexus's
  project-context use case. Needs validation.

### Critical Assumptions

- [ ] Recency-windowed rollups improve injection quality without losing
  needed detail (drill-down preserves raw entries) — **Status**: Unverified
  — **Method**: Spike
- [ ] Post-session async summarization latency is acceptable and never blocks
  `session_end_flush` — **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Add a T2 table `memory_summaries(project, span_start, span_end, content,
entry_ids)`. A post-session async job (dispatched from, but not blocking,
`session_end_flush`) groups recent session entries by recency window and
produces a rollup summary via `claude -p`. `memory_get` returns summaries
first, with a `drill_down=True` flag to fetch the underlying raw entries.

Start with a "last-7-days rollup only" before any multi-level hierarchy.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| async rollup job | RDR-089 aspect worker pattern | Reuse pattern (off hot path) |
| `memory_summaries` table | `db/t2/memory_store.py` | Extend T2 schema (WAL-safe migration) |

[Decision rationale, alternatives, trade-offs, test plan, finalization gate:
to be completed during research.]

## References

- T3 synthesis: `research-memforest-nexus-leverage-2026-05-27` (idea #5)
- MemForest paper, catalog tumbler `1.14.4`
- RDR-057 (Progressive Formalization Across Memory Tiers), RDR-089 (async
  aspect extraction), RDR-063 (T2 domain split)
