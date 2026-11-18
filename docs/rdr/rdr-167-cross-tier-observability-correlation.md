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
> emits for that request. Since 2026-11-18 it also records the log sink escape (nexus-dngtz.1.11), which
> keeps a client value from starting a log line and which the readers' anchoring depends on.
> The readiness gap shipped by other work and is struck.
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

Gaps 1, 2, 4 and 5 describe the tree at develop `2e09b8cfc`, before Phase 1 shipped. Phase 1 addresses
Gaps 1, 2 and 5; the Technical Design below describes what was built.

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
publishes it in a `ThreadLocal` (set at `service/src/main/java/dev/nexus/service/http/AuthFilter.java:392`,
cleared in the `finally` at `:218-219`; `RequestContext.java:67-73`). As found, before
Phase 1, nothing in `service/src/main` called SLF4J's `MDC` (the per-thread key/value
map Logback can print on every line), and the log pattern had no MDC field (it was at
`service/src/main/resources/logback.xml:8`, and is at `:17` now). So a log line carries a tenant only
when a call site wrote `tenant={}` into the message by hand, which many do and some
do not (`CatalogHandler.java:593` logs the field and value but no tenant). A
correlation id from Gap 1 is useless unless it lands on every engine line.

#### Gap 3: No readiness signal distinct from liveness (CLOSED: shipped, struck from scope)

The draft asked for a `/ready` endpoint. The need was met another way, so this gap
no longer belongs to this RDR (`nexus_rdr/167-research-6`):

- `GET /livez` is liveness with no dependency (`NexusService.java:719`,
  `LivezHandler.java:8-43`; nexus-hubc0) and `GET /health` is readiness with a
  `SELECT 1` probe that answers 503 on failure (`NexusService.java:714`,
  `HealthHandler.java:32-58`).
- The process does not bind its HTTP port until Liquibase has migrated the schema
  (`Main.java:285`) and the local bge-768 model is loaded (`Main.java:488`
  precedes `new NexusService` at `:612` and `service.start()` at `:620`). A process
  that answers at all is migrated and has its local embedder.
- The supervisor tracks boot with a migration-aware state machine
  (`src/nexus/daemon/readiness.py:1-30`; nexus-8vp0i), and `/v1/status` reports boot
  work (`NexusService.java:742`).
- "Token registry warm" has no state to gate on: `AuthFilter` resolves each token
  through a cache per request (`AuthFilter.java:270`).

One leftover was a stale comment: `HealthHandler.java:16` said "liveness + DB
probe". Step 5 (nexus-dngtz.1.12) corrected it to say that `/health` is readiness
(a `SELECT 1` probe, 503 on failure) and that liveness is `/livez`.

#### Gap 4: Java logs are plain text on a synchronous appender (DEFERRED: optional later phase)

Logback writes plain text to stdout (`logback.xml:11-19`). For a managed deployment a
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
  root level INFO (`logback.xml:21`), so no line existed. The failure line added in
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

- **Python:** `structlog` configured in `src/nexus/logging_setup.py:176-190` (key=value
  renderer, rotating files per entry-point mode). The CLI group callback is
  `main` in `src/nexus/cli.py:176-194`; the MCP server's per-call hook is the `tools/call`
  middleware `_logged` in `src/nexus/mcp/_tool_call_log.py:270`.
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
- **Log consumers that parse engine output** (a pattern change must not break them).
  The log sink is the last step that turns a log event into text, just before the bytes
  are written; the sink change below (nexus-dngtz.1.11) works there. The supervisor
  recognises an engine log line by a leading date, time and level
  (`src/nexus/daemon/storage_service_daemon.py:1005-1006`). It finds a schema-migration
  marker (`event=schema_migration_start`, `_pending`, `_complete`, `_failed`) only as the
  first token of the message, meaning the text after the pattern's `] - ` up to the
  first space, and only on a line from the logger that emits it:
  `dev.nexus.service.db.SchemaMigrator` for start, pending and complete, and
  `dev.nexus.service.Main` at ERROR for failed (`src/nexus/daemon/readiness.py:122-169`;
  `Main.java:290`). Liquibase's own lines are not logback lines. "Running Changeset:" is bare
  text on its UI channel, and the changelog-lock wait is a `java.util.logging` (JUL) record whose
  second line reads `INFO: Waiting for changelog lock`; each counts only at the start of a line.
  A reason shown to the user is cut at the first literal `\x0a` or `\x0d`
  (`_reason_text`, `storage_service_daemon.py:1035`).

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
| Logback `logback-classic` 1.5.18 | Yes | `%X{key:-default}` is a constructor reference, `DEFAULT_CONVERTER_SUPPLIER_MAP.put("X", MDCConverter::new)` (`PatternLayout.java:107`), so it needs no native-image reflection entry; a run rendered `- - hi` with no MDC and `abc - hi` with the key set. `JsonEncoder` ships in logback-classic with an MDC block (`JsonEncoder.java:65,117,174-175`), so the JSON phase needs no `logstash-logback-encoder`. Those were JVM runs and source reads. The sink's three converters are different: `%msgSafe`, `%exSafe` and `%threadSafe` are our classes, named in `logback.xml` and made by Logback through reflection, so each needs a `reflect-config.json` entry and is initialized at image build time (Technical Design, Log sink). No native binary has printed the pattern or run those converters yet: `service/native-smoke.sh` now checks both (nexus-dngtz.1.9 and nexus-dngtz.1.11), and its first native run is the engine-service 0.1.195 release smoke on its linux and mac legs (Sam's ruling, 2026-11-17), so until then the native result is pending. |
| SLF4J `MDC` (`LogbackMDCAdapter`) | Yes, and spiked | `ThreadLocal`-backed; correct under the engine's server shape (research-1). Not inherited by child threads. |
| `structlog.contextvars` | Yes, and spiked | `bind_contextvars` plus `merge_contextvars` stamps every line; an event's own keyword overrides a bound value (research-5). Not used in the shipped design: one `ContextVar` and a structlog processor that reads it (see Technical Design). |
| `anyio` / `asyncio` thread hops | Spiked; live MCP server path run in nexus-dngtz.1.11 | `anyio.to_thread.run_sync`, `asyncio.to_thread` and `copy_context().run` carry the context; a bare `ThreadPoolExecutor.submit` does not (research-4). |
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
  and will not carry MDC: `PgVectorRepository.java:3187-3189` (search arms) and
  `CceEmbedder.java:573` (embed batches). As found, `AuthFilter` had early returns before its
  `try/finally`, so an MDC put at the top had to wrap the whole method body; the shipped
  `doFilter` (`AuthFilter.java:194-222`) does.
  *Source: `nexus_rdr/167-research-2`.*
- **✅ Verified** (source search) The client inventory for CA-2. Running the census in
  `tests/test_z0o2p24_client_version_header.py` found 36 modules under `src/nexus`
  that make an HTTP call. Six import `nexus.db.client_identity` and send its header:
  the T3 client (`http_vector_client.py:803`), the shared httpx mixin used by the T2
  stores, the catalog client and the pipeline client (`_refreshable_client.py:809`),
  `HttpTokenStore` (`http_token_store.py:186`), `HttpScratchStore`
  (`http_scratch_store.py:217`) and the two cloud backup modules
  (`backup/cloud_export.py:349,436,533`, `backup/cloud_import.py:230,311,675`; ten call sites in all). Thirty are
  named non-senders, 13 of them engine-bound probes, admin, token-mint or hook
  modules. The mixin and the T3 client build headers per request. `HttpScratchStore`
  and `HttpTokenStore` bake headers into their `httpx.Client` at construction
  (as found, at `http_scratch_store.py:643` and `http_token_store.py:193`), so a context
  variable read there would freeze at construction. Each is now built with a per-request
  request hook (`stamp_request`: `http_scratch_store.py:648`, `http_token_store.py:198`)
  instead. At research time the test file passed 78 of 79 without an engine (it now
  collects 142 tests); the one failure needed the engine substrate and was an environment
  gap.
  *Source: `nexus_rdr/167-research-3`.*
- **✅ Verified** (spike) The Python id survives the MCP tool path: with 50 concurrent
  coroutines each bound to a distinct id, sync bodies reached through
  `anyio.to_thread.run_sync`, `asyncio.to_thread` and `copy_context().run` all saw
  their own id (0 mismatches); a bare `ThreadPoolExecutor.submit` saw none. The
  spike drove `anyio` directly, not the live MCP server. The live server path was run
  later: `tests/test_correlation_id_mcp_stack.py` (nexus-dngtz.1.11) drives a real
  `MCPServer` through `mcp.client.Client` with the logging middleware installed, for one
  sync and one async tool, and checks that each call sees a fresh id and that nothing is
  left bound afterwards. It uses the `mcp` 2.x middleware API, which is provisional. The tree has 46
  modules that start threads (115 sites), so worker paths need `copy_context`, as
  `search_engine.py:757,3126` and `operators/llm_backend.py:548` already do.
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
  what that edge does with an id of its own. Nothing in the tree says. A local run now
  takes a client through a live engine to a log line (nexus-dngtz.1.10, see CA-2), but no run
  goes through a managed edge. The local case has no edge and does
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
- [x] **CA-2: A propagated `X-Correlation-ID` threaded through the Python clients reaches
  the engine and lands on its log lines.**
  **Status**: Verified for a local engine, which is the case Phase 1 covers. The managed
  edge and the native image are not covered (below). **Method**: source search, spikes
  and a live run. The client sites are enumerated and covered by one shared function plus
  two per-request request hooks (`167-research-3`); the id survives the Python thread hops
  (`167-research-4`) and the live MCP server path
  (`tests/test_correlation_id_mcp_stack.py`, nexus-dngtz.1.11); the engine side works
  (`167-research-1`). The cross-tier run is done (nexus-dngtz.1.10,
  `tests/test_correlation_cross_tier.py`): `nx -v memory list` with a bearer the engine
  never minted leaves the same id in the client's stderr and in the engine log's
  `request_failed` line. Still assumed: pass-through at a managed edge
  (`167-research-8`; Phase 2). Native evidence is pending: `service/native-smoke.sh`
  checks the id and the failure line on a native binary (nexus-dngtz.1.9), and its first
  native run is the engine-service 0.1.195 release smoke (Sam's ruling, 2026-11-17).
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
   group callback) and one per MCP tool call (in the `tools/call` middleware), keep it
   in one `ContextVar` that a structlog processor reads, so Python log lines carry it
   (structlog's own contextvars are not used), and send it as the `X-Correlation-ID` header
   on engine requests through the six sender modules found in research-3.
2. **MDC bridge at the engine (Gap 2).** At the top of `AuthFilter.doFilter`, read the
   inbound header (accept a well-formed one, otherwise generate one starting `nxe-`), put
   `correlation_id` in MDC, add `tenant` when the bearer token resolves and `session`
   once the session token is verified (both escaped by `LogText.mdcValue`), and
   clear everything in a `finally` that wraps the whole method. In that `finally`,
   write one `request_failed` line (WARN; a sent 404 with nothing thrown at INFO) for every response of an authenticated route with status 400 or above,
   for a request that sent no response, and for a request whose chain threw.
   Add the three keys to the log pattern. Echo the id in the response so a caller that sent none can still
   learn the id the engine made. Escape control characters at the log sink so no value a client chose can
   start a log line of its own (nexus-dngtz.1.11, see Log sink).
3. **Optional, Phase 3: JSON logs (Gap 4).** Switch the managed image to
   `JsonEncoder` behind a toggle. Taken up only with the managed deployment, and it must keep
   what the sink change gave the pattern layout (see Phase 3).

### Technical Design

**Names.** The header, the log field and the MDC key carry the same word:
`X-Correlation-ID` and `correlation_id`. Neither is `request_id` or `X-Request-ID`,
because RDR-222's idempotency key already owns `request_id` in both tiers
(research-5), and proxies and load balancers are commonly said to generate or
overwrite `X-Request-ID` (inferred, not read in this tree; research-9). Neither new
name appeared anywhere in `src` or `service/src` before Phase 1 (a case-insensitive `grep`
for `x-correlation-id` found nothing). `correlation_id` is also a field of the
mailbox tuple template (`service/src/main/resources/tuples/templates/mailbox.yaml:23,57`,
where it pairs a reply with its request), but no log call in either tier uses it as a
keyword (research-9), so it does not collide as a log field. The two ids differ in lifetime: the idempotency key is stable
across retries of one keyed write and is sent as `Idempotency-Key`
(`http_vector_client.py:818-819`); the correlation id names one operation, and a
`nx index repo` run that makes thousands of calls sends the same one on all of them.

**Python.**

- A new leaf module, `src/nexus/correlation.py` (standard library only, outside `nexus.db`,
  like `nexus/db/client_identity.py`, which documents why it must stay a leaf), holds the
  one `ContextVar`, `new_correlation_id()` (a UUID4 as 32 hex characters),
  `current_correlation_id()`, `bind` and `reset` (by token), `correlation_headers()`, the
  `stamp_request` httpx request hook and the `add_correlation_id` structlog processor.
- Binding. `cli.py:main` binds a fresh id beside `configure_logging` and registers
  `ctx.call_on_close` to reset it by token. A `try/finally` in the group callback would not
  cover the subcommand, because click returns from the callback before the subcommand runs;
  `call_on_close` fires when the context closes, after it. The `_logged` MCP middleware does
  the same per `tools/call` (a `finally` resets the token), so concurrent calls, each in its
  own task context, bind separate ids. `logging_setup.configure_logging` puts
  `add_correlation_id` first in the processor chain; it adds `correlation_id` unless the
  event already has that key. The first draft of this design called `bind_contextvars` and
  added `merge_contextvars`. It was replaced by this single store and a processor, so a reset
  leaves nothing bound and no second copy can drift.
- `client_identity_headers()` (the one function the six sender modules call, ten call sites in
  all) adds `X-Correlation-ID` only when an id is bound, so hook processes and probes that do
  not bind one send exactly what they send today and the exact-dictionary assertion in
  `tests/test_z0o2p24_client_version_header.py` (`:60`) still holds when no id is bound.
  The mixin and the T3 client call it per request and need no further change.
  `HttpScratchStore` and `HttpTokenStore` bake their headers into one long-lived
  `httpx.Client`, so each builds that client with `event_hooks={"request": [stamp_request]}`.
  The hook sets the header to the bound id on every request and removes it when none is
  bound, so a client built during one call never sends that call's id on a later one.
- The client's own failure line. `RefreshableHttpStoreMixin._raise_for_status`
  (`src/nexus/db/t2/_refreshable_client.py:1460`) logs `engine_request_failed` with `status=`
  and `path=` for every non-2xx response, and the processor stamps the bound id on it. It is
  logged at DEBUG, so it shows only with `nx -v`: it runs on every attempt, before a 401
  re-mint and before callers that treat a 400, 404 or 409 as control flow, so a louder level
  would be noise. An operator holding a failed default-level CLI command therefore reruns it
  with `-v` to read the id. Only clients built on the mixin write the line (the T2 stores, the
  catalog client and the pipeline client); the T3 vector client, `HttpScratchStore` and
  `HttpTokenStore` write no failure line, so for those the id reaches the client log only on
  lines the call sites already write.
- Thread pools. Four engine-bound pools on the `nx index repo` path submit through a fresh
  `contextvars.copy_context().run` per submit (a Context cannot be entered by two threads at
  once), as `search_engine.py:757` already does: `nx-index` (`indexer_utils.py`), `nx-flush`
  (`chunk_batcher.py`), the PDF pipeline's extractor, chunker and uploader submits
  (`pipeline_stages.py`) and `aspect-enqueue` (`db/t2/http_aspect_queue.py`). An AST pin
  (a test that parses the source into its syntax tree and fails when a pattern appears)
  over those four files (`tests/test_correlation_indexer_pools.py`) fails on a bare `submit`, a
  pool `map` or a shared Context. Other pools do not carry the id: the fan-out pools in
  `search_engine.py` (`:1920`, `:2008`, `:2261`, `:2330`, `:2366`) send none, so a multi-batch
  `nx search` joins nothing, and the same is true of any pool not listed. A bare `submit` loses
  the id (research-4); such calls go out without a correlation id and the engine generates
  one, so correlation degrades and nothing breaks.

**Engine.**

```text
// Illustrative; the real change wraps AuthFilter.doFilter (AuthFilter.java:194).
doFilter(exchange, chain):
    cid = accept(exchange.header("X-Correlation-ID")) or generate()   // generated: "nxe-" + 32 hex
    start = nanoTime(); thrown = null
    try:
        MDC.put("correlation_id", cid); exchange.responseHeaders.set("X-Correlation-ID", cid)
        ... existing steps 1 to 4; MDC.put("tenant", LogText.mdcValue(tenant)) as soon as the bearer token resolves;
        after step 3 verifies the session: if (sessionId != null) MDC.put("session", LogText.mdcValue(sessionId))
        // mdcValue escapes only the printed text; the token store and routing keep the raw values
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

- **Failure line.** For a request that goes through `AuthFilter` (not a path with no
  route, which the JDK server answers itself, and not the five unauthenticated routes),
  one line per response with status 400 or above, per request
  that sent no response, and per request whose chain threw, written in
  that `finally` before MDC is cleared, so the pattern prints the id, tenant and
  session on it. The tenant is there from the moment the bearer token resolves; a
  missing, non-Bearer or unknown token has no tenant, so the pattern prints `-`.
  Fields: `event=request_failed`, method, path (no query string, as the
  existing `event=auth_rejected` line already logs the path only,
  `AuthFilter.java:413-416`), status, exception and duration. Method and path are
  client text, so both are escaped with `LogText.escapeControl`: `getPath()` is the
  decoded path, so a `%0A` would otherwise write a second line, and the method token
  can carry control characters. A backslash is doubled and a control character is
  written as `\xNN`. The tenant and session values in the `[cid=... tenant=... session=...]`
  prefix are escaped too, by `LogText.mdcValue` (`escapeControl` plus space, `[`, `]` and
  `=`, each as `\xNN`), and so are the same two values in the `session_token_minted`,
  `session_token_closed` and `t1_session_closed` lines (`TokenStore`, `ScratchRepository`):
  `/v1/sessions/start` takes any non-blank session id from the client, and printed raw one
  could end the prefix early, write a second line or add a field such as
  `event=schema_migration_pending`. Raw values stay in the token store and in routing; only
  the printed text changes. These call-site escapes are the first of two layers: the log sink
  escapes again behind them (see Log sink), and it does not double a backslash, so the two do
  not stack. Where a handler threw
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
  (research-9). WARN is the right level: root is INFO (`logback.xml:21`), so WARN is
  printed, while the lines that exist today for these failures are DEBUG
  (the `auth_rejected` lines at `AuthFilter.java:298,318,414`, `PlanHandler.java:98`) and are not. A sent 404 with nothing thrown logs at INFO instead, still printed: the Step 3 measurement found 404 was 16 of 18 failure lines, and Sam ruled on 2026-11-17 that 404 goes at INFO and every other failure stays WARN. Cost: 404 is an
  ordinary answer on some routes (the `PlanHandlerTest` contract returns 404 for an
  empty project with no row, commit `3da30e2af`), so ordinary misses will be
  logged. Step 3 measured 52 failure lines per 1000 client calls on a scenario run of about
  350 calls (404 was 16 of the 18 lines). That is a scenario figure, not an indexing-volume
  one: the rate under `nx index repo` is not measured (inferred, not read, to be low there).

- **Accepting an id.** The header is client-controlled text that goes into a log
  line, so the engine accepts it only when it matches `[A-Za-z0-9._-]{1,64}`;
  anything else is replaced by an id the engine generates: `nxe-` and 32 lowercase hex
  characters (a UUID4 without hyphens). The `nx` client mints 32 hex characters with no prefix,
  so its ids never carry the marker, and the Phase 2 probe can tell a stripped inbound header
  from an overwritten one. This blocks log-line injection through the id. It covers only the
  id: the tenant and session values are not limited to that character set, so they are
  escaped when printed (see Failure line), not rejected, and every other value an engine line
  prints is escaped at the sink (see Log sink). `MDC` keys never carry a token or its hash (`credentialHash` stays out), only
  `correlation_id`, `tenant` and `session`.
- **Pattern.** The shipped pattern is
  `%d{ISO8601, UTC} %-5level [%threadSafe] %logger{36} [cid=%X{correlation_id:--} tenant=%X{tenant:--} session=%X{session:--}] - %msgSafe%n%exSafe`
  (`logback.xml:17`), with three `<conversionRule>` elements (`:6`, `:7`, `:10`) that name the
  classes behind `%msgSafe`, `%exSafe` and `%threadSafe` (a conversion rule maps a pattern word
  to the Logback class that renders it). Phase 1 inserted
  `[cid=... tenant=... session=...]` after `%logger{36}` and before ` - `; the sink change
  (nexus-dngtz.1.11) then replaced `%thread` with `%threadSafe` and `%msg` with `%msgSafe`, and
  put `%exSafe` after `%n`. The date, time and level stay at the front of the line and
  `event=...` stays the first token of the message, so the supervisor's recognisers keep
  matching (see the reader contract below). The escape of the tenant and session values keeps
  the prefix at exactly three fields, so a recogniser that anchors on the date, time and level
  or matches `event=...` in the message is unaffected. The Java test holds a literal copy of the
  supervisor's line regex (it must be re-confirmed there if the supervisor's regex moves); the
  Python tests pin sample lines of the new format against the real recognisers
  (`tests/daemon/test_storage_service_daemon.py`, `tests/daemon/test_readiness.py`).
- **Log sink (nexus-dngtz.1.11).** The census of the engine's log calls (T2
  `nexus/rdr-167-engine-log-client-value-census-2026-11-17`) found 877 calls, 359 of them printing a
  client-controlled value raw, and 174 more printing exception text, which can carry PostgreSQL
  error detail that echoes stored values. Escaping at each call site would never be finished, so
  Sam ruled (option b, 2026-11-17) that the engine escapes once, at the sink, in 8.4. The escape is
  `LogText.lineSafe` (`service/src/main/java/dev/nexus/service/http/LogText.java`): a control
  character (C0, 0x7F, C1 0x80 to 0x9F, so U+0085 too), U+2028 or U+2029 is written as `\xNN`
  (lowercase hex) below 0x100 and as `\uNNNN` above, and nothing else changes: it does not double a
  backslash, and it leaves space, `[`, `]` and `=` alone, because a call site has already escaped
  its own value with `escapeControl` or `mdcValue` and the sink must not escape that text again. A
  string with nothing to escape comes back as the same instance. Four paths reach it:
  - *Logback.* `%msgSafe` (`SafeMessageConverter`) writes the formatted message through it, so it
    covers every log call in the engine and every library that logs through Logback. `%exSafe`
    (`SafeThrowableConverter`) runs Logback's own trace renderer on a view of the throwable whose
    class name and message are escaped, for the exception, each cause and each suppressed one, so the
    shape is unchanged: the first line `<class>: <message>`, one `\tat ...` line per frame, and
    `Caused by:`, `Suppressed:` and the frame counts each on their own line. Listing it in the
    pattern stops Logback adding its plain `%ex`, so a trace prints once. `%threadSafe`
    (`SafeThreadConverter`) writes the thread name through `LogText.mdcValue`, so a name holding a
    line break, `]`, a space or `=` cannot end the first bracketed block early; `main` and
    `t1-ttl-sweep` print as before, a thread with no name prints `[]`, and Hikari's
    `HikariPool-1 housekeeper` prints as `HikariPool-1\x20housekeeper`. The effect on a reader of
    the log: a message that used to run over several physical lines, such as a PostgreSQL error with
    its `Where:` line, is now one line with `\x0a` for each line break. A message or exception text with nothing to
    escape is byte for byte what it was; the thread field is the exception, because `%threadSafe`
    also escapes space, `[`, `]` and `=` (the Hikari name above). `LogMasking` still swaps the encoder for
    `MaskingPatternLayoutEncoder` with the same pattern and masks the text the layout produced, so
    the secret mask runs after the escape.
  - *JUL.* Liquibase logs through `java.util.logging`, and so does the JDK HTTP server, through
    `System.Logger`, so neither reaches Logback. `LineSafeFormatter` extends the JDK's
    `SimpleFormatter`, and `LineSafeFormatter.install()` sets it on every `ConsoleHandler` of the
    JUL root logger. A record with nothing to escape formats byte for byte as the JDK formats it,
    its `java.util.logging.SimpleFormatter.format` property and the blank line after a trace
    included (for a throwable that does not override `toString`: the copy renders as `Throwable.toString`
    does); a record with an exception is formatted from a copy of the throwable whose messages
    are escaped, causes and suppressed ones too (followed to a depth of 32). A handler that is not a
    console handler is left alone. If no console handler exists `Main` logs
    `event=jul_line_safe_inactive` at WARN, and if the install throws the formatter logs
    `event=jul_line_safe_failed`; neither stops the engine.
  - *The Liquibase UI channel.* When a changeset fails Liquibase sends `ERROR: Exception Primary
    Reason:  <the PostgreSQL error text>` through its UI service, to stdout, past both Logback and
    JUL. `LineSafeUIService` extends Liquibase's `ConsoleUIService` and writes `sendMessage` and
    both forms of `sendErrorMessage` through `lineSafe`. `SchemaMigrator.update` runs the walk
    under a Liquibase scope that binds it (the test forks that set `nexus.liquibase.ui=logger`
    get Liquibase's `LoggerUIService` instead, and the engine never sets it). It pins
    `getOutputStackTraces()` to `false`, because the trace the console UI would print carries each
    exception's message raw; Liquibase's own default is already false and nothing in the engine
    turns it on, so the pin closes a path against a later change. "Running Changeset: <changelog
    file>::<id>::<author>" lines are unchanged: they carry the ids of the engine's own changelog
    files.
  - *Uncaught exceptions.* The JVM's default handler prints `Exception in thread "x" <class>:
    <message>` straight to stderr. `UncaughtExceptionLog.install()` makes a handler that logs
    `event=uncaught_exception thread=<name through mdcValue> exception=<class>` at ERROR through
    SLF4J, with the throwable as the last argument, so its message and its causes' pass through
    `%exSafe`; the message itself names the class only. It never throws. The engine's own code
    writes nothing to `System.out` or `System.err` outside the trust-anchors mode, which starts
    nothing. That is a grep result with no test behind it: a search of `service/src/main/java` for
    `System.out` and `System.err` finds the one use, `Main.java:75`, and a later one would not fail a
    build. Library code, and native libraries that write to stderr (ONNX Runtime can), are not audited
    (see What the sink does not cover).
  `Main.main` installs the handler and the JUL formatter right after the trust-anchors check and
  before `goOfflineAtStartup()`, the singleton lock, the secret masking and the migration, so
  nothing that fails early prints a raw line (`MainLogSinkWiringTest` pins the order on the text of
  `Main.java`, because `main` cannot run in a test. It checks the handler against the trust-anchors
  check, `goOfflineAtStartup()`, the secret masking and the singleton lock, and checks that the
  handler's install appears once; it checks the formatter against the trust-anchors check,
  `goOfflineAtStartup()` and the secret masking only, so the formatter's install-once and its place
  against the lock are not pinned).
  *Native image.* Each of the three converters has an entry in `reflect-config.json`
  (`allDeclaredConstructors` and `allDeclaredMethods`), because Logback makes a class named in
  `logback.xml` by reflection. `service/pom.xml` adds
  `--initialize-at-build-time` for the three converter classes and `LogText`, listed by class
  and not by package: `dev.nexus.service.log` also holds `SecretMask`, whose state must not be
  baked into the image. `LineSafeUIService` is made with `new` and needs no entry. A converter
  missing from the native image would show as `%PARSER_ERROR` in every line, which is why
  `LogbackNativeConfigTest` checks the entries and `service/native-smoke.sh` looks for it. The native
  result is pending until the engine-service 0.1.195 release smoke, as for nexus-dngtz.1.9.

- **Reader contract (nexus-dngtz.1.11).** The client half is in `src/nexus/daemon/readiness.py`,
  `src/nexus/daemon/storage_service_daemon.py` and `src/nexus/health.py`.
  - An engine logback line is recognised by `RE_ENGINE_LINE`: date, time, level, `[thread]`, logger,
    an optional `[cid=... tenant=... session=...]` block, then ` - ` and the message. The block is
    optional so an engine from before Phase 1, which may still be running during an upgrade, is
    still read. No prefix value can hold `] - `: the id has a grammar, and the tenant, session and
    thread values go through `mdcValue`.
  - A marker counts only as the first token of the message (the marker, then the end of the message
    or a space), from the logger that writes it, as `%logger{36}` prints it: start, pending and
    complete from `dev.nexus.service.db.SchemaMigrator`, failed from `dev.nexus.service.Main` at
    ERROR. Both names are 36 characters or fewer, so Logback prints them unabbreviated. Those
    loggers write their own literal first, so a client value is never the first token.
    `tests/test_engine_log_literals_pinned.py` pins the names, the 36-character bound, the levels
    and the place of the message converter against the Java.
  - `Running Changeset:` and the JUL `Waiting for changelog lock` count only at the start of a line
    (the JUL one may carry a `LEVEL: ` prefix). They start the MIGRATING phase, which has a longer
    wait bound, and name a changeset; they never mark a migration complete or failed.
  - A reason shown to the user is the first non-blank part of the text before a literal `\x0a` or
    `\x0d` (`_reason_text`), with credentials redacted and a length cap; `last_engine_root_cause`
    drops PostgreSQL's `ERROR: ` after the cut, so the RDR-231 halt cause ends at the runbook URL
    and not at the `Where:` text; the supervisor's schema-migration-failed message shows Main's line
    through the same function. `nx doctor` (`_last_boot_failure_detail`) reads the failing changeset
    only in the trace of Main's failed line, takes the first match (Liquibase's outer wrapper), turns
    escaped line breaks back into line breaks to find the SQL `ERROR:` one-liner, and reads a tail of
    1 MiB, up from 64 KiB, so a long `Failed SQL` trace cannot push Main's line out of it.
  - Anchoring holds only while no value can start a line of its own, which is what the sink gives:
    against an engine without the sink, a value carrying a newline and then a whole engine-shaped
    line would pass every anchor. The two halves ship in the same release.
  - Readers that only display, and already match at a line start, keep their own code
    (`last_engine_error_line`, `engine_boot_failure_reason`, `last_supervisor_error_line`,
    `_migration_halt_cause`, `upgrade_finish._start_failure_reason`). The first three show their
    line through `_reason_text`, so they get the cut at an escaped line break; `_migration_halt_cause`
    shows what `engine_root_cause` returns, which is cut the same way, and `_start_failure_reason`
    shows the output of `nx daemon service start`. The harness-run gate and
    rehearsal readers read engines the harness started itself, with fixed inputs and no outside
    client, and are out of scope.

- **What the sink does not cover, named.**
  - `Running Changeset:` lines are bare text on Liquibase's UI channel with no logback prefix, so the
    reader cannot check a logger for them. Only the ids of the engine's own changelog files reach
    them, and the line-start anchor plus the sink (no value can start a line) is what protects them.
  - JUL and UI output is not secret-masked. `LogMasking` swaps only Logback's `PatternLayoutEncoder`;
    the sink change escapes control characters in the JUL and UI text and masks nothing there. This
    is a confidentiality gap that predates the sink change.
  - Bidirectional and other Unicode format characters (category Cf, for example U+202E) are not
    escaped; only control characters (category Cc), U+2028 and U+2029 are. They cannot start a line,
    so the risk is a display spoof when a person reads the log.
  - The sink does not double a backslash, so text printed through it alone is not injective: a
    client-typed literal `\x0a` reads the same as an escaped newline, and a reader that cuts a
    displayed reason at the first literal `\x0a` can truncate it at client-typed text. Values escaped
    at a call site (`escapeControl`, `mdcValue`) do double it. The effect is a shorter reason on
    screen; no marker is read after the cut.
  - Anything that writes to the process's standard output or error outside Logback and
    `java.util.logging` bypasses the sink: library code that calls `System.out` or `System.err`, and
    native libraries that write to stderr (ONNX Runtime can). The engine's own code does not, outside
    the trust-anchors mode, and "none does today" is a grep result (`Main.java:75` is the only use in
    `service/src/main/java`) with no test behind it; the libraries are not audited.

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
| Correlation `ContextVar` and structlog processor | `src/nexus/logging_setup.py`, `src/nexus/cli.py`, `src/nexus/mcp/_tool_call_log.py` | New leaf module `nexus.correlation` for the id, a structlog processor and a request hook; extend the three call sites |
| Outbound `X-Correlation-ID` | `client_identity_headers()` in `src/nexus/db/client_identity.py`, six sender modules (ten call sites) | Extend the one function; add the `stamp_request` request hook to the scratch and token stores |
| Client failure line | `RefreshableHttpStoreMixin._raise_for_status` in `src/nexus/db/t2/_refreshable_client.py` | Extend: one DEBUG `engine_request_failed` line with status and path; the T3, scratch and token clients have none |
| Pin that every engine client sends it | `tests/test_z0o2p24_client_version_header.py` census (`_census`, `_NOT_SENDERS`) | Extend the same census so a new client must send or be named, and drive each of the ten call sites on the wire |
| MDC bridge | `AuthFilter.java:194-`, `CorrelationId.java`, `LogText.java`, `RequestContext.java` | Extend: accept or generate at the top, put inside a `try`, clear in a `finally` over the whole method |
| Log pattern | `service/src/main/resources/logback.xml:17` | Extend with three `%X{...:--}` fields; the sink change swaps `%thread`, `%msg` and the implicit `%ex` for `%threadSafe`, `%msgSafe` and `%exSafe` |
| Log sink escape | `LogText` (`escapeControl`, `mdcValue`), `LogMasking`'s encoder swap | Extend: `LogText.lineSafe`, three Logback converters, `LineSafeFormatter` (JUL), `LineSafeUIService` (Liquibase UI) and `UncaughtExceptionLog`, all in `service/src/main/java/dev/nexus/service/log/`; `LogMasking` unchanged and masks after the escape |
| Engine-log readers | `readiness.py` (`MigrationLogScanner`), `storage_service_daemon.py` (`last_engine_root_cause`, `_reason_text`), `health.py` (`_last_boot_failure_detail`) | Extend: markers anchored as the first token of the message from the emitting logger, displayed reasons cut at the first literal `\x0a` or `\x0d` |
| Log-capture test pattern | `ListAppender` in 56 test files, e.g. `OwnerlessWriteRefusalTest.java:657` | Reuse |
| JSON appender (Phase 3) | `logback-classic` `JsonEncoder` | Reuse; add its reflect-config entry; keep line-safety and reader compatibility, and account for the secret mask (Phase 3) |

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

- (+) One id finds a client operation's lines in the engine log, for the work that carries it.
  The four engine-bound indexer pools carry it (`nx-index`, `nx-flush`, the pipeline's
  extractor, chunker and uploader submits, and `aspect-enqueue`); the `search_engine.py`
  fan-out pools and any other pool do not, so a multi-batch `nx search` joins nothing, and a
  long-lived `nx` daemon command carries one id for its whole process. The same id is on the
  client's own structlog lines, but a failed request's client line exists only for the clients
  built on the shared HTTP store mixin, and only with `nx -v`.
- (+) Every engine line a handler already logs gains tenant, session and id.
- (−) A new engine client must send the header; the extended census test makes that
  a failing test and not a convention.
- (+) Every response of 400 or above from a route behind `AuthFilter` leaves one line with the id, so a client
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
  match a real engine line. The sink change swapped `%msg` for `%msgSafe` in the same place:
  `AuthFilterCorrelationTest` pins the pattern from `%logger{36}` to its end (the cid block, ` - `,
  `%msgSafe%n%exSafe`), `LogSinkEscapeTest` renders events through the shipped file and so covers the
  thread field (`%threadSafe`), and `tests/test_engine_log_literals_pinned.py` pins that a message converter, under any name, sits
  after `] - `.
- **Risk**: a client value forges an engine log line or a migration marker. **Mitigation**: three
  layers. Call sites escape the values they know are client text (`escapeControl`, `mdcValue`); the
  sink escapes control characters in every message and exception message, in JUL and Liquibase UI
  output and in uncaught exceptions, so no value can start a line; and the readers accept a marker
  only as the first token of the message from the logger that emits it. Residuals are named under
  Log sink: bare `Running Changeset:` lines, JUL and UI output that is not secret-masked, bidi
  format characters, a client-typed literal `\x0a`, and anything written straight to `System.out`
  or `System.err` (engine code does not, by grep and no test; libraries and native code such as ONNX
  Runtime can).
- **Risk**: a converter is missing from the native image, which would break every log line.
  **Mitigation**: `LogbackNativeConfigTest` checks that each `<conversionRule>` class has a
  constructor entry in `reflect-config.json`, and `service/native-smoke.sh` fails on `%PARSER_ERROR`
  in the native log; the native run is pending until the 0.1.195 release smoke.
- **Risk**: a worker thread drops the id. **Mitigation**: `copy_context().run` at the four
  engine-bound indexer pools, pinned by an AST test over four files; a missing id degrades
  to an engine-generated one. The `search_engine.py` fan-out pools and other pools are not
  covered, by choice.
- **Risk**: a managed edge rewrites or strips the header. **Mitigation**: unverified
  (research-8); the Phase 2 probe tests overwrite and strip as well as pass-through.
- **Risk**: the failure line is noisy, because 404 is a normal answer on some routes
  and a bad token repeats 401. **Mitigation**: the line carries no body or query
  string; Step 3 counted 52 failure lines per 1000 client calls on a scenario run of about
  350 calls, not at indexing volume, and Sam ruled that a sent 404 logs at INFO. The rate
  under heavy indexing is unmeasured; if lines dominate there, bring the level back to the gate.
- **Risk**: the raw client-chosen session id is still stored and sent. The engine escapes it
  only when it prints it; the id still goes into `session_tokens.session_id` (a bound
  parameter, so no injection). The client sends it in request bodies (`/v1/sessions/start`
  and the scratch routes such as `/v1/t1/get`); the `X-Nexus-T1-Session` header carries the
  minted session token, not the id. `/v1/sessions/start` does not reject an id outside a safe character set, because a server-side
  400 would be a wire change for older clients and for ids that come from outside our code (the
  Claude Code hook's `session_id`). **Mitigation**: the escape. A charset check, run first in
  log-only mode to measure real ids, is a candidate for a later release, after the 8.4 cut (nexus-dngtz.1.11).

### Failure Modes

- Missing, blank or malformed id: the engine generates one (`nxe-` and 32 hex); correlation
  degrades and the request is never refused.
- The engine echoes the id in `X-Correlation-ID` on the response, so a client with none
  can still read it.

## Implementation Plan

### Prerequisites

- [x] CA-1 verified (research-1, -2).
- [x] CA-2's end-to-end run is the Phase 1 minimum validation below, run before
  Phase 1 is called done, not before this RDR is accepted. Done in nexus-dngtz.1.10.

### Minimum Viable Validation

One service-backed operation from an `nx` command produces, in the client's
`structlog` output and in the engine's log, lines carrying the same `correlation_id`,
asserted by an automated test on the engine substrate and not by hand. The engine
line is the `event=request_failed` line, triggered by a request the engine answers
with a 4xx.

Outcome (nexus-dngtz.1.10, `tests/test_correlation_cross_tier.py`): the trigger is a 401
from a bearer the engine never minted. The test runs `nx -v memory list` in a subprocess with
`NX_SERVICE_TOKEN` set to a well-formed token the engine never issued, which `AuthFilter`
answers 401 (`unresolved_token`). The first candidate, a malformed document tumbler that
`CatalogHandler.rejectIfBadDocumentTumbler` (`CatalogHandler.java:615-621`) rejects with a
400, cannot be reached from `nx`: `register` sends no document tumbler (the engine mints it),
`show` and `update` resolve their argument first, a malformed one becoming a title search,
and `--alias-of` is parsed on the client. The client half is the DEBUG `engine_request_failed`
line, so the command runs with `-v`. The test reads the id from the client's stderr only, and
then looks for it in the engine log, so a missing client line cannot be hidden by whatever
id the engine made itself.

### Phase 1: Request id and MDC

#### Step 1: Python id leaf module and `structlog` binding
Create the leaf module; add the `add_correlation_id` processor first in the chain; bind at
`cli.py:main` (with `ctx.call_on_close` to reset) and in the MCP `_logged` middleware. Submit
through a fresh `copy_context().run` at the four engine-bound pools on the `nx index repo`
path: `nx-index`, `nx-flush`, the PDF pipeline stages and `aspect-enqueue`.

#### Step 2: Send the header
Extend `client_identity_headers()`; add the `stamp_request` request hook to
`HttpScratchStore` and `HttpTokenStore`; add the DEBUG `engine_request_failed` line to
`RefreshableHttpStoreMixin._raise_for_status` (nexus-dngtz.1.10); extend the census test so a
sender must send it.

#### Step 3: MDC bridge in the engine
Wrap `AuthFilter.doFilter`; validate the id and generate an `nxe-` one when needed; put
`correlation_id`, `tenant` (when the token resolves) and `session`, the last two through
`LogText.mdcValue`; echo on the response; in the
`finally`, write the `request_failed` line (WARN; a sent 404 at INFO) for a status of 400 or above, none or a
thrown chain (naming the exception class) and clear. Update
`logback.xml` (the pattern, and the sink's three conversion rules, Step 6). On the engine-substrate run, count failure lines per 1000 requests and
record the number in the bead (done in nexus-dngtz.1.6: 52 per 1000 client calls on a scenario
run of about 350 calls, which is not indexing volume).

#### Step 4: Tests
The Java and Python tests in the Test Plan, ending with the cross-tier test.

#### Step 5: Doc fixes
Correct the `HealthHandler.java:16` comment; add the field to the engine and logging
docs; keep the RDR-222 `request_id` fields unchanged and say so beside them. Done in
nexus-dngtz.1.12 (`docs/configuration.md` Logging, `docs/architecture.md` Request
correlation), which also matched this RDR's Technical Design and Phase 2 text to the reviewed
code.

#### Step 6: Log sink and reader anchoring (nexus-dngtz.1.11)
Sam's option (b) on the log-forgery census. Engine, develop `775bc535d`: `LogText.lineSafe`;
`%msgSafe`, `%exSafe` and `%threadSafe` in `logback.xml`; `LineSafeFormatter` on the JUL console
handlers; `LineSafeUIService` in `SchemaMigrator.update`; `UncaughtExceptionLog`; their
installs first in `Main.main`; the three `reflect-config.json` entries and the
`--initialize-at-build-time` list in `service/pom.xml`; the `native-smoke.sh` asserts. Client,
develop `b1345a3f1`, `a002fbe68`, `55b3b55c3`, `84fd68b0b` and `9a3175a30`: the anchored readers and the cut at a literal `\x0a`. The text is
under Technical Design (Log sink, Reader contract).

### Phase 2: Managed-deployment check (only with the managed deployment)

Extend `nx doctor --probe-edge` (`src/nexus/doctor_edge_probe.py`) to send a known
`X-Correlation-ID` and read back the id the engine echoes, the way the existing probe
compares the echoed `Idempotency-Key` with the one sent (`doctor_edge_probe.py:214-226`).
It reports four outcomes, not one, which the engine's `nxe-` prefix on an id it generates makes
distinguishable. The probe sends a valid id P without that prefix and reads the echo:
**pass-through** (echo equals P), **inbound strip** (the echo starts with `nxe-`: the engine
received no valid header and made its own), **overwrite** (an echo that is present, is not P
and has no `nxe-` prefix, so something on the path replaced the id) and **response strip** (no
echo, so the response header was removed on the way back). The engine side is built
(`CorrelationId.java`); the probe is not.
This closes the assumption in research-8.

### Phase 3 (optional, later): JSON logs

Switch the managed image to `JsonEncoder` behind a toggle; add its
`reflect-config.json` entry; run the native smoke; confirm the log consumers in the
Technical Environment. Taken up only with the managed deployment. The JSON encoder must keep two things
the sink change gave the pattern layout, and Phase 3 must account for a third that the engine already
has (nexus-dngtz.1.11):
- Line-safety. Every record is one physical line, with control characters escaped in the message, the
  MDC values and the stack trace, and no value able to start a line of its own.
- Reader compatibility. `RE_ENGINE_LINE` reads the pattern layout (date, time, level, `[thread]`,
  logger, the optional `[cid=...]` block, ` - `, message), and the marker rules anchor on it. A JSON
  line needs the readers changed and tested in the same change, or the supervisor stops seeing
  `schema_migration_*`.
- The secret mask, which Phase 3 must account for. `LogMasking` swaps only `PatternLayoutEncoder`,
  so a `JsonEncoder` appender is reported as unmasked (`event=log_masking_unmasked`) until the mask
  is extended to it. This is how the engine already works, not a new requirement of the sink change.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Correlation id | N/A (not stored) | `grep <id>` on client and engine logs | N/A | The cross-tier test | N/A |
| Log pattern | N/A | engine log lines | N/A | Pattern test | N/A |
| Log sink escape | N/A | a line break inside a message reads `\x0a` in the engine log | N/A | `LogSinkEscapeTest`; the native smoke | N/A |

### New Dependencies

None. `JsonEncoder` (Phase 3) is already in logback-classic (research-7). The sink change adds no
dependency: `LineSafeFormatter` extends the JDK's `SimpleFormatter` and `LineSafeUIService` extends
the `ConsoleUIService` that Liquibase already ships.

## Test Plan

- **Java, MDC on a line.** Through the real `AuthFilter` and a handler that logs, a
  `ListAppender` sees `correlation_id`, `tenant` and `session` in
  `getMDCPropertyMap()` (`AuthFilterCorrelationTest`).
- **Java, concurrency.** 400 concurrent requests with two tenants: each log event
  carries its own id and tenant; MDC is empty after the request. This is the shape of
  the spike.
- **Java, id handling.** A valid id is kept; an over-long or malformed one (a space, a trailing
  newline) is replaced; a request with none gets a generated `nxe-` id; the response header
  echoes it (`CorrelationIdTest`, `AuthFilterCorrelationTest`).
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
- **Java, escaping.** `AuthFilterLogEscapeTest`: a newline in the request path stays on
  one line in both the `request_failed` and `auth_rejected` lines; a control character in the
  method and in the claimed tenant header is escaped; the board-ci and mint-scope refusal lines
  are escaped. The rendered-prefix forgery test runs the shipped `logback.xml` pattern: a
  client-chosen session id carrying a newline, `]`, space and `=` cannot forge a line or add
  fields to the prefix, and the `session_token_minted` line prints it escaped. The close-line
  tests pin the same for `session_token_closed` (`TokenStoreSessionSweepTest`) and
  `t1_session_closed` (`ScratchRepositoryTest`), and `LogTextTest` pins `mdcValue`.
- **Java, pattern.** A formatted engine line starts with date, time and level and
  still matches the supervisor's line regex (`_ENGINE_LOGBACK_LINE`) and keeps its `event=...`
  in the message. The Java test holds a literal copy of that regex, so it must be re-confirmed
  in `storage_service_daemon.py` if the regex moves; the Python tests pin sample lines of the
  new format against the real recognisers (`tests/daemon/test_storage_service_daemon.py`,
  `tests/daemon/test_readiness.py`).
- **Java, sink.** `LogSinkEscapeTest` renders events through the shipped `logback.xml` pattern, once
  with the encoder the file builds and once with the masking encoder `LogMasking` swaps in. A newline
  and a forged engine line inside a message argument render on one physical line; a forged
  `] - event=schema_migration_failed` with no newline stays on one line and the real event token
  still comes first; an exception whose message and cause message hold forged lines renders
  escaped, with its frames on their own lines, once; a thread name holding a forged terminator
  and a newline renders escaped; CR, U+0085, U+2028 and U+2029 are each escaped; a normal line, a
  normal exception and an exception with no message render as before; a secret is still masked in
  the message and in the exception message. `LogTextTest` pins `lineSafe`: all 67 characters (C0,
  0x7F to 0x9F, U+2028, U+2029) in `\xNN` or `\uNNNN` form, no backslash doubling, space, brackets
  and `=` left alone, the same instance when nothing needs escaping, and `null` staying `null`.
- **Java, JUL and Liquibase UI.** `LiquibaseLineSafeTest`: a clean record formats exactly as the
  JDK's `SimpleFormatter` does, and a record with an exception does too, its closing blank line
  included; a newline in a message becomes `\x0a` in the same two lines; parameters are formatted
  and then escaped; a thrown exception keeps its frames and has its messages escaped, and a
  throwable whose `getMessage` throws still gets its record; `install` sets the formatter on a
  console handler and leaves a handler that is not one alone; the UI service writes a message with a
  newline as one line, never prints a stack trace even when asked to, and leaves a
  `Running Changeset:` message unchanged; a `System.Logger` record, the JDK HTTP server's path,
  reaches the root handler as one escaped line.
- **Java, uncaught exception and wiring.** `UncaughtExceptionLogTest`: a thread that throws a
  message with forged lines is logged once, at ERROR, through the engine's logger, as one escaped
  line that names the class only, with its frames on their own lines and a cause message escaped;
  `install` sets the default handler and a failing logger does not throw out of it.
  `MainBootEnvValidationIntegrationTest` now looks for `event=uncaught_exception thread=main`, in
  place of the JVM's `Exception in thread "main"` banner. `MainLogSinkWiringTest` pins that the
  handler is installed once and that the handler and the formatter come after the trust-anchors check
  and before `goOfflineAtStartup()` and the secret masking (the handler also before the singleton lock);
  the formatter's install-once is not pinned. `LogbackNativeConfigTest` pins that each
  `<conversionRule>` class in `logback.xml` has a constructor entry in `reflect-config.json` (and
  that the file names exactly the three converters, so it cannot pass on an empty list); it cannot
  show that the native image works.
- **Python, readers.** `tests/daemon/test_readiness.py`: a marker inside a client value is ignored; a
  marker mid-line, or from a logger other than the one that writes it, is ignored; the lock-wait flag
  is not sticky. `tests/test_health.py`: the doctor detail ignores the changeset marker inside a
  logged value. `tests/test_engine_log_literals_pinned.py`: the logger names, their 36-character
  bound, the levels and the place of the message converter match the Java.
  `tests/daemon/test_engine_log_post_sink.py` replays the RDR-231 halt and an uncaught exception as
  the sink renders them (`tests/fixtures/rdr231_halt/*_post_sink.txt`): the root cause ends at the
  runbook URL, the engine error line shows no text after an escaped line break, an uncaught
  exception gives its reason and not its forged second line, the doctor detail keeps the SQL
  one-liner and stops at the line break, a reason that starts with an escaped line break is not
  empty, and escaped and empty thread fields still parse.
- **Python, binding.** The CLI callback and the MCP middleware bind an id and leave nothing
  bound; two concurrent tool calls see different ids; `copy_context().run` carries it into a
  pool (`tests/test_correlation_id.py`). The binding is also pinned on a real `MCPServer`,
  through `mcp.client.Client`, for a sync and an async tool
  (`tests/test_correlation_id_mcp_stack.py`; the `mcp` 2.x middleware API is provisional).
- **Python, pools.** Each of the four engine-bound indexer pools shows the bound id inside
  its worker threads, and an AST pin over four files fails on a bare `submit`, a pool `map` or
  a shared Context (`tests/test_correlation_indexer_pools.py`). The `search_engine.py`
  fan-out pools are not in it.
- **Python, header.** Six sender modules call `client_identity_headers()` at ten sites; each of
  the ten has a driver (eleven wire drivers in all, since the catalog client shares the mixin's
  site; a wire driver is a test that runs the real client against a recording fake transport (an `httpx.MockTransport`, or a fake urllib opener for the T3 client)
  and reads the request headers it sent) that puts `X-Correlation-ID` on the wire when an id is bound and omits it when none is,
  including a baked client (the scratch or token store, whose `httpx.Client` fixes its headers
  at construction) built before or under a different id. The extended census fails on
  a client that does neither (`tests/test_z0o2p24_client_version_header.py`).
- **Python, collision.** The `keyed_sweep_*` lines in `http_vector_client.py` still
  show their idempotency key as `request_id` and the operation's id as `correlation_id`.
- **Cross-tier (MVV).** One real command against the engine substrate that the engine
  answers with a 4xx: `nx -v memory list` with a bearer the engine never minted (401). The same
  id is in the client's `engine_request_failed` line (DEBUG, so `-v`) and in the engine's
  `request_failed` line (`tests/test_correlation_cross_tier.py`).
- **Native image.** `service/native-smoke.sh` sends a bad bearer with `X-Correlation-ID`, expects
  a 401 that echoes it and a `request_failed` line with that `cid=`, and sends a malformed
  document tumbler to `/v1/catalog/register` and expects a 400 whose line carries a resolved
  `tenant=`. It has passed on the JVM jar; its first native run is the engine-service 0.1.195
  release smoke (nexus-dngtz.1.9, native-pending). The same script checks the sink
  (nexus-dngtz.1.11) with two probes. A bad-bearer request to `/v1/_whoami%0Aforged-sink-probe=1`
  (the decoded path carries a real newline, so `%0A` is the probe) answers 401 and leaves exactly one
  `request_failed` line with `path=/v1/_whoami\x0aforged-sink-probe=1 ` and no line that starts
  with `forged-sink-probe=1`. `AuthFilter` already escapes the path at the call site, so this probe shows
  that the call-site escape survives natively and would pass without the sink. The second probe is a
  POST to `/v1/vectors/search` naming an unregistered collection with a JSON `\n` in the name
  (`x\nforged-sink-raw-probe=1`): it answers 422, no row is read or written, and the engine's warnings
  print the name raw (no call-site escape), so exactly one line ends `collection=x\x0aforged-sink-raw-probe=1`
  and none starts with `forged-sink-raw-probe=1`. That is the probe that fails when `%msgSafe` becomes
  `%msg`; on the JVM jar it passes as shipped and fails with that one change, on 2026-11-18. The log also
  holds no `event=jul_line_safe_inactive`, `event=jul_line_safe_failed`, `%PARSER_ERROR` or
  `PARSER_ERROR[`. The first native run of all of this is the 0.1.195 release smoke, so it is
  native-pending too.

## Validation

### Testing Strategy

1. **Scenario**: cross-tier correlation on the engine substrate. **Expected**: shared
   id asserted (the MVV).
2. **Scenario**: concurrent requests, two tenants, Java suite. **Expected**: no id or
   tenant crossover.
3. **Scenario**: pattern change against the supervisor's recognisers. **Expected**:
   both still match.
4. **Scenario**: the log sink, a client value holding a newline in a message, an exception message or a
   thread name, and a reader given a forged marker. **Expected**: one physical line with `\x0a`, and the
   readers ignore the forged marker (`LogSinkEscapeTest`, `LiquibaseLineSafeTest`,
   `UncaughtExceptionLogTest`, `tests/daemon/test_readiness.py`, the native smoke).

### Performance Expectations

One map put and clear per request and three short fields per log line. The sink adds a scan of
each message, exception message and thread name for characters to escape, and a copy of the throwable's
message chain for a record with an exception (`%exSafe`, and the JUL formatter); a message with nothing
to escape builds no new string. No target is claimed for it either; the Java concurrency test also times
400 requests so a gross regression shows.

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

CA-1 is Verified by a spike and a source read (research-1, -2). CA-2 is
verified for a local engine: the site inventory, the Python thread behaviour, the live MCP
server path and the engine side are verified (research-3, -4, -1; nexus-dngtz.1.11), and the
live-engine run is done as the MVV (nexus-dngtz.1.10). The managed edge is still assumed
(research-8; Phase 2). CA-3 is unverified by design and belongs to the optional phase. CA-4 is
struck with Gap 3. Nothing is presented as measured that was not: the Python spike used `anyio`
directly (the live MCP server path was added later, in nexus-dngtz.1.11, as
`tests/test_correlation_id_mcp_stack.py` on a provisional `mcp` 2.x API), and no
native image has printed the pattern or run the sink's three converters yet. The native checks
exist in `service/native-smoke.sh`, and their first native run is the engine-service 0.1.195
release smoke, so that part is native-pending (Sam's ruling, 2026-11-17).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `MDC.put/clear` under virtual threads | SLF4J / Logback 1.5.18 | Spike (research-1) |
| `%X{key:--}` pattern | Logback | Source read and run (research-7) |
| `bind_contextvars`, `merge_contextvars` | structlog 25.5.0 | Spike and run (research-4, -5); not used in the shipped design, which reads its own `ContextVar` in a processor (`tests/test_correlation_id.py`) |
| `anyio.to_thread.run_sync`, `copy_context().run` | anyio 4.15.1 | Spike (research-4) |
| `JsonEncoder` | Logback 1.5.18 | Source read (research-7), Phase 3 only |

### Scope Verification

The MVV is in scope for Phase 1. Struck from scope with the reason recorded: the
readiness endpoint (shipped), the T2 daemon hop (retired). Deferred with a named
trigger: JSON logs (the managed deployment takes up observability shipping).
Not in scope: a line per successful request (failures of 400 or above get one), an id on the five unauthenticated contexts, and an id on lines logged from the two engine fan-out threads. Also not in scope, on the client: a failure line from the T3 vector client, `HttpScratchStore` and `HttpTokenStore` (only the mixin's clients write `engine_request_failed`, so a failure there is joined to the engine only through the engine line and the id the request sent), an id on the fan-out pools in `search_engine.py`, and an id on the threads a long-lived `nx` daemon command starts (the command carries one id for its whole process, and its threads carry none).

### Cross-Cutting Concerns

- **Versioning**: additive request and response headers; `/version` unchanged.
- **Build tool compatibility**: `%X` needs no reflection entry (research-7). The sink's
  `%msgSafe`, `%exSafe` and `%threadSafe` do: they are our classes, made by Logback by name, so each
  has a `reflect-config.json` entry, and each is initialized at image build time with `LogText`
  (by class, so `SecretMask` stays out). The Phase 3 `JsonEncoder` needs an entry too and is
  checked there.
- **Licensing**: no new dependency.
- **Deployment model**: the local engine needs no edge change; the managed edge is
  Phase 2.
- **Secret and credential lifecycle**: the id is client text; it is validated before
  it is logged, the tenant and session values are escaped when printed, and no token or token
  hash enters MDC. Every other value an engine line prints, and every exception message, has its
  control characters escaped at the sink; the secret mask (`LogMasking`) then runs over the escaped
  text, on Logback lines only. JUL and Liquibase UI output is escaped but not masked, a gap that
  predates the sink change.
- **Cloud backup clients**: the export and import clients (`backup/cloud_export.py`,
  `backup/cloud_import.py`, three `client_identity_headers()` call sites each) send the id to
  the cloud engine too. It is a random per-invocation token that names no tenant data, and the
  engine logs it so a failed backup call can be tied to its client line.
- **Memory management**: MDC is cleared per request; the id is not stored.
- **Compatibility with RDR-222**: the two ids are separate headers (`Idempotency-Key`
  and `X-Correlation-ID`) and separate log fields (`request_id` and `correlation_id`).

### Proportionality

Right-sized: about two to three days for the two pieces (a leaf module, one function
and two request hooks on the client; one filter, one pattern line and tests on the engine),
over seams that already exist. The sink escape (nexus-dngtz.1.11) came after that estimate: Sam ruled
it (option b, 2026-11-17) and accepted a delay of about a day to the 0.1.195 candidate. The heavy
option, full OpenTelemetry, stays rejected, and the JSON appender is not built until something needs it.

## References

- RDR-030 (logging policy), RDR-087 (observability surfaces), RDR-017 (progress).
- RDR-152, RDR-155, RDR-161, RDR-166 (service stack, native install, managed
  journeys); RDR-168 (closed; resolved `nexus-7y0ab`); RDR-222 (closed; owns the
  idempotency key and the name `request_id`).
- Engine: `service/src/main/resources/logback.xml`, `AuthFilter.java`, `CorrelationId.java`,
  `LogText.java`, `RequestContext.java`, `HealthHandler.java`, `LivezHandler.java`,
  `NexusService.java`, `Main.java`, `SchemaMigrator.java`, `service/native-smoke.sh`,
  `service/src/main/java/dev/nexus/service/log/` (the sink: `SafeMessageConverter`,
  `SafeThrowableConverter`, `SafeThreadConverter`, `LineSafeFormatter`, `LineSafeUIService`,
  `UncaughtExceptionLog`), `META-INF/native-image/reflect-config.json`.
- Client: `src/nexus/correlation.py`, `src/nexus/logging_setup.py`, `src/nexus/cli.py`,
  `src/nexus/mcp/_tool_call_log.py`, `src/nexus/db/client_identity.py`,
  `src/nexus/db/t2/_refreshable_client.py`, `tests/test_z0o2p24_client_version_header.py`.
  Engine-log readers: `src/nexus/daemon/readiness.py`, `src/nexus/daemon/storage_service_daemon.py`,
  `src/nexus/health.py`.
- Operator docs: `docs/configuration.md` (Logging), `docs/architecture.md` (Request correlation).
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
- 2026-11-17: Phase 1 code-match amendment (nexus-dngtz.1.12, standing grant 3: wording matched to reviewed code; status unchanged). Technical Design, Phase 1 steps and Test Plan now describe the merged code:
  - Client: one `ContextVar` read by a structlog processor in place of `bind_contextvars` and `merge_contextvars`; `call_on_close` at the CLI; a `stamp_request` hook in the scratch and token stores; the DEBUG `engine_request_failed` line (shown by `-v`, written only by the mixin's clients); the four named indexer pools (the `search_engine.py` fan-out pools and long-lived daemon commands carry no id).
  - Engine: the `nxe-` prefix and the four Phase 2 outcomes; `LogText` escaping of the path, method, tenant and session; the synthetic-handler, escape and close-line Java tests.
  - Tests: the live MCP server test; ten call sites and eleven wire drivers, including the cloud export and import clients; the cross-tier MVV (a 401 from a bearer the engine never minted; the malformed-tumbler 400 is unreachable from `nx`).
  - Risks and figures: the raw client-chosen session id as a risk; the 52-per-1000 figure scoped to a scenario run of about 350 calls.
  - Status: CA-2 is verified for a local engine. The native check (`service/native-smoke.sh`) is native-pending until the engine-service 0.1.195 release smoke (Sam's ruling, 2026-11-17).
  - Review round 1 (same bead): the `request_failed` scope (authenticated routes only), the session header (`X-Nexus-T1-Session` carries the token, not the id), stale sentences and line cites corrected.
- 2026-11-18: Sink amendment (nexus-dngtz.1.12, standing grant 3: wording matched to reviewed code; status and decisions unchanged). Sam's ruling on the engine log-forgery census, option (b) (2026-11-17, by dialog in halcyon's session): escape at the log sink in 8.4, accepting a delay of about a day to the 0.1.195 candidate. Landed as develop `775bc535d` (engine) and `b1345a3f1`, `a002fbe68`, `55b3b55c3`, `84fd68b0b` and `9a3175a30` (readers). This amendment describes it:
  - Technical Environment and Technical Design: the readers find a marker only as the first token of the message from the logger that emits it; the shipped pattern with `%msgSafe`, `%exSafe` and `%threadSafe` and its three conversion rules; the Log sink (control characters to `\xNN` or `\uNNNN`, no backslash doubling; the JUL formatter; `LineSafeUIService`; the uncaught-exception handler; install order; native image); the Reader contract; and the named residuals (bare `Running Changeset:` lines, JUL and UI output not secret-masked, bidi format characters, a client-typed literal `\x0a`, and anything written straight to `System.out` or `System.err`, which bypasses the sink: engine code does not, by grep with no test, and libraries and native code such as ONNX Runtime are not audited).
  - Risks, Phase 1 Step 6, Test Plan (Java sink, JUL and UI, uncaught and wiring, Python readers, native smoke with two sink probes: a path value the call site escapes, and a collection name only the sink escapes), Cross-Cutting and the Existing Infrastructure Audit rows.
  - Phase 3: a JSON encoder must keep line-safety and reader compatibility, and Phase 3 must account for the secret mask (`LogMasking` applies it to the pattern layout only).
  - Native: the three converters' `reflect-config.json` entries and the `--initialize-at-build-time` list are in; the native result is pending until the engine-service 0.1.195 release smoke, as for nexus-dngtz.1.9.
  - Line cites re-read in this tree: `logback.xml:17`, `:21`, `Main.java:285`, `:488`, `:612`, `:620`.
