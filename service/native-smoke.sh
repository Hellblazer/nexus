#!/usr/bin/env bash
# nexus-lp2qo: native-image smoke test. Boots the native binary against a
# pgvector Postgres and asserts the full runtime path works: liquibase migration
# applies, and jOOQ INSERT / SELECT / Postgres-FTS-search all return 200 with
# real rows. Exits non-zero on any failure — used as the CI native gate and for
# local verification.
#
# Env (optional):
#   NX_DB_URL / NX_DB_USER / NX_DB_PASS  — point at an existing Postgres.
#       When unset, a throwaway pgvector/pgvector:pg17 container is started.
#   BIN  — path to the native binary (default: target/nexus-service)
set -uo pipefail
cd "$(dirname "$0")"
BIN="${BIN:-target/nexus-service}"
[ -x "$BIN" ] || { echo "FAIL: native binary not found/executable at $BIN"; exit 2; }

OWN_PG=0
if [ -z "${NX_DB_URL:-}" ]; then
  OWN_PG=1
  PGPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
  docker rm -f lp2qo-smoke-pg >/dev/null 2>&1 || true
  docker run -d --name lp2qo-smoke-pg -e POSTGRES_DB=nexus -e POSTGRES_USER=nexus \
    -e POSTGRES_PASSWORD=nexus -p ${PGPORT}:5432 pgvector/pgvector:pg17 >/dev/null
  until docker exec lp2qo-smoke-pg pg_isready -U nexus >/dev/null 2>&1; do sleep 1; done
  sleep 2
  export NX_DB_URL="jdbc:postgresql://localhost:${PGPORT}/nexus"
  export NX_DB_USER=nexus NX_DB_PASS=nexus
fi

SVCPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
export NX_SERVICE_PORT=$SVCPORT NX_SERVICE_TOKEN=smoketoken NX_EMBED_MODE=onnx

cleanup() {
  [ -n "${SVCPID:-}" ] && kill "$SVCPID" 2>/dev/null
  [ "$OWN_PG" = "1" ] && docker rm -f lp2qo-smoke-pg >/dev/null 2>&1
}
trap cleanup EXIT

"$BIN" > /tmp/native-smoke-svc.log 2>&1 &
SVCPID=$!
U="http://localhost:${SVCPORT}"

UP=0
for i in $(seq 1 60); do
  kill -0 $SVCPID 2>/dev/null || { echo "FAIL: service exited during startup"; tail -40 /tmp/native-smoke-svc.log; exit 1; }
  curl -fsS "$U/health" >/dev/null 2>&1 && { UP=1; break; }
  sleep 1
done
[ "$UP" = "1" ] || { echo "FAIL: service never became healthy"; tail -40 /tmp/native-smoke-svc.log; exit 1; }

# Migration must have applied (changeset_count > 0).
VER=$(curl -fsS -H "Authorization: Bearer smoketoken" "$U/version")
echo "version: $VER"
echo "$VER" | grep -qE '"schema_changeset_count":[1-9]' || { echo "FAIL: migration did not apply"; tail -40 /tmp/native-smoke-svc.log; exit 1; }

fail=0
assert() { # name expected_code curl-args...
  local name="$1" exp="$2"; shift 2
  local code; code=$(curl -s -o /tmp/ns.out -w "%{http_code}" "$@")
  if [ "$code" = "$exp" ]; then echo "  ok   $name -> $code"; else echo "  FAIL $name -> $code (want $exp): $(head -c160 /tmp/ns.out)"; fail=1; fi
}
A=(-H "Authorization: Bearer smoketoken"); J=(-H "Content-Type: application/json")
echo "jOOQ runtime path:"
assert "memory/put (INSERT)"   200 "${A[@]}" "${J[@]}" -X POST -d '{"project":"smoke","title":"a","content":"native ok","tags":"t","ttl":30}' "$U/v1/memory/put"
assert "memory/get (SELECT)"   200 "${A[@]}" "$U/v1/memory/get?project=smoke&title=a"
assert "memory/search (FTS)"   200 "${A[@]}" "${J[@]}" -X POST -d '{"query":"native","project":"smoke"}' "$U/v1/memory/search"
assert "memory/list"           200 "${A[@]}" "$U/v1/memory/list?project=smoke"
assert "plans/search"          200 "${A[@]}" "${J[@]}" -X POST -d '{"query":"q","project":"smoke"}' "$U/v1/plans/search"
assert "taxonomy/topics"       200 "${A[@]}" "$U/v1/taxonomy/topics?collection=knowledge__x"
assert "chash/distinct"        200 "${A[@]}" "$U/v1/chash/distinct_collections"

if grep -qiE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log; then
  echo "FAIL: native runtime error in service log:"; grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log | head; fail=1
fi

if [ "$fail" = "0" ]; then echo "NATIVE SMOKE PASS"; exit 0; else echo "NATIVE SMOKE FAIL"; exit 1; fi
