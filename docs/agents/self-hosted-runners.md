# Self-hosted runners and fork PRs, full text

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); AGENTS.md keeps only the per-turn rules and links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

## Self-hosted runners and fork PRs

CI runs on GitHub-hosted runners, except two jobs that run on the owned lab
pool (below). The `qwen-linux` and `hellmini-ci`
routes, the runner isolation probes, the box lock and the qwentescence
post-publish gate were deleted on 2026-10-03 (cleanup step 7). `hellmini` (a Mac
mini, macOS user `ghrunner`) is a self-hosted runner for release jobs only. Since
2026-10-11 (Sam, nexus-9q6ox) the mac-arm64 build, signing and PG-bundle legs run
on the lab pool's `lab-mac-m` class on the same machine, and the persistent
`hellmini` runner keeps the mac-arm64 smoke (it needs Docker, which the pool
lacks) and one leg of the signing rehearsal (Sam, 2026-09-28, nexus-yd9po). `qwentescence` (WSL) is a test host reachable by
ssh, not a runner; its native Windows side is the separate `win-release` runner
described below, and the place hand and agent Windows native builds run (through
`lab-run-win`, below). `chas` (a native Ubuntu 26.04 box, 32 cores, 123 GB, ssh
alias `chas-test`) is the test host for agents' hand runs and, since
2026-10-10, the main host of the lab pool. Agents' full Python
suites and engine suites go to chas first (2026-10-08, nexus-y10bw). hellmini takes
macOS-specific runs, and the overflow when chas is busy. The T2 how-tos
`nexus/chas-test-host-howto`, `nexus/hellmini-second-test-host-howto` and
`nexus/qwentescence-test-host-howto` carry the recipes.

**The lab pool** (Sam, 2026-10-10, nexus-480wm; T2 `homelab/labrun-pool-howto`)
is a set of single-use runners that a scheduler on chas starts per job, with
labels `lab-s`, `lab-m`, `lab-6c18g`, `lab-16c16g`, `lab-l`, `lab-xl` (Linux) and `lab-mac-s`, `lab-mac-m`
(macOS on hellmini). Two CI jobs use it: the Service CI Java job on `lab-16c16g` (16 cores, 16 GB) and the
pytest shard matrix on `lab-6c18g` (6 cores, 18 GB; six fit on chas at once),
with 4 xdist workers inside each shard (not `-n auto`: each worker also runs
an engine JVM and a Postgres).
The mac-arm64 release legs use the macOS classes (below, § Engine-service
release). Every other job stays where it was. Three things
differ from a GitHub-hosted runner:

- **A pull request from a fork never runs there.** Both jobs choose the runner
  with a `runs-on` expression that gives `ubuntu-latest` when the pull request's
  head repository is not this one. A new pool job must carry the same
  expression. The repo variable `NX_CI_POOL=off` sends every run back to
  `ubuntu-latest`; set it when chas is down, because both jobs feed required
  checks and a job with no runner waits.
- **`$HOME` and the tool cache outlive the job and are shared** with jobs
  running at the same time (the work directory is per job and deleted). So a
  pool job skips `actions/cache` for `$HOME` paths (`if: runner.environment ==
  'github-hosted'`) and passes `persistent-home` to the `prime-*` actions, which
  then use the files on disk when their digests match and install a download by
  rename. A step that writes a fixed path under `$HOME` must be safe against a
  sibling job doing the same.
- **No sudo, and a hard memory cap** (18 GB on `lab-6c18g`, 16 GB on `lab-16c16g`); a
  step over the cap is killed with exit 137. Containers started through the
  host Docker daemon are outside the cap. The host session caps how many nexus
  jobs run at once. A hand run as `nxtest` on chas is invisible to the
  scheduler unless it goes through the wrapper, so run a heavy suite there as
  `lab-run -c <class> -- <command>`: it queues behind CI, then runs the
  command in place with the class's cores and memory cap, and `-n auto`
  resolves to the grant.
  An engine build inverts the order: `scripts/mvnw-leased.sh --lab-run <class>
  ...` takes the build lease first and then the grant, so waiting for another
  build holds no cores (nexus-iqmih).

`hellmini` is a bare custom label (registered with `--no-default-labels`), so a
job has to name it. There are two persistent self-hosted registrations, `hellmini`
and `win-release`, beside the pool's labels: `qwen-linux` and `gtr-windows` were
removed on 2026-10-03 and `hellmini-ci` on 2026-10-04, so a job that says
`runs-on: self-hosted` (or names any other self-hosted label) waits with no
runner rather than landing somewhere unexpected. A `runs-on` label is a routing rule, not a
security boundary: whoever can push a branch controls the workflow files. The
controls are the collaborator list (owner only), the fork-PR approval policy and
branch protection.

`win-release` is the native Windows side of qwentescence, a self-hosted runner
for the Windows release legs only: the windows-x64 PG-bundle leg and the
windows-x64 engine leg (`build-publish-engine-windows`: native build, dumpbin
dependency check, the `nexus-service-windows-x64.txz` with the four VC++ DLLs,
a smoke against the Windows PG bundle; its steps are the composite action
`.github/actions/windows-engine-leg`, which the rehearsal runs too) in
`engine-service-release.yml`, the PG bundle's seed in `pg-bundle-cache-seed.yml`
and `windows-pg-bundle-rehearsal.yml` (RDR-224 P0.1, Sam 2026-10-05). It is a bare
custom label like `hellmini`, never runs a `pull_request` job, is inside the
release trust boundary on the same terms as hellmini, and persists state between
jobs (the actions tool cache, uv and the Pythons it installs, the Visual Studio
Build Tools). The runner's name and service must not contain `qwen` or `llama` (a host
rule). Every job on it is gated on the repo variable `NX_WINDOWS_RELEASE_LEGS`
being `on`; with it unset a tag run queues nothing and promotion expects no
Windows asset, and with it `on` the Windows assets block promotion (P0.4). The
runner (`gtr9-win`, service account `ghwin`; T2 `nexus/qwentescence-win-release-runner`)
was registered and the variable turned `on` by an agent on 2026-10-06, each on Sam's
explicit go; any later change to either needs the same explicit go.

**Hand and agent Windows native builds** (GraalVM native-image with MSVC) run on
qwentescence's Windows side through `C:\build\lab\lab-run-win.cmd`, never in WSL
and never by a bare ssh build (labrun README, "Windows native builds", labrun
PR #30):

    ssh qwentescence 'cd C:\build\hand\<you>\...; C:\build\lab\lab-run-win.cmd .\mvnw.cmd -B -Pnative -Pprebuilt-jooq "-Dnative.image.maxheap=11g" package'

`lab-run-win` and the `win-release` runner's job hooks share one build slot (a
lock file in `C:\build\lab\lock`): a hand build waits for a running CI leg, and a
CI leg waits at most 30 minutes for a hand build, then runs beside it, the wait
counting against the job's timeout. A build started over ssh without
`lab-run-win` is invisible to the slot. Quote every dotted `-D` argument (the ssh
shell is PowerShell 5.1, which splits at the dot), work under
`C:\build\hand\<you>`, and keep `maxheap` at 11g or below (the release leg uses
12g and Windows-side memory is not booked). The GraalVM there follows the
`java-version` pin in `.github/actions/windows-engine-leg/action.yml`; a bump
there needs labrun's installer re-run with the new version.

**Before approving a fork-PR run, read its diff for:**

- anything under `.github/` (a workflow or action can name any runner);
- any `runs-on` that names a self-hosted label, `self-hosted`, `hellmini`,
  `win-release`, a `lab-*` pool label, `hellmini-ci`, `qwen-linux` or
  `gtr-windows`;
- any change to a `runs-on` expression that sends fork pull requests to
  `ubuntu-latest`.

**Agents never approve a fork-PR run** (`POST /actions/runs/{id}/approve`), even
when they hold the owner's `gh` token. Report the run id and stop; the approval
click is Sam's.

**Review a contributor change as release-runner code, not only a fork-PR run**
(nexus-f9bgu.27, RDR-224 review). A fork PR's run needs a click; MERGED code does
not. A push to `develop` runs `windows-pg-bundle-rehearsal.yml` on `win-release`
under the merger's actor, so its `github.actor == 'Hellblazer'` guard says who
merged, not who wrote the code, and the scripts, the pom and the composite
actions it runs execute with the runner's standing access and no further click.
So a contributor change that touches ANY path in that workflow's `paths:` filter
(which includes `service/pom.xml`, since a pom edit is arbitrary code at build
time), `scripts/*windows*`, `scripts/windows_*.py`, the `.github/actions/windows-*`
actions, `.github/actions/resolve-windows-python/**` or the Windows jobs of
`engine-service-release.yml` and `pg-bundle-cache-seed.yml` is read in full for
what it executes (commands, downloads, `subprocess`, environment, anything that
reaches the network or the runner's files), the same way as a change under
`.github/` above. The Windows jobs carry the owner actor guard and the
`NX_WINDOWS_RELEASE_LEGS` switch; neither makes the code they run trustworthy. If
that standing is ever too much, a GitHub `environment:` with a required reviewer on
those jobs would make the click mechanical; it is a repository setting and Sam's
call, so it is not configured.

**What the rehearsal does not prove.** `windows-pg-bundle-rehearsal.yml` runs the
shared composite actions and scripts. It deliberately does not run
`engine-service-release.yml`'s own Windows jobs (a push trigger on a release
workflow would run release jobs): the cosign steps, `gh release upload`, the
cross-job artifact handoff, cache restore on the self-hosted runner and the
promotion check are proven only by a `workflow_dispatch` run of
`engine-service-release.yml` on `develop` with the switch on, before the first
Windows-carrying cut. The `engine-release` skill's pre-cut steps name it.
