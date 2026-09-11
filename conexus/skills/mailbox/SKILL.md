---
name: mailbox
description: Use when sending or draining a mailbox tuple-space message between agents or instances, or when authoring or reviewing code against the mailbox/<address> template
effort: low
---

# Mailbox

RDR-205 Phase 5/6 consumer: `mailbox/<address>` tuple-space messaging, addressed to an agent id (Phase 5) or an instance name (Phase 6, cross-instance request and ack).

## When This Skill Activates

- Sending a message to another agent's or instance's address
- Draining a mailbox before composing a hand-back
- Authoring or reviewing code, tests, or docs against the `mailbox/<address>` template

## Rules

- Address (agent): the harness id `subagent-start.sh` injects (its "Claimant id: `<id>`" line). The mailbox address is `mailbox/<id>`.
- Address (instance, RDR-205 Phase 6): the session name the harness's ListAgents tool shows (for example `nexus-23`), address_kind `instance`. Both instances mint against ONE tenant — no new addressing scheme.
- Cross-instance request and ack: send `tuple_out` to the peer's address with `dims={"from": "<your instance name>", "kind": "request", "correlation_id": "<id>", "address_kind": "instance"}`; park a `PARKED in` on YOUR OWN mailbox (`mailbox/<your instance name>`) for the ack. Each `in` parks at most the 25 s cap — LOOP the call for a wait of minutes, never rely on one park outlasting it. The peer acks by `tuple_out` back to your address with `dims={"from": "<peer>", "kind": "ack", "correlation_id": "<same id>"}`; `correlation_id` is what pairs the ack with its request.
- Unacked-request sweep: `scripts/check_inbound_relay_acks.py` also reads every `mailbox/*` subspace for `kind=request` rows with no matching `kind=ack` row at the requester's own mailbox.
- Send: `tuple_out` to the recipient's address, with a SENDER-MINTED message id as the nonce. `mcp__plugin_conexus_nexus__tuple_out(subspace="mailbox/<address>", keys={"to": "<address>"}, dims={"from": "<your id>"}, body="...", nonce="<sender-minted nonce>")`. The nonce is REQUIRED — `mailbox/<address>` is a `keys+nonce` template, and an `out` with no nonce is refused as a `SchemaViolation`, the same way a missing `from` is. It is an id ingredient only: reading the message back (`tuple_rd`/`tuple_in`) never echoes the nonce.
- Nonce scope: the nonce need only be unique among the SENDER's own messages. `from` is the template's `id_dims` field, so two senders' messages to the same address never collide even on the same nonce — the sender is part of the tuple's identity.
- Drain: `tuple_in` BEFORE composing any hand-back. `mcp__plugin_conexus_nexus__tuple_in(subspace="mailbox/<address>", keys_pattern={"to": "<address>"}, claimant="<address>", lease_s=<n>)`. Ack or nack the claim once handled.
- Two messages, two rows: two sends to one address with different nonces are two tuples.
- Resend, one row: the SAME sender resending the SAME nonce to one address is ONE tuple, not a second. `expires_at` never passes `created_at` plus the template's 7-day retention, however many times it is resent.
- Missing `from`: an `out` naming no `from` is a `SchemaViolation` — `from` is the template's one required dimension.
- Dead-letter: three attempts against one message — nacks, lapsed leases, or a mix of both — park it out of every future claimant's view. It stays readable by `rd` with `claim_state="dead"`, and `subspace_stats` counts it under `dead`.
- Template (`service/src/main/resources/tuples/templates/mailbox.yaml`): keys `to`; dims `from` (required), `kind`, `correlation_id`, `address_kind` in `{agent, instance}`; `take.enabled=true`, `max_attempts=3`, `max_lease_seconds=900`, `retention_seconds=604800`.

## Success Criteria

- [ ] A directive sent mid-turn is drained by `tuple_in` before the hand-back is composed
- [ ] A resend of the same sender+nonce lands on the same tuple id, `expires_at` unchanged
- [ ] A mailbox `out` with no `from` is rejected as a `SchemaViolation`, never silently accepted
- [ ] A cross-instance request parks on the requester's OWN mailbox for the ack, looping at the 25 s cap rather than depending on one park to outlast it
- [ ] An unacked request (no `kind=ack` row at the requester's mailbox) is visible to `scripts/check_inbound_relay_acks.py`'s mailbox scan
