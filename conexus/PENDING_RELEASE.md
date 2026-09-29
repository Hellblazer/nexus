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



## Awaiting the next release or plugin cut (pinned: v7.66.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- nexus-2lf1v: `sn/hooks/scripts/auto_approve_sn_mcp.py` stops auto-approving
  Serena's write tools, `jet_brains_debug` (arbitrary Groovy/Java in the IDE's
  JVM), `query_project`, `onboarding` and `restart_language_server`. They were
  all approved before, which skipped your prompt and the auto-mode classifier.
  Reads and Context7 are still approved. To keep editing through Serena without
  prompts, add the writers you use by name to `permissions.allow`.
- nexus-2lf1v: `sn/hooks/scripts/serena-section.md` tells subagents that Serena
  writes now go through the permission flow, and to fall back to Edit or Write
  rather than retry a denied one.

## Deferred to the next client release

- nexus-nmzsg: `conexus/hooks/scripts/routing/_lib.py` adds
  `ask_envelope`/`ask` (`permissionDecision: ask`, reason in
  `permissionDecisionReason`), the PreToolUse decision that forces a
  permission prompt in auto mode where a bare advisory would let the
  classifier approve silently. No plugin script calls it yet; the pre-close
  gate that uses it ships in the wheel (`nexus.hooks._routing_lib`). Deferred because the bead also touches `src/`, which a plugin cut refuses deterministically.
- nexus-qxyqz: `conexus/hooks/hooks.json`, `conexus/README.md` drop the `UserPromptSubmit` entry (and its hook-table row) for `nx-hook mcp-connect-check`, the mid-session "nx-mcp is not connected" warning (it read a session-id-keyed marker that cannot be a reliable liveness signal, and cost 270-300 ms of imports per prompt). Safe against every CLI: the verb stays registered as a silent no-op for plugins that still name it. Deferred because the bead also touches `src/`, which a plugin cut refuses deterministically.
