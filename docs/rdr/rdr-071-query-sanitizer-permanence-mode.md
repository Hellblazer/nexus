---
title: "RDR-071: Query Sanitizer + Permanence Mode"
status: draft
type: feature
priority: P1
created: 2026-04-13
---

# RDR-071: Query Sanitizer + Permanence Mode

Two quick wins from the [MemPalace gap analysis](../analysis-mempalace-gap-2026-04-12.md). Both are low-complexity, high-value additions that can share a single implementation cycle.

## Problem

### Query contamination

AI agents sometimes prepend system prompts to search queries. The embedding model represents the concatenated string as a single vector where the system prompt (2000+ chars) overwhelms the actual question (10-50 chars). MemPalace measured the impact: **89% recall drops to 1%** without mitigation. This is a silent catastrophic failure, nexus has no defense against it.

### No permanence guarantee

Knowledge entries stored via `store_put` are subject to TTL expiry and consolidation merge. Some entries should be permanent by policy: architectural decisions, reference standards, compliance records. Currently the only workaround is `ttl_days=0`, which prevents expiry but not consolidation.

## Research

### RF-071-1: MemPalace query sanitizer effectiveness

Source: `mempalace/query_sanitizer.py` + `tests/test_query_sanitizer.py`

4-step cascade, ~130 lines of pure Python:

1. **Passthrough** (query <= 200 chars): no degradation, ~89% recall
2. **Question extraction** (find sentences ending with ?): near-full recovery, ~85-89%
3. **Tail sentence extraction** (last meaningful sentence): moderate recovery, ~80-89%
4. **Tail truncation** (last 500 chars, fallback): minimum viable, ~70-80%

Without sanitizer: **1% recall** (catastrophic silent failure).

The sanitizer has no dependencies, no LLM calls, and runs in <1ms. Well-tested with realistic contamination patterns.

### RF-071-2: Contamination vectors in nexus

Nexus search queries arrive via three paths:

1. **MCP `search()` tool**: agents pass the query string. System prompt contamination is possible when agents construct the query by concatenating context + question.
2. **MCP `query()` tool**: same risk, plus catalog routing parameters may carry extra context.
3. **CLI `nx search`**: human-typed, contamination unlikely.

The MCP paths are the vulnerable surface. The sanitizer should run before the query enters `search_cross_corpus`.

### RF-071-3: Permanence semantics in existing systems

Current nexus behavior:
- `ttl_days=0` + `expires_at=""`: entry never expires (TTL guard in `ttl.py` skips it)
- `memory_consolidate merge`: can merge any two entries regardless of TTL
- No metadata flag to exempt from consolidation

MemPalace approach: all entries are permanent by default. No TTL, no consolidation.

Proposed nexus approach: opt-in permanence via `permanent=True` metadata flag. Entries with this flag are:
- Exempt from TTL expiry (already true for `ttl_days=0`)
- Exempt from `memory_consolidate merge` (new guard)
- Visible in consolidation `find-overlaps` but not mergeable without explicit override

## Design

### Query sanitizer

Add `sanitize_query(raw: str) -> str` to `src/nexus/filters.py` (shared by MCP + CLI). Call it at the top of `search_cross_corpus` before the query is used for embedding.

The function ports MemPalace's 4-step cascade with one adaptation: nexus queries often include `where=` filters that look like noise but are intentional. The sanitizer should operate on the `query` parameter only, not on metadata filters.

Configuration: `search.query_sanitizer: true` (default: true). Disable if queries are known-clean.

### Permanence mode

Add `permanent` parameter to `store_put` MCP tool. When true:
- Sets `ttl_days=0` and `expires_at=""`
- Adds `permanent=true` to chunk metadata
- `memory_consolidate merge` checks for the flag and refuses to merge permanent entries

No schema changes needed. The flag lives in ChromaDB metadata alongside existing fields.

## Success Criteria

- SC-1: Query sanitizer recovers >= 70% recall on contaminated queries (baseline: 1%)
- SC-2: Sanitizer adds < 1ms latency to search path
- SC-3: `store_put(permanent=True)` entries survive TTL expiry and consolidation
- SC-4: `memory_consolidate find-overlaps` still shows permanent entries (visibility, not immunity)

### RF-071-4: Voyage AI resilience (measured 2026-04-13)

Tested on `knowledge__delos` with "byzantine fault tolerant consensus":
- Clean (34 chars): avg distance 0.478
- Moderate contamination (302 chars): avg distance 0.413, **80% top-5 overlap**
- Extreme contamination (2646 chars): avg distance 0.443, **80% top-5 overlap**

Voyage AI (1024d) maintains 80% result overlap even with 75x query inflation. Cloud mode is resilient. The sanitizer is defense-in-depth for cloud, critical for local.

### RF-071-5: MiniLM contamination confirmed (measured 2026-04-13)

Local MiniLM (384d) on 5-doc test corpus:
- Clean: top result d=0.151, all 3 results BFT-relevant
- Contaminated (1274 chars): top result d=0.775 (5x worse), **database indexing intrudes as #2**

With nexus distance thresholds (knowledge=0.65), contaminated results would be filtered as noise, returning 0 results. **Local mode users are fully exposed to silent search failure.**

## Open Questions

1. Should the sanitizer log when it activates? (Proposed: yes, structured log at debug level)
2. Should `permanent` entries be visually distinguished in `nx store list`? (Proposed: yes, a `[P]` marker)
3. Should existing entries be upgradeable to permanent? (Proposed: yes, via `nx store update --permanent`)
