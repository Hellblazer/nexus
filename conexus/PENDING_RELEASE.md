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



## Awaiting the next release or plugin cut (pinned: v7.69.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- cleanup step A1 (nexus-0r1uz): `conexus/hooks/hooks.json` drops the six entries for the RDR-184 ledger and RDR-205 projector hooks and the behaviour census (SessionStart `behaviour_census.py`, PreToolUse `agent-dispatch-expect`, SubagentStart `subagent-start-stamp` and `subagent-start-tuple`, SubagentStop `subagent-stop` and `subagent-stop-tuple`); the SubagentStop event is no longer wired.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/behaviour_census.py` is deleted; its SessionStart entry is gone and the module it fed (`nx census`) no longer exists.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/mailbox_drain.py` has comment-only edits dropping references to the deleted `tuple_ledger_project.py` hook; no behaviour change.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/_endpoint_resolve.py` drops `resolve_endpoint_and_token`, the credential policy only the deleted ledger projector used; `resolve_base_url` and the readers are unchanged.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/routing/_lib.py` has comment-only edits dropping references to the deleted census producer and projector; no behaviour change.
- cleanup step A1 (nexus-0r1uz): `conexus/skills/orchestration/SKILL.md` deletes the Background-Teammate Ledger section, the ledger-reading wait-for-a-report section and the ledger-credit bullet of the resume section, because the hooks that wrote the ledger are deleted.
- cleanup step A1 (nexus-0r1uz): `conexus/commands/continuation.md` drops Step 0 audits 3 and 4 (declaration-completeness and the scenario-27 payload tripwire), which read the deleted ledger.
- cleanup step A1 (nexus-0r1uz): `conexus/README.md` drops the hook table rows and the command-tier explanation for the deleted ledger, projector and census hooks.
- cleanup step 15 (nexus-0r1uz): `conexus/skills/writing-nx-skills/SKILL.md` points at the renamed frontmatter test (`test_every_skill_frontmatter_is_valid`); text pointer only.
- cleanup step 12 (nexus-0r1uz): `conexus/skills/rdr-audit-checklist/SKILL.md` drops the `schedule` and `unschedule` subcommands and their plist and crontab templates; `list`, `status` and `history` stay. The template files under `scripts/` they pointed at are deleted.
- cleanup step 12 (nexus-0r1uz): `conexus/commands/rdr-audit.md` drops the `schedule` / `unschedule` text; the management subcommands are `list`, `status` and `history`, all read-only.
- cleanup step 7 (nexus-0r1uz): `conexus/skills/orchestration/SKILL.md` drops its text about the agent-verify-claims checker script (the script is deleted); the orchestrator still re-runs each reported COMMAND itself.
- cleanup step 7 (nexus-0r1uz): `conexus/skills/mailbox/SKILL.md` drops the unacked-request sweep bullet and its success criterion, since the inbound-relay-acks sweep script is deleted.

## Deferred to the next client release

_Empty. The entry deferred here (nexus-wbfpw.41, the `skills/upgrade/SKILL.md` text for the `rdr192-manifest-backfill` rung) shipped with the 7.68.0 client release._
