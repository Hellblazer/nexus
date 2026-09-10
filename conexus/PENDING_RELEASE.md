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


## Awaiting the next release or plugin cut (pinned: v7.39.0)

- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.13 — On Pass step 2's
  `prior:` field names the previous round's own critique record id,
  never the upserted `{id}-gate-latest` row's own id (that row is one
  fixed title, re-written every round, so its id never changes); also
  notes that `nx rdr preamble rdr-verdict` prints the whole block to
  copy verbatim rather than retype by hand.
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.13 — the same `prior:`
  wording fix and verbatim-copy note, mirroring the skill.
- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.12 — Gate Aggregation, On
  Pass step 3, and the Success Criteria checklist all now say a gate round
  appends ONE printed Revision History line (date, round, outcome, counts,
  ship-blockers, the commit and the two T2 record titles); the findings,
  residual lists and fix narrative live only in the gate record and the
  critique, never repeated in the RDR file.
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.12 — the gate outcome bullet
  mirrors the same one-line Revision History rule and drops the earlier
  claim that residuals are also recorded in Revision History for accept
  to disposition (accept reads the gate record's `residuals:` field).
- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.11 — Layer 0 step 1 now
  scopes the sweep to the findings printed under "Prior findings" (the
  last two rounds' critiques); a finding absent from both, printed under
  "Retired from the sweep" instead, needs no re-check.
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.11 — the Layer 0 bullet
  mirrors the same last-two-rounds scoping.
- `conexus/skills/rdr-gate/SKILL.md`: follow-on review of nexus-yjf5l.11/
  .12/.13 — the `prior:` field template's register (bare clause, no
  narrative or bead pointer) and the Revision History line's composition
  (the commit and the critique's own T2 record title only, not the
  gate-latest record's upserted title) both corrected; Gate Aggregation,
  On Pass step 2 and step 3 all updated in lock-step.
- `conexus/commands/rdr-gate.md`: same follow-on review — the `prior:`
  field template and the gate-outcome bullet's Revision History wording
  mirror the skill's corrections.
  bead: nexus-yjf5l.13
- `conexus/skills/rdr-accept/SKILL.md`: same follow-on review — step 1b's
  "Record the dispositions in Revision History" instruction now states
  its own bound explicitly: one line, naming each residual's disposition
  (sha or bead id), never the finding text.
  bead: nexus-yjf5l.12
- `conexus/commands/rdr-accept.md`: same follow-on review — Step 2b
  mirrors the skill's disposition-recording bound.
  bead: nexus-yjf5l.12
- `conexus/skills/rdr-gate/SKILL.md`: nexus-dxksa — the fix check is three independent dispatches of one brief; a defect
  counts only when at least two raise it; every row carries a Class
  (BLOCKS-PLANNING, DISCOVER-AT-IMPLEMENTATION, OBSERVATION) and only a
  counted BLOCKS-PLANNING defect fails; a failed check is fixed once and
  checked once more, then its counted defects are residuals for accept.
  Replaces "any FAIL: fix, re-run" in every placement.
  bead: nexus-dxksa
- `conexus/skills/rdr-fix/SKILL.md`: nexus-dxksa — the fix check is three independent dispatches of one brief; a defect
  counts only when at least two raise it; every row carries a Class
  (BLOCKS-PLANNING, DISCOVER-AT-IMPLEMENTATION, OBSERVATION) and only a
  counted BLOCKS-PLANNING defect fails; a failed check is fixed once and
  checked once more, then its counted defects are residuals for accept.
  Replaces "any FAIL: fix, re-run" in every placement.
  bead: nexus-dxksa
- `conexus/skills/rdr-accept/SKILL.md`: nexus-dxksa — the fix check is three independent dispatches of one brief; a defect
  counts only when at least two raise it; every row carries a Class
  (BLOCKS-PLANNING, DISCOVER-AT-IMPLEMENTATION, OBSERVATION) and only a
  counted BLOCKS-PLANNING defect fails; a failed check is fixed once and
  checked once more, then its counted defects are residuals for accept.
  Replaces "any FAIL: fix, re-run" in every placement.
  bead: nexus-dxksa
- `conexus/commands/rdr-gate.md`: nexus-dxksa — the fix check is three independent dispatches of one brief; a defect
  counts only when at least two raise it; every row carries a Class
  (BLOCKS-PLANNING, DISCOVER-AT-IMPLEMENTATION, OBSERVATION) and only a
  counted BLOCKS-PLANNING defect fails; a failed check is fixed once and
  checked once more, then its counted defects are residuals for accept.
  Replaces "any FAIL: fix, re-run" in every placement.
  bead: nexus-dxksa
- `conexus/commands/rdr-fix.md`: nexus-dxksa — the fix check is three independent dispatches of one brief; a defect
  counts only when at least two raise it; every row carries a Class
  (BLOCKS-PLANNING, DISCOVER-AT-IMPLEMENTATION, OBSERVATION) and only a
  counted BLOCKS-PLANNING defect fails; a failed check is fixed once and
  checked once more, then its counted defects are residuals for accept.
  Replaces "any FAIL: fix, re-run" in every placement.
  bead: nexus-dxksa
- `conexus/commands/rdr-accept.md`: nexus-dxksa — the fix check is three independent dispatches of one brief; a defect
  counts only when at least two raise it; every row carries a Class
  (BLOCKS-PLANNING, DISCOVER-AT-IMPLEMENTATION, OBSERVATION) and only a
  counted BLOCKS-PLANNING defect fails; a failed check is fixed once and
  checked once more, then its counted defects are residuals for accept.
  Replaces "any FAIL: fix, re-run" in every placement.
  bead: nexus-dxksa
