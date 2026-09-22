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



## Awaiting the next release or plugin cut (pinned: v7.57.0)

ONE PATH PER BULLET, on the bullet's FIRST line. The parser reads backtick
spans from a bullet line only, so a path on a continuation line is invisible
to it — which is exactly how two of these went undeclared and red'd develop
on 2026-09-22. A bullet that wraps is fine; a PATH that wraps is not.

- nexus-t9klx — `conexus/hooks/hooks.json`: all five bare-`python3` entries
  are being re-pointed at `nx-hook` verbs, three of them so far
  (`behaviour-census`, `version-lockstep`, `phase-review-close-gate`). Stock
  Windows has no `python3` on PATH, so those entries could never fire there;
  a console script gets a real `.exe` shim from the installer. Sessions on
  v7.57.0 keep running the old entries against their own copies until a new
  pin ships.

- nexus-t9klx — `conexus/hooks/scripts/behaviour_census.py`: DELETED, ported
  to the `behaviour-census` verb.

- nexus-t9klx — `conexus/hooks/scripts/version_lockstep_hook.py`: DELETED,
  ported to the `version-lockstep` verb.

- nexus-t9klx — `conexus/hooks/scripts/version_lockstep_action.py`: DELETED,
  ported to `nexus.hooks.version_lockstep_action` and dispatched by module.
  It moved with its dispatcher rather than being reached back into the
  plugin: one caller is enough to keep a plugin-resident script alive, which
  is what RDR-215 removes.

- nexus-t9klx — `conexus/hooks/scripts/routing/phase_review_close_requires_gate.py`:
  DELETED, ported to the `phase-review-close-gate` verb. The routing
  framework's one fail_closed rule; it stays on the command tier, which is
  where a rule that must still deny when it crashes belongs. `_lib.py` moved
  into the wheel with it, dropping the stdlib endpoint mirror for the
  client's own primitives.

(The previous entry, `conexus/hooks/scripts/preflight.py`'s deletion for
nexus-sa187, went live when `source.ref` advanced to `v7.57.0`.)
