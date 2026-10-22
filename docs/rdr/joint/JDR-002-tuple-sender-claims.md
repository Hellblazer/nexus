---
id: JDR-002
title: "The tenant is the tuple space's security boundary; from, claimant and directory names are claims"
status: active
owners: [RDR-205, RDR-206, RDR-208]
created: 2026-10-22
---

# JDR-002: The tenant is the tuple space's security boundary; from, claimant and directory names are claims

## Decision

The engine authenticates two things on the tuple path:

- the **tenant**, taken from the bearer token (`AuthFilter` ignores a
  client `X-Nexus-Tenant`, and row-level security is forced on the tuple
  tables);
- the token's **scope**: a `board-ci` credential may only `out`, and only to
  `board/ci/<topic>` (`TupleHandler`).

`TupleHandler` reads no session for authorization. Any holder of the
tenant's data token can therefore read every mailbox, claim any unclaimed
row under any claimant, and write any dim. These fields are **claims, not
authenticated identity**:

- a tuple's `from` and `correlation_id` dims;
- `claimant` on `in`/`inp`, which is also rendered on `rd`;
- a `directory/<name>` row's `session_id`.

A reader that acts on one of these is trusting the sender, not the engine.

The one capability on the tuple path is the **`claim_id`**. It is returned
only to the claim's winner and withheld from `rd` (nexus-em75s.35).
`ack`, `nack`, `renew` and `release` all require it.

On an engine after `engine-service-v0.1.162`, a reply written by
`ack(..., reply=)` gets an id that no `out` can compute (nexus-b747g), so
a pre-written row cannot block or replace it; v0.1.162 and earlier compute
it as an `out` would. A
separate row with a forged `from` can still sit beside it.

What a reader may rely on beyond the tenant:

- `mailbox_send` refuses a session-shaped `from_address` that is not one of
  the calling server's own session ids (nexus-2mv7y). An agent id still
  passes, unchecked beyond its shape. A session id passes when no session
  resolves (Claude Desktop, Cowork, or a Claude Code server whose T1 has
  not published its id yet), because nothing exists there to compare
  against. This guards against a confused model, not a hostile caller.
- Raw `tuple_out` and the dims of a `tuple_ack` reply are not guarded.

Planned, not built: a server-written `sender_session` on `out`, taken from
the request's minted T1 session token and `null` without one, rendered on
`rd`, so the drain hook and the channel can label mail whose `from` does
not match it as unverified (F3 step 2, nexus-fetre, nexus-ztwp4,
nexus-ggmg7). It will be a label, never a refusal.

Revisit this rule, in an RDR rather than here, when a tenant gains a second
human principal or an agent class it does not trust: per-principal
identity, mailbox ACLs, binding directory writes to the writer's session,
or host-attested Desktop sessions.

## Owners

RDR-205 (the tuple space), RDR-206 (ack with reply), RDR-208 (session-id
mail addressing and the directory).

## Cited by

- `docs/tuple-space.md` § Session-id mail addressing
- `conexus/skills/mailbox/SKILL.md` (nexus-e4kls)

## Revision History

- 2026-10-22: Created from the 2026-10-21 cross-surface shakeout, finding F3
  (T2 `nexus/f3-sender-identity-analysis-2026-10-21`). Nothing had recorded
  that `from` is unverified; RDR-205 assumed cooperating processes without
  saying so.
- 2026-10-22: F3 step 2 built (nexus-fetre, nexus-ztwp4, nexus-ggmg7). The
  engine stamps `sender_session` on a tuple at INSERT only, from the minted
  session token's session id, never from the request body, and never refuses a
  write for lack of one (a lock's reset of an expired row keeps the first
  stamp, so the stamp names the writer of the stored body and dims). The
  client sends the token as `X-Nexus-T1-Session`; a token the engine does not
  know is 401 and the client drops it and retries, so the write lands
  unstamped. The drain hook (both copies) and the channel push label a message
  `unverified` when its `from` does not match its `sender_session` (UUIDs
  compared case-insensitively), when the stamp is null, or when `from` is an
  agent id (rendered with the session it was sent under, since subagents share
  the parent's token); a row from an engine that sent no `sender_session` key
  gets no label. Null means any of: no live minted session at write time
  (including a stale token the client dropped), a reply row until nexus-g2ckl,
  a row that predates the column, or a tuple written by a hook over raw HTTP
  (no header). NO LABEL IS NOT VERIFICATION: any data-token holder can mint a
  session token for any session id (the session endpoint takes `session_id`
  from the request body), so a match shows only that the writer presented the
  claimed session's token. The tenant stays the security boundary. Scope gap:
  `tuple_in`, `tuple_rd` and `nx tuple` print the raw stamp and no label.
