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


## Awaiting the next release (pinned: v7.14.0)

- `conexus/skills/nexus/reference.md`:
  nexus-nyry9.19 (RDR-196 .p3a): the `nx_answer` parameter table's
  `budget_usd` row no longer documents the unmeasured `0.25` default; it
  now reads `None` = "use `budget_default.DERIVED_BUDGET_USD`" (itself
  unset until a sufficient post-flip derivation is recorded), enforcement
  landing in .p3c. Docs-only drift: an installed plugin at v7.14.0 shows
  a default the 7.15.0 tool no longer has.
- `conexus/resources/rdr/TEMPLATE.md`: nexus-ptwm2 fixed 20 `nx prose lint`
  findings (em dashes, `load-bearing`) and added a pointer to
  docs/writing-style.md under the top blockquote.
- `conexus/resources/rdr/post-mortem/TEMPLATE.md`: nexus-ptwm2 fixed 15
  `nx prose lint` findings (em dashes) in the drift-category definitions
  and takeaway criteria.
- `conexus/skills/rdr-create/SKILL.md`: nexus-ptwm2 fixed 2 `nx prose lint`
  findings (em dashes).
- `conexus/skills/rdr-gate/SKILL.md`: nexus-ptwm2 fixed 9 `nx prose lint`
  findings (em dashes, including the three Layer heading separators) and
  documented the new prose-lint preamble check under Layer 1.
- `conexus/commands/rdr-create.md`: nexus-ptwm2 fixed 1 `nx prose lint`
  finding (em dash).
- `conexus/commands/rdr-gate.md`: nexus-ptwm2 fixed 7 `nx prose lint`
  findings (em dashes) and documented the prose-lint preamble check
  alongside the Layer 1 bullet.
- `conexus/commands/rdr-accept.md`: nexus-ptwm2 fixed 26 `nx prose lint`
  findings (em dashes) across the step and planning-chain labels.
- `conexus/commands/rdr-close.md`: nexus-ptwm2 fixed 2 `nx prose lint`
  findings (em dashes).
- `conexus/commands/rdr-research.md`: nexus-ptwm2 fixed 2 `nx prose lint`
  findings (em dashes).
- `conexus/commands/rdr-show.md`: nexus-ptwm2 fixed 2 `nx prose lint`
  findings (em dashes).
- `conexus/commands/rdr-list.md`: nexus-ptwm2 fixed 1 `nx prose lint`
  finding (em dash).
- `conexus/commands/rdr-audit.md`: nexus-ptwm2 fixed 5 `nx prose lint`
  findings (em dashes).
- `conexus/agents/substantive-critic.md`: nexus-ptwm2 added a "Prose
  Deliverables" section (point at docs/writing-style.md, run `nx prose
  lint` first, six review questions reported as Significant).
- `conexus/hooks/scripts/subagent-start.sh`: nexus-ptwm2 added a PROSE_STYLE
  preflight row pointing every subagent at docs/writing-style.md and the
  lint; inert in installed sessions until the pin advances.
