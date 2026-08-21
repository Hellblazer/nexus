---
title: "Scope-Routed T1 to T2 Promotion: Entity / Session / Project Scopes for Targeted Memory Retrieval"
id: RDR-132
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date:
related_issues: []
related_rdrs: [RDR-041, RDR-057, RDR-105]
related_tests: []
implementation_notes: ""
---

# RDR-132: Scope-Routed T1 to T2 Promotion

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> **STUB** captured 2026-05-27 from the MemForest research synthesis
> (T3: `research-memforest-nexus-leverage-2026-05-27`, idea #6; MemForest
> paper at catalog tumbler `1.14.4`). Problem Statement and Approach are
> sketched; deeper gate sections await `/conexus:rdr-research`.

## Problem Statement

All T1 entries promote to a single flat T2 namespace keyed by
`(project, title)`. There is no scope differentiation, so retrieval and
SubagentStart context injection cannot target "everything about RDR-112"
versus "everything in this project." MemForest routes canonicalized facts to
distinct scopes (entity / session / scene) and retrieves against the relevant
scope rather than the whole accumulated memory.

### Enumerated gaps to close

#### Gap 1: T2 promotion is scope-blind

`session_end_flush` promotes every flagged T1 entry into one flat project
namespace. A reader cannot ask for a scoped slice; it gets the full project
history or nothing.

#### Gap 2: The relay's Bead field is a latent entity scope that goes unused

The agent-relay format already carries a `Bead` field. That is a natural
entity-scope key (e.g. `RDR-112`, a bead ID), but nothing in the promotion or
retrieval path uses it to partition memory.

## Context

### Background

MemForest (tumbler `1.14.4`) normalizes extractions into canonical facts and
routes them to scope-specific MemTrees, so retrieval targets a scope instead of
scanning all memory. nexus has the raw ingredients (the relay Bead field, T1
flag/promote path) but no scope dimension on T2 entries.

### Technical Environment

- `src/nexus/db/t1.py` `flag()`; `src/nexus/hooks.py` `session_end_flush`.
- `src/nexus/db/t2/memory_store.py` (`memory` table; `memory_put` / `memory_search` / `memory_get`).

## Research Findings

### Investigation

[To be completed during `/conexus:rdr-research`: evaluate the bead-ID regex
bootstrapping heuristic vs LLM entity extraction; quantify orphaned-tag
accumulation as beads close.]

### Key Discoveries

- **Documented**: MemForest per-scope routing (paper §3, tumbler `1.14.4`).
- **Assumed**: bead-ID regex on entry content is a good-enough entity
  bootstrap before LLM extraction. Needs validation (brittleness risk).

### Critical Assumptions

- [ ] A `scope` + `entity_name` dimension improves targeted retrieval without
  fragmenting memory into unusable shards — **Status**: Unverified
  — **Method**: Spike
- [ ] Bead-ID regex entity-tagging has acceptable precision/recall as a v1
  heuristic — **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Add `scope` and `entity_name` columns to the T2 `memory` table (WAL-safe
migration). `memory_put` accepts an optional `scope` kwarg; `memory_search` /
`memory_get` accept a scope filter. `session_end_flush` auto-tags entity scope
for entries whose content matches a known bead-ID pattern (regex bootstrap
before LLM entity extraction). SubagentStart can then filter by
`entity=RDR-112` rather than returning the full flat project history.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| scope/entity columns | `db/t2/memory_store.py` | Extend schema (WAL-safe) |
| entity auto-tag at promote | `hooks.py session_end_flush` | Extend |

[Decision rationale, alternatives, trade-offs, test plan, finalization gate:
to be completed during research. Note overlap with RDR-133 entity clusters:
scope routing is the write-side partition; RDR-133 is the cross-tier read-side
aggregation. Decide whether they merge.]

## References

- T3 synthesis: `research-memforest-nexus-leverage-2026-05-27` (idea #6)
- MemForest paper, catalog tumbler `1.14.4`
- RDR-041 (T1 scratch inter-agent context), RDR-057 (progressive
  formalization), RDR-105 (T1 chroma architecture)
