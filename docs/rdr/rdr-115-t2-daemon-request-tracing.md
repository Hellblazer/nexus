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

#### Gap 2: Blocking-take returns None for at least four distinct reasons with no log differentiation

`blocking_take` returns `None` when (a) the deadline elapsed
naturally, (b) the per-claimant semaphore was already saturated and
the rollback path fired, (c) the service is shutting down and
`_wake_stop` was observed, (d) the sqlite connection was closed
underneath the in-flight call (third 360° SEC handling). Each of
these is operationally distinct; an operator triaging "why are my
agents not getting work?" cannot tell the four apart from the
client-side `None` return alone.

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

To be expanded under `/nx:rdr-research`. The minimum set of source
paths to read before locking the design:

- `src/nexus/daemon/t2_daemon.py` (dispatch loop, ping response,
  `_dispatch_store_rpc` error frame shape)
- `src/nexus/daemon/t2_client.py` (`call`, `_call`, `_SocketConnection`
  frame protocol)
- `src/nexus/daemon/tuplespace_service.py` (blocking_take's four
  distinct "return None" paths)
- One representative store (`src/nexus/db/t2/memory_store.py`) to
  understand the contextvar / log-binding plumbing surface
- `structlog` documentation for `structlog.contextvars.bind_contextvars`
  and the context-propagation patterns recommended for asyncio +
  thread-pool executors.

### Critical Assumptions

- [ ] structlog's `bind_contextvars` propagates correctly across
  `asyncio.run_in_executor` boundaries when the contextvar API is
  used (Python 3.12's `contextvars` module ensures per-task
  propagation; the thread-pool path requires `copy_context()`)
  — **Status**: Unverified — **Method**: Spike
- [ ] Adding a `request_id` field to every RPC frame does not push
  any production payload over the 1 MiB frame cap — **Status**:
  Documented — **Method**: Docs Only
- [ ] Domain-store logs can be retrofitted to read the active
  contextvars without source changes to each store — **Status**:
  Unverified — **Method**: Source Search

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

To be expanded. Initial sketch:

- Add `request_id` to the RPC frame shape (already-flexible JSON;
  zero protocol-version bump if treated as optional).
- New helper in `nexus.daemon.protocol` (or inline in
  `t2_daemon.py` if the protocol-extract from third 360° M-2 is
  deferred): `bind_request_context(*, request_id, op)` that wraps
  `structlog.contextvars.bind_contextvars` with the project's
  processor chain.
- Domain stores require no source changes — the structlog processor
  picks up the contextvars set by the dispatch layer.
- `_dispatch_store_rpc` measures `t0 = time.perf_counter()` before
  the executor dispatch and `elapsed_ms = (time.perf_counter() - t0)
  * 1000` before logging the exit event.
- Update `T2Client._call` / `T2Client.call` / `_SocketConnection.call`
  signatures to thread an optional caller-provided `request_id`.

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

To be expanded during `/nx:rdr-research` and locked at
`/nx:rdr-gate`. High-level phases:

- Phase 1: structlog processor + contextvar plumbing on the daemon
  side (no wire change yet).
- Phase 2: extend the RPC frame with optional `request_id`; client
  + daemon both honour it; daemon mints on miss.
- Phase 3: differentiate the four `blocking_take None` return paths
  via DEBUG-level events.
- Phase 4: optional CLI / monitoring hook — `nx daemon t2 doctor
  --recent` could surface the last N request IDs + elapsed times.

## Test Plan

- **Scenario**: Two concurrent `T2Client.call`s with distinct
  request_ids — **Verify**: every log line in each RPC's chain
  carries the matching ID.
- **Scenario**: Caller omits `request_id`; daemon mints one —
  **Verify**: response frame carries the daemon-minted ID; logs
  use the same ID.
- **Scenario**: `blocking_take` returns None due to deadline vs.
  shutdown vs. saturated semaphore — **Verify**: the DEBUG event
  for each path identifies which.

## Validation

### Performance Expectations

Negligible per-RPC overhead expected. Quantify under the CA-3
read-latency spike harness if introduced.

## Finalization Gate

To be completed before `/nx:rdr-accept`.

## References

- nexus-6m9i umbrella (third 360° remediation)
- Third 360° agent scratch entry `7c5abccb` (observability)
- structlog documentation, contextvars module (Python 3.12)
- RDR-112 (storage-as-service container boundary, sets the daemon
  surface this RDR extends)

## Revision History

(Gate rounds will be appended here.)
