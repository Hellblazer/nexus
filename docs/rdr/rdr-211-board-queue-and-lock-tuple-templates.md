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

#### Gap 5: No push delivery to the session

Nothing in a Claude Code session sits in the engine's wait. Mail reaches a
session by two mechanisms that the session can see the seam between: a bash
watcher under the Monitor tool (`nx tuple watch`, T2
`nexus/plan-mailbox-monitor-push-delivery-2026-09-12`) that probes each mailbox
every few seconds with a zero-timeout `rd` and prints a ping line the session
must then act on, and a `UserPromptSubmit` hook (`mailbox_drain.py`) that
claims and renders at the next prompt. The watcher must be armed by the model
at every session start and re-armed at every 30-minute expiry, it can only
watch the one or two addresses it resolved at startup, and that plan itself names push
as "the structurally right transport" and the loop as "an interim" (its item
E). A board makes the gap wider: a session that follows ten topics has ten
things to watch, and nothing lets it change that set while it runs. What is
missing is one delivery path the session does not operate by hand: the
process that already serves the session's tuple tools waits on its behalf,
wakes it when something arrives, and lets it change what it is subscribed to.

## Relationship to Prior RDRs

The scan covered every RDR whose title names the tuple space, Linda, the
mailbox, the ledger, or addressing, plus RDR-110, the scrapped predecessor.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-205 (Linda tuple space over Postgres) | Origin | Built the tuple space and its template registry. Its Alternative 1 rejected separate queue, mailbox, and lock tables because "one primitive with a registry is less code than three tables by the second consumer" (rdr-205:1006-1018). That rationale still holds, and it is why this RDR adds templates. Its scope gate requires this RDR. |
| RDR-206 (claim renew and reply-in-ack) | Precedent | Added two engine operations to the same claim machinery, closing items RDR-205 left "not scheduled" (rdr-206:72). This RDR adds two more, `release` to the claim machinery the same way, and `wait` beside `rd`. The queue uses reply-in-ack to answer the producer, and both the queue and the lock use `renew` for long work. |
| RDR-208 (session-id mail addressing) | Adjacent, shipped | Made the session id the address and added the directory. Nothing in RDR-208 is deferred to this RDR (rdr-208:519). Board authors, queue workers, and lock holders identify themselves by session id or agent id. |
| RDR-184 (orchestration protocol hardening) | Precedent | Its dispatch ledger records agent starts and reports in a local file. The tuple ledger template, built by RDR-205, records the same events in the tuple space and is the shipped example of a take-disabled, one-writer, many-reader subspace, which the board follows. Its Gap 3 (collisions between overlapping runs on one resource) is the class the lock serves across machines; the machine-local mkdir locks it chose over flock stay as they are. |
| Mailbox push delivery via the Monitor tool (plan T2 `nexus/plan-mailbox-monitor-push-delivery-2026-09-12`, epic nexus-6konb; not an RDR) | Superseded in part | Built the interim: a Monitor-driven `nx tuple watch` that pings, and a `UserPromptSubmit` drain hook that claims. Its item E named a push transport as the right end state. This RDR's Gap 5 replaces the watcher with the channel delivery below; the drain hook stays as the floor. Its open beads (.15, .16, .21) are dispositioned by Sam when this RDR ships. |
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
| Engine wake-up (TupleRepository.java) | Yes | `out` and `ackWithReply` call `signalAll(tenant, subspace)` after commit, waking every reader parked on that subspace (lines 440 and 1009). |
| Client tuple tools (core.py, tuple_cmd.py) | Yes | Subspace-agnostic; new templates need no client change. A new engine operation that a session calls needs a new tool and CLI verb; `wait` is called only by the session's own MCP server and gets neither (Open Question 6). |

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
- [ ] A `notifications/claude/channel` notification sent by the nexus MCP
  server wakes a fully idle Claude Code session into a turn, and one sent
  mid-turn is delivered at the next turn. **Status**: Documented (Claude Code
  channels reference, "Events queue into the session and are processed in
  order"; T2 `nexus_rdr/211-research-3`). **Method**: Spike, Phase 1 Step 0,
  three observations on a real idle session; if it fails, the floor (the
  drain hook) stays the only delivery and Step 3 reduces to the subscription
  tools and the doctor row.
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
engine-wide park-slot report, and one client delivery path: the session's MCP
server waits on the session's subscriptions and pushes what arrives through a
Claude Code channel. The templates reuse the claim machinery the engine
already has. `release` closes Gap 4, which the queue and the lock share; the
delivery path closes Gap 5.

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
   template gets a flag with three effects. `ack` on a lock claim is refused
   (item 3). A claim or renew moves the lock tuple's
   expiry to now plus retention. An `out` that meets an expired lock row resets
   it to available instead of leaving it dead. Retention then bounds only an idle
   lock (Scale and Limits).
6. **One parked call per process.** A new engine operation, `wait`, parks one
   call on several subspaces and returns when any of them has something new. A
   session's MCP server waits on its mailboxes and board topics in one call, so
   the engine's park slots grow with sessions, not with topics (Technical
   Design, Waiting).
7. **Delivery through a channel.** The nexus MCP server, which Claude Code
   already spawns per session over stdio, declares the `claude/channel`
   capability and runs one background waiter over the session's subscriptions.
   When a tuple arrives it pushes a `notifications/claude/channel` notification,
   which wakes an idle session into a turn and queues behind a busy one. Mail
   is claimed at delivery on the session's own claimant identity and handed
   over with its claim id, so the session acks or nacks what it was given; a
   board post is handed over without a claim. Delivery is pure back pressure:
   one mailbox message is outstanding at a time, and the session's ack or
   nack is the credit for the next; the server holds that one claim, renews
   it, and re-sends its notification until the credit arrives, so a dropped
   notification costs nothing, and after a bounded number of re-sends it
   releases the message to the floor. The Monitor-driven watcher and its
   arming are deleted; the `UserPromptSubmit` drain hook stays as the floor
   (Technical Design, Delivery).
8. **Subscriptions.** What the waiter covers is a per-session list the session
   can change while it runs: its own mailboxes by default, board topics added
   and removed by three MCP tools (`tuple_subscribe`, `tuple_unsubscribe`,
   `tuple_subscriptions`), persisted in T1 scratch so a `/resume` comes back
   subscribed. A change re-issues the one `wait` call; it costs no park slot
   (Technical Design, Subscriptions).

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
tuple past its cursor. The session's MCP server uses it to wait on the
session's mailboxes and on every board topic it follows, all in one call,
instead of probing each address every three seconds. `wait` parks with no
claimant, as `rd` does (TupleRepository.java:597), so it takes one global slot
and nothing against the per-claimant cap of four (line 84); a session's `in`
parkers on queues and locks are its only per-claimant count. Queue workers and
would-be lock holders still park with `in`, one slot each. Each parked call waits at most 25 seconds, so a longer wait is a
loop. `release` and `out` both signal waiters, so a released lock, a new task, or
a new post wakes the waiting processes at once. `wait` is an engine-plus-client
change and gets its own wire-contract ledger entry.

**Delivery.** The nexus MCP server (`nx-mcp`, stdio, one per session) declares
`capabilities.experimental['claude/channel'] = {}` at initialize, which is what
makes Claude Code register a listener, and runs one asyncio waiter in its
lifespan beside the T1 lease refresher. The waiter loops on `wait` over the
subscriptions with per-subspace cursors. The declaration itself goes through
the low-level server's initialization options
(`create_initialization_options(experimental_capabilities={"claude/channel":
{}})`), since FastMCP exposes no experimental capabilities of its own.

Mail is delivered under pure back pressure (Sam, 2026-09-16, decision record
item 6): the waiter holds at most one live mailbox claim per session, and the
session's `tuple_ack` or `tuple_nack` for that claim is the credit that lets
it claim the next. Nothing is claimed ahead of demand, so a burst of mail
sits in the mailbox as available rows, ordered, and arrives one message per
credit; this is a coordination channel, not a stream, and the design
optimises the handling of one message for correctness over throughput.

The waiter claims only when the initialize handshake carried the channel.
`ServerSession.client_params` exposes the client's declared capabilities,
and a Claude Code launched without the flag leaves the experimental set
empty; what Claude Code declares there when the flag is present is not in
the channels reference, so Phase 1 Step 0 records the observed shape and the
guard is written against it. Without the channel the waiter claims nothing
and the floor delivers.

To claim, the waiter takes the oldest available row across the session's
mailboxes with `in` under a claimant that is stable per session,
`waiter:<session id>`, minted once at server start from the session id the
server already leases. It is not the drain hook's claimant, which is random
per call so that two concurrent drainers of one address never share one; a
session has one MCP server, so the waiter has no concurrent twin. Because
only one claim is ever live, the engine's same-claimant retake
(TupleRepository.java:777-793, `in` returns the caller's own live claim
before taking a new row) is exactly the restart path: a restarted server for
the same session calls `in` and receives its one outstanding claim, spending
no attempt, and continues.

The waiter then sends `notifications/claude/channel` as a raw
`JSONRPCNotification` on the session's write stream (the SDK's typed
notification union has no member for it) with the body as `content` and
`meta` carrying `subspace`, `from`, `kind`, `correlation_id`, `tuple_id`,
`claim_id` and `claimant`. The session acts and calls `tuple_ack` (with a
reply when the mail was a request) or `tuple_nack`, passing the claim id and
claimant back. Claude Code does not acknowledge notifications, so the waiter
does not rely on delivery: it claims with a 300 s lease, renews at 150 s (a
renew counts no attempt), and, because the session's ack and nack run in this
same process, it knows whether the credit for its claim id has arrived. At
each renew while unacked it re-sends the same notification, at most five
times. After the fifth it calls `release`, this RDR's Gap 4 operation, which
returns the row to available with no attempt spent, so the message reaches
the drain-hook floor at the session's next prompt and the waiter moves to
the next row; the doctor row counts the release. Redelivery is a
re-notification by the holder and spends no attempt. An attempt is spent in
two cases: the lease lapses before any retake, which is a server death with
no successor inside 300 s, after which the sweep's
`releaseLapsedClaimsBatch` (TupleRepository.java:1280) or the next `in`
reclaims the row; or the session nacks the message. Any three of those on
one message reach the mailbox template's cap of three, and that row is a
dead letter the drain hook surfaces once, as today. For each new board post the waiter sends the post as `content` with
`subspace`, `from`, `kind` and `tuple_id`, no claim, and advances that
topic's cursor; a dropped post notification is not re-sent, the post stays
readable by `rd` for its retention, and `tuple_subscriptions` shows the
cursor to re-read from. Notifications carry the body up to the tuple body
cap of 4096 bytes; a session busy in a turn receives everything that arrived,
together, at its next turn.

The channel is a Claude Code research preview: a session opts in per launch
(`--channels server:nexus`, and during the preview a custom server loads only
with `--dangerously-load-development-channels server:nexus`), the flag
syntax may change, and channels are not available on Amazon Bedrock, Google
Cloud Agent Platform or Microsoft Foundry. The channel is the only push path.
The Monitor-driven `nx tuple watch` loop, the SessionStart injection that
armed it and the 30-minute re-arm rule are deleted, not kept as a fallback:
two mechanisms for one delivery is the seam this gap names. What remains
under the channel is the floor: the `UserPromptSubmit` drain hook keeps
claiming and rendering anything unclaimed at the next prompt, so a session
launched without the channel still gets its mail, at its next prompt rather
than at once, and no path depends on the model arming anything. A new
`nx doctor` row reports three observable facts: the capability is declared
by the server, the waiter is alive with its last wake time and its count of
unacked claims, and the client's experimental capabilities as received in
the initialize handshake (`ServerSession.client_params`), which is what a
Claude Code launched without the flag leaves empty; the channels reference
does not document what Claude Code declares there, so that last fact is an
observation the row reports as seen, not a contract. Queues and locks
are not delivered: a worker or a would-be holder waits with `in`, which
already wakes on `out` and `release`.

**Subscriptions.** The waiter's list is per session. Defaults: `mailbox/<session
id>` and, once registered, `mailbox/<instance name>`. `tuple_subscribe(subspace)`
adds a board topic; it refuses a take-enabled template's subspace (a queue or
a lock) with `SchemaViolation` naming `in`, since those are never delivered.
It accepts exactly one mailbox, the session's own instance-name mailbox: the
instance name exists only in the harness (the `ListAgents` row), so the
session says it once, `tuple_subscribe("mailbox/<name>")`, and the server
does what `nx tuple watch --instance` did: writes the per-session
registration file the drain hook reads, starts the RDR-208 directory lease
(`directory/<name>`, 300 s, re-sent every 60 s from the lifespan) so the
name resolves, and adds the mailbox to the waiter. The SessionStart
injection that armed the Monitor becomes one line asking for that call; a
`/resume` under a new name repeats it, and the old name's mail strands, as
RDR-208 accepted. Any other mailbox is refused, since a session subscribing
to another session's mailbox would claim that session's mail. Beyond the two
mailboxes the list holds at most 32 board topics, refused past that with
`SchemaViolation`; spike 3 tested three, and the bound guards the engine's
per-call work, not slots. `tuple_unsubscribe(subspace)`
removes one, `tuple_subscriptions()` lists the set with each cursor. The list
lives in T1 scratch under the session id, so a `/resume` restores it and a
`/clear` starts clean. A change cancels the parked `wait` and re-issues it
with the new list, so it costs no slot. The mailbox skill's rule for following
a topic is one `tuple_subscribe` call, not a watcher argument.

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
   reports parked or refused calls today. Guards: one `wait` per session's MCP
   server covers its mailboxes and board topics, so slots grow with sessions,
   not topics, and `wait` is uncharged against the per-claimant cap of four
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
| Delivery to the session | src/nexus/mcp/core.py (the FastMCP server and its lifespan tasks) | Add the channel capability, the `wait` waiter, claim-at-delivery and the subscription tools here. |
| Mailbox watcher | src/nexus/tuple_watch.py, src/nexus/mailbox_arm.py, the `nx tuple watch` verb, the mailbox skill's arming rule | Delete, after moving two pieces into the MCP server: the per-session instance registration file (`write_instance_registration`) and the directory lease heartbeat (`_directory_heartbeat`). The drain hook (conexus/hooks/scripts/mailbox_drain.py) stays as the floor. |
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
  **Mitigation**: one `wait` per session's MCP server instead of one parked call
  per topic,
  and the park report and doctor row
  show the cap filling before callers see 429.
- **Risk**: a crashed holder counts as a failed attempt, so repeated crashes
  could dead-letter a lock.
  **Mitigation**: the lock template omits `max_attempts`, which the engine treats
  as unbounded, so a lock never dead-letters. Every lapse stays in the claim log.
- **Risk**: the channel is a research preview; its flag may change, and a
  session launched without it, or on a platform without it, gets no push.
  **Mitigation**: the drain hook is the floor (Delivery): mail still arrives at
  the next prompt, and the doctor row says whether push is live. The flag is
  documented as setup; nothing else changes with it.
- **Risk**: a channel notification is dropped; Claude Code does not acknowledge
  them.
  **Mitigation**: the waiter holds and renews the claim and re-sends the
  notification until the session's ack passes through it, spending no
  attempt; a lease lapsing before a retake, or the session's own nack, spends
  one, and any three of those on one message is the dead letter the drain
  hook surfaces once. A dropped board post is not re-sent; the post stays
  readable for its retention.
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
- An engine without `wait` receives the call: the waiter logs the 404 once
  and stops, the doctor row reports it, and the drain hook carries mail at the
  next prompt; the ledger pairs `wait` as it pairs `release`.
- A session without the channel: no notification is ever delivered; the drain
  hook carries mail at the next prompt, the doctor row shows an empty client
  capability set from the handshake, and `tuple_subscriptions` still answers.
- A notification is dropped: the waiter still holds the claim and re-sends it
  at its next renew, 150 s; nothing in the claim log changes. If the server
  itself dies and no successor for the session starts within the 300 s lease,
  the lease lapses, the sweep or the next server's `in` reclaims the row, and
  the claim log shows one expiry; a successor inside the lease retakes the
  claim with no expiry logged.
- The session never acks (it is busy for longer than five renews, or the
  channel is loaded but nothing handles the event): the waiter releases the
  message after the fifth re-send, the floor delivers it at the next prompt,
  and the doctor row counts one release.

## Implementation Plan

### Prerequisites

- [x] All engine-side Critical Assumptions verified (the three spikes ran
  2026-09-16; the lock attempts question is answered by the engine as built).
- [ ] The channel assumption spiked (Phase 1 Step 0).
- [x] Sam decides the Open Questions below (all seven answered by 2026-09-16).

### Minimum Viable Validation

One end-to-end run against a real engine, with two sessions and one script:

1. The script posts to a board. Both sessions, subscribed to the topic and
   launched with the channel, wake with the post in context, delivered by
   their own MCP servers' single `wait` each; neither ran a watcher.
2. The script puts two tasks on a queue. Each session takes a different one. One
   session releases its task, and the task's attempt count stays at zero. The
   other session takes and acks it.
3. Both sessions contend for one lock. Exactly one holds it. The holder releases,
   and the other takes it at once. The second holder is killed without releasing,
   the lease lapses, and the first takes the lock again.
4. The script sends a request to one session's mailbox. The session wakes with
   the body and a claim id in context, answers with `tuple_ack` and a reply, and
   the script's parked `rd` on its own mailbox wakes with the reply. The
   notification for a second request is suppressed in the test; the session
   still receives it after the re-send bound, with the same claim id, and the
   claim log shows no expiry.

### Phase 1: Code Implementation

#### Step 0: The channel spike

Before any delivery code: a throwaway nexus MCP build declares the channel
capability and sends one notification from its lifespan to a real, fully idle
Claude Code session launched with the development-channel flag. Three
observations that the session wakes into a turn, one that a notification sent
mid-turn arrives at the next turn, and one measurement of wake latency. A
failure leaves the drain hook as the only delivery, reduces Step 3 to the
subscription tools and the doctor row, and keeps the watcher deletion (the
watcher never wakes an idle session reliably either; it depends on the model
arming it); the RDR records the result either way.

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
past `max_live_rows` is refused with the typed error; a template with a shorter
claim-log TTL has its log rows purged before the engine default's; the park
report counts a 429.

#### Step 2: The three templates

Add the YAML files and the registry paths. Tests: each template loads, rejects a
missing required dimension, and behaves as designed (board never takeable, queue
dead-letters at three failures, lock `out` idempotent).

#### Step 3: Clients

`HttpTupleStore.release` and `HttpTupleStore.wait`, the `tuple_release` MCP
tool, the `nx tuple release` CLI verb, and two doctor rows (park-slot use and
queue depth), each with tests. Then delivery: the channel capability and the
lifespan waiter in the nexus MCP server, claim-at-delivery for mail and
cursor delivery for posts, the three subscription tools with their T1
persistence, the instance-name registration and directory lease behind
`tuple_subscribe` of the session's own instance mailbox, the one-line
SessionStart request for that call, the doctor row (capability declared by
the server; waiter alive, last wake, unacked and released counts; the
client's handshake capabilities), and
the deletion of `nx tuple watch`, `tuple_watch.py`, `mailbox_arm.py`'s
SessionStart injection and the skill's arming rule. Tests: a notification per
new tuple with the documented shape, `claimant` included; a suppressed
notification is re-sent at the next renew with the same claim id and no expiry
in the claim log, and stops after five re-sends while the renew continues; a
restarted server for the same session retakes its live claims with no expiry
logged; a killed server with no successor inside the lease lapses and the next
server's `in` reclaims with one expiry logged; a session's `tuple_nack` on
delivered mail counts one attempt; `tuple_subscribe` on a queue, a lock or a
mailbox subspace is refused, and so is the thirty-third subscription; a subscription change re-issues `wait` and the old parked
call is gone from the park report; the list survives a resume and not a clear;
a second message is not claimed until the first is acked, nacked or
released; the sixth unacked renew releases the message and the floor
delivers it; a server whose handshake carried no client capability claims
nothing; `tuple_subscribe` of the session's own instance mailbox writes the
registration file, sends the directory lease, and is refused for any other
name; the doctor row reads not-declared for a server started without the
capability and shows the three facts otherwise; the SessionStart hook emits
the one-line subscribe request and no arm text.

#### Step 4: Documentation

Update web/tuple-space.html's uses table and web/coordination.html, which list
these shapes as not built, docs/tuple-space.md, docs/cli-reference.md (the
`watch` verb removed), and the mailbox skill: the arming and re-arm rules are
replaced by the launch flag and `tuple_subscribe`.

### Phase 2: Operational Activation

#### Activation Step 1: Paired release

Cut the engine first, record the wire change in the wire-contract ledger, and
ship the client release paired with it, following the paired-release
choreography in AGENTS.md.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| board, queue, and lock subspaces | In scope (`nx tuple list --prefix`) | In scope (`nx tuple stats`) | N/A: retention and the sweep | In scope (the existing `nx doctor` tuple rows) | N/A: coordination state, not archived |
| a session's subscriptions | In scope (`tuple_subscriptions`) | In scope (each cursor) | In scope (`tuple_unsubscribe`; a `/clear` drops the list) | In scope (a new `nx doctor` row: capability declared; waiter alive, last wake, unacked and released counts; client handshake capabilities) | N/A: session state in T1 |

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
- **Scenario**: a session's MCP server waits on two mailboxes and two board
  topics with one `wait`. **Verify**: a post on either topic is delivered once
  with that topic's cursor advanced, a mailbox write wakes the same call, and
  the engine's park report shows one slot for the session.
- **Scenario**: mail arrives for an idle session launched with the channel.
  **Verify**: the session wakes with the body and claim id in context, acks,
  and the row is consumed; no watcher ran.
- **Scenario**: the notification is dropped (the transport write is suppressed
  in the test). **Verify**: the waiter re-sends it after the bound with the
  same claim id, and the claim log shows no expiry. Then the server is killed:
  the lease lapses, the next server reclaims with a new claim id, and the log
  shows one expiry.
- **Scenario**: the session subscribes to a topic while the waiter is parked.
  **Verify**: the next post on that topic is delivered, and the park report
  never shows two slots for the session.
- **Scenario**: the session is launched without the channel. **Verify**: the
  waiter claims nothing, the drain hook delivers the mail at the next prompt,
  and the doctor row shows an empty client capability set.
- **Scenario**: three messages arrive at once. **Verify**: one notification;
  the second is sent only after the first is acked; the third after the
  second.
- **Scenario**: a message is never acked. **Verify**: five re-sends 150 s
  apart, then a `release` transition in the claim log, no attempt spent, and
  the drain hook delivers it at the next prompt.
- **Scenario**: `tuple_subscribe("queue/builds")`. **Verify**:
  `SchemaViolationException` naming `in`; the subscription list is unchanged.
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
6. **Delivery.** Answered (Sam, 2026-09-16, T2
   `nexus_rdr/211-decision-channel-delivery-2026-09-16`): push through the
   Claude Code channel, the emitter in this RDR, the Monitor watcher retired to
   a fallback and then, on Sam's second ruling the same day, deleted outright,
   the drain hook alone as the floor; `wait` gets no standalone MCP tool or CLI
   verb; engine-side SSE or WebSocket is a later decision. The author's choice
   inside that ruling, open for Sam to reverse: mail is claimed at delivery and
   the claim is held and re-notified until the session acks, rather than
   pinged and claimed by the session. Sam's further ruling the same day
   (decision record item 6): delivery is pure back pressure, one message
   outstanding, the ack as the credit for the next; correctness of handling
   one message over throughput.
7. **Subscriptions.** Answered (Sam, 2026-09-16, the same decision record,
   item 4): the set is managed, not fixed at startup: three tools, per session,
   persisted in T1 across a resume, boards and mailboxes only.

## Finalization Gate

> Complete each item with a written response before marking this RDR as
> **Accepted**.

### Contradiction Check

To be completed at the gate. At draft time: no contradictions found between the
research findings and the proposed design.

### Assumption Verification

Every engine-side assumption is verified: three by spike on 2026-09-16, the
rest by source search. The one client-side assumption, that a channel
notification wakes an idle session, is documented and is spiked as Phase 1
Step 0, before any delivery code.

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
sites, two per-template guards and the park-slot report on the engine; on the
client, one delivery path (the channel and its waiter), three subscription
tools and the watcher's deletion, all reusing the MCP server the session
already runs.
The document is sized to those changes and the three template decisions; Phase
1 Steps 1 and 3 enumerate every one with its test.

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
- 2026-09-16: Gate round 2 — PASSED (0 Critical, 7 Significant, 0 ship-blocker(s)); commit `f94817adf`; critique `nexus_rdr/211-gate-critique-2026-09-16b`.
- 2026-09-16: Post-gate amendment on Sam's decision (T2
  `nexus_rdr/211-decision-channel-delivery-2026-09-16`, research
  `nexus_rdr/211-research-3`): Gap 5, push delivery through the Claude Code
  channel from the session's MCP server, with `wait` as that server's
  transport, claim-at-delivery for mail, managed subscriptions (three tools,
  T1-persisted), the Monitor watcher retired to a fallback and the drain hook
  kept as the floor; a channel assumption added with its spike as Phase 1 Step
  0; Approach items 7 and 8, Technical Design Delivery and Subscriptions,
  Risks, Failure Modes, MVV step 4, Steps 0, 3 and 4, Test Plan, Day 2 and
  Open Questions 6 and 7. Also the round-2 observations: item 5 names its
  third effect, the RDR-206 row counts two operations, the `signalAll`
  citation re-read (440, 1009), a claim-log TTL test named in Step 1, a
  failure mode for an engine without `wait`.
- 2026-09-16: Follow-up to fix check `nexus_rdr/211-fix-check-79dda06d8` and
  Sam's ruling to replace the Monitor watcher outright (decision record items
  4 and 5; research `nexus_rdr/211-research-4`): the waiter holds and renews
  the claim and re-notifies until the session's ack passes through it, so a
  dropped notification spends no attempt (the first draft's lapsed-lease
  redelivery would have dead-lettered after three drops); `tuple_subscribe`
  accepts boards and mailboxes only; the watcher, its arming injection and
  the `watch` verb are deleted rather than kept as a fallback; the
  infrastructure audit routes delivery to the MCP server; the capability
  declaration named at the low-level server; `wait` has no tool or verb; the
  claimant travels in the notification; the doctor row is in Step 3; the
  platforms named.
- 2026-09-16: Follow-up to fix check `nexus_rdr/211-fix-check-3fc5d76b3`
  (research `nexus_rdr/211-research-5`): the waiter's claimant is stable per
  session, so a same-session restart retakes its claims with no attempt; an
  attempt is spent on a lapse before retake or on the session's nack, not
  only on a server death; the lease (300 s), renew (150 s), re-send cadence
  and cap (5) named; the notification goes out as a raw JSON-RPC
  notification; the doctor row's three observable facts named, the client's
  handshake capabilities as an observation; subscriptions are board topics
  only beyond the session's own mailboxes, at most 32; the Open Question
  count and Gap 5's address count corrected.
- 2026-09-16: Follow-up to fix check `nexus_rdr/211-fix-check-c9ba5ebc3`
  (research `nexus_rdr/211-research-6`) and Sam's ruling that delivery is
  pure back pressure, one message outstanding and the ack as the credit for
  the next: the waiter claims only when the handshake carried the channel
  and releases a message after the fifth re-send, so the floor is never
  starved; one live claim makes the stable claimant's restart retake exact;
  `tuple_subscribe` of the session's own instance mailbox takes over the
  registration file and directory lease the watcher wrote; the doctor row's
  three facts propagated to Step 3, Day 2 and the Test Plan; the sweep named
  as a reclaimer; the bound is 32 board topics beyond the two mailboxes.
