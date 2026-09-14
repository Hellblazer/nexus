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



## Awaiting the next release or plugin cut (pinned: v7.46.0)

- `conexus/hooks/scripts/mailbox_drain.py` (nexus-galkv.19, RDR-208 MVV step 6): `/branch` runs no SessionStart, so the parent's watcher kept running in the fork and pinging the parent's mail there. A session's first prompt now re-arms at once when no watcher self-stop marker names it, and the spawned `nx hook mailbox-arm` moves the marker to the fork, which stops the parent's watcher and releases its directory entry. The watcher-liveness probe passes `ps -ww`, so a narrow terminal on Linux no longer hides a live watcher.
- `conexus/skills/mailbox/SKILL.md` (nexus-galkv.19): a watcher from before a `/branch` also stops itself.

