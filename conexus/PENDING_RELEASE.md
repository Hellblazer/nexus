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



## Awaiting the next release or plugin cut (pinned: v7.78.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- hook exec off the cwd (nexus-f9bgu.38): `conexus/hooks/scripts/_exec_path.py` skips a `PATH` entry that is the working directory itself and a rooted one without a drive (`\tools`), and one that spells the working directory with `.` or `..` parts, and tries a name that already ends in a listed extension as written only (`uv.exe`, not `uv.exe.cmd`). A `.cmd` or `.bat` hit is no longer returned (Sam, 2026-10-13): a tool found only as a batch file counts as not found, as in `nexus.util.nx_argv`. No change on POSIX.
- CLI lookup (nexus-sv2wx.5): `conexus/hooks/scripts/_cli_lookup.py` is new. One lookup for the launcher and the hook shim: the generation layout's `<tools>/current`, resolved to its generation directory first, then the `PATH` lookup that skips the current directory. It also holds the no-CLI note and the project-directory rule the two share.
- hook shim lookup (nexus-sv2wx.5): `conexus/hooks/scripts/nx_hook_shim.py` finds `nx-hook` through `_cli_lookup`, so it looks in the generation layout before `PATH` and finds an install made after the application started. On a machine that has a generation and a different `nx-hook` earlier on `PATH`, the three shim entries (`auto-approve` twice, `mcp-connect-wait`) now run the generation's. Its note for a missing `nx-hook` no longer says to run `nx self install`.



## Deferred to the next client release

ONE PATH PER BULLET, on the bullet's FIRST line. These wait for the client release that carries nexus-sv2wx.8's fix to `src/nexus/session.py`: the server entries below put `uv` between Claude and `nx-mcp`, and a CLI without that fix no longer finds the server for the `/clear` and `/resume` session handoff.

- server entries through the launcher (nexus-sv2wx.8): `conexus/.mcp.json` starts `nexus` and `nexus-catalog` as `uv tool run ... python launch/nx_launch.py nx-mcp` (and `nx-mcp-catalog`), where it named the two programs directly (RDR-228 Phase 1 Step 4). With no CLI the launcher serves a stand-in and the application shows a connected server; before, the servers failed to start.
- hook entries through the launcher (nexus-sv2wx.8): `conexus/hooks/hooks.json` runs the six `nx-hook` session-start entries and the session-end entry through `launch/nx_launch.py`, and adds `nx_setup_notice.py` as the first session-start entry. With no CLI each entry is one line and exit 0, where it was "Executable not found". The three `nx_hook_shim.py` entries and the `SubagentStart` `mcp_tool` entry are unchanged; against the stand-in that tool is absent and Claude Code records a non-blocking error the user does not see (T2 `nexus_rdr/228-research-20`). The `preflight` entry's timeout goes from 5 s to 10 s, as the other `nx-hook` entries have: through the launcher on Windows it took 2.2 s at the median and was cancelled once under load (nexus-217c3, T2 `nexus_rdr/228-research-25`).
- lockstep hook plugin set (nexus-gdllu): `conexus/hooks/scripts/version_lockstep_hook.py` reads the plugin list from the marketplace's own clone when the plugin runs from the versioned cache copy, which is every marketplace install. Before, it looked beside the cache directory, found nothing, used the built-in list and printed a fallback line on every session start. A dev checkout is read as before. This entry was under "Awaiting"; it is here because nexus-sv2wx.8 changes the same file and a deferred path is held back whole.
- lockstep action lookup (nexus-sv2wx.9): `conexus/hooks/scripts/version_lockstep_action.py` finds `nx` by the launcher's lookup, the install directory then `PATH`, as the hook that starts it does. It looked on `PATH` alone, so on a machine whose install is newer than the application's `PATH` the hook said an upgrade was dispatched and the action found no `nx`, every session. Deferred with the hook's own change below.
- lockstep hook with no CLI (nexus-sv2wx.8): `conexus/hooks/scripts/version_lockstep_hook.py` does nothing when `nx` does not resolve: no message, no action, no marker, on the version path and on the ref-drift path. Before, it told the model an upgrade had been dispatched and the action it started did nothing.
- setup process (nexus-sv2wx.15): `conexus/launch/nx_setup.py` is new (RDR-228 Phase 2 Step 2). One detached process that builds the CLI (`uv tool run ... nx self install --bootstrap`), then for mode `local` runs `nx init --yes --no-beads-prime` (or starts a provisioned, stopped service) and `nx doctor`, writing its stage to `setup/status.json` under the Nexus config directory, with a log, the "not now" marker and a lock directory kept alive by a heartbeat. The build is skipped only for an identified Nexus CLI: the generation layout's `nx` when `nx self install --bootstrap --dry-run` says nothing is left to do, or a `PATH` `nx` with `nx-mcp` beside it and a Nexus `--version` line; any other `nx` (Nrwl's) is never run. Child output goes straight into the log, a timed-out or orphaned step is killed with its children, a lost lock stops the run without a further status write, and every command prints one JSON object (state `error` on an unexpected failure), never a traceback. Its command line (`start`, `status`, `not-now`) and its importable `status_view`, `provisioned` and state constants are the one way to use that directory. Nothing starts it yet: the setup tools that call it are Step 3, and mode `cloud` is refused without changing anything because the credentials page is Step 5. It waits for the client release with `nx self install --bootstrap` (nexus-sv2wx.14), which the build step runs.
- CLI launcher (nexus-sv2wx.5): `conexus/launch/nx_launch.py` is new (RDR-228 Phase 1 Step 1). It takes the name of a CLI program and its arguments, finds the program and runs it: on POSIX it replaces itself with it, on Windows it starts it and returns its exit code. With no CLI a hook program (`nx-hook`, `nx-session-end-launcher`) is exit 0 with one line on standard error, and a server (`nx-mcp`, `nx-mcp-catalog`) is served by the stand-in beside it (`nx_standin.py`). It moves to the project directory first (a hook's payload `cwd`, else `CLAUDE_PROJECT_DIR`). The entries that start it are deferred below (nexus-sv2wx.8).
- stand-in servers (nexus-sv2wx.6): `conexus/launch/nx_standin.py` is new (RDR-228 Phase 1 Step 2). The launcher serves it when `nx-mcp` or `nx-mcp-catalog` does not resolve. In place of `nx-mcp` it answers the handshake, says in its instructions and in two tool descriptions that Nexus is not installed, and offers `setup` and `setup_status`; in Phase 1 `setup` installs nothing and answers with the manual commands (Phase 2 gives both tools their work: see the nexus-sv2wx.16 entry). In place of `nx-mcp-catalog` it offers no tools. It writes nothing to standard error.
- stand-in setup tools (nexus-sv2wx.16): `conexus/launch/nx_standin.py` gives `setup` and `setup_status` their work (RDR-228 Phase 2 Step 3). `setup` takes `mode` (`local`, `cli-only`, `not-now`; a managed service is refused) and, for `local`, `autostart` (false unless given), starts `nx_setup.py` beside it with its own Python and returns at once with the stage, how long the stage usually takes and when to ask again. `setup_status` reports the stage, the log path and the next step. Both go through `nx_setup.py`'s command line; the stand-in reads and writes no setup file. The names are the Phase 1 names; the arguments and descriptions change (the descriptions tell the model to ask the user first, and name `/conexus:setup`). They are the same tools, with the same schema and text, that the real `nx-mcp` registers (`nexus.mcp.setup_tools`); a test pins the two copies equal.
- launcher hands the setup script to the real server (nexus-sv2wx.16): `conexus/launch/nx_launch.py` sets `NX_SETUP_SCRIPT` to the path of `nx_setup.py` beside it when it hands off to the real `nx-mcp` (RDR-228 Phase 2 Step 3), in every state of the setup directory, which it does not read. An inherited value is replaced, and no other program gets it. The real server's `setup` tool starts that file; without the variable the tools start nothing and answer with the terminal commands.
- setup notice hook (nexus-sv2wx.7): `conexus/hooks/scripts/nx_setup_notice.py` is new (RDR-228 Phase 1 Step 3). A session-start script that prints nothing when `nx-mcp` resolves and, when it does not, tells the model Nexus is not installed and gives the commands that install it, for the user to run. It starts no process, opens no socket and writes no file. The `hooks.json` entry that runs it is deferred below (nexus-sv2wx.8).
- setup notice hook silent in a cloud session (nexus-217c3): `conexus/hooks/scripts/nx_setup_notice.py` prints nothing when `CLAUDE_CODE_REMOTE` is true. A Desktop conversation runs its hooks in a cloud workspace that has `uv` and no CLI, while the plugin's servers run on the user's machine; the notice there said Nexus was not installed when it was (measured 2026-10-13). When the machine has no CLI the stand-in's tool descriptions still say so.
