---
title: "RDR-074: Permanence Mode"
status: deferred
type: feature
priority: P3
created: 2026-04-13
---

# RDR-074: Permanence Mode

Opt-in permanence for knowledge entries. Split from RDR-071 after gate review identified a critical tier-confusion issue: `store_put` operates on T3 (ChromaDB), `memory_consolidate` operates on T2 (SQLite). The two tiers need separate permanence designs.

## Problem

Some knowledge should never expire or be merged: architectural decisions, reference standards, compliance records. Currently:

- T3 (`store_put`): `ttl_days=0` prevents expiry, but no consolidation exists for T3 today.
- T2 (`memory_put`): 30-day default TTL, `memory_consolidate merge` can merge any entries.

The immediate need is T2 permanence (protecting memory entries from TTL and consolidation). T3 permanence is a future concern when T3 consolidation is built.

## Research needed

- What T2 memory entries should be permanent by default? (decisions, reference links, project identity?)
- How does `merge_memories()` work today and where would the guard go?
- Should permanent entries be promotable (T2 permanent that auto-copies to T3)?

## Design (sketch)

### T2 permanence

Add `permanent` column to T2 `memory` table (default false). `memory_put(permanent=True)` sets it. Guard in `merge_memories()` refuses to merge entries with `permanent=true`. `memory_consolidate find-overlaps` still shows permanent entries (visibility, not immunity).

### T3 permanence (deferred)

When T3 consolidation is built, add `permanent=true` to ChromaDB metadata. Until then, T3 entries with `ttl_days=0` are already effectively permanent.

## Success Criteria

- SC-1: `memory_put(permanent=True)` entries survive TTL expiry
- SC-2: `memory_consolidate merge` refuses to merge permanent entries
- SC-3: `memory_consolidate find-overlaps` still shows permanent entries
