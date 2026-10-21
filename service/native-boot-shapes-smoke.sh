#!/usr/bin/env bash
# nexus-hgzx4 (RDR-210 Phase 2 Step 2): native-image smoke of the boot shapes the
# embedder presence probe produces at the edges, run against the real native binary:
#
#   cloud-shape      key set, no bge files: boots Voyage-only, /version reports voyage,
#                    no ONNX Runtime extraction, clean SIGTERM exit (143).
#   neither-family   no key, no bge files: exits non-zero, never ready, output names
#                    BOTH remedies (NX_VOYAGE_API_KEY and `nx init`).
#   truncated-model  both bge files present, model a few bytes: exits non-zero naming
#                    the size floor (never "absent"), keyless AND keyed (Sam's ruling:
#                    a both-files-present truncated model fails loud even with a key).
#
# Every boot gets its own EMPTY NX_ORT_NATIVE_DIR and its own NX_ONNX_MODEL_DIR under
# one per-run private scratch dir, and a clean env (env -i) so an ambient
# NX_VOYAGE_API_KEY / HOME never changes the shape. Fake tiny files stand in for the
# tokenizer and model; no real model is downloaded and no Voyage call is made (the
# key value is a dummy; the boot makes no Voyage request).
#
# Prints one PASS/FAIL line per shape (and per leg), exits non-zero on any failure.
#
# Env (optional):
#   BIN  — native binary (default: target/nexus-service, relative to this script)
#   BOOT_SHAPES_PG_CONTAINER / BOOT_SHAPES_PG_PORT — an already-running pgvector
#       container to create throwaway databases in (native-smoke.sh passes its own).
#       When unset and NX_DB_URL is unset, a throwaway pgvector/pgvector:pg17
#       container is started. With only an external NX_DB_URL the script cannot
#       create clean databases and SKIPS loudly (exit 0, "NOT covered").
#
# The engine migrates BEFORE it probes the embedders, and re-running Liquibase over a
# migrated database is not a clean boot, so every boot targets its own fresh database.
# SC2016: ck's detail argument is eval'd lazily on failure, so it is single-quoted on purpose.
# SC2329: cleanup and the predicates are invoked via trap and via ck.
# shellcheck disable=SC2016,SC2329
set -uo pipefail

# Resolve BIN against the caller's cwd first, then this script's directory, before cd.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="${BIN:-target/nexus-service}"
case "$BIN" in
  /*) ;;
  *) if [ -x "$BIN" ]; then BIN="$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"
     else BIN="$SCRIPT_DIR/$BIN"; fi ;;
esac
[ -x "$BIN" ] || { echo "FAIL: native binary not found/executable at $BIN"; exit 2; }
cd "$SCRIPT_DIR" || exit 2

case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) WINLIKE=1 ;; *) WINLIKE=0 ;; esac

SHAPES_TMP=$(mktemp -d "${TMPDIR:-/tmp}/native-boot-shapes.XXXXXX") \
  || { echo "FAIL: cannot create scratch dir under ${TMPDIR:-/tmp}"; exit 2; }

PGC="${BOOT_SHAPES_PG_CONTAINER:-}"
PGP="${BOOT_SHAPES_PG_PORT:-}"
OWN_PG=0
BOOT_PID=""
fail=0
leg_ok=0

cleanup() {
  local rc=$?
  [ -n "$BOOT_PID" ] && kill -9 "$BOOT_PID" 2>/dev/null
  [ "$OWN_PG" = "1" ] && docker rm -f "$PGC" >/dev/null 2>&1
  if [ "$rc" = "0" ]; then
    rm -rf "$SHAPES_TMP"
  else
    echo "native-boot-shapes: FAILED (exit $rc); logs kept in $SHAPES_TMP" >&2
  fi
  return "$rc"
}
trap cleanup EXIT

free_port() { python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()"; }

if [ -z "$PGC" ]; then
  if [ -n "${NX_DB_URL:-}" ]; then
    echo "SKIP boot shapes: external NX_DB_URL cannot give each boot a clean database."
    echo "     WARN the cloud shape and the two failure boots are NOT covered by this run."
    # A gate that skip-passes when its dependency is absent needs a max-skip: a release
    # leg sets NATIVE_SMOKE_REQUIRE_SHAPES=1 so a skipped shapes block fails the run.
    if [ "${NATIVE_SMOKE_REQUIRE_SHAPES:-0}" = "1" ]; then
      echo "FAIL: NATIVE_SMOKE_REQUIRE_SHAPES=1 and the boot shapes were skipped"
      exit 3
    fi
    exit 0
  fi
  OWN_PG=1
  PGC="nx-bootshapes-pg-$$"
  PGP=$(free_port)
  docker rm -f "$PGC" >/dev/null 2>&1 || true
  docker run -d --name "$PGC" -e POSTGRES_DB=nexus -e POSTGRES_USER=nexus \
    -e POSTGRES_PASSWORD=nexus -p "127.0.0.1:${PGP}:5432" pgvector/pgvector:pg17 >/dev/null \
    || { echo "FAIL: cannot start the throwaway pgvector container"; exit 2; }
  for _ in $(seq 1 60); do docker exec "$PGC" pg_isready -U nexus >/dev/null 2>&1 && break; sleep 1; done
  docker exec "$PGC" pg_isready -U nexus >/dev/null 2>&1 || { echo "FAIL: pgvector never became ready"; exit 2; }
  sleep 2
fi
[ -n "$PGP" ] || { echo "FAIL: BOOT_SHAPES_PG_CONTAINER set without BOOT_SHAPES_PG_PORT"; exit 2; }

# boot_prepare <name>: a fresh database, port, empty ORT dir and empty model dir for one boot.
# Sets BOOT_DIR, BOOT_LOG, BOOT_ORT, BOOT_MODELS, BOOT_DB, BOOT_PORT. The caller may populate
# BOOT_MODELS before boot_start.
boot_prepare() {
  local name="$1"
  BOOT_DIR="$SHAPES_TMP/$name"
  BOOT_LOG="$BOOT_DIR/svc.log"
  BOOT_ORT="$BOOT_DIR/ort_native"
  BOOT_MODELS="$BOOT_DIR/onnx_models"
  BOOT_DB="bootshape_$(echo "$name" | tr -c 'a-zA-Z0-9' '_')"
  mkdir -p "$BOOT_DIR/jtmp" "$BOOT_DIR/home" "$BOOT_ORT" "$BOOT_MODELS"
  docker exec "$PGC" psql -U nexus -d nexus -c "DROP DATABASE IF EXISTS $BOOT_DB;" >/dev/null 2>&1
  docker exec "$PGC" psql -U nexus -d nexus -c "CREATE DATABASE $BOOT_DB;" >/dev/null 2>&1 \
    || { echo "FAIL: cannot create database $BOOT_DB in $PGC"; return 1; }
  BOOT_PORT=$(free_port)
}

# boot_start <voyage-key-or-empty>: start the binary in a clean env. env execs the binary, so
# BOOT_PID is the engine's own pid.
boot_start() {
  local key="$1"
  local -a keyenv=()
  [ -n "$key" ] && keyenv=("NX_VOYAGE_API_KEY=$key")
  env -i PATH="$PATH" HOME="$BOOT_DIR/home" TMPDIR="$BOOT_DIR/jtmp" \
    NX_DB_URL="jdbc:postgresql://localhost:${PGP}/${BOOT_DB}" NX_DB_USER=nexus NX_DB_PASS=nexus \
    NX_SERVICE_PORT="$BOOT_PORT" NX_SERVICE_TOKEN=smoketoken \
    NX_ONNX_MODEL_DIR="$BOOT_MODELS" NX_ORT_NATIVE_DIR="$BOOT_ORT" \
    ${keyenv[@]+"${keyenv[@]}"} \
    "$BIN" -Duser.timezone=UTC -Djava.io.tmpdir="$BOOT_DIR/jtmp" > "$BOOT_LOG" 2>&1 &
  BOOT_PID=$!
}

# wait_ready: poll the log for event=service_ready, bounded; return 1 if the process dies first.
wait_ready() {
  for _ in $(seq 1 90); do
    grep -q 'event=service_ready' "$BOOT_LOG" 2>/dev/null && return 0
    kill -0 "$BOOT_PID" 2>/dev/null || return 1
    sleep 1
  done
  return 1
}

# wait_exit <seconds>: bounded wait for our own pid to exit. Sets EXIT_RC; on timeout kills it
# (only the pid we started) and sets EXIT_RC=137. Returns 1 on timeout.
wait_exit() {
  local limit="$1"
  for _ in $(seq 1 "$limit"); do
    if ! kill -0 "$BOOT_PID" 2>/dev/null; then
      wait "$BOOT_PID"; EXIT_RC=$?; BOOT_PID=""; return 0
    fi
    sleep 1
  done
  kill -9 "$BOOT_PID" 2>/dev/null; wait "$BOOT_PID" 2>/dev/null; EXIT_RC=137; BOOT_PID=""
  return 1
}

kill_boot() { kill -9 "$BOOT_PID" 2>/dev/null; wait "$BOOT_PID" 2>/dev/null; BOOT_PID=""; }

# ck <label> <detail-on-failure (eval'd lazily)> <command...>: one assertion line; sets leg_ok=1 on failure.
ck() {
  local label="$1" detail="$2"; shift 2
  if "$@"; then echo "    ok   $label"; else echo "    FAIL $label: $(eval "$detail")"; leg_ok=1; fi
}
verdict() { # verdict <shape label>
  if [ "$leg_ok" = "0" ]; then echo "PASS $1"; else echo "FAIL $1"; fail=1; tail -25 "$BOOT_LOG" | sed 's/^/      | /'; fi
}
has_log() { grep -q -- "$1" "$BOOT_LOG"; }
no_log() { ! grep -q -- "$1" "$BOOT_LOG"; }
exited_in_bound() { [ "$EXIT_TIMEOUT" = "0" ]; }
exited_with_failure() { [ "$EXIT_RC" != "0" ] && [ "$EXIT_RC" -lt 128 ]; }
exit_is_143() { [ "$EXIT_RC" = "143" ]; }
ort_dir_nonempty() { [ "$(find "$BOOT_ORT" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')" != "0" ]; }
ort_dir_empty() { [ "$(find "$BOOT_ORT" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')" = "0" ]; }
size_floor_named() {
  has_log 'far below the standard fp32 bge export' && grep -qE 'model at .*model\.onnx is [0-9]+ bytes' "$BOOT_LOG"
}
version_is_voyage_only() {
  VERSION_JSON="$BOOT_DIR/version.json" python3 -c "
import json, os, sys
v = json.load(open(os.environ['VERSION_JSON']))
models = v.get('embedding_models') or []
print('mode=%s models=%s' % (v.get('embedding_mode'), models))
sys.exit(0 if v.get('embedding_mode') == 'voyage' and models and all(m.startswith('voyage-') for m in models) else 1)" > "$BOOT_DIR/version.check" 2>&1
}

# ── Shape (a): cloud shape ────────────────────────────────────────────────────
run_cloud_shape() {
  leg_ok=0
  boot_prepare cloud-shape || { leg_ok=1; verdict "cloud-shape"; return; }
  boot_start spike-dummy
  if ! wait_ready; then
    echo "    FAIL reaches service_ready: the engine exited or never became ready"; leg_ok=1
    kill_boot; verdict "cloud-shape"; return
  fi
  echo "    ok   reaches service_ready"
  # Positive controls: the probe ran against OUR empty model dir and saw a key and no bge.
  ck "probe saw the voyage key and no bge" 'grep -m1 embedder_presence "$BOOT_LOG"' \
    has_log "event=embedder_presence bge_present=false voyage_key_present=true"
  ck "probe looked in NX_ONNX_MODEL_DIR" 'echo "probe line does not name $BOOT_MODELS"' has_log "path=$BOOT_MODELS/"
  curl -fsS -m 15 -H "Authorization: Bearer smoketoken" "http://127.0.0.1:${BOOT_PORT}/version" \
    > "$BOOT_DIR/version.json" 2> "$BOOT_DIR/curl.err"
  ck "GET /version answers" 'cat "$BOOT_DIR/curl.err"' test -s "$BOOT_DIR/version.json"
  ck "/version reports embedding_mode voyage with Voyage models only" 'cat "$BOOT_DIR/version.check"' version_is_voyage_only
  echo "         $(cat "$BOOT_DIR/version.check" 2>/dev/null)"
  ck "no ort_native_dir event" 'grep -m1 ort_native_dir "$BOOT_LOG"' no_log 'event=ort_native_dir'
  ck "no onnx_model_root event (no local embedder built)" 'grep -m1 onnx_model_root "$BOOT_LOG"' no_log 'event=onnx_model_root'
  ck "NX_ORT_NATIVE_DIR stays empty (no ONNX extraction)" 'find "$BOOT_ORT" -mindepth 1 | head -5' ort_dir_empty
  if [ "$WINLIKE" = "1" ]; then
    echo "    skip SIGTERM exit-code assertion (Windows-like environment)"
    kill_boot
  else
    kill -TERM "$BOOT_PID"
    wait_exit 30; EXIT_TIMEOUT=$?
    ck "SIGTERM exits 143" 'echo "exit $EXIT_RC (137 = hung past 30 s; 134/139 = native crash)"' exit_is_143
  fi
  verdict "cloud-shape (key, no bge files: Voyage-only, no ONNX session)"
}

# ── Shape (b): neither family ─────────────────────────────────────────────────
run_neither_family() {
  leg_ok=0
  boot_prepare neither-family || { leg_ok=1; verdict "neither-family"; return; }
  boot_start ""
  wait_exit 90; EXIT_TIMEOUT=$?
  ck "exits within the bound" 'echo "still running after 90 s; killed"' exited_in_bound
  ck "exits non-zero, not a crash or a kill" 'echo "exit $EXIT_RC"' exited_with_failure
  ck "never reaches service_ready" 'echo "service_ready was logged"' no_log 'event=service_ready'
  ck "logs event=no_embedder_available" 'echo "event missing"' has_log 'event=no_embedder_available'
  ck "names the Voyage-key remedy (NX_VOYAGE_API_KEY)" 'echo "not in the output"' has_log 'NX_VOYAGE_API_KEY'
  ck "names the local-model remedy (nx init)" 'echo "not in the output"' has_log 'nx init'
  verdict "neither-family (no key, no bge files: boot fails naming both remedies)"
}

# ── Shape (c): truncated model, with and without a key ────────────────────────
run_truncated_model() {
  local leg key want_key
  for leg in keyless keyed; do
    leg_ok=0
    key=""; want_key=false
    if [ "$leg" = "keyed" ]; then key="spike-dummy"; want_key=true; fi
    boot_prepare "truncated-model-$leg" || { leg_ok=1; verdict "truncated-model ($leg)"; continue; }
    mkdir -p "$BOOT_MODELS/bge-base-en-v1.5/onnx"
    printf '{}' > "$BOOT_MODELS/bge-base-en-v1.5/onnx/tokenizer.json"
    printf 'not-a-model' > "$BOOT_MODELS/bge-base-en-v1.5/onnx/model.onnx"
    boot_start "$key"
    wait_exit 90; EXIT_TIMEOUT=$?
    ck "exits within the bound" 'echo "still running after 90 s; killed"' exited_in_bound
    ck "exits non-zero, not a crash or a kill" 'echo "exit $EXIT_RC"' exited_with_failure
    ck "never reaches service_ready" 'echo "service_ready was logged"' no_log 'event=service_ready'
    ck "probe reports the truncated model PRESENT (presence is not validity)" 'grep -m1 embedder_presence "$BOOT_LOG"' \
      has_log "event=embedder_presence bge_present=true voyage_key_present=$want_key"
    # Positive controls for the cloud shape's negative ONNX checks: a boot that DOES reach
    # applyAtBoot logs the extraction event and fills NX_ORT_NATIVE_DIR. Without these a
    # renamed event, or a binary that ignores the env var, would pass the cloud shape vacuously.
    ck "positive control: applyAtBoot logged the extraction" 'grep -m1 ort_native_dir "$BOOT_LOG"' \
      has_log 'event=ort_native_dir source=extracted'
    ck "positive control: NX_ORT_NATIVE_DIR was filled" 'find "$BOOT_ORT" -mindepth 1 | head -5' ort_dir_nonempty
    ck "names the size floor (model at <path> is <N> bytes, far below the standard fp32 export)" \
      'echo "size-floor text missing"' size_floor_named
    ck "is not reported as absent (no no_embedder_available)" 'echo "reported as absent"' no_log 'event=no_embedder_available'
    verdict "truncated-model ($leg: both files present, model truncated: boot fails naming the size floor)"
  done
}

echo "boot shapes (RDR-210 P2.2): $BIN"
run_cloud_shape
run_neither_family
run_truncated_model

if [ "$fail" = "0" ]; then echo "BOOT SHAPES PASS"; exit 0; else echo "BOOT SHAPES FAIL"; exit 1; fi
