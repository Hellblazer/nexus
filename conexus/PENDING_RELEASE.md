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

## Awaiting the next release or plugin cut (pinned: v7.86.2)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `tool-spill/hooks/register.ts` (nexus-121ke): new opt-in plugin conexus-tool-spill. In the main conversation only (subagents and, unless `includeHeadless` is on, `claude -p` runs are left alone), a Bash result over `limit` bytes (default 4000) is written to `<temp dir>/claude-tool-spill-<user>/<tool_use_id>.txt` and the model sees the first `head` bytes (1400) and the last `tail` bytes (800) of each stream, cut at line ends, with a note between them naming the file, the middle lines cut and how many of them mention an error or failure. A Read of a whole file over the limit is cut with the next offset and a limit to use; a Read with an offset or limit, an instruction file, an errored or denied call, output Claude Code already persisted, and every other tool pass untouched. Install with `/plugin install conexus-tool-spill@nexus-plugins`; it replaces any machine-local copy of the mod, and nothing changes for anyone who does not install it.
- `tool-spill/hooks/hooks.json` (nexus-121ke): names the hooks module of conexus-tool-spill.
- `tool-spill/.claude-plugin/plugin.json` (nexus-121ke): manifest of conexus-tool-spill, with the `limit`, `head`, `tail` and `includeHeadless` userConfig options.
- `conexus/hooks/scripts/version_lockstep_hook.py` (nexus-121ke): the fallback plugin set, used only when no marketplace.json is reachable, now names conexus-tool-spill too, so an installed copy of it is kept in version lockstep on that path.
