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



## Awaiting the next release or plugin cut (pinned: v7.54.0)

- `conexus/agents/codebase-deep-analyzer.md`, `conexus/agents/deep-research-synthesizer.md`: both told the agent that
  `nx search` / the `search` MCP tool provides "semantic search + ripgrep +
  git frecency". The ripgrep path is deleted (nexus-06aei) — it was opt-in,
  default-off, nothing enabled it, and it cost 1.9 GB of line caches on this
  box with one past its 500 MB cap and therefore silently truncated. Both
  lines now say "semantic search + git frecency", which is what the surviving
  `--hybrid` / `hybrid_default` switch actually does. These two changes are
  prose only and carry no tool, hook or command surface change — that is a
  statement about THESE edits, not a claim that every mention of `--hybrid`
  in the plugin was swept. `conexus/skills/architecture/SKILL.md` still
  recommends `nx search --hybrid` for discovery in three places and is
  deliberately unchanged: it is still true (the flag exists and still blends
  frecency), just narrower than when written.
