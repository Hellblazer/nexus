---
title: "Request Correlation Across Tiers: X-Correlation-ID Propagation and an SLF4J MDC Bridge for the Service Stack"
id: RDR-167
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-25
accepted_date: 2026-11-16
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
> `file:line`; the research records are `nexus_rdr/167-research-1` to `-9`.

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
by hand. Only one of the three was an engine response (the plan 400); the other two
failed inside the Python client before a request reached the engine, so an id on
engine lines cannot join them (see Background). RDR-222's research hit the same wall in September: "no `request_id` /
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
`~/.config/nexus/logs/storage_service_native.log` and CLI tracebacks. Taking them one
at a time, against the design below (`nexus_rdr/167-research-9`):

- **The empty-project plan 400: joined.** The engine answered 400 to a blank
  `project` on `/v1/plans/get` (commit `3da30e2af`: "The Java plan handler rejected a
  blank `project` query param as 'missing required query param'"). Today the
  engine's handler for that 400 logs at DEBUG (`PlanHandler.java:97-98`), below the
  root level INFO (`logback.xml:12`), so no line existed. The failure line added in
  Phase 1 (see Technical Design) would carry the id and tie it to the client's line.
- **The chash-backfill AttributeError: not joined.** `1db06147c`: "In service mode t3
  is an HttpVectorClient (no _client), so the loop raised AttributeError". That is a
  Python error on a local attribute; no request failed at the engine.
- **`nexus-7y0ab`: not joined.** RDR-168 records a `collection_for(owner=...)`
  `TypeError` (`rdr-168-service-mode-catalog-interface-conformance.md:73-79`), a
  Python signature error raised before any request, and `phase4_migration_failed` is
  logged by the Python process (`src/nexus/indexer.py:5475`). The engine never saw a
  failing request.

So this RDR claims to join one of the three. For the other two, correlation does not
help; they were fixed in the Python client (`1db06147c`, RDR-168).

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

Nine research records are filed as `nexus_rdr/167-research-1` to `-9`. Three spikes
were run outside the repository in the session scratchpad
(`.../scratchpad/mdcspike/MdcSpike.java`, `.../scratchpad/mdcspike/StatusSpike.java`,
`.../scratchpad/ctxspike.py`); the rest are
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
- **❓ Assumed** That a client-sent `X-Correlation-ID` reaches the managed engine
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
- [~] **CA-2: A propagated `X-Correlation-ID` threaded through the Python clients reaches
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
   send it as the `X-Correlation-ID` header on engine requests through the six sender
   sites found in research-3.
2. **MDC bridge at the engine (Gap 2).** At the top of `AuthFilter.doFilter`, read the
   inbound header (accept a well-formed one, otherwise generate one), put
   `correlation_id` in MDC, add `tenant` when the bearer token resolves and `session`
   once the session token is verified, and
   clear everything in a `finally` that wraps the whole method. In that `finally`,
   write one `request_failed` line (WARN; a sent 404 with nothing thrown at INFO) for every response with status 400 or above,
   for a request that sent no response, and for a request whose chain threw.
   Add the three keys to the log pattern. Echo the id in the response so a caller that sent none can still
   learn the id the engine made.
3. **Optional, Phase 3: JSON logs (Gap 4).** Switch the managed image to
   `JsonEncoder` behind a toggle. Taken up only with the managed deployment.

### Technical Design

**Names.** The header, the log field and the MDC key carry the same word:
`X-Correlation-ID` and `correlation_id`. Neither is `request_id` or `X-Request-ID`,
because RDR-222's idempotency key already owns `request_id` in both tiers
(research-5), and proxies and load balancers are commonly said to generate or
overwrite `X-Request-ID` (inferred, not read in this tree; research-9). Neither new
name appears anywhere in `src` or `service/src` today (a case-insensitive `grep`
for `x-correlation-id` found nothing). `correlation_id` is also a field of the
mailbox tuple template (`service/src/main/resources/tuples/templates/mailbox.yaml:23,57`,
where it pairs a reply with its request), but no log call in either tier uses it as a
keyword (research-9), so it does not collide as a log field. The two ids differ in lifetime: the idempotency key is stable
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
  adds `X-Correlation-ID` only when an id is bound, so hook processes and probes that do
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
    cid = accept(exchange.header("X-Correlation-ID")) or generate()
    MDC.put("correlation_id", cid); exchange.responseHeaders.set("X-Correlation-ID", cid)
    start = nanoTime(); thrown = null
    try:
        ... existing steps 1 to 4; MDC.put("tenant", tenant) as soon as the bearer token resolves;
        after step 3 verifies the session: if (sessionId != null) MDC.put("session", sessionId)
        RequestContext.set(...); chain.doFilter(exchange)
    catch (Throwable t): thrown = t; throw t         // precise rethrow, the exception is unchanged
    finally:
        // the warn is guarded: a logging failure must not skip the clears
        try:
            status = exchange.getResponseCode()      // -1 when no response was sent
            if (status >= 400 or status == -1 or thrown != null)
                level = (status == 404 and thrown == null) ? INFO : WARN
                log.atLevel(level).log("event=request_failed method={} path={} status={} exception={} duration_ms={}", ...)
        catch (Throwable): pass
        RequestContext.clear(); RequestContext.clearDeadline(); MDC.clear()
```

- **Failure line.** One line per response with status 400 or above, per request
  that sent no response, and per request whose chain threw, written in
  that `finally` before MDC is cleared, so the pattern prints the id, tenant and
  session on it. The tenant is there from the moment the bearer token resolves; a
  missing, non-Bearer or unknown token has no tenant, so the pattern prints `-`.
  Fields: `event=request_failed`, method, path (no query string, as the
  existing `event=auth_rejected` line already logs the path only,
  `AuthFilter.java:411-412`), status, exception and duration. Method and path are
  client text, so both are escaped with `LogText.escapeControl`: `getPath()` is the
  decoded path, so a `%0A` would otherwise write a second line, and the method token
  can carry control characters. A backslash is doubled and a control character is
  written as `\xNN`. Where a handler threw
  and no status was sent, `getResponseCode()` is -1 and the client sees a dropped
  connection; the line then says `status=none` and names the exception class, and the
  exception is rethrown unchanged. A handler that sent 200 and then threw while
  writing the body also leaves a line, with `status=200` and the class. The line
  never carries the exception message or stack trace, which can hold client text. Why here, and not in `HttpUtil.send`: the filter
  list puts `AuthFilter` first and `IdempotencyKeyGuard` second
  (`NexusService.java:760-761`), so the filter's `finally` sees the guard's 400 and
  `AuthFilter`'s own 401s; two handlers send without `HttpUtil.send`
  (`MemoryHandler.java:610`, `TenantExportHandler.java:357`) and a thrown handler
  never reaches it. The status read was run on the engine's server shape: 200, 404,
  chunked 503 and body-less 400 all read back correctly, and a throw read -1
  (research-9). WARN is the right level: root is INFO (`logback.xml:12`), so WARN is
  printed, while the lines that exist today for these failures are DEBUG
  (`AuthFilter.java:342`, `PlanHandler.java:98`) and are not. A sent 404 with nothing thrown logs at INFO instead, still printed: the Step 3 measurement found 404 was 16 of 18 failure lines, and Sam ruled on 2026-11-17 that 404 goes at INFO and every other failure stays WARN. Cost: 404 is an
  ordinary answer on some routes (the `PlanHandlerTest` contract returns 404 for an
  empty project with no row, commit `3da30e2af`), so ordinary misses will be
  logged; the rate was not measured (inferred, not read, to be low outside
  indexing) and Step 3 measures it on the engine-substrate run.

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
- **No line for successful requests.** The engine still writes no per-request line
  for a 2xx or 3xx, and at indexing volumes one would be expensive. So on a
  successful request the id appears only on lines that handlers already log. Failures
  always leave the failure line above. This is a known limit of the narrowed scope,
  not an oversight.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Correlation `ContextVar` and structlog binding | `src/nexus/logging_setup.py`, `src/nexus/cli.py`, `src/nexus/mcp/_tool_call_log.py` | New leaf module for the id; extend the three call sites |
| Outbound `X-Correlation-ID` | `client_identity_headers()` in `src/nexus/db/client_identity.py`, six sender sites | Extend the one function; add a per-request merge to scratch and token stores |
| Pin that every engine client sends it | `tests/test_z0o2p24_client_version_header.py` census (`_census`, `_NOT_SENDERS`) | Extend the same census so a new client must send or be named |
| MDC bridge | `AuthFilter.java:187-338`, `RequestContext.java` | Extend: put at the top, clear in a `finally` over the whole method |
| Log pattern | `service/src/main/resources/logback.xml:8` | Extend with three `%X{...:--}` fields |
| Log-capture test pattern | `ListAppender` in 56 test files, e.g. `OwnerlessWriteRefusalTest.java:657` | Reuse |
| JSON appender (Phase 3) | `logback-classic` `JsonEncoder` | Reuse; add its reflect-config entry |

### Decision Rationale

A correlation id plus MDC is the smallest change that makes the existing logs
correlatable without a tracing backend. It reuses the seam that already publishes
the principal and the shared function that already names the client, so attribution
is one put and clear and one function, not a sweep of call sites. `X-Correlation-ID` was
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
- (+) Every engine response of 400 or above leaves one line with the id, so a client
  failure of that kind can be joined to the engine.
- (−) On a successful request the id is useful only where a handler already logs (see
  "No line for successful requests").

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
  (research-8); the Phase 2 probe tests overwrite and strip as well as pass-through.
- **Risk**: the failure line is noisy, because 404 is a normal answer on some routes
  and a bad token repeats 401. **Mitigation**: the line carries no body or query
  string; Step 3 counts failure lines per 1000 requests on the engine-substrate run
  and brings the 404 level back to the gate if they dominate.

### Failure Modes

- Missing, blank or malformed id: the engine generates one; correlation degrades and
  the request is never refused.
- The engine echoes the id in `X-Correlation-ID` on the response, so a client with none
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
line is the `event=request_failed` line, triggered by a request the engine answers
with a 4xx. The candidate trigger is a malformed document tumbler, which
`CatalogHandler.java:584-598` already rejects with a 400; whether a client path can
send one to the engine is not shown, and Step 4 confirms it or picks another 4xx.

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
`tenant` (when the token resolves), `session`; echo on the response; in the
`finally`, write the `request_failed` line (WARN; a sent 404 at INFO) for a status of 400 or above, none or a
thrown chain (naming the exception class) and clear. Update
`logback.xml`. On the engine-substrate run, count failure lines per 1000 requests and
record the number in the bead.

#### Step 4: Tests
The Java and Python tests in the Test Plan, ending with the cross-tier test.

#### Step 5: Doc fixes
Correct the `HealthHandler.java:16` comment; add the field to the engine and logging
docs; keep the RDR-222 `request_id` fields unchanged and say so beside them.

### Phase 2: Managed-deployment check (only with the managed deployment)

Extend `nx doctor --probe-edge` (`src/nexus/doctor_edge_probe.py`) to send a known
`X-Correlation-ID` and read back the id the engine echoes, the way the existing probe
compares the echoed `Idempotency-Key` with the one sent (`doctor_edge_probe.py:214-226`).
It reports three outcomes, not one: **pass-through** (echo equals sent), **overwrite**
(echo present and different, so an edge replaced the id) and **strip** (echo absent).
This closes the assumption in research-8.

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
  a request with none gets a generated one; the response header echoes it.
- **Java, failure line.** With the real filter list: a handler answering 400, 404 and
  503 (fixed length and chunked), `IdempotencyKeyGuard`'s 400, a 401 from a bad token,
  and a synthetic handler that calls `exchange.sendResponseHeaders(409, -1)` directly,
  without `HttpUtil.send`, each log exactly one `request_failed` line (WARN; the 404 at INFO) carrying the
  id; a 200, a synthetic direct `sendResponseHeaders(204, -1)` (the
  `MemoryHandler.java:610` shape, which sends 204) and a synthetic direct
  `sendResponseHeaders(302, -1)` log none, because all three are below 400. A handler that throws before sending logs `status=none` and the exception class
  and the exception still propagates; a handler that returns without sending logs
  `status=none exception=none`; a handler that sends 200 and then throws logs
  `status=200` and the class. No line holds an exception message.
- **Java, pattern.** A formatted engine line starts with date, time and level and
  still matches `_ENGINE_LOGBACK_LINE` and the readiness `event=` pattern.
- **Python, binding.** The CLI callback and the MCP middleware bind an id; two
  concurrent tool calls see different ids; `copy_context().run` carries it into a pool.
- **Python, header.** Each of the six sender sites puts `X-Correlation-ID` on the wire when
  an id is bound and omits it when none is; the extended census fails on a client that
  does neither.
- **Python, collision.** The `keyed_sweep_*` lines in `http_vector_client.py` still
  show their idempotency key as `request_id` and the operation's id as `correlation_id`.
- **Cross-tier (MVV).** One real command against the engine substrate that the engine
  answers with a 4xx: the same id in the client's structlog output and in the engine's
  `request_failed` line.

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
Not in scope: a line per successful request (failures of 400 or above get one), an id
on the five unauthenticated contexts, and an id on lines logged from the two engine fan-out threads.

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
- **Compatibility with RDR-222**: the two ids are separate headers (`Idempotency-Key`
  and `X-Correlation-ID`) and separate log fields (`request_id` and `correlation_id`).

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
- T2: `nexus_rdr/167-research-1` to `-9`; `nexus/draft-rdr-triage-2026-11-16`.

## Revision History

- 2026-06-25: Initial draft (created from 6.0.0 validation findings).
- 2026-11-16: Narrowed per Sam's draft triage. Scope is request-id propagation and the
  SLF4J MDC bridge; Gap 3 struck as shipped (`/livez`, `/health`, bind order, readiness
  state machine); the T2-daemon hop removed; the JSON appender kept as an optional
  later phase; CA-1 verified by spike, CA-2 partly verified; correlation field named
  `correlation_id` to avoid RDR-222's `request_id`; anchors, plan, tests and gate
  section rewritten against develop `2e09b8cfc`.
- 2026-11-16: Gate round 1 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); blob `05a00fba9b74`; critique `nexus_rdr/167-gate-critique-2026-11-16-r1`.
- 2026-11-16: Fix round 1 (Sam: fix S1 and S3). S1: one WARN `request_failed` line per
  engine response of 400 or above, written in AuthFilter's outer `finally`; the three
  6.0.0 examples taken one by one, Background narrowed to join one of three. S3: header
  renamed `X-Correlation-ID`; the Phase 2 probe tests overwrite and strip as well as
  pass-through. Research `nexus_rdr/167-research-9`.
- 2026-11-16: Accepted (Sam, by dialog in halcyon's session: "Accept"). Fix check `nexus_rdr/167-fix-check-b5b0e4bbeab4` PASS. Epic nexus-dngtz. Residual dispositions: gate S2 nexus-dngtz.1.9, S4 nexus-dngtz.1.1, S5 nexus-dngtz.1.2; fix-check probe outcomes nexus-dngtz.1.4, exception naming nexus-dngtz.1.6, synthetic 4xx test nexus-dngtz.1.7, path escaping nexus-dngtz.1.8.
- 2026-11-17: Step 3 measured (nexus-dngtz.1.6): 52 failure lines per 1000 client calls on the engine-substrate scenario run, 404 16 of 18. Sam's ruling (by dialog in halcyon's session): a sent 404 logs at INFO, every other failure stays WARN. Step 3 text and sketch amended to the reviewed code: tenant at token resolution, the thrown arm, the `exception=` field.
