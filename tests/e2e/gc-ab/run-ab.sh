#!/usr/bin/env bash
# GC A/B: measure the runtime-GC choice (--gc=serial vs --gc=G1) compiled into
# the linux native nexus-service, under deterministic concurrent T2 load.
#
# Motivation (2026-07-13, Hal): before switching the cloud engine's runtime GC
# for throughput, measure it — no flag flips without evidence. Serial is the
# GraalVM default and the only option on macOS; G1 is linux-only.
#
# Method: build the linux binary twice at -Ob (same opt level both sides; GC
# choice is orthogonal to compile optimization — note results are -Ob, the
# published binary is -O2), boot each against its own throwaway pgvector on a
# private docker network, drive load_driver.py from the host (8 workers x 150
# iterations x 3 ops = 3600 requests, fixed budget), sample the service
# container's RSS via docker stats once per second, print both JSON lines and
# a delta summary.
#
# Requires: docker, uv, the repo checkout. ~15 min total on a 32GB Docker VM.
set -euo pipefail
cd "$(dirname "$0")/../../.."   # repo root

GRAAL_IMAGE="container-registry.oracle.com/graalvm/native-image-community:25"
OUT="tests/e2e/gc-ab/out"
WORKERS="${GCAB_WORKERS:-8}"
ITERS="${GCAB_ITERS:-150}"
mkdir -p "$OUT"

build_variant() { # gc-name
  local gc="$1"
  echo "[build] --gc=${gc} (-Ob, linux, GraalVM container)…"
  rm -f service/target/nexus-service
  docker run --rm --entrypoint bash \
    --add-host=host.docker.internal:host-gateway \
    -v "$PWD":/src -w /src/service \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e TESTCONTAINERS_RYUK_DISABLED=true \
    -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
    "$GRAAL_IMAGE" \
    -c "./mvnw -q -B -Pnative -DskipTests -Dnative.image.opt=-Ob -Dnative.image.gc=${gc} package" \
    > "$OUT/build-${gc}.log" 2>&1
  mkdir -p "$OUT/$gc"
  cp service/target/nexus-service "$OUT/$gc/"
  # native-image dlopen's its .so siblings from the executable's own dir.
  cp service/target/*.so "$OUT/$gc/" 2>/dev/null || true
  echo "[build] ${gc}: $(du -h "$OUT/$gc/nexus-service" | cut -f1) binary"
}

measure_variant() { # gc-name
  local gc="$1" net="gcab-net" pg="gcab-pg" svc="gcab-svc"
  docker rm -f "$pg" "$svc" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  docker network create "$net" >/dev/null
  docker run -d --name "$pg" --network "$net" \
    -e POSTGRES_DB=nexus -e POSTGRES_USER=nexus -e POSTGRES_PASSWORD=nexus \
    pgvector/pgvector:pg17 >/dev/null
  until docker exec "$pg" pg_isready -U nexus >/dev/null 2>&1; do sleep 1; done
  sleep 2

  local port
  port=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
  docker run -d --name "$svc" --network "$net" -p "${port}:8080" \
    -v "$PWD/$OUT/$gc":/svc:ro \
    -e NX_DB_URL="jdbc:postgresql://${pg}:5432/nexus" \
    -e NX_DB_USER=nexus -e NX_DB_PASS=nexus \
    -e NX_SERVICE_PORT=8080 -e NX_SERVICE_TOKEN=gcabtoken \
    --entrypoint /svc/nexus-service \
    "$GRAAL_IMAGE" >/dev/null

  local up=0
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1 && { up=1; break; }
    sleep 1
  done
  [ "$up" = 1 ] || { echo "FAIL: ${gc} service never became healthy"; docker logs "$svc" | tail -20; return 1; }

  # RSS sampler (1 Hz) for the duration of the load run.
  : > "$OUT/rss-${gc}.log"
  ( while docker inspect "$svc" >/dev/null 2>&1; do
      docker stats --no-stream --format '{{.MemUsage}}' "$svc" 2>/dev/null | cut -d/ -f1 >> "$OUT/rss-${gc}.log"
      sleep 1
    done ) &
  local sampler=$!

  echo "[load] ${gc}: ${WORKERS} workers x ${ITERS} iterations x 3 ops…"
  uv run python tests/e2e/gc-ab/load_driver.py \
    "http://127.0.0.1:${port}" gcabtoken "$WORKERS" "$ITERS" > "$OUT/result-${gc}.json"

  kill "$sampler" 2>/dev/null || true
  docker rm -f "$svc" "$pg" >/dev/null 2>&1
  docker network rm "$net" >/dev/null 2>&1
  echo "[load] ${gc}: done — $(cat "$OUT/result-${gc}.json")"
  echo "[rss]  ${gc}: peak $(sort -h "$OUT/rss-${gc}.log" | tail -1)"
}

for gc in serial G1; do build_variant "$gc"; done
for gc in serial G1; do measure_variant "$gc"; done

echo
echo "== GC A/B summary (linux -Ob, ${WORKERS}x${ITERS}x3 requests) =="
python3 - <<'PYEOF'
import json
for gc in ("serial", "G1"):
    r = json.load(open(f"tests/e2e/gc-ab/out/result-{gc}.json"))
    rss = sorted(open(f"tests/e2e/gc-ab/out/rss-{gc}.log").read().split())[-1]
    print(f"{gc:>6}: {r['rps']:>7} req/s  wall {r['wall_s']}s  5xx={r['server_5xx']}  "
          f"put p50/p95/p99 {r['put']['p50']}/{r['put']['p95']}/{r['put']['p99']}ms  "
          f"search p95 {r['search']['p95']}ms  peak RSS {rss.strip()}")
PYEOF
