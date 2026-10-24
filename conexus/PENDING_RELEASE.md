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



## Awaiting the next release or plugin cut (pinned: v7.85.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/version_lockstep_action.py` (nexus-2k66t.4.7, nexus-qytet): a failed `nx self install` logs the LAST 500 characters of its stderr, not the first, because the build now streams uv's progress ahead of the error. The action's `nx upgrade` step no longer runs under the 120 s cap that killed an upgrade-time rebuild (the PDF carry, the installer rebuild rung): the action detaches PLAIN `nx upgrade` itself (not `--auto`, which skips plugin convergence, the generated-agent and hooks refresh, the repos.json migration, beads PRIME and the engine install), stderr to `logs/lockstep-upgrade.log`, waits up to 2 s and treats an immediate nonzero exit or a spawn error as not started, then falls back to the bounded synchronous run. `lockstep.log` records `lockstep_upgrade_background state=started pid=... log=...`; the marker still means "binary confirmed", not "upgrade finished". `nx self install` (step 3a) now has its own 1800 s budget (`NX_LOCKSTEP_SELF_INSTALL_TIMEOUT`) for a first ~0.8 GB install. Works with the released client. Known gap: the ref-drift branch still waits on plain `nx upgrade` under 120 s (it runs plugin convergence and parses its output), so a carry or rebuild owed at that moment is cut off and retried by the next SessionStart.

## Deferred to the next client release

- `conexus/commands/setup.md` (nexus-2k66t.4.2, nexus-2k66t.4.9, nexus-2k66t.4.10): the setup command's install-size rows show the full default install (PDF support included; measured sizes, nexus-2k66t.4.10) with a slim line, and step 3 names the `slim` argument. Needs the client release that carries the [pdf] split and the default-[pdf] installers.
- `conexus/launch/nx_standin.py` (nexus-2k66t.4.2, nexus-2k66t.4.9, nexus-2k66t.4.10, nexus-w8ug5): the stand-in's size rows, the `setup` tool's new `slim` argument (tri-state: omitted, true, false) and the manual and repair commands (`--extras pdf`) match the real server's `src/nexus/mcp/setup_tools.py`, for the same release. Needs the client release that carries the [pdf] split and the default-[pdf] installers.
- `conexus/launch/nx_setup.py` (nexus-2k66t.4.9, nexus-w8ug5): the setup build runs `nx self install --bootstrap --extras pdf` (PDF support by default; `start --slim` leaves it out and records the choice; `--no-slim` installs it over an inherited slim run). Needs the client release whose `--bootstrap` accepts `--extras`.
