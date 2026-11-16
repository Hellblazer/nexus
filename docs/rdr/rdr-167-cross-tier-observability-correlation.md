---
title: "Request Correlation Across Tiers: X-Request-ID Propagation and an SLF4J MDC Bridge for the Service Stack"
id: RDR-167
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-25
accepted_date:
related_issues: [nexus-7y0ab]
related: [RDR-030, RDR-087, RDR-152, RDR-155, RDR-161, RDR-166, RDR-168, RDR-222]
---

# RDR-167: Request Correlation Across Tiers

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> Narrowed on 2026-11-16 per Sam's draft triage. This RDR now covers one thing: a
> correlation id that a client mints and the engine writes on every log line it
> emits for that request. The readiness gap shipped by other work and is struck.
> The JSON appender is kept as an optional later phase. Every statement about
> current code was re-read from the working tree at develop `2e09b8cfc` and cites
> `file:line`; the research records are `nexus_rdr/167-research-1` to `-8`.

## Problem Statement

Before RDR-152/155, nexus was effectively one Python process, and one `structlog`
stream under `~/.config/nexus/logs/` was enough to reconstruct an operation.
Today a single logical operation (an `nx` command, or one MCP tool call) crosses
three process boundaries: the client (the `nx` CLI or the MCP server process), the
Java engine (the `service/` HTTP server, which this RDR calls "the engine"), and
Postgres. The earlier fourth hop, a Python "T2 daemon" between client and engine,
is retired (`src/nexus/daemon/AGENTS.md:5`), so it no longer appears here. In the
managed deployment (RDR-166) the engine runs in the cloud, so the client log and
the engine log live on different machines.

Each tier logs to its own sink and nothing ties the lines together. The 6.0.0
local validation pass is the direct evidence: every service-mode defect it found
(the empty-project plan 400, the chash-backfill AttributeError, and the catalog
interface divergence `nexus-7y0ab`, closed by RDR-168) was found by reading logs
by hand. RDR-222's research hit the same wall in September: "no `request_id` /
`X-Request-Id` anywhere in `service/src/main`; logback is a plain console pattern"
(`docs/rdr/rdr-222-request-identity-for-mutating-engine-routes.md:180-182`).

### Enumerated gaps to close

#### Gap 1: No cross-tier request correlation

No identifier flows from the client to the engine. The Python HTTP clients send
`X-Nexus-Tenant` (tenant isolation) and `X-Nexus-Client-Version` but no request
id (`src/nexus/db/http_vector_client.py:794-804`,
`src/nexus/db/t2/_refreshable_client.py:804-810`,
`src/nexus/db/http_scratch_store.py:210-217`). Python binds no `structlog`
context variables: `configure_logging` builds a processor chain without
`merge_contextvars` (`src/nexus/logging_setup.py:176-186`). To debug "operation X
returned the wrong result at 14:32:15" an operator greps two disjoint log files by
wall-clock time and guesses which lines belong together. In the managed deployment
this also risks attributing a log line to the wrong tenant.

#### Gap 2: The engine's per-request principal never reaches the log lines

`AuthFilter` resolves a per-request principal (tenant, session, operator flag) and
publishes it in a `ThreadLocal` (`service/src/main/java/dev/nexus/service/http/AuthFilter.java:320`,
cleared at `:336-337`; `RequestContext.java:67-73`). Nothing in `service/src/main`
calls SLF4J's `MDC` (the per-thread key/value map Logback can print on every line),
and the log pattern has no MDC field
(`service/src/main/resources/logback.xml:8`). So a log line carries a tenant only
when a call site wrote `tenant={}` into the message by hand, which many do and some
do not (`CatalogHandler.java:593` logs the field and value but no tenant). A
correlation id from Gap 1 is useless unless it lands on every engine line.

#### Gap 3: No readiness signal distinct from liveness (CLOSED: shipped, struck from scope)

The draft asked for a `/ready` endpoint. The need was met another way, so this gap
no longer belongs to this RDR (`nexus_rdr/167-research-6`):

- `GET /livez` is liveness with no dependency (`NexusService.java:719`,
  `LivezHandler.java:8-43`; nexus-hubc0) and `GET /health` is readiness with a
  `SELECT 1` probe that answers 503 on failure (`NexusService.java:714`,
  `HealthHandler.java:36-57`).
- The process does not bind its HTTP port until Liquibase has migrated the schema
  (`Main.java:245-248`) and the local bge-768 model is loaded (`Main.java:450`
  precedes `new NexusService` at `:574` and `service.start()` at `:582`). A process
  that answers at all is migrated and has its local embedder.
- The supervisor tracks boot with a migration-aware state machine
  (`src/nexus/daemon/readiness.py:1-30`; nexus-8vp0i), and `/v1/status` reports boot
  work (`NexusService.java:742`).
- "Token registry warm" has no state to gate on: `AuthFilter` resolves each token
  through a cache per request (`AuthFilter.java:205`).

One leftover is a stale comment: `HealthHandler.java:16` still says "liveness + DB
probe". It is corrected in Step 5 below.

#### Gap 4: Java logs are plain text on a synchronous appender (DEFERRED: optional later phase)

Logback writes plain text to stdout (`logback.xml:3-10`). For a managed deployment a
log aggregator would prefer JSON with MDC fields as keys. This is kept, but only as
Phase 3, tied to the managed deployment, because the local case does not need it
and Gaps 1 and 2 are useful without it.

#### Gap 5: The correlation path is untested (narrowed)

The draft said the engine has no log-content tests. That is no longer true: 56 test
files under `service/src/test` capture log events with Logback's `ListAppender`
(for example `OwnerlessWriteRefusalTest.java:657`), and `ProbeEndpointsTest.java`
pins `/livez` and `/version`. What is missing is narrower and is the real gap: no
test asserts that a request id appears on an engine log line, that MDC does not
leak between concurrent requests, or that a client-sent id arrives at the engine.

## Context

### Background

Discovered during the 6.0.0 local service-stack validation campaign (2026-06-24):
three service-mode defects surfaced only by reading
`~/.config/nexus/logs/storage_service_native.log` and CLI tracebacks. The worst,
`nexus-7y0ab`, produced an exception that was caught, logged as a
`phase4_migration_failed` warning, and swallowed. With a correlation id on every
line, the warning and the request that caused it would have been one `grep` apart.

RDR-030 (silent-error audit and logging policy), RDR-087 (collection observability
surfaces) and RDR-017 (indexing progress) set a deliberate local observability
posture. This RDR extends it across the client-to-engine boundary that RDR-152/155
created; it does not reopen the local design.

### Technical Environment

- **Python:** `structlog` configured in `src/nexus/logging_setup.py:176-186` (key=value
  renderer, rotating files per entry-point mode). The CLI group callback is
  `src/nexus/cli.py:175-181`; the MCP server's per-call hook is the `tools/call`
  middleware `_logged` in `src/nexus/mcp/_tool_call_log.py:266`.
- **Engine:** SLF4J 2.0.16 and Logback 1.5.18 (`service/pom.xml:23-24`), Java 25
  (`pom.xml:13`), a GraalVM native image whose reflection entries are listed by hand
  (`service/src/main/resources/META-INF/native-image/reflect-config.json`). The HTTP
  server is `com.sun.net.httpserver` with a virtual-thread-per-task executor
  (`NexusService.java:931`). Every context takes the `AuthFilter` list except five:
  `/health`, `/livez`, `/version`, `/v1/status` and `/v1/install-ping`
  (`NexusService.java:714,719,725,735,750`). `NexusService.java` has 32
  `createContext` calls, 27 of them followed by `getFilters().addAll(authFilter)`, and
  the two registration helpers apply it too (`ManifestCheckWiring.java:32-33`,
  `TenantImportWiring.java:34-35`).
- **Managed deployment:** conexus engine and controlplane images; log shipping today
  is the container log driver only.
- **Log consumers that parse engine output** (a pattern change must not break them):
  the supervisor recognises engine log lines by a leading date, time and level
  (`src/nexus/daemon/storage_service_daemon.py:986-987`) and finds migration
  markers by `event=...` inside the message (`src/nexus/daemon/readiness.py:105-106`).

## Research Findings

### Investigation

Eight research records are filed as `nexus_rdr/167-research-1` to `-8`. Two spikes
were run outside the repository in the session scratchpad
(`.../scratchpad/mdcspike/MdcSpike.java`, `.../scratchpad/ctxspike.py`); the rest are
source reads of the working tree.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Logback `logback-classic` 1.5.18 | Yes | `%X{key:-default}` is a constructor reference, `DEFAULT_CONVERTER_SUPPLIER_MAP.put("X", MDCConverter::new)` (`PatternLayout.java:107`), so it needs no native-image reflection entry; a run rendered `- - hi` with no MDC and `abc - hi` with the key set. `JsonEncoder` ships in logback-classic with an MDC block (`JsonEncoder.java:65,117,174-175`), so the JSON phase needs no `logstash-logback-encoder`. |
| SLF4J `MDC` (`LogbackMDCAdapter`) | Yes, and spiked | `ThreadLocal`-backed; correct under the engine's server shape (research-1). Not inherited by child threads. |
| `structlog.contextvars` | Yes, and spiked | `bind_contextvars` plus `merge_contextvars` stamps every line; an event's own keyword overrides a bound value (research-5). |
| `anyio` / `asyncio` thread hops | Spiked | `anyio.to_thread.run_sync`, `asyncio.to_thread` and `copy_context().run` carry the context; a bare `ThreadPoolExecutor.submit` does not (research-4). |
| W3C `traceparent` | No | Not adopted; see Decision Rationale. |

### Key Discoveries

- **✅ Verified** (spike) CA-1: MDC keeps per-request values correct under
  `com.sun.net.httpserver` with a virtual-thread executor, Logback 1.5.18, Java
  25.0.3. 400 concurrent requests, two tenants: in three of four runs every request
  had both of its log events carrying its own id (400/400), with zero tenant
  crossover and no log event on a platform thread. The fourth run had one client-side
  connection failure that never reached the server (399/400, zero crossover). A
  first run with unbounded client concurrency lost 33 requests at the client (the
  server's listen backlog is 10); bounding client concurrency to 32 removed the
  loss, so it was load shape and not MDC. An early reject before `MDC.put` logs a
  null id, and a child thread (platform or virtual) sees null.
  *Source: `nexus_rdr/167-research-1`.*
- **✅ Verified** (source search) The engine runs a request's filter chain and
  handler on one virtual thread (`NexusService.java:931`; `RequestContext.java:13-15`
  states the same invariant). Two request-path fan-outs start threads of their own
  and will not carry MDC: `PgVectorRepository.java:3182-3184` (search arms) and
  `CceEmbedder.java:573` (embed batches). `AuthFilter` has early returns before its
  `try/finally` (`AuthFilter.java:191,196,207`), so an MDC put at the top must wrap
  the whole method body.
  *Source: `nexus_rdr/167-research-2`.*
- **✅ Verified** (source search) The client inventory for CA-2. Running the census in
  `tests/test_z0o2p24_client_version_header.py` found 36 modules under `src/nexus`
  that make an HTTP call. Six import `nexus.db.client_identity` and send its header:
  the T3 client (`http_vector_client.py:803`), the shared httpx mixin used by the T2
  stores, the catalog client and the pipeline client (`_refreshable_client.py:809`),
  `HttpTokenStore` (`http_token_store.py:185`), `HttpScratchStore`
  (`http_scratch_store.py:216`) and the two cloud backup modules
  (`backup/cloud_export.py:349,533`, `backup/cloud_import.py:230,675`). Thirty are
  named non-senders, 13 of them engine-bound probes, admin, token-mint or hook
  modules. The mixin and the T3 client build headers per request. `HttpScratchStore`
  and `HttpTokenStore` bake headers into their `httpx.Client` at construction
  (`http_scratch_store.py:642`, `http_token_store.py:192`), so a context variable read
  there would freeze at construction and each needs a per-request merge. The test
  file passes 78 of 79 without an engine; the one failure needs the engine substrate
  and is an environment gap.
  *Source: `nexus_rdr/167-research-3`.*
- **✅ Verified** (spike) The Python id survives the MCP tool path: with 50 concurrent
  coroutines each bound to a distinct id, sync bodies reached through
  `anyio.to_thread.run_sync`, `asyncio.to_thread` and `copy_context().run` all saw
  their own id (0 mismatches); a bare `ThreadPoolExecutor.submit` saw none. The
  spike drove `anyio` directly, not the live MCP server, which is read from the
  SDK-patch docstring (`src/nexus/mcp/_sdk_patches.py:28`), not run. The tree has 46
  modules that start threads (115 sites), so worker paths need `copy_context`, as
  `search_engine.py:757,3123` and `operators/llm_backend.py:548` already do.
  *Source: `nexus_rdr/167-research-4`.*
- **✅ Verified** (source search and run) A naming collision. RDR-222 already uses
  `request_id` for the idempotency key, the identifier that stays the same across
  retries of one write: `RequestHandler.java:18-26,72-73` in the engine and 12
  occurrences in `http_vector_client.py` (for example `:2335-2354`). `structlog`
  lets an event's own keyword win over a bound value, so a correlation field also
  called `request_id` would be overwritten on exactly those lines. This RDR therefore
  names the log field `correlation_id` (see Technical Design).
  *Source: `nexus_rdr/167-research-5`.*
- **✅ Verified** (source search) Gap 3 is closed by other work (see Gap 3 above).
  *Source: `nexus_rdr/167-research-6`.*
- **✅ Verified** (source search and run) The Logback facts in the table above.
  *Source: `nexus_rdr/167-research-7`.*
- **❓ Assumed** That a client-sent `X-Request-ID` reaches the managed engine
  unchanged through whatever sits in front of it (an edge, CDN or load balancer), and
  what that edge does with an id of its own. Nothing in the tree says. No run took a
  client through a live engine to a log line. The local case has no edge and does
  not depend on this.
  *Source: `nexus_rdr/167-research-8`.*

### Critical Assumptions

- [x] **CA-1: MDC is correct under virtual-thread-per-request with a set-on-entry,
  clear-on-exit discipline.**
  **Status**: Verified. **Method**: Spike plus source search
  (`167-research-1`, `-2`). Residual limits are recorded, not hidden: MDC does not
  follow work into threads the engine starts itself (two request-path fan-outs), and
  requests rejected before the id is put are unattributed unless the put moves to the
  top of the filter, which this design does.
- [~] **CA-2: A propagated `X-Request-ID` threaded through the Python clients reaches
  the engine and lands on its log lines.**
  **Status**: Partly verified. **Method**: Source search plus spike for the parts;
  the whole chain is not yet run. Verified: the client sites are enumerated and
  covered by one shared function plus two per-request merges (`167-research-3`); the
  id survives the Python thread hops (`167-research-4`); the engine side works
  (`167-research-1`). Assumed: the end-to-end path on a live engine, and
  pass-through at a managed edge (`167-research-8`). The end-to-end path is the
  Phase 1 minimum validation and is the gate for implementation, not for accepting
  this RDR.
- [ ] **CA-3: A JSON Logback appender does not regress the engine's contract tests or
  its native image.**
  **Status**: Unverified, deliberately. **Method**: needed only if Phase 3 is taken
  up. The dependency question is answered (research-7, no new library); the native
  image reflection entry and the log consumers in the Technical Environment are not
  tested.
- CA-4 (a cheap `/ready` gate) is struck with Gap 3.

## Proposed Solution

### Approach

Two pieces ship first, in this order, each useful without the other; a third is
optional and later.

1. **Request id at the client (Gap 1).** Mint one id per CLI invocation (in the click
   group callback) and one per MCP tool call (in the `tools/call` middleware), bind it
   in a `ContextVar` and into `structlog` contextvars so Python log lines carry it, and
   send it as the `X-Request-ID` header on engine requests through the six sender
   sites found in research-3.
2. **MDC bridge at the engine (Gap 2).** At the top of `AuthFilter.doFilter`, read the
   inbound header (accept a well-formed one, otherwise generate one), put
   `correlation_id` in MDC, add `tenant` and `session` once they are resolved, and
   clear everything in a `finally` that wraps the whole method. Add the three keys to
   the log pattern. Echo the id in the response so a caller that sent none can still
   learn the id the engine made.
3. **Optional, Phase 3: JSON logs (Gap 4).** Switch the managed image to
   `JsonEncoder` behind a toggle. Taken up only with the managed deployment.

### Technical Design

**Names.** The header is `X-Request-ID` (the conventional name, and the one the
approved scope uses). The log field and MDC key are `correlation_id`, not
`request_id`, because RDR-222's idempotency key already owns `request_id` in both
tiers (research-5). The two ids differ in lifetime: the idempotency key is stable
across retries of one keyed write and is sent as `Idempotency-Key`
(`http_vector_client.py:818-819`); the correlation id names one operation, and a
`nx index repo` run that makes thousands of calls sends the same one on all of them.

**Python.**

- A new leaf module (standard library only, like `nexus/db/client_identity.py`, which
  documents why it must stay a leaf) holds the `ContextVar`, `new_correlation_id()`
  (a UUID4 as 32 hex characters) and `current_correlation_id()`.
- `cli.py:main` calls `bind_contextvars(correlation_id=...)` beside
  `configure_logging`; the `_logged` MCP middleware does the same per `tools/call`
  before `await call_next`. `logging_setup.configure_logging` adds
  `structlog.contextvars.merge_contextvars` first in the processor chain
  (`logging_setup.py:176-186`).
- `client_identity_headers()` (the one function all six sender sites already call)
  adds `X-Request-ID` only when an id is bound, so hook processes and probes that do
  not bind one send exactly what they send today and the exact-dictionary assertion at
  `tests/test_z0o2p24_client_version_header.py:50` still holds when no id is bound.
  The mixin and the T3 client call it per request and need no further change.
  `HttpScratchStore` and `HttpTokenStore` evaluate it once at construction, so each
  gets a per-request merge (scratch already passes per-call headers at
  `http_scratch_store.py:918`; the token store has five call sites at
  `http_token_store.py:258-306`).
- Thread pools that carry engine calls on behalf of a bound operation submit through
  `contextvars.copy_context().run`, as `search_engine.py:757` does. A bare
  `submit` loses the id (research-4); such lines go out without a correlation id and
  the engine generates one, so correlation degrades and nothing breaks.

**Engine.**

```text
// Illustrative; the real change wraps AuthFilter.doFilter (AuthFilter.java:187).
doFilter(exchange, chain):
    cid = accept(exchange.header("X-Request-ID")) or generate()
    MDC.put("correlation_id", cid); exchange.responseHeaders.set("X-Request-ID", cid)
    try:
        ... existing steps 1 to 4; after step 3 resolves them:
        MDC.put("tenant", tenant); if (sessionId != null) MDC.put("session", sessionId)
        RequestContext.set(...); chain.doFilter(exchange)
    finally:
        RequestContext.clear(); RequestContext.clearDeadline(); MDC.clear()
```

- **Accepting an id.** The header is client-controlled text that goes into a log
  line, so the engine accepts it only when it matches `[A-Za-z0-9._-]{1,64}`;
  anything else is replaced by a generated UUID. This blocks log-line injection.
  `MDC` keys never carry a token or its hash (`credentialHash` stays out), only
  `correlation_id`, `tenant` and `session`.
- **Pattern.** Insert `[cid=%X{correlation_id:--} tenant=%X{tenant:--} session=%X{session:--}]`
  after `%logger{36}` and before ` - %msg%n` in `logback.xml:8`. The date, time and
  level stay at the front of the line and `event=...` stays in the message, so the
  supervisor's recognisers (`storage_service_daemon.py:986-987`,
  `readiness.py:105-106`) keep matching. A test pins both.
- **Not covered, by design.** The five contexts outside `AuthFilter` carry no tenant
  and their lines stay without an id. Lines logged from the two engine-started fan-out
  threads (research-2) show `-`. Neither is worth a copy-the-MDC change now; both are
  recorded so nobody expects otherwise.
- **No access log.** The engine writes no per-request line, and at indexing volumes a
  per-request INFO line is expensive. So an id appears only on lines that handlers
  already log. This is a known limit of the narrowed scope, not an oversight.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Correlation `ContextVar` and structlog binding | `src/nexus/logging_setup.py`, `src/nexus/cli.py`, `src/nexus/mcp/_tool_call_log.py` | New leaf module for the id; extend the three call sites |
| Outbound `X-Request-ID` | `client_identity_headers()` in `src/nexus/db/client_identity.py`, six sender sites | Extend the one function; add a per-request merge to scratch and token stores |
| Pin that every engine client sends it | `tests/test_z0o2p24_client_version_header.py` census (`_census`, `_NOT_SENDERS`) | Extend the same census so a new client must send or be named |
| MDC bridge | `AuthFilter.java:187-338`, `RequestContext.java` | Extend: put at the top, clear in a `finally` over the whole method |
| Log pattern | `service/src/main/resources/logback.xml:8` | Extend with three `%X{...:--}` fields |
| Log-capture test pattern | `ListAppender` in 56 test files, e.g. `OwnerlessWriteRefusalTest.java:657` | Reuse |
| JSON appender (Phase 3) | `logback-classic` `JsonEncoder` | Reuse; add its reflect-config entry |

### Decision Rationale

A correlation id plus MDC is the smallest change that makes the existing logs
correlatable without a tracing backend. It reuses the seam that already publishes
the principal and the shared function that already names the client, so attribution
is one put and clear and one function, not a sweep of call sites. `X-Request-ID` was
chosen over W3C `traceparent` because there is no tracing backend to consume spans;
`traceparent` can replace it later without changing the structure.

## Alternatives Considered

### Alternative 1: Full OpenTelemetry (traces, spans, collector)

**Description**: Adopt the OTEL SDK in Python and Java and run a collector.

**Pros**: Standard distributed tracing; span timing for free.

**Cons**: Collector infrastructure, SDK dependencies in both languages, sampling
configuration, cost. Disproportionate to a problem that was found by hand-reading
logs and needs correlation, not timing.

**Reason for rejection**: The evidence calls for correlation. `traceparent` keeps
the door open.

### Alternative 2: Reuse the Idempotency-Key as the correlation id

**Description**: RDR-222 already sends a per-write key and logs it as `request_id`.

**Cons**: It exists only on keyed writes, it is stable across retries so it cannot
tell two attempts apart, and it is minted inside the T3 client, not at the CLI or MCP
entry. Most operations (searches, T2 and catalog reads) have none.

**Reason for rejection**: Wrong lifetime and wrong coverage.

### Briefly Rejected

- **Thread fields through each log call:** the status quo, inconsistent and
  unenforceable; MDC is the one-seam fix.
- **Postgres `log_statement=all` as the substrate:** high volume, no application id,
  no link back to the CLI or MCP origin.
- **A `/ready` endpoint:** the need shipped (Gap 3).

## Trade-offs

### Consequences

- (+) One id finds a client operation's lines in the engine log, and the same id is
  on the client's own lines.
- (+) Every engine line a handler already logs gains tenant, session and id.
- (−) A new engine client must send the header; the extended census test makes that
  a failing test and not a convention.
- (−) The id is useful only where a line is logged (see "No access log").

### Risks and Mitigations

- **Risk**: MDC leaks across requests. **Mitigation**: the put and clear sit in one
  `try/finally` over the whole filter body, and a concurrency test (400 requests,
  two tenants, the spike's shape) runs in the Java suite. The spike showed no
  crossover; the test keeps it that way.
- **Risk**: a pattern change breaks a log reader. **Mitigation**: the new field goes
  after the logger name, and a test asserts the supervisor's two recognisers still
  match a real engine line.
- **Risk**: a worker thread drops the id. **Mitigation**: `copy_context().run` at
  engine-bound pools; a missing id degrades to an engine-generated one.
- **Risk**: a managed edge rewrites or strips the header. **Mitigation**: unverified
  (research-8); extend the edge probe if the managed deployment needs it.

### Failure Modes

- Missing, blank or malformed id: the engine generates one; correlation degrades and
  the request is never refused.
- The engine echoes the id in `X-Request-ID` on the response, so a client with none
  can still read it.

## Implementation Plan

### Prerequisites

- [x] CA-1 verified (research-1, -2).
- [ ] CA-2's end-to-end run is the Phase 1 minimum validation below, run before
  Phase 1 is called done, not before this RDR is accepted.

### Minimum Viable Validation

One service-backed operation from an `nx` command produces, in the client's
`structlog` output and in the engine's log, lines carrying the same `correlation_id`,
asserted by an automated test on the engine substrate and not by hand. The engine
line comes from an event a handler already logs and can be triggered on demand,
`catalog_handler_tumbler_grammar_violation` at `CatalogHandler.java:593` (a request
with a malformed document tumbler); the choice is confirmed in Step 4.

### Phase 1: Request id and MDC

#### Step 1: Python id leaf module and `structlog` binding
Create the leaf module; add `merge_contextvars` to the chain; bind at `cli.py:main`
and in the MCP `_logged` middleware. Add `copy_context` at the engine-bound thread
pools found by grep of the 46 thread-starting modules.

#### Step 2: Send the header
Extend `client_identity_headers()`; add the per-request merge to `HttpScratchStore`
and `HttpTokenStore`; extend the census test so a sender must send it.

#### Step 3: MDC bridge in the engine
Wrap `AuthFilter.doFilter`; validate and generate the id; put `correlation_id`,
`tenant`, `session`; echo on the response; clear in the `finally`. Update `logback.xml`.

#### Step 4: Tests
The Java and Python tests in the Test Plan, ending with the cross-tier test.

#### Step 5: Doc fixes
Correct the `HealthHandler.java:16` comment; add the field to the engine and logging
docs; keep the RDR-222 `request_id` fields unchanged and say so beside them.

### Phase 2: Managed-deployment check (only with the managed deployment)

Extend `nx doctor --probe-edge` (`src/nexus/doctor_edge_probe.py`) to send
`X-Request-ID` and report whether it arrived unchanged, closing the assumption in
research-8.

### Phase 3 (optional, later): JSON logs

Switch the managed image to `JsonEncoder` behind a toggle; add its
`reflect-config.json` entry; run the native smoke; confirm the log consumers in the
Technical Environment. Taken up only with the managed deployment.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Correlation id | N/A (not stored) | `grep <id>` on client and engine logs | N/A | The cross-tier test | N/A |
| Log pattern | N/A | engine log lines | N/A | Pattern test | N/A |

### New Dependencies

None. `JsonEncoder` (Phase 3) is already in logback-classic (research-7).

## Test Plan

- **Java, MDC on a line.** Through the real `AuthFilter` and a handler that logs, a
  `ListAppender` sees `correlation_id`, `tenant` and `session` in
  `getMDCPropertyMap()`.
- **Java, concurrency.** 400 concurrent requests with two tenants: each log event
  carries its own id and tenant; MDC is empty after the request. This is the shape of
  the spike.
- **Java, id handling.** A valid id is kept; an over-long or malformed one is replaced;
  a request with none gets a generated one; the response header echoes it; a rejected
  (401) request still logs its id.
- **Java, pattern.** A formatted engine line starts with date, time and level and
  still matches `_ENGINE_LOGBACK_LINE` and the readiness `event=` pattern.
- **Python, binding.** The CLI callback and the MCP middleware bind an id; two
  concurrent tool calls see different ids; `copy_context().run` carries it into a pool.
- **Python, header.** Each of the six sender sites puts `X-Request-ID` on the wire when
  an id is bound and omits it when none is; the extended census fails on a client that
  does neither.
- **Python, collision.** The `keyed_sweep_*` lines in `http_vector_client.py` still
  show their idempotency key as `request_id` and the operation's id as `correlation_id`.
- **Cross-tier (MVV).** One real command against the engine substrate: the same id in
  the client's structlog output and in the engine log.

## Validation

### Testing Strategy

1. **Scenario**: cross-tier correlation on the engine substrate. **Expected**: shared
   id asserted (the MVV).
2. **Scenario**: concurrent requests, two tenants, Java suite. **Expected**: no id or
   tenant crossover.
3. **Scenario**: pattern change against the supervisor's recognisers. **Expected**:
   both still match.

### Performance Expectations

One map put and clear per request and three short fields per log line. No target is
claimed; the Java concurrency test also times 400 requests so a gross regression
shows.

## Finalization Gate

### Contradiction Check

The draft described a four-hop chain with a T2 daemon, a `/ready` endpoint and a
JSON-by-default managed image. None of those survives: the daemon is retired
(`src/nexus/daemon/AGENTS.md:5`), readiness shipped (Gap 3), and JSON is Phase 3. The
draft's "Java has zero log tests" is wrong (56 files use `ListAppender`) and is
replaced in Gap 5. The one real conflict found is a name: `request_id` already means
the idempotency key (RDR-222), so this RDR uses `correlation_id` and leaves RDR-222's
fields alone. No conflict remains between the Problem Statement, the design and the
plan.

### Assumption Verification

CA-1 is Verified by a spike and a source read (research-1, -2). CA-2 is partly
verified: the site inventory, the Python thread behaviour and the engine side are
verified; the run through a live engine and the managed edge are assumed
(research-3, -4, -8), and the live-engine run is the MVV. CA-3 is unverified by
design and belongs to the optional phase. CA-4 is struck with Gap 3. Nothing is
presented as measured that was not: the Python spike used `anyio` directly, not the
live MCP server, and no native image was built.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `MDC.put/clear` under virtual threads | SLF4J / Logback 1.5.18 | Spike (research-1) |
| `%X{key:--}` pattern | Logback | Source read and run (research-7) |
| `bind_contextvars`, `merge_contextvars` | structlog 25.5.0 | Spike and run (research-4, -5) |
| `anyio.to_thread.run_sync`, `copy_context().run` | anyio 4.15.1 | Spike (research-4) |
| `JsonEncoder` | Logback 1.5.18 | Source read (research-7), Phase 3 only |

### Scope Verification

The MVV is in scope for Phase 1. Struck from scope with the reason recorded: the
readiness endpoint (shipped), the T2 daemon hop (retired). Deferred with a named
trigger: JSON logs (the managed deployment takes up observability shipping).
Not in scope: an access log line per request, an id on the five unauthenticated
contexts, and an id on lines logged from the two engine fan-out threads.

### Cross-Cutting Concerns

- **Versioning**: additive request and response headers; `/version` unchanged.
- **Build tool compatibility**: `%X` needs no reflection entry (research-7); the
  Phase 3 `JsonEncoder` does and is checked there.
- **Licensing**: no new dependency.
- **Deployment model**: the local engine needs no edge change; the managed edge is
  Phase 2.
- **Secret and credential lifecycle**: the id is client text; it is validated before
  it is logged, and no token or token hash enters MDC.
- **Memory management**: MDC is cleared per request; the id is not stored.
- **Compatibility with RDR-222**: the two ids are separate headers and separate log
  fields.

### Proportionality

Right-sized: about two to three days for the two pieces (a leaf module, one function
and two merges on the client; one filter, one pattern line and tests on the engine),
over seams that already exist. The heavy option, full OpenTelemetry, stays rejected,
and the JSON appender is not built until something needs it.

## References

- RDR-030 (logging policy), RDR-087 (observability surfaces), RDR-017 (progress).
- RDR-152, RDR-155, RDR-161, RDR-166 (service stack, native install, managed
  journeys); RDR-168 (closed; resolved `nexus-7y0ab`); RDR-222 (closed; owns the
  idempotency key and the name `request_id`).
- Engine: `service/src/main/resources/logback.xml`, `AuthFilter.java`,
  `RequestContext.java`, `HealthHandler.java`, `LivezHandler.java`, `NexusService.java`,
  `Main.java`.
- Client: `src/nexus/logging_setup.py`, `src/nexus/cli.py`,
  `src/nexus/mcp/_tool_call_log.py`, `src/nexus/db/client_identity.py`,
  `tests/test_z0o2p24_client_version_header.py`.
- T2: `nexus_rdr/167-research-1` to `-8`; `nexus/draft-rdr-triage-2026-11-16`.

## Revision History

- 2026-06-25: Initial draft (created from 6.0.0 validation findings).
- 2026-11-16: Narrowed per Sam's draft triage. Scope is request-id propagation and the
  SLF4J MDC bridge; Gap 3 struck as shipped (`/livez`, `/health`, bind order, readiness
  state machine); the T2-daemon hop removed; the JSON appender kept as an optional
  later phase; CA-1 verified by spike, CA-2 partly verified; correlation field named
  `correlation_id` to avoid RDR-222's `request_id`; anchors, plan, tests and gate
  section rewritten against develop `2e09b8cfc`.
- 2026-11-16: Gate round 1 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); blob `05a00fba9b74`; critique `nexus_rdr/167-gate-critique-2026-11-16-r1`.
