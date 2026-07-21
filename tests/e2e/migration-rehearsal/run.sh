#!/usr/bin/env bash
# Host orchestrator for the soup-to-nuts migration dress rehearsal.
#
#   tests/e2e/migration-rehearsal/run.sh              # ONNX leg only (secret-free)
#   tests/e2e/migration-rehearsal/run.sh --with-cloud # + Voyage leg (reads .env)
#   tests/e2e/migration-rehearsal/run.sh --no-build   # reuse existing wheel/JAR
#   tests/e2e/migration-rehearsal/run.sh --hole-punch # verify-fill delta-fill proof (nexus-s3dd4.7)
#   tests/e2e/migration-rehearsal/run.sh --era-hop    # RDR-185 era-spanning hop: ancient install -> current via `nx upgrade` ALONE (nexus-n7u38.30)
#   tests/e2e/migration-rehearsal/run.sh --chash-window # RDR-180 pre-cutover window: cohort engine boots (bytea conversion) BEFORE the chash-rekey rung runs (nexus-p78a0)
#
# Builds the wheel on the host and the LINUX native nexus-service binary in a
# GraalVM container (RDR-161: the native binary is the sole launch artifact; the
# java -jar path is expunged). The native build runs IN a linux container so the
# binary matches the rehearsal image's platform — a host build would produce the
# wrong-OS binary. It uses Docker-out-of-Docker (mounted socket) because -Pnative
# runs jOOQ codegen via a Testcontainers pgvector. Both go into an ephemeral image
# with PG16 + pgvector (no JRE), running the full operator path (install → provision
# → serve → seed → migrate → validate → rollback). NOT DinD: PG is provisioned
# inside the box by nx itself.
set -euo pipefail

# Captured BEFORE the `cd` below so it is robust to the invocation cwd (RDR-184
# P0.2, nexus-ccs9v.2): BASH_SOURCE is relative to wherever this script was
# invoked FROM, not the repo root the next line cd's into.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$(git rev-parse --show-toplevel)"
HERE="tests/e2e/migration-rehearsal"
IMAGE="nexus-migration-rehearsal"
WITH_CLOUD=0
DO_BUILD=1
GUIDED=0
COLD=0
COMPREHENSIVE=0
STRESS=0
FULLSTACK=0
HOLE_PUNCH=0
SHAKEOUT=0
PACKAGE_UPGRADE=0
ERA_HOP=0
CHASH_WINDOW=0
# RDR-002 ez5.13: the release_version the guided MVV stamps into the binary so
# its /version reports >= the guided-upgrade version-pin floor and PASSES.
# Derived from the product constant (engine_version.REQUIRED_ENGINE_VERSION —
# the ONLY floor constant after the nexus-b6qlf unification) so this stamp can
# never go stale: a hardcoded "0.1.6" silently fell below the bumped v0.1.8 floor
# and the MVV fail-closed at the version gate without ever exercising the
# migration (nexus-... 6.0.0 validation). Then the derivation itself went stale
# the same way: it parsed REQUIRED_RELEASE_VERSION out of guided_upgrade.py after
# the constant moved to engine_version.py, and the silent "0.1.8" fallback
# fail-closed the v0.1.37 pre-tag rehearsal at the version gate. No fallback:
# if the constant can't be parsed, abort loudly.
GUIDED_STAMP_VERSION="$(
  python3 - <<'PY'
import re, pathlib
src = pathlib.Path("src/nexus/engine_version.py").read_text()
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
print(".".join(m.groups()) if m else "")
PY
)"
[ -n "$GUIDED_STAMP_VERSION" ] || { echo "FATAL: could not parse REQUIRED_ENGINE_VERSION from src/nexus/engine_version.py — the guided stamp would be wrong; fix the regex/path before rehearsing" >&2; exit 2; }
RELEASE_PROPS="service/src/main/resources/META-INF/nexus/release.properties"
# nexus-4mm24: the published engine-service tag the COLD box auto-acquires from.
# Must be >= the guided-upgrade version-pin floor (REQUIRED_ENGINE_VERSION); a
# stale default fail-closes the --cold MVV at the version gate. Kept literal (it
# names a PUBLISHED release tag, which need not equal the floor) but bumped to
# track it; override via NEXUS_SERVICE_TAG. (nexus-v0zmv)
COLD_TAG="${NEXUS_SERVICE_TAG:-engine-service-v0.1.49}"
# nexus-cfgo9: the PACKAGE-UPGRADE leg's starting point — a REAL, already
# published PyPI release + the engine tag ITS OWN PINNED_SERVICE_TAG
# resolves to (see CHANGELOG.md's "[6.9.0]" entry: "Ships with (and
# requires) engine-service-v0.1.42"). Kept literal (like COLD_TAG) but
# bumped alongside REQUIRED_ENGINE_VERSION so the scenario never silently
# stops being "stale" — the guard below fails loud if it does.
PREV_RELEASE="${NEXUS_PREV_RELEASE:-6.13.1}"
PREV_ENGINE_TAG="${NEXUS_PREV_ENGINE_TAG:-engine-service-v0.1.47}"
# RDR-185 P4.3 (nexus-n7u38.30): the ERA-HOP's starting point. Deliberately NOT
# "one release back" like PREV_RELEASE — this leg's whole claim is that an
# ANCIENT install converges, so the default is the OLDEST install the product
# still promises to carry: conexus 6.0.0, the migration-capable release (the
# two-release deprecation window's first half, docs/migration-runbook.md §0.1)
# and the exact population that holds the GH #1408 shape. ERA_ENGINE_TAG is
# 6.0.0's OWN PINNED_SERVICE_TAG — the engine that install would be running —
# and is acquired at runtime by 6.0.0's own code, never supplied by us.
#
# NOT the OLD_TAG rotation: nexus-dlhub owns that (a hop REDESIGN, not a bump).
# When RDR-155 P4b deletes the Chroma read path, this leg's SOURCE disappears
# with it and the whole scenario retires — it is a deprecation-window leg by
# construction.
ERA_RELEASE="${NEXUS_ERA_RELEASE:-6.0.0}"
ERA_ENGINE_TAG="${NEXUS_ERA_ENGINE_TAG:-engine-service-v0.1.11}"
# nexus-p78a0 (RDR-180): the CHASH-WINDOW leg's starting point — the last
# PRE-COHORT (legacy 32-hex TEXT chash) release + engine pair. The engine tag
# defaults to the FLOOR tag (derived below from REQUIRED_ENGINE_VERSION):
# pre-cutover that IS the last pre-cohort engine, AND it must equal the floor
# so converge_engine (which reads the install-binary provenance sidecar)
# no-ops over the harness's swapped-in cohort binary instead of re-downloading
# the published tag over it — see the guard past the arg loop. Post-cutover
# (floor bumped to the cohort tag) the leg's premise inverts; the DRIVER's
# era guard then fails loud against the real store (chash already bytea) with
# redesign instructions rather than grading a window that never opened.
CHASH_OLD_RELEASE="${NEXUS_CHASH_OLD_RELEASE:-6.13.1}"
CHASH_OLD_ENGINE_TAG="${NEXUS_CHASH_OLD_ENGINE_TAG:-engine-service-v${GUIDED_STAMP_VERSION}}"
# The NEW required engine — derived from the SAME constant COLD_TAG's
# default and GUIDED_STAMP_VERSION are, so this leg tracks a floor bump
# automatically (nexus-b6qlf: one source of truth).
NEW_ENGINE_TAG="engine-service-v${GUIDED_STAMP_VERSION}"
for a in "$@"; do
  case "$a" in
    --with-cloud) WITH_CLOUD=1 ;;
    --no-build)   DO_BUILD=0 ;;
    --guided)     GUIDED=1 ;;   # RDR-002 ez5.13: drive nx guided-upgrade
    --cold)       COLD=1 ;;     # nexus-4mm24: cold-acquire from the published release
    --comprehensive) COMPREHENSIVE=1 ;;  # Phase D: daily-driver surface on the default rehearse.sh
    --stress)     STRESS=1 ;;            # Phase E: concurrency + queue-drain stress on the default rehearse.sh
    --fullstack)  FULLSTACK=1 ;;         # standalone: full topology (service + nx-mcp + claude) MCP-driven enqueue + worker drain
    --hole-punch) HOLE_PUNCH=1 ;;        # standalone: verify-fill delta-fill proof against a real fault-injected PG target (nexus-s3dd4.7)
    --shakeout)   SHAKEOUT=1 ;;          # standalone: CANDIDATE shakeout — CLI verb matrix + incremental index + concurrent load against the locally-built -Ob binary (nexus-h8rf6)
    --package-upgrade) PACKAGE_UPGRADE=1 ;;  # standalone: nexus-cfgo9 ONE-engine convergence MVV — package-only upgrade from a real previous release, engine acquired for real by the product, never supplied by this harness
    --era-hop)    ERA_HOP=1 ;;           # standalone: RDR-185 nexus-n7u38.30 — ancient install (old release + old engine + pre-RDR-108 ids + Chroma substrate) -> current via `nx upgrade` ALONE, unattended
    --chash-window) CHASH_WINDOW=1 ;;    # standalone: RDR-180 nexus-p78a0 — pre-cohort store -> locally-built cohort engine boot (window: loud + safe) -> nx upgrade rekey (window closed)
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done
[ "$PACKAGE_UPGRADE" = 1 ] && [ "${PREV_ENGINE_TAG#engine-service-v}" = "$GUIDED_STAMP_VERSION" ] && {
  echo "FATAL: PREV_ENGINE_TAG ($PREV_ENGINE_TAG) already equals the current REQUIRED_ENGINE_VERSION ($GUIDED_STAMP_VERSION) — the package-upgrade scenario is no longer 'stale'. Bump NEXUS_PREV_RELEASE/NEXUS_PREV_ENGINE_TAG in run.sh to the release immediately before this floor bump." >&2
  exit 2
}

# --guided: stamp release.properties so the native binary reports a release
# version (an unstamped build -> release_version=null -> version-pin fail-closes,
# which is not the success path this MVV exercises). Force a native rebuild so
# the stamp is actually baked in, and restore the file on exit. The stamp must
# happen BEFORE the native build below.
# Single restore hook for the stamped release.properties (folded into every EXIT
# trap below so a later `trap ... EXIT` does not clobber it). Defined + armed
# BEFORE the stamp mutation so a signal in the stamp window still restores it.
_guided_restore() {
  { [ "$GUIDED" = 1 ] || [ "$CHASH_WINDOW" = 1 ]; } || return 0
  rm -f "$RELEASE_PROPS.tmp" 2>/dev/null || true
  git checkout -- "$RELEASE_PROPS" 2>/dev/null || true
}
trap '_guided_restore' EXIT

[ "$COLD" = 1 ] && [ "$GUIDED" = 1 ] && { echo "--cold and --guided are different flows; pick one" >&2; exit 2; }
# nexus-gilf2: --guided seeds local-ONNX (bge-768) cross-model targets, while
# --with-cloud boots a voyage-only service. The combination is incoherent: the
# bge-768 targets have no embedder in voyage mode and the pebfx.2 guard 422s the
# leg. Use --guided alone for the local bge-768 MVV.
[ "$GUIDED" = 1 ] && [ "$WITH_CLOUD" = 1 ] && { echo "--guided and --with-cloud are incoherent (guided seeds local bge-768 targets; cloud is voyage-only); run --guided alone" >&2; exit 2; }
[ "$COLD" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--cold always rebuilds the wheel + cold-acquires the binary; --no-build is irrelevant" >&2; exit 2; }
# --comprehensive adds Phase D to the DEFAULT rehearse.sh entrypoint; --cold and
# --guided override the entrypoint (rehearse_cold.sh / rehearse_guided.sh) and so
# never run Phase D. Reject the incoherent combination loudly.
[ "$COMPREHENSIVE" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ]; } && { echo "--comprehensive runs on the default rehearse path; it cannot combine with --cold/--guided (they override the entrypoint)" >&2; exit 2; }
[ "$STRESS" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ]; } && { echo "--stress runs on the default rehearse path; it cannot combine with --cold/--guided (they override the entrypoint)" >&2; exit 2; }
[ "$FULLSTACK" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ]; } && { echo "--fullstack is a standalone full-topology run (its own entrypoint); do not combine with other legs" >&2; exit 2; }
# --hole-punch is a standalone journey: it reuses the --cold box's staging
# internally (cheapest to compose — no native GraalVM build) but drives its
# own entrypoint (rehearse_hole_punch.sh, nexus-s3dd4.7), never combined with
# another flow flag.
[ "$HOLE_PUNCH" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ]; } && { echo "--hole-punch is a standalone verify-fill delta-fill journey (its own cold-acquire entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$HOLE_PUNCH" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--hole-punch always rebuilds the wheel + cold-acquires the binary; --no-build is irrelevant" >&2; exit 2; }
# --shakeout is a standalone candidate-validation journey: it builds the current
# service/ tree natively (like --guided) and drives its own entrypoint
# (rehearse_shakeout.sh, nexus-h8rf6) — never combined with another flow flag.
[ "$SHAKEOUT" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ]; } && { echo "--shakeout is a standalone candidate shakeout (its own entrypoint); do not combine with other legs" >&2; exit 2; }
# --package-upgrade is a standalone journey (nexus-cfgo9): NO native build (the
# NEW engine is acquired for real by the product's own convergence code, never
# locally built or supplied by this harness) — never combined with another
# flow flag.
[ "$PACKAGE_UPGRADE" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ]; } && { echo "--package-upgrade is a standalone convergence journey (its own entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$PACKAGE_UPGRADE" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--package-upgrade always rebuilds the working-tree wheel; --no-build is irrelevant" >&2; exit 2; }
# --era-hop is a standalone journey (nexus-n7u38.30): NO native build (both
# engines are acquired for real by the product's own code) — never combined.
[ "$ERA_HOP" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ]; } && { echo "--era-hop is a standalone era-spanning journey (its own entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$ERA_HOP" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--era-hop always rebuilds the working-tree wheel; --no-build is irrelevant" >&2; exit 2; }
# Staleness guard, mirroring the --package-upgrade one: if the era's engine has
# caught up to the current floor there is no era left to span, and every
# convergence assertion in the leg would pass vacuously.
[ "$ERA_HOP" = 1 ] && [ "${ERA_ENGINE_TAG#engine-service-v}" = "$GUIDED_STAMP_VERSION" ] && {
  echo "FATAL: ERA_ENGINE_TAG ($ERA_ENGINE_TAG) already equals the current REQUIRED_ENGINE_VERSION ($GUIDED_STAMP_VERSION) — there is no era to span and the hop's convergence asserts would be vacuous. Fix NEXUS_ERA_RELEASE/NEXUS_ERA_ENGINE_TAG in run.sh." >&2
  exit 2
}
# --chash-window is a standalone journey (nexus-p78a0): stamped native build
# of the working tree (the cohort engine, like --guided) PLUS runtime
# acquisition of the pre-cohort pair — never combined.
[ "$CHASH_WINDOW" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ERA_HOP" = 1 ]; } && { echo "--chash-window is a standalone window rehearsal (its own entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$CHASH_WINDOW" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--chash-window requires a fresh STAMPED native build of the cohort engine; drop --no-build" >&2; exit 2; }
# The window's premise: the pre-cohort engine's provenance sidecar satisfies
# the floor, so converge_engine NO-OPS over the harness's swapped-in cohort
# binary. An old tag below the floor would make the transition re-download
# the published floor tag OVER the swap and silently collapse the window.
[ "$CHASH_WINDOW" = 1 ] && [ "${CHASH_OLD_ENGINE_TAG#engine-service-v}" != "$GUIDED_STAMP_VERSION" ] && {
  echo "FATAL: CHASH_OLD_ENGINE_TAG ($CHASH_OLD_ENGINE_TAG) != the floor (v$GUIDED_STAMP_VERSION) — converge_engine would re-download the floor tag over the swapped cohort binary and the window would collapse. The leg requires old-tag == floor (the pre-cutover premise)." >&2
  exit 2
}

# RDR-184 P0.2 (nexus-ccs9v.2): serialize on the machine-global fixed
# resources this harness mutates — the fixed docker tag ($IMAGE) and the
# shared dist/ wheel output (the near-miss that motivated this bead: two
# concurrent rehearsals racing the same wheel/image). The lock dir lives
# under a stable machine-global temp root, NOT under this checkout — the
# resource being serialized (one docker daemon, one dist/ per host) is
# machine-global, so two different checkouts on the same host must still
# serialize against each other. Acquired here, after arg parsing/validation
# (usage errors don't need the lock) but strictly before the first mutation
# (the --guided release.properties stamp just below). Lock dir is a
# HARD-CODED /tmp path, deliberately NOT ${TMPDIR:-/tmp} (code-review
# SIGNIFICANT fix): on darwin, an interactive shell's TMPDIR is a per-user
# /var/folders/... path while a LaunchAgent/CI/stripped-env invocation sees
# plain /tmp — two different invocation contexts would silently compute
# DIFFERENT lockdirs and never contend, defeating the whole point of a
# machine-global guard (this repo runs LaunchAgents that could race an
# interactive run). /tmp is always the same path across every context on
# the same host.
# shellcheck source=../lib/lock.sh disable=SC1091
source "$SCRIPT_DIR/../lib/lock.sh"
LOCKDIR="/tmp/nexus-e2e-locks/migration-rehearsal.lock"
mkdir -p "$(dirname "$LOCKDIR")"
lock_acquire "$LOCKDIR" || exit 1
# Code-review CRITICAL fix: the trap installed at the top of the script
# (before LOCKDIR existed) referenced $LOCKDIR unconditionally — any of the
# 12 argument-conflict guards ABOVE this point firing `exit 2` would invoke
# that trap under `set -u` with $LOCKDIR unbound, aborting on the trap's own
# evaluation instead of the documented exit 2. LOCKDIR cannot be referenced
# by a trap before this line, where it is first assigned — reassign the
# trap to the lock-aware form only now that it is safe to do so.
trap '_guided_restore; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
# Test seam (RDR-184 P0.2, nexus-ccs9v.2): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (wheel build / native build / docker).
# No-op — unset in every normal invocation.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

if [ "$GUIDED" = 1 ] || [ "$CHASH_WINDOW" = 1 ]; then
  # --guided / --chash-window force-rebuild the native binary with the stamp
  # baked in, so both are incompatible with --no-build (which would reuse a
  # stale/unstamped binary). For --chash-window the stamp matters for the
  # same reason as --guided: an unstamped binary reports release_version=null
  # and every version-shaped surface degrades to "unknown".
  [ "$DO_BUILD" = 0 ] && { echo "--guided/--chash-window require a fresh native build; drop --no-build" >&2; exit 2; }
  echo "[stamp] stamping $RELEASE_PROPS release_version=$GUIDED_STAMP_VERSION (restored on exit)…"
  grep -v '^release_version=' "$RELEASE_PROPS" > "$RELEASE_PROPS.tmp"
  printf 'release_version=%s\n' "$GUIDED_STAMP_VERSION" >> "$RELEASE_PROPS.tmp"
  mv "$RELEASE_PROPS.tmp" "$RELEASE_PROPS"
  # Force a fresh native build so the stamp is baked in.
  rm -f service/target/nexus-service
fi

GRAAL_IMAGE="container-registry.oracle.com/graalvm/native-image-community:25"
if [ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ERA_HOP" = 1 ]; then
  # nexus-4mm24 / nexus-s3dd4.7 / nexus-cfgo9 / nexus-n7u38.30: these boxes acquire every
  # engine binary at runtime (PUBLISHED release) — NO local native build, NO
  # stamping. Just the wheel.
  echo "[1/2] Building the conexus wheel (host)…"
  uv build --wheel >/dev/null 2>&1
  ls dist/conexus-*.whl >/dev/null 2>&1 || { echo "no wheel in dist/" >&2; exit 1; }
elif [ "$DO_BUILD" = 1 ]; then
  echo "[1/3] Building the conexus wheel (host)…"
  uv build --wheel >/dev/null 2>&1
  echo "[2/3] Building the LINUX native nexus-service binary (GraalVM container, ~2-3m)…"
  if [ ! -x service/target/nexus-service ]; then
    # Native build in a linux GraalVM container. The mounted Docker socket lets
    # -Pnative's Testcontainers jOOQ codegen reach the host daemon (DooD);
    # TESTCONTAINERS_HOST_OVERRIDE + the host-gateway alias make the build
    # container reach the sibling pgvector. -Ob = quick-build (correctness gate,
    # not a perf binary). Output: service/target/nexus-service + its *.so siblings.
    # Builder heap: the pom default (native.image.maxheap=5632m) is sized for
    # the 7GB CI runner; locally it GC-thrashes (403 GCs / 6.8% of build time
    # observed on the 8GB-VM default, 2026-07-13). Auto-size to ~70% of the
    # Docker VM's memory, never below the pom default.
    vm_mib=$(( $(docker info --format '{{.MemTotal}}') / 1048576 ))
    NATIVE_MAXHEAP="$(( vm_mib * 70 / 100 ))m"
    [ "$(( vm_mib * 70 / 100 ))" -lt 5632 ] && NATIVE_MAXHEAP=5632m
    echo "      (builder heap ${NATIVE_MAXHEAP} — 70% of the ${vm_mib}MiB Docker VM)"
    docker run --rm --entrypoint bash \
      --add-host=host.docker.internal:host-gateway \
      -v "$PWD":/src -w /src/service \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -e TESTCONTAINERS_RYUK_DISABLED=true \
      -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
      "$GRAAL_IMAGE" \
      -c "./mvnw -B -Pnative -DskipTests -Dnative.image.opt=-Ob -Dnative.image.maxheap=${NATIVE_MAXHEAP} package"
  else
    echo "      (native binary already built — reusing service/target/nexus-service)"
  fi
else
  echo "[1-2/3] --no-build: reusing existing wheel + native binary"
fi

if [ "$COLD" = 0 ] && [ "$HOLE_PUNCH" = 0 ] && [ "$PACKAGE_UPGRADE" = 0 ] && [ "$ERA_HOP" = 0 ]; then
  ls dist/conexus-*.whl >/dev/null 2>&1 || { echo "no wheel in dist/ — drop --no-build" >&2; exit 1; }
  [ -x service/target/nexus-service ] || { echo "no native binary at service/target/nexus-service — drop --no-build" >&2; exit 1; }
fi

# ── Pre-flight Docker disk-pressure check (nexus-h8rf6.13) ────────────────────
# The recurring barf is Docker Desktop's capped VM disk, not the host:
# iteration-heavy sessions accumulate build cache + dangling rehearsal-image
# generations until builds crawl (~80GB observed across 4 shakeout iterations).
# When reclaimable build cache exceeds the threshold, prune — with headroom
# generous enough to KEEP the hot layers (v0.1.21 lesson: an aggressive
# --reserved-space 6GB evicted the freshly-unreferenced 692MB bge model layer
# and forced a full re-download on the next build; 12GB spares it). Old
# dangling image generations are pruned by age so the current lineage stays.
# Prune only touches unused entries, so this is safe even with other builds up.
preflight_docker_prune() {
  local reclaimable_gb
  reclaimable_gb="$(docker system df --format '{{.Type}} {{.Reclaimable}}' 2>/dev/null \
    | awk '/^Build Cache/ {v=$3+0; if ($3 ~ /TB/) v=v*1024; else if ($3 !~ /GB/) v=0; print int(v)}')"
  reclaimable_gb="${reclaimable_gb:-0}"
  if [ "${reclaimable_gb:-0}" -gt 10 ] 2>/dev/null; then
    echo "[preflight] Docker build cache reclaimable ~${reclaimable_gb}GB (>10GB) — pruning (reserved-space 12GB keeps hot layers incl. the bge model)…"
    docker builder prune -f --reserved-space 12GB 2>/dev/null | tail -1 || true
    # Belt: drop dangling (untagged) image generations older than a day —
    # this is what actually releases superseded rehearsal-image layers.
    docker image prune -f --filter 'until=24h' 2>/dev/null | tail -1 || true
  fi
}
preflight_docker_prune

echo "[stage] Staging a minimal build context + building image (COLD=$COLD HOLE_PUNCH=$HOLE_PUNCH ERA_HOP=$ERA_HOP WITH_CLOUD=$WITH_CLOUD)…"
# Flatten wheel + JAR + driver to fixed names in a tiny throwaway context. The
# repo .dockerignore excludes dist/, and the inputs live in three different
# trees — staging sidesteps both without touching the shared .dockerignore.
STAGE="$(mktemp -d)"
trap '_guided_restore; rm -rf "$STAGE"; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
cp "$(ls -t dist/conexus-*.whl | head -1)"            "$STAGE/"   # keep real PEP 427 name
if [ "$ERA_HOP" = 1 ]; then
  # nexus-n7u38.30: same posture as --package-upgrade (working-tree wheel in its
  # own subdirectory, real PEP 427 name preserved, no engine artifact staged at
  # all — BOTH engines are acquired at runtime by the product's own code) PLUS
  # the seeder, which writes the ancient Chroma/T2/catalog state under the ERA
  # release's own libraries.
  mkdir -p "$STAGE/worktree-wheel"
  cp "$(ls -t dist/conexus-*.whl | head -1)" "$STAGE/worktree-wheel/"
  cp "$HERE/Dockerfile.era-hop" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_era_hop.sh" "$HERE/seed_legacy.py" "$STAGE/"
elif [ "$CHASH_WINDOW" = 1 ]; then
  # nexus-p78a0: the STAMPED cohort native build travels in (the unpublished
  # cohort engine the driver swaps in by hand — its only engine supply, see
  # the driver header) + the worktree wheel under its own subdirectory (real
  # PEP 427 name preserved; the driver tool-installs the OLD release from
  # real PyPI first) + the driver.
  mkdir -p "$STAGE/native" "$STAGE/worktree-wheel"
  cp service/target/nexus-service "$STAGE/native/"
  if compgen -G "service/target/*.so" > /dev/null; then
    cp service/target/*.so "$STAGE/native/"
  fi
  cp "$(ls -t dist/conexus-*.whl | head -1)" "$STAGE/worktree-wheel/"
  cp "$HERE/Dockerfile.chash-window" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_chash_window.sh" "$STAGE/"
elif [ "$PACKAGE_UPGRADE" = 1 ]; then
  # nexus-cfgo9: the WORKING-TREE wheel travels in under its OWN subdirectory
  # (its real PEP 427 filename preserved — pip/uv parse the filename strictly
  # and a prefix-mangled name fails with "invalid version") so it never
  # collides with the driver script's `pip install conexus==$PREV_RELEASE`
  # from real PyPI into the SAME venv. No engine artifact is staged at all
  # (both $PREV_ENGINE_TAG and $NEW_ENGINE_TAG are acquired at runtime by the
  # product's own code — the harness never supplies an engine binary).
  mkdir -p "$STAGE/worktree-wheel"
  cp "$(ls -t dist/conexus-*.whl | head -1)" "$STAGE/worktree-wheel/"
  cp "$HERE/Dockerfile.package-upgrade" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_package_upgrade.sh" "$STAGE/"
elif [ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ]; then
  # nexus-4mm24: NOTHING the service needs is staged — the cold box acquires the
  # binary + PG bundle from the published release at runtime. Only the wheel +
  # both cold drivers (rehearse_cold.sh, rehearse_hole_punch.sh — nexus-s3dd4.7)
  # + seed travel in; the entrypoint below picks the right one.
  cp "$HERE/Dockerfile.cold" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_cold.sh" "$HERE/rehearse_hole_punch.sh" "$HERE/seed_legacy.py" "$STAGE/"
elif [ "$FULLSTACK" = 1 ]; then
  # Full topology: native binary + the fullstack Dockerfile (adds linux claude) +
  # the fullstack driver. Same native-binary staging as the default path.
  mkdir -p "$STAGE/native"
  cp service/target/nexus-service "$STAGE/native/"
  if compgen -G "service/target/*.so" > /dev/null; then
    cp service/target/*.so "$STAGE/native/"
  fi
  cp "$HERE/Dockerfile.fullstack" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_fullstack.sh" "$HERE/seed_legacy.py" "$STAGE/"
else
  # The native binary travels into the image. A LOCAL -Pnative -Ob quick build also
  # emits native-image .so siblings (libjvm/libawt/liblcms/...) that must be
  # co-located (native-image dlopen's JDK libs from the executable's own dir); a
  # RELEASE binary (engine-service-v*) is self-contained with NO .so siblings. So
  # the .so copy is best-effort — present them when they exist, skip when they don't.
  mkdir -p "$STAGE/native"
  cp service/target/nexus-service "$STAGE/native/"
  if compgen -G "service/target/*.so" > /dev/null; then
    cp service/target/*.so "$STAGE/native/"
  fi
  cp "$HERE/Dockerfile" "$HERE/rehearse.sh" "$HERE/rehearse_guided.sh" "$HERE/rehearse_shakeout.sh" "$HERE/seed_legacy.py" "$STAGE/"
fi

# Docker Desktop's credsStore=desktop helper can't reach a locked login keychain
# in a non-interactive session, which fails even cached/anonymous image
# resolution at build time. Temporarily strip credsStore (the auths entries are
# empty), restore on exit. docker run is unaffected (only build-time auth fails).
DCFG="$HOME/.docker/config.json"
if [ -f "$DCFG" ] && grep -q '"credsStore"' "$DCFG"; then
  cp "$DCFG" "$STAGE/.docker-config.bak"
  python3 -c "import json,os;p=os.path.expanduser('~/.docker/config.json');d=json.load(open(p));d.pop('credsStore',None);json.dump(d,open(p,'w'),indent=2)"
  trap '_guided_restore; cp "$STAGE/.docker-config.bak" "$DCFG"; rm -rf "$STAGE"; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
  echo "      (temporarily stripped credsStore from ~/.docker/config.json — restored on exit)"
fi

# nexus-myk4e/nexus-5votw: the image's bge fetch defaults to the self-hosted
# GitHub release asset (ci-assets-bge-768-v1, set in the Dockerfile ARGs);
# NEXUS_BGE_MODEL_URL/NEXUS_BGE_TOKENIZER_URL override for a re-cut asset tag.
BUILD_ARGS=()
[ -n "${NEXUS_BGE_MODEL_URL:-}" ] && BUILD_ARGS+=(--build-arg "BGE_MODEL_URL=$NEXUS_BGE_MODEL_URL")
[ -n "${NEXUS_BGE_TOKENIZER_URL:-}" ] && BUILD_ARGS+=(--build-arg "BGE_TOKENIZER_URL=$NEXUS_BGE_TOKENIZER_URL")
# Progress streams deliberately (no -q): the image build is the longest quiet
# stage of a run (14-18 min uncached, measured 2026-07-21) and -q made a slow
# build indistinguishable from a hang. The step timings it prints are also the
# evidence base for the layer-caching work (nexus-imkxs).
docker build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -f "$STAGE/Dockerfile" -t "$IMAGE" "$STAGE"

run_env=(-e "WITH_CLOUD=$WITH_CLOUD" -e "COMPREHENSIVE=$COMPREHENSIVE" -e "STRESS=$STRESS")
if [ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ]; then
  # nexus-4mm24 / nexus-s3dd4.7: tell the cold box which published release to
  # acquire from (--hole-punch needs v0.1.18+ for /v1/telemetry/ids/probe).
  run_env+=(-e "NEXUS_SERVICE_TAG=$COLD_TAG")
fi
if [ "$PACKAGE_UPGRADE" = 1 ]; then
  run_env+=(-e "PREV_RELEASE=$PREV_RELEASE" -e "PREV_ENGINE_TAG=$PREV_ENGINE_TAG" -e "NEW_ENGINE_TAG=$NEW_ENGINE_TAG")
fi
if [ "$ERA_HOP" = 1 ]; then
  run_env+=(-e "ERA_RELEASE=$ERA_RELEASE" -e "ERA_ENGINE_TAG=$ERA_ENGINE_TAG" -e "NEW_ENGINE_TAG=$NEW_ENGINE_TAG")
fi
if [ "$CHASH_WINDOW" = 1 ]; then
  run_env+=(-e "OLD_RELEASE=$CHASH_OLD_RELEASE" -e "OLD_ENGINE_TAG=$CHASH_OLD_ENGINE_TAG" -e "FLOOR_VERSION=$GUIDED_STAMP_VERSION")
fi
if [ "$WITH_CLOUD" = 1 ]; then
  # Forward the Voyage key from .env (export VOYAGE_API_KEY=…) under both names
  # the code probes. Never echoed.
  # shellcheck disable=SC1091
  set +u; . ./.env 2>/dev/null || true; set -u
  key="${VOYAGE_API_KEY:-${NX_VOYAGE_API_KEY:-}}"
  [ -n "$key" ] || { echo "--with-cloud needs VOYAGE_API_KEY in .env" >&2; exit 1; }
  run_env+=(-e "NX_VOYAGE_API_KEY=$key" -e "VOYAGE_API_KEY=$key")
fi

# NOT `exec` — exec replaces this shell and would suppress the EXIT trap that
# restores ~/.docker/config.json + release.properties and removes the staging
# dir. Run as a child and propagate its exit code.
if [ "$FULLSTACK" = 1 ]; then
  # Provide FRESH claude oauth so the in-container `claude -p` (MCP driver + real
  # aspect extraction) authenticates. The ~/.claude/.credentials.json FILE goes
  # stale within ~1h (oauth access tokens are short-lived + the refresh token
  # rotates); the live token lives in the macOS keychain. Pull it at run time
  # (same approach as tests/cc-validation), stage it (ephemeral, cleaned on exit),
  # mount read-only. Real, billed calls; data/PG stay container-isolated.
  FRESHCREDS="$(security find-generic-password -s 'Claude Code-credentials' -w 2>/dev/null || true)"
  if [ -z "$FRESHCREDS" ] && [ -f "$HOME/.claude/.credentials.json" ]; then
    echo "      (keychain miss — falling back to ~/.claude/.credentials.json, may be stale)" >&2
    FRESHCREDS="$(cat "$HOME/.claude/.credentials.json")"
  fi
  [ -n "$FRESHCREDS" ] || { echo "--fullstack needs claude oauth (keychain 'Claude Code-credentials' or ~/.claude/.credentials.json)" >&2; exit 1; }
  printf '%s' "$FRESHCREDS" > "$STAGE/.claude-credentials.json"; chmod 600 "$STAGE/.claude-credentials.json"
  docker run --rm "${run_env[@]}" \
    -v "$STAGE/.claude-credentials.json":/home/nexus/.claude/.credentials.json:ro \
    "$IMAGE"
elif [ "$HOLE_PUNCH" = 1 ]; then
  # nexus-s3dd4.7: override the cold box's default entrypoint to drive the
  # verify-fill hole-punch journey instead of the plain cold-acquire MVV.
  docker run --rm "${run_env[@]}" --entrypoint /bin/bash "$IMAGE" \
    /home/nexus/rehearse_hole_punch.sh
elif [ "$PACKAGE_UPGRADE" = 1 ]; then
  # nexus-cfgo9: Dockerfile.package-upgrade's default entrypoint IS
  # rehearse_package_upgrade.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$ERA_HOP" = 1 ]; then
  # nexus-n7u38.30: Dockerfile.era-hop's default entrypoint IS
  # rehearse_era_hop.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$CHASH_WINDOW" = 1 ]; then
  # nexus-p78a0: Dockerfile.chash-window's default entrypoint IS
  # rehearse_chash_window.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$COLD" = 1 ]; then
  # nexus-4mm24: Dockerfile.cold's default entrypoint IS rehearse_cold.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$SHAKEOUT" = 1 ]; then
  # nexus-h8rf6: candidate shakeout — verb matrix + incremental-index +
  # concurrent-load assertions against the locally-built candidate binary.
  docker run --rm "${run_env[@]}" --entrypoint /bin/bash "$IMAGE" \
    /home/nexus/rehearse_shakeout.sh
elif [ "$GUIDED" = 1 ]; then
  # RDR-002 ez5.13: override the default entrypoint to drive the one-command
  # guided-upgrade MVV instead of the full manual rehearsal.
  docker run --rm "${run_env[@]}" --entrypoint /bin/bash "$IMAGE" \
    /home/nexus/rehearse_guided.sh
else
  docker run --rm "${run_env[@]}" "$IMAGE"
fi
rc=$?
exit "$rc"
