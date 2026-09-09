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
related_rdrs: [RDR-041, RDR-105, RDR-110, RDR-120, RDR-127, RDR-149, RDR-152, RDR-155, RDR-158, RDR-184, RDR-204]
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
entanglement (five further RDRs stacked on it, 67 stranded beads). The
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
the operation. Scratch (T1) and memory (T2) are read-multiple: two readers
of the same entry both see it, and neither can claim it.

#### Gap 2: Dispatched agents owe reports, and the ledger cannot name them

RDR-184 Gap 1 recorded ten occurrences of an agent finishing without
reporting, one of them a failed release-gate result read hours later from
a raw output file. The remedy that shipped, the expectations ledger, is a
per-session TSV written by a bash hook and keyed on the agent's type only,
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
| RDR-120 (closed 2026-05-27) | Origin of the moratorium | §Scope Boundaries lists "tuple-space primitives: atomic in/out/rd, blocking take" as blocked until 30 days after its P6 under `NX_STORAGE_MODE=daemon`, lifted by "a follow-on RDR if any consumer is wanted". Its implementation notes record that the calendar lift was replaced by the substrate stress-validation arc (nexus-57pwo, closed), and the daemon mode it names was retired by the PG migration. The lift clause is the authority for this RDR existing. Its scope rule, no co-shipped consumers beyond the ones the RDR names, is adopted verbatim in §Approach. |
| RDR-184 (orchestration protocol hardening) | Precedent | Diagnosed Gaps 2 and 3 with counts and shipped the flat ledger and the inbox rule. This RDR does not re-diagnose; it gives those remedies a key and a store. The ledger's fail-open hook contract is inherited unchanged. |
| RDR-105 and JDR-001 (T1 identity, three scopes) | Adjacent, untouched | The T1 session lease file holds the MCP server's minted credential so a CLI borrows it instead of re-minting; `current_session` is the no-harness fallback; the handoff marker is a hook message to a live MCP server. A draft of this design proposed moving them into the space and a review pass withdrew it: a reader needs a credential before it can read a tuple, and the identity inventory (T2 `nexus/s1-t1-identity-inventory-2026-08-22`) rules KEEP. This RDR consumes session identity from that layer and does not own it. |
| RDR-152, RDR-155, RDR-158 | Origin of the substrate | The engine, the PG-only storage rule, and the removal of the SQLite and Chroma paths. The `aspect_extraction_queue` claim in RDR-152's engine is the proven statement this RDR generalises. |
| RDR-149 (service registry) | Adjacent, untouched | Daemon discovery and lifecycle stay in the shared primitive. Nothing here changes how the engine is found. |
| RDR-204 (embedding profile, ghost sweep) | Precedent | Its ghost-sweep and quarantine-reclaim pattern is the retention model for tuples; its tenant premise (one model per content type per tenant, verified on both production tenants) is the reason the semantic column can be one fixed dimension. |
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
  live. In front of the engine, three timeouts in series: the ALB idle
  timeout of 60 s, the control plane's upstream response-start timeout
  of 30 s (an env knob, `CONEXUS_UPSTREAM_REQUEST_TIMEOUT_MS`) with a
  60 s exchange deadline, and the sidecar's 120 s read timeout. The WAF
  rejects request bodies over 8 KB. `/v1/*` forwards the bearer token
  unvalidated to the engine, whose `AuthFilter` is the check.
- Client: Python 3.12, `HttpScratchStore` for T1, HTTP stores for T2,
  MCP tools in `nexus.mcp`, hooks in `conexus/hooks/scripts/`.
- Harness facts (Claude Code 2.1.251, measured in `expectations.sh`):
  the `SubagentStart` payload carries an opaque per-instance `agent_id`
  and the `subagent_type`; the Agent tool has no name parameter; the
  `PreToolUse` payload's `tool_use_id` is absent from `SubagentStart`.
  The conexus `SubagentStart` hook already injects context into the
  agent.
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

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Postgres row locking | Yes (docs, explicit-locking) | `FOR NO KEY UPDATE … SKIP LOCKED` is valid, sufficient for an update that does not touch key columns, and the locked select cannot block; the enclosing transaction can still deadlock on other locks. Clause order is `ORDER BY … LIMIT n FOR NO KEY UPDATE SKIP LOCKED`. |
| Postgres HOT updates | Yes (docs, storage-hot; `relcache.c`) | An update that changes a column in a partial index's predicate is not HOT-eligible. A predicate on `claim_state` or `consumed_at` makes every claim and ack a non-HOT update. |
| LISTEN/NOTIFY under PgBouncer | Yes (PgBouncer docs) | `LISTEN` is unsupported in transaction pooling mode. Oban ships a non-Postgres notifier for exactly this. |
| jOOQ 3.20.11 | Yes | `forNoKeyUpdate()` since 3.12; chains `.skipLocked()`. |
| `AspectRepository` | Yes (`:1056-1132`, `:1206`) | `claimNext` is `forUpdate().skipLocked()` inside one tenant-scoped transaction; `claimBatch` loops `claimNext`, one transaction per row; `reclaimStale` releases in-progress rows past a timeout. The table carries a foreign key (`fk-003`) and forced RLS, and passes the transaction-mode PgBouncer isolation test. |
| Liquibase changelog | Yes | No changeset sets a per-table autovacuum factor or fillfactor today; this RDR introduces the first. |
| Claude Code hook payloads | Yes (`expectations.sh:20-63`, `agent-dispatch-expect.sh:7-36`) | Per-instance `agent_id` exists only on the `SubagentStart` side; whether `SubagentStop` carries the same id is not recorded and is a spike. |
| pgvector | Yes | `vector(1024)` column type; nullable. |

### Key Discoveries

- **Verified** — The single-row atomic claim this RDR needs runs in
  production today on a table with a foreign key and forced RLS, through
  a transaction-mode pooler (`AspectRepository.claimNext`,
  `PgBouncerTenantIsolationTest`).
- **Verified** — Load on this box, from 44 session ledgers: 687 EXPECT
  rows, 3,443 rows in all, a mean of 16 dispatches per session and a
  peak of 78, busiest ledger 391 rows. Peak coordination traffic is
  under ten operations a second. The design is insensitive to these
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

- [ ] **CA 1: A single-statement claim on the new table is atomic across
      concurrent claimants, including through a transaction-mode
      pooler.** — **Status**: Verified for the aspect queue by source
      search and the existing isolation test; Unverified for the new
      table until the ten-worker harness runs. — **Method**: Source
      Search, then Spike (Phase 1 Step 6).
- [ ] **CA 2: The `SubagentStart` hook can inject the harness's
      per-instance `agent_id` into the agent's context, and the same id
      is recoverable at `SubagentStop`.** — **Status**: Unverified. The
      start-side id is documented; the stop side is not. If the stop
      payload lacks it, the report tuple is keyed by the injected id the
      agent carries, and the stop hook matches on type as today. —
      **Method**: Spike (one dispatch, both hooks logged).
- [x] **CA 3: A parked long-poll survives the public edge in front of
      the managed engine.** — **Status**: Verified as a constraint,
      not a capability: the control plane times out a response that has
      not started within 30 s. Design consequence: `timeout_s` is capped
      at 25 s by default (an engine setting), the call returns the probe
      result at the cap, and the client loops. Raising the control-plane
      knob or sending early headers are recorded options, not taken.
      — **Method**: Docs Only (conexus's measurement of the live edge),
      confirmed by a Spike through `tests/e2e/cloud-client-path-gate.sh`
      in Phase 1 Step 6.
- [ ] **CA 4: The hook-path write can be projected to the engine without
      ever blocking a dispatch.** — **Status**: Unverified. — **Method**:
      Spike: the projection runs after the TSV append, in the background,
      and the hook's exit code and latency are measured with the engine
      up, down, and rate-limiting mints.
- [x] **CA 5: One engine JVM at any time.** — **Status**: Verified
      (one container, stop-then-start deploys, measured gap about 25 s).
      The in-process wake is therefore the mechanism, not an
      optimisation. The one-second re-run timer stays as defence against
      a missed signal, and a parked client must treat a 502/504 during a
      deploy as a retry, not an error. — **Method**: Docs Only
      (conexus's measurement), recorded here; re-checked at the Phase 1
      close.
- [x] **CA 6: Both instances mint against one tenant.** — **Status**:
      Verified (one box, one config, `mint_tenant: nexus`). Gap 5 is a
      same-tenant mailbox. — **Method**: Docs Only (config and the
      cloud store's tenant list), recorded here.
- [x] **CA 7: Production connects to Postgres directly.** — **Status**:
      Verified on the engine host. `LISTEN/NOTIFY` is available and
      still deferred, because one JVM needs no cross-process wake; the
      two engine tests that disagreed are reconciled in Phase 1 Step 1.
      — **Method**: Docs Only (conexus's reading of the live host).

**Method definitions**: Source Search (API verified against dependency
source), Spike (behaviour verified by running code against a live
service), Docs Only (documentation or a named authority's answer;
insufficient alone for a load-bearing assumption).

## Proposed Solution

### Approach

Ship one engine-owned table, `nexus.tuples`, with its append-only claim
log, a registry of subspace schemas checked at engine boot, seven HTTP
operations (`out`, `rd`, `rdp`, `in`, `inp`, `ack`, `nack`) plus registry
introspection, a Python HTTP store shaped like the aspect-queue client, a
small MCP tool set and an `nx tuple` verb over it. The destructive read
matches on equality over a pinned key set per subspace; the
non-destructive read may additionally rank by pgvector similarity in a
later phase, gated on a calibration result this RDR does not claim.
Blocking reads park on an in-engine waiter woken at commit, with a
one-second poll as the fallback that carries deploys.

Two consumers land with it and no more: the RDR-184 dispatch ledger, as
start and report tuples keyed on the hook-injected `agent_id` with the
TSV kept as the write-ahead on the hook path, and a mailbox read on
demand, addressed to an agent id or to an instance. The cross-instance
request and ack of Gap 5 is the mailbox with `scope = instance`, not a
third consumer, because both instances mint against one tenant. Nothing
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
*claimant* is the id doing the taking.

**Table (illustrative; the changeset is the authority):**

```text
nexus.tuples
  id             bytea PK      sha256(canonical(tenant, subspace, keys, nonce))
  tenant_id      text          forced RLS, same discipline as every tenant table
  subspace       text
  template       text          registered template name
  scope          text          session | instance | host | tenant
  keys           jsonb         pinned per template; equality match for in/inp
  dims           jsonb         validated against the template's schema
  body           text
  embedding      vector(1024)  nullable; present only when the template declares embed_from
  claim_state    text          NULL | 'claimed'
  claimant       text
  claim_id       text
  lease_until    timestamptz
  consumed_at    timestamptz   NULL = available
  consumed_by    text
  expires_at     timestamptz   TTL, never NULL
  created_at     timestamptz

nexus.tuple_claim_log             append-only: claim | ack | nack | expire
  log_id, tuple_id REFERENCES nexus.tuples(id), claim_id, claimant, transition, at
```

The nonce in the id is the claimant or instance id for coordination
subspaces (two byte-identical messages from two agents are two tuples)
and empty for a subspace that declares idempotent `out`. This is a
per-template decision recorded in the registry; RDR-110's C3 finding is
the reason a global formula is wrong.

**Claim (illustrative jOOQ shape; the repository method is the
authority):**

```text
// in one tenant-scoped transaction, nothing else in it
row = select(...).from(TUPLES)
        .where(TENANT.eq(t), SUBSPACE.eq(s), KEYS.eq(pattern),
               CONSUMED_AT.isNull(),
               CLAIM_STATE.isNull().or(LEASE_UNTIL.lt(now())))
        .orderBy(CREATED_AT).limit(1)
        .forNoKeyUpdate().skipLocked().fetchOne();
update(TUPLES).set(CLAIM_STATE, "claimed").set(CLAIMANT, c)
        .set(CLAIM_ID, id).set(LEASE_UNTIL, now + lease)
        .where(ID.eq(row.id)).execute();
insert claim_log(tuple_id, claim_id, c, 'claim', now)
```

`FOR NO KEY UPDATE` because the update touches no key column and the
weaker lock lets the log's foreign key coexist. `SKIP LOCKED` because a
contended row is skipped, not waited on. The transaction holds the claim
and nothing else, so the taxonomy-015 class (a lock upgrade inside one
transaction) has nothing to upgrade.

**Operations (HTTP under `/v1/tuples`; signatures are the contract, the
handler is the implementation):**

```text
out(subspace, keys, dims, body, *, scope, ttl_seconds, embed=False) -> tuple_id
rd (subspace, keys_pattern, *, where=None, n=1, timeout_s=0) -> [Tuple]   # non-destructive; blocks up to timeout_s
rdp(subspace, keys_pattern, *, where=None, n=1) -> [Tuple]                # probe
in (subspace, keys_pattern, *, claimant, lease_s, timeout_s=0) -> (Tuple, claim_id) | None
inp(subspace, keys_pattern, *, claimant, lease_s) -> (Tuple, claim_id) | None
ack(claim_id) ; nack(claim_id)
subspaces() -> [TemplateSchema] ; subspace_stats(subspace) -> counts by state
```

`timeout_s` is capped at 25 seconds by default (CA 3: the edge times
out a response that has not started within 30 s), settable on the
engine; a call at the cap returns the probe result and the caller
loops. A body over the edge is bounded by the WAF's 8 KB request limit,
which the client enforces before sending. Errors are typed: `UnknownSubspace`, `SchemaViolation`
(field and reason, before any write), `TakeDisabled`, `TimeoutTooLong`.

**Wake.** There is one engine JVM (CA 5) and every `out` passes through
it. The handler that commits an `out` signals a per-subspace waiter set
in the same JVM; parked `rd`/`in` calls sit on virtual threads and re-run
their query on signal. A one-second timer re-runs the query regardless,
as defence against a missed signal, not as a replica story. During a
deploy the edge returns 502 or 504 for about 25 seconds; the client
treats that as a retry of the same call. `LISTEN/NOTIFY` is available on
the direct connection (CA 7) and deferred until a second JVM exists; the
one thing to measure first when it is adopted is Crunchy's idle and
statement timeouts on a held-open connection.

**Registry.** Templates ship as YAML in engine resources, loaded and
validated at boot; a breach fails boot with the file and field named.
Schema evolution is additive in v1. v1 templates: `ledger/<session_id>`
(keys: `agent_id`, `kind` in {start, report}; nonce: agent_id),
`mailbox/<address>` (keys: `to`; dims: `from`, `kind`, `correlation_id`;
scope: `agent` or `instance`; nonce: from + created). An instance address
is the session name `ListAgents` shows (for example `nexus-23`), so the
Gap 5 request is `out` to `mailbox/conexus-ed` and its ack is `out` back.

**Indexes and hygiene.** Partial index on `(tenant_id, subspace,
created_at) WHERE consumed_at IS NULL AND claim_state IS NULL` for the
claim scan; a GIN index on `keys` only if a consumer's pattern needs it.
Claims and acks write predicate columns and are therefore not HOT
updates; at the measured load the index churn is affordable and the
sweep budget assumes it. `autovacuum_vacuum_scale_factor = 0.01` on
both tables (new precedent; the changeset comment says so). TTL on every
row; the existing sweep scheduler deletes expired and consumed-past-
retention rows in batches of a few hundred, in the RDR-204 sweep shape,
with the same non-vacuity assertion on the sweep's own row counts. If a
consumer ever pushes the table into millions of rows, the Solid Queue
shape (a separate claimable table) is the fix, not a fillfactor.

**Client.** `nexus.db.t2.http_tuple_store.HttpTupleStore`, constructor-
injected like the other stores; MCP tools `tuple_out`, `tuple_rd`,
`tuple_in`, `tuple_ack`, `tuple_nack`, `tuple_subspaces`; `nx tuple
{out,rd,in,ack,nack,list,stats}`; two `nx doctor` rows (oldest unclaimed
age per subspace; dead-tuple ratio and last autovacuum on the table).

**Identity and scope.** The `SubagentStart` hook mints nothing: it takes
the harness's `agent_id`, writes the start tuple, and injects the same id
into the agent's context as its claimant id and mailbox address. The
`PreToolUse` expectation row keeps covering a dispatch that never starts.
Every tuple's `scope` value comes from that injected id or from the
existing session lease, never from resolving the session at write time.

**Hook path.** The TSV append stays exactly as it is and stays the
write-ahead. The projection to the engine runs after the append, detached
from the hook's exit, and its failure is logged and never propagated. The
census reads the space and falls back to the TSV with a named reason when
the engine is unreachable.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Atomic claim | `AspectRepository.claimNext` / `reclaimStale` | Reuse the statement shape and the tenant-scoped transaction; new repository, since the queue's columns are aspect-specific. |
| Batch claim | `AspectRepository.claimBatch` (a loop) | Do not reuse; a real `LIMIT n` claim is new work and lands only when a consumer asks. |
| Sweep | `NexusService` sweep scheduler; RDR-204 ghost sweep | Extend: one more scheduled sweep with the same batch and non-vacuity discipline. |
| Tenant scoping | `TenantScope`, forced RLS changesets | Reuse unchanged. |
| HTTP store client | `http_aspect_queue.py` | Reuse the shape (constructor injection, typed errors, data-token handling). |
| Dispatch ledger | `expectations.sh`, `agent-dispatch-expect.sh`, `subagent-start.sh` | Extend: the start hook writes a tuple and injects the id; the census gains a space-backed path. The TSV and its readers stay. |
| Session identity | `t1.py` lease and handoff, JDR-001 | Untouched, by decision. |
| Registry loader | RDR-110's YAML registry (archive branch) | Reuse the schema format; the loader moves to the engine. |

### Decision Rationale

Three things decide the shape. The claim statement is already proven in
this engine, so the risk in the primitive is near zero and the cost is a
table and a handler. The two consumers are the two most-measured failures
in the orchestration record and each is a small change to a hook that
already exists. And RDR-120's rule about co-shipped consumers is the only
structural difference between the attempt that shipped and the attempt
that was scrapped, so it is written into §Approach rather than left to
discipline.

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

**Reason for rejection**: Deferred, not rejected. The column is in the
table so it can land additively when a consumer asks and the fuzz gate
passes on the engine's embedding.

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

- One new table pair, one handler, one client store, six MCP tools, one
  CLI verb. The engine's public surface grows by one route family.
- The ledger and the mailbox get a real per-instance key and a store that
  can be queried across sessions and processes.
- A new operational surface: lease expiries, oldest-unclaimed age and
  dead-tuple ratio become doctor rows.
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
- **Risk**: Table bloat from claim/ack churn on non-HOT updates.
  **Mitigation**: Per-table autovacuum factor, TTL on every row, batched
  sweep, doctor row; the Solid Queue shape as the named next step.
- **Risk**: Two same-type dispatches collapse into one ledger tuple.
  **Mitigation**: The nonce in the id for coordination templates,
  pinned by a test that writes two identical starts and reads two rows.
- **Risk**: The hook projection blocks or fails a dispatch.
  **Mitigation**: Append first, project detached, never propagate; CA 4
  measures with the engine down.

### Failure Modes

- Visible: `SchemaViolation` before any write, naming field and reason.
- Visible: `in`/`inp` return `None`; the caller loops or reports no
  work.
- Visible: sweep logs `tuples_expired`, `claims_released`,
  `tuples_purged` counts every run; a run that finds nothing to check is
  not a pass, per the vacuous-gate doctrine.
- Silent, resolved: a claimant crashes after `in`; the lease lapses and
  the sweep releases the row with an `expire` log entry.
- Silent, resolved: two claimants race; `SKIP LOCKED` gives each a
  distinct row or `None`.
- Silent, open until CA 4: the projection silently stops and the census
  reads a space that is behind the TSV. Mitigation: the census reports
  the space's newest row age against the TSV's, and a gap is a finding.

## Implementation Plan

### Prerequisites

- [ ] CA 1, CA 2 and CA 4 verified by their named spikes; CA 3 confirmed
      through the cloud client-path gate. CA 5 to CA 7 are recorded from
      conexus's measurements of 2026-09-09.
- [ ] The RDR-120 lift statement in §Relationship to Prior RDRs stands
      unchallenged at the gate.
- [ ] `PgBouncerTenantIsolationTest`'s comment corrected to say it
      exercises a topology that is not deployed (Phase 1 Step 1).

### Minimum Viable Validation

Two runs, both in scope. First, a real session dispatches ten sub-agents
of two types; the space holds ten start tuples with ten distinct ids, the
orchestrator takes ten report tuples, and the space-backed census agrees
with the TSV census row for row. Second, the ten-worker work-stealing
harness from RDR-110 runs against the engine on a `tasks`-shaped test
template with six metrics recorded (wake latency through the public edge,
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

`tuples-001-baseline.xml`: both tables, the partial index, forced RLS
and grants in the house pattern, the per-table autovacuum factor with a
comment naming this RDR as the first use.

#### Step 3: Registry

YAML templates in engine resources; loader and validator at boot; the
three v1 templates, the third marked conditional.

#### Step 4: Repository and handler

`TupleRepository` (claim, out, read, ack, nack, stats, sweep) in jOOQ;
`TupleHandler` under `/v1/tuples`; the waiter set and the one-second
re-run timer; typed errors.

#### Step 5: Sweep

One scheduled sweep on the existing scheduler: release lapsed claims with
a log entry, purge expired and consumed-past-retention rows in batches,
assert non-vacuity on its own counts.

#### Step 6: Spikes

CA 1 on the new table (the ten-worker harness with an injected delay
between select and update), CA 3 through the cloud client-path gate.

### Phase 2: Client

#### Step 1: `HttpTupleStore` and tests against the engine substrate.

#### Step 2: MCP tools and `nx tuple`; doctor rows.

#### Step 3: CA 2 and CA 4 spikes (hook injection and hook-path timing).

### Phase 3: Consumer one, the ledger

The `SubagentStart` hook writes the start tuple and injects the id; the
stop hook or the agent writes the report tuple; `expectations_census`
gains a space-backed path with the TSV fallback and the age comparison.
The MVV's first run closes this phase.

### Phase 4: Consumer two, the mailbox

A `mailbox` skill and one paragraph in the orchestration skill: send by
`tuple_out` to the agent's id, drain by `tuple_in` before composing any
hand-back. A scenario test with a mid-turn directive.

### Phase 5: Cross-instance request and ack

The mailbox with `scope = instance`: the relay convention in the
cross-instance memory becomes `tuple_out` to the peer's address with an
ack expected under a lease, and the nexus-w374z sweep reads unacked
requests from the space. A scenario test with two sessions on one box.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Templates | `nx tuple list` | `nx tuple stats <subspace>` | Via changeset | Boot validation | git |
| `nexus.tuples` | `nx tuple stats` | doctor rows | TTL sweep; purge by tenant via admin SQL | `nx doctor` | PG bundle / managed backups |
| `nexus.tuple_claim_log` | via stats | per-claim history | retention sweep | `nx doctor` | same |

### New Dependencies

None. Postgres, pgvector, jOOQ and Liquibase are in place.

## Test Plan

- **Scenario**: `out` with a schema breach — **Verify**: `SchemaViolation`
  names field and reason; no row written.
- **Scenario**: two identical `out` calls on a coordination template —
  **Verify**: two rows (nonce); on an idempotent template, one row.
- **Scenario**: ten concurrent `inp` on one available row — **Verify**:
  exactly one `(Tuple, claim_id)`, nine `None`; one `claim` log entry.
- **Scenario**: `in` then crash before `ack` — **Verify**: after the
  lease, the sweep releases the row with an `expire` entry and a new
  claimant takes it.
- **Scenario**: `ack` — **Verify**: `consumed_at` set, `ack` logged, the
  row invisible to `rd` and `in`.
- **Scenario**: a less specific key pattern against a more specific
  tuple — **Verify**: no match (equality, not containment).
- **Scenario**: tenant A's `rd` against tenant B's mailbox — **Verify**:
  empty, by RLS, with no error that names the other tenant.
- **Scenario**: `rd` with `timeout_s` while another client `out`s —
  **Verify**: returns within the wake budget; with the waiter disabled,
  within the poll interval.
- **Scenario**: the parked call through the public edge at the 25 s cap
  — **Verify**: returns the probe result without a 504; at 31 s the edge
  returns 504, which pins the reason for the cap.
- **Scenario**: hook append with the engine down — **Verify**: the hook's
  exit code and latency are unchanged; the projection failure is logged;
  the census falls back with a named reason.
- **Scenario**: bloat leg — **Verify**: dead-tuple ratio stays under the
  target across a million cycles with the per-table autovacuum factor.
- **Scenario**: the sweep finds nothing to check — **Verify**: reported
  as a non-vacuity failure, not a pass.

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
| `vector(1024)` | pgvector | Docs |
| `SubagentStart` payload `agent_id` | Claude Code 2.1.251 | Source Search (`expectations.sh`) |
| `SubagentStop` payload | Claude Code | Spike (CA 2) |

### Scope Verification

_To be completed during `/conexus:rdr-gate`. Both MVV runs are Phase 3
and Phase 1 deliverables, not deferred._

### Cross-Cutting Concerns

- **Versioning**: templates evolve additively; a removal is a changeset
  with a migration note.
- **Build tool compatibility**: N/A.
- **Licensing**: no new dependencies.
- **Deployment model**: engine-owned; a cloud deploy carries the
  changeset through the PITR fork rehearsal like any other; local mode
  gets it at the pinned engine version. The space is per-install in local
  mode and shared per tenant in cloud mode; the RDR declares both. A
  stop-then-start deploy parks every reader for about 25 s; clients
  retry.
- **IDE compatibility**: N/A.
- **Incremental adoption**: additive; no existing caller changes.
- **Secret/credential lifecycle**: unchanged; the data token discipline
  applies; tenant binding decides visibility.
- **Memory management**: TTL on every row; batched sweep; parked calls
  on virtual threads.

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
