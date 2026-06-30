#!/usr/bin/env bash
# Host orchestrator for the soup-to-nuts migration dress rehearsal.
#
#   tests/e2e/migration-rehearsal/run.sh              # ONNX leg only (secret-free)
#   tests/e2e/migration-rehearsal/run.sh --with-cloud # + Voyage leg (reads .env)
#   tests/e2e/migration-rehearsal/run.sh --no-build   # reuse existing wheel/JAR
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
# RDR-002 ez5.13: the release_version the guided MVV stamps into the binary so
# its /version reports >= the guided-upgrade version-pin floor and PASSES.
# Derived from the product constant (REQUIRED_RELEASE_VERSION) so this stamp can
# never go stale: a hardcoded "0.1.6" silently fell below the bumped v0.1.8 floor
# and the MVV fail-closed at the version gate without ever exercising the
# migration (nexus-... 6.0.0 validation). Falls back to a literal floor if the
# constant can't be parsed.
GUIDED_STAMP_VERSION="$(
  python3 - <<'PY' 2>/dev/null || true
import re, pathlib
src = pathlib.Path("src/nexus/migration/guided_upgrade.py").read_text()
m = re.search(r"REQUIRED_RELEASE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
print(".".join(m.groups()) if m else "")
PY
)"
[ -n "$GUIDED_STAMP_VERSION" ] || GUIDED_STAMP_VERSION="0.1.8"
RELEASE_PROPS="service/src/main/resources/META-INF/nexus/release.properties"
# nexus-4mm24: the published engine-service tag the COLD box auto-acquires from.
# Must be >= the guided-upgrade version-pin floor (REQUIRED_RELEASE_VERSION); a
# stale default fail-closes the --cold MVV at the version gate. Kept literal (it
# names a PUBLISHED release tag, which need not equal the floor) but bumped to
# track it; override via NEXUS_SERVICE_TAG. (nexus-v0zmv)
COLD_TAG="${NEXUS_SERVICE_TAG:-engine-service-v0.1.12}"
for a in "$@"; do
  case "$a" in
    --with-cloud) WITH_CLOUD=1 ;;
    --no-build)   DO_BUILD=0 ;;
    --guided)     GUIDED=1 ;;   # RDR-002 ez5.13: drive nx guided-upgrade
    --cold)       COLD=1 ;;     # nexus-4mm24: cold-acquire from the published release
    --comprehensive) COMPREHENSIVE=1 ;;  # Phase D: daily-driver surface on the default rehearse.sh
    --stress)     STRESS=1 ;;            # Phase E: concurrency + queue-drain stress on the default rehearse.sh
    --fullstack)  FULLSTACK=1 ;;         # standalone: full topology (service + nx-mcp + claude) MCP-driven enqueue + worker drain
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# --guided: stamp release.properties so the native binary reports a release
# version (an unstamped build -> release_version=null -> version-pin fail-closes,
# which is not the success path this MVV exercises). Force a native rebuild so
# the stamp is actually baked in, and restore the file on exit. The stamp must
# happen BEFORE the native build below.
# Single restore hook for the stamped release.properties (folded into every EXIT
# trap below so a later `trap ... EXIT` does not clobber it). Defined + armed
# BEFORE the stamp mutation so a signal in the stamp window still restores it.
_guided_restore() {
  [ "$GUIDED" = 1 ] || return 0
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

if [ "$GUIDED" = 1 ]; then
  # --guided force-rebuilds the native binary with the stamp baked in, so it is
  # incompatible with --no-build (which would reuse a stale/unstamped binary).
  [ "$DO_BUILD" = 0 ] && { echo "--guided requires a fresh native build; drop --no-build" >&2; exit 2; }
  echo "[guided] stamping $RELEASE_PROPS release_version=$GUIDED_STAMP_VERSION (restored on exit)…"
  grep -v '^release_version=' "$RELEASE_PROPS" > "$RELEASE_PROPS.tmp"
  printf 'release_version=%s\n' "$GUIDED_STAMP_VERSION" >> "$RELEASE_PROPS.tmp"
  mv "$RELEASE_PROPS.tmp" "$RELEASE_PROPS"
  # Force a fresh native build so the stamp is baked in.
  rm -f service/target/nexus-service
fi

GRAAL_IMAGE="container-registry.oracle.com/graalvm/native-image-community:25"
if [ "$COLD" = 1 ]; then
  # nexus-4mm24: the cold box acquires the PUBLISHED binary + PG bundle at
  # runtime — NO local native build, NO stamping. Just the wheel.
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
    docker run --rm --entrypoint bash \
      --add-host=host.docker.internal:host-gateway \
      -v "$PWD":/src -w /src/service \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -e TESTCONTAINERS_RYUK_DISABLED=true \
      -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
      "$GRAAL_IMAGE" \
      -c './mvnw -B -Pnative -DskipTests -Dnative.image.opt=-Ob package'
  else
    echo "      (native binary already built — reusing service/target/nexus-service)"
  fi
else
  echo "[1-2/3] --no-build: reusing existing wheel + native binary"
fi

if [ "$COLD" = 0 ]; then
  ls dist/conexus-*.whl >/dev/null 2>&1 || { echo "no wheel in dist/ — drop --no-build" >&2; exit 1; }
  [ -x service/target/nexus-service ] || { echo "no native binary at service/target/nexus-service — drop --no-build" >&2; exit 1; }
fi

echo "[stage] Staging a minimal build context + building image (COLD=$COLD WITH_CLOUD=$WITH_CLOUD)…"
# Flatten wheel + JAR + driver to fixed names in a tiny throwaway context. The
# repo .dockerignore excludes dist/, and the inputs live in three different
# trees — staging sidesteps both without touching the shared .dockerignore.
STAGE="$(mktemp -d)"
trap '_guided_restore; rm -rf "$STAGE"' EXIT
cp "$(ls -t dist/conexus-*.whl | head -1)"            "$STAGE/"   # keep real PEP 427 name
if [ "$COLD" = 1 ]; then
  # nexus-4mm24: NOTHING the service needs is staged — the cold box acquires the
  # binary + PG bundle from the published release at runtime. Only the wheel +
  # the cold driver + seed travel in.
  cp "$HERE/Dockerfile.cold" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_cold.sh" "$HERE/seed_legacy.py" "$STAGE/"
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
  cp "$HERE/Dockerfile" "$HERE/rehearse.sh" "$HERE/rehearse_guided.sh" "$HERE/seed_legacy.py" "$STAGE/"
fi

# Docker Desktop's credsStore=desktop helper can't reach a locked login keychain
# in a non-interactive session, which fails even cached/anonymous image
# resolution at build time. Temporarily strip credsStore (the auths entries are
# empty), restore on exit. docker run is unaffected (only build-time auth fails).
DCFG="$HOME/.docker/config.json"
if [ -f "$DCFG" ] && grep -q '"credsStore"' "$DCFG"; then
  cp "$DCFG" "$STAGE/.docker-config.bak"
  python3 -c "import json,os;p=os.path.expanduser('~/.docker/config.json');d=json.load(open(p));d.pop('credsStore',None);json.dump(d,open(p,'w'),indent=2)"
  trap '_guided_restore; cp "$STAGE/.docker-config.bak" "$DCFG"; rm -rf "$STAGE"' EXIT
  echo "      (temporarily stripped credsStore from ~/.docker/config.json — restored on exit)"
fi

docker build -q -f "$STAGE/Dockerfile" -t "$IMAGE" "$STAGE" >/dev/null

run_env=(-e "WITH_CLOUD=$WITH_CLOUD" -e "COMPREHENSIVE=$COMPREHENSIVE" -e "STRESS=$STRESS")
if [ "$COLD" = 1 ]; then
  # nexus-4mm24: tell the cold box which published release to acquire from.
  run_env+=(-e "NEXUS_SERVICE_TAG=$COLD_TAG")
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
elif [ "$COLD" = 1 ]; then
  # nexus-4mm24: Dockerfile.cold's default entrypoint IS rehearse_cold.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
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
