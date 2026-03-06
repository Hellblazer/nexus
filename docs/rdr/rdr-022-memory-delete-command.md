---
id: RDR-022
title: "Add delete subcommand to nx memory, nx store, nx scratch"
type: enhancement
status: closed
priority: P2
created: 2026-03-05
accepted_date: 2026-03-05
closed_date: 2026-03-06
close_reason: implemented
---

# RDR-022: Add delete subcommand to nx memory, nx store, nx scratch

## Problem

No storage tier has a targeted delete command. Removing a T2 entry currently requires:

1. Locating the SQLite database at `~/.config/nexus/memory.db`
2. Inspecting the schema manually
3. Running raw `sqlite3` DELETE statements

The same problem exists for T3 (`nx store`) — there is no way to remove a single
knowledge entry without deleting the entire collection (`nx collection delete`).
T1 (`nx scratch`) has `clear` (drop all) but no per-entry removal.

This was discovered during a routine cleanup of stale RDR metadata in a project. A task
that should take one command required ~10 shell invocations and direct database manipulation.

## Research Findings

### T2 — DB layer is already ready (Verified)

`T2Database.delete(project, title) -> bool` exists in `src/nexus/db/t2.py`. Only a thin
CLI wrapper is needed. `T2Database.delete()` will be overloaded with an `id: int | None`
keyword argument (matching the `get()` API pattern) rather than adding a separate
`delete_by_id` method.

### FTS5 cascade is automatic (Verified)

The schema in `t2.py` defines three triggers: `memory_ai`, `memory_ad`, `memory_au`.
The `memory_ad` trigger fires `AFTER DELETE ON memory` and issues the FTS5 tombstone
insert automatically. Hard-deleting from the `memory` table is sufficient — no explicit
FTS5 shadow table manipulation required.

### T3 — ID is 16-char hex, not UUID; no substring-match API (Verified)

`T3Database.put()` derives `doc_id` as `hashlib.sha256(f"{collection}:{title}".encode()).hexdigest()[:16]`
— a 16-character hex string. `nx store list` currently truncates this to 12 chars (a
cosmetic choice with no benefit). ChromaDB's `col.get()` accepts `ids=` (exact list) or
`where=` (metadata equality filter) — neither supports ID substring matching. The
`where={"$contains": ...}` hint in the original design is incorrect; that is a metadata
filter, not an ID filter. Prefix scanning requires fetching all IDs client-side and is
O(n) with non-unique results possible.

**Fix**: show the full 16-char ID in `nx store list` (one-character change in `store.py`)
and require the exact 16-char ID for `nx store delete --id`. `col.delete(ids=[full_id])`
is then a single round-trip.

### T3 — delete_by_id is a thin wrapper (Verified)

`_delete_batch()` is the internal primitive in `t3.py`. `T3Database.delete_by_id(collection,
doc_id)` calls `col.delete(ids=[doc_id])` directly — it does not need the paginated path
since it targets a single known ID.

### T1 — scratch entries are session-scoped by metadata, not by collection (Verified)

The scratch collection is shared (`_COLLECTION = "scratch"`). `T1Database.clear()` correctly
filters by `session_id` before deleting. A naive `col.delete(ids=[id])` would succeed on
entries from other sessions (or silently no-op on unknown IDs — ChromaDB does not raise on
missing IDs). `T1Database.delete(id)` must first call `col.get(ids=[id], where={"session_id":
self._session_id})` to verify ownership, then delete. Returns False (not found / wrong session)
rather than silently succeeding.

### Missing verb audit across all tiers (Verified)

| Command | Verb | Gap |
|---------|------|-----|
| `nx memory` | `delete` | CLI missing; DB ready |
| `nx store` | `delete` | CLI missing; needs `delete_by_id` in T3Database |
| `nx scratch` | `delete <id>` | CLI missing; needs session-scoped `delete()` in T1Database |
| `nx store` | `get` | No single-entry retrieve — out of scope here |

## Design

Scope broadened from T2-only to all three tiers — the problem is identical across them
and shipping memory-delete without store-delete would leave an obvious asymmetry.

### `nx memory delete`

```
nx memory delete --project <project> --title <title>
nx memory delete --id <id>
nx memory delete --project <project> --all [--yes]
```

**Option constraints** (enforced via Click mutual exclusion):
- `--id` is mutually exclusive with `--project`, `--title`, and `--all`
- `--all` requires `--project`; mutually exclusive with `--title`
- `--all` without `--project` is rejected with a usage error

**Confirmation behaviour:**
- Single-entry (`--project/--title` or `--id`): fetch the full row first; display
  `project/title` and a 120-char content preview; ask `Delete? [y/N]`
- Bulk (`--all`): display project name and entry count; ask `Delete N entries from <project>? [y/N]`
- `--yes` / `-y` bypasses all prompts (scripting)
- Not-found exits non-zero via `click.ClickException`

**DB changes:** overload `T2Database.delete()` to accept `id: int | None = None` in
addition to `project` and `title`, matching the `get()` API pattern.

### `nx store delete`

```
nx store delete --collection <collection> --id <doc_id>
nx store delete --collection <collection> --title <title> [--yes]
```

- `--collection` is always required (no default; unlike `nx store list`)
- `--id`: exact 16-char hex ID as shown by `nx store list`; calls
  `col.delete(ids=[doc_id])` directly — no prefix scan, no client-side iteration
- `--title`: exact match on the metadata `title` field via
  `col.get(where={"title": title})`; if multiple chunks share the title (e.g. a
  multi-chunk document), all are listed and a single confirmation deletes all of them
- Not-found exits non-zero via `click.ClickException`
- No `--all` flag — use `nx collection delete <name>` instead

**Companion change:** update `nx store list` to show the full 16-char ID (change `[:12]`
→ `[:16]` in `store.py`).

**DB changes:** add `T3Database.delete_by_id(collection: str, doc_id: str) -> bool` using
`col.delete(ids=[doc_id])`.

### `nx scratch delete`

```
nx scratch delete <id>
```

- ID is the full string ID as shown by `nx scratch list` (currently displayed as `[:8]`
  prefix — implementation must use the full ID internally, or show enough chars to be
  unambiguous; see note below)
- No confirmation (T1 is ephemeral scratch)
- `T1Database.delete(id)` verifies session ownership via
  `col.get(ids=[id], where={"session_id": self._session_id})` before deleting
- If entry not found or belongs to another session: exit non-zero with
  `click.ClickException("scratch entry {id!r} not found")`

**Note on ID display**: `nx scratch list` currently shows `[:8]` of the full ChromaDB
UUID. The `delete` command accepts the same 8-char prefix only if the implementation
resolves it to the full UUID (via `col.get()` scan filtered by session) before deletion.
Alternatively, show more chars in `nx scratch list`. Either choice is implementation-time;
the requirement is that the command never silently deletes the wrong entry.

**DB changes:** add `T1Database.delete(id: str) -> bool` with session-ownership check.

## Decision

Implement in three phases, each as an independent bead:

1. **T2**: overload `T2Database.delete()` with `id=` + `nx memory delete`
2. **T3**: `T3Database.delete_by_id` + `nx store delete` + fix `nx store list` ID display
3. **T1**: `T1Database.delete` (session-scoped) + `nx scratch delete`

Hard delete throughout. No soft-delete / tombstoning. No `--dry-run`.
