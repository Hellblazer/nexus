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

---


## Awaiting the next release or plugin cut (pinned: v7.24.0)

- `conexus/hooks/scripts/t2_prefix_scan.py` — bead nexus-znvjd: the
  SessionStart T2 summary prefers the client's cached data-token lease
  (`<config_dir>/data_token_lease.<digest>`, written by
  `nexus.db.data_token`) for the resolved host over the static
  `service_token`, on every resolution leg; a 401 with no usable lease
  names the lease path. Until this ships, every session on an armed
  pass-through box (RDR-005 step (d): the persisted service_token is the
  scope=mint-locked credential) prints `WARNING: T2 memory unreachable:
  ... HTTP 401 for /v1/memory/projects` and injects no T2 summary. nx CLI
  and MCP T2 reads are unaffected; only this hook presented the static
  token.
