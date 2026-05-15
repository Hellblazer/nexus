---
name: tuplespace-tasks
description: Use when dispatching or claiming units of work via the tasks subspace, including coordinator handoff between agents
effort: low
---

# Tuplespace Tasks

The `tasks/<project>` subspace is the canonical work queue for agent coordination (RDR-110). One agent posts a tuple describing a work unit; another agent semantically takes it. The `take` operation is atomic (CAS under SQLite's single-writer lock), so two claimants cannot succeed on the same row.

## When to use

- Dispatching work that any capable agent can pick up.
- Coordinator wants to fan out subtasks without naming a specific recipient.
- Agent has finished its part and wants to surface the next step.

Use the mailbox subspace instead when you have a named recipient. Use locks when you need mutual exclusion on a resource rather than a work item.

## Schema (canonical defaults)

- Dimensions: `status` (open / in_progress / done / cancelled), `priority` (P0..P4), `assignee` (optional), `created_by` (required).
- `embed_from: content`, semantic take with floor 0.55 / margin 0.08.
- Retention: 90 days.

## Posting a task

MCP tool (preferred for agents):

```
tuplespace_out(
    subspace="tasks/nexus",
    content="Ship the consumer landing surface for RDR-110",
    dimensions='{"status": "open", "priority": "P1", "created_by": "planner-1"}',
)
```

CLI smoke test:

```
nx tuplespace out tasks/nexus '{"status":"open","priority":"P1","created_by":"alice"}' \
  --content "ship the landing surface"
```

`tuple_id` is deterministic over `(subspace, content, dimensions, match_text)`, so re-posting is idempotent.

## Claiming a task

```
tuplespace_take(
    subspace="tasks/nexus",
    query="landing surface for tuple space",
    claimant="agent-impl-3",
)
```

Returns `(tuple_dict, claim_id)` or `None`. Default lease is 600 s. The claim expires automatically if you crash; another agent can then re-claim.

## Acking and nacking

When the work is complete, call `tuplespace_ack(claim_id, claimant)`. The tuple transitions to `consumed`.

When you cannot complete the work (precondition failed, dependency missing), call `tuplespace_nack(claim_id, claimant)`. The tuple returns to `available` and another claimant can take it.

## Inspecting the queue

```
nx tuplespace stats tasks/nexus
nx tuplespace read tasks/nexus --query "your capability"
```

## Pitfalls

- Re-claim by the same claimant returns the existing `claim_id`, not an error. Idempotent.
- `block=True` is feature-flagged OFF in direct mode (Phase 1). Poll instead.
- In `NX_STORAGE_MODE=daemon` the CLI refuses writes; agents must route through MCP tools.

## Related

- `/nx:tuplespace-mailbox` for direct sender-to-recipient handoff.
- `/nx:tuplespace-lock` for mutual exclusion.
- `/nx:tuplespace-stats` for queue introspection.
