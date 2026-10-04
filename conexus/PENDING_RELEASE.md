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



## Awaiting the next release or plugin cut (pinned: v7.70.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- nexus-catalog schemas deferred (nexus-ivi4s): `conexus/.mcp.json` sets `alwaysLoad: false` on `nexus-catalog` (10 tools, about 3.4k tokens of schema), so Claude Code loads its schemas through tool search on first use instead of in every session. `nexus` and `sequential-thinking` keep `alwaysLoad: true`, which conexus 4.34.4 set because deferred tools stopped being invoked (3cd9e7d12); the catalog tools are the least-used of the three servers. Sam, 2026-10-04.
- outside-critique reply (nexus-0cq5m): `conexus/commands/nx-preflight.md` no longer assumes its preflight output is present; when the `!` preamble's output is missing it runs `nx command-context nx-preflight` instead of summarizing nothing. Text only.


## Deferred to the next client release

- operators demoted from the MCP surface (nexus-ivi4s): `conexus/skills/nexus/SKILL.md` drops the ten direct `operator_*` call examples and points at `nx_answer`. Ships with the client release that demotes the tools in `src/nexus/mcp/core.py`; shipped alone it would tell sessions on the old client not to call tools they still have.
- operators demoted from the MCP surface (nexus-ivi4s): `conexus/skills/nexus/reference.md` replaces the ten per-operator tool sections with one table of plan verbs, and drops the operators from its core-tool list. Same pairing as above.
