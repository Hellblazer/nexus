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

## Awaiting the next release (pinned: v7.0.0)

- `conexus/skills/brainstorming-gate/SKILL.md` — HARD-GATE scoped to work with NO design of record (a locked T2 memo, accepted RDR, or reviewed bead IS the approval; Hal directive 2026-08-02, plugin-intent alignment). Until released, sessions load the unscoped every-project gate from the pin.
- `conexus/skills/using-nx-skills/SKILL.md` — routing + Common Mistakes lines aligned to the scoped brainstorming-gate (same directive).
- `conexus/skills/code-review/SKILL.md` — model table corrected to the agent frontmatter truth (sonnet default, opus escalation; the haiku default was unreachable) + new `## Prompt Rigour` section that `/conexus:phase-review-gate` § references (was dangling).
- `conexus/skills/substantive-critique/SKILL.md` — model table corrected to sonnet default / opus escalation (same unreachable-haiku fix).
