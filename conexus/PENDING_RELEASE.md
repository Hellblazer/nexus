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

---



## Awaiting the next release or plugin cut (pinned: v7.53.0)

- `conexus/skills/research-synthesis/SKILL.md` (nexus-3uxbc): its Model
  Selection section said the default is haiku with sonnet escalation, while
  `conexus/agents/deep-research-synthesizer.md` pins `model: sonnet`. Same
  defect shape as the four below, a different model pair, missed by the
  first sweep because that sweep looked only at agents pinning opus.
- `conexus/skills/strategic-planning/SKILL.md` (nexus-3uxbc): its Model Selection
  section said the default is sonnet with opus escalation, while
  `conexus/agents/strategic-planner.md` pins `model: opus`. An agent's own
  frontmatter beats the session default, so the stated default was wrong
  and the escalation advice pointed the wrong way. It now names the
  pinning file and says to pass `model: sonnet` for the cheap end.
- `conexus/skills/architecture/SKILL.md` (nexus-3uxbc): its Model Selection
  section said the default is sonnet with opus escalation, while
  `conexus/agents/architect-planner.md` pins `model: opus`. An agent's own
  frontmatter beats the session default, so the stated default was wrong
  and the escalation advice pointed the wrong way. It now names the
  pinning file and says to pass `model: sonnet` for the cheap end.
- `conexus/skills/debugging/SKILL.md` (nexus-3uxbc): its Model Selection
  section said the default is sonnet with opus escalation, while
  `conexus/agents/debugger.md` pins `model: opus`. An agent's own
  frontmatter beats the session default, so the stated default was wrong
  and the escalation advice pointed the wrong way. It now names the
  pinning file and says to pass `model: sonnet` for the cheap end.
- `conexus/skills/deep-analysis/SKILL.md` (nexus-3uxbc): its Model Selection
  section said the default is sonnet with opus escalation, while
  `conexus/agents/deep-analyst.md` pins `model: opus`. An agent's own
  frontmatter beats the session default, so the stated default was wrong
  and the escalation advice pointed the wrong way. It now names the
  pinning file and says to pass `model: sonnet` for the cheap end.

- `conexus/hooks/hooks.json` (nexus-kdxyv): the SessionStart matcher gains `fork`, so `/branch`
  and `--fork-session` (Claude Code >= 2.1.213 reports them as source
  `fork`) run `nx hook session-start` like a `/clear` does: the session
  marker moves to the fork, no cleared record is written, and the wheel's
  handoff (`_T1_HANDOFF_SOURCES` now includes `fork`) moves the live MCP
  server's channel waiter and directory lease to the fork, so the parent's
  mail stays with the parent instead of being pushed into the fork
  (nexus-kdxyv, RDR-208 Fork paragraph). Inert until installed: with the
  pinned plugin a `/branch` fires no hook at all.
- `conexus/hooks/scripts/mailbox_drain.py` (nexus-kdxyv): the dead `_session_marker_names`
  (unused since RDR-211 nexus-rplay.14; kept only for an e2e grep that
  now reads the matcher above) is deleted. No behaviour change.
