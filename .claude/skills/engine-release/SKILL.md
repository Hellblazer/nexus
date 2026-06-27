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

### 3. Validate the candidate with the 5x5 migration-rehearsal (REQUIRED)

The isolated container rehearsal exercises the three cloud-relevant journeys end to end. It is the gate that catches what unit tests miss (it found `nexus-pi3s3` + `nexus-qeoxf` on 2026-06-26). Fully isolated — provisions its own PG in-box, installs the wheel in-container, never touches `~/.config/nexus` or prod.

```bash
tests/e2e/migration-rehearsal/run.sh --cold        # green → local  (published-artifact bare-box install)
tests/e2e/migration-rehearsal/run.sh --guided      # local → local  (nx guided-upgrade, develop HEAD)
tests/e2e/migration-rehearsal/run.sh --with-cloud  # cloud → cloud  (Voyage leg; needs .env voyage key)
```

Each must end `SOUP-TO-NUTS REHEARSAL PASSED`. Notes:
- `--cold` and `--guided` force a fresh native build; `--with-cloud` accepts `--no-build` to reuse artifacts.
- The cloud leg bills Voyage (embeddings only — token cost, no prod data store touched).
- **Do NOT use `release-sandbox.sh`** — it swaps the uv tool venv and can break the live install. The container rehearsal is the safe, isolated one.

### 4. Human pushes the tag

```bash
git tag -a engine-service-vX.Y.Z -m "engine-service X.Y.Z" <commit>
git push origin engine-service-vX.Y.Z
```

Tag-push fires `engine-service-release.yml` → builds + cosign-signs the 4 native binaries (linux/mac × arm64/amd64) and publishes the GitHub release. Publishes nothing to PyPI.

### 5. Relay deploy + cloud-gate to conexus (passive bus)

Deploy and cloud-validation are **conexus-side operations** — the bus is passive, so surface an explicit relay to Hal; never frame the cross-instance deploy as autonomous:

> relay: deploy `engine-service-vX.Y.Z` to the cloud + re-run the cloud gate (recall + hybrid parity, xr7.8.9-style).

For cross-repo gate / deploy status, **read the authoritative bead + the conexus bus, not memory** — cross-repo state goes stale fast (2026-06-26: a `luxe6` condition had been cleared a week earlier than memory implied).

### 6. After conexus confirms deployed + cloud-gated green, bump downstream refs

- `tests/e2e/migration-rehearsal/run.sh` `COLD_TAG` default → the new published tag (or override via `NEXUS_SERVICE_TAG`).
- When the **next PyPI release** pins this engine: `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`) and — ONLY if the release hard-requires the new engine's features — `REQUIRED_RELEASE_VERSION` (`src/nexus/migration/guided_upgrade.py`; the floor is a minimum, not "latest"). These are the `release` skill's job, not this one.

### 7. Record state (T2)

```
nx memory put -p nexus -t deployed-engine-version "engine-service-vX.Y.Z @ <commit>; cloud-gated <date>; gate result <...>"
```

So the next session (and the engine-freshness gate in the `release` skill) can see what the cloud is actually running without re-deriving it.

## Relationship to the PyPI release

The conexus PyPI release (the `release` skill) PINS one engine tag and gates on its cloud-validation (its Step 0 engine-freshness gate). This skill is what produces + validates the tag that gate pins. Run this whenever the engine drifts; run `release` only when shipping the Python package.
