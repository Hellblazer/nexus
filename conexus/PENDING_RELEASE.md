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

**Deferring a straddling entry (nexus-2x3qy, nexus-rbmja.10).** A plugin-only
release cherry-picks commits from `develop` onto a branch off the last client
tag (`release` skill, "Plugin-only fast lane"). The eligibility gate reads the
bead ids named in those commits and refuses the release when any other commit
on `origin/develop` since the tag that names the same bead touches a path the
lane does not ship (`src/`, `service/`, `scripts/`, `.github/`, `mcpb/`,
`conexus/plans/`, `conexus/daemon/`, release machinery under `tests/`, and so
on). Attribution is by commit message, matched on the whole bead id, so a
commit that does not name its bead is not seen by that check. To keep a
straddling entry out of the release, move it under
`## Deferred to the next client release` below and do not cherry-pick its
commits, so the whole bead ships together at the next normal release. A
deferred entry is exempt from the release-window "ledger must be empty" rule
above -- still declared there is correct, not stale -- and stays exactly where
it is until moved back deliberately.

---
## Awaiting the next release (pinned: v7.98.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/routing/destructive_action_advice.py`
  nexus-qo7ie.36: new table-driven PreToolUse hook, ADVISE mode only (never denies): eight destructive-action shapes (pattern kills, bulk or output-hiding branch deletes, stash with more than one worktree, background job piped to tail/head/grep, whole-disk scans, glob deletes under a shared temp root, hand-run hook verbs, editing a script a live shell runs), each row naming its rule and the safe alternative.
- `conexus/hooks/hooks.json`
  nexus-qo7ie.36: registers `destructive_action_advice.py` on `Bash|Edit|Write|MultiEdit` (PreToolUse, 5 s).
- `conexus/hooks/scripts/routing/registry.yaml`
  nexus-qo7ie.36: the `destructive_action_advice` rule entry.
- `conexus/hooks/scripts/routing/README.md`
  nexus-qo7ie.36: the cap table counts the third Bash hook and states that new destructive-action shapes get a table row, not a hook.
- `conexus/skills/orchestration/SKILL.md`
  nexus-qo7ie.36: the shared-host clauses every developer, test or debugger brief carries (delete scope, no pattern kills, hook verbs only in pytest or a scratch HOME, exact scan roots), a "Destroys" item in the implementer checklist, a deleting-consumer addition to Consumers, a disturbed-run rule under the VERIFY convention, and a Destroys suspect for first-pass reviewers.
- `conexus/skills/peer-messaging/SKILL.md`
  nexus-qo7ie.36: "Sharing One Machine" names what an action destroys before it touches shared state.
- `conexus/skills/nexus/SKILL.md`
  nexus-qo7ie.36: run long nx commands with full output to a log and filter when reading.
- `conexus/skills/mailbox/SKILL.md`
  nexus-qo7ie.36: read the session marker's contents before assuming a `/clear` stopped the watcher.

## Deferred to the next client release
