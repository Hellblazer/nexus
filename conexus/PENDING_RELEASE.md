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

## Awaiting the next release or plugin cut (pinned: v7.57.0)

ONE PATH PER BULLET, on the bullet's FIRST line. The parser reads backtick
spans from a bullet line only, so a path on a continuation line is invisible
to it — which is exactly how two of these went undeclared and red'd develop
on 2026-09-22. A bullet that wraps is fine; a PATH that wraps is not.

- nexus-j4iy0 — `sn/hooks/hooks.json`: all four entries launch through exec-form `uv run --no-project --no-config --quiet <script>` instead of bare `python3`, which stock Windows lacks. uv is already required by Serena's `uvx`; `--no-config` keeps a `.python-version` above the session's cwd (uv 0.8) or above the installed plugin (uv 0.12) from choosing the interpreter. A Context7-only user now needs uv for the hooks too.
- nexus-j4iy0 — `sn/hooks/scripts/auto_approve_sn_mcp.py`: docstring names the uv launcher. Wording only.

- nexus-ebx0s — `sn/hooks/scripts/worktree_guard.py`: adds a per-session Serena-root record (`git_toplevel`, `record_startup_root`, `read_recorded_root`) and a new deny reason (`deny_reason_relocated`), plus the single PreToolUse decision `write_denial_reason` that composes them with `is_linked_worktree`/`deny_reason`. Round 2 (review finding): when this session has a recorded root, the comparison against the call's own cwd decides ALONE — a match allows even if cwd is itself a linked worktree (a session that legitimately started there), a mismatch denies naming both trees (the relocated-session shape). `is_linked_worktree` now runs only as the FALLBACK when no comparison is possible (no session_id, or nothing recorded), where it still denies unconditionally on cwd alone — that part is unchanged from round 1. Round 3 (review finding): the record moved from ONE shared `serena-roots.json`, read-modify-written with no lock (measured: 8/8 trials lost at least one of 12 concurrent sessions' records), to one file per session_id under `serena-roots/`, keyed by a sanitised (`_sanitized_session_filename`) and directory-escape-checked (`_session_root_file`) filename, written atomically via a same-directory temp file plus `replace` — no lock, none needed, since two sessions now write two different files. Opportunistic pruning (`_prune_stale_records`, 30 days) runs from `record_startup_root` so the per-session files do not accumulate forever. Round 4 (review finding): that pruning now also sweeps orphaned `<name>.tmp<pid>` files -- a process killed between the write and the `replace()` leaves one behind, and nothing else ever revisits it -- once they are at least an hour old (short enough not to linger, long enough that a genuinely in-flight write, sub-millisecond in practice, can never be the one pruned).
- nexus-ebx0s — `sn/hooks/scripts/session_start.py`: now reads stdin and, only when `source == "startup"`, records the payload cwd's git working-tree root for this session_id via `worktree_guard.record_startup_root`, so the PreToolUse guard above has something to compare against. Wrapped in its own try/except, isolated from the boundary that protects the section-text emission: a malformed payload, undecodable stdin, or missing `worktree_guard.py` each log to stderr and never cost the reminder text.
- nexus-ebx0s — `sn/hooks/scripts/auto_approve_sn_mcp.py`: `decide()`'s Serena-write branch is now one call to `worktree_guard.write_denial_reason(tool_name, cwd, session_id)`, replacing round 1's two-branch `is_linked_worktree`-then-`relocated_write_root` sequence with the record-decides-first ordering described above.
- nexus-ebx0s — `sn/hooks/scripts/session-start-section.md`: the worktree reminder now names the relocated-session hazard (a write can succeed against the primary; the tool's own dry-run report is not trustworthy proof otherwise) instead of only the dispatched-subagent case.
- nexus-ebx0s — `sn/README.md`: § Worktree Guidance and Guard reworded to describe the single `write_denial_reason` decision and its two-branch ordering (record-decides-first, cwd-only fallback second), replacing round 1's "three places" framing where the relocated-session case was a separate, always-consulted check.
- nexus-ebx0s — `AGENTS.md`: § Worktrees rule 11 reworded to name the actual hazard (a relocated session's Serena write can succeed against the primary; the dry-run report cannot be trusted) instead of describing only a restriction, to say plainly that the exact reported shape (cwd never differs from the primary) remains undetectable from cwd alone even with this fix, and to note that round 2 now allows a session that legitimately started inside a worktree to keep writing there.

- nexus-vupim — `conexus/skills/rdr-close/SKILL.md`: post-mortem archival
  (Step 6, the Abandoned flow's mirror of it, and the Agent Invocation /
  Success Criteria / PRODUCE references to the same write) no longer
  resolves its target collection via `nx catalog collection-name
  --content-type knowledge`. That resolution renders the OWNER-ID-shaped
  name for the calling repo's own tumbler (`knowledge__<repo-tumbler>__...`)
  for every content type, knowledge included — correct for code/docs/rdr,
  which are genuinely repo-owned, but wrong for a post-mortem, which is
  catalog-owned by the knowledge curator, not the repo, and which
  docs/collections.md Rule 1 requires land in a SUBJECT collection, never
  an owner-id one. The target is now the bare subject `{repo}-rdr-research`.
  Three post-mortems (RDR-203 twice, RDR-215) archived under the old
  resolution landed in `knowledge__1-1__...` on the nexus repo; see
  nexus-vupim for the re-home record. Sessions on the pinned tag keep
  misrouting post-mortems into their repo's own tumbler collection until a
  new pin ships.

(The previous entry, `conexus/hooks/scripts/preflight.py`'s deletion for
nexus-sa187, went live when `source.ref` advanced to `v7.57.0`.)

## Deferred to the next client release

7.58.0 restored `hooks.json` and the eleven scripts its entries need to their
v7.57.0 bytes (nexus-t9klx skew fix): an older `nx-hook` exits 2 on a verb it
does not know, so a plugin that updated before its CLI would have blocked every
prompt and every Bash call. Their entries were removed from this ledger because
those paths no longer differ from the pin. The verbs stay in the wheel; the
entries that name them come back here when hooks.json moves to them, and
version lockstep never does.

nexus-2x3qy (2026-09-23): these four beads (nexus-t9klx, nexus-z9cz2,
nexus-silj0, nexus-veh77 -- the RDR-215 hook-migration work) straddle
wheel content (their commits also touch `src/nexus/hooks/*` and kin):
atomic_split_check refuses a plugin-only cut on all four as of this
commit (confirmed by running it directly against origin/develop,
before this bead's own change). Moved here so a plugin-only cut of
whatever else is pending (nexus-j4iy0, nexus-ebx0s below -- neither
touches wheel content) is not blocked by them. They ship normally at
the next full client release regardless of deferral; nothing here
changes what that release contains, only what an interim plugin-only
cut may take on its own. Move back once RDR-215 is fully wheel-side
and a fresh straddle check confirms it, or once the next client
release ships them anyway.

- nexus-t9klx — `sn/hooks/scripts/_hook_boundary.py`: a docstring reference to
  conexus's drain hook now names the wheel module. Comment only; sn's
  behaviour is unchanged.

nexus-z9cz2: the eight DELETED bullets below are plugin scripts that nothing
shipped executes: hooks.json names none of them, and each was superseded by
the wheel code named. (z9cz2 deleted three more helpers, which came back with
the 7.58.0 skew fix because the restored mailbox drain and routing guards
import them.) A session on v7.57.0 still carries its own copies until the pin
moves, and never ran them either.

- nexus-z9cz2 — `conexus/hooks/scripts/t2_prefix_scan.py`: DELETED; superseded by `nexus.hooks.t2_prefix_scan`, which `subagent_start` calls in-process.

- nexus-z9cz2 — `conexus/hooks/scripts/tuple_ledger_project.py`: DELETED; superseded by `nexus.hooks.tuple_ledger_project`, which `tuple_projection` calls in-process.

- nexus-z9cz2 — `conexus/hooks/scripts/rdr_hook.py`: DELETED; superseded by the `rdr` verb.

- nexus-z9cz2 — `conexus/hooks/scripts/session_start_hook.py`: DELETED; superseded by the `session-context` verb.

- nexus-z9cz2 — `conexus/hooks/scripts/stop_failure_hook.py`: DELETED; superseded by `nexus.hooks.stop_failure`.

- nexus-z9cz2 — `conexus/hooks/scripts/subagent-stop-scan.py`: DELETED; superseded by `nexus.hooks.subagent_stop_scans`.

- nexus-z9cz2 — `conexus/hooks/scripts/subagent-stop-writes-scan.py`: DELETED; superseded by `nexus.hooks.subagent_stop_scans`.

- nexus-z9cz2 — `conexus/hooks/scripts/read_verification_config.py`: DELETED; superseded by `nexus.hooks.verification_config`.

- nexus-z9cz2 — `conexus/skills/orchestration/SKILL.md`: names the ledger's VERIFY parser as `nexus.hooks.tuple_ledger_project` instead of the deleted plugin copy. Wording only.

- nexus-silj0 — `conexus/skills/orchestration/SKILL.md`: item 3 of the
  Background-Teammate Ledger section now names the `workflow-subagent`
  exception: a Workflow-tool agent gets its own census/undeclared bucket
  (`WORKFLOW\tchecked=<n>`, excluded from the `undeclared` deficit) instead
  of reading as UNDECLARED the way every other hook-invisible dispatch
  still does. Wording only — the audit itself lives in the wheel
  (`nexus.hooks.expectations`) and is live the moment the release ships,
  independent of this pin.

- nexus-silj0 — `conexus/commands/continuation.md`: the Gap-1
  declaration-completeness step names the same `workflow-subagent`
  exception, so a continuation handoff written under the OLD pin does not
  file a bead for a Workflow-tool run that the new wheel already buckets
  cleanly. Wording only.

- nexus-veh77 — `conexus/README.md`: new hook-table rows for `nx-hook
  mcp-connect-wait` and, round 5, `nx-hook mcp-connect-check`.
