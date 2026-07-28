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
cd service && ./mvnw -q test
```

The Java CI (`service-ci.yml`) is **advisory** — it does not block auto-merge — so verify it actually passed on this tree rather than assuming.

### 3. PRE-TAG gate: `--shakeout` (the leg that builds the candidate)

> **`--guided` IS RETIRED — do not use it.** RDR-155 P4b (commit `7e47c285`,
> 2026-07-24) deleted `nx guided-upgrade` / `migrate-to-service` / `storage
> migrate all`, so `--guided`, `--cold` and `--hole-punch` now refuse at the
> arg loop with a RETIRED message and exit 2. This step named `--guided` for
> one cut after the retirement and would have failed the next engine cut at the
> gate. Surviving journeys: `--era-hop`, `--package-upgrade`, `--shakeout`,
> `--fullstack`, `--chash-window`, and the default `rehearse.sh` (Phases A/D/E).

> **Ordering, still load-bearing.** `--shakeout` BUILDS the candidate locally
> (`run.sh` does the GraalVM `-Ob` native build; only the retired `--cold` path
> skipped it and acquired a PUBLISHED binary instead). `--with-cloud` exercises
> the conexus-DEPLOYED service, so it cannot run pre-tag either — it is part of
> the post-deploy cloud gate (Step 6).

```bash
tests/e2e/migration-rehearsal/run.sh --shakeout
```

Must end `CANDIDATE SHAKEOUT PASSED`.

This is strictly stronger than the `--guided` gate it replaces. It performs the
same native-image build — the `-Ob` quick build has the SAME reachability
requirements as the full release build, so it catches a broken native build
before the tag burns a release-workflow run — and then adds the full CLI-verb
matrix, incremental index, and a zero-5xx-under-load assertion against that
binary. A FAIL here is a product finding, not a harness formality: its maiden
runs caught two production bugs the unit suites missed (nexus-h8rf6).

Notes:
- The host JVM suite (`cd service && ./mvnw -q test`, Step 2) validates the Java
  on the JVM; `--shakeout` adds the native-image build + serve + drive.
- **Do NOT use `release-sandbox.sh`** — it swaps the uv tool venv and can break
  the live install. The container rehearsal is the safe, isolated one.
- When the two-hop stranded-redirect rehearsal lands (nexus-8nlj4) it becomes
  the acceptance journey that replaced the retired guided legs; add it here
  then, alongside `--shakeout` rather than instead of it.

### 4. Push the tag (human, or AI when explicitly authorized)

Releaser is **human** by default (AI preps + validates); the human pushes the
tag, OR the AI pushes it when the human explicitly authorizes that cut.

```bash
git tag -a engine-service-vX.Y.Z -m "engine-service X.Y.Z" <commit>   # <commit> must be on origin
git push origin engine-service-vX.Y.Z
```

Tag-push fires `engine-service-release.yml` → builds + cosign-signs the 3 native binaries for the supported targets (`linux-amd64`, `linux-arm64`, `mac-arm64`) plus their PG bundles, and publishes the GitHub release. (Intel macOS / `mac-amd64` is NOT a supported target — not built.) Publishes nothing to PyPI. Wait for the workflow to finish publishing before Step 5 (prior runs ~30 min).

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

### 6. Relay deploy + post-deploy cloud validation to conexus (passive bus)

Deploy and cloud-validation are **conexus-side operations** — the bus is passive, so surface an explicit relay to Hal; never frame the cross-instance deploy as autonomous:

> relay: deploy `engine-service-vX.Y.Z` to `api.conexus-nexus.com` + re-run the cloud gate (recall + hybrid parity, xr7.8.9-style).

The post-deploy `--with-cloud` rehearsal (`run.sh --with-cloud`, the cloud → cloud Voyage journey) requires the candidate to be **deployed on conexus** first — it runs as part of this cloud-gate, once the deploy lands, not in Step 5. For cross-repo gate / deploy status, **read the authoritative bead + the conexus bus, not memory** — cross-repo state goes stale fast (2026-06-26: a `luxe6` condition had been cleared a week earlier than memory implied).

### 7. After conexus confirms deployed + cloud-gated green, bump downstream refs

- `tests/e2e/migration-rehearsal/run.sh` `COLD_TAG` default → the new published tag (or override via `NEXUS_SERVICE_TAG`).
- When the NEXT PyPI release bumps `REQUIRED_ENGINE_VERSION` to this tag, also rotate `run.sh`'s `NEXUS_PREV_RELEASE`/`NEXUS_PREV_ENGINE_TAG` defaults (the `--package-upgrade` convergence leg's starting point — must stay one release BEHIND the new dependency or its staleness guard fails loud; nexus-cfgo9). The `--package-upgrade` leg itself runs in the PyPI `release` skill's Step 1, not here — this skill only keeps its inputs fresh.
- `SchemaUpgradeRehearsalIntegrationTest.OLD_TAG` (`service/src/test/java/dev/nexus/service/`) → the PREVIOUSLY-deployed tag (nexus-7z6s7 rotation policy: the old→HEAD rehearsal's "real aged box" realism rots as the fleet moves on; re-verify the two structural preconditions documented on the constant when bumping) OLD_TAG rotation is a THREE-part edit (nexus-gm38i): regenerate the changeset snapshot (`uv run python scripts/gen_rehearsal_hop_manifest.py`), re-derive the new hop's row-DML seed coverage, and re-point the data leg's seeding + its SEED-COVERAGE block + the lint's `DECLARED_SEED_COVERAGE` together — `tests/test_rehearsal_seed_coverage_lint.py` fails loudly until all three agree.
- **`REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) MUST move to this tag** — unconditionally, not "only if the release needs the features". There is ONE engine identity per release: the engine it was built and gated with, on EVERY install path (Hal directive 2026-07-15, after the 14h GH #1402 incident). It is NOT a compatibility minimum. For local-mode installs this constant is the ONLY delivery vehicle — an engine tag that is cut, gated, and never pinned reaches nobody. `PINNED_SERVICE_TAG` is DERIVED from it, so the one edit moves both.
  Sequencing: the bump lands with the NEXT PyPI release, AFTER conexus deploys this tag (Step 6). Bumping before the deploy makes every cloud client refuse the managed service as below-identity — GH #1402 inverted. Until then the owed bump is tracked, not applied; `scripts/check_engine_release_floor.py` fails the release if a gated tag was never pinned.

### 8. Record state (T2) — guarded by a live /version read (DO NOT SKIP)

```
nx service record-deploy engine-service-vX.Y.Z --commit <sha> --gate PASSED
```

This GETs the deployed service's `/version`, ASSERTS `release_version == X.Y.Z`,
and only then writes the `deployed-engine-version` tracker. The recorded version
is machine-sourced from the live read — never hand-typed — so it cannot disagree
with what the cloud is actually running, and running it before the deploy lands
fails loud instead of recording a wrong fact (nexus-dz6b1 / RDR-179).

**Scope honesty:** this guards the *value* (no wrong version can be recorded); it
does NOT force the step to run. The original v0.1.17-stale-across-three-deploys
incident was an OMISSION, not a fat-finger — so this step is still skippable and
you must not skip it. Closing the omission vector for good (cloud-gate writes the
tracker on pass) is a tracked follow-up. `--commit`/`--gate` are verbatim
provenance, not verified against the deploy.

To read what the cloud is running WITHOUT trusting the tracker, use the live
handshake directly: `nx service probe` (prints `release_version`). The tracker is
a cache; `/version` is truth.

So the next session (and the engine-freshness gate in the `release` skill) can see what the cloud is actually running without re-deriving it.

## Relationship to the PyPI release

The conexus PyPI release (the `release` skill) PINS one engine tag and gates on its cloud-validation (its Step 0 engine-freshness gate). This skill is what produces + validates the tag that gate pins. Run this whenever the engine drifts; run `release` only when shipping the Python package.
