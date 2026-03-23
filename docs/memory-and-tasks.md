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

## Session Integration

The plugin's SessionStart hooks automatically surface T2 memory context so
agents know where a project stands without being told. Two hooks contribute:

1. `nx hook session-start` lists recent T2 entries for the current repo.

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
