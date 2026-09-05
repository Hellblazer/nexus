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


## Awaiting the next release or plugin cut (pinned: v7.31.0)

- `conexus/skills/rdr-research/SKILL.md` (nexus-zu1q0): the add step now shells
  out to `nx rdr preamble rdr-research -- add <id> <text>`, which computes the
  next `<id>-research-N` sequence in Python and never overwrites an existing
  title. Until this ships, the installed skill still does the list-and-upsert
  itself and can land two adds on the same title.
- `conexus/hooks/scripts/agent-dispatch-expect.sh` (nexus-mqnkt): a real
  incident (session 49d1c3ab, 2026-09-01) left a START row with no matching
  EXPECT row and zero forensic trace — every skip path in this hook exited 0
  with nothing on stdout OR stderr, so a genuinely dropped write was
  indistinguishable from the hook never firing. Every skip that represents a
  write the hook could have made, but did not (source failure, an empty
  session_id, an `expectations_file` rejection, a swallowed validation error
  inside `expectations_expect`), now names itself on stderr; stdout is
  unchanged (still empty on every path — no risk to the dispatch). Until this
  ships, the installed hook stays fully silent on every skip.
- `conexus/hooks/scripts/routing/_lib.py` (nexus-gjv9b PART 2): `log_routing_event`
  now records to the engine's `routing_events` table (best-effort POST via
  `urllib`, ~250ms timeout) instead of appending to `routing_log.jsonl`; a
  write that cannot reach the engine (service down, or no
  `NX_SERVICE_HOST`/`PORT`/`TOKEN` exported — the common interactive-session
  case) is counted in `nx doctor`'s drop meter (`nexus.dropped_writes`,
  `hook="routing_events"`), never appended to the JSONL log. Requires an
  engine carrying the `routing_events` table + `POST /v1/telemetry/
  routing_events/record` route (engine tag TBD, see
  `docs/wire-contract-pending.md`'s nexus-gjv9b entries) — until both the
  plugin cut and the engine tag ship, the installed hook keeps writing
  `routing_log.jsonl` exactly as before, and `nx hook routing-stats`
  without `--from-store` keeps reading it.
