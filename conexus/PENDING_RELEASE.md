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


## Awaiting the next release or plugin cut (pinned: v7.18.0)

Reviewer-agent guidance only — no hooks, no commands, no executable surface.
Three defect classes that a strict, fully-green gate does not catch (PR #1480).
Until the pin advances, reviews in every running session are still done by the
v7.18.0 agents and will keep missing these.

- `conexus/agents/code-review-expert.md`: nexus-csrto — three additions to the
  shipped-past-green-suites list: a statistic that can exceed its own maximum, a
  quantity obtained by subtracting an assumed constant, and an assumption stated
  in prose that nothing enforces.
- `conexus/agents/test-validator.md`: nexus-csrto — new section for a test that
  MEASURES rather than asserts: gate the setup (coverage, identity, isolation,
  well-formedness), not only the result.
- `conexus/agents/substantive-critic.md`: nexus-csrto — premise check placed
  ahead of every other deconstruction method, plus unenforced-prose-assumptions
  as the critic's highest-yield target.
