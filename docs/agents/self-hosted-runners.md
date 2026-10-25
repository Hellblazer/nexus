# Self-hosted runners and fork PRs, full text

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); the short rules-only form is [`.claude/rules/ci-and-runners.md`](../../.claude/rules/ci-and-runners.md), which links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

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
  The suite also caps its own workers to the grant's memory (grant / 3.2 GiB
  each, a full suite's measured peak, nexus-vo9qn): `-n auto` runs 15 on `lab-l` and 5 on `lab-m` (8 -> 5 workers, a
  longer wall clock, in exchange for staying on tmpfs and clear of the grant's OOM kill).
  Substrate clusters go on `/dev/shm` for the whole run (decided once, at the
  start) rather than on the NVMe, and the pytest summary says
  `clusters on tmpfs N, on disk M`; a start that cannot fit fails loud instead of
  writing to disk. `NX_XDIST_NO_CAP=1` and `NX_TEST_PG_ROOT=default` are the
  escapes; `default` puts the clusters back on the NVMe, which is the write this
  removes.
  Pytest's basetemp and `TMPDIR` go on `/dev/shm` too (nexus-kyujr): a CI-shaped
  shard wrote 442 MiB to the NVMe, 227 MB of it `tmp_path` data that pytest kept for
  three runs. Inside a grant, with a writable exec-capable tmpfs and the same 3 GiB
  floors, the controller makes `/dev/shm/nexus-tmp-<uid>/r<random>` once per run,
  points `TMPDIR` and `PYTEST_DEBUG_TEMPROOT` at it, and deletes it when the run
  ends; a killed run's dir is removed by the next one. The summary line `nexus test
  tmp: basetemp and TMPDIR on tmpfs ...` (or `on disk ... because: <reason>`) says
  where it went. The data is charged to the grant's memory: one CI shard (one of six)
  grew `~/tmp` by about 230 MB per RUN, not per worker, and a hand full-suite run would
  hold about 1.4 GB; neither is measured on tmpfs yet. Of the shard's 442 MiB of NVMe
  writes, the rest (~215 MiB) is not attributed yet (nexus-ww33u). `NX_TEST_TMP_ROOT=default` is the
  escape; `NX_TEST_TMP_ROOT=<dir>` keeps the tree in `<dir>` for inspecting a failure.
  The DDL and storage-touch `nx-*` reports are copied to
  `<tempdir>/nexus-test-reports-<uid>/<run>` before the removal, and a run that exits
  non-zero also copies the per-test engine logs (`nexus-svc-*`; the session engine's own log is not kept)
  and the failed tests' `tmp_path` trees (256 MiB
  cap) to `<run>/failed/`. The engine JVM gets `-Djava.io.tmpdir=<TMPDIR>` (the JDK
  ignores `TMPDIR`), so its native extraction lands on the same tmpfs; its hs_err report
  goes to the original tempdir. The `tests/_tmp_root.py` docstring has the full paragraph.
  If `SubstrateRootExhausted` trips, the floor is 512 MiB of tmpfs space or cgroup
  headroom after a 45 s wait: read the cause in the exception, the summary line and
  the `cluster_root` events (`--log-level=INFO`), then run fewer workers (`-n` =
  headroom / 3.2 GiB) or a larger class, then `NX_TEST_PG_ROOT=<dir>`
  (`tests/AGENTS.md` has the full paragraph).
  An engine build inverts the order: `scripts/mvnw-leased.sh --lab-run <class>
  ...` takes the build lease first and then the grant, so waiting for another
  build holds no cores (nexus-iqmih); `scripts/build-gate-jar.sh --lab-run <class>`
  does the same for the gate jar (nexus-t6t9f). Run inside a grant instead,
  `mvnw-leased.sh` and `build-gate-jar.sh` refuse at once with exit 75 when another
  build holds the lease (nexus-dfg0q); CI jobs keep waiting. `--lab-run` inside a
  grant is refused (exit 64): a second grant can wait forever while the first is held.

**Standing worktree for verification runs on chas** (nexus-fkhd3, under
nexus-aca0q). Measured 2026-10-19: once the test phase stopped writing, what a
verification run still paid per run was setup: a fresh worktree, `uv sync` into
a new `.venv`, and a gate-jar build whose jOOQ codegen boots Postgres and
replays the whole Liquibase changelog (~1000 changesets, ~350-410 MB written).
So a session mints ONE worktree and reuses it. The jar is built first, in its own earlier
grant (`scripts/build-gate-jar.sh --lab-run <class>`), the suite runs inside a later one:

```bash
# once per session, as nxtest on chas, from the clone that holds the other worktrees
git -C ~/src/nexus worktree add --detach ~/src/nexus-wt/<session> <sha>

# every run: announce it to labrun first (see below), then move the SAME worktree to the commit under test
cd ~/src/nexus-wt/<session>
git fetch origin
git checkout --detach <sha>
test -z "$(git status --porcelain)" || { echo "dirty tree: a stray edit or stamp keys a different jar"; exit 1; }

# (a) the jar: --lab-run takes the build lease first (waiting for it costs no cores), then builds under a lab-m grant
scripts/build-gate-jar.sh --lab-run lab-m

# (b) the suite, inside the grant, under the stall watchdog
lab-run -c lab-l -- bash -c 'uv sync && python3 scripts/watched_pytest.py --stall 180 --status ~/pytest-<session>.json -- uv run pytest -n auto <selection>'
```

- **The jar is built inside `lab-run`** (labrun, 2026-10-19: Maven package is
  heavy work, and no heavy work runs outside a grant). `build-gate-jar.sh --lab-run
  <class>` does it in the right order: it waits for the build lease outside any
  grant, then runs Maven as `lab-run -c <class> -- ./mvnw ...` in its own process
  group, so the build is visible to the pool scheduler and a TERM reaches it;
  `release.properties` is restored and the lease released only after that group
  is gone (nexus-t6t9f). A `HIT` starts no `lab-run` at all. The older form,
  `lab-run -c lab-m -- scripts/build-gate-jar.sh`, still works: inside a grant a hand
  run does not wait for the build lease and refuses at once with exit 75 when
  another build holds it (nexus-dfg0q), so the grant never sits idle on a lease. On
  75, wait and rerun; never fall back to running unwrapped. A `HIT` (the `gate jar
  cache HIT` line) runs no Maven and writes no jar bytes: it hard-links the
  cached jar into `service/target` (`jar link`), or leaves the jar already there
  (`jar kept`), and copies the 131 MB only where linking fails (`jar clone-or-copy` or
  `jar copy`, e.g. another filesystem; nexus-k6zxp). The shared inode is why the cache entry
  is read-only and why `build-gate-jar.sh` and `mvnw-leased.sh` unlink a shared
  `service/target` jar before a `package`-reaching build, so a bare `./mvnw
  package` or an IDE build right after it fails with "jar is read-only" (remedy:
  remove `service/target/nexus-service-1.0-SNAPSHOT.jar`, or build through
  `mvnw-leased.sh` / `build-gate-jar.sh`); a `MISS` runs Maven under the grant.
- **The suite goes through `scripts/watched_pytest.py`**, never a bare pytest, and
  you read its status file instead of sleeping and guessing. `uv sync` sits inside
  the grant because it is incremental against the kept `.venv`.
- **Keep the `.venv`.** Never `rm -rf .venv` between runs: `uv sync` against the
  existing one installs only what the lockfile changed. A new worktree is the
  only reason to pay a full sync.
- **Why the build is now cheap, and what to check.** The gate-jar cache is keyed
  on `service/pom.xml`, `service/src/main/**`, `service/src/build/**`, `service/mvnw`
  and `service/.mvn/**` only, so a commit that touches `service/src/test` or docs prints `gate jar cache
  HIT` and runs no Maven. On a real MISS the jOOQ generated sources come from the
  jOOQ cache when the changelog, pom and jOOQ/PostgreSQL versions are unchanged
  since any worktree of this clone generated them, so no codegen container starts;
  only a changelog or pom edit regenerates. `NX_GATE_JAR_CACHE=off` and
  `NX_JOOQ_CACHE=off` force a rebuild; `-Dnexus.jooq.regenerate=true` forces a
  regeneration (the floating `pgvector:pg17` image tag is the one input the hash
  cannot see). Both caches are best-effort: one that cannot be written or read
  whole costs a warning and a rebuild, never a failed build.
- **Where the caches are, how big, who prunes.** Both live in the clone's git
  common dir, so the standing worktrees must be worktrees of ONE clone, not
  separate clones: `.git/nexus-gate-jar-cache/` (one directory per key holding the
  stamped jar, ~131 MB each, newest 5 kept, `GATE_JAR_CACHE_KEEP`) and
  `.git/nexus-jooq-cache/` (`main-<hash>-<producer>/` and `test-<hash>-<producer>/`,
  ~4.4 MB each, newest 6 of each kind kept, `JOOQ_CACHE_KEEP`). So the budget is
  about 0.7 GB for the jars plus about 55 MB for the trees. Pruning is owned by
  `scripts/build-gate-jar.sh`, after a build that missed; nothing else prunes, so a
  `mvnw-leased.sh`-only workflow adds jOOQ entries and never trims them. An entry is
  taken out of service by rename before it is deleted, so a prune never empties an
  entry a restore is copying. To empty a cache by hand, `rm -rf` the directory with
  no run in flight.
- **Announce each run to the lab owner session first.** That session is `labrun`
  (peer-messaging bus): send it the class, the selection, the sha, and whether the
  jar will MISS. The pool is shared with CI and with other sessions' runs, labrun is
  the one place that sees all of them, and a change to shared state (a runner, a
  class, a cache or worktree you do not own) is claimed through it, not made.
- **Prune the others.** A worktree no session owns is dead weight (23 sat in
  `~nxtest/src/nexus-wt` at 13 GB on 2026-10-19): `git worktree remove` it, then
  `git worktree prune`.

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
