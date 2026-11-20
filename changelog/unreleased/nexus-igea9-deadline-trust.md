### Fixed

- **The owner stamper's acknowledgement deadlines survive a slow engine start and a stamper fault.** The rebuild of the
  deadline timer after an engine start retries, with a doubling delay capped at five minutes and only for the tenants
  and topics that failed, until one full census completes, instead of making one attempt five seconds after start; the
  first failure logs its stack and the twelfth attempt raises the error-level event
  `board_ack_deadline_rebuild_stuck`. The census now visits every tenant in `nexus.tuple_tenants`, not only those
  holding a token row. A `stamper_error` owner row runs the owner deadline like every other owner row and escalates at
  it. If the deadline's own evaluation throws, or the restart's re-arm does, the engine still escalates, or arms, from
  the current owner row alone (nexus-igea9).
- **A client can no longer forge the engine's owner or clear rows.** The tuple routes (`out`, including
  `mode=replace`, and an `ack` reply) refuse a client write to a `board/ci/<topic>` subspace whose `from` dim is
  `engine`, with a 403 and the code `reserved_dim`; the stamper writes through the repository and is unaffected. The
  owner decision also counts a prior owner or clear row only when `sender_session` is null, which separates it from
  rows a session-bearing client wrote before this release, but a null stamp alone does not prove the engine wrote a
  row: the route refusal is the control (nexus-igea9).
