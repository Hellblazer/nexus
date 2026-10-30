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
## Awaiting the next release (pinned: v7.89.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `sn/hooks/scripts/subagent_start.py` (nexus-j6un5.5): drops the Serena and Context7 sections for a conexus agent whose frontmatter denies that server.
- `conexus/skills/architecture/SKILL.md` (nexus-j6un5.5): Serena symbol discovery is main-session work before the dispatch, since architect-planner denies the shared Serena.
- `conexus/agents/architect-planner.md` (nexus-j6un5.5): frontmatter denies the shared Serena server, about 10K tokens off every dispatch's context.
- `conexus/agents/code-review-expert.md` (nexus-j6un5.5): frontmatter denies the shared Serena server, about 10K tokens off every dispatch's context.
- `conexus/agents/debugger.md` (nexus-j6un5.5): frontmatter denies the shared Serena server, about 10K tokens off every dispatch's context.
- `conexus/agents/deep-analyst.md` (nexus-j6un5.5): frontmatter denies the shared Serena server, about 10K tokens off every dispatch's context.
- `conexus/agents/developer.md` (nexus-j6un5.5): frontmatter denies the shared Serena server, about 10K tokens off every dispatch's context.
- `conexus/agents/strategic-planner.md` (nexus-j6un5.5): frontmatter denies the shared Serena server and Context7, about 10K tokens off every dispatch's context.
- `conexus/agents/substantive-critic.md` (nexus-j6un5.5): frontmatter denies the shared Serena server, about 10K tokens off every dispatch's context.
- `conexus/agents/codebase-deep-analyzer.md` (nexus-j6un5.6): runs on Haiku 5.5 instead of Sonnet 5.5. Denies the shared Serena server and Context7 (nexus-j6un5.5).
- `conexus/agents/deep-research-synthesizer.md` (nexus-j6un5.6): runs on Haiku 5.5 instead of Sonnet 5.5. Denies the shared Serena server (nexus-j6un5.5).
- `conexus/agents/test-validator.md` (nexus-j6un5.6): runs on Haiku 5.5 instead of Sonnet 5.5. Denies the shared Serena server and Context7 (nexus-j6un5.5).
- `conexus/commands/rdr-audit.md` (nexus-j6un5.6): the audit's dispatch step passes `model: "sonnet"` to deep-research-synthesizer, which now defaults to haiku.
- `conexus/skills/codebase-analysis/SKILL.md` (nexus-j6un5.6): names haiku as codebase-deep-analyzer's model.
- `conexus/skills/rdr-audit-checklist/SKILL.md` (nexus-j6un5.6): the classification dispatches deep-research-synthesizer with `model: "sonnet"` (header, step 5 and Agent Invocation), since the agent now defaults to haiku.
- `conexus/skills/rdr-research/SKILL.md` (nexus-j6un5.6): names haiku as both agents' model.
- `conexus/skills/research-synthesis/SKILL.md` (nexus-j6un5.6): haiku is the default; multi-source synthesis passes `model: "sonnet"`.
- `conexus/skills/orchestration/SKILL.md` (nexus-j6un5.4): mechanical beads dispatch the developer with `model: "haiku"`, labelled `dev-model:haiku` or `dev-model:sonnet` for the comparison; a failed Haiku round is re-dispatched on Sonnet. The debugger note says subagents have no Serena, so symbol tracing happens in the main session (nexus-j6un5.5).


## Deferred to the next client release

- `conexus/commands/setup.md` (nexus-tr1rj.18): the setup command's install-size rows and slim line carry the figures re-measured on 7.89.0, whose PDF support is MinerU and torch alone (Local about 0.8 GB down, 2.6 to 2.9 GB on disk; Cloud and CLI only about 0.5 GB down, 1.9 to 2.2 GB on disk; PDF support 0.27 GB down on macOS, 0.37 GB on Linux). Deferred because the same bead changes user-facing strings in `src/`.
- `conexus/launch/nx_standin.py` (nexus-tr1rj.18): the stand-in's size rows and slim description carry the same re-measured figures as the real server's `src/nexus/mcp/setup_tools.py`. Deferred because the same bead changes user-facing strings in `src/`.
- `conexus/launch/nx_setup.py` (nexus-b0y6w): the setup credentials page listens with a backlog of twice its connection cap (a full backlog is a suspected, not reproduced, contributor to a Windows CI flake). Deferred because the same bead changes `src/` and `service/`.
- `conexus/skills/mailbox/SKILL.md` (nexus-7hzb2, RDR-233): a session launched without the channel flag runs `nx tuple wait` with `run_in_background` and re-runs it only when its output says to; the no-poll-loop rule now covers a loop of the session's own beside the channel, the drain hook and this verb; names `--force` for a flagged session whose channel never registers and the `Bash(nx tuple wait:*)` allow rule. The verb and the SessionStart line that asks for it ship in the wheel, so this waits for the client release.
