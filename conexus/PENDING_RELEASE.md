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



## Awaiting the next release or plugin cut (pinned: v7.61.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/routing/credential_print_guard.py` — new (nexus-wauo1.22)
  PreToolUse:Bash guard (RDR-219 Gap 2): denies a keychain read naming
  `Claude Code-credentials`/`nexus-automation-oauth-token`, a
  `.credentials.json` read, a command naming a protected variable
  (`$CLAUDE_CODE_OAUTH_TOKEN`, `printenv` of it, python reading it), and
  reads of other processes' environments (`ps -E`, `/proc/*/environ`).
  Whole-environment dumps are not denied, since Claude Code removes the
  token from its own environment. NO escape token. Stdlib-only, no `nexus`
  import, never ported.
- `conexus/hooks/hooks.json` — registers the guard above (nexus-wauo1.22) under the
  PreToolUse:Bash matcher, same shape as
  `subagent_git_write_requires_orchestrator.py`. **RECORDED RESIDUAL
  (bead nexus-wauo1.22):** this adds a sixth bare `python3` hooks.json
  entry, the same interpreter-discovery shape RDR-215 and nexus-t9klx set
  out to eliminate so a native Windows client becomes viable (stock
  Windows has no `python3` on PATH). `8664d8ec8` restored the
  plugin-script entries for CLI-skew reasons, which is the precedent this
  guard follows; it is not yet ported to an `nx-hook` verb. **The guard
  does not run on native Windows** until that port lands — a Windows
  Bash call reaches no credential-print check from this guard, RDR-219
  Gap 2's only mitigation on that platform being the underlying fact that
  `CLAUDE_CODE_OAUTH_TOKEN` is deleted from Claude Code's own environment
  before a Bash-tool child ever starts.
- `conexus/hooks/scripts/routing/registry.yaml` — the guard's rule entry (nexus-wauo1.22),
  `credential_print_guard`, `fail_closed:
  false` (the marker-scoped split lives inside the hook itself).
- `conexus/hooks/scripts/routing/README.md` — cap accounting for the new rule (nexus-wauo1.22):
  updated cumulative-cap table; nx's own
  PreToolUse:Bash count reaches the RDR-121 cap of 4.
- `conexus/hooks/scripts/routing/credential_print_guard.py` — amended (nexus-wauo1.39)
  RDR-219's "nx-mcp dispatch grant" adds `NX_HARNESS_CLAUDE_OAUTH_TOKEN` to
  `CREDENTIAL_ENV_VARS`, so the guard also denies a shell expansion
  (`echo $NX_HARNESS_CLAUDE_OAUTH_TOKEN`) or `printenv` of the harness-side
  automation-token name, the same as the protected `CLAUDE_CODE_OAUTH_TOKEN`
  name already was. No new rule, no registry/README change — the pattern
  set is built from the one tuple, so this is a data-only amendment to the
  existing guard.

## Deferred to the next client release

_Empty. The four RDR-215 straddling beads deferred here by nexus-2x3qy
(nexus-t9klx, nexus-z9cz2, nexus-silj0, nexus-veh77) shipped with the 7.58.0
client release, except their hooks.json entries: 7.58.0 kept the v7.57.0
plugin-script entries, because an older `nx-hook` exits 2 on a verb it does not
know. Moving those entries to `nx-hook` verbs is future plugin-surface drift
and gets declared here when it lands; version lockstep never moves._
