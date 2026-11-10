---
paths:
  - ".github/**"
  - "scripts/*windows*"
  - "scripts/windows_*.py"
---

## CI Cost Discipline

Project policy, cited by name from the workflows. Full text and origin: [ci-cost-discipline](../../docs/agents/ci-cost-discipline.md).

- **Never test the same tree twice.** A tree that passed a PR's required checks does not re-run on the merge-push or at tag time; tag/release workflows publish, they do not re-test.
- **Expensive jobs run only when their inputs changed** (per-job path filters). Required checks stay satisfied via job-level skip or always-run-report-skip WITH a non-vacuity assert.
- **Never rebuild deterministic artifacts.** Version-pinned compiles are cached or prebuilt, keyed on exact inputs.
- **BILLED macOS/premium runners only where the artifact requires the platform** (release/tag builds), never in routine push/PR CI. Self-hosted runners on owned hardware are the exception.
- **Every workflow has a concurrency group; superseded runs cancel.**
- **Full matrix breadth only at merge boundaries.** PRs run the full matrix; interior pushes run the minimal representative.
- **Tag cadence is a cost decision.** Batch related work into one cut.

## GitHub Actions traps

Each was earned by one burn. Source: memory `feedback_release_and_develop_ci` § release_and_ci_mechanics.

- **`waiting` is a deployment-environment approval, a person's click, never runner backlog** (v0.1.59 sat for hours, 2026-07-31). A watch that emits only on job conclusions is silent on it, so a watch also emits on `waiting`, `queued` and `action_required`; a gate whose cost is a click carries a notification or is not built.
- **An environment does three things:** secret scoping, a ref policy, required reviewers. To drop the approval, remove the reviewers rule alone and keep the branch policy.
- **Approve a pending deployment without the UI:** write `{"environment_ids":[<id>],"state":"approved","comment":"<why>"}` to a file and run `gh api --method POST repos/Hellblazer/nexus/actions/runs/<run-id>/pending_deployments --input <file>`. This is an environment approval, not a fork-PR run approval (that click stays Sam's, below).
- **A required check must run and report `skipped`.** Never put a workflow-level `paths:` filter on a workflow that carries a required check: it never reports and wedges every PR. Gate the expensive job with a job-level `if:` on a SHA-pinned detector, and make the detector a required check too; a failed detector that skips the job is fail-open. Admin direct pushes bypass required checks, so the gate has to hold without them.
- **Never rename a required job.** The release preflight's `ci-evidence-contexts` leg fails on drift from branch protection.
- **`on.schedule` fires only from the default branch.** A scheduled workflow authored on develop never runs, and one retired on develop keeps firing from main (2026-07-31: the watchdog written to catch silent red gates was itself inert). After any schedule change, diff the workflow file against `origin/main`.
- **Read a red gate's own header before patching its symptom.**
- **`gh pr merge --auto` is never used.** It fires when the required checks pass, including while a non-required check is red, and squashes only the tip at that moment, orphaning later pushes (three times, 2026-05 to 2026-06). Release PRs merge by hand with `--merge` after CI is green.
- **Read completion from the run's or the commit's check-run conclusion.** The jobs API and `statusCheckRollup` can show a job in progress forever on a completed run, and a wait on them never fires (7.6.0, 2026-08-11). One read, not a loop (`docs/agents/worktrees.md` rule 7).

## Self-hosted runners and fork PRs

Full text (runner inventory, lab pool, hellmini, win-release): [self-hosted-runners](../../docs/agents/self-hosted-runners.md).

- CI runs on GitHub-hosted runners except two lab-pool jobs (Service CI Java on `lab-16c16g`, pytest shards on `lab-6c18g`, or `lab-6c18g-release` for a pull request into `main`) and the release legs on `hellmini` / `win-release`. **A pull request from a fork never runs on those:** a new pool job must carry the same `runs-on` expression that sends fork PRs to `ubuntu-latest`. Repo variable `NX_CI_POOL=off` sends every run back to `ubuntu-latest`.
- Pool runners share a persistent `$HOME`: a step that writes a fixed path under it must be safe against a sibling job. No sudo; hard memory cap (exit 137 = over it).
- **Before approving a fork-PR run, read its diff for:** anything under `.github/`; any `runs-on` naming a self-hosted label (`self-hosted`, `hellmini`, `win-release`, a `lab-*` label, `hellmini-ci`, `qwen-linux`, `gtr-windows`); any change to the `runs-on` expression that routes forks to `ubuntu-latest`.
- **Agents never approve a fork-PR run** (`POST /actions/runs/{id}/approve`), even holding the owner's `gh` token. Report the run id and stop; the click is Sam's.
- **Read a contributor change as release-runner code, not only a fork-PR run:** merged code on `win-release` runs without a further click. Read in full what it executes if it touches `windows-pg-bundle-rehearsal.yml`'s `paths:` filter (including `service/pom.xml`), `scripts/*windows*`, `scripts/windows_*.py`, `.github/actions/windows-*`, `.github/actions/resolve-windows-python/**`, or the Windows jobs of `engine-service-release.yml` and `pg-bundle-cache-seed.yml`.
- Registering `win-release` or flipping `NX_WINDOWS_RELEASE_LEGS` needs Sam's explicit go.
- Heavy commands on chas run under `lab-run`, engine builds through `mvnw-leased.sh --lab-run <class>`: root `AGENTS.md` rule 13 is the rule; runner detail is in the full text above.
