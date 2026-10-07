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



## Awaiting the next release or plugin cut (pinned: v7.72.1)

ONE PATH PER BULLET, on the bullet's FIRST line.

- hook interpreter off every `.venv` (nexus-f9bgu.36, RDR-224 finding C): `conexus/hooks/hooks.json` launches all seven uv entries as `uv tool run --directory ${CLAUDE_PLUGIN_ROOT} --no-config --quiet --python >=3.12 python <script>`. `uv run --no-project` executes a `.venv` Python it finds in its starting directory or any parent: from the hook's cwd that is the project, so a cloned repository could supply the interpreter that runs every hook, and from the plugin root it is `~/.venv` or, on Windows, a `C:\.venv` any local user can create. A tool environment never consults a `.venv`. A script's process cwd is the plugin root from here on.
- hook interpreter off every `.venv` (nexus-f9bgu.36): `sn/hooks/hooks.json` launches its four uv entries the same way, for the same reason. The sn scripts already take the project from the payload's `cwd`.
- hook exec off the cwd (nexus-f9bgu.36, RDR-224 finding A): `conexus/hooks/scripts/_exec_path.py` is new, a stdlib `which_off_cwd` that resolves an executable on PATH alone (PATHEXT honoured, relative PATH entries skipped on Windows) and returns an absolute path. On Windows a bare name is searched in the cwd first and a hook's cwd is the project, so a planted `nx-hook.exe`, `git.exe` or `uv.exe` would have run. POSIX behaviour is `shutil.which`, unchanged.
- hook exec off the cwd (nexus-f9bgu.36): `conexus/hooks/scripts/nx_hook_shim.py` spawns `nx-hook` by the absolute path `which_off_cwd` returns, and treats a CLI found only in the cwd as absent (exit 0 with the not-installed note). It also starts `nx-hook` in the project directory (the payload's `cwd`, else `CLAUDE_PROJECT_DIR`), since its own cwd is now the plugin root (finding C). No other change on POSIX.
- hook exec off the cwd (nexus-f9bgu.36): `conexus/hooks/scripts/version_lockstep_action.py` resolves `uv` and `nx` through `which_off_cwd` and spawns the absolute path; one not on PATH is a failed command. No change on POSIX.
- hook exec off the cwd (nexus-f9bgu.36): `conexus/hooks/scripts/version_lockstep_hook.py` resolves `git` through `which_off_cwd` for the ref-drift check. No change on POSIX.
- hook exec off the cwd (nexus-f9bgu.36): `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` resolves `git` through `which_off_cwd` for the linked-worktree check, which runs with the project as cwd; none on PATH is the existing undeterminable (fail-open) answer. A payload with no `cwd` now falls back to `CLAUDE_PROJECT_DIR`, and with neither the project is undeterminable (finding C: the process cwd is the plugin root, so it is no longer read). No other change on POSIX.
- hook exec off the cwd (nexus-f9bgu.36): `conexus/hooks/scripts/_interpreter.py` finds `python3.13` / `python3.12` through `which_off_cwd` before it re-execs. Its dev-venv match compares against `CLAUDE_PROJECT_DIR` instead of the process cwd (finding C), and finds no match when that is unset. No other change on POSIX.



## Deferred to the next client release

