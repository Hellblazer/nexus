---
name: tuplespace-events
description: Use when broadcasting a state change that multiple subscribers may want to observe (pub-sub via the events subspace)
effort: low
---

# Tuplespace Events

The events subspace carries broadcast notifications (RDR-110). Unlike tasks or mailbox, events are read non-destructively, so many subscribers can observe the same tuple. `take` is typically disabled on the schema.

## When to use

- A state change happened that multiple agents may care about (file changed, RDR accepted, build finished).
- You do not know in advance who is listening.
- You want subscribers to filter on dimensions rather than carry a routing table.

## Publishing

```
tuplespace_out(
    subspace="events/rdr",
    content="RDR-110 status flipped to accepted",
    dimensions='{"kind": "rdr-accepted", "rdr_id": "RDR-110", "actor": "planner-1"}',
)
```

Match the dimensions to whatever filters subscribers will use. If a `kind` field is universal across all event subspaces, keep that convention so the broker layer can route uniformly.

## Subscribing

Subscribers poll via `tuplespace_read` with a filter:

```
tuplespace_read(
    subspace="events/rdr",
    query="rdr accepted",
    where={"kind": "rdr-accepted"},
    n=20,
)
```

`read` is non-destructive. Two subscribers calling read see the same tuples.

## Subscription windowing

Events accumulate. Each subscriber tracks its own watermark (highest `created_at` observed) and only acts on tuples newer than that. The watermark is subscriber-local state, not stored in the tuple.

## Pitfalls

- Do not call `take` on an events subspace. The schema typically has `take.enabled: false`; an attempt raises `TakeDisabledError`. If you need destructive consumption, you wanted tasks or mailbox.
- Retention matters. Events are bounded by `retention_seconds`; a subscriber that wakes after a long sleep misses everything older than the retention window. Heartbeat or persist watermark to survive across restarts.
- Two subscribers may receive the same event with different floors; tune your local filter to your needs.

## Related

- `/nx:tuplespace-barriers` when you need fan-in synchronization after a set of events.
- `/nx:tuplespace-mailbox` for targeted delivery rather than broadcast.
