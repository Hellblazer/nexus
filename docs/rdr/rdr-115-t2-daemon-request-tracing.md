---
title: "T2 daemon request tracing: correlation IDs across client → daemon → store call"
id: RDR-115
type: Architecture
status: draft
priority: medium
author: Hellblazer
reviewed-by: self
created: 2026-05-17
accepted_date:
related_issues: [nexus-6m9i]
---

# RDR-115: T2 daemon request tracing: correlation IDs across client → daemon → store call

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The third 360° review (umbrella `nexus-6m9i`, dimension-2
observability, scratch `7c5abccb`) flagged that a production incident
spanning the T2 client, the daemon dispatch, and the underlying
domain store cannot be reconstructed from logs alone. RPC handlers
log at the daemon-layer; store methods log at the data-layer;
clients log at the call-site. None of these log lines share a
common identifier that ties a single user-initiated call to its
fan-out across the substrate.

### Enumerated gaps to close

#### Gap 1: No request/correlation ID threaded through the RPC dispatch

`T2Client.call("memory.put", args)` issues a frame; the daemon's
`_dispatch_store_rpc` accepts the frame; the store method runs; the
response frame returns. Each step writes its own structured-log
event (`t2_client_connected`, `rpc_handler_error`,
`memory_put_succeeded`, etc.). There is no key common to all of
them. An operator running `grep` against the structured-log file
cannot reconstruct the sequence that processed call #N out of
several thousand interleaved RPCs. The semaphore in HR-1 + the
multi-thread executor mean concurrent calls are the rule, not the
exception.

#### Gap 2: Blocking-take returns None for three distinct reasons with no log differentiation

`blocking_take` returns `None` when (a) the deadline elapsed
naturally, (b) the service is shutting down and `_wake_stop` was
observed at the loop-top check, (c) the sqlite connection was
closed underneath the in-flight call (third 360° SEC handling).
Each of these is operationally distinct; an operator triaging
"why are my agents not getting work?" cannot tell the three apart
from the client-side `None` return alone.

(Note: semaphore-exhaustion paths — both the global HR-1 cap and
the per-claimant SEC-4 cap — raise `BlockingTakeResourceExhausted`,
a typed exception that already survives wire traversal. Those are
not None-returning paths and are distinguishable today.)

#### Gap 3: No latency breadcrumb in the standard RPC log

`rpc_handler_error` carries `op`, `exc_type`, `exc` (and the
traceback for UDS). `memory_put_succeeded` carries domain-level
fields. Neither carries the elapsed time spent in the executor.
Capacity-planning queries against the structured-log corpus
("which ops are growing slower week-over-week") have no signal.

## Context

### Background

Discovered during the 2026-05-17 third 360° review. The first 360°
(`nexus-ku5k`, closed) covered architecture / line-level / security /
test quality. The second 360° (`nexus-ftpm`, closed) covered
concurrency / resource lifecycle / error paths / logging discipline
(event names, throttling, exc_info) and 12 other dimensions. The
third 360° (`nexus-6m9i`, closed) re-audited with 14 new lenses;
observability surfaced these correlation-ID + latency-breadcrumb
gaps that prior rounds did not scan because their logging-discipline
work focused on event naming and per-event content, not cross-event
correlation.

This RDR captures the design decision deferred from the third 360°
remediation. The behavioural fixes for the dimension are split:
the immediate items (rich `ping` response, `nx daemon t2 doctor`
subcommand) shipped under `nexus-6m9i`; the structural change
(correlation IDs threaded through dispatch + a latency breadcrumb
in every store-call log line) is captured here for proper design.

### Technical Environment

- Python 3.12+. `structlog.get_logger(__name__)` everywhere.
- Daemon-side dispatch in `t2_daemon._dispatch_store_rpc` runs
  store methods in `loop.run_in_executor(None, ...)`; logs interleave
  across the thread pool.
- Domain stores (`MemoryStore`, `PlanLibrary`, `Telemetry`, ...) use
  their own `_log = structlog.get_logger(__name__)` instances.
- `T2Client.call` is synchronous; no native async context.
- No OpenTelemetry / W3C TraceContext today; introducing one is
  in scope for this RDR's design space.

## Research Findings

### Investigation

Round 1 research complete (2026-05-17). Full evidence in T2:
`nexus_rdr/115-research-1` through `115-research-5`. Sources
consulted:

- `src/nexus/daemon/t2_daemon.py` (dispatch loop, ping response,
  `_dispatch_store_rpc` error frame shape)
- `src/nexus/daemon/t2_client.py` (`call`, `_call`,
  `_SocketConnection` frame protocol — `t2_json_loads` passes
  unknown top-level keys through without error)
- `src/nexus/daemon/tuplespace_service.py` (blocking_take's
  three actual None-return paths — enumeration below)
- All nine `src/nexus/db/t2/*.py` stores audited for logger
  construction pattern
- structlog `bind_contextvars` source + Python 3.12 asyncio
  `loop.run_in_executor` behaviour empirically tested in the
  project's uv env

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `asyncio.AbstractEventLoop.run_in_executor` | Yes | Does NOT propagate the current task's `contextvars.Context` to the thread-pool worker by default in Python 3.12. The asyncio implementation does not call `copy_context()` for the default executor path. Verified empirically. |
| `contextvars.copy_context()` | Yes | When the dispatch wraps the executor callable as `ctx = copy_context(); run_in_executor(None, lambda: ctx.run(fn))`, contextvars set in the asyncio task DO propagate to the worker thread. |
| `structlog.contextvars.bind_contextvars` | Yes | Stores its state in a single `contextvars.ContextVar`; processor `merge_contextvars` (already in the project's processor chain) pulls keys at log time. Inherits the propagation semantics of the underlying `contextvars` module. |
| `t2_json_loads` / `_sock_read_frame` | Yes | No schema validation on top-level keys; unknown keys pass through. Adding `request_id` is fully backward-compatible — old daemons silently ignore client-supplied IDs; old clients that don't send any trigger daemon-side minting. No protocol version bump required. |

### Key Discoveries

- **Refuted** (A1): the initial scaffolded "subtle but likely
  works" framing for `bind_contextvars` propagation across
  `run_in_executor` was wrong. Without explicit
  `copy_context().run(fn)`, the worker thread sees `None` for
  every bound key. This is a *mandatory* design constraint, not
  a nice-to-have. See `nexus_rdr/115-research-1`.
- **Verified** (A2): UUID-v4 string is 36 bytes; with JSON key
  overhead (`"request_id":"…"`) the total per-frame cost is ~54
  bytes. Production frames are dominated by content payload
  (largest observed ~60 KB → 0.057 MiB); 1 MiB cap is never
  approached. See `nexus_rdr/115-research-2`.
- **Verified** (A3): all 9 stores under `src/nexus/db/t2/*.py`
  use `_log = structlog.get_logger(...)` — none use
  `logging.getLogger`. The contextvar-merging processor is
  global to all structlog loggers, so zero source edits per
  store. One housekeeping note: 8 of 9 stores omit `__name__`
  on the `get_logger` call (only `catalog_store.py` passes it);
  this does not block the design but should be fixed during
  Phase 1. See `nexus_rdr/115-research-3`.
- **Verified** (Gap 2 enumeration): blocking_take has THREE
  None-returning paths in the current code (not four as the
  draft claimed). The semaphore-exhaustion cases raise
  `BlockingTakeResourceExhausted` and are already
  distinguishable. The three None paths are: (a) shutdown
  observed at the loop-top `_wake_stop.is_set()` check, (b)
  `sqlite3.ProgrammingError` from the conn-closed-race caught
  in the take attempt, (c) `remaining <= 0` deadline elapsed.
  See `nexus_rdr/115-research-4`.
- **Verified** (no competing tracing infra): no
  `opentelemetry-*` packages in `pyproject.toml`. The
  Alternatives-Considered rejection of OpenTelemetry stands;
  no existing tracing surface would conflict with structlog
  contextvars. See `nexus_rdr/115-research-5`.

### Critical Assumptions

- [x] structlog's `bind_contextvars` propagates correctly across
  `asyncio.run_in_executor` boundaries — **Status**: REFUTED —
  **Method**: Spike. The dispatch layer MUST wrap the executor
  callable with `contextvars.copy_context().run(...)` for keys
  to cross the thread-pool boundary. Without this the feature
  silently emits no request_ids in store logs.
- [x] Adding a `request_id` field to every RPC frame does not
  push any production payload over the 1 MiB frame cap —
  **Status**: Verified — **Method**: Source Search. 54-byte
  overhead vs. 1 MiB cap; production frames dominated by
  content payload (largest observed 60 KB).
- [x] Domain-store logs can be retrofitted to read the active
  contextvars without source changes to each store —
  **Status**: Verified — **Method**: Source Search. All 9
  stores under `src/nexus/db/t2/*.py` use structlog; the
  merging processor is global to all loggers. Pre-existing
  housekeeping note: 8 of 9 omit `__name__` on
  `get_logger` — fix during Phase 1.

## Proposed Solution

### Approach

Two-layer approach:

1. **Wire-level**: every RPC frame carries an optional
   `request_id: str` (UUID-v4 or short shake_128). The client mints
   one per `call()`; if missing, the daemon mints one and echoes it
   on the response frame so clients without their own IDs can still
   recover one.
2. **Log-level**: the daemon's dispatch handler binds `request_id`
   + `op` + start-monotonic-perf-counter into a contextvar before
   the store method runs; the domain stores' loggers pick the
   contextvar values up automatically via a structlog processor.
   Elapsed-time is logged on the exit path under an `rpc_handler_ok`
   event (paired with the existing `rpc_handler_error`).

For Gap 2, `blocking_take`'s three return-None paths each emit a
distinct DEBUG-level log event (`blocking_take_shutdown_observed`,
`blocking_take_conn_closed_during_take`,
`blocking_take_deadline_elapsed`) before returning, carrying the
same `request_id`. Operators triaging the "agents not getting work"
symptom can grep on `request_id` and see the categorised return
reason. (Semaphore exhaustion raises `BlockingTakeResourceExhausted`,
a typed exception — distinguishable already; not a None path.)

### Technical Design

Locked-in design after Round 1 research:

**Daemon side** (`_dispatch_store_rpc`):
1. Read `request_id` from the inbound frame; mint a fresh
   `uuid.uuid4().hex` if absent.
2. Bind `request_id`, `op`, and `start_ns = time.perf_counter_ns()`
   into the asyncio task's contextvars via
   `structlog.contextvars.bind_contextvars(request_id=...,
   op=...)`.
3. **CRITICAL** (per Round 1 A1 refutation): capture
   `ctx = contextvars.copy_context()` and dispatch as
   `loop.run_in_executor(None, lambda: ctx.run(fn, **raw_args))`
   so the bound vars propagate to the worker thread. Without this
   the store-layer logs see no request_id (the feature silently
   fails). This is a one-line change but it is load-bearing.
4. Echo the (possibly minted) `request_id` on the response frame
   so clients without their own IDs can recover one.
5. Log `rpc_handler_ok` (or existing `rpc_handler_error`) on exit
   with `elapsed_ms`.

**Client side** (`T2Client` / `_SocketConnection`):
- Architectural symmetry with the daemon: introduce a
  module-level `_request_id_cv: ContextVar[str | None]` plus a
  context manager `client.request_context(request_id=...)`.
- `_SocketConnection.call` reads the contextvar and threads its
  value into the outbound frame's `request_id` field.
- This avoids modifying the signature of every generated
  `_StoreProxy` method (~50+ methods across 9 stores). Callers
  that want a custom ID enter the context manager; callers that
  don't get a daemon-minted ID echoed back on the response frame.

**Protocol compat**: `t2_json_loads` is schema-free at the top
level (unknown keys pass through), so adding `request_id` does
not break older daemons/clients. No `DAEMON_PROTOCOL_VERSION`
bump.

**Domain stores**: zero source edits — the `merge_contextvars`
processor (already in the project's processor chain) pulls the
bound keys at log time. Housekeeping: align the 8-of-9 stores
that omit `__name__` on `structlog.get_logger()` during
Phase 1 (Round 1 A3 housekeeping note).

**Gap 2 (blocking_take None paths)**: emit three distinct
DEBUG events before returning:
  - `blocking_take_shutdown_observed`: `_wake_stop` was set at
    the loop-top check.
  - `blocking_take_conn_closed_during_take`: caught
    `sqlite3.ProgrammingError` while `_wake_stop` was set.
  - `blocking_take_deadline_elapsed`: `remaining <= 0` after a
    take returned no candidate.

Each carries the active `request_id` + `op` so the client's
None-return log line and the daemon's classified-return event
share a key.

### Decision Rationale

**Why wire-level + log-level contextvars over per-log-site
injection.** The naive alternative — make every log call thread
a `request_id` kwarg via signature changes — would touch ~50+
domain-store methods across nine `src/nexus/db/t2/*.py` modules
plus the dispatch layer itself. Round 1 research (T2
`nexus_rdr/115-research-3`) confirmed all nine stores use
`structlog.get_logger(...)` with the `merge_contextvars`
processor already in the chain, so an asyncio-task-bound
contextvar with explicit `copy_context().run(...)` propagation
across `run_in_executor` reaches every store-layer log line with
zero per-store source edits. The cost is one mandatory wrapper
line in `_dispatch_store_rpc`; the benefit is that future stores
inherit correlation automatically. Symmetry on the client side
(module-level `_request_id_cv` + `client.request_context(...)`
context manager) avoids a parallel kwarg-threading sprawl across
the auto-generated `_StoreProxy` surface.

**Why not OpenTelemetry now.** OpenTelemetry would deliver the
same correlation + latency story plus cross-process span
propagation if the daemon ever grew multi-host or out-of-process
clients. Round 1 research (`nexus_rdr/115-research-5`) confirmed
the dependency footprint (`opentelemetry-api` +
`opentelemetry-sdk` + protobuf + grpcio) is disproportionate to
the v1 single-host single-daemon scale and would add operational
surface area (collector configuration, environment differences)
that the substrate does not yet need. The contextvar approach
ships the same observable benefit at zero new-dependency cost.
**Re-evaluate trigger**: when the daemon serves multiple hosts
or callers want spans visible in an external tracing UI
(Honeycomb / Tempo / Datadog), file a follow-up RDR proposing
the OpenTelemetry migration with the contextvar contract as the
client-facing API to preserve.

**Why three None-paths + typed exceptions over a unified return
code.** Folding the three None-returning paths plus the typed
exceptions (`BlockingTakeResourceExhausted`,
`InvalidTimeoutError`) into one machine-readable return code was
considered and rejected: it would either bloat the success-path
return shape with an always-present `_reason` field or force
callers to inspect both a return value AND a side-channel log
line. Distinct DEBUG events keyed on `request_id` let callers
opt into reason-discovery without changing the existing
`Optional[dict]` return contract.

## Alternatives Considered

### Alternative 1: OpenTelemetry SDK adoption

**Description**: Adopt the `opentelemetry-api` + `opentelemetry-sdk`
trio so spans cross asyncio / thread boundaries with industry-standard
semantics. Export to a local OTLP collector or stdout for development.

**Pros**:
- Industry-standard surface (Datadog / Honeycomb / Jaeger / Tempo all
  ingest OTLP).
- Free correlation across processes (the daemon's MCP-process clients
  could propagate a parent span over the wire if we adopted W3C
  TraceContext headers).
- Spans carry start + end automatically (Gap 3 latency falls out
  for free).

**Cons**:
- Heavyweight dependency tree (`opentelemetry-*` family pulls in
  protobuf, grpcio, etc).
- Forces the project into a tracing-ecosystem opinion that the
  current scale doesn't justify.
- Adds operational surface area (configuring the collector / exporter,
  environment differences between dev / CI / prod).

**Reason for rejection (provisional)**: project scale + dependency
cost are too high for the value at v1. Re-evaluate once the substrate
is in cross-process production use.

### Briefly Rejected

- **Per-call structured-log "request_id" injected by every log
  site**: rejected because it requires touching every log call
  in the dispatch + domain layers.

## Trade-offs

### Risks and Mitigations

- **Risk**: contextvar propagation across thread-pool boundary is
  subtle; missed propagation produces silent missing correlation IDs.
  **Mitigation**: spike against a small executor before rolling
  the dispatch-layer integration.
- **Risk**: latency breadcrumb adds two `time.perf_counter()` calls
  per RPC. Cost is negligible (~ns) but verifying it under load
  is part of MVV.
  **Mitigation**: measure under the existing CA-3 spike harness.

## Implementation Plan

Updated after Round 1 research. Phases sequenced so each delivers
observable value independently:

- **Phase 1: daemon-side contextvar plumbing + copy_context spike**
  - Add `bind_request_context` helper inline in `t2_daemon.py`.
    (The third 360° dim-7 M-2 protocol-extract aspiration was
    never promoted to an RDR; no upstream RDR to defer to. A
    future protocol-extract RDR can relocate this helper to the
    new module.)
  - Update `_dispatch_store_rpc` to bind contextvars AND wrap the
    executor dispatch with `copy_context().run(...)`. THIS IS THE
    SPIKE — verify domain-store logs receive the bound keys end
    to end. Without this gate the rest of the work is wasted.
  - Add `rpc_handler_ok` exit event with `elapsed_ms` (Gap 3).
  - The 8-of-9 stores omitting `__name__` on `get_logger` is a
    pre-existing housekeeping debt unrelated to correlation IDs;
    file as a separate `housekeeping`-tagged bead, NOT scoped to
    this RDR's Phase 1 (per gate Round 1 observation O3 — avoids
    PR review noise and prevents conflation of the
    housekeeping change with the functional logger pipeline
    change).
- **Phase 2: wire-level request_id extension**
  - Extend RPC frame shape with optional `request_id`.
  - Daemon mints on miss; echoes on response frame.
  - No protocol-version bump (verified backward-compat).
- **Phase 3: client-side contextvar API**
  - Module-level `_request_id_cv` + `client.request_context(...)`
    context manager.
  - `_SocketConnection.call` reads the contextvar; no `_StoreProxy`
    method signatures change.
- **Phase 4: blocking_take None-path disambiguation (Gap 2)**
  - Three DEBUG events: `blocking_take_shutdown_observed`,
    `blocking_take_conn_closed_during_take`,
    `blocking_take_deadline_elapsed`. All carry `request_id`.
- **Phase 5: DEFERRED to a follow-on bead / RDR**. The
  in-memory ring buffer for `nx daemon t2 doctor --recent`
  (last N request_ids + their elapsed_ms) is genuinely useful
  but not load-bearing for the correlation contract. Resolving
  it to "out of scope until evidence demands it" instead of
  "maybe later" cleans up the planning-chain scope (gate
  Round 1 observation O1).

## Test Plan

- **Phase 1 spike (load-bearing) — positive arm**: bind a
  request_id in the asyncio task via
  `structlog.contextvars.bind_contextvars`, capture
  `ctx = contextvars.copy_context()`, dispatch a sync function
  through `loop.run_in_executor(None, lambda: ctx.run(fn))`,
  capture the function's structlog output via
  `structlog.testing.capture_logs()`, and assert the bound
  `request_id` key appears in the captured log event. **Pass
  criterion**: assert the bound key is present in the captured
  record.
- **Phase 1 spike (load-bearing) — negative control arm**: same
  test scaffold but dispatch as
  `loop.run_in_executor(None, fn)` (no `copy_context()` wrap).
  **Pass criterion**: assert the bound key is ABSENT from the
  captured record. This regression guard ensures that an
  implementer who forgets the `copy_context()` wrapper cannot
  ship a green test. Both arms must pass for Phase 1 to be
  declared done.
- **Phase 1 ordering invariant**: explicit test that the bind
  MUST precede the `copy_context()` call. Bind-after-copy
  produces an empty context snapshot; the spike asserts the
  worker thread sees a default-value contextvar in that ordering.
- **Scenario**: Two concurrent `T2Client.call`s with distinct
  client-side request_ids — **Verify**: every log line in each
  RPC's chain (client `t2_client_*` + daemon `rpc_handler_*` +
  store `<op>_*`) carries the matching ID; the two streams do
  not interleave by ID.
- **Scenario**: Caller omits `request_id`; daemon mints one —
  **Verify**: response frame carries the daemon-minted ID; the
  daemon's own logs use the minted ID; the client's logs also
  use it once the response frame is parsed.
- **Scenario**: Three blocking_take None-return paths fire —
  **Verify**: the corresponding DEBUG event is emitted before
  the return; each event carries the request_id; client-side
  None-return log uses the same ID for correlation.
- **Scenario**: Backward-compat — an older client (one that does
  not send `request_id`) connects to the new daemon —
  **Verify**: daemon mints; old client ignores the unknown key
  in the response frame; no protocol error raised.

## Validation

### Performance Expectations

Negligible per-RPC overhead expected. Quantify under the CA-3
read-latency spike harness if introduced.

## Finalization Gate

### Contradiction Check

No contradictions found between Research Findings (Round 1),
Technical Design, Implementation Plan, and Test Plan after the
Round 1 gate remediation.  The single round-1 contradiction (the
stale "four None-paths" count in §Approach vs the three named in
§Technical Design) was fixed in the same revision pass.

### Assumption Verification

All three Critical Assumptions verified by Round 1 research:

- A1 (`bind_contextvars` propagation across `run_in_executor`):
  REFUTED — Spike confirmed `copy_context().run(...)` is
  mandatory; mitigation locked into Technical Design and
  Phase 1 spike with both positive + negative arms.
- A2 (request_id frame-size budget): Verified — Source Search.
- A3 (domain-store retrofit without per-store source changes):
  Verified — Source Search across all 9 stores.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `structlog.contextvars.bind_contextvars` | structlog | Source Search (`nexus_rdr/115-research-1`) |
| `contextvars.copy_context()` | stdlib | Source Search + Spike (Python 3.12.11 in project env) |
| `asyncio.AbstractEventLoop.run_in_executor` | stdlib | Source Search — default executor does NOT propagate context |
| `t2_json_loads` unknown-key tolerance | nexus.daemon.t2_daemon | Source Search (`nexus_rdr/115-research-2`) |

### Scope Verification

Minimum Viable Validation: Phase 1 spike (positive + negative
arms) verifies the `copy_context()` wrapper is both effective
and necessary.  In scope for Phase 1; will be executed as the
first deliverable, not deferred.

### Cross-Cutting Concerns

- **Versioning**: `DAEMON_PROTOCOL_VERSION = "1.0"` stays
  unchanged. Daemon's strict equality check at
  `t2_daemon.py:1238` is unaffected because the new
  `request_id` is an optional top-level key in the JSON frame;
  `t2_json_loads` is schema-free and ignores unknown keys
  (verified `nexus_rdr/115-research-2`). The semantic
  compatibility (new daemon + old client and vice versa) is
  preserved without a version bump.
- **Build tool compatibility**: N/A (no build-system change).
- **Licensing**: N/A (no new third-party dependencies; uses
  stdlib `contextvars` + already-bundled `structlog`).
- **Deployment model**: `request_id` binding is transport-
  agnostic. The dispatch layer binds the contextvar before
  consulting `is_uds`, so both UDS and TCP frames carry the
  same correlation. The traceback-stripping rule from third
  360° SEC-2 (`is_uds` gate at `t2_daemon.py:1430`) is
  unchanged — `request_id` appears in both `is_uds` and
  `not is_uds` error frames.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phases 1-4 each deliver observable
  value independently. Operators can adopt by phase; old
  clients without `client.request_context(...)` continue to
  work and receive daemon-minted IDs.
- **Secret/credential lifecycle**: N/A (request_id is a
  correlation token, not a secret; minted as `uuid.uuid4().hex`
  with no security implications; not logged into any persistent
  store beyond the rolling daemon logs).
- **Memory management**: Phase 5 (in-memory ring buffer) is
  DEFERRED — that's the only feature with a memory-growth
  surface. Phases 1-4 add only short-lived contextvar bindings
  (cleared at task / frame exit) and per-frame UUID strings
  (54 bytes, GC-collected with the frame). No new long-lived
  allocations.

### Proportionality

Right-sized. The RDR touches a structurally significant surface
(every RPC dispatch path + every domain store's logger + the
wire protocol), and the design space had a non-obvious failure
mode (A1 refutation) that warranted careful documentation. The
remaining sections — Day 2 Operations, New Dependencies — are
N/A and intentionally omitted to avoid template-driven bloat.

## References

- nexus-6m9i umbrella (third 360° remediation)
- Third 360° agent scratch entry `7c5abccb` (observability)
- T2 research entries: `nexus_rdr/115-research-1` (A1 refuted),
  `115-research-2` (A2 verified), `115-research-3` (A3 verified),
  `115-research-4` (Gap 2 enumeration), `115-research-5` (no
  competing tracing infra)
- structlog documentation, contextvars module (Python 3.12)
- Python asyncio docs on `loop.run_in_executor` +
  `contextvars.copy_context()` propagation semantics
- RDR-112 (storage-as-service container boundary, sets the daemon
  surface this RDR extends)

## Revision History

### 2026-05-17 — Round 1 research

- A1 status moved from Unverified → **REFUTED**. Initial draft
  framed `bind_contextvars` + `run_in_executor` propagation as
  "subtle but likely works"; empirical testing confirmed the
  worker thread sees `None` without explicit `copy_context()`.
  Technical Design updated to make `copy_context().run(...)` a
  load-bearing requirement; Phase 1 spike now the first
  validation gate.
- A2 status moved from Documented → **Verified**. 54-byte
  per-frame overhead vs 1 MiB cap; no protocol-version bump.
- A3 status moved from Unverified → **Verified**. All 9
  domain stores under `src/nexus/db/t2/*.py` use structlog;
  zero source edits per store. Housekeeping noted (8-of-9
  omit `__name__` on `get_logger`).
- Gap 2 None-path count corrected from 4 to **3**. Semaphore
  exhaustion raises `BlockingTakeResourceExhausted` (already
  distinguishable), not None.
- Client-side API approach changed from "kwarg threading
  through every method" to **module-level contextvar +
  `client.request_context(...)` context manager** for
  architectural symmetry with the daemon and to avoid touching
  50+ `_StoreProxy` method signatures.

### 2026-05-17 — Gate Round 1

Gate result: BLOCKED on first dispatch (1 critical + 3 significant
+ 3 observations). Critic identified an absent Decision Rationale,
a Phase 1 spike criterion missing its control arm, a stale
"four" None-paths count contradicting the corrected Technical
Design, and an empty Finalization Gate on a structurally
significant RDR.

Single-pass remediation:

- C1: §Decision Rationale rewritten with three substantive
  paragraphs (why contextvars over per-log-site injection; why
  not OpenTelemetry now + re-evaluate trigger; why three
  None-paths + typed exceptions over a unified return code).
- S1: §Phase 1 test plan now specifies positive AND negative
  control arms — both must pass; ordering invariant (bind
  before copy_context) gets its own assertion.
- S2: stale "four" at line 210 corrected to "three" + the three
  event names spelled out in the Approach paragraph for
  consistency with §Technical Design.
- S3: §Finalization Gate filled — Contradiction Check,
  Assumption Verification + API Verification table, Scope
  Verification, Cross-Cutting Concerns (Versioning, Build tool,
  Licensing, Deployment model, IDE, Incremental adoption,
  Secret/credential, Memory management), Proportionality.
- O1: §Phase 5 changed from conditional ("out of scope if
  Phases 1-4 prove sufficient") to explicitly DEFERRED to a
  follow-on bead/RDR.
- O2: stale "RDR-117 / M-2 protocol-extract" reference replaced
  with a one-liner explaining there is no upstream RDR; helper
  is inline in `t2_daemon.py` until a protocol-extract RDR
  lands.
- O3: 8-of-9 `__name__` housekeeping removed from Phase 1
  scope; called out as a separate housekeeping-tagged bead so
  the correlation-ID change is not conflated with unrelated
  logger-style cleanup.

Cross-RDR P7 check (RDR-112) ran clean in Round 1 and does not
need re-verification in Round 2 unless the new Decision
Rationale introduces a fresh design claim (it does not).
