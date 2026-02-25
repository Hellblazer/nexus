# Project Management

## T2: The Local Store

T2 is a local SQLite database that replaced the AllPepper Memory Bank MCP
server. The model is simple: every entry has a **project**, a **title**, and
**content** — like a flat filesystem where project is the directory and title
is the filename. Entries can have tags, a TTL, and full-text search via FTS5.
No API keys, no network. It survives restarts.

```bash
nx memory put "auth uses JWT with 24h expiry" --project myrepo --title auth-notes
nx memory get --project myrepo --title auth-notes
nx memory search "JWT" --project myrepo
```

That's the whole model. Everything else in this document is a usage pattern
built on it.

## PM: Conventions on T2

Multi-week projects accumulate state that needs to survive between sessions:
what phase you're in, what's blocking progress, what each agent last worked
on. Without somewhere to hold this, every new conversation starts cold.

`nx pm` is a set of convenience commands that manage T2 entries with specific
titles and tags. There's no separate PM database or schema — it's the same
store, the same entries, just with conventions that the commands know about.

`nx pm init` writes four entries into the repo's T2 namespace, tagged `pm`:

| Title | What it holds |
|-------|---------------|
| `METHODOLOGY.md` | Engineering discipline and workflow for this project |
| `BLOCKERS.md` | Active blockers as a bullet list |
| `CONTEXT_PROTOCOL.md` | Context management rules and relay format |
| `phases/phase-1/context.md` | Current phase goals and state |

These are ordinary T2 entries. You can read them with `nx memory get`, edit
them with `nx memory put`, and find them with `nx memory list`. The `nx pm`
commands just know which titles to look for.

## Working With PM

**Status** shows where things stand — phase number, last-active agent, and
open blockers. Phase is computed from the highest `phase:N` tag across all
PM entries, so there's no separate counter to get out of sync:

```bash
nx pm status
```

**Resume** assembles a continuation summary from PM entries — phase, blockers,
recent activity, current phase context — capped at 2000 characters. The
SessionStart hook calls this automatically, so every Claude Code session opens
with project state already injected:

```bash
nx pm resume
```

**Blockers** are bullets in BLOCKERS.md. Add and remove them by content or
line number:

```bash
nx pm block "waiting on API access"
nx pm unblock 1
```

**Phases** are logical chunks of work. Each phase gets its own context entry.
Advance when you're ready:

```bash
nx pm phase next
```

This creates `phases/phase-2/context.md` (and so on) without touching the
previous phase entries, which stay available for reference.

## Archiving

When a project is done or paused, archive synthesizes the accumulated state
and moves it to permanent storage:

```bash
nx pm archive
nx pm close        # shorthand for archive with status "completed"
```

This does two things. First, Haiku reads all PM entries and produces a
structured summary — key decisions, architecture choices, challenges,
lessons learned — which goes to T3 as a permanent, searchable document.
Second, the T2 entries start a 90-day decay. During that window you can
restore them with `nx pm restore`; after it, they expire and the T3
synthesis is what remains.

To search across archived projects:

```bash
nx pm reference "caching strategy"     # semantic search across all archives
nx pm reference projectname            # direct lookup for one project
```

## Session Integration

The plugin's SessionStart hooks automatically inject PM context so agents
know where a project stands without being told. Two hooks contribute:

1. `nx hook session-start` detects whether the current repo has PM entries
   and, if so, runs `pm_resume()` to inject phase, blockers, and recent
   activity.

2. The plugin's `session_start_hook.py` calls `nx pm resume` and
   `nx pm status`, adds the T2 memory listing and ready beads.

Together, these give every session a picture of current project state.

## Task Tracking With Beads

[Beads](https://github.com/BeadsProject/beads) (`bd`) is an external
task-tracking tool that the plugin integrates with. Where PM tracks
project-level state — phases and blockers — beads tracks individual work
items: tasks, bugs, features, their dependencies, and who's working on what.

The plugin wires beads into the session lifecycle:

- **SessionStart** and **PreCompact** run `bd prime` to load bead context
- **SessionStart** also shows ready beads (unblocked work) via `bd ready`
- **SubagentStart** injects the active bead so spawned agents know what
  task they're continuing
- **RDR close** (`/rdr-close`) decomposes a decision into beads — one epic
  for the overall effort, plus task beads for each implementation step
- **Branch naming** ties git branches to beads: `feature/<bead-id>-<description>`

Beads is optional. Nexus and PM work without it. But when present, it gives
agents a shared view of what work is available, what's blocked, and what's
in progress — the task-level complement to PM's project-level view.

See the [beads documentation](https://github.com/BeadsProject/beads) for
`bd` command reference.

## Relationship to RDR

PM tracks execution — phases, blockers, working state. RDR tracks decisions —
research, design, review. They're complementary but independent: you can use
either without the other.

When you do use both, the connection is manual: close an RDR, then update the
current PM phase context to reference it. There's no automated link between
the two — just a convention that phase docs mention the RDRs that drove them.
