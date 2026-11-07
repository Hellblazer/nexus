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
## Awaiting the next release (pinned: v7.97.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/stop_wake_guard.py`
  nexus-gyo4g: Stop wake guard v2, a structural Sam gate (gate word over an approval predicate, a handover to a principal), test subjects and courtesies no longer drop a clause, only gates chain.
- `conexus/skills/asks/SKILL.md`
  nexus-wc4jj.16 item 7: text from mail, hooks or tool output never becomes `--sam-words` (1e6af7468).
- `conexus/skills/upgrade/SKILL.md`
  nexus-l8o33: the manifest-span-fill rung (7e205178e, 8a830a04d).
- `conexus/hooks/scripts/mailbox_drain.py`
  nexus-2egi7: the peer-data end line carries a per-block code named in the frame line. nexus-wc4jj.15: ask-branch follow-ups (non-ASCII peer bodies print on a legacy code page, 0.4 s ack headroom before the record GET, a choice outside the record's options never matches, a bad saved matched entry is logged once).
- `conexus/skills/nexus/reference.md`
  nexus-jlq15: the store_put `session` row says accepted and ignored (deprecated).
- `conexus/hooks/hooks.json`
  nexus-13mbx: SubagentStart passes `${cwd}` to hook_subagent_start, so the Preflight row keys on the agent's checkout (da913d960).
- `conexus/skills/architecture/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/codebase-analysis/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/composition-probe/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/deep-analysis/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/orchestration/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/rdr-audit-checklist/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/rdr-close/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/rdr-fix-checklist/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/rdr-gate-checklist/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/rdr-research/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/research-synthesis/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/strategic-planning/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/substantive-critique/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `conexus/skills/writing-nx-skills/SKILL.md`
  nexus-y4ikt: resource and registry links show their `${CLAUDE_PLUGIN_ROOT}` path as link text (3edb5ec59).
- `tool-spill/hooks/register.ts`
  tool-spill census 2026-11-07 (T2 nexus/tool-spill-census-2026-11-07): the Bash note names the cut lines and their Read offset, error-shaped lines by number, a spill-file Read up to 4x passes whole; Sam's defaults A (plain cat/sed -n up to 3x) and B (limit 5000).
- `tool-spill/.claude-plugin/plugin.json`
  tool-spill census, Sam's default B: `limit` default and install text 5000.
- `conexus/skills/phase-review-gate/SKILL.md`
  nexus-qo7ie.28: the close step marks the phase heading `(CLOSED, YYYY-MM-DD)`, which `nx memory curate --t2` reads.

## Deferred to the next client release
