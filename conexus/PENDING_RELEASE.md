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
  entries naming `nx` are bead nexus-q02nx.22's and are untouched.
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
