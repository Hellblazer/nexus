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


## Awaiting the next release (pinned: v7.8.0)

nexus-haf6p — bound the unbounded review loop; report-everything + ship-blocker
gate; de-compound mandatory reviewer-dispatch scaffolding; two-sided scope
clause; delegation-restraint split; deliverable-length calibration:

- `conexus/skills/code-review/SKILL.md` — acceptance bar, bounded Rounds section, replaced the unbounded digraph, added the round-2 Confirmation Pass template
- `conexus/skills/substantive-critique/SKILL.md` — cross-references the shared Rounds bound instead of duplicating it
- `conexus/agents/substantive-critic.md` — added `Ship-blocker: yes|no` per finding and `ship_blockers: N` in the Verdict block (additive, parser-safe); Operating Principles filtering line became a recall line
- `conexus/agents/code-review-expert.md` — reviewer now falsifies tests itself by reading (FALSIFIED/NOT FALSIFIED); test-validator hand-off is now conditional, not always
- `conexus/agents/developer.md` — removed the 3x-repeated mandatory "dispatch code-review-expert, substantive-critic, test-validator" block; hands back per the caller's standing policy instead
- `conexus/agents/_shared/CONTEXT_PROTOCOL.md` — new § Escalation (conditional, never routine), § Scope (both directions), § Deliverable Length
- `conexus/skills/orchestration/SKILL.md` — new § Review Rounds (round marker + human-gated round 3), § Scope Discipline cross-reference, fleet-size cap, delegation-restraint scoped away from the review gate and fork fleets
- `conexus/skills/development/SKILL.md` — orchestrator-facing gate rewritten to the bounded flow (acceptance bar, round cap, confirmation pass); fixed a stale reference to developer.md's now-removed mandatory block
