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


## Awaiting the next release or plugin cut (pinned: v7.32.0)

- `conexus/agents/developer.md` (nexus-yhcxi): the Beads Integration section
  no longer tells the agent to close beads and commit the beads file, which
  contradicted its own Completion Protocol; the composed worktree-developer
  inherits the fix at the next `nx agents install`.

- `conexus/hooks/scripts/routing/_lib.py` (nexus-gjv9b PART 3): the dead
  JSONL append and rotation machinery (`_default_log_path`, `_log_path`,
  `_lock_file`/`_unlock_file`, `_rotate_log_if_oversized`) is deleted.
  `log_routing_event` has written only to the engine's `routing_events`
  table since the 7.32.0 plugin; nothing behavioural changes for a hook
  caller.


