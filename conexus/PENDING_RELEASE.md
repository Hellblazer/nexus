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


## Awaiting the next release or plugin cut (pinned: v7.38.0)

- `conexus/skills/rdr-accept/SKILL.md`: nexus-yjf5l.1 — step 1c and its
  Success Criteria row: a residual dispositioned by a change to the RDR
  file carries a fix check on that change, stored as
  `{id}-fix-check-<sha>`; a bead-id disposition needs none.
- `conexus/commands/rdr-accept.md`: nexus-yjf5l.1 — Step 2b mirrors the
  skill: the residual disposition rule and the fix check a sha
  disposition carries.
- `conexus/skills/rdr-fix/SKILL.md`: nexus-yjf5l.2 — Rules gains the
  round-3 rule: the fix closes only findings marked `Ship-blocker: yes`;
  every other Critical and Significant is a residual, dispositioned at
  accept, never re-gated.
- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.2 — the same rule added
  to Fixing findings, so the gate side and the fix side agree.
- `conexus/commands/rdr-fix.md`: nexus-yjf5l.2 — mirrors the skill's
  round-3 rule and the fix preamble's two-list split.
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.2 — a Fixing findings
  bullet mirroring the same rule.
- `conexus/hooks/hooks.json`: nexus-xn84f — SessionStart runs `nx self gc`
  (third, after the upgrade and the preflight) so a generation a
  long-lived session held at install time is reaped once that session
  ends, not at the next install; a box without the verb or the layout
  is silent (`|| true`).
- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.3 — Layer 0 step 1 exempts
  a recorded residual from the survivor sweep: dispositioned at accept, not
  swept again.
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.3 — the Layer 0 bullet mirrors
  the same exemption.
- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.5 — Fix check step 1 gains
  a fifth clause: list every other occurrence of a changed identifier
  (column, caller-supplied parameter, typed error, setting, phase or step
  number) and say whether each still holds; step 4 states the serial
  precondition (fix, check, then Layer 1 and Layer 3, never dispatched in
  parallel against the same commit).
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.5 — the Fix check bullet
  mirrors both the identifier clause and the serial precondition.
- `conexus/skills/rdr-fix/SKILL.md`: nexus-yjf5l.5 — the identifier clause
  and the serial precondition in Rules, Behavior step 5, and the Relay
  Template's Deliverable line.
- `conexus/commands/rdr-fix.md`: nexus-yjf5l.5 — mirrors the identifier
  clause and the serial precondition.
- `conexus/agents/substantive-critic.md`: nexus-yjf5l.7 — the Issue output
  format gains a `Class:` bullet (BLOCKS-PLANNING or
  DISCOVER-AT-IMPLEMENTATION), scoped as required for an RDR gate critique
  and optional for every other consumer of this agent. nexus-yjf5l.10
  corrected the named "every other consumer" list to the ones that
  actually dispatch this agent (code review, rdr-fix, rdr-accept,
  rdr-close, and the substantive-critique skill) — the original list
  named plan-audit and phase-review-gate, neither of which ever
  dispatches it.
- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.7 — the Layer 3 relay's
  brief and Gate Aggregation gain the Class line, the
  classification-governs-disposition rule, and the
  Ship-blocker/DISCOVER-AT-IMPLEMENTATION contradiction refusal.
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.7 — mirrors the Class line
  and the contradiction refusal.
- `conexus/skills/rdr-accept/SKILL.md`: nexus-yjf5l.8 — step 1b and its
  Success Criteria row: a `DISCOVER-AT-IMPLEMENTATION` residual is
  dispositioned by a bead naming its Implementation Plan phase; a
  `BLOCKS-PLANNING` or unclassified residual needs an explicit author
  disposition, never a default.
- `conexus/commands/rdr-accept.md`: nexus-yjf5l.8 — Step 2b mirrors the
  same class-to-disposition rule.
- `conexus/skills/rdr-gate/SKILL.md`: nexus-yjf5l.17 — the T3-lead clause
  becomes one sentence (matching the Relay Template's join) instead of
  two, and the Fix check list gains a sixth check: for every added check
  and every added or altered parameter, column or setting, name what
  constrains what, regardless of shared vocabulary.
- `conexus/commands/rdr-gate.md`: nexus-yjf5l.17 — the T3-lead clause
  stops being a paraphrase and becomes the identical sentence carried by
  the other gate-side surfaces; the same sixth check is added.
- `conexus/skills/rdr-fix/SKILL.md`: nexus-yjf5l.17 — the sixth check
  added in Behavior step 5, Rules, and the Relay Template's Deliverable
  line (as check (6), the T3-lead clause's (4) capitalised to match).
- `conexus/commands/rdr-fix.md`: nexus-yjf5l.17 — the sixth check added
  as its own bullet, mirroring the skill.
