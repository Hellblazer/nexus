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
## Awaiting the next release (pinned: v8.5.6)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/_audience.py` (nexus-sby49.2): new. Answers who a hook runs for: `maintainer()` (account flag file under the OS account's home, or a nexus marker in the checkout), `nexus_repo()`, `account_flag()`, the `remote-test-hosts` file reader and a once-per-session tripwire. Strict on error.
- `conexus/hooks/scripts/routing/_user_git_seatbelt.py` (nexus-sby49.2): new. The git guard's short user-mode deny list (force pushes, `branch -D`, reflog and gc pruning, hook-path and global config writes, discards of uncommitted work).
- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` (nexus-sby49.2): the full rule now runs only for a maintainer; anyone else gets the seatbelt, so a user's subagent can commit, add, push, rebase and merge. `NX_REMOTE_TEST_HOSTS` is no longer read: the exempt ssh hosts come from `~/.config/nexus/remote-test-hosts`.
- `conexus/hooks/scripts/routing/registry.yaml` (nexus-sby49.2): stale fail-open text corrected.
- `conexus/agents/architect-planner.md` (nexus-sby49.2): `bd` steps only where `.beads/` exists; nexus-tool steps skipped when the tools are absent.
- `conexus/agents/code-review-expert.md` (nexus-sby49.2): same wording change.
- `conexus/agents/codebase-deep-analyzer.md` (nexus-sby49.2): same wording change.
- `conexus/agents/debugger.md` (nexus-sby49.2): same wording change.
- `conexus/agents/deep-analyst.md` (nexus-sby49.2): same wording change.
- `conexus/agents/deep-research-synthesizer.md` (nexus-sby49.2): same wording change.
- `conexus/agents/developer.md` (nexus-sby49.2): same wording change, plus the no-commit rule gains "unless your brief tells you to commit" and the linked-worktree clause.
- `conexus/agents/strategic-planner.md` (nexus-sby49.2): same wording change.
- `conexus/agents/substantive-critic.md` (nexus-sby49.2): same wording change.
- `conexus/agents/test-validator.md` (nexus-sby49.2): same wording change.
- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md` (nexus-sby49.2): `bd` steps conditional on `.beads/`; the implementer no-commit line gains the brief and worktree clauses.
- `conexus/resources/agent-shared/RELAY_TEMPLATE.md` (nexus-sby49.2): the `bd list` recovery step is conditional on `.beads/`.
- `conexus/skills/orchestration/SKILL.md` (nexus-sby49.2): the no-commit lines gain the brief and worktree clauses.

## Deferred to the next client release
