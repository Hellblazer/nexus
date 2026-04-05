---
title: "Post-Mortem: Git-Backed Xanadu-Inspired Catalog for T3"
date: 2026-04-05
rdr: RDR-049
status: closed
close_reason: implemented
type: post-mortem
severity: n/a
---

# Post-Mortem: RDR-049 — Git-Backed Xanadu-Inspired Catalog for T3

**Closed**: 2026-04-05  **Reason**: implemented  **PR**: #126
**Branch**: `feature/nexus-lrlo-catalog-foundation`
**Epic**: nexus-pkbj (18 beads, 5 phases, 17/17 closed + epic auto-closed)

## Problem Statement

T3 (ChromaDB) stores chunks but has no concept of "documents" or relationships
between them. A PDF becomes 200 anonymous vectors. A code file becomes fragments
with no parent. There is no way to ask "what cites this paper?" or "which code
implements this RDR?" or even "what documents exist?"

RDR-049 designed a Xanadu-inspired catalog layer that gives T3 a document
registry, hierarchical addressing, and typed bidirectional links — all backed
by git-mergeable files.

## What Was Built

1. **Tumbler addressing** — hierarchical addresses (store.owner.document.chunk)
   inspired by Ted Nelson's Literary Machines. Every document gets a stable
   address that survives re-indexing.

2. **JSONL + SQLite dual storage** — JSONL files are the source of truth
   (git-backed, mergeable), SQLite is the query cache (rebuilt automatically
   on mtime change via `_ensure_consistent`).

3. **Typed bidirectional links** — `cites`, `implements`, `implements-heuristic`,
   `supersedes`, `relates`, `comments`, `quotes` — with `created_by` provenance
   tracking.

4. **Auto-registration hooks** — every indexing pathway (`index repo`,
   `index pdf`, `store put`, `enrich`) auto-registers catalog entries. No
   manual curation.

5. **Link generators** — citation auto-generation from Semantic Scholar IDs,
   code-to-RDR heuristic linking via title substring match.

6. **8 MCP tools** — `catalog_search`, `catalog_show`, `catalog_list`,
   `catalog_register`, `catalog_update`, `catalog_link`, `catalog_links`,
   `catalog_resolve`.

7. **Full CLI** — `nx catalog setup/search/show/list/links/link/unlink/
   stats/sync/pull/generate-links/delete/update`.

## Implementation Stats

| Metric | Value |
|--------|-------|
| Beads | 18 (epic nexus-pkbj) |
| Phases | 5 |
| Tests added | ~180 across 14 test files |
| Files changed | 75 |
| Insertions | 9,588 |
| Commits | 72 |

Key modules: `catalog/catalog.py`, `catalog/catalog_db.py`,
`catalog/tumbler.py`, `catalog/link_generator.py`,
`catalog/consolidation.py`.

## What Went Well

1. **Xanadu framing was productive.** Nelson's tumbler addressing, link types,
   and "link search is free" principles gave clear architectural guidance.
   Instead of inventing an ontology, we adapted one with 60 years of thought
   behind it.

2. **JSONL + SQLite dual storage worked exactly as designed.** Git-backed JSONL
   gives durability and mergeability. SQLite gives fast queries. The mtime-based
   `_ensure_consistent` rebuild keeps them synchronized without manual
   intervention.

3. **Hook-based auto-registration.** The catalog populates itself during normal
   indexing — 558 documents registered without any user action. Zero friction
   adoption means the catalog is always current.

4. **`nx catalog setup` as a single entry point.** One command initializes,
   populates from all T3 collections, and generates links. This eliminated
   the bootstrap problem entirely.

5. **Link generators bootstrapped the graph.** Citation cross-matching and
   code-to-RDR heuristic linking produced 428 links automatically. The
   catalog shipped with a useful link graph from day one.

## What Went Wrong

1. **ChromaDB Cloud quota violations.** The catalog setup command hit
   ChromaDB's rate limits when scanning large collections for backfill.
   Required adding per-collection progress reporting, timeouts, and
   pagination. Fix: 5802dc3.

2. **Silent data loss — the five-alarm audit.** The nexus-s5mf audit found
   11 bugs across 7 modules where errors were silently swallowed. These were
   pre-existing bugs exposed by catalog work, not introduced by it. Every
   instance was `except Exception: pass` or `_log.debug()` hiding real
   failures. All 11 fixed in a single commit.

3. **JSONL reader fragility.** Initial implementation crashed on any malformed
   line. Had to add per-line try/except with skip + structured log to handle
   partial corruption gracefully. Production JSONL files accumulate garbage
   lines from interrupted writes.

4. **RDR backfill pagination.** Initial backfill only processed the first page
   of chunks (45 entries instead of 259). ChromaDB's default page size is not
   "all results." Fix: f11adfb.

5. **Deadlock in sync and defrag.** Concurrent catalog sync and defrag
   operations could deadlock on SQLite locks. Fixed with operation ordering.
   Fix: 70cda82.

## Plan vs Actual

| Aspect | Plan | Actual | Divergence |
|--------|------|--------|------------|
| Layer 3 (concept nodes) | Deferred | Deferred | None — no demonstrated need |
| Home-set filtering | Deferred | Deferred | None — not needed at current scale |
| `_ensure_consistent` rebuild | Simple mtime check | Needed `degraded` flag (nexus-f2vp) | More fragile than expected |
| Link types | 6 types | 7 types (added `implements-heuristic`) | Needed to distinguish auto from manual |
| MCP tools | 6 planned | 8 shipped | Added `catalog_links` and `catalog_resolve` during implementation |
| Error handling | Standard | Required resilience audit (nexus-s5mf) | Pre-existing silent failures surfaced |

## Key Learnings

1. **JSONL + SQLite is a powerful pattern for git-backed metadata.** JSONL
   provides the durability and mergeability guarantees of flat files. SQLite
   provides the query performance of a database. Mtime-based consistency
   checks bridge them. This pattern is reusable beyond the catalog.

2. **Ted Nelson's principles translate to modern systems.** Tumblers, typed
   links, "categories are user business," and "link search is free" all
   provided clear design guidance. The 60-year-old Xanadu design vocabulary
   saved weeks of ontology invention.

3. **Silent error swallowing is the #1 data corruption vector.** The audit
   found 11 instances across 7 modules. Every single one was actively
   corrupting or losing data in production. The pattern is always the same:
   `except Exception` with `pass` or `_log.debug()` where the exception
   signals a real failure that needs propagation or at minimum
   `_log.error()`.

4. **Auto-registration via hooks beats manual curation.** The catalog
   populated itself with 558 documents during normal indexing. Users never
   need to think about catalog registration. Any system that requires
   manual curation will have stale metadata within a week.

5. **Paginate everything.** ChromaDB, SQLite, any data source with a default
   page size — always paginate through all results. The "first page only"
   bug is silent and produces plausible-looking partial results that pass
   casual inspection.
