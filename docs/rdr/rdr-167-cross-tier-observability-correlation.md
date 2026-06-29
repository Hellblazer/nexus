---
title: "Cross-Tier Observability and Request Correlation for the Service Stack: X-Request-ID Propagation, SLF4J MDC, Readiness, and Structured Logs"
id: RDR-167
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-25
accepted_date:
related_issues: [nexus-7y0ab]
related: [RDR-030, RDR-087, RDR-152, RDR-155, RDR-161, RDR-166]
---

# RDR-167: Cross-Tier Observability and Request Correlation for the Service Stack

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Before RDR-152/155, nexus was effectively a single Python process: one `structlog`
stream under `~/.config/nexus/logs/` was enough to reconstruct any operation. The
6.0.0 boundary changed the shape — a single logical operation now crosses four
process boundaries (`nx` CLI / MCP tool → T2 daemon → Java engine-service → Postgres),
and in the managed deployment (RDR-166) the Java service runs in the cloud
(conexus engine/controlplane). The logging did not follow the architecture across
that boundary: each tier logs to its own sink with no shared identifier, the Java
service's logging is materially thinner than the Python side, and almost none of it
is tested. The 6.0.0 local validation pass is the direct evidence: every service-mode
defect it found (the empty-project plan 400, the chash-backfill AttributeError, and
the systemic catalog-API parity gap `nexus-7y0ab`) was discovered by reading logs by
hand. No telemetry row, no `nx doctor` check, and no test flagged any of them.

### Enumerated gaps to close

#### Gap 1: No cross-tier request correlation

There is no request/trace identifier that flows `nx`/MCP → T2 daemon → Java service →
Postgres. The HTTP clients send `X-Nexus-Tenant` (tenant isolation) but no
`X-Request-ID` / `traceparent`. Postgres statement logging is not configured. So to
debug "operation X returned wrong/empty results at 14:32:15," an operator must grep
three disjoint log files by wall-clock timestamp and guess which lines belong to the
same logical call. In a multi-tenant managed deployment this is not just slow — it
risks mis-attributing log lines across tenants.

#### Gap 2: The Java service's RequestContext is not wired to SLF4J MDC

The service already resolves a per-request principal (`RequestContext`: tenant,
session, operator flag, virtual-thread-scoped). None of it reaches the log lines:
Logback has no MDC pattern and the handlers do not populate MDC. Every `event=...`
line is therefore un-attributable to a tenant/session/request without manually
threading those fields into each `log.info(...)` call (which the code does not do
consistently). The correlation ID from Gap 1 is useless unless it lands on every log
line via MDC.

#### Gap 3: No readiness signal distinct from liveness

`/health` is a liveness probe (a `SELECT 1`). There is no `/ready` that gates on
"schema migrated, embedder loaded, vector backend reachable, token registry warm."
A rolling deploy (or `nx init --service` restart) can route traffic to a process that
is alive but still applying Liquibase or loading the bge-768 ONNX model, producing
transient errors that look like real failures.

#### Gap 4: Java logs are plain-text stdout, not structured

Logback writes `key=value`-ish plain text to stdout with a synchronous appender. For
the managed deployment (RDR-166) that means log aggregators must regex-parse the
lines, the `event=` fields are not first-class queryable keys, and high-throughput
request logging blocks the request thread on console writes.

#### Gap 5: Observability is effectively untested

The Java service has zero tests asserting on log content, MDC propagation, or
`/health` behaviour with the DB down; `/version` response shape is only partially
pinned. On the Python side ~507 structured events exist but ~25 have contract tests.
There is no end-to-end test that a logical operation is correlatable across tiers.
This is the gap that let the 6.0.0 service-mode defects ship green: the observability
that would have surfaced them does not exist and would not be caught regressing if it
did.

## Context

### Background

Discovered during the 6.0.0 local service-stack validation campaign (2026-06-24).
The campaign stood up the mac-native binary + host PG16/pgvector + onnx-local bge-768
and drove greenfield install + 5.x→6.0 migration. Three service-mode defects surfaced
ONLY by hand-reading `~/.config/nexus/logs/storage_service_native.log` and CLI
tracebacks. The most serious (`nexus-7y0ab`) — service-mode `nx index repo` silently
not populating the catalog — produced an exception that was caught and logged as a
`phase4_migration_failed` warning, then swallowed. With a correlation ID + a telemetry
surface + a service-mode logging test, that class would have been visible at the
point of failure instead of three layers deep in a manual probe.

RDR-030 (silent-error audit + logging policy), RDR-087 (collection observability
surfaces + `search_telemetry`), and RDR-017 (indexing progress) already established a
deliberate **local** observability posture. This RDR extends that posture across the
**tier boundary** that RDR-152/155 introduced — it does not re-litigate the local
design, it connects it to the service.

### Technical Environment

- Python: `structlog` via `src/nexus/logging_setup.py` (8 entry-point modes, rotating
  files, KeyValueRenderer). HTTP clients: `src/nexus/catalog/http_catalog_client.py`,
  `src/nexus/db/t2/http_*`, `src/nexus/db/http_vector_client.py`.
- Java service: SLF4J 2.0.16 + Logback 1.5.18, `service/src/main/resources/logback.xml`
  (console appender, plain text). Endpoints: `HealthHandler`, `VersionHandler`, the
  `*Handler` family; `AuthFilter` + `RequestContext` (ThreadLocal, virtual-thread-per-request).
- T2 daemon: `src/nexus/daemon/` (per-call `request_id` for same-process RPC handshake
  only — NOT a distributed trace ID).
- Managed deployment: conexus engine/controlplane images (ECR); cloud log shipping
  is currently the container log driver only.

## Research Findings

### Investigation

To be completed during `/conexus:rdr-research`. Anchor points already identified:

- `service/src/main/resources/logback.xml` — current appender/pattern (no MDC, no JSON).
- `service/src/main/java/dev/nexus/service/http/RequestContext.java` — ThreadLocal
  principal; the source of truth to bridge into MDC.
- `service/src/main/java/dev/nexus/service/http/AuthFilter.java` — where the request
  principal is established (the natural MDC set/clear seam).
- `HealthHandler` / `VersionHandler` — current probe surface; `/ready` lands here.
- Python HTTP client request builders (catalog/T2/T3) — where an outbound
  `X-Request-ID` header is attached.
- `src/nexus/logging_setup.py` — structlog processor chain; where an inbound/contextvar
  request id is bound for the Python tiers.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Logback (`logback-classic` 1.5.18) | No | Confirm `%X{key}` MDC pattern + `JsonEncoder`/`logstash-logback-encoder` option and licensing during research |
| SLF4J `MDC` | No | Confirm MDC is virtual-thread-safe with the per-request set/clear discipline (it is ThreadLocal-backed; verify behaviour under virtual-thread-per-request) |
| `structlog` contextvars | No | Confirm `structlog.contextvars.bind_contextvars` is the right binding for per-operation request id in the CLI/MCP tiers |
| W3C Trace Context | No | Decide `X-Request-ID` (simple) vs `traceparent` (standard, future OTEL-compatible) |

### Key Discoveries

- **Documented** — `RequestContext` already carries tenant/session per request but is
  not bridged to MDC (`logback.xml` has no `%X{...}`).
- **Documented** — Python HTTP clients attach `X-Nexus-Tenant` but no request/trace
  header (verified by the validation pass).
- **Assumed** — a single `X-Request-ID` propagated header + MDC bridge is sufficient
  for correlation without adopting a full tracing backend (OTEL collector). Needs a
  decision in research.

### Critical Assumptions

- [ ] **MDC is safe and correct under virtual-thread-per-request** with a strict
  set-on-entry / clear-on-exit discipline in `AuthFilter` — **Status**: Unverified
  — **Method**: Spike (assert MDC value isolation across concurrent virtual-thread requests)
- [ ] **A propagated `X-Request-ID` (or `traceparent`) header threaded through the
  Python HTTP clients reaches the Java handlers and lands on log lines** end-to-end —
  **Status**: Unverified — **Method**: Spike (one logical op, grep one id across all tiers)
- [ ] **JSON Logback + the MDC pattern do not regress the existing Java contract tests
  or the plain-text expectations of any current log consumer** — **Status**: Unverified
  — **Method**: Source Search + Spike
- [ ] **A `/ready` gate can cheaply and correctly report schema/embedder/vector
  readiness** without a heavyweight health framework — **Status**: Unverified
  — **Method**: Source Search (what the service already knows at startup)

## Proposed Solution

### Approach

Four coordinated, independently-shippable pieces, smallest-blast-radius first:

1. **Request correlation (Gap 1).** Generate a request id at the outermost nexus
   boundary (the CLI command / MCP tool invocation), bind it into a Python contextvar
   so the structlog tiers stamp it, and attach it as an `X-Request-ID` header on every
   outbound HTTP call (catalog/T2/T3 clients). The Java service reads the inbound
   header (generating one if absent) and puts it in MDC. Decision deferred to research:
   plain `X-Request-ID` vs W3C `traceparent` (the latter buys future OTEL interop at
   the cost of a parser).

2. **MDC bridge (Gap 2).** In `AuthFilter` (where the principal is resolved), set MDC
   keys `request_id`, `tenant`, `session` on entry and clear on exit. Add `%X{request_id}
   %X{tenant} %X{session}` to the Logback pattern. This makes every existing `event=...`
   line attributable without touching the call sites.

3. **Readiness + structured logs (Gaps 3, 4).** Add `GET /ready` distinct from
   `/health`: ready iff schema-migrated AND embedder-loaded AND vector-backend
   reachable. Switch the Logback appender to JSON (and to an async appender) behind a
   config toggle, defaulting to JSON in the managed image and leaving plain text
   available for local dev readability.

4. **Validation (Gap 5).** Add the missing tests as first-class artifacts: Java tests
   for `/health` DB-down (503), `/version` shape, `/ready` gating, and MDC presence on
   a log line; a cross-tier correlation E2E (one op, assert the same id appears in the
   CLI structlog and the service log); and a small expansion of the Python event
   contract tests for the highest-risk events (hook lifecycle, daemon start/stop,
   service-mode catalog operations).

Cloud log shipping (CloudWatch/OTEL exporters) is noted as a **dimension**, not built
here — once logs are JSON + correlated, shipping is a deployment concern for the
managed image (RDR-166 territory), and the local case (the primary scope) needs none
of it.

### Technical Design

To be expanded in research. Interface-level intent:

- Python: a `request_id` contextvar bound at CLI/MCP entry; HTTP client request
  builders add `X-Request-ID: <id>`; structlog processor includes it.
- Java: `AuthFilter` does `MDC.put("request_id", ...)` / `MDC.clear()`; `logback.xml`
  pattern includes `%X{request_id}`; a `ReadinessHandler` returns
  `{ready: bool, checks: {schema, embedder, vector}}` with 200/503.

```text
// Illustrative — verify signatures during implementation
// Java: AuthFilter.handle(exchange) { MDC.put("request_id", rid); try { chain } finally { MDC.clear() } }
// Python: client._headers()["X-Request-ID"] = current_request_id()
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Request-id contextvar (Python) | `src/nexus/logging_setup.py` | Extend: add a contextvar + processor |
| Outbound `X-Request-ID` header | catalog/T2/T3 HTTP client request builders | Extend: add header in the shared builder |
| MDC bridge (Java) | `AuthFilter` + `RequestContext` | Extend: set/clear MDC at the existing principal seam |
| `/ready` endpoint | `HealthHandler` | Reuse pattern, add sibling `ReadinessHandler` |
| JSON appender | `service/src/main/resources/logback.xml` | Replace/augment appender behind a toggle |

### Decision Rationale

`X-Request-ID` + MDC is the minimal change that makes the existing logs correlatable
without adopting a tracing backend the project does not yet need. It reuses the
principal seam (`AuthFilter`) that already exists, so the per-line attribution is one
set/clear, not a call-site sweep. Readiness and JSON logs are independent, cheap, and
directly serve the managed deployment (RDR-166).

## Alternatives Considered

### Alternative 1: Full OpenTelemetry (traces + spans + collector)

**Description**: Adopt OTEL SDK in Python + Java, run a collector, export spans.

**Pros**:
- Industry-standard distributed tracing; span timing for free.

**Cons**:
- Heavy: collector infra, SDK deps both languages, sampling config, cost.
- Disproportionate to the local-primary scope; premature for current scale.

**Reason for rejection**: The evidence (hand-reading logs to find defects) calls for
*correlation*, not *distributed tracing*. `traceparent` keeps the door open to OTEL
later without paying for it now.

### Briefly Rejected

- **Per-call-site field threading instead of MDC**: rejected — it is the status quo,
  inconsistent and unenforceable; MDC is the one-seam fix.
- **Postgres `log_statement=all` as the correlation substrate**: rejected — high
  volume, no app-level id, doesn't correlate to the CLI/MCP origin.

## Trade-offs

### Consequences

- (+) Any single operation becomes greppable by one id across all tiers.
- (+) Every Java log line gains tenant/session/request attribution for free.
- (+) Managed deployment gets JSON logs + a real readiness gate.
- (−) A small, permanent header/MDC discipline every new HTTP client and handler must
  honour (mitigated by putting it in the shared builder/filter, not call sites).

### Risks and Mitigations

- **Risk**: MDC leaks across virtual-thread-per-request borrows (stale tenant on a
  reused thread). **Mitigation**: strict `try/finally MDC.clear()` at the one seam +
  a concurrency isolation test (Critical Assumption #1).
- **Risk**: JSON logback switch breaks a consumer expecting plain text.
  **Mitigation**: config toggle; default plain-text local, JSON managed; pin the
  format with a test.

### Failure Modes

- Missing/blank request id → service generates one (never blocks a request); the
  correlation is degraded, not broken.
- `/ready` false-negative during boot → traffic correctly withheld (the intended
  behaviour) — diagnose via the `checks` block in the body.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (esp. MDC under virtual threads).

### Minimum Viable Validation

A single logical operation (`nx` issues a service-backed catalog write) produces, in
both the CLI/MCP structlog stream and the Java `storage_service` log, log lines
carrying the **same** `request_id` — asserted by an automated cross-tier test, not a
manual grep. In scope, not deferred.

### Phase 1: Code Implementation

#### Step 1: Request-id contextvar + outbound header (Python)
#### Step 2: MDC bridge + Logback `%X{...}` pattern (Java)
#### Step 3: `/ready` endpoint (Java)
#### Step 4: JSON/async appender behind a config toggle (Java)

### Phase 2: Operational Activation

#### Activation Step 1: Default the managed image to JSON logs; document the toggle.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Log format toggle (config) | N/A | `nx doctor` reports active format | N/A | In scope | N/A |
| `/ready` endpoint | N/A | curl/`nx daemon service status` | N/A | In scope | N/A |

### New Dependencies

- Possibly `logstash-logback-encoder` (JSON) — confirm license (Apache-2.0 expected)
  during research before adding.

## Test Plan

- **Scenario**: One service-backed op end-to-end — **Verify**: same `request_id` in
  CLI structlog + service log (the MVV).
- **Scenario**: `/health` with DB down — **Verify**: 503 + logged at WARN.
- **Scenario**: `/ready` during simulated mid-migration — **Verify**: 503 + `checks`
  identifies the not-ready component.
- **Scenario**: concurrent requests, two tenants — **Verify**: no MDC tenant bleed.
- **Scenario**: `/version` response shape — **Verify**: contract pinned.

## Validation

### Testing Strategy

1. **Scenario**: cross-tier correlation E2E. **Expected**: shared id asserted.
2. **Scenario**: Java MDC presence on a log line. **Expected**: tenant/request_id present.
3. **Scenario**: readiness gating. **Expected**: 200 ready / 503 not-ready with reasons.

### Performance Expectations

Async appender removes the synchronous-console write from the request path; correctness,
not throughput, is the goal. No speculative targets.

## Finalization Gate

### Contradiction Check

To be completed at gate.

### Assumption Verification

The four Critical Assumptions must be Verified (Spike for the MDC/correlation ones)
before Accept.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `MDC.put/clear` | SLF4J | Source Search + Spike (virtual-thread isolation) |
| `%X{key}` pattern / JSON encoder | Logback | Source Search |
| `bind_contextvars` | structlog | Source Search |

### Scope Verification

The MVV (cross-tier shared `request_id`, automated) is in scope for Phase 1.

### Cross-Cutting Concerns

- **Versioning**: N/A (additive headers/endpoints; `/version` unchanged).
- **Build tool compatibility**: confirm any new logback encoder dep builds under the
  native-image path (RDR-161) — reachability metadata.
- **Licensing**: confirm JSON-encoder license before adding.
- **Deployment model**: JSON default in managed image; toggle for local.
- **Secret/credential lifecycle**: N/A (no secrets; ensure request id / MDC never
  carry token or PII).
- **Memory management**: async appender bounded queue; MDC cleared per request.

### Proportionality

Right-sized: four small, independently-shippable pieces over the existing seams; the
heavy alternative (OTEL) is explicitly deferred.

## References

- RDR-030 (logging policy), RDR-087 (observability surfaces), RDR-017 (progress).
- RDR-152/155/161/166 (service stack, native install, managed journeys).
- `service/src/main/resources/logback.xml`, `AuthFilter.java`, `RequestContext.java`,
  `HealthHandler.java`, `VersionHandler.java`, `src/nexus/logging_setup.py`.
- nexus-7y0ab (the systemic defect whose invisibility motivated this RDR).

## Revision History

- 2026-06-25: Initial draft (created from 6.0.0 validation findings).
