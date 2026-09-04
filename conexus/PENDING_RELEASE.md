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


## Awaiting the next release or plugin cut (pinned: v7.28.0)

- `conexus/skills/using-nx-skills/SKILL.md` (nexus-ht9m5): reworded in plain register, no rule or destination added or removed; em dashes and bold lead-ins gone so the always-injected guidance stops modelling the prose the Communication rules ban.
- `conexus/skills/orchestration/SKILL.md` (nexus-ht9m5): same register pass; 31 em dashes and every bold lead-in removed, three heading em dashes became colons, no rule, identifier, path, or fenced template changed.
- `sn/hooks/scripts/auto-approve-sn-mcp.sh`, `sn/hooks/scripts/auto_approve_sn_mcp.py`, `sn/hooks/scripts/serena-tools.txt` (nexus-jbt5x): the Serena allowlist is a snapshot generated from the revision pinned in `sn/.mcp.json`, not a hand-kept case list; 45 tools approve (the old list had 27, of which 4 were context-excluded, so 22 live tools including the whole LSP-backend navigation set and `jet_brains_debug`, the inspections, `replace_in_files`, `serena_info` had been prompting), `remove_project` and `open_dashboard` are refused by a named deny set, and the three the claude-code context excludes no longer approve.
- `sn/hooks/scripts/serena-section.md`, `sn/hooks/scripts/mcp-inject.sh` (nexus-jbt5x): the injected routing table names the tools the pinned Serena ships per backend and drops the three excluded ones; both sections inject for every subagent, the task-text skip heuristic is gone.
- `sn/hooks/scripts/routing/README.md`, `sn/hooks/scripts/routing/_lib.py`, `sn/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py`, `sn/hooks/scripts/routing/registry.yaml`, `sn/hooks/scripts/_run_python_hook.sh` (nexus-jbt5x): deleted; unregistered since a69bea883.
- `conexus/hooks/scripts/routing/README.md` (nexus-jbt5x): docs only; records that sn no longer ships a routing rule and that the vendored-copy drift guard went with the last vendored copy.
- `sn/.mcp.json` (nexus-jbt5x): Serena pinned to revision 801a388c (what this box already ran) and Context7 to 4.0.5; a fresh MCP spawn previously resolved git HEAD and npx latest.
