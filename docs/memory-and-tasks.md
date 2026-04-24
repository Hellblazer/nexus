# Memory and Tasks

## T2: The Local Store

T2 is a local SQLite database with a simple model: every entry has a
**project**, a **title**, and **content** — like a flat filesystem where
project is the directory and title is the filename. Entries can have tags,
a TTL, and full-text search via FTS5. No API keys, no network. It survives
restarts.

```bash
nx memory put "auth uses JWT with 24h expiry" --project myrepo --title auth-notes
nx memory get --project myrepo --title auth-notes
nx memory search "JWT" --project myrepo
```

That's the whole model. Store whatever context you need — design notes,
active decisions, working state — and retrieve it by project and title.

**Title resolution is exact-then-prefix (4.11.0, nexus-e59o).** If
`--title` does not match any entry exactly, a unique prefix match is
returned. Ambiguous prefixes list the candidates and fail rather than
silently picking one. So `nx memory get --project nexus_rdr --title
088-research-1` resolves to the full
`088-research-1: RDR-092 baseline for Gap 4 spike` entry as long as
only one entry in the project starts with that prefix.

**Access tracking (RDR-057)**: `memory_get` and `memory_search` hits
increment `access_count` and update `last_accessed` on the returned rows.
This drives heat-weighted TTL — frequently-accessed entries survive longer
than their nominal `ttl`. See
[Configuration — Heat-Weighted T2 Expiry](configuration.md#heat-weighted-t2-expiry)
for the formula and retention math. Internal scans (like `find_overlapping_memories`)
pass `access="silent"` to avoid contaminating the staleness signal.

Under sustained concurrent cross-domain write load (RDR-063 Phase 2 interaction),
the access-count increment runs as a best-effort side-effect: roughly 5–10% of
updates can be skipped during heavy indexing and are logged at warning as
`memory.access_tracking.skipped`. The returned row content is unaffected; only
the counter update may be skipped. See
[Storage Tiers — Heat-Weighted Expiry](storage-tiers.md#t2----memory-bank) for the
full explanation.

## Taxonomy cascade on delete

When a memory entry is deleted (`memory_delete` MCP tool or `nx memory delete`), the T2 facade also calls `CatalogTaxonomy.purge_assignments_for_doc(project, title)`. This removes any topic assignments that reference the deleted entry and drops any topics left empty by the deletion. The cascade is handled by the T2 facade — `MemoryStore` itself has no knowledge of taxonomy tables. If the delete is by numeric id rather than `(project, title)`, the facade resolves the row's project and title first.

## Consolidation (RDR-061 E6)

T2 grows with every note an agent writes. Over time, near-duplicate entries
accumulate — different titles, similar content. The `memory_consolidate` MCP
tool provides three hygiene operations:

```
# Find overlapping pairs (Jaccard > 0.7)
mcp__plugin_nx_nexus__memory_consolidate(action="find-overlaps", project="myrepo")

# Flag entries not accessed in 30+ days
mcp__plugin_nx_nexus__memory_consolidate(action="flag-stale", project="myrepo", idle_days=30)

# Preview a merge (dry run)
mcp__plugin_nx_nexus__memory_consolidate(
    action="merge", project="myrepo",
    keep_id=42, delete_ids="43", merged_content="...",
    dry_run=True)

# Execute the merge (single delete — no confirm required)
mcp__plugin_nx_nexus__memory_consolidate(
    action="merge", project="myrepo",
    keep_id=42, delete_ids="43", merged_content="...")

# Multi-entry merge (confirm required)
mcp__plugin_nx_nexus__memory_consolidate(
    action="merge", project="myrepo",
    keep_id=42, delete_ids="43,44,45", merged_content="...",
    confirm_destructive=True)
```

**Safety**: Merges use SQLite's write lock via `with self.conn:` to ensure
UPDATE and DELETE are atomic. If `keep_id` was deleted by a concurrent
`expire()` call, the merge raises `KeyError` and `delete_ids` survive —
preventing silent data loss when the consolidation scan races with TTL
expiry.

The `/nx:query` skill (via `nx_answer`) and the `nx_tidy` MCP tool
(formerly the knowledge-tidier agent; RDR-080) both use these
operations during periodic memory hygiene.

## Session Integration

The plugin's SessionStart hooks automatically surface T2 memory context so
agents know where a project stands without being told. Two hooks contribute:

1. `nx hook session-start` (internal, invoked by plugin hooks) lists recent T2 entries for the current repo.

2. The plugin's `session_start_hook.py` scans all T2 namespaces for the
   project (bare, `_rdr`, etc.) and injects a summary, along with ready beads.

## Task Tracking With Beads

[Beads](https://github.com/BeadsProject/beads) (`bd`) is an external
task-tracking tool that the plugin integrates with. Beads tracks individual
work items: tasks, bugs, features, their dependencies, and who's working on
what.

The plugin wires beads into the session lifecycle:

- **SessionStart** and **PreCompact** run `bd prime` to load bead context
- **SessionStart** also shows ready beads (unblocked work) via `bd ready`
- **SubagentStart** injects the active bead so spawned agents know what
  task they're continuing
- **RDR close** (`/nx:rdr-close`) decomposes a decision into beads — one epic
  for the overall effort, plus task beads for each implementation step
- **Branch naming** ties git branches to beads: `feature/<bead-id>-<description>`

See the [beads documentation](https://github.com/BeadsProject/beads) for
`bd` command reference.

## Relationship to RDR

RDR tracks decisions — research, design, review. T2 memory tracks working
state. They're complementary but independent: you can use either without
the other.

When you use both, the connections are automated:

- `/nx:rdr-close` creates beads (epic + task beads) for implementation tracking.
  The `epic_bead` field in each RDR's T2 metadata provides a machine-readable
  link from decision to work items.
- RDR decisions surface as prior art during planning via `nx search "topic"` against the knowledge corpus.
- RDR T2 metadata includes timestamps, so you can find which decisions were
  active during any phase without manual cross-referencing.
