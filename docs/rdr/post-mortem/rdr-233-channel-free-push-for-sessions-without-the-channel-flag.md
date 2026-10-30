# RDR-233 Post-Mortem: Channel-Free Push

**Closed** 2026-10-30 · **Accepted** 2026-10-30 · **Bead** nexus-7hzb2 (closed) · **Shipped** develop 83235a663 and 6a6c4cf85, in the next conexus client release

## What the RDR set out to do

A session launched without `--channels`, which includes every Claude Desktop
Code-tab session, got no push. Mailbox mail arrived only when the user next
typed, board posts never arrived, and the SessionStart line told the session
its mail came over a channel it did not have.

The RDR proposed `nx tuple wait`: a background Bash task that parks on the
engine's `/wait`, exits when a row from another session lands on the session's
mailbox or board, and prints that row so the exit wakes the session. The
SessionStart line reads the launch flags and, for a session without the
channel, says to arm the waiter instead.

## Implementation status

Implemented as planned, in one phase of three steps, all shipped in 83235a663:
`nexus.channel_launch`, `nexus.tuple_wait`, the `nx tuple wait` verb, the
SessionStart wording switch in `nexus.mailbox_arm`, and the docs, including the
`Bash(nx tuple wait:*)` allow rule. The Minimum Viable Validation is the
real-engine journey in `tests/test_tuple_wait.py`; it passes on develop CI.
A live Desktop Code-tab dogfood against the cloud engine ran on 2026-10-22,
before the gate.

Two assumptions stay assumed, acknowledged by Sam at accept: research-9 (re-arm
asks no Bash permission once the allow rule is in place) and research-12 (a
parked waiter survives `/compact` and system sleep). Windows is untested on a
Windows host.

## Implementation vs plan

### As planned

- The waiter, the launch-flag read and the guidance switch.
- Delivery once per row, own posts and claimed mail skipped, a board backlog
  not replayed, a newer waiter superseding an older one.
- Retry with backoff on a transient `/wait` 503, added after the 2026-10-22
  dogfood.

### Diverged

1. **Subagent mail is delivered although it carries the parent's stamp.**
   Subagents share the parent's token, so their rows look like the session's
   own posts. The own-post filter would have dropped them. Approach 3 now
   states the exception (a row whose `from` is an agent id is delivered); it
   was found at gate round 2 (N1) and written in at accept.
2. **The ten real-engine tests became one journey.** Each minted a tenant, and
   develop went red at 83235a663 on the storage-touch ceiling (4971 provisioned
   against 4961). They now run as scenarios of one test that collects every
   failure (6a6c4cf85, ceiling 4962).
3. **The branch was squashed before it landed.** The gate records cite the
   round commits e0923d3c6, b934d3325 and 56e6bd046, which are not on develop.
   They are kept on the local branch `backup/nexus-7hzb2-pre-squash` and listed
   in the cited-sha allowlist.
4. **`--json` lost the wake-loop instruction until the close.** The Risks
   rule is that only a delivery tells the model to run the waiter again. Text
   mode kept it, but `--json` printed only `{reason, notices}`, and no test
   covered `--json` or the superseded and session-changed lines. The close critique found it; the JSON now carries `rerun`
   (true only on a delivery) and `hint` (the line text mode prints), and the
   CLI tests pin every exit in both modes.

### Drift classification

- 1: Missing failure mode.
- 2: Missing cross-cutting concern (the test-tenant budget).
- 3: none; a landing mechanic, recorded so the gate records stay traceable.
- 4: Under-specified architecture (the rule was stated for one output mode).

## Lessons

- A filter keyed on the session's token must ask who else holds that token.
  Subagents do, and the filter is wrong for exactly the mail a parent waits on.
- A test that mints a tenant per method costs one tenant per method on every
  CI shard. Write real-engine scenarios as one journey from the start.
- A safety rule carried by output wording needs a test for every output mode
  the verb has, or a second mode drops it without anything going red.
