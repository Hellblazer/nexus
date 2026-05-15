---
name: tuplespace-barriers
description: Use when waiting for multiple parallel agents to complete before proceeding (fan-in synchronization via the barriers subspace)
effort: low
---

# Tuplespace Barriers

The barriers subspace is the fan-in synchronization primitive (RDR-110). Each participant posts an arrival tuple; the coordinator waits until the expected count has arrived. Once the barrier is satisfied, the coordinator proceeds.

## When to use

- N parallel agents must all finish before the next step.
- You want a count-based gate, not a semantic one.
- You need each participant's arrival to be observable individually (so debugging "who is late?" is possible).

## Pattern

Coordinator declares a barrier name. Participants each post an arrival tuple keyed to that name. Coordinator reads until the participant count reaches the expected value.

### Participant posts arrival

```
tuplespace_out(
    subspace="barriers/phase-1",
    content="agent-impl-3 finished phase 1",
    dimensions='{"barrier": "rdr-110-phase-1", "participant": "agent-impl-3", "status": "arrived"}',
)
```

### Coordinator waits

```
arrivals = tuplespace_read(
    subspace="barriers/phase-1",
    query="",
    where={"barrier": "rdr-110-phase-1", "status": "arrived"},
    n=100,
)
if len(arrivals) >= expected_count:
    # barrier satisfied
    proceed()
else:
    # poll again after backoff
    pass
```

## Cleanup

After the barrier fires, the coordinator may `take` each arrival (when the schema enables take) or simply let `retention_seconds` reap them. Leaving them for retention is the simpler default.

## Pitfalls

- A late participant arrives after the coordinator has already proceeded. Decide whether the late arrival is a warning, an error, or ignored.
- Without a stable `barrier` name, two runs of the same phase mix arrivals. Use a fresh barrier id (timestamp, run id) per fan-in episode.
- Block-mode wait is not available in Phase 1 direct mode. Coordinators poll with backoff.

## Related

- `/nx:tuplespace-events` when subscribers act on each arrival independently rather than waiting for a count.
- `/nx:tuplespace-tasks` for the unit of work itself; barriers gate the work, they are not the work.
