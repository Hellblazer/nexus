---
name: engine-release
description: Use when cutting or deploying the Java engine-service binary (engine-service-vX.Y.Z), refreshing the cloud engine, or validating develop's engine tip in the cloud. This is the SECOND release lifecycle — separate from, and not gated by, the conexus PyPI release (use the `release` skill for that). Authority: AGENTS.md § Engine-service release.
---

# Engine-service Release Checklist

The Java engine-service is a separate release artifact from the conexus PyPI package. Cutting it is lightweight, frequent, and **NOT gated by the luxe6 / RDR-155-P4a develop release boundary** — so the cloud engine can (and must) be kept current with develop's engine tip independently of develop being unreleasable. Conflating the two lifecycles is how the cloud engine silently drifts (2026-06-26: 22 `service/` commits / 4 days un-deployed, un-cloud-tested).

Follow in order. Releaser is **human**: AI preps + validates; the human pushes the tag.

## Steps

**Removed in cleanup step 11 (nexus-0r1uz), numbering left as it was:** Step 3
(the local-candidate `--shakeout` gate and the `--candidate-migration` populated-store
rehearsal) and Step 3c (the published-client write leg) went with the scripts and
harness modes they ran. Steps that remain: 1, 2, 3b, 3e, 4, 5, 5b, 5c, 6, 6.1, 7, 8.

### 1. Decide whether a cut is needed (drift check)

```bash
git tag -l "engine-service-v*" | sort -V | tail -1     # last engine tag
git log --oneline <last-engine-tag>..HEAD -- service/   # what engine changed since
```

Cut a fresh engine when `service/` has accumulated **cloud-relevant** work: pooler/RLS, pgvector, catalog conformance (RDR-168), aspect queue (RDR-163), batch endpoints, embedder. Don't let it pile up — a large unvalidated engine delta means any "cloud test" result is testing a stale binary, and any PyPI release pinning that tag ships behind.

### 2. Verify the engine is green on the exact commit you'll tag

The version is **tag-stamped** — there is NO manifest to bump (`release.properties` `release_version` is blank in source, stamped at native-build time from the tag; the Maven pom stays `1.0-SNAPSHOT`).

Confirm the full Java suite + native build passed on the exact `service/` tree:

```bash
# fast path: if service/ at HEAD is byte-identical to a green service-ci commit, that CI covers it
git diff --stat <green-service-ci-sha> HEAD -- service/    # empty = covered
# else run locally (needs Docker for Testcontainers pgvector + the bge ONNX model):
scripts/mvnw-leased.sh -q test
```

The Java CI (`service-ci.yml`) is a required check on `main`, but nothing gates a push to `develop` on it, and a develop run can be cancelled or time out — so verify it actually passed on this tree rather than assuming.

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
commits. The paired-release choreography (AGENTS.md § Engine-service
release) closes the gap: the client release bumps the floor to this tag IN
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
service/src/main/java/dev/nexus/service/http/VersionHandler.java`. For every
field ADDED, surface a REQUEST relay to conexus naming the field and the
engine tag ("add `<field>` to the edge /version allowlist, mirror-don't-invent:
absent on older engines stays absent") and record the conexus bead id on the
nexus bead before the tag — the edge half must be live before Step 6.1 can
pass, and Step 6.1 is what gates the paired client release.

### 4. Push the tag (human, or AI when explicitly authorized)

Releaser is **human, every time** (AI preps + validates; the human pushes the
tag). Not "human by default": that wording drifted in here and widened an
absolute rule into one with a self-assessed exception, since the AI is the
party deciding whether it heard an authorization. AGENTS.md § Engine-service
release says "The human pushes the tag" with no carve-out, and the user-level
CLAUDE.md § Releases says "Releaser is human, every time."

```bash
git tag -a engine-service-vX.Y.Z -m "engine-service X.Y.Z" <commit>   # <commit> must be on origin
git push origin engine-service-vX.Y.Z
```

Tag-push fires `engine-service-release.yml` → builds + cosign-signs the 3 native binaries for the supported targets (`linux-amd64`, `linux-arm64`, `mac-arm64`) plus their PG bundles, and publishes the GitHub release. The release is created as a DRAFT and promoted by the final `promote-release` job only after both matrices succeed and `scripts/promote_engine_release.sh` finds all 21 assets (nexus-cl14i); until then no consumer can resolve the tag, `check_engine_release_floor.py` reads it as unpublished, and a failed leg on any platform (mac-arm64 is the slowest) holds the whole release as a draft: rerun the failed jobs and promote runs again. (Intel macOS / `mac-amd64` is NOT a supported target — not built.) Publishes nothing to PyPI. Wait for the workflow to finish publishing before Step 5 (prior runs about 35 to 65 min (v0.1.118 took 36, a single measurement)).

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
already pinned, strictly after the moment this gate protects.

History: this step said "CURRENTLY NO LEG / escalate to Hal" for one cut after
RDR-155 P4b retired `run.sh --cold` (whose TAIL drove the deleted
`nx guided-upgrade`; its acquire half never did). `nexus-1ddsy` rebuilt the
acquire half as `--acquire` and it gated `engine-service-v0.1.55` in production
(11 PASS / 0 FAIL). Hal REFUSED "accept the gap" on 2026-07-24 — do not
re-propose it. Instance of `nexus-1e2eh` (release-only procedures rot silently).

### 5b. Migration-release branch (CONDITIONAL — this tag carries schema or data DDL)

Trigger: `service/` since the last engine tag includes a new Liquibase changeset, or any change to a data shape existing rows already have to conform to (T2 [22511] gap 9). Skip this step entirely when the cut carries no such change.

1. **Representative-scale rehearsal.** Run the populated-store rehearsal in PUBLISHED-bytes mode against a corpus seeded ABOVE a stated floor, not the harness's default toy seed (10-30 docs in `rehearse_package_upgrade.sh` / `rehearse_acquire.sh`):
   ```bash
   NEXUS_TARGET_RELEASE=<published-conexus-version> tests/e2e/migration-rehearsal/run.sh --package-upgrade
   ```
   Name the floor and the actual seed count used in the deploy relay. RDR-191's cloud 385,484-row unify-chunks migration (T2 [22485]) remains the only at-scale proof this project has produced for a chunk-table DDL change — and it ran in PRODUCTION. If the rehearsal cannot be brought to a genuinely representative scale before the deploy relay fires, say so explicitly rather than letting a toy-scale pass stand in for one.

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

### 7. After conexus confirms deployed + cloud-gated green, bump downstream refs

(For the RDR-223 cut, "cloud-gated green" means Step 6.1 against `log-only`; the enforce flip, nexus-z0o2p.40, follows the cut and is not waited on here.)

- When the NEXT PyPI release bumps `REQUIRED_ENGINE_VERSION` to this tag, also rotate `run.sh`'s `NEXUS_PREV_RELEASE`/`NEXUS_PREV_ENGINE_TAG` defaults (the `--package-upgrade` convergence leg's starting point — must stay one release BEHIND the new dependency or its staleness guard fails loud; nexus-cfgo9). The `--package-upgrade` leg itself runs in the PyPI `release` skill's Step 1, not here — this skill only keeps its inputs fresh.
  **The unit is RELEASES, not engine tags — a SKIPPED engine tag does NOT rotate them** (2026-08-11). `PREV_ENGINE_TAG` is the engine the PREVIOUS RELEASE PINNED. An engine tag that is cut, published, and gated but never pinned by any release (v0.1.70: a defect was found after the cut, so 7.6.0 shipped v0.1.71) is a skipped version — rotating `PREV_ENGINE_TAG` onto it would point the rehearsal's "previous install" at a hop no user ever made. At the 7.6.0 bump the correct values stayed `7.5.0` / `engine-service-v0.1.69` while the newest published engine tag moved to v0.1.71. The staleness guard only fires when PREV collapses to EQUAL the floor; it does NOT catch "rotated onto a tag no release shipped", so check this by hand at every bump.
- `SchemaUpgradeRehearsalIntegrationTest.OLD_TAG` (`service/src/test/java/dev/nexus/service/`) → the PREVIOUSLY-deployed tag (nexus-7z6s7 rotation policy: the old→HEAD rehearsal's "real aged box" realism rots as the fleet moves on; re-verify the two structural preconditions documented on the constant when bumping) OLD_TAG rotation re-points the data leg's seeding and its SEED-COVERAGE block together; the snapshot generator and the seed-coverage lint were deleted (cleanup step 10b).
- **`REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) MUST move to this tag** — unconditionally, not "only if the release needs the features". There is ONE engine identity per release: the engine it was built and gated with, on EVERY install path (Hal directive 2026-07-15, after the 14h GH #1402 incident). It is NOT a compatibility minimum. For local-mode installs this constant is the ONLY delivery vehicle — an engine tag that is cut, gated, and never pinned reaches nobody. `PINNED_SERVICE_TAG` is DERIVED from it, so the one edit moves both.
  Sequencing — PAIRED release (Hal directive 2026-08-02, supersedes "bump lands with the NEXT release AFTER deploy"): the bump rides the client release PAIRED with this engine's deploy — same release, not the next one (floor-lag ships a client whose pinned engine lacks the engine halves of its own features: the 7.1.0/v0.1.62 inversion). The deploy relay fires at client-tag push, parallel with the PyPI publish (Step 6), so the engine is live before any user can install the floor-bumped client — UNLESS every wire-ledger `## Unshipped` entry leads with `[additive]`, in which case deploy BEFORE the client tag instead (nexus-1emxn, Step 6's refinement: the preferred branch whenever the ledger allows it — no window can open at all). GH #1402's lesson stands as: never publish a floor-bumped client with NO deploy armed — the deploy fires at tag push (or already fired, on the additive branch), not "eventually". `scripts/check_engine_release_floor.py` fails the release if a gated tag was never pinned. The client-side `release` skill's Step 0 runs this gate with `--paired-deploy engine-service-vX.Y.Z` (nexus-k1c08) to distinguish the expected pre-deploy cloud-behind state from real drift — this skill only needs to ensure the tag it just cut is what that flag names.

## Relationship to the PyPI release

The conexus PyPI release (the `release` skill) PINS one engine tag and gates on its cloud-validation (its Step 0 engine-freshness gate). This skill is what produces + validates the tag that gate pins. Run this whenever the engine drifts; run `release` only when shipping the Python package.
