---
title: "Containerize the Release Battery: Layered Images, a Compose-Expressed Gate DAG, and the macOS-Native Residue"
id: RDR-202
type: Architecture
status: draft
priority: high
author: Sam
created: 2026-09-04
reviewed-by: self
related_issues: [nexus-mfage, nexus-f2g8u, nexus-98zsp]
---

# RDR-202: Containerize the Release Battery

## Problem Statement

A conexus release costs about four hours of wall clock when nothing goes wrong, and the 7.30.0 cut on 2026-09-04 measured where that time goes. The pre-tag battery is a serial chain of gates, and the chain repeats work: the same tree is unit-tested four times (the local suite, the local-service gate's pytest leg, the PR CI on the release branch, and develop CI on the back-merge), the same wheel is built and installed six times (the fresh-install MVV, the sandbox smoke, the shakedown, the package-upgrade rehearsal, the gen-flip gate, and the post-publish MVV), and the same PG bundle, engine binary, and ONNX models are acquired or copied into a fresh HOME by nearly every leg.

Measured per-leg timings from the 7.27.0 and 7.30.0 batteries on this box (16 cores, 128 GB, warm caches):

| Leg | Wall time | What it uniquely proves |
|---|---|---|
| release-preflight (run twice) | 2 x 2:30 | seconds-scale blockers: engine floor, ledger, surfaces, required-check drift |
| unit suite, `pytest -n auto` | 10:54 | the Python tree against the in-process substrates |
| local-service-gate | 9:39 | a throwaway service boots and serves; the FLOOR/BUDGET family is non-vacuous |
| fresh-install-mvv | 1:56 (warm) to 8:00 (cold) | the virgin journey on a scrubbed HOME, engine-catalog registration, the generation install |
| gen-flip live-holder | 1:00 | a live nx-mcp survives a `current` flip; GC refuses the held tree |
| migration-rehearsal `--package-upgrade` | 9:49, and 25:00 when rerun | an existing install converges its engine to `REQUIRED_ENGINE_VERSION` with the harness forbidden from touching the binary |
| release-sandbox smoke | 1:47 | the reinstall is isolated from live holders |
| upgrade-shakeout | 0:44 | the upgrade path on a populated install |
| release-sandbox shakedown | 23:38 | MinerU end to end through `nx index pdf`, the RDR corpus indexed with bge-768, the nexus-98zsp throughput floor |
| PR CI on the release branch | 15:00 | the same tree, in the CI job shape |
| tag to publish | 2:00 | the OIDC publish |
| post-publish MVV and package-upgrade on published bytes | 30:00 | dependency resolution off PyPI, which no pre-tag leg can reproduce |

The gate group is 47 minutes serial, the whole pre-tag battery about 62, and the tail through publish and post-publish another hour. Two properties of the battery make it serial rather than one property. First, every E2E gate self-provisions into the same machine-wide waist: one bundled PG, one launchd unit, one `~/.config/nexus`, one port surface. Scrubbing HOME does not isolate the service install; only a container does (recorded in memory as project_home_does_not_isolate_launchd). Second, `pytest -n auto` saturates the box, and the gates' teardown paths (`nx daemon service stop --with-pg`, the engine-substrate orphan sweep) have machine-wide blast radius, so the unit suite cannot overlap the gates either. The standing rule that Docker gates never run concurrently with the local-service gate (feedback_acquire_gate_never_concurrent_with_lsg) exists because they share this waist, and the 7.30.0 battery paid for it again: the package-upgrade rehearsal failed on baseline provisioning under contention and passed only when rerun alone, at 25 minutes.

Sam's contention, stated 2026-09-04, is that Docker layers trivially remove most of this: every repeated acquisition is a deterministic function of pinned inputs, so it should be built once and reused as a layer, and the gates that share nothing but the waist should be expressed as a compose graph and run at once. This RDR takes that as the design and works out what it buys, what it does not, and what stays native on macOS.

## Relationship to Prior RDRs

- **RDR-178 (unattended migration) and the migration-rehearsal harness** established the pattern this RDR generalizes: `tests/e2e/migration-rehearsal/` already runs the package-upgrade, cold, era-hop, stranded, and full-stack journeys inside single ephemeral Debian containers where `nx init --service` provisions PG from the signed bundle, with no system PostgreSQL and no DinD. Its Dockerfiles are seven near-copies with no shared base and no layer reuse across journeys; every image re-installs Python, re-fetches the wheel's dependency graph, and re-acquires the engine. That is the redundancy this RDR removes, not a pattern it replaces.
- **RDR-197 (plugin-only release channel)** made the rule that release and cut machinery is rehearsed end to end against a fake origin in a container before the real run (`tests/e2e/plugin-cut-rehearsal/run.sh`, container by default). RDR-202 extends that posture from the cut machinery to the product-proving battery.
- **RDR-157 and RDR-161 (distribution and native-only install)** fixed the artifacts the battery proves: the wheel, the signed PG bundle, the native engine binary, the bundled ONNX models. Those are exactly the version-pinned, deterministic inputs that make layer caching sound. RDR-161's no-JRE posture is why the engine layer is a single binary and not a runtime.
- **RDR-112 (storage as service behind a container boundary, abandoned)** proposed containers as the product's runtime boundary. This RDR does not revisit that; containers here are the test harness's isolation boundary only. The product stays a native install, and the gates that prove the native install shape stay native.
- **RDR-184 (background teammate ledger)** matters for the driver: a parallel gate group is several processes producing interleaved output, and nexus-f2g8u (a gate that exited non-zero with no error text) shows the battery's diagnosability is already marginal. Concurrency without per-leg attribution makes that class worse. The driver design below carries that constraint.
- **The CI cost discipline (AGENTS.md § CI Cost Discipline, PRs #1375/#1376)** already states "never test the same tree twice" and "never rebuild deterministic artifacts" for GitHub Actions. This RDR applies the same two rules to the local battery, where they have not been applied.

## Context

What the battery repeats, and why each repetition exists today:

- **Unit tests, four times.** The local `pytest -n auto` run, the pytest leg inside the local-service gate (which does not exercise the gate's throwaway service; the autouse fixtures route tests at self-provisioned substrates, so it is the same suite again), the release-branch PR CI, and develop CI on the back-merge. Only the CI shape adds information (the census gate is the CI-job shape; an `-n auto` red is real signal first). The local-service gate's pytest leg carries one thing the plain suite does not: the FLOOR/BUDGET non-vacuity assert at the end.
- **Wheel build and install, six times.** Each gate builds `dist/*.whl` from the working tree and installs it into a fresh venv or generation. The wheel is a deterministic function of the tree, and the dependency resolution is a deterministic function of `uv.lock`, so five of the six installs resolve identical graphs. The sixth, the post-publish MVV, is the one that legitimately differs: it resolves off PyPI, which is the only way to catch a nexus-l2ku5 class defect (an unbounded pin resolving a breaking upstream release).
- **PG bundle, engine binary, ONNX models, per gate.** Each virgin HOME acquires them. `FRESH_MVV_CACHE` reuses the 416 MB model download; nothing reuses the bundle or the engine across gates, and the migration-rehearsal images re-fetch the previous release's engine from GitHub on every build.
- **MinerU, once but cold.** The shakedown cold-installs MinerU and its model weights and is the only gate that exercises `nx index pdf` through the production path. It is the longest leg and the parallel floor.

What cannot be layered:

- **The generation install layout and the shim.** `install_generation.sh`, `<tools>/current`, and the gen-flip gate prove the native install shape on the operator's platform. A Linux container proves the Linux shape, which the user base does not run.
- **launchd.** The sandbox smoke's isolation claim is about the live macOS service unit and live MCP holders. There is no launchd in a container.
- **macOS-specific wheels.** torch on macOS resolves different wheels than the Linux CPU index (nexus-mt1tj), so the Linux layer cannot stand in for the macOS install.
- **Published bytes.** Nothing pre-tag can install what is not on PyPI yet.

## Research Findings

**F1. The layer stack is four layers deep and every layer keys on a pinned input.**

| Layer | Inputs (cache key) | Contents | Rebuild trigger |
|---|---|---|---|
| L0 base | Debian tag, apt package list | python3, venv, git, curl, ca-certificates, the non-root `nexus` user | rarely |
| L1 deps | `uv.lock`, `pyproject.toml` dependency tables, the pytorch-cpu index pin | the full dependency graph installed from the lock, no conexus | a dependency change |
| L2 artifacts | `REQUIRED_ENGINE_VERSION`, the PG bundle version pin, the ONNX model manifest | the engine binary (sha256 and cosign verified), the PG bundle, bge-768 and MiniLM models, MinerU weights | an engine or bundle bump |
| L3 wheel | the working tree | `dist/conexus-*.whl` installed on top of L1 | every commit |

L1 is the expensive one and the one that changes least. On the 7.30.0 cut the dependency tables changed (mt1tj, jpsn1, heykz), which is the case where L1 rebuilds and is also exactly the case where a fresh resolution proves something. L2 keyed on the engine pin is what makes "never rebuild deterministic artifacts" hold locally: an engine bump invalidates one layer, not six downloads. L3 is a wheel install into an already-resolved environment, which `uv pip install --no-deps` does in seconds.

The migration-rehearsal images need a fifth shape: a baseline image at the previous release's published wheel and engine (`NEXUS_PREV_RELEASE`, `PREV_ENGINE_TAG`). That is L0 plus a published-bytes install, keyed on the previous release pin, and it changes only when the previous release changes. Today it is rebuilt on every rehearsal.

**F2. The gate DAG has one shared root and five leaves; the leaves share nothing but the waist.**

```
preflight ──┬── unit suite (native or container)
            ├── local-service-gate        (container: own PG, own service, own config dir)
            ├── fresh-install-mvv         (container, Linux shape)   ┐
            ├── package-upgrade rehearsal (container, already)       │ leaves; read-only on L3
            ├── upgrade-shakeout          (container)                │
            └── shakedown                 (container; the floor)     ┘
gen-flip, sandbox smoke, one fresh-install-mvv: native macOS, serial, after the group
```

Each leaf takes L3 as an image and provisions into its own filesystem, its own PG port, its own config dir, by construction. The concurrency rule then has nothing to protect. The unit suite can overlap the group once the group no longer runs a machine-wide teardown, which was the bead's fix A; in a container the teardown is the container's own.

**F3. The wall-clock floor is the shakedown, and layering shortens it more than parallelism does.**

Shakedown at 23:38 is the parallel floor whatever the fan-out, so containerizing the other five gates buys the difference between 47 minutes serial and about 24 minutes, with CPU contention pushing the realistic group toward 28. The shakedown itself decomposes: MinerU cold install and weight download (a layer, L2), the RDR corpus index with bge-768 (real work, the nexus-98zsp throughput gate, and the sandbox engine held about 5.8 cores through it), the PDF path through MinerU (real work, minutes), and the smoke steps. With MinerU in L2 the leg drops by the install time; the index and PDF legs stay because they are the product being measured. The bead's note that MinerU and RDR-index are separable holds: they can be two leaves off the same L3 rather than one serial leg, at the cost of the RDR corpus contending with MinerU's CPU. The revised group floor is then roughly the longer of the two halves plus provisioning.

**F4. Repetition removed, incident by incident.** Each repetition in the battery was added after an incident, so removing one needs the incident named and the replacement proof stated.

| Repetition removed | Incident that added it | What now carries the proof |
|---|---|---|
| local unit suite before the gates | routine | the container unit leaf, same command, overlapped with the group |
| the pytest leg inside local-service-gate | nexus-edwlp, nexus-x81ks: the 74/516 ambient degradation | the leaf keeps the direct smoke leg and the FLOOR/BUDGET vacuity assert; the assert runs against the unit leaf's junit output rather than a second run |
| release-branch PR CI re-running the suite | the tree changed at the bump | the release branch is develop plus manifests and changelog; CI runs the surface-parity and ledger jobs, and the suite only on a path filter that excludes the seven version surfaces and changelogs (nexus-jndz0's job-level skip pattern, with the non-vacuity assert) |
| develop CI on the back-merge | the 2026-07-23 drift incident | the back-merge is a fast-forward by construction after a merge commit release; CI on a tree whose SHA already carried green is the "same tree twice" the CI discipline forbids, so the workflow's concurrency and path filters skip it |
| five wheel builds | none; each gate was written standalone | one `uv build` into L3; every leaf reads the same image |
| per-gate engine and bundle acquisition | nexus-yv5m4 (GH #1381): a baked-in host PG masked the bundle acquisition path | L2 holds the bundle bytes; the fresh-install leaf still runs `nx init --service` against them, so acquisition and initdb are exercised; only the download is skipped, and the sha256 check still runs on the cached bytes |
| MinerU cold install in the shakedown | nexus-6xkdu: the only end-to-end MinerU path | L2 holds MinerU and weights; the `nx index pdf` step still runs |

Not removed: the post-publish MVV against PyPI (nexus-l2ku5), the gen-flip gate (nexus-utpuw.17, nexus-q3xrx), the sandbox smoke (137d2688 isolation), one native fresh-install MVV for the macOS wheel shape (nexus-mt1tj made the Linux and macOS torch graphs differ), and the package-upgrade rehearsal's ban on the harness supplying the engine (GH #1402), which the L2 layer must respect by mounting the cache into the baseline image's acquisition path read-only rather than pre-placing the binary.

**F5. Diagnosability is a precondition, not a follow-up.** nexus-f2g8u is a gate that exited non-zero with no reason. The preflight's empty-detail red was the same class (fixed in 46ada7885). Six concurrent leaves interleaving on one terminal turn a silent death into an unattributable one. The driver therefore writes per-leaf logs, per-leaf start and end stamps, and a verdict line per leaf, and the composite verdict names the failing leaf and its last twenty lines. The non-vacuity condition on concurrency itself is the overlap assert the bead already states: the driver fails if no two leaves' intervals overlap.

**F6. The macOS-native residue is small and stays serial.** gen-flip (1:00), sandbox smoke (1:47), and one native fresh-install MVV (1:56 warm) total under five minutes and share the waist, so they run serially after the group. The generation install and launchd are what they prove; there is no container that proves them.

## Proposed Solution

Build the four-layer image stack under `tests/e2e/battery/` with one Dockerfile using multi-stage targets (`base`, `deps`, `artifacts`, `wheel`) and BuildKit cache mounts, keyed as in F1. Rewrite the migration-rehearsal Dockerfiles as targets off the same base rather than seven copies, with the baseline image as a fifth target keyed on the previous release pin. Express the gate group as a compose file whose services are the leaves in F2, each with its own volume for `NEXUS_CONFIG_DIR` and PG data, each running the existing gate script unchanged where the script already tolerates a container (migration-rehearsal does; local-service-gate, upgrade-shakeout, and release-sandbox need their launchd and HOME assumptions parameterized behind the config-dir and service-endpoint variables they already read). A driver, `tests/e2e/battery/run.sh`, runs preflight, builds L3 once, brings the compose group up, overlaps the unit leaf, collects per-leaf logs and stamps (F5), then runs the native residue serially (F6) and prints a wall-clock total and a per-leg table so the timing table in this RDR maintains itself.

Retire the two CI repetitions by policy (F4 rows three and four) using the existing job-level skip pattern with non-vacuity asserts. Rewrite feedback_acquire_gate_never_concurrent_with_lsg to name this RDR and nexus-mfage as what made it obsolete for containerized leaves; the rule still binds any leg that runs native.

Target after this lands, on the measured basis: preflight 2.5, then the longer of the unit leaf (10.9) and the gate group (24 to 28, floor set by the shakedown's index and PDF work), then the native residue (5), for a pre-tag battery of about 32 to 36 minutes against 62 today. Shakedown layering and splitting (F3) is the only lever below that.

## Alternatives Considered

- **Schedule the existing native gates concurrently.** Rejected; they share the waist, and scrubbing HOME does not isolate the service install. The 7.30.0 rehearsal red under contention is the measurement.
- **Skip gates on judgment.** This is what the four-hour grind pressures a releaser into, and the release-skill history is a list of gates that were skipped and then paid for. The design removes repetition, not proof.
- **Move the battery to CI runners.** The gates need the engine, the bundle, MinerU, and 16 cores; macOS runners cost ten times Linux and the CI discipline forbids them outside artifact builds. The local box is the right place; the fix is that the local box was doing serial work.
- **Containerize everything, including the native residue.** Rejected; the residue's proofs are about macOS install shape and launchd, which a container cannot state.

## Trade-offs

Layer caching trades a download for trust in the cache key. Each key is a pinned input already checked elsewhere (`REQUIRED_ENGINE_VERSION`, `uv.lock`, the bundle pin), and the sha256 and cosign checks still run on cached bytes, so the trust is the same trust the product already places in those pins. The Linux leaves prove Linux; the macOS residue is kept for that reason, and the post-publish MVV stays native and off PyPI. Concurrency costs CPU contention, which is why the target says 24 to 28 rather than 24. A shared base image is one more thing that drifts; the migration-rehearsal images have already drifted seven ways without one.

## Implementation Plan

1. **Diagnosability first (F5).** Per-leg logs, stamps, and verdict lines in the existing serial driver, and the overlap assert ready but inert. Closes nexus-f2g8u's class before any fan-out.
2. **The image stack (F1).** One Dockerfile with four targets and BuildKit cache mounts; the migration-rehearsal Dockerfiles become targets of it; the baseline image becomes the fifth target. Measured: rebuild time at each layer for each trigger.
3. **Parameterize the three native gates** (local-service-gate, upgrade-shakeout, release-sandbox smoke and shakedown) behind the config-dir and endpoint variables so they run unchanged in a leaf.
4. **The compose group and driver.** Leaves as services, own volumes, the unit leaf overlapped, the native residue serial after. Acceptance is the bead's: two leaves concurrent with neither observing the other's state, the overlap assert, a wall-clock total.
5. **Shakedown split (F3).** MinerU and weights into L2; the PDF leg and the RDR-index leg as two leaves.
6. **CI policy (F4).** Path-filtered suite on release branches and skipped back-merge runs, both with non-vacuity asserts.
7. **Retire the concurrency rule** for containerized leaves, naming this RDR.

## Test Plan

- The overlap assert fails a run in which no two leaves overlapped, and passes a run in which they did (the non-vacuity of the concurrency claim).
- Two leaves run at once with different `NEXUS_CONFIG_DIR` and PG ports; each asserts the other's config dir and port are absent from its own view.
- Each layer's cache key is proven by changing its input and asserting the layer rebuilds, and by changing nothing and asserting it does not.
- The package-upgrade leaf still fails when the harness pre-places an engine binary (the GH #1402 guard survives the cache).
- The per-leaf verdict names the failing leaf for an injected failure in one leaf while the others pass.

## Validation

The 7.31.0 or later battery runs through the driver end to end and prints the per-leg table. Compare against the table in the Problem Statement; the RDR closes when the pre-tag battery lands in the 32 to 36 minute range with every gate's discriminating assertion still exercised, or the measured floor is recorded and explained.

## Finalization Gate

Full unit suite and the lint bucket green, the battery driver green on a real release cut, feedback_acquire_gate_never_concurrent_with_lsg rewritten, nexus-mfage and nexus-f2g8u closed with the measured table on the bead.

## References

- nexus-mfage: origin bead, with the measured 7.27.0 per-leg timings and acceptance criteria.
- nexus-f2g8u: a gate that exits non-zero with no error text.
- nexus-98zsp: the indexing throughput gate inside the shakedown.
- `tests/e2e/migration-rehearsal/`: the container pattern this RDR generalizes.
- AGENTS.md § CI Cost Discipline; § Cutting a release.
- RDR-157, RDR-161, RDR-178, RDR-197, RDR-112.
