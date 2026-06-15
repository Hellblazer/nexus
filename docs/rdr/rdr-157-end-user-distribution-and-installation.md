---
title: "End-User Distribution and Installation: Native-Image Binaries, Embedded PG16+pgvector, Two-Distribution Model"
id: RDR-157
type: Architecture
status: accepted
accepted_date: 2026-06-14
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-11
related_issues: [nexus-luxe6, nexus-pebfx, nexus-jdpn9, nexus-ykrhb, nexus-lqb9j, nexus-6laob]
related: [RDR-144, RDR-152, RDR-155, RDR-076]
---

# RDR-157: End-User Distribution and Installation

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Since RDR-155 P4a, T3 serving routes exclusively through the PG16 + pgvector + Java
nexus-service stack. `pip install conexus` (or the plugin marketplace install) delivers
none of that stack. A release cut from develop today requires every user to
hand-assemble it. The gaps below are framed in their **post-decision** form (native
binary default + two distributions + embedded PG, locked 2026-06-14); the original
prerequisite framing (JAR channel, JRE) is preserved in the Decision section's research
trail.

#### Gap 1: No per-OS/arch distribution channel for the service binary

The service has **no distribution channel**. Today users need a repo checkout and a
Maven (or native-image) build. `nx daemon service install-jar` (nexus-pebfx.4) solved
the *local lifecycle* (well-known location, sha256 provenance sidecar, schema-skew
gate) but not *acquisition*. With the native-image decision (RF-157-6/7), the artifact
is a per-OS/arch native binary published via a release channel, not a 134 MB fat JAR —
but that channel and its per-platform build matrix are undesigned. (This also retires
the former Java-runtime prerequisite: a native binary needs no JRE.)

#### Gap 2: The local distribution has no PostgreSQL 16 + pgvector to provision from

`discover_pg_binaries()` assumes a host PG (homebrew `postgresql@16`); the pgvector
brew formula targeting the wrong PG major was empirically hit on the dev machine
(2026-06-09), and the Debian/Ubuntu socket failure (nexus-6laob, fixed 2026-06-14)
showed host-PG provisioning is itself fragile per-platform. `nx init --service`
provisions a cluster from *found* binaries but does not get the binaries there. The
locked decision is to **embed a relocatable PG16 + pgvector** in the local
distribution (CA-1/CA-2 verified feasible); the bundle build matrix and the first-run
extract→provision wiring are undesigned.

#### Gap 3: The cloud distribution has no skip-provision + validate-remote path

The cloud distribution ships the native binary with **no DB**: it must connect to a
managed PG that already has pgvector, skip `pg_provision` entirely, validate the remote
(pgvector present + supported major, fail loud), run Liquibase, and serve. That
cloud-mode split does not exist today (`nx init --service` always provisions locally).

#### Gap 4 (resolved prerequisites, tracked for completeness)

**Java runtime** — eliminated by the native-image decision. **Voyage key plumbing** —
already solved (nexus-pebfx.2: credential-chain resolution, fail-loud ONNX refusal of
voyage-token collections); local-only mode works with the bundled ONNX MiniLM; the
embedder choice is an onboarding question (RDR-144), not a packaging one.

This is the substance of release blocker `nexus-luxe6`: Gaps 1–3 are the undesigned
legs. The operability of an *assembled* stack is largely done
(epic nexus-pebfx, 8/9: endpoint discovery via registry lease, status surface,
JAR lifecycle, embedding fail-loud, ETL operability, migration runbook, supervisor
PG-recovery, daemon observability). The *migration engine* is production-proven
(2026-06-11 run: `total_failed == 0`, T2 `nexus_rdr/153-production-t2-migration-complete`),
but its user-facing orchestration (`nx upgrade` detecting Chroma/SQLite data and
walking the cutover) is conexus RDR-001 scope, out of scope here except as a consumer
of this RDR's install primitives.

### Evidence

- 5 distinct first-run failures on the developer's own machine, ~40 min of expert
  diagnosis (nexus-jdpn9, 2026-06-10) — a normal user dead-ends.
- 4 supervisor silent-deaths in 48h before observability landed (nexus-ovbr7) — an
  end user has no `mvn package` recourse when the JAR is missing or stale.
- The 134 MB JAR exceeds PyPI's default limits and dwarfs the 'conexus' wheel itself.

## Decision — UPDATED 2026-06-14 (owner-locked; supersedes the draft options below)

Two prerequisites that gated the draft options are now resolved, so the owner has
locked the architecture:

- **Native-image is feasible and shipped.** The draft's Open Question 1 / RF-157-4
  risk (ONNX + DJL JNI under native-image) was retired by the native-image
  productionization (epic `nexus-ykrhb`, PR #1172, merged 2026-06-12): the full
  service — Liquibase migration + jOOQ + ONNX/DJL embedding — runs as a native
  binary, with a CI native-build+smoke job green on linux-amd64 and per-OS/arch
  `native-libs-*` profiles wired. The native-vs-jlink default decision
  (`nexus-lqb9j`) is closed in favour of native.
- **TCP-only provisioning is portable.** The Debian/Ubuntu Unix-socket bug
  (`nexus-6laob`, merged 2026-06-14, found by `tests/e2e/migration-rehearsal`) is
  fixed, so a provisioned cluster comes up for a non-`postgres` OS user.

**Locked decisions:**

1. **Artifact = GraalVM native-image (draft option 1(b) promoted).** The JAR channel
   (1(a)) and jlink (1(c)) are rejected as the shipping default. This eliminates the
   Java-runtime prerequisite (draft Decision 2) outright.
2. **Two distributions:**
   - **Cloud** — native binary, **no DB**. Connects to a managed PG that already has
     pgvector. Cloud mode skips `pg_provision` entirely; it validates the remote
     (pgvector present + supported major, fail loud), runs Liquibase, and serves.
   - **Local** (per OS/arch) — native binary **+ embedded relocatable PG16+pgvector**.
     "No reason not to embed." PG stays a separate process (it cannot be linked into
     the native executable — it is a multi-process C server); "embedded" means the
     relocatable PG+pgvector is carried in the distribution — ideally as an
     `-H:IncludeResources` payload inside the native binary, self-extracted to a cache
     dir on first run (the same mechanism the binary already uses for the ONNX/DJL
     native libs), with ship-alongside-in-the-archive as the fallback if in-binary
     embedding proves too heavy. First run: extract → `initdb` → provision → serve.
3. **PG provisioning = embedded bundle (draft option 3(a) promoted, 3(b) becomes the
   fallback).** `pg_provision` already has the seam: point `NEXUS_PG_BIN` (or a new
   candidate dir) at the extracted bundle; `check_pgvector_available` is satisfied by
   bundling `vector.control` + `vector--*.sql` + `vector.{so,dylib,dll}` in the
   relocatable PG's sharedir/pkglibdir. The PG build must be relocatable so
   `pg_config --sharedir` resolves from the cache dir.

**Critical assumptions to verify in research (P0):**
- **CA-1**: relocatable PG16 **+ matching pgvector** builds are obtainable (or
  buildable in CI) for every target — {linux-amd64, linux-aarch64, mac-arm64,
  windows}. (mac-x64 is explicitly out of scope — owner call 2026-06-14.)
- **CA-2**: a PG tarball embedded via `-H:IncludeResources` in the native image
  self-extracts and runs on first launch (size/startup cost acceptable; else
  ship-alongside).
- **CA-3**: `pg_provision`'s `NEXUS_PG_BIN` seam + `check_pgvector_available` work
  unchanged against the extracted relocatable bundle (TCP-only; socket fix applies).

The original draft option enumeration is retained below as the research trail.

## Decision (draft — options enumerated, to be resolved in research)

1. **JAR acquisition channel.** Candidates:
   - **(a) GitHub Releases asset per tag + `nx daemon service install-jar --from-release [vX.Y.Z]`.**
     A downloader is a small extension of the existing install-jar machinery: the
     provenance sidecar already records sha256/version/changesets; the release workflow
     already fires on tag push (OIDC PyPI publish) and can attach the Maven artifact.
     Pinned-source philosophy carries over: the release tag is the immutable channel.
     Default-lean candidate.
   - **(b) GraalVM native-image per-platform binaries.** CI already builds on GraalVM.
     Eliminates prerequisite (2) entirely (no JRE), likely shrinks the artifact, but
     adds per-platform build matrix (darwin-arm64/x86_64, linux), native-image
     compatibility work (jOOQ/Liquibase/Hikari reflection configs), and a second build
     system to keep green. Candidate for a later release, not the gating one.
   - **(c) jlink-trimmed bundled JRE.** Middle ground: one artifact per platform with
     a private runtime, no native-image compatibility risk. Larger than (b).
   - PyPI sdist/wheel embedding and Maven Central are rejected outright (size limits /
     wrong audience).

2. **Java runtime prerequisite.** If 1(a) ships first, the JDK stays a documented
   prerequisite enforced by `nx doctor` + `init --service` preflight (with platform
   install hints). 1(b)/(c) retire it. The preflight must fail loud with the exact
   remedy, never a stack trace.

3. **PG16 + pgvector provisioning.** Candidates:
   - **(a) Embedded-postgres wheels** (`pgserver`-style: PG binaries + pgvector inside
     a pip dependency). True zero-step; adds a heavyweight dependency and a second
     PG lineage to maintain; pgvector inclusion must be verified per platform.
   - **(b) Guided preflight provisioning** (RDR-144 onboarding pattern): detect
     binaries + extension availability, drive the platform package manager
     (brew/apt) with explicit user consent, then provision. Lighter; keeps the system
     PG lineage; the wrong-major pgvector formula problem must be detected and
     explained, not just hit.
   - **(c) Docker/compose path.** Rejected as the default (heavy prerequisite, fights
     the local-first design); possibly documented as an alternative for server installs.
   - Either way, `init --service` gains `CREATE EXTENSION IF NOT EXISTS vector`
     wiring and an extension-availability preflight (nexus-jdpn9 item 3).

4. **`nx init --service` collapse.** One command from fresh install to serving:
   preflight (java, PG binaries, pgvector, disk) → provision cluster → install JAR
   (from release channel) → start supervisor → status green. Each step idempotent and
   individually re-runnable; the epic's existing pieces (install-jar, status surface,
   lease discovery) are the building blocks. Failure at any step names the remedy.
5. **Sequencing with the deprecation window** (locked by nexus-luxe6): release N
   ships this RDR's install path + both storage paths + the migration tool;
   release N+1 ships RDR-155 P4b Chroma deletion. This RDR gates release N.

## Approach (phased)

Reconciled with the 2026-06-14 locked Decision (native-image default, two
distributions, embedded PG). **P0 research is complete** (RF-157-1…8); the phases below
are the build-out. Release N targets {linux-amd64, linux-aarch64, mac-arm64}; Windows
is release N+1.

1. **P0 — research (DONE).** Native-image feasibility (RF-157-6, executed spike),
   distribution/licensing (RF-157-5), CA-1 relocatable PG+pgvector (RF-157-7), CA-2
   in-binary embed + fallback (RF-157-8). Decision locked.
2. **P1 — CA-3 live test (gate before build-out).** Extract a real zonky PG16 bundle
   for one linux target, inject a CI-built pgvector `.so` (matching PG16 minor +
   glibc), set `NEXUS_PG_BIN` at it, run `pg_provision` + `check_pgvector_available`,
   assert: cluster starts, `pg_config --sharedir` resolves from the extracted dir, and
   `CREATE EXTENSION vector` succeeds. Record as **RF-157-9** (VERIFIED or FAILED +
   fallback). Pin the **minimum glibc** per linux target to zonky's baseline as an
   explicit go/no-go (else `dlopen` of `vector.so` fails on older distros at extension
   load, not at preflight). If FAILED, fall back to Strategy B (build PG from source).
3. **P2 — native-image build matrix.** Per-platform GitHub Actions runners
   (linux-amd64, linux-aarch64, mac-arm64) producing the native binary; release-workflow
   attaches each as a GitHub Releases asset (RF-157-5: < 2 GiB, unconstrained).
   Parity discipline extends to the native binary's version stamp. Supply-chain: ship a
   sha256 manifest for release N; evaluate Sigstore/cosign signing for N+1 (the binary
   runs with DB credentials and is opaque to static inspection — see Open Q4).
4. **P3 — embedded-PG bundle + the two distributions.**
   - **Bundle build (per OS/arch):** Strategy A (zonky PG16 + CI-built pgvector injected
     into the bundle `pkglibdir`/`sharedir`), or Strategy B if CA-3 forced it. Smoke:
     extract → `initdb` → `CREATE EXTENSION vector` in CI.
   - **Embed vs ship-alongside (CA-2):** spike `-H:IncludeResources` of the bundle +
     first-run self-extract; measure build cost + binary bloat + extract latency. Fall
     back to ship-alongside (`{binary, pg-<plat>.txz}` archive) if unacceptable.
   - **Cloud distribution:** native binary, no DB. A cloud mode skips `pg_provision`,
     points at the managed `NX_DB_URL`, and **validates the remote**: pgvector present
     AND version ≥ the floor RDR-155 requires (0.8+ for `iterative_scan`), fail loud
     with the remedy.
   - **Local distribution:** native binary + embedded/bundled PG; first run extract →
     `initdb` → provision (the socket fix nexus-6laob applies) → serve.
5. **P4 — one-command collapse + fresh-machine E2E.** `nx init --service` end-to-end,
   idempotent. Release-sandbox E2E proves **fresh-machine → serving with zero manual
   steps**, bounded to: (a) **local mode** requires the bundled ONNX MiniLM model —
   depends on `nexus-jrrve` (model fetch on service install) being closed, OR the model
   pre-positioned in the bundle; (b) **cloud mode** requires a pre-supplied Voyage
   credential. The E2E names which mode it exercises; `nexus-jrrve` is a declared P4
   dependency, not an orthogonal nicety.
6. **P5 — handoff to conexus RDR-001.** The upgrade-orchestration consumes P1–P4
   primitives; not implemented here.

## Alternatives considered

- **Ship a PyPI release now with documented manual assembly.** Rejected: empirically
  ~40 min of expert diagnosis on the author's own machine; indefensible for users.
- **Stay SQLite/Chroma for end users, PG for power users.** Rejected: dual serving
  paths forever is the maintenance disaster RDR-152/155 exist to end; the deprecation
  window already provides the transition.
- **Containers as the only install.** Rejected as default (local-first tool); may be
  documented as an option.

## Consequences

- Release N's gate acquires concrete, testable criteria (fresh-machine E2E in the
  release sandbox) instead of a standing prose blocker.
- The release workflow grows a **per-platform native-image build matrix** (3 targets
  for release N; the native build is ~tens of seconds per runner per RF-157-6) **plus a
  per-platform embedded-PG bundle-build job** — materially more CI than a single Maven
  artifact-attach, and a per-platform build farm to keep green.
- The native binary (and, for the local distro, the embedded-PG bundle) becomes part of
  the pinned-source release discipline — parity tests must cover its version stamp.
- A second OS/arch dimension enters the release matrix; Windows (N+1) adds MSVC.

## Open Questions

1. ~~Does anything in the service preclude native-image (jOOQ codegen reflection,
   Liquibase changelog parsing, ONNX runtime JNI for minilm)?~~ **RESOLVED 2026-06-12
   (nexus-ykrhb, PR #1172): no — the full native service is CI-green on linux-amd64.
   jOOQ generated-record metadata + ONNX/DJL JNI handled via agent-traced reachability
   metadata + IncludeResources.**
2. ~~Embedded-PG wheels: does any maintained wheel ship pgvector for darwin-arm64 +
   linux-x86_64?~~ **RESOLVED 2026-06-14 (RF-157-7): no off-the-shelf relocatable
   PG16+pgvector exists; the project owns a CI bundle-build matrix (zonky PG16 +
   CI-built pgvector). The pip-wheel form is rejected (Alternatives). Open sub-question
   folded into the P1 CA-3 gate: disk/extract cost is measured by the CA-2 spike.**
3. ~~Windows: out of scope for release N?~~ **RESOLVED 2026-06-14 (owner): Windows
   ships in release N+1, not the gating release N. It is the long pole for both the
   native-image build and the pgvector MSVC source build (RF-157-7); release N targets
   linux-amd64, linux-aarch64, mac-arm64.**
4. **Supply-chain signing for the native binary.** (The original `install-jar`
   form is moot — the JAR channel is rejected.) Should the GitHub Releases **native
   binary** carry a detached signature (Sigstore/cosign) beyond a sha256 manifest? It
   executes with DB credentials and is opaque to static inspection, so the posture
   matters more than for a JAR. Position: sha256 manifest for release N; evaluate
   signing for N+1 (P2 / Approach).

## Research Findings

All verified 2026-06-11. Author preference registered: native-image is the desired
end state for Decision 1; the findings below say it is feasible with the risk
concentrated in two JNI libraries, and they reshape the draft in three ways.

### RF-157-1 (VERIFIED, local): the 134 MB JAR is an artifact-hygiene problem first

`unzip -l` of the production fat JAR: the bulk is **multi-platform native payloads
and debug symbols** — a 304 MB (uncompressed) Windows `onnxruntime.pdb`, onnxruntime
natives for six platforms (osx-x64/aarch64, linux-x64/aarch64, win-x64), DJL
tokenizers natives for four, plus `.dSYM` DWARF bundles; 54 native libs total.
**Per-platform assembly (exclude foreign-platform natives + debug artifacts) shrinks
the artifact to roughly 30-40 MB before native-image enters the picture.** Decision 1
therefore decomposes into two independent wins: per-platform packaging (cheap, ships
with channel (a)) and runtime-prereq removal (native-image).

### RF-157-2 (VERIFIED, local): the GraalVM toolchain is already in place

Dev machine JDK IS Oracle GraalVM 25.0.1 with `native-image` on PATH; CI builds on
GraalVM (GRAALVM_HOME in the Java job). No toolchain acquisition cost for the spike.

### RF-157-3 (VERIFIED, web — per-library native-image status, sources in T2)

The SQL/migrations/pool/JSON/logging layer is **green** via the actively-maintained
`oracle/graalvm-reachability-metadata` repo (verified by listing the repo directly,
entries current May-June 2026): jOOQ (tested to 3.21.5), liquibase-core (tested to
5.0.3, standalone — not via Quarkus), pgjdbc (to 42.7.11), HikariCP (to 7.0.2),
jackson-databind/jsr310 (to 2.21.4+), logback (to 1.5.29). sqlite-jdbc ships its own
in-jar metadata since 3.40.1.0 (we use 3.47.1.0 — out of the box). Virtual threads
(the service's thread model) are fully supported on native-image since GraalVM for
JDK 21. Two caveats that are work, not blockers: jOOQ metadata covers jOOQ internals,
NOT our precompiled generated Record classes (tracing-agent pass over the integration
suite generates that config); same for our Jackson DTOs.

### RF-157-4 (VERIFIED, web): the risk is exactly two ML JNI libraries

- **onnxruntime Java**: proven standalone recipe exists (JNI config + resource
  patterns for the bundled natives + `--initialize-at-run-time=ai.onnxruntime.*`,
  microsoft/onnxruntime#5172), BUT oracle/graal#8431 — the native-image **builder
  itself bundles ai.onnxruntime** for ML-guided PGO, conflicting with the app's copy —
  was still open as of 2026-03 with mixed fixed/not-fixed reports on GraalVM 24.
  Community Edition historically sidesteps the enterprise-jar variant of the
  conflict; the spike must verify on the exact distribution chosen.
- **DJL HuggingFace tokenizers**: **zero prior art found** under native-image
  (not in the metadata repo; djl-demo/graalvm covers other engines; issue search
  empty). Mechanically the same JNI/resource/runtime-init pattern as onnxruntime,
  but that is inference, not evidence. This is the single most likely item to kill
  or delay the native path.

### RF-157-5 (VERIFIED, web): distribution + licensing constraints

GitHub Releases: < 2 GiB per asset, 1000 assets/release, no total-size or bandwidth
limit, `browser_download_url` downloads not API-rate-limited — a 30-40 MB jar or a
~100 MB native binary per platform is trivially fine (Decision 1(a) channel is
unconstrained). Licensing: Oracle GraalVM 25 is GFTC — native-image output is
"deemed an unmodified Program," redistribution permitted only "not for a fee," and
Oracle-revocable; **GraalVM Community Edition (GPLv2+CE) output carries no
redistribution conditions** — CI should build release binaries on CE. Platform
matrix note: GraalVM 25 dropped macOS x64 (Apple Silicon only); Mandrel has no
macOS builds at all.

### RF-157-6 (VERIFIED, executed spike 2026-06-11): native-image WORKS — both risk items cleared

Executed on Oracle GraalVM 25.0.1 / macos-aarch64 against a throwaway pgvector
container (production untouched); full record: T2 `nexus_rdr/157-P0-spike-results`.

- **Tokenizers JNI: YES** — first known confirmation. The native binary served
  /health, /version (onnx-local), a store-put that embedded server-side through
  tokenizers + onnxruntime JNI, and a search returning it. Cold-cache caveat:
  DJL extracts `libtokenizers` from the jar resource `native/lib/<platform>/cpu/`;
  the tracing agent misses that resource when `~/.djl.ai` pre-exists — the
  resource glob must be added explicitly (+11.5 MB in-image).
- **graal#8431: does NOT bite** on this toolchain — ML-PGO active in the build
  banner, app's ai.onnxruntime compiled cleanly across 4 builds, zero
  workarounds needed.
- **Metrics**: 51-54 s builds (7.4 GB peak builder RSS), 145 MB binary
  (tokenizers embedded), 3.0-3.3 s to /health-200 (includes Liquibase + ONNX
  init), 368-377 MB runtime RSS.
- **Build recipe**: `native-image --no-fallback --enable-url-protocols=http,https
  -H:ConfigurationFileDirectories=<agent-config> -jar nexus-service.jar`; the
  agent corpus MUST cover both fresh-migration and already-migrated startups
  (Liquibase's snapshot path needs reflection config the fresh path never hits).
- **Remaining unknowns are mechanical**: re-run on GraalVM CE 25 (the licensing
  choice, RF-157-5) and linux-x64/aarch64 in CI. ONNX model files stay an
  external install-time dependency either way.

### RF-157-7 (CA-1, VERIFIED 2026-06-14, web): relocatable PG16 + pgvector per target

**Verdict: CA-1 SUPPORTED for {linux-amd64, linux-aarch64, mac-arm64, windows}.** No
single distributor ships a relocatable PG16 **with** pgvector off the shelf, so the
project must own a CI bundle-build matrix — but every piece exists and the
combination is proven prior art.

- **Relocatable vanilla PG16 exists per target.** `io.zonky.test.postgres`
  embedded-postgres-binaries ship reduced-size, extract-and-run PG16 (BOM 16.x) as
  Maven artifacts, Docker-cross-compiled, for `linux-amd64`, `linux-arm64v8`
  (= aarch64), `darwin-arm64v8` (= mac-arm64), and `windows-amd64`. Relocatability is
  their entire purpose (extract to a tmp dir, `initdb`, run) — which is exactly our
  first-run flow, so `pg_config --sharedir` resolves post-extract (satisfies CA-3
  mechanically, pending a live test).
- **pgvector is a per-platform source build, not a relocatable download.** pgvector
  (the `vector` extension) supports PG13+ (incl. 16). Build needs `make` + `pg_config`
  + **PG server dev headers** (`postgres.h`); Windows uses `nmake /F Makefile.win`
  under MSVC. Produces `vector.{so,dylib,dll}` + `vector.control` + `vector--*.sql`.
  zonky's own extension mechanism (PostGIS via `postgisVersion`) **explicitly excludes
  Windows and macOS**, so we cannot lean on zonky to add pgvector — we build it.
- **The combined bundle is proven feasible.** `boomship/postgres-vector-embedded`
  ships a precompiled **relocatable PG17.5 + pgvector 0.8.0** bundle for mac-arm64,
  mac-x64, linux-x64, linux-arm64, and windows-x64 (Windows shipped *lite* only — no
  SSL/JIT). Different PG major and Node-oriented, but it demonstrates the exact
  artifact we need across exactly our targets.

**Construction strategies (P1 decides):**
- **A — assemble (lean):** pull zonky relocatable PG16 per platform; in the same
  per-platform CI runner, build pgvector from source against a matching full PG16
  (for headers), then inject `vector.*` into the bundle's `pkglibdir`/`sharedir`.
  Risk: zonky bundles are "reduced for testing" (verify they `initdb`+serve our
  workload, not header-stripped past use); the injected `.so` must ABI-match zonky's
  PG build (same PG16 minor, libc/compiler).
- **B — build both from source in the existing native-image CI matrix:** guaranteed
  ABI match, full-featured PG, version-pinned; higher CI cost and PG-from-source on
  Windows/MSVC is the painful part.

**Recommendation:** Strategy A for linux-amd64/linux-aarch64/mac-arm64 (fast, low
risk); treat **Windows as the long pole** (MSVC pgvector build + zonky's no-extension
story + boomship's lite-only Windows) and **defer Windows to release N+1** — it is the
long pole for the native-image build too (RDR-157 Open Q3), so deferring de-risks the
gating release without holding up linux/mac. ONNX model fetch (`nexus-jrrve`) is an
orthogonal install-time dependency either way.

### RF-157-8 (CA-2, VERIFIED 2026-06-14, web + local): embed PG tarball via IncludeResources + self-extract

**Verdict: CA-2 FEASIBLE pending a small build-spike; not a blocker — every mechanical
piece is already proven locally, and ship-alongside is a zero-risk fallback.**

- **The extract-and-run model is exactly how zonky already ships PG.** Each zonky
  artifact is a jar containing `postgres-{platform}-{arch}.txz` — a compressed PG
  tarball extracted to a dir at runtime. That `.txz` is precisely the
  `-H:IncludeResources` payload we would embed; first-run = extract `.txz` → `initdb`
  → provision (the flow `pg_provision` already runs).
- **Our native binary already embeds + extracts native payloads.** The service pom
  passes `-H:IncludeResources=ai/onnxruntime/native/...` and `native/lib/<djl>/.*`;
  the onnxruntime-java / DJL loaders extract those `.so`/`.dylib` from the binary to a
  temp dir at load. So "embed a binary blob as a resource, extract on first use, then
  use it" is **already working in our native image** — the PG case differs only in
  size and that the extracted artifact is exec'd as a subprocess (ProcessBuilder works
  unchanged under native-image).
- **`-H:IncludeResources` embeds arbitrary files (Java-regex selectors), no documented
  hard size cap.** Included resources are kept verbatim in the binary's data section.

**Unverified delta (the spike's job, < 1 day):**
1. Build-time cost + binary bloat of a ~30–50 MB `.txz` IncludeResources payload.
   GraalVM has reports of large embedded resources raising build memory; 30–50 MB is
   moderate against our already-~100 MB binary, but must be measured.
2. First-run extract + `initdb` latency (one-time, then cached) — measure it's
   acceptable.

**Recommendation:** spike the in-binary embed (the "chef's kiss"); **if build cost or
bloat is unacceptable, fall back to ship-alongside** — the per-OS/arch archive carries
`{native binary, pg-<plat>.txz}` and extracts on first run. Identical runtime path,
zero native-image involvement, guaranteed to work. Either way CA-2 does not gate the
two-distribution architecture; it only decides single-file-vs-archive packaging.

### RF-157-9 (CA-3, P1 gate, VERIFIED 2026-06-15): Strategy A falsified → Strategy B proven

**Verdict: Strategy A (zonky reduced bundle + inject pgvector) FAILED on completeness;
Strategy B (build PG16 from source) VERIFIED green in CI. Bead `nexus-vwvv5.2`.**

CA-3 is the go/no-go before the P2/P3 build-out: can a complete PG16 tree + pgvector be
driven by nx's own provisioner to a cluster that loads pgvector, with the glibc floor
pinned?

**Strategy A empirically falsified.** The original plan was "zonky
`embedded-postgres-binaries-linux-amd64` + CI-built pgvector injected." Extracting that
bundle (16.4.0) shows it ships **only** `initdb`, `pg_ctl`, `postgres` — no `pg_config`,
`psql`, `createdb`, headers, or pgxs (zonky's reduced bundle is by design for embedded
test servers). Two independent blockers: (1) pgvector cannot be built against it (no
pg_config/headers/pgxs); (2) nx's own `discover_pg_binaries` requires `psql`+`createdb`,
so the provisioner can't even use it. This is the CA-1 "reduced bundle completeness"
risk, confirmed fatal. There is no "full" zonky artifact.

**Strategy B verified.** The CA-3 gate was pivoted (owner-approved) to build a complete
PG16 from source. Two artifacts:
- **`tests/db/test_pg_provision_ca3_bundle.py`** — pure verification over a
  pre-materialized bundle via the production `NEXUS_PG_BIN` seam: PG16 binaries present
  and complete (`all_present` incl. psql+createdb), `pg_config --sharedir` resolves inside
  the bundle, pgvector present + `check_pgvector_available` passes, the pinned glibc
  floor holds, and — the load-bearing assertion — provisions a hermetic cluster and runs
  `CREATE EXTENSION vector` (forces `dlopen(vector.so)`) + a vector distance op. Skips
  loudly (named CI job) when no bundle.
- **`.github/workflows/ci.yml` job `ca3-pgvector-bundle`** (linux-amd64) — inside a
  **manylinux_2_28 (glibc 2.28)** container: `./configure --prefix=<bundle> && make &&
  make install` PostgreSQL **16.4** from source (a complete tree), build pgvector
  `v0.8.2` against that pg_config, then run the test with a junit parse asserting
  **0 skipped + `CREATE EXTENSION` passed** (no silent all-skip). **Green 2026-06-15:
  11 passed, 0 skipped, glibc floor verified.**

**Glibc floor.** `check_pgvector_available` only stats `vector.control`; it never
`dlopen`s. The ABI/glibc failure surfaces at extension LOAD, not preflight — so the tree
+ pgvector must be built on the **oldest reasonable glibc baseline**, not the CI runner's
(ubuntu-latest ≈ glibc 2.39, which would `dlopen`-fail on older distros). Decision: build
in **manylinux_2_28 (glibc 2.28** = RHEL8 / Debian 10 / Ubuntu 18.10+, a defensible 2026
floor; manylinux2014 / glibc 2.17 was rejected — CentOS 7 is EOL with dead in-container
repos). The test pins `GLIBC_FLOOR=(2,28)` and `objdump -T` asserts **both** `vector.so`
and the `postgres` binary require ≤ `GLIBC_2.28`. A builder drift that raises the floor
fails the test, not the user.

**Relocation caveat (P3).** The gate builds the bundle at, and consumes it from, the same
prefix, so `pg_config`'s compiled-in paths agree with the runtime location. True
extract-to-arbitrary-dir relocation is **not** proven here: `pg_config` reports build-time
absolute paths, so any nx path resolution via `pg_config` (e.g. `check_pgvector_available`'s
sharedir lookup) is not relocation-safe. The local-distribution bundle build must either
install at a fixed runtime prefix or resolve paths relative to the binary (PostgreSQL's
`find_my_exec`). Tracked as P3 work (`nexus-vwvv5.9`).

**Per-target coverage (release N = linux-amd64 + linux-aarch64 + mac-arm64 per CA-1).**
The P1 gate covers **linux-amd64** live. The other two release-N targets are deferred to
P3 with **named beads** so the deferral is traceable, not silent:
- **linux-aarch64** (`nexus-xqk5r`): same `manylinux_2_28_aarch64` baseline (glibc 2.28,
  `GLIBC_FLOOR` applies); runs the same from-source build on an arm64 runner.
- **mac-arm64** (`nexus-0ixqc`): darwin-specific unknowns the linux gate does NOT
  exercise — pgvector produces a `.dylib` (not `.so`), the loader is `dyld` (no glibc
  floor; pin the macOS deployment-target/`minos` via `otool`/`vtool` instead), and
  code-signing/SIP can reject an unsigned `.dylib` at load. Needs its own darwin test
  variant on a `macos-14` runner. P3 must not start the mac-arm64 bundle build assuming
  the linux result transfers.

**Consequence for P3.** The embedded-PG bundle is built from PostgreSQL source per
OS/arch (not repackaged from zonky). Windows MSVC PG-from-source remains the long pole
(release N+1).

### Implications for the draft Decisions — SUPERSEDED by the 2026-06-14 lock

> **Historical research trail. Superseded by `## Decision — UPDATED 2026-06-14` and the
> reconciled `## Approach (phased)`.** The 2026-06-14 owner lock chose native-image as
> the default artifact outright (no JAR-first sequence) and embedded PG for the local
> distribution. The notes below — written when the native path was still "desired, not
> gating" — are retained only to show how the decision evolved; do not plan from them.

1. ~~Decision 1 becomes a sequence, not a choice: release N ships per-platform JARs…
   native-image binaries follow…~~ — superseded: native-image is the default, not a
   later upgrade.
2. **The P0 spike is DONE and PASSED** (RF-157-6): both risk items cleared on
   Oracle GraalVM 25. (Still current.)
3. Platform matrix for native binaries: linux-amd64 + linux-aarch64 + mac-arm64 for
   release N (GraalVM 25 has no mac-x64, which is out of scope per the owner call);
   Windows is release N+1.
