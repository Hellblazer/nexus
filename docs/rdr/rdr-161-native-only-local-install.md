---
title: "Native-Only Local Install: Expunge the JAR Launch Path, Acquire the Signed Native Binary and PG Bundle"
id: RDR-161
type: Architecture
status: accepted
accepted_date: 2026-06-18
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-18
related_issues: [nexus-luxe6, nexus-1jt17, nexus-1odsm]
related: [RDR-157, RDR-155, RDR-152, RDR-160]
---

# RDR-161: Native-Only Local Install

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-157 decided the service ships as a per-OS/arch **GraalVM native-image binary**
(RF-157-6/7, locked 2026-06-14) and that the local distribution ships a relocatable
**PG + pgvector bundle** alongside it (CA-1/CA-2). The native launch path is already
the runtime default: `StorageServiceSupervisor` prefers `binary_path` over the JAR
(`_launch_mode = "native"`), `_find_service_binary` resolves the well-known binary or
`NEXUS_SERVICE_BIN`, and `engine-service-release.yml` publishes cosign-signed
`nexus-service-{linux-amd64,linux-arm64,mac-arm64}` assets.

Three gaps stop a fresh local install from working end to end, and they are the
residue of the JAR-era design that RDR-157 superseded but did not finish removing.

#### Gap 1: The JAR launch path is now vestigial dead weight, still wired

`install-jar` (nexus-pebfx.4) solved the JAR *lifecycle* before the native decision
landed. Post-decision the JAR is the dev/JVM fallback only, but it is still load-bearing
in four modules: `daemon/storage_service_daemon.py` (`_find_service_jar`, the
`java -jar` spawn arm, the `_launch_mode` dual-mode and JVM-specific heartbeat
handling), `daemon/jar_lifecycle.py` (`well_known_jar_path`, `install_jar`, the
fat-JAR changelog validation), `commands/daemon.py` (`service install-jar`), and
`migration/pregate.py`. Keeping two launch artifacts means two acquisition stories,
two provenance shapes, and a JVM-fallback path that silently masks a missing native
binary. The decision is **native-only**: one launch artifact, one acquisition path.

#### Gap 2: No acquisition command places the published native binary

The runtime will launch a native binary sitting at `well_known_binary_path` or pointed
to by `NEXUS_SERVICE_BIN`, but **no code puts it there**. There is `service install-jar`
and no `service install-binary`; nothing fetches from the `engine-service-v*` release.
The signed assets (`.cosign.bundle`, `.sha256`) exist and are unverified at the point of
install. `nx init --service` does not place the binary either. Today a fresh user must
manually download, `chmod +x`, and set an env var. This is the **local** counterpart to
the cloud-only cosign verify in `nexus-1jt17` (conexus `deploy/engine`, docker path) —
which does **not** cover the local install.

#### Gap 3: The PG bundle is built and consumed but never published

`scripts/build_pg_bundle.sh` builds the relocatable `nexus-pg-<target>.txz` and
`db/pg_bundle.py` (RDR-157 P3.4) locates+extracts+selects it on first run. But
`engine-service-release.yml` publishes **only** the service binary; `build_pg_bundle.sh`
is referenced solely by `ci.yml` (build/smoke, no publish). So `_select_bundled_pg`
finds nothing on a fresh install → host-PG fallback → `PgBinaryNotFoundError` unless the
user hand-installed PG + pgvector (the brew-wrong-major friction RDR-157 documented).
The build and consume halves exist; the **publish + acquire** seam does not.

### Evidence

- Native launch is preferred and implemented: `storage_service_daemon.py`
  `_find_service_binary` / `_spawn_service` (`binary_path` wins over JAR).
- Release ships native + signatures, not the PG bundle: `engine-service-release.yml`
  matrix `linux-amd64 / linux-arm64 / mac-arm64`, cosign + sha256 per asset; no
  `nexus-pg-*` upload step.
- PG bundle build+consume exist: `scripts/build_pg_bundle.sh`, `src/nexus/db/pg_bundle.py`.
- `nexus-1jt17` is scoped to conexus docker deploy, explicitly "CONEXUS-REPO work";
  it does not carry the local `install-binary` fetch.

## Decision

**Native-only local install.** Remove the JAR launch and install paths entirely; make
the GraalVM native binary the single launch artifact. Add one verified acquisition
command, `nx daemon service install-binary`, that fetches the platform asset from the
`engine-service-v*` release and **signature-verifies it before placing it**, failing
closed on a missing/invalid signature or digest. Per RF-161-1 the verification mechanism
is **`sigstore-python`** (the PyPI `sigstore` package — a normal dependency, no 130 MB
cosign binary, offline-capable) consuming a **protobuf `.sigstore.json`** bundle, with
the cert identity pinned to `Hellblazer/nexus .github/workflows/engine-service-release.yml`
at an `engine-service-v*` ref and issuer `token.actions.githubusercontent.com`. This
requires a **publisher-side change** (emit `.sigstore.json` via `--new-bundle-format`),
so the verification contract is co-owned by Phase 1's publisher and consumer sub-tasks.
The command also **sha256-verifies**, places the binary at the well-known path with a
provenance sidecar, and is invoked by `nx init --service`. Publish the PG bundle on the
same release channel and acquire+verify it through the same seam so `_select_bundled_pg`
finds it.

**Platform scope now: mac-arm64 (Apple Silicon) + linux-amd64 + linux-arm64** — exactly
the current release matrix, so no matrix change is required for this RDR. **Windows
native is deferred** to a follow-on bead (build matrix + `.exe` asset + platform map +
the UNSMOKED caveat). mac-amd64 (Intel) is out of scope.

## Approach (phased)

### Phase 1 — Acquire the signed native binary (unblocks the install before removal)

- **Publisher sub-task (RF-161-1):** add `--new-bundle-format` to the `cosign sign-blob`
  step in `engine-service-release.yml` so each release also publishes a protobuf
  `nexus-service-<arch>.sigstore.json` alongside the existing `.cosign.bundle` and
  `.sha256`. This is the prerequisite that lets the consumer verify with `sigstore-python`
  and is part of Phase 1, not a later phase — the publisher and consumer halves land
  together or verification is non-functional.
- **Consumer command:** `nx daemon service install-binary TAG [--path PATH]`: TAG is an
  explicit `engine-service-v*` tag (RF-161-2 — no "latest" resolution in Phase 1).
  Resolve `platform → nexus-service-<arch>` via the existing `current_platform_tag()`
  (`pg_bundle.py:54`, same `<target>` tokens as the PG bundle); download the asset, its
  `.sigstore.json`, and `.sha256`; sha256-verify (fast fail on corrupt download), then
  **signature-verify with `sigstore-python`** pinning cert-identity-regexp to
  `Hellblazer/nexus .github/workflows/engine-service-release.yml@refs/tags/engine-service-v[0-9].*`
  and issuer `token.actions.githubusercontent.com`; **fail closed** on any failure with
  an actionable message (never a bare exception). Place at `well_known_binary_path`
  (atomic tmp + `os.replace`), `chmod +x`, write a provenance sidecar (mirroring
  `install_jar`'s: version-from-tag, sha256, source-url, installed_at).
- Wire into `nx init --service`: after PG provisioning, before `_start_service_step`,
  install-binary if no binary is resolvable (idempotent; a present valid binary is a
  no-op).

### Phase 2 — Publish + acquire the PG bundle

- Release job that runs `scripts/build_pg_bundle.sh` per target on its native runner,
  signs (`cosign sign-blob --new-bundle-format` → `.sigstore.json`, same format as the
  binary so the consumer's sigstore-python path is uniform) + sha256s, and uploads
  `nexus-pg-<target>.txz(+.sigstore.json +.sha256)` as release assets (alongside or
  sibling to `engine-service-release.yml`).
- Acquire+verify through the same install seam (extend `install-binary` or a parallel
  `install-pg-bundle`): fetch `.txz` + `.sigstore.json` + `.sha256`, sha256- then
  signature-verify (same sigstore-python pin as the binary), atomic-place at
  `<config_dir>/service/nexus-pg-<tag>.txz` with a provenance sidecar.
- **Bug fix (RF-161-3):** `_select_bundled_pg` (`init.py:396`) currently calls
  `ensure_pg_bundle` with no `search_dirs`, so the default `[Path(sys.executable).parent]`
  (the venv `bin/`) fires instead of `<config_dir>/service/` where the acquire seam
  places the `.txz` — a correctly published+acquired bundle is never found. Fix: pass
  `search_dirs=[well_known_binary_path(config_dir).parent]` when the caller supplies none
  (preserving the `NEXUS_PG_BUNDLE` override and the test-injectable param). Without this
  the install silently falls back to host-PG — the exact failure this RDR exists to kill.
- Add a **published-then-downloaded** bundle round-trip test (current relocation tests
  only exercise a locally-built bundle — RF-161-3 gap).

### Phase 3 — Expunge the JAR launch + install path (native-only)

- `storage_service_daemon.py`: delete `_find_service_jar` and the `java -jar` spawn arm;
  `StorageServiceSupervisor` requires `binary_path` (drop the `jar_path` param and the
  dual `_launch_mode`); collapse JVM-specific heartbeat handling to the native case.
- `jar_lifecycle.py`: remove `install_jar`, `well_known_jar_path`, fat-JAR changelog
  validation; move the surviving binary helpers (`well_known_binary_path`,
  `_find_service_binary` support) to a renamed `binary_lifecycle.py` so no JAR-named
  module remains.
- `commands/daemon.py`: delete `service install-jar`.
- `migration/pregate.py`: drop the JAR reference.
- Inverse-grep gate: `jar` is absent from `src/nexus` non-test launch/install surface
  (mirrors the exhaustive-surface-audit discipline).

### Phase 4 — Docs + deferred Windows bead

- Update install/runbook docs to the native-only one-command story.
- Windows-native is tracked by the existing `nexus-f9bgu` (RDR-157 release-N+1 follow-on:
  native-image + relocatable PG + windows pgvector `.dll`); update it with the
  install-binary/`.exe` platform-map + smoke-caveat scope rather than filing a new bead.
  mac-amd64 remains explicitly out of scope.

## Alternatives considered

- **Keep the JAR as a JVM fallback.** Rejected: two acquisition stories and a fallback
  that silently masks a missing/failed native binary; RDR-157 already chose native as
  the artifact. A dev who wants the JAR can still `mvn package` and point
  `NEXUS_SERVICE_BIN`-style at a local build behind a dev-only flag if ever needed —
  but it leaves the shipped install path.
- **Fold local cosign verify into `nexus-1jt17`.** Rejected: that bead is conexus-repo
  docker-deploy scope; the local CLI consumer is a distinct surface in this repo.
- **Embed PG in the binary (vs ship-alongside `.txz`).** Already decided ship-alongside
  in RDR-157 CA-2 (nexus-vwvv5.11); not reopened.

## Consequences

- One launch artifact, one verified acquisition path; `pip install` + `nx init --service`
  becomes a survivable local install for mac-arm64 + linux.
- Supply-chain verification (sigstore-python signature + sha256, fail-closed) at the
  point of install for both the binary and the PG bundle. Adds two deps: the `sigstore`
  PyPI package (consumer) and a `--new-bundle-format` flag (publisher).
- A dev who relied on `service install-jar` / `java -jar` loses it; mitigated by the
  native binary being the supported artifact and a possible dev-only local-build flag.
- Windows users remain unserved until the deferred bead; called out explicitly.

## Open Questions

- **RESOLVED (RF-161-2):** `install-binary` requires an **explicit `engine-service-v*`
  tag** in Phase 1; "latest" resolution is deferred (no gh-release helper exists, and an
  explicit tag avoids silent drift against the running schema, `/version` handshake from
  pebfx.4). A namespace filter is mandatory when "latest" later lands.
- One command for both artifacts (`install-binary` fetches binary + PG bundle) or two?
  Leaning one verified seam with a `--pg-bundle/--no-pg-bundle` toggle. (Early-Phase-2
  implementation decision, not a late refinement.)
- **RESOLVED (RF-161-1):** verification uses **`sigstore-python`** (PyPI `sigstore`
  package) consuming a publisher-emitted `.sigstore.json` — no 130 MB cosign binary, no
  vendored-static-cosign bootstrap, offline-capable. The former "cosign as runtime dep"
  question is closed in favor of the sigstore-python path.

## Research Findings

### RF-161-1 (CA-1, VERIFIED 2026-06-18, web + local): cosign verify on a clean host — FEASIBLE-WITH-CAVEATS, and it reshapes Phase 1

The publisher signs keyless via cosign **v2.4.1** (`cosign-installer@1aa8e0f` = v3.7.0):
`cosign sign-blob "$ASSET" --bundle "$ASSET.cosign.bundle"` with `id-token: write`
(`engine-service-release.yml:252-279`). The self-verify uses the exact ref identity; the
release notes (line 326) document the consumer regexp form.

Two load-bearing facts:

1. **The `.cosign.bundle` is the OLD JSON format → not fully offline-verifiable.** It
   carries the signature, Fulcio cert, and a Rekor SET (an *inclusion promise*, not a
   full Merkle proof). `cosign verify-blob --bundle` reaches `rekor.sigstore.dev:443` at
   verify time. `--offline` verifies only the SET (weaker posture); `--insecure-ignore-tlog`
   fails *open* (wrong direction). So a clean-host install needs outbound HTTPS to Rekor,
   OR the publisher emits the new protobuf bundle for true offline verify.
2. **`sigstore-python` (pure-Python, no 130 MB cosign binary) CANNOT consume the old
   JSON bundle** — it speaks the protobuf `.sigstore.json` format only. The cosign CLI is
   ~130 MB per platform and creates a bootstrap problem (need cosign to verify the
   binary; need to trust cosign).

**Design implication (new):** the low-friction consumer path for `pip install conexus`
users is to add `--new-bundle-format` to the publisher's `sign-blob` (cosign v2.4.1
supports it) so a `.sigstore.json` asset ships alongside `.cosign.bundle`; `install-binary`
then verifies with **sigstore-python** (a normal PyPI dep, offline-capable, pinned
cert-identity + issuer) and no 130 MB binary. This is a **publisher-side addition to
Phase 1**, not just a consumer command. Literal consumer pin (issuer is exact-match,
identity is regexp):
`--certificate-oidc-issuer https://token.actions.githubusercontent.com`,
`--certificate-identity-regexp 'https://github\.com/Hellblazer/nexus/\.github/workflows/engine-service-release\.yml@refs/tags/engine-service-v[0-9].*'`.
Failure modes (no network, tool absent, identity mismatch, tampered asset) all fail
**closed** except "tool not installed", which needs an explicit actionable message.
Stored: T2 `nexus/rdr-161-ca1-cosign-verification-research`.

### RF-161-2 (CA-2, VERIFIED 2026-06-18, local): asset-naming + tag-resolution contract is firm and reuses one platform helper

Per-platform release assets (`engine-service-release.yml:91-99,344-345`), three files
each: `nexus-service-<arch>`, `nexus-service-<arch>.sha256` (`<hex>  <name>`),
`nexus-service-<arch>.cosign.bundle`, for `<arch>` ∈ {`linux-amd64`,`linux-arm64`,
`mac-arm64`}. Release is non-draft/non-prerelease via `gh release create` on
`push: tags: engine-service-v*` only (a separate namespace from the PyPI `v*` tags;
`workflow_dispatch` uploads Actions artifacts only). Version = tag minus
`engine-service-v` (`:149-156`), stamped into the pom (`:169`); the binary self-reports
`app_version` (= tag suffix) at `/version` (`VersionHandler.java`, pebfx.4 handshake).
**Reusable platform helper:** `src/nexus/db/pg_bundle.py:54-72 current_platform_tag()`
emits the *same* `<target>` tokens the binary uses → `install-binary` builds the asset
name as `f"nexus-service-{current_platform_tag()}"`, one convention for binary + PG
bundle. No GitHub-release query helper exists (repo uses `urllib.request`, no `requests`);
**Phase 1 requires an explicit TAG** (defer "latest `engine-service-v*`" resolution to
avoid silent schema drift) with a mandatory `engine-service-v*` namespace filter.
Well-known dest: `well_known_binary_path(config_dir)` = `<config_dir>/service/nexus-service`.

### RF-161-3 (CA-3, VERIFIED 2026-06-18, local): PG-bundle round trip is sound; publish + a `_select_bundled_pg` search-dir bug are the only gaps

`scripts/build_pg_bundle.sh` builds PG 17.5 + pg_trgm + pgvector 0.8.2 from source into
a `bundle/` tree (`bin/include/lib/share` + a `.build_prefix` relocation marker), no
sign/checksum. `ci.yml` packages it to `nexus-pg-<target>.txz` (top-level `bundle/`) but
uploads it only as a **7-day Actions artifact** — **no release upload anywhere**
(`engine-service-release.yml` ships only the service binary). Consumer `db/pg_bundle.py`
expects exactly `nexus-pg-<current_platform_tag()>.txz`, extracts to
`<config_dir>/pg-bundle/bundle/bin`, validates the four binaries + `.build_prefix`,
idempotent via a `.nx_bundle_extracted` marker. Naming matches the build output exactly —
no drift. Relocation is proven per-target by `tests/db/test_pg_bundle_relocation.py`
(find_my_exec, glibc/minos floors, end-to-end `CREATE EXTENSION vector`).

**Real bug found:** `_select_bundled_pg` (`init.py:396`) calls `ensure_pg_bundle` with no
`search_dirs`, so the default `[Path(sys.executable).parent]` (the uv venv `bin/`) fires —
**not** `<config_dir>/service/` where the acquire seam must place the `.txz` next to the
binary. Fix: pass `search_dirs=[well_known_binary_path(config_dir).parent]` when the
caller supplies none (preserving the `NEXUS_PG_BUNDLE` override and test-injectable
param). Without it, a correctly-published+acquired bundle is never found.

**Gaps Phase 2 must close:** (a) a release publish job running `build_pg_bundle.sh` per
target → sign (same Sigstore/format decision as RF-161-1) + sha256 → `gh release upload`
to the `engine-service-v*` tag; (b) the client acquire seam (fetch → sha256 → signature
verify → atomic place at `<config_dir>/service/nexus-pg-<tag>.txz` + provenance sidecar);
(c) the `_select_bundled_pg` search-dir fix. No test exercises a *published-then-downloaded*
bundle (only locally-built) — Phase 2 should add one. linux-arm64 relocation deferred
(nexus-xqk5r).

### Implications for the Decision / Approach (FOLDED IN at gate, 2026-06-18)

These were folded into the Decision/Approach/Open-Questions bodies during the Layer-3
gate (substantive-critic, 2026-06-18); retained here as the audit trail:

- Phase 1 gained a **publisher-side** sub-task: emit a protobuf `.sigstore.json` bundle so
  the consumer verifies with sigstore-python (no 130 MB cosign dep, offline-capable).
  Open Question 3 resolved in favor of sigstore-python.
- Phase 1 **requires an explicit TAG** (no "latest" resolution yet); namespace filter
  added when "latest" later lands. Open Question 1 resolved.
- Phase 2 carries the `_select_bundled_pg` search-dir bug fix plus a published-bundle
  round-trip test, alongside the publish job + acquire seam.
- One platform convention (`current_platform_tag()`) spans binary + PG bundle.
