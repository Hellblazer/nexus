---
name: engine-release
description: Use when cutting or deploying the Java engine-service binary (engine-service-vX.Y.Z), refreshing the cloud engine, or validating develop's engine tip in the cloud. This is the SECOND release lifecycle — separate from, and not gated by, the conexus PyPI release (use the `release` skill for that). Authority: AGENTS.md § Engine-service release.
---

# Engine-service Release Checklist

The Java engine-service is a separate release artifact from the conexus PyPI package. Cutting it is lightweight, frequent, and **NOT gated by the luxe6 / RDR-155-P4a develop release boundary** — so the cloud engine can (and must) be kept current with develop's engine tip independently of develop being unreleasable. Conflating the two lifecycles is how the cloud engine silently drifts (2026-06-26: 22 `service/` commits / 4 days un-deployed, un-cloud-tested).

Follow in order. Releaser is **human**: AI preps + validates; the human pushes the tag.

## Steps

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

### 2b. PRE-TAG check: the rider commits are ancestors of the commit you will tag (nexus-ujbz8)

```bash
uv run python scripts/check_engine_cut_riders.py ancestry <tag-commit>
```

Must end `PASSED`. A fix that is on develop does not ride a tag placed on an older commit, and the release job has no check that would notice (the native-image size fix, nexus-lhr6a and nexus-vwfc0, ships binaries at the old size when either commit is missing). The rider list is `RIDERS` in the script; add a commit there when a bead says its fix must ride the next cut, and remove it when the bead closes. Exit 1 names the missing commits; exit 2 means a rider or the commit did not resolve in this clone (fetch first), which is never a pass. A tag cut from the develop tip carries every rider.

### 3. PRE-TAG gate: `--shakeout` (the leg that builds the candidate)

> **`--guided` IS RETIRED — do not use it.** RDR-155 P4b (commit `7e47c285`,
> 2026-07-24) deleted `nx guided-upgrade` / `migrate-to-service` / `storage
> migrate all`, so `--guided`, `--cold` and `--hole-punch` now refuse at the
> arg loop with a RETIRED message and exit 2. This step named `--guided` for
> one cut after the retirement and would have failed the next engine cut at the
> gate. `--chash-window` is ALSO RETIRED (nexus-lgdel.l2, 2026-08-16): its
> entire subject was the pre-cutover legacy 32-hex chash window (RDR-180),
> a capability deleted along with `nexus.chash_alias`; the leg, its
> Dockerfile, and its rehearsal script are gone. Surviving journeys:
> `--era-hop`, `--package-upgrade`, `--shakeout`, `--fullstack`, `--stranded`
> (nexus-8nlj4: two-hop
> stranded-redirect — armed-detector refusal + pin-side migration; weekly
> heartbeat via stranded-redirect-rehearsal.yml, dispatch it on demand when a
> cut touches stranded_install.py or the migration-rehearsal harness),
> `--candidate-migration` (nexus-z0ylb: the locally-built CANDIDATE
> engine's full Liquibase walk over a POPULATED store — provisions the
> PUBLISHED FLOOR engine for real, populates through it (content + catalog
> manifests + a real taxonomy-discovery pass), hand-swaps the candidate
> binary in with the provenance sidecar's tag/version kept pinned at the
> floor (HARNESS bookkeeping, not a production technique — a real release
> `install-binary`'s an honest sidecar at download time; this rewrite only
> keeps a LATER assert in the SAME test run from silently re-acquiring the
> floor over the candidate mid-rehearsal, and a `nx daemon restart-stale
> --dry-run` no-op check inside the leg proves exactly that, nothing
> broader), then boots and asserts the changeset delta plus EXACT row
> invariants — see the leg's own coverage statement below for what it does
> and does NOT prove), and the default `rehearse.sh` (Phases A/D/E).

> **Ordering, still load-bearing.** `--shakeout` BUILDS the candidate locally
> (`run.sh` does the GraalVM `-Ob` native build; only the retired `--cold` path
> skipped it and acquired a PUBLISHED binary instead). `--candidate-migration`
> ALSO builds the candidate locally (same `-Ob` build, stamped with the floor
> version) — the two legs are complementary, not redundant: `--shakeout` proves
> the candidate's CLI-verb/concurrency surface on a fresh install; `--candidate-
> migration` proves its Liquibase walk over populated data. `--with-cloud`
> exercises the conexus-DEPLOYED service, so it cannot run pre-tag either — it
> is part of the post-deploy cloud gate (Step 6).

```bash
NX_CUT_MODE=1 tests/e2e/migration-rehearsal/run.sh --shakeout
```

Must end `CANDIDATE SHAKEOUT PASSED`. `NX_CUT_MODE=1` is the cut-mode switch of
Step 3f: with it, the journey also prints `ENGINE IDENTITY` and `ENGINE
OWNERLESS REFUSALS` for the native binary it served and fails on a refusal. For
this pre-tag run, always set it. Without it the two lines print and nothing
fails on them.

**Phase F now runs `service/native-smoke.sh`'s own probe set too (nexus-l8xnz,
2026-08-17).** The raw-curl probe set (taxonomy/assignments/details' 64-hex
`doc_id` width validation, T1's separate-jOOQ-schema reflection check, the
memory/plans/taxonomy/chash routes, the bge-768 embed path, the fused-rerank
stage) used to run ONLY inside `engine-service-release.yml` — a stale probe
fixture (the pre-fix 16-char `doc_id` literal) burned the `v0.1.77` tag on
both linux release legs while the binary itself was fine, and `--shakeout`
stayed green because nothing local ever exercised the script. `--shakeout`
now drives the IDENTICAL script (byte-for-byte; only the caller is adapted)
against the candidate over the already-provisioned Postgres — a stale probe
now fails `--shakeout` locally, before a tag is ever cut. This does **not**
replace `--acquire` below (which drives the SIGNED, PUBLISHED bytes) — it
closes the "release-workflow-only procedures rot silently" gap for the
*script's own assertions*, not for signing/codesign/cosign/PG-bundle
packaging defects, which remain `--acquire`-only by construction (see below).

**Also runs the release-workflow SHAPE check now, automatically (nexus-xihsm).**
Right after `run.sh`'s own native-build step (the `-Ob` candidate plus jOOQ
codegen), `--shakeout` calls `scripts/check_release_workflow_shape.py`
against the just-built native candidate via
`tests/e2e/migration-rehearsal/lib/shakeout_shape_check.sh`. On a host that
cannot execute that Linux candidate (a macOS host, where the `-Ob` build
runs in a container) phase (a) boots the same build's JVM jar through a
shim instead; phase (a) tests the checkout classification, which does not
depend on native versus JVM. A missing jar there is a FAILED verdict. This does NOT
live inside `rehearse_shakeout.sh`: that script runs inside the `--shakeout`
container, which is a `uv`-tool-installed wheel with no `.git`/`pyproject.toml`
ancestor by design, so the check's phase (a) non-vacuity assert could only
ever refuse there, and phase (b) has no `service/mvnw` or JDK in that image
at all. `run.sh` is the one place both preconditions hold. A failure prints
`[shakeout] release-workflow SHAPE check: FAILED` and exits `run.sh` nonzero
before the container ever starts, so it gates `--shakeout`'s own exit code,
and its verdict line appears in the same terminal output as everything else
`--shakeout` prints. See AGENTS.md's engine-release section for the full
contract. Still uncovered even when wired: cosign signing, the
`promote-release` all-21-assets gate, and mac-arm64's genuine no-Docker
GitHub-hosted runner, none of which run on a box `--shakeout` builds on.

**`--candidate-migration` — MANDATORY whenever this cut's `service/` delta
touches `db/changelog/**` (a new or modified Liquibase changeset); optional
otherwise** (a cut that only touches Java handler/repository code with no
schema change has nothing new for this leg to prove beyond what
`--candidate-migration` already proved on a prior cut of the same schema
generation — run it anyway when in doubt, it is not expensive relative to
`--shakeout`). MANDATORY here means "this class of change gets no other
pre-tag rehearsal against populated data," not "this leg proves the
changeset is safe in every dimension" — read the coverage statement below
before treating a green run as exhaustive:

```bash
NX_CUT_MODE=1 tests/e2e/migration-rehearsal/run.sh --candidate-migration
# a cut whose changeset count is known ahead of time pins it instead of
# merely reporting it, e.g. a cut adding exactly 3 new changesets:
NX_CUT_MODE=1 EXPECT_NEW_CHANGESETS=3 tests/e2e/migration-rehearsal/run.sh --candidate-migration
```

Must end `CANDIDATE-MIGRATION REHEARSAL PASSED`. `NX_CUT_MODE=1` is the same
cut-mode switch as for `--shakeout` above (Step 3f): the journey reads the
candidate engine's ownerless-write counters and log and fails on a refusal. Reports (never asserts to
an exact value unless `EXPECT_NEW_CHANGESETS` is set) the changeset delta
between the floor's post-init `DATABASECHANGELOG` count and the candidate's
post-boot count — `delta=0` is a legitimate, explicitly-stated outcome (it
still proves boot-over-populated-store + checksum stability + grants
idempotence), not a silent skip.

**Coverage — what this leg DOES and does NOT prove** (substantive-critic
finding, 2026-08-14, T2
`nexus/critique-nexus-z0ylb-candidate-migration-rehearsal-2026-08-14`
[22547] — the leg's own script header carries the identical statement,
kept in lockstep). It exercises: boot succeeding over populated data (a
changeset that silently assumes an empty table fails here, not at a
customer's box); RLS not going DML-blind mid-migration; a changeset's own
GRANT/ownership statements not bricking boot; CASCADE fallout on a DROP
TABLE; and checksum/row-count integrity (Liquibase's own checksum
re-validation plus this leg's EXACT row-invariant asserts, now spanning
chunks, catalog manifest/documents, taxonomy centroids AND
`topic_assignments`).

**Tuple space (bead nexus-58vc9, added for the v0.1.118 cut that carries
tuples-003 + nexus-8zoyp).** Stage 3h seeds `nexus.tuples` through the
FLOOR engine before the swap: mailbox rows in every claim state
(unclaimed, claimed-and-left, consumed with and without a reply,
dead-lettered via 3 claim/nack cycles), an over-4096-byte body (written
past the working-tree client's own mirrored 4096-byte pre-check, when
the floor enforces no size limit; a floor from v0.1.118 on refuses it with
TooLarge, which the leg asserts in place of the over-cap checks), an
exactly-4096-byte body, and
ledger rows — under a second tenant too when the floor's `nx tenant
create` supports minting one. Post-walk it asserts: the over-cap row is
gone with its claim-log history surviving at `tuple_id=NULL`
(`tuple_claim_log_tuple_fk`'s `ON DELETE SET NULL`); the at-cap row is
untouched; unconsumed and dead-lettered bodies are untouched;
`chk_tuples_body_size` is VALIDATED; RLS is ENABLE+FORCE on both
tuple-space tables; and the candidate can still claim and ack a surviving
row. The consumed-body assert counts the row together with its NULL body
(`1:1`), so a changeset that deleted consumed rows instead of clearing
their bodies fails rather than reading as an empty body. The PASSED and
FAILED lines name how many tenants the tuple population covered
(`tuple_tenants=1|2`). The scheduled sweep itself (6h interval,
6h initial delay) cannot fire inside this leg's wall-clock budget, so
"the candidate can run the sweep" is asserted structurally (the
dead-lettered row's `claim_state`/`attempts` shape matches what the
sweep's own release/purge arms key their `WHERE` clauses on), not by
observing a scheduled pass execute.

It structurally CANNOT catch four classes, by construction of what this leg
seeds:
- **Cross-shard PK collision** (the "cross-shard collision" `DO $$` guards
  that vectors-004/taxonomy-007-style changesets carry) — this leg seeds
  ONE embedding dimension (bge-768) only; reproducing a genuine collision
  needs a second populated dimension sharing a colliding key.
- **Planner-statistics flips** from a stats-absent post-migration table
  picking a different query plan under real data volume — this leg's
  corpus tops out around ~190 rows, far too small to exhibit one. That
  class is pinned at the JAVA layer instead:
  `SchemaMigratorIntegrationTest::rdr180Rewrite_leavesPlannerStatsFresh`
  (`service/src/test/java/dev/nexus/service/SchemaMigratorIntegrationTest.java`).
- **Scheduled tuple-sweep execution** (6h interval, 6h initial delay): the
  sweep never fires inside this leg, so its per-arm isolation and per-row
  savepoint recovery are not exercised here. Covered by
  `NexusServiceTupleSweepTest` and `NexusServiceTupleSweepIsolationTest` in
  the Java suite.
- **Tuple-table scan duration at live volume**: Stage 3h seeds about 20
  tuple rows, against about 2150 live ledger rows across 37 subspaces
  (2026-09-13). tuples-003's DELETE and VALIDATE and tuples-004's
  consumed-body UPDATE scan the whole table; their lock duration at real
  volume is the PITR-fork walk's to measure, not this leg's.

This is strictly stronger than the `--guided` gate it replaces. It performs the
same native-image build — the `-Ob` quick build has the SAME reachability
requirements as the full release build, so it catches a broken native build
before the tag burns a release-workflow run — and then adds the full CLI-verb
matrix, incremental index, and (nexus-xm0cp) a CLIENT-SIDE census over the
concurrent load phase against that binary — the candidate has no request
logger (`com.sun.net.httpserver`, no access-log appender), so there is no
service-side 5xx signal to scan. The census has TWO parts, not one: (1) none
of the concurrent `nx store put` / `nx index repo` calls exit non-zero, and
(2) none of them logs an absorbed `vector_gateway_retry` either — coverage
gap (1) alone would miss it: `HttpVectorClient` retries a 502/503/504 within
a bounded budget BEFORE ever raising (`_GATEWAY_RETRY_CODES`,
`src/nexus/db/http_vector_client.py`), so a gateway blip that resolves in
time never reaches a client exit code at all, exactly the shape a lock
convoy is most likely to take. Part (2) closes that gap by scanning each
call's log for the retry's own structlog line. A FAIL here is a product
finding, not a harness formality: its maiden runs caught two production bugs
the unit suites missed (nexus-h8rf6).

Notes:
- The host JVM suite (`scripts/mvnw-leased.sh -q test`, Step 2) validates the Java
  on the JVM; `--shakeout` adds the native-image build + serve + drive.
- **Prefer the container rehearsal over `release-sandbox.sh`** for engine work —
  it is the isolated harness for a NATIVE build, and it owns its own image.
  (The old reason given here — that release-sandbox.sh "swaps the uv tool venv
  and can break the live install" — is no longer true and was contradicted by
  this repo's own release skill. Installs are side-by-side generations: the
  sandbox activates its `HOME` before reinstalling, so generations land in
  `$SANDBOX/.local/{share/nexus/tools,bin}`, nothing is swapped under a live
  holder, and `--force`/`--cycle-daemons` no longer exist — nexus-utpuw.8.
  Recommendation unchanged, mechanism corrected: nexus-utpuw.20.)
- When the two-hop stranded-redirect rehearsal lands (nexus-8nlj4) it becomes
  the acceptance journey that replaced the retired guided legs; add it here
  then, alongside `--shakeout` rather than instead of it.
- `--candidate-migration` requires a native build the same way `--shakeout`
  does and refuses `--no-build`; it does NOT combine with any other leg
  (standalone entrypoint, same discipline as every other journey in this
  harness).

### 3b. Client-release preconditions — a DEPLOY gate, NOT a tag gate

```bash
uv run python scripts/check_client_release_precondition.py --engine-tag engine-service-vX.Y.Z
```

Some engine changes BREAK clients that predate a specific client commit (the
nexus-9ssih dangling-endpoint 400 is the canonical case — its first landing
was REMOVED by 6714e70e to wait for the client half). This script refuses
(exit 1) until every client commit the tag requires is an ancestor of the
latest RELEASED `v*` tag. Register new preconditions in the script's
`ENGINE_CLIENT_PRECONDITIONS` whenever an engine change ships a wire behavior
old clients mishandle. Prose deploy-gates get skipped; this one does not.

This is also where the mechanized both-halves wire-contract ledger
(`docs/wire-contract-pending.md`, `scripts/check_wire_contract_pairing.py`)
gets consulted on the **unpaired** deploy path (protocol-audit [22511] Gap 1,
2026-08-14) — an ordinary "refresh the cloud engine" run with no paired
client release in flight. A non-empty `## Unshipped` section blocks (exit 1)
regardless of `--engine-tag` unless every entry's bead is acknowledged:

```bash
uv run python scripts/check_client_release_precondition.py --ack-client-lag nexus-1234
```

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

### 3c. PRE-TAG gate: published-client write leg (`published-client-write-gate.sh`, nexus-86mx2)

```bash
tests/e2e/published-client-write-gate.sh
```

Must end `PUBLISHED-CLIENT WRITE GATE PASSED`. Runs the CURRENTLY-PUBLISHED
conexus client (real PyPI, scrubbed-HOME sandbox — same isolation idiom as
`fresh-install-mvv.sh --published`) against THIS candidate engine (working-
tree dev jar by default via `scripts/build-gate-jar.sh`;
`NEXUS_SERVICE_TAG=engine-service-vX.Y.Z` points it at a specific published
tag instead) and asserts REAL catalog registration — manifest ROW COUNT via
`GET /v1/catalog/manifest/verify`, exact expected count, never a 200 alone —
for a `store put` and an `index md` write.

**Why the other legs do not cover this.** Every other gate in this checklist
tests a CONSISTENT client/engine pair: the host JVM suite and `--shakeout`
are develop×develop; `--acquire` is the WORKING-TREE client against a
published engine; `--package-upgrade` proves an EXISTING install's engine
converges, never a fresh client's WRITE path against a stricter successor.
None of them is "the client every user currently has, writing against the
engine about to ship." That gap is exactly how `engine-service-v0.1.73`'s
RDR-191 GATE-2 constraint (manifest writes must name their collection)
400'd every released-7.6.1 manifest write in PRODUCTION while every gate
above stayed green (T2 22488/22489, nexus-sh9v2 — 910 live documents
accumulated invisible to catalog-aware retrieval before anyone noticed).
This leg would have caught it here, before deploy, instead of in production.

A published client KNOWN to be incompatible with the current engine (the
exact window before a client release ships the fix) is not a tag-cut
blocker — acknowledge it explicitly and by name:

```bash
NX_EXPECTED_CLIENT_LAG=nexus-z0o2p.24 tests/e2e/published-client-write-gate.sh   # the bead named in the script header (EXPECTED_LAG_BEAD)
```

The bead is typed literally because the variable `EXPECTED_LAG_BEAD` lives inside the script, not in your shell: `"$EXPECTED_LAG_BEAD"` expands to nothing here and the script exits 1 on the mismatch. `tests/scripts/test_engine_release_skill_commands.py` pins this literal to the script's own constant, so a hand-update of one fails until the other follows.

Exits 2 (`PUBLISHED-CLIENT WRITE GATE EXPECTED-INCOMPATIBLE`) — a named,
counted state, never a silent pass. The script refuses the acknowledgment
(hard-fails instead, exit 1) once the published client it actually resolves
is >= the version its own header names as the fix — an ack held past its
expiry is exactly the drift this gate exists to catch. See the script's own
header for the full contract; do not re-derive it here.

**The RDR-223 + RDR-192 cut: run this gate TWICE, once per ownerless-write mode
(nexus-9a6io).** The final cut's engine refuses a chunk write no document owns
(422, `reason: ownerless_chunk_write`), and every published client before the
Phase 2 client migration writes that way for `nx store put` and `nx index md`. So
against this candidate the published client is incompatible by design, and the
expected result depends on the engine's `NX_OWNERLESS_WRITE_MODE`:

```bash
# first production deploy posture (cloud, variable unset: the engine's own default is log-only): expect exit 0
NX_GATE_OWNERLESS_WRITE_MODE=log-only tests/e2e/published-client-write-gate.sh
# the final posture: expect exit 2, EXPECTED-INCOMPATIBLE
NX_EXPECTED_CLIENT_LAG=nexus-z0o2p.24 NX_GATE_OWNERLESS_WRITE_MODE=enforce \
  tests/e2e/published-client-write-gate.sh
```

Exit 0 in `log-only` requires the engine's `ownerless_writes_would_refuse_total` to be
above zero afterwards, which proves the legacy path reached the ownerless route. Exit 2 in
`enforce` is accepted only when BOTH journeys failed, each journey's own client output names
the refusal, and `ownerless_writes_refused_total` is at least 2 (one per journey), so the
ack cannot hide a failure that is not the refusal: one journey refused plus the other
broken for another reason is exit 1. The refusal match was MEASURED on 2026-10-01 against
published conexus 7.67.0 and the working-tree candidate (`NX_PUBLISHED_CLIENT_VERSION=7.67.0
NX_GATE_OWNERLESS_WRITE_MODE=enforce NX_EXPECTED_CLIENT_LAG=nexus-z0o2p.24`: exit 2,
`refused_total=2`): each journey's own output carries the engine's sentence "refusing an
ownerless chunk write on <route>" (`nx store put` inside a `store_put_ghost_register_compensated`
warning, `nx index md` in its one-line `Error:`), and the gate prints each journey's deciding
line as `refusal line:`. Re-measure when the client's error rendering changes; if a journey's
output stops naming the refusal the cut stops here, and the fix is the classifier, decided
from the engine's wire text, never loosened to any failure. An UNSET gate mode against a candidate that carries
the refusal runs the engine through the local launcher, which sets `enforce` by default, so
unset is not log-only here; set the mode explicitly in both runs. A red verdict from either run is a stop. If the
published client is already at or above `FIXED_IN_VERSION` (the paired release), both
runs must pass with both counters at zero, and a published client below it that moves no
counter means `FIXED_IN_VERSION` in the script is stale: set it to the paired release in
the release PR. The cutover order that follows this gate is
`docs/operations/ownerless-write-cutover.md`; the verdict logic is pinned by
`tests/e2e/published_client_write_gate_verdict_test.sh`.

### 3d. PRE-TAG gate: RDR-194 D4 cloud-count-5 delivery precondition (nexus-tk070.p5a)

```bash
uv run python scripts/check_rdr194_cc5_delivery_gate.py --ref HEAD
```

Only relevant when this cut's `service/` delta carries
`taxonomy-014-topics-tenant-unique.xml` (RDR-194 P5a) — the gate exits 0
immediately, without ever consulting T2, when that file is absent from
`--ref`. When it IS present, the gate blocks (exit 1) unless a T2 record at
`(nexus, cloud-count-5-measured)` carries an explicit MEASURED-zero reading
for all three cross-tenant sub-populations (`topic_assignments`,
`topic_links`, `topics.parent_id` — see the script's own header for the
exact record contract). `nx` unreachable is exit 2, UNVERIFIABLE — never a
silent pass, same fail-closed doctrine as `check_engine_release_floor.py`
(mold this gate copies). Mirrors that gate's shape: a mechanical pre-tag
check replaces what would otherwise be a P7 human-checklist item alone —
see nexus-i5c2u for why an eyeball-only version of this class of check has
already burned this project once (9+ days of cloud engine drift that
nothing caught).

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

### 3f. PRE-TAG gate: the engine gates must run the CANDIDATE, not the pinned engine (nexus-0kmat)

**Who, when, where.** The AI preparer runs this, before Sam's tag (Step 4), after
Step 3's `--shakeout` and Step 3c. It runs in a temporary worktree at the exact
commit you will tag, not in the primary: the battery refuses a checkout that
holds `develop` while peers exist (a push elsewhere fast-forwards that tree
under a 70-minute run), and a detached worktree holds no branch at all.

```bash
git worktree add --detach ../nexus-wt/engine-cut-<sha7> <sha-you-will-tag>
cd ../nexus-wt/engine-cut-<sha7>          # a plain cd; the battery reads this tree's identity
tests/e2e/release-battery.sh --cut --only mvv,smoke,shakedown,dtok,lsg
```

`NX_BATTERY_ALLOW_DEVELOP=1` in the primary is the fallback only when nothing
else on the box pushes `develop` for the length of the run; a peer's push reds
it on a tree-identity mismatch after the legs are paid for.

Always pass `--cut`. `--candidate-engine PATH` without `--cut` is an error (it
would export a candidate and assert nothing), as is an ambient
`NX_CANDIDATE_ENGINE`. Without `--candidate-engine` the candidate is the
stamped jar the battery's artifacts leg builds from this tree. A candidate that
is not byte-identical to the artifacts manifest's jar or native binary would
put two engines in one green run, so it is refused unless you also pass
`--accept-candidate-mismatch`, which makes the verdict PARTIAL.

**Why these legs.** `fresh-install-mvv.sh`, `data-token-cli-gate.sh` and
`release-sandbox.sh` (battery legs `mvv`, `dtok`, `smoke`, `shakedown`) run
`nx init`, which downloads the PINNED PUBLISHED engine
(`REQUIRED_ENGINE_VERSION`). That engine predates the change you are about to
tag, so a leg that provisions it says nothing about that change; the RDR-223
ownerless-write refusal went through all of them untested because the pinned
engine has no ownership check. The pin moves only after the tag is immutable,
so a missed writer costs a re-cut. `lsg` serves the artifacts jar and is
included. Two more legs run the candidate NATIVE binary inside a container:
`shakeout` (Step 3's `--shakeout`) and `candmig` (Step 3's
`--candidate-migration`). Step 3's commands for those two carry `NX_CUT_MODE=1`,
so they read the engine's refusals too. The `--only` list above leaves them out
(the battery would only repeat them), so the battery's table says nothing about
them: **their evidence is Step 3's console output.** Look there for `ENGINE
IDENTITY [shakeout]` and `ENGINE OWNERLESS REFUSALS [shakeout]`, and the same two
lines labelled `candidate-migration`, each naming the native binary by sha256
with `controls=0` and zero counters. A Step 3 run that printed neither pair did
not read the engine, whatever its verdict line says. `pkgup` is not an engine
leg: it converges an old install to the PUBLISHED engine and never runs the
candidate.

**The native `-Ob` proof stays with Step 3 `--shakeout`.** It is the only
gate on the binary that ships. The jar these legs take is a JVM build of the
same source, not the signed native binary, and the artifacts leg builds the
native binary again anyway.

**Reading the result.** `--only` always ends `RELEASE BATTERY PASSED (PARTIAL:
n leg(s) skipped by --only ...)`: that is the expected last line here, never a
release verdict by itself. The evidence is the table: all five legs PASSED, none
`VACUOUS in cut mode`, no `CUT MODE` abort line, and no other PARTIAL reason.
What the cut mode adds, all in `tests/e2e/lib/candidate_engine.py`:

- No candidate, a candidate that cannot be found, or an ambient
  `NEXUS_SERVICE_*` naming a different artifact, is a refusal at the gate's
  start; nothing falls back to the pinned engine.
- Every leg prints `ENGINE IDENTITY [leg]: candidate=yes|no kind= artifact=
  sha256= release_version= build_ref= ownerless_write_mode=` and, at the end of
  its journey, `ENGINE OWNERLESS REFUSALS [leg]: candidate= sha256=
  refused_total= would_refuse_total= log_lines= log= controls= mode=`.
  `release_version` alone cannot tell a dev jar from the pinned release (the jar
  bakes the same floor value); read `build_ref` and `sha256`.
- In cut mode the leg itself fails, and the battery fails a leg whose log lacks
  EITHER line, when the engine is not the candidate (by sha256, at the start AND
  at the end of the journey), when `/v1/status` is unreachable (the probe uses
  no proxy), lacks `ownerless_write_mode=enforce`, or lacks both counters, when
  the counters and the engine log do not read EXACTLY the gate's own declared
  control, or when the engine log is missing. The log read is the one the lease's
  launch kind names (`storage_service_jar.log` for a jar,
  `storage_service_native.log` for a native binary); it is the half that
  survives an engine restart mid-journey.
- `controls=` is how many DELIBERATE ownerless writes the gate itself sent that
  engine: 1 for `lsg` (its smoke leg sends one ownerless `upsert-chunks`, the
  negative leg of nexus-z0o2p.24) and 0 for every other leg. In enforce the
  reading must be `refused_total=<controls> would_refuse_total=0
  log_lines=<controls>`; in log-only, `refused_total=0
  would_refuse_total=<controls>`. More than the control is a writer nobody
  intended: it writes a chunk with no manifest owner, so fix the writer before
  tagging. Fewer is red too: the counter or the log did not see the gate's own
  control, so a zero from that engine proves nothing. A gate states its count
  next to the control it sends and cannot omit it.
- Each gate stages a private copy of the candidate: the supervisor finds its
  engine by argv, so two parallel gates on one jar path stop each other's
  engines (measured 2026-10-01: exit 143). The copies are removed on every exit.

**The mode variable, and one that is not there yet.**
`NX_CANDIDATE_EXPECT_OWNERLESS_MODE` (this step) is an ASSERTION: the mode the
engine serving a cut leg must report, default `enforce`. Setting it to anything
else (`none` drops the mode assert, `log-only` expects log-only) relaxes that
one assert only: identity, reachability, counters, the log and the control
count are still required. The battery prints a `CUT MODE WARNING` banner and
ends PARTIAL, as `--only` does; it is never the final cut. A second variable,
`NX_GATE_OWNERLESS_WRITE_MODE`, is planned as an INPUT to
`published-client-write-gate.sh` (Step 3c: the mode that gate starts the
candidate in, so it can prove the refusal in both modes). It is added by
nexus-9a6io, which was open on 2026-10-01, and `published-client-write-gate.sh`
reads nothing by that name on develop until that bead lands. Check the script
before relying on it.

Still NOT covered: writers outside this repo (other machines, hooks, the WSL
appliance, hellmini), which only production traffic exercises (T2
`nexus/review-z0o2p24-critique` Issue 2b); and the remaining engine-driving
legs (`upshakeout`, `genflip`, `rehearse_*`), which write no chunks.

**The pinned-engine red is real and expected until the pin moves.** Develop's
client sends `metadata_merge` and checks that the engine echoes it. The pinned
engine (v0.1.142) does not, so any non-cut battery leg whose journey writes a
chunk through that path is red against it with `EngineOlderThanClientError`.
Measured 2026-10-01 for `dtok` (store put, "asked for metadata_merge but the
response did not echo it"); `mvv`, `smoke` and `shakedown` write through the same
path and are expected to go the same way, which a non-cut run has not yet
confirmed. The paired-release choreography resolves it (engine tag first, the
client release bumps the floor and gates its battery against that engine), so a
non-cut battery on develop is not evidence before the pin moves; the cut-mode
run above is the only coherent pre-tag evidence. To keep the red from reading
as an unexplained failure, name it:

```bash
tests/e2e/release-battery.sh --expected-engine-lag nexus-z0o2p.9@0.1.142 ...   # <bead>@<REQUIRED_ENGINE_VERSION>
```

A red `mvv`/`smoke`/`shakedown`/`dtok` leg whose failing STEP carries the
`EngineOlderThanClientError` signature then reads `EXPECTED-LAG(<bead>)`, is
counted, and ends the battery PARTIAL (never a release verdict). The failing step
is the failed verdict line, the log files that line names (and the `.stderr.log`
beside a named `.log`), and the stretch of the leg log that ends at the failure
and starts after the previous step boundary, at most 30 lines. A tolerated
mention of the error in an earlier step does not count, and neither does the
newest log in some evidence directory. Any other red stays red. The ack refuses
to run once `REQUIRED_ENGINE_VERSION` is no longer the version it names, and in
cut mode it is an error (cut mode gates the candidate, where a lag ack would
hide the red it exists to find). `dtok` is a leg in cut mode, and in a non-cut
run when you name it: `--only dtok`.

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

**Why `--shakeout` does not cover this.** Step 3 drives the LOCALLY BUILT `-Ob`
candidate. The published artifact is different bytes from a different builder:
full native build (not quick-build), codesign, cosign, PG-bundle packaging. A
defect introduced by the release workflow is invisible to the local shakeout BY
CONSTRUCTION — `nexus-2oh5q` is exactly that hazard (signing breaking JNI dlopen
of the bundled onnxruntime/DJL), dormant only while the Apple secrets are
unprovisioned. Historically this gate caught `nexus-pi3s3` + `nexus-qeoxf`
(2026-06-26), defects every local suite missed.

**Scope limit, carried from `nexus-1ddsy`'s close:** the container is Linux, so
this exercises the linux artifact. The mac-arm64 post-signing path is NOT covered
here — tracked on `nexus-2oh5q`.

**The mac-arm64 gap has a gate — it is just MANUAL and not yet armed.** The
provisioning half (six Apple credentials, both portals, the pre-flight and the
renewal failure modes) is
[`docs/operations/apple-code-signing.md`](../../../docs/operations/apple-code-signing.md).
Once those are provisioned and the first Developer-ID-signed tag publishes,
run on an arm64 Mac, BEFORE setting `APPLE_SIGNING_REQUIRED=true`:

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

> **`--with-cloud` does NOT belong here.** It is NOT a local/acquire leg — it
> exercises the **conexus-DEPLOYED** cloud service, so it can only run AFTER the
> engine is deployed to `api.conexus-nexus.com` (Step 6). Running it pre-deploy
> tests the *previously*-deployed cloud engine, not the candidate. It is part of
> the post-deploy cloud-gate, below.

### 5b. Migration-release branch (CONDITIONAL — this tag carries schema or data DDL)

Trigger: `service/` since the last engine tag includes a new Liquibase changeset, or any change to a data shape existing rows already have to conform to (T2 [22511] gap 9). Skip this step entirely when the cut carries no such change.

1. **Representative-scale rehearsal.** Run the populated-store rehearsal in PUBLISHED-bytes mode against a corpus seeded ABOVE a stated floor, not the harness's default toy seed (10-30 docs across `rehearse_cold.sh` / `rehearse_acquire.sh` / `rehearse_shakeout.sh` / `rehearse_hole_punch.sh`):
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

Must end `PASSED`. It reads the release's asset sizes (`gh release view`) and fails when a binary is at or near its pre-fix size: v0.1.142 shipped `nexus-service-linux-amd64` at 231.8 MiB, `linux-arm64` at 227.0 and `mac-arm64` at 193.3; nexus-lhr6a measured the fixed build at 150 (amd64) and 154 (mac), and the ceilings are 175, 175 and 175. linux-arm64 was never measured: its expected size is about 147 (an estimate scaled from amd64) and its ceiling uses amd64's margin; replace the estimate with the first published arm64 size. A size back near the old values means the dedup did not take effect in the release build, which the embedded-resources checker does not cover (nexus-zz2w7). Step 2b proves the fix was in the tagged commit (ancestry, not content: a later revert leaves it an ancestor); the size gate proves it shipped. Close nexus-ujbz8 after a pass.

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
> **Assertions the fork walk must carry (nexus-k9fs1, from the nexus-q81g7 schema pin).** The engine pins Liquibase's history to `public` and the migration session's `search_path`. The failing property (a second walk that replans everything) exists only on a database that has already been walked once, so only the fork shows it. Hand conexus Step 6a: the schema check three times (before the walk, after walk 1, after walk 2) and the walk check twice (once per boot), against a SECOND boot of the same engine on the fork. `tests/e2e/two-walk-check.sh` runs the same assertions against a throwaway local engine across two boots; run it before the relay so the checker is known good.
>
> **Cutover posture for the ownerless-write refusal (nexus-20onx).** Tell conexus the first deploy runs `NX_OWNERLESS_WRITE_MODE` unset or `log-only` (never `enforce`), that the flip is a Terraform parameter plus a same-tag redeploy, and where the soak is read; the full order of operations, including the restart step, is `docs/operations/ownerless-write-cutover.md`. Include the doc in the relay. Three more items for the same relay: (1) `deploy/engine/image-smoke.sh` (conexus repo) must boot the built image with the production `NX_OWNERLESS_WRITE_MODE` value, not its own default; that catches only an INVALID value (the engine refuses to boot), which is a smaller claim than "a mis-wired parameter fails before the push": a valid `enforce` on the first deploy boots fine and refuses every legacy write; (2) so the first deploy's arming checklist also carries an assertion run BEFORE the push, against the booted image or the staged parameter: `/v1/status` `ownerless_write_mode` must equal `log-only`. **This is a conexus-owned hold-the-push line, not a nexus gate, and nothing in this repo checks it (nexus-20onx round 4, deliberately).** Owner: conexus; step: image built and redeploy staged, before the paired client tag is pushed; evidence: the value they read, in their arming reply. Why not an `ownerless_write_mode` field on the `docs/release-arming/` attestation: the attestation records deploy facts conexus itself re-checks at ITS flip (image digest, parameter version), and the mode is another property of a deploy that has not happened at tag push, so a nexus reader would only echo conexus's own claim; a required field no writer emits yet would fail every paired tag until conexus's writer changes (a repo this side cannot see or test), an optional one asserts nothing; and `--paired-deploy-auto` can skip the battery that would read it. The nexus-side backstop runs after the harm window, not before it: Step 6.1 leg B3 after the first deploy; (3) after the first deploy, `/v1/status` must report `ownerless_write_mode` = `log-only` again, and after the flip redeploy `enforce`: Step 6.1 asserts both (`NX_EXPECTED_OWNERLESS_WRITE_MODE`). The code default itself (unset is `log-only`) is pinned by the engine's own test, `OwnerlessWriteRefusalTest.anUnsetModeBootsLogOnly_andAnExplicitEnforceBootsEnforce`; no direct-binary battery leg repeats it (nexus-20onx comment: the launcher always sets the variable, so a leg that boots the binary with it absent needs its own PG and env wiring, for a property the unit test already pins).
>
> Also confirm with conexus before the window opens: (a) the per-release PRE-DEPLOY prerequisites table — some changesets need a Crunchy-superuser grant to EXIST before boot migration, and its absence is a loud failure on the live engine; (b) the per-release DATA EFFECTS table — anything the walk deletes is acknowledged in advance, never discovered mid-deploy; (c) the image is cosign-signed, since under `enable_image_verification=true` an unsigned image BRICKS BOOT; (d) the current image tag is captured FIRST as the rollback target, and the rollback floor is `nexus-service-0.1.84`.
>
> **The DATA EFFECTS table in (b) is produced mechanically, not written by hand** (nexus-f7dwp — before this, a destructive changeset's effect reached conexus only because someone typed it into the handoff, and tuples-003-2 / tuples-004-1 shipped in v0.1.118 that way). Run `uv run python scripts/list_data_effects.py <previous-engine-tag> <this-tag>` and paste its markdown table verbatim into the relay — it lists every changeset added in this range that modifies or removes existing rows, each carrying its `DATA EFFECT:` line and a CENSUS PREDICATE column (the exact matched SQL statement). For each row, ask conexus to turn that predicate into a `SELECT count(*) FROM ... WHERE ...` probe against the PITR fork BEFORE the walk. **Only when the changeset's own comment or a paired changeset documents a RAISE NOTICE'd count** (e.g. tuples-003-2, paired with tuples-003-1's logged count) compare the probe to that RAISE NOTICE count — the two must agree, or the row's disposition needs a second look before the window closes. Most data-effecting changesets carry no such count at all (22 of the 38 files nexus-f7dwp backfilled emit zero RAISE NOTICE — single-changeset ALTER COLUMN TYPE rewrites, backfills, and drops, tuples-004-1 itself included): for those, there is nothing to compare the probe against, so just confirm the probe's count is plausible against the DATA EFFECT prose's own stated scope (e.g. "every existing row", "the N rows measured at census time") before the walk runs. A non-zero exit from the script (a row shown `MISSING`) means a changeset in this range modifies rows with no disclosure at all — fix it (add the `DATA EFFECT:` line to the changeset's `<comment>`, checksum-neutral per `scripts/data_effect_lint.py`'s own docstring) before cutting the tag, not after. **After pasting the table into the relay, machine-check that it actually landed there rather than trusting the paste** (nexus-iu43o — before this, the paste itself was a prose step with nothing checking it happened): `uv run python scripts/list_data_effects.py <previous-engine-tag> <this-tag> --record-relay-attestation` writes `docs/data-effect-relay/<this-tag>.json`; a release battery's `--verify-relay-attestation` (same two refs) then refuses if that attestation is missing or stale, and passes as not-applicable when the range carries no data-effecting changesets at all — mirrors `docs/release-arming/`'s reader/writer shape, both halves nexus-side this time since the relay's sender and its own record live in one repo.
>
> What conexus does NOT have is a staged/shadow deploy of the BINARY — one environment, and it is the live estate (conexus-vbti). State that narrowly. On 2026-08-27 this checklist's post-deploy-only gate list was read as "the cutover is unvalidated by construction" and reported to Hal; the binary half was right and the WALK half was wrong.

> relay: deploy `engine-service-vX.Y.Z` to `api.conexus-nexus.com` + re-run the cloud gate (recall + hybrid parity, xr7.8.9-style).

**THIS is where 3b's precondition check blocks.** Re-run `check_client_release_precondition.py --engine-tag <tag>` before surfacing the relay: a red exit means the deploy waits for the client tag carrying the listed commits. In the paired-release choreography that is not a long wait — the deploy relay fires at client-tag push, in parallel with the client's PyPI publish, so the precondition is satisfied the instant the client tag exists and the engine is live before any user can install the client that requires it.

**nexus-1emxn refinement — prefer deploying BEFORE the client tag when the ledger allows it.** When every wire-ledger `## Unshipped` entry carries the `[additive]` direction-safety token (old client + new engine safe), `check_client_release_precondition.py` accepts the unpaired deploy by name — surface the relay and get the engine LIVE ahead of the client tag, so the tag can never open a refusal window (the v7.23.0 window sat open 48+ minutes because "fires at tag push" was an unsent human relay). When any entry is not additive, the client release's Step 9 refuses to tag until this relay is ARMED with conexus (image built, redeploy staged on the named tag trigger) and confirmed.

The post-deploy `--with-cloud` rehearsal (`run.sh --with-cloud`, the cloud → cloud Voyage journey) requires the candidate to be **deployed on conexus** first — it runs as part of this cloud-gate, once the deploy lands, not in Step 5. For cross-repo gate / deploy status, **read the authoritative bead + the conexus bus, not memory** — cross-repo state goes stale fast (2026-06-26: a `luxe6` condition had been cleared a week earlier than memory implied).

### 6a. PITR-fork walk assertions, handed to conexus (nexus-k9fs1)

Hand conexus these instructions with the relay. Five invocations: **schema three times, walk twice.**

- **Pin the script to the tagged commit.** Run from a checkout of the commit the engine tag points at, not develop and not a release branch: the checker's expected `runAlways` count belongs to that tag's changelog (`DEFAULT_REEXECUTED`, pinned to the changelog by a test).
- **Plain `python3`, no `uv run`.** The script is stdlib-only; a synced nexus environment is not needed. It also needs `psql` and libpq environment variables (`PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD`) for the fork; nothing goes on argv.
- **Where the walk logs come from.** One log file per boot of the engine on the fork, taken from the per-boot CloudWatch export of the engine's log stream (confirm the export with conexus; nothing in this repo reads it). `walk1.log` is the first boot of the new image on the restored fork; `walk2.log` is a SECOND boot of the same image on the same fork. Do not concatenate two boots into one file.
- **Walk 1 must pin `--expect-recorded NEW`, NEW > 0, for a tag that carries a changeset** (the checker refuses a walk given none of `--expect-recorded`, `--expect-new` and `--noop`, exit 2). Without it a no-op boot of the new image passes: the counts are self-consistent and nothing was applied. NEW counts every changeset the walk records, executed or marked ran: `--expect-recorded` pins `new + mark_ran`. **Do not pin `--expect-new` for this tag**: `staging-6-drop-landing-schema` is `MARK_RAN`-guarded (`onFail=MARK_RAN` on schema-exists), so on a fork whose `staging` schema is already gone the walk logs `new=2 mark_ran=1`, and `--expect-new 3` fails against a correct walk. Size NEW from the cloud's live `release_version` (the walk is cumulative): the changesets the fork's `public.databasechangelog` lacks against the tagged changelog. A first estimate from the diff, counting a tag's start line whether or not its attributes continue on later lines: `git diff --unified=0 <live-release-tag> <this-tag> -- service/src/main/resources | grep -E '^[+-][[:space:]]*<changeSet([[:space:]]|$)' | cut -c1 | sort | uniq -c` (added tags minus removed tags; an edited tag shows as one of each), then confirm against the fork's own history before the run. A mismatch is a finding to explain, never a number to adjust until it passes.
- **Walk 1 also has an independent floor.** The engine's counts and the table's row delta both read `databasechangelog`, so on their own they prove only that the log agrees with the table. Every changeset the tagged changelog carries must have a row after walk 1, so the step-3 row count must be at least the tree's own count, `python3 scripts/check_pitr_fork_walk.py changelog-count` (XML parse of the tagged changelog): pass it as `--min-rows "$TREE"` on the step-3 `schema` run. It is a floor, not an equality, because production also holds rows for superseded changesets and 13 duplicate rows (AGENTS.md, Engine-service release, "CONFIRMED CAUSE"). `tests/e2e/two-walk-check.sh` asserts the exact equality on a throwaway engine, where nothing else is in the table.
- **One boot per log file is enforced.** A file holding more than one `schema_migration_start` is exit 2, not read as its last boot: the previous reading checked only the last boot of a file that held two and hid the first.
- **The migration role** is the engine's `NX_DB_ADMIN_USER`, else `NX_DB_USER` (the engine defaults the admin user to the service user), read from the engine's environment, never from records. An empty value is exit 2: `"$NX_DB_ADMIN_USER"` expands to the empty string when that variable is unset, so use `${NX_DB_ADMIN_USER:-$NX_DB_USER}`. On `walk` the option may be omitted, in which case the role is taken from the log's `schema_migration_session` line.

```bash
ROLE="${NX_DB_ADMIN_USER:-$NX_DB_USER}"
S=/path/to/scratch/pg_db_role_setting.json       # written by the first schema run, compared by the other two
python3 scripts/check_pitr_fork_walk.py schema --migration-role "$ROLE" --save-settings "$S"        # 1. before the walk
python3 scripts/check_pitr_fork_walk.py walk --engine-log walk1.log --migration-role "$ROLE" --expect-recorded NEW   # 2. walk 1: identity, no anomaly, session line present, NEW changesets recorded (new + mark_ran)
TREE="$(python3 scripts/check_pitr_fork_walk.py changelog-count)"                                    # the tagged changelog's own changeSet count
python3 scripts/check_pitr_fork_walk.py schema --migration-role "$ROLE" --compare-settings "$S" --min-rows "$TREE"   # 3. after walk 1: rows >= TREE; note "public.databasechangelog rows = ROWS"
python3 scripts/check_pitr_fork_walk.py walk --engine-log walk2.log --migration-role "$ROLE" --noop  # 4. walk 2: nothing applies
python3 scripts/check_pitr_fork_walk.py schema --migration-role "$ROLE" --compare-settings "$S" --expect-rows ROWS   # 5. after walk 2: ROWS is the step-3 count
```

`schema` asserts exactly one `databasechangelog` and one `databasechangeloglock`, both in `public`, the lock not held, the role not named `nexus`, `t1` or `staging` (or any existing schema), and that the `pg_db_role_setting` rows are the same as before the walk (the first run saves them, the others compare). `walk` asserts `new + reexecuted + mark_ran == pending_at_start`, no `schema_migration_count_anomaly`, and that `schema_migration_session` was logged, which a pre-fix engine cannot do. `--expect-recorded NEW` asserts `new + mark_ran == NEW`. `--noop` asserts `new_changesets` 0 and `reexecuted_changesets` equal to the `runAlways` count (the checker's default, counted from the changelog by a test; pass `--expect-reexecuted N` only to probe). Exit 1 is a failed assertion. Exit 2 is "evidence unreadable" and is never a pass: an empty or missing role, an empty log, a psql that does not run, or a walk whose counts the engine logged as unavailable (`counts_unavailable=true`, the -1 sentinel). `tests/e2e/two-walk-check.sh` runs the same assertions against a throwaway local engine across two boots; run it before the relay so the checker is known good. Its last line is plain `TWO-WALK CHECK PASSED` only when walk 1 applied something; when the tree adds no changeset over the engine that provisioned the database it ends `TWO-WALK CHECK PASSED (walk 1 applied no changeset: the changeset-applying path was NOT exercised)`, which is a weaker result, not the same one.

### 6.1. Post-deploy client-visibility gate (MANDATORY, run from a cloud-mode box)

The flip from `log-only` to `enforce` is its own bead, nexus-z0o2p.40, and it FOLLOWS the cut: it needs the soak, a written disposition for every writer the log names, and Sam's confirmation (`docs/operations/ownerless-write-cutover.md`, Order of operations 3 to 4). So the `=enforce` form below is that bead's check after the flip redeploy; it is NOT a Step 6.1 or Step 7 precondition for this cut, and a cut's sign-off never waits on it. This cut's sign-off asserts `log-only`.

```bash
# a cut that carries the ownerless-write refusal also asserts the LIVE mode (nexus-20onx):
NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only tests/e2e/cloud-client-path-gate.sh   # after the FIRST deploy
NX_EXPECTED_OWNERLESS_WRITE_MODE=enforce  tests/e2e/cloud-client-path-gate.sh   # after the flip redeploy
tests/e2e/cloud-client-path-gate.sh                                             # ONLY an engine from before the refusal (no ownerless_write_mode in /v1/status)
```

The mode assertion (leg B3) reads `ownerless_write_mode` from `/v1/status` through the public edge. Nothing else in this repo reads the live mode, and conexus wires the knob, so without it a mis-wired parameter enforces on the first deploy and refuses every legacy write from the hosts before anyone looks. Unset, an engine that reports a mode FAILS the gate (a live mode nobody asserted), and so does an unreadable `/v1/status` body (a curl failure, an edge 401/403/502 or a WAF page: a body without `embedding_mode` is not a status body), whether or not the variable is set; an engine that reports none prints `NOT RUN [B3]` and the final sentinel then reads `... violations=0 (ownerless-write mode NOT asserted: B3 not run)`: that is a skipped check, not a passed one, so a cut that carries the refusal always sets the variable.

Run this AFTER Step 6's deploy relay confirms the tag is live, and BEFORE Step 7's downstream-ref bump or signing off any release shakeout that depends on this engine (T2 [22511] gap 7 — this gate existed only as one prose line in AGENTS.md, in no numbered step of this checklist, since it was born from the nexus-bwulw incident). The gates above prove the ENGINE works, direct; they do not prove the PUBLIC edge (`api.conexus-nexus.com`) exposes the same contracts — 2026-07-23 (nexus-bwulw): the edge stubbed `/version` and auth-gated `/health`, silently disabling voyage threshold gating and dimension-orphan tooling and blocking guided migrations to cloud, while three client features shipped green through every engine-direct gate above. This asserts the engine's pinned contracts (`/version` fields, the `ez5.1` `/health` contract, the client `embedding_mode` probe, the `/v1` read path) survive the public edge.

Client version to run it from: the working tree (`HEAD`), the same dev-client × new-engine pairing every other gate in this checklist uses — this script has no separate published-client mode. It is NOT a substitute for Step 3c's `published-client-write-gate.sh`, which is the leg that pairs the CURRENTLY-PUBLISHED client against the candidate; this step's job is edge-contract visibility, not client-write compatibility.

### 7. After conexus confirms deployed + cloud-gated green, bump downstream refs

(For the RDR-223 cut, "cloud-gated green" means Step 6.1 against `log-only`; the enforce flip, nexus-z0o2p.40, follows the cut and is not waited on here.)

- `tests/e2e/migration-rehearsal/run.sh` `COLD_TAG` default → the new published tag (or override via `NEXUS_SERVICE_TAG`).
- When the NEXT PyPI release bumps `REQUIRED_ENGINE_VERSION` to this tag, also rotate `run.sh`'s `NEXUS_PREV_RELEASE`/`NEXUS_PREV_ENGINE_TAG` defaults (the `--package-upgrade` convergence leg's starting point — must stay one release BEHIND the new dependency or its staleness guard fails loud; nexus-cfgo9). The `--package-upgrade` leg itself runs in the PyPI `release` skill's Step 1, not here — this skill only keeps its inputs fresh.
  **The unit is RELEASES, not engine tags — a SKIPPED engine tag does NOT rotate them** (2026-08-11). `PREV_ENGINE_TAG` is the engine the PREVIOUS RELEASE PINNED. An engine tag that is cut, published, and gated but never pinned by any release (v0.1.70: a defect was found after the cut, so 7.6.0 shipped v0.1.71) is a skipped version — rotating `PREV_ENGINE_TAG` onto it would point the rehearsal's "previous install" at a hop no user ever made. At the 7.6.0 bump the correct values stayed `7.5.0` / `engine-service-v0.1.69` while `COLD_TAG` moved to v0.1.71. The staleness guard only fires when PREV collapses to EQUAL the floor; it does NOT catch "rotated onto a tag no release shipped", so check this by hand at every bump.
- `COLD_TAG` moves at EVERY floor bump, unconditionally — `TestDownstreamConsumersTrackTheFloor::test_cold_rehearsal_tag_is_at_least_the_floor` requires `COLD_TAG >= REQUIRED_ENGINE_VERSION`. A comment in `run.sh` once said not to bump it (true about runtime effect, since `--cold` is retired; false as an instruction) and following it blocked the 7.6.0 battery. A prose comment that contradicts a mechanical test loses to the test.
- `SchemaUpgradeRehearsalIntegrationTest.OLD_TAG` (`service/src/test/java/dev/nexus/service/`) → the PREVIOUSLY-deployed tag (nexus-7z6s7 rotation policy: the old→HEAD rehearsal's "real aged box" realism rots as the fleet moves on; re-verify the two structural preconditions documented on the constant when bumping) OLD_TAG rotation is a THREE-part edit (nexus-gm38i): regenerate the changeset snapshot (`uv run python scripts/gen_rehearsal_hop_manifest.py`), re-derive the new hop's row-DML seed coverage, and re-point the data leg's seeding + its SEED-COVERAGE block + the lint's `DECLARED_SEED_COVERAGE` together — `tests/test_rehearsal_seed_coverage_lint.py` fails loudly until all three agree.
- **`REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) MUST move to this tag** — unconditionally, not "only if the release needs the features". There is ONE engine identity per release: the engine it was built and gated with, on EVERY install path (Hal directive 2026-07-15, after the 14h GH #1402 incident). It is NOT a compatibility minimum. For local-mode installs this constant is the ONLY delivery vehicle — an engine tag that is cut, gated, and never pinned reaches nobody. `PINNED_SERVICE_TAG` is DERIVED from it, so the one edit moves both.
  Sequencing — PAIRED release (Hal directive 2026-08-02, supersedes "bump lands with the NEXT release AFTER deploy"): the bump rides the client release PAIRED with this engine's deploy — same release, not the next one (floor-lag ships a client whose pinned engine lacks the engine halves of its own features: the 7.1.0/v0.1.62 inversion). The deploy relay fires at client-tag push, parallel with the PyPI publish (Step 6), so the engine is live before any user can install the floor-bumped client — UNLESS every wire-ledger `## Unshipped` entry leads with `[additive]`, in which case deploy BEFORE the client tag instead (nexus-1emxn, Step 6's refinement: the preferred branch whenever the ledger allows it — no window can open at all). GH #1402's lesson stands as: never publish a floor-bumped client with NO deploy armed — the deploy fires at tag push (or already fired, on the additive branch), not "eventually". `scripts/check_engine_release_floor.py` fails the release if a gated tag was never pinned. The client-side `release` skill's Step 0 runs this gate with `--paired-deploy engine-service-vX.Y.Z` (nexus-k1c08) to distinguish the expected pre-deploy cloud-behind state from real drift — this skill only needs to ensure the tag it just cut is what that flag names.

### 8. Record state (T2) — written by the post-tag VERIFY from conexus's STEP-6 report

```bash
uv run python scripts/check_engine_release_floor.py --record-deploy-from-gate-report <conexus checkout>/deploy
```

The bare post-tag VERIFY (the same command the `release` skill's Step 0 tells
you to re-run WITHOUT `--paired-deploy` once the deploy lands) is where the
`deployed-engine-version` tracker gets written (nexus-nx3l5, shape c, adopted
2026-08-28). After the cloud engine verifies current and source ancestry passes,
it reads conexus's STEP-6 gate reports from that directory (the conexus
checkout's `deploy/`; gitignored there, so operator-local — set
`NX_GATE_REPORT_DIR` once on the box and the flag becomes optional; a bare verify
with NEITHER refuses, exit 3, and the only way to run it without recording is the
explicit, transcript-visible `--no-record-deploy "<reason>"` opt-out for a box that
does not hold the reports — the reason is required and printed), selects the
LATEST report (by `run_timestamp`) that gated the live `release_version`, requires
it green, and writes the tracker with the report's basename as the `gate`
provenance. Nothing is written — exit `3`, named reason — when no report gated the
live version, the latest is red, or the report schema moved. A green report's
advisories are printed, never inferred empty.

Why this shape: `--gate PASSED` used to be typed. On 2026-08-28 it was typed at
02:41:10Z with ~10 min of Step 6's gate still running; run 1 came back RED 17 s
later (T2 `release-7.22.0-ship-2026-08-28`). Before that the step was simply
skipped (v0.1.17 stale across three deploys; nexus-6igii). The report IS the
verdict, so the write cannot precede it, and it rides the verify you already run,
so it cannot be skipped by a verify that ran, and cannot be skipped SILENTLY at
all: the opt-out is a flag you type. Step 7's ordering still holds: the verify
finds no green report until conexus's STEP-6 has actually reported. The `commit`
provenance is resolved from the LIVE version's tag after the probe, never from
the floor tag (the floor is only a lower bound on what is running).

**The direct form is REQUIRED, not a fallback, whenever the floor legitimately
trails the newest published tag** — which is every engine cut after the first
since the last client release, because the pinned engine version moves only with
a client release (its pin derives from CHANGELOG's newest released section; the
bump itself is unconditional, one engine identity per release) while engine tags
keep shipping. `check_engine_release_floor.py`'s wrapper runs the
ENGINE PIN CHECK before the tracker write and fails closed on that state, so it
cannot record the deploy at all. Measured 2026-09-16: the v0.1.122 write went
through only because the floor had been bumped and not yet reverted; the v0.1.123
write was refused (`published v0.1.123 but this release pins v0.1.121`) and
recorded via the command below instead, which verifies against the LIVE
`/version` rather than the pin and has no pin check by construction. Use it
whenever the wrapper refuses on the pin; do not bump the floor to get past the
wrapper. Also the form for a verify you cannot run from the box that holds the
reports:

```
nx service record-deploy engine-service-vX.Y.Z --commit <sha> --gate-report-dir <conexus checkout>/deploy
```

Same selection, same refusals, same single writer. The verbatim
`--gate PASSED` form still exists and is exactly what it says: a hand-typed
claim recorded verbatim — use it only when there is genuinely no report, and
say so in the ship record.

To read what the cloud is running WITHOUT trusting the tracker, use the live
handshake directly: `nx service probe` (prints `release_version`). The tracker is
a cache; `/version` is truth.

So the next session (and the engine-freshness gate in the `release` skill) can see what the cloud is actually running without re-deriving it.

## Relationship to the PyPI release

The conexus PyPI release (the `release` skill) PINS one engine tag and gates on its cloud-validation (its Step 0 engine-freshness gate). This skill is what produces + validates the tag that gate pins. Run this whenever the engine drifts; run `release` only when shipping the Python package.
