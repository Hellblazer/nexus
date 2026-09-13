# Tuple Space Walkthroughs

> Status: design of record from RDR-205 (accepted) and RDR-206 (accepted), which adds `renew` and reply-in-ack. RDR-205's engine (Phase 1) and client surface (Phase 2 — `nx tuple`, the nine `tuple_*` MCP tools, the doctor rows) have both shipped: `/v1/tuples` on `engine-service-v0.1.114` (Phase 3, deployed to the managed cloud since 2026-09-11), and the client in conexus 7.41.0, which also bumps the pinned local-mode engine floor to the same tag. A local install on 7.41.0 or later has the route live; an install on an older release stays pinned below the floor and a local-mode call 404s until it upgrades. RDR-206's `renew` and reply-in-ack are implemented on both halves as of this writing but not yet in a tagged engine release or a client release; see `docs/wire-contract-pending.md`'s `## Unshipped` entry.

Scenario walkthroughs for the [Tuple Space reference](tuple-space.md). Each section follows one use of the space from the caller's side, drawn as a sequence between the processes involved.

## Ledger: dispatch start and report

Consumer one. Today the RDR-184 dispatch ledger is a tab-separated (TSV) file that three blocking hooks append to. The space does not replace that file; two new asynchronous hook entries project the same payload into `ledger/<session_id>`, and the orchestrator can wait on a report instead of hand-counting. The agent itself never touches the space. See [Operations](tuple-space.md#operations) and [Blocking reads](tuple-space.md#blocking-reads).

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator session
    participant H as Claude Code hooks
    participant T as TSV ledger (write-ahead)
    participant E as Engine /v1/tuples
    participant A as Sub-agent

    O->>H: Agent tool dispatch
    H->>T: PreToolUse EXPECT row (blocking)
    Note over H,T: No agent id exists yet, so no tuple
    H->>A: SubagentStart
    H->>T: stamp START row (blocking)
    H-->>E: async out ledger/[session] keys agent_id kind start dims agent_type
    Note over H,E: reads the lease file, POSTs with curl, never blocks the dispatch
    H->>A: additionalContext your claimant id and mailbox address
    par Agent works
        A->>A: implement, test, commit
    and Orchestrator waits
        O->>E: rd ledger/[session] keys agent_id kind report timeout_s=25
        E-->>O: no row yet, parks, wakes on commit or 1 s timer
    end
    A-->>H: SubagentStop
    H->>T: REPORT row (blocking)
    H-->>E: async out ledger/[session] keys agent_id kind report
    E-->>O: report tuple
    O->>E: rd ledger/[session] empty pattern, the census, whole subspace
    E-->>O: every start and report row for this session
```

Waiting for a report is a parked read on the ledger. The report tuple needs no cooperation from the agent: the stop hook writes it from the harness payload. `expectations_census`'s space-backed read (`conexus/hooks/scripts/expectations.sh`, RDR-205 Phase 4.1) compares the space against the TSV only inside the 90-day retention window: `SPACE_PRESENT`/`SPACE_AGE` when the subspace exists, `SPACE_NEVER_RAN` when it is absent and the session is younger than the retention window, `SPACE_OUTSIDE_WINDOW` when absent and older, `SPACE_BLINDSPOT` when the walk examined no ledger subspace at all (never read as every session being outside the window), and `SPACE_FALLBACK` with a named reason when the engine cannot be consulted (no `nx` on PATH, unreachable, unparseable output). These lines never change the census function's own exit code — they are additional report lines on top of the TSV verdict, not a new one.

## Mailbox: send, contend, drain

Consumer two. A message is `out` to the recipient's address; the recipient drains with `in` before composing any hand-back. Because `in` is a destructive read under a lease, a message is delivered to exactly one reader even when two are draining the same box. See [Templates and subspaces](tuple-space.md#templates-and-subspaces) and [A row's life](tuple-space.md#a-rows-life).

```mermaid
sequenceDiagram
    autonumber
    participant S as Sender (agent or orchestrator)
    participant E as Engine
    participant PG as Postgres nexus.tuples
    participant R1 as Reader 1 (claimant a)
    participant R2 as Reader 2 (claimant b)

    S->>E: out mailbox/[addr] keys to dims from kind nonce msg-17 body
    E->>PG: INSERT id = sha256(tenant, subspace, keys, from, nonce)
    E->>E: signal waiters on mailbox/[addr] after commit
    par Two readers race
        R1->>E: in mailbox/[addr] to claimant=a lease_s=60
        R2->>E: in mailbox/[addr] to claimant=b lease_s=60
    end
    E->>PG: SELECT the oldest available row, FOR NO KEY UPDATE SKIP LOCKED
    E->>PG: UPDATE claimed by a, lease 60 s, log claim
    E-->>R1: tuple and claim_id
    Note over E,R2: the locked row is skipped, not waited on
    E-->>R2: None, nothing else available
    R1->>R1: act on the message
    R1->>E: ack claim_id claimant=a
    E->>PG: consumed_at=now, consumed_by=a, INSERT claim_log ack
    S->>E: out ... nonce msg-17 (network retry)
    E->>PG: same id, refresh expires_at only, never past created_at + retention
    Note over S,PG: the resend is one tuple, already consumed, nothing is redelivered
```

Exactly-once delivery comes from the claim statement, not from the client. `SKIP LOCKED` means a contended row is skipped rather than waited on, and the transaction holds the claim and nothing else. A same-claimant retry of `in` after a lost response returns the existing claim id without a new update or log row.

## Crash, lease lapse, dead letter

A reader that takes a message and dies never acks. The lease bounds the damage: when it lapses the row is available again, the next claimant records the previous claim's `expire` and increments `attempts`. A reader that `nack`s counts the same way. At the template's `max_attempts` (3 for the mailbox) the row is dead-lettered: out of every claimant's view, still readable. See [Claims, leases, nack and dead letter](tuple-space.md#claims-leases-nack-and-dead-letter).

```mermaid
sequenceDiagram
    autonumber
    participant R1 as Reader 1
    participant E as Engine
    participant PG as nexus.tuples / claim_log
    participant R2 as Reader 2

    R1->>E: in mailbox/[addr] to claimant=a lease_s=60
    E->>PG: claim attempts 0, log claim
    E-->>R1: tuple and claim c1
    Note over R1: process crashes, no ack
    Note over PG: 60 s pass, lease_until < now, row is available again
    R2->>E: in mailbox/[addr] to claimant=b lease_s=60
    E->>PG: select finds row with lapsed lease
    E->>PG: log expire for c1, attempts = 1
    E->>PG: claim for b, log claim
    E-->>R2: tuple and claim c2
    R2->>E: nack c2 claimant=b
    E->>PG: release claim, attempts = 2, log nack
    R2->>E: in ... claimant=b
    E->>PG: claim c3, log claim
    Note over R2: crashes again, lease lapses
    R1->>E: in mailbox/[addr] to claimant=a
    E->>PG: lapsed lease found, attempts + 1 equals max_attempts (3)
    E->>PG: claim_state = dead, log dead, re-run select
    E-->>R1: None, nothing else available
    R1->>E: rd mailbox/[addr] empty pattern
    E-->>R1: the dead row, with its state
```

Every claim reaches a terminal transition: ack, nack, expire or dead. `lease_until` is clamped to the row's `expires_at`, so a claim cannot outlive its tuple. The re-run after a dead-letter is bounded by `NX_TUPLE_READ_MAX` passes; each pass either claims or dead-letters one row. The sweep does the same expire-and-count for lapsed claims nobody re-took.

## Cross-instance request and ack

A request from one instance to another is the same mailbox with an instance name as the address. Both instances on the box mint against one tenant, so `mailbox/conexus-58` is reachable from the nexus session and vice versa. The requester parks an `in` on its own mailbox for the ack. The peer answers with `ack(reply=...)` (RDR-206): the reply lands in the requester's mailbox and the request is consumed, in one transaction, so there is no longer a separate `out` call that a crash could land between. See [What it is not for](tuple-space.md#what-it-is-not-for) for the scope this stays inside.

```mermaid
sequenceDiagram
    autonumber
    participant N as nexus-a6 (requester)
    participant E as Engine (shared tenant)
    participant C as conexus-58 (peer instance)

    N->>E: out mailbox/conexus-58 kind request correlation_id r-41 nonce r-41
    E-->>N: tuple_id
    loop until an ack or the caller's own deadline
        N->>E: in mailbox/nexus-a6 to nexus-a6 claimant=nexus-a6 timeout_s=25
        E-->>N: None at the 25 s cap, probe result, loop
    end
    C->>E: in mailbox/conexus-58 to conexus-58 claimant=conexus-58 lease_s=300
    E-->>C: the request tuple, claim c9
    C->>C: deploy engine, re-gate
    C->>E: ack c9 reply: subspace mailbox/nexus-a6, keys {to nexus-a6}, dims {kind ack, correlation_id r-41}
    Note over E: c9 consumed and the reply written in the SAME transaction (RDR-206); the reply's nonce is the engine's own, hex(request tuple id), never the caller's
    E->>E: signal waiters on mailbox/nexus-a6
    E-->>N: the ack tuple, claim c10
    N->>E: ack c10
    Note over N,C: the unacked-request sweep is rd over every mailbox for kind request with no matching ack
```

A wait of minutes is a loop of parked calls, never one long park. Each call parks for at most 25 s because the public edge times out a response that has not started within 30 s; at the cap the engine returns the probe result and the client loops. The `correlation_id` dim is what pairs an ack with its request. Before RDR-206 the peer's ack was `out` followed by a separate `ack c9`; a crash between the two left the reply delivered and the request unconsumed, so it was redelivered and the requester could see a second reply carrying the same `correlation_id`. Writing the reply inside `ack`'s own transaction closes that window: both commit or neither does.

`scripts/check_inbound_relay_acks.py`'s mailbox arm is that unacked-request sweep, addressed by `--mailbox-prefix`/`--tuple-read-max`: it scans every `mailbox/*` subspace for `kind=request` rows with no matching `kind=ack` row at `mailbox/<from>`, distinguished in its findings by a `MAILBOX-UNACKED-REQUEST:` tag, and reports into the same finding list as the older T2-memory ack check (the pre-tuple-space relay convention between the nexus and conexus repos). An empty mailbox tuple space is a legitimate clean state for this arm, not a blindspot; a request younger than `--max-age-days` is a legitimate in-flight handshake and is not reported even unacked.

## How a blocking read parks

The wake path is what makes `rd` and `in` with a timeout cheap. There is one engine JVM and every `out` passes through it, so a per-subspace condition variable is enough; `LISTEN`/`NOTIFY` is deferred until a second JVM exists. See [Blocking reads](tuple-space.md#blocking-reads).

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant H as Engine handler
    participant W as Waiter map (Condition per subspace)
    participant PG as Postgres
    participant O as Any out on the same subspace

    C->>H: rd / in ... timeout_s=25
    H->>W: register on subspace S before the query
    loop until match, timeout, or shutdown
        H->>PG: open short tx, run equality query, close tx (connection returned)
        alt row matched
            H-->>C: result
        else nothing yet
            H->>W: park outside any transaction, wake on signal or 1 s timer
            O->>PG: out ... on subspace S commits
            O->>W: signal all waiters on S (after the tx lambda returns)
            W-->>H: wake, re-run query
        end
    end
    Note over H,C: at the 25 s cap the probe result returns and the caller loops. Shutdown signals every waiter.
    Note over H,W: park caps 4 per claimant and 16 global, beyond them ParkCapExceeded and the probe result
```

A parked call never holds a pooled connection. Waking every waiter on a subspace is intended: each re-runs its own equality query and `SKIP LOCKED` keeps the fan-out cheap. Waiters on subspace B do not wake for commits on subspace A. The one-second timer is defence against a missed signal, not the primary wake.

## The sweep

Every six hours the existing sweep scheduler runs a second task. It enumerates tenants from `nexus.tuple_tenants`, least-recently-swept first, and per tenant releases lapsed claims nobody re-took, purges expired and consumed-past-retention rows, then purges claim-log rows past the log's own longer TTL, a few hundred rows per committed batch. Two bounds keep it from starving the T1 sweep that shares the scheduler: a statement timeout on every arm and a per-run budget of batches per tenant and wall clock. See [The sweep](tuple-space.md#the-sweep).

<svg viewBox="0 0 760 210" role="img" aria-label="The sweep visits tenants in last_swept_at order, stamps a tenant only when its sweep finishes, and a tenant cut short by the budget keeps its old stamp so it is first next run.">
  <defs><marker id="tuple-sweep-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker></defs>
  <g font-family="monospace" font-size="12" fill="currentColor" stroke="currentColor">
    <text x="20" y="28" font-weight="700" font-size="13" stroke="none">Run N (budget: 50 batches per tenant, 120 s wall clock)</text>
    <rect x="20" y="44" width="150" height="46" rx="4" fill="none"/>
    <text x="95" y="63" text-anchor="middle" stroke="none">tenant c</text>
    <text x="95" y="80" text-anchor="middle" stroke="none" font-size="11">last_swept_at NULL</text>
    <rect x="215" y="44" width="150" height="46" rx="4" fill="none"/>
    <text x="290" y="63" text-anchor="middle" stroke="none">tenant a</text>
    <text x="290" y="80" text-anchor="middle" stroke="none" font-size="11">swept 18 h ago</text>
    <rect x="410" y="44" width="150" height="46" rx="4" fill="none" stroke-dasharray="4 3"/>
    <text x="485" y="63" text-anchor="middle" stroke="none">tenant b</text>
    <text x="485" y="80" text-anchor="middle" stroke="none" font-size="11">swept 6 h ago</text>
    <line x1="170" y1="67" x2="213" y2="67" marker-end="url(#tuple-sweep-arrow)"/>
    <line x1="365" y1="67" x2="408" y2="67" marker-end="url(#tuple-sweep-arrow)"/>
    <text x="95" y="112" text-anchor="middle" stroke="none" font-size="11">release, purge, purge log</text>
    <text x="95" y="128" text-anchor="middle" stroke="none" font-size="11">stamped</text>
    <text x="290" y="112" text-anchor="middle" stroke="none" font-size="11">release, purge, purge log</text>
    <text x="290" y="128" text-anchor="middle" stroke="none" font-size="11">stamped</text>
    <text x="485" y="112" text-anchor="middle" stroke="none" font-size="11">budget exhausted mid-tenant</text>
    <text x="485" y="128" text-anchor="middle" stroke="none" font-size="11">stamp unchanged</text>
    <line x1="485" y1="140" x2="485" y2="168" marker-end="url(#tuple-sweep-arrow)"/>
    <text x="485" y="190" text-anchor="middle" stroke="none" font-size="12">first in run N+1: order lives in the table, no cursor in the JVM</text>
    <text x="640" y="63" stroke="none" font-size="11">logged: tenants visited,</text>
    <text x="640" y="79" stroke="none" font-size="11">oldest last_swept_at,</text>
    <text x="640" y="95" stroke="none" font-size="11">scanned, released, dead,</text>
    <text x="640" y="111" stroke="none" font-size="11">purged, budget exhausted?</text>
  </g>
</svg>

A run that finds nothing expired is the healthy state. The failure the counts detect is a run that scanned nothing or a run that did not happen; the doctor row on last-sweep age reports the second.

## Where JavaSpaces differs

JavaSpaces, the Jini-era Linda, is the leased and transactional form of this design, and a post-gate research pass compared the two. All six points of contact are now met: the pinned key set on take (JavaSpaces places no floor on a template's generality), Postgres as the fixed substrate (the spec permits transient spaces), one engine over HTTP (no multicast lookup, no RMI), the in-process park where JavaSpaces had leased remote listeners that leaked, a holder-renewable lease (`renew`, RDR-206), and take-and-reply in one transaction (`ack`'s optional `reply`, RDR-206). The comparison table lives in [Prior art](tuple-space.md#prior-art).

```mermaid
sequenceDiagram
    autonumber
    participant C as Consumer
    participant E as Engine
    participant Q as Requester's mailbox

    C->>E: in mailbox/[addr] claimant=c lease_s=900
    E-->>C: request, claim c1, lease_until = now + 900 s clamped to expires_at
    Note over C: work runs past 900 s
    C->>E: renew c1 lease_s=900
    E-->>C: lease_until extended, clamped to expires_at; no attempt spent
    C->>E: ack c1 reply subspace=mailbox/[requester] keys {to [requester]} dims {kind ack, correlation_id r-41}
    Note over C,E: c1 consumed and the reply written in the SAME transaction; a crash before this call leaves the request still claimed, to be re-delivered at lease lapse -- there is no longer a window between a written reply and its ack
    E-->>Q: reply visible only once the ack has committed
```

What each operation buys, now that both are closed: `renew` lets a consumer keep every v1 lease short and still finish long work, holder-renewable exactly as Jini's leases are, without the renewal *traffic* Jini's own retrospective named as a failure cause — a holder renews only when it is still working, never on a fixed schedule. `ack`'s optional `reply` removes the second call the JavaSpaces comparison flagged: the reply and the request's consumption commit together or not at all, so the crash-between-two-calls window this design used to accept is gone. The window between `in` and the work that follows it is still inherent to any leased take, JavaSpaces included; `renew` narrows how much of that window a lapse can silently swallow, but neither operation removes the window itself.
