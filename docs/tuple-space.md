# Tuple Space

> Status: design of record from RDR-205 (accepted) and RDR-206 (accepted), which adds `renew` and an optional reply on `ack`. RDR-205's Phase 1 (engine) and Phase 2 (client — `nx tuple`, the nine `tuple_*` MCP tools, the doctor rows) have both shipped: `/v1/tuples` shipped on `engine-service-v0.1.114` (Phase 3), deployed to the managed cloud since 2026-09-11. The client shipped in conexus 7.41.0, which also bumps the pinned local-mode engine floor (`REQUIRED_ENGINE_VERSION`, `src/nexus/engine_version.py`) to `v0.1.114` — a local install on 7.41.0 or later has the route live. An install on an older release stays pinned below the floor and a local-mode call 404s (the three `nx doctor` rows report this as informational, not a defect, below that floor). RDR-206's `renew` and ack-with-reply are implemented on both the engine and the client as of this writing, but not yet in a tagged engine release or a client release — see the wire ledger's `## Unshipped` entry (`docs/wire-contract-pending.md`) for the exact commits and the paired-release plan.

## What it is

A tuple space is a shared bag of typed records that any process can add to, read from, or take out of, where taking is atomic: when two processes try to take the same record, exactly one gets it. Linda (Gelernter, 1985) named four operations, `out` (add), `read`, `in` (take) and `eval`; this design ships the non-blocking probe forms as well. A work queue, a mailbox, a lock and a request-reply channel are the same three operations over different record shapes. The [walkthroughs](tuple-space-walkthroughs.md) draw each of this design's uses as a sequence.

Two consumers ship with this design and no others: the RDR-184 dispatch ledger (start and report tuples keyed on the harness's per-instance agent id; today a tab-separated file, the TSV, that the hooks keep) and a mailbox addressed to an agent id or an instance name, which also carries the cross-instance request-and-ack between the nexus and conexus sessions on one box. Nothing else: not the build lease, not the T1 identity files, not the push vouching, not any wrapping of scratch, memory or plans, not surfaces. A consumer not named here needs its own RDR.

## Operations

Eleven HTTP routes under `/v1/tuples`. Signatures are the contract; the handler is the implementation.

```text
out(subspace, keys, dims, body, *, nonce=None, ttl_seconds=None) -> tuple_id
                                                            # nonce required where id_from is keys+nonce;
                                                            # ttl_seconds defaults to the template's retention
rd (subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0) -> [Tuple]   # non-destructive; blocks up to timeout_s
rdp(subspace, keys_pattern=None, *, n=1, since=None) -> [Tuple]                # probe
in (subspace, keys_pattern, *, claimant, lease_s, timeout_s=0) -> (Tuple, claim_id) | None
inp(subspace, keys_pattern, *, claimant, lease_s) -> (Tuple, claim_id) | None
ack(claim_id, claimant, *, reply=None) -> reply_id | None  # ownership checked; reply is {subspace, keys, dims,
                                                            # body, ttl_seconds}, written and the request
                                                            # consumed in one transaction (RDR-206)
nack(claim_id, claimant)                                   # ownership checked; counts an attempt
renew(claim_id, claimant, lease_s) -> lease_until          # ownership checked; extends a live claim, clamped
                                                            # to expires_at, refused above the template's
                                                            # max_lease_seconds; never counts an attempt (RDR-206)
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
| `ack` | no | no: a second `ack` on the same claim is `ClaimNotFound`; a reply written with it shares that rule, since the reply and the consumption commit in one transaction | none |
| `nack` | no | no: every call counts an attempt against `max_attempts` | none |
| `renew` | no | no: a repeat renews again from the new `now`, extending the lease further; a lapsed claim is `ClaimNotFound`, not resurrected | none |
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

The id is derived from caller-supplied fields only, so `out` is idempotent by construction and a retry across a deploy gap is the same tuple. Which fields is the template's `id_from`: `keys` (the ledger: `agent_id` and `kind` identify a dispatch, so a second start for the same agent is the same tuple, which is what the census wants); `keys+nonce` (the mailbox: the sender mints a message id, unique among its own messages, and passes it as the nonce; the template's `id_dims` name the dims that also enter the id, `from` for the mailbox, so a nonce need only be unique per sender and two senders' messages never collide; two messages to one address are two tuples and a resent message is one); or `keys+body` (content-addressed, no v1 template). The insert time is never part of an id. A refire with the same id refreshes `expires_at` only, never the body, the claim state or the consumed state, and never past `created_at` plus the template's `retention_seconds`. The nonce is REQUIRED on any `keys+nonce` template, the mailbox included: an `out` that omits it is refused as `SchemaViolation`, the same way a missing required dim is. It is an id ingredient only, not a stored field — a read (`rd`/`rdp`/`in`/`inp`) never echoes it back, the same way it never echoes a claim_id the reader has not won.

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

Every claim reaches a terminal transition: `ack`, `nack`, `expire` or `dead`. `ack` sets `consumed_at` and logs `ack`; `nack` releases the claim (`claim_state`, `claimant`, `claim_id` and `lease_until` to NULL) and increments `attempts`. When a claim finds a row whose previous lease has lapsed, the same transaction writes the `expire` row for the previous claim and increments `attempts`; if that brings `attempts` to `max_attempts` the row is dead-lettered there and then and the claim re-runs its select. The re-run is bounded: each pass either claims or dead-letters one row, and the call gives up after `NX_TUPLE_CLAIM_PASSES` passes (default 8, an engine setting distinct from `NX_TUPLE_READ_MAX`) and returns the probe result. `ack` and `nack` are checked against ownership: `ClaimOwnership` if a live claim is held by someone else, `ClaimNotFound` if no live claim matches the id (including a second `ack` on an already-acked claim, or a claim_id whose lease has already lapsed but nobody has re-claimed or swept it yet). The check is enforced on the update itself, as a compare-and-swap on the claim's identity (`id`, `claim_state = 'claimed'`, `claim_id`, `consumed_at IS NULL`), so an `ack` or `nack` that loses the race with the sweep's release between its read and its write fails `ClaimNotFound` and writes no log row, instead of consuming or releasing a row it no longer holds.

`renew(claim_id, claimant, lease_s)` extends a live claim without consuming it and without spending an attempt (RDR-206). The new `lease_until` is `now + lease_s`, clamped to the row's `expires_at` at UPDATE time in SQL — never against a value read earlier, which closes a window a same-tuple refire could otherwise open — so a duration inside the template's cap can still come back shorter than asked, silently. A `lease_s` above the template's `max_lease_seconds` is refused outright with `LeaseTooLong` rather than clamped: renewal changes who decides when work is long, not the cap. `renew` reads through the same `liveClaimRow` predicate as `ack` and `nack`, so a claim whose lease has already lapsed is `ClaimNotFound`, not resurrected — a holder that missed its window learns it lost the claim, exactly as a late `ack` would tell it — and it is checked against ownership the same way, and enforced by the same compare-and-swap, so a renew that loses the race with the sweep's release also fails `ClaimNotFound` and writes no log row. A successful renew writes one `renew` claim-log row and never touches `attempts`: a renew is the holder keeping the message, not a delivery given back, so unlike a nack or a lapsed lease it never counts toward `max_attempts`.

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
| `ack` with a reply | retry only while the first call's response is genuinely lost, never after a `ClaimNotFound` | the ack and the reply write commit together in one transaction, so a retry after a lost response finds the claim already consumed and answers `ClaimNotFound` rather than writing a second reply; re-send the reply with `out` if it still matters |
| `renew` | retry freely while the claim is still live | each call renews again from the new `now`; a retry after the claim has lapsed answers `ClaimNotFound` instead of resurrecting it |
| parked `rd` / `in` | returns the probe result at shutdown | every waiter is signalled on shutdown, so the gap is one retry, not a stall on top of it |

Two more guards live in the client: the request it is about to send is measured against the edge's 8 KB body limit before sending, and its own HTTP timeout sits above `timeout_s` so the engine's cap always fires first.

## The sweep

Every `SWEEP_INTERVAL_HOURS` (six hours today), a second scheduled task on the same scheduler that runs the existing sweep enumerates tenants from `nexus.tuple_tenants`, a small table with no row-level security, one row per tenant that has ever written a tuple, upserted by `out`, visited least-recently-swept first (`last_swept_at` ascending, nulls first, `tenant_id` as the tie-break). Per tenant it runs three arms, each committing one batch of up to `NX_TUPLE_SWEEP_BATCH_SIZE` rows (default 300) at a time until drained or its own share of the per-tenant cap runs out: (1) release lapsed claims nobody re-took, with an `expire` log row and an `attempts` increment, dead-lettering at `max_attempts`; (2) purge expired and consumed-past-retention tuple rows (setting the claim log's `tuple_id` to null); (3) purge log rows past the log's own longer TTL (`NX_TUPLE_CLAIM_LOG_TTL_DAYS`, default 180 days). Two budgets keep the sweep from starving the T1 crash-safety sweep sharing the same single-thread scheduler and from letting one tenant starve the rest: `NX_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT` (default 50) caps the batches one tenant spends in one visit, split into three roughly-equal per-arm shares — so a large release-arm backlog can no longer exhaust the whole cap on arm one and leave the two purge arms unrun that visit, each arm's loop runs unconditionally regardless of the others' outcome; `NX_TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS` (default 120) caps the whole run and is checked only at a tenant boundary, before starting the next tenant, never mid-tenant — a tenant already underway always finishes cleanly or hits its own per-arm share first. Every statement the sweep issues, including the tenant enumeration itself, also carries the engine's ordinary per-statement timeout. A tenant is stamped only when all three arms finish cleanly within its share this visit; a tenant cut short by either budget keeps its old stamp and is therefore first next run, so the order lives in the table and survives a restart with no cursor held in the JVM.

Every run logs one structured `event=tuple_sweep_run` line: `tenants_visited`, `oldest_last_swept_at` (after the run), `scanned`, `released`, `dead_lettered`, `purged`, `purge_examined`, `log_rows_purged`, `log_purge_examined`, and `incomplete_cause` — `NONE`, `TENANT_CAP` (a tenant hit its own per-arm share), `TENANT_ERROR` (a tenant's arms threw), or `WALL_CLOCK` (the run's wall-clock budget was exhausted at a tenant boundary), reported as the worst cause seen across every tenant the run touched (the enum's own ordinal order is the severity order). A run that finds nothing expired is the normal state of a healthy table; the failure the counts detect is a run that scanned nothing at all, or a run that did not happen, which the doctor row on last-sweep age reports. This line is a log record only, never a persisted row — `nx doctor`'s sweep-freshness check reads `nexus.tuple_tenants.last_swept_at` alone and cannot see `incomplete_cause`.

Three `nx doctor` rows: oldest unclaimed age per subspace over claimable rows only (live, unclaimed, not dead-lettered); dead-tuple ratio and last autovacuum on the table; age of the last tuple sweep, read off `nexus.tuple_tenants.last_swept_at` alone. The third row cannot see a run's `incomplete_cause` — that value is a structured log line only (`event=tuple_sweep_run ...`), never a persisted row a client can read back — so it reports staleness, not cause: any of "scanned nothing", "did not run" or "hit its budget every visit" looks the same to it, a `last_swept_at` older than expected. `autovacuum_vacuum_scale_factor = 0.01` on `nexus.tuples` and `nexus.tuple_claim_log`, the first per-table storage parameter in the changelog, reclaims during sustained churn; the default already self-heals once churn stops.

## Errors

Nine typed errors, one base class (`TupleException`) carrying a `code` and the HTTP status `TupleHandler` sends for it, so a new subtype cannot be added without also declaring how it renders. Every error is rendered `{"error": "<code>", "detail": "<message>"}` at its own status; `TupleHandler` catches this base type ahead of the generic 500 ladder.

- `UnknownSubspace` (404): the subspace does not match a registered template, or its address segment is not of the form `[A-Za-z0-9][A-Za-z0-9._-]*` (one segment, no spaces, no empty address; a session id, an agent id, or an instance name such as `nexus-70` all qualify). `subspace_stats` on such a name is this error, never a zero census (engines after v0.1.114). A reply's target subspace on `ack` is checked the same way, before the ack's transaction opens.
- `SchemaViolation` (400): a field and reason, checked before any write; covers a missing pinned key, a missing required dim, an `out` without a nonce on a `keys+nonce` template, a `ttl_seconds` or `lease_s` at or below zero or a negative `timeout_s`, a reply object on `ack` that carries a `nonce` key (the engine sets a reply's nonce itself, to the request's own tuple id, so a caller-supplied one is refused rather than silently dropped), and a reply whose target template is not `keys+nonce` (a keys-only target, such as the RDR-184 ledger, would collapse two replies onto one id).
- `TakeDisabled` (422): the template's `take.enabled` is false.
- `TimeoutTooLong` (400): `timeout_s` above the engine's cap.
- `ClaimNotFound` (404): no live claim with that id — including a `renew` on one whose lease has already lapsed, which is not resurrected.
- `ClaimOwnership` (403): a live claim held by someone else — checked on `renew` the same way as on `ack` and `nack`.
- `ParkCapExceeded` (429): the per-claimant or global park cap is reached; the caller gets the probe result and backs off.
- `TtlTooLong` (400): a `ttl_seconds` above the template's `retention_seconds`, on `out` or on a reply written by `ack`.
- `LeaseTooLong` (400): a `lease_s` above the template's `max_lease_seconds`, on `in`/`inp` or on `renew`; on `in`/`inp` a lease longer than the row's remaining TTL is clamped, not refused, and `renew` clamps the same way against the row's `expires_at` at update time — but a `lease_s` over the template's cap itself is refused on both, never clamped.

Three refusals outside the nine, all in `TupleHandler` itself: a request against a route with the wrong HTTP method refuses 405 (every write route is POST-only, `registry`/`subspace_list`/`subspace_stats` are GET-only); a malformed or missing required field in the request body refuses 400 (`IllegalArgumentException`, the same mapping every other handler in this package uses); a request with no tenant resolved refuses 500 (`internal: tenant not set` — never reachable through the auth filter on a correctly configured route).

## Client surface

`nexus.db.t2.http_tuple_store.HttpTupleStore` is a ninth T2 domain store (`db.tuples`), an HTTP client over `/v1/tuples` built the same way every other `Http*Store` is (constructor injection, credential/endpoint self-heal, `RefreshableHttpStoreMixin`'s default idempotent gateway retry on 502/503/504 — no operation here opts out: `rd`/`rdp` are freely retryable, a retried `out` lands on the same tuple by its id formula, and a retried `in`/`inp` shares the identical crash-after-claim ambiguity the lease and sweep already cover). Two things it carries that no other T2 store needs:

- **8 KB pre-send guard.** The edge WAF rejects request bodies over 8 KB. The client measures the exact serialised request `json.dumps` would put on the wire and refuses before sending (`RequestTooLargeError`), rather than letting a request die at the edge with no local signal. A reply attached to `ack` can push it over the cap that a bare ack could never reach.
- **Typed-error mapping.** The engine renders each of the nine errors below as `{"error": "<code>", "detail": "<message>"}` at the error's own HTTP status. Some codes share a status (`UnknownSubspace` and `ClaimNotFound` are both 404), so the client classifies by the `error` field, never the bare status code, and re-raises the matching `TupleError` subclass (`UnknownSubspaceError`, `SchemaViolationError`, `TakeDisabledError`, `TimeoutTooLongError`, `ClaimNotFoundError`, `ClaimOwnershipError`, `ParkCapExceededError`, `TtlTooLongError`, `LeaseTooLongError`) — a code the engine did not name this way passes through as an ordinary `httpx.HTTPStatusError`.
- **Reply-loss guard (RDR-206).** `ack(claim_id, claimant, reply=...)` raises `ReplyNotWrittenError` — deliberately outside the `TupleError` hierarchy, so it survives a broad `except TupleError` rather than being swallowed by it — when the response carries no `reply_id` after a reply was sent. The request is already consumed at that point, whether the engine predates `renew`/ack-with-reply (no `reply_id` key at all) or a new engine simply wrote nothing (a null `reply_id`); either way a retried ack answers `ClaimNotFound`, not a second attempt to write the reply, so the caller must re-send the reply with `out()` if it still matters.

**HTTP timeout ordering.** A blocking `rd`/`in_` call (`timeout_s > 0`) passes a per-call HTTP timeout of `timeout_s` plus a five-second margin — strictly above the caller's park budget, so the engine's own cap (25 s by default) always returns its probe result before the client's own transport timeout could fire first.

Two access paths sit on top of `HttpTupleStore`:

- **`nx tuple`** — `out`, `rd`, `in`, `ack`, `nack`, `renew`, `templates`, `list`, `stats`, `watch` (a ping-then-pull mailbox watcher for a Claude Code Monitor; it never claims, preflights before watching, and holds a machine-wide lock per address). `ack` takes `--reply-subspace`/`--reply-key`/`--reply-dim`/`--reply-body`/`--reply-ttl-seconds` (RDR-206); there is no `--reply-nonce` flag, since the engine sets it. See [CLI Reference — nx tuple](cli-reference.md#nx-tuple) for every flag.
- **Nine `tuple_*` MCP tools** — `tuple_out`, `tuple_rd`, `tuple_in`, `tuple_ack`, `tuple_nack`, `tuple_renew`, `tuple_registry`, `tuple_list`, `tuple_stats` (`rd`/`in`'s own `timeout_s=0` default covers the probe case; there are no separate `tuple_rdp`/`tuple_inp` tools). `tuple_ack` takes an optional `reply` object (RDR-206) with the same fields as `tuple_out` minus the nonce. See [MCP Servers — Tuple space](mcp-servers.md#tuple-space-t2-adjacent-rdr-205) for signatures and the routing rule of thumb.

**Three `nx doctor` rows**, each gated the same way against the engine floor that first serves `/v1/tuples` — an install below that floor reports the check as informational, not a defect, and an install at or above the floor that still 404s reports UNKNOWN and asks you to investigate the engine install:

| Label | What it reports |
| --- | --- |
| `tuples.oldest_unclaimed` | Oldest unclaimed tuple's age, per subspace, over claimable rows only (live, unclaimed, not dead-lettered); subspaces whose template disables `take` are skipped |
| `tuples.dead_tuple_ratio` | Dead-tuple ratio and last autovacuum on `nexus.tuples` / `nexus.tuple_claim_log` (local-only admin-psql path, same as the migration-state and RLS checks) |
| `tuples.sweep_freshness` | Age of the last tuple sweep, read off `nexus.tuple_tenants.last_swept_at` alone — the sweep's own incomplete-cause classification is a structured log line, never a persisted row, so this row reports staleness, not cause |

## What it is not for

The build lease (it guards building the engine and must work with the engine down), the T1 identity files (a reader needs a credential before it can read a tuple), the push vouching, any wrapping of scratch, memory or plans, and surfaces. A consumer not named in RDR-205 needs its own RDR.

## Prior art

JavaSpaces, the Jini-era Linda, is the leased and transactional form of this design. All six points of contact are now met.

| Point of contact | JavaSpaces | This design | Status |
| --- | --- | --- | --- |
| Take template | null-as-wildcard, no floor on generality | every pinned key required by equality | covered, stricter |
| Durability | spec permits transient spaces | Postgres only; claims re-earned by lease lapse after restore | covered, stronger |
| Discovery and transport | multicast lookup, RMI | one engine, HTTP and JSON | covered by construction |
| `notify` | leased remote listener registration | in-process park, caps four and sixteen, `ParkCapExceeded` | covered, different failure mode |
| Lease renewal | renew and cancel by the holder | `renew(claim_id, claimant, lease_s)`: relative duration, clamped to the tuple's `expires_at`, refused above the template's `max_lease_seconds`, refused rather than resurrected on a lapsed claim | closed by RDR-206; no cancel operation — a holder that wants to give up a claim early calls `nack` |
| Take and reply | one transaction under 2PC | `ack`'s optional `reply`: written and the request consumed in one transaction, no 2PC | closed by RDR-206 |

Both gaps RDR-205 left open for its first version are closed by RDR-206. The first was an assumption the record did not argue: that no v1 consumer holds a mailbox claim across work longer than the template's `max_lease_seconds` of 900 seconds. `renew` ends that dependency: a consumer still working extends its own claim before the lease lapses, so the cap can stay short without a long task losing its claim silently. The second was that `in` and the `out` that answers it are two calls rather than one transaction, so a consumer that crashed between them left the reply either lost or, worse, duplicated once the request was redelivered. `ack`'s optional `reply` closes that window by writing the reply and consuming the request in the same transaction: both commit or neither does, so a crash between the old two calls can no longer happen because there is no longer a gap between them. See [RDR-206](rdr/rdr-206-tuple-claim-renew-and-reply-in-ack.md) for the design, its research, and its gate history.

## Where the decisions live

The design, its research, its alternatives and its gate history are recorded in [RDR-205](rdr/rdr-205-linda-tuple-space-over-postgres.md) and, for `renew` and ack-with-reply, [RDR-206](rdr/rdr-206-tuple-claim-renew-and-reply-in-ack.md).
