---
title: "RDR-094: MCP-Owned T1 Chroma Lifecycle"
id: RDR-094
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-24
accepted_date:
related_issues: []
related_tests: [test_session.py, test_hooks.py, test_mcp_server.py, test_session_end_launcher.py]
related: [RDR-010, RDR-034, RDR-062, RDR-078]
---

# RDR-094: MCP-Owned T1 Chroma Lifecycle

The T1 ChromaDB HTTP server that backs per-session scratch is spawned
by a `SessionStart` hook, cleaned up by a `SessionEnd` hook, policed
by an external watchdog sidecar, and swept by a liveness-plus-UUID-
mismatch reaper on subsequent startups. The stack grew over three
consecutive 4.10.x / 4.11.x releases as we discovered each new hole
in the hook-based lifecycle: graceful exit needs the hook, ungraceful
exit needs the watchdog, UUID rollover needs the sweep, cold-start
timing needs the fork-first launcher. Each layer addresses a case
the layer above cannot, and the result is a five-piece machine
(hook + detach launcher + watchdog + sweep + tmpdir) that exists
only because the process tree we're trying to couple to, the
Claude Code session itself, isn't directly addressable.

The MCP server subprocess (`nx-mcp`, spawned by Claude Code via
stdio) **is** directly addressable. Claude Code owns its lifetime,
negotiates shutdown over the MCP protocol, and waits for it to exit
before finishing session teardown. Moving chroma ownership into the
MCP server collapses most of the current machinery: `atexit` +
`SIGTERM` handlers in the server kill chroma when Claude Code kills
the server, with no hook timing race, no UUID rollover gap, and no
orphan tmpdir case because the process that created the tmpdir is
the one that cleans it up.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Hook-based chroma cleanup has irreducible race against Claude Code shutdown

The `SessionEnd` hook owns graceful chroma cleanup. Claude Code
delivers SIGTERM to the hook's process group during its own
shutdown; if the hook subprocess has not reached its cleanup
logic before SIGTERM arrives, the cleanup does not run. The
4.10.3 `nx hook session-end-detach` double-forks into a
daemonised grandchild that survives the SIGTERM, but the fork
itself runs only after Click parses argv and after
`nexus.*` imports complete, ~2 seconds of cold-start cost on a
reference install. The 4.11.1 `nx-session-end-launcher` console
script (nexus-2u7o) shrinks this to ~256ms by forking before any
`nexus.*` import, but the race is still a race, a sufficiently
aggressive Claude Code shutdown on a contested host would still
cancel it. The fix is stacking mitigations around a fundamentally
misaligned lifecycle anchor rather than changing the anchor.

**Impact**: every Claude Code session pays the cost of a layered
defense whose only job is to simulate the lifecycle coupling the
MCP server already has with Claude Code natively. Every new
corner-case (UUID rollover within a live claude, watchdog
process-group SIGKILL, cold-start cache miss) has to be
rediscovered and patched separately.

#### Gap 2: Session-UUID rollover within a live Claude process required a dedicated sweep arm

When `/clear` or `/resume` rolls the conversation UUID without
killing Claude Code, the existing session record's `server_pid`
and `claude_root_pid` both remain alive, the watchdog sees the
claude parent alive and does not fire; the `server_dead` and
`anchor_dead` sweep triggers are both satisfied (as in,
not-dead); and the orphan chroma would stay alive until the
24-hour age sweep catches it. The 4.11.0 `uuid_mismatch` sweep
arm (nexus-886w) addressed this by comparing the record's
`session_id` against the `current_session` flat-file pointer,
reaping when they disagree. The fix is correct but shouldn't be
necessary, if the MCP server owns chroma and Claude Code
restarts the MCP server on UUID rollover, the old MCP subprocess's
`atexit` kills the old chroma before the new subprocess writes a
new record. No intermediate-state sweep required.

**Impact**: an entire reap trigger exists because the hook/daemon
pair cannot observe UUID rollover in the parent Claude Code process.

#### Gap 3: Orphan tmpdirs without session records have no cleanup path

The 4.11.0 sweep operates on session **records**. A tmpdir whose
record was deleted but whose chroma server crashed before the
cleanup path completed leaves an orphan directory with a
`chroma.sqlite3` file inside and no record pointing at it. Audit
on this install 2026-04-24: 10 such tmpdirs from 2026-04-23 ~19:55,
total ~900KB of stable cruft. Every one of them is unreachable
through any sweep trigger. If the MCP server owns both the tmpdir
creation and the tmpdir removal as paired operations in the same
process, there is no intermediate state where the tmpdir exists
without an owner, the process is the owner, and its `atexit`
removes the directory.

**Impact**: bounded but monotonic disk-cruft accumulation, and a
known hole in the defense-in-depth story documented but not fixed
in the nexus-886w analysis.

## Context

### Background

RDR-010 (Closed, 2026-03-01) established the T1 per-session
ChromaDB server pattern: one HTTP server per Claude conversation,
started by `SessionStart`, reachable by subagents via the PPID
chain. RDR-034 (Closed, 2026-03-11) introduced the MCP server
process as the storage interface. RDR-062 (Closed, 2026-03-14)
split the MCP server into `nx-mcp` (core) and `nx-mcp-catalog`
(catalog), these are the subprocesses Claude Code spawns via
stdio.

The lifecycle machinery layered on top of RDR-010:

- **RDR-010 original**: `SessionStart` hook spawns chroma +
  writes a PID-keyed session file; `SessionEnd` hook stops it.
- **nexus-99jb (4.10.3)**: hook race discovered in production;
  added `t1_watchdog` sidecar + `session-end-detach` double-fork
  + liveness-based `sweep_stale_sessions`. Three independent
  defense layers.
- **nexus-886w (4.11.0)**: `uuid_mismatch` arm added to sweep
  after discovering `/clear`-triggered orphans on live Claude
  processes.
- **nexus-2u7o (4.11.1)**: fork-first `nx-session-end-launcher`
  console script replaces `nx hook session-end-detach` in
  hooks.json to bypass the 2s cold-start race.

Five pieces in the cleanup path today: `SessionEnd` hook →
`session-end-detach` fallback → `nx-session-end-launcher` primary
→ `t1_watchdog` out-of-band → `sweep_stale_sessions` on next
SessionStart. The MCP server is cleanly shut down by Claude Code
independently of all five.

### Technical Environment

- `src/nexus/mcp/core.py`, `nx-mcp` server entry point (`main()`,
  FastMCP instance at module scope). Long-lived subprocess owned by
  Claude Code via stdio.
- `src/nexus/mcp/catalog.py`, `nx-mcp-catalog` server. Separate
  process; typically doesn't need T1.
- `src/nexus/session.py`, `start_t1_server()`, `stop_t1_server()`,
  `sweep_stale_sessions()`, `find_claude_root_pid()`, session
  record I/O.
- `src/nexus/hooks.py`, `session_start()`, `session_end()`. Called
  by the hook subcommands.
- `src/nexus/commands/hook.py`, `nx hook session-start`, `nx hook
  session-end`, `nx hook session-end-detach` subcommands. The
  SessionStart hook runs synchronously; SessionEnd fires via the
  launcher.
- `src/nexus/_session_end_launcher.py`, 4.11.1 fork-first console
  script. Minimal imports at top level.
- `src/nexus/t1_watchdog.py`, 4.10.3 sidecar. Watches
  `--claude-pid` + `--chroma-pid`, kills chroma when claude dies.
- `src/nexus/db/t1.py`, T1 ChromaDB client. Resolves session by
  `NX_SESSION_ID` env / `current_session` flat file / PPID walk.
- `nx/hooks/hooks.json`, SessionStart + SessionEnd wired to the
  corresponding `nx hook ...` commands.

## Research Findings

### Investigation

Claude Code's MCP-subprocess lifecycle observed through plugin
use: the server is spawned on session start and killed before
Claude Code finishes exiting, with stdio handshake for both
boundaries. No "Hook cancelled" analog exists for MCP servers -
Claude Code blocks on the server's close before completing
session teardown. Empirically observed across 4.10.x / 4.11.x
sessions: MCP server deaths correlate exactly with Claude Code
deaths; orphaned MCP subprocesses have not been observed in any
deployment.

Cross-referenced against RDR-010's original design rationale: the
PID-keyed session record existed because RDR-010 predated the MCP
server being the session-anchoring subprocess. At the time the T1
chroma had no parent process it could confidently be bound to
except the terminal's shell (PPID). The MCP server didn't yet
exist as a stable per-session subprocess to inherit from.

### Key Discoveries

- **Verified** (`src/nexus/mcp/core.py`): the MCP server is
  FastMCP-based, spawned via the `nx-mcp` console script entry
  point. It stays alive for the duration of the session; Claude
  Code manages its stdio. Python's `atexit` and `signal` handlers
  fire during clean shutdown.
- **Verified** (RDR-062 split): `nx-mcp` and `nx-mcp-catalog` are
  independent processes. Only `nx-mcp` touches T1. Chroma ownership
  belongs in `nx-mcp`, not in the catalog server.
- **Verified** (`src/nexus/db/t1.py`): the T1 client already has
  the ancestor-detection logic needed for subagent connection
  (NX_SESSION_ID env + current_session pointer + PPID walk). None
  of that has to change, only the SPAWN side moves.
- **Assumed**: Claude Code sends SIGTERM (not SIGKILL) to MCP
  servers on clean shutdown, giving Python time to run `atexit`.
  **Status**: Unverified, **Method**: instrumented spike on a
  local Claude Code install; run 10 graceful close cycles, 10
  `kill -9 claude` cycles, 10 OOM simulations; measure fraction of
  runs where atexit-based chroma stop reached completion.
- **Assumed**: MCP server crash/restart within a live Claude
  session is rare in practice. **Status**: Unverified -
  **Method**: Layer 3 watchdog continues to exist for this case;
  the spike above will also measure MCP-restart frequency.

### Critical Assumptions

- [x] Claude Code's MCP-server shutdown ordering delivers SIGTERM
      with enough time for cleanup. **Status**: VERIFIED 2026-04-25
      via Spike A. **Evidence**: 10/10 SIGTERM cycles cleaned
      chroma within ~11s, attributed to `mcp_owned_signal` path.
      T2 nexus_rdr/094-spike-a-lifecycle (id=976), T3
      knowledge__nexus 9f1c77b683a9f429.
- [ ] Moving chroma spawn from the SessionStart hook to MCP-server
      startup does not regress subagent T1 sharing. **Status**:
      Unverified. **Method**: integration test dispatching a subagent
      chain and asserting scratch read/write across the tree.
      Note: not exercised by Spike A; subagent path is structurally
      identical to the top-level path through the `nested=True`
      short-circuit and the existing `find_ancestor_session` walk.
      Worth a focused integration test before Phase 4 flag removal.
- [x] The dual-watch watchdog (`--mcp-pid` + `--claude-pid` with
      OR-trigger logic) preserves coverage of BOTH the "MCP dies
      without atexit firing" case AND the "Claude Code crashes and
      orphans the MCP server" case (Claude Code issue #1935).
      **Status**: VERIFIED 2026-04-25 via Spike A. **Evidence**:
      10/10 mcp_sigkill runs cleaned chroma via watchdog (mcp-pid
      trigger) within ~10s; 10/10 mcp_oom (SIGSEGV) same. 10/10
      claude_crash runs cleaned via watchdog (claude-pid trigger)
      within ~10s. Path attributions: `watchdog_mcp` and
      `watchdog_claude` respectively.
- [ ] Claude Code issue #40207's mid-session SIGTERM (10-60s after
      successful connection) does NOT apply to vanilla stdio servers
      like nx-mcp, OR if it does, the FM-NEW-2 mitigation (TCP-probe
      reuse of an existing session record) prevents the 1-5s T1 gap.
      **Status**: Unverified (Spike C scope). **Method**: run
      `scripts/spikes/spike_rdr094_mid_session_observer.py
      --duration-min 30` alongside a real Claude Code session
      with `NEXUS_MCP_OWNS_T1=1`; observer tails mcp.log for
      mid-session SIGTERM events and reports `no_mid_session_sigterm`
      / `issue_40207_confirmed` / `inconclusive`. Pending operator
      scheduling. Spike A covered CA-1 + CA-3 only.

### Empirical primary-path-by-transport finding

Spike A surfaced one design clarification (no design change): on
macOS stdio transport, FastMCP's lifespan async finally is NOT the
primary cleanup path under SIGTERM, despite the original RDR
assumption. anyio does not install a SIGTERM handler that
propagates cancellation through the lifespan on stdio. The
`signal.signal(SIGTERM, _sigterm_handler)` registration in
`main()` IS the path that runs (10/10 attributions to
`mcp_owned_signal`).

The lifespan path remains primary on HTTP / SSE transports where
anyio installs the handler (per python-sdk #514). Both code
locations call the same idempotent `_t1_chroma_shutdown`, so
correctness is unaffected; only the documentation's primary /
secondary nomenclature changes. §Approach and §Technical Design
above reflect the empirical behaviour.

## Research Appendix (2026-04-24)

Multi-source research synthesis addressing six questions for gate
evidence. Sources: MCP spec, Python runtime docs, Claude Code
GitHub issues, FastMCP GitHub issues, OpenAI Codex GitHub issues,
nexus source analysis.

### RQ1: Sidecar Subprocess Prior Art in MCP Servers

No production MCP server implementation was found that spawns a
separate HTTP server sidecar subprocess from within the MCP server
process. Community examples (mcp-toolbox, python-sdk lifespan
examples, kanban-mcp) use connection pools or embedded databases
cleaned up via async context manager finally blocks. The nexus
ChromaDB sidecar pattern is architecturally novel.

Prior art for the inverse problem (MCP server orphaned by client
crash) is documented in openai/codex issues #16256 and #18881.
The Codex McpConnectionManager lacked a Drop implementation; 492
orphaned child processes accumulated over 15 hours, each consuming
approximately 35% CPU. The failure mode, client dies without
cleaning up server, server dies without cleaning up its children
- is confirmed real in production MCP deployments.

### RQ2: Python atexit and Signal Handler Patterns

Python documentation is explicit: atexit handlers are NOT called
"when the program is killed by a signal not handled by Python."
SIGTERM is unhandled by default. The canonical bridge:

```
signal.signal(SIGTERM, lambda s, f: sys.exit(0))
atexit.register(cleanup)
```

For FastMCP servers (anyio-backed), the lifespan context manager
is the more robust pattern. anyio installs its own SIGTERM handler
that cancels the running task group. Async cancellation propagates
through `async finally` blocks, making the lifespan `finally`
block fire on SIGTERM without a manual signal handler. FastMCP
supports `lifespan=` in the constructor (same API as FastAPI).

Known FastMCP signal handling bugs: issue modelcontextprotocol
python-sdk #514 (SSE transport hangs after processing one request);
PrefectHQ fastmcp #2837 (stdio transport requires multiple Ctrl-C
for clean exit). These confirm the SIGTERM-to-cleanup path is
fragile and the watchdog backstop is not optional.

**Implementation recommendation**: use the FastMCP lifespan
context manager as the primary cleanup path, not raw atexit +
signal handler. The `signal.signal(SIGTERM, ...)` handler in the
proposed design is still correct as insurance, but the lifespan
`finally` block should own the cleanup call so anyio's cancellation
is the authoritative path.

### RQ3: Claude Code MCP Client Shutdown Semantics

MCP specification 2025-03-26 (authoritative), stdio shutdown:

1. Client closes the input stream (stdin EOF) to the server.
2. Wait for the server to exit.
3. Send SIGTERM if the server does not exit within a "reasonable
   time" (no specific timeout defined).
4. Send SIGKILL if no exit within "reasonable time" after SIGTERM.

No shutdown RPC exists. "Reasonable time" is unspecified.

Claude Code's observed sequence (issue #7718 strace evidence):

1. SIGINT (not in spec).
2. SIGTERM (if SIGINT fails).
3. SIGKILL (if SIGTERM fails).
4. "Cleanup timeout reached" + SIGABRT on Claude Code itself.

**Critical finding, issue #40207**: Claude Code has an internal
mid-session timeout that sends SIGTERM to healthy stdio MCP servers
10-60 seconds after successful connection. The timeout shrinks over
session lifetime: 60s, 30s, 10s. All MCP servers killed at the
same second in evidence. Note: issue #40207 involves a proxy
wrapper (mcp-stdio-proxy.sh); whether this applies to vanilla
stdio servers like nx-mcp requires the Critical Assumption spike
to verify. If it applies, chroma would restart multiple times
within a single Claude session.

**Critical finding, issue #1935**: Claude Code does NOT reliably
kill its MCP server children when Claude Code itself crashes.
Users documented 40+ orphaned MCP server processes (Docker
containers, Python processes including chroma-mcp, Node.js servers)
dating back days. This challenges the stated assumption "Claude
Code owns its lifetime, negotiates shutdown, and waits for it to
exit." The assumption holds for graceful exit; it does not hold
for crash.

### RQ4: Three-Level Process Chain (A owns B owns C)

For Claude Code (A) to nx-mcp (B) to chroma (C) on macOS:

`prctl(PR_SET_PDEATHSIG)`: Linux-only. macOS has no equivalent.
Cannot use for macOS-first deployment.

Process group: chroma in nx-mcp's pgroup would receive pgroup
signals. Incompatible with `start_new_session=True` isolation that
is load-bearing for POSIX semaphore cleanup (nexus-dc57/ze2a).

Polling watchdog: correct architecture for macOS. No OS-specific
primitives. 5s poll acceptable. The watchdog's own
`start_new_session=True` ensures it survives nx-mcp pgroup kill.

Conclusion: the polling watchdog is the right pattern. No change
to the fundamental watchdog design is needed.

### RQ5: Failure Mode Enumeration

Failure modes solved by MCP ownership (confirmed):

- Hook race vs shutdown: eliminated. MCP server exit is the anchor.
- UUID rollover orphan: eliminated. Old MCP atexit fires before
  the new MCP server starts, so no intermediate-state orphan is
  possible.
- Orphan tmpdir gap: eliminated. Same process creates and removes
  the tmpdir; no state where tmpdir exists without an owner.

New failure modes introduced by MCP ownership:

**FM-NEW-1: MCP server orphaned by Claude Code crash (HIGH)**

Issue #1935 documents Claude Code regularly leaving MCP servers
alive after crash. In the current design, the watchdog watches
`claude_root_pid`: Claude dies => watchdog fires within 5s.
In the proposed design, the watchdog watches `mcp_pid`: Claude dies
and MCP server survives as an orphan => watchdog sees mcp_pid alive
and never fires. Chroma stays alive until the 24h sweep.

This is a regression relative to the current watchdog behavior
for the Claude-crash failure mode.

MITIGATION REQUIRED before gate: the watchdog must watch BOTH
`mcp_pid` (fires when MCP dies without atexit) AND `claude_root_pid`
(fires when Claude dies while MCP server is still alive and thus
orphaned). The session record already stores `claude_root_pid`; no
schema change is needed. The watchdog argument interface needs to
accept both PIDs and implement OR-trigger logic.

**FM-NEW-2: Mid-session MCP restart creates T1 gap (LOW-MEDIUM)**

If the #40207 mid-session timeout applies to nx-mcp, each timeout
fires the atexit (chroma stops), then the restarted MCP server
spawns a fresh chroma. The gap is estimated at 1-5 seconds during
which T1 falls back to EphemeralClient. Hook-based chroma is
immune because chroma lives independently of the MCP lifecycle.

Mitigation: at MCP startup, TCP-probe the address in any existing
session record. If reachable, reuse it rather than spawning a new
chroma. This eliminates the gap for mid-session restart cases.

Retained failure modes (behavior identical in both designs):

- MCP SIGKILL without atexit: watchdog detects mcp_pid gone, cleans
  up chroma within 5s.
- Watchdog also killed: sweep server_dead trigger on next
  SessionStart.

### RQ6: Crash-Restart Semantics

On nx-mcp crash (SIGSEGV, OOM, unhandled exception):

- atexit and lifespan finally block do not fire (fatal signal).
- Watchdog (watching mcp_pid) detects mcp_pid gone within 5s.
- Watchdog runs in its own session (start_new_session=True) and
  survives pgroup kill of nx-mcp.
- Fallback if watchdog also killed: sweep server_dead on next
  SessionStart.

On Claude Code crash where nx-mcp is orphaned (FM-NEW-1):

- A watchdog watching mcp_pid only sees mcp_pid alive, never fires.
- Sweep anchor_dead trigger (claude_root_pid in session record)
  fires on next SessionStart. This coverage already exists.
- The regression is 5s watchdog detection becoming SessionStart-only
  detection. This is acceptable if FM-NEW-1 mitigation is added.

### Summary for Gate Decision

**Overall recommendation: PROCEED with modifications.**

The core premise is sound. The MCP server is a better lifecycle
anchor than hooks. The three documented gaps are genuinely
eliminated. The complexity reduction (five-piece to three-piece)
is real.

One required design change before gate accept:

- Watchdog dual-watch: pass both `--mcp-pid` and `--claude-pid`
  to the watchdog. The watchdog fires if mcp_pid dies while chroma
  is alive (atexit didn't run) AND signals the MCP server to exit
  if claude_root_pid dies while mcp_pid is alive (orphaned MCP
  server). The existing session record already stores both PIDs;
  the watchdog argument interface is the only change.

One recommended improvement for implementation (not a gate blocker):

- Use the FastMCP lifespan context manager as the primary cleanup
  path rather than raw atexit + manual signal handler. anyio's
  cancellation propagation through async finally blocks is more
  reliable for FastMCP than the atexit chain.

The Critical Assumption spike (30-run lifecycle probe) remains the
essential gate prerequisite.

### Research Sources

- MCP spec 2025-03-26 lifecycle:
  https://modelcontextprotocol.io/specification/2025-03-26/basic/lifecycle
- Python atexit docs:
  https://docs.python.org/3/library/atexit.html
- Claude Code issue #40207 (mid-session SIGTERM to MCP servers):
  https://github.com/anthropics/claude-code/issues/40207
- Claude Code issue #1935 (orphaned MCP server processes):
  https://github.com/anthropics/claude-code/issues/1935
- Claude Code issue #7718 (SIGABRT on shutdown):
  https://github.com/anthropics/claude-code/issues/7718
- Claude Code issue #37127 (TaskStop SIGTERM/SIGKILL):
  https://github.com/anthropics/claude-code/issues/37127
- FastMCP / python-sdk issue #514 (SSE signal handling):
  https://github.com/modelcontextprotocol/python-sdk/issues/514
- FastMCP issue #2837 (stdio Ctrl-C):
  https://github.com/PrefectHQ/fastmcp/issues/2837
- OpenAI Codex issue #16256 (MCP orphaned processes):
  https://github.com/openai/codex/issues/16256
- OpenAI Codex issue #18881 (McpConnectionManager leak):
  https://github.com/openai/codex/issues/18881

## Proposed Solution

### Approach

Move chroma spawn to `nx-mcp` server startup. Cleanup runs through
three paths, with the primary path differing by transport:

- **stdio transport (the deployed path on macOS)**: `signal.signal
  (SIGTERM, _sigterm_handler)` registered in `core.py:main()` is
  the primary cleanup path. anyio does NOT install a SIGTERM
  handler under FastMCP stdio (Spike A 2026-04-25 evidence,
  T2 nexus_rdr/094-spike-a-lifecycle id=976), so the FastMCP
  lifespan async finally never fires from a signal alone. The
  explicit signal handler calls `_t1_chroma_shutdown` directly.
- **HTTP / SSE transports**: the FastMCP `lifespan` context manager
  is the primary path. anyio does install a SIGTERM handler there
  (research RQ2 + python-sdk #514 reference), and cancellation
  propagates through the `async finally`. The signal handler is
  redundant on those transports but harmless.
- **Always belt-and-braces**: `atexit.register(_t1_chroma_shutdown)`
  covers clean stdin EOF and SystemExit paths that arrive without
  going through a signal. `_t1_chroma_shutdown` is idempotent so
  any combination of paths firing is safe.

The empirical primary-path-by-transport split was a Spike A
finding, not the original design assumption. The pre-Spike design
called the lifespan path primary and the signal handler belt-and-
braces; that ordering is correct for HTTP/SSE but inverted for
stdio. Cleanup correctness was unaffected (idempotent shutdown)
but the documentation now reflects the empirical behaviour.

Pivot the `t1_watchdog` sidecar to watch BOTH `--mcp-pid` AND
`--claude-pid` with OR-trigger logic. This is required to cover
the FM-NEW-1 failure mode (Claude Code issue #1935: Claude Code
does not reliably clean up MCP server children on crash, leaving
the MCP server orphaned and chroma alive). A watchdog watching
only `--mcp-pid` would never fire when Claude Code crashes and
the MCP server survives. The session record already stores both
PIDs; only the watchdog argument interface and polling loop change.

Keep the `sweep_stale_sessions` liveness sweep as final belt-and-
braces for the combined-failure scenario (MCP SIGKILL + watchdog
also killed). Retire the `SessionStart` and `SessionEnd` hooks'
chroma-management responsibilities; keep the hooks for scratch-
flush and memory-expire which remain hook-native tasks.

### Technical Design

**`nx-mcp` server lifecycle** (`src/nexus/mcp/core.py`):

Three cleanup paths run, all calling the same idempotent
`_t1_chroma_shutdown`:

1. `signal.signal(SIGTERM, _sigterm_handler)` and SIGINT registered
   in `main()`. **Primary on stdio transport** (Spike A 2026-04-25
   evidence, 10/10 SIGTERM cycles attributed to `mcp_owned_signal`
   path). On HTTP / SSE transports anyio's own SIGTERM handler
   beats this and cancels the task group via the lifespan instead;
   the explicit handler is redundant there but harmless.
2. `lifespan=_t1_chroma_lifespan` async context manager passed to
   `FastMCP(...)`. **Primary on HTTP / SSE transports** via anyio
   cancellation through `async finally`. Skipped on stdio because
   anyio does not install a SIGTERM handler there.
3. `atexit.register(_t1_chroma_shutdown)`. Belt-and-braces for
   clean stdin EOF and SystemExit paths that arrive without going
   through a signal. Always registered.

```text
@asynccontextmanager
async def _t1_chroma_lifespan(_app):
    _t1_chroma_init_if_owner()
    try:
        yield
    finally:
        _t1_chroma_shutdown()      # PRIMARY cleanup path

mcp = FastMCP("nexus", lifespan=_t1_chroma_lifespan)

main():
    configure_logging("mcp")
    atexit.register(_t1_chroma_shutdown)              # SECONDARY
    signal.signal(SIGTERM, _sigterm_handler)          # SECONDARY
    signal.signal(SIGINT, _sigterm_handler)           # SECONDARY
    log.info("mcp_server_starting", server="nx-mcp")
    try:
        mcp.run(transport="stdio")
    except BaseException as exc:
        log.exception("mcp_server_crashed", error=str(exc))
        raise
    finally:
        log.info("mcp_server_stopping")

_t1_chroma_init_if_owner():
    # Subagent detection, same logic t1.py already has.
    if os.environ.get("NX_SESSION_ID") and _find_ancestor_session():
        return   # Nested MCP server; connect to ancestor, don't spawn.
    # TCP-probe any existing session record for the same session_id
    # (FM-NEW-2 mitigation): if the address is reachable, reuse it
    # rather than spawning fresh. This eliminates the 1-5s T1 gap
    # that issue #40207's mid-session SIGTERM-restart cycle would
    # otherwise create.
    own_id = _resolve_own_session_id()
    existing = read_session_record_by_id(SESSIONS_DIR, own_id)
    if existing and _tcp_probe_alive(existing["host"], existing["port"]):
        _OWNED_CHROMA.update({"reused": True})
        return
    # Top-level MCP server, own the chroma lifecycle.
    host, port, pid, tmpdir = start_t1_server()
    _OWNED_CHROMA.update({"pid": pid, "tmpdir": tmpdir})
    write_session_record_by_id(
        SESSIONS_DIR, session_id=own_id,
        host=host, port=port, server_pid=pid,
        claude_root_pid=find_claude_root_pid(),
        watchdog_pid=spawn_t1_watchdog(
            mcp_pid=os.getpid(),
            claude_pid=find_claude_root_pid(),    # FM-NEW-1 dual-watch
            chroma_pid=pid,
        ),
    )

_t1_chroma_shutdown():
    if not _OWNED_CHROMA or _OWNED_CHROMA.get("reused"):
        return                                          # idempotent
    stop_t1_server(_OWNED_CHROMA["pid"])
    shutil.rmtree(_OWNED_CHROMA["tmpdir"], ignore_errors=True)
    _remove_own_session_record()
    _OWNED_CHROMA.clear()                               # idempotent
```

`_t1_chroma_shutdown` is idempotent so the lifespan finally block,
the atexit handler, and the SIGTERM handler can each call it
safely; the first to fire performs the work, the rest are no-ops.

**Watchdog dual-watch** (`src/nexus/t1_watchdog.py`):

The watchdog accepts `--mcp-pid` AND keeps `--claude-pid` (research
FM-NEW-1, Claude Code issue #1935: Claude Code does not reliably
clean up MCP children on crash). Polling loop now uses OR-trigger
logic:

```text
while True:
    if not _alive(mcp_pid):
        # MCP server died without atexit firing (SIGKILL, segfault).
        _stop_chroma_pgrp()
        break
    if not _alive(claude_pid):
        # Claude Code died and orphaned the MCP server. Send SIGTERM
        # to mcp_pid so its lifespan finally runs (cleans chroma),
        # then exit.
        os.kill(mcp_pid, SIGTERM)
        time.sleep(2)                  # Grace for lifespan finally.
        if _alive(mcp_pid):
            os.kill(mcp_pid, SIGKILL)
        _stop_chroma_pgrp()            # Belt-and-braces.
        break
    time.sleep(_POLL_INTERVAL)
```

Watchdog continues to be spawned detached (`start_new_session=True`)
so it survives the MCP server's clean shutdown and is not killed by
the MCP server's pgroup teardown. The 5s poll interval is unchanged.

**Hook retirement** (`nx/hooks/hooks.json`):

- `SessionStart` hook: retain the non-chroma work (RDR skill
  loader, plugin upgrade banner, rdr-audit cadence hint). Remove
  the `nx hook session-start` chroma-spawn invocation.
- `SessionEnd` hook: retain scratch-flush + memory-expire (called
  via `nx hook session-end-flush`, a renamed subcommand that does
  everything `session_end()` does today MINUS the chroma stop).
  Remove `nx-session-end-launcher` from the hook chain, it
  becomes vestigial. The launcher script stays in the codebase as
  a fallback but is no longer wired in.

**Sweep retention**:

`sweep_stale_sessions` keeps all four triggers (age, server_dead,
anchor_dead, uuid_mismatch). It's belt-and-braces. The expected
production path is MCP atexit → clean; watchdog → SIGKILL fallback;
sweep → residual. With the primary path owned by MCP, the sweep
should fire rarely or never in practice, but removing it would
be premature until the MCP atexit path has production evidence.

**Tmpdir orphan coverage (Gap 3)**:

Two complementary paths:
1. MCP atexit removes the tmpdir of the chroma it owned → no new
   orphans created by MCP-managed chroma.
2. One-time migration sweep: `sweep_stale_sessions` gains an
   optional tmpdir-scan pass that reaps `nx_t1_*` directories
   under the system temp root when no session record points at
   them and the directory mtime is > 24h. Handles the pre-existing
   orphans left by the hook-era lifecycle.

### Existing Infrastructure Audit

| Proposed component | Existing module | Decision |
| --- | --- | --- |
| Chroma spawn at MCP startup | `nexus.hooks.session_start` | **Move**: port the chroma-spawn block from hooks.py into a new `_t1_chroma_init_if_owner` in mcp/core.py. Keep the session-start hook for non-chroma work. |
| Chroma teardown at MCP exit | `nexus.hooks.session_end` (chroma block) | **Move to atexit**: reuse `stop_t1_server` + tmpdir rmtree + record removal as an atexit handler. Keep `session_end_flush` for scratch-flush + memory-expire. |
| Watchdog target PIDs | `nexus.t1_watchdog` | **Add second flag**: keep `--claude-pid`, add `--mcp-pid`, OR-trigger logic. Both PIDs already in the session record; only the watchdog argument interface and polling loop change. Required to cover Claude Code issue #1935 (orphaned MCP on Claude crash). |
| Sweep | `sweep_stale_sessions` | **Keep + extend**: all four current triggers stay; optional tmpdir-scan pass added for Gap 3. |
| SessionEnd hook / launcher | `nx/hooks/hooks.json` + `_session_end_launcher.py` | **Retire from chroma path**: launcher stays as code but is unwired. Hook shrinks to scratch/memory cleanup via `nx hook session-end-flush`. |

### Decision Rationale

The MCP server is the only per-session subprocess whose lifecycle
Claude Code manages directly and reliably. Every layer of the
current machinery exists to simulate that coupling through an
out-of-band channel. Moving ownership to the MCP server eliminates
three of the five cleanup layers and tightens the remaining two
(watchdog target pivot, sweep becomes belt-and-braces).

The retention of watchdog + sweep is deliberate. MCP-SIGKILL is
the one case where `atexit` does not run; the watchdog still
needs to cover that path. Sweep is cheap insurance for the
combined-failure scenario (MCP SIGKILL + watchdog pgrp-signaled
to death). The point isn't to remove all defense-in-depth, it's
to make the primary path reliable enough that the other layers
fire in exceptional cases only.

## Alternatives Considered

### Alternative 1: Keep hook-based lifecycle, continue layered fixes

**Description**: Keep adding sweep triggers and hook hardening as
new corner cases surface.

**Pros**: No migration. No risk to existing deployments. The
three recent releases (4.10.3 + 4.11.0 + 4.11.1) already give a
functional system.

**Cons**: Layer count grows monotonically. Every release of
Claude Code that changes shutdown timing risks breaking the hook
anchor. Each new corner case is rediscovered in production.
Complexity floor keeps climbing for no benefit.

**Reason for rejection**: the proposed shift collapses three
layers into the primary path and leaves two as exception handling.
Net simplification with equivalent coverage.

### Alternative 2: Collapse into a single long-lived CLI daemon

**Description**: Spawn a `nx-daemon` process independently of
both Claude Code and the MCP server that owns all T1 lifecycle.

**Pros**: Not coupled to either Claude Code or MCP server
internals. Clean separation.

**Cons**: Adds a new long-lived process to the per-session surface.
No natural lifecycle anchor, back to simulating Claude Code
liveness. Re-introduces every problem the current machinery
exists to solve.

**Reason for rejection**: solves no problem we have. Adds a
problem we don't.

### Briefly rejected

- **Use `nx-mcp-catalog` as the anchor**: catalog server doesn't
  need T1 and the catalog lifecycle is narrower. Using `nx-mcp`
  is correct, T1 clients are the natural consumer.
- **Have chroma spawn inline in the nx-mcp main() without the
  subagent-detection check**: would create N chroma servers (one
  per nested MCP server) and destroy the cross-agent scratch
  sharing RDR-010 established. The ancestor-detection logic is
  load-bearing.

## Trade-offs

### Consequences

- `nexus.hooks.session_start` loses its chroma-spawn block; the
  block moves to `nexus.mcp.core._t1_chroma_init_if_owner`. Pure
  relocation, same logic.
- `nexus.hooks.session_end` loses its chroma-stop block; atexit
  + SIGTERM handler in the MCP server owns it. The remaining
  `session_end_flush` does scratch-flush + memory-expire only.
- `nx/hooks/hooks.json` SessionStart command tightens (no more
  chroma spawn in the hook); SessionEnd command swaps the
  launcher for a direct `nx hook session-end-flush` call. The
  5s timeout stays; work inside the hook is now milliseconds, not
  seconds.
- `t1_watchdog.py` keeps `--claude-pid` and adds `--mcp-pid`; the
  polling loop uses OR-trigger logic. The argument interface and
  the polling block are the only changes. Required to cover the
  orphaned-MCP-on-Claude-crash case (issue #1935, FM-NEW-1).
- `sweep_stale_sessions` gains an optional tmpdir-scan pass for
  historical orphan cleanup (Gap 3). The four record-based
  triggers unchanged.
- Test surface: `test_session.py` hook-lifecycle tests shift to
  MCP-lifecycle tests in `test_mcp_server.py` or a new
  `test_mcp_chroma_lifecycle.py`. `test_session_end_launcher.py`
  stays (launcher still exists, just unwired from hooks.json).

### Risks and Mitigations

- **Risk**: Claude Code's MCP-server shutdown ordering is faster
  than documented and doesn't give Python time to run atexit
  handlers, leaving chroma orphaned on every clean shutdown.
  **Mitigation**: Critical Assumption #1 requires a spike before
  accept. If the spike fails, the watchdog is the fallback -
  same coverage the hook-era setup has now for ungraceful exit.

- **Risk**: A future Claude Code release changes MCP shutdown
  semantics (e.g. SIGKILL after 100ms instead of SIGTERM+grace).
  Same race condition re-emerges, in a different subprocess.
  **Mitigation**: Canary test in the integration suite that
  exercises chroma cleanup on MCP shutdown; runs on every Claude
  Code version bump. Explicit break condition rather than silent
  regression.

- **Risk**: Migration leaves deployments in a mixed state -
  plugin 4.12.0 installed but conexus CLI still on 4.11.1, or
  vice versa. If plugin hooks.json no longer spawns chroma but
  MCP server doesn't yet own chroma, no T1 scratch works.
  **Mitigation**: Ship the conexus-side change first (4.12.0 adds
  MCP ownership but keeps the hook's chroma-spawn block as a
  no-op when the MCP server already owns one). Plugin can update
  after at least one conexus release has the dual-ownership path.
  Chained fallback in hooks.json (same pattern RDR-088 used for
  `nx-session-end-launcher || nx hook session-end-detach`) during
  the migration window.

- **Risk**: Subagent MCP servers don't correctly inherit the
  parent's session context and each spawns its own chroma,
  breaking cross-agent scratch.
  **Mitigation**: Critical Assumption #2 requires an integration
  test that explicitly dispatches a subagent chain and asserts
  scratch write-visibility across the tree. Same ancestor-detection
  code used today; the assertion just pins it to the new spawn
  site.

- **Risk (FM-NEW-1)**: Claude Code crashes and orphans the MCP
  server. Issue #1935 documents 40+ orphaned MCP server processes
  surviving Claude Code crashes in the wild (Docker, Python, Node).
  A watchdog watching only `--mcp-pid` would never fire, leaving
  chroma alive until the next session's sweep finds it.
  **Mitigation**: dual-PID watchdog described in the Approach and
  Technical Design sections. Watchdog watches BOTH `mcp_pid` and
  `claude_root_pid`; on Claude crash, the watchdog signals SIGTERM
  to mcp_pid (lifespan finally runs, chroma stops) then exits.
  Critical Assumption #3 verifies this end-to-end on a controlled
  Claude-kill harness.

- **Risk (FM-NEW-2)**: Claude Code issue #40207 documents an
  internal mid-session timeout that sends SIGTERM to healthy stdio
  MCP servers 10-60s after successful connection. If this affects
  vanilla stdio servers like nx-mcp, every timeout cycle would
  stop and restart chroma, creating a 1-5s window where T1 falls
  back to EphemeralClient and cross-agent scratch breaks.
  **Mitigation**: at MCP startup, TCP-probe any existing session
  record for the same `session_id`; if reachable, reuse the
  existing chroma rather than spawning fresh. The mid-session
  restart cycle becomes a no-op for chroma. Critical Assumption
  #4 verifies the timeout's applicability to nx-mcp and exercises
  the TCP-reuse path under simulated mid-session SIGTERM.

### Failure Modes

- **Visible**: MCP server startup fails to spawn chroma. T1 falls
  through to `EphemeralClient` (existing defense-in-depth); T2/T3
  unaffected. Logged via the new mcp.log path (PR #286).
- **Visible**: MCP server crash. PR #286 wires `mcp_server_crashed`
  with full traceback to mcp.log so post-mortem diagnosis no
  longer depends on Claude Code's captured stderr. The dual-watch
  watchdog detects mcp_pid gone within 5s and reaps chroma.
- **Silent (resolved in design)**: UUID rollover orphan chroma.
  Eliminated by the move. Old MCP subprocess's lifespan finally
  block stops its chroma before the new MCP subprocess starts.
- **Silent (resolved in design)**: Orphan tmpdirs. Eliminated by
  co-locating tmpdir creation and removal in the same process.
  One-time tmpdir-scan migration handles pre-existing cruft.
- **Silent (resolved in design, FM-NEW-1)**: Claude Code crashes
  and orphans the MCP server. Documented in issue #1935. The
  dual-watch watchdog (`--mcp-pid` + `--claude-pid`) handles this
  by signalling SIGTERM to mcp_pid when claude_pid disappears.
  Without the dual-watch, this would be a silent regression
  relative to the current hook-based design.
- **Silent (resolved in design, FM-NEW-2)**: Claude Code mid-
  session SIGTERM (issue #40207) repeatedly restarts the MCP
  server, creating a T1 gap on each restart. Mitigated by the
  TCP-probe-and-reuse path in `_t1_chroma_init_if_owner`. If the
  spike (Critical Assumption #4) shows the issue does not affect
  vanilla stdio servers, the TCP-reuse path remains as cheap
  insurance; if it does affect nx-mcp, the path is load-bearing.

## Implementation Plan

### Prerequisites

- [x] Spike A: instrumented 40-run lifecycle probe. **COMPLETED
      2026-04-25**, all four phases at 100% cleanup rate, all
      targets exceeded. Path attribution: `mcp_owned_signal` for
      clean (the empirical primary on stdio; see §Critical
      Assumptions for the lifespan-vs-signal-handler clarification),
      `watchdog_mcp` for SIGKILL + OOM, `watchdog_claude` for
      Claude crash. Median walls 10-11s per phase.
      Code: `scripts/spikes/spike_rdr094_lifecycle.py`.
      Records: `scripts/spikes/spike_rdr094_results.jsonl` (40
      lines), `spike_rdr094_summary.json`.
      Persisted: T2 nexus_rdr/094-spike-a-lifecycle (id=976) +
      T3 knowledge__nexus 9f1c77b683a9f429.
- [ ] Spike B: subagent chain scratch-sharing integration test.
      Parent MCP server spawns chroma; 3-level-deep subagent
      dispatch writes to scratch; each level reads back sibling
      writes. Assert all reads succeed.
- [ ] Spike C: mid-session SIGTERM probe (Claude Code issue
      #40207). Run a 30-minute idle session with the spike harness
      logging every signal received by nx-mcp. If SIGTERM is
      observed mid-session, run the follow-up: enable the
      TCP-probe-reuse path in `_t1_chroma_init_if_owner` and assert
      no T1 gap surfaces during 5 simulated restart cycles. If
      no SIGTERM is observed, document #40207 as not applicable
      to vanilla stdio servers and keep the TCP-reuse path as
      cheap insurance.

### Minimum Viable Validation

A fresh Claude Code session starts with no chroma process, the
first T1 operation spawns one (via MCP-owned startup), a `/clear`
replaces both MCP server and chroma, and clean session close
leaves zero orphan processes and zero orphan tmpdirs.

### Phase 1: MCP-owned chroma spawn (feature-flagged)

#### Step 1: Add `_t1_chroma_init_if_owner` to `src/nexus/mcp/core.py`

Port the chroma-spawn block from `nexus.hooks.session_start`.
Add the TCP-probe-and-reuse path (FM-NEW-2 mitigation): if a
session record for the same `session_id` exists and its address
is reachable, reuse it rather than spawning fresh. Register
under a feature flag (`NEXUS_MCP_OWNS_T1=1`) initially so the
migration is opt-in for spike validation.

#### Step 2: Wire FastMCP lifespan + atexit + signal handlers

Primary cleanup path: pass `lifespan=_t1_chroma_lifespan` to the
`FastMCP(...)` constructor. anyio's cancellation propagation
through the `async finally` block fires on SIGTERM without a
manual handler. Secondary: register `_t1_chroma_shutdown` via
`atexit` and bind it to SIGTERM/SIGINT signal handlers as belt-
and-braces. `_t1_chroma_shutdown` is idempotent (safe under
double-fire from any combination of paths).

#### Step 3: Keep hook spawn as fallback

`session_start()` checks whether MCP-owned chroma already wrote
the session record; if yes, no-op. If no, falls through to the
current spawn path. Migration window safety.

### Phase 2: Watchdog dual-watch + hook retirement

#### Step 1: Extend `t1_watchdog.py` to dual-watch

Add `--mcp-pid` to the argument interface, keep `--claude-pid`.
Polling loop uses OR-trigger logic: fires `_stop_chroma_pgrp()`
if mcp_pid disappears (MCP died without lifespan/atexit), or
sends SIGTERM to mcp_pid (then SIGKILL after 2s grace) if
claude_pid disappears (Claude crashed and orphaned the MCP
server, FM-NEW-1). Update `spawn_t1_watchdog(...)` callers in
`mcp/core.py` to pass both PIDs. Required to cover the Claude-
crash failure mode that issue #1935 documents.

#### Step 2: Split `hooks.session_end` into `session_end_flush`

Extract scratch-flush + memory-expire into a new function. The
chroma-stop path is conditional on MCP ownership: if MCP owns,
skip; if hook-era ownership (fallback), run.

#### Step 3: Update `nx/hooks/hooks.json`

SessionEnd command swaps `nx-session-end-launcher` for
`nx hook session-end-flush`. Timeout reduced from 5s to 3s (flush
is sub-second).

### Phase 3: Tmpdir-scan sweep + one-time cleanup

#### Step 1: Add tmpdir-scan pass to `sweep_stale_sessions`

Optional (gated by flag initially); scans `/var/folders/.../nx_t1_*`
for directories with no session record AND mtime > 24h; rmtree
with ignore_errors.

#### Step 2: Run the migration sweep on the reference install

Clean up the 10 orphan tmpdirs from 2026-04-23. One-shot.

### Phase 4: Feature-flag removal

After Phase 1–3 land and one release of canary evidence, remove
`NEXUS_MCP_OWNS_T1` gate. MCP-owned chroma becomes the default
path. Hook-era chroma-spawn block deleted from `session_start()`.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| T1 chroma process | `ps aux \| grep nx_t1` | per-session log | SIGTERM via MCP shutdown | `nx doctor --check-t1` | N/A (ephemeral) |
| Session records | `ls ~/.config/nexus/sessions/` | cat `<uuid>.session` | removed by MCP atexit | `nx doctor` T1 section | N/A |
| Tmpdir scan | `find /var/folders -name 'nx_t1_*'` | `du -sh` | sweep pass | `nx doctor --check-tmpdirs` | N/A |

### New Dependencies

None. Python `atexit`, `signal`, `socket` (TCP probe) are stdlib.
The FastMCP `lifespan=` constructor argument is already supported
by the `mcp.server.fastmcp.FastMCP` version pinned in `pyproject.toml`.

## Test Plan

- **Scenario**: `nx-mcp` starts without `NX_SESSION_ID` env, no
  existing session record. **Verify**: spawns chroma, writes
  session record (with both `server_pid` and `claude_root_pid`),
  registers lifespan + atexit + signal handlers.
- **Scenario**: `nx-mcp` starts with `NX_SESSION_ID` pointing at
  an ancestor's session. **Verify**: no new chroma spawned, t1
  client resolves to ancestor server.
- **Scenario (FM-NEW-2)**: `nx-mcp` starts with an existing
  session record for the same `session_id` and a reachable
  address. **Verify**: TCP-probe succeeds, no new chroma spawned,
  `_OWNED_CHROMA["reused"]=True`, the lifespan finally / atexit
  paths skip cleanup.
- **Scenario**: Clean MCP shutdown (Claude Code SIGTERM). **Verify**:
  lifespan finally block runs, chroma process gone, session file
  removed, tmpdir removed. atexit handler is a no-op (lifespan
  already cleaned up; idempotent shutdown).
- **Scenario**: MCP SIGKILL (no lifespan or atexit). **Verify**:
  watchdog `mcp_pid` trigger fires, reaps chroma within 5s.
- **Scenario (FM-NEW-1)**: Claude Code SIGKILL leaves MCP server
  orphaned. **Verify**: watchdog `claude_pid` trigger fires,
  signals SIGTERM to mcp_pid, lifespan finally runs, chroma stops
  within 7s (5s poll + 2s grace).
- **Scenario**: MCP crash + watchdog also killed (pgrp SIGKILL).
  **Verify**: next SessionStart's sweep catches via `server_dead`
  or `anchor_dead`; record and chroma reaped.
- **Scenario**: Subagent chain scratch sharing. Parent writes
  scratch entry, 3-level-deep child reads it. **Verify**: read
  returns parent's value.
- **Scenario**: UUID rollover (`/clear`). Old MCP subprocess
  lifespan finally fires, new MCP subprocess spawns fresh chroma.
  **Verify**: old chroma gone, new chroma alive, `current_session`
  pointer matches new chroma's record.
- **Scenario**: Tmpdir-scan pass finds 5 orphan tmpdirs older
  than 24h with no session record. **Verify**: rmtree all 5,
  log entry per reap.
- **Scenario**: Migration. conexus 4.12.0 installed, plugin
  4.11.x on disk (old hook still spawns chroma). **Verify**: MCP
  detects existing record via TCP-probe, single chroma process,
  single session record (no duplicate spawn).

## Validation

### Testing Strategy

Unit tests for the MCP-owned chroma spawn / teardown logic
(mocked `start_t1_server` / `stop_t1_server`), including the
TCP-probe-and-reuse path and the idempotent-shutdown contract
(double-fire safety). Integration test exercising the full
subagent-chain scratch-sharing path against a real chroma.
Lifecycle test that brings up a real `nx-mcp` subprocess, sends
SIGTERM, and asserts the lifespan finally block (or atexit
fallback) drove the cleanup to completion. FM-NEW-1 lifecycle
test: same harness, SIGKILL Claude Code instead, assert dual-
watch watchdog signals SIGTERM to mcp_pid and chroma exits
within 7s. Canary test (plugin-version-indexed) that runs each
test-plan scenario against the current Claude Code version,
detecting MCP-shutdown-ordering regressions including the
issue-#40207 mid-session SIGTERM case.

## Finalization Gate

_To be completed during /nx:rdr-gate._

## References

- RDR-010, T1 Scratch: Cross-Process Session Sharing via
  ChromaDB Server + PPID Chain (original design).
- RDR-034, MCP Server for Agent Storage Operations.
- RDR-062, MCP Interface Tiering (`nx-mcp` + `nx-mcp-catalog` split).
- RDR-078, Dimensional plan identity (session-ID propagation
  consumer).
- nexus-99jb, 4.10.3 three-layer defense-in-depth (watchdog +
  session-end-detach + liveness sweep). Layer this RDR's primary
  path replaces.
- nexus-886w, 4.11.0 uuid_mismatch sweep arm. Made largely
  redundant by this RDR.
- nexus-2u7o, 4.11.1 fork-first `nx-session-end-launcher`.
  Retires from the critical path under this RDR.
- `src/nexus/mcp/core.py`, `nx-mcp` entry point (insertion site).
- `src/nexus/session.py`, session record + chroma lifecycle
  helpers (reused, relocated callers).
- `src/nexus/t1_watchdog.py`, PID pivot target.
- `src/nexus/hooks.py`, hook bodies (shrink to scratch/memory only).
