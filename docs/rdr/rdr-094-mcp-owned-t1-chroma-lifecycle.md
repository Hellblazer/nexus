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
only because the process tree we're trying to couple to — the
Claude Code session itself — isn't directly addressable.

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
`nexus.*` imports complete — ~2 seconds of cold-start cost on a
reference install. The 4.11.1 `nx-session-end-launcher` console
script (nexus-2u7o) shrinks this to ~256ms by forking before any
`nexus.*` import, but the race is still a race — a sufficiently
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
and `claude_root_pid` both remain alive — the watchdog sees the
claude parent alive and does not fire; the `server_dead` and
`anchor_dead` sweep triggers are both satisfied (as in,
not-dead); and the orphan chroma would stay alive until the
24-hour age sweep catches it. The 4.11.0 `uuid_mismatch` sweep
arm (nexus-886w) addressed this by comparing the record's
`session_id` against the `current_session` flat-file pointer,
reaping when they disagree. The fix is correct but shouldn't be
necessary — if the MCP server owns chroma and Claude Code
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
without an owner — the process is the owner, and its `atexit`
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
(catalog) — these are the subprocesses Claude Code spawns via
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

- `src/nexus/mcp/core.py` — `nx-mcp` server entry point (`main()`,
  FastMCP instance at module scope). Long-lived subprocess owned by
  Claude Code via stdio.
- `src/nexus/mcp/catalog.py` — `nx-mcp-catalog` server. Separate
  process; typically doesn't need T1.
- `src/nexus/session.py` — `start_t1_server()`, `stop_t1_server()`,
  `sweep_stale_sessions()`, `find_claude_root_pid()`, session
  record I/O.
- `src/nexus/hooks.py` — `session_start()`, `session_end()`. Called
  by the hook subcommands.
- `src/nexus/commands/hook.py` — `nx hook session-start`, `nx hook
  session-end`, `nx hook session-end-detach` subcommands. The
  SessionStart hook runs synchronously; SessionEnd fires via the
  launcher.
- `src/nexus/_session_end_launcher.py` — 4.11.1 fork-first console
  script. Minimal imports at top level.
- `src/nexus/t1_watchdog.py` — 4.10.3 sidecar. Watches
  `--claude-pid` + `--chroma-pid`, kills chroma when claude dies.
- `src/nexus/db/t1.py` — T1 ChromaDB client. Resolves session by
  `NX_SESSION_ID` env / `current_session` flat file / PPID walk.
- `nx/hooks/hooks.json` — SessionStart + SessionEnd wired to the
  corresponding `nx hook ...` commands.

## Research Findings

### Investigation

Claude Code's MCP-subprocess lifecycle observed through plugin
use: the server is spawned on session start and killed before
Claude Code finishes exiting, with stdio handshake for both
boundaries. No "Hook cancelled" analog exists for MCP servers —
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
  of that has to change — only the SPAWN side moves.
- **Assumed**: Claude Code sends SIGTERM (not SIGKILL) to MCP
  servers on clean shutdown, giving Python time to run `atexit`.
  **Status**: Unverified — **Method**: instrumented spike on a
  local Claude Code install; run 10 graceful close cycles, 10
  `kill -9 claude` cycles, 10 OOM simulations; measure fraction of
  runs where atexit-based chroma stop reached completion.
- **Assumed**: MCP server crash/restart within a live Claude
  session is rare in practice. **Status**: Unverified —
  **Method**: Layer 3 watchdog continues to exist for this case;
  the spike above will also measure MCP-restart frequency.

### Critical Assumptions

- [ ] Claude Code's MCP-server shutdown ordering delivers SIGTERM
      with enough time for Python `atexit` handlers to run chroma
      cleanup. — **Status**: Unverified — **Method**: 30-run spike
      across graceful / SIGKILL / OOM paths.
- [ ] Moving chroma spawn from the SessionStart hook to MCP-server
      startup does not regress subagent T1 sharing. — **Status**:
      Unverified — **Method**: integration test dispatching a
      subagent chain and asserting scratch read/write across the
      tree.
- [ ] The watchdog's pivot from `--claude-pid` to `--mcp-pid`
      preserves coverage of the "MCP dies without atexit firing"
      case. — **Status**: Unverified — **Method**: controlled
      `kill -9` of the MCP server in a test harness; assert
      watchdog reaps chroma within 5s.

## Proposed Solution

### Approach

Move chroma spawn to `nx-mcp` server startup and chroma teardown
to `atexit` + `SIGTERM` handlers in the same process. Keep the
`t1_watchdog` sidecar but pivot it to watch the MCP server's PID.
Keep the `sweep_stale_sessions` liveness sweep as final belt-and-
braces for the rare case of both `atexit` and watchdog failing.
Retire the `SessionStart` and `SessionEnd` hooks' chroma-management
responsibilities; keep the hooks for scratch-flush and memory-
expire which remain hook-native tasks.

### Technical Design

**`nx-mcp` server lifecycle** (`src/nexus/mcp/core.py`):

```text
main():
    # Existing FastMCP setup unchanged.
    _t1_chroma_init_if_owner()       # NEW
    atexit.register(_t1_chroma_shutdown)
    signal.signal(SIGTERM, _sigterm_handler)
    signal.signal(SIGINT, _sigterm_handler)
    # FastMCP run loop.
    mcp.run()

_t1_chroma_init_if_owner():
    # Subagent detection — same logic t1.py already has.
    if os.environ.get("NX_SESSION_ID") and _find_ancestor_session():
        return   # Nested MCP server; connect to ancestor, don't spawn.
    # Top-level MCP server → own the chroma lifecycle.
    host, port, pid, tmpdir = start_t1_server()
    _OWNED_CHROMA.update({"pid": pid, "tmpdir": tmpdir})
    write_session_record_by_id(
        SESSIONS_DIR, session_id=_resolve_own_session_id(),
        host=host, port=port, server_pid=pid,
        claude_root_pid=find_claude_root_pid(),
        watchdog_pid=spawn_t1_watchdog(mcp_pid=os.getpid(), chroma_pid=pid),
    )

_t1_chroma_shutdown():
    if not _OWNED_CHROMA:
        return
    stop_t1_server(_OWNED_CHROMA["pid"])
    shutil.rmtree(_OWNED_CHROMA["tmpdir"], ignore_errors=True)
    _remove_own_session_record()
```

**Watchdog pivot** (`src/nexus/t1_watchdog.py`):

The `--claude-pid` flag renames to `--mcp-pid`. The polling loop
swaps target but otherwise unchanged — still uses `os.kill(pid, 0)`
for liveness probe, still pgrp-signals chroma on parent death.
Watchdog continues to be spawned detached so it survives the MCP
server's clean shutdown, then observes the MCP's exit code to
decide whether cleanup is already done (atexit fired) or needs to
happen from the sidecar (MCP SIGKILL'd).

**Hook retirement** (`nx/hooks/hooks.json`):

- `SessionStart` hook: retain the non-chroma work (RDR skill
  loader, plugin upgrade banner, rdr-audit cadence hint). Remove
  the `nx hook session-start` chroma-spawn invocation.
- `SessionEnd` hook: retain scratch-flush + memory-expire (called
  via `nx hook session-end-flush`, a renamed subcommand that does
  everything `session_end()` does today MINUS the chroma stop).
  Remove `nx-session-end-launcher` from the hook chain — it
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
| Watchdog target PID | `nexus.t1_watchdog` | **Rename flag**: `--claude-pid` → `--mcp-pid`. Same observer, different target. |
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
to death). The point isn't to remove all defense-in-depth — it's
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
No natural lifecycle anchor — back to simulating Claude Code
liveness. Re-introduces every problem the current machinery
exists to solve.

**Reason for rejection**: solves no problem we have. Adds a
problem we don't.

### Briefly rejected

- **Use `nx-mcp-catalog` as the anchor**: catalog server doesn't
  need T1 and the catalog lifecycle is narrower. Using `nx-mcp`
  is correct — T1 clients are the natural consumer.
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
- `t1_watchdog.py` renames `--claude-pid` to `--mcp-pid`. One-line
  semantics change.
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
  accept. If the spike fails, the watchdog is the fallback —
  same coverage the hook-era setup has now for ungraceful exit.

- **Risk**: A future Claude Code release changes MCP shutdown
  semantics (e.g. SIGKILL after 100ms instead of SIGTERM+grace).
  Same race condition re-emerges, in a different subprocess.
  **Mitigation**: Canary test in the integration suite that
  exercises chroma cleanup on MCP shutdown; runs on every Claude
  Code version bump. Explicit break condition rather than silent
  regression.

- **Risk**: Migration leaves deployments in a mixed state —
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

### Failure Modes

- **Visible**: MCP server startup fails to spawn chroma → T1 falls
  through to `EphemeralClient` (existing defense-in-depth); T2/T3
  unaffected. Logged.
- **Silent (resolved in design)**: UUID rollover orphan chroma —
  eliminated by the move. Old MCP subprocess's atexit stops its
  chroma before the new MCP subprocess starts.
- **Silent (resolved in design)**: Orphan tmpdirs — eliminated by
  co-locating tmpdir creation and removal in the same process.
  One-time tmpdir-scan migration handles pre-existing cruft.

## Implementation Plan

### Prerequisites

- [ ] Spike: instrumented 30-run lifecycle probe. Phases: 10 clean
      shutdowns, 10 `kill -9` of Claude Code, 10 simulated OOM.
      Measure chroma-cleanup completion rate by source (atexit,
      watchdog, sweep, none). Target: atexit covers ≥90% of clean
      shutdowns; watchdog covers ≥95% of SIGKILL/OOM.
- [ ] Spike: subagent chain scratch-sharing integration test.
      Parent MCP server spawns chroma; 3-level-deep subagent
      dispatch writes to scratch; each level reads back sibling
      writes. Assert all reads succeed.

### Minimum Viable Validation

A fresh Claude Code session starts with no chroma process, the
first T1 operation spawns one (via MCP-owned startup), a `/clear`
replaces both MCP server and chroma, and clean session close
leaves zero orphan processes and zero orphan tmpdirs.

### Phase 1: MCP-owned chroma spawn (feature-flagged)

#### Step 1: Add `_t1_chroma_init_if_owner` to `src/nexus/mcp/core.py`

Port the chroma-spawn block from `nexus.hooks.session_start`.
Register under a feature flag (`NEXUS_MCP_OWNS_T1=1`) initially
so the migration is opt-in for spike validation.

#### Step 2: Register atexit + signal handlers

`_t1_chroma_shutdown` handles SIGTERM, SIGINT, and atexit.
Idempotent (safe under double-fire).

#### Step 3: Keep hook spawn as fallback

`session_start()` checks whether MCP-owned chroma already wrote
the session record; if yes, no-op. If no, falls through to the
current spawn path. Migration window safety.

### Phase 2: Watchdog + hook retirement

#### Step 1: Pivot `t1_watchdog.py` to `--mcp-pid`

Rename flag, update `spawn_t1_watchdog` caller. Same observer
logic.

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

None. Python `atexit` and `signal` are stdlib.

## Test Plan

- **Scenario**: `nx-mcp` starts without `NX_SESSION_ID` env →
  spawns chroma, writes session record, registers atexit. **Verify**:
  session file exists, chroma process alive, atexit handler
  registered.
- **Scenario**: `nx-mcp` starts with `NX_SESSION_ID` pointing at
  an ancestor's session → no chroma spawned, connects to ancestor.
  **Verify**: no new chroma process, t1 client resolves to
  ancestor server.
- **Scenario**: Clean MCP shutdown (Claude Code SIGTERM) →
  atexit fires. **Verify**: chroma process gone, session file
  removed, tmpdir removed.
- **Scenario**: MCP SIGKILL (no atexit) → watchdog detects dead
  MCP PID, reaps chroma. **Verify**: chroma gone within 5s.
- **Scenario**: MCP crash + watchdog also killed (pgrp SIGKILL)
  → next SessionStart's sweep catches via `server_dead` or
  `anchor_dead`. **Verify**: record and chroma reaped.
- **Scenario**: Subagent chain scratch sharing → parent writes
  scratch entry, 3-level-deep child reads it. **Verify**: read
  returns parent's value.
- **Scenario**: UUID rollover (`/clear`) → old MCP subprocess
  atexit fires → new MCP subprocess spawns fresh chroma. **Verify**:
  old chroma gone, new chroma alive, `current_session` pointer
  matches new chroma's record.
- **Scenario**: Tmpdir-scan pass finds 5 orphan tmpdirs older
  than 24h with no session record → rmtree all 5. **Verify**:
  directories removed, log entry per reap.
- **Scenario**: Migration — conexus 4.12.0 installed, plugin
  4.11.x on disk (old hook still spawns chroma) → MCP detects
  existing record, no duplicate spawn. **Verify**: single chroma
  process, single session record.

## Validation

### Testing Strategy

Unit tests for the MCP-owned chroma spawn / teardown logic
(mocked `start_t1_server` / `stop_t1_server`). Integration test
exercising the full subagent-chain scratch-sharing path against
a real chroma. Lifecycle test that brings up a real `nx-mcp`
subprocess, sends SIGTERM, and asserts atexit-driven cleanup
completed. Canary test (plugin-version-indexed) that runs each
of the nine test-plan scenarios against the current Claude Code
version, detecting MCP-shutdown-ordering regressions.

## Finalization Gate

_To be completed during /nx:rdr-gate._

## References

- RDR-010 — T1 Scratch: Cross-Process Session Sharing via
  ChromaDB Server + PPID Chain (original design).
- RDR-034 — MCP Server for Agent Storage Operations.
- RDR-062 — MCP Interface Tiering (`nx-mcp` + `nx-mcp-catalog` split).
- RDR-078 — Dimensional plan identity (session-ID propagation
  consumer).
- nexus-99jb — 4.10.3 three-layer defense-in-depth (watchdog +
  session-end-detach + liveness sweep). Layer this RDR's primary
  path replaces.
- nexus-886w — 4.11.0 uuid_mismatch sweep arm. Made largely
  redundant by this RDR.
- nexus-2u7o — 4.11.1 fork-first `nx-session-end-launcher`.
  Retires from the critical path under this RDR.
- `src/nexus/mcp/core.py` — `nx-mcp` entry point (insertion site).
- `src/nexus/session.py` — session record + chroma lifecycle
  helpers (reused, relocated callers).
- `src/nexus/t1_watchdog.py` — PID pivot target.
- `src/nexus/hooks.py` — hook bodies (shrink to scratch/memory only).
