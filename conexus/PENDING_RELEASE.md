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



## Awaiting the next release or plugin cut (pinned: v7.82.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/version_lockstep_hook.py` (nexus-ehg1y): with a missing or stale `cli_lockstep_marker` (a hand install never writes one; a pip or dev-tree install cannot have the action rewrite it) the lockstep hook runs `nx --version` (3 s bound) and treats a CLI not older than the plugin as in lockstep, by the action's own `satisfies` rule, so a matching CLI no longer gets a false nudge and an upgrade dispatch. A marker equal to the plugin's version still decides alone, with no subprocess.
- `conexus/launch/nx_setup.py` (nexus-ehg1y): a docstring in `_record_lockstep` now names what the lockstep hook does when the marker is missing; no behaviour change.

- Description without angle brackets (nexus-0nskc): `conexus/skills/mailbox/SKILL.md` writes the address placeholder as ADDRESS in its description, since Claude Desktop's plugin sync rejects angle brackets there.

- Description without angle brackets (nexus-0nskc): `conexus/skills/tumbler-footnotes/SKILL.md` writes the tumbler placeholder as TUMBLER in its description, since Claude Desktop's plugin sync rejects angle brackets there.

- Description without angle brackets (nexus-0nskc): `conexus/agents/code-review-expert.md` asks to be briefed with a "Ship when ..." line stating the acceptance bar, in place of the angle-bracket placeholder Claude Desktop's plugin sync rejects in descriptions.

- Description without angle brackets (nexus-0nskc): `conexus/agents/substantive-critic.md` asks to be briefed with a "Ship when ..." line stating the acceptance bar, in place of the angle-bracket placeholder Claude Desktop's plugin sync rejects in descriptions.

- Description without angle brackets (nexus-0nskc): `conexus/commands/continuation.md` writes the path placeholder as PATH in its description, since Claude Desktop's plugin sync rejects angle brackets there.


- sequential-thinking survives hostile stdio (nexus-rwv5i): `conexus/mcp/sequential_thinking.py` answers input that is not UTF-8, nested past the recursion limit, an integer too long to parse, or a lone surrogate or NaN in an id with a JSON-RPC error and goes on, leaves quietly when the client stops reading (at the write and again at interpreter exit) or when stdin or stdout is closed at start, and writes nothing to stderr in any of them. Replies are ASCII-escaped JSON. Carries over the fixes the RDR-228 stand-in review made (nexus-sv2wx.6).


## Deferred to the next client release
