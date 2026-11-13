# The threads board: thread fields

The short form of the board convention is AGENTS.md § Cross-session board: one T2 entry per session in project `threads`, titled with the session's name, whose first line is `session: <session id>`, with one `## <the work in plain words>` block per open thread. This page lists the fields a thread block can carry and who reads each one. Structured asks (`ask:` blocks) are RDR-245 and the `asks` skill, not repeated here.

Each field is one line, `key: value`, and belongs to the nearest `#`, `##` or `###` heading above it; a bullet (`- due: ...`) is not a field. Every field here ends an open `ask:` block, so put a thread's fields above its first `ask:` line. A key is matched with `-`, space and `_` treated alike, in any case. A field that is not listed here is kept as a note on the thread, except that a key mentioning "sam" or "needs" is read as an ask.

| Field | Value | Read by |
|---|---|---|
| `ref:` | comma-separated bead ids, RDRs, T2 titles, shas | console |
| `state:` | where the work stands, one line | console |
| `waiting_on:` | who or what the thread waits on; `sam` makes it an ask | console |
| `needs_sam:` | a decision only Sam can make, or `ask <id>` pointing at an ask block | console (Needs you), `nx ask` |
| `due:` | `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM[:SS[.f]]` with an optional `Z` or `±HH:MM` | console, SessionStart |
| `worktree:` | an absolute path, or another stable id for where the work lives: an artifact slug or a file path | console |

## `due:` (nexus-ct74m)

When the thread wants attention again: a measurement window, a recount date, a deploy reading. Write it instead of relying on a session-only cron or a reminder in the conversation, both of which a restart or a `/clear` loses.

- `due: 2026-11-17` is due from local midnight on that day.
- `due: 2026-11-17T09:00` (no offset) is local time; `due: 2026-11-17T17:00Z` or an explicit offset is exact.
- Those shapes only: a space instead of `T`, `20261117` or an hour with no minutes is not read.
- `due: none` or an empty value is no due date. Any other value that is not one of the shapes above never comes due, and both the SessionStart section and the console say so, so a typo is not silent.
- A thread is due once the time is at or past the value. Given twice, the last `due:` line counts.

Once the time has come, the thread is due until its `due:` line is changed or removed:

- Every SessionStart (startup, resume, clear, compact, fork) prints `## Due on your threads board (<entry>): move or delete each due: line once you have acted on it`, with one line per due thread, its refs and worktree. The `session-context` hook finds the entry the way the PreCompact checkpoint does: every entry whose `session:` line names the session's current id, or, when none does, the entry titled exactly the session's name (from Claude Code's `~/.claude/sessions/<pid>.json`). The name is what carries a reminder across a restart, `/clear`, resume or fork, all of which give the session a new id. A session with no name whose `session:` line is out of date gets no reminder. The lookup has a hard budget (`NX_BOARD_DUE_BUDGET_S`, default 3 s); over budget, or on any failure, it prints nothing and logs a warning to the hook log.
- `nx console`, Threads tab, shows `due <value>` on the thread, highlighted once it has come.
- The PreCompact checkpoint's half of nexus-ct74m reads the same line to mark a past `due:` as overdue.

Move the date forward, or delete the line, when you act on it.

## `worktree:` (nexus-ct74m)

Where the thread's work lives, so a session coming back from a compaction or a restart does not have to guess from its cwd. Use the worktree's absolute path (`worktree: /Users/…/git/nexus-wt/marlowe-ct74m`). For work that has no worktree, an artifact slug or a file path serves the same purpose.

The console shows it under the thread. The PreCompact checkpoint's half of nexus-ct74m takes it as its first choice for the session's worktree.
