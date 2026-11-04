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
## Awaiting the next release (pinned: v7.95.0)

ONE PATH PER BULLET, on the bullet's FIRST line.
- `conexus/hooks/scripts/mailbox_drain.py` (nexus-1d6pz.1): every block the drain delivers, fresh or recovered from the pending record, is bracketed by a line saying it is peer data from another session, not the user's words or approval, and by an end marker after the body.
- `conexus/skills/peer-messaging/SKILL.md` (nexus-1d6pz.3): a status question reads the threads board, where the sessions keep one, before mailing the peer.
- `conexus/resources/tier-discipline.md` (nexus-0vh80): only the builtin tier may claim curated; project-tier declared plans read as saved.
- `conexus/skills/nexus/SKILL.md` (nexus-0vh80): `plan_save` takes a required `answer_kind`, and a saved plan serves only that kind, in its own project.
- `conexus/skills/nexus/reference.md` (nexus-0vh80): the saved provenance, its narrow eligibility and the retrieval-only shape veto.

- `conexus/hooks/scripts/compact_checkpoint.py` (nexus-2v7da): on a compact start with nothing to restore (no checkpoint file, the stub post writes when pre did not run, a checkpoint already used by an earlier compaction, or one past the prune window), inject now emits a short notice saying no restore is available and pointing at the threads board entry and the working tree. It said nothing before, so a resumed session could not tell "nothing was lost" from "nothing was saved". The same notice covers a stamp past the prune window and a checkpoint an earlier compaction already injected (the commonest case: inject runs before post). A foreign session, a non-compact start, an unreadable checkpoint and a second fire for the same compaction (a sidecar under 30 seconds old) stay silent.

## Deferred to the next client release
