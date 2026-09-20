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

- `conexus/.mcp.json`:
  bead: nexus-2xso4 — the `env` block on both the nexus and
  nexus-catalog servers set `CLAUDE_PLUGIN_ROOT` to the literal string
  `${CLAUDE_PLUGIN_ROOT}`. Claude Code does not expand `${...}` inside
  an MCP server's `env`, so every `nx-mcp` process carried that text as
  the value — measured on all six such processes on one box across
  three repositories, so every conexus user. Worse than unset: a
  non-empty literal is truthy, so callers took the env branch and built
  a path that cannot exist while the documented unset fallback never
  ran. The block is deleted; there is no correct value to substitute,
  and the sibling sn plugin has always shipped without one.
  INERT until the next cut, but harmless to wait for: the hooks this
  broke were fixed by porting them into the wheel, and the surviving
  in-server reader goes through `plugin_root()`, which now rejects the
  literal and falls back to the checkout. So deleting the block changes
  no behaviour today — it removes the trap for the next thing that
  reads that variable.

- `conexus/hooks/scripts/read_verification_config.py`:
  bead: nexus-634ye — the same gate had a SECOND, independent off-switch,
  and the fix above does not touch it. `.nexus.yml` is gitignored by
  design (`docs/configuration.md`: "It is gitignored by default"), so
  there is one per repo and it sits in the primary checkout; the reader
  resolved it from the process cwd, so every linked worktree got DEFAULTS
  — `on_close: false`, gate off — from the day this project moved to
  one-session-one-worktree. It now resolves from the git COMMON dir, so
  every worktree finds the primary's file, which is the same reasoning
  the engine build lease and the stamped-jar cache already use.
  INERT until the next cut, like everything else here.

  **Read those two entries together before concluding what a cut buys.**
  The gate needs BOTH to be live: the first makes the harness listen to
  the verdict, the second makes the gate armed enough to have one. Either
  alone leaves it silent, and the two fail identically, which is how one
  hid behind the other during this investigation. A third path, an
  unreachable T1, also allows — but that one was always loud (it stamps
  the ids `unverified` and says so) and is a deliberate fail-open, not a
  defect. The `on_close: false` path is now loud too, in the wheel rather
  than in the plugin, so a session on an old pin with a new wheel will at
  least be told why nothing was checked.
