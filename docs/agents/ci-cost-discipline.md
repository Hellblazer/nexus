# CI Cost Discipline, full text

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); AGENTS.md keeps only the per-turn rules and links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

## CI Cost Discipline

Project policy, cited by name from the workflows (nexus-jndz0 landed it here
so those citations resolve inside the repo; origin: the 2026-07-06 billing
incident — 24 CI runs + 4 engine tags in one day, macOS at ~10x rates, the
same tree tested four times en route to PyPI. Reference implementation:
PRs #1375/#1376):

- **Never test the same tree twice.** A tree that passed a PR's required
  checks does not re-run CI on the merge-push or at tag time. Tag/release
  workflows publish — they do not re-test.
- **Expensive jobs run only when their inputs changed.** Per-job path
  filters on native builds, from-source compiles, platform matrices.
  Required checks stay satisfied via job-level skip (skipped == success for
  branch protection) or always-run-report-skip WITH a non-vacuity assert.
- **Never rebuild deterministic artifacts.** Version-pinned compiles (PG
  bundles, models, toolchains) are cached or prebuilt, keyed on exact
  inputs; rebuild only on key miss.
- **BILLED macOS/premium runners only where the artifact requires the
  platform** (release/tag artifact builds), never in routine push/PR CI. The
  self-hosted runners on owned hardware carry no per-minute charge and are the
  exception; see § Self-hosted runners and fork PRs.
- **Every workflow has a concurrency group; superseded runs cancel.**
- **Full matrix breadth only at merge boundaries.** PRs run the full
  matrix; interior branch pushes run the minimal representative.
- **Tag cadence is a cost decision.** Batch related work into one cut.
