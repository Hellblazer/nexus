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
- Arming the mailbox watcher at session start, or acting on a line it prints
- Authoring or reviewing code, tests, or docs against the `mailbox/<address>` template

## Rules

- Address (agent): the harness id `subagent-start.sh` injects (its "Claimant id: `<id>`" line). The mailbox address is `mailbox/<id>`.
- Address (instance, RDR-205 Phase 6): the session name the harness's ListAgents tool shows (for example `nexus-23`), address_kind `instance`. Both instances mint against ONE tenant — no new addressing scheme.
- Cross-instance request and ack: send `tuple_out` to the peer's address with `dims={"from": "<your instance name>", "kind": "request", "correlation_id": "<id>", "address_kind": "instance"}`; park a `PARKED in` on YOUR OWN mailbox (`mailbox/<your instance name>`) for the ack. Each `in` parks at most the 25 s cap — LOOP the call for a wait of minutes, never rely on one park outlasting it. The peer replies by claiming the request with `tuple_in`, doing the work, then `tuple_ack(claim_id, claimant, reply={"subspace": "mailbox/<your instance name>", "keys": {"to": "<your instance name>"}, "dims": {"from": "<peer>", "kind": "ack", "correlation_id": "<same id>"}, "body": "..."})` — never a separate `tuple_out` followed by `tuple_ack`; `correlation_id` is what pairs the ack with its request.
- Renew: `mcp__plugin_conexus_nexus__tuple_renew(claim_id="<id>", claimant="<address>", lease_s=<n>)`. Renew at half the lease when the work behind a claim is still running — do not wait for the lease to lapse. A lapsed claim is retaken, `attempts` increments, and two readers act on the same message; after three lapses the message is dead-lettered while the first reader may still be working on it. `lease_s` is relative to now, refused (`LeaseTooLong`) above the mailbox template's `max_lease_seconds` of 900, and clamped to the tuple's own expiry inside that cap. A renew on an already-lapsed claim fails `ClaimNotFound` — the holder learns it lost the claim rather than resurrecting it — and a renew never counts as an attempt.
- Reply in ack, not a second call: a request that needs an answer is answered through `tuple_ack`'s `reply` argument, in the ack's own transaction — never by a separate `tuple_out` followed by `tuple_ack`. A crash between those two calls would deliver the reply and leave the request unconsumed, so the requester gets a second reply under the same correlation id; reply-in-ack makes that impossible because the reply write and the request's consumption commit together or not at all. The reply's target subspace must resolve to a `keys+nonce` template (the mailbox is; the RDR-184 ledger is not, and is refused with `SchemaViolation`); an unrecognized subspace is refused with `UnknownSubspace`. Both refusals happen before anything is consumed, so the request stays claimed and the holder corrects the reply and retries. The engine sets the reply's nonce to `hex(request tuple id)` itself — no caller supplies one, and a `nonce` key in the reply object is refused as a `SchemaViolation`. If an `ack` with a reply reports that the reply was not written (an old engine with no `/renew`/reply support), the request was still consumed as a plain ack; re-send the reply with a fresh `tuple_out`.
- Push delivery (nexus-6konb): arm ONE watcher per session with the exact `Monitor` call the SessionStart hook injects (its `MAILBOX WATCH` block): `nx tuple watch --instance <your ListAgents name>`, or no arguments when you have no name, with `persistent: true` and a `timeout_ms` (the tool requires it even when persistent). Never pass an address as a bare positional; it suppresses the session-id mailbox. A second arm is harmless: the per-address lock refuses it. Every line the watcher prints is a PING naming an address, a sender, a kind and a tuple id, never the message: drain that address with `tuple_in`, handle it, then `tuple_ack` (with `reply` for a request) or `tuple_nack`. The watcher never claims and never acks. The watcher is additive, never the only delivery path: the UserPromptSubmit drain hook still claims and renders mail at your next prompt when no watcher is armed, and it drains your instance-name mailbox only after `nx tuple watch --instance` has registered that name for this session. The cadence lives in `nx tuple watch`. Do NOT substitute `/loop`, `/schedule`, or a sleep-and-poll loop written in prose or in a Bash command for the watcher.
- Unacked-request sweep: `scripts/check_inbound_relay_acks.py` also reads every `mailbox/*` subspace for `kind=request` rows with no matching `kind=ack` row at the requester's own mailbox.
- Send: `tuple_out` to the recipient's address, with a SENDER-MINTED message id as the nonce. `mcp__plugin_conexus_nexus__tuple_out(subspace="mailbox/<address>", keys={"to": "<address>"}, dims={"from": "<your id>"}, body="...", nonce="<sender-minted nonce>")`. The nonce is REQUIRED — `mailbox/<address>` is a `keys+nonce` template, and an `out` with no nonce is refused as a `SchemaViolation`, the same way a missing `from` is. It is an id ingredient only: reading the message back (`tuple_rd`/`tuple_in`) never echoes the nonce.
- Nonce scope: the nonce need only be unique among the SENDER's own messages. `from` is the template's `id_dims` field, so two senders' messages to the same address never collide even on the same nonce — the sender is part of the tuple's identity.
- Drain: `tuple_in` BEFORE composing any hand-back. `mcp__plugin_conexus_nexus__tuple_in(subspace="mailbox/<address>", keys_pattern={"to": "<address>"}, claimant="<address>", lease_s=<n>)`. Ack (with or without a reply) or nack the claim once handled.
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
- [ ] A claim held across long work is renewed at half its lease, never left to lapse
- [ ] A reply to a request is sent via `tuple_ack(reply=...)`, never via a separate `tuple_out` followed by `tuple_ack`
- [ ] The mailbox watcher is armed once per session through `Monitor` on `nx tuple watch`, each ping is drained with `tuple_in` then acked or nacked, and no `/loop`, `/schedule` or prose poll loop stands in for it
