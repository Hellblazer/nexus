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
for a in "$@"; do
  case "$a" in
    --with-cloud) WITH_CLOUD=1 ;;
    --no-build)   DO_BUILD=0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

GRAAL_IMAGE="container-registry.oracle.com/graalvm/native-image-community:25"
if [ "$DO_BUILD" = 1 ]; then
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

ls dist/conexus-*.whl >/dev/null 2>&1 || { echo "no wheel in dist/ — drop --no-build" >&2; exit 1; }
[ -x service/target/nexus-service ] || { echo "no native binary at service/target/nexus-service — drop --no-build" >&2; exit 1; }

echo "[3/3] Staging a minimal build context + building image (WITH_CLOUD=$WITH_CLOUD)…"
# Flatten wheel + JAR + driver to fixed names in a tiny throwaway context. The
# repo .dockerignore excludes dist/, and the inputs live in three different
# trees — staging sidesteps both without touching the shared .dockerignore.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$(ls -t dist/conexus-*.whl | head -1)"            "$STAGE/"   # keep real PEP 427 name
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
cp "$HERE/Dockerfile" "$HERE/rehearse.sh" "$HERE/seed_legacy.py" "$STAGE/"

# Docker Desktop's credsStore=desktop helper can't reach a locked login keychain
# in a non-interactive session, which fails even cached/anonymous image
# resolution at build time. Temporarily strip credsStore (the auths entries are
# empty), restore on exit. docker run is unaffected (only build-time auth fails).
DCFG="$HOME/.docker/config.json"
if [ -f "$DCFG" ] && grep -q '"credsStore"' "$DCFG"; then
  cp "$DCFG" "$STAGE/.docker-config.bak"
  python3 -c "import json,os;p=os.path.expanduser('~/.docker/config.json');d=json.load(open(p));d.pop('credsStore',None);json.dump(d,open(p,'w'),indent=2)"
  trap 'cp "$STAGE/.docker-config.bak" "$DCFG"; rm -rf "$STAGE"' EXIT
  echo "      (temporarily stripped credsStore from ~/.docker/config.json — restored on exit)"
fi

docker build -q -f "$STAGE/Dockerfile" -t "$IMAGE" "$STAGE" >/dev/null

run_env=(-e "WITH_CLOUD=$WITH_CLOUD")
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
# restores ~/.docker/config.json and removes the staging dir. Run as a child and
# propagate its exit code.
docker run --rm "${run_env[@]}" "$IMAGE"
rc=$?
exit "$rc"
