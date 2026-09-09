---
title: "Linda Tuple Space over Postgres: A Coordination Primitive for Agents and Instances"
id: RDR-205
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-09
accepted_date:
related_issues: []
related_rdrs: [RDR-041, RDR-105, RDR-110, RDR-116, RDR-117, RDR-120, RDR-127, RDR-149, RDR-152, RDR-155, RDR-158, RDR-184, RDR-204]
---

# RDR-205: Linda Tuple Space over Postgres

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

A tuple space is a shared bag of typed records that any process can add
to, read from, or take out of, where "take" is atomic: when two processes
try to take the same record, exactly one gets it. Linda (Gelernter, 1985)
named the four operations `out` (add), `read`, `in` (take) and `eval`; the
non-blocking probe forms came later. The value of the abstraction is that
a work queue, a mailbox, a lock, a barrier and a request-reply channel are
all the same three operations over different record shapes.

Nexus runs many cooperating processes: an orchestrating Claude Code
session, the sub-agents it dispatches, `claude -p` subprocesses, the
hooks that fire around every tool call, and a second instance (the
conexus session on the same box, working the plugin's own repository
against the same managed service). None of them has an
atomic take. The coordination they do have is improvised in flat files
and shell libraries, each keyed on whatever the harness happens to expose,
and the measured cost since July is concrete: reports that never arrive,
messages that land after the decision they were meant to change, and a
cross-instance request that sat unacknowledged for five weeks.

RDR-110 shipped exactly this primitive in May 2026 over SQLite and Chroma,
and the whole arc it belonged to was scrapped two days later for scope
entanglement (its scrap reason counts nine RDRs and 67 stranded beads;
beads are the project's issue tracker). The
design survived every gate it faced; its substrate did not survive the
year. Every mode of nexus now runs one Postgres 17 with pgvector behind
the Java engine, and the engine already claims one work queue with the
exact statement a tuple space needs. This RDR brings the primitive back
on that substrate, with one table, two consumers, and a scope clause.

### Enumerated gaps to close

#### Gap 1: No atomic take a client can reach

The engine's `aspect_extraction_queue` is claimed with `SELECT … FOR
UPDATE SKIP LOCKED LIMIT 1` (`AspectRepository.claimNext`), which is an
atomic take over one fixed record shape, reachable only by the aspect
worker. Nothing a session, a hook, a skill or a sub-agent can call has
the operation. Scratch (T1, the session-scoped store) and memory (T2, the
project-scoped store) are read-multiple: two readers of the same entry
both see it, and neither can claim it.

#### Gap 2: Dispatched agents owe reports, and the ledger cannot name them

RDR-184 recorded ten occurrences (its §Context, "by final count") of an agent finishing without
reporting, one of them a failed validation-run result that sat unreported
until the orchestrator read the agent's raw output file. The remedy that
shipped, the expectations ledger, is a per-session TSV (a tab-separated
text file) written by a bash hook and keyed on the agent's type only,
because the dispatch payload carries no per-instance key; two dispatches
of one type are matched N-of-type. A hand-called declaration convention
measured zero pairable rows across twenty-five dispatches. The ledger
works, and its census is scripted, but it cannot say which agent of a
type owes the report, and it cannot be queried across sessions or from
another process.

#### Gap 3: Messages to an agent land only between turns

RDR-184 Gap 2: a directive sent to an agent mid-turn is absent from its
next hand-back, because harness delivery lands between turns and the
agent composes against stale scope. The shipped mitigation is a rule to
re-check the inbox before composing. There is no store an agent can read
on demand that holds messages addressed to it by a stable id.

#### Gap 4: Coordination state that should be queryable lives in a per-session file

The ledger is the source of truth for "who owes what" and it is a file
that only a shell function on the same box can read. A cross-session
question ("did any agent in the last five sessions idle without
reporting?") is a loop over files. This gap is specifically about the
ledger. Four other flat files in the same neighbourhood are not gaps and
stay files; §Relationship to Prior RDRs and §Research Findings say why for
each.

#### Gap 5: Cross-instance requests have no delivery

A request from the conexus instance (a Claude Code session working the
conexus repository) to the nexus instance is relayed by hand, and
conexus-bv5z sat five weeks before it was acknowledged (nexus-w374z
mechanised the sweep that found it). Both instances are sessions on the
same box, minting against the same tenant, `nexus` (verified 2026-09-09,
§Key Discoveries). So the missing delivery is a mailbox addressed to an
instance rather than to an agent, with the same take-and-ack semantics;
no cross-tenant channel is needed, and none is designed here.

## Relationship to Prior RDRs

Scanned the index for: tuple, Linda, coordination, queue, claim, lease,
mailbox, orchestration, scratch, session, daemon, substrate.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-110 (abandoned 2026-05-19) | Origin | It named the abstraction and shipped it over `tuples.db` (SQLite) plus a Chroma collection per subspace, with `UPDATE … RETURNING` under SQLite's single-writer lock and a 1 ms `PRAGMA data_version` poll for blocking take. Its stated rationale for that substrate ("T2 is the shared bus, SQLite WAL is multi-process safe") expired when RDR-152/155/158 retired SQLite and Chroma. Its design kernel (registered schemas, lease plus ack/nack with an append-only claim log, exact match for locks, floors as a safety knob) still holds and is carried forward. The lifecycle table has no edge out of `abandoned`, so this RDR cites 110 and does not supersede it. |
| RDR-120 (closed 2026-05-27) | Origin of the moratorium | §Scope Boundaries lists "tuple-space primitives: atomic in/out/rd, blocking take" as blocked until 30 days after its P6 under `NX_STORAGE_MODE=daemon`, lifted by "a follow-on RDR if any consumer is wanted". Its implementation notes record that the calendar lift was replaced by the substrate stress-validation arc (nexus-57pwo, closed), and the daemon mode it names was retired by the PG migration. The lift clause is the authority for this RDR existing. Its scope rule is no co-shipped consumers at all; this RDR adapts it to two named consumers and no others, in §Approach, and says so rather than claiming verbatim adoption. |
| RDR-184 (orchestration protocol hardening) | Precedent | Diagnosed Gaps 2 and 3 with counts and shipped the flat ledger and the inbox rule. This RDR does not re-diagnose; it gives those remedies a key and a store. The ledger's fail-open hook contract is inherited unchanged. |
| RDR-105 and JDR-001 (T1 identity, three scopes) | Adjacent, untouched | The T1 session lease file holds the MCP server's minted credential so a CLI borrows it instead of re-minting; `current_session` is the no-harness fallback; the handoff marker is a hook message to a live MCP server. A draft of this design proposed moving them into the space and a review pass withdrew it: a reader needs a credential before it can read a tuple, and the identity inventory (T2 `nexus/s1-t1-identity-inventory-2026-08-22`) rules KEEP. This RDR consumes session identity from that layer and does not own it. |
| RDR-152, RDR-155, RDR-158 | Origin of the substrate | The engine, the PG-only storage rule, and the removal of the SQLite and Chroma paths. The `aspect_extraction_queue` claim in RDR-152's engine is the proven statement this RDR generalises. |
| RDR-116 and RDR-117 (drafts at 8cce803b7 on the archive branch, never accepted; the RDR-110 post-mortem calls them accepted, which their frontmatter contradicts) | Precedent | 116 recorded the single-writer-lock ceiling (every op serialised on one connection, modelled at 70 to 300 ops/s) and cross-subspace wake amplification (one event per file woke every parked caller); both are answered by construction here with one transaction per call, `SKIP LOCKED`, and a per-subspace waiter. 117 recorded that the tuple store shipped with no backup or restore story; the PG substrate closes it, and its one design statement survives in §Cross-Cutting Concerns: after a restore, active claims are re-earned by lease lapse and the claim log is the only history. |
| RDR-149 (service registry) | Adjacent, untouched | Daemon discovery and lifecycle stay in the shared primitive. Nothing here changes how the engine is found. |
| RDR-204 (embedding profile, ghost sweep) | Precedent | Its ghost-sweep and quarantine-reclaim pattern is the retention model for tuples. Its embedding profile is keyed per tenant, not a global constant per content type (RDR-204 §Critical Assumptions), which is why v1 ships no embedding column: a later semantic column must consult that profile and follow the per-dimension nullable pattern `nexus.chunks` already uses. |
| RDR-127 (surface rendering is downstream) | Lineage | The one surviving successor of the RDR-110 arc. It decided nexus ships no surface rendering, which is why nothing about surfaces, the ORB, or cockpit projection appears here. |
| RDR-041 (scratch tag vocabulary) | Precedent | SHOULD-not-MUST vocabularies drift. Subspace schemas here are registered and checked at engine boot. |

## Context

### Background

The revival was brainstormed on 2026-09-09 (artifact "Linda over Postgres",
T2 `nexus/rdr-110-revival-brief-2026-09-09`) and the brainstorm was put
through three review passes before this draft: a substantive critique
(T2 `nexus/critique-linda-over-postgres-brainstorm-2026-09-09`), a
confirmation pass (`…-r2-2026-09-09`), a repo fact-check
(`nexus/factcheck-linda-over-postgres-brainstorm-rev3-2026-09-09`) and an
external fact-check (`nexus/factcheck-linda-over-postgres-external-claims-2026-09-09`).
Two proposals were withdrawn by those passes and are recorded here so they
are not re-proposed: moving the build lease into the space (it guards
building the engine and must work with the engine down), and moving the
T1 identity files into the space (they carry the credential a reader
needs first).

What has not changed since May: every coordination need in Gaps 1 to 3 is
as open as it was, and the three shapes RDR-110 named (work queue,
mailbox, lock) are still the shapes that come up.

### Technical Environment

- Engine: Java 25, HTTP server on a virtual-thread-per-task executor
  (`NexusService.java:485`), one single-thread sweep scheduler
  (`:494`), jOOQ 3.20.11 (`service/pom.xml:16`), raw SQL in Java banned by
  `RawSqlGateTest`. Schema through Liquibase only.
- Postgres 17 with pgvector in every mode: bundled locally, managed in
  cloud. Every tenant table runs forced row-level security keyed on the
  per-transaction tenant setting (`TenantScope`). The engine refuses to
  bind if an interposed PgBouncer is not in transaction mode
  (`PoolerModeCheck`). Production connects to Postgres directly
  (verified 2026-09-09 on the engine host: a `:5432` JDBC URL, no
  `NX_PGBOUNCER_*` keys, `pooler_mode_check_skipped` at boot), so
  `PgBouncerTenantIsolationTest` describes a topology that is not
  deployed and `BackendReaperIntegrationTest` is right.
- Cloud topology (verified 2026-09-09 by the conexus session): one engine
  JVM on one EC2 host with an nginx TLS sidecar; deploys are
  stop-then-start with a measured gap of about 25 seconds during which
  the edge returns 502 or 504; there is never a window with two engines
  live. In front of the engine, three timeouts in series: the ALB (the
  load balancer) idle timeout of 60 s, the control plane's upstream response-start timeout
  of 30 s (an env knob, `CONEXUS_UPSTREAM_REQUEST_TIMEOUT_MS`) with a
  60 s exchange deadline, and the sidecar's 120 s read timeout. The WAF (the
  web application firewall) rejects request bodies over 8 KB. `/v1/*` forwards the bearer token
  unvalidated to the engine, whose `AuthFilter` is the check.
- Client: Python 3.12, `HttpScratchStore` for T1, HTTP stores for T2,
  MCP tools in `nexus.mcp`, hooks in `conexus/hooks/scripts/`.
- Harness facts (Claude Code 2.1.251 in `expectations.sh`; 2.1.266
  re-measured in research 5): the `SubagentStart` payload carries an
  opaque per-instance `agent_id` and the `subagent_type`; the
  `SubagentStop` payload carries the same `agent_id`, an
  `agent_transcript_path` named by it, `last_assistant_message` and
  `stop_hook_active`; the Agent tool has no name parameter; the
  `PreToolUse` payload's `tool_use_id` is absent from `SubagentStart`.
  Two scripts are registered on `SubagentStart`: `subagent-start.sh`
  injects context through the `hookSpecificOutput.additionalContext`
  envelope and does not read `agent_id`; `subagent-start-stamp.sh` reads
  `agent_id` for the ledger and is contractually silent on stdout.
  Command hooks accept `async: true` on every event, all matching hooks
  run in parallel, and Claude Code waits for a blocking hook's stdout
  and stderr to close, not for its exit.
- Engine HTTP layer: `HttpUtil.send` computes the whole body and commits
  headers and body together; there is no streaming response path. The
  Hikari pool defaults to ten connections (`NX_POOL_SIZE`).
  `RequestContext` and `RequestDeadline` are embed-budget machinery only
  and have nothing to do with a parked read.
- This box binds tenant `nexus` with a mint-locked credential
  (`~/.config/nexus/config.yml`; `config.py:569-577`).

## Research Findings

### Investigation

The design was derived from RDR-110's text, the engine's queue
implementation, the RDR-184 ledger and hook scripts, JDR-001, and the
published record of Postgres-backed job systems (Que, pgmq, Quartz,
db-scheduler, JobRunr, Oban, Graphile Worker, River, Solid Queue), each
checked against its source by the external fact-check. Load was measured
from the 44 session ledgers on this box.

Six research passes ran in parallel on 2026-09-09, each recorded in T2
under `nexus_rdr/205-research-N`: (1) the engine-side implementation
map; (2) the client, MCP, CLI, ledger and hook-path map; (3) the CA 1
spike on the bundled Postgres 17.5; (4) what transfers from the May
implementation on the archive branch; (5) the CA 2 and CA 4 hook spikes
on Claude Code 2.1.266; (6) blocking reads through the edge, the in-JVM
waiter, deploy-gap retry and the deferred `LISTEN` path.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Postgres row locking | Yes (docs, explicit-locking) | `FOR NO KEY UPDATE … SKIP LOCKED` is valid, sufficient for an update that does not touch key columns, and the locked select cannot block; the enclosing transaction can still deadlock on other locks. Clause order is `ORDER BY … LIMIT n FOR NO KEY UPDATE SKIP LOCKED`. |
| Postgres HOT updates | Yes (docs, storage-hot; `relcache.c`) | An update that changes a column in a partial index's predicate is not HOT-eligible. A predicate on `claim_state` or `consumed_at` makes every claim and ack a non-HOT update. |
| LISTEN/NOTIFY under PgBouncer | Yes (PgBouncer docs) | `LISTEN` is unsupported in transaction pooling mode. Oban ships a non-Postgres notifier for exactly this. |
| jOOQ 3.20.11 | Yes | `forNoKeyUpdate()` since 3.12; chains `.skipLocked()`. |
| `AspectRepository` | Yes (`:1056-1132`, `:1206`) | `claimNext` is `forUpdate().skipLocked()` inside one tenant-scoped transaction; `claimBatch` loops `claimNext`, one transaction per row; `reclaimStale` releases in-progress rows past a timeout. The table carries a foreign key (`fk-003`) and forced RLS, and passes the transaction-mode PgBouncer isolation test. |
| Liquibase changelog | Yes | No changeset sets a per-table autovacuum factor or fillfactor today; this RDR introduces the first. |
| Claude Code hook payloads | Yes (`expectations.sh:20-63`, `agent-dispatch-expect.sh:7-36`; research 5 spike at 2.1.266) | Per-instance `agent_id` is present and equal on `SubagentStart` and `SubagentStop`; `additionalContext` injection round-trips it; the docs' `agent_name`, `agent_model` and `stop_reason` are absent from measured payloads and nothing keys on them. |
| Claude Code hook runner | Yes (docs § Run hooks in the background; research 5 spike) | A blocking hook is done when its stdout and stderr close; a child inheriting them holds the dispatch. `async: true` hooks are never read, never timed out, and killed without grace at session end. |
| Engine HTTP and pool | Yes (`HttpUtil.java:18-25`, `Main.java:71`, `NexusService.java:485`) | Headers and body commit together, no streaming variant; `NX_POOL_SIZE` default 10; virtual-thread-per-task executor. |
| `nexus.retry`, `edge_refusal.py` | Yes | 502, 503, 504 and 429 already classified retryable; edge-signed 5xx rendered as transient. Reused, not reinvented. |
| `subagent-start.sh`, `subagent-start-stamp.sh`, `subagent-stop.sh` | Yes | Injection and ledger stamping are two scripts on one event; the stop hook already keys on the Stop payload's `agent_id` (2,083 of 2,083 stop-side ledger rows match a start row). |
| pgvector | Yes (`vectors-004-unify-chunks.xml:267-269`) | `nexus.chunks` carries three nullable per-dimension `vector(N)` columns; that is the pattern a later semantic column follows. No vector column in v1. |

### Key Discoveries

- **Verified** — The single-row atomic claim this RDR needs runs in
  production today on a table with a foreign key and forced RLS, through
  a transaction-mode pooler (`AspectRepository.claimNext`,
  `PgBouncerTenantIsolationTest`).
- **Verified** (T2 `nexus/rdr-110-revival-brief-2026-09-09`, from the
  confirmation critique's count) — Load on this box, from 44 session
  ledgers: 687 EXPECT rows, 3,443 rows in all, a mean of 16 dispatches
  per session and a peak of 78, busiest ledger 391 rows. Derived from
  those counts, not measured against a clock: peak coordination traffic
  is under ten operations a second. The design is insensitive to these
  numbers; they are recorded so the RDR is not argued from estimates.
- **Documented** — Published Postgres-queue throughput (Que 7,690
  claims/s on PG 9.3, 32 vCPU, advisory locks; pgmq about 30,000
  messages/s on 16 vCPU with aggressive autovacuum and an unlogged
  option) sits three orders of magnitude above that load. Neither
  number transfers to this mechanism or hardware; the point is that
  throughput is not the axis to optimise.
- **Documented** — What broke Postgres-backed queues in the record was
  bloat under churn plus long transactions holding vacuum back (Brandur
  Leach, "Postgres Job Queues & Failure By MVCC", 2015), a global lock
  row (Quartz `QRTZ_LOCKS`), connection-held locks leaking on worker
  death (Que's advisory locks), and the notification channel under a
  pooler (Oban). Each has a specific answer in §Technical Design.
- **Verified** — RDR-110's paraphrase-fuzz spike for semantic take
  (CA #4) ran and passed in May (PR #769, nexus-tq96, zero false
  positives) on MiniLM over Chroma. The RDR's checkbox never flipped.
  The result does not transfer to pgvector and Voyage; it is re-earned
  before any semantic destructive read ships, which this RDR does not.
- **Verified** — The taxonomy-015 deadlock was a same-transaction lock
  upgrade (an insert's `FOR KEY SHARE` on foreign-key parent rows, then
  a trigger's `FOR UPDATE` on the same rows). It does not apply to a
  claim under `SKIP LOCKED`, and is not a reason to ban foreign keys on
  the tuple table.
- **Documented** — The T1 session lease file holds the MCP server's
  minted `(session_id, session_token)` so a CLI borrows it rather than
  re-minting, which would rotate the live token (`t1.py:836-848`,
  nexus-5daww). It is a credential cache, not discovery state, and
  cannot be a tuple.
- **Verified** (research 3, spike on the bundled PostgreSQL 17.5, ten
  concurrent `psql` claimants, a 50 ms sleep injected between the select
  and the update) — 200 of 200 rounds produced exactly one winner and
  nine `None` under `FOR NO KEY UPDATE SKIP LOCKED`, and 50 of 50 under
  `FOR UPDATE SKIP LOCKED`; zero deadlocks. Ten workers drained 10,000
  rows in 5.15 s with claim latency p50 3.96 ms and p99 6.01 ms through a
  local `psql` round trip, every row acked exactly once. Those latencies
  are the statement's cost, not the end-to-end cost through the engine
  and the edge.
- **Verified** (research 3) — With the partial index on `(tenant_id,
  subspace, created_at) WHERE consumed_at IS NULL AND claim_state IS
  NULL`, 2,000 claim and ack updates were 0 % HOT (heap-only tuple
  updates, the kind that skip index maintenance); with the index dropped
  the same workload was 48.6 % HOT. Both state transitions write
  predicate columns, as the design predicted.
- **Verified** (research 3) — Bloat under churn: 100,000 claim, ack and
  reopen cycles on a 1,000-row table (300,000 non-HOT updates, committed
  every 200 cycles) left 300,000 dead tuples with default autovacuum and
  zero after one naptime once churn stopped; with
  `autovacuum_vacuum_scale_factor = 0.01` autovacuum ran twice during the
  churn. The default self-heals at this load; the tuned factor's
  measured benefit is reclaim during sustained churn, not rescue from
  unbounded growth. A sweep that holds one long transaction across many
  cycles defeats either setting.
- **Verified** (research 1) — The engine's claim uses
  `forUpdate().skipLocked()`; `forNoKeyUpdate()` exists in jOOQ 3.20.11
  (`SelectForUpdateStep`) and has no call site anywhere in the codebase.
  It is new code with `forUpdate()` as the fallback if it surprises.
  `RawSqlGateTest` binds any new repository to the generated DSL with no
  exception path. No changeset sets a per-table autovacuum factor today;
  it is a first and the changeset comment says so. `nexus.chunks` already
  carries three nullable per-dimension `vector` columns
  (`vectors-004-unify-chunks.xml:267-269`), so a nullable vector column
  is not a first; the round-1 gate caught an earlier claim to the
  contrary, which had read a rollback block as the live table.
- **Verified** (research 1; cadence from `NexusService.java:78`) —
  RDR-204's ghost sweep is triggered from `AuthFilter` once per tenant
  per JVM lifetime, a different mechanism from the `sweepScheduler` in
  `NexusService`, which runs every `SWEEP_INTERVAL_HOURS` (six hours
  today). The scheduler builds one tenant set before its arms, the
  default tenant plus every row in `service_tokens`
  (`NexusService.java:558-580`), which works only because that table has
  no row-level security; the engine's service role `nexus_svc` is
  `NOBYPASSRLS` (`role-001-nexus-svc.xml:36-42`; the diagnostics role
  `nexus_diag` bypasses RLS for integrity checks only, RDR-182, and the
  engine never runs on it), so the engine cannot enumerate tenants across
  a forced-RLS table such as `nexus.tuples`, and the same cycle deletes
  expired token rows, so that set is not the tuple table's either. The
  tuple sweep therefore enumerates from its own small table with no
  row-level security, `nexus.tuple_tenants` (one row per tenant that has
  ever written a tuple, upserted by `out`; the posture `service_tokens`,
  `install_pings` and `embedding_models` already take for install-wide
  rows that carry no tenant data), runs as its own scheduled task on the
  same scheduler at the same cadence, and borrows the ghost sweep's
  counted outcome record, not its trigger. Reads never return an expired
  row, so the sweep's latency is hygiene only. Nothing in the design needs the sweep sooner:
  a lapsed lease is claimable by the availability predicate, and the
  sweep's `expire` row and purge are bookkeeping with a worst-case
  latency of one interval.
- **Verified** (research 4, archive branch at 17428ba77) — The May API
  contracts the skills and the MVV audit relied on: `ack` and `nack`
  take the claimant and check ownership, with `ClaimNotFound` and
  `ClaimOwnership` as distinct errors; a same-claimant retake returns the
  existing claim id without a new log row; the availability predicate
  makes a lapsed lease claimable before any sweep runs; `take` returns
  `None` rather than an error on no candidate. The May registry format
  transfers almost verbatim; its pinned key set was `take.match_keys`.
  May's `SubagentStop` bridge docstring (2026-05-14) recorded `agent_id`
  in the stop payload, a prior for CA 2 that research 5 then verified.
- **Verified** (research 5, Claude Code 2.1.266) — One dispatch with both
  hooks logging raw stdin: `agent_id` is the same value on
  `SubagentStart` and `SubagentStop`, the Stop payload's
  `agent_transcript_path` is named by it, and an injected
  `additionalContext` line carrying the id came back verbatim in the
  agent's reply. Across 44 session ledgers every one of 2,083 stop-side
  rows keyed on the Stop payload's id matched a start row.
- **Verified** (research 5) — Claude Code waits for a blocking hook's
  stdout and stderr to close: a detached child inheriting them held the
  dispatch for 20.03 s against a 3 ms hook exit; the same child with all
  three fds on `/dev/null` cost 18 ms; a separate `async: true` hook
  entry cost 5 ms and was killed without trace when the session ended
  during its 15 s sleep. Today's ledger hooks run in 30 to 40 ms. A
  projection that shells `uv run nx` pays 0.8 to 0.9 s of CPU importing
  the CLI; a `curl` POST pays about 10 ms. The data-token lease file is
  readable from a shell, and only the guarded Python path may mint
  (burst five per credential and tenant per minute).
- **Verified** (research 2) — Six MCP tools cost two hand-maintained
  exact-set literals in `test_mcp_package.py`; a `tuple_cmd.py` module
  needs a `_MODULE_TO_CLI` entry because `tuple` is a builtin name; the
  raw-handle-guard roster is a hand-maintained list of nine store classes.
  No client enforces a pre-send request-body cap today; `edge_refusal.py`
  is a post-rejection renderer. No hook backgrounds a subprocess today.
- **Documented** (research 6) — Sending headers early and delaying the
  body would need a new streaming path beside `HttpUtil.send` and depends
  on the nginx sidecar forwarding headers before a delayed chunked body,
  which nginx's own issue tracker records as not guaranteed under
  `proxy_buffering`. Raising the control-plane knob is a change to a
  system this RDR does not own and the ALB's 60 s becomes the next
  ceiling. Capping at 25 s and looping needs neither. No prior RDR
  decided for long-polling or streaming through the public edge; the one
  precedent is nexus-bwulw, where the edge silently broke three features
  that passed every engine-direct gate.
- **Verified** (conexus session, 2026-09-09, each measured on the live
  estate) — Tenant: one laptop, one client config, every real agent
  instance mints against tenant `nexus`; the other tenants on the store
  (`gate-xr789`, `conexus-edge`, `default`, `smoke-2nx`) are gate and
  edge scopes with no agent traffic. Pooler: direct, no PgBouncer, so
  `LISTEN` is available on the engine's own connections. Replicas: one
  JVM, stop-then-start deploys, about a 25 s gap. Edge: the binding
  limit on a parked call is the control plane's 30 s response-start
  timeout; a 30 s park sits exactly on it and fails on jitter. Not yet
  measured: Crunchy's server-side idle and statement timeouts on a
  held-open connection, which matters only when `LISTEN` is adopted.

### Critical Assumptions

- [x] **CA 1: A single-statement claim on the new table is atomic across
      concurrent claimants.** — **Status**: Verified (research 3): the
      RDR's own table shape, ten claimants, a 50 ms injected race window,
      200 of 200 rounds with exactly one winner, zero deadlocks, under
      both lock modes. The pooler case is moot for production (CA 7) and
      already proven for the aspect queue's statement. Phase 1 Step 6
      re-runs the same harness against the engine's repository, which is
      a regression pin, not the verification. — **Method**: Spike, then
      Source Search.
- [x] **CA 2: The `SubagentStart` hook can inject the harness's
      per-instance `agent_id` into the agent's context, and the same id
      arrives on the `SubagentStop` payload.** — **Status**: Verified
      (research 5, Claude Code 2.1.266): the same opaque id on both
      payloads, the transcript path named by it, the injected line
      returned verbatim, and 2,083 of 2,083 stop-side ledger rows matched
      to a start row. The shipped stop hook has relied on the equality
      since it landed; the prose now says so. Design consequence: the
      report tuple is written by the stop hook without the agent's
      cooperation, and the agent's own writes use the injected id as
      claimant. — **Method**: Spike plus Source Search.
- [x] **CA 3: A parked long-poll survives the public edge in front of
      the managed engine.** — **Status**: Verified as a constraint,
      not a capability: the control plane times out a response that has
      not started within 30 s. Design consequence: `timeout_s` is capped
      at 25 s by default (an engine setting), the call returns the probe
      result at the cap, and the client loops. Raising the control-plane
      knob or sending early headers are recorded options, not taken.
      In-repo corroboration: `src/nexus/retry.py:373` records the edge's
      own 30 s timeout surfacing as a 502 or 504 in the 2026-08-15
      incident. — **Method**: a named authority's live measurement
      (the conexus session reading the deployed control plane, edge and
      sidecar on 2026-09-09), not documentation; accepted on that basis,
      with the 25 s cap re-verified through
      `tests/e2e/cloud-client-path-gate.sh` in Phase 1 Step 6.
- [x] **CA 4: The hook-path write can be projected to the engine without
      ever blocking a dispatch.** — **Status**: Verified as a mechanism
      with one shape excluded (research 5). Claude Code waits for the
      blocking hook's fds to close, so a child that inherits them holds
      the dispatch; a separate `async: true` hook entry, or a child with
      all three fds on `/dev/null`, does not (5 ms and 18 ms measured
      against 20.03 s for the inheriting shape). The projection is a
      separate async hook that reads the data-token lease file, POSTs
      with `curl`, never mints, and on a missing or near-expiry lease
      skips, appending the reason to a log file beside the ledger (an
      async hook's stdout and stderr are never read). Residual: an async
      hook is killed
      without trace at session end; the census's newest-row-age
      comparison is the detector. What research 5 did not run, and Phase
      3 must: the real projection script's timing with the engine up,
      down, and rate limiting. — **Method**: Spike plus Docs plus Source
      Search.
- [x] **CA 5: One engine JVM at any time.** — **Status**: Verified
      (one container, stop-then-start deploys, measured gap about 25 s).
      The in-process wake is therefore the mechanism, not an
      optimisation. The one-second re-run timer stays as defence against
      a missed signal, and a parked client must treat a 502/504 during a
      deploy as a retry, not an error. — **Method**: a named authority's
      live measurement (the conexus session's reading of the engine
      host on 2026-09-09, recorded verbatim in T2 `nexus_rdr/205` under
      "cloud facts"), not documentation; re-checked at the Phase 1 close.
- [x] **CA 6: Both instances mint against one tenant.** — **Status**:
      Verified (one box, one config, `mint_tenant: nexus`). Gap 5 is a
      same-tenant mailbox. — **Method**: a named authority's live
      measurement (this box's config and the cloud store's tenant list,
      2026-09-09), not documentation.
- [x] **CA 7: Production connects to Postgres directly.** — **Status**:
      Verified on the engine host. `LISTEN/NOTIFY` is available and
      still deferred, because one JVM needs no cross-process wake; the
      two engine tests that disagreed are reconciled in Phase 1 Step 1.
      — **Method**: a named authority's live measurement (the engine
      host's JDBC URL and boot log, 2026-09-09), not documentation.

**Method definitions**: Source Search (API verified against dependency
source), Spike (behaviour verified by running code against a live
service), Docs Only (documentation alone; insufficient for a
load-bearing assumption). CA 3 and CA 5 to CA 7 rest on a fourth basis
this RDR names explicitly: a named authority's measurement of the live
estate, recorded with its date and what was read, which is a spike run
by someone else and not documentation.

## Proposed Solution

### Approach

Ship one engine-owned table, `nexus.tuples`, with its append-only claim
log, a registry of subspace schemas checked at engine boot, ten HTTP
operations (`out`, `rd`, `rdp`, `in`, `inp`, `ack`, `nack`, `registry`,
`subspace_list`, `subspace_stats`), a Python HTTP store shaped like the aspect-queue client, a
small MCP tool set and an `nx tuple` verb over it. The destructive read
matches on equality over a pinned key set per subspace; the
non-destructive read may additionally rank by pgvector similarity in a
later phase, gated on a calibration result this RDR does not claim.
Blocking reads park on an in-engine waiter woken at commit, with a
one-second re-run timer as defence against a missed signal. Two v1
callers park: an orchestrator waiting for one agent's report (`rd` on
the ledger with a timeout) and the instance that sent a request waiting
for its ack (`in` on its own mailbox with a timeout). `out` is
idempotent everywhere: a tuple's id is derived from caller-supplied
fields only, so a retried `out` is the same tuple.

Two consumers land with it and no more: the RDR-184 dispatch ledger, as
start and report tuples keyed on the hook-injected `agent_id` with the
TSV kept as the write-ahead on the hook path, and a mailbox read on
demand, addressed to an agent id or to an instance name. The
cross-instance request and ack of Gap 5 is the same mailbox with an
instance name as the address, not a third consumer, because both
instances mint against one tenant. Nothing
else: not the build lease, not the T1 identity files, not the push
vouching, not any wrapping of scratch, memory or plans, not surfaces.
This is RDR-120's rule restated for this RDR: a consumer not named here
needs its own RDR.

### Technical Design

**Terms.** A *subspace* is a named partition of the space with a
registered schema (`mailbox/<agent_id>`, `ledger/<session_id>`). A
*template* is the schema's name with its parameter (`mailbox/<agent_id>`);
a *subspace* is a concrete instance. The *keys* of a tuple are the
registered dimensions the destructive read matches on; *dims* are the
rest. A *claim* is the state between `in` and `ack`/`nack`, held under a
*lease* (a deadline after which the claim is released by a sweep). A
*claimant* is the id doing the taking. A *nonce* is a value the caller
supplies with `out` (a message id it minted) that is mixed into the
tuple's id, so two otherwise identical messages stay distinct while a
retry of the same message is the same tuple.

**Table (illustrative; the changeset is the authority):**

```text
nexus.tuples
  id             bytea PK      sha256(canonical(tenant, subspace, keys [, nonce | body]))
                               per the template's id_from; every input is caller-supplied
  tenant_id      text          forced RLS, same discipline as every tenant table
  subspace       text
  template       text          registered template name
  keys           jsonb         pinned per template; equality match for in/inp
  dims           jsonb         validated against the template's schema
  body           text
  claim_state    text          NULL | 'claimed'
  claimant       text
  claim_id       text
  lease_until    timestamptz
  attempts       int           nacks so far; at the template's max_attempts the row is dead-lettered
  consumed_at    timestamptz   NULL = available; set by ack and by dead-lettering
  consumed_by    text          the acking claimant, or 'dead-letter'
  expires_at     timestamptz   TTL, never NULL; defaults to the template's retention_seconds
  created_at     timestamptz

nexus.tuple_tenants               tenant_id PK, first_seen, last_seen
                                  no RLS (it names tenants and holds no tenant data, the
                                  service_tokens / embedding_models posture); maintained by out
                                  in the same transaction as the tuple insert, so a live tuple
                                  can never lack its tenant row: out reads the tenant's row first
                                  (a plain select, no lock) and issues the upsert only when the
                                  row is missing or last_seen is older than a minute, so an
                                  ordinary out takes no row lock here; the sweep's enumeration
                                  source; never purged by the sweep; exposed by no client operation

nexus.tuple_claim_log             append-only: claim | ack | nack | expire | dead
  log_id, tenant_id (own RLS policy, like every audit table here),
  subspace, template          denormalised so a row reads standalone (May's nexus-pce1.4 fix)
  tuple_id REFERENCES nexus.tuples(id) ON DELETE SET NULL, indexed (the purge deletes through it)
  claim_id, claimant, transition, at
  expires_at                  the log's own TTL; the registry loader refuses to boot unless it
                              exceeds every template's retention_seconds by the purge interval
```

No v1 template embeds. A later semantic column lands as an additive
changeset following the per-dimension nullable pattern `nexus.chunks`
already uses, keyed on the tenant's embedding profile (RDR-204); the
row above carries nothing for it.

The id is derived from caller-supplied fields only, so `out` is
idempotent by construction and a retry across the deploy gap is the same
tuple. Which fields is the template's `id_from`: `keys` (the ledger:
`agent_id` and `kind` identify a dispatch, so a second start for the
same agent is the same tuple, which is what Gap 2 wants); `keys+nonce`
(the mailbox: the sender mints a message id and passes it as the nonce,
so two messages to one address are two tuples and a resent message is
one); or `keys+body` (content-addressed, the May formula, no v1
template). A refire with the same id refreshes `expires_at` only, never
the body, the claim state or the consumed state. The insert time is
never part of an id. RDR-110's gate finding C3 (a hash that omitted a
distinguishing field collapsed distinct tuples) is why each template
names every field its id covers.

**Claim (illustrative jOOQ shape; the repository method is the
authority):**

```text
// in one tenant-scoped transaction, nothing else in it
row = select(...).from(TUPLES)
        .where(TENANT.eq(t), SUBSPACE.eq(s), KEYS.eq(pattern),
               CONSUMED_AT.isNull(), EXPIRES_AT.gt(now()),
               CLAIM_STATE.isNull().or(LEASE_UNTIL.lt(now())))
        .orderBy(CREATED_AT).limit(1)
        .forNoKeyUpdate().skipLocked().fetchOne();
update(TUPLES).set(CLAIM_STATE, "claimed").set(CLAIMANT, c)
        .set(CLAIM_ID, id).set(LEASE_UNTIL, now + lease)
        .where(ID.eq(row.id)).execute();
insert claim_log(tuple_id, claim_id, c, 'claim', now)
```

`FOR NO KEY UPDATE` because the update touches no key column and the
weaker lock lets the log's foreign key coexist; it has no call site in
the engine today and `forUpdate()` is the fallback. `SKIP LOCKED`
because a contended row is skipped, not waited on. The transaction holds
the claim and nothing else, so the taxonomy-015 class (a lock upgrade
inside one transaction) has nothing to upgrade.

Two rules from the May implementation travel with the statement. The
availability predicate is `consumed_at IS NULL AND expires_at > now()
AND (claim_state IS NULL OR lease_until < now())`, so a lapsed lease is
claimable the moment it lapses and an expired row is invisible the
moment it expires. When a claim takes a row whose previous lease has
lapsed, the same transaction writes the `expire` row for the previous
claim, so every claim reaches a terminal transition (ack, nack, expire
or dead) whether or not the sweep saw it first; the sweep writes
`expire` only for lapsed claims nobody re-took. And a
same-claimant retake is idempotent: before the claim statement, `in`
reads for a live claim held by this claimant on a matching tuple and, if
one exists, returns its claim id with no new update and no log row.
Without that read a retry after a lost response could never recover its
claim.

**Operations (HTTP under `/v1/tuples`; signatures are the contract, the
handler is the implementation):**

```text
out(subspace, keys, dims, body, *, nonce=None, ttl_seconds=None) -> tuple_id
                                                            # nonce required where id_from is keys+nonce;
                                                            # ttl_seconds defaults to the template's retention
rd (subspace, keys_pattern, *, n=1, since=None, timeout_s=0) -> [Tuple]   # non-destructive; blocks up to timeout_s
rdp(subspace, keys_pattern, *, n=1, since=None) -> [Tuple]                # probe
in (subspace, keys_pattern, *, claimant, lease_s, timeout_s=0) -> (Tuple, claim_id) | None
inp(subspace, keys_pattern, *, claimant, lease_s) -> (Tuple, claim_id) | None
ack(claim_id, claimant) ; nack(claim_id, claimant)         # ownership checked; nack counts an attempt
subspace_list(prefix) -> [{subspace, total, available, claimed, consumed, expired_unpurged,
                           oldest_created_at, newest_created_at}]    # concrete subspaces that exist;
                                                                     # the two timestamps span all rows,
                                                                     # expired included (the census needs
                                                                     # the newest write, not the newest live row);
                                                                     # total counts live rows only
registry() -> {digest, templates: [TemplateSchema]}
subspace_stats(subspace) -> {total, available, claimed, consumed, expired_unpurged}
                                                            # total counts live rows only
```

`rd` and `rdp` return up to `n` live tuples (capped at the paging limit
in `limits.py`) ordered by `(created_at, id)`, resuming strictly after
`since`, a `(created_at, id)` cursor the caller keeps, so rows sharing a
timestamp are neither skipped nor repeated. Consumed rows are never
returned; no v1 consumer reads them (the ledger is never consumed and
nothing takes a census of a mailbox). `nack` releases the claim and
increments `attempts`; at the template's `max_attempts` the row is
dead-lettered instead: `consumed_at` set, `consumed_by = 'dead-letter'`,
a `dead` log row, and the row leaves every claimant's view until its
TTL. `subspace_list` carries each subspace's newest and oldest
`created_at`, so "the last five sessions" is a sort over the list, not
a scan of every ledger. `timeout_s` is capped at 25 seconds by default (CA 3: the
edge times out a response that has not started within 30 s), settable
on the engine; a call at the cap returns the probe result and the
caller loops. The client's own HTTP timeout is set above `timeout_s` so the
server's cap fires first. A request over the edge is bounded by the
WAF's 8 KB body limit; the client measures the serialised request it is
about to send, not the tuple body alone, and refuses before sending;
that guard is new code (nothing pre-checks a request size today). Errors are
typed: `UnknownSubspace`, `SchemaViolation` (field and reason, before any
write), `TakeDisabled` (the template's `take.enabled` is false),
`TimeoutTooLong`, `ClaimNotFound` (no live claim with that id),
`ClaimOwnership` (a live claim held by someone else), `ParkCapExceeded`
(the per-claimant or global park cap is reached; the caller gets the
probe result and backs off); a `ttl_seconds` or `lease_s` at or below
zero and a negative `timeout_s` are refused. Retry across the deploy gap: `rd` and `rdp` are freely
retryable on a 502 or 504; a retried `out` is the same tuple, by the id
formula; a retried `in` shares the ambiguity of a crash after `in`,
which the lease and sweep already cover. The client reuses the retry
classification in `nexus.retry` rather than a new one.

**Wake.** There is one engine JVM (CA 5) and every `out` passes through
it. The waiter is a per-subspace `Condition` (one lock and condition per
subspace key in a concurrent map). The `out` handler signals all waiters
on that subspace after the tenant-scoped transaction lambda has
returned, which is the commit, never from a transaction listener. Waking
every waiter on a subspace is the intended behaviour: each re-runs its
own equality query, `SKIP LOCKED` makes the fan-out cheap, and the pool
bounds it. A parked call is a loop: open a short transaction, run the
query, return the connection, then park outside any transaction until
the signal or a one-second timer, then repeat. A parked call never holds
one of the ten pooled connections while parked. The waiter is registered
before the query, not after, so a commit between query and park is not
lost. Per-claimant and global park caps are engine settings (defaults
four and sixteen, the May daemon's values) refused with
`ParkCapExceeded`. On shutdown every waiter is signalled so parked calls return
the probe result instead of riding out their budget, which is what makes
the 25-second deploy gap a retry rather than a stall on top of it.
Parked callers on subspace B do not wake on commits in subspace A.
`LISTEN/NOTIFY` is available on the direct connection (CA 7) and
deferred until a second JVM exists; when it is adopted, the listening
connection lives outside the pool, `getNotifications` is polled with a
non-zero timeout (the zero form hangs on a partition), and the first
thing to measure is TCP keepalive on a held-open connection to Crunchy,
whose public docs override none of Postgres's disabled-by-default idle
and statement timeouts.

**Registry.** Templates ship as YAML in engine resources, loaded and
validated at boot; a breach fails boot with the file and field named.
The engine is the only holder of the registry: no client carries a copy,
every write is validated by the engine against its own templates, and a
client learns what exists by asking. Cooperating installations cannot
drift on the registry because in cloud mode they share one engine; the
only skew possible is between a client and its engine, which is the
version skew the pinned engine version per client release already
governs. `registry()` returns a digest of the loaded templates beside
them, so a client or a hook script that expects a shape can detect skew
and report it rather than guess. (RDR-110 held the registry client-side
in the plugin, and its daemon persisted registered schemas with a digest
and gated third-party additions by reserved prefix, research 4; one copy
in the engine removes the class.)
The document shape is the May format with the substrate keys dropped:
`name`, `dimensions` (name to type, values, required), `keys` (the pinned
key set, May's `take.match_keys`), `id_from` (`keys`, `keys+nonce` or
`keys+body`), `take` (`enabled`, `default_lease_seconds`,
`max_attempts`; a template with `take.enabled` declares at least one
key), `retention_seconds` (required; `out`'s default TTL). Dropped: `tier`,
`tiers`, `content_type`, `embed_from`, `floor`, `margin`, the read
defaults, `match_text`, and retention zero meaning never; `embed_from`
returns with the semantic column, not before. Load rules verbatim from May: a literal
name is looked up before templates; a `<param>` matches one path
segment; a duplicate name or an empty parameter fails the load. A refire
on an idempotent template refreshes `expires_at` and leaves `created_at`
alone, so it does not move the tuple to the back of the claim order.
Templates are resource files: they change with an engine release, not
with a data changeset. The loader also refuses to boot unless the claim
log's TTL exceeds every template's `retention_seconds` by at least one
sweep interval, so the purge order in §Indexes and hygiene is checked,
not assumed. Schema evolution is additive in v1. Two v1 templates, and
only two: `ledger/<session_id>` (keys: `agent_id`, `kind` in {start,
report}; dims: `agent_type`; `id_from: keys`; take disabled, since
ledger rows are read and never claimed; retention 90 days, the window
the census compares within), and `mailbox/<address>` (keys: `to`; dims:
`from`, `kind`, `correlation_id`, `address_kind` in {agent, instance};
`id_from: keys+nonce`, the nonce a sender-minted message id; take
enabled, `max_attempts` 3; retention 7 days). The ten-worker harness
registers its own test-only template in the test, not in the shipped
resources. An instance address is the session name the harness's
`ListAgents` tool shows (for example `nexus-23`), so the Gap 5 request
is `out` to `mailbox/conexus-ed` and its ack is `out` back to the
requester's own mailbox, which the requester waits on with `in`.

**Indexes and hygiene.** Partial index on `(tenant_id, subspace,
created_at) WHERE consumed_at IS NULL AND claim_state IS NULL` for the
claim scan; an index on `tuple_claim_log(tuple_id)`, since the purge
deletes through that reference and an unindexed child would turn every
batch into a scan of the log; a GIN index on `keys` only if a consumer's
pattern needs it.
Claims and acks write predicate columns and are therefore not HOT
updates (measured 0 % HOT with the index, research 3); at the measured
load the index churn is affordable and the sweep budget assumes it.
`autovacuum_vacuum_scale_factor = 0.01` on `nexus.tuples` and
`nexus.tuple_claim_log`, the first
per-table storage parameter in the changelog; its measured benefit is
reclaim during sustained churn, since the default already self-heals
once churn stops. TTL on every row; the `sweepScheduler` in
`NexusService`, every `SWEEP_INTERVAL_HOURS` (six hours today,
`NexusService.java:78`), gains a second scheduled task that enumerates
its tenants from `nexus.tuple_tenants` and, per tenant, releases
lapsed claims nobody re-took with an `expire` log row, purges expired
and consumed-past-retention tuple rows (which sets the log's `tuple_id`
to null), then purges log rows past the log's own longer TTL, in batches
of a few hundred, committing per batch (one long transaction would
defeat autovacuum). Two bounds, because the task shares one
single-thread scheduler with the T1 crash-safety sweep and would
otherwise starve it: the token loop's statement bound
(`NexusService.java:539-542`: passed to every arm, because bounding one
of three leaves the cycle unbounded) on every statement, and a budget on
the task itself, a cap on batches per tenant per run and a wall-clock
budget per run, both engine settings, with the remainder carried to the
next interval (the sweep is idempotent and cumulative, so carrying is
safe, as `NexusService.java:544-547` says of the token loop). Every run
logs a counted outcome record in the RDR-204 ghost sweep's convention:
tenants visited, scanned, released, purged, log rows purged, and whether
the budget was exhausted. A
run that finds nothing expired is the normal state of a healthy table
and is logged as such; the failure the counts detect is a run that
scanned nothing at all, or a run that did not happen, which the doctor
row on last-sweep age reports. If a consumer ever pushes the
table into millions of rows, the Solid Queue shape (a separate claimable
table) is the fix, not a fillfactor.

**Client.** `nexus.db.t2.http_tuple_store.HttpTupleStore`, constructor-
injected like the other stores; MCP tools `tuple_out`, `tuple_rd`,
`tuple_in`, `tuple_ack`, `tuple_nack`, `tuple_registry` (templates and
digest), `tuple_list` (concrete subspaces), `tuple_stats` (probes are
`rd` and `in` with `timeout_s` zero); `nx tuple
{out,rd,in,ack,nack,templates,list,stats}`; three `nx doctor` rows:
oldest unclaimed age per subspace over live rows only; dead-tuple ratio
and last autovacuum on the table; age of the last tuple sweep and
whether its budget was exhausted.

**Identity and scope.** No hook mints anything. `subagent-start.sh`,
the one script on `SubagentStart` that may write to stdout, parses
`agent_id` from its own payload (the stamp script parses the same
payload and stays silent) and adds one line to the `additionalContext`
envelope it already emits, giving the agent its claimant id and mailbox
address. It does no network I/O. The start and report tuples are keyed
on the same harness id and written by the async entries described under
Hook path, so the report needs no cooperation from the agent. The
`PreToolUse` expectation row keeps covering a dispatch that never
starts. Every address a tuple carries, the agent id in the ledger's keys
and the mailbox's `to`, comes from that injected id or from the existing
session lease, never from resolving the session at write time.

**Hook path.** The three blocking hooks that write the TSV today
(`agent-dispatch-expect.sh` on `PreToolUse`, `subagent-start-stamp.sh`
on `SubagentStart`, `subagent-stop.sh` on `SubagentStop`) stay exactly
as they are, and the TSV stays the write-ahead. Two new `async: true`
entries, each beside its blocking hook in `hooks.json`, project the same
payload to the space: the `SubagentStart` entry writes the start tuple
and the `SubagentStop` entry writes the report tuple. `PreToolUse` gets
no entry: an expectation has no agent id yet, and its TSV row is its
only record. Neither entry is a child of a blocking hook (a child that inherits the hook's fds holds the
dispatch, CA 4). Each reads the data-token lease file (the cached
credential the client library keeps under `~/.config/nexus/`), POSTs
with `curl`, never mints, and on a missing or near-expiry lease skips
and appends the reason to a log file beside the ledger, since an async
hook's stdout, stderr and exit code are never read; a failure is never
propagated. The census
(`expectations_census`, the scripted count the orchestration skill
requires) gains a space-backed path that reads `ledger/<session_id>`
with `rd` (ledger rows are never consumed), enumerating sessions with
`subspace_list("ledger/")`, and falls back to the TSV with a named reason
when the engine is unreachable. It compares only within the ledger
template's retention window, since the TSV is a durable file and the
space is not; it reports the space's newest-row age against the TSV's so
a stalled projection is a finding, and reports a session present in the
TSV directory and absent from `subspace_list` as a projection that never
ran.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Atomic claim | `AspectRepository.claimNext` / `reclaimStale` | Reuse the statement shape and the tenant-scoped transaction; new repository, since the queue's columns are aspect-specific. |
| Batch claim | `AspectRepository.claimBatch` (a loop) | Do not reuse; a real `LIMIT n` claim is new work and lands only when a consumer asks. |
| Sweep | `NexusService.sweepScheduler` (every `SWEEP_INTERVAL_HOURS`; its tenant set comes from the non-RLS `service_tokens`); RDR-204 ghost sweep (`AuthFilter`, once per tenant per JVM) | Add a second scheduled task on the same scheduler that enumerates from the non-RLS `nexus.tuple_tenants`; borrow the ghost sweep's counted outcome record, not its trigger. |
| Pre-send request cap | none (`edge_refusal.py` is post-rejection; `limits.py` holds store quotas) | New: an 8 KB guard on the serialised request in `HttpTupleStore.out`. |
| Retry across the deploy gap | `nexus.retry` (502, 503, 504, 429 retryable) | Reuse unchanged. |
| Tenant scoping | `TenantScope`, forced RLS changesets | Reuse unchanged. |
| HTTP store client | `http_aspect_queue.py` | Reuse the shape (constructor injection, typed errors, data-token handling). |
| Dispatch ledger | `expectations.sh`, `agent-dispatch-expect.sh`, `subagent-start.sh`, `subagent-start-stamp.sh`, `subagent-stop.sh` | Extend: `subagent-start.sh` injects the id; two async entries beside the start and stop hooks write the tuples; the census gains a space-backed path. The blocking hooks, the TSV and its readers stay. |
| Session identity | `t1.py` lease and handoff, JDR-001 | Untouched, by decision. |
| Registry loader | RDR-110's YAML registry (archive branch, `registry.py`) | Reuse the document shape and load rules named in §Technical Design; drop the tier, similarity and Chroma keys; the loader moves to the engine. |

### Decision Rationale

Three things decide the shape. The claim statement's shape is already
proven in this engine and the exact statement passed its own spike, so
the risk in the primitive is near zero and the cost is a table and a
handler. RDR-116's ceiling, every operation serialised behind one
connection, cannot recur: each call is its own transaction and a
contended row is skipped. The two consumers are the two most-measured failures
in the orchestration record and each is a small change to a hook that
already exists. And RDR-120's rule about co-shipped consumers, adapted here to two
named consumers, is written into §Approach rather than left to
discipline, because RDR-120's own text says of its moratorium that it
"is the only structural difference between this attempt and the
scrapped one".

Equality on pinned keys for the destructive read, rather than Linda's
subset match or RDR-110's semantic take, is the C2 lesson: a lock or a
mailbox must never be taken by a less specific pattern. Similarity stays
on the non-destructive read, later, because a false positive there costs
a read and not a claim.

## Alternatives Considered

### Alternative 1: Three endpoints, no tuple space (queue, mailbox, lock)

**Description**: Add a mailbox table and a lock table beside the aspect
queue, each with its own handler.

**Pros**: Cheapest first commit; the queue exists.

**Cons**: Three schemas and three vocabularies, which is the drift
RDR-110 was written against; no cross-instance request without a fourth;
every future coordination need is another table.

**Reason for rejection**: The record of the last four months is that the
needs recur in the same three shapes; one primitive with a registry is
less code than three tables by the second consumer.

### Alternative 2: RDR-110 ported statement for statement (semantic take)

**Description**: Bring back semantic destructive read with per-subspace
floor and margin, over pgvector.

**Pros**: Work-stealing on natural-language task text from day one.

**Cons**: The May calibration was earned on MiniLM over Chroma and does
not transfer; no consumer in this RDR needs it; a false-positive take
leaks a tuple meant for someone else.

**Reason for rejection**: Deferred, not rejected. A later additive
changeset adds per-dimension nullable vector columns in the
`nexus.chunks` pattern, keyed on the tenant's embedding profile
(RDR-204), when a consumer asks and the fuzz gate passes on the engine's
embedding.

### Alternative 3: Use T2 memory or T1 scratch with polling

**Description**: Agents write findings to scratch or memory and poll.

**Pros**: No new table.

**Cons**: No atomic take, no lease, no claim log; two readers both see
the record; the RDR-184 failures are exactly what this produced.

**Reason for rejection**: It is the status quo.

### Briefly Rejected

- **LISTEN/NOTIFY in v1**: unavailable through a transaction-mode pooler,
  and unnecessary while one JVM commits every `out`.
- **Session-level advisory locks for claims** (Que's shape): leak on
  worker death and need a reaper; a lease column does not.
- **Moving the build lease or the T1 identity files into the space**:
  withdrawn on review; see §Relationship to Prior RDRs.
- **Partitioning by day from the start**: the measured load is thousands
  of rows per month; TTL and a batched sweep suffice.

## Trade-offs

### Consequences

- Three new tables, one handler, one client store, eight MCP tools,
  one CLI verb. The engine's public surface grows by one route family.
- The ledger and the mailbox get a real per-instance key and a store that
  can be queried across sessions and processes.
- A new operational surface: oldest-unclaimed age, dead-tuple ratio
  with last autovacuum, and last-sweep age become doctor rows; lease
  expiries per hour is an MVV metric.
- Agents learn one more tool family. The skills that teach it are two
  files.
- A hook now does a network write after its append, in the background.
  The hook's exit and latency must not change; CA 4 measures that.

### Risks and Mitigations

- **Risk**: Scope grows, as it did in May.
  **Mitigation**: §Approach names the consumers; any other is a new RDR.
  The phase-review gate cross-walks §Approach at each phase close.
- **Risk**: A mailbox tuple is readable across tenants.
  **Mitigation**: Forced RLS from the first changeset; both instances
  share one tenant, so no cross-tenant path is built at all.
- **Risk**: A parked long-poll outlives the edge and looks like a hang.
  **Mitigation**: The default cap of 25 s sits under the edge's 30 s
  response-start timeout; the call returns the probe result at the cap;
  the gate runs through the public edge.
- **Risk**: Table bloat under sustained claim/ack churn that outlasts an
  autovacuum naptime (the default self-heals once churn stops, research
  3).
  **Mitigation**: Per-table autovacuum factor for reclaim during churn,
  TTL on every row, a sweep that commits per batch, doctor row; the
  Solid Queue shape as the named next step.
- **Risk**: Two same-type dispatches collapse into one ledger tuple.
  **Mitigation**: The harness's per-instance `agent_id` is in the
  ledger's keys (CA 2), pinned by a test that writes two starts with
  different ids and reads two rows, and one start twice and reads one.
- **Risk**: The tuple sweep starves the T1 crash-safety sweep on the
  shared single-thread scheduler.
  **Mitigation**: A budget on the task (batches per tenant, wall clock
  per run) and the statement bound on every statement; the doctor row
  reports budget exhaustion.
- **Risk**: A message nobody can process is redelivered until its TTL.
  **Mitigation**: `attempts` and the template's `max_attempts`; the row
  is dead-lettered with a `dead` log row.
- **Risk**: The hook projection blocks or fails a dispatch.
  **Mitigation**: The projection is a separate async hook entry, never a
  child of the blocking hook; Phase 2 Step 3 measures it with the engine
  down.

### Failure Modes

- Visible: `SchemaViolation` before any write, naming field and reason.
- Visible: `in`/`inp` return `None`; the caller loops or reports no
  work.
- Visible: the sweep logs tenants visited, scanned, released, purged
  and log-purged counts every run. Zero expired is the healthy steady state; a sweep
  that did not run shows as a stale last-sweep age in the doctor row.
- Silent, resolved: a claimant crashes after `in`; the lease lapses,
  the next claim takes the row and writes the `expire` row for the old
  claim, or the sweep does if nobody re-took it.
- Visible: a claim nacked `max_attempts` times is dead-lettered with a
  `dead` log row and leaves every claimant's view.
- Visible: the sweep reports budget exhaustion in its counted record and
  the doctor row; the remainder is swept next interval.
- Silent, resolved: a live tuple can never lack its tenant row, because
  `out` maintains `tuple_tenants` in the same transaction.
- Silent, resolved: two claimants race; `SKIP LOCKED` gives each a
  distinct row or `None`.
- Silent, recorded: an async projection hook is killed without trace at
  session end, so the space can be behind the TSV. The census reports
  the space's newest-row age against the TSV's, and a session in the TSV
  directory with no ledger subspace at all, and either is a finding.

## Implementation Plan

### Prerequisites

- [x] CA 1, CA 2 and CA 4 verified by the research 3 and research 5
      spikes; CA 5 to CA 7 recorded from conexus's measurements of
      2026-09-09.
- [ ] The RDR-120 lift statement in §Relationship to Prior RDRs stands
      unchallenged at the gate.

CA 3's confirmation through the cloud client-path gate and the
`PgBouncerTenantIsolationTest` comment are Phase 1 deliverables (Steps
6 and 1), not prerequisites; nexus-bwulw is why that gate, and not an
engine-direct check, is the proof for CA 3.

### Minimum Viable Validation

Two runs, both in scope (this is the minimum viable validation, the
one end-to-end proof). First, a real session dispatches ten sub-agents
of two types; the space holds ten start tuples and ten report tuples
with ten distinct ids, the orchestrator's parked `rd` for one named
agent's report returns when that report lands, and the space-backed
census, reading `ledger/<session_id>` with `rd`, agrees with the TSV
census row for row within the ledger's retention window. Second, the ten-worker work-stealing harness
from RDR-110 runs against the engine on a test-registered template with
six metrics recorded (wake latency through the public edge,
oldest-unclaimed age, empty claims after wake, dead-tuple ratio and last
autovacuum, lease expiries, claim transaction duration), plus a bloat leg
of one million claim-ack cycles with dead-tuple ratio sampled throughout.

### Phase 1: Engine

#### Step 1: Settle the pooler fact

`PgBouncerTenantIsolationTest` says PgBouncer fronts production; it does
not (CA 7). Its comment changes to say the test guards a topology the
engine supports but does not deploy, and `BackendReaperIntegrationTest`'s
statement stands.

#### Step 2: Changesets

`tuples-001-baseline.xml`: `nexus.tuples` and `nexus.tuple_claim_log`
each with `tenant_id`, created and given forced RLS in one changeset
with one rollback block, the catalog-036 shape; `nexus.tuple_tenants` with no RLS and a comment giving the reason
(it names tenants and holds no tenant data, like `service_tokens` and
`embedding_models`); the partial index, the
per-table autovacuum factor with a comment naming this RDR as the first
use, the claim log's indexed `ON DELETE SET NULL` reference and its own
TTL, the `attempts` column, and complete rollback blocks
(the rollback round-trip test replays the whole chain). No per-changeset
grants: the `runAlways` grant changesets cover every relation. One
include line in the master changelog before the grant includes.

#### Step 3: Registry

YAML templates in engine resources; loader and validator at boot,
including the log-TTL-exceeds-every-retention check; the two v1
templates named in §Technical Design and no third.

#### Step 4: Repository and handler

`TupleRepository` (claim, out, read, ack, nack, stats, sweep) in jOOQ;
`TupleHandler` under `/v1/tuples`; the waiter set and the one-second
re-run timer; typed errors.

#### Step 5: Sweep

A second scheduled task on `sweepScheduler` at the same cadence
(`SWEEP_INTERVAL_HOURS`), separate from `runScheduledSweep` because that
method builds its token-derived tenant set once before its arms. It
enumerates `nexus.tuple_tenants` and, inside `withTenant` for each:
releases lapsed claims nobody re-took with an `expire` log row, purges
expired and consumed-past-retention tuple rows, then log rows past the
log's own TTL, in batches with a commit per batch, under the token
loop's statement bound on every statement including the enumeration
(`NexusService.java:539-542`) and under the task's own budget (batches
per tenant per run, wall clock per run, remainder carried); log the
counted outcome record every run. The sweep test seeds expired rows and
asserts the counts; production runs that find nothing are normal.

#### Step 6: Spikes

The research 3 harness re-run against `TupleRepository` with a
test-only delay between select and update (a regression pin for CA 1);
CA 3 through the cloud client-path gate, including a 31 s park to pin
the 504 that justifies the cap.

### Phase 2: Client

#### Step 1: `HttpTupleStore`

The `HttpAspectQueue` shape (both mixins, route-prefix overrides,
constructor injection), a `db.tuples` attribute on the facade with
reverse-order close, typed errors, the 8 KB pre-send guard, the retry
classification from `nexus.retry` on parked calls, an HTTP timeout above
`timeout_s`. Tests against the engine substrate. The raw-handle-guard
roster gains the class by hand.

#### Step 2: MCP tools, `nx tuple`, doctor rows

Eight tools in `nexus.mcp` (`structured_output=False` only where the
return annotation is a union or a list); both exact-set literals in
`test_mcp_package.py` updated. `commands/tuple_cmd.py` as a click group
with a `_MODULE_TO_CLI` entry, documented in `docs/cli-reference.md`.
Three doctor rows in `health.py` on the RDR-204 pattern with every
report class covered.

#### Step 3: Hook wiring

`subagent-start.sh` parses `agent_id` and adds the injection line; the
two `async: true` projection entries are added to `hooks.json` beside
the start and stop hooks, the `SubagentStart` one writing the start
tuple and the `SubagentStop` one the report tuple. CA 2 and CA 4 are
discharged by research 5; what this step measures is the real projection
script with the engine up, down, and rate limiting, and that the
blocking hooks still run in their 30 to 40 ms.

### Phase 3: Consumer one, the ledger

`subagent-start.sh` injects the id; the async `SubagentStart` entry
writes the start tuple and the async `SubagentStop` entry writes the
report tuple, neither needing the agent; `expectations_census` gains the
space-backed read path (`rd` over `ledger/<session_id>`, sessions
enumerated with `subspace_list`) with the TSV fallback, the retention
window, the age comparison and the absent-projection report; the
orchestration skill gains "wait for this agent's report" as a parked
`rd` with a timeout. The MVV's first run closes this phase.

### Phase 4: Consumer two, the mailbox

A `mailbox` skill and one paragraph in the orchestration skill: send by
`tuple_out` to the agent's id with a sender-minted message id as the
nonce, drain by `tuple_in` before composing any hand-back. A scenario
test with a mid-turn directive, and one with a resent message that
lands once.

### Phase 5: Cross-instance request and ack

The mailbox with an instance name as the address: the relay convention
in the cross-instance memory becomes `tuple_out` to the peer's address
and a parked `in` on the requester's own mailbox for the ack, and the
nexus-w374z sweep reads unacked requests from the space. A scenario test
with two sessions on one box.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Templates (resource files) | `nx tuple templates` | `nx tuple stats <subspace>` | Removed in an engine release; a removal with live rows needs a data changeset | Boot validation | git |
| `nexus.tuples` | `nx tuple list` | `nx tuple stats <subspace>`, doctor rows | TTL sweep; purge by tenant via admin SQL | `nx doctor` | PG bundle / managed backups |
| `nexus.tuple_tenants` | admin SQL | `last_seen` per tenant | never by the sweep; a tenant row is removed only with the tenant | the sweep's counted record (tenants visited) | same |
| `nexus.tuple_claim_log` | via stats | per-claim history | retention sweep | `nx doctor` | same |

### New Dependencies

None. Postgres, pgvector, jOOQ and Liquibase are in place.

## Test Plan

- **Scenario**: `out` with a schema breach — **Verify**: `SchemaViolation`
  names field and reason; no row written.
- **Scenario**: two ledger starts with different `agent_id` — **Verify**:
  two rows; the same start twice — **Verify**: one row.
- **Scenario**: two mailbox messages to one address with different
  nonces — **Verify**: two rows; the same message resent with its nonce
  — **Verify**: one row, `expires_at` refreshed.
- **Scenario**: `nack` `max_attempts` times by different claimants —
  **Verify**: the row is dead-lettered, a `dead` log row, no further
  `in` returns it, `rd` still does not (consumed).
- **Scenario**: a claim takes a row whose lease lapsed — **Verify**: the
  previous claim's `expire` row is written in the same transaction.
- **Scenario**: the sweep budget is exhausted on a seeded million rows
  — **Verify**: the run stops at its budget, reports exhaustion, and the
  next run continues; the T1 sweep still runs in the same interval.
- **Scenario**: ten concurrent `inp` on one available row — **Verify**:
  exactly one `(Tuple, claim_id)`, nine `None`; one `claim` log entry.
- **Scenario**: `in` then crash before `ack` — **Verify**: after the
  lease, the sweep releases the row with an `expire` entry and a new
  claimant takes it.
- **Scenario**: `ack` — **Verify**: `consumed_at` set, `ack` logged, the
  row invisible to `rd` and `in`.
- **Scenario**: `ack` or `nack` by a claimant that does not hold the
  claim — **Verify**: `ClaimOwnership`; a second `ack` on the same claim
  — **Verify**: `ClaimNotFound`.
- **Scenario**: the same claimant calls `in` twice within its lease —
  **Verify**: the same claim id both times, one `claim` log row.
- **Scenario**: parked callers on subspace B while `out` commits on
  subspace A — **Verify**: B's callers do not re-run (RDR-116's
  scenario).
- **Scenario**: ten workers drain N tuples — **Verify**: N consumed, zero
  claimed, zero available, exactly N `claim` and N `ack` log rows, zero
  `expire` rows, no tuple with two claim ids (the May MVV audit).
- **Scenario**: a parked `rd` receives a 502 from the edge — **Verify**:
  the client retries within its bounded policy and returns the tuple.
- **Scenario**: a less specific key pattern against a more specific
  tuple — **Verify**: no match (equality, not containment).
- **Scenario**: tenant A's `rd` against tenant B's mailbox — **Verify**:
  empty, by RLS, with no error that names the other tenant.
- **Scenario**: `rd` with `timeout_s` while another client `out`s —
  **Verify**: returns within the wake budget; with the signal suppressed
  by a test hook, within the one-second poll interval.
- **Scenario**: the parked call through the public edge at the 25 s cap
  — **Verify**: returns the probe result without a 504; at 31 s the edge
  returns 504, which pins the reason for the cap.
- **Scenario**: hook append with the engine down — **Verify**: the hook's
  exit code and latency are unchanged; the projection failure is logged;
  the census falls back with a named reason.
- **Scenario**: bloat leg — **Verify**: dead-tuple ratio is recorded
  across a million cycles with the per-table autovacuum factor; the
  target is set from that record at the Phase 3 close.
- **Scenario**: a tenant whose data tokens have all expired still has
  tuples — **Verify**: the sweep visits it (its `tuple_tenants` row), and
  a read before the sweep never returns its expired rows.
- **Scenario**: the sweep runs against seeded expired tuples, lapsed
  claims and old log rows — **Verify**: counts match the seed, `expire`
  rows exist, purged tuples' log rows survive with `tuple_id` null, and
  an idle run logs zeros without failing.
- **Scenario**: a serialised request over 8 KB with a body under it —
  **Verify**: refused by the client before any request is sent.
- **Scenario**: an unknown subspace, a `timeout_s` above the cap, a
  `ttl_seconds` or `lease_s` at or below zero, a negative `timeout_s` —
  **Verify**: the named typed error, no row written.
- **Scenario**: a template file with a breach — **Verify**: the engine
  refuses to boot naming the file and field.
- **Scenario**: the engine stops with parked readers — **Verify**: every
  reader returns the probe result before shutdown completes.
- **Scenario**: the seventeenth parked reader, or a claimant's fifth —
  **Verify**: `ParkCapExceeded` with the probe result.
- **Scenario**: the census over `ledger/<session_id>` after the MVV's
  first run — **Verify**: equals the TSV census row for row within the
  retention window; a session directory with no ledger subspace is
  reported as a projection that never ran.
- **Scenario**: a template whose `retention_seconds` exceeds the log
  TTL — **Verify**: the engine refuses to boot naming the template.

## Validation

### Testing Strategy

1. Engine unit and integration tests against the bundled PG (claim
   atomicity, RLS, sweep, registry boot failure).
2. Client tests against the engine substrate (`ensure_engine`), one
   happy and one failure path per operation.
3. The two MVV runs, recorded with their six metrics in T2 under this
   RDR's research prefix.
4. The RDR-184 scenario journeys re-run with the ledger on the space.

### Performance Expectations

None claimed beyond the measured load in §Key Discoveries. The six MVV
metrics are recorded, not targeted, on the first run; targets are set
from that record at the Phase 3 close.

## Finalization Gate

### Contradiction Check

_To be completed during `/conexus:rdr-gate`._

### Assumption Verification

_To be completed during `/conexus:rdr-gate`. CA 3 and CA 5 to CA 7
rest on the conexus session's measurements of 2026-09-09, recorded in
§Key Discoveries._

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `forNoKeyUpdate().skipLocked()` | jOOQ 3.20.11 | Source Search |
| `SELECT … FOR NO KEY UPDATE SKIP LOCKED` | PostgreSQL 17 | Docs |
| `SubagentStart` payload `agent_id` | Claude Code 2.1.251, 2.1.266 | Source Search (`expectations.sh`), Spike (research 5) |
| `SubagentStop` payload `agent_id` | Claude Code 2.1.266 | Spike (research 5) |
| `async: true` command hook | Claude Code 2.1.266 | Docs plus Spike (research 5) |
| `SELECT … FOR NO KEY UPDATE SKIP LOCKED` under ten claimants | PostgreSQL 17.5 | Spike (research 3) |
| `HttpExchange.sendResponseHeaders(status, 0)` (streaming, not taken) | JDK 25 | Docs |

### Scope Verification

_To be completed during `/conexus:rdr-gate`. Both MVV runs are Phase 3
and Phase 1 deliverables, not deferred._

### Cross-Cutting Concerns

- **Versioning**: templates are resource files that evolve additively
  with engine releases; removing a template that has live rows needs a
  data changeset with a migration note.
- **Build tool compatibility**: N/A.
- **Licensing**: no new dependencies.
- **Deployment model**: engine-owned; a cloud deploy carries the
  changeset through the point-in-time-restore fork rehearsal that gates
  every changeset; local mode
  gets it at the pinned engine version. The space is per-install in local
  mode and shared per tenant in cloud mode; the RDR declares both. A
  stop-then-start deploy parks every reader for about 25 s; clients
  retry.
- **IDE compatibility**: N/A.
- **Incremental adoption**: additive; no existing caller changes.
- **Secret/credential lifecycle**: unchanged; the data token discipline
  applies; tenant binding decides visibility.
- **Memory management**: TTL on every row; batched sweep; parked calls
  on virtual threads holding no pooled connection.
- **Recovery**: after a point-in-time restore, content and dimensions
  come back from the snapshot, active claims are re-earned by lease
  lapse, and the claim log is the only claim history (RDR-117's
  statement, carried here).

### Proportionality

_To be completed during `/conexus:rdr-gate`._

## References

- `docs/rdr/rdr-110-semantic-tuple-space.md` (tombstone) and its close
  post-mortem at commit 9808ff85b on `archive/develop-2026-05-19`.
- `docs/rdr/rdr-120-storage-substrate-split.md` §Scope Boundaries,
  lines 195-199 and 250-251.
- `docs/rdr/rdr-184-orchestration-protocol-hardening.md`;
  `tests/e2e/lib/expectations.sh`;
  `conexus/hooks/scripts/agent-dispatch-expect.sh`.
- `docs/rdr/joint/JDR-001-t1-three-scopes.md`; T2
  `nexus/s1-t1-identity-inventory-2026-08-22`.
- `service/src/main/java/dev/nexus/service/db/AspectRepository.java`
  `:1056-1132`; `service/src/main/resources/db/changelog/aspects-001-baseline.xml`,
  `taxonomy-015-doc-count-lock-mode.xml`, `fk-003-*`.
- T2: `nexus/rdr-110-revival-brief-2026-09-09`,
  `nexus/critique-linda-over-postgres-brainstorm-2026-09-09`,
  `nexus/critique-linda-over-postgres-brainstorm-r2-2026-09-09`,
  `nexus/factcheck-linda-over-postgres-brainstorm-rev3-2026-09-09`,
  `nexus/factcheck-linda-over-postgres-external-claims-2026-09-09`.
- Gelernter, "Generative Communication in Linda", ACM TOPLAS 1985.
- PostgreSQL docs: explicit locking; heap-only tuples. PgBouncer docs:
  feature matrix for pooling modes.
- Brandur Leach, "Postgres Job Queues & Failure By MVCC" (2015). Que,
  pgmq, Oban, River, db-scheduler, Graphile Worker, Solid Queue, Quartz
  sources as checked in the external fact-check record.

## Revision History

### 2026-09-09 — Created

Drafted from the reviewed brainstorm. Four questions sent to the conexus
session (tenant binding, pooler topology, replica count, edge timeout)
and answered the same day from the live estate: one tenant for every
agent instance, direct Postgres with no pooler, one engine JVM with
stop-then-start deploys, and a 30 s response-start timeout at the edge.
Gap 5 went from conditional to a same-tenant mailbox; the wake became the
mechanism rather than an optimisation; the blocking cap became 25 s.

### 2026-09-09 — Research pass (six parallel records, T2 `nexus_rdr/205-research-1` to `-6`)

CA 1, CA 2 and CA 4 verified by spike; the spike numbers are in §Key
Discoveries. Changes to the design text: `ack` and `nack` take the
claimant and check ownership; the same-claimant retake and the
availability predicate are stated as rules; the claim log carries its
own `tenant_id` and policy; the waiter is a per-subspace `Condition`
signalled after commit, parked calls hold no pooled connection; the
sweep extends the scheduled sweep and commits per batch; the registry
document shape and load rules are named; the injection lands in
`subagent-start.sh` and the projection is a separate async hook that
never mints; the pre-send body cap is named as new code; RDR-116 and
RDR-117 join the prior-RDR table; the recovery statement joins
§Cross-Cutting Concerns. `forNoKeyUpdate()` is recorded as having no
call site in the engine today, with `forUpdate()` as the fallback.

### 2026-09-09 — Gate round 1, BLOCKED (6 critical, 11 significant), fixes applied

Critique: T2 `nexus_rdr/205-gate-critique-2026-09-09`. Fixes, each
applied at every site the critique listed: C1 the sweep cadence is
`SWEEP_INTERVAL_HOURS` (six hours), cited to the constant, with the
design's independence from it stated; C2 the nullable-vector "first" is
withdrawn (`nexus.chunks` has three) and the autovacuum "first" kept;
C3 the RDR-204 row now cites the per-tenant profile as the reason v1
ships no embedding column, and the column, the `embed` parameter and
`embed_from` are removed from v1; C4 the hook path is three async
entries beside the three blocking hooks, one per event, with the
`PreToolUse` one writing nothing; C5 the census read model is `rd` with
`include_consumed` over `ledger/<session_id>` plus `subspace_list`, both
added to the operations; C6 the claim log carries denormalised
`subspace` and `template`, an `ON DELETE SET NULL` reference, and its
own TTL, with the purge order stated. Significant: scope domain gains
`agent`; `take.enabled` is a registry field; `ParkCapExceeded` with
defaults four and sixteen; two v1 templates and no third; CA 3 and CA 5
to CA 7 name their basis as a named authority's live measurement, with
`retry.py:373` as in-repo corroboration of CA 3; the bloat scenario
records rather than gates; the id formula names its fields per template
kind; RDR-120's rule is called adapted and its sentence quoted
correctly; the Test Plan gains eight scenarios; the sweep's counted
record replaces the misapplied non-vacuity assertion. Minor: the scrap
count, Gap 2's wording, template lifecycle, frontmatter `related_rdrs`,
the load bullet's citation, the seventh MCP tool and third doctor row,
and first-use glosses for T1, T2, beads, TSV, HOT, WAF, ALB and PITR.

### 2026-09-09 — Fix check on the round-1 fix commit (FAIL), second fix

T2 `nexus_rdr/205-fix-check-8ec08d5af`. C4 had survived in the Existing
Infrastructure Audit's dispatch-ledger row; closed. C5's Gap 4 query
needed a time field on `subspace_list`; added (oldest and newest
`created_at`). The sweep's tenant set is the default tenant plus every
tenant in `service_tokens`, not "all tenants", at four sites. The
`PreToolUse` async entry that wrote nothing is dropped: two async
entries. An async hook's skip reason goes to a log file beside the
ledger, since its stderr is never read. `subspace_list` and the registry
digest gain their MCP tools (`tuple_list`, `tuple_registry`) and CLI
verbs (`list`, `templates`); eight tools. CA 5's method names the T2
record that carries the measurement. Added, from the author's question:
the engine is the only holder of the registry, no client carries a
copy, `registry()` returns a digest so skew is detected rather than
guessed.

### 2026-09-09 — Fix check on the second fix (FAIL), third fix

T2 `nexus_rdr/205-fix-check-6c8fa32d4`. The tuple sweep enumerates its
tenants from `nexus.tuples`, not from `service_tokens`, whose rows the
same cycle deletes (four sites). Day 2 points templates at `nx tuple
templates`. CA 5 cites the T2 record that carries the measurement
verbatim. The registry parenthetical says what research 4 records.
Approach counts eleven operations.

### 2026-09-09 — Fix check on the third fix (FAIL), fourth fix: the sweep redesigned

T2 `nexus_rdr/205-fix-check-193d2b5ad`. Three fixes had reworded the
same clause; the constraint they missed is that no engine role can
enumerate tenants across a forced-RLS table, and the existing sweep
manages only because `service_tokens` has no row-level security. The
tuple sweep now enumerates from its own non-RLS `nexus.tuple_tenants`
table, upserted by `out` and never purged by the sweep, and runs as a
second scheduled task on the same scheduler rather than an arm of a loop
whose tenant set is built once. The availability predicate gains
`expires_at > now()`, so sweep latency is hygiene only. Day 2's tuples
row lists with `nx tuple list`; the round-2 revision entry cites
8ec08d5af now that it is published.

### 2026-09-09 — Fix check on the fourth fix (FAIL, no ship-blocker), fifth fix

T2 `nexus_rdr/205-fix-check-390e3c41b`. The redesign stood; the clause
justifying it said "every engine role is NOBYPASSRLS", and
`nexus_diag` bypasses RLS for integrity checks (RDR-182), so the claim
is now about the service role the engine runs on. The autovacuum
setting names its two tables; the tenant table's `last_seen` is
refreshed at most once a minute so an ordinary `out` takes no lock on
it; the second sweep task enumerates under the same statement bound the
token loop uses; `subspace_stats` counts live rows and reports
`expired_unpurged` separately, the oldest-unclaimed doctor row reads
live rows only, and the counted record and the Day 2 verify cell name
tenants visited.

### 2026-09-09 — Fix check on the fifth fix (FAIL, no ship-blocker), sixth fix

T2 `nexus_rdr/205-fix-check-0e2b22e80`. `subspace_list` reports
`expired_unpurged` and qualifies `total` as `subspace_stats` does; Phase
1 Step 5 names the statement bound; Failure Modes enumerates the same
five counted fields as the counted record; the tenant table's no-lock
property is delivered by a read-before-upsert, not asserted of the
upsert.

### 2026-09-09 — Fix check on the sixth fix (FAIL, no ship-blocker), seventh fix

T2 `nexus_rdr/205-fix-check-700a741c6`. The statement bound applies to
every statement the tuple sweep issues, not only its enumeration
(`NexusService.java:539-542` is the doctrine: bounding one arm of three
leaves the cycle unbounded), stated in Technical Design and Phase 1 Step
5. `subspace_list`'s timestamps span all rows, expired included, which
is what the census's stalled-projection detector needs. Round 2 of the
gate is run on this text: the sixth check found no ship-blocker
(the three before it found one critical each on the third and fourth
fixes and none on the fifth).

### 2026-09-09 — Gate round 2, BLOCKED (3 critical, 11 significant), fixes applied

Critique: T2 `nexus_rdr/205-gate-critique-2026-09-09b`. Layer 0 was
clean. Decisions taken with Sam: ledger rows are read-only and the
census reads with plain `rd`, so `include_consumed` leaves v1; `out` is
idempotent everywhere, the id derived from caller-supplied fields per the
template's `id_from` (keys; keys plus a sender-minted nonce; keys plus
body), never the insert time; blocking reads stay in v1 with their two
parking callers named (an orchestrator waiting for one agent's report,
a requester waiting for its ack); `nack` counts attempts and the
template's `max_attempts` dead-letters the row with a `dead` log row.
The tuple sweep gains a budget on the task itself (batches per tenant,
wall clock per run, remainder carried), beside the statement bound. The
claim statement writes the `expire` row when it takes a lapsed row; the
log's `tuple_id` is indexed; the log TTL is checked against every
template's retention at boot; both templates declare a retention and
`out` defaults to it; the census compares within the ledger's window
and reports absent projections; the 8 KB guard measures the serialised
request; `where` leaves the read signatures; the tenant table is
maintained in the tuple insert's transaction; the `scope` column goes
(addressing is the subspace, and the mailbox carries `address_kind`);
`subspaces()` goes, `registry()` covers it, ten operations; three
tables and the three doctor rows named consistently; `since` is a
`(created_at, id)` cursor; Prerequisites keep only what precedes Phase
1; Gap 2 cites RDR-184 §Context for its count. Cross-walked: every fix
above was checked against the others before this commit.
