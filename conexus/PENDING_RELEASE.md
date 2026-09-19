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

- `conexus/commands/continuation.md`, `conexus/skills/orchestration/SKILL.md`:
  bead: nexus-q02nx.14 — both told the reader to reach the RDR-184 ledger by
  `source tests/e2e/lib/expectations.sh`. That file is deleted; the ledger is
  `nexus.hooks.expectations`, reachable as `nx-hook expectations_census` /
  `expectations_undeclared`. No source step, and nothing to keep in sync.
  WHEEL FLOOR: `nx-hook` is a console script, so these instructions need a
  conexus generation at or past the wheel that declares it — an older
  installed generation has no `nx-hook` shim and the command is not found.
  INERT until then, which for prose means a session reading the pinned plugin
  is told to source a file that no longer exists; the plugin copy at
  `conexus/hooks/scripts/expectations.sh` still exists, so that instruction
  still WORKS from the pinned tag, it is merely the older of the two paths.

- `conexus/hooks/scripts/tuple_ledger_project.py`:
  bead: nexus-q02nx.14 — comments only. The two mirrors of the ledger's state
  dir and path-safe charset guard now name `nexus.hooks.expectations` rather
  than the deleted shell file. Still MIRRORED rather than imported, and that
  is forced: this file is invoked by a bare `python3` from a detached wrapper
  and is stdlib-only by contract, so `nexus` is not on its path. Bead
  nexus-q02nx.20 moves the work into the server, where the import belongs and
  where the pair should collapse. No behaviour change.

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
  bead: nexus-06aei

- `conexus/hooks/scripts/stop_verification_hook.sh`: deleted the Check-2
  catalog-sync block, which was dead at three independent levels. Its guard
  tested for `$CATALOG_PATH/.git` and `documents.jsonl`, the git/JSONL
  catalog substrate RDR-158 P4 removed, so it could never be true; the
  `nx catalog sync` it guarded has raised unconditionally since conexus
  7.0.0, so the call could never have succeeded; and the call discarded its
  result with `|| true`, so no caller could ever have observed the outcome.
  Behaviour change is nil by construction — the block could not run, could
  not succeed, and could not be observed — but it is plugin surface, so it
  is declared. A hook-surface deletion, no tool or command change. Found by
  `tests/test_retired_command_callers_lint.py` on its first run; the
  surviving mentions of the command in that file are inside the replacement
  comment, which the lint does not flag because naming is not invoking.
  bead: nexus-06aei
