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
## Awaiting the next release (pinned: v8.5.7)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/skills/rdr-close/SKILL.md`: one clause: the Step 6 target is found by its attributes and minted on first write, not rendered, nexus-p2m1x.30.
- `conexus/agents/architect-planner.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/code-review-expert.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/codebase-deep-analyzer.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/debugger.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/deep-analyst.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/deep-research-synthesizer.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/developer.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/strategic-planner.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/substantive-critic.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/agents/test-validator.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3.
- `conexus/resources/agent-shared/RELAY_TEMPLATE.md`: the Bead field also takes a tracking id (D10), nexus-sby49.3; a section says what an implementer relay gives a subagent about git (worktree root, the one shape, who pushes), nexus-5y47c.
- `conexus/hooks/scripts/routing/_lib.py`: `log_routing_event` records only under `maintainer()`, so a user's command fragments no longer reach their engine as routing_events rows, nexus-sby49.3.
- `conexus/hooks/scripts/_audience.py`: three lazy imports carry a `noqa: PLC0415` reason so the byte copy under `src/` passes the repo's ruff job (comment only), nexus-sby49.3.
- `conexus/README.md`: the relay example's Bead field takes a tracking id, nexus-sby49.3.
- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md`: the relay validation and the relay example take a tracking id in the Bead field, nexus-sby49.3.
- `conexus/commands/debug.md`: the relay's Bead field takes a tracking id, nexus-sby49.3.
- `conexus/commands/review-code.md`: the relay's Bead field takes a tracking id, nexus-sby49.3.
- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py`: the maintainer's full guard also denies `git reflog delete` (plan 2.1), nexus-sby49.3; it admits read-only git and verb words in text, chained writes in a worktree git proves, a plain fetch, a detached worktree outside every repo, a scratch repository the command creates, and a push of the worktree's own branch, and its deny text is 1.3 KB where it was 2.9 KB, nexus-5y47c.
- `conexus/hooks/scripts/routing/_git_admit.py`: new module, the admission half of nexus-5y47c: a strict lexer, per-segment shapes and the unchanged guard as the fallback for every segment it does not recognise.
- `conexus/skills/orchestration/SKILL.md`: the implementer dispatch section says what a brief gives a subagent about git, nexus-5y47c.
- `conexus/hooks/audience.yaml`: the audience manifest, one entry per hook registration, module and injected constant; read by the lint and the behaviour leg, nexus-sby49.3; a module entry for `_git_admit.py`, nexus-5y47c.
- `conexus/hooks/scripts/routing/registry.yaml`: each rule carries its `audience`, nexus-sby49.3.
- `conexus/skills/writing-nx-skills/promotion.md`: the target table gains an Audience column, a hook target is live only with its audience.yaml entry and a behaviour-leg case, and the loss check lists over-reach, nexus-sby49.3.
- `conexus/hooks/scripts/routing/destructive_action_advice.py`: the advice `Row` gains an `audience` field (every row EVERY-USER), nexus-sby49.3.

## Deferred to the next client release
