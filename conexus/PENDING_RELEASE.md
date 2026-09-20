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



## Awaiting the next release or plugin cut (pinned: v7.55.0)

- `conexus/hooks/hooks.json`, `conexus/README.md`:
  bead: nexus-17i1n — the three DECIDING hooks move off the `mcp_tool`
  tier onto `nx-hook` verbs, in four entries (`auto-approve` is wired
  twice). An `mcp_tool` hook cannot return a verdict: Claude Code names
  four hook types that carry a decision — `prompt`, `agent`, `command`,
  `http` — and `mcp_tool` is not among them; its documented posture is
  "non-blocking error" and its output is read as context. So
  `pre-close-verification` (deny), `subagent-stop` (block) and
  `auto-approve` (allow, which on PreToolUse and PermissionRequest skips
  a prompt) have all been inert since bead nexus-q02nx.21 rewired them.
  INERT until the next cut, and this is the sharp case for what this
  file is FOR: **the close gate is dead in every running session right
  now**, including the one reading this. 7.55.0 is the pinned tag and
  7.55.0 is the release that broke it, so a `bd close` naming a bead
  with no review-completed marker passes unchecked until the pin
  advances. Merging the fix changes nothing for anyone until then.
  Two other causes of the same silence are NOT addressed here:
  `.nexus.yml` is untracked and resolved from cwd, so `on_close` is
  false in every worktree (bead nexus-634ye, awaiting Sam's call), and
  an unreachable T1 fails open by design. Advancing the pin fixes one
  of the three.
