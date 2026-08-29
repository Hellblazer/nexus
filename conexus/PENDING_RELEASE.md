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


## Awaiting the next release or plugin cut (pinned: v7.22.0)

- `conexus/hooks/scripts/expectations.sh` — bead nexus-houpu: RDR-184 ledger
  audit surfaces re-keyed for the CC 2.1.251 SubagentStart payload. A named
  background teammate now arrives with an opaque `a<hex>` `agent_id` and
  `agent_type` == its `subagent_type`; the `a<name>-<hash>` encoding is gone
  from the wire. `expectations_undeclared` and `expectations_census` pair
  every START by (agent_type, N-of-type EXPECT credit) — the key
  `expectations_owes_report` has used since nexus-hbr4x — and share one
  blind-spot rule: rc=1 only when EXPECT rows exist and zero STARTs were
  walked. A START whose type has no EXPECT row (including a dispatch the
  PreToolUse Agent|Task hook never saw, unless hand-declared) is now NAMED
  UNDECLARED instead of silently skipped.

  **What is inert until this ships:** through the INSTALLED plugin path
  (`~/.claude/plugins/marketplaces/*/conexus/hooks/scripts/`) the two audits
  still give an unpaired START the old free pass (skipped, not named) and
  still read rc=1 as `recognized==0`; hooked dispatches were already paired
  by type there, so a fully mechanized session audits the same either way.
  The in-repo path (`source tests/e2e/lib/expectations.sh`) is live
  immediately. The stop guard is unaffected: `expectations_owes_report`
  needed no change and `conexus/hooks/scripts/subagent-stop.sh` consumes
  only it.
