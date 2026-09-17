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



## Awaiting the next release or plugin cut (pinned: v7.51.1)

- `conexus/skills/phase-review-gate/SKILL.md`: the evidence-key note says Pass 1 renumbers and heading-qualifies items when two §Approach lists each restart at 1, so keys are copied from the Pass 1 table (bead nexus-8tpw3).
- `conexus/hooks/scripts/rdr_hook.py`: the SessionStart status loader keeps `RDR-NNN`-titled T2 status records, keyed on the bare number and counted once per RDR (bead nexus-nc08w.1); `_collection_exists`'s T3 timeout leg no longer leaks a non-daemon `ThreadPoolExecutor` worker past `_T3_DEADLINE_S` — a bare daemon thread cannot hold the hook process open past the harness's 10s cap (bead nexus-r8643, intrastate review [26115] #3).
- `conexus/skills/rdr-close/SKILL.md`: the file-flip step names `--reason` for closing a never-accepted draft, the lifecycle table's guarded `close-unaccepted` edge (bead nexus-nc08w.4).
- `conexus/skills/rdr-create/SKILL.md`: the T2 record template writes `status: draft`, the lifecycle domain's lower-case value, not `Draft` (bead nexus-nc08w.5).
- `conexus/agents/code-review-expert.md`: a mandated terminal `## Verdict` block with the same `- **outcome**:` shape and the same three values `substantive-critic` emits (bead nexus-4hoc0).
- `conexus/skills/development/SKILL.md`: a new check, gate, census or lint pastes its first live run, with examined and skipped counts, into its bead before close (bead nexus-uuf3w).
- `conexus/agents/substantive-critic.md`: one sentence fixing the order at the end of the output: a Recommended Next Step block precedes the Verdict block, which is always last (bead nexus-4hoc0).
