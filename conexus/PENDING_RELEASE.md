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



## Awaiting the next release or plugin cut (pinned: v7.51.0)

- `conexus/skills/mailbox/SKILL.md`: the board-topic rule and its checklist line no longer mention a watcher argument; `tuple_subscribe` is the whole call (RDR-211 follow-up, bead nexus-rplay.22, sibling sweep of the coordination-page fix).
- `conexus/skills/peer-messaging/SKILL.md`: the waiting checklist names the channel notification and the drain hook at the next prompt in place of the deleted watcher (RDR-211 follow-up, bead nexus-rplay.22).
- `conexus/skills/mailbox/SKILL.md`: the push-delivery rule names the dialog-free launch form `--channels plugin:conexus@nexus-plugins` with the `allowedChannelPlugins` managed setting beside the development-channels flag (bead nexus-tk2cz).
