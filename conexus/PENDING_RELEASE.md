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
## Awaiting the next release (pinned: v8.0.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/skills/writing-nx-skills/SKILL.md`
  nexus-qo7ie.35: a "Promoting a Memory File" pointer section (RDR-240 Technical Design 9); the description now names promoting a memory file, and the section points at promotion.md. Its script target needs `procedure_run`, which shipped in conexus 8.0.0.
- `conexus/skills/writing-nx-skills/promotion.md`
  nexus-qo7ie.35: new; the promotion recipe the skill loads on demand (moved out of SKILL.md to fit its context budget).
- `conexus/skills/orchestration/reference.md`
  nexus-qo7ie.35: the Skill Routing line for `/conexus:writing-nx-skills` says it also covers promoting a memory file.
- `conexus/launch/nx_setup.py`
  nexus-mjmjl (first-run F1): a running setup step rewrites its status every 30 s with the time so far and the last setup.log line, so setup_status no longer reads "running: build" for up to an hour.
- `conexus/launch/nx_standin.py`
  nexus-mjmjl (first-run F1): the stand-in's build wait text says a build can take up to an hour on a cold machine, equal to setup_tools.py's copy.

## Deferred to the next client release

- `conexus/skills/peer-messaging/SKILL.md`
  nexus-1d6pz.11.6: one bullet on waiting for your push's CI by the wake (a --done subscription with the channel, `nx ci wait <sha>` without it), never a Monitor or a poll (deferred: the notice sha it names is nexus-1d6pz.11.3's src/ change, not in v8.0.0); a peer's sha still matters because window beads close on the tip.
