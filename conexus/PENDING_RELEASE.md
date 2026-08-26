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


## Awaiting the next release or plugin cut (pinned: v7.18.0)

- `conexus/hooks/scripts/version_lockstep_action.py` (nexus-utpuw.15) — gate 1
  no longer requires a uv receipt (False forever under the generation layout,
  which silently no-opped the whole auto-upgrade), and gate 3 calls
  `nx self install` on a generation box instead of `uv tool upgrade conexus`.
- `conexus/hooks/hooks.json` (nexus-utpuw.15) — the unconditional leg's failure
  message named `uv tool upgrade conexus` only.

**What is NOT protected until this ships.** On a migrated box the installed
plugin still runs the OLD action from the pinned tag, so auto-upgrade remains a
silent no-op there: the marker stays stale and the hook re-nudges forever while
nothing happens. Users are not currently migrated (nothing calls the migration
yet), so the live exposure is limited to boxes migrated by hand — but the moment
migration is wired up, this entry is the difference between working
auto-upgrades and a silent one. Manual remedy meanwhile: `nx self install`.

Note this pairing is deliberate and must ship TOGETHER. Shipping the gate-1 fix
without the gate-3 fix would be WORSE than the bug: the hook would proceed past
detection and run `uv tool upgrade conexus` on a generation box, rebuilding the
legacy uv tree and re-symlinking over the nexus-owned shims on every session
start (nexus-utpuw.7's accepted risk, automated).

- `conexus/agents/_shared/CONTEXT_PROTOCOL.md` (nexus-e3mak) — reserves the
  `review-completed` token: no agent may write it, only the session that owns
  the gate, once every mandated reviewer has run.
- `conexus/agents/code-review-expert.md` (nexus-e3mak) — same prohibition,
  stated at the point of use for one half of a stacked review gate.
- `conexus/agents/substantive-critic.md` (nexus-e3mak) — same, for the other
  half of the gate.

**What is NOT protected until this ships.** `pre_close_verification_hook.sh`
matches `review-completed` by substring across T1 and T2, so a reviewer's
handoff note carrying that token plus a bead id closes the gate it is only half
of. Observed twice on 2026-08-26 during RG-C: the code-review-expert wrote one
after reviewer 1 of 2, and the substantive-critic dispatched afterwards did the
same thing independently and caught itself. Until the pin advances, reviewers
still load from the tag and still do not know the token is reserved — so every
stacked gate run before the next release needs the marker checked by hand
(`nx scratch search review-completed`) before closing.

The lint that pins this guidance (`tests/test_review_marker_authorship_guidance.py`)
DOES run from the working tree and is live now; it protects the files from
silently losing the rule, not the sessions from lacking it.
