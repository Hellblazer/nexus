# RDR-144 Post-Mortem: Guided Onboarding & Local-Embedder Provisioning

**Closed:** 2026-06-02. **Shipped:** conexus 5.7.0 (PyPI + GitHub release).
**Epic:** nexus-qwl7o (closed). **Phases:** P1-P6 + phase-review gate, all closed.

## What shipped

`nx init` as a guided first-run verb: the 384-vs-768 local embedder is now an
informed choice, not a silent packaging default. Editable-safe `[local]`
extra-add, offline-safe bge-768 warmup into a stable XDG cache, a gate-locked
safe 384->768 collection migration (dry-run -> double-confirm -> reindex-first
-> delete-after-verify), `nx doctor` advisories for the two previously-invisible
embedder states, and an MCP server-instructions notice for Desktop/Cowork users
who never run the Claude Code SessionStart hook.

## What went right

- **Shape A + sub-(i) held end to end.** The locked decision (new guided verb,
  768-for-local, config.yml persistence, no packaging-default flip) survived all
  six phases without scope drift. The phase-review gate cross-walk confirmed all
  seven §Implementation-Plan items traceable to closed beads.
- **Spike before P5b paid off.** The "how does a non-interactive notice reach a
  Desktop user" question was a real unknown; the spike verified the MCP
  server-`instructions` mechanism (and rejected the RDR's original
  "first-tool-response" phrasing as fragile) before any code was written.

## What the stacked review caught that green tests did not

This arc is a clean demonstration of why both reviewers run at every boundary.
Each of these was found by review with the full suite green:

1. **P3 - write-path token diverged from the EF.** `local_model_token()` still
   resolved off `_fastembed_available()` while the EF resolved off the config
   choice, so a collection could be named bge-768 while embedding 384-dim.
   Both reviewers flagged it independently.
2. **P4 - mixed-collection silent note loss.** A `knowledge__` collection mixing
   indexed files with manual `store_put` notes classified as reindexable; the
   reindex covered only the file-backed chunks, so delete-after-verify dropped
   the notes. Data loss on *success* - the exact class the gate-locked protocol
   was designed to prevent. Fixed with a safe-by-default `allow_sourceless_loss`
   guard and a never-under-`--yes` interactive confirm.
3. **P4 follow-up - cascade-failure silence.** Extracting the delete cascade
   into `purge_collection_cascade` demoted its failure warnings from stderr to
   structlog-only, so a daemon-down delete would orphan catalog rows silently.
   Surfaced via `CascadeCounts.failures`.

Lesson reinforced: code-review-expert and substantive-critic catch different
classes; neither was visible from passing tests.

## Release-time hazards (process, not product)

- **The rdr-143 deletion trap.** RDR-143's draft file had been landed directly
  on `main` (out of band), but `develop` never received it. The release branch
  (cut from develop) therefore showed rdr-143 as a *deletion* in the diff to
  main - merging the release would have deleted a tracked RDR file, violating
  "never delete RDR files." Caught by a pre-PR `--diff-filter=D` check; fixed by
  merging `main` into the release branch before opening the PR. **Takeaway:**
  before a release PR, always run `git diff --diff-filter=D --name-only
  origin/main..HEAD` and reconcile main-only commits into the release branch.
- **Mid-session `reinstall-tool.sh` zombied the T2 daemon.** Swapping the nx
  wheel during release prep left an unreachable daemon holding the spawn lock,
  failing the first integration run. Cleared by killing the orphan + removing
  the stale `t2_spawn.lock`. The daemon also needed >15s to come up; the
  ensure-running timeout reported failure prematurely. **Takeaway:** after a
  wheel swap, expect a daemon restart and budget for slow first-start.
- **Dev-env-only test flake made hermetic.** `test_doctor_local_mode_shows_
  collection_count` failed only in a `[local]`-extra dev env (active EF
  auto-selects 768 vs a 384 fixture). Pinned the active model in the test rather
  than dismissing it as pre-existing. A sibling shared-EphemeralClient
  isolation flake (`startswith()` grabbing another test's collection) was fixed
  by exact-name matching + per-collection-unique chash.

## Deferred (tracked, not gaps)

None open. Both P4 hardening follow-ups closed this arc: nexus-s5m44 (empty-file
reindex-count inflation) and nexus-prgf4 (catalog orphan cleanup via the shared
cascade).
