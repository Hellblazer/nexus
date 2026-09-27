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



## Awaiting the next release or plugin cut (pinned: v7.63.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/skills/tumbler-footnotes/SKILL.md` — nexus-sxiay: new standalone
  skill wrapping `nx catalog footnotes` (GH #896 ask 4, the in-place
  `nx://catalog/<tumbler>` -> GFM-footnote converter, including `--check` and
  `--to-links`). Inert until the next release/cut ships it — until then,
  `/conexus:tumbler-footnotes` resolves to nothing installed.

## Deferred to the next client release

- nexus-5l8i8: `conexus/hooks/hooks.json` rewires the RDR-184 ledger's two
  writers (`agent_dispatch_expect`, `subagent_start_stamp`) from `mcp_tool`
  to the command tier, through two new `nx-hook` verbs
  (`agent-dispatch-expect`, `subagent-start-stamp`) added to
  `src/nexus/_hook_runtime/entry.py`'s `VERB_TABLE`. An `mcp_tool` hook
  depends on this session's own MCP server being connected, and root cause
  (session 81d1d28b's transcript, 2026-09-27) showed an MCP outage
  dropping EXPECT rows while the matching SubagentStart still wrote a
  START row after reconnect — read by the retro audit as an undeclared
  dispatch. Deferred because the verbs are wheel content
  (`src/nexus/_hook_runtime/entry.py`, `src/nexus/hooks/agent_dispatch_expect.py`,
  `src/nexus/hooks/subagent_start_stamp.py`, `src/nexus/mcp/hooks.py`), so
  the hooks.json rewiring ships with the client release that makes the
  verbs resolve — an installed CLI predating this release would exit 2 on
  a direct `nx-hook` call naming either verb, which is exactly what the
  shim-routed form in hooks.json avoids.
- nexus-5l8i8: `conexus/skills/orchestration/SKILL.md` item 1's prose named
  `hook_agent_dispatch_expect` as the MCP tool that writes the EXPECT row;
  corrected to say the writer fires on the command tier
  (`nx-hook agent-dispatch-expect`) and the MCP tool is registration-only,
  same reasoning and same wheel-content dependency as the bullet above.
- `conexus/skills/orchestration/SKILL.md` — nexus-xxvv3: new "Resuming a
  Worktree Agent After a /clear" section. A SendMessage-resumed agent runs in
  the primary checkout, so the subagent git guard refuses its commits and its
  own hand-back must use `git -C <worktree>`; the ledger credits it as
  RESUMED. Deferred because the bead's ledger half is wheel content
  (`src/nexus/hooks/expectations.py`, `subagent_start_stamp.py`), so the
  doc ships with the client release that makes it true.
- `conexus/hooks/scripts/mailbox_drain.py` — nexus-3lc5s: the claim loop's
  budget stop now logs a SKIP instead of returning silently. Deferred because
  the same fix is in the wheel copy (`src/nexus/hooks/mailbox_drain.py`), so
  both copies ship together in the next client release.
- nexus-smsau: `conexus/hooks/scripts/version_lockstep_hook.py` now derives
  its plugin set from marketplace.json (a `known_plugins()` reader keyed off
  the `CLAUDE_PLUGIN_ROOT` clone this hook already resolves), replacing the
  hardcoded `PLUGINS = ("conexus", "sn")` tuple that silently dropped any
  plugin marketplace.json listed without a matching source edit. Deferred
  because the wheel-side half of the same fix (`src/nexus/plugin_registry.py`,
  `src/nexus/plugin_lockstep.py`, `src/nexus/routing_stats.py`) is wheel
  content, so both derivations ship together at the next client release --
  until then the hook's own fallback and the wheel's fallback must keep
  agreeing on the same hardcoded set.

_The four RDR-215 straddling beads deferred here by nexus-2x3qy
(nexus-t9klx, nexus-z9cz2, nexus-silj0, nexus-veh77) shipped with the 7.58.0
client release, except their hooks.json entries: 7.58.0 kept the v7.57.0
plugin-script entries, because an older `nx-hook` exits 2 on a verb it does not
know. Moving those entries to `nx-hook` verbs is future plugin-surface drift
and gets declared here when it lands; version lockstep never moves._
