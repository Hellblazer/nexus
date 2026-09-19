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

- `conexus/skills/git-worktrees/SKILL.md`:
  bead: nexus-q02nx.17 — the skill covered worktrees for FEATURE WORK and for
  `isolation: "worktree"` subagent dispatches, and said nothing about the
  session itself, so sessions defaulted into the shared checkout by omission
  rather than by decision. Its description named only those two cases, so it
  could not be selected for "more than one session on one checkout" either —
  the skill existed, covered adjacent ground, and was unfindable for the case
  that mattered. Adds that case, the move procedure (verify in the new
  worktree BEFORE reverting the source; cherry-pick committed work because a
  commit is recoverable where an applied-but-unverified diff is not), and the
  Serena difference between a session STARTED in a worktree and one RELOCATED
  into it. Project-specific mechanics are deliberately NOT duplicated here —
  they live in this repo's AGENTS.md next to the commands they concern,
  because a second copy of a project rule drifts from the first until the
  stale one wins.
  INERT until the next cut: a session loading the pinned plugin gets the old
  description and will not find this for the multi-session case.

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
  and is stdlib-only by contract, so `nexus` is not on its path.
  An earlier version of this entry said bead nexus-q02nx.20 would move the
  work into the server, where the import belongs and where the pair should
  collapse. It did not, and the prediction was wrong when written: .20 moves
  the two async WRAPPERS to daemon threads and deliberately leaves this file
  in the plugin, because it is stdlib-only by contract and importing it into
  the wheel would invert this epic's dependency direction. The mirror stays
  a mirror. No behaviour change.

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

- `conexus/hooks/hooks.json` (21 of its 25 entries), and the plugin
  scripts twelve of them used to run:
  bead: nexus-q02nx.21 — RDR-215 re-declares the hook layer across two
  tiers. Twelve entries become `mcp_tool` calls on `plugin:conexus:nexus`
  (`hook_auto_approve`, `hook_subagent_start`, `hook_subagent_start_stamp`,
  `hook_subagent_start_tuple`, `hook_subagent_stop`,
  `hook_subagent_stop_tuple`, `hook_stop_verification`,
  `hook_pre_close_verification`, `hook_agent_dispatch_expect`,
  `hook_post_compact`, `hook_divergence_language_guard`,
  `hook_stop_failure`); three SessionStart entries become exec-form
  `nx-hook` verbs (`preflight`, `session-context`, `rdr`); five stay
  plugin-resident on bare `python3` (`version_lockstep_hook.py`,
  `mailbox_drain.py`, `subagent_git_write_requires_orchestrator.py`,
  `phase_review_close_requires_gate.py`, `behaviour_census.py`). The four
  entries naming `nx` were left for bead nexus-q02nx.22, which has since
  converted them — see its own entry below.
  THE WHEEL FLOOR, and why this entry is NOT cuttable on its own: every
  one of those fifteen handlers is wheel-resident. The twelve `hook_*`
  tools come from `nexus.mcp.hooks`; `nx-hook` is a console script
  declared in `pyproject.toml`. A plugin-only cut (RDR-197) ships
  `conexus/**` WITHOUT a client release, so this hooks.json landing on a
  box running the currently pinned conexus would name fifteen handlers
  that box does not have — and Claude Code treats an unavailable tool as
  a non-blocking error, so they would not fail, they would silently do
  nothing. Among them the bd-close gate and the RDR-184 EXPECT writer.
  This is ENFORCED, not merely intended: `cut_plugin_release.py`'s
  `atomic_split_check` refuses a cut whose ledger entry straddles into
  the wheel surface, with no flag and no warn mode, and this bead touches
  19 paths under `src/` (verified by the attribution rule the check
  itself reads). It is written down here because a guard nobody can see
  is one people route around — a reviewer reading this file concluded the
  exposure was open, and the only thing wrong with that reading was that
  nothing in the file said otherwise.
  WHY FIVE STAY IN THE PLUGIN, since it is not the RDR's original shape:
  `_endpoint_resolve.py` cannot leave `conexus/hooks/scripts/` —
  `t2_prefix_scan.py` and `tuple_ledger_project.py` import it and neither
  is ported by this epic — so the three hooks that reach it cannot become
  wheel-resident without a second copy of a 449-line resolver beside
  `nexus.db.service_endpoint`. The lockstep repairs a wheel that is
  behind the plugin so it can depend on neither tier, and
  `behaviour_census.py` is plugin-domain (it parses Claude Code
  transcripts). Full reasoning: T2
  `nexus_rdr/215-tier-resolution-bead-21`.
  WHEEL FLOOR, and it is the hard one in this ledger: a plugin naming
  `nx-hook` or any `hook_*` tool needs a conexus generation at or past
  the wheel that ships them. An older installed generation has no
  `nx-hook` shim and no `hook_*` tools on `nx-mcp`, so the entries do not
  merely go stale, they DO NOT FIRE — the command form is not found and
  the tool form logs `[WARN] Hooks: mcp_tool hook skipped` and proceeds.
  That is a fail-open on twelve hooks at once, including the close gate
  and the orchestrator guard. This entry must not be cleared by a release
  that does not also ship the wheel.
  INERT until the next cut: sessions on the pinned tag keep running the
  bash layer, which is still on disk and still correct.

- `conexus/hooks/hooks.json` (the last four shell-form entries), `conexus/hooks/scripts/_run_python_hook.sh` (deleted), `conexus/hooks/scripts/routing/README.md`, `conexus/hooks/scripts/version_lockstep_action.py`:
  bead: nexus-q02nx.22 — RDR-215 Approach items 6 and 7, the tail of the
  re-declaration. `nx upgrade --auto 2>/dev/null || echo ... >&2` becomes
  `nx-hook upgrade-auto`, `nx self gc >/dev/null 2>&1 || true` becomes
  `nx-hook self-gc`, `nx hook session-start` becomes `nx-hook
  session-start`, and `nx-session-end-launcher` gains an explicit empty
  `args`. Exec form has no shell, so the two entries that carried
  redirects and `||` had to put them somewhere: `nexus.hooks.upgrade_auto`
  and `nexus.hooks.self_gc`, which still spawn `nx` as a separate process
  — that command installs generations and flips `<tools>/current`, which
  is not work to do inside the hook interpreter running out of one. After
  this, no entry in the file names `nx`.
  `_run_python_hook.sh` is deleted: the interpreter resolution it
  performed is `_interpreter.py`'s since .21, and the launcher's remaining
  callers (the detached lockstep action, the lockstep e2e gate, the
  RDR-208 MVV container) are repointed in the same change. The routing
  README is prose only: it named the deleted launcher as half of "the
  framework", which is `_interpreter.py` now.
  `version_lockstep_action.py` is docstring only — it said its interpreter
  came from the deleted launcher; it now says it is inherited from the
  spawning hook's `sys.executable`. Declared anyway, because the ledger is
  about what differs from the pinned tag, not about what matters.
  `conexus/hooks/scripts/version_lockstep_hook.py` is NOT listed again here:
  it is already declared under .21's entry above, and a path is declared
  once. Its .22 change is the dispatch argv (`sys.executable` in place of
  the launcher), named there rather than duplicated.
  EVERY PATH IS ON THE BULLET LINE ABOVE, and that is not
  incidental formatting. `_declared_paths` reads backtick spans from lines
  matching `^\s*-\s+`, so a path wrapped onto a continuation line is
  invisible to it. This entry was written that way first and the ledger
  gate caught it: two declared paths reported as undeclared drift. It is
  the same shape as the defect the bead itself is about — a checker whose
  domain is a spelling, and a wrap that leaves it.
  WHEEL FLOOR: the same one as .21's entry directly above, for the same
  reason and with two more verbs on it — `upgrade-auto` and `self-gc` are
  entries in `nexus._hook_runtime.entry.VERB_TABLE`, so a box whose
  conexus predates them has no handler. Not cuttable without the wheel.
  Worth stating plainly because of WHICH two hooks these are: on an old
  generation the exec-form `nx-hook upgrade-auto` is simply not found, so
  the self-upgrade that would have repaired that box is the thing that
  cannot run. The skew path that still works there is
  `version_lockstep_hook.py`, which stays plugin-resident and stdlib-only
  precisely so it runs when the wheel is behind — it nudges and dispatches
  the detached reinstall. That is now the load-bearing recovery path, not
  a redundant second one.
  INERT until the next cut: sessions on the pinned tag keep running the
  four shell strings and the launcher, both still on disk and correct.

- `conexus/hooks/scripts/behaviour_census.py`:
  bead: nexus-4lnn1 — a new SessionStart hook reporting the PREVIOUS
  session's delegation and deliberation rates against baselines computed
  from the user's own trailing sessions. Both rates are invisible while a
  session runs, which is why prose has never moved them: measured 4.3%
  delegation and 16.8% positional thinking coverage across 25 transcripts.
  Stdlib-only, no nexus imports, and verified to run under
  `/usr/bin/python3` 3.9.6 — the oldest interpreter likely to win PATH —
  which is why it is declared in the plugin-resident `python3` exec form
  and does NOT need `_run_python_hook.sh`. Authored by nexus-93; merged
  here and carried into bead nexus-q02nx.21's re-declaration so the entry
  did not have to be rewritten twice.
  INERT until the next cut: the census does not run, so no session sees
  its own prior rates.

- `conexus/skills/rdr-create/SKILL.md`:
  bead: nexus-4lnn1 — Step 2 told sessions to find the highest existing
  RDR id by hand-scanning for `[0-9][0-9][0-9]-*.md`. That glob requires a
  filename to BEGIN with three digits and matches none of this repo's 217
  `rdr-NNN-*.md` files, so a session following it literally found no
  maximum, fell through to the step's own "start at 001" clause, and would
  have collided with RDR-001. Now points at the executable preamble, which
  was already correct and prints the style it detected. Authored by
  nexus-93.
  INERT until the next cut: a session on the pinned tag still reads the
  glob. Worth noting the failure mode is silent — a scan matching nothing
  and a stale checkout both answer confidently with a wrong maximum, and
  unlike the stale checkout this one has no fetch that would save it,
  which is how it survived 217 RDRs unhit.


- `conexus/hooks/scripts/routing/phase_review_close_requires_gate.py`:
  bead: nexus-q02nx.21 — carries the interpreter preamble
  (`_interpreter.reexec_if_needed()`) ahead of its `_lib` import. Read
  this one first: its hooks.json entry had ALSO lost the `routing/`
  segment from its declared path at 9b1081514, so from that commit until
  86eb999e8 the routing framework's only `fail_closed` rule pointed at a
  file that does not exist. `python3` on a missing file exits 2 with no
  envelope, which Claude Code reads as a non-blocking error and not a
  deny, so the close gate was failing OPEN. The path fix is in
  hooks.json, already declared; this entry is the preamble.
  INERT until the next cut: sessions on the pinned tag run the pre-rewrite
  declaration, where the path was correct and the shim resolved the
  interpreter — so the failing-open window is this branch's, not theirs.

- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py`:
  bead: nexus-q02nx.21 — same preamble, and the same lost `routing/`
  segment, so this hook was equally dead between those two commits. It is
  `warn`, not `fail_closed`, so the cost was a missing warning rather than
  an inverted decision.
  INERT until the next cut.

- `conexus/hooks/scripts/_interpreter.py`:
  bead: nexus-q02nx.21 — NEW. Puts `_run_python_hook.sh`'s interpreter
  resolution back in Python, per Sam's ruling of 2026-09-19, now that the
  exec-form entries launch these scripts with a bare `python3` and PATH
  decides. Mirrors the shim's chain exactly: `$NX_HOOK_PYTHON`, a venv
  holding this checkout's `nexus`, the generation python, then
  `python3.13` / `python3.12` by name. Stdlib-only and 3.9-parseable by
  necessity — it is imported by scripts whose whole point is to be
  reachable from a 3.9 interpreter.
  INERT until the next cut.

- `conexus/hooks/scripts/version_lockstep_hook.py`:
  bead: nexus-q02nx.21 — the preamble, ahead of its own 3.12 guard.
  Measured rc=1 under `/usr/bin/python3` 3.9.6 before it, rc=0 after.
  ALSO bead nexus-q02nx.22: the detached action is spawned with this
  process's own `sys.executable` instead of `bash <launcher> <action>`,
  because .22 deletes that launcher. Safe without a second interpreter
  probe precisely because of .21's preamble above — `reexec_if_needed()`
  and the 3.12 guard both run at module scope, so no dispatch is reached
  under an unvetted interpreter on any path, including the one where
  resolution fails and returns having changed nothing.
  INERT until the next cut: a hook whose job is repairing a wheel that is
  behind the plugin could not run on the interpreter most likely to be
  present when things are already broken.

- `conexus/hooks/scripts/mailbox_drain.py`:
  bead: nexus-q02nx.21 — the preamble, ahead of its `_endpoint_resolve`
  import. Same measurement: rc=1 under 3.9.6 before, rc=0 after.
  INERT until the next cut.

- `conexus/hooks/scripts/tuple_ledger_project.py`:
  bead: nexus-q02nx.21 — docstring only. It said it was invoked ONLY from
  inside the two async wrapper shell scripts; those are deleted by this
  bead and its caller is now `nexus.hooks.tuple_projection`'s `run_start`
  / `run_stop`. The module itself is unchanged and stays a plugin-resident
  stdlib-only subprocess.
  INERT until the next cut, and harmless either way — no behaviour moves.

- `conexus/hooks/scripts/divergence-language-scan.py`:
  bead: nexus-q02nx.21 — docstring only. It named
  `divergence-language-guard.sh` as its caller; that script is deleted and
  the caller is now `nexus.hooks.divergence_language_guard`, which imports
  this file BY PATH rather than copying it, so the locked pattern bank
  stays single-copy.
  INERT until the next cut, and harmless either way.

All twelve entries below are the same deletion, bead nexus-q02nx.21.
Each script is unreferenced from hooks.json after the re-declaration at
9b1081514, and its behaviour now lives in `src/nexus/hooks/` as an MCP
tool or an exec-form `nx-hook` verb. The deletion deliberately lands
AFTER that re-declaration and never before: three of the four scripts
that sourced `expectations.sh` did so with `|| exit 0` and no
diagnostic, so the other order would have silenced the whole RDR-184
guard with nothing to see.

They are INERT until the next cut, and this is the one place where inert
cuts the right way: a session on the pinned tag still has all twelve
files AND a hooks.json that names them, so nothing there breaks. The two
halves only have to agree within a single cut.

- `conexus/hooks/scripts/agent-dispatch-expect.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.agent_dispatch_expect`, the PreToolUse hook that writes the RDR-184 EXPECT row from a dispatch's own subagent_type.
  INERT until the next cut.

- `conexus/hooks/scripts/auto-approve-nx-mcp.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.auto_approve`.
  INERT until the next cut.

- `conexus/hooks/scripts/divergence-language-guard.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.divergence_language_guard`, which imports the surviving `divergence-language-scan.py` by path.
  INERT until the next cut.

- `conexus/hooks/scripts/expectations.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.expectations`, the RDR-184 ledger library the four sourcers shared.
  INERT until the next cut.

- `conexus/hooks/scripts/post_compact_hook.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.post_compact`.
  INERT until the next cut.

- `conexus/hooks/scripts/pre_close_verification_hook.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.pre_close_verification`.
  INERT until the next cut.

- `conexus/hooks/scripts/stop_verification_hook.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.stop_verification`.
  INERT until the next cut.

- `conexus/hooks/scripts/subagent-start-stamp.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.subagent_start_stamp`.
  INERT until the next cut.

- `conexus/hooks/scripts/subagent-start-tuple-async.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.tuple_projection.run_start`, a daemon thread rather than a disowned subshell.
  INERT until the next cut.

- `conexus/hooks/scripts/subagent-start.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.subagent_start`.
  INERT until the next cut.

- `conexus/hooks/scripts/subagent-stop-tuple-async.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.tuple_projection.run_stop`, same change of detachment.
  INERT until the next cut.

- `conexus/hooks/scripts/subagent-stop.sh`:
  bead: nexus-q02nx.21 — DELETED; ported to `nexus.hooks.subagent_stop`, with its two transcript scans in `nexus.hooks.subagent_stop_scans`.
  INERT until the next cut.

- `conexus/agents/code-review-expert.md`:
  bead: nexus-q02nx.21 — cited `pre_close_verification_hook.sh` by filename
  as the live close gate. Now names the port, `hook_pre_close_verification`,
  which is an MCP tool with no command line to run by hand.
  INERT until the next cut: an agent on the pinned tag reads the old name,
  and on the pinned tag that name is still correct.

- `conexus/agents/substantive-critic.md`:
  bead: nexus-q02nx.21 — same citation, same fix.
  INERT until the next cut.

- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md`:
  bead: nexus-q02nx.21 — same citation, same fix. This one matters most of
  the three: every agent reads it, and it is where the marker-authorship
  rule lives.
  INERT until the next cut.

- `conexus/skills/code-review/SKILL.md`:
  bead: nexus-q02nx.21 — same citation, same fix.
  INERT until the next cut.

- `conexus/skills/mailbox/SKILL.md`:
  bead: nexus-q02nx.21 — the two "Claimant id" lines named
  `subagent-start.sh` as the source of an agent's claimant id. They now name
  the SubagentStart hook rather than a script that is being deleted.
  INERT until the next cut.

- `sn/hooks/hooks.json`, `sn/hooks/scripts/subagent_start.py`, `sn/hooks/scripts/session_start.py`, `sn/hooks/scripts/_hook_boundary.py`, `sn/hooks/scripts/auto_approve_sn_mcp.py`, `sn/hooks/scripts/mcp-inject.sh` (deleted), `sn/hooks/scripts/session-start.sh` (deleted), `sn/hooks/scripts/auto-approve-sn-mcp.sh` (deleted), `sn/hooks/scripts/worktree_guard.py`, `sn/README.md`:
  bead: nexus-q02nx.23 — RDR-215 Approach item 8, the sn half. All four
  entries become exec-form `python3` on plugin-resident scripts, and the
  three bash wrappers are deleted. `auto_approve_sn_mcp.py` and
  `worktree_guard.py` were already bundled stdlib-only scripts that the
  wrappers merely called; the two SubagentStart/SessionStart wrappers had
  bodies, so those move to `subagent_start.py` and `session_start.py`.
  Approach item 8 named only three exec targets — the SubagentStart entry
  had none, which a gate residual caught; `subagent_start.py` is it.
  NO WHEEL FLOOR, and that is the difference from every other hooks.json
  entry in this ledger. .21 and .22 above are uncuttable alone because
  their handlers are wheel-resident (`hook_*` tools, `nx-hook` verbs);
  these four name `python3` and a path inside sn itself. sn ships no
  Python package and no server, and nothing here imports `nexus` — that
  independence is a Cross-Cutting Concern in the RDR, and the port keeps
  it. So this entry is cuttable on the plugin channel on its own, which
  is worth saying out loud precisely because the neighbouring entries are
  not and a reader scanning the file would reasonably assume otherwise.
  TWO DEFECTS FIXED RATHER THAN CARRIED. `auto-approve-sn-mcp.sh` ended
  in an unconditional `exit 0` that hid a Python crash completely: the
  wrapper reported success whatever the Python did, so a broken allowlist
  looked exactly like an empty one. `session-start.sh` was the opposite
  and the only script in the whole set whose exit was not unconditionally
  0 — a bare `cat` with no `2>/dev/null` and no fallback, so a missing
  section file failed the event. Both now run inside
  `_hook_boundary.guard`, which logs to stderr and returns 0. The
  envelope's `mktemp`-failure path goes too: the bash buffered the body
  into a tempfile and wrapped it from an `EXIT` trap, and when `mktemp`
  failed the capture no-opped and the sections went out UNWRAPPED, which
  is the silent-drop shape the envelope exists to prevent. Accumulating
  and dumping once has no half-wrapped state to reach.
  TWO REVIEW FINDINGS ARE IN HERE TOO, both in paths declared above.
  `subagent_start.py` guards its `worktree_guard` import: the bash ran
  that detection in its own subprocess under `2>/dev/null`, so nothing it
  could do reached the `cat` calls after it, and importing it at module
  scope put it ahead of the boundary — measured, a sibling raising at
  import took the whole envelope where the bash exited 0 with both
  universal sections. `auto_approve_sn_mcp.py` deliberately does NOT do
  the same, because there the dependency IS the guard and degrading would
  approve a Serena write from a linked worktree; both sides carry that
  reasoning in place so the asymmetry is not "fixed" later.
  `worktree_guard.py` itself gains one isinstance check: `is_serena_write_tool`
  raised `AttributeError` on a non-string `tool_name`, which is valid JSON
  a payload can carry. Never unsafe — the crash reached a boundary and
  nothing was approved — so this is a quality fix, not a security one.
  Declared because the ledger is about what differs from the pinned tag.
  INERT until the next cut or release: sessions on the pinned tag keep
  running the three bash wrappers, which are still on disk there and
  still correct. Unlike .21's entry this one has no fail-open risk if the
  halves are split, because nothing it names lives outside sn.
