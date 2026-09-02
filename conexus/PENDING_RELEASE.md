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


## Awaiting the next release or plugin cut (pinned: v7.26.0)

- `conexus/skills/query/SKILL.md` — bead: nexus-4e75w.4 — describes
  `nx_answer`'s two possible return shapes now that continuation mode
  exists (composed answer, or a reduction instruction the calling
  session executes in-context). No routing change; the nexus-h33x8.6
  narrowing stands. RDR-200 Phase 1b.
- `conexus/skills/using-nx-skills/SKILL.md` — bead: nexus-4e75w.4 —
  same one-paragraph description in the routing table's `nx_answer`
  entry. RDR-200 Phase 1b.
- `conexus/hooks/scripts/auto-approve-nx-mcp.sh` — bead: nexus-4e75w.5 —
  adds `nx_answer_report` to the auto-approve list so the new completion-
  report tool does not prompt on every call while every sibling nx_* tool
  auto-approves. RDR-200 Phase 1c.

All three are INERT until the next release or plugin cut: sessions load the
plugin from the pinned tag, so continuation mode is live in the CLIENT
(shipped 85c79761e) while these descriptions and the auto-approve entry are not yet
loaded by any session.
