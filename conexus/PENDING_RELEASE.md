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

- subagent git guard exempts one ssh command to chas (nexus-yc7fs): `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` lets a subagent run `ssh chas-test '<git write>'`, whose git runs in chas's own clone. The command must be a single ssh to a chas alias; outside single quotes it may use only letters, digits, spaces and `_ . / : @ , + - =`, so nothing expands or redirects and no word can become an extra ssh option; options are limited to `-n -q -t -T` and four `-o Key=value` settings; and the remote command names no onward hop (`ssh scp sftp rsync`). Use `ssh -n` in place of `</dev/null`.



## Deferred to the next client release

ONE PATH PER BULLET, on the bullet's FIRST line. These wait for the client release that carries the rest of their bead: nexus-3fme8 (the hooks' Knowledge Map reads in `src/nexus/hooks/`), nexus-ox0c0 (the text IO sweep across `src/nexus`), nexus-ikoqj (the subprocess output decoding sweep across `src/nexus`) and nexus-fbtyl (the per-tool `alwaysLoad` flags in `src/nexus/mcp/core.py`; the `.mcp.json` half must not ship without them, or every `nexus` tool loads through tool search).

- lockstep hook text IO as UTF-8 (nexus-3fme8): `conexus/hooks/scripts/version_lockstep_hook.py` reads `plugin.json`, the marketplace and registry JSON and its markers, and writes its drift marker, as UTF-8. It used the locale encoding, cp1252 on Windows, where a non-ASCII marketplace description raised and the hook degraded to "no pin".
- lockstep action text IO as UTF-8 (nexus-3fme8): `conexus/hooks/scripts/version_lockstep_action.py` writes its marker and appends its log as UTF-8.
- endpoint resolve reads as UTF-8 (nexus-3fme8): `conexus/hooks/scripts/_endpoint_resolve.py` reads its lease and state files as UTF-8.
- lockstep hook reads git's output as UTF-8 (nexus-ikoqj): `conexus/hooks/scripts/version_lockstep_hook.py` decodes the `git rev-parse` it runs as UTF-8 with replacement. With `text=True` alone it used the locale encoding, cp1252 on Windows.
- lockstep action reads nx, uv and git output as UTF-8 (nexus-ikoqj): `conexus/hooks/scripts/version_lockstep_action.py` decodes the `uv tool dir`, `nx --version`, `nx upgrade` and install-command output it runs as UTF-8 with replacement.
- subagent git-write guard reads git's output as UTF-8 (nexus-ikoqj): `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` decodes both `git rev-parse` calls that tell a linked worktree from the primary as UTF-8 with replacement, so a non-ASCII checkout path no longer garbles the comparison on Windows.
- sn auto-approve snapshot read as UTF-8 (nexus-ox0c0): `sn/hooks/scripts/auto_approve_sn_mcp.py` reads its snapshot file as UTF-8.
- sn worktree guard reads .git as UTF-8 (nexus-ox0c0): `sn/hooks/scripts/worktree_guard.py` reads a worktree's `.git` file as UTF-8.
- nexus tools besides eight core ones load through tool search (nexus-fbtyl): `conexus/.mcp.json` drops the server-level `alwaysLoad: true` on `nexus`. The server now flags `search`, `query`, `memory_get`, `memory_put`, `memory_search`, `scratch`, `store_get` and `store_put` as always-loaded in their own tool metadata, so the other 36 `nexus` tools no longer sit in the first request of every session; Claude Code finds them through tool search. Measured on Claude Code 2.1.300: the same tool choice and success as all-eager, a 41% smaller first request. A server-level `false` would defer all 44 and lose the memory tasks, so the key is removed, not flipped.
