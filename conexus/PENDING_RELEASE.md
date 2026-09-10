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


## Awaiting the next release or plugin cut (pinned: v7.40.0)

- `conexus/hooks/scripts/subagent-start.sh`: RDR-205 "Identity and
  addressing" — parses `agent_id` from its own SubagentStart payload and
  adds one line to the injected `additionalContext` giving the agent its
  claimant id and mailbox address (`mailbox/<agent_id>`). No network I/O.
  bead: nexus-em75s.11
- `conexus/hooks/hooks.json`: two new `async: true` projection entries,
  each beside its blocking hook — `subagent-start-tuple-async.sh` on
  `SubagentStart`, `subagent-stop-tuple-async.sh` on `SubagentStop`. The
  three blocking TSV hooks (`agent-dispatch-expect.sh`,
  `subagent-start-stamp.sh`, `subagent-stop.sh`) are untouched.
  bead: nexus-em75s.11
- `conexus/hooks/scripts/subagent-start-tuple-async.sh`,
  `conexus/hooks/scripts/subagent-stop-tuple-async.sh` (new): thin,
  inert-safe wrappers — read the hook's own stdin payload, then
  background `tuple_ledger_project.py` with all three fds redirected to
  `/dev/null` before backgrounding, so the wrapper itself returns in
  milliseconds regardless of whether the installed harness honors
  `async: true` on this hooks.json entry (CA 4).
  bead: nexus-em75s.11
- `conexus/hooks/scripts/tuple_ledger_project.py` (new): the async
  projection body. Reads the client's cached storage-service and
  data-token lease files under `~/.config/nexus/`, POSTs the ledger
  start/report tuple to `/v1/tuples/out` with stdlib `urllib.request`
  (bearer in a header, never a subprocess argv), never mints, and on a
  missing or near-expiry data-token lease scoped to the resolved tenant
  skips and appends the reason to `<session_id>.tuple-projection.log`
  beside the session's `.expectations` ledger.
  bead: nexus-em75s.11
- `conexus/hooks/scripts/auto-approve-nx-mcp.sh`: RDR-205 Phase 2 review
  fix — the eight tuple-space MCP tools (`tuple_out`, `tuple_rd`,
  `tuple_in`, `tuple_ack`, `tuple_nack`, `tuple_registry`, `tuple_list`,
  `tuple_stats`) were registered by `nexus.mcp.core` without a matching
  allow-list entry, so every call prompted for permission instead of
  auto-approving.
  bead: nexus-em75s.12
