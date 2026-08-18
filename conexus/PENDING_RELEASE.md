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


## Awaiting the next release (pinned: v7.10.0)

- `conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py` — nexus-2e874: malformed shell quoting no longer silently drops a push segment (review-coverage gate was fully bypassable by one stray quote); degraded quote-blanked tokenization fallback.
- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` — nexus-2e874: same degraded-tokenization fallback; a stray quote no longer hides a subagent git write from the shared-tree guard.
- `conexus/hooks/scripts/pre_close_verification_hook.sh` — nexus-2e874: BD_VERBS matcher degrades instead of skipping; a stray quote in a `--reason` value no longer bypasses the bd-close review gate.
