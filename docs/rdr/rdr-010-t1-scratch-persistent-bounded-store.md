---
title: "T1 Scratch: Cross-Process Session Sharing via ChromaDB Server + PPID Chain"
id: RDR-010
type: architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
accepted_date: 2026-03-02
created: 2026-03-01
updated: 2026-03-02
related_issues:
  - RDR-008
---

## RDR-010: T1 Scratch: Cross-Process Session Sharing via ChromaDB Server + PPID Chain

## Summary

T1 scratch uses `chromadb.EphemeralClient` — purely in-process memory. When a
subagent is spawned via the Agent tool, two things break simultaneously:

1. **Storage**: The new process has no way to reach the parent's EphemeralClient.
   It is a private heap object with no networking and no on-disk representation.
   A new process cannot find it regardless of what key it holds.

2. **Key propagation**: The child's `SessionStart` hook generates a new UUID,
   so even if storage were shared, the child would be looking in the wrong
   namespace.

**Decision**: Solve both with the same mechanism.

- **Storage**: Replace `EphemeralClient` with `chromadb.HttpClient` connecting
  to a per-session ChromaDB server process. The server owns an in-memory
  collection; all agents connect to it over a local socket. Semantic search is
  fully preserved.
- **Key propagation**: Use the OS process hierarchy. The parent hook records
  the session ID and server address in `sessions/{pid}.session` keyed by the
  parent Claude Code process PID. Child agents walk the PPID chain via
  `ps -o ppid=` to find and adopt the nearest ancestor's server address,
  rather than starting their own.

This is the design originally intended for T1: a shared, semantically-searchable
scratch space for an entire agent session tree. No file-format changes, no loss
of embedding-based recall, no single-writer limitations.

## Motivation

1. **The core promise of T1 is broken.** `nx scratch search "[topic]"` is
   documented in every agent file as a way to recover in-session working notes.
   In practice it returns nothing because the data lives in a dead process.

2. **Semantic search is the point.** T1 is meant to let agents find earlier
   notes by meaning, not keyword. Replacing ChromaDB with SQLite + FTS5 to fix
   a process-boundary problem would trade away the primary value of the tier.

3. **The fix is architectural, not a rewrite.** The `T1Database` public
   interface (`put`, `get`, `search`, `flag`, `promote`, `clear`) does not
   change. Only the client changes from `EphemeralClient` to `HttpClient`.

4. **Per-session server lifetime.** The ChromaDB server process lives for the
   session lifetime and holds only the current session's data. No persistent
   files accumulate — the backing tmpdir is deleted on SessionEnd.

## Evidence Base

### Why EphemeralClient Cannot Be Shared

`chromadb.EphemeralClient()` is a purely in-process object. It has no HTTP
listener, no Unix socket, no file representation. There is no mechanism — API
or otherwise — by which a second OS process can connect to or read from an
existing `EphemeralClient`. The data lives in the creating process's heap and
is unreachable from outside.

### ChromaDB Client Modes

| Mode | Cross-process reads | Concurrent writes | Semantic search |
|------|--------------------|--------------------|-----------------|
| `EphemeralClient` | ✗ — in-process only | N/A | ✓ |
| `PersistentClient(path=...)` | Reads: yes; Writes: ✗ | Unsafe — segment files (parquet) are not concurrent-write safe across processes; WAL covers only the SQLite metadata layer | ✓ |
| `HttpClient(host, port)` | ✓ | ✓ — server serialises all writes | ✓ |

`PersistentClient` was considered and rejected: multiple parallel subagents
writing concurrently to the same segment files can corrupt them. ChromaDB's own
documentation treats `PersistentClient` as single-process. `HttpClient` is the
only mode that handles multi-process concurrent reads and writes correctly.

### Propagation Approaches Evaluated

Four mechanisms were evaluated in `tests/test_session_propagation_hypotheses.py`
(17 tests, all pass) and `tests/test_ppid_chain_hypothesis.py` (8 tests, all
pass, 0.22 s):

| Approach | Mechanism | Fatal flaw |
|----------|-----------|------------|
| **Env var** (`NX_SESSION_ID`) | Parent exports; child inherits | Claude Code runs each Bash invocation in a fresh subprocess; env vars are not reliably propagated to Agent-tool spawns; fails silently |
| **Handoff file + TTL** | Parent writes file; child atomically adopts via `rename()` | Single-adopter: parallel agent spawns compete for one file; exactly one wins, the rest get fresh sessions |
| **Sticky session** (mtime) | Child adopts `current_session` if younger than TTL | `current_session` is a global flat file; two concurrent Claude Code windows started within TTL share a session — the design the codebase already rejected |
| **PPID chain** | Child walks `ps -o ppid=` ancestors to find parent's session file | None — proven correct for 1- and 2-level nesting, sibling-isolated, handles arbitrary depth |

The PPID chain was chosen because:
- Concurrent independent sessions have disjoint OS process trees → no
  cross-contamination possible, no TTL race
- Works automatically for any agent nesting depth
- Requires no new hooks, no env var threading, no agent prompt changes
- `tests/test_ppid_chain_hypothesis.py::test_sibling_sessions_stay_isolated`
  proves concurrent windows cannot see each other's session files

## Proposed Solution

### Server Lifecycle

**SessionStart** (parent session):

1. Allocate a free localhost port via `socket.bind(('127.0.0.1', 0))` (Unix sockets are not supported by ChromaDB 1.5.1 — Finding 010-05). **Close the socket before launching the server** — the OS-assigned port is passed to `chroma run --port {port}` after the Python socket is released (TOCTOU window exists but is negligible on localhost).
2. Start a ChromaDB server process in the background:
   ```
   chroma run --host 127.0.0.1 --port {port} --path {tmpdir} --log-level ERROR
   ```
   The backing path is a per-session `tmpdir`; it exists only to satisfy the
   CLI — the data in it is ephemeral and cleaned up on SessionEnd.
3. If `chroma run` fails to start (binary not on PATH, port already taken): log a warning and fall back to `EphemeralClient` for local-only T1. Cross-process sharing is unavailable for this session; subsequent `nx scratch` commands work locally but subagents get fresh sessions. No error is raised.
4. Write a JSON session record to `~/.config/nexus/sessions/{ppid}.session`:
   ```json
   {"session_id": "{uuid4}", "server_host": "127.0.0.1", "server_port": {port},
    "server_pid": {pid}, "created_at": {unix_timestamp}, "tmpdir": "{path}"}
   ```
   The key is `os.getppid()` from within the `nx hook session-start` process —
   this is the Claude Code process PID, which IS in any child agent's PPID
   ancestry (see Finding 010-03).

**SessionEnd** (parent session — only the process that started the server stops it):

1. Check whether `sessions/{ppid}.session` exists (where `ppid = os.getppid()` of the hook script). If this file is present and readable, this process owns the session and is responsible for shutdown.
2. Send `SIGTERM` to the server process; wait briefly; `SIGKILL` if needed.
3. Delete the session record file and the backing tmpdir.
4. Flush flagged T1 entries to T2 and run T2 `expire()`.

**Child SessionEnd**: A child agent that adopted the parent's server must NOT stop it. `session_end()` in child processes detects that no `sessions/{ppid}.session` file belongs to them and skips the server-stop step, running only T1 flush + T2 `expire()`.

### Client Connection (all processes)

`T1Database.__init__` no longer creates an `EphemeralClient`. Instead:

```python
record = find_ancestor_session(sessions_dir)   # PPID chain walk
if record:
    client = chromadb.HttpClient(
        host=record["server_host"],
        port=record["server_port"],
    )
    session_id = record["session_id"]
else:
    # No ancestor session found — ps unavailable, restricted environment, or
    # server startup failed at SessionStart. Fall back to EphemeralClient.
    # Cross-process sharing is unavailable; T1 works locally for this process.
    import warnings
    warnings.warn("No T1 server found; falling back to local EphemeralClient", stacklevel=2)
    client = chromadb.EphemeralClient()
    session_id = str(uuid.uuid4())
```

`T1Database` is otherwise unchanged: same collection name (`scratch`), same
`session_id`-scoped metadata filtering, same `put`/`get`/`search`/`flag`/`clear`
interface.

### Session ID Propagation via PPID Chain

Before writing a new session record, `session_start()` calls
`find_ancestor_session()` to walk the PPID chain. If an ancestor record is
found, the session adopts it (connects to the existing server) rather than
starting a new one.

```
Parent Claude Code (PID 1000)
  └─ bash → nx hook session-start (PID 1100, PPID=1000)
       writes: sessions/1000.session = {session_id, server_host, server_port, server_pid, created_at, tmpdir}

  └─ [Agent tool] → Child Claude Code (PID 2000, PPID=1000)
       └─ bash → nx hook session-start (PID 2100, PPID=2000)
            walks PPID chain: 2100 → 2000 → 1000
            finds: sessions/1000.session
            connects: chromadb.HttpClient("127.0.0.1", 51823)
            does NOT start a new server
```

For parallel subagents (multiple children of the same parent): each child
independently walks the chain and finds the same server address. All connect
to the same server. The server handles concurrent writes.

### Session File Format

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "server_host": "127.0.0.1",
  "server_port": 51823,
  "server_pid": 9900,
  "created_at": 1740825600.0,
  "tmpdir": "/tmp/nx_t1_abc123"
}
```

`find_ancestor_session()` returns this dict (or `None`). Ownership is determined
structurally: if `sessions/{ppid}.session` (keyed by the hook's `os.getppid()`)
exists and is readable by `session_end()`, that process is the owner and is
responsible for stopping the server and deleting the file. Child processes find
the ancestor's file via the PPID chain but their own `{ppid}` key doesn't match,
so they skip the server-stop step.

The key change from the original `session.py` design: the session file now carries
a JSON payload rather than a bare UUID string. All existing callers that read a
bare UUID must be updated to parse JSON.

## Scope

### Files changed
- `src/nexus/session.py` — add `write_session_record()` writing JSON
  (`session_id`, `server_host`, `server_port`, `server_pid`, `created_at`,
  `tmpdir`); add `find_ancestor_session()` (PPID chain walk via `/proc/{pid}/status`
  then `ps`, JSON parse, 24h orphan sweep); add `start_t1_server()` /
  `stop_t1_server()` helpers; add `sweep_stale_sessions()` called from
  `session_start()` at startup
- `src/nexus/hooks.py` — `session_start()` acquires `fcntl.flock(LOCK_EX)` on
  `session.lock`, calls `sweep_stale_sessions()`, then `find_ancestor_session()`;
  if found adopts existing server; if not starts new server and writes JSON record
  via `write_session_record()`. `session_end()` checks whether
  `sessions/{ppid}.session` exists (ownership test) before calling
  `stop_t1_server()`; child sessions skip server stop and run T1 flush + T2 `expire()`
- `src/nexus/db/t1.py` — replace `EphemeralClient()` with PPID-chain lookup;
  fall back to `EphemeralClient()` with warning if no record found
- `src/nexus/commands/scratch.py` — update `_t1()` helper to use new session-aware
  `T1Database` constructor; remove direct `read_session_id()` UUID path
- `tests/test_t1.py` — update fixtures to mock `HttpClient`; add cross-process
  session-sharing test (start server in fixture, connect from subprocess)
- `tests/test_session.py` — update for JSON session file format; add PPID
  chain walk tests; add orphan sweep test
- `tests/test_hooks.py` — update fixtures that write bare UUID session files
  to write JSON records; update `test_session_end_with_session_file`

### Not in scope
- Changes to T2 or T3
- Changes to the `T1Database` public interface
- Semantic/embedding model changes
- Agent file prompt changes (propagation is automatic)

## Alternatives Considered

### SQLite + FTS5

Replace ChromaDB entirely with SQLite keyword search. Eliminates cross-process
storage concerns and concurrent-write risk. Rejected because it discards the
primary value of T1: semantic recall. "Authentication errors" does not
keyword-match "JWT middleware throws on missing claims." The whole point of the
scratch tier is embedding-based working memory, not keyword indexing. That is
what T2 already provides.

### ChromaDB PersistentClient

Swap `EphemeralClient` for `chromadb.PersistentClient(path=...)`, keyed by
session ID. Cross-process reads work; semantic search is preserved. Rejected
because concurrent multi-process writes to the same segment files are not safe.
ChromaDB's parquet-based segment storage is designed for single-process access.
With parallel subagents all writing to the same PersistentClient directory,
segment corruption is a real risk. `HttpClient` + server is the correct
concurrent-write architecture.

### Handoff File + TTL

Race-safe (POSIX `rename()`); single-adopter. Rejected because exactly one of
N parallel spawns adopts the file; the rest generate fresh sessions. For
parallel agents this produces fragmented scratch namespaces.

### NX_SESSION_ID Environment Variable

Simple. Rejected because Claude Code runs each Bash invocation in a fresh
subprocess. Env var propagation to Agent-tool spawns is not guaranteed and
fails silently — children generate fresh sessions with no error.

### Sticky current_session (mtime gate)

Zero new infrastructure. Rejected because `current_session` is a global flat
file per user; two concurrent Claude Code windows started within the TTL window
would share a session. Already rejected in the codebase for this reason.

## Trade-offs

### Positive
- Semantic search fully preserved — the original intent of T1
- True multi-process concurrent access via server serialisation
- Parallel agents all share one session automatically (all find the same server)
- Concurrent independent Claude Code windows stay isolated via disjoint process trees
- Agent-spawning-agent works transitively to any depth
- No agent prompt changes required — propagation is purely hook + process tree

### Negative
- Server process per session: adds a ChromaDB process for the session lifetime.
  Overhead is modest (ChromaDB starts in ~0.5–1 s), but `SessionEnd` must kill
  it reliably to avoid orphan processes.
- Port allocation: must find a free localhost port at startup. Risk of collision
  is low with random port selection but non-zero. Unix sockets are not supported
  by ChromaDB 1.5.1 (Finding 010-05) so TCP is the only option.
- `chroma` CLI must be on PATH, or the programmatic server API must be used.
  ChromaDB is already a dependency (`pyproject.toml`); the server is bundled.

### Risks and Mitigations
- **Risk**: Server process orphaned if `SessionEnd` hook does not fire (e.g.
  Claude Code killed). **Mitigation**: A `sweep_stale_sessions()` function is
  called from `session_start()` at startup. It scans `sessions/` for JSON records
  older than 24h, sends `SIGTERM` to each `server_pid` (ignoring errors if already
  dead), and deletes the stale record and its backing tmpdir. This sweep is
  separate from `find_ancestor_session()` and runs unconditionally at startup.
- **Risk**: Port collision. **Mitigation**: Allocate via
  `socket.bind(('127.0.0.1', 0))`, read back the OS-assigned port, then close
  the socket before launching `chroma run --port {port}`. The TOCTOU window
  between socket close and chroma bind is negligible on localhost. Unix sockets
  are not supported by ChromaDB 1.5.1 (Finding 010-05).
- **Risk**: `ps -o ppid=` unavailable in a restricted container, or PPID chain
  topology does not match assumption (Agent-tool spawns are not direct OS
  descendants). **Mitigation**: `find_ancestor_session()` returns `None`; T1
  falls back to a local `EphemeralClient` with a warning. Degradation is graceful
  and silent — T1 functions locally, subagents get isolated sessions.

## Implementation Plan

1. Add `find_ancestor_session(sessions_dir, start_pid)` to `session.py` — PPID
   chain walk, JSON parse, returns `dict | None`
2. Add `sweep_stale_sessions(sessions_dir)` to `session.py` — scans for JSON
   records older than 24h, SIGTERMs each `server_pid`, deletes stale files
3. Add `write_session_record()` to write JSON payload (session_id, server_host,
   server_port, server_pid, created_at, tmpdir) — no `started_by` field; ownership
   is determined by PPID-keyed file existence, not a PID embedded in the record
4. Add `start_t1_server() -> (host, port, server_pid)` helper — allocates port
   via `socket.bind`, closes socket, launches `chroma run`; returns on success or
   raises on failure (caller handles fallback)
5. Add `stop_t1_server(server_pid)` helper — SIGTERM + SIGKILL fallback
6. Update `hooks.py::session_start()`: call `sweep_stale_sessions()`, then
   `find_ancestor_session()`; if found adopt; if not call `start_t1_server()`,
   handle startup failure by writing no session record (fallback path)
7. Update `hooks.py::session_end()`: check if `sessions/{ppid}.session` exists
   (where ppid = os.getppid()); if found this process owns the session — call
   `stop_t1_server()`, delete session file and tmpdir; child sessions find no
   matching file and skip server stop; all sessions flush flagged T1 entries then
   run T2 `expire()`
8. Update `T1Database.__init__`: call `find_ancestor_session()`; on success build
   `HttpClient`; on `None` fall back to `EphemeralClient` with warning
9. Update `commands/scratch.py::_t1()`: remove direct `read_session_id()` UUID
   path; use new session-aware `T1Database` constructor
10. Update tests: `test_hooks.py` fixtures (JSON record format, bare-UUID tests),
    `test_session.py` (JSON format, PPID chain, orphan sweep), `test_t1.py`
    (mock HttpClient, subprocess cross-process test)

## Research Findings

### Finding 010-01: Three propagation mechanisms tested and rejected

`tests/test_session_propagation_hypotheses.py` (17 tests). Env var: silent
failure on Agent-tool spawns. Handoff file: single-adopter, wrong for parallel
agents. Sticky session: global flat file collides across concurrent windows.
See Alternatives Considered for detail.

### Finding 010-02: PPID chain walk proven correct

`tests/test_ppid_chain_hypothesis.py` (8 tests, 0.22 s). Direct parent, 2-level
grandparent, nearest-ancestor semantics, sibling isolation, graceful termination
— all verified. `ps -o ppid=` is POSIX standard; works on macOS and Linux.

### Finding 010-03: Session file write key must be PPID, not PID

The session file is keyed by `os.getppid()` from within `nx hook session-start`
— this is the Claude Code process PID. The hook's own PID (`os.getpid()`) is a
sibling of any spawned agent, not an ancestor, and will never appear in a child's
PPID chain. The Claude Code PID (`os.getppid()`) is in the chain because agents
are direct children of their parent Claude Code process.

### Finding 010-04: ChromaDB client mode comparison

`EphemeralClient` — no cross-process access, period. `PersistentClient` — reads
across processes work; concurrent multi-process writes are unsafe (parquet segment
files). `HttpClient` — fully concurrent multi-process reads and writes via server
serialisation. Only `HttpClient` meets the requirements for parallel subagents.

## Open Questions

- **PPID topology empirical verification**: The PPID chain mechanism is proven at
  the OS level (Finding 010-02). Whether Claude Code's Agent tool spawns child
  processes as direct OS descendants has not been empirically confirmed via `ps`
  from inside a running subagent. If Claude Code uses a worker pool or container
  isolation that breaks the chain, the fallback to EphemeralClient activates. A
  future integration test from inside a live Agent-tool spawn would close this.

## Closed Questions

- **Unix socket vs TCP** (closed — Finding 010-05): Unix sockets are **not
  supported**. `HttpClient` accepts only `host` (str) and `port` (int). The
  `Settings` class has no UDS path field, and a full source search finds zero
  references to Unix sockets in the ChromaDB 1.5.1 package. TCP localhost with
  a randomly allocated port is the only option.

- **Programmatic server start via `chromadb.server.fastapi`** (closed — Finding
  010-05): Not viable. In ChromaDB 1.5.1, `EphemeralClient` and
  `PersistentClient` have moved to a Rust binary backend
  (`chromadb.api.rust.RustBindingsAPI`). The `chroma` CLI itself is now a Rust
  binary (`chromadb_rust_bindings.cli`). The Python `chromadb.server.fastapi`
  module is the old architecture, is not installed without the `chromadb[server]`
  optional extra, and is being phased out. The correct approach is to start the
  server via subprocess `chroma run` — the stable public interface. Port
  allocation uses `socket.bind(('127.0.0.1', 0))` to find a free port before
  launching the server.
