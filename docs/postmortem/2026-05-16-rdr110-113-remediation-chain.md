# Postmortem: RDR-110/111/112/113 deep-review remediation chain

**Date**: 2026-05-16
**Severity**: N/A — planned remediation arc, not an incident
**Scope**: Cockpit + tuplespace + daemon substrate, post-PR #786 review fallout
**Outcome**: Epic nexus-hp9f closed 16/16; 18 PRs landed; one open canary (nexus-9eaz)

## Summary

A continuation handoff identified PR #786 (y0nb audit follow-ups) plus a 16-bead epic of P0-P3 remediations surfaced by a five-critic deep review of the RDR-110/111/112/113 cockpit work. Over a single session the 16 beads landed via 17 substantive PRs plus one follow-up instrumentation PR. PR #786 itself was closed as superseded after the daughter PRs carried its uniquely load-bearing content (extracted as #803). One concurrent-migration race (nexus-9eaz) remains open with instrumentation in place but no reproducer.

## What landed

### Substrate (cockpit + tuplespace + daemon)

- **Write path**: hook bridge → daemon tuplespace RPCs → tuples.db. Daemon-mode cutover live (PR #789), bridge SQLite retry on locked/busy (#807), Chroma-first atomicity (#800), wheel-resolvable builtin dir (#803).
- **Reactive layer**: `_BindingWatcher` reaction loop + bindings subspace (#787).
- **Consumer surface**: seven `nx` skills (`nx:tuplespace-{tasks,mailbox,lock,events,barriers,list,stats}`), `nx tuplespace` CLI, session banner (#791), Phase 3 cockpit panels + dashboard (#802), `nx doctor --check-bridge` (#803).
- **Correctness invariants**: api.out two-store atomicity (#800), `embed_from` fail-loud (#800), tuple refire refreshes `expires_at`/`created_at` (#806), bridge plugin↔wheel version-compat guard (#809), daemon registry single source-of-truth with 12 reserved prefixes (#801).
- **Lifecycle**: retention sweeper (#788), `run_until_signal` stop-event hoist (#795), daemon auto-start (launchd + systemd) (#808).
- **Diagnostic**: chroma multi-process race spike — real init-race found (#794), RDR-113 contradiction-check fill (#792), `apply_pending` race instrumentation (#811 + INFO bump #814).

### Numbers

- **18 PRs landed**; PR #786 closed as superseded
- **16/16 epic beads closed** (3 P0, 5 P1, 6 P2, 2 P3); one follow-up bead `nexus-9eaz` open
- **~250 new tests** across the chain (binding watcher, retention sweeper, daemon RPCs, CLI, panels, bridge retry, version compat, autostart, dedup refresh)
- **Develop CI fixed**: seven pre-existing failing tests repaired in #795 (migration ordering, hook bridge tests stale-indexed after y0nb prepended bridge entries, hooks.json structure tests, session_end timeout)

## What went wrong, and what it cost

### The hang that ate the chain (most expensive single bug)

The first three daughter PRs (#787, #788, #789) all hung CI at the 20-minute timeout immediately after `test_sigterm_unlinks_discovery_file`. Three full CI cycles wasted (~60 minutes) before tracing the cause.

Root cause: `T2Daemon.run_until_signal()` created a **local** `stop_event` only the SIGTERM/SIGINT signal handlers could set. Tests calling `daemon.stop()` directly never woke that event. On darwin the test passed in 89 seconds via pytest-asyncio teardown cancellation; on Linux CI it hung the full 20 minutes. Fix in #795 hoisted `stop_event` to an instance attribute that `stop()` also sets.

The fix took five minutes once located. The diagnosis took longer because the failing branch was three PRs deep and the failure looked identical across them, suggesting a sibling-PR-introduced regression rather than a latent develop bug. Always check develop's own CI history before assuming a daughter PR caused the failure — develop's last green CI was hours earlier, the smoking gun.

### Pre-existing develop failures masquerading as PR-introduced

After #795, develop CI still failed on seven tests. None were caused by the signal-event fix. They were latent failures from a prior y0nb merge that prepended `orb_bridge_*` entries to every event in `nx/hooks/hooks.json` without updating the tests asserting on the original hook positions. Five hook-related tests + migration ordering + session-end timeout, all in the same PR (#795). Time cost was modest because once the pattern was clear, the fixes were rote, but the lesson is the same: develop CI green is a prerequisite for any new PR to be diagnosable.

### The CLAUDECODE gate that only failed on Linux CI

`tests/cockpit/test_hook_bridge.py::TestEmit::test_*_emit_calls_out` failed only on CI. The bridge's `emit()` early-returns when `CLAUDECODE` is unset (RF-5). Claude Code itself sets `CLAUDECODE` in dev shells, so the tests passed locally on every darwin machine. CI runners had no `CLAUDECODE`, so `emit()` no-op'd and `called_args` stayed empty. Fixed by an autouse `monkeypatch.setenv("CLAUDECODE", "1")` fixture on the test class. Generalised lesson: any contract gated on an environment variable needs explicit fixture coverage; do not rely on inherited dev environment.

### Cross-contamination via shared worktrees

The retention-sweeper PR (#788) inadvertently carried the entire `tuplespace_service` plumbing from PR #789 because the agents ran in worktrees that briefly shared a workdir. The agent staged only its own files (good discipline) but the underlying branch state had the sibling's work present. When #788 rebased against develop after #789 landed, git's 3-way merge saw both branches add the same code and auto-resolved it. Only one cosmetic comment-wording difference required manual resolution. The conflict was trivial; the bigger lesson is to verify worktree isolation before dispatching parallel agents on adjacent files.

### The 30-file PR you can't rebase

PR #786 ended up with 29 files / 1870 insertions / 89 deletions against a develop that had moved through ten daughter PRs. ~70% overlap with merged work, ~30% still uniquely load-bearing (6 coordination subspace YAMLs, wheel packaging, `nx doctor --check-bridge`). Rebasing meant resolving heavy conflict zones in `hook_bridge.py`, `cockpit.py`, `subspace_registry.py`, `registry.py`, `store.py`, `mcp/core.py`, and an 824-line test file. Extracting the unique 30% as a fresh PR (#803) and closing #786 as superseded was the cleaner path — 250 lines vs 1870, zero conflicts. When the daughter chain diverges this far from a parent, treat the parent as a checkpoint, not a destination.

## What didn't get fixed

### nexus-9eaz — concurrent migration race

`tests/test_migrations.py::TestApplyPending::test_concurrent_apply_pending_stress` and `test_concurrent_apply_pending_runs_once` periodically fail on Linux CI with `bootstrap_version called twice despite _upgrade_lock`. The code under inspection is correct on paper: `threading.RLock` serialises the check-then-add on `_upgrade_done`. 100 local iterations on darwin produced zero failures.

What we tried:
- Static analysis of `apply_pending` lock semantics: clean.
- Search for monkey-patching of `_upgrade_lock` or `_upgrade_done`: only `with`-block reentrant uses in tests, no leak vectors.
- 100-iteration stress on darwin: no reproductions.
- Audit of autouse fixtures and conftest.py: no concurrent mutation paths.
- Cross-test pollution check: confirmed `_clear_upgrade_done()` autouse fixtures are module-scoped.

What's in place:
- Both flaky tests skipped with explicit pointer to `nexus-9eaz`.
- INFO-level structlog instrumentation added to `apply_pending`'s lock-protected window: `upgrade_done_check` and `upgrade_done_add` emit `path_key`, `thread_id`, `done_size`, and membership at every check-then-add (#811 + #814).
- Bead `nexus-9eaz` stays open until the next CI failure surfaces the evidence.

The instrumentation is the right shape: when two threads both report `membership=False` for the same `path_key`, we'll know the lock genuinely failed. When only one does, the race is elsewhere (test counter race, set mutation during the lock-protected window, or environmental).

## Operational notes

### CI cycle costs

A full CI run on this repo takes 13-16 minutes per push. Branch protection requires up-to-date with base on merge, so every time develop advanced through one PR, the other in-flight PRs had to re-sync. Across the chain that meant roughly eight sync-and-rerun cycles. Two practical mitigations were applied:

- **Auto-merge armed early**: every PR got `gh pr merge $pr --auto --squash` immediately after opening. The auto-merge fired the moment CI cleared, regardless of when the human checked.
- **Sync, don't rebase, when possible**: for the orthogonal PRs, `git merge develop --no-edit` into the branch (preserving the agent's commit history) was cheaper than rebasing. Force-pushes happened only when the rebase actually subsumed a conflict.

### Worktree hygiene

Several agents reported confusion about worktree isolation. The pattern that worked: spawn the agent with `isolation: "worktree"`, let the agent claim and branch off `develop`, and instruct it to stage by explicit path. Drift between the agent's worktree and the main repo only mattered when the agent inherited stale staged files from a sibling — the cross-contamination noted above. Add an early `git status --short` check to the agent's process to surface inherited state.

### Beads as work-tracking

The full epic was tracked in `bd` with a parent-child hierarchy. Closing a bead requires that the corresponding PR merged (verified via `git log --grep` if needed). The `Closes nexus-X` keyword in PR bodies is recognised by the merge automation as a hint but does not auto-close beads — manual `bd close <id>` was required after each merge.

## Action items

| # | Action | Owner | Bead |
|---|---|---|---|
| 1 | Investigate `_upgrade_lock` race once CI surfaces instrumented evidence | unassigned | nexus-9eaz |
| 2 | Add a pre-dispatch worktree-isolation check that aborts if the worktree has staged files inherited from a sibling | unassigned | — (filed if pattern recurs) |
| 3 | Document the `nx daemon t2 install --autostart` flow in the user-facing docs (#808 shipped the CLI; user docs not updated) | unassigned | — |
| 4 | Verify the chroma multi-process race protections cited in the #794 spike: daemon-routed bridge (already shipped via #789), flock-guarded `PersistentClient` construction, `nx doctor` pre-warm | unassigned | — (consider filing) |

## Provenance

- Initial handoff: `/tmp/nexus-continuation-pr786-2026-05-15.md` (see umbrella memory `session-handoff-2026-05-15-pr786-deep-review-state`)
- Epic: `nexus-hp9f` (closed)
- Open follow-up: `nexus-9eaz`
- PRs: #787, #788, #789, #791, #792, #794, #795, #800, #801, #802, #803, #806, #807, #808, #809, #811, #814
- Superseded: #786
