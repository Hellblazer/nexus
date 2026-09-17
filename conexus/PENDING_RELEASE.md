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
