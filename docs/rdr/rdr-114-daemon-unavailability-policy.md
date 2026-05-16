---
title: "Daemon unavailability policy: EventStream reconnect contract + bridge fail-closed-vs-open"
id: RDR-114
type: Architecture
status: draft
priority: medium
author: Hellblazer
reviewed-by: self
created: 2026-05-16
accepted_date:
related_issues: [nexus-sddg, nexus-mx0z]
---

# RDR-114: Daemon unavailability policy: EventStream reconnect contract + bridge fail-closed-vs-open

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-112 establishes the T2 daemon as the single writer for the
shared SQLite stores (`memory.db`, `tuples.db`) and the Chroma
collections used by the tuplespace. Two RDR-112 client surfaces still
have undefined behaviour when the daemon is unavailable (stopped,
restarting, or refusing connections). Both came out of the 2026-05-16
360-critique sweep and were deferred for a design call instead of an
inline fix.

### Enumerated gaps to close

#### Gap 1: EventStream subscribers have no defined reconnect contract

`event_stream.subscribe` (RDR-112 P1.3) is a persistent server-push
RPC. When the daemon restarts, the server-side asyncio handler is
torn down and the client's stream closes. The client library has no
specified retry policy: callers see an arbitrary `IncompleteReadError`
or `ConnectionResetError` and are left to roll their own reconnect.
The cursor mechanism (`since_cursor`) is already round-trippable, so
exactly-once-or-more delivery semantics are achievable; what is
missing is a written contract for *who* retries, *with what backoff*,
*how many times*, and *what happens after exhaustion*. Cockpit-side
binding watchers, the planned auditor agent, and any third-party
subscriber would otherwise each re-derive a slightly different
policy, with cross-implementation drift.

#### Gap 2: Bridge silently falls back to direct sqlite3 open when daemon RPC fails

`hook_bridge._emit_routed` (RDR-111) prefers daemon-mode dispatch via
`T2Client.call("tuplespace.out", ...)` and, on any failure (discovery
miss, connection refused, RPC error), falls back to opening
`tuples.db` directly via `api.out`. Under
`NX_STORAGE_MODE=daemon` the daemon owns the WAL writer; a parallel
direct open from the bridge process is a WAL-contention race and a
boundary violation of RDR-112 §A2 (single-writer compliance). The
fallback is "fail-open" by default, emission succeeds at the cost
of crossing the boundary. The alternative "fail-closed" path would
drop the tuple and log; emission fails loudly but the boundary
holds. The 2026-05-16 critique flagged this as defaulting silently
to the wrong policy.

## Context

### Background

The 6-critic 360 review on 2026-05-16 (postmortem at
`docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`) found
both gaps. The P0 + P1 remediation sweep that followed closed 14
ship-blockers and 12 P1 critique leaves on develop, ending at
`d0bad8c5`. These two beads (`nexus-sddg` + `nexus-mx0z`) were
deferred because each is a policy decision with multiple defensible
alternatives, not a bug fix.

The substrate for both decisions already exists:
- `T2Client.call(...)` for the bridge dispatch path
  (`src/nexus/daemon/t2_client.py`).
- `event_stream.subscribe` cursor parameter for resumption
  (`src/nexus/daemon/event_stream.py:200..228`).
- `find_t2_daemon()` discovery + PID-liveness probe
  (`src/nexus/daemon/discovery.py`; PID probe shipped as nexus-j6dj
  in PR #826).
- `action_idempotency` table (`src/nexus/db/migrations.py`,
  shipped as nexus-8wvs in PR #824), guards against
  at-least-once duplicate processing on the receive side.
- `_BRIDGE_DISABLE` opt-out env (already wired by nexus-7zvp).
- `NX_STORAGE_MODE=daemon` env gate read by
  `reject_under_daemon_mode` (`src/nexus/db/__init__.py`).

Both gaps share the same axis: how does a client behave when the
daemon, which is supposed to be running, is not? The answer should
be uniform across surfaces, emitters and subscribers, so operators
have one mental model.

### Technical Environment

- Python 3.12+. asyncio for both subscriber stream and daemon RPC.
- Bridge runs as a one-shot Python subprocess per hook fire (no
  long-lived process to maintain a reconnect loop).
- EventStream subscribers run as long-lived clients (binding
  watcher, cockpit panels, future auditor agent).
- WAL mode is on for `tuples.db`; SQLite tolerates concurrent
  writers via busy-wait, but Chroma writes are not similarly
  serialised.
- No retry library currently in tree at the right abstraction layer;
  `nexus.retry._voyage_with_retry` is Voyage-API-specific.

## Research Findings

### Investigation

Direct reading of:
- `src/nexus/cockpit/hook_bridge.py` lines 306..362 (the
  `_emit_routed` daemon-then-direct flow).
- `src/nexus/daemon/event_stream.py` lines 173..305 (server-side
  subscribe handler).
- `src/nexus/daemon/t2_client.py` (client-side RPC primitives and
  `T2Client.event_stream` generator).
- `src/nexus/daemon/discovery.py` (PID probe, file format).

Beads that pre-shipped supporting substrate:
- nexus-7zvp `NX_BRIDGE_DISABLE` wired (PR #822).
- nexus-8wvs `action_idempotency` table (PR #824).
- nexus-j6dj PID-liveness probe in `find_t2_daemon` (PR #826).
- nexus-uvv1 pre-migration backup + recovery doc (PR #829).
- nexus-r7dy non-blocking startup retention sweep (`45b026e2`).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `asyncio.open_unix_connection` | Yes | `ConnectionRefusedError` raised when UDS path exists but no listener, distinguishable from `FileNotFoundError` (no path). |
| `asyncio.IncompleteReadError` | Yes | Raised when peer closes mid-frame. The EventStream server-side handler closes cleanly on `_stopping`, so client sees `IncompleteReadError` with `partial=b""`. |
| `socket.AF_UNIX` connect on stale path | Yes | If `find_t2_daemon`'s PID probe is bypassed, `connect()` to a dead UDS path returns `ConnectionRefusedError` (verified by RDR-112 A2 spike, RDR-112 §A2 line ~470). |
| `sqlite3.connect` on a daemon-owned WAL DB | Yes | SQLite does not refuse the open, both writers race via busy-timeout. The "violation" is semantic (RDR-112 boundary), not a syscall failure. |

### Key Discoveries

- **Verified**: The bridge's direct-mode fallback opens its own
  Chroma `PersistentClient` (`src/nexus/cockpit/hook_bridge.py`
  `_emit_direct_auto`), which does NOT honour the daemon's
  in-process index. The orphan-vector class is the same one
  `nexus-dc9a` just closed for the daemon's retention sweep,
  fallback writes leave Chroma vectors keyed differently from the
  daemon's index until both reconcile.
- **Verified**: The cursor parameter of `event_stream.subscribe` is
  always written into the wire frame on the server side
  (`{op: "out", cursor: N}`), and the client's generator yields
  events with their cursor. Resumption from `since_cursor=N`
  delivers strictly events with `rowid > N`. So a client that
  records the last-seen cursor and reconnects with `since_cursor=last`
  has effectively exactly-once delivery semantics across reconnects.
- **Documented**: `find_t2_daemon` with the PID probe distinguishes
  "daemon truly running" from "stale discovery file" in O(microseconds).
  Reconnect attempts that bypass discovery (e.g., reusing a known
  UDS path) lose this discrimination.
- **Documented**: Hook bridges run as one-shot subprocesses. They
  cannot run a background retry loop or persist a queue. The bridge's
  daemon-unavailability budget is bounded by the hook's own
  wall-clock budget (sub-second for `PreToolUse`, ~5s for
  `PostToolUse`).
- **Assumed**: Most daemon "unavailability" in normal operation is
  measured in seconds (graceful restart) not minutes. Multi-minute
  outages reflect operator intervention or an OS-level supervisor
  failure that no client-side retry can paper over.

### Critical Assumptions

- [x] Bridge-side daemon-RPC latency p99 under load is < 250 ms,
  **Status**: Verified (local-mode) / Caveat (cloud-mode),
  **Method**: Spike (2026-05-16).
  *Why load-bearing*: if the daemon path is slow enough that bridges
  routinely give up on it, fail-closed becomes a hot-path correctness
  regression rather than a defense-in-depth gain.

  *Spike result*, local-mode, daemon on tmp config_dir with
  production registry seeded, `chromadb.PersistentClient` +
  `DefaultEmbeddingFunction` (bundled ONNX MiniLM), N=1000 sequential
  `tuplespace.out` calls after 5-call warmup:

  | Metric | Value |
  | --- | --- |
  | min | 36.57 ms |
  | p50 | 41.01 ms |
  | p95 | 45.20 ms |
  | p99 | **48.47 ms** |
  | max | 89.44 ms |
  | mean / stdev | 41.30 ms / 2.86 ms |

  PASS: p99 48.47 ms is roughly 5x under the 250 ms target. The
  spike script is preserved at
  `scripts/spikes/spike_rdr114_tuplespace_out_latency.py` for
  reproducibility. Methodology: background-thread daemon + sync
  T2Client (the asyncio loop must run on a separate thread to
  avoid a same-loop deadlock when the sync client blocks waiting
  on RPC reply from a daemon scheduled on the same loop).

  *Cloud-mode caveat*: under `chromadb.CloudClient` + `voyage-context-3`
  the per-embed cost is dominated by a Voyage API round-trip
  (typical 100–300 ms p50, higher under provider load). p99 in
  that regime could approach or exceed the 250 ms target. The
  fail-closed default is still correct under cloud mode (a slow
  daemon is preferable to a WAL-contention race), but the
  implementation must size the T2Client per-RPC timeout to
  accommodate cloud-mode latency, and operator docs should call
  out the trade-off. Spike for cloud mode is deferred to
  implementation time when API credentials are wired into a test
  fixture.

- [x] EventStream subscribers can tolerate a 0.5–2.0 s reconnect
  gap without dropping reactive correctness, **Status**: Verified
  via Source Search.

  *Finding*: `src/nexus/cockpit/bindings.py` lines 549..572 ship
  `_BindingWatcher` as **direct SQLite polling against the events
  table**, NOT via `event_stream.subscribe`. The class docstring
  explicitly defers daemon-mode subscription ("will swap the SQL
  polling for `event_stream.subscribe` once the RPC ships"). Grep
  across `src/nexus` + `nx/` confirms zero production consumers of
  `event_stream.subscribe` outside the daemon's own handler.

  Implications:
  1. The reconnect-gap assumption is moot for v1 because no
     production reader is on `event_stream.subscribe` yet.
  2. When the binding watcher migrates, its current poll interval
     is 50 ms (`poll_interval=0.05` default). A 0.5–2.0 s
     reconnect gap is 10–40x the steady-state tick budget. The
     watcher would observe a brief latency bump but lose nothing,
     because the cursor-driven `rowid > last_rowid` query
     replays any events that landed during the gap. Reactive
     correctness is preserved.
  3. Cockpit panels (RDR-111) currently read recent events via
     daemon RPC (`introspection.peek` over `events`), not via
     `event_stream.subscribe`. Same conclusion as above.

- [x] No production reader currently depends on the silent direct-
  mode fallback to mask daemon outages, **Status**: Verified via
  Source Search.

  *Finding*: There are exactly 7 production callers of
  `hook_bridge.emit`, the seven `orb_bridge_*.py` scripts under
  `nx/hooks/scripts/`. Each script wraps `emit()` in a
  `try / except Exception: log + exit 0` block and does not check
  the return value. None of the scripts contain logic that depends
  on the tuple actually landing on the SQLite side, they all
  produce their RF-2 transparent-allow stdout regardless of the
  emission result.

  The direct-fallback test suite at
  `tests/cockpit/test_hook_bridge_direct_fallback.py` exercises
  the fallback as a regression shield (verifying that
  `_emit_direct_auto` works correctly when invoked), not as a
  production consumer that depends on it. Under the proposed
  default flip:
  1. The test fixture must set `NX_STORAGE_MODE=daemon` +
     `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` to keep exercising the
     fallback path under daemon-mode. Or run in direct mode (no
     `NX_STORAGE_MODE` env), the default test path.
  2. A new test must cover the fail-closed branch under daemon
     mode without the opt-in (no SQLite / Chroma writes, drop
     event logged).

  Conclusion: the silent fallback exists only to mask interactive
  daemon-not-yet-started situations during operator setup. The
  `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` opt-in env preserves this
  exact behaviour as a one-line escape hatch.

**Method definitions**:

- **Source Search**: API verified against dependency source code
  (standard method for libraries).
- **Spike**: Behavior verified by running code against a live
  service (for opaque services only).
- **Docs Only**: Based on documentation reading alone (insufficient
  for load-bearing assumptions).

## Proposed Solution

### Approach

Adopt one unified mental model, **"daemon-mode means daemon-owned;
client-side retry is bounded and visible"**, and apply it to both
surfaces.

**Gap 1 (EventStream reconnect):** Add a thin reconnect wrapper to
`T2Client.event_stream` that owns the retry policy. The wrapper
records the last-seen cursor, catches close-side exceptions, and
retries via `find_t2_daemon` + new subscribe with `since_cursor=last`.
Retry policy: capped exponential backoff (250 ms × 2^N, max 8 s, max
attempts 10 ≈ 30 s budget), jittered by ±25 % to avoid synchronised
reconnect storms when multiple subscribers race the same daemon
restart. After exhaustion the wrapper raises a typed
`EventStreamUnavailable` exception with the last-seen cursor in the
message so the operator (or supervising loop) can decide what to do.
The contract is **explicitly documented in the client-library
docstring + the daemon CLI reference** so all subscribers, current
and future, share one policy.

**Gap 2 (bridge fail-closed):** Under `NX_STORAGE_MODE=daemon`, the
bridge's direct-mode fallback is **off by default**. Daemon-RPC
failure logs a typed event (`hook_bridge_emit_drop_daemon_unavailable`,
with the hook type, subspace, and exception class) and returns
without writing anywhere. The hook script still produces correct
stdout (RF-2 transparent allow) so user-facing tools never see the
drop. An opt-in env `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` re-enables
the legacy fail-open path for operators who knowingly accept the
WAL-contention risk during planned daemon downtime. In direct mode
(`NX_STORAGE_MODE` unset or `direct`) the current behaviour is
unchanged, direct emission is the supported path.

### Technical Design

**EventStream reconnect wrapper** (`src/nexus/daemon/t2_client.py`):

```text
# Illustrative, verify signatures during implementation.

def event_stream(
    self,
    subspace_prefix: str,
    *,
    since_cursor: int = 0,
    where: dict | None = None,
    reconnect: bool = True,  # New; default-on.
    max_reconnect_attempts: int = 10,
    initial_backoff_seconds: float = 0.25,
    max_backoff_seconds: float = 8.0,
) -> Iterator[dict]:
    """Yield event frames. Reconnects across daemon restarts.

    Yields each event from the daemon's event_stream.subscribe RPC.
    On EOF (daemon close / restart), reconnects with the last-seen
    cursor up to ``max_reconnect_attempts`` times, with capped
    exponential backoff and ±25% jitter, before raising
    ``EventStreamUnavailable``.

    Setting reconnect=False reproduces the legacy behaviour
    (single subscribe, no retry).
    """
```

The wrapper is the only place reconnect logic lives. Existing
callers (`binding_watcher`, cockpit panels) get the new policy for
free by upgrading to the new client method.

**Bridge fail-closed gate** (`src/nexus/cockpit/hook_bridge.py`):

```text
# Illustrative, verify env-var parsing matches NX_BRIDGE_DISABLE.

def _direct_fallback_allowed() -> bool:
    """True iff direct-mode fallback is permitted from this process.

    Returns True when NX_STORAGE_MODE is not set or != "daemon"
    (i.e., we're in direct mode and direct is the supported path).
    Returns True when NX_BRIDGE_ALLOW_DIRECT_FALLBACK is explicitly
    set to a truthy value (operator override during planned downtime).
    Returns False otherwise, under daemon mode the bridge fails
    closed on daemon-RPC failure.
    """

def _emit_routed(...) -> str:
    ...
    if daemon_failed:
        if not _direct_fallback_allowed():
            _log.warning(
                "hook_bridge_emit_drop_daemon_unavailable",
                hook_type=hook_type, subspace=subspace,
                error=str(daemon_exc),
            )
            return "skipped-daemon-unavailable"
        # Else: existing direct fallback path.
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| EventStream reconnect wrapper | `T2Client.event_stream` (single-subscribe generator) | Extend: wrap the existing generator in a reconnect loop. Keep the no-reconnect path reachable via `reconnect=False`. |
| `EventStreamUnavailable` exception | None | New: small dataclass-like exception carrying the last-seen cursor. |
| Bridge fail-closed gate | `_emit_routed` in `hook_bridge.py` | Extend: insert a single env-gated branch in the daemon-failure path. No new module. |
| `_direct_fallback_allowed` helper | None (sibling to `_bridge_disabled`) | New: mirrors the `_bridge_disabled` opt-out pattern. |
| Backoff + jitter | `nexus.retry._voyage_with_retry` (Voyage-specific) | Reuse pattern, don't import: copy the small jittered-exponential helper inline. The Voyage helper expects an HTTP response code; ours operates on socket-close exceptions. |

### Decision Rationale

**One unified policy** beats per-surface ad-hoc behaviour because
operators need one mental model. The principle: when the daemon is
unavailable in daemon mode, *clients fail visibly and recover when
the daemon comes back*, they do not silently cross the boundary.

**Client-side reconnect, not server-side push-then-pull**: the
daemon would have to track subscriber state across its own restart
to retry-from-the-server-side, which is exactly the kind of
durability surface RDR-112 §9 explicitly does not provide. The
cursor already carries the resumption point; the client owns the
loop.

**Fail-closed default in daemon mode** because the orphan-Chroma /
WAL-contention risk grows with the rate of direct fallbacks, and a
fail-closed drop is observable (one log line per drop) where a
silent fallback is not. The opt-in env gives operators a one-line
escape hatch for planned downtime.

**Bounded retry budget** (~30 s) because the daemon is intended to
be HA-supervised (launchd / systemd KeepAlive + RestartSec). If the
daemon is down for longer, that's an operator-attention event, not
a thing the client should paper over.

## Alternatives Considered

### Alternative 1: Server-side reconnect notification

**Description**: Daemon writes a "shutting-down" frame to every
open subscriber connection, including a hint of the expected restart
window. Clients use the hint to schedule a precise reconnect.

**Pros**:

- Clients reconnect at the right time, not via backoff guessing.
- Operator-visible signal of planned restart.

**Cons**:

- Adds a new frame type to the wire protocol; every client must
  understand it.
- Unplanned daemon termination (kill -9, crash) skips the frame; the
  client still needs a fallback backoff anyway.
- Cross-cutting change vs. the targeted client-side wrapper.

**Reason for rejection**: pushes complexity into the wire protocol
to save backoff guessing during planned restarts only. The
client-side wrapper handles both planned and unplanned outages with
one code path.

### Alternative 2: Per-surface policy (different for subscribers vs emitters)

**Description**: Keep the bridge's fail-open default; only formalise
the subscriber reconnect contract.

**Pros**:

- Smaller blast radius. Bridge behaviour unchanged.

**Cons**:

- Two different operator mental models on the same daemon-mode
  axis: "subscribers fail loud / emitters fail silent." Cognitive
  cost during incidents.
- Leaves the WAL-contention boundary violation in place.

**Reason for rejection**: the 360 critique flagged the bridge
fallback specifically as a silent boundary violation under daemon
mode. Postponing the policy decision does not eliminate the risk.

### Alternative 3: Persistent client-side queue for bridge emissions

**Description**: When the daemon is unavailable, the bridge spools
emissions to a local file; a sidecar daemon-watcher drains the
spool when the daemon returns.

**Pros**:

- Zero data loss across daemon outages.

**Cons**:

- Out of scope for one-shot hook subprocesses (no sidecar exists).
- Requires durable on-disk format, draining order semantics, GC
  policy, etc., multi-RDR investment.

**Reason for rejection**: cost / value mismatch given the brief
nature of daemon outages and the existence of `event_stream` cursor
backfill for catching up subscriber state. Revisit only if operator
data demands it.

### Briefly Rejected

- **Synchronous in-process daemon-spawn-on-demand**: violates RDR-112
  §A2 (only the supervised daemon may own the WAL writer).
- **Crash the bridge process on daemon failure**: would surface the
  failure too loudly, every hook fire would prompt the user during
  a daemon restart.

## Trade-offs

### Consequences

- Operators get one consistent rule across both surfaces: in daemon
  mode, daemon unavailability is loud + recoverable, not silent.
- One new typed exception (`EventStreamUnavailable`) and one new env
  (`NX_BRIDGE_ALLOW_DIRECT_FALLBACK`) for the operator manual.
- Some short daemon restarts that today result in 0 dropped tuples
  (via direct fallback) will after this RDR result in N dropped
  tuples + N log lines. Acceptable: each drop is observable, and
  the boundary holds.
- EventStream consumers automatically inherit the reconnect policy
 , no per-call wiring beyond the existing `event_stream(...)` call.

### Risks and Mitigations

- **Risk**: Subscribers in a tight reconnect loop hammer the daemon
  during recovery.
  **Mitigation**: Capped exponential backoff with jitter caps the
  worst case at ~2 reconnects/second steady state, well under the
  daemon's accept-rate budget; the `max_reconnect_attempts=10`
  ceiling bounds the per-subscriber load.
- **Risk**: A planned-downtime workflow that depended on the silent
  fallback regresses to dropped tuples after this change.
  **Mitigation**: Documented opt-in env (`NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1`)
  preserves the legacy path. The CHANGELOG entry must call out the
  default flip.
- **Risk**: `EventStreamUnavailable` propagates into callers that
  expected the legacy infinite-block-on-failure behaviour.
  **Mitigation**: The reconnect wrapper is opt-in via the default-on
  `reconnect=True` parameter; callers that explicitly pass
  `reconnect=False` get the legacy single-subscribe semantics.

### Failure Modes

- Daemon down + bridge in daemon mode: visible, one log line per
  dropped emission, hook stdout still correct (transparent allow);
  diagnosable via `nx doctor --check-bridge` which already counts
  recent hook events.
- Daemon flapping: bridge drops the tuples during each window,
  subscribers reconnect each cycle with jittered backoff. Both
  surfaces are visible via structlog.
- Wrapper retry exhaustion: typed `EventStreamUnavailable` raised
  with the last-seen cursor in the message. Supervising loop logs
  + decides (restart, escalate, exit).
- Operator override: setting `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` is
  reflected in `nx doctor --check-bridge` output as a one-line
  warning so the override doesn't get forgotten.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions verified (2026-05-16)
- [x] `find_t2_daemon` PID probe (nexus-j6dj), shipped PR #826
- [x] `NX_BRIDGE_DISABLE` env handling (nexus-7zvp), shipped PR #822

### Minimum Viable Validation

A two-phase integration test:

1. Start a T2 daemon on a tmp config_dir. Open a `T2Client.event_stream`
   subscription. Insert 5 tuples. Send the daemon SIGTERM; the client's
   stream closes. Within the reconnect budget, restart the daemon
   in-test and insert 5 more tuples. Assert the wrapper yields all
   10 tuples in cursor-sorted order with no duplicates.

2. With the daemon stopped and `NX_STORAGE_MODE=daemon`, invoke a
   bridge emit. Assert: zero rows written to either `tuples.db` or
   the daemon's Chroma; one `hook_bridge_emit_drop_daemon_unavailable`
   structlog event; hook stdout matches RF-2 expectations.

### Phase 1: Code Implementation

#### Step 1: EventStream reconnect wrapper

Add the wrapper to `T2Client.event_stream` with the policy
parameters above. Surface `EventStreamUnavailable` as a public
exception from `nexus.daemon.t2_client`. Update the docstring with
the contract. Update `tests/daemon/test_event_stream.py` with the
SIGTERM-and-restart integration test.

#### Step 2: Bridge fail-closed default

Add `_direct_fallback_allowed()` to `hook_bridge.py`. Wire the
gate into `_emit_routed` so direct fallback is skipped under
daemon mode unless the env opt-in is set. Add the structlog event
on drop. Add a unit test in `tests/cockpit/test_hook_bridge_daemon_routing.py`
that drives the daemon-failure path and asserts the drop event +
absence of direct writes.

#### Step 3: Operator surfacing

Update `nx doctor --check-bridge` to:
- Report whether `NX_BRIDGE_ALLOW_DIRECT_FALLBACK` is set (operator
  override visible).
- Surface the last hook-bridge drop event from `~/.config/nexus/logs/`
  if any in the last 24h.

### Phase 2: Operational Activation

No deployment changes. The new env is documented in
`docs/cli-reference.md` and the relevant skill docs.

#### Activation Step 1: CHANGELOG entries

Call out the bridge fail-closed default flip in both
`CHANGELOG.md` and `nx/CHANGELOG.md` with the env-opt-in escape
hatch and the operator-visible drop event name.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `NX_BRIDGE_ALLOW_DIRECT_FALLBACK` env opt-in | N/A, env var, no persistent resource | `nx doctor --check-bridge` reports current state | Unset env to revert | `nx doctor` surfaces override | N/A |
| `EventStreamUnavailable` exception | N/A, exception class | Module docstring | N/A | Caller-side regression tests | N/A |
| Hook drop log events | Already rotated via `daemon` mode log handler (nexus-uuuh) | `grep hook_bridge_emit_drop_daemon_unavailable ~/.config/nexus/logs/daemon.log` | Log rotation handles | `nx doctor --check-bridge` surfaces | RotatingFileHandler keeps 5 × 10MB |

### New Dependencies

None. Both surfaces use existing modules.

## Test Plan

- **Scenario**: Subscriber reconnect across SIGTERM-restart cycle,
  **Verify**: no duplicates, no gaps, no leaked socket descriptors.
- **Scenario**: Subscriber reconnect attempts exhaust budget,
  **Verify**: `EventStreamUnavailable` raised with last-seen cursor
  in message; no infinite retry loop.
- **Scenario**: Reconnect jitter spread across N concurrent
  subscribers, **Verify**: backoff windows do not collapse to a
  thundering herd (statistical check across, say, 20 subscribers
  driving the same daemon-restart event).
- **Scenario**: Bridge fail-closed under daemon mode, **Verify**:
  zero rows in `tuples.db` and the daemon's Chroma collection; one
  structlog event per drop; hook stdout unchanged.
- **Scenario**: Bridge fail-open under direct mode, **Verify**:
  legacy direct emission still works when `NX_STORAGE_MODE` is unset.
- **Scenario**: Operator override re-enables fail-open,
  **Verify**: setting `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` restores
  the direct-mode fallback path; `nx doctor --check-bridge` reports
  the override.
- **Scenario**: Subscriber that explicitly opts out of reconnect,
  **Verify**: `reconnect=False` reproduces the legacy single-subscribe
  exception-on-close behaviour for callers that need it.

## Validation

### Testing Strategy

1. **Scenario**: Real-daemon SIGTERM + restart with active subscriber.
   **Expected**: all events delivered exactly once across cursor.
2. **Scenario**: Real-daemon stopped + bridge emit.
   **Expected**: structlog drop event, zero SQLite writes, zero
   Chroma writes.
3. **Scenario**: `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` with stopped
   daemon.
   **Expected**: legacy direct fallback fires; SQLite row written;
   Chroma vector written through the bridge's own client.
4. **Scenario**: Subscriber retry-budget exhaustion (daemon never
   starts).
   **Expected**: `EventStreamUnavailable` raised after ~30 s with
   cursor in the message.

### Performance Expectations

Reconnect budget bounded at ~30 s (10 attempts × max 8 s backoff).
The wrapper's per-event overhead is one cursor assignment; negligible
vs. the existing per-event JSON decode. Backoff jitter is 16 random
floats per reconnect, well under any measurable cost.

## Finalization Gate

> Complete each item with a written response before marking this RDR
> as **Accepted**. Written responses prevent rubber-stamping and
> produce a review record.

### Contradiction Check

No contradictions found between research findings, design
principles, and proposed solution. The proposed solution uses
substrate that the research-findings section verified (cursor
parameter, PID-liveness probe, action_idempotency, `NX_STORAGE_MODE`
env gate, structlog rotation).

### Assumption Verification

All three Critical Assumptions are verified as of 2026-05-16:

1. Bridge-side daemon-RPC p99 latency: local-mode PASS at 48.47 ms
   under N=1000 sequential `tuplespace.out` (target 250 ms).
   Cloud-mode caveat documented (Voyage RTT may push p99 closer
   to target); implementation must size T2Client RPC timeout
   accordingly. Spike methodology in Research Findings.
2. Subscriber reconnect-gap tolerance: VERIFIED via Source Search.
   No production consumer of `event_stream.subscribe` exists yet;
   the planned binding-watcher migrant uses cursor-driven catch-up
   on 50 ms polling that absorbs gaps without loss.
3. No production reader depends on silent direct-mode fallback:
   VERIFIED via Source Search. All seven `orb_bridge_*.py` scripts
   wrap `emit()` in `try/except + log + exit 0` and do not check
   the return value. The opt-in env preserves the interactive-
   setup escape hatch.

No remaining blockers from the assumption gate.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `T2Client.event_stream` resumption with `since_cursor` | nexus.daemon.t2_client (in-tree) | Source Search confirmed cursor is a server-side filter on `rowid > since_cursor`. |
| `asyncio.IncompleteReadError` on graceful server close | stdlib | Source Search confirmed. |
| `find_t2_daemon` PID probe | nexus.daemon.discovery (in-tree) | Source Search confirmed PR #826. |
| `sqlite3.connect` with `mode=ro` URI | stdlib | Already used elsewhere in tree. |
| `tuplespace.out` end-to-end latency | in-tree RPC | Spike 2026-05-16: p99 48.47 ms local-mode (N=1000, warmup=5). |

### Scope Verification

Minimum Viable Validation is the two-phase integration test in §
Phase 1. Both tests must pass before implementation merges; neither
is deferred.

### Cross-Cutting Concerns

- **Versioning**: No wire-protocol change. `BRIDGE_API_VERSION`
  unaffected. Add a `RECONNECT_WRAPPER_VERSION` constant in
  `t2_client.py` if future iterations need explicit drift checks;
  not required for v1.
- **Build tool compatibility**: N/A (no new dependencies).
- **Licensing**: N/A.
- **Deployment model**: Unchanged. The new env is opt-in.
- **IDE compatibility**: N/A.
- **Incremental adoption**: `reconnect=False` keeps the legacy
  single-subscribe semantics available; `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1`
  keeps the legacy fail-open behaviour available.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: Reconnect wrapper holds one int (last
  cursor) and one generator at a time; no buffering of events.
  N/A as a concern.

### Proportionality

The RDR is right-sized for the work: two related policy decisions
plus a thin wrapper. No deferred sections, no premature scaling
discussion. Could be trimmed further only by dropping the
Alternatives discussion, but that would erase the operator-facing
rationale.

## References

- RDR-110 `docs/rdr/rdr-110-semantic-tuple-space.md`, tuplespace
  substrate.
- RDR-111 `docs/rdr/rdr-111-orb-agentic-cockpit-substrate.md`,
  ORB hook bridge.
- RDR-112 `docs/rdr/rdr-112-storage-as-service-container-boundary.md`
 , daemon single-writer boundary.
- RDR-113 `docs/rdr/rdr-113-host-trust-model.md`, host-trust model.
- Postmortem `docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`.
- Bead `nexus-sddg`, original critique finding for EventStream
  reconnect.
- Bead `nexus-mx0z`, original critique finding for bridge fallback.
- Bead `nexus-j6dj` (PR #826), PID-liveness probe substrate.
- Bead `nexus-7zvp` (PR #822), `NX_BRIDGE_DISABLE` wiring.
- Bead `nexus-8wvs` (PR #824), `action_idempotency` table.

## Revision History

(Gate findings are appended here in dated subsections after each
`/nx:rdr-gate` run.)
