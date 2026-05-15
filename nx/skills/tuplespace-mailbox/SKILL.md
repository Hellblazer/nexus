---
name: tuplespace-mailbox
description: Use when sending a named message from one agent to another agent (point-to-point delivery via the mailbox subspace)
effort: low
---

# Tuplespace Mailbox

The mailbox subspace is the point-to-point delivery channel between named agents (RDR-110). Unlike tasks (anyone can claim) or events (broadcast), a mailbox tuple targets a specific `recipient`. The recipient takes by exact match on that dimension.

## When to use

- You know exactly which agent should receive the message.
- You want a reply lane back to a specific sender.
- Tasks is too open and events is too noisy.

## Sending

```
tuplespace_out(
    subspace="mailbox/nexus",
    content="Patch ready for review on PR #786",
    dimensions='{"sender": "agent-impl", "recipient": "agent-review", "kind": "review-request"}',
)
```

The `recipient` dimension is the routing key. The sender field carries the reply-to identity.

## Receiving

```
tuplespace_take(
    subspace="mailbox/nexus",
    query="review request",
    claimant="agent-review",
    where={"recipient": "agent-review"},
)
```

Always filter by `recipient`. Otherwise an unrelated agent could semantically take a message addressed to someone else.

## Replying

A reply is just another mailbox tuple with sender and recipient swapped.

```
tuplespace_out(
    subspace="mailbox/nexus",
    content="LGTM, ready to merge",
    dimensions='{"sender": "agent-review", "recipient": "agent-impl", "kind": "review-ack"}',
)
```

## Pitfalls

- Forgetting the `recipient` filter on take is the classic mailbox bug. The recipient may semantically match someone else's message and accidentally claim it.
- Mailbox tuples do not auto-expire by default; configure TTL when you post if the message is time sensitive.
- A nack returns the tuple to available; the original recipient can claim again.

## Related

- `/nx:tuplespace-tasks` for unsolicited work units.
- `/nx:tuplespace-events` for broadcasts to all subscribers.
