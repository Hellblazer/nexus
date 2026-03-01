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

## PPID Topology: Empirically Confirmed (2026-03-01)

Spawned a subagent via the Agent tool and traced its full PPID chain:

```
37930 (python subprocess) → 37928 (zsh) → 10094 (claude) → 9983 (zsh) → 99114 (tmux)
```

Parent Claude Code PID was 10094. The subagent's chain passes through 10094 at
depth 2. A session file written to `sessions/10094.session` by the parent hook
would be found by `find_ancestor_session()` walking the subagent's chain. The
PPID mechanism is fully confirmed on macOS with Claude Code.
