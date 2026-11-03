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
## Awaiting the next release (pinned: v7.94.1)

ONE PATH PER BULLET, on the bullet's FIRST line.
- `conexus/skills/mailbox/SKILL.md` (nexus-mdbds.2.18): the cross-instance request and ack rule points at the mailbox template's doc block (`nx tuple dictionary`, `tuple_registry` with `doc=True`) for what each `kind` value means.
- `conexus/skills/peer-messaging/SKILL.md` (nexus-mdbds.2.18): the mailbox request rule points at the same doc block for the `kind` values.
- `conexus/hooks/scripts/version_lockstep_hook.py` (nexus-emiky): a lockstep marker holding the retry sentinel `retry-nx-upgrade` is skew whatever `nx --version` reports, so the session dispatches the upgrade again.
- `conexus/hooks/scripts/version_lockstep_action.py` (nexus-emiky, nexus-rcngf): with the retry sentinel in the marker the action skips the fast path and the binary step and starts the detached `nx upgrade` again; on a dev/editable tree it replaces the sentinel with the target. The marker now records finished work, not a spawn: the action writes the retry sentinel before it spawns the child, waits on the child, and writes the version itself on exit 0 (compare-and-swap), so it needs nothing from the CLI and works with a CLI that lacks `settle_upgrade_retry`; it spawns nothing while the upgrade lock is held or after three consecutive failed upgrade exits for one target. The retry-mode pre-start target write and its restore are gone.

- `conexus/skills/analyze/SKILL.md` (nexus-egv6q): the tier-discipline link text is `${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md`, an absolute path once rendered.
- `conexus/skills/deep-analysis/SKILL.md` (nexus-egv6q): the tier-discipline link text is `${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md`, an absolute path once rendered.
- `conexus/skills/design-to-code-trace/SKILL.md` (nexus-egv6q): the tier-discipline link text is `${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md`, an absolute path once rendered.
- `conexus/skills/document/SKILL.md` (nexus-egv6q): the tier-discipline link text is `${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md`, an absolute path once rendered.
- `conexus/skills/knowledge-tidying/SKILL.md` (nexus-egv6q): the tier-discipline link text is `${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md`, an absolute path once rendered.
- `conexus/skills/rdr-accept-checklist/SKILL.md` (nexus-egv6q, nexus-3811j): the register fallback uses the braced `${CLAUDE_PLUGIN_ROOT}`, which Claude Code substitutes, so a brief carries an absolute path. A brief or relay names the RDR directory by the `<RDR dir>` placeholder (the absolute path), never `$RDR_DIR`.
- `conexus/skills/rdr-close/SKILL.md` (nexus-egv6q): the register fallback uses the braced `${CLAUDE_PLUGIN_ROOT}`, which Claude Code substitutes, so a brief carries an absolute path.
- `conexus/skills/rdr-create/SKILL.md` (nexus-egv6q): plugin template and register paths use the braced `${CLAUDE_PLUGIN_ROOT}`, which Claude Code substitutes.
- `conexus/skills/rdr-fix-checklist/SKILL.md` (nexus-egv6q, nexus-3811j): the register fallback uses the braced `${CLAUDE_PLUGIN_ROOT}`, which Claude Code substitutes, so a brief carries an absolute path. A brief or relay names the RDR directory by the `<RDR dir>` placeholder (the absolute path), never `$RDR_DIR`.
- `conexus/skills/rdr-gate-checklist/SKILL.md` (nexus-egv6q, nexus-3811j): the critic's register criterion is self-contained and the register is named as `$RDR_DIR/REGISTER.md` with the `${CLAUDE_PLUGIN_ROOT}` fallback, never a bare `resources/` path. A brief or relay names the RDR directory by the `<RDR dir>` placeholder (the absolute path), never `$RDR_DIR`.
- `conexus/skills/rdr-research/SKILL.md` (nexus-egv6q, nexus-3811j): the register fallback uses the braced `${CLAUDE_PLUGIN_ROOT}`, which Claude Code substitutes, so a brief carries an absolute path. A brief or relay names the RDR directory by the `<RDR dir>` placeholder (the absolute path), never `$RDR_DIR`.
- `conexus/skills/research-synthesis/SKILL.md` (nexus-egv6q): the tier-discipline link text is `${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md`, an absolute path once rendered.
- `conexus/skills/why-was-this-written/SKILL.md` (nexus-egv6q): the tier-discipline link text is `${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md`, an absolute path once rendered.
- `conexus/skills/writing-nx-skills/SKILL.md` (nexus-egv6q): the relay template is named by `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY_TEMPLATE.md`.

- `conexus/agents/architect-planner.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/code-review-expert.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/codebase-deep-analyzer.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/debugger.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/deep-analyst.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/deep-research-synthesizer.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/developer.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/strategic-planner.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/substantive-critic.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/agents/test-validator.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/commands/analyze-code.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/commands/create-plan.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/commands/debug.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/commands/implement.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/commands/research.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/commands/review-code.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/commands/test-validate.md` (nexus-egv6q): its RELAY_TEMPLATE.md and CONTEXT_PROTOCOL.md links read `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, which Claude Code substitutes; a `../` link in an agent or command prompt has no base directory to resolve against.
- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md` (nexus-egv6q): the agent-link snippets use `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, and maintainer notes name repo paths.
- `conexus/resources/agent-shared/MAINTENANCE.md` (nexus-egv6q): the agent-link snippets use `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, and maintainer notes name repo paths.
- `conexus/resources/agent-shared/README.md` (nexus-egv6q): the agent-link snippets use `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, and maintainer notes name repo paths.
- `conexus/resources/agent-shared/RELAY_TEMPLATE.md` (nexus-egv6q): the agent-link snippets use `${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/`, and maintainer notes name repo paths.
- `conexus/skills/rdr-audit-checklist/SKILL.md` (nexus-egv6q): the incident template is named by `${CLAUDE_PLUGIN_ROOT}/resources/rdr_process/INCIDENT-TEMPLATE.md`, not a repo-only path.
- `conexus/skills/composition-probe/SKILL.md` (nexus-4deer, nexus-3811j): the Maven runner is `<mvn>`: `scripts/mvnw-leased.sh` only where the project has it (the nexus repo, Maven under `service/`), else `./mvnw` or `mvn`; the chosen runner is passed into the pinned subagent prompt.
- `conexus/commands/continuation.md` (nexus-4deer): the commit-scope audit runs `tests/e2e/lib/commit_scope_audit.sh` only when the project has it, with a `git diff --name-only` fallback elsewhere.
- `conexus/skills/orchestration/SKILL.md` (nexus-4deer): the review-round cap is named as the nexus wheel's `review-rounds.toml` table with the cap stated inline, not a bare file an agent in another project cannot open.
- `conexus/skills/decision-drift-review/SKILL.md` (nexus-4deer): the plan authoring guide is a GitHub link, not a `docs/` path an agent in another project cannot open.
- `conexus/skills/nexus/reference.md` (nexus-4deer): the collection rule is a GitHub link, not `docs/collections.md`.
- `conexus/skills/orchestration/reference.md` (nexus-4deer): the collection rules are a GitHub link, and the review-round cap is stated (2 for a code review) with the nexus wheel's `review-rounds.toml` named as its source.
- `conexus/skills/tumbler-footnotes/SKILL.md` (nexus-4deer): the flag reference is `nx catalog footnotes --help`, not `docs/cli-reference.md`.
- `conexus/skills/upgrade/SKILL.md` (nexus-4deer): the migration runbook is a GitHub link, with the import order stated inline for an agent with no web access.

## Deferred to the next client release
