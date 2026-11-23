---
name: engine-release
description: Use when cutting or deploying the Java engine-service binary (engine-service-vX.Y.Z), refreshing the cloud engine, or validating develop's engine tip in the cloud. This is the SECOND release lifecycle — separate from, and not gated by, the conexus PyPI release (use the `release` skill for that). Authority: docs/agents/engine-service-release.md (.claude/rules/releases-and-engine.md § Engine-service release keeps the short rules and links it).
---

# Engine-service Release Checklist

The Java engine-service is a separate release artifact from the conexus PyPI package. Cutting it is lightweight, frequent, and **NOT gated by the luxe6 / RDR-155-P4a develop release boundary** — so the cloud engine can (and must) be kept current with develop's engine tip independently of develop being unreleasable. Conflating the two lifecycles is how the cloud engine silently drifts (2026-06-26: 22 `service/` commits / 4 days un-deployed, un-cloud-tested).

Follow in order. The releaser is **Sam**: AI preps + validates; Sam pushes the tag, or his go for the release lets the AI push it (Step 4).

## Steps

**Removed in cleanup step 11 (nexus-0r1uz), numbering left as it was:** Step 3
(the local-candidate `--shakeout` gate and the `--candidate-migration` populated-store
rehearsal) and Step 3c (the published-client write leg) went with the scripts and
harness modes they ran. Steps that remain: 1, 1b, 2, 2b, 3b, 3e, 3f, 3g, 3h, 4, 5, 5b, 5c, 6, 6.1, 7, 8.

### 1. Decide whether a cut is needed (drift check)

```bash
git tag -l "engine-service-v*" | sort -V | tail -1     # last engine tag
git log --oneline <last-engine-tag>..HEAD -- service/   # what engine changed since
```

Cut a fresh engine when `service/` has accumulated **cloud-relevant** work: pooler/RLS, pgvector, catalog conformance (RDR-168), aspect queue (RDR-163), batch endpoints, embedder. Don't let it pile up — a large unvalidated engine delta means any "cloud test" result is testing a stale binary, and any PyPI release pinning that tag ships behind.

**Size every open engine bead before recommending a cut.** A cut is about two hours of wall clock and three premium native builds. List `bd ready` plus the in-progress engine beads, size each in minutes, fold everything under a few hours into the cut, and say the sizing out loud; only a bead that genuinely needs design or measurement waits. v0.1.97 was cut for one bead while a twenty-line sibling was already filed and rode a second cut (Sam, 2026-09-03: "wtf did we do that for when we know we had more to do?"). Source: memory `feedback_release_and_develop_ci` § engine_cut_discipline.

### 1b. Freeze the tag commit when the cut starts (Sam, 2026-11-19)

Pick the commit you will tag when the cut starts, record it on the cut bead as `TAG_SHA`, and make a clean worktree at it (`TAG_TREE`). Every gate below runs on that one tree. A fix that lands on develop after the freeze goes to the next cut: do not move the tag commit to chase develop. Only a red gate on the frozen tree moves it, and then you freeze a new commit and every gate starts again on it, because a gate's result is about the tree it ran on. On 0.1.196 the Step 3h legs ran three times because the tag commit moved twice while they ran.

```bash
TAG_SHA=$(git rev-parse origin/develop)      # or the develop commit the cut is sized for (Step 1)
git worktree add --detach ../nexus-wt/engine-cut-"$TAG_SHA" "$TAG_SHA"
TAG_TREE=$(cd ../nexus-wt/engine-cut-"$TAG_SHA" && pwd)
```

### 2. Verify the engine is green on the exact commit you'll tag

The version is **tag-stamped** — there is NO manifest to bump (`release.properties` `release_version` is blank in source, stamped at native-build time from the tag; the Maven pom stays `1.0-SNAPSHOT`).

Confirm the full Java suite (main and integration group) and the native build passed on the frozen `service/` tree. Service CI's `Java tests + jOOQ codegen drift guard` job runs both suites; when it concluded success on a commit whose `service/` tree equals `TAG_SHA`'s, that run is the gate and no chas suite runs (a second run of the same tree finds nothing the first did not). The job must have RUN: Service CI skips it when it decides `service/` did not change, and a skipped, cancelled or timed-out job covers nothing. The native build is CI's `engine-service native build (PR trip-wire)` check concluded `success` (not `skipped`: since suite audit item 8 the trip-wire skips a push that changes none of its inputs, and a skipped check builds nothing) on a commit whose `service/` tree, `.github/workflows/ci.yml`, `.github/actions/maven-cache/` and `scripts/check_native_embedded_resources.py` equal `TAG_SHA`'s (the trip-wire's inputs, `scripts/native_trip_wire_svc.py`), or the Step 3f dispatch run, which builds every native platform.

```bash
# the run that covers the frozen tree: Java tests job concluded success, not skipped
git diff --stat <service-ci-sha> "$TAG_SHA" -- service/    # empty = same engine tree
gh api "repos/Hellblazer/nexus/commits/<service-ci-sha>/check-runs" \
  -q '.check_runs[] | select(.name=="Java tests + jOOQ codegen drift guard") | .conclusion'   # success
# the native build of that tree: the trip-wire concluded success, not skipped (find the commit that built it)
git diff --stat <trip-wire-sha> "$TAG_SHA" -- service/ .github/workflows/ci.yml .github/actions/maven-cache/ scripts/check_native_embedded_resources.py   # empty
gh api "repos/Hellblazer/nexus/commits/<trip-wire-sha>/check-runs" \
  -q '.check_runs[] | select(.name=="engine-service native build (PR trip-wire)") | .conclusion'   # success
# only when no such run exists: one chas run of both suites on TAG_TREE (needs Docker for Testcontainers)
scripts/mvnw-leased.sh --lab-run lab-m -B test
scripts/mvnw-leased.sh --lab-run lab-m -B test -Dnexus.ddl.suite=integration -Dtest.excluded.groups= -Dgroups=integration -Dnexus.test.forks=4
```

The Java CI (`service-ci.yml`) is a required check on `main`, but nothing gates a push to `develop` on it, and a develop run can be cancelled or time out — so verify it actually passed on this tree rather than assuming.

### 2b. Start the pre-tag gates together on the frozen tree

The gates that take machine time start at the same time on `TAG_SHA`, not one after another: the chas suite of Step 2 when one is needed, the Windows dispatch run of Step 3f, the three legs of Step 3h, and the cloud leg once it is part of the cut. Steps 3e and 3g are reads of the tree and take seconds. None of these gates reads another's result, so running them together changes when each finishes, not what it checks. The cut waits for all of them; any red stops it (see Step 1b for what a red means for the tag commit). On 0.1.196 the gates were about two hours of machine time, and the wall clock went to serial waits for a green develop and to reruns after the tree moved.

When the cut changes the cut or release machinery itself (the engine release workflow, the promote and floor scripts, the rehearsal harness), rehearse the whole flow end to end in isolation first, as the `release` skill's Step 0c paragraph describes; build the missing rehearsal when you find the gap, do not file it.

Engine work is tested on develop before any cut: `scripts/mvnw-leased.sh test`, the Python substrate tests, and the local-service gate (`tests/e2e/local-service-gate.sh`) against a dev jar.

**Measure throughput before cutting a change to the thread model, admission, pool sizes, batch shape or planner GUCs.** Correctness gates do not measure it: v0.1.99 capped the shared ONNX session at two threads on a sixteen-core box, passed every suite, both reviewers and both rehearsal legs, and stalled the client shakedown for thirty minutes (Sam, 2026-09-04: "not testing this, which apparently was about 1/2 hour, cost us 8"). The A/B is one dev jar, two scratch installs, the old behaviour forced through its env override, and `nx index rdr` on thirty files; compare the elapsed times and record them on the bead. A reviewer's "unmeasured" on such a change is a red. A gate-latency shift that coincides with such a change is that change until proven otherwise; "inside bound" is not "not caused by us". Source: memory `feedback_release_and_develop_ci` § engine_cut_discipline.

### 3b. Client-release preconditions — a DEPLOY gate, NOT a tag gate

```bash
uv run python scripts/check_engine_release_floor.py --client-precondition engine-service-vX.Y.Z
```

Some engine changes BREAK clients that predate a specific client commit (the
nexus-9ssih dangling-endpoint 400 is the canonical case — its first landing
was REMOVED by 6714e70e to wait for the client half). This script refuses
(exit 1) until every client commit the tag requires is an ancestor of the
latest RELEASED `v*` tag. Register new preconditions in the script's
`ENGINE_CLIENT_PRECONDITIONS` (in `check_engine_release_floor.py`) whenever an engine change ships a wire behavior
old clients mishandle. Prose deploy-gates get skipped; this one does not.

This is also where the mechanized both-halves wire-contract ledger
(`docs/wire-contract-pending.md`, `scripts/check_wire_contract_pairing.py`)
gets consulted on the **unpaired** deploy path (protocol-audit [22511] Gap 1,
2026-08-14) — an ordinary "refresh the cloud engine" run with no paired
client release in flight. A non-empty `## Unshipped` section blocks (exit 1)
regardless of the tag unless every entry carries the `[additive]` token.
There is no acknowledgment flag: ship the client half, or mark the entry
additive when it truly is.

**A red exit blocks the DEPLOY, never the tag cut** (Hal directive
2026-08-02; the pre-tag wiring of this check is what forced conexus 7.1.0 to
ship pinned to a pre-fence engine — its own flagship feature inert on fresh
local installs). A tag gates DELIVERY, not work, and not even publication:
cut the engine tag whenever the tree is green. On a red exit here, the tag
still cuts; the DEPLOY waits for the client tag that carries the listed
commits. The paired-release choreography (`docs/agents/engine-service-release.md`)
closes the gap: the client release bumps the floor to this tag IN
the same release, and the deploy fires at client-tag push, in parallel with
the PyPI publish — satisfied the instant the client tag exists, live before
any user can install the client that requires it.

### 3e. PRE-TAG check: new `/version` fields need a PAIRED conexus edge-allowlist change (nexus-04sff)

The public edge (`api.conexus-nexus.com`) does NOT pass the engine's
`/version` body through verbatim — it trims it to a deliberate, reviewed
ALLOWLIST (conexus-24c4: `schema_*` fields fingerprint the DB journal and
`schema_error` can carry raw exception text, so verbatim pass-through was
rejected by design and stays rejected). Any NEW field the engine adds to
`/version` is therefore dead at the edge until conexus adds it to that
allowlist — exactly how `nx_answer_steps_supported` (engine-service-v0.1.85)
shipped engine-green through every gate above and then read as absent to
every cloud client (cloud-client-path-gate leg A FAILED 2026-08-21,
nexus-04sff / conexus-f6w7; same class as nexus-bwulw).

Before pushing the tag: `git diff <last-engine-tag>..HEAD --
service/src/main/java/dev/nexus/service/http/VersionHandler.java
service/src/main/java/dev/nexus/service/http/EmbedPosture.java`. The body is
built in both: `EmbedPosture.appendFields` adds the embedding and reranker fields,
and a diff of `VersionHandler.java` alone missed `reranker_available` at the
v0.1.162 cut (conexus caught it at image-smoke). For every
field ADDED, surface a REQUEST relay to conexus naming the field and the
engine tag ("add `<field>` to the edge /version allowlist, mirror-don't-invent:
absent on older engines stays absent") and record the conexus bead id on the
nexus bead before the tag — the edge half must be live before Step 6.1 can
pass, and Step 6.1 is what gates the paired client release.

**Request headers too (2026-11-19).** The edge also filters what reaches the engine: CloudFront's origin request
policy forwards an allowlist of request headers, and a header not on it is dropped silently. RDR-253's
`X-Nexus-Identity-Token` shipped engine-green in 0.1.196 and client 8.5.0 and was never forwarded, so every fenced
cloud write arrived with no token and was refused `403 identity_token_required` (a dropped header reads as "no token",
never as an error at the edge). Before the tag, list the request headers the cut adds on either side:

```bash
git diff <last-engine-tag>..HEAD -U0 -- service/src/main/java | grep '^+' | grep -oE '"X-[A-Za-z0-9-]+"' | sort -u
git diff <last-client-tag>..HEAD -U0 -- src/nexus conexus | grep '^+' | grep -oE '"X-[A-Za-z0-9-]+"' | sort -u
```

For each name that the engine reads from a request (`getRequestHeaders()`) or the client sends, and that the edge does
not already forward (today, by name: `Authorization`, `X-Nexus-Tenant`, `X-Nexus-Client-Version`,
`X-Nexus-Request-Deadline-Ms`, `X-Correlation-ID`, `X-Forwarded-For`, and `X-Nexus-Identity-Token` once bob's
2026-11-19 change is live; confirm against the policy, not this list), raise the paired edge change with bob (a cloud
change: Sam's go) and record it on the cut bead before the tag. Response headers (`X-Nexus-Export-*`,
`X-Nexus-Usage-Tokens`, `X-Nexus-Skipped-Collections`, `X-Nexus-Deadline-Outcome`, `X-Nexus-Unattested`) go the other
way and are governed by the edge's response policy: check a new one there the same way. Step 6.1's client-visibility
gate does not catch a dropped request header unless a leg makes a fenced write, so this check is the gate.

### 3f. PRE-CUT check: the Windows switch, when the cut must carry Windows (RDR-224, nexus-f9bgu.28)

A tag cut with the repo variable `NX_WINDOWS_RELEASE_LEGS` off publishes the 21
assets of the three native platforms, becomes immutable, and leaves a Windows
client nothing to install from it; the only recovery is another cut. So before the
tag, say out loud which kind of cut this is and check the variable matches:

```bash
gh variable get NX_WINDOWS_RELEASE_LEGS      # 'on' for a cut that must carry Windows (36 assets); anything else = 30
```

A cut meant to carry Windows (the paired release's P5.0a cut, nexus-f9bgu.42) needs
`on`. A mismatch with intent stops the cut and goes to Sam: the variable and the
runner are his, agents flip neither (`docs/agents/self-hosted-runners.md`).
Two more things must hold, and neither can be read from the variable:

- The `win-release` runner is online. With the switch on and the runner offline,
  EVERY engine tag, Linux and mac included, stays a draft until the queued Windows
  jobs time out (see Step 4).
- A `workflow_dispatch` run of `engine-service-release.yml` on `develop` with the
  switch on was GREEN on a commit whose Windows surface (the Windows jobs of that
  workflow and of `pg-bundle-cache-seed.yml`, `scripts/*windows*`,
  `scripts/windows_*.py`, `.github/actions/*windows*`, `service/pom.xml`) equals the
  commit being tagged. Start it when the cut starts (Step 2b), on `develop` while its
  Windows surface still equals `TAG_SHA`'s
  (`git diff --stat "$TAG_SHA" origin/develop -- <the Windows surface above>` is empty);
  on develop the run also builds the POSIX native jobs. Do not dispatch on an
  `engine-cut/*` branch pushed at `TAG_SHA`: the `apple-signing` environment's protection
  rules refuse that branch, so the linux-amd64, linux-arm64 and mac-arm64 native jobs fail
  in seconds with no steps run and only the Windows jobs' result can be read (0.1.197,
  run 44528101260). A non-tag ref runs every Windows step except sign, upload and
  promote, so this is the only run that exercises the release workflow's own Windows
  jobs before the tag (the rehearsal cannot: it is not allowed to trigger release
  jobs). Read the run's conclusion once:
  `gh run list --workflow=engine-service-release.yml --event=workflow_dispatch --branch=develop --limit 5 --json conclusion,headSha,createdAt`.
  Record the run id in the cut's bead.

After the cut publishes (Step 4), confirm it carries them:

```bash
uv run python scripts/check_engine_release_floor.py --require-windows engine-service-vX.Y.Z   # exit 0 = published, all 36 assets (27 for a tag cut before the POSIX engine .txz assets)
```

### 3g. PRE-TAG check: no held changeset in the tagged tree (nexus-3wh8d.28)

```bash
TAG_TREE=/path/to/worktree-at-the-commit-you-will-tag
git -C "$TAG_TREE" status --short   # clean
uv run python scripts/check_release_holds.py --root "$TAG_TREE"   # exit 0 = RELEASE_HOLDS_CLEAR
```

`scripts/release-holds.txt` lists changesets that have landed on develop ahead of their release gates; a tag from develop would ship them to the cloud and, through the pin, to every local install. Exit 1 names each held changeset and its bead: do not tag. Either cut from a commit without the changeset, or release it by deleting its line in a reviewed commit once its bead says it may ship (for `vectors-030-1`: RDR-225's .19 closed, the PITR-fork rehearsal recorded, and Sam's go). The release workflow's `create-release` job runs the same check on the tagged tree and refuses to create the release, so a tag pushed past this step builds nothing.

### 3h. PRE-TAG check: an upgrade does not rewrite user data at boot (nexus-f3yqd.1)

Sam's release criterion of 2026-11-10 (halcyon): an upgrade's first engine boot
writes at most 64 MiB over its migrate and ready phases; on the normal upgrade, a
plain same-version reboot writes at most 64 MiB over the same phases; and an
engine restart during a background index build never throws the build away. Run
the three legs of `scripts/upgrade_boot_writes.py` from INSIDE a clean worktree
at the exact commit you will tag (`TAG_TREE`, frozen in Step 1b), all three legs
started together (Step 2b). Each leg builds the
gate jar in that tree and the harness boots it (`--new-jar` left at its default,
the service jar the same tree just built), so the jar under test is
the tag commit's. Run each leg on a Linux pool host, chas or qwent, named
explicitly: the harness reads `/proc/<pid>/stat` and `/proc/<pid>/cmdline`, which
macOS lacks, so `lab-remote --host auto` (which can place lab-m on hellmini) is not
a route for it. Both Linux hosts have a real disk under the run (not tmpfs). The legs
build the gate jar, so they go over ssh in the host's own clone, as engine builds do
(`build-gate-jar.sh --lab-run` takes the build lease before the grant), never through
`lab-remote`: inside a lab-run grant `build-gate-jar.sh` refuses at once with 75 when
another build holds the lease (nexus-dfg0q), which on a busy host fails the leg before
it starts. Recipe: T2 `nexus/chas-test-host-howto` (qwent: the same with `qwent-test`).

```bash
cd "$TAG_TREE"   # clean, at the commit you will tag
PREV=8.1.1       # the newest released conexus client (it pins the published engine); re-check at cut time
TAG=$(git rev-parse HEAD)
H=chas           # the laptop remote; its ssh host is $H-test (chas-test, or qwent and qwent-test); legs may split across both
git push -q "$H" "$TAG:refs/heads/engine-cut-$TAG"
ssh "$H-test" "cd ~/src/nexus && for L in A B C; do git worktree add -q --detach ../nexus-wt/engine-cut-$TAG-\$L $TAG; done"
# Leg A: the normal upgrade, then a plain reboot
ssh "$H-test" 'cd ~/src/nexus-wt/engine-cut-'"$TAG"'-A && uv sync -q && NX_BUILD_LEASE_WAIT=28800 scripts/build-gate-jar.sh --lab-run lab-m && mkdir -p output/gate && \
  lab-run -c lab-m -- uv run python scripts/upgrade_boot_writes.py --prev-client '"$PREV"' --new-jar-commit '"$TAG"' \
  --ceiling-boot 67108864 --ceiling-reboot 67108864 --json output/gate/A.json'
# Leg B: from before per-collection indexes, queued aspect rows, graceful restart mid-queue
ssh "$H-test" 'cd ~/src/nexus-wt/engine-cut-'"$TAG"'-B && uv sync -q && NX_BUILD_LEASE_WAIT=28800 scripts/build-gate-jar.sh --lab-run lab-m && mkdir -p output/gate && \
  lab-run -c lab-m -- uv run python scripts/upgrade_boot_writes.py --prev-client 7.76.0 --new-jar-commit '"$TAG"' --no-pci-wait \
  --collections 7 --rows 30000 --rot pending_aspect_queue \
  --restart-mid-queue 3 --restart-mode graceful --ceiling-boot 67108864 --json output/gate/B.json'
# Leg C: as Leg B, with a forced kill of the supervisor and engine (PostgreSQL stays up)
ssh "$H-test" 'cd ~/src/nexus-wt/engine-cut-'"$TAG"'-C && uv sync -q && NX_BUILD_LEASE_WAIT=28800 scripts/build-gate-jar.sh --lab-run lab-m && mkdir -p output/gate && \
  lab-run -c lab-m -- uv run python scripts/upgrade_boot_writes.py --prev-client 7.76.0 --new-jar-commit '"$TAG"' --no-pci-wait \
  --collections 7 --rows 30000 --rot pending_aspect_queue \
  --restart-mid-queue 3 --restart-mode kill --ceiling-boot 67108864 --json output/gate/C.json'
```

Start each leg as a background command (`run_in_background`) with its output
redirected to a file: the ssh session holds until the leg exits, so the session is woken
on exit with no polling. Then fetch its report: `scp "$H-test:src/nexus-wt/engine-cut-$TAG-A/output/gate/A.json" output/gate/`
(and `-B`, `-C`). To run the three legs together (Step 2b), each has its own worktree
on the host, so their work dirs do not collide. Legs on one host share
that host's build lease for `scripts/build-gate-jar.sh` (the wait setting keeps them
queued, not refused), so their jar builds take turns; the legs themselves run
together. `output/` is git-ignored, so the fetched reports leave the tag tree clean.
Remove the host worktrees and the pushed ref after the cut (`git worktree remove`
and `git branch -D` on the host). The jar must come from
`scripts/build-gate-jar.sh`, which stamps the build commit the engine serves on
`/version`. The script reuses a cached jar while `service/` is unchanged, so the
stamp can name an older commit with the same engine tree: `--new-jar-commit`
accepts that and records how it matched in `new_jar_identity`, and exits 2 when
the booted engine's `service/` tree differs from the tag's, or it carries no
stamp (a plain `mvnw package`).

- **Leg A** is the normal upgrade, from the release users are on now, then a
  plain same-version reboot. `--ceiling-boot` bounds the upgrade boot's migrate
  and ready, and `--ceiling-reboot` the reboot's migrate and ready. The reboot
  follows an upgrade whose data moves have run to completion, so an engine that
  rewrites data on every boot fails here even if its first boot passes. The
  earlier figure of 0.45-0.59 MB was measured before any stamp move completed
  and is no reference for it: the first leg A run with the completed stamps sets
  one. A vacuum of the moves' dead tuples that starts after idle can land in the
  reboot's migrate and ready; read `boots[1].phases` before trusting a red or a green.
- **Leg B** starts from 7.76.0, the newest release before per-collection
  indexes (`--no-pci-wait`: the old engine builds none), with rows left in the
  retired aspect queue, and restarts the engine while the third index build
  runs. `--ceiling-boot` bounds the UPGRADE boot only, over migrate and ready
  (measured 2.95 MB). The index builds after ready are expected work and carry
  no byte bound on this leg. Later boots are reported in `boots[]`, not gated:
  on an engine that keeps builds across restarts, the kept build finishes while
  the next boot migrates, so that boot's migrate phase can carry about 100 MB of
  the build's WAL. That is the fix working. What the later boots must get
  right is the restart rule: the build in flight survives (same backend pid), is
  adopted (`pci_anchor_adopted`), is never rebuilt, and no index is built twice.
- **Leg C** is Leg B with a forced kill, under the same upgrade-boot bound and the
  restart rule.

Data moves (Sam, T2 `nexus/sam-ruling-upgrade-gate-move-pacing-2026-11-23`). The
harness sets `NX_DATA_MOVE_SLEEP_MS` for every boot of the new engine (`--data-move-sleep-ms`,
default 0, recorded as `data_move_sleep_ms`), so the registered moves, the RDR-229 stamps
included, run to completion inside `--max-wait` instead of at production pacing (about 4.8 h
for the fixture's chunks stamp). A green leg proves the moves complete and the engine
settles at that pause, and nothing about production pacing, which rests on the
`DataMoveWorker*IntegrationTest` suites; the harness prints the same note in its output
(`pacing_note`). Every leg's byte bound covers migrate and ready (`--ceiling-boot`) and the
reboot (`--ceiling-reboot`) and nothing after ready. `--ceiling`, the old total through
settle, is retired: the harness refuses it, so never put it on a leg. Reported, never gated:
`pre_move_wal_bytes` (ready to the first status that shows a move working) and
`move_wal_bytes` (from that status until idle: the moves, plus whatever else wrote in that
window, an autovacuum included if it started before idle; about 2.1 GB for the chunks
stamp). Once ready, the upgrade boot sizes the open moves: ceil(rows / chunk_size) times
the pause, summed, against the time left before `--max-wait`. It is a lower bound on the
pauses alone (no compute time; rows counted for the whole table, not per tenant leaf;
moves parked on a missing prerequisite left out), so it can stop a leg that cannot finish
(exit 2) and never prove one will. An unreadable registry or count also exits 2. A move
still working when `--max-wait` runs out is red (exit 1, `data-move-never-finished`).

Every leg must exit 0. Exit 1 is a red (a bound tripped, a boot failed, or the
restart rule failed): do not tag. Exit 2 is a harness failure with no measurement: fix the
run and repeat it, never tag on it. Paste each fetched `output/gate/*.json`'s
`verdict`, `boot_wal_bytes`, `reboot_wal_bytes` (Leg A), `total_wal_bytes`, `pre_move_wal_bytes`,
`move_wal_bytes`, `data_move_sleep_ms`, `pacing_note`,
`ceilings_tripped`, `new_jar_sha256`, `new_jar_commit`, `new_jar_identity` and the
`resume` block on the cut bead, and check that `new_jar_identity` shows the jar's
engine tree equals the tag commit's. Not covered by this step: background work
that starts after idle (a later sweep or periodic job), and non-index background
jobs (their own gate, nexus-f3yqd.2). Reference results: engine-service-v0.1.186
is red on Leg B (`pci_builders_terminated reason=schema_changesets_pending`,
then a rebuild); the 0.1.187 candidate (0c6f9e1c5) is green on Legs B and C. A
red control in Leg A's shape from 7.72.1 (engine 0.1.149, before the vectors-030
walk) exits 1 with `boot-over-ceiling`: 846,283,216 bytes over migrate, 12.6
times the bound (marginalia, on nexus-f3yqd.1).

### 3i. PRE-TAG: cloud leg on the client candidate (BLOCKING; the cloud test tenant only)

Say it plainly: a pre-tag run tests the candidate CLIENT against the OLD cloud engine, because the tag is what arms the
deploy. Anything this cut itself fixes (self-seat in 0.1.197, a new route, a new header the edge must pass) can only
read red here. Run it inside an announced cloud window, against the cloud test tenant only (release skill Step 6f has
the full text):

```bash
uv build --wheel -o dist-candidate/        # a candidate wheel of the tree you will tag, when only the engine is cut
scripts/build-gate-jar.sh
tests/e2e/cloud-leg.sh --wheel dist-candidate/conexus-*.whl --window <id> \
    --expect-red identity-seat-create@403:"cloud self-seat ships in 0.1.197" \
    --expect-red identity-seat-show@404:"the seat was never created"
```

The cut's new routes answer 404 at the edge until the deploy, so the leg reads the engine release the cloud runs
(`GET /version`) and skips each ledger route whose engine tag is later ("SKIPPED, engine tag ... not deployed",
nexus-h4hge), so `--core-only` is no longer needed here. An unmapped route still fails, and a run with any such skip
ends `CLOUD LEG PASSED (NOT A RELEASE PASS: ...)`: it is not the gating run.

Each `--expect-red <step>@<status>:<reason>` pins the cloud-only status the step may show (a comma
list such as `@401,403` pins several): that step is reported EXPECTED-RED with its status and reason and does not fail the
run. Any other failure on it (another status, a blind tap, no HTTP, a route never called, a round-trip miss) stays FAIL, and a
PASS on it fails the run ("expected red but passed": remove the flag). `nx identity show` of a missing seat answers 404 through
the client (`GET /v1/identity/alias/<name>`), which is why the show step pins 404 when the create step is red. After Step 6's
deploy relay confirms the tag is live, rerun it without those flags and with `--require-deployed` (`--release vX.Y.Z`
for the client's own ledger entries), which fails on any route the cloud still lacks: that rerun is the real test, and it
gates the client tag push. Paste both reports, from the
`examined` line to the verdict line, onto the cut bead.

### 4. Push the tag (Sam, or the AI on Sam's go for the release)

The releaser is **Sam**. His go for a named release ("7.95.0 let's get that out
the door", 2026-11-04; "tag and push engine-service-vX.Y.Z", 2026-10-27) lets the
AI push every tag that release needs, its engine tag and its client tag, without
asking again (nexus-e74o6: asking again stalled 7.95.0 for half a day). The AI
pushes each tag only once every gate named before its push is green, and records
Sam's words, the session and time he said them, and the gate evidence on the cut
bead. A session driving the release for the one Sam told holds the same go, quoted
the same way. The go covers the release it names, not the next one; an instruction
that names no release is not a go.

**A shipped defect found during cut prep re-cuts.** The bar is "already shipped", not "this cut introduces it". After v0.1.110 was tagged (2026-09-09), deploy prep surfaced two defects already live in v0.1.109; recommending to ship around them as "not regressions this cut introduces" was wrong (Sam: "introduces is not the bar ... it is ALREADY SHIPPED"). Hold the tag, or the deploy if the tag is out, fix both on develop, and cut a successor. Source: memory `feedback_release_and_develop_ci` § engine_cut_discipline.

```bash
git tag -a engine-service-vX.Y.Z -m "engine-service X.Y.Z" <commit>   # <commit> must be on origin
git push origin engine-service-vX.Y.Z
```

Tag-push fires `engine-service-release.yml` → builds + cosign-signs the 3 native binaries for the supported targets (`linux-amd64`, `linux-arm64`, `mac-arm64`), each also as `nexus-service-<arch>.txz` (the asset `nx init` downloads), plus their PG bundles, and publishes the GitHub release. The release is created as a DRAFT and promoted by the final `promote-release` job only after both matrices succeed and `scripts/promote_engine_release.sh` finds all 30 assets (nexus-cl14i; 36 with the Windows legs on); until then no consumer can resolve the tag, `check_engine_release_floor.py` reads it as unpublished, and a failed leg on any platform (mac-arm64 is the slowest) holds the whole release as a draft: rerun the failed jobs and promote runs again. (Intel macOS / `mac-amd64` is NOT a supported target — not built.) Publishes nothing to PyPI. Wait for the workflow to finish publishing before Step 5 (prior runs about 35 to 65 min (v0.1.118 took 36, a single measurement)). A run that sits at `waiting` is a deployment-environment approval, a person's click, never runner backlog (v0.1.59 sat for hours, 2026-07-31): find the pending environment and tell Sam (`.claude/rules/ci-and-runners.md` § GitHub Actions traps).

**Engine-bundle canary (after the tag publishes).** `engine-service-release.yml`'s `bundle-canary` job calls `engine-bundle-canary.yml` once `promote-release` has published the tag (the workflow also fires on a hand-published draft, and on `workflow_dispatch`). It measures `ddl: migration=N` on the pinned bundle and on the new tag's bundle, with develop's tests and gate jar, and lists each bundle's PostgreSQL and pgvector version in the job summary. Read the result before the paired client release's PR is cut. A call is a job of the tag's engine release run, not a run of the canary workflow: `gh run list --workflow=engine-service-release.yml --branch=<tag> --limit 1`, then `gh run view <id> --json jobs` for the `bundle-canary` job and its summary and annotations (or the tag sha's check-runs); `gh run list --workflow=engine-bundle-canary.yml` shows only a hand dispatch. One read. Green with no warning: nothing to do. **A warning** ("bundle changes migration DDL from P to T") is the expected outcome when the bundle moved: the client release that bumps `REQUIRED_ENGINE_VERSION` to this tag sets `tests/ddl_ceiling.toml` `migration_ddl_per_worker` to T in the same commit, with the cause (the PostgreSQL or pgvector version) as the reason. **Red** means the pinned bundle's count is above the ceiling (develop drifted), an engine-backed test is red on a bundle, or nothing was measured. A red `bundle-canary` job after the release is published is a finding about develop or the bundle; the publish stands. A skipped run (no `ddl: migration=` line) is a failure, not a pass. Dispatch it by hand (`gh workflow run engine-bundle-canary.yml --ref develop -f tag=<tag>`) for a tag whose `bundle-canary` job did not run, never while it is running. Full text: `docs/agents/engine-service-release.md`, "Engine-bundle canary".

**With `NX_WINDOWS_RELEASE_LEGS` on and the `win-release` runner offline, every engine tag stays a draft until the Windows jobs time out** (nexus-f9bgu.28, code review m8). `promote-release` requires both Windows jobs to have succeeded while the switch is on (P0.4), the jobs queue with nothing to run them, and GitHub cancels a queued job after 24 hours; Linux and mac assets are attached all the while, so the release looks broken and is not. A hotfix cut during that window waits too. Recovery: Sam brings the runner back and runs `gh run rerun <run-id> --failed` (the Windows jobs run, `promote-release` follows). If the cut must ship WITHOUT Windows, Sam sets the variable to off first and then reruns the failed jobs: `vars` is read per job when it is scheduled, so `promote-release` then expects the 30 assets. Flipping the variable mid-run can make the jobs disagree with each other; every disagreement ends in a draft, never in a wrong publish.

**A burned tag means the local battery has a shape gap** (same rule as the `release` skill's Step 10). Do not move the tag yourself (`docs/contributing.md` § Tag-retraction policy: an admin decision); cut the next version, and before that reproduce the failed job's exact commands locally and fix everything that shape surfaces, not only the visible line. v0.1.101, v0.1.102 and v0.1.103 burned in one afternoon on three build-only defects of one class (a smoke run from a wheel where a guard is inert, a native build with Docker present); each cut fixed the visible line and hit the next step of the same job. Then add the shape as a rehearsal leg (nexus-xihsm) instead of filing it.

### 5. POST-PUBLISH gate: `--acquire` (the leg that drives the PUBLISHED bytes)

Wait for `engine-service-release.yml` to finish publishing, then:

```bash
NEXUS_SERVICE_TAG=engine-service-vX.Y.Z tests/e2e/migration-rehearsal/run.sh --acquire
```

Must end `ACQUIRE GATE PASSED`. Standalone — do not combine with other legs. The
tag is REQUIRED and never defaulted (the point is a specific published artifact);
`run.sh` exits 2 without it.

What it does on a bare box: quarantine asserts (no `nx` binary pre-staged, no
system PostgreSQL) -> `nx daemon service install-binary <tag>` cold-acquires the
native binary + PG bundle, cosign-verified -> `init --service` -> `/version`
asserts `release_version` equals the acquired tag -> store / index / search drive
it -> `doctor` with no ✗.

**Why no local gate covers this.** The published artifact is different bytes from a
different builder than any locally built candidate: full native build (not
quick-build), codesign, cosign, PG-bundle packaging. A defect introduced by the
release workflow is invisible to a local gate BY CONSTRUCTION — `nexus-2oh5q` is exactly that hazard (signing breaking JNI dlopen
of the bundled onnxruntime/DJL), dormant only while the repository variable
`APPLE_SIGNING_REQUIRED` is not `true`. Historically this gate caught `nexus-pi3s3` + `nexus-qeoxf`
(2026-06-26), defects every local suite missed.

**Scope limit, carried from `nexus-1ddsy`'s close:** the container is Linux, so
this exercises the linux artifact. The mac-arm64 post-signing path is NOT covered
here — tracked on `nexus-2oh5q`.

**The mac-arm64 gap has a gate — it is just MANUAL, and has nothing to check until signing is switched on.** The
provisioning half (six Apple credentials, both portals, the pre-flight and the
renewal failure modes) is
[`docs/operations/apple-code-signing.md`](../../../docs/operations/apple-code-signing.md).
Signing is opt-in: a tag signs only when the repository variable
`APPLE_SIGNING_REQUIRED` is `true`, whatever secrets exist (nexus-e8iml). After
the first tag cut with it set, run on an arm64 Mac (on failure, delete the
variable and cut a fresh tag):

```bash
NEXUS_SERVICE_TAG=engine-service-vX.Y.Z tests/e2e/mac-signed-binary-gate.sh
```

Must end `MAC SIGNED-BINARY GATE PASSED`. It downloads the published mac-arm64
artifact, applies the quarantine xattr a browser download would set (the API
path `install-binary` uses sets none, which is why this hazard has never bitten
anyone), asserts Developer-ID signature + Hardened Runtime + the
disable-library-validation entitlement + `spctl` acceptance, then boots the
SIGNED binary through `native-smoke.sh` and asserts the bge-768 embed actually
executed — the DJL tokenizer JNI + onnxruntime `System.load()`s are precisely
what Library Validation refuses. A skipped embed is a FAILURE there, not a pass.

Why it cannot be a CI job: mac-arm64 is `smoke: false` (macos-14 runners have no
Docker) and codesign runs AFTER the linux-only smoke, so CI never boots the
signed mac bytes at all. `codesign --verify` cannot see a runtime dlopen refusal.

`--package-upgrade` is NOT a substitute (checked; do not re-derive): it converges
to `NEW_ENGINE_TAG`, which `run.sh` derives from `REQUIRED_ENGINE_VERSION` — the
release's engine identity, not an arbitrary tag — so it can only validate a tag a PyPI release has
already pinned, strictly after the moment this gate protects. (A tree with the pin bumped by hand
does reach the tag's published binary through `nx upgrade`, which is how Step 5b.1 rehearses a
migration; that is an upgrade of a populated install, not this gate's cold acquire on a bare box.)

History: this step said "CURRENTLY NO LEG / escalate to Hal" for one cut after
RDR-155 P4b retired `run.sh --cold` (whose TAIL drove the deleted
`nx guided-upgrade`; its acquire half never did). `nexus-1ddsy` rebuilt the
acquire half as `--acquire` and it gated `engine-service-v0.1.55` in production
(11 PASS / 0 FAIL). Hal REFUSED "accept the gap" on 2026-07-24 — do not
re-propose it. Instance of `nexus-1e2eh` (release-only procedures rot silently).

### 5b. Migration-release branch (CONDITIONAL — this tag carries schema or data DDL)

Trigger: `service/` since the last engine tag includes a new Liquibase changeset, or any change to a data shape existing rows already have to conform to (T2 [22511] gap 9). Skip this step entirely when the cut carries no such change.

1. **Representative-scale rehearsal: the upgrade rehearsal from the user's seat** (the `release` skill's Step 6e; audit cut 7, Sam 2026-11-10 by dialog, nexus-jh1f2: it replaces the `--package-upgrade` leg here, whose 10-30 document seed was never representative). Run it from a tree whose `REQUIRED_ENGINE_VERSION` names THIS tag, so the candidate's `nx upgrade` installs the tag's published, signed binary: the paired client release's branch, or a scratch worktree off `develop` with two one-line edits, the pin bumped to this tag and `pyproject.toml`'s version bumped to the next unpublished patch (so the first start is the hop users make, from the newest release, and the gate reads a plain `PASSED`). Check the pin before you start: `grep -n '^REQUIRED_ENGINE_VERSION' src/nexus/engine_version.py` shows this tag's tuple. Never `--engine-jar`, and no `NEXUS_SERVICE_JAR` in the environment: either swaps in a jar built from a tree, not the published bytes, and marks the run `NOT THE RELEASE GATE`.
   ```bash
   lab-remote -c lab-m -- tests/e2e/upgrade-rehearsal.sh --expect-engine X.Y.Z > /tmp/upgrade-rehearsal-engine-vX.Y.Z.out 2>&1   # run_in_background
   ```
   It installs the previous releases from PyPI (the newest below the tree's version, the last 7.x and 7.76.0), seeds each store above the `DEFAULT_ROWS_FLOOR` of 50,000 rows (86,000; 140,000 from 7.76.0), and times `nx upgrade`, `nx doctor` and `nx search` against the Step 6e limits, failing on 10 s of silence, on a lost row (`nexus.chunks` counted before and after) or on a missed sentinel. `--expect-engine X.Y.Z` makes the driver refuse before any start when the candidate wheel pins another engine (the forgotten pin bump), and each start fails unless `nx doctor` reports the install converged on that engine; each `UPGRADE REHEARSAL START (from X)` line ends with the engine the install converged on, and every line must name this tag. The seeder fills chunks (and aspects from 7.76.0); when this tag's changeset touches a table the seeder leaves empty (a T2 store, the catalog, a queue), the pass says nothing about that table at scale: say so in the relay. Name the floor, the actual seed count, each start's `nx upgrade` time and WAL figure, and the engine on each start line in the deploy relay. For a local install, an upgrade ceiling this tag's changeset blows is a finding about the changeset: a data move belongs in a visible, resumable job, never in the engine's boot (Sam 2026-11-10); the managed deployment's migration window is items 2 and 3. RDR-191's cloud 385,484-row unify-chunks migration (T2 [22485]) remains the only at-scale proof this project has produced for a chunk-table DDL change — and it ran in PRODUCTION. If the rehearsal cannot be brought to a genuinely representative scale before the deploy relay fires, say so explicitly rather than letting a toy-scale pass stand in for one.

2. **Rollback decision point, settled before the relay fires.** Determine whether this migration can be rolled back after it commits on the managed deployment. Non-transactional DDL (`CREATE INDEX CONCURRENTLY`, any Liquibase changeset that cannot run inside a transaction) forfeits the free atomic rollback a transactional migration gets — `nexus-o8dil.22`'s own wording: "CIC, non-blocking, +11%, cannot run in a transaction, and therefore forfeits the free atomic rollback that the local path gets." When the answer is no, write **IRREVERSIBLE** in the deploy relay verbatim, and have the substitute in hand before the operator opens the window: a written rollback/abort runbook with exact statements and named abort criteria.

3. **Freeze-window derivation from measurement, not a guess.** Generalizing `nexus-o8dil.22`'s pre-flight (T2 [22420] / [22427] / [22485]): derive and state (a) copy-peak — the largest transient storage footprint the migration needs mid-flight; (b) WAL budget — retained WAL under the deployment's replication settings during the window, checked against `max_wal_size`; (c) disk floor — abort unless available disk clears (steady-state floor + copy-peak) with a stated margin, re-measured immediately before executing (corpus growth between planning and execution shrinks the margin). Name the resulting threshold and the abort condition in the operator runbook.

4. **Post-deploy data-integrity verification, beyond `/version`.** A version match proves the binary shipped, not that the data survived. Verify, per T2 [22485]'s pattern: exact row-count reconciliation pre vs. post (per dimension/table, not an aggregate), that `ANALYZE` actually fired, and that Step 6.1's client-visibility gate plus the deploy gate's parity/recall legs are green post-migration. [22485]'s own verdict is the bar to match: "ROW INVARIANT EXACT: 385,484 pre == post ... ANALYZE fired ... STEP-6 green (112/113 parity, recall 12/12 pools identical), cloud client-path gate PASSED all four legs."

Full rationale and evidence citations: `docs/contributing.md` § Schema/data-migration releases.

### 5c. The binaries are the fixed size (nexus-ujbz8): blocking at promotion, re-read after publish

The size ceilings are enforced BLOCKING in `scripts/promote_engine_release.sh`, the draft-to-published step of the release workflow's `promote-release` job: an oversized binary leaves the release a DRAFT (re-run the failed legs, or fix and re-cut), and no consumer ever resolves it. A check that ran only after the publish would find the problem on an immutable tag, where the only remedy is another cut. After the release publishes, read it a second time:

```bash
uv run python scripts/check_engine_cut_riders.py sizes engine-service-vX.Y.Z
```

Add `--windows on` when the Windows legs were on for the cut (repo variable `NX_WINDOWS_RELEASE_LEGS`): the Windows engine archive `nexus-service-windows-x64.txz` is then required and held to its own ceiling (55 MiB; measured 32.1 MiB for an `-Ob` build, the `-O2` release build not yet measured), as `promote_engine_release.sh` already did. Must end `PASSED`. It reads the release's asset sizes (`gh release view`) and fails when a binary is at or near its pre-fix size: v0.1.142 shipped `nexus-service-linux-amd64` at 231.8 MiB, `linux-arm64` at 227.0 and `mac-arm64` at 193.3; nexus-lhr6a measured the fixed build at 150 (amd64) and 154 (mac), and the ceilings are 175, 175 and 175. linux-arm64 was never measured: its expected size is about 147 (an estimate scaled from amd64) and its ceiling uses amd64's margin; replace the estimate with the first published arm64 size. A size back near the old values means the dedup did not take effect in the release build, which the size gate catches after the fact and the embedded-resources checker catches earlier: it now runs as a step on every native release leg (`scripts/check_native_embedded_resources.py --platform <arch>`, nexus-zz2w7), so a build with a doubled or foreign library fails the leg before publish. Step 2b proves the fix was in the tagged commit (ancestry, not content: a later revert leaves it an ancestor); the size gate proves it shipped. Close nexus-ujbz8 after a pass.

### 6. Relay deploy + post-deploy cloud validation to conexus

Deploy and cloud-validation are **conexus-side operations**. Send the relay to
the conexus instance DIRECTLY — the RDR-205 tuple mailbox (`tuple_out` to
`mailbox/<instance>`, delivered by the recipient's channel push or, failing
that, its drain hook at the next prompt) and cross-session `SendMessage` both
reach it; the older wording here ("the bus is passive, so surface an explicit relay to
Hal") predates both and read, on 2026-09-16, as "you cannot talk to conexus, hand
the relay to a human", which is false and cost a round trip. What has NOT changed
is the substance: never frame the cross-instance deploy as autonomous. A relay
carries the tag, the gate evidence and what changed; it never carries
authorization. conexus owns every production write, and its flip runs on Sam's go
typed in THEIR session — a go relayed through this instance is refused there, by
design, exactly as a ruling relayed from them is not acted on here (same rule,
both directions). Say the deploy is theirs; do not say it is happening:

> **Before the relay, when this tag carries a changeset: ask conexus to run the PITR-fork walk rehearsal.** conexus can restore a Crunchy fork of production to a point in time (~6 min, `deploy/RESTORE.md`) and replay the Liquibase walk against the real row set before it runs live. That is the pre-deploy gate for a schema-carrying tag, and it is the one this skill used to omit. It caught `v0.1.78`'s zero-grant `nexus_diag` regression. The walk is CUMULATIVE — it replays everything the target cluster is behind on — so confirm the cloud's live `release_version` from the engine and size the walk from THAT, not from how many changesets you added.
>
> **Cutover posture for the ownerless-write refusal (nexus-20onx).** Tell conexus the first deploy runs `NX_OWNERLESS_WRITE_MODE` unset or `log-only` (never `enforce`), that the flip is a Terraform parameter plus a same-tag redeploy, and where the soak is read; the full order of operations, including the restart step, is `docs/operations/ownerless-write-cutover.md`. Include the doc in the relay. Three more items for the same relay: (1) `deploy/engine/image-smoke.sh` (conexus repo) must boot the built image with the production `NX_OWNERLESS_WRITE_MODE` value, not its own default; that catches only an INVALID value (the engine refuses to boot), which is a smaller claim than "a mis-wired parameter fails before the push": a valid `enforce` on the first deploy boots fine and refuses every legacy write; (2) so the first deploy's relay checklist also carries an assertion run BEFORE the push, against the booted image or the staged parameter: `/v1/status` `ownerless_write_mode` must equal `log-only`. **This is a conexus-owned hold-the-push line, not a nexus gate, and nothing in this repo checks it (nexus-20onx round 4, deliberately).** Owner: conexus; step: image built and redeploy staged, before the paired client tag is pushed; evidence: the value they read, in their staging reply. The nexus-side backstop runs after the harm window, not before it: Step 6.1 leg B3 after the first deploy; (3) after the first deploy, `/v1/status` must report `ownerless_write_mode` = `log-only` again, and after the flip redeploy `enforce`: Step 6.1 asserts both (`NX_EXPECTED_OWNERLESS_WRITE_MODE`). The code default itself (unset is `log-only`) is pinned by the engine's own test, `OwnerlessWriteRefusalTest.anUnsetModeBootsLogOnly_andAnExplicitEnforceBootsEnforce`; no direct-binary battery leg repeats it (nexus-20onx comment: the launcher always sets the variable, so a leg that boots the binary with it absent needs its own PG and env wiring, for a property the unit test already pins).
>
> Also confirm with conexus before the window opens: (a) the per-release PRE-DEPLOY prerequisites table — some changesets need a Crunchy-superuser grant to EXIST before boot migration, and its absence is a loud failure on the live engine; (b) the per-release DATA EFFECTS table — anything the walk deletes is acknowledged in advance, never discovered mid-deploy; (c) the image is cosign-signed, since under `enable_image_verification=true` an unsigned image BRICKS BOOT; (d) the current image tag is captured FIRST as the rollback target, and the rollback floor is `nexus-service-0.1.84`.
>
> **The DATA EFFECTS table in (b) is produced mechanically, not written by hand** (nexus-f7dwp — before this, a destructive changeset's effect reached conexus only because someone typed it into the handoff, and tuples-003-2 / tuples-004-1 shipped in v0.1.118 that way). Run `uv run python scripts/list_data_effects.py <previous-engine-tag> <this-tag>` and paste its markdown table verbatim into the relay — it lists every changeset added in this range that modifies or removes existing rows, each carrying its `DATA EFFECT:` line and a CENSUS PREDICATE column (the exact matched SQL statement). For each row, ask conexus to turn that predicate into a `SELECT count(*) FROM ... WHERE ...` probe against the PITR fork BEFORE the walk. **Only when the changeset's own comment or a paired changeset documents a RAISE NOTICE'd count** (e.g. tuples-003-2, paired with tuples-003-1's logged count) compare the probe to that RAISE NOTICE count — the two must agree, or the row's disposition needs a second look before the window closes. Most data-effecting changesets carry no such count at all (22 of the 38 files nexus-f7dwp backfilled emit zero RAISE NOTICE — single-changeset ALTER COLUMN TYPE rewrites, backfills, and drops, tuples-004-1 itself included): for those, there is nothing to compare the probe against, so just confirm the probe's count is plausible against the DATA EFFECT prose's own stated scope (e.g. "every existing row", "the N rows measured at census time") before the walk runs. A non-zero exit from the script (a row shown `MISSING`) means a changeset in this range modifies rows with no disclosure at all — fix it (add the `DATA EFFECT:` line to the changeset's `<comment>`, checksum-neutral per `scripts/data_effect_lint.py`'s own docstring) before cutting the tag, not after. **After pasting the table into the relay, machine-check that it actually landed there rather than trusting the paste** (nexus-iu43o — before this, the paste itself was a prose step with nothing checking it happened): `uv run python scripts/list_data_effects.py <previous-engine-tag> <this-tag> --record-relay-attestation` writes `docs/data-effect-relay/<this-tag>.json`; a release battery's `--verify-relay-attestation` (same two refs) then refuses if that attestation is missing or stale, and passes as not-applicable when the range carries no data-effecting changesets at all — both halves nexus-side, since the relay's sender and its own record live in one repo.
>
> What conexus does NOT have is a staged/shadow deploy of the BINARY — one environment, and it is the live estate (conexus-vbti). State that narrowly. On 2026-08-27 this checklist's post-deploy-only gate list was read as "the cutover is unvalidated by construction" and reported to Hal; the binary half was right and the WALK half was wrong.

> relay: deploy `engine-service-vX.Y.Z` to `api.conexus-nexus.com` + re-run the cloud gate (recall + hybrid parity, xr7.8.9-style).

**THIS is where 3b's precondition check blocks.** Re-run `check_engine_release_floor.py --client-precondition <tag>` before surfacing the relay: a red exit means the deploy waits for the client tag carrying the listed commits. In the paired-release choreography that is not a long wait — the deploy relay fires at client-tag push, in parallel with the client's PyPI publish, so the precondition is satisfied the instant the client tag exists and the engine is live before any user can install the client that requires it.

**nexus-1emxn refinement — prefer deploying BEFORE the client tag when the ledger allows it.** When every wire-ledger `## Unshipped` entry carries the `[additive]` direction-safety token (old client + new engine safe), `check_engine_release_floor.py --client-precondition` accepts the unpaired deploy by name — surface the relay and get the engine LIVE ahead of the client tag, so the tag can never open a refusal window (the v7.23.0 window sat open 48+ minutes because "fires at tag push" was an unsent human relay). When any entry is not additive, the client release's Step 9 does not tag until conexus has the redeploy staged (image built, a named tag trigger) and has confirmed it back.

For cross-repo gate / deploy status, **read the authoritative bead + the conexus bus, not memory** — cross-repo state goes stale fast (2026-06-26: a `luxe6` condition had been cleared a week earlier than memory implied).

### 6.1. Post-deploy client-visibility gate (MANDATORY, run from a cloud-mode box)

Leg A asserts the four RDR-241 route flags (`catalog_docs_by_author_supported`, `catalog_docs_by_meta_supported`, `catalog_sensitive_locators_supported`, `catalog_docs_by_scheme_supported`) read true through the edge (nexus-uwggq.2.8), so from the cut that carries the routes this gate proves the conexus edge allowlist lists them; before that deploy and allowlist change, Leg A is red against production by design.

The flip from `log-only` to `enforce` is its own bead, nexus-z0o2p.40, and it FOLLOWS the cut: it needs the soak, a written disposition for every writer the log names, and Sam's confirmation (`docs/operations/ownerless-write-cutover.md`, Order of operations 3 to 4). So the `=enforce` form below is that bead's check after the flip redeploy; it is NOT a Step 6.1 or Step 7 precondition for this cut, and a cut's sign-off never waits on it. This cut's sign-off asserts `log-only`.

```bash
# a cut that carries the ownerless-write refusal also asserts the LIVE mode (nexus-20onx):
NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only tests/e2e/cloud-client-path-gate.sh   # after the FIRST deploy
NX_EXPECTED_OWNERLESS_WRITE_MODE=enforce  tests/e2e/cloud-client-path-gate.sh   # after the flip redeploy
tests/e2e/cloud-client-path-gate.sh                                             # ONLY an engine from before the refusal (no ownerless_write_mode in /v1/status)
```

The mode assertion (leg B3) reads `ownerless_write_mode` from `/v1/status` through the public edge. Nothing else in this repo reads the live mode, and conexus wires the knob, so without it a mis-wired parameter enforces on the first deploy and refuses every legacy write from the hosts before anyone looks. Unset, an engine that reports a mode FAILS the gate (a live mode nobody asserted), and so does an unreadable `/v1/status` body (a curl failure, an edge 401/403/502 or a WAF page: a body without `embedding_mode` is not a status body), whether or not the variable is set; an engine that reports none prints `NOT RUN [B3]` and the final sentinel then reads `... violations=0 (ownerless-write mode NOT asserted: B3 not run)`: that is a skipped check, not a passed one, so a cut that carries the refusal always sets the variable.

Run this AFTER Step 6's deploy relay confirms the tag is live, and BEFORE Step 7's downstream-ref bump or signing off any release that depends on this engine (T2 [22511] gap 7 — this gate existed only as one prose line in AGENTS.md, in no numbered step of this checklist, since it was born from the nexus-bwulw incident). The gates above prove the ENGINE works, direct; they do not prove the PUBLIC edge (`api.conexus-nexus.com`) exposes the same contracts — 2026-07-23 (nexus-bwulw): the edge stubbed `/version` and auth-gated `/health`, silently disabling voyage threshold gating and dimension-orphan tooling and blocking guided migrations to cloud, while three client features shipped green through every engine-direct gate above. This asserts the engine's pinned contracts (`/version` fields, the `ez5.1` `/health` contract, the client `embedding_mode` probe, the `/v1` read path) survive the public edge.

Two legs added by nexus-wbfpw.50 cover the RDR-192 surface through the same edge, both read-only against production (the script's older legs E and H do write probe rows; see its header). Leg J asserts the `reaper` object of `/v1/status`: present, `enabled`, `last_completed_pass_at` non-null and within three `interval_seconds` plus the `wall_clock_budget_seconds` the same object reports, `failed_passes_total` 0. An edge that strips the key makes `nx doctor`'s Engine reaper row read "not applicable", which looks healthy, so this is the only check that notices. It FAILS on an engine whose first pass has not happened (about a minute after boot): run Step 6.1 a few minutes after the deploy relay confirms the tag live, never inside the boot window. Leg K posts only refused, missing-route or unregistered-collection requests to `/v1/vectors/gc/*`, `/v1/vectors/reapable` and `/v1/vectors/manifest-less-census`, and asserts the engine's own JSON and the 404/400/422/200 the client branches on (the restore verb's exit 4 is a 404). It does NOT cover the success shapes of the four `gc/*` routes (each moves, restores or deletes) or the typed 503 `quarantine_restore_busy` (it needs a held sweep gate), so those stay with the conexus-side gate and the engine's own tests.

Client version to run it from: the working tree (`HEAD`) — this script has no separate published-client mode. This step's job is edge-contract visibility, not client-write compatibility.

### 6.2. Post-deploy cloud backup round trip (MANDATORY, in the deploy window)

Sam, 2026-11-15 (RDR-248 close, finding 4): "Add it to the engine release". After Step 6.1, still inside the
deploy window Sam gave for this cut, run one cloud round trip of `nx backup` against the newly live engine,
about 12 minutes end to end (the 2026-11-15 restore took 555 s for 233,107 rows). Nothing else tests the export
and import routes through the public edge: the first run found the edge dropping `X-Nexus-Part-Sha256`
(conexus PR #710), which every engine-direct gate had passed. The operator session (bob) runs it; provision
and teardown are production writes, so they run only on Sam's go for the window, typed in that session.

Preconditions: the edge `/version` reports `tenant_import_supported`, and CloudFront forwards every request
header the import routes send. A new import request header must be added to the forwarded list in conexus
`infra/terraform/modules/app-server/cloudfront.tf`, or the restore fails 400 at the edge.

1. Export the test tenant `gate-xr789` with the shipped client: `nx backup --to <dir>/fresh.nxtenant`, its
   mint-locked credential in the environment only, an isolated `NEXUS_CONFIG_DIR`.
2. Provision an empty throwaway tenant from the conexus repo, with a slug carrying the throwaway marker
   (`throwaway-<bead>-<yyyymmdd>`):
   `PYTHONPATH=deploy deploy/gate/with-db-tunnel.sh --writes --i-know-this-writes-production -- python3 -m gate.throwaway_tenant provision --slug <slug> --name "<tag> restore target"`.
   It prints the engine credential once: send stdout to a 0600 file, read the credential into a shell
   variable, remove it from the file, and pass it as `NX_SERVICE_TOKEN` with its own `NEXUS_CONFIG_DIR`. Leave
   `OPERATOR_TOKEN` unset, so no control-plane bearer is issued. Then `nx backup restore <dir>/fresh.nxtenant`.
3. With the same credential, export the restored tenant to `<dir>/restored.nxtenant`. Run `nx backup --check`
   on both files; a non-zero exit from either fails the step. Then
   `uv run python tests/e2e/backup_roundtrip_compare.py <dir>/fresh.nxtenant <dir>/restored.nxtenant > <dir>/compare.json`.
   Exit 0 is PASS. Exit 1 (FAIL) or 3 (REVIEW) holds the release's sign-off until it is read. Expected and not a
   finding: `catalog_meta` is not judged and the restored export is one row larger (the restore's
   `rdr248_restore` marker); `tenant_id` and reassigned keys differ.
4. Tear the tenant down, whatever the outcome: the same `gate.throwaway_tenant teardown --slug <slug>` command
   once as a dry run and once with `--confirm`. It passes on `residue_total` 0,
   `engine_leaves.leaves_remaining` 0 and an empty `engine_api.active_token_hashes`.
5. Read the cloud engine's log over the deploy window for the two page-limit events. The log is the CloudWatch
   log group `/conexus/dev/engine` in `us-east-1`; `S` and `E` are the window's start and end in epoch ms:
   ```bash
   aws logs filter-log-events --region us-east-1 --log-group-name /conexus/dev/engine --start-time $S --end-time $E \
     --filter-pattern '?"event=export_page_over_budget" ?"event=export_page_wall_deadline"' \
     --query 'events[].message' --output text | tr '\t' '\n' | grep -cE 'event=export_page_(over_budget|wall_deadline)'
   ```
   Positive control: the same window must show the round trip's own `event=export_session_opened` lines (the
   fresh export and the re-export): the same command with `--filter-pattern '"event=export_session_opened"'` and
   `grep -c event=export_session_opened`, so a
   zero means no page hit a limit, not a wrong log or window. Each hit names the tenant and the table. A hit on an
   id-keyed table (memory, plans, aspects and the other tables keyed by `id` alone) is the trigger for Sam's
   `(tenant_id, id)` index decision (RDR-248 Risks; Sam, 2026-11-15: "Accept, watch the 20 s line"). Record both
   counts, zero included. Baseline since engine 0.1.192: 0 page-limit hits against 4 export session events (bob).

Record in T2 `conexus/<tag>-backup-roundtrip`, and name it on the cut bead: the export, restore and re-export
wall times and row counts, the compare verdict and its per-table summary, the teardown residue, and the
page-limit event count. Keep the
files at 0600 until recorded, then delete them. The worked run is RDR-248 Activation Step 2 (nexus-csayy.10).

### 7. After conexus confirms deployed + cloud-gated green, bump downstream refs

(For the RDR-223 cut, "cloud-gated green" means Step 6.1 against `log-only`; the enforce flip, nexus-z0o2p.40, follows the cut and is not waited on here.)

- When the NEXT PyPI release bumps `REQUIRED_ENGINE_VERSION` to this tag, also rotate `run.sh`'s `NEXUS_PREV_RELEASE`/`NEXUS_PREV_ENGINE_TAG` defaults (the `--package-upgrade` convergence leg's starting point — must stay one release BEHIND the new dependency or its staleness guard fails loud; nexus-cfgo9). The `--package-upgrade` leg itself runs in the PyPI `release` skill's Step 1, not here — this skill only keeps its inputs fresh.
  **The unit is RELEASES, not engine tags — a SKIPPED engine tag does NOT rotate them** (2026-08-11). `PREV_ENGINE_TAG` is the engine the PREVIOUS RELEASE PINNED. An engine tag that is cut, published, and gated but never pinned by any release (v0.1.70: a defect was found after the cut, so 7.6.0 shipped v0.1.71) is a skipped version — rotating `PREV_ENGINE_TAG` onto it would point the rehearsal's "previous install" at a hop no user ever made. At the 7.6.0 bump the correct values stayed `7.5.0` / `engine-service-v0.1.69` while the newest published engine tag moved to v0.1.71. The staleness guard only fires when PREV collapses to EQUAL the floor; it does NOT catch "rotated onto a tag no release shipped", so check this by hand at every bump.
- `SchemaUpgradeRehearsalIntegrationTest.OLD_TAG` does NOT move: it stays pinned at `engine-service-v0.1.17`, kept by Sam's decision as a long real upgrade path for a dormant install (Sam 2026-10-17, nexus-dodjd; the nexus-7z6s7 rotation step is retired). The rationale lives on the constant.
- **`REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) MUST move to this tag** — unconditionally, not "only if the release needs the features". There is ONE engine identity per release: the engine it was built and gated with, on EVERY install path (Hal directive 2026-07-15, after the 14h GH #1402 incident). It is NOT a compatibility minimum. For local-mode installs this constant is the ONLY delivery vehicle — an engine tag that is cut, gated, and never pinned reaches nobody. `PINNED_SERVICE_TAG` is DERIVED from it, so the one edit moves both.
  Sequencing — PAIRED release (Hal directive 2026-08-02, supersedes "bump lands with the NEXT release AFTER deploy"): the bump rides the client release PAIRED with this engine's deploy — same release, not the next one (floor-lag ships a client whose pinned engine lacks the engine halves of its own features: the 7.1.0/v0.1.62 inversion). The deploy relay fires at client-tag push, parallel with the PyPI publish (Step 6), so the engine is live before any user can install the floor-bumped client — UNLESS every wire-ledger `## Unshipped` entry leads with `[additive]`, in which case deploy BEFORE the client tag instead (nexus-1emxn, Step 6's refinement: the preferred branch whenever the ledger allows it — no window can open at all). GH #1402's lesson stands as: never publish a floor-bumped client with NO deploy armed — the deploy fires at tag push (or already fired, on the additive branch), not "eventually". `scripts/check_engine_release_floor.py` fails the release if a gated tag was never pinned. The client-side `release` skill's Step 0 runs this gate with `--paired-deploy engine-service-vX.Y.Z` (nexus-k1c08) to distinguish the expected pre-deploy cloud-behind state from real drift — this skill only needs to ensure the tag it just cut is what that flag names.

## Relationship to the PyPI release

The conexus PyPI release (the `release` skill) PINS one engine tag and gates on its cloud-validation (its Step 0 engine-freshness gate). This skill is what produces + validates the tag that gate pins. Run this whenever the engine drifts; run `release` only when shipping the Python package.
