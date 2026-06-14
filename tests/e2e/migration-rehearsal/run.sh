#!/usr/bin/env bash
# Host orchestrator for the soup-to-nuts migration dress rehearsal.
#
#   tests/e2e/migration-rehearsal/run.sh              # ONNX leg only (secret-free)
#   tests/e2e/migration-rehearsal/run.sh --with-cloud # + Voyage leg (reads .env)
#   tests/e2e/migration-rehearsal/run.sh --no-build   # reuse existing wheel/JAR
#
# Builds the wheel + service JAR on the host (fast, cached), bakes them into an
# ephemeral image with PG16 + pgvector + a JRE, and runs the full operator path
# (install → provision → serve → seed → migrate → validate → rollback) in one
# throwaway container. NOT DinD: PG is provisioned inside the box by nx itself.
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

if [ "$DO_BUILD" = 1 ]; then
  echo "[1/3] Building the conexus wheel (host)…"
  uv build --wheel >/dev/null 2>&1
  echo "[2/3] Building the nexus-service JAR (host)…"
  if ! ls service/target/nexus-service-*.jar >/dev/null 2>&1; then
    (cd service && mvn -o -q -DskipTests package)
  else
    echo "      (JAR already built — reusing $(ls service/target/nexus-service-*.jar | head -1))"
  fi
else
  echo "[1-2/3] --no-build: reusing existing wheel + JAR"
fi

ls dist/conexus-*.whl >/dev/null 2>&1 || { echo "no wheel in dist/ — drop --no-build" >&2; exit 1; }
ls service/target/nexus-service-*.jar >/dev/null 2>&1 || { echo "no service JAR — drop --no-build" >&2; exit 1; }

echo "[3/3] Staging a minimal build context + building image (WITH_CLOUD=$WITH_CLOUD)…"
# Flatten wheel + JAR + driver to fixed names in a tiny throwaway context. The
# repo .dockerignore excludes dist/, and the inputs live in three different
# trees — staging sidesteps both without touching the shared .dockerignore.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$(ls -t dist/conexus-*.whl | head -1)"            "$STAGE/"   # keep real PEP 427 name
cp "$(ls -t service/target/nexus-service-*.jar | head -1)" "$STAGE/nexus-service.jar"
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
