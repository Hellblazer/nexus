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
  are re-pointed at `nx-hook` verbs (`behaviour-census`, `version-lockstep`,
  `phase-review-close-gate`, `subagent-git-write-gate`, `mailbox-drain`), so
  the manifest names no interpreter and no plugin script at all. Stock
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

- nexus-t9klx — `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py`:
  DELETED, ported to the `subagent-git-write-gate` verb. The routing
  framework's other guard, and the deliberately fail-OPEN one: a crash in a
  broken guard must not brick every agent's Bash, and that posture is carried
  across unchanged.

- nexus-t9klx — `conexus/hooks/scripts/routing/_lib.py`: DELETED. Its last
  plugin importer was the guard above; the wheel's `nexus.hooks._routing_lib`
  is the same library, and two copies of it would drift.

- nexus-t9klx — `conexus/hooks/scripts/routing/registry.yaml`: both rules now
  declare `hook_verb` plus the wheel `module` instead of a `hook_script`
  filename, since neither guard is a plugin script any more. Documentation
  only — `routing_stats` reads hooks.json, never this file — so nothing
  behaves differently when it goes live; it is declared because it drifted,
  which is the whole contract.

- nexus-t9klx — `conexus/hooks/scripts/mailbox_drain.py`: DELETED, ported to
  the `mailbox-drain` verb. The last of the five. Its endpoint and size-limit
  mirrors are replaced by the client's own primitives, and its output still
  goes to stdout the moment each row is acked (`_io.stream`), because a
  harness timeout is a kill and a consumed row must already be shown.

- nexus-t9klx — `conexus/hooks/scripts/_interpreter.py`: DELETED. It re-execed
  a bare `python3` under an interpreter that could import `nexus`; its last
  importer was `mailbox_drain.py`, and a console-script verb has no
  interpreter to choose.

- nexus-t9klx — `conexus/hooks/scripts/routing/README.md`: says the routing
  framework lives in the wheel as `nexus.hooks._routing_lib` and each rule is
  an `nx-hook` verb, where it still described a vendored `_lib.py` +
  `_interpreter.py`. Documentation only.

- nexus-t9klx — `sn/hooks/scripts/_hook_boundary.py`: a docstring reference to
  conexus's drain hook now names the wheel module. Comment only; sn's
  behaviour is unchanged.

nexus-z9cz2: the eleven bullets below are DELETIONS of plugin scripts that
nothing shipped executed any more: hooks.json names none of them since
nexus-t9klx, and each was superseded by the wheel code named. The one
plugin script left, which the wheel runs, is divergence-language-scan.py.
A session on v7.57.0 still carries its own copies until the pin moves, and
never ran them either.

- nexus-z9cz2 — `conexus/hooks/scripts/t2_prefix_scan.py`: DELETED; superseded by `nexus.hooks.t2_prefix_scan`, which `subagent_start` calls in-process.

- nexus-z9cz2 — `conexus/hooks/scripts/tuple_ledger_project.py`: DELETED; superseded by `nexus.hooks.tuple_ledger_project`, which `tuple_projection` calls in-process.

- nexus-z9cz2 — `conexus/hooks/scripts/rdr_hook.py`: DELETED; superseded by the `rdr` verb.

- nexus-z9cz2 — `conexus/hooks/scripts/session_start_hook.py`: DELETED; superseded by the `session-context` verb.

- nexus-z9cz2 — `conexus/hooks/scripts/stop_failure_hook.py`: DELETED; superseded by `nexus.hooks.stop_failure`.

- nexus-z9cz2 — `conexus/hooks/scripts/subagent-stop-scan.py`: DELETED; superseded by `nexus.hooks.subagent_stop_scans`.

- nexus-z9cz2 — `conexus/hooks/scripts/subagent-stop-writes-scan.py`: DELETED; superseded by `nexus.hooks.subagent_stop_scans`.

- nexus-z9cz2 — `conexus/hooks/scripts/read_verification_config.py`: DELETED; superseded by `nexus.hooks.verification_config`.

- nexus-z9cz2 — `conexus/hooks/scripts/_endpoint_resolve.py`: DELETED; superseded by nothing: a stdlib mirror of the client's endpoint precedence, needed only by the plugin scripts above.

- nexus-z9cz2 — `conexus/hooks/scripts/_tuple_size_limits.py`: DELETED; superseded by nothing: a stdlib mirror of the tuple size caps, needed only by the plugin scripts above.

- nexus-z9cz2 — `conexus/hooks/scripts/_hook_logging.py`: DELETED; superseded by nothing: its one function has a same-name twin in `nexus._hook_runtime._io`.

- nexus-z9cz2 — `conexus/skills/orchestration/SKILL.md`: names the ledger's VERIFY parser as `nexus.hooks.tuple_ledger_project` instead of the deleted plugin copy. Wording only.

- nexus-j4iy0 — `sn/hooks/hooks.json`: all four entries launch through exec-form `uv run --no-project --no-config --quiet <script>` instead of bare `python3`, which stock Windows lacks. uv is already required by Serena's `uvx`; `--no-config` keeps a `.python-version` above the session's cwd (uv 0.8) or above the installed plugin (uv 0.12) from choosing the interpreter. A Context7-only user now needs uv for the hooks too.
- nexus-j4iy0 — `sn/hooks/scripts/auto_approve_sn_mcp.py`: docstring names the uv launcher. Wording only.

- nexus-ebx0s — `sn/hooks/scripts/worktree_guard.py`: adds a per-session Serena-root record (`git_toplevel`, `record_startup_root`, `read_recorded_root`, `relocated_write_root`) and a new deny reason (`deny_reason_relocated`), so the PreToolUse guard can catch a RELOCATED session (cwd stays in the primary the whole time; Serena's server is rooted wherever the session's cwd was at ITS OWN startup) in addition to the existing cwd-is-a-linked-worktree case. Additive: `is_linked_worktree`'s unconditional denial is unchanged.
- nexus-ebx0s — `sn/hooks/scripts/session_start.py`: now reads stdin and, only when `source == "startup"`, records the payload cwd's git working-tree root for this session_id via `worktree_guard.record_startup_root`, so the PreToolUse guard above has something to compare against. Wrapped in its own try/except, isolated from the boundary that protects the section-text emission: a malformed payload, undecodable stdin, or missing `worktree_guard.py` each log to stderr and never cost the reminder text.
- nexus-ebx0s — `sn/hooks/scripts/auto_approve_sn_mcp.py`: `decide()` now falls through to `worktree_guard.relocated_write_root` whenever `is_linked_worktree` does not already deny, and denies a Serena write tool on a mismatch too (fails open, with a stderr log, when no session_id/record/resolvable cwd is available).
- nexus-ebx0s — `sn/hooks/scripts/session-start-section.md`: the worktree reminder now names the relocated-session hazard (a write can succeed against the primary; the tool's own dry-run report is not trustworthy proof otherwise) instead of only the dispatched-subagent case.
- nexus-ebx0s — `sn/README.md`: § Worktree Guidance and Guard reworded from "two places" to three, adding the relocated-session PreToolUse case and framing the whole section as a hazard (a write can silently succeed against the wrong tree) rather than only a restriction.
- nexus-ebx0s — `AGENTS.md`: § Worktrees rule 11 reworded to name the actual hazard (a relocated session's Serena write can succeed against the primary; the dry-run report cannot be trusted) instead of describing only a restriction, and to say plainly that the exact reported shape (cwd never differs from the primary) remains undetectable from cwd alone even with this fix.

(The previous entry, `conexus/hooks/scripts/preflight.py`'s deletion for
nexus-sa187, went live when `source.ref` advanced to `v7.57.0`.)
