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


## Awaiting the next release (pinned: v7.4.0)

- `conexus/hooks/scripts/version_lockstep_action.py` — the detached
  auto-upgrade now writes an always-on audit line per swap attempt
  (`lockstep_upgrade_started` / `lockstep_upgrade_result`) to
  `~/.config/nexus/lockstep.log` (nexus-otnvr item 4: the hook's output
  was routed to DEVNULL and its debug() gated behind NX_HOOK_DEBUG, so
  the venv swap was invisible — it raced the MCP server boot at the
  7.4.0 reinstall with no trace). Installed sessions keep the silent
  swap until the next plugin release.
- `conexus/hooks/scripts/expectations.sh` + `conexus/hooks/scripts/subagent-stop.sh`
  — nexus-7z7rj + nexus-plycy (two-round fix, same day). Round 1
  (nexus-7z7rj): the `expectations_owes_report` credit read-decide-append
  is now gated strictly `if [[ -n "$held" ]]`, closing the double-spend
  the 4b8sz lock-budget knob only narrowed (CI red 2026-08-08, PR #1445
  run 31244456036, blocked=5 vs credit=4 at NX_EXPECT_LOCK_TRIES=200).
  Round 2 (nexus-plycy, substantive-critic Critical on round 1 before it
  shipped): round 1's fixed exhaustion default ("does not owe", never
  consult the ledger) was itself unsafe combined with the pre-existing
  60s stale-lock reap — an orphaned lock holder could silence the
  owes-report guard SESSION-WIDE for up to a minute (silent miss, not
  the guard's usual safe-direction over-block). Round 2 ships the
  contract actually live going forward:
    - the owes-report lockdir is now PER AGENT_TYPE
      (`<file>.owes.<type>.lock`, `:` encoded to `__`), not
      session-wide, so exhaustion can only mean same-type contention or
      a same-type stale lock;
    - the fixed default on exhaustion is now BLOCK (owes), not pass —
      the safe direction is restorable now that cross-type
      false-accusation risk is gone — but the CONSUMED ledger row is
      still never written on an unverified decision (no credit is ever
      spent without holding the lock);
    - every exhaustion-forced block is DISCLOSED, never silent: the
      JSON `reason` names the lock-contention cause and the BLOCKED
      row's ledger entry carries an optional 4th "cause" field
      (`lock-exhausted`) so it's distinguishable from a genuine,
      credit-verified block on audit.
  Residual, honest and bounded: a stale SAME-type lock still over-blocks
  stops of that one type for up to the existing ~60s reap window before
  self-healing — the accepted price, always disclosed, never silent.
  Also adds the test-only `NX_EXPECT_LOCK_HOLD_DELAY_S` contention seam.
  Installed sessions keep the pre-round-1 racy-unlocked-fallback
  behavior (rare, load-dependent, undisclosed over-block of the
  owes-report SubagentStop guard) until the next plugin release.
