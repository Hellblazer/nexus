---
title: "Broadcast Board, Work Queue, and Lock as Tuple-Space Templates"
id: RDR-211
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-15
accepted_date:
related_issues: []
related_rdrs: [RDR-205, RDR-206, RDR-208, RDR-184, RDR-110]
---

# RDR-211: Broadcast Board, Work Queue, and Lock as Tuple-Space Templates

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

**Provenance.** Sam, 2026-09-15, while revising the Coordination site page:
"we don't really discuss the 1 -> many coordination, broadcast capabilities",
then "draft the RDR covering board, queue and lock." The research behind this
draft read the tuple-space design records, the engine, and the clients at
origin/develop ea014c62b.

Terms used throughout:

- **Tuple space**: the shared store of small records that Nexus sessions,
  agents, hooks, and scripts use to coordinate, built on David Gelernter's Linda
  model (RDR-205). It lives in the engine.
- **Engine**: the Java `nexus-service` process that owns the database and
  handles every tuple call.
- **Tuple**: one record in the tuple space. It has **keys** (the fields a reader
  matches on), **dimensions** (descriptive fields that are not matched), and an
  optional short **body**.
- **Subspace**: a named part of the tuple space, such as `mailbox/<address>`.
- **Template**: the engine's registered definition of one kind of subspace: its
  keys, dimensions, identity rule, retention, and whether tuples can be taken.
- **out / rd / in**: the three Linda operations. `out` adds a tuple. `rd` reads
  matching tuples and leaves them in place. `in` takes one matching tuple, so
  that only one reader acts on it.
- **Claim and lease**: an `in` gives the caller a claim on the tuple for a
  limited time, the lease. The caller ends the claim with `ack` (the tuple is
  consumed) or `nack` (the tuple goes back). `renew` extends a live lease.
- **Attempt and dead letter**: each `nack`, and each lease that ends without an
  ack, counts one failed attempt. At the template's `max_attempts`, the tuple
  becomes a dead letter: readable by `rd`, never takeable again.

## Problem Statement

The engine registers three templates: `mailbox` (messages, taken once),
`directory` (which session holds each name), and `ledger` (agent starts and
reports, read and never taken). RDR-205 named the work queue and the lock as the
coordination shapes that keep recurring, and chose one generic tuple space over
separate tables so that later shapes would be templates, not new machinery. It
built only the ledger and the mailbox and said: "Any consumer not named in
RDR-205 needs its own RDR." This is that RDR for three shapes, and for one engine
operation that two of them cannot work without.

### Enumerated gaps to close

#### Gap 1: No one-to-many announcement

A session that wants every other session to know something (a release is
published, a shared service is down, a branch is frozen) has no single record to
write. SendMessage takes one recipient per call, and a mailbox message is taken
by one reader. Today the sender repeats the message once per peer, knows only
the peers it can see now, and leaves no record that a later session can read.
`rd` already serves any number of readers from one tuple, and the engine already
wakes every reader waiting on a subspace when a tuple is written there. What is
missing is a template whose purpose is announcements: readable by every session,
never taken, and kept long enough for a session that starts later to catch up.

#### Gap 2: No shared work queue

A session or script that has several independent tasks, and several workers
(agents or sessions) that could each do any of them, has no place to put the
tasks so that each is done exactly once. A mailbox is addressed to one reader
(its `to` key), so it assigns work to a worker before the work starts. A queue
addresses the work, not the worker: any free worker takes the next task, a
worker that stops loses nothing because its lease ends and the task returns, and
a task that keeps failing becomes a visible dead letter. web/tuple-space.html's
uses table lists the work queue as "not built".

#### Gap 3: No cross-session lock

Two sessions or machines that must not act on the same shared resource at the
same time (one release in flight, one migration against a shared database, one
writer of a shared document) have no way to agree which of them holds it. The
machine-local locks in this repository, such as the build lease in
`scripts/lib/build-lease.sh`, use the filesystem and cannot be seen from another
machine or another tenant member's session. web/tuple-space.html's uses table
lists the lock as "not built".

#### Gap 4: No way to give back a claim without it counting as a failure

A lock holder that finishes, and a worker that decides a task belongs to someone
else, both need to return a claim without consuming the tuple and without
counting a failure. The engine has no such operation. `nack` always counts an
attempt (TupleRepository.java:1014-1032), so a lock that is released three times
by healthy holders dead-letters itself, and a queue that hands tasks back
dead-letters tasks that never failed. `ack` is no substitute for a lock release:
it consumes the lock tuple, and a later `out` with the same identity only
refreshes the consumed row's expiry (TupleRepository.java:420-426), so the lock
cannot be recreated until the sweep purges that row.

## Relationship to Prior RDRs

The scan covered every RDR whose title names the tuple space, Linda, the
mailbox, the ledger, or addressing, plus RDR-110, the scrapped predecessor.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-205 (Linda tuple space over Postgres) | Origin | Built the tuple space and its template registry. Its Alternative 1 rejected separate queue, mailbox, and lock tables because "one primitive with a registry is less code than three tables by the second consumer" (rdr-205:1006-1018). That rationale still holds, and it is why this RDR adds templates. Its scope gate requires this RDR. |
| RDR-206 (claim renew and reply-in-ack) | Precedent | Added two engine operations to the same claim machinery, closing items RDR-205 left "not scheduled" (rdr-206:72). This RDR adds a third operation, `release`, the same way. The queue uses reply-in-ack to answer the producer, and both the queue and the lock use `renew` for long work. |
| RDR-208 (session-id mail addressing) | Adjacent, shipped | Made the session id the address and added the directory. Nothing in RDR-208 is deferred to this RDR (rdr-208:519). Board authors, queue workers, and lock holders identify themselves by session id or agent id. |
| RDR-184 (orchestration protocol hardening) | Precedent | Its dispatch ledger records agent starts and reports in a local file. The tuple ledger template, built by RDR-205, records the same events in the tuple space and is the shipped example of a take-disabled, one-writer, many-reader subspace, which the board follows. Its Gap 3 (collisions between overlapping runs on one resource) is the class the lock serves across machines; the machine-local mkdir locks it chose over flock stay as they are. |
| RDR-110 (semantic tuple space, scrapped) | Superseded | Its SQLite design is gone, but one argument survives: a lock needs exact-key matching, because semantic matching "cannot guarantee exclusion" (rdr-110:814-821). The current engine's `in` already requires every pinned key to match exactly, so the lock inherits that property. |

## Context

### Background

The Coordination site page (web/coordination.html) describes how sessions and
agents coordinate through the tuple space. When Sam asked for one-to-many
coordination to be covered, the page could name only the directory and the
ledger as one-to-many uses, and had to list a broadcast board, a work queue,
and a lock as fitting the operations but not built. docs/exploration/linda-in-nexus.md
and docs/tuple-space.md (line 9) both repeat RDR-205's rule that a new consumer
needs its own RDR. No bead tracks any of the three: `bd search` for "work
queue", "queue", "broadcast", "board", and "lock" found nothing relevant.

### Technical Environment

- Templates are YAML resources under
  `service/src/main/resources/tuples/templates/`. `TemplateRegistry` loads them
  by an explicit list of paths, never by directory scan, because the native
  image cannot enumerate the classpath (TemplateRegistry.java:72-83). A new
  template is therefore a YAML file, one line in that list, and an engine
  release. The `nexus.tuples` table is generic, so no Liquibase change is
  needed.
- Clients discover templates at run time through `tuple_registry`
  (src/nexus/mcp/core.py:6562-6574) and `nx tuple templates`. The MCP tuple
  tools and the generic CLI verbs take the subspace as a parameter, so they work
  with a new template without code changes.
- `nx tuple watch` (src/nexus/tuple_watch.py) watches mailboxes only. Its
  addresses, ping lines, and state are all mailbox-specific (lines 5, 415-424,
  498).
- The tuple rows in `nx doctor` (src/nexus/health.py:5043, 5217, 5352) read
  templates generically and skip take-disabled subspaces.
- Row-level security isolates tenants and nothing finer: any session of a tenant
  can `out`, `rd`, and `in` in any subspace that has a template
  (tuples-001-baseline.xml:101-105). Claimant and sender fields are
  caller-supplied strings checked only for size.

## Research Findings

### Investigation

A read-only research pass read RDR-205, RDR-206, RDR-208, RDR-110,
docs/exploration/linda-in-nexus.md, docs/tuple-space.md, web/tuple-space.html,
the three template YAML files, TemplateSchema.java, TemplateRegistry.java,
TupleRepository.java, TupleLimits.java, the MCP tools, the CLI, the watcher,
health.py, and the tuple baseline changelog. It also ran the bead searches listed
above.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Engine template schema (TemplateSchema.java) | Yes | Fields: `name`, `keys` (optionally with allowed values), `dimensions` (type, values, required), `id_from` (`keys`, `keys+nonce`, `keys+body`), `id_dims`, `take` (`enabled`, `max_attempts`, `max_lease_seconds`), `retention_seconds`, `max_body_bytes` (lower only, at most 4096). |
| Engine claim operations (TupleRepository.java) | Yes | `in` uses `FOR NO KEY UPDATE SKIP LOCKED`, oldest first; `ack` consumes and clears the body; `nack` and lease lapse both count an attempt; `renew` does not; no release-without-attempt operation exists. |
| Engine wake-up (TupleRepository.java) | Yes | `out` and `ackWithReply` call `signalAll(tenant, subspace)` after commit, waking every reader parked on that subspace (lines 396-397, 917-919). |
| Client tuple tools (core.py, tuple_cmd.py) | Yes | Subspace-agnostic; new templates need no client change. A new engine operation needs a new tool and CLI verb. |

### Key Discoveries

- **Documented**: `out` is idempotent by identity. A second `out` of the same
  identity refreshes `expires_at`, clamped to the row's original retention
  ceiling, and never changes the body, claim, or consumed state
  (TupleRepository.java:399-433, the refire update at 420-426). So a template with `id_from: keys` lets any
  process "make sure the lock exists" safely: a second writer cannot create a
  second lock tuple.
- **Documented**: `rd` is non-destructive, supports subset key matching and a
  `(created_at, id)` cursor for paging, and waits at most 25 seconds per call
  (TupleRepository.java:584-637, :81).
- **Documented**: a waiting `rd` registers before its first query, so a write
  that lands between the query and the wait is not lost (lines 592-593).
- **Documented**: `in` requires every pinned key, returns a caller's own live
  claim again if it asks twice, reclaims a lapsed claim at the next take
  (counting an attempt), and clamps the lease to the tuple's expiry (lines
  685-810).
- **Documented**: `ack` marks the tuple consumed and sets the body to NULL
  (`consumeClaim` at 907, the update at 925-929, called from `ack` at 942). A consumed tuple
  stays as a row until the sweep purges it, and `rd` and `in` never return it.
- **Documented**: `nack` always counts an attempt (line 1026). There is no way to
  end a claim that neither consumes the tuple nor counts a failure.
- **Documented**: nothing in the schema or the security policy restricts which
  session may write to a subspace. A "single writer" is a convention, not an
  engine guarantee.
- **Documented**: the watcher cannot watch a board or a queue without new code.

### Critical Assumptions

- [x] Adding a template needs a YAML file, one line in
  `TemplateRegistry.RESOURCE_TEMPLATE_PATHS`, and an engine release, with no
  schema change. **Status**: Verified. **Method**: Source Search.
- [x] The client tuple tools work with a new template unchanged.
  **Status**: Verified. **Method**: Source Search.
- [x] A write wakes every reader parked on its subspace.
  **Status**: Verified. **Method**: Source Search.
- [x] `in` gives a tuple to exactly one claimant at a time.
  **Status**: Verified. **Method**: Source Search (`SKIP LOCKED` plus the claim
  state compare-and-swap).
- [x] A `release` operation can reuse the claim-release core
  (`releaseOrDeadLetter`) without counting an attempt, log a new `release`
  transition in the claim log without a schema change, and signal waiters after
  commit. **Status**: Verified 2026-09-16. **Method**: Spike.
  `TupleReleaseSpikeTest` 6/6: attempts unchanged, never dead-letters, lapsed
  and consumed claims raise `ClaimNotFound`, wrong claimant raises
  `ClaimOwnership`, a parked `in` wakes. `tuple_claim_log.transition` is plain
  `TEXT NOT NULL` with no check constraint (`tuples-001-baseline.xml`), so the
  new value needs no changeset. T2 `nexus_rdr/211-spike-1-2026-09-16`.
- [x] A lock template can tolerate holder crashes without dead-lettering
  itself. **Status**: Verified. **Method**: Source Search. A template that omits
  `max_attempts` is treated as unbounded (TupleRepository.java:772, 1025), so a
  lock never dead-letters, and every lapse is still logged.
- [x] The lock flag (claim and renew move expiry forward; `out` resets an
  expired lock row) can be scoped to lock templates without changing `out`'s
  idempotency for any other template. **Status**: Verified 2026-09-16, with one
  design consequence. **Method**: Spike. `TupleLockFlagSpikeTest` 3/3: a lock
  renewed across its retention boundary stays held and renewable, `out` on an
  expired lock row makes it available, and a non-flagged template's second
  `out` leaves the row byte-identical. The flag is read at three sites, not
  one: `writeOut`, `claimOnce` and `renew` (Technical Design). The existing
  tuple suites re-ran green with the prototype in place. T2
  `nexus_rdr/211-spike-2-2026-09-16`.
- [x] `ack` on a lock claim can be refused inside `consumeClaim`, after the
  claim's row is read and before any column is written, from the template name
  that row carries. **Status**: Verified 2026-09-16. **Method**: Source Search.
  `consumeClaim` reads the row first (TupleRepository.java:908) and refuses a
  stale claim or a wrong claimant there (909-912) before its update; the row's
  template name is already used at 1031. The placement is the one those two
  refusals use, so no spike (T2 `nexus_rdr/211-research-2`).
- [x] The wait registry can register one waiter in several subspace groups and
  wake it from any of them, without losing a write that lands between the first
  query and the park. `rd` gets that guarantee by registering before it queries
  (TupleRepository.java:592-593), and `wait` must keep it for every subspace.
  **Status**: Verified 2026-09-16. **Method**: Spike.
  `TupleWaitRegistryMultiSpikeTest` 5/5: a signal landing between register and
  the first await is not lost, a signal on any of three registered subspaces
  wakes the waiter, an unregistered subspace never does, and three
  registrations plus one park-slot call track one claimant. T2
  `nexus_rdr/211-spike-3-2026-09-16`.

## Proposed Solution

### Approach

Add three templates and two engine operations, `release` and `wait`, plus the
lock flag, two per-template guards (`max_live_rows`, a claim-log TTL) and an
engine-wide park-slot report. The templates reuse the claim machinery
the engine already has. `release` closes Gap 4, which the queue and the lock
share.

1. **`board/<topic>`**: announcements. Take disabled. Each post is a new tuple
   (`id_from: keys+nonce`), so the board is an append-only log. Readers use `rd`
   with a cursor to read what is new since they last looked, and the newest post
   is the current state of the topic. Any session can post, and the `from`
   dimension records who did. A reader that waits with `rd` wakes when a post
   arrives, as does every other waiting reader.
2. **`queue/<name>`**: shared work. Take enabled. Producers `out` one tuple per
   task (`id_from: keys+nonce`). Workers `in` the oldest available task, do the
   work under a lease they `renew` if needed, then `ack`, optionally with a reply
   to the producer's mailbox. A worker that cannot finish calls `nack` (a
   failure) or the new `release` (a hand-back that is not a failure).
3. **`lock/<resource>`**: one holder at a time. Take enabled. One tuple per
   resource (`id_from: keys`), so `out` is a safe "make sure the lock exists"
   for anyone. To hold the lock, a process takes the tuple with `in`. It renews
   the lease while it works and ends with `release`, which returns the tuple
   for the next holder. The lock is never acked, so it is never consumed, and
   the engine enforces that: `ack` on a lock claim is refused (Technical
   Design, the lock flag), because a consumed lock row is unobtainable until
   the sweep purges it. `nack` on a lock stays allowed; it returns the lock
   and counts an attempt, which a lock template never dead-letters on.
4. **`release(claim_id, claimant)`**: a new engine operation. It ends a live
   claim, returns the tuple to available without counting an attempt, logs a
   `release` transition, and signals the subspace's waiters after commit. A
   `release` on a claim that is no longer live fails the same way `ack` and
   `renew` do.
5. **A lock lives as long as it is used.** Today a tuple expires at its creation
   time plus the template's retention, and nothing can move that ceiling. A lock
   template gets a flag with three effects. A claim or renew moves the lock tuple's
   expiry to now plus retention. An `out` that meets an expired lock row resets
   it to available instead of leaving it dead. Retention then bounds only an idle
   lock (Scale and Limits).
6. **One parked call per process.** A new engine operation, `wait`, parks one
   call on several subspaces and returns when any of them has something new. A
   session's watcher waits on its mailboxes and board topics in one call, so the
   engine's park slots grow with processes, not with topics (Technical Design,
   Waiting).

### Technical Design

**Templates.** Illustrative only; the implementation fixes the exact values.

```text
// Illustrative, verify field names against TemplateSchema during implementation
board/<topic>:     keys [topic]; dims from (required), kind; id_from keys+nonce;
                   take disabled; retention 7 days (a post may set a shorter ttl);
                   max_body_bytes 1024; max_live_rows 500
queue/<name>:      keys [queue]; dims from (required), kind, correlation_id;
                   id_from keys+nonce; take enabled, max_attempts 3,
                   max_lease_seconds 900; retention 2 days; max_live_rows 10000
lock/<resource>:   keys [resource]; dims from; id_from keys; take enabled,
                   max_attempts omitted (never dead-letters); max_lease_seconds 900;
                   retention 7 days; the lock flag (Approach item 5)
```

**The `release` operation.** It follows the pattern RDR-206 used for `renew`:

- Engine: `release(tenant, claimId, claimant)` with the same compare-and-swap on
  the live claim that `ack`, `nack`, and `renew` use. On success it clears the
  claim without incrementing `attempts` and calls `signalAll` after commit. A
  claim that is not live raises `ClaimNotFoundException`, as the other three do.
- HTTP: one new endpoint beside `/v1/tuples/nack` and `/v1/tuples/renew`.
- Client: `HttpTupleStore.release`, an MCP tool `tuple_release`, and a CLI verb
  `nx tuple release`.
- Wire contract: an engine-plus-client change, recorded in
  `docs/wire-contract-pending.md` and shipped as a paired release.

**The lock flag.** A template-level `lock: true`. It is read at three sites,
and each is a branch in Java, so a template without the flag produces the same
SQL as today: `writeOut`, where an `out` that meets an expired lock row resets
it to available instead of leaving it dead; `claimOnce`, where a claim moves the
tuple's expiry to now plus retention before the lease is clamped against it;
and `renew`, where a renew does the same. Claim and renew both carry the flag
because the lease clamp otherwise still caps against the stale ceiling.

A fourth site refuses: `consumeClaim`, the body `ack` and `ackWithReply` share
(TupleRepository.java:907, called at 942 and 1001), raises `SchemaViolation`
when the claim's template carries the flag, with a message that names
`release`. The check sits inside the transaction, after `liveClaimRow` reads
the claim's row (line 908) and before any column is written, beside the
stale-claim and wrong-claimant refusals that already live there (lines
909-912); the template name is on that row, and nothing about a bare
`claim_id` encodes it, so the check cannot precede `withTenant`. The claim
stays live because the ack never wrote anything. The reset in `writeOut` clears the claim
columns but never `consumed_at`, and `claimOnce` requires `consumed_at IS
NULL` (lines 788 and 801), so an acked lock would stay dead until the sweep
purged it: the failure Alternative 1 is rejected for, reached by an ordinary
call. The check uses the existing `SchemaViolation` rather than a new typed
error, as RDR-206's reply-target shape check does, so the pinned error set is
unchanged.

**Waiting.** A parked call takes one of 16 slots shared by every tenant on one
engine (TupleRepository.java:86-87). So the design spends one slot per waiting
process, not one per thing it waits for. A new engine operation, `wait`, is a
multi-subspace `rd`. It parks one call on several subspaces, each with its own
key pattern and cursor, and it returns as soon as any of them holds a matching
tuple past its cursor. The watcher uses it to wait on its session's mailboxes and
on every board topic it follows, all in one call, instead of probing each address
every three seconds. Queue workers and would-be lock holders still park with `in`,
one slot each. Each parked call waits at most 25 seconds, so a longer wait is a
loop. `release` and `out` both signal waiters, so a released lock, a new task, or
a new post wakes the waiting processes at once. `wait` is an engine-plus-client
change and gets its own wire-contract ledger entry.

**The watcher.** `nx tuple watch` gains a board mode that pings once for each new
post on the named topics, with a per-topic cursor, and it waits on its mailboxes and topics with one
`wait` call. It stays read-only, as the
mailbox watcher is. Watching queues and locks is out of scope: a worker or a
would-be holder waits with `in`, which already wakes on `out` and `release`.

### Scale and Limits

This section rests on a read-only research pass (T2
nexus_rdr/211-research-scalability-2026-09-15). Its live numbers come from the
MCP tuple tools, and its query-plan claims are inferred from the index
definitions, because EXPLAIN was not run.

**Today (measured 2026-09-15 about 20:08Z).** The tenant holds 99 subspaces and
10,736 tuple rows. The ledger accounts for 10,461 rows in 46 subspaces and grows
about 2,346 rows a day, twenty times the "thousands of rows per month" RDR-205
planned for. At its 90-day retention it settles near 211,000 rows in about 930
subspaces. That growth happens whether or not this RDR ships.

**What this RDR adds (assumed load: 10 sessions a day, 3 to 6 at once; 10 board
topics with 50 posts a day; 3 queues with up to 1,000 tasks a day; 5 locks with
1,200 holds a day).**

| Template | Tuple rows a day | Steady-state rows | Claim-log rows a day | Claim-log rows at 180 days |
| --- | --- | --- | --- | --- |
| board | 50 | 350 | 0 | 0 |
| queue | 1,000 | 2,000 at 2-day retention | 3,000 | about 540,000 |
| lock | about 0 (one row per resource) | 5 | 3,600 | about 650,000 |

The tuple rows are not the pressure. A take-enabled tuple writes one claim-log
row per transition (claim, ack, nack, renew, expire, dead), and the log keeps
them for 180 days (TemplateRegistry.java:66-67).

**What fails first, and the guard for each.**

1. **The park cap.** Board readers parked with `rd` would take 15 of the 16 slots
   at five sessions, and the 17th call gets HTTP 429 `ParkCapExceeded`. Nothing
   reports parked or refused calls today. Guards: one `wait` per watcher covers
   its mailboxes and board topics, so slots grow with processes, not topics
   (Waiting, above). The engine also reports park slots in use and refused calls,
   with a doctor row that warns above 75% of the cap (new).
2. **A runaway writer.** Nothing limits the rows in a subspace. Guard: a
   per-template `max_live_rows` that refuses `out` with a typed error (new): 500
   live posts per board topic, 10,000 live tasks per queue. The board skill also
   asks each session to post at most once a minute per topic (convention).
3. **A stalled queue.** Guard: a doctor row over the census `available` and
   `dead` counts for queue subspaces, warning above 1,000 available or at any
   dead task (a new row over existing fields).
4. **Acked tasks.** An acked task keeps its row, with a NULL body, until its
   retention ends (TupleRepository.java:925-929). Guard: queue retention of two
   days, not seven.
5. **The lock expiry cliff.** A lock tuple expires at its creation time plus
   retention, a refire cannot move that ceiling (TupleRepository.java:420-426),
   `in` never sees an expired row (lines 789 and 802), and a lease is clamped to the tuple's
   expiry. Without a change, a lock held across the boundary loses its lease there
   and cannot be renewed, and an idle expired lock stays unobtainable until the
   sweep deletes it, up to six hours. Guard: the lock flag (Approach item 5),
   read at `writeOut`, `claimOnce` and `renew` (Technical Design).
6. **Claim-log volume.** Guard: a template may set a shorter claim-log TTL than
   the engine's 180 days (new). The value for queues and locks is set at
   implementation.

**Known limits outside this RDR, since fixed.** The research found six defects
that affected the ledger on 2026-09-15. They were fixed on their own under bead
nexus-xapt8 and are on develop before this RDR's gate (commits 0242bc100,
c5d872faa, 42cb39693, 42982cc2f, bc4622c2f): a subspace-scan index and a
claim-log purge-by-expiry index; `tuple_list` rewritten as one `GROUP BY` query
with paging; the doctor's sweep-freshness row judging by the laggard tenant;
the per-claimant park counters released; and `default_lease_seconds` applied in
`claimOnce` (TupleRepository.java:754-755). The failure order above was framed
against the pre-fix engine; with the index and paging in place the census cost
no longer competes with the park cap, which stays first.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| board template | `tuples/templates/ledger.yaml` | Extend the pattern: take disabled, many readers, but `keys+nonce` identity and a body. |
| queue template | `tuples/templates/mailbox.yaml` | Reuse its claim semantics (lease, attempts, dead letter, reply-in-ack) with the work, not the worker, as the key. |
| lock template | none | New: `id_from: keys`, so `out` is idempotent and one tuple exists per resource. |
| `release` operation | `nack` and `releaseOrDeadLetter` in TupleRepository.java | Extend: a variant of the release core that does not count an attempt. |
| Template loading | `TemplateRegistry.RESOURCE_TEMPLATE_PATHS` | Extend: add three paths. |
| Client tuple tools | src/nexus/mcp/core.py, src/nexus/commands/tuple_cmd.py | Reuse for the templates; add `release`. |
| Board notifications | src/nexus/tuple_watch.py (mailbox only) | Extend with a read-only board mode on the new `wait` operation. |
| Health checks | src/nexus/health.py tuple rows | Extend: two rows, park-slot use and queue depth (Scale and Limits). |
| Machine-local locks | `scripts/lib/build-lease.sh` and `tests/e2e/lib/lock.sh`, both mkdir-based | Keep. They are out of scope; RDR-205 excluded the build lease from the tuple space by name. |

### Decision Rationale

Templates over tables, because RDR-205's reason for one registry still holds and
the engine already enforces exclusion, leases, retries, dead letters, and
wake-ups. One `release` operation over two special cases, because the lock and the
queue need the same thing: to end a claim without consuming the tuple and without
calling it a failure. An append-only board over a single replaceable record,
because `out` deliberately never changes a body (RDR-205's idempotency rule), and
a log gives every reader both the latest post and what it missed.

## Alternatives Considered

### Alternative 1: Release a lock with `ack` and recreate it with `out`

**Description**: the holder acks the lock tuple, and the next holder, or any
process, writes a fresh one.

**Pros**:

- No new engine operation.

**Cons**:

- The ack consumes the row, and a later `out` of the same identity only refreshes
  that consumed row's expiry. The lock stays unavailable until the sweep purges
  the row.
- Giving each lock generation a new identity (`keys+nonce`) would let two
  processes create two lock tuples at once, which breaks exclusion.

**Reason for rejection**: it either leaves the lock dead or lets two holders in.
Because the same dead lock is one ordinary `ack` away under the chosen design,
the lock flag also makes `ack` refuse on a lock claim (Technical Design).

### Alternative 2: Release a lock or hand back a task with `nack`

**Description**: use the existing operation that returns a claim.

**Pros**:

- No new engine operation.

**Cons**:

- Every `nack` counts an attempt, so healthy releases dead-letter the lock after
  `max_attempts`, and hand-backs dead-letter tasks that never failed.

**Reason for rejection**: it turns normal use into failure.

### Alternative 3: A board as one record per topic, replaced in place

**Description**: `id_from: keys`, where each post overwrites the last.

**Pros**:

- Readers read one row.

**Cons**:

- `out` never updates a body on an identity conflict, by RDR-205's design, so the
  engine would need a new replace operation.
- Readers who were offline lose every post but the last.

**Reason for rejection**: it needs an engine change that weakens `out`'s
idempotency, and it loses history.

### Briefly Rejected

- **Separate tables or services per shape**: rejected by RDR-205's Alternative 1,
  and nothing has changed.
- **Fan-out with SendMessage or mailboxes**: one recipient per call, no record
  for later readers, and the sender must know every reader in advance.
- **Semantic matching for queue tasks**: RDR-205 deferred semantic take until a
  consumer asks. Exact keys suffice for this RDR.
- **Filesystem locks for cross-session work**: machine-local and invisible to
  other machines.

## Trade-offs

### Consequences

- Positive: three coordination shapes with no new tables, reusing leases, retries,
  dead letters, wake-ups, visibility, and the health rows.
- Positive: `release` makes the claim machinery complete. Every way to end a
  claim now exists: consume (`ack`), fail (`nack`), extend (`renew`), and give
  back (`release`).
- Negative: an engine release is needed for each template, because the registry
  loads by explicit path.
- Negative: the board has no single-writer guarantee. Any session of the tenant
  can post.

### Risks and Mitigations

- **Risk**: a lock is advisory. A holder that stops renewing loses the lock when
  its lease ends, even if it is still working.
  **Mitigation**: the holder renews at half the lease, as the mailbox skill
  already requires for long claims. A resource that can check a token treats the
  claim id as a fencing token (a value that proves the current holder), and
  refuses work from an older claim.
- **Risk**: without the lock flag, a lock expires at its creation time plus
  retention even while it is held, and an expired lock stays dead until the sweep
  deletes it (Scale and Limits, item 5).
  **Mitigation**: the lock flag (Approach item 5), with a test that holds a lock
  across the retention boundary.
- **Risk**: a holder, or a tool acting for one, calls `ack` on a lock claim out
  of habit, and a consumed lock row is unobtainable until the sweep purges it.
  **Mitigation**: the lock flag refuses `ack` on a lock claim with
  `SchemaViolation` naming `release` (Technical Design), with a test that the
  claim stays live and can still be released.
- **Risk**: parked calls reach the engine-wide cap of 16.
  **Mitigation**: one `wait` per watcher instead of one parked call per topic,
  and the park report and doctor row
  show the cap filling before callers see 429.
- **Risk**: a crashed holder counts as a failed attempt, so repeated crashes
  could dead-letter a lock.
  **Mitigation**: the lock template omits `max_attempts`, which the engine treats
  as unbounded, so a lock never dead-letters. Every lapse stays in the claim log.
- **Risk**: board volume grows with posts.
  **Mitigation**: retention and `max_live_rows` bound it, and the sweep purges
  expired posts.

### Failure Modes

- A lock holder crashes: its lease ends, the next `in` reclaims the lock, and the
  claim log records the expiry. Visible in `nx tuple stats lock/<resource>`.
- A queue task keeps failing: after three attempts it becomes a dead letter,
  counted by `nx tuple stats` and readable by `rd`.
- A worker hands a task back: `release` returns it without a failure, and the
  next waiting worker wakes.
- A board reader is offline longer than the retention: it misses posts older than
  the retention. Silent to the reader, so the retention is part of the board's
  contract: 7 days, unless a post sets less.
- An engine without `release` receives the call: the client gets an HTTP error,
  not a silent no-op, and the wire-contract ledger pairs the release.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions verified (the three spikes ran 2026-09-16;
  the lock attempts question is answered by the engine as built).
- [x] Sam decides the Open Questions below (all five answered by 2026-09-16).

### Minimum Viable Validation

One end-to-end run against a real engine, with two sessions and one script:

1. The script posts to a board. Both sessions, each running `nx tuple watch`
   in board mode over its mailboxes and the topic with one `wait`, ping once,
   and both read the same post with `rd`.
2. The script puts two tasks on a queue. Each session takes a different one. One
   session releases its task, and the task's attempt count stays at zero. The
   other session takes and acks it.
3. Both sessions contend for one lock. Exactly one holds it. The holder releases,
   and the other takes it at once. The second holder is killed without releasing,
   the lease lapses, and the first takes the lock again.

### Phase 1: Code Implementation

#### Step 1: The `release` operation in the engine

Add `release` to TupleRepository beside `nack` and `renew`, the HTTP endpoint,
the claim-log transition, and the after-commit signal. Tests: release does not
count an attempt; release of a lapsed claim fails; release wakes a parked `in`.

In the same step, the other engine changes: the multiplexed `wait`, the lock
flag, the per-template `max_live_rows` refusal, the per-template claim-log TTL,
and a report of park slots in use and refused calls. Tests: one `wait` on three
subspaces wakes on a write to any of them, takes one park slot, and loses no
write that lands between its query and its park; a lock held and renewed across
its retention boundary stays held; an `out` on an expired lock makes it
available; `ack` and `ack` with a reply on a lock claim are refused with
`SchemaViolation` and the claim stays live; `nack` on a lock returns it; `out`
past `max_live_rows` is refused with the typed error; the park report counts a
429.

#### Step 2: The three templates

Add the YAML files and the registry paths. Tests: each template loads, rejects a
missing required dimension, and behaves as designed (board never takeable, queue
dead-letters at three failures, lock `out` idempotent).

#### Step 3: Clients

`HttpTupleStore.release`, the `tuple_release` MCP tool, the `nx tuple release`
CLI verb, the board mode of `nx tuple watch`, and two doctor rows (park-slot use
and queue depth), each with tests.

#### Step 4: Documentation

Update web/tuple-space.html's uses table and web/coordination.html, which list
these shapes as not built, and docs/tuple-space.md.

### Phase 2: Operational Activation

#### Activation Step 1: Paired release

Cut the engine first, record the wire change in the wire-contract ledger, and
ship the client release paired with it, following the paired-release
choreography in AGENTS.md.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| board, queue, and lock subspaces | In scope (`nx tuple list --prefix`) | In scope (`nx tuple stats`) | N/A: retention and the sweep | In scope (the existing `nx doctor` tuple rows) | N/A: coordination state, not archived |

### New Dependencies

None.

## Test Plan

- **Scenario**: two readers wait on a board, one post arrives. **Verify**: both
  wake and read it; the post is still readable afterwards.
- **Scenario**: two workers, one task. **Verify**: exactly one claims it.
- **Scenario**: a worker releases a task. **Verify**: attempts unchanged, the task
  is available, a waiting worker wakes.
- **Scenario**: a task is nacked three times. **Verify**: it is a dead letter.
- **Scenario**: two processes run `out` on the same lock at once. **Verify**: one
  lock tuple exists.
- **Scenario**: a lock holder releases. **Verify**: the tuple is not consumed and
  the next `in` gets it.
- **Scenario**: a lock holder calls `ack`. **Verify**: `SchemaViolationException`,
  the claim is still live, the holder can still `release`.
- **Scenario**: the watcher waits on two mailboxes and two board topics with one
  `wait`. **Verify**: a post on either topic pings once with that topic's
  cursor advanced, a mailbox write wakes the same call, and the engine's park
  report shows one slot for the watcher.
- **Scenario**: a lock holder's lease lapses. **Verify**: the next `in` reclaims
  the lock, and the lock is not dead-lettered under the chosen attempts rule.
- **Scenario**: `release` on a lapsed claim. **Verify**: `ClaimNotFoundException`.
- **Scenario**: a new client calls `release` on an old engine. **Verify**: a clear
  error, never a silent success.

## Validation

### Testing Strategy

1. **Scenario**: the Minimum Viable Validation above, against a real engine.
   **Expected**: all three steps pass with the stated counts.
2. **Scenario**: the engine suite for `release` and the three templates.
   **Expected**: every Test Plan scenario passes.

### Performance Expectations

The volumes, the failure order, and the guards are in Scale and Limits. Each
retention and cap there states the load it assumes, so a change in load reopens
the number.

## Open Questions (for Sam)

1. **Lock attempts.** Answered by the engine as built: the lock template omits
   `max_attempts`, so it never dead-letters. No new rule is needed.
2. **Board retention.** Answered (Sam, 2026-09-16): a 7-day ceiling from the
   template, and a post may set a shorter `ttl_seconds`. There is no per-topic
   engine configuration. A reader away longer than seven days misses the posts
   that expired (Failure Modes).
   Keeping a board's history beyond retention is a curation question for the
   separate lifecycle-events work, not for this RDR.
3. **Lock retention.** Answered by the lock flag: retention bounds only an idle
   lock, so 7 days is enough.
4. **Board writers.** Answered (Sam): any session of the tenant can post, and the
   `from` dimension records the author. Nothing below the tenant has an identity
   the engine could check, because row-level security is tenant-only and sender
   fields are caller-supplied. An owner check could not be enforced.
5. **Park cap.** Answered (Sam): 16 stays. The multiplexed `wait` spends one slot
   per waiting process instead of one per topic (Waiting).

## Finalization Gate

> Complete each item with a written response before marking this RDR as
> **Accepted**.

### Contradiction Check

To be completed at the gate. At draft time: no contradictions found between the
research findings and the proposed design.

### Assumption Verification

Every assumption is verified: three by spike on 2026-09-16, the rest by source search.
Both must be verified before implementation begins.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `TupleRepository.in`, `ack`, `nack`, `renew`, `out` | engine | Source Search |
| `TupleRepository.release` | engine | To be built; Spike (`nexus_rdr/211-spike-1-2026-09-16`) |
| `TupleRepository.wait`, `TupleWaitRegistry.registerMulti` | engine | To be built; Spike (`nexus_rdr/211-spike-3-2026-09-16`) |
| `consumeClaim` refusal on a lock claim | engine | To be built; Source Search (`nexus_rdr/211-research-2`) |

### Scope Verification

The Minimum Viable Validation is in scope and runs before the RDR closes.

### Cross-Cutting Concerns

- **Versioning**: engine and client paired through the wire-contract ledger.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: local and cloud engines both load the templates from the
  release. The cloud needs a deploy.
- **IDE compatibility**: N/A.
- **Incremental adoption**: the templates are opt-in. Nothing changes for
  existing consumers.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: N/A.

### Proportionality

Three templates, two operations (`release`, `wait`), the lock flag at its four
sites, two per-template guards and the park-slot report, all reusing existing
machinery. The
document is sized to those engine changes and the three template decisions;
Phase 1 Step 1 enumerates every one with its test.

## References

- docs/rdr/rdr-205-linda-tuple-space-over-postgres.md (lines 31-32, 145-147, 414-420, 1006-1032)
- docs/rdr/rdr-206-tuple-claim-renew-and-reply-in-ack.md (line 72)
- docs/rdr/rdr-208-session-id-mail-addressing.md (line 519)
- docs/rdr/rdr-110-semantic-tuple-space.md (lines 111-112, 814-834)
- docs/exploration/linda-in-nexus.md; docs/tuple-space.md (line 9)
- web/tuple-space.html, uses table (lines 413-423); web/coordination.html
- service/src/main/resources/tuples/templates/{directory,ledger,mailbox}.yaml
- service/src/main/java/dev/nexus/service/db/TupleRepository.java, TupleLimits.java,
  and the TemplateSchema and TemplateRegistry classes
- service/src/main/resources/db/changelog/tuples-001-baseline.xml (lines 101-105)
- src/nexus/mcp/core.py, src/nexus/commands/tuple_cmd.py, src/nexus/tuple_watch.py,
  src/nexus/health.py

## Revision History

- 2026-09-15: created (draft), from Sam's request and a read-only research pass.
- 2026-09-15: Scale and Limits added from the scalability research (T2
  nexus_rdr/211-research-scalability-2026-09-15): the lock flag against the
  expiry cliff, board readers never park, `max_live_rows`, queue retention of two
  days, park and queue doctor rows, per-template claim-log TTL. Open Questions 1
  and 3 answered, 5 added. Scope kept narrow per Sam; lifecycle events are
  separate work (T2 nexus_rdr/211-decision-scope-2026-09-15).
- 2026-09-15: Sam answered Open Questions 4 (any session posts, the author is
  recorded, an owner check cannot be enforced) and 5 (the cap of 16 stays;
  multiplex instead). Added the multiplexed `wait`, one parked call per process
  over several subspaces, which the watcher uses in place of per-address probes.
  The six defects outside this RDR are bead nexus-xapt8.
- 2026-09-16: The three spikes ran as JUnit tests against the engine (worktree
  branch `rdr-211-spikes`, T2 `nexus_rdr/211-spike-1..3-2026-09-16`). All three
  assumptions hold. Spike 2 adds a design fact: the lock flag is read at three
  sites (`writeOut`, `claimOnce`, `renew`), recorded in Technical Design. The
  claim-log transition column has no check constraint, so `release` needs no
  changeset. Prerequisite 1 ticked; Open Question 2 still waits on Sam.
- 2026-09-16: Sam answered Open Question 2: board retention is a 7-day ceiling
  from the template, a post may set a shorter `ttl_seconds`, nothing per topic
  (T2 `nexus_rdr/211-decision-oq2-board-retention-2026-09-16`). Both
  prerequisites ticked; the RDR goes to gate.
- 2026-09-16: Gate round 1 — BLOCKED (1 Critical, 4 Significant, 1 ship-blocker(s)); commit `83d053f90`; critique `nexus_rdr/211-gate-critique-2026-09-16`.
- 2026-09-16: Gate round 1 fix (research `nexus_rdr/211-research-1`): `ack` on a lock claim is refused by the lock flag at `consumeClaim`, so the dead-lock failure Alternative 1 is rejected for cannot be reached by an ordinary call; the Known-limits list marked fixed under nexus-xapt8; engine line citations re-read from the working tree; Approach and Proportionality count both operations; the MVV and Test Plan exercise `wait` and the watcher's board mode.
- 2026-09-16: Fix-check follow-up (`nexus_rdr/211-fix-check-6c34f1913`, research
  `nexus_rdr/211-research-2`): the `consumeClaim` refusal sits inside the
  transaction after the row is read, not before the transaction opens; the
  refusal gets its own verified assumption and API row, `wait` gets an API row,
  Approach item 5 counts three effects, the guards are two per-template plus the
  engine-wide park report, and Risks names the ack-on-lock case.
