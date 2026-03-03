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

**Effective hooks directory** — before installing, `nx hooks install` runs:

```bash
git rev-parse --git-common-dir
```

to resolve the actual git directory (handles worktrees, where `.git` is a gitlink file).
It also checks `git config core.hooksPath`; if set, hooks are installed there instead of
`.git/hooks/`, and `install`/`status`/`uninstall` all operate on that path. If
`core.hooksPath` is set to a directory not writable by the user, `nx hooks install`
prints a clear warning rather than silently failing.

**Hook script** installed into the effective hooks directory as `post-commit`,
`post-merge`, and `post-rewrite`:

```bash
#!/bin/sh
# >>> nexus managed begin >>>
nx index repo "$(git rev-parse --show-toplevel)" \
  >> "$HOME/.config/nexus/index.log" 2>&1 &
disown
# <<< nexus managed end <<<
```

- Output is appended to `~/.config/nexus/index.log` so failures are auditable.
- `disown` detaches the process from the terminal session, preventing kill on terminal
  close.
- `nx doctor` reports the log path and its last-modified time.

**Concurrency guard** — `index_repository` acquires a per-repo file lock
(`~/.config/nexus/locks/<repo-hash>.lock`) before indexing. If a lock is already held
(e.g. rapid-fire hooks during `git rebase -i`), subsequent hook invocations exit
immediately rather than launching concurrent index runs.

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

A file is considered "nexus-managed" if it contains the sentinel marker
`# >>> nexus managed begin >>>`. Detection uses the sentinel (not a looser comment
string) to avoid false positives.

The nexus stanza is bounded by sentinel comments for reliable install/uninstall:

```bash
# >>> nexus managed begin >>>
nx index repo "$(git rev-parse --show-toplevel)" &
# <<< nexus managed end <<<
```

`uninstall` removes everything between (and including) the sentinel lines.

### `nx hooks install` behaviour

```
$ nx hooks install
Installing hooks for /path/to/repo…
  ✓ post-commit  (created)
  ✓ post-merge   (appended to existing hook)
  ✓ post-rewrite (created)
Done. Indexing will run in the background after each commit.
```

### `nx hooks uninstall` behaviour

```
$ nx hooks uninstall
Removing nexus hooks from /path/to/repo…
  ✓ post-commit  (removed)
  ✓ post-merge   (stanza removed, existing hook preserved)
  ✓ post-rewrite (removed)
Done.
```

### `nx doctor` integration

The "Nexus server (optional)" check in `doctor.py` currently imports from
`commands/serve.py` and is deleted along with the serve command.

It is replaced with two checks:

**Hooks check** — iterates every registered repo using the same effective-directory
resolution logic as `nx hooks install` (worktree-aware, `core.hooksPath`-aware):

```
  ✓ git hooks: /path/to/repo (post-commit, post-merge, post-rewrite)
  ✗ git hooks: /path/to/other-repo — not installed
    Fix: nx hooks install /path/to/other-repo
  ✓ git hooks: /path/to/third-repo (core.hooksPath=/shared/hooks — 3 hooks active)
```

If no repos are registered yet:

```
  ✓ git hooks: no repos registered (run: nx index repo <path>)
```

The hooks check is always `✓` (non-fatal) — missing hooks are a reminder.

**Index log check** — reports the hook log location and last activity:

```
  ✓ index log: ~/.config/nexus/index.log (last write: 4 minutes ago)
```

If the log does not exist yet:

```
  ✓ index log: ~/.config/nexus/index.log (not created yet — hooks have not fired)
```

### `head_hash` lifecycle

The registry's `head_hash` field was updated by the polling server after each successful
index. With hooks, `index_repository` itself updates `head_hash` to the current HEAD
after a successful run (moving the update responsibility from the caller to the callee).
This keeps the registry accurate and enables future `nx doctor` diagnostics (e.g.
"last indexed at commit abc1234").

### Credential failure behaviour

The polling server retried indexing on `CredentialsMissingError` by not advancing
`head_hash`, allowing the next poll to catch it. With hooks, a commit during a
credentials outage will fail silently (logged to `index.log`). Recovery requires the
user to manually run `nx index repo` after restoring credentials. This is an acceptable
regression given the overall complexity reduction; it is documented here so users are
aware.

### CLI naming

`src/nexus/commands/hooks.py` (plural) is distinct from the existing
`src/nexus/commands/hook.py` (singular), which handles `nx hook session-start/session-end`
(Claude Code lifecycle hooks). The CLI group name `nx hooks` (plural) disambiguates from
`nx hook` (singular). Both names are intentional and refer to different hook systems.

### `post-push` exclusion

`post-push` is not installed because it fires after the local push completes, not after
the remote receives changes. The local working tree has already been indexed by
`post-commit` before the push. Remote-side indexing (e.g. on a CI server) requires
a separate `nx index repo` invocation and is out of scope for this RDR.

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
- [ ] `nx hooks install` resolves the effective hooks directory via `git rev-parse --git-common-dir` and respects `core.hooksPath`
- [ ] `nx hooks install` installs post-commit, post-merge, post-rewrite scripts; output shows per-hook created/appended status
- [ ] `nx hooks install` warns clearly if `core.hooksPath` is set to a non-writable path
- [ ] `nx hooks uninstall` removes nexus sentinel stanzas without destroying unrelated hook content; output shows per-hook removed/preserved status
- [ ] `nx hooks status` correctly identifies: managed-owned, managed-appended, unmanaged, absent — for each of the three hooks; uses same directory resolution as install
- [ ] `nx hooks status` reports effective hooks directory (`.git/hooks/` or `core.hooksPath`)
- [ ] Hook script redirects stdout+stderr to `~/.config/nexus/index.log` and calls `disown`
- [ ] `index_repository` acquires a per-repo file lock; concurrent hook invocations skip rather than pile up
- [ ] `index_repository` updates `head_hash` in the registry after a successful run
- [ ] `nx index repo` prints the reminder iff none of the three hooks are detected in the effective hooks directory
- [ ] `nx doctor` shows per-repo hook status (worktree-aware, `core.hooksPath`-aware); non-fatal with Fix: hint
- [ ] `nx doctor` shows index log path and last-modified time; no longer imports from `commands/serve.py`
- [ ] Worktree install: hooks installed into main repo's hooks dir, not the gitlink stub
- [ ] `core.hooksPath` install: hooks installed into the configured path, not `.git/hooks/`
- [ ] All tests pass; deleted test files removed; new hook tests added (happy path, worktree, coexistence, lock guard)
- [ ] `docs/cli-reference.md`, `docs/repo-indexing.md`, `docs/contributing.md` updated

## Impact

- **Removes** ~411 lines across `server.py` (125), `polling.py` (77), `commands/serve.py` (209)
- **Adds** ~250 lines in `commands/hooks.py` + doctor update + lock guard in indexer + tests
- **Net**: simpler codebase, zero background processes, event-driven reindexing,
  auditable log, better diagnostics
