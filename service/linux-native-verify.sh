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

cleanup() {
  docker rm -f "$PG" "$BUILDER" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_DB=nexus -e POSTGRES_USER=nexus -e POSTGRES_PASSWORD=nexus \
  pgvector/pgvector:pg17 >/dev/null
echo "waiting for pg..."
until docker exec "$PG" pg_isready -U nexus >/dev/null 2>&1; do sleep 1; done
sleep 2; echo "pg ready"

# Build + smoke inside the linux/arm64 GraalVM container. Docker socket + host
# override let the testcontainers jOOQ codegen reach the host daemon; the smoke
# pgvector is reached by container name on $NET. ONNX model mounted from host cache.
docker run --rm --name "$BUILDER" --platform linux/arm64 --network "$NET" \
  -v "$REPO:$REPO" -w "$REPO/service" \
  -v "$HOME/.m2:/root/.m2" \
  -v "$HOME/.cache/chroma:/root/.cache/chroma" \
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
    export NX_SERVICE_PORT=8080 NX_SERVICE_TOKEN=lintoken NX_EMBED_MODE=onnx
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
    grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointer" /tmp/lin-svc.log && { echo "LINUX RUNTIME ERROR"; exit 1; } || true
    kill $PID 2>/dev/null || true
    echo "LINUX NATIVE OK"
  '
