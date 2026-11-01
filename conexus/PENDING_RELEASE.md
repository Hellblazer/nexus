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

**Deferring a straddling entry (nexus-2x3qy, nexus-rbmja.10).** A plugin-only
release cherry-picks commits from `develop` onto a branch off the last client
tag (`release` skill, "Plugin-only fast lane"). The eligibility gate reads the
bead ids named in those commits and refuses the release when any other commit
on `origin/develop` since the tag that names the same bead touches a path the
lane does not ship (`src/`, `service/`, `scripts/`, `.github/`, `mcpb/`,
`conexus/plans/`, `conexus/daemon/`, release machinery under `tests/`, and so
on). Attribution is by commit message, matched on the whole bead id, so a
commit that does not name its bead is not seen by that check. To keep a
straddling entry out of the release, move it under
`## Deferred to the next client release` below and do not cherry-pick its
commits, so the whole bead ships together at the next normal release. A
deferred entry is exempt from the release-window "ledger must be empty" rule
above -- still declared there is correct, not stale -- and stays exactly where
it is until moved back deliberately.

---
## Awaiting the next release (pinned: v7.93.0)

ONE PATH PER BULLET, on the bullet's FIRST line.
- `conexus/skills/rdr-fix-checklist/SKILL.md` (nexus-qo7ie): rules 1 and 2 say where unnamed and design-needing Significants go before and from round 3, the fix check runs after the round's last fix commit, and the round-3 ship-blocker-only rule is scoped to a BLOCKED gate.
- `conexus/skills/rdr-gate-checklist/SKILL.md` (nexus-qo7ie): the same rule wording, the Layer 3 brief names the swept prior findings, and the round-3 ship-blocker-only rule is scoped to a BLOCKED gate.
- `conexus/skills/orchestration/reference.md` (nexus-qo7ie): the RDR lifecycle line runs `/conexus:rdr-fix` for a BLOCKED gate's findings with one fix check per fix round, then a re-gate, in place of a fix per finding.
- `conexus/hooks/scripts/compact_checkpoint.py` (nexus-qowvl): the launch-directory test that picks a session's own worktree compares normcase/normpath spellings of both sides, so it no longer relies on realpath alone to normalize slashes and drive-letter case on Windows (defensive; no production miss is known).
- `conexus/skills/orchestration/SKILL.md` (nexus-mtheb): states where MCP-tool scratch lives across `/clear` and `/resume` as JDR-001 has it (the scope moves only on a consumed handoff marker and the old rows strand), in place of saying it survives both, and notes the 24 h sweep.
- `conexus/hooks/scripts/compact_checkpoint.py` (nexus-702gy): a checkpoint is restored even when its compaction's summary is used later than it was made (the changelog's deferred idle case): post and inject pair a checkpoint with its compaction by use (a summary in the file, a stamp in the `<session>.injected` sidecar) in place of a 30-minute window, work in either order (the harness runs inject first), and the intro gives the real age in hours and minutes and, past ten minutes, says the state may predate the compaction.
- `conexus/hooks/hooks.json` (nexus-mtheb.3): a `SubagentStop` entry (matcher `.+`, so harness-internal stops with no agent type never spawn it) runs the new `subagent-stop-t1-nudge` verb, which gives a named agent one more turn when its transcript shows no T1 put. It goes through `nx_hook_shim.py`, so a CLI older than the verb skips it with a note and never blocks.
- `conexus/README.md` (nexus-mtheb.3): the hook table gains the `SubagentStop` row.
- `conexus/hooks/scripts/compact_checkpoint.py` (nexus-mtl98): a garbled or non-UTF-8 sidecar or checkpoint no longer silences the restore (it raised and printed nothing for two days) and is logged; post's summary heading is matched whole with its UTC stamp, which narrows a collision with a threads board block titled "Compact summary ..."; a summary carrying a lone surrogate is written, not lost; the stale intro states both cases it cannot tell apart (a deferred summary, a failed pre) in place of asserting either.

## Deferred to the next client release
