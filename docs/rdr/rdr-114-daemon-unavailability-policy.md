---
title: "Daemon unavailability policy: EventStream reconnect contract + bridge fail-closed-vs-open"
id: RDR-114
type: Architecture
status: closed
priority: medium
author: Hellblazer
reviewed-by: self
created: 2026-05-16
accepted_date: 2026-05-16
closed_date: 2026-05-16
close_reason: implemented
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
The cursor mechanism (`since_cursor`) is round-trippable, so
**at-least-once** delivery is achievable on resumption (the server
sends each event with its cursor; a caller that records the last-
seen cursor and reconnects with `since_cursor=last` replays gap-
period events). Exactly-once requires caller-side dedup via the
`action_idempotency` table (RDR-111 / nexus-8wvs) or an equivalent
idempotency key keyed on `tuple_id`. What is missing today is a
written contract for *who* retries, *with what backoff*, *how many
times*, and *what happens after exhaustion*. Cockpit-side binding
watchers, the planned auditor agent, and any third-party subscriber
would otherwise each re-derive a slightly different policy, with
cross-implementation drift.

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
- `NX_BRIDGE_DISABLE` opt-out env (already wired by nexus-7zvp).
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
  delivers strictly events with `rowid > N`. **Delivery is at-least-
  once**, not exactly-once: the server advances `last_emitted` as it
  writes each frame (`event_stream.py:271..273, 304..305`), but the
  client cursor is updated only when the caller persists it after
  processing. A caller that processes an event and then crashes
  before flushing the cursor will see that event re-delivered on
  reconnect. Callers requiring exactly-once must guard via the
  `action_idempotency` table (nexus-8wvs) keyed on `tuple_id`, or an
  equivalent external dedup surface.
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

  *Re-verification gate at migration*: when `_BindingWatcher`
  migrates from SQL polling to `event_stream.subscribe`, A2 must
  be re-verified against the watcher's then-current reactive
  latency contract before the migration merges. The migration
  bead must reference this RDR and re-evaluate the gap tolerance
  in the context of any binding actions whose downstream
  deadlines have changed since this RDR was drafted.

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
attempts 10; nominal sum ~48 s, range 36-60 s with jitter), jittered
by ±25 % to avoid synchronised
reconnect storms when multiple subscribers race the same daemon
restart. After exhaustion the wrapper raises a typed
`EventStreamUnavailable` exception with the last-seen cursor in the
message so the operator (or supervising loop) can decide what to do.
The contract is **explicitly documented in the client-library
docstring + the daemon CLI reference** so all subscribers, current
and future, share one policy.

**Gap 2 (bridge fail-closed):** When the bridge's routing
discriminant `_ROUTING_TBA` is `"daemon"` (the shipped default), the
bridge's direct-mode fallback is **off by default**. Daemon-RPC
failure logs a typed event (`hook_bridge_emit_drop_rpc_failed`, with
the hook type, subspace, and exception class) and returns without
writing anywhere. The hook script still produces correct stdout
(RF-2 transparent allow) so user-facing tools never see the drop.
An opt-in env `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` re-enables the
legacy fail-open path for operators who knowingly accept the
WAL-contention risk during planned daemon downtime. In legacy
direct routing (`_ROUTING_TBA = "direct"`, available via the
constant; not the shipped default) the bridge writes directly and
no fallback is in play.

**Gate keys off the routing discriminant, not `NX_STORAGE_MODE`.**
`_ROUTING_TBA` is the same constant the bridge already consults to
decide whether to try the daemon path at all (`hook_bridge.py:128`,
checked at `:334`). Keying the new fail-closed gate off the same
constant guarantees the gate fires in exactly the cases where the
daemon path is attempted, even if `NX_STORAGE_MODE` is unset (the
common developer-machine workflow with a daemon running but no env
exported). `NX_STORAGE_MODE` continues to gate `reject_under_daemon_mode`
for direct-T2-open paths inside the wheel; the two envs are
orthogonal.

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

    The gate keys off _ROUTING_TBA (the routing discriminant the
    bridge already consults at line 334), NOT NX_STORAGE_MODE:

    - When _ROUTING_TBA != "daemon" (legacy direct routing, not the
      shipped default): fallback irrelevant, direct is the supported
      path. Return True.
    - When _ROUTING_TBA == "daemon" AND NX_BRIDGE_ALLOW_DIRECT_FALLBACK
      is truthy: operator opt-in to the legacy fail-open path
      (planned daemon downtime, CI without a daemon, etc.).
      Return True.
    - When _ROUTING_TBA == "daemon" AND NX_BRIDGE_ALLOW_DIRECT_FALLBACK
      is unset / falsy: fail-closed default. Return False.

    Interaction with NX_BRIDGE_DISABLE: the privacy opt-out
    (_bridge_disabled, hook_bridge.py:590..604) is checked earlier in
    emit() and exits before _emit_routed runs. NX_BRIDGE_ALLOW_DIRECT_FALLBACK
    has no effect when NX_BRIDGE_DISABLE is also set.
    """

def _emit_routed(...) -> str:
    ...
    if daemon_failed:
        if not _direct_fallback_allowed():
            _log.warning(
                "hook_bridge_emit_drop_rpc_failed",
                hook_type=hook_type, subspace=subspace,
                error=str(daemon_exc),
            )
            return "skipped-rpc-failed"
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

**Bounded retry budget** (~48 s nominal, range 36-60 s with the
±25 % jitter) because the daemon is intended to be HA-supervised
(launchd / systemd KeepAlive + RestartSec). If the daemon is down
for longer, that's an operator-attention event, not a thing the
client should paper over. The 10-attempt geometric series
``0.25 + 0.5 + 1 + 2 + 4 + 8 + 8 + 8 + 8 + 8 = 47.75 s`` (the
attempts saturate at the 8 s cap from attempt 5 onward) gives the
budget; jitter expands it to ~36-60 s. Supervisor-restart asymmetry
note (Layer 3 gate finding):

- **systemd**: `RestartSec=5s` + `SuccessExitStatus=143`. A clean
  SIGTERM does not trigger restart; a crash respawns after 5 s.
  The ~48 s nominal budget comfortably covers a crash-restart cycle.
- **launchd**: `KeepAlive.Crashed: true` (NOT unconditional
  KeepAlive). A clean SIGTERM is not supervised. A crash triggers
  respawn but launchd's default `ThrottleInterval` is 10 s, so a
  crash-restart cycle can be up to 10 s + daemon-init time. Under
  the ~48 s budget the client gets 4-5 restart attempts before
  exhaustion. Operators who restart the daemon via `nx daemon t2
  stop && nx daemon t2 start` on macOS are responsible for
  starting it back themselves; planned restarts are not auto-
  supervised under launchd today.

This calibration is sufficient for systemd-managed deployments and
acceptable for the macOS interactive workflow. Tightening it would
require either flipping the plist to unconditional `KeepAlive: true`
(out of scope for RDR-114) or extending the retry budget. The
~48 s nominal budget is the proposed default; callers needing
different semantics can pass `max_reconnect_attempts` /
`max_backoff_seconds` explicitly.

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

- Daemon down + bridge in daemon mode (UDS connect refused or
  discovery file absent): visible, one `hook_bridge_emit_drop_rpc_failed`
  log per dropped emission, hook stdout still correct (transparent
  allow); diagnosable via `nx doctor --check-bridge` which already
  counts recent hook events.
- Partial daemon failure (UDS accepts, daemon process running,
  internal RPC hangs or returns error, e.g. `tuples.db` locked
  during a WAL checkpoint stall or migration runner): bridge sees
  a typed RPC error or socket-level RPC-timeout (Step 4) and emits
  the same `hook_bridge_emit_drop_rpc_failed` event with the
  exception class in the payload. The log event is intentionally
  named after the RPC outcome, not the daemon process state, so
  partial-failure does not falsely report the daemon as "down."
- Daemon flapping: bridge drops the tuples during each window,
  subscribers reconnect each cycle with jittered backoff. Both
  surfaces are visible via structlog.
- Wrapper retry exhaustion: typed `EventStreamUnavailable` raised
  with the last-seen cursor in the message. Supervising loop logs
  + decides (restart, escalate, exit).
- Operator override: setting `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` is
  reflected in `nx doctor --check-bridge` output as a one-line
  warning so the override doesn't get forgotten. When combined
  with `NX_BRIDGE_DISABLE=1`, doctor warns that the latter exits
  first and the former has no effect.

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
   in-test, then *wait for daemon readiness* (discovery file present
   AND PID probe passes) before inserting 5 more tuples. Assert the
   wrapper yields all 10 tuples in cursor-sorted order with no
   duplicates and no events lost.

   *Readiness wait*: avoids a flaky race where the wrapper's first
   retry can fire before the new daemon has written its discovery
   file. The retry would back off and the test would interpret the
   delay as a wrapper bug. Standard pattern: poll the discovery
   file with PID probe up to a short timeout (~2 s) before driving
   the second insertion batch.

2. With the daemon stopped and `_ROUTING_TBA == "daemon"` (shipped
   default), invoke a bridge emit. Assert: zero rows written to
   either `tuples.db` or the daemon's Chroma; one
   `hook_bridge_emit_drop_rpc_failed` structlog event; hook stdout
   matches RF-2 expectations.

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
- Warn when `NX_BRIDGE_ALLOW_DIRECT_FALLBACK` and `NX_BRIDGE_DISABLE`
  are both set (latter exits first; former has no effect).

#### Step 4: T2Client RPC timeout (closes A1 cloud-mode caveat)

`T2Client._recvexactly` currently has no socket-level timeout
(verified by Layer 3 critique). Wire an explicit RPC timeout (default
5 s, override via `T2Client(rpc_timeout_seconds=...)`) on the call
path. This is required for the bridge fail-closed default to be
hot-path-safe under cloud-mode: without the timeout, a hung
`tuplespace.out` would block the bridge subprocess past Claude
Code's hook deadline. With it, the bridge waits up to 5 s, sees a
typed timeout exception, then fails closed (or falls open under the
opt-in env). Implementation note: must propagate as a typed
exception class distinguishable from `ConnectionRefusedError` so
the reconnect wrapper (Step 1) does not misclassify a hung daemon
as "daemon gone."

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
| `NX_BRIDGE_ALLOW_DIRECT_FALLBACK` env opt-in | N/A, env var, no persistent resource | `nx doctor --check-bridge` reports current state and warns when set together with `NX_BRIDGE_DISABLE` (combination is no-op because BRIDGE_DISABLE exits first) | Unset env to revert | `nx doctor` surfaces override | N/A |
| `EventStreamUnavailable` exception | N/A, exception class | Module docstring documenting at-least-once delivery + caller-side dedup expectation | N/A | Caller-side regression tests | N/A |
| Hook drop log events | Already rotated via `daemon` mode log handler (nexus-uuuh) | `grep hook_bridge_emit_drop_rpc_failed ~/.config/nexus/logs/daemon.log` | Log rotation handles | `nx doctor --check-bridge` surfaces | RotatingFileHandler keeps 5 × 10MB |

### New Dependencies

None. Both surfaces use existing modules.

## Test Plan

- **Scenario**: Subscriber reconnect across SIGTERM-restart cycle,
  **Verify**: no duplicates, no gaps, no leaked socket descriptors.
- **Scenario**: Subscriber reconnect attempts exhaust budget,
  **Verify**: `EventStreamUnavailable` raised with last-seen cursor
  in message; no infinite retry loop.
- **Scenario**: Reconnect jitter spread across N concurrent
  subscribers, **Verify**: no more than 2 of 20 subscribers reconnect
  within the same 250 ms window as measured by reconnect timestamp
  distribution (test uses a seeded RNG for determinism; passing
  threshold matches the ±25 % jitter spec).
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
   **Expected**: all events delivered (at-least-once; no gaps in
   cursor ordering; duplicates possible only if the caller crashes
   between yield and cursor-persist, guarded via
   `action_idempotency` when exactly-once is required by the caller).
2. **Scenario**: Real-daemon stopped + bridge emit.
   **Expected**: structlog drop event, zero SQLite writes, zero
   Chroma writes.
3. **Scenario**: `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1` with stopped
   daemon.
   **Expected**: legacy direct fallback fires; SQLite row written;
   Chroma vector written through the bridge's own client.
4. **Scenario**: Subscriber retry-budget exhaustion (daemon never
   starts).
   **Expected**: `EventStreamUnavailable` raised after the budget
   sum of 10 backoffs (~36-60 s with jitter) with cursor in the
   message.

### Performance Expectations

Reconnect budget bounded at ~48 s nominal, range 36-60 s with the
±25 % jitter. The wrapper's per-event overhead is one cursor
assignment; negligible vs. the existing per-event JSON decode.
Backoff jitter is 16 random floats per reconnect, well under any
measurable cost.

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

### 2026-05-16, Gate round 1 (Layer 3 substantive critic): BLOCKED, fixed inline

Verdict from `/nx:rdr-gate rdr-114`: BLOCKED with 2 critical and 3
significant issues. All resolved inline:

**Critical 1: Exactly-once delivery claim overstated.** The server
advances `last_emitted` when writing each frame, but the client
cursor is persisted by the caller after processing. A caller crash
between yield and cursor-persist re-delivers the event. Fixed by
relabelling delivery as "at-least-once" throughout the RDR and
adding the `action_idempotency` pointer for callers requiring
exactly-once. Affects Problem Statement (Gap 1) and Key Discoveries.

**Critical 2: Fail-closed gate keyed off wrong discriminant.** The
bridge's daemon-route decision uses `_ROUTING_TBA == "daemon"`
(module constant, always `"daemon"` in shipped builds), but the
proposed `_direct_fallback_allowed()` gated on `NX_STORAGE_MODE`.
With `NX_STORAGE_MODE` unset (common dev-workflow scenario), the
gate would not fire, leaving silent fallback in place. Fixed by
keying the gate off `_ROUTING_TBA` directly, matching the routing
decision. Also documented the orthogonal relationship with
`NX_STORAGE_MODE` (which continues to gate `reject_under_daemon_mode`
inside the wheel). Affects Proposed Solution Gap 2 + Technical
Design pseudocode.

**Significant 1: Supervisor restart asymmetry.** Added explicit
note on launchd `KeepAlive.Crashed` (NOT unconditional) vs systemd
`RestartSec=5s`. The ~48 s reconnect budget (geometric sum of the
capped backoff series) covers 4-5 crash-restart cycles on macOS
at default `ThrottleInterval=10s`; planned restarts on macOS are
operator-driven. Affects Decision Rationale.

**Significant 2: `NX_BRIDGE_DISABLE × NX_BRIDGE_ALLOW_DIRECT_FALLBACK`
interaction.** Documented in the Technical Design docstring and in
the Day 2 Operations table; `nx doctor --check-bridge` warns when
both are set.

**Significant 3: A2 has no migration trigger.** Added an explicit
re-verification gate to A2 in Critical Assumptions: when
`_BindingWatcher` migrates from SQL polling to
`event_stream.subscribe`, A2 must be re-verified against the
watcher's then-current reactive latency contract before merging.

**Observation: partial daemon failure.** Renamed the structlog drop
event from `hook_bridge_emit_drop_daemon_unavailable` to
`hook_bridge_emit_drop_rpc_failed` to be accurate in the partial-
failure case (daemon process running, RPC hangs / errors). Added
the partial-failure scenario to Failure Modes.

**Observation: T2Client has no RPC timeout.** Added Phase 1 Step 4
to wire an explicit RPC timeout (default 5 s, override via
`T2Client(rpc_timeout_seconds=...)`) propagated as a typed exception
distinguishable from `ConnectionRefusedError` so the reconnect
wrapper does not misclassify a hung daemon as "daemon gone."

**Observation: thundering-herd test scenario.** Replaced the
"statistical check" wording with a concrete pass criterion
("no more than 2 of 20 subscribers reconnect within the same 250 ms
window, with a seeded RNG").

**Observation: MVV readiness sequencing.** Spelled out the post-
restart daemon-readiness wait (discovery file + PID probe) before
driving the second insertion batch, to avoid a flaky race.

Layer 1 (structure) and Layer 2 (assumption audit) both PASS. Ready
for gate round 2.

### 2026-05-16, Gate round 2 (Layer 3 substantive critic): BLOCKED, fixed inline

Verdict from re-run of `/nx:rdr-gate rdr-114`: 1 critical and 2
observations.

**Critical 1: Residual "exactly once" in Validation Testing
Strategy Scenario 1.** Round 1's at-least-once relabel was applied
to Problem Statement, Key Discoveries, Day 2 table, and the round-1
revision entry, but not to the Validation Testing Strategy at
line 740. Fixed by replacing the expected outcome with the full
at-least-once statement plus the `action_idempotency` pointer.

**Observation: `_BRIDGE_DISABLE` typo on line 89.** Substrate-list
bullet referenced what looked like an internal constant name
instead of the public env var `NX_BRIDGE_DISABLE`. Fixed.

**Observation: Step 4 sizing + log event rename are clean.**
Critic confirmed the new T2Client RPC timeout is correctly scoped
and the typed exception is distinguishable from
`ConnectionRefusedError`. Critic also confirmed the renamed log
event has no consumer-breakage risk (neither old nor new name
exists in source yet).

Ready for gate round 3.

### 2026-05-16, Gate round 3 (Layer 3 substantive critic): PASSED

Verdict from re-run of `/nx:rdr-gate rdr-114`: 0 critical, 0
significant, 0 observations. Both round-2 fixes landed cleanly:

- Zero remaining "exactly once" (unhyphenated) claims in the body;
  the five `exactly-once` (hyphenated) hits are all caller-side
  qualifiers, not system guarantees.
- Line 89 substrate-list bullet now reads `NX_BRIDGE_DISABLE`
  correctly.
- Revision History honestly describes the round-1 and round-2
  findings and fixes.
- Validation Testing Strategy Scenario 1 coherence intact;
  at-least-once with `action_idempotency` pointer is consistent
  with Research Findings.

Gate complete. Ready for `/nx:rdr-accept rdr-114`.

### 2026-05-16, Post-implementation review round 2: budget math corrected

After Phase 1 shipped (Steps 1-4 + review follow-ups + CHANGELOG),
a second-pass code review (nx:code-review-expert dispatched manually)
caught a conservative arithmetic error in the reconnect budget claim.

The original docstring said "10 attempts × max 8 s ≈ a 30 s budget"
which is arithmetically incoherent (10 × 8 = 80, not 30). The
actual geometric series at the capped exponential defaults is
``0.25 + 0.5 + 1 + 2 + 4 + 8 + 8 + 8 + 8 + 8 = 47.75 s`` nominal,
expanding to ~36-60 s with ±25 % jitter.

The error was conservative (operators get more retry budget than
the docstring promised, never less), but the math was confusing
to a future reader trying to verify calibration. Fixed in:

- `src/nexus/daemon/t2_client.py` `_jittered_backoff_seconds` docstring
- `src/nexus/daemon/t2_client.py` `event_stream` docstring
- `CHANGELOG.md` Unreleased section RDR-114 entry
- This RDR file (Approach + Decision Rationale + Test Plan +
  Performance Expectations + Revision History round-1 entry)

No code change: the implementation was always correct; only the
prose around the calibration claim was wrong.



