---
name: tuplespace-stats
description: Use when inspecting per-subspace tuple counts (total, available, claimed, consumed) for triage or capacity checks
effort: low
---

# Tuplespace Stats

Counts and gauges for the RDR-110 tuple space. Use this to triage a stuck coordinator, confirm the queue is draining, or sanity-check that a `take` actually moved a tuple to `claimed`.

## Commands

Session-banner summary plus per-subspace breakdown:

```
nx tuplespace stats
nx tuplespace stats --json
```

Single-subspace counts:

```
nx tuplespace stats tasks/nexus
nx tuplespace stats locks/build --json
```

Each row reports four numbers:

- `total` (rows in the subspace)
- `available` (un-consumed, no active claim or expired claim)
- `claimed` (active claim, lease not yet expired)
- `consumed` (acked, terminal)

## When to use

- A coordinator says "no tasks available" and you want to know whether the queue is empty or every tuple is held.
- A lock is stuck and you want to confirm the holder still has a live claim or whether the lease has expired.
- Before re-driving a phase, verify the previous run drained to zero.

## Session-start banner

The session-start hook emits a one-line summary using the same data path:

```
tuplespace: <N> subspaces, <M> tuples, <K> active claims
```

This is the first signal in a session that the tuple space is healthy. Zero `active claims` plus non-zero `tuples` after a coordinator started usually means the coordinator never ran or the schema rejected its dimensions.

## Pitfalls

- In `NX_STORAGE_MODE=daemon`, stats refuses rather than racing the daemon writer. Query via the MCP tool surface instead.
- A consumed tuple still counts in `total` until retention reaps it. To estimate live load, use `available + claimed`.

## Related

- `/nx:tuplespace-list` to see which subspaces exist before you query counts.
- `/nx:tuplespace-tasks` and the other consumer skills for the actual posting / taking flows.
