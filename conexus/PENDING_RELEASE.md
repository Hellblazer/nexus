# Pending release: plugin changes that are NOT live yet

`.claude-plugin/marketplace.json` pins `plugins[].source.ref` to an immutable
release tag. Claude Code loads this plugin's hooks, commands, skills, and agents
from **that tag**, not from your working tree. So every change below is merged
on `develop` and **inert in every running session** until the next release ships
and users install it.

This file is the acknowledgement ledger for that gap. It exists because the gap
is otherwise invisible: on 2026-07-25 a subagent ran `git stash -u` in a shared
tree and the guard that covers exactly that verb did not fire, because the
coverage had landed hours earlier and the installed plugin was still `v6.18.1`.
Three guards had been merged, closed as "mechanized", and were protecting
nothing.

**Rules, enforced by `tests/test_plugin_release_drift_ledger.py`:**

- Every file under the behavioural surface that differs from the pinned tag MUST
  be listed here. Adding a guard without declaring it fails the suite.
- When a release ships and the pin advances, drift goes to zero and this list
  MUST be emptied. A stale entry also fails the suite, so the ledger cannot
  quietly become fiction.
- Do NOT "fix" a failure by deleting entries. The entry is the honest statement
  that the thing is not yet live.

**Do not use this to justify skipping a release.** If a guard matters enough to
mechanize, it matters enough to ship.

---



## Awaiting the next release or plugin cut (pinned: v7.43.0)

- nexus-6konb.7 — `conexus/hooks/hooks.json` gains a `UserPromptSubmit` block, an
  event this file has never carried. It runs the mailbox drain hook below on every prompt.
  Until a release or plugin cut advances the pin, no running session fires it,
  so RDR-205 mailbox mail is still delivered only by an explicit `nx tuple rd`
  or by a `nx tuple watch` ping that a model chose to act on. Bead
  nexus-6konb.7 (MM-2.2).
- nexus-6konb.7 — `conexus/hooks/scripts/mailbox_drain.py`, new. The deterministic
  consumer of record for RDR-205 mailbox delivery: a zero-timeout `rd` on this session's
  addresses, then `in` + `ack` per row, then render as injected context. The
  Phase 1 watcher pings and never claims; this claims and consumes. Inert until
  the pin advances, and the floor the epic's design rests on does not exist
  until it is live — a session today loses mail the watcher pinged but nobody
  drained. Bead nexus-6konb.7 (MM-2.2).
- nexus-h61dl.11 — `conexus/skills/mailbox/SKILL.md`: renew and reply-in-ack
  rules for RDR-206 Phase 2 (renew at half the lease, reply through
  `tuple_ack(reply=...)` in one transaction instead of a separate `tuple_out`
  then `tuple_ack`), plus two `## Success Criteria` rows. Migrates the old
  cross-instance ack line off `tuple_out` back to your address. Inert until
  the pin advances — a session today still sees the pre-RDR-206 rules. Bead
  nexus-h61dl.11.
