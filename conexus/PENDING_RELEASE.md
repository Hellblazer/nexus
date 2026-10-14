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
- session-start hook, eight states (nexus-sv2wx.17): `conexus/hooks/scripts/nx_setup_notice.py` reports the first state that matches (RDR-228 Phase 2 Step 4): a setup run is live; the CLI is found and cannot start; the CLI resolves and a service is usable (nothing, whatever an earlier run left); a run was interrupted; the last run failed; no CLI (the one question, with the table of choices, or one line naming `/conexus:setup` when the "not now" marker exists); a provisioned local service is down; no service is set up. A usable service is the CLI's own definition (`_endpoint_resolve.endpoint_usable`), so a saved address with no token is not one. It imports `nx_setup.status_view` and `nx_standin.render` (the text `setup_status` returns, so every suggested call asks the user first in the same words), starts no process, opens no socket, writes nothing, and bounds all of its file reads (the status, its error record, the lease) to 30 ms of Windows sharing-violation retry together. The user's choice is honoured (Sam, 2026-10-14): the "not now" marker turns the no-CLI state, the service-down and no-service states and the stale failed or interrupted report into one quiet line naming `/conexus:setup` that asks nothing, and a recorded CLI-only run (mode `cli-only`, state `done`) does the same for service-down and no-service; a live run, a CLI that cannot start and a usable service are never quieted. It speaks now on a machine with a CLI and no service, where it used to be silent.
- found-and-cannot-start check (nexus-sv2wx.17): `conexus/hooks/scripts/_cli_lookup.py` gains `cannot_start`, the files-only answer to whether a program the lookup found can run: on Windows `pyvenv.cfg`'s `home` directory is gone (what `uv` leaves when it prunes a Python); on POSIX `home` gone is not enough by itself, because the interpreter can still start through the venv's `bin/python` link, and counts beside a `python*` link in the venv that points nowhere, while a shebang that names a missing or non-executable interpreter, or an `env` program not on `PATH`, counts alone (the whole first line is read). The CLI states the same rule for `--bootstrap` and a test runs both over one matrix. It claims "cannot start" only on positive evidence and starts no process.
- usable-service check (nexus-sv2wx.17): `conexus/hooks/scripts/_endpoint_resolve.py` gains `endpoint_usable`, the plugin's restatement of `resolve_service_endpoint()` returning (the four legs: `NX_SERVICE_URL`, a saved `service_url`, `NX_SERVICE_HOST`/`NX_SERVICE_PORT`, a live lease; a token or mint credential required on the URL legs and a port and token on the others), `read_lease_text` and `read_storage_service_lease` take a retry budget (the hook spends 30 ms of it across all its reads), `read_storage_service_lease` takes `grace` (only the usable-service check passes it: a lease past its TTL is kept while its recorded owner process is alive, within ten TTLs, on a loopback host, as the CLI's reader grace does without its `/health` probe), and `read_config_yml_credentials` can read more keys and a flow-style `credentials: {...}` mapping on one line or several. `resolve_base_url` is unchanged.
- launcher serves the stand-in for a CLI that cannot start (nexus-sv2wx.17): `conexus/launch/nx_launch.py` asks `_cli_lookup.cannot_start` of a program it found (Sam, 2026-10-14). A hook program gets one line on standard error and exit 0; `nx-mcp` and `nx-mcp-catalog` get the stand-in, which says "Nexus is installed but cannot start; setup repairs it" with the reason; any other program fails with one line. A program the system will not execute is treated the same way: before, a server got exit 1 and no stand-in.
- stand-in question and installed-but-cannot-start mode (nexus-sv2wx.17): `conexus/launch/nx_standin.py` puts the one question in the server instructions (the table of choices without the Cloud row, the Local start-at-login follow-up, "ASK THE USER FIRST", `/conexus:setup`) and, when the launcher says the CLI cannot start, opens its instructions, both tool descriptions and `setup_status` with "Nexus is installed but cannot start; setup repairs it" and the reason. `QUESTION_TABLE` is the one copy the hook imports. In that mode `setup_status` reports a setup run (running, done, failed, interrupted) as it is and puts the repair offer only over "no setup has run"; a missing setup process says "cannot start" and gives the repair command. The `setup` description shows the on-disk sizes (2.4 GB, 1.6 GB) beside the download sizes.
- setup process repairs a CLI that cannot start (nexus-sv2wx.17): `conexus/launch/nx_setup.py` no longer skips the build for an `nx` that is found and cannot start. A generation layout `nx` or a `PATH` `nx` with no way to start is not asked anything and the build runs (`nx self install --bootstrap`, which now replaces such a generation); a dry run that cannot even be spawned counts as a build to do; a build that leaves an `nx` that cannot start fails the step and says why; a `PATH` `nx` that is a link into a legacy `uv tool install` tree is judged by that tree. Its "a service already answers" test is the CLI's definition, so `local` no longer calls a saved address with no token a finished service.
- `/conexus:setup` (nexus-sv2wx.17): `conexus/commands/setup.md` is new. It calls `setup_status`, shows the table with the choices still open, asks first, and calls `setup` with the answer. It has no `nx` shell line (a named exemption in `tests/test_plugin_structure.py`). When the server offers no setup tools, which means the CLI is older than the release that carries them, it says so and gives `nx self install` then `nx upgrade` (or `uv tool upgrade conexus` on the uv-tool layout) as text, after telling the model to look at `/mcp` first because a server that is not connected offers no tools.
- command registry (nexus-sv2wx.17): `conexus/registry.yaml` lists `setup` among the utility commands.
- lazy imports for the session-start hook (nexus-sv2wx.17): `conexus/hooks/scripts/_exec_path.py` imports `shutil` only on POSIX, where `shutil.which` is used, and `conexus/hooks/scripts/_endpoint_resolve.py` and `conexus/launch/nx_setup.py` import `hashlib`, `urllib.parse`, `secrets` and `argparse` inside the functions that need them. The hook imports these files and has 100 ms; no behavior changes.
