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

### 3. PRE-TAG gate: `--guided` ONLY (the only leg that builds the candidate)

> **Ordering, load-bearing (corrected 2026-06-29).** Of the three rehearsal
> legs, **only `--guided` builds the candidate binary locally** (a GraalVM
> `-Ob` native build of the current `service/` tree, `rm -f
> service/target/nexus-service` then rebuild). `--cold` and the `--with-cloud`
> cloud leg **ACQUIRE the *published* binary** — so before the tag exists they
> can only acquire the **previous** release and would validate the **old**
> engine as a "gate" for the new one. That is incoherent; do NOT run them
> pre-tag. The `--cold`/`--with-cloud` validation of the actual release artifact
> happens **post-publish** (Step 5), once the workflow has built + published it.

```bash
tests/e2e/migration-rehearsal/run.sh --guided      # local -Ob native build → nx guided-upgrade MVV
```

Must end `GUIDED-UPGRADE MVV PASSED`.

**Optional but recommended when the cut carries CLI-visible or concurrency-relevant service changes**: also run the candidate shakeout — the full CLI-verb matrix + incremental-index + concurrent-load journey against the SAME locally-built candidate (nexus-h8rf6; born from the 2026-07-03 post-release shakeout, whose findings were all locally discoverable):

```bash
tests/e2e/migration-rehearsal/run.sh --shakeout   # verb matrix + staleness + zero-5xx-under-load
```

Must end `CANDIDATE SHAKEOUT PASSED`. A FAIL here is a product finding, not a harness formality — its maiden runs caught two production bugs the unit suites missed. This proves the candidate `service/` tree
compiles + serves under GraalVM native-image (the `-Ob` build has the **same**
reachability requirements as the full release build, so it catches a broken
native build before the tag burns a release-workflow run). It is a *proxy* for
the published binary — the canonical native binaries are built by the workflow in
Step 4; the published artifact itself is validated in Step 5.

Notes:
- The host JVM suite (`cd service && ./mvnw -q test`, Step 2) validates the Java
  on the JVM; `--guided` adds the native-image build + serve.
- **Do NOT use `release-sandbox.sh`** — it swaps the uv tool venv and can break
  the live install. The container rehearsal is the safe, isolated one.

### 4. Push the tag (human, or AI when explicitly authorized)

Releaser is **human** by default (AI preps + validates); the human pushes the
tag, OR the AI pushes it when the human explicitly authorizes that cut.

```bash
git tag -a engine-service-vX.Y.Z -m "engine-service X.Y.Z" <commit>   # <commit> must be on origin
git push origin engine-service-vX.Y.Z
```

Tag-push fires `engine-service-release.yml` → builds + cosign-signs the 3 native binaries for the supported targets (`linux-amd64`, `linux-arm64`, `mac-arm64`) plus their PG bundles, and publishes the GitHub release. (Intel macOS / `mac-amd64` is NOT a supported target — not built.) Publishes nothing to PyPI. Wait for the workflow to finish publishing before Step 5 (prior runs ~30 min).

### 5. POST-PUBLISH validation: `--cold` against the new tag (REQUIRED)

Now the candidate is published, validate the **actual release artifact** by
acquiring it — this is the gate that catches what unit tests miss (it found
`nexus-pi3s3` + `nexus-qeoxf` on 2026-06-26). Fully isolated (own PG in-box,
wheel installed in-container, never touches `~/.config/nexus` or prod).

```bash
export NEXUS_SERVICE_TAG=engine-service-vX.Y.Z      # point the cold-acquire at the JUST-published candidate
tests/e2e/migration-rehearsal/run.sh --cold         # bare box → install-binary <new tag> → init → guided-upgrade
```

Must end `... MVV PASSED`. `--cold` rebuilds the wheel and cold-acquires the
published binary + PG bundle (it does **not** build a native binary). If it
fails, the published tag is bad — fix and cut a new patch tag (a published tag
is immutable; do not re-point it).

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
- When the **next PyPI release** pins this engine: `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`) and — ONLY if the release hard-requires the new engine's features — `REQUIRED_RELEASE_VERSION` (`src/nexus/migration/guided_upgrade.py`; the floor is a minimum, not "latest"). These are the `release` skill's job, not this one.

### 8. Record state (T2)

```
nx memory put -p nexus -t deployed-engine-version "engine-service-vX.Y.Z @ <commit>; cloud-gated <date>; gate result <...>"
```

So the next session (and the engine-freshness gate in the `release` skill) can see what the cloud is actually running without re-deriving it.

## Relationship to the PyPI release

The conexus PyPI release (the `release` skill) PINS one engine tag and gates on its cloud-validation (its Step 0 engine-freshness gate). This skill is what produces + validates the tag that gate pins. Run this whenever the engine drifts; run `release` only when shipping the Python package.
