# RDR-010 Post-Mortem: T1 Cross-Process Session Sharing

**Status:** Implemented
**PR:** `feature/rdr-010-t1-http-server` (#38)
**Closed:** 2026-03-01

## What Was Built

Replaced `chromadb.EphemeralClient` with a per-session ChromaDB HTTP server +
PPID chain propagation mechanism. All agents in a session tree share one scratch
namespace. Concurrent independent Claude Code windows stay isolated via disjoint
OS process trees.

## Plan vs. Implementation

All 10 implementation plan steps delivered. Key divergences:

| Plan | Actual |
|------|--------|
| `write_session_file()` writes JSON with `started_by` | `write_session_record()` — no `started_by`; ownership via PPID-keyed file existence |
| `start_t1_server()` returns 3-tuple | Returns 4-tuple `(host, port, server_pid, tmpdir)` |
| `_ppid_of()` uses only `ps` | `/proc/{pid}/status` first (Linux containers), `ps` fallback |
| Ownership: `started_by == os.getpid()` | Ownership: `sessions/{ppid}.session` file presence |

## Additions Beyond Original Plan

Found during two rounds of code review (PR #38):

- **Reconnect resilience (`T1Database`)** — Silent reconnect once on connection
  error (e.g. parent session ended while child is running), then marks a `_dead`
  flag to prevent loops. EphemeralClient fallback preserves local T1 functionality
  even after server death. Three methods: `_exec`, `_reconnect`, `_dead`.

- **`fcntl.flock(LOCK_EX)`** — Sibling-agent race prevention in `session_start()`.
  Without this, two parallel agents both seeing `ancestor=None` could each start
  a ChromaDB server and orphan the first.

- **Zombie reap** — `os.waitpid(server_pid, WNOHANG)` after SIGKILL in
  `stop_t1_server()` prevents zombie processes when the hook script is a direct
  parent.

- **`session_id` pre-initialization** — `session_id` initialized before the
  `flock` try-block to prevent `NameError` if `fcntl.flock` raises unexpectedly.

- **`structlog` logging** — Warning for missing `flush_record` at session end;
  debug for corrupt/unreadable session files during PPID chain walk.

## Test Coverage

`tests/test_t1.py`:
- `TestT1DatabaseConstructor` — constructor paths (HTTP, EphemeralClient fallback,
  client injection, real file read)
- `TestT1DatabaseReconnect` — 6 tests covering reconnect/dead-flag behavior
- `TestT1DatabaseCRUD`, `TestT1DatabaseFlag`, `TestT1DatabaseClear` — full CRUD

All 1819 tests passing (unit + plugin_structure + integration with real ChromaDB).

## Live Validation Bugs Found Post-Close (2026-03-01, PR #40)

Two bugs surfaced during the first live session validation after rc8 shipped:

### Bug 1 — `chroma run --log-level` removed in chroma 1.x

`start_t1_server()` passed `--log-level ERROR` to `chroma run`. This flag was
dropped in chroma 1.x; `chroma 1.3.1` exits with code 2 immediately on
encountering the unknown flag. The session hook caught the early exit and fell
back to EphemeralClient, silently degrading T1 to per-process isolation.

**Fix:** Remove the `--log-level` argument. stdout/stderr were already discarded
via `DEVNULL`, so silencing was already handled.

**Lesson:** Version-pin or smoke-test CLI flags when taking a dependency on an
external binary's argument interface.

### Bug 2 — Session file keyed to transient shell subprocess

`session_start()` called `ppid = os.getppid()` to determine the session file
key. In the Claude Code hook invocation model, `os.getppid()` returns the
**transient zsh subprocess** that Claude Code spawns for each `Bash(...)` call
— not Claude Code itself. That shell exits the moment the hook completes, so
the session file path (e.g. `sessions/41462.session`) is never reachable from
subsequent Bash invocations whose PPID chains run through a different (newer)
transient shell.

**Root cause:** The PPID chain topology assumed the hook runs as a direct child
of Claude Code. In practice:

```
Claude Code (40496, stable) → zsh/A (41462, transient) → nx hook session-start
```

Hook writes `sessions/41462.session`. Next `nx scratch put` runs in:

```
Claude Code (40496) → zsh/B (41599, transient) → nx scratch put
```

Walk: 41599 → 40496 → ... never visits 41462 (already dead). Cache miss.

**Fix:** Key to the grandparent instead of the direct parent:
```python
_direct_ppid = os.getppid()
ppid = _ppid_of(_direct_ppid) or _direct_ppid
```
`_ppid_of(zsh/A)` = Claude Code (40496) — stable for the session lifetime.
All subsequent Bash invocations walk through 40496 and find the file.

**Verified:** `nx scratch put` → `nx scratch list` now returns the stored entry
across separate Bash calls within the same Claude session.

**Lesson:** The "PPID = direct parent" assumption breaks when hooks are run
through a shell intermediary. An integration test that spans two separate
subprocess invocations would have caught this before release.

## PPID Topology: Empirically Confirmed (2026-03-01)

Spawned a subagent via the Agent tool and traced its full PPID chain:

```
37930 (python subprocess) → 37928 (zsh) → 10094 (claude) → 9983 (zsh) → 99114 (tmux)
```

Parent Claude Code PID was 10094. The subagent's chain passes through 10094 at
depth 2. A session file written to `sessions/10094.session` by the parent hook
would be found by `find_ancestor_session()` walking the subagent's chain. The
PPID mechanism is fully confirmed on macOS with Claude Code.
