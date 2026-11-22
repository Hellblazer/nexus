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
## Awaiting the next release (pinned: v8.5.5)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/compact_checkpoint.py`: RDR-253 Phase 2: the checkpoint finds the session's threads board entry by its `identity:` line only (the identity a token file binds the session id to); an entry with a `session:` line and no `identity:` line is not found, and a session with no token file finds none. The exact-title fallback is kept, taken only when that entry's own `identity:` line matches. The no-restore notice no longer names the `session:` line. The checkpoint file's own `- session:` line is kept as information (nothing reads it back as a key).
- `conexus/hooks/scripts/_identity_lookup.py`: docstring only: a hook with no token file has no identity and finds no board entry (RDR-253 Phase 2).
- `conexus/skills/asks/SKILL.md`: the refusal list names the `identity:` line, not the `session:` id (RDR-253 Phase 2).
- `conexus/hooks/scripts/mailbox_drain.py`: drains only `mailbox/<identity>`, never `mailbox/<session id>` (nexus-hczdo.2.1, RDR-253 Phase 2)
- `conexus/skills/mailbox/SKILL.md`: mail is addressed by identity id or seat name; the session mailbox, the `directory/<name>` lease and `nx tuple directory` are gone (nexus-hczdo.2.1)
- `conexus/skills/peer-messaging/SKILL.md`: a send to a peer goes to its identity id or seat name, never a session id or `ListAgents` name (nexus-hczdo.2.1)

## Deferred to the next client release
