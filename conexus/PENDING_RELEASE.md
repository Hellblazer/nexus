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

## Awaiting the next release (pinned: v7.2.0)

- `conexus/skills/test-authoring/SKILL.md` — NEW reference skill: nexus
  test-suite layer routing (-n auto / -m lint / integration / scenario
  journeys) + authoring directives from the 2026-08-05 compression arc.
  Until released, sessions get the same guidance from `tests/AGENTS.md` and
  root `AGENTS.md` (already live on develop), so the gap is informational,
  not protective.
- `conexus/registry.yaml` — standalone_skills entry for `test-authoring`
  (same change, same informational-only gap).
- `conexus/hooks/scripts/pre_close_verification_hook.sh` — nexus-4av2n
  scope (a), ROUND 3: `bd close`/`bd done` BLOCKS on a missing
  review-completed marker instead of only advising, and `verification=passed`
  is stamped only on the marker-found branch (previously unconditional — a
  false audit record). Coverage lookup is DUAL-SOURCE (T1 scratch OR T2
  memory) since a marker written via the MCP scratch tool is invisible to a
  T1-only CLI check whenever the CLI's own T1 lease is stale (round-2
  Critical-1, reproduced live). The `bd close|done|create` matcher is
  tokenized (no longer a blanket substring grep that could false-trigger on
  a commit message merely mentioning the words). ROUND 3 (Critical,
  substantive-critic closure verification): coverage lookup is now bounded
  by a wall-clock deadline (3.5s) the hook enforces on itself, denying
  deterministically rather than ever risking the harness's own 5s
  PreToolUse timeout killing the hook mid-check (round 2's call-count
  budget did not bound wall-clock time and could stack past that ceiling).
  PROTECTIVE gap: until the pin advances, every running session still has
  the old advisory-only, T1-only, always-stamps-passed, substring-matching,
  wall-clock-unbounded hook.
- `conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py`
  — nexus-4av2n scope (b), ROUND 3: consolidated third check denies
  `git push` when the outgoing range carries a gated-path commit (`src/`,
  `service/src/main/`, `conexus/`, `tests/`) with no review-completed
  coverage (T1 OR T2, same dual-source fix as scope (a)). Pure tag pushes
  (including `engine-service-vX.Y.Z`) and `release/*` branch pushes are
  exempt (loud, not silent); merge commits are now visible to the
  gated-path scan (were previously silently ungated regardless of content
  — bare `git diff-tree` returns nothing for merges by git's own default);
  the 50-commit scan cap now warns loudly instead of silently passing when
  exceeded. ROUND 3 (Critical, substantive-critic closure verification):
  the round-2 call-count T2 budget did not bound wall-clock time — the real
  2026-07-31 incident shape (5-8 uncovered gated commits) measured
  6.49s-6.91s live, exceeding the hook's own 5s PreToolUse timeout.
  Replaced by a self-enforced wall-clock deadline (3.5s); the fast-path
  range-tip T2 lookup that round 2 ran unconditionally (costing a second
  `nx` call even when T1 alone already covered everything) is now lazy,
  restoring the 1-call fast path. PROTECTIVE gap: until the pin advances,
  no installed session gates git push on review coverage at all — this is
  the check the bead's own postmortem says would have caught the
  2026-07-31 miss (all five unreviewed commits were pushed before any bead
  close that day).
- `conexus/hooks/scripts/routing/registry.yaml` — rationale update for
  `git_add_all_redirects_to_explicit_paths` documenting the third check and
  its round-2/round-3 revisions above (same change, no new registry entry
  — the RDR-121/125 aggregate cap stays at 4).
