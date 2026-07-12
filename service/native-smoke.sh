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

# ── T1 scratch (separate jOOQ schema, nexus-opr9m) ───────────────────────────
# T1 scratch lives in its OWN generated jOOQ schema (t1, e.g.
# dev.nexus.service.jooq.t1.T1) — a completely separate schema model from every
# assertion above (all of which are in the `nexus` schema). JooqRecordReflectionFeature
# enumerated only Nexus.NEXUS.getTables() and never T1.T1.getTables(), so
# ScratchRecord's constructor was unreachable via reflection in every native image
# built since the t1 schema was introduced (nexus-gmiaf.13) — every deployed
# get/search/list against T1 500'd with MissingReflectionRegistrationError, and
# this gate (which ran on every native build the whole time) never caught it because
# nothing above touches /v1/t1/* or /v1/sessions/*. Mint a session first (the real
# production path — mirrors mcp/core.py's lifespan) then exercise the full T1
# put/get/search/list surface so a future new generated schema being added without
# updating JooqRecordReflectionFeature fails HERE, not silently in production.
echo "T1 scratch runtime path (separate jOOQ schema):"
SESSION_RESP=$(curl -fsS "${A[@]}" "${J[@]}" -X POST -d '{"session_id":"native-smoke-t1"}' "$U/v1/sessions/start")
SESSION_TOKEN=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['session_token'])" "$SESSION_RESP" 2>/dev/null)
if [ -z "$SESSION_TOKEN" ]; then
  echo "  FAIL t1/session-mint -> could not mint session: $SESSION_RESP"; fail=1
else
  echo "  ok   t1/session-mint -> 200"
  T1=(-H "Authorization: Bearer smoketoken" -H "X-Nexus-T1-Session: ${SESSION_TOKEN}")
  PUT_RESP=$(curl -fsS "${T1[@]}" "${J[@]}" -X POST -d '{"id":"native-smoke-t1-id","session_id":"native-smoke-t1","content":"t1 native smoke","tags":"","flagged":false}' "$U/v1/t1/put")
  echo "$PUT_RESP" | grep -q '"id"' && echo "  ok   t1/put (INSERT) -> 200" || { echo "  FAIL t1/put -> $PUT_RESP"; fail=1; }
  assert "t1/get (SELECT, separate schema)"  200 "${T1[@]}" "${J[@]}" -X POST -d '{"id":"native-smoke-t1-id","session_id":"native-smoke-t1"}' "$U/v1/t1/get"
  assert "t1/search (FTS, separate schema)"  200 "${T1[@]}" "${J[@]}" -X POST -d '{"query":"native smoke","session_id":"native-smoke-t1","limit":5}' "$U/v1/t1/search"
  assert "t1/list (separate schema)"         200 "${T1[@]}" "${J[@]}" -X POST -d '{"session_id":"native-smoke-t1"}' "$U/v1/t1/list"
fi

# ── T1 via the REAL Python client (nexus-97oz3) ──────────────────────────────
# Everything above proves the native binary's /v1/t1/* endpoints work when
# driven by raw curl. It does NOT prove nexus.db.t1.get_t1_database()'s
# three-tier session-routing, HttpTokenStore.start_session() minting, or
# HttpScratchStore's request/response handling actually work against this
# SAME compiled artifact — nexus-opr9m (the reflection bug this file's T1
# section above was written to catch) came back clean on the JVM AND would
# have come back clean here too if this section had bypassed the real client
# code the way the curl section above does. Routing-correctness (tested
# elsewhere, against a JVM backend) and backend-correctness (tested above,
# via curl) have never been proven TOGETHER against the actual production
# artifact reached through the actual production client code. This closes
# that seam FOR T1 SPECIFICALLY: mint + put + get + search + list through
# the real Python nexus.db.t1.get_t1_database() factory (the identical code
# path a live bare-CLI `nx scratch` invocation takes) against the native
# binary this script just booted.
#
# NOT YET CLOSED for the other jOOQ-backed endpoint families this script
# already curls above (memory/plans/taxonomy/chash) — each has its own real
# Python HTTP client (src/nexus/db/t2/http_memory_store.py and siblings)
# that is equally untested against the compiled artifact. Same gap, same
# fix shape, different bead (not filed as of this writing — check for one
# before assuming this comment is stale).
#
# ALSO requires `uv` on PATH with the nexus package importable (`uv sync`
# from the repo root) to actually run — see the CI workflow step this
# depends on (engine-service-release.yml, gated on matrix.target.smoke).
# WARN+skip loudly if unavailable rather than silently pass — a run without
# this dependency present does NOT cover routing+backend together, only
# backend-via-curl above, regardless of how green the rest of the output is.
# NOTE (unlike the bge-embed section below, whose model IS provisioned in CI
# via .github/actions/prime-bge-onnx and only rarely skips): until
# nexus-l8ybx's companion CI workflow change lands, this WARN is the
# deterministic 100%-of-runs outcome in CI, not a rare fallback — do not
# read a green run as proof this section executed.
echo "T1 via the real Python client (routing + backend together):"
REPO_ROOT="$(cd .. && pwd)"
# `timeout` is GNU coreutils -- present on every CI runner (Linux) but not
# guaranteed on a vanilla local macOS dev machine (only via `brew install
# coreutils`, and even then often as `gtimeout`). Degrade gracefully rather
# than break local runs that lack it.
TIMEOUT_CMD=""
command -v timeout >/dev/null 2>&1 && TIMEOUT_CMD="timeout 60"
if command -v uv >/dev/null 2>&1 && [ -f "$REPO_ROOT/pyproject.toml" ]; then
  T1_PY_TMPDIR=$(mktemp -d)
  PY_OUT=$(cd "$REPO_ROOT" && NEXUS_CONFIG_DIR="$T1_PY_TMPDIR" \
    NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$SVCPORT" NX_SERVICE_TOKEN=smoketoken \
    NX_STORAGE_BACKEND=service \
    $TIMEOUT_CMD uv run python -c '
from nexus.db.t1 import get_t1_database

t1 = get_t1_database()
doc_id = t1.put("t1 native smoke via real python client", tags="native-smoke-py")
assert doc_id, "put returned no id"

got = t1.get(doc_id)
assert got is not None, "get returned None for a just-put id"
assert got["content"] == "t1 native smoke via real python client", got

results = t1.search("native smoke via real python", n_results=5)
assert any(r["id"] == doc_id for r in results), f"search did not find {doc_id}: {results}"

entries = t1.list_entries()
assert any(e["id"] == doc_id for e in entries), f"list_entries did not find {doc_id}: {entries}"

print("OK")
' 2>&1)
  rm -rf "$T1_PY_TMPDIR"
  if echo "$PY_OUT" | grep -q "^OK$"; then
    echo "  ok   t1 real-client put/get/search/list (routing + backend together)"
  else
    echo "  FAIL t1 real-client check:"; echo "$PY_OUT" | sed 's/^/    /'; fail=1
  fi
else
  echo "  WARN skipping (uv or pyproject.toml not found at $REPO_ROOT) -- this run does NOT cover routing+backend together, only backend-via-curl above"
fi

# ── Local bge-768 EMBED (nexus-pqatt) ────────────────────────────────────────
# The embed path drives the DJL HuggingFace tokenizers JNI (libtokenizers.so) and
# the onnxruntime session run — both of which need jniAccessible registrations the
# native image previously omitted, so the FIRST embed SIGABRTed at lib.rs:475
# (Result::unwrap on a JavaException) while every other endpoint above worked. The
# old gate never embedded, so the crash shipped. We assert the encode+infer path
# returns a real 768-dim vector. Requires the ~416MB bge ONNX model (provisioned by
# `nx init --service`). If it is absent we WARN+skip loudly rather than silently
# pass — a model-less CI run must not read as "embed covered".
BGE_MODEL="${NX_BGE_MODEL_PATH:-$HOME/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/model.onnx}"
if [ -f "$BGE_MODEL" ]; then
  echo "local bge-768 embed path:"
  ecode=$(curl -s -o /tmp/ns-embed.out -w "%{http_code}" "${A[@]}" "${J[@]}" -X POST \
    -d '{"collection":"knowledge__x","texts":["native embed smoke"]}' "$U/v1/vectors/embed")
  if [ "$ecode" = "200" ] && grep -q '"embeddings"' /tmp/ns-embed.out \
     && [ "$(python3 -c "import json,sys;print(len(json.load(open('/tmp/ns-embed.out'))['embeddings'][0]))" 2>/dev/null)" = "768" ]; then
    echo "  ok   embed (DJL tokenizer JNI + onnx run) -> 200, 768-dim"
  else
    echo "  FAIL embed -> $ecode (want 200 + 768-dim): $(head -c200 /tmp/ns-embed.out)"; fail=1
  fi
else
  echo "  WARN embed path NOT covered — bge model absent at $BGE_MODEL"
  echo "       (set NX_BGE_MODEL_PATH or provision via 'nx init --service' to gate the JNI embed path)"
fi

if grep -qiE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log; then
  echo "FAIL: native runtime error in service log:"; grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log | head; fail=1
fi

# ── Voyage-mode boot + egress-proxy wiring (nexus-myg2d) ──────────────────────
# The local-mode boot above never exercises the CLOUD (voyage) config path — the
# exact coverage gap that let two native-image-vs-JVM regressions ship to conexus
# deploys: nexus-0n7uc (voyage-branch OnnxEmbedder boot segfault) and nexus-f1syh
# (Voyage HttpClient ignored the egress proxy). Boot the SAME binary in voyage mode
# with a proxy and assert it (1) boots clean (segfault guard), (2) selected voyage
# mode, (3) wired the proxy onto the client from HTTPS_PROXY. The proxy points at a
# closed local port: construction must succeed (no network call at build time);
# real Voyage routing-through-proxy is covered by EgressProxyTest + the cloud STEP-6.
echo "voyage-mode boot + egress proxy:"
kill "$SVCPID" 2>/dev/null; wait "$SVCPID" 2>/dev/null
# The engine migrates on boot (Main: SchemaMigrator before the HTTP bind), so a second
# boot must target a CLEAN database — re-running Liquibase over the local-mode DB hits
# "relation already exists". With our own PG, create a fresh DB in the same container;
# with an external NX_DB_URL we can't safely reset it, so skip this phase there.
if [ "$OWN_PG" != "1" ]; then
  echo "  skip   voyage-mode phase (external NX_DB_URL — needs a clean DB)"
else
  docker exec lp2qo-smoke-pg psql -U nexus -d nexus -c 'CREATE DATABASE voyagesmoke;' >/dev/null 2>&1 || true
  DEADPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
  NX_DB_URL="jdbc:postgresql://localhost:${PGPORT}/voyagesmoke" \
    NX_VOYAGE_API_KEY=dummy-smoke-key HTTPS_PROXY="http://127.0.0.1:${DEADPORT}" \
    "$BIN" > /tmp/native-smoke-voyage.log 2>&1 &
SVCPID=$!
VUP=0
for i in $(seq 1 60); do
  kill -0 $SVCPID 2>/dev/null || { echo "FAIL: voyage-mode service exited during startup (segfault?)"; tail -40 /tmp/native-smoke-voyage.log; exit 1; }
  curl -fsS "$U/health" >/dev/null 2>&1 && { VUP=1; break; }
  sleep 1
done
[ "$VUP" = "1" ] || { echo "FAIL: voyage-mode service never became healthy"; tail -40 /tmp/native-smoke-voyage.log; exit 1; }
# (2) took the cloud (voyage) embedding branch, not local bge/onnx
if grep -qE 'event=embedding_mode_banner mode=voyage' /tmp/native-smoke-voyage.log; then
  echo "  ok   voyage-mode boot (no segfault)"
else
  echo "  FAIL voyage mode not selected:"; grep embedding_mode_banner /tmp/native-smoke-voyage.log | head; fail=1
fi
# (3) EgressProxy parsed HTTPS_PROXY and set the proxy on the Voyage client
if grep -qE "event=egress_proxy_configured.*port=${DEADPORT}" /tmp/native-smoke-voyage.log; then
  echo "  ok   egress proxy wired from HTTPS_PROXY -> 127.0.0.1:${DEADPORT}"
else
  echo "  FAIL egress proxy not configured from HTTPS_PROXY:"; grep egress_proxy /tmp/native-smoke-voyage.log | head; fail=1
fi
if grep -qiE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-voyage.log; then
  echo "FAIL: native runtime error in voyage-mode service log:"; grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-voyage.log | head; fail=1
fi
fi  # end voyage-mode phase (OWN_PG)

if [ "$fail" = "0" ]; then echo "NATIVE SMOKE PASS"; exit 0; else echo "NATIVE SMOKE FAIL"; exit 1; fi
