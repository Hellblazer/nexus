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


## Awaiting the next release or plugin cut (pinned: v7.16.0)

- `conexus/commands/rdr-create.md`: nexus-bc292 — added a PRIOR-ART SCAN
  step that runs BEFORE drafting. The command already pre-loads every
  existing RDR with title and status, but framed it as "data … no
  additional tool calls needed", which reads as reference material for
  picking the next ID rather than a corpus to mine. Measured failure:
  RDR-198 was drafted claiming a novel diagnosis while RDR-164 —
  present in that same pre-load table — had already reached it, shipped
  the fix for one domain, and named two resulting bugs. The new step
  requires classifying each overlapping RDR as origin / precedent /
  adjacent-draft / superseded, recording the result in a
  `## Relationship to Prior RDRs` section, and writing one line if the
  scan finds nothing (an unrecorded dead end is indistinguishable from
  not having looked).
  **INERT UNTIL RELEASE.** Commands load from the pinned v7.16.0 tag,
  so `/conexus:rdr-create` keeps its current text in every running
  session until the next release or plugin cut ships.

- `conexus/skills/using-nx-skills/SKILL.md`: nexus-bc292 — replaced the
  lead sentence "You MUST invoke `Skill`" (measured, not assumed, not to
  work: across 6 sandboxed eval-corpus runs exercising exactly this
  rule, a conexus skill was invoked 1 time in 6, three runs made zero
  Skill calls) with "Conexus skills carry this project's accumulated
  practice for specific situations", mirroring the same change already
  made to `src/nexus/session_start_guidance.py`'s `GUIDANCE_IMPERATIVE`.
  **INERT UNTIL RELEASE.** Skills load from the pinned v7.16.0 tag, so
  the reworded lead does not reach any running session until the next
  release ships.
