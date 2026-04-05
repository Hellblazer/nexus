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

### Component 3: Catalog API — `update_link()`

```python
def update_link(
    self,
    from_t: Tumbler, to_t: Tumbler, link_type: str,
    *, new_type: str = "", new_from_span: str | None = None,
    new_to_span: str | None = None, meta: dict | None = None,
) -> bool:
    """Update fields on an existing link. Preserves created_by and created_at."""
```

JSONL: appends a new record (last-line-wins). SQLite: UPDATE in place. The old record remains in JSONL history for git audit trail.

### Component 4: Catalog API — Bulk Operations

```python
def bulk_unlink(self, **filters) -> int:
    """Delete all links matching the filter. Returns count removed."""
    # Uses same filter syntax as link_query
    # Appends tombstones to JSONL for each deleted link

def bulk_update_links(self, filters: dict, **fields) -> int:
    """Update fields on all links matching the filter. Returns count updated."""
```

### Component 5: Catalog API — Validation & Audit

```python
def validate_link(self, from_t: Tumbler, to_t: Tumbler) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    # Check both endpoints exist via resolve()
    # Check for duplicates

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
| `catalog_link` | `link()` (exists) | Create — add endpoint validation |
| `catalog_unlink` | `unlink()` (exists) | Delete specific |
| `catalog_link_update` | `update_link()` | Change type/span/meta |
| `catalog_link_query` | `link_query()` | Composable filter — the workhorse |
| `catalog_link_bulk` | `bulk_unlink()` / `bulk_update_links()` | Bulk operations by filter |
| `catalog_link_audit` | `link_audit()` | Health report |
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

nx catalog link-update FROM TO --type cites         # NEW
  --new-type supersedes
  --meta '{"note": "corrected classification"}'

nx catalog link-audit                               # NEW
  --json                                            # machine-readable report

nx catalog link-bulk-delete                         # NEW
  --created-by index_hook --type implements
  --dry-run                                         # preview before delete

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

### Immutable links — no update, only delete+recreate (rejected)
Loses provenance (`created_at`, `created_by`). Links evolve as understanding deepens — a `relates` link may be reclassified as `cites` after reading the paper more carefully. Preserving the creation record matters.

## Success Criteria

- [ ] `link_query()` answers "all bib_enricher citations" in one call
- [ ] `link_if_absent()` is idempotent — no duplicates from repeated generator runs
- [ ] `update_link()` changes type without losing `created_by`/`created_at`
- [ ] `bulk_unlink(created_by="index_hook")` cleans up a bad run in one call
- [ ] `link_audit()` reports orphans and duplicates
- [ ] `catalog_link_query` MCP tool enables agent-driven graph exploration
- [ ] `catalog_links` MCP returns nodes + edges (no N+1 show calls)
- [ ] All CLI commands accept titles in addition to tumblers
- [ ] E2E test: generate links → query by creator → bulk delete → regenerate → verify count matches
- [ ] 100% of link operations go through `CatalogDB.execute()` (thread-safe)

## Open Questions

1. **Should `link()` validate endpoints by default?** Validation adds a `resolve()` call per endpoint. For bulk generators this is O(2N) extra queries. Option: validate in `link_if_absent()` only, skip in raw `link()`.

## Implementation Plan

Depends on: RDR-049 (closed), RDR-050 (accepted).

Estimated order:
1. `link_query()` + `catalog_link_query` MCP + CLI — the foundation
2. `link_if_absent()` + update generators to use it
3. `update_link()` + `catalog_link_update` MCP + CLI
4. `bulk_unlink()` / `bulk_update_links()` + MCP + CLI
5. `link_audit()` + `catalog_link_audit` MCP + CLI
6. Fix `catalog_links` to return nodes
7. Update SubagentStart hook + skill references
8. E2E test suite

## Research Findings

### RF-1: Current Link Storage Analysis (2026-04-05)
**Classification**: Verified — Codebase Analysis | **Confidence**: HIGH

Links are stored as rows in SQLite `links` table with composite indexes `idx_links_from_type` and `idx_links_to_type`. A `link_query()` with `(from_t, link_type)` or `(to_t, link_type)` hits an index. A query with only `created_by` requires `idx_links_created_by` (exists). A query with only `link_type` requires `idx_links_type` (exists but single-column — less selective). All index patterns needed for the proposed query filters are already in place. No schema changes required.

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
