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


## Awaiting the next release or plugin cut (pinned: v7.19.0)

- `conexus/hooks/scripts/pre_close_verification_hook.sh` — nexus-e3mak: a
  review-completed marker now counts only when it NAMES the full required
  reviewer set (code-review-expert AND substantive-critic). Previously the gate
  matched the literal string plus the bead id, so one reviewer's handoff note
  saying "reviewer 1/2" satisfied the gate it was half of, with the critic
  never dispatched. Inert until the pin advances — the hook and the remedy text
  it prints ship together, which is why no cross-version carve-out was needed.
- `conexus/hooks/scripts/subagent-stop-writes-scan.py` (NEW) — nexus-piqm5
  Layer 1: the unlanded-write scan. Reads a finished subagent's transcript,
  correlates each storage-write tool_use to its tool_result by tool_use_id,
  and reports writes that came back "Error: ". Positive evidence only; an
  unreadable transcript reads as CLEAN.
- `conexus/hooks/scripts/subagent-stop.sh` — nexus-piqm5 Layer 1 wiring:
  stamps an `UNLANDEDWRITE` ledger row in both modes, and blocks ONCE — for
  agents already inside the owes-report allowlist — in the
  reported-but-writes-failed case. No dispatch that is unblockable today
  becomes blockable.

  Worth saying out loud given what this ledger is for: **this guard is inert in
  every running session until the pin advances.** The failure it detects —
  findings reported as complete while every persistence call failed — can still
  happen silently in the meantime, exactly as it did on 2026-08-25. It is not
  mechanized until it ships.
