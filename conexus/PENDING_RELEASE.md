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


## Awaiting the next release or plugin cut (pinned: v7.35.0)

- `conexus/skills/rdr-gate/SKILL.md` — nexus-g7zgw: Layer 0 fires after PASSED gates too and sweeps the critic's `Sites:` list; the diff-scoped Fix check layer; round-numbered Gate Aggregation (any Critical in rounds 1-2, `ship_blockers` only from round 3, residuals recorded); the fix-commit rule.; T3 file:line hits are leads, re-read from the tree before citing
- `conexus/commands/rdr-gate.md` — nexus-g7zgw: the same Layer 0, Fix check and aggregation rules; gate record gains `ship_blockers:`, `fix_check:`, `residuals:`, `prior:`.
- `conexus/skills/rdr-research/SKILL.md` — nexus-g7zgw.5: pre-edit capture for gate fixes (entry before the edit, quote or "inferred, not read" per clause, census for universals).; T3 file:line hits re-read from the tree before quoting
- `conexus/skills/rdr-accept/SKILL.md` — nexus-g7zgw.2: residuals in the gate record are dispositioned (commit sha or bead id) before accept; a `fix_check:` sha that differs from `commit:` blocks accept.
- `conexus/agents/substantive-critic.md` — nexus-g7zgw.4: the canonical Issue format carries a `Sites:` line.
