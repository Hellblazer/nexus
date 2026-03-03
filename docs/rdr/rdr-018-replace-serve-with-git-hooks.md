---
title: "Replace nx serve Polling Server with Git Hooks"
type: refactor
status: draft
priority: P2
author: Hal Hildebrand
date: 2026-03-03
related_issues: []
---

# RDR-018: Replace nx serve Polling Server with Git Hooks

## Problem

`nx serve` starts a Flask + Waitress background process with a dedicated threading poll
loop. Every 10 seconds it iterates every registered repo, runs `git rev-parse HEAD`, and
compares the result against a stored hash to decide whether to trigger `index_repository`.

Problems with this design:

- **Always-on daemon** — requires `nx serve start` before auto-indexing works; users
  forget to start it, or it dies silently after a system restart.
- **Polling overhead** — wakes up every 10 seconds regardless of activity; wastes CPU
  and generates unnecessary `git rev-parse` subprocess calls for idle repos.
- **Operational complexity** — PID file, log file, start/stop/status/logs subcommands,
  stale PID detection. All of this is accidental complexity.
- **REST API nobody uses** — `GET/POST/DELETE /repos` HTTP endpoints exist but are never
  called by anything. Registration is done via `nx index repo` instead.
- **Race conditions** — HEAD-hash polling can miss rapid consecutive commits; hooks fire
  exactly once per qualifying git operation.

## Decision

**Delete `nx serve` entirely. Replace the polling loop with git hooks.**

### What gets deleted

| File / component | Reason |
|------------------|--------|
| `src/nexus/server.py` | Flask app + poll thread — no longer needed |
| `src/nexus/polling.py` | HEAD-hash polling logic — replaced by event-driven hooks |
| `src/nexus/commands/serve.py` | `nx serve start/stop/status/logs` CLI — deleted |
| `src/nexus/cli.py` serve registration | Remove `cli.add_command(serve)` |
| Tests for serve + polling | Delete alongside the code |
| `docs/` references to `nx serve` | Update to `nx hooks` |

### What gets added

**`nx hooks` command group** (`src/nexus/commands/hooks.py`):

| Subcommand | Description |
|------------|-------------|
| `nx hooks install [PATH]` | Install hooks into `.git/hooks/` for the repo at PATH (default: cwd) |
| `nx hooks uninstall [PATH]` | Remove nexus-managed hooks from the repo |
| `nx hooks status [PATH]` | Show which hooks are installed and whether they are nexus-managed |

**Hook script** installed into `.git/hooks/post-commit`, `.git/hooks/post-merge`,
`.git/hooks/post-rewrite`:

```bash
#!/bin/sh
# managed by nexus — do not edit manually
nx index repo "$(git rev-parse --show-toplevel)" &
```

The `&` runs indexing in the background so git operations are never blocked.

**Hooks installed:**

| Hook | Fires when |
|------|-----------|
| `post-commit` | After every `git commit` |
| `post-merge` | After `git pull` / `git merge` |
| `post-rewrite` | After `git rebase` / `git commit --amend` |

`post-checkout` is deliberately excluded — branch switches do not change file content
and would trigger unnecessary reindexing.

### Explicit install, with reminder

Hooks are **not** installed automatically. `nx index repo` checks for the presence of
the nexus hooks and prints a one-line reminder if they are missing:

```
Tip: run `nx hooks install` to auto-index this repo on every commit.
```

This keeps `nx index repo` side-effect-free while still making the feature discoverable.

### Coexistence with existing hooks

If a `.git/hooks/post-commit` already exists and is not nexus-managed:

- `nx hooks install` appends a nexus stanza to the existing script rather than
  overwriting it.
- `nx hooks uninstall` removes only the nexus stanza, leaving the rest intact.
- `nx hooks status` reports `managed (appended)` vs `managed (owned)` vs `not installed`.

A file is considered "nexus-managed" if it contains the comment
`# managed by nexus`.

## Alternatives Considered

### B — Hybrid (keep serve, add hooks)

Keep `nx serve` for environments where hooks are impractical (bare repos, CI workers)
and add hooks as the primary mechanism. Rejected: maintains two code paths and all the
operational complexity we are trying to eliminate. Bare repos and CI can call
`nx index repo` directly.

### C — Filesystem watcher (watchfiles / inotify)

Replace polling with `watchfiles` watching `.git/HEAD` or the worktree. Still requires
a background daemon process — same operational cost as the current server, just slightly
more responsive. Rejected: all the daemon complexity remains; hooks are both simpler
and more semantically correct (fires on git events, not arbitrary filesystem writes).

## Success Criteria

- [ ] `nx serve` command group is gone; `nx` help no longer lists it
- [ ] `server.py` and `polling.py` are deleted; no references remain in the codebase
- [ ] `nx hooks install` installs post-commit, post-merge, post-rewrite scripts
- [ ] `nx hooks uninstall` removes nexus stanzas without destroying unrelated hook content
- [ ] `nx hooks status` correctly identifies managed vs unmanaged vs absent hooks
- [ ] `nx index repo` prints the reminder iff none of the three hooks are installed
- [ ] Existing hook coexistence: appending and removal work correctly
- [ ] All tests pass; deleted test files removed; new hook tests added
- [ ] `docs/cli-reference.md`, `docs/repo-indexing.md`, `docs/contributing.md` updated

## Impact

- **Removes** ~400 lines across `server.py`, `polling.py`, `commands/serve.py`
- **Adds** ~150 lines in `commands/hooks.py` + tests
- **Net**: simpler codebase, zero background processes, event-driven reindexing
