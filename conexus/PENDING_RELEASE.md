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

**Deferring a straddling entry (nexus-2x3qy).** A plugin cut (`scripts/
cut_plugin_release.py`) refuses when a ledger entry's bead also touches wheel
content (`src/`, `conexus/plans/`, `conexus/daemon/`, `mcpb/`, `dt/`) the
wholesale import cannot hold back on a per-entry basis. The only fix is moving
that entry under `## Deferred to the next client release` below: the cut then
holds the entry's channel path(s) back from itself too (restored to the base
branch's own content) so the whole bead ships together, in one piece, at the
next client release. A deferred entry is exempt from the release-window
"ledger must be empty" rule above -- still declared there is correct, not
stale -- and stays exactly where it is until moved back deliberately.


---

## Awaiting the next release or plugin cut (pinned: v7.86.3)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `tool-spill/.claude-plugin/plugin.json` (nexus-121ke): each userConfig option's title says what it changes and what a blank field means (Claude Code shows only titles and never prefills a declared default), and the plugin description says no setup is needed.
- `tool-spill/README.md` (nexus-121ke): the install section explains the blank option fields, the four defaults, why one would change each, and the note a cut result carries.
- `tool-spill/hooks/register.ts` (nexus-x967v): the `<persisted-output>` marker counts only at the start of a Bash result, so output that merely quotes it (the plugin's own README or source) is cut instead of passing whole.
- `conexus/hooks/scripts/routing/subagent_git_integrity_check.py` (nexus-i93tz): deleted. The subagent shared-git-state integrity check (shipped report-only in 7.83.x) is dropped by Sam's decision of 2026-10-25: three and a half days of real subagent work produced no change report and its restore phase is never built. The subagent git-write guard (`subagent_git_write_requires_orchestrator.py`) keeps its logic and stays the only line. Rows the hook already wrote to the routing log stay readable (`integrity_info`, `integrity_unverified` and `integrity_change` outcomes count under `extra` in `nx hook routing-stats`, and the rule shows as unregistered); the `git-integrity/` session state it left under the nexus config directory is reaped by the client's `nx-hook self-gc` (session directories untouched for two days), not by the plugin, and nothing reads it.
- `conexus/hooks/hooks.json` (nexus-i93tz): drops the three registrations of `subagent_git_integrity_check.py` (PreToolUse, PostToolUse and PostToolUseFailure on Bash); the PostToolUse and PostToolUseFailure events leave the manifest, and the per-call Bash hook count goes from 4 back to 2.
- `conexus/hooks/scripts/routing/registry.yaml` (nexus-i93tz): drops the `subagent_git_integrity_check` rule.
- `conexus/hooks/scripts/routing/README.md` (nexus-i93tz): the cumulative-cap table counts 2 rules again and names the removal; a paragraph says the git-write guard is frozen except for real incidents and lists the routes it does not cover.
- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` (nexus-i93tz): docstring only, no logic change; the KNOWN LIMITS text gains the frozen-guard paragraph and the accepted not-covered routes.
- `conexus/skills/rdr-create/SKILL.md` (nexus-3piej): Step 2 confirms `{repo}_rdr/NNN` is free in T2 (only a not-found answer; a record or an ambiguous-prefix answer means held) before the file is created, because `memory_put` upserts by title and silently replaced RDR-233's record on 2026-10-26; the step also says what the preamble's new held-elsewhere list and incomplete-scan notice mean.

## Deferred to the next client release

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/skills/nexus/SKILL.md` (nexus-jypmj.1.16): the `nx_answer` `scope="1.2"` example says a catalog subtree filters `query` and `search_metadata_scoped` steps and that a `search` step runs unscoped and is named in the answer.
- `conexus/skills/nexus/reference.md` (nexus-jypmj.1.16): the `scope` parameter row says a subtree is a filter and never a corpus, which tools it reaches, that a document-level address filters nothing, and the `[scope not applied: ...]` line and `subtree_not_applied` outcome that disclose it.
