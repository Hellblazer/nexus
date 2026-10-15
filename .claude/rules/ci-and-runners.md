---
paths:
  - ".github/**"
  - "scripts/*windows*"
  - "scripts/windows_*.py"
---

## CI Cost Discipline

Project policy, cited by name from the workflows. Full text and origin: [ci-cost-discipline](docs/agents/ci-cost-discipline.md).

- **Never test the same tree twice.** A tree that passed a PR's required checks does not re-run on the merge-push or at tag time; tag/release workflows publish, they do not re-test.
- **Expensive jobs run only when their inputs changed** (per-job path filters). Required checks stay satisfied via job-level skip or always-run-report-skip WITH a non-vacuity assert.
- **Never rebuild deterministic artifacts.** Version-pinned compiles are cached or prebuilt, keyed on exact inputs.
- **BILLED macOS/premium runners only where the artifact requires the platform** (release/tag builds), never in routine push/PR CI. Self-hosted runners on owned hardware are the exception.
- **Every workflow has a concurrency group; superseded runs cancel.**
- **Full matrix breadth only at merge boundaries.** PRs run the full matrix; interior pushes run the minimal representative.
- **Tag cadence is a cost decision.** Batch related work into one cut.

## Self-hosted runners and fork PRs

Full text (runner inventory, lab pool, hellmini, win-release): [self-hosted-runners](docs/agents/self-hosted-runners.md).

- CI runs on GitHub-hosted runners except two lab-pool jobs (Service CI Java on `lab-16c16g`, pytest shards on `lab-6c18g`) and the release legs on `hellmini` / `win-release`. **A pull request from a fork never runs on those:** a new pool job must carry the same `runs-on` expression that sends fork PRs to `ubuntu-latest`. Repo variable `NX_CI_POOL=off` sends every run back to `ubuntu-latest`.
- Pool runners share a persistent `$HOME`: a step that writes a fixed path under it must be safe against a sibling job. No sudo; hard memory cap (exit 137 = over it).
- **Before approving a fork-PR run, read its diff for:** anything under `.github/`; any `runs-on` naming a self-hosted label (`self-hosted`, `hellmini`, `win-release`, a `lab-*` label, `hellmini-ci`, `qwen-linux`, `gtr-windows`); any change to the `runs-on` expression that routes forks to `ubuntu-latest`.
- **Agents never approve a fork-PR run** (`POST /actions/runs/{id}/approve`), even holding the owner's `gh` token. Report the run id and stop; the click is Sam's.
- **Read a contributor change as release-runner code, not only a fork-PR run:** merged code on `win-release` runs without a further click. Read in full what it executes if it touches `windows-pg-bundle-rehearsal.yml`'s `paths:` filter (including `service/pom.xml`), `scripts/*windows*`, `scripts/windows_*.py`, `.github/actions/windows-*`, `.github/actions/resolve-windows-python/**`, or the Windows jobs of `engine-service-release.yml` and `pg-bundle-cache-seed.yml`.
- Registering `win-release` or flipping `NX_WINDOWS_RELEASE_LEGS` needs Sam's explicit go.
- On chas run a heavy suite as `lab-run -c <class> -- <command>` so it queues behind CI.
