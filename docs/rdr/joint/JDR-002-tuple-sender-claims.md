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

`TupleHandler` reads no session for authorization except on a mailbox whose
address is an identity id (`mailbox/i<digits>`): there the owner's `in`, `inp`,
`ack`, `nack`, `renew` and `release` need the request's identity token
(RDR-253 Step 2; Revision History, 2026-11-18). A send (`out`), `rd`, `rdp` and
`wait` are unchanged. Any holder of the
tenant's data token can therefore read every mailbox, claim any unclaimed
row under any claimant, and write any dim. These fields are **claims, not
authenticated identity**:

- a tuple's `from` and `correlation_id` dims;
- `claimant` on `in`/`inp`, which is also rendered on `rd`;
- a `directory/<name>` row's `session_id`.

A reader that acts on one of these is trusting the sender, not the engine.
On an identity's own mailbox `claimant` is still a claim; the check there is
on the identity token, not on `claimant`.

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
  resolves on the server (a Claude Code server whose T1 has not published
  its id yet, or Claude Desktop and Cowork), because nothing exists there to
  compare against. This guards against a confused model, not a hostile
  caller.
- On Claude Desktop and Cowork the guard is a no-op. The conversation does
  have a session id (its container carries `CLAUDE_CODE_SESSION_ID`), but the
  Desktop-spawned nx-mcp on the Mac never sees it (F17, nexus-wkjqm). So any
  session-shaped `from_address` passes there, another live session's
  included. Protection on that surface is label-only: such a send carries no
  session token, the engine stamps `sender_session` null, and the drain hook
  and channel mark it `unattested` (engine-service-v0.1.163 and conexus
  7.84.0). A bridge-carried session id would arm both this guard and T1 on
  that surface, as a host-asserted label, not a credential.
- Raw `tuple_out` and the dims of a `tuple_ack` reply are not guarded.

Built in engine-service-v0.1.163 and conexus 7.84.0: a server-written
`sender_session` on `out` (and on an ack's reply row), taken from the
request's minted T1 session token and `null` without one, rendered on `rd`,
so the drain hook and the channel label mail whose `from` the stamp does not
confirm (F3 step 2, nexus-fetre, nexus-ztwp4, nexus-g2ckl, nexus-ggmg7). It
is a label, never a refusal. The refusal RDR-253 adds is of the claim
operations on an identity's mailbox, not of the stamp.

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
  (including a stale token the client dropped), a reply row written by an
  engine before nexus-g2ckl (v0.1.163 and later stamp the acker's session on
  an ack reply row), a row that predates the column, or any raw-HTTP writer
  (no header). A missing `from` gets no label: there is nothing to compare.
  The label rides with or after the engine that carries nexus-g2ckl: an
  engine at v0.1.162 or earlier renders no stamp, so nothing is labelled,
  and only a build that stamps `out` rows but not replies (none was
  released) would read every ack reply unverified. A session id appears in a
  label only when the stamp is a canonical UUID; the stamp is attacker-chosen
  text, so any other stamp reads a bare `unverified`. The channel text words
  the cases apart (mismatch, null, agent id) and tells the reader not to act
  on a shared-state request on the sender's word alone. NO LABEL IS NOT VERIFICATION: any data-token holder can mint a
  session token for any session id (the session endpoint takes `session_id`
  from the request body), so a match shows only that the writer presented the
  claimed session's token. The tenant stays the security boundary. Scope gap:
  `tuple_in`, `tuple_rd` and `nx tuple` print the raw stamp and no label.
- 2026-10-22: Label words revised by Sam (nexus-ohvpe, nexus-syylf; T2
  `nexus/decision-sender-label-table-2026-10-22`). A null stamp reads
  `unattested`, still a label, so a writer that omits the session header is
  still marked; an agent-id `from` under a canonical-UUID stamp reads
  `agent mail, sent under session <S>`, since the mailbox skill tells
  subagents to send that way; `unverified` is kept for a `from` that differs
  from a recorded session. A CI board post (a board-ci token is not known to
  mint a session; inferred, not measured) reads `unattested` like any null stamp: the subspace name is not
  attestation. The drain hook prints a one-line legend for the three marks
  (nexus-565ds). NO LABEL IS NOT VERIFICATION is unchanged.
- 2026-10-22: the `from_address` guard is recorded as a no-op on Claude Desktop and Cowork, where protection is the `unattested` label only; the conversation's own session id exists but does not reach the Mac nx-mcp (F17). Requested by the Cowork session, correlation F3-plan.
- 2026-11-18: RDR-253 Step 2 (nexus-hczdo.1.2) adds identity tokens and four fenced families (drafted 2026-11-17; Sam approved it as drafted 2026-11-18). The engine now authenticates one more thing on the tuple path: an identity token, sent as `X-Nexus-Identity-Token` beside the bearer, which names an identity and one generation of it (live one hour; minted by `POST /v1/identity/<id>/bind` for start, resume, takeover, clear, fork and refresh, and by an ephemeral or delegated create; stored in `nexus.identity_tokens`, one insert per mint, so no mint invalidates an earlier token). An expired or unknown token is 401 (client: re-read the token file once). To fence a write is to refuse it because it comes from a process that no longer holds the seat. Two checks, in this order: ownership (the token's identity is the family's owner; else 403 `not_owner`) and generation (the token's generation is the owner's current one; else 409 `generation_superseded`, a code no other 409 of the engine uses, including `generation_conflict`, which is the lost compare-and-set on start). A write with no token is 403 `identity_token_required`. The one exception, keyed on the target: a write to a family whose owner's current binding is `unleased` (a Desktop seat) is accepted with no token and marked with the response header `X-Nexus-Unattested: true` and a log line; a token that names another identity is still refused. Reads are never checked, a delegated child's included. The four families:
  1. Mailbox `mailbox/i<digits>` in `TupleHandler`: the owner's `in`, `inp`, `ack`, `nack`, `renew`, `release` (ack, nack, renew and release name only a claim id, so the engine looks up the live claim's subspace). A send is open to any caller, with or without a token, a superseded one included.
  2. Membership, the `identity_memberships` table (not a tuple template; the memberships left the tuple space at Step 1), routes `POST /v1/identity/<id>/memberships` (join, role change) and `DELETE /v1/identity/<id>/memberships/<group>` (leave). The `/work` routes stay unfenced: work is a label.
  3. Threads entry, T2 project `threads` in `MemoryHandler`: `put` (with `replaces`), `put_or_merge`, `merge`, `import`, `import_batch`, `delete` (by title or id), `restore`, rollup `summaries` (its sources). The check reads the stored entry (a quarantined one included): an entry carrying an `identity: <id>` line needs that identity; a write may not remove or change the line (403 `identity_line_immutable`); an entry with no line may be given one only by the identity it names; `expire` and `reap` skip such entries. In project `threads`, `put_or_merge` skips its Jaccard scan and is a plain upsert (the scan could append the caller's content to another identity's fenced entry).
  4. Identity scratch, T1 scope `session_id = identity:<id>` in `ScratchHandler`: `put`, `flag`, `unflag`, `delete`; `session/close` returns 0 for the scope and every sweep (the request one and the scheduled one) excludes it.
  Also: T2 rows written with an identity token record the identity in `memory.identity` (a label; nothing reads it for a decision); `bind` events clear, fork, refresh, end and `renew` from a superseded generation (expected < current) answer 409 `generation_superseded` in place of `generation_conflict` (start, resume, takeover, release and gone keep `generation_conflict`).
  The `sender_session` stamp of an identity token (the open item of nexus-hczdo.1.2, option (b) of the plan audit): it is the session id of the identity's newest binding row under the token's generation that names one, read at write time (`identity_bindings`, order by seq desc), never a session id frozen into the token. A tuple written after `/clear` with the token the MCP server already holds is stamped with the new session id, because `/clear` appends a row with the new id under the same generation; a Bash child from before the `/clear` is stamped the same way. The stamp maps back to the writer's identity and generation through `GET /v1/identity/sessions/<sid>`, which is what RDR-252's owner stamper reads. With both an identity token and a minted session token on a request, the identity's session wins; with only the minted token the rule is unchanged. A send made with a superseded token is accepted and stamped with its own generation's last session. The stamp is still a label, never a refusal, and an ack's reply is stamped the same way.
  Client rule (RDR-253 Step 3): on a fenced family a client never retries without a token. This replaces, for those families, the 401 fallback of the 2026-10-22 entry (drop the session header and retry): a 401 on an identity token means expired (re-read the token file once, then refresh through bind), and 409 `generation_superseded` means stop writing identity state and name the holder.
  Unchanged: any holder of the data credential can still bind any seat with the data events, as any can mint a session token; the fence guards against a confused or superseded process, not a hostile one, and the tenant is still the security boundary. NO LABEL IS NOT VERIFICATION is unchanged for every subspace the fence does not cover. The drain hook's `unattested` label (a null stamp) is a different thing from the `X-Nexus-Unattested` response header (an accepted tokenless write to an unleased seat's family).
- 2026-11-19: RDR-252 Step 1 (nexus-igea9.5, on develop at d357e1e6f) adds a fifth fenced family, group records: table `group_records` (tenant RLS), routes `POST /v1/group` (form or re-form) and `POST /v1/group/<name>/set|disband` (reads `GET /v1/group/<name>` are never checked). The same two checks as the four families, against the leader: for an existing record the token's identity is the stored `leader` and its generation is that identity's current one, re-checked inside the write transaction; for a first write, and for a re-form of a disbanded name, the writer is the leader it names and becomes the only member. A tokenless write is 403 `identity_token_required` even when the leader is `unleased`: the Desktop exception does not extend to group records. The one tokenless group write is `POST /v1/group/<name>/override` (set a leader or end the group), which takes only the tenant data credential and is audited in `gc_audit` as `group_override`. The `board-ci` scope narrows: a board-ci token may still only `out` to `board/ci/*`, but never to a `--owner` or `--push` topic (403), checked on both the subspace segment and `keys.topic`, which must agree; those topics are written by the engine's owner stamper (RDR-252 Step 3, `from=engine`), by sessions' acks, and by the develop push script.
- 2026-11-22: Two engine checks on `board/ci` now go past a label (Sam chose this revision, ask `jdr002-revision`). The first is in engine-service-v0.1.199 and later, the second in v0.1.200 and later; v0.1.200 is live in the cloud since 2026-11-21 15:29Z.
  1. A client write whose `from` dim is `engine` on a `board/ci` topic is refused with 403 `reserved_dim`, on `out` (also `mode=replace`) and on an ack's reply row (3c4880bcf9). A null `sender_session` does not prove the engine wrote a row, so `from=engine` is reserved for the owner stamper, which writes through the repository.
  2. A `kind=ack` write to `board/ci/<topic>--owner` has its `from` set by the engine to the identity its own `sender_session` maps to (a delegated session's parent identity): filled when absent, overwritten and logged when it differs, nothing refused; a stamp that maps to nothing leaves `from` as sent (b308e6dc97).
  So the Decision lines "Raw `tuple_out` and the dims of a `tuple_ack` reply are not guarded" and "It is a label, never a refusal" (said of the `sender_session` stamp, and read as covering `from`), no longer hold for these two cases. Everywhere else they stand: `from` stays a claim, and the tenant is still the security boundary.
