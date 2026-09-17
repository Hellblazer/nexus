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



## Awaiting the next release or plugin cut (pinned: v7.50.0)

- `conexus/hooks/scripts/auto-approve-nx-mcp.sh`: `mcp__plugin_conexus_nexus__tuple_release` joins the auto-approve allow-list (RDR-211 Step 3, nexus-rplay.9). Until the pin advances, a session on the installed plugin is prompted for permission on the new tool.
- `conexus/hooks/scripts/auto-approve-nx-mcp.sh`: added `tuple_release`, `tuple_subscribe`, `tuple_unsubscribe`, and `tuple_subscriptions` to the auto-approve case list (RDR-211, bead nexus-rplay.11; `tuple_release` was a pre-existing gap from bead nexus-rplay.9, closed in the same edit).
- `conexus/hooks/scripts/auto-approve-nx-mcp.sh`: added `tuple_channel_probe` to the auto-approve case list (RDR-211 Phase 1 Step 3, bead nexus-rplay.10 — the channel gate's probe fallback).
- `conexus/hooks/scripts/mailbox_drain.py`: the per-prompt re-arm (the watcher-lock liveness check and its `nx hook mailbox-arm` re-spawn) is deleted; the drain, claim, render and dead-letter surfacing stay unchanged (RDR-211 Phase 1 Step 3, bead nexus-rplay.14).
- `conexus/skills/mailbox/SKILL.md`: the Monitor-arm instructions and the 30-minute re-arm rule are replaced with the `tuple_subscribe` call and the development-channel launch flag/dialog (RDR-211 Phase 1 Step 3, bead nexus-rplay.14).
- `conexus/skills/mailbox/SKILL.md`: added the board-topic `tuple_subscribe` rule, the once-a-minute board posting convention, and the queue/lock claim lifecycle (`in`, `renew`, `ack`/`nack`/`release`, `ack` refused on a lock) (RDR-211 Phase 1 Step 4, bead nexus-rplay.15).
- `conexus/skills/mailbox/SKILL.md`: the push-delivery rule is amended so a pushed notification is documented as a reference only (subspace, tuple id, claim id) — the body is read on purpose with `tuple_rd`, never carried in the notification (Sam, 2026-09-17, T2 `nexus_rdr/211-decision-push-reference-2026-09-17`; RDR-211 Phase 1 Step 4, bead nexus-rplay.15).
- `conexus/skills/peer-messaging/SKILL.md`: the watcher-ping delivery description is replaced with the channel + drain-hook floor description (RDR-211 Phase 1 Step 3, bead nexus-rplay.14).
- `conexus/skills/peer-messaging/SKILL.md`: "the armed mailbox watcher" is replaced with "the channel push", matching the RDR-211 delivery model (RDR-211 Phase 1 Step 4, bead nexus-rplay.15). Also: the "the tuple space has no lock template, and a new tuple-space consumer needs its own RDR" line is replaced with a description of the `lock/<resource>` template (`in`/`renew`/`release`; `ack` refused) alongside the local file-based capacity locks that stay out of scope, matching `conexus/skills/mailbox/SKILL.md`'s queue/lock rule (RDR-211 Phase 1 Step 4, bead nexus-rplay.19).
