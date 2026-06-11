---
title: "End-User Distribution and Installation: JAR Channel, PG16+pgvector Provisioning, One-Command init --service"
id: RDR-157
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-11
related_issues: [nexus-luxe6, nexus-pebfx, nexus-jdpn9]
related: [RDR-144, RDR-152, RDR-155, RDR-076]
---

# RDR-157: End-User Distribution and Installation

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Since RDR-155 P4a, T3 serving routes exclusively through the PG16 + pgvector + Java
nexus-service stack. `pip install conexus` (or the plugin marketplace install) delivers
none of that stack. A release cut from develop today requires every user to
hand-assemble:

1. **The service JAR** — a 134 MB Maven fat JAR with **no distribution channel**.
   PyPI cannot carry it (size limits, wrong artifact type); users today need a repo
   checkout and a Maven build. `nx daemon service install-jar` (nexus-pebfx.4) solved
   the *local lifecycle* (well-known location, sha256 provenance sidecar, schema-skew
   gate) but not *acquisition*.
2. **A Java 17+ runtime** — the supervisor's `_find_java()` requires JAVA_HOME or
   PATH. A second hidden prerequisite end users will not have.
3. **PostgreSQL 16 with the pgvector extension** — `discover_pg_binaries()` assumes
   homebrew `postgresql@16`; the pgvector brew formula targeting the wrong PG major
   was empirically hit on the dev machine (2026-06-09). `nx init --service` provisions
   a cluster from found binaries but does not get the binaries there, and is missing
   `CREATE EXTENSION` wiring (nexus-jdpn9 item 3).
4. **Voyage key plumbing** — solved (nexus-pebfx.2: credential-chain resolution,
   fail-loud ONNX refusal of voyage-token collections); local-only mode works with the
   bundled ONNX MiniLM. Listed for completeness: the *decision* a new user makes here
   is an onboarding question (RDR-144 pattern), not a packaging one.

This is the substance of release blocker `nexus-luxe6`: prerequisites (1)–(3) are the
undesigned legs. The operability of an *assembled* stack is largely done
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

## Approach (phased, draft)

1. **P0 — research + channel decision.** Verify GitHub Releases asset size limits and
   download UX (auth-less), GraalVM native-image feasibility spike for the service
   (jOOQ/Liquibase reflection inventory), embedded-postgres wheel landscape
   (pgvector inclusion per platform), and the pgvector-formula-major detection story.
   Output: locked Decision 1 + 3 choices.
2. **P1 — JAR channel.** Release-workflow attachment of the service artifact +
   `install-jar --from-release` downloader (sha256 verification against a published
   manifest; provenance sidecar recorded as today).
3. **P2 — provisioning preflight.** `nx doctor`/`init --service` preflight for java,
   PG binaries, pgvector extension (incl. wrong-major detection); `CREATE EXTENSION`
   wiring; guided-install hints or embedded-PG dependency per the P0 decision.
4. **P3 — the one-command collapse.** `nx init --service` end-to-end with idempotent
   steps; release-sandbox E2E proving fresh-machine → serving with zero manual steps.
5. **P4 — handoff to conexus RDR-001.** The upgrade-orchestration consumes P1–P3
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
- The release workflow grows a Maven build + artifact-attach step (CI time).
- A second artifact lineage (service JAR or native binary) becomes part of the
  pinned-source release discipline — parity tests must cover its version stamp.

## Open Questions

1. Does anything in the service preclude native-image (jOOQ codegen reflection,
   Liquibase changelog parsing, ONNX runtime JNI for minilm)? (P0 spike.)
2. Embedded-PG wheels: does any maintained wheel ship pgvector for darwin-arm64 +
   linux-x86_64? What is the disk/install-time cost?
3. Windows: out of scope for release N? (Current daemon substrate is POSIX-leaning.)
4. Should `install-jar --from-release` verify a detached signature in addition to
   sha256 (supply-chain posture), given the artifact executes with DB credentials?

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

### Implications for the draft Decisions (to lock at gate)

1. **Decision 1 becomes a sequence, not a choice**: release N ships per-platform
   JARs via GitHub Releases + `install-jar --from-release` (RF-157-1 makes these
   small; JDK 17+ stays a preflighted prereq); native-image binaries follow as the
   prereq-removal upgrade once the spike clears the two JNI items.
2. **The P0 spike narrows to**: build the service with native-image on **GraalVM CE
   for JDK 25**, tracing-agent config from the integration suite, and empirically
   answer (a) does tokenizers' JNI load from image resources, (b) does graal#8431
   bite on CE 25. Everything else is metadata-repo-covered.
3. Platform matrix for native binaries: linux-x64/aarch64 + macos-aarch64
   (GraalVM 25 has no macos-x64; per-platform JARs cover that tail).
