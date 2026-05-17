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

For Gap 2, `blocking_take`'s four return-None paths each emit a
distinct DEBUG-level log event before returning, carrying the same
`request_id`. Operators triaging the "agents not getting work"
symptom can grep on `request_id` and see the categorised return
reason.

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

Substantive refactor (touches every dispatch path, every domain
store's transitive logger) — deferred from the third 360°
remediation precisely because the right design needs research and
a gate, not an inline patch.

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
  - Add `bind_request_context` helper in `nexus.daemon.protocol`
    (or inline in `t2_daemon.py` if RDR-117 / M-2 protocol-extract
    is deferred).
  - Update `_dispatch_store_rpc` to bind contextvars AND wrap the
    executor dispatch with `copy_context().run(...)`. THIS IS THE
    SPIKE — verify domain-store logs receive the bound keys end
    to end. Without this gate the rest of the work is wasted.
  - Add `rpc_handler_ok` exit event with `elapsed_ms` (Gap 3).
  - Housekeeping: align the 8-of-9 stores omitting `__name__`.
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
- **Phase 5 (optional)**: `nx daemon t2 doctor --recent` could
  surface the last N request IDs + their elapsed_ms from an
  in-memory ring buffer on the daemon side. Out of scope for v1
  if Phases 1-4 prove sufficient for incident triage.

## Test Plan

- **Phase 1 spike (load-bearing)**: bind a request_id in the
  asyncio task, dispatch a sync function through `run_in_executor`
  with `copy_context().run(...)`, verify the function's structlog
  logger sees the bound key. Same test WITHOUT `copy_context()`
  must fail (regression guard).
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

To be completed before `/nx:rdr-accept`.

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
