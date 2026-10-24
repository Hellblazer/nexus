#!/usr/bin/env bash
# nexus-lp2qo: genuine Linux/arm64 verification of the native-image path, run on
# the macOS host via Docker Desktop (linux/arm64). Builds the native image inside
# the Oracle GraalVM 25.0.3 container using the Maven wrapper (./mvnw -Pnative),
# then boots the resulting Linux binary against a sibling pgvector and asserts the
# jOOQ + liquibase runtime path. Proves the committed metadata is Linux-correct
# (the spike verified macOS/arm64; CI proves amd64; this bridges to Linux now).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMG=container-registry.oracle.com/graalvm/native-image:25
NET=lp2qo-net
PG=lp2qo-lin-pg
BUILDER=lp2qo-lin-build

# nexus-rc4pz: bounded docker calls, as native-smoke.sh bounds its own (nexus-ub4ge). GNU timeout on
# Linux; gtimeout from Homebrew coreutils on a Mac; neither on a vanilla Mac, where the call runs
# unbounded as before, with a warning.
# --foreground keeps the command in the shell's process group, so a terminal's Ctrl-C reaches it (nexus-rc4pz).
_bounded() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout --foreground "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout --foreground "$secs" "$@"
  else "$@"; fi
}
command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1 \
  || echo "WARN: neither timeout nor gtimeout (brew install coreutils); every Docker call runs unbounded" >&2

cleanup() {
  _bounded 60 docker rm -f "$PG" "$BUILDER" >/dev/null 2>&1 || true
  _bounded 60 docker network rm "$NET" >/dev/null 2>&1 || true
  build_lease_release service 2>/dev/null || true
}
trap cleanup EXIT
cleanup

# nexus-8qdpe: the ./mvnw below runs INSIDE the container, but $REPO is bind-mounted,
# so service/target is the same single-writer resource every host build writes. Take
# the host-side build lease around the docker build, as tests/e2e/gc-ab/run-ab.sh does.
# shellcheck source=../scripts/lib/build-lease.sh
. "$REPO/scripts/lib/build-lease.sh"
build_lease_acquire_wait service "${NX_BUILD_LEASE_WAIT:-3600}" linux-native-verify || exit 1

# nexus-k6zxp: service/target's jar may be a hard link into the gate-jar cache. This
# build runs as root in the container, ignores the read-only bit and rewrites the jar
# in place, which would rewrite the cache entry too: unlink it first.
# shellcheck source=../scripts/lib/gate-jar-cache.sh
. "$REPO/scripts/lib/gate-jar-cache.sh"
gate_jar_cache_unshare "$REPO/service/target/nexus-service-1.0-SNAPSHOT.jar"

_bounded 60 docker network create "$NET" >/dev/null
_bounded 600 docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_DB=nexus -e POSTGRES_USER=nexus -e POSTGRES_PASSWORD=nexus \
  pgvector/pgvector:pg17 >/dev/null
echo "waiting for pg..."
# 90 attempts: 90 s when docker answers, about 9 min at most when every exec hangs to its 5 s bound.
for _ in $(seq 1 90); do _bounded 5 docker exec "$PG" pg_isready -U nexus >/dev/null 2>&1 && break; sleep 1; done
_bounded 5 docker exec "$PG" pg_isready -U nexus >/dev/null 2>&1 || { echo "FAIL: pgvector not ready after 90 attempts"; exit 2; }
sleep 2; echo "pg ready"

# Build + smoke inside the linux/arm64 GraalVM container. Docker socket + host
# override let the jOOQ codegen runner (service/src/build/java/JooqCodegen.java)
# reach the host daemon; the smoke
# pgvector is reached by container name on $NET. ONNX model mounted from host cache.
# Two hours bound the native build and smoke; a timeout leaves the container to cleanup()'s rm -f.
_bounded 7200 docker run --rm --name "$BUILDER" --platform linux/arm64 --network "$NET" \
  -v "$REPO:$REPO" -w "$REPO/service" \
  -v "$HOME/.m2:/root/.m2" \
  -v "$HOME/.cache/nexus/onnx_models:/root/.cache/nexus/onnx_models" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
  -e DOCKER_HOST=unix:///var/run/docker.sock \
  --add-host=host.docker.internal:host-gateway \
  --entrypoint bash "$IMG" -c '
    set -e
    echo "=== container: $(native-image --version | head -1) ==="
    echo "=== Linux native build via wrapper ==="
    ./mvnw -q -Pnative -DskipTests package
    echo "=== boot linux native binary + smoke ==="
    export NX_DB_URL="jdbc:postgresql://'"$PG"':5432/nexus" NX_DB_USER=nexus NX_DB_PASS=nexus
    export NX_SERVICE_PORT=8080 NX_SERVICE_TOKEN=lintoken
    ./target/nexus-service > /tmp/lin-svc.log 2>&1 &
    PID=$!
    UP=0
    for i in $(seq 1 60); do
      kill -0 $PID 2>/dev/null || { echo "SVC EXITED"; tail -30 /tmp/lin-svc.log; break; }
      curl -fsS http://localhost:8080/health >/dev/null 2>&1 && { UP=1; break; }
      sleep 1
    done
    [ "$UP" = 1 ] || { echo "LINUX SMOKE FAIL: not healthy"; tail -30 /tmp/lin-svc.log; exit 1; }
    echo -n "version : "; curl -s -H "Authorization: Bearer lintoken" http://localhost:8080/version; echo
    echo -n "put     : "; curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer lintoken" -H "Content-Type: application/json" -X POST -d "{\"project\":\"lin\",\"title\":\"a\",\"content\":\"linux native\",\"tags\":\"t\",\"ttl\":30}" http://localhost:8080/v1/memory/put; echo
    echo -n "get     : "; curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer lintoken" "http://localhost:8080/v1/memory/get?project=lin&title=a"; echo
    echo -n "search  : "; curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer lintoken" -H "Content-Type: application/json" -X POST -d "{\"query\":\"linux\",\"project\":\"lin\"}" http://localhost:8080/v1/memory/search; echo
    # bge-768 EMBED (nexus-pqatt): the DJL tokenizers JNI + onnx-run path that
    # SIGABRTed at lib.rs:475 under native-image. Model mounted from host cache.
    # RDR-204 Phase 2 fix round (nexus-ft04v.16 fix round): posts "model" directly
    # rather than a "collection" -- /v1/vectors/embed no longer serves an
    # unregistered collection name (422), and this probe registers nothing.
    echo -n "embed   : "
    ecode=$(curl -s -o /tmp/lin-embed.out -w "%{http_code}" -H "Authorization: Bearer lintoken" -H "Content-Type: application/json" -X POST -d "{\"model\":\"bge-base-en-v15-768\",\"texts\":[\"linux native embed\"]}" http://localhost:8080/v1/vectors/embed); echo "$ecode"
    kill -0 $PID 2>/dev/null || { echo "LINUX EMBED FAIL: service died on embed (SIGABRT?)"; tail -30 /tmp/lin-svc.log; exit 1; }
    [ "$ecode" = 200 ] && grep -q "\"embeddings\"" /tmp/lin-embed.out || { echo "LINUX EMBED FAIL: $ecode $(head -c160 /tmp/lin-embed.out)"; tail -30 /tmp/lin-svc.log; exit 1; }
    grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointer" /tmp/lin-svc.log && { echo "LINUX RUNTIME ERROR"; exit 1; } || true
    kill $PID 2>/dev/null || true
    echo "LINUX NATIVE OK"
  '
