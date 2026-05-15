---
name: tuplespace-lock
description: Use when an agent needs exclusive access to a named resource (mutual exclusion via the locks subspace)
effort: low
---

# Tuplespace Lock

The `locks/<resource>` subspace provides mutual exclusion over named resources (RDR-110). The schema uses `mode: exact` rather than semantic match, so the take operation is a pure SQL CAS on the `resource` dimension.

## When to use

- You need at-most-one-holder semantics over a named resource (a database, a file path, a build slot).
- Optimistic semantic matching is wrong here. Use the `resource` key as a hard identity.

## Acquiring a lock

```
tuplespace_out(
    subspace="locks/build",
    content="ci holds the build slot",
    dimensions='{"resource": "build", "holder": "ci-runner-3"}',
    ttl_seconds=300,
)
result = tuplespace_take(
    subspace="locks/build",
    query="",
    claimant="ci-runner-3",
    where={"resource": "build"},
)
```

The pattern is: post a lock tuple with a TTL (so a crash auto-releases), then immediately take it. The `where` filter must include every `match_key` declared in the schema (here, just `resource`).

## Releasing

Use `tuplespace_ack(claim_id, claimant)` to release cleanly. The tuple transitions to `consumed`.

If the holder crashes, the claim expires after `lease_seconds` and the resource becomes available again.

## Detecting contention

When `take` returns `None`, the lock is held by someone else. Poll, back off, or fail loud depending on the workload.

## Pitfalls

- Exact mode requires every `match_key` in the where filter. Omitting it raises `SubspaceSchemaError`.
- A lock without a TTL on the underlying tuple persists forever in the table. Always set `ttl_seconds` or rely on `lease_seconds` to bound exposure.
- The CAS guarantees one winner per row, not one holder per resource string. If two posters write tuples with the same `resource` value, both rows exist; only one take can succeed against any given row.

## Related

- `/nx:tuplespace-events` when you want "lock released" notifications.
- `/nx:tuplespace-tasks` for cooperative work units rather than exclusive resources.
