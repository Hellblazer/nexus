# RDR-208 Post-Mortem: Session-Id Mail Addressing

**Closed** 2026-09-25 · **Accepted** 2026-09-14 · **Epic** nexus-galkv (31 beads, all closed) · **Shipped** engine-service-v0.1.119 and conexus 7.46.0 (Phases 1 and 2), conexus 7.60.0 (Phase 3 client), engine-service-v0.1.131 with conexus 7.61.0 (Phase 3 engine)

## What the RDR set out to do

Mail between sessions was addressed to the session's `ListAgents` name, and
that name changes on every `/resume`. Four gaps followed: mail to a renamed
session's old name was never read (Gap 1); a name could pass to another
session, because the harness draws names from 256 values per directory (Gap
2); `/clear` stranded mail sent to the previous session id (Gap 3); and names
resolved only on one machine, because the name registry was a local file (Gap
4).

The design: one mailbox per session id, names kept in a tuple-space directory
with a short lease, `mailbox_send` resolving a name to its holder's session id
at send time, and a `/clear` record that lets the new session drain the old
session's mailbox once.

## Implementation status

Implemented. All four gaps are closed:

| Gap | Closed by |
|---|---|
| 1, renamed session | `mailbox_send` resolves the name at send time (`src/nexus/tuple_directory.py`, `resolve_send_address`); mail goes to the session id, which survives a resume |
| 2, name reuse | a name held by two live sessions is refused, naming both holders (`classify_directory_holders`) |
| 3, `/clear` | the cleared record and one-time drain (`conexus/hooks/scripts/mailbox_drain.py`, `_drain_cleared_record`) |
| 4, one machine | the directory is an engine tuple-space template (`directory.yaml`), shared by every session on one engine and tenant |

Phase 3 retired the old address kind in client-then-engine order: 7.60.0
stopped draining instance-name mailboxes after the 7-day retention window, and
engine-service-v0.1.131 refuses `address_kind: instance` on write. Rows already
stored with it stay readable, claimable and ackable.

## Implementation vs plan

### As planned

- The three gate decisions (Sam, T2 `nexus_rdr/208-decision-gate-2026-09-14`):
  a name held by two live sessions is refused; a fork leaves the parent's
  mailbox with the parent; a 300-second lease re-sent every 60 seconds.
- Client before engine for the retirement, each half in its own release, with
  a retention window between R2 and R3.

### Diverged

- **`/branch` fires no SessionStart.** The design assumed a fork announces
  itself. It does not, so the parent's watcher kept running in the fork and
  pushed the parent's mail there. MVV step 6 caught it on 7.46.0; fixed in
  73938b94b (7.46.1), and again for the channel waiter in 9c730421f.
- **A fork's name can fall outside the address charset.** The re-run found a
  fork armed as `listagents (Branch)`; the watcher skipped that subspace and
  exited, dropping the session-id mailbox with it. Fixed in 08298f6db (7.47.0).
- **The Phase 3 client release missed one sender.** 7.60.0 stopped draining
  instance-name mailboxes, but the mailbox skill still told sessions to send a
  raw `tuple_out` with `address_kind: instance` for cross-session
  request/ack. The P3.2 critic found it. Sam chose to ship the fix in the
  paired 7.61.0 release rather than a plugin cut first, so a session on the
  7.60.0 plugin that follows the old text gets a loud refusal until it
  updates. Nothing loses mail silently.
- **The live drain script was untested.** hooks.json runs the plugin copy of
  the drain hook; the tests ran only the wheel copy. The Phase 3
  test-validator found it; cef72ee69 now runs the key cases against both.
- **Local mode was validated with stand-ins.** The MVV ran real Claude Code
  sessions in cloud mode only; local mode used a container harness with
  stand-in claude processes over the real hooks and tools.

## Drift classification

| Divergence | Category |
|---|---|
| `/branch` fires no SessionStart | Unvalidated assumption |
| Fork name outside the address charset | Missing failure mode |
| Skill text still sending `address_kind: instance` after R3 | Missing cross-cutting concern |
| Plugin drain script untested beside the wheel copy | Missing cross-cutting concern |
| Local-mode MVV on stand-ins | Deferred critical constraint |

## What to check first next time

1. **Test harness events on a real session before designing around them.**
   The fork rule rested on a SessionStart that `/branch` never sends; one
   manual `/branch` before the design would have shown it.
2. **When retiring a wire value, grep the docs and skills too, not only the
   code.** A skill that tells a model which JSON to send is a client, and the
   ledger's "no client wire caller" check does not see it.
3. **Two copies of one hook need one test that runs both.** The plugin copy
   exists for CLI-skew reasons and will keep existing; the test now guards the
   copy that actually runs.
