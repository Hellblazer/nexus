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



## Awaiting the next release or plugin cut (pinned: v7.83.1)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/routing/_lib.py` (nexus-m4lej): `log_routing_event` defaults to the session id of the payload `run_hook` read, so the credential-print and subagent git-write guards' `routing_events` rows carry a session; the drop meter keeps it too.


## Deferred to the next client release

- `conexus/skills/mailbox/SKILL.md` (nexus-zl1jv, nexus-e4kls, nexus-ggmg7): `from` is a claim and the tenant is the boundary (JDR-002); `mailbox_send` refuses a session id in `from_address` other than this session's own. The sender's default `from` is the tuple-watch session marker, then `NX_T1_SESSION_ID`, then `NX_SESSION_ID` (a UUID, unique per server, set in a self-configured MCP server entry); the skill says so, gives the subscribe asymmetry (env variables only, no marker) and says a non-Claude-Code host reads with `tuple_in`/`tuple_rd`. Client-side code for the fallback ships with the wheel.
  Also (nexus-ggmg7): one sentence on the `unverified` mark and what an unmarked message does not prove.
- `conexus/hooks/scripts/mailbox_drain.py` (nexus-ggmg7, F3 step 2c, JDR-002): the plugin drain hook marks a message `[unverified]` (or `[unverified, sent under session <S>]` for an agent-id `from`) when its `from` does not match the `sender_session` the engine stamped, or the stamp is null; a row with no `sender_session` key (an engine without the stamp) gets no mark. Needs the engine stamp (nexus-fetre) to mean anything: against an older engine nothing is ever marked. Until nexus-g2ckl lands, every ack reply reads unverified (reply rows are stamped null). No mark is not verification; any data-token holder can mint a session token for any session id.
- `conexus/hooks/scripts/_tuple_sender.py` (nexus-ggmg7): new stdlib-only sibling the drain hook imports; byte-identical to `src/nexus/tuple_sender.py` (pinned by `tests/test_tuple_sender_label.py`).
