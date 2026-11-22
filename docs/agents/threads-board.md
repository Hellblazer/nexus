# The threads board: thread fields

The short form of the board convention is AGENTS.md § Cross-session board: one T2 entry per session in project `threads`, titled with the session's name, with one `## <the work in plain words>` block per open thread. The entry's first line is `identity: i<n>`, the id `nx identity show` names. The identity outlives `/clear` and restart, and the checkpoint, `nx ask` answer routing, the console, the due-date hook and the aura threads block find the entry by it. The engine refuses a write that removes the line, changes it, or names another identity. An older entry's `session:` line is ignored: no reader looks an entry up by it, an entry that has a `session:` line and no `identity:` line is not found, and a session that no token file binds to an identity finds no entry. The line must be exactly `identity: i<n>` at the start of a line, the form the engine fences, and a new entry may name only the writer's own identity. One identity on two entries is a lint finding and routes no answer. An ask in an entry with no `identity:` line is a lint finding (`no-identity`): no answer can be routed to it. The entry's title is the seat's live alias; an ephemeral identity (a `-n` that names no seat) has no alias, so its title is `<display name> (i<n>)`, or `ephemeral (i<n>)` when the launch gave no name (`nx identity show` prints the title). `nx identity promote` moves the entry to the new alias: from engine 0.1.198 the engine does it in the promote's own transaction, from any shell, and never overwrites an entry already titled with the alias (the command says so and the entry keeps its title); against an older engine the client does it only inside the owning session, and from another shell the entry keeps its title and is found by its `identity:` line. This page lists the fields a thread block can carry and who reads each one. Structured asks (`ask:` blocks) are RDR-245 and the `asks` skill, not repeated here.

Each field is one line, `key: value`, and belongs to the nearest `#`, `##` or `###` heading above it; a bullet (`- due: ...`) is not a field. Every field here ends an open `ask:` block, so put a thread's fields above its first `ask:` line. A key is matched with `-`, space and `_` treated alike, in any case. A field that is not listed here is kept as a note on the thread, except that a key mentioning "sam" or "needs" is read as an ask.

| Field | Value | Read by |
|---|---|---|
| `ref:` | comma-separated bead ids, RDRs, T2 titles, shas | console |
| `state:` | where the work stands, one line | console |
| `waiting_on:` | who or what the thread waits on; `sam` makes it an ask | console |
| `needs_sam:` | a decision only Sam can make, or `ask <id>` pointing at an ask block | console (Needs you), `nx ask` |
| `due:` | `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM[:SS[.f]]` with an optional `Z` or `±HH:MM` | console, SessionStart, the PreCompact checkpoint |
| `worktree:` | an absolute path, or another stable id for where the work lives: an artifact slug or a file path | console, the PreCompact checkpoint |

A thread whose `waiting_on:` is `none` is one you are working on now. When it is finished, delete the block; when it waits on someone or something, name that in `waiting_on:`. A stale `none` (an umbrella lane, a `(done)` thread left in place) reads as open work to the checkpoint and to anyone reading the board: in the turn-end census of 2026-11-12..19, 1999 turn ends showed a `none` thread and a sample of 14 long idles held no real miss (T2 `nexus/turn-end-owned-idle-census-2026-11-19`).

## `due:` (nexus-ct74m)

When the thread wants attention again: a measurement window, a recount date, a deploy reading. Write it instead of relying on a session-only cron or a reminder in the conversation, both of which a restart or a `/clear` loses.

- `due: 2026-11-17` is due from local midnight on that day.
- `due: 2026-11-17T09:00` (no offset) is local time; `due: 2026-11-17T17:00Z` or an explicit offset is exact.
- Those shapes only: a space instead of `T`, `20261117` or an hour with no minutes is not read.
- `due: none` or an empty value is no due date. Any other value that is not one of the shapes above never comes due, and both the SessionStart section and the console say so, so a typo is not silent.
- A thread is due once the time is at or past the value. Given twice, the last `due:` line counts.

Once the time has come, the thread is due until its `due:` line is changed or removed:

- Every SessionStart (startup, resume, clear, compact, fork) prints `## Due on your threads board (<entry>): move or delete each due: line once you have acted on it`, with one line per due thread, its refs and worktree. The `session-context` hook finds the entry in the PreCompact checkpoint's two steps: every entry whose `identity:` line names the identity a token file binds the session's id to, or, when none does, the entry titled exactly the session's name (from Claude Code's `~/.claude/sessions/<pid>.json`), taken only when its own `identity:` line matches. The identity survives `/clear`, fork and a fresh start under the same seat, so the session's id changing does not lose the entry. Not covered: a session with no token file (no identity, so no entry), and an entry with no `identity:` line. The lookup has a hard budget (`NX_BOARD_DUE_BUDGET_S`, default 3 s); over budget, or on any failure, it prints nothing and logs a warning to the hook log.
- `nx console`, Threads tab, shows `due <value>` on the thread, highlighted once it has come.
- The PreCompact checkpoint reads the same line and prints `due <value>: due now` once its time has come.

Move the date forward, or delete the line, when you act on it.

## `worktree:` (nexus-ct74m)

Where the thread's work lives, so a session coming back from a compaction or a restart does not have to guess from its cwd. Use the worktree's absolute path (`worktree: /Users/…/git/nexus-wt/marlowe-ct74m`). For work that has no worktree, an artifact slug or a file path serves the same purpose.

The console shows it under the thread. The PreCompact checkpoint takes it as its first choice for the session's worktree, whatever the directory is named; an absolute path that does not exist is reported as `worktree <value>: no such path`, and a value naming an existing file is accepted.
