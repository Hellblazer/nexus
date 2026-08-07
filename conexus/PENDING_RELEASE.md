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

## Awaiting the next release (pinned: v7.3.0)

- `conexus/hooks/scripts/expectations.sh` — owes-report lock: NX_EXPECT_LOCK_TRIES
  env knob (base-10 coerced, 600-try clamp) + stderr warning on fail-open
  unlocked proceed (nexus-4b8sz; observable under `claude --debug` only).
  Installed sessions keep the silent 10-try fail-open until the next plugin
  release.
- `conexus/hooks/scripts/expectations.sh` — `expectations_undeclared` now exits
  3 with a stderr NOTE when the session has no ledger file, instead of the
  indistinguishable-from-clean rc=0 (nexus-ahl9v; a mistyped session id
  previously audited as clean). Installed sessions keep the old rc=0
  behavior until the next plugin release.
- `conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py` —
  rules 2 (push-to-main, nexus-vduer) and 3 (review-coverage gate,
  nexus-4av2n) are now scoped to the nexus repo via `_repo_scope_is_nexus`
  (origin remote URL basename `nexus` OR the conexus plugin marker file at
  toplevel — an OR, per nexus-w3apo) and no-op in any other repo
  (nexus-vscgz; evidence: the hook denied a plain `git push origin master`
  in an unrelated hobby repo with no `develop`, no marketplace surface).
  Rule 1 (wildcard `git add`) stays global, unaffected. Installed sessions
  keep firing rules 2+3 in every repo until the next plugin release.
- `conexus/hooks/scripts/routing/registry.yaml` — rationale updated to
  record the nexus-vscgz repo-scope fix above. Installed sessions read the
  old rationale until the next plugin release (documentation-only drift,
  no behavioral effect).
- `conexus/skills/orchestration/SKILL.md` — documents the
  `expectations_undeclared` rc=3 no-ledger contract (nexus-ahl9v, landed
  599e4980) alongside the rc 0/1/2 meanings. Backfilled entry: the commit
  shipped without declaring it and the drift-ledger CI job was vacuous-green
  at the time (nexus-05m1i). Installed sessions read the pre-rc=3 skill text
  until the next plugin release.
