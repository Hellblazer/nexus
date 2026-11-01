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
## Awaiting the next release (pinned: v7.91.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/compact_checkpoint.py` (nexus-3lqxv): new stdlib-only script, three modes. `pre` (PreCompact) writes the session's `threads` board entry (exact title match) first, then the working state (`nx command-context continuation`: branch, HEAD, in-progress beads, uncommitted files, open PRs, recent commits) to `<config>/compact-checkpoints/<session_id>.md`, mode 0600, and never vetoes; `post` (PostCompact) appends the compact summary and logs a warning when it names none of the board title, the checkpoint path or an in-flight bead, and archives and stubs a checkpoint whose `written:` stamp is older than 30 minutes instead of refreshing it; `inject` (SessionStart `compact` only) re-injects the checkpoint, capped near 8 KB, judged fresh by its `written:` stamp (30 minutes) and labelled with its age and trigger, from a file read alone.
- `conexus/hooks/hooks.json` (nexus-3lqxv): adds a PreCompact entry (`compact_checkpoint.py pre`, 45 s), a PostCompact entry (`post`, 10 s) and a separate SessionStart group with matcher `compact` (`inject`, 35 s: the 30 s cold-start allowance every SessionStart entry carries plus its own work); the existing SessionStart group is unchanged.

## Deferred to the next client release
