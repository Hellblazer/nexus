---
title: "Post-Mortem: Link Lifecycle — Full CRUD, Queryable Links, Bulk Operations"
date: 2026-04-05
rdr: RDR-051
status: closed
close_reason: implemented
type: post-mortem
severity: n/a
---

# Post-Mortem: RDR-051 — Link Lifecycle

**Closed**: 2026-04-05  **Reason**: implemented  **PR**: #126
**Branch**: feature/nexus-lrlo-catalog-foundation

## What Was Done

Completed the catalog link system from a basic create/delete API to a full
lifecycle with composable query, idempotent creation, bulk operations, and
audit. 11 components, 13 research findings, implemented in one day.

1. **`link_query()`** — composable filter (from, to, type, created_by, direction, limit, offset). Single parameterized SQL WHERE clause.
2. **`link_if_absent()`** — idempotent link creation for generators. Returns True if created, False if existed.
3. **`delete_document()`** — tombstones document, leaves links intentionally orphaned (Nelson-faithful: addresses are vacant, not erased).
4. **`bulk_unlink()`** — delete all links matching filter criteria. Query-then-act pattern reusing `link_query` filters.
5. **`link_audit()`** — graph health report: orphans, duplicates, stats by type/creator.
6. **`catalog_link_query` MCP tool** — composable filter for agents (admin/audit, not a planner operation per RF-11).
7. **`catalog_links` MCP fix** — returns `{nodes, edges}` dict instead of edges-only (Component 10).
8. **UNIQUE constraint** on `(from_tumbler, to_tumbler, link_type)` — database-enforced dedup.
9. **Composite index** `(created_by, link_type)` for audit queries (RF-12).
10. **JSONL reader resilience** — per-line try/except + skip + log for malformed lines (GST S2).
11. **Generator improvements** — `link_if_absent()` replaces manual dedup sets in `generate_citation_links()` and `generate_code_rdr_links()`.

## Plan vs. Actual Divergences

| Planned | Actual | Impact |
|---------|--------|--------|
| `update_link()` as Component 3 | Removed per RF-6 — link type IS identity | Simpler API. Type change = `unlink()` + `link()` (a new fact, not an edit) |
| `bulk_update_links()` in Component 4 | Removed per RF-6 | Bulk type-reclassification = `bulk_unlink()` + re-run generator |
| `validate_link()` as blocking pre-check | Validation is advisory (`link()` warns, doesn't block) | Links to ghost/future elements are valid per RF-9 |
| 11 sequential implementation steps | Dense but sequential, all in one session | No divergence in ordering |
| `created` → `created_at` full rename (RF-5) | Cleaned up as part of schema step | Pre-existing tech debt from RDR-049 resolved |

## Key Design Decisions

1. **RF-6: No `update_link()`** — Link identity is `(from_t, to_t, link_type)`. Changing the type changes the identity; it IS a new fact. Span and meta changes on the same key go through `link()` upsert with merge. This matches Neo4j's model: relationship identity is `(startNode, type, endNode)`.

2. **RF-8: Concurrent creation merges `created_by`** — When two agents discover the same link, the dedup key stays `(from_t, to_t, link_type)`. `link()` detects the existing entry and merges the new `created_by` into `meta["co_discovered_by"]`. First discoverer keeps primary attribution.

3. **RF-9: Orphaned links are intentional** — Document deletion leaves links intact. "A cited B, but B was removed" is more informative than cascade-deleting the citation. `link_audit()` reports orphans as a diagnostic, not a consistency violation.

4. **RF-11: `link_query` is admin/audit, not a planner operation** — It doesn't produce `physical_collection` values for downstream search. The planner's `catalog_links` (BFS from a tumbler) remains the graph traversal primitive for query plans.

## What Went Well

1. **RF-6 "no update_link"** eliminated an entire class of key-mutation bugs. The API is smaller and correct by construction.
2. **`link_if_absent()`** simplified all generators — no more manual dedup sets tracking what was already linked.
3. **UNIQUE constraint** caught duplicate link creation bugs during testing that application-level dedup had missed.
4. **`link_audit()`** immediately found 0 orphans and 0 duplicates on production data — validation that the generators were working correctly.
5. **GST analysis (RF-13)** identified 8 significant system-level issues that shaped the implementation plan. The JSONL crash-on-malformed-line bug (S2) and the `graph()` starting-node exclusion (S8) would have been missed by unit-level analysis.
6. **Query-then-act pattern** — same filter syntax drives `link_query()`, `bulk_unlink()`, and `link_audit()`. One mental model for all link operations.

## What Went Wrong

1. **Scope was larger than expected** — 11 components and 13 research findings in one day. Correct decision to fast-track (all components were tightly coupled), but the session was dense.
2. **`created` vs `created_at` naming inconsistency** (RF-5) was pre-existing tech debt from RDR-049 that needed cleanup before the schema changes could proceed cleanly.
3. **`graph()` excluding starting node** (GST S8) was a subtle bug causing N+1 `catalog_show` calls from agents. Only visible from the systems-level view, not from any single unit test.

## Discoveries

- **JSONL readers had no error recovery**: A single malformed line in `links.jsonl` crashed `read_links()` entirely. The same fragility existed in `read_documents()` and `read_owners()`. All three now have per-line try/except with skip + log.

- **`catalog_links` MCP was discarding nodes**: `graph()` returns `{nodes, edges}` but the MCP tool was returning only the edges list. Agents had to make separate `catalog_show` calls for every node — an N+1 pattern. Fixed to return the full dict.

- **Nelson's orphan principle is practically valuable**: "Addresses are vacant, not erased" sounded philosophical in Literary Machines, but in practice, keeping links to deleted documents provides useful provenance. An agent can see that a citation target was removed, rather than silently losing the relationship.

## Key Learnings

1. **"Query then act" is the right pattern for bulk operations.** The same filter drives read, delete, and audit. Adding a new bulk operation means reusing the existing filter, not inventing a new query language.

2. **Immutable identity + mutable metadata** is the right model for links. `(from, to, type)` never changes; spans and meta can be updated via upsert. Attempting to update identity fields is a semantic error, not a missing feature.

3. **GST analysis pays for itself on system-boundary work.** The 8 significant findings from RF-13 shaped half the implementation plan. Unit-level analysis of each component would have missed the boundary inconsistencies (naming, signal loss at MCP layer, missing feedback loops).

4. **Database-enforced constraints beat application-level dedup.** The UNIQUE constraint on `(from_tumbler, to_tumbler, link_type)` is authoritative. `link_if_absent()` uses `INSERT OR IGNORE` — the database is the arbiter, not application code.

## Test Coverage

- Unit tests for `link_query()`, `link_if_absent()`, `delete_document()`, `bulk_unlink()`, `link_audit()`
- UNIQUE constraint violation tests (duplicate detection)
- JSONL malformed-line resilience tests
- `catalog_links` MCP returns `{nodes, edges}` structure test
- Generator idempotency tests (`link_if_absent` returns False on second call)
