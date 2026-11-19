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
## Awaiting the next release (pinned: v8.4.1)

ONE PATH PER BULLET, on the bullet's FIRST line.

## Deferred to the next client release

- `conexus/agents/deep-research-synthesizer.md` -- the catalog `resolve` example names the corpus content types and says an unknown `corpus` fails the call (GH #1668); the behaviour ships in the client's MCP server, so this line waits for the client release.
- `conexus/hooks/scripts/_identity_lookup.py` -- new stdlib sibling that finds this session's identity (and its unexpired token) from the identity token files, for the hooks below (RDR-253 Step 5, nexus-hczdo.1.7); the identity binding ships in the client, so this waits for the client release.
- `conexus/hooks/scripts/compact_checkpoint.py` -- the PreCompact checkpoint finds the threads entry by its `identity:` line first, and never takes an entry naming another seat (nexus-hczdo.1.7). It also lists the identity's running work rows from `nx identity work list --running --json` (nexus-hczdo.1.9).
- `conexus/hooks/scripts/_tuple_sender.py` -- an identity-shaped `from` is labelled by the identity the stamped session is bound to, not unverified (nexus-hczdo.1.8).
- `conexus/hooks/scripts/mailbox_drain.py` -- the prompt drain also drains `mailbox/<identity>`, sending the identity token on every tuple request of that drain (nexus-hczdo.1.7).
- `conexus/skills/mailbox/SKILL.md` -- documents identity mailboxes, alias resolution, the 7-day rename notice and the identity `from` rule (nexus-hczdo.1.7).
- `conexus/skills/finishing-branch/SKILL.md` -- cleanup asks `nx identity may-stop worktree` first, stops on any nonzero exit, and chains `git worktree remove && nx identity work done` (RDR-253 Step 6, nexus-hczdo.1.9); the verbs ship in the client, so this waits for the client release.
- `conexus/skills/git-worktrees/SKILL.md` -- a new worktree registers itself as the identity's `worktree` work row (RDR-253 Step 6, nexus-hczdo.1.9); the verb ships in the client, so this waits for the client release.
