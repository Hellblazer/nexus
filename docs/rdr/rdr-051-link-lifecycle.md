---
title: "Link Lifecycle: Full CRUD, Queryable Links, Bulk Operations"
id: RDR-051
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: pending
created: 2026-04-05
related_issues:
  - "RDR-049 - Git-Backed Xanadu-Inspired Catalog for T3 (closed)"
  - "RDR-050 - Knowledge Graph and Catalog-Aware Query Planning (accepted)"
---

# RDR-051: Link Lifecycle — Full CRUD, Queryable Links, Bulk Operations

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The catalog (RDR-049) has links. The knowledge graph (RDR-050) uses links. But the link API is incomplete — it has create and delete but no update, no query, no bulk operations, no validation, and no audit. Links are the entire point of the catalog: they encode relationships between code, design documents, research papers, and knowledge. Without a complete link lifecycle, the graph is write-once and opaque.

Specific gaps:

- **No update**: Can't change a link's type, span, or metadata without delete+recreate — loses `created_at` and `created_by` provenance
- **No query**: Can only traverse from a specific node (`links_from`, `links_to`, `graph`). Can't answer "show me all `bib_enricher` citation links" or "how many `implements` links exist?"
- **No bulk operations**: Can't clean up a bad generator run. Can't reassign all links of one type to another. Can't prune by age or origin.
- **No validation**: Can link to nonexistent tumblers. No duplicate detection on manual create. No orphan detection.
- **No audit**: No way to assess graph health — orphaned links, duplicate links, creator distribution, type distribution over time.
- **MCP gap**: `catalog_links` discards resolved nodes. No `catalog_link_query` for flat filtering. Agents can't explore the link graph without knowing a starting tumbler.

Nelson: *"Link search is deemed to be 'free'... THE QUANTITY OF LINKS NOT SATISFYING A REQUEST DOES NOT IN PRINCIPLE IMPEDE SEARCH ON OTHERS."* (Literary Machines Ch. 4). Our link search isn't free — it's barely possible.

## Context

### Current Link API Surface

| Layer | Create | Read | Update | Delete | Query | Bulk |
|-------|--------|------|--------|--------|-------|------|
| **Catalog (Python)** | `link()` | `links_from()`, `links_to()`, `graph()` | -- | `unlink()` | -- | -- |
| **MCP tools** | `catalog_link` | `catalog_links` | -- | `catalog_unlink` | -- | -- |
| **CLI** | `nx catalog link` | `nx catalog links` | -- | `nx catalog unlink` | -- | -- |
| **Auto-generators** | `generate_citation_links()`, `generate_code_rdr_links()` | -- | -- | -- | -- | -- |

### Nelson's FEBE Link Search (Literary Machines Ch. 4)

Nelson's most powerful link command is `FINDLINKSFROMTOTHREE(home-set, from-set, to-set, type-set)` — a 4-dimensional filter. Our current API offers 2 dimensions (from + type, or to + type). A composable query with all dimensions is the design target.

### Dependencies

- **RDR-049** (closed): Provides `Catalog`, `CatalogLink`, `CatalogDB`, JSONL+SQLite persistence, `fcntl` locking, `_filter_fields` for schema evolution.
- **RDR-050** (accepted): Query planner uses `catalog_links` for graph traversal. Link query enables the planner to scope by origin/type before traversing.

## Proposed Solution

### Design Principle: Query-Oriented

Treat links as a first-class queryable collection. One composable query method replaces multiple narrow accessors. Bulk operations are "query then act" — the same filter syntax drives read, delete, and update.

### Component 1: Catalog API — `link_query()`

```python
def link_query(
    self,
    from_t: Tumbler | None = None,
    to_t: Tumbler | None = None,
    link_type: str = "",
    created_by: str = "",
    direction: str = "",  # "in"/"out"/"both" — sugar for from_t/to_t
    tumbler: Tumbler | None = None,  # used with direction
    limit: int = 100,
    offset: int = 0,
) -> list[CatalogLink]:
    """Composable link filter. All parameters are optional; combine to narrow."""
```

SQL backend: parameterized WHERE clause built from non-empty filters, with `LIMIT`/`OFFSET`. Uses existing composite indexes `idx_links_from_type` and `idx_links_to_type`.

Replaces use cases of `links_from()` and `links_to()` for flat queries. `graph()` remains for BFS traversal.

### Component 2: Catalog API — `link_if_absent()`

```python
def link_if_absent(
    self,
    from_t: Tumbler, to_t: Tumbler, link_type: str, created_by: str,
    *, from_span: str = "", to_span: str = "", **meta,
) -> bool:
    """Idempotent link creation. Returns True if created, False if existed."""
```

Checks `link_query(from_t, to_t, link_type)` before insert. All auto-generators should use this instead of manual dedup.

### Component 3: Catalog API — `delete_document()`

```python
def delete_document(self, tumbler: Tumbler) -> None:
    """Tombstone a document. Links remain (intentionally orphaned per RF-9)."""
```

JSONL: appends tombstone (`_deleted: true`). SQLite: DELETE from documents. Links to/from this tumbler are NOT deleted — `link_audit()` reports them as orphaned. This preserves the historical record that the link existed.

### ~~Component 3 (original): `update_link()`~~ — REMOVED per RF-6

Changing a link's type changes its composite key — it IS a new fact. The correct operation is `unlink()` + `link()`. For span/meta changes on the same key, `link()` detects the existing entry and merges (Component 2 handles this).

### Component 4: Catalog API — Bulk Operations

```python
def bulk_unlink(self, **filters) -> int:
    """Delete all links matching the filter. Returns count removed.
    Uses same filter syntax as link_query.
    Appends tombstones to JSONL (preserving original created_by in tombstone).
    """
```

~~`bulk_update_links`~~ — REMOVED per RF-6. Link identity is `(from_t, to_t, link_type)` — all three are immutable. Span and meta changes go through the idempotent `link()` upsert (Component 2). Bulk type-reclassification is `bulk_unlink()` + re-run generator.

### Component 5: Catalog API — Validation & Audit

```python
def validate_link(self, from_t: Tumbler, to_t: Tumbler, link_type: str) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    # Check both endpoints exist via resolve()
    # Check for duplicate (from_t, to_t, link_type) — RF-6 composite key

def link_audit(self) -> dict:
    """Return link graph health report."""
    # {
    #   "total": N,
    #   "by_type": {"cites": 42, "implements": 7, ...},
    #   "by_creator": {"bib_enricher": 38, "user": 11, ...},
    #   "orphaned": [{"from": "1.1.99", "to": "1.2.1", ...}],
    #   "duplicates": [{"from": ..., "to": ..., "count": 3}],
    # }
```

Validation is called by `link()` and `link_if_absent()`. `link()` warns on invalid endpoints but does not block (links to future/ghost elements are valid). `link_if_absent()` blocks on duplicates.

### Component 6: MCP Tools

| Tool | Maps to | Purpose |
|------|---------|---------|
| `catalog_link` | `link()` (exists) | Create/upsert — idempotent with merge (RF-8) |
| `catalog_unlink` | `unlink()` (exists) | Delete specific |
| `catalog_link_query` | `link_query()` | Composable filter — admin/audit workhorse (RF-11) |
| `catalog_link_bulk` | `bulk_unlink()` | Bulk delete by filter (no bulk update — RF-6) |
| `catalog_link_audit` | `link_audit()` | Health report — orphans, duplicates, stats |
| `catalog_links` | `graph()` (exists) | BFS traversal — fix to return nodes |

### Component 7: CLI Commands

```
nx catalog link A B --type cites                    # exists — add validation
nx catalog unlink A B --type cites                  # exists
nx catalog links A --depth 2                        # exists — fix node discard

nx catalog link-query                               # NEW
  --from 1.1.42 --to 1.2.1
  --type cites --created-by bib_enricher
  --limit 20 --offset 0 --json

nx catalog link-audit                               # NEW
  --json                                            # machine-readable report

nx catalog link-bulk-delete                         # NEW
  --created-by index_hook --type implements
  --dry-run                                         # preview before delete

nx catalog delete TUMBLER                           # NEW — tombstone document, orphan links

nx catalog generate-links                           # exists
```

All commands that take tumbler arguments also accept titles (via `_resolve_tumbler()`).

### Component 8: Agent & Skill Integration

**query-planner.md**: No changes needed — `catalog_links` operation already handles graph traversal. `catalog_link_query` is available as an MCP tool but not a plan operation (flat link listing is not a retrieval step — it's an admin/audit operation).

**SKILL.md**: Add `catalog_link_query` to the storage tools reference. The skill can use it for pre-traversal scoping: "only follow `cites` links from `bib_enricher`" — filter by `created_by` before graph traversal.

**SubagentStart hook**: Already injects catalog tools for relevant tasks. Add `catalog_link_query` and `catalog_link_audit` to the injected block.

**deep-research-synthesizer**: Can use `catalog_link_query(created_by="bib_enricher", link_type="cites")` to assess citation graph density before deciding whether to traverse.

### Component 9: Generator Improvements

Update `generate_citation_links()` and `generate_code_rdr_links()` to use `link_if_absent()` instead of manual dedup sets. Simpler code, consistent validation.

### Component 10: `catalog_links` MCP — Return Nodes

Fix the existing `catalog_links` MCP tool to return both nodes and edges from `graph()`:

```python
return {
    "nodes": [_entry_to_dict(n) for n in result["nodes"]],
    "edges": [_link_to_dict(e) for e in result["edges"]],
}
```

The skill's `$step_N.collections` extraction reads `nodes[*].physical_collection` directly — no N+1 `catalog_show` calls.

## Explicitly Deferred

### Home-Set Filtering
Nelson's 4th dimension. Links in our system are free-floating — no "home document." Build only when competing organizational views (concept nodes from different agents) create ambiguity.

### Link Versioning
Track link history (who changed it, when, what was the previous type). Git history of `links.jsonl` already provides this implicitly. Explicit versioning adds complexity without demonstrated need.

### Computed Link Properties
Derived fields like "transitive citation count" or "shortest path length." These are query-time computations, not stored properties. Build only if graph queries become a performance bottleneck.

## Alternatives Considered

### Separate links table per link type (rejected)
Would enable per-type indexes but fragments the query surface. A single table with type column + composite index is simpler and sufficient at current scale (expected <10K links).

### GraphQL-style link query language (rejected)
Over-engineering. SQL WHERE clauses composed from keyword arguments are sufficient and match the existing `search` MCP tool's `where` parameter pattern.

### Immutable links — no update, only delete+recreate (partially adopted)
Per RF-6: link type IS identity. Reclassifying `relates` → `cites` is `unlink()` + `link()` — a new fact, not an edit. But span and meta changes on the same `(from, to, type)` key are handled by `link()` upsert with merge, preserving `created_by` and `created_at`. This is a middle ground: identity fields are immutable, metadata fields are mutable.

## Success Criteria

- [ ] `link_query()` answers "all bib_enricher citations" in one call
- [ ] `link()` is idempotent — detects existing `(from, to, type)` and merges meta/created_by (RF-8)
- [ ] `link_if_absent()` returns bool without merge — generators use this for fast dedup
- [ ] `delete_document()` tombstones document, leaves links intact (RF-9)
- [ ] `bulk_unlink(created_by="index_hook")` cleans up a bad run in one call
- [ ] `link_audit()` reports orphans, duplicates, and stats by type/creator
- [ ] `catalog_link_query` MCP tool enables agent-driven audit (not a planner step — RF-11)
- [ ] `catalog_links` MCP returns nodes + edges (no N+1 show calls)
- [ ] All CLI commands accept titles in addition to tumblers
- [ ] Composite index `(created_by, link_type)` added (RF-12)
- [ ] E2E test: generate links → query by creator → bulk delete → regenerate → verify count matches
- [ ] 100% of link operations go through `CatalogDB.execute()` (thread-safe)
- [ ] `created` vs `created_at` naming standardized (RF-5)

## Open Questions

1. ~~**Should `link()` validate endpoints by default?**~~ **RESOLVED**: `link()` does NOT validate endpoints — links to ghost/future elements are valid (RF-9). `link_audit()` reports orphaned links post-hoc. Generators use `link_if_absent()` which is fast (dedup check only, no validation).

## Implementation Plan

Depends on: RDR-049 (closed), RDR-050 (accepted).

Estimated order:
1. **Resilience first**: JSONL reader error handling — try/except + skip + log for malformed lines (GST S2)
2. **Schema**: UNIQUE constraint on `(from_tumbler, to_tumbler, link_type)` + `(created_by, link_type)` composite index + standardize `created`→`created_at` (RF-5, RF-12)
3. **MCP title resolution**: shared `_resolve_tumbler_mcp()` helper for all catalog MCP tools (GST S4) — prerequisite for Steps 4-5
4. **Idempotent link()**: upsert with merge semantics (RF-6, RF-8) + `link_if_absent()` for generators + `batch_id` in meta for generator runs (GST S3) + fix tombstone to preserve original `created_by`
5. **link_query()** + `catalog_link_query` MCP + `nx catalog link-query` CLI
6. **bulk_unlink()** + `catalog_link_bulk` MCP + `nx catalog link-bulk-delete` CLI (with `--created-at-before` for time-range cleanup)
7. **delete_document()** + `nx catalog delete` CLI (RF-9)
8. **link_audit()** + `catalog_link_audit` MCP + `nx catalog link-audit` CLI (include orphans, duplicates, stale spans)
9. **Fix graph()**: include starting node in results + fix `catalog_links` MCP to return `{nodes, edges}` dict (GST S8) — document breaking change for callers expecting list
10. Update SubagentStart hook + skill references
11. E2E test suite

## Research Findings

### RF-1: Current Link Storage Analysis (2026-04-05)
**Classification**: Verified — Codebase Analysis | **Confidence**: HIGH

Links are stored as rows in SQLite `links` table with composite indexes `idx_links_from_type` and `idx_links_to_type`. A `link_query()` with `(from_t, link_type)` or `(to_t, link_type)` hits an index. A query with only `created_by` requires `idx_links_created_by` (exists). A query with only `link_type` requires `idx_links_type` (exists but single-column — less selective). Most index patterns are already in place. Schema additions required: UNIQUE constraint on `(from_tumbler, to_tumbler, link_type)` and composite `(created_by, link_type)` index (see RF-12).

### RF-2: JSONL Last-Line-Wins for Link Updates (2026-04-05)
**Classification**: Design Decision | **Confidence**: HIGH

`update_link()` must append a new complete record to `links.jsonl` (last-line-wins) rather than modifying in place. The JSONL key for links is `(from_t, to_t, link_type)`. If `update_link()` changes the type, the key changes — this requires: (1) tombstone the old `(from, to, old_type)` key, (2) append a new record with `(from, to, new_type)`. Same pattern as document `update()` but with the added complexity of a composite key that includes the field being updated.

### RF-3: Nelson on Link Modification (2026-04-05)
**Classification**: Verified — Literary Machines Ch. 4 | **Confidence**: HIGH

Nelson's Xanadu links are immutable in the public record but mutable in the user's private link space. Our catalog is a private link space (owned by one user/team), so mutability is appropriate. The git history of `links.jsonl` serves as the immutable audit trail — the current state is mutable, the history is not.

### RF-4: Implementation Audit — 10 Concrete Gaps (2026-04-05)
**Classification**: Verified — Deep Codebase Analysis | **Confidence**: HIGH

Full trace of every link code path (write, read, delete) with line numbers. Key findings:

**Schema gaps:**
1. No UNIQUE constraint on `(from_tumbler, to_tumbler, link_type)` in SQLite — application dedup only
2. No `link_type` validation in `Catalog.link()` or MCP `catalog_link` — free string accepted
3. No endpoint validation — links to nonexistent tumblers silently accepted
4. `created` vs `created_at` naming inconsistency across JSONL/SQLite/CatalogLink

**Write path gaps:**
5. `Catalog.link()` has no dedup check — calling twice creates duplicate rows
6. Partial write risk: JSONL append before SQLite INSERT; crash between leaves inconsistent state (recovered by `_ensure_consistent` on next startup)

**Read path gaps:**
7. MCP `catalog_links` drops `graph()` nodes — returns edges only
8. No `catalog_link_query` MCP tool — no way to filter links by created_by or type globally
9. Link `id` (SQLite primary key) never exposed — cannot reference a specific link row

**Delete path gaps:**
10. No filter-only delete — cannot delete all links of a type from a node without specifying every target

**Design decisions from audit:**
- Add `UNIQUE(from_tumbler, to_tumbler, link_type)` to schema with `INSERT OR IGNORE` for idempotent `link_if_absent()`
- `link()` stays permissive (no type validation at core — CLI and agents enforce their own sets)
- `update_link()` when changing type: tombstone old key + insert new key (RF-2 confirmed)
- `link_query()` built on parameterized WHERE — all needed indexes already exist (RF-1 confirmed)
- Expose `id` in `CatalogLink` for targeted operations but keep `(from, to, type)` as the logical key

### RF-5: Naming Inconsistency — `created` vs `created_at` (2026-04-05)
**Classification**: Verified — Codebase Analysis | **Confidence**: HIGH

Three layers use different names for the same timestamp field:
- `LinkRecord.created` (tumbler.py:93) — JSONL serialization uses `"created"`
- SQLite column: `created_at` (catalog_db.py:72)
- `CatalogLink.created_at` (catalog.py:70) — Python API uses `created_at`

The mapping works via positional indexing (`row[6]` in `_row_to_link`) and explicit assignment in `rebuild()` (`lnk.created` → `created_at` column). Correct but fragile. RDR-051 should standardize on `created_at` everywhere or accept the mismatch as documented technical debt.

### RF-6: Link Identity — Composite Key Is Canonical (2026-04-05)
**Classification**: Design Decision | **Confidence**: HIGH

The canonical link identity is the composite triple `(from_t, to_t, link_type)`. The SQLite `id` is a session-ephemeral surrogate — not stored in JSONL, not stable across `rebuild()`, not exposed in the API.

**Implication**: There should be no `update_link()`. Changing a link's type changes its identity — it IS a new fact. The correct operation is `unlink()` + `link()`. This is the same pattern Neo4j uses: relationship IDs are internal, identity is `(startNode, type, endNode)`. Document this as a design rule, not an omission.

For span and meta changes on an existing link (same key), `link()` should detect the existing entry and merge rather than duplicate. This is an upsert, not an update.

### RF-7: Link Directionality — Single Direction, Bidirectional Read (2026-04-05)
**Classification**: Design Decision | **Confidence**: HIGH

All links are stored as single directional records. `graph(direction="both")` provides bidirectional traversal by querying both `links_from()` and `links_to()`. Do NOT auto-create reverse links for `relates` — `unlink()` only deletes by exact `(from, to, type)` and would leave ghost reverse links.

`quotes(A→B)` is directional. "What quotes B?" is answered by `catalog_links(B, direction="in", link_type="quotes")` — no `quoted_by` type needed.

### RF-8: Concurrent Creation — Merge created_by, Don't Duplicate (2026-04-05)
**Classification**: Design Decision | **Confidence**: HIGH

When two agents independently discover the same link, the dedup key stays `(from_t, to_t, link_type)`. `link()` should detect the existing entry and merge the new `created_by` into `meta["co_discovered_by"]` (a list). The primary `created_by` retains the first discoverer. This preserves provenance without inflating link count.

Do NOT include `created_by` in the dedup key — that would mean `cites(A, B, bib_enricher)` and `cites(A, B, agent-1)` are different facts, defeating deduplication.

### RF-9: Document Deletion — Orphaned Links Are Intentional (2026-04-05)
**Classification**: Design Decision (Nelson-faithful) | **Confidence**: HIGH

When a document is tombstoned, its links remain. "A cited B, but B was removed" is more informative than silently deleting the citation. This matches Xanadu (addresses are vacant, not erased) and git (blame annotations survive file deletion).

RDR-051 must deliver:
1. `Catalog.delete_document(tumbler)` — write tombstone, DELETE from SQLite, leave links
2. `link_audit()` → orphaned links (either endpoint not in documents table)
3. Documentation: orphaned links are intentional, not a consistency violation

### RF-10: Span Semantics — Advisory, Not Content-Addressed (2026-04-05)
**Classification**: Design Decision | **Confidence**: HIGH

Spans (`from_span`, `to_span`) are positional markers at index time — "chunks 3-7 when the link was created." They break on re-index if content shifts. This is acceptable: the link (A cites B) remains valid; only the span is advisory.

Content-addressed spans (character offsets or chunk content hashes) require chunk-level tumblers (`1.2.5.3`). Defer to Layer 3+.

### RF-11: link_query Is Admin/Audit, Not a Planner Operation (2026-04-05)
**Classification**: Design Decision | **Confidence**: HIGH

`link_query()` — "find all links matching global criteria" — is a maintenance operation, not a search scoping step. It does not produce `physical_collection` values for downstream search. Do not add it to `query-planner.md` as a plan operation. It belongs as an MCP tool (`catalog_link_query`) for direct invocation by humans and audit agents.

The planner's `catalog_links` operation (BFS from a tumbler) is the correct graph traversal primitive for query plans.

### RF-12: Performance — Add Composite Index (created_by, link_type) (2026-04-05)
**Classification**: Verified — SQLite EXPLAIN QUERY PLAN | **Confidence**: HIGH

The most common audit query `WHERE created_by='bib_enricher' AND link_type='cites'` uses only `idx_links_created_by` (single column). At 10K+ links, this is a full scan over all bib_enricher rows. Add:
```sql
CREATE INDEX IF NOT EXISTS idx_links_created_by_type ON links(created_by, link_type);
```

### RF-13: General Systems Theory Analysis (2026-04-05)
**Classification**: Verified — Systems Analysis | **Confidence**: HIGH

Full GST analysis covering boundary definition, signal flows, feedback loops, homeostasis, entropy, emergence, hierarchy, and requisite variety. 0 critical, 8 significant, 12 observations.

**Key system findings:**

**Boundaries**: Seven interfaces cross the link system boundary. The MCP→Agent interface is thinner than CLI→Human (inverted hierarchy — agents have fewer capabilities than humans). The storage boundary has a naming inconsistency (`created` vs `created_at`) and dual-write atomicity risk.

**Signal flows**: Creation signal has a gap between JSONL write and SQLite INSERT recoverable by `_ensure_consistent()`. Query signal loses nodes at MCP boundary (Component 10 fix). Deletion signal loses original `created_by` in tombstones. Auto-generation signal has no batch identifier for selective undo.

**Feedback loops**: Positive loop (more links → better queries → more research → more links) is healthy. Negative loop (junk filtering via `created_by` → `bulk_unlink`) exists but is entirely human-triggered. **Missing**: link quality feedback (no signal when an agent follows a link to irrelevant content), generator accuracy feedback (bad links recreated on next run), stale span detection.

**Homeostasis**: System recovers from most perturbations except: (1) corrupt JSONL line crashes entire `read_links()` — no skip/continue error handling; (2) content-divergence without count-divergence goes undetected by `_ensure_consistent()`.

**Entropy**: Link count grows without bound. No TTL, aging, or relevance decay. Generator-created links will dominate. Orphan fraction increases over time. Anti-entropy: compact (file size only), audit (diagnostic only), git history (ultimate undo).

**Emergence**: Implicit document importance from inbound link count (not computed). Title resolution errors propagate through subsequent links. Hub removal disconnects graph.

**Requisite variety**: 4 query dimensions match Nelson's FEBE minus home-set (correctly deferred). Missing: batch/run identifier for generator undo, link migration for tumbler changes on re-index.

**Significant findings to address in implementation:**

| ID | Finding | Impact | Fix |
|---|---|---|---|
| S2 | JSONL readers crash on single malformed line | Data loss on corrupt file | try/except + skip + log in `read_links/read_documents/read_owners` |
| S3 | No batch/run ID on generated links | Cannot selectively undo bad generator run | Add `batch_id` or `created_at` range to meta |
| S4 | MCP lacks title resolution | Agents need 2+ round trips vs CLI's 1 | Add tumbler_or_title params to link MCP tools |
| S7 | Unbounded link growth | Graph noise at scale | `last_verified` timestamp; audit flags stale |
| S8 | graph() excludes starting node | Callers need extra catalog_show | Include starting node in results |
