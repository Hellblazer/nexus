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



## Awaiting the next release or plugin cut (pinned: v7.71.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- hydration cap wording (nexus-rf87b): `conexus/skills/nexus/reference.md` calls the `store_get_many` cap the "300-record read cap (MAX_QUERY_RESULTS)" rather than a ChromaDB one, matching SKILL.md. Text only.
- service identity (nexus-f9bgu.16): `conexus/hooks/scripts/_endpoint_resolve.py` derives the lease file name from a stdlib mirror of `service_identity()` (uid on POSIX, unchanged; the user SID on Windows) instead of a bare `os.getuid()`. No behaviour change on POSIX.
- owner-only lease check (nexus-f9bgu.22): `conexus/hooks/scripts/_endpoint_resolve.py` refuses a local-supervisor lease whose ACL grants another account on Windows (a stdlib mirror of `nexus._winsec.owner_only_problem`) instead of testing `st_mode` group/other bits, which Windows reports as `0o666` for every file. POSIX behaviour and its refusal message are unchanged.
- hook launcher (nexus-efk2h): `conexus/hooks/hooks.json` launches its seven plugin-resident entries (version-lockstep, subagent-git-write-gate, credential-print-guard, mailbox-drain, and the three nx-hook shim entries) as `uv run --no-project --no-config --quiet <script>` instead of `python3 <script>`. Stock Windows has no `python3` on PATH, so those hooks never fired there. A machine with no Python 3.12 or newer has uv fetch one on the first hook run.
- hook interpreter floor (nexus-efk2h): `conexus/hooks/scripts/version_lockstep_hook.py` gained a PEP 723 `requires-python = ">=3.12"` block, the floor its own guard enforces, so uv runs it under a 3.12+ interpreter. No behaviour change otherwise.
- hook interpreter floor (nexus-efk2h): `conexus/hooks/scripts/mailbox_drain.py` gained the same PEP 723 `requires-python = ">=3.12"` block. No behaviour change otherwise.
- hook interpreter floor (nexus-efk2h): `conexus/hooks/scripts/routing/credential_print_guard.py` gained the same PEP 723 `requires-python = ">=3.12"` block. No behaviour change otherwise.
- hook interpreter floor (nexus-efk2h): `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` gained the same PEP 723 `requires-python = ">=3.12"` block. No behaviour change otherwise.
- shim on Windows (nexus-efk2h): `conexus/hooks/scripts/nx_hook_shim.py` no longer names `signal.SIGHUP` unconditionally, which does not exist on Windows and raised AttributeError after `nx-hook` had started. It forwards the signals the platform has. No change on POSIX.
- lease read retry (nexus-f9bgu.44): `conexus/hooks/scripts/_endpoint_resolve.py` retries a Windows sharing violation for up to 2 s when it reads the supervisor lease (a stdlib mirror of `ServiceRegistry`'s own bounded retry), so a read that lands during the supervisor's lease replace no longer resolves to no endpoint. No change on POSIX.

- identity failure fails open (nexus-f9bgu.33): `conexus/hooks/scripts/_endpoint_resolve.py` resolves "no lease" instead of raising when the Windows user SID cannot be read (`ServiceIdentityError` used to escape `read_storage_service_lease`, `resolve_base_url` and `read_local_supervisor_token`), and caches the SID and the advapi32/kernel32 bindings per process (a stdlib mirror of `nexus._winsec`'s own caches). No change on POSIX.


## Deferred to the next client release

