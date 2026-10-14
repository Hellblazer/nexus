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



## Awaiting the next release or plugin cut (pinned: v7.79.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

## Deferred to the next client release

ONE PATH PER BULLET, on the bullet's FIRST line. These wait for the client release that carries RDR-228 Phase 2: `nx self install --bootstrap` (nexus-sv2wx.14) and the real server's `setup` tools (nexus-sv2wx.16). A plugin-only cut ahead of it would ship setup parts that cannot work (nexus-sv2wx.13 guards this).

- setup process (nexus-sv2wx.15): `conexus/launch/nx_setup.py` is new (RDR-228 Phase 2 Step 2). One detached process that builds the CLI (`uv tool run ... nx self install --bootstrap`), then for mode `local` runs `nx init --yes --no-beads-prime` (or starts a provisioned, stopped service) and `nx doctor`, writing its stage to `setup/status.json` under the Nexus config directory, with a log, the "not now" marker and a lock directory kept alive by a heartbeat. The build is skipped only for an identified Nexus CLI: the generation layout's `nx` when `nx self install --bootstrap --dry-run` says nothing is left to do, or a `PATH` `nx` with `nx-mcp` beside it and a Nexus `--version` line; any other `nx` (Nrwl's) is never run. Child output goes straight into the log, a timed-out or orphaned step is killed with its children, a lost lock stops the run without a further status write, and every command prints one JSON object (state `error` on an unexpected failure), never a traceback. Its command line (`start`, `status`, `not-now`) and its importable `status_view`, `provisioned` and state constants are the one way to use that directory. Nothing starts it yet: the setup tools that call it are Step 3, and mode `cloud` is refused without changing anything because the credentials page is Step 5. It waits for the client release with `nx self install --bootstrap` (nexus-sv2wx.14), which the build step runs.
- stand-in setup tools (nexus-sv2wx.16): `conexus/launch/nx_standin.py` gives `setup` and `setup_status` their work (RDR-228 Phase 2 Step 3). `setup` takes `mode` (`local`, `cli-only`, `not-now`; a managed service is refused) and, for `local`, `autostart` (false unless given), starts `nx_setup.py` beside it with its own Python and returns at once with the stage, how long the stage usually takes and when to ask again. `setup_status` reports the stage, the log path and the next step. Both go through `nx_setup.py`'s command line; the stand-in reads and writes no setup file. The names are the Phase 1 names; the arguments and descriptions change (the descriptions tell the model to ask the user first, and name `/conexus:setup`). Every returned text that mentions a recorded run shows its mode and autostart and, where a call to `setup` is suggested, says to ask the user first; the `next` strings of `nx_setup.py` carry the same clause (nexus-sv2wx.16). They are the same tools, with the same schema and text, that the real `nx-mcp` registers (`nexus.mcp.setup_tools`); a test pins the two copies equal.
- launcher hands the setup script to the real server (nexus-sv2wx.16): `conexus/launch/nx_launch.py` sets `NX_SETUP_SCRIPT` to the path of `nx_setup.py` beside it when it hands off to the real `nx-mcp` (RDR-228 Phase 2 Step 3), in every state of the setup directory, which it does not read. An inherited value is replaced, and no other program gets it. The real server's `setup` tool starts that file; without the variable the tools start nothing and answer with the terminal commands.
