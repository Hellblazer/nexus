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

**Deferring a straddling entry (nexus-2x3qy, nexus-rbmja.10).** A plugin-only
release cherry-picks commits from `develop` onto a branch off the last client
tag (`release` skill, "Plugin-only fast lane"). The eligibility gate reads the
bead ids named in those commits and refuses the release when any other commit
on `origin/develop` since the tag that names the same bead touches a path the
lane does not ship (`src/`, `service/`, `scripts/`, `.github/`, `mcpb/`,
`conexus/plans/`, `conexus/daemon/`, release machinery under `tests/`, and so
on). Attribution is by commit message, matched on the whole bead id, so a
commit that does not name its bead is not seen by that check. To keep a
straddling entry out of the release, move it under
`## Deferred to the next client release` below and do not cherry-pick its
commits, so the whole bead ships together at the next normal release. A
deferred entry is exempt from the release-window "ledger must be empty" rule
above -- still declared there is correct, not stale -- and stays exactly where
it is until moved back deliberately.

---
## Awaiting the next release (pinned: v8.5.9)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/hooks/scripts/_audience.py`: `show` labels a missing project file "(no checkout)" outside a git checkout, instead of "(no account home)", nexus-sby49; `telemetry_child_env` also drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the telemetry child's environment (a project's settings can set them; on Windows the first kills a process that makes a default TLS context), nexus-ec37o.

- `conexus/hooks/scripts/_endpoint_resolve.py`: the bearer TLS context sets TLS 1.2 as an explicit floor (`minimum_version`), matching the CLI's `bearer_transport`; behaviour is unchanged (Python's client default is already TLS 1.2), the floor is now stated, nexus-ec37o. The module also drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the hook process's environment at import, before it builds a TLS context (nexus-ec37o).
- `conexus/launch/nx_setup.py`: the `nx setup` check's tokenless `GET /version` takes the proxy and extra CA of `config.yml` (`_endpoint_resolve.bearer_opener`); the two requests that carry the token take no proxy, no redirect and the trust store with no environment in it, nexus-ec37o.

- `conexus/hooks/scripts/compact_checkpoint.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/nx_hook_shim.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/nx_setup_notice.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/routing/credential_print_guard.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/routing/destructive_action_advice.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/settings_notice.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/stop_wake_guard.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/version_lockstep_hook.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/launch/nx_launch.py`: drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from the process environment before its first import (a project's settings can set them; OpenSSL reads the second when it starts and the first kills a Windows process), nexus-ec37o.

- `conexus/hooks/scripts/mailbox_drain.py`: the ask-answer frame says "the user" instead of a person's name and fences the relayed words with `user-words` in place of `sam-words`; nonce, record-sourced words and one-option authorization are unchanged (RDR-259 S6, nexus-sby49). The script also drops `SSLKEYLOGFILE` and `OPENSSL_CONF` from its environment before its first import, nexus-ec37o.

- `conexus/commands/nx-preflight.md`: the summary reads a check printed as SKIP (nx doctor outlasting its bound, nx not installed) as a WARN, GH #1689.
- `conexus/hooks/scripts/compact_checkpoint.py`: a docstring only; it names the one-line placeholder the continuation's in-progress-beads subsection now carries where it used to say "bd unavailable or no results", GH #1689.
- `prose-edit/skills/prose-edit/SKILL.md`: the rejections row says `--remove N` prints the renumbered list that is left and is run once, a stdin run takes "Yes, apply." as no pick, and the apply-question exception names a bare apply, nexus-scmcs.
- `prose-edit/skills/prose-edit/markup-route.md`: a bare "Yes, apply." after a held query counts as No on the query, applies in the same turn (even when every edit is held), and leaves the voice-card question to step 13, nexus-scmcs.
- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py`: the linked-worktree probe asks git for its git dir and common dir in one `rev-parse` instead of two, one process fewer per subagent Bash call that reaches it; a reply that is not exactly two lines fails closed as a failed call does (no bead; found diagnosing the Windows test set timeout, 2026-11-27).


## Deferred to the next client release

- `conexus/skills/asks/SKILL.md`: the mediator passes `--user-words` instead of `--sam-words`; it needs a client that has `--user-words`, so it ships with the client release that adds it, nexus-sby49.

