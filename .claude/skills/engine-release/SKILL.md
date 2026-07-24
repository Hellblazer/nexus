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

### 5. POST-PUBLISH validation of the published artifact — ⚠️ CURRENTLY NO LEG

> **BROKEN, needs a decision. Do not skip silently; escalate to Hal.**
> This step used to be `run.sh --cold`, which cold-acquired the just-published
> binary on a bare box. `--cold` was RETIRED by RDR-155 P4b (commit `7e47c285`,
> 2026-07-24) along with `--guided` and `--hole-punch`, because it drove the
> deleted `nx guided-upgrade`. **There is currently no leg that acquires and
> validates an arbitrary just-published engine tag.**
>
> This matters: this is the gate that caught `nexus-pi3s3` + `nexus-qeoxf` on
> 2026-06-26 — defects in the PUBLISHED artifact that every local suite missed.
> Losing it silently is exactly the class that burned `engine-service-v0.1.53`
> (a release-only script nobody swept when the contract under it changed).
>
> Why the obvious candidate does NOT substitute: `--package-upgrade` does
> acquire the engine for real, but it converges to `NEW_ENGINE_TAG`, which
> `run.sh` DERIVES from `REQUIRED_ENGINE_VERSION` — the floor, not an arbitrary
> new tag. It therefore cannot validate a tag until the PyPI release has already
> pinned it, which is after the point this step exists to protect.
>
> OPTIONS for whoever resolves this (nexus-8nlj4 is the likely home, since it
> already owns the replacement acceptance rehearsal):
> 1. Teach `--package-upgrade` to honour an explicit `NEXUS_SERVICE_TAG`
>    override so it can acquire and converge to a specific published tag.
> 2. Build the two-hop stranded-redirect rehearsal (nexus-8nlj4) with a
>    published-artifact acquire leg, restoring this coverage by construction.
> 3. Accept the gap explicitly and record it — the cost is that a bad published
>    binary is first discovered by conexus at deploy time (Step 6) rather than
>    by us, which is a worse place to find it.
>
> Until one of those lands, Step 6's conexus cloud gate is the FIRST validation
> the published artifact receives. Say so out loud when handing the tag over.

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
- When the **next PyPI release** pins this engine: `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`) and — ONLY if the release hard-requires the new engine's features — `REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`; the floor is a minimum, not "latest"). These are the `release` skill's job, not this one.

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
