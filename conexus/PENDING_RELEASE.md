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



## Awaiting the next release or plugin cut (pinned: v7.41.0)

- `conexus/hooks/scripts/tuple_ledger_project.py`: nexus-0zsmg — endpoint
  resolution now mirrors ``nexus.db.service_endpoint.resolve_service_endpoint``'s
  FULL precedence, not just the local-supervisor leg: ``NX_SERVICE_URL`` env,
  then the persisted ``config.yml`` ``credentials.service_url`` (``nx config
  set service_url``), then ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` env, then
  the local lease. Before this fix a cloud-mode box with no
  ``NX_SERVICE_URL`` exported (an all-persisted-config install — the common
  shape after ``nx init``) skipped every ledger projection. Also: the
  ``report`` kind now tolerates a missing ``agent_type`` (the SubagentStop
  payload does not reliably carry it; the ``ledger.yaml`` template's
  ``agent_type`` dimension is not ``required: true``) — ``session_id`` +
  ``agent_id`` stay mandatory for both kinds.
  bead: nexus-0zsmg
