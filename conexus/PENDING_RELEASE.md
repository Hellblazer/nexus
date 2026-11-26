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
- `conexus/agents/architect-planner.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/code-review-expert.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/codebase-deep-analyzer.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/debugger.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/deep-analyst.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/deep-research-synthesizer.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/developer.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/strategic-planner.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` (retrieval plans only); without `.beads/` the planner skips the `bd` steps, puts the task breakdown in its hand-back and, where the conexus nexus tools are available, in the T2 plan record `plan-{name}.md`; the internal ids are removed from the body, nexus-sby49.4.
- `conexus/agents/substantive-critic.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/agents/test-validator.md`: Relay Reception's Bead field also takes a tracking id (D10), nexus-sby49.3. The plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan; the internal RDR and bead ids are removed from the body; where a bead is required it is gated on `.beads/`, nexus-sby49.4.
- `conexus/resources/agent-shared/RELAY_TEMPLATE.md`: the Bead field also takes a tracking id (D10), nexus-sby49.3; a one-line pointer to the orchestration skill's reference.md for what a brief gives a subagent about git, nexus-5y47c.
- `conexus/hooks/scripts/routing/_lib.py`: `log_routing_event` records only under `maintainer()`, so a user's command fragments no longer reach their engine as routing_events rows, nexus-sby49.3.
- `conexus/hooks/scripts/_audience.py`: the settings reader (RDR-259): one `SETTINGS` table, `.nexus/project.json` and `conexus.json` checked and bounded, every problem reported, the strictness keys also read from `HEAD`, the session-root and cwd-checkout copies, `integration_branch`, and `python3 _audience.py show`, nexus-sby49.8.
- `conexus/README.md`: the relay example's Bead field takes a tracking id, nexus-sby49.3.
- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md`: the relay validation and the relay example take a tracking id in the Bead field, nexus-sby49.3.
- `conexus/commands/debug.md`: the relay's Bead field takes a tracking id, nexus-sby49.3.
- `conexus/commands/review-code.md`: the relay's Bead field takes a tracking id, nexus-sby49.3.
- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py`: the maintainer's full guard also denies `git reflog delete` (plan 2.1), nexus-sby49.3; it admits read-only git and verb words in text, chained writes in a worktree git proves, a plain fetch, a detached worktree outside every repo, a scratch repository the command creates, and a push of the worktree's own branch, and its deny text is 1.3 KB where it was 2.9 KB, nexus-5y47c.
- `conexus/hooks/scripts/routing/_git_admit.py`: new module, the admission half of nexus-5y47c: a strict lexer, per-segment shapes and the unchanged guard as the fallback for every segment it does not recognise.
- `conexus/skills/orchestration/SKILL.md`: the implementer dispatch section points to reference.md for what a brief gives a subagent about git, nexus-5y47c; internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/orchestration/reference.md`: a section, Git in an implementer brief (worktree root, the one shape, who pushes), nexus-5y47c; internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/hooks/scripts/routing/README.md`: the git guard's freeze note records the one lift for the six nexus-5y47c fixes (Sam, 2026-11-25).
- `conexus/hooks/audience.yaml`: the audience manifest, one entry per hook registration, module and injected constant; read by the lint and the behaviour leg, nexus-sby49.3; a module entry for `_git_admit.py`, nexus-5y47c.
- `conexus/hooks/scripts/routing/registry.yaml`: each rule carries its `audience`, nexus-sby49.3; the git guard's rationale names the segment admission and its limits, nexus-5y47c.
- `conexus/skills/writing-nx-skills/promotion.md`: the target table gains an Audience column, a hook target is live only with its audience.yaml entry and a behaviour-leg case, and the loss check lists over-reach, nexus-sby49.3.
- `conexus/hooks/scripts/routing/destructive_action_advice.py`: the advice `Row` gains an `audience` field (every row EVERY-USER), nexus-sby49.3.
- `conexus/skills/asks/SKILL.md`: the description no longer cites an RDR id, nexus-sby49.4.
- `conexus/commands/plan-audit.md`: the description no longer cites an RDR id, nexus-sby49.4.
- `conexus/skills/nexus/SKILL.md`: the note table says "the user's own standing rules" where it named a person, and two lines lose an RDR id and a bead id, nexus-sby49.4.
- `conexus/skills/writing-nx-skills/SKILL.md`: the `bd update` pitfall applies only where `.beads/` exists and says what to do without it, nexus-sby49.4.
- `conexus/skills/architecture/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/catalog/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/cli-controller/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/composition-probe/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/decision-drift-review/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/mailbox/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/nexus/reference.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/peer-messaging/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/phase-review-gate/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/plan-first/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/plan-validation/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/rdr-accept-checklist/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/rdr-audit-checklist/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/rdr-create/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/rdr-gate-checklist/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/research-synthesis/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/strategic-planning/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/substantive-critique/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/tumbler-footnotes/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/upgrade/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/skills/why-was-this-written/SKILL.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/hooks/scripts/_session_facts.py`: the unlanded-work check compares against the repository's default branch (`origin/HEAD`, else main, master or develop) and reports -1 when none resolves, nexus-sby49.4.
- `conexus/hooks/scripts/routing/_user_git_seatbelt.py`: the module text names the seatbelt as the guard's default mode and the `subagent_git` setting that turns the strict form on, nexus-sby49.4.
- `conexus/hooks/scripts/compact_checkpoint.py`: the compaction text names the threads board and `bd` only where the project has them, nexus-sby49.4.
- `conexus/hooks/scripts/routing/credential_print_guard.py`: a user's deny and redirect text names no maintainer tool or host, nexus-sby49.4.
- `conexus/hooks/scripts/stop_wake_guard.py`: the stop guard's CI wording is read only in a nexus checkout; a user gets neutral text, nexus-sby49.4.
- `conexus/hooks/scripts/version_lockstep_action.py`: the version-lockstep action's text is neutral outside a nexus checkout, nexus-sby49.4.
- `conexus/resources/agent-shared/ERROR_HANDLING.md`: the plan-library tool is `answer_plan_search`, nexus-sby49.4.
- `conexus/resources/tier-discipline.md`: the plan-library tool is `answer_plan_search` and a saved plan is a retrieval plan, never an implementation plan, nexus-sby49.4.
- `conexus/skills/analyze/SKILL.md`: the Tier-aware discipline line names `answer_plan_search` and a retrieval plan, never an implementation plan, nexus-sby49.4.
- `conexus/skills/deep-analysis/SKILL.md`: the Tier-aware discipline line names `answer_plan_search` and a retrieval plan, never an implementation plan, nexus-sby49.4.
- `conexus/skills/design-to-code-trace/SKILL.md`: the Tier-aware discipline line names `answer_plan_search` and a retrieval plan, never an implementation plan, nexus-sby49.4.
- `conexus/skills/document/SKILL.md`: the Tier-aware discipline line names `answer_plan_search` and a retrieval plan, never an implementation plan, nexus-sby49.4.
- `conexus/skills/knowledge-tidying/SKILL.md`: the Tier-aware discipline line names `answer_plan_search` and a retrieval plan, never an implementation plan, nexus-sby49.4.
- `conexus/commands/continuation.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/commands/create-plan.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/commands/implement.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/commands/rdr-accept.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/commands/rdr-audit.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/commands/rdr-gate.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/commands/research.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/resources/agent-shared/README.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/resources/rdr/TEMPLATE.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/resources/rdr_process/AUDIT-PROMPT.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/resources/rdr_process/INCIDENT-TEMPLATE.md`: internal RDR and bead ids removed from the text a session loads, meaning kept, nexus-sby49.4.
- `conexus/hooks/scripts/mailbox_drain.py`: the mailbox header and the ask-answer frame name no internal id, and the frame says "the user" except in its pinned opening phrase and delimiters, nexus-sby49.4.
- `conexus/hooks/scripts/version_lockstep_hook.py`: the plugin-only-release nudge no longer cites an internal id, nexus-sby49.4.
- `conexus/skills/brainstorming-gate/SKILL.md`: the relay's deliverable names beads only where the project has a `.beads/` directory, nexus-sby49.4.

## Deferred to the next client release
