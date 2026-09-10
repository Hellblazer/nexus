# Tuple Space

> Status: design of record from RDR-205 (accepted). Phase 1 (engine) and Phase 2 (client — `nx tuple`, the eight `tuple_*` MCP tools, the doctor rows) are both on `develop`. **No released `engine-service` tag serves `/v1/tuples` as of 2026-09-10** — the route ships with the next engine cut. Until that cut is released and pinned, every client tool this design ships is inert against every currently released engine: a call 404s.

## What it is

A tuple space is a shared bag of typed records that any process can add to, read from, or take out of, where taking is atomic: when two processes try to take the same record, exactly one gets it. Linda (Gelernter, 1985) named four operations, `out` (add), `read`, `in` (take) and `eval`; this design ships the non-blocking probe forms as well. A work queue, a mailbox, a lock and a request-reply channel are the same three operations over different record shapes. The [walkthroughs](tuple-space-walkthroughs.md) draw each of this design's uses as a sequence.

Two consumers ship with this design and no others: the RDR-184 dispatch ledger (start and report tuples keyed on the harness's per-instance agent id; today a tab-separated file, the TSV, that the hooks keep) and a mailbox addressed to an agent id or an instance name, which also carries the cross-instance request-and-ack between the nexus and conexus sessions on one box. Nothing else: not the build lease, not the T1 identity files, not the push vouching, not any wrapping of scratch, memory or plans, not surfaces. A consumer not named here needs its own RDR.

## Operations

Ten HTTP routes under `/v1/tuples`. Signatures are the contract; the handler is the implementation.

```text
out(subspace, keys, dims, body, *, nonce=None, ttl_seconds=None) -> tuple_id
                                                            # nonce required where id_from is keys+nonce;
                                                            # ttl_seconds defaults to the template's retention
rd (subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0) -> [Tuple]   # non-destructive; blocks up to timeout_s
rdp(subspace, keys_pattern=None, *, n=1, since=None) -> [Tuple]                # probe
in (subspace, keys_pattern, *, claimant, lease_s, timeout_s=0) -> (Tuple, claim_id) | None
inp(subspace, keys_pattern, *, claimant, lease_s) -> (Tuple, claim_id) | None
ack(claim_id, claimant) ; nack(claim_id, claimant)         # ownership checked; nack counts an attempt
subspace_list(prefix) -> [{subspace, total, available, claimed, dead, consumed, expired_unpurged,
                           oldest_created_at, newest_created_at}]    # concrete subspaces that exist;
                                                                     # the two timestamps span all rows,
                                                                     # expired included (the census needs
                                                                     # the newest write, not the newest live row);
                                                                     # total counts live rows only
registry() -> {digest, sources, templates: [TemplateSchema]}
subspace_stats(subspace) -> {total, available, claimed, dead, consumed, expired_unpurged}
                                                            # the exact-name form of subspace_list, kept
                                                            # for the CLI verb; total counts live rows only
```

| Operation | Parks | Idempotent | Probe form |
| --- | --- | --- | --- |
| `out` | no | yes: the id is derived from caller-supplied fields only, so a retry is the same tuple | none |
| `rd` | yes, up to `timeout_s` | yes: a read takes nothing | `rdp` |
| `rdp` | no | yes | is the probe form of `rd` |
| `in` | yes, up to `timeout_s` | a same-claimant retake within its lease returns the existing claim id, no new update or log row | `inp` |
| `inp` | no | same same-claimant rule as `in` | is the probe form of `in` |
| `ack` | no | no: a second `ack` on the same claim is `ClaimNotFound` | none |
| `nack` | no | no: every call counts an attempt against `max_attempts` | none |
| `registry` | no | yes | none |
| `subspace_list` | no | yes | none |
| `subspace_stats` | no | yes | none |

Matching differs by read. `in` and `inp` require every pinned key and match by equality, because exclusion needs an exact target. `rd` and `rdp` match by equality on every key the pattern supplies and place no condition on keys it omits; a pattern of `None` or `{}` reads the whole subspace, which is what a census does and what a claimant never may. `rd` and `rdp` return up to `n` live tuples, live meaning `expires_at > now()` and not acked, whatever the claim state: a row under a live claim and a dead-lettered row are both returned, with their state — but never their `claim_id`: that field is rendered only by `in`/`inp`'s own top-level response, the ack/nack credential, so reading a claimed row without having won the claim never leaks the means to ack or nack it. `n` is capped by the engine setting `NX_TUPLE_READ_MAX` (default 300, the client's paging convention); an `n` above the cap is clamped, not refused. Results are ordered by `(created_at, id)`, resuming strictly after `since`, a `(created_at, id)` cursor the caller keeps. Acked rows are never returned by any read.

## Templates and subspaces

A *subspace* is a named partition of the space with a registered schema (`mailbox/<agent_id>`, `ledger/<session_id>`). A *template* is the schema's name with its parameter; a concrete subspace is an instance of it. The engine is the only holder of the registry: no client carries a copy, every write is validated by the engine against its own templates, and a client learns what exists by asking. `registry()` returns a digest of the loaded templates beside them, so a client or a hook script that expects a shape can detect skew and report it rather than guess.

Two v1 templates, and only two:

| Template | Keys (take matches all) | Dims | Id from | Take | Retention |
| --- | --- | --- | --- | --- | --- |
| `ledger/<session_id>` | `agent_id`, `kind` ∈ {start, report} | `agent_type` | `keys` | disabled: rows are read, never claimed | 90 days |
| `mailbox/<address>` | `to` | `from` (required), `kind`, `correlation_id`, `address_kind` ∈ {agent, instance} | `keys+nonce`, with `from` in `id_dims` | enabled: `max_attempts` 3, `max_lease_seconds` 900 | 7 days |

The id is derived from caller-supplied fields only, so `out` is idempotent by construction and a retry across a deploy gap is the same tuple. Which fields is the template's `id_from`: `keys` (the ledger: `agent_id` and `kind` identify a dispatch, so a second start for the same agent is the same tuple, which is what the census wants); `keys+nonce` (the mailbox: the sender mints a message id, unique among its own messages, and passes it as the nonce; the template's `id_dims` name the dims that also enter the id, `from` for the mailbox, so a nonce need only be unique per sender and two senders' messages never collide; two messages to one address are two tuples and a resent message is one); or `keys+body` (content-addressed, no v1 template). The insert time is never part of an id. A refire with the same id refreshes `expires_at` only, never the body, the claim state or the consumed state, and never past `created_at` plus the template's `retention_seconds`.

Templates ship as YAML in engine resources, loaded and validated at boot; a breach fails boot with the file and field named. Templates change with an engine release, not with a data changeset; removing a template that has live rows needs a data changeset with a migration note. One test-only path exists beside the resource files: the engine also loads templates from the directory named by `NX_TUPLE_TEMPLATE_DIR` when it is set, logs that it did at boot, and lists both sources in `registry()`; a directory set in production is a red gate, not a silent second registry.

## A row's life

Every state a row in `nexus.tuples` can be in, and what moves it. The claim log records each transition as its own append-only row.

<svg viewBox="0 0 820 400" role="img" aria-label="A tuple row moves from available to claimed on in, back on nack or a lapsed lease, to consumed on ack, to dead at max_attempts, to expired when expires_at passes, and the sweep purges consumed and expired rows.">
  <defs>
    <marker id="tuple-life-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker>
  </defs>
  <g font-family="monospace" font-size="12" fill="currentColor" stroke="currentColor" stroke-width="1.2">
    <rect x="40" y="120" width="140" height="52" rx="6" fill="none"/>
    <text x="110" y="141" text-anchor="middle" stroke="none" font-weight="600">available</text>
    <text x="110" y="160" text-anchor="middle" stroke="none" font-size="10.5">claim_state NULL</text>

    <rect x="340" y="120" width="140" height="52" rx="6" fill="none"/>
    <text x="410" y="141" text-anchor="middle" stroke="none" font-weight="600">claimed</text>
    <text x="410" y="160" text-anchor="middle" stroke="none" font-size="10.5">lease_until set</text>

    <rect x="640" y="120" width="140" height="52" rx="6" fill="none"/>
    <text x="710" y="141" text-anchor="middle" stroke="none" font-weight="600">consumed</text>
    <text x="710" y="160" text-anchor="middle" stroke="none" font-size="10.5">consumed_at set</text>

    <rect x="340" y="270" width="140" height="52" rx="6" fill="none" stroke-dasharray="2 2"/>
    <text x="410" y="291" text-anchor="middle" stroke="none" font-weight="600">dead</text>
    <text x="410" y="310" text-anchor="middle" stroke="none" font-size="10.5">readable, never claimable</text>

    <rect x="40" y="270" width="140" height="52" rx="6" fill="none" stroke-dasharray="5 3"/>
    <text x="110" y="291" text-anchor="middle" stroke="none" font-weight="600">expired</text>
    <text x="110" y="310" text-anchor="middle" stroke="none" font-size="10.5">expires_at passed</text>

    <line x1="110" y1="60" x2="110" y2="118" marker-end="url(#tuple-life-arrow)"/>
    <text x="118" y="92" stroke="none">out</text>
    <path d="M40 132 C 8 132, 8 160, 40 160" fill="none" marker-end="url(#tuple-life-arrow)"/>
    <text x="4" y="112" stroke="none" font-size="10.5">out again:</text>
    <text x="4" y="125" stroke="none" font-size="10.5">refresh expires_at</text>

    <line x1="182" y1="134" x2="338" y2="134" marker-end="url(#tuple-life-arrow)"/>
    <text x="260" y="126" text-anchor="middle" stroke="none">in / inp</text>
    <line x1="338" y1="160" x2="182" y2="160" marker-end="url(#tuple-life-arrow)"/>
    <text x="260" y="182" text-anchor="middle" stroke="none">nack, or lease lapses</text>
    <text x="260" y="196" text-anchor="middle" stroke="none" font-size="10.5">attempts + 1</text>

    <line x1="482" y1="146" x2="638" y2="146" marker-end="url(#tuple-life-arrow)"/>
    <text x="560" y="138" text-anchor="middle" stroke="none">ack</text>

    <line x1="410" y1="174" x2="410" y2="268" marker-end="url(#tuple-life-arrow)"/>
    <text x="420" y="214" stroke="none">nack or lapse</text>
    <text x="420" y="228" stroke="none">at max_attempts</text>

    <line x1="110" y1="174" x2="110" y2="268" marker-end="url(#tuple-life-arrow)"/>
    <text x="118" y="224" stroke="none">expires_at passes</text>

    <line x1="338" y1="296" x2="182" y2="296" marker-end="url(#tuple-life-arrow)"/>
    <text x="260" y="288" text-anchor="middle" stroke="none">expires_at passes</text>

    <line x1="110" y1="324" x2="110" y2="372" marker-end="url(#tuple-life-arrow)"/>
    <text x="118" y="352" stroke="none">sweep purges</text>
    <line x1="710" y1="174" x2="710" y2="372" marker-end="url(#tuple-life-arrow)" stroke-dasharray="4 3"/>
    <text x="718" y="280" stroke="none">sweep purges</text>
    <text x="718" y="294" stroke="none" font-size="10.5">past retention</text>

    <text x="560" y="392" text-anchor="middle" stroke="none" font-size="10.5">every arrow out of claimed writes a claim_log row: claim, ack, nack, expire, dead</text>
  </g>
</svg>

The availability predicate is the row's whole story:

```text
consumed_at IS NULL AND expires_at > now() AND claim_state IS DISTINCT FROM 'dead' AND (claim_state IS NULL OR lease_until < now())
```

A lapsed lease is claimable the moment it lapses; an expired row is invisible the moment it expires; a dead-lettered row is never a candidate whatever its `lease_until` holds.

The claim statement, in one tenant-scoped transaction, nothing else in it:

```text
row = select(...).from(TUPLES)
        .where(TENANT.eq(t), SUBSPACE.eq(s), KEYS.eq(pattern),
               CONSUMED_AT.isNull(), EXPIRES_AT.gt(now()),
               CLAIM_STATE.isDistinctFrom("dead"),
               CLAIM_STATE.isNull().or(LEASE_UNTIL.lt(now())))
        .orderBy(CREATED_AT).limit(1)
        .forNoKeyUpdate().skipLocked().fetchOne();
// if row.leaseLapsed and row.attempts + 1 == maxAttempts: mark dead, log, re-run select
// (bounded re-run: NX_TUPLE_CLAIM_PASSES, default 8)
update(TUPLES).set(CLAIM_STATE, "claimed").set(CLAIMANT, c)
        .set(CLAIM_ID, id).set(LEASE_UNTIL, now + lease)
        .where(ID.eq(row.id)).execute();
insert claim_log(tuple_id, claim_id, c, 'claim', now)
```

`FOR NO KEY UPDATE` because the update touches no key column; `SKIP LOCKED` because a contended row is skipped, not waited on. The transaction holds the claim and nothing else.

## Claims, leases, nack and dead letter

A claim is the state between `in` and `ack`/`nack`, held under a lease, a deadline after which the claim is released by a sweep. `lease_until` is clamped to the row's `expires_at` at claim time, so a claim can never outlive its tuple. `attempts` counts nacks and lapsed leases alike; at the template's `max_attempts` the row is dead-lettered (`claim_state = 'dead'`, a `dead` log row), whether the cap was reached by nacks or by lapsed leases nobody re-took. A dead-lettered row leaves every claimant's view but stays readable by `rd`, and `subspace_stats` counts it under `dead`.

A same-claimant retake is idempotent: before the claim statement, `in` reads for a live claim already held by this claimant on a matching tuple — `claim_state = 'claimed'`, this claimant, unconsumed, unexpired, and `lease_until` still in the future — and, if one exists, returns its claim id with no new update and no log row. Without that read a retry after a lost response could never recover its claim. The `lease_until` check is load-bearing: a lapsed claim is not retaken by this shortcut even for its own former holder — once the lease passes, the same claimant falls through to the ordinary claim loop below like anyone else, and either reclaims the same row (if nobody else won it first) or a different one; only a claim whose lease has not yet lapsed is ever handed back as-is.

Every claim reaches a terminal transition: `ack`, `nack`, `expire` or `dead`. `ack` sets `consumed_at` and logs `ack`; `nack` releases the claim (`claim_state`, `claimant`, `claim_id` and `lease_until` to NULL) and increments `attempts`. When a claim finds a row whose previous lease has lapsed, the same transaction writes the `expire` row for the previous claim and increments `attempts`; if that brings `attempts` to `max_attempts` the row is dead-lettered there and then and the claim re-runs its select. The re-run is bounded: each pass either claims or dead-letters one row, and the call gives up after `NX_TUPLE_CLAIM_PASSES` passes (default 8, an engine setting distinct from `NX_TUPLE_READ_MAX`) and returns the probe result. `ack` and `nack` are checked against ownership: `ClaimOwnership` if a live claim is held by someone else, `ClaimNotFound` if no live claim matches the id (including a second `ack` on an already-acked claim, or a claim_id whose lease has already lapsed but nobody has re-claimed or swept it yet).

## Blocking reads

There is one engine JVM and every `out` passes through it, so a per-subspace `Condition` (one lock and condition per subspace key in a concurrent map) is the wake mechanism. The `out` handler signals all waiters on that subspace after the tenant-scoped transaction has committed, never from a transaction listener. A parked call is a loop: open a short transaction, run the equality query, return the connection, then park outside any transaction until the signal or a one-second timer, then repeat. A parked call never holds one of the pooled connections while parked. The waiter is registered before the query, not after, so a commit between query and park is not lost.

`timeout_s` is capped at 25 seconds by default, settable on the engine; a call at the cap returns the probe result and the caller loops, so a wait of minutes is a loop of parked calls, not one long park. The client's own HTTP timeout is set above `timeout_s` so the server's cap fires first. Per-claimant and global park caps are engine settings (defaults four and sixteen) refused with `ParkCapExceeded`, which returns the probe result. On shutdown every waiter is signalled so parked calls return the probe result instead of riding out their budget, which is what makes a deploy gap a retry rather than a stall on top of it. Parked callers on one subspace do not wake on commits in another. `LISTEN`/`NOTIFY` is available on the direct connection and deferred until a second JVM exists.

## Retry across a deploy

An engine deploy opens a gap of about 25 seconds. The operations are shaped so a client can retry through it without a coordination protocol of its own.

| Operation | On a 502 or 504 | Why it is safe |
| --- | --- | --- |
| `rd`, `rdp` | retry freely | a read takes nothing |
| `out` | retry freely | the id is derived from caller fields only; the resend is the same row and only refreshes `expires_at` |
| `in` | retry with the same claimant | a live claim held by this claimant on a matching tuple is returned with its claim id, no new update; otherwise the lease and sweep cover it exactly as a crash after `in` |
| parked `rd` / `in` | returns the probe result at shutdown | every waiter is signalled on shutdown, so the gap is one retry, not a stall on top of it |

Two more guards live in the client: the request it is about to send is measured against the edge's 8 KB body limit before sending, and its own HTTP timeout sits above `timeout_s` so the engine's cap always fires first.

## The sweep

Every `SWEEP_INTERVAL_HOURS` (six hours today), a second scheduled task on the same scheduler that runs the existing sweep enumerates tenants from `nexus.tuple_tenants`, a small table with no row-level security, one row per tenant that has ever written a tuple, upserted by `out`, visited least-recently-swept first (`last_swept_at` ascending, nulls first, `tenant_id` as the tie-break). Per tenant it runs three arms, each committing one batch of up to `NX_TUPLE_SWEEP_BATCH_SIZE` rows (default 300) at a time until drained or its own share of the per-tenant cap runs out: (1) release lapsed claims nobody re-took, with an `expire` log row and an `attempts` increment, dead-lettering at `max_attempts`; (2) purge expired and consumed-past-retention tuple rows (setting the claim log's `tuple_id` to null); (3) purge log rows past the log's own longer TTL (`NX_TUPLE_CLAIM_LOG_TTL_DAYS`, default 180 days). Two budgets keep the sweep from starving the T1 crash-safety sweep sharing the same single-thread scheduler and from letting one tenant starve the rest: `NX_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT` (default 50) caps the batches one tenant spends in one visit, split into three roughly-equal per-arm shares — so a large release-arm backlog can no longer exhaust the whole cap on arm one and leave the two purge arms unrun that visit, each arm's loop runs unconditionally regardless of the others' outcome; `NX_TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS` (default 120) caps the whole run and is checked only at a tenant boundary, before starting the next tenant, never mid-tenant — a tenant already underway always finishes cleanly or hits its own per-arm share first. Every statement the sweep issues, including the tenant enumeration itself, also carries the engine's ordinary per-statement timeout. A tenant is stamped only when all three arms finish cleanly within its share this visit; a tenant cut short by either budget keeps its old stamp and is therefore first next run, so the order lives in the table and survives a restart with no cursor held in the JVM.

Every run logs a counted outcome record: tenants visited, the oldest `last_swept_at` after the run, scanned, released, dead-lettered, purged, log rows purged, and the run's incomplete cause — none, a tenant hitting its own per-arm cap, a tenant's arms throwing, or the run's wall-clock budget exhausted at a tenant boundary, reported in that ascending order of severity when a run touches more than one. A run that finds nothing expired is the normal state of a healthy table; the failure the counts detect is a run that scanned nothing at all, or a run that did not happen, which the doctor row on last-sweep age reports.

Three `nx doctor` rows: oldest unclaimed age per subspace over claimable rows only (live, unclaimed, not dead-lettered); dead-tuple ratio and last autovacuum on the table; age of the last tuple sweep, read off `nexus.tuple_tenants.last_swept_at` alone. The third row cannot see a run's `incomplete_cause` — that value is a structured log line only (`event=tuple_sweep_run ...`), never a persisted row a client can read back — so it reports staleness, not cause: any of "scanned nothing", "did not run" or "hit its budget every visit" looks the same to it, a `last_swept_at` older than expected. `autovacuum_vacuum_scale_factor = 0.01` on `nexus.tuples` and `nexus.tuple_claim_log`, the first per-table storage parameter in the changelog, reclaims during sustained churn; the default already self-heals once churn stops.

## Errors

Nine typed errors, one base class (`TupleException`) carrying a `code` and the HTTP status `TupleHandler` sends for it, so a new subtype cannot be added without also declaring how it renders. Every error is rendered `{"error": "<code>", "detail": "<message>"}` at its own status; `TupleHandler` catches this base type ahead of the generic 500 ladder.

- `UnknownSubspace` (404): the subspace does not match a registered template.
- `SchemaViolation` (400): a field and reason, checked before any write; covers a missing pinned key, a missing required dim, an `out` without a nonce on a `keys+nonce` template, and a `ttl_seconds` or `lease_s` at or below zero or a negative `timeout_s`.
- `TakeDisabled` (422): the template's `take.enabled` is false.
- `TimeoutTooLong` (400): `timeout_s` above the engine's cap.
- `ClaimNotFound` (404): no live claim with that id.
- `ClaimOwnership` (403): a live claim held by someone else.
- `ParkCapExceeded` (429): the per-claimant or global park cap is reached; the caller gets the probe result and backs off.
- `TtlTooLong` (400): a `ttl_seconds` above the template's `retention_seconds`.
- `LeaseTooLong` (400): a `lease_s` above the template's `max_lease_seconds`; a lease longer than the row's remaining TTL is clamped, not refused.

Three refusals outside the nine, all in `TupleHandler` itself: a request against a route with the wrong HTTP method refuses 405 (every write route is POST-only, `registry`/`subspace_list`/`subspace_stats` are GET-only); a malformed or missing required field in the request body refuses 400 (`IllegalArgumentException`, the same mapping every other handler in this package uses); a request with no tenant resolved refuses 500 (`internal: tenant not set` — never reachable through the auth filter on a correctly configured route).

## What it is not for

The build lease (it guards building the engine and must work with the engine down), the T1 identity files (a reader needs a credential before it can read a tuple), the push vouching, any wrapping of scratch, memory or plans, and surfaces. A consumer not named in RDR-205 needs its own RDR.

## Prior art

JavaSpaces, the Jini-era Linda, is the leased and transactional form of this design. Four of six points of contact are already met; two are not.

| Point of contact | JavaSpaces | RDR-205 | Status |
| --- | --- | --- | --- |
| Take template | null-as-wildcard, no floor on generality | every pinned key required by equality | covered, stricter |
| Durability | spec permits transient spaces | Postgres only; claims re-earned by lease lapse after restore | covered, stronger |
| Discovery and transport | multicast lookup, RMI | one engine, HTTP and JSON | covered by construction |
| `notify` | leased remote listener registration | in-process park, caps four and sixteen, `ParkCapExceeded` | covered, different failure mode |
| Lease renewal | renew and cancel by the holder | fixed lease, max 900 s on the mailbox | accepted for v1; `renew` a later candidate |
| Take and reply | one transaction under 2PC | two calls; window bounded by the lease | accepted for v1; ack with reply a later candidate |

Two gaps are accepted for v1, each named as what it is rather than argued as settled. The first is an assumption the record does not argue: that no v1 consumer holds a mailbox claim across work longer than the template's `max_lease_seconds` of 900 seconds, so the absence of a renew operation is safe by scope rather than by construction. The second is that `in` and the `out` that answers it are two calls rather than one transaction, so a consumer that crashes after its `in` and before its answering `out` sees the request re-delivered at lease lapse and repeats the work; the window is bounded by the lease and visible in the claim log, and no reply is lost, since none was written. Both a `renew` operation on a live claim and an `ack` that carries an optional reply `out` in the ack's own transaction are named as candidates for a later version, not scheduled and not designed here.

## Where the decisions live

The design, its research, its alternatives and its gate history are recorded in [RDR-205](rdr/rdr-205-linda-tuple-space-over-postgres.md).
