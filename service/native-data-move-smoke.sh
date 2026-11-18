#!/usr/bin/env bash
# nexus-f3yqd.2.22 (RDR-246): the native binary's data-move boot, on a provisioned PostgreSQL bundle.
#
# The vendored ddl_utils and dml_utils changelogs use Liquibase sqlFile and createProcedure changesets that no other
# nexus changelog uses, and no native build had loaded them before the 0.1.195 candidate; the JVM tests cannot see a
# native-only failure. native-smoke.sh boots against a throwaway pgvector with ONE database role, where the engine
# skips the libraries by design, so it cannot reach them. This script boots the native binary twice, each time on a
# cluster provisioned from the released PG bundle by the same code `nx init --service` runs (two roles, nexus_admin
# and nexus_svc, and the dml_utils bootstrap):
#
#   with-pg-background     the bundle as released (pg_background 2.0 since engine-service-v0.1.194). The boot log
#                          must show both event=data_move_library_applied lines.
#   without-pg-background  the same bundle with pg_background's control file removed before provisioning, which is
#                          a PostgreSQL that cannot create the extension (Sam's 8.4 ruling: 0.1.195 ships the
#                          move code with no pg_background on the cloud DB host). The log must show
#                          event=data_move_libraries_skipped reason="pg_background is not installed" at WARN, the
#                          nexus schema walk completing, and the engine serving.
#
# Both boots also prove the log masking of nexus-f3yqd.2.18: the script requests /v1/_whoami/<label>-<password> with
# a bad token, the auth filter logs that path, and the checker needs the line to show <redacted> in place of each
# database password and the raw password to be nowhere in the log.
#
# The log assertions are scripts/check_native_data_move_boot.py (unit-tested in
# tests/test_check_native_data_move_boot.py); this script only boots, probes and hands the log over.
#
# The engine runs as a cloud-shaped boot: a dummy Voyage key and an empty model dir, so no ONNX model is needed
# (native-boot-shapes-smoke.sh's cloud shape proves that boot reaches service_ready on a key alone). The key is never
# used: no embedding is requested.
#
# Env:
#   PG_BUNDLE  (required) the nexus-pg-<arch>.txz of the release (the Actions artifact of build-publish-pg-bundle)
#   BIN        the native binary (default: target/nexus-service, relative to this directory)
# Needs: bash, tar with xz, curl, python3, and `uv` with the nexus package synced at the repo root (`uv sync`).
# Exit: 0 both boots passed, 1 an assertion failed, 2 the setup failed (never a pass).
#
# The database passwords of these throwaway clusters are in the engine's environment for the length of
# the run; the clusters live in a per-run private directory and are stopped at exit.
# SC2329: cleanup is invoked via trap.
# shellcheck disable=SC2329
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECKER="$REPO_ROOT/scripts/check_native_data_move_boot.py"
PROVISION="$SCRIPT_DIR/smoke-probes/provision_bundle_cluster.py"
# The probe route the checker looks for (check_native_data_move_boot.PROBE_PATH_PREFIX); a test pins that they agree.
PROBE_PATH_PREFIX="/v1/_whoami/"

# The bundle's root under an extraction dir: the release packs it with its top directory (tar -C parent basename,
# engine-service-release.yml), so initdb sits at <dir>/<top>/bin/initdb, depth 3. Prints the root, or nothing.
locate_bundle() {
  local hit
  hit=$(find "$1" -mindepth 3 -maxdepth 3 -type f -name initdb -path '*/bin/initdb' -print -quit)
  [ -n "$hit" ] && dirname "$(dirname "$hit")"
}

# Test hook: extract a bundle and print where its root landed, nothing else (tests/test_native_data_move_smoke_wiring.py).
if [ "${1:-}" = "--locate-bundle" ]; then
  [ $# -eq 3 ] || { echo "usage: $0 --locate-bundle <bundle.txz> <empty dir>"; exit 2; }
  tar -xJf "$2" -C "$3" || exit 2
  locate_bundle "$3" || { echo "FAIL: no bin/initdb in the extracted bundle"; exit 2; }
  exit 0
fi

BIN="${BIN:-target/nexus-service}"
case "$BIN" in
  /*) ;;
  *) if [ -x "$BIN" ]; then BIN="$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"
     else BIN="$SCRIPT_DIR/$BIN"; fi ;;
esac
[ -x "$BIN" ] || { echo "FAIL: native binary not found/executable at $BIN"; exit 2; }
PG_BUNDLE="${PG_BUNDLE:-}"
[ -n "$PG_BUNDLE" ] && [ -f "$PG_BUNDLE" ] || { echo "FAIL: PG_BUNDLE must name the nexus-pg-<arch>.txz (got '${PG_BUNDLE}')"; exit 2; }
PG_BUNDLE="$(cd "$(dirname "$PG_BUNDLE")" && pwd)/$(basename "$PG_BUNDLE")"
for tool in tar curl python3 uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "FAIL: $tool is not on PATH"; exit 2; }
done

SMOKE_TMP=$(mktemp -d "${TMPDIR:-/tmp}/native-data-move-smoke.XXXXXX") \
  || { echo "FAIL: cannot create scratch dir under ${TMPDIR:-/tmp}"; exit 2; }

BOOT_PID=""
PG_STOPS=()   # "<pg_ctl>|<pgdata>" for every cluster this run started
fail=0

free_port() { python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()"; }

cleanup() {
  local rc=$?
  [ -n "$BOOT_PID" ] && kill -9 "$BOOT_PID" 2>/dev/null
  local entry
  for entry in ${PG_STOPS[@]+"${PG_STOPS[@]}"}; do
    "${entry%%|*}" -D "${entry#*|}" stop -m fast >/dev/null 2>&1 || true
  done
  if [ "$rc" = "0" ]; then
    rm -rf "$SMOKE_TMP"
  else
    echo "native-data-move-smoke: FAILED (exit $rc); logs kept in $SMOKE_TMP" >&2
  fi
  return "$rc"
}
trap cleanup EXIT

# cred <file> <KEY>: one value from the credentials env-file (KEY=value lines).
cred() { awk -v k="$2" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$1"; }

# urlenc <text>: percent-encoded for one path segment.
urlenc() { python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" "$1"; }

# redacted_tail <file>: for a failure printout, the log's last 40 lines and every line a check can cite (the
# data-move, masking, schema-walk and ready events), each with its line number in the whole log, so a line the
# checker cites by number is shown here; both passwords are replaced first.
redacted_tail() {
  python3 - "$1" <<'PY'
import os, re, sys
with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
    lines = f.read().splitlines()
cited = re.compile(r"event=(data_move_|log_masking_|schema_migration_complete|service_ready)")
keep = sorted({n for n, ln in enumerate(lines, 1) if cited.search(ln)} | set(range(max(1, len(lines) - 39), len(lines) + 1)))
out = "\n".join(f"{n}: {lines[n - 1]}" for n in keep)
for k in ("SMOKE_SVC_PASS", "SMOKE_ADMIN_PASS"):
    v = os.environ.get(k, "")
    if v:
        out = out.replace(v, "<smoke-redacted>")
sys.stdout.write(out + "\n")
PY
}

run_mode() {
  local mode="$1"
  local d="$SMOKE_TMP/$mode" bundle ctl log creds port pgdata U
  echo "== $mode =="
  mkdir -p "$d/jtmp" "$d/home" "$d/ort" "$d/models" "$d/cfg" "$d/x" || return 2
  tar -xJf "$PG_BUNDLE" -C "$d/x" || { echo "FAIL: cannot extract $PG_BUNDLE"; return 2; }
  bundle=$(locate_bundle "$d/x") || { echo "FAIL: no bin/initdb in the extracted bundle"; return 2; }

  # Non-vacuity: a bundle that lacks pg_background cannot be the 'with' case, and the 'without' case must have
  # removed something.
  ctl=$(find "$bundle" -name 'pg_background.control' -print -quit)
  if [ "$mode" = "with-pg-background" ]; then
    [ -n "$ctl" ] || { echo "FAIL: the bundle has no pg_background.control: it cannot serve the with-pg-background boot"; return 2; }
  else
    [ -n "$ctl" ] || { echo "FAIL: the bundle has no pg_background.control to remove: the without boot would prove nothing"; return 2; }
    find "$bundle" -name 'pg_background*.control' -delete
    [ -z "$(find "$bundle" -name 'pg_background.control')" ] || { echo "FAIL: could not remove pg_background.control"; return 2; }
    echo "  removed $(basename "$ctl") from the bundle copy"
  fi

  if ! NEXUS_PG_BIN="$bundle/bin" NEXUS_CONFIG_DIR="$d/cfg" \
       uv run --project "$REPO_ROOT" python "$PROVISION" "$d/cfg"; then
    echo "FAIL: provisioning the cluster failed"; return 2
  fi
  creds="$d/cfg/pg_credentials"
  pgdata=$(cred "$creds" PG_DATA)
  PG_STOPS+=("$bundle/bin/pg_ctl|$pgdata")
  port=$(free_port)
  SMOKE_SVC_PASS=$(cred "$creds" NX_DB_PASS); export SMOKE_SVC_PASS
  SMOKE_ADMIN_PASS=$(cred "$creds" NX_DB_ADMIN_PASS); export SMOKE_ADMIN_PASS
  [ -n "$SMOKE_SVC_PASS" ] && [ -n "$SMOKE_ADMIN_PASS" ] || { echo "FAIL: the credentials file lacks a database password"; return 2; }
  [ "$(cred "$creds" NX_DB_ADMIN_USER)" != "$(cred "$creds" NX_DB_USER)" ] \
    || { echo "FAIL: the cluster has one role; the engine would skip the libraries as the single-role posture"; return 2; }

  log="$d/svc.log"
  env -i PATH="$PATH" HOME="$d/home" TMPDIR="$d/jtmp" \
    NX_DB_URL="$(cred "$creds" NX_DB_URL)" NX_DB_USER="$(cred "$creds" NX_DB_USER)" NX_DB_PASS="$SMOKE_SVC_PASS" \
    NX_DB_ADMIN_URL="$(cred "$creds" NX_DB_ADMIN_URL)" NX_DB_ADMIN_USER="$(cred "$creds" NX_DB_ADMIN_USER)" \
    NX_DB_ADMIN_PASS="$SMOKE_ADMIN_PASS" \
    NX_SERVICE_PORT="$port" NX_SERVICE_TOKEN=smoketoken \
    NX_VOYAGE_API_KEY=smoke-dummy-key NX_ONNX_MODEL_DIR="$d/models" NX_ORT_NATIVE_DIR="$d/ort" \
    "$BIN" -Duser.timezone=UTC -Djava.io.tmpdir="$d/jtmp" > "$log" 2>&1 &
  BOOT_PID=$!

  local up=0
  for _ in $(seq 1 120); do
    grep -q 'event=service_ready' "$log" 2>/dev/null && { up=1; break; }
    kill -0 "$BOOT_PID" 2>/dev/null || break
    sleep 1
  done
  U="http://127.0.0.1:${port}"
  if [ "$up" != "1" ]; then
    echo "FAIL: the engine never logged event=service_ready (exited or timed out)"
    redacted_tail "$log"
    kill -9 "$BOOT_PID" 2>/dev/null; wait "$BOOT_PID" 2>/dev/null; BOOT_PID=""
    return 1
  fi
  local code
  code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$U/health")
  if [ "$code" != "200" ]; then
    echo "FAIL: GET /health -> $code (want 200)"; redacted_tail "$log"
    kill -9 "$BOOT_PID" 2>/dev/null; wait "$BOOT_PID" 2>/dev/null; BOOT_PID=""
    return 1
  fi
  echo "  engine up: /health 200"

  # The masking probes: a request whose path carries each password, with a token the engine does not know, so the
  # auth filter answers 401 and logs event=request_failed path=<the path>. The curl output is not printed.
  curl -s -m 15 -o /dev/null -H "Authorization: Bearer nx-smoke-no-such-token" \
    "$U${PROBE_PATH_PREFIX}svc-$(urlenc "$SMOKE_SVC_PASS")"
  curl -s -m 15 -o /dev/null -H "Authorization: Bearer nx-smoke-no-such-token" \
    "$U${PROBE_PATH_PREFIX}admin-$(urlenc "$SMOKE_ADMIN_PASS")"
  sleep 1

  # Stop the engine so every line is flushed before the checker reads the log. Only the pid this run started.
  kill -TERM "$BOOT_PID" 2>/dev/null
  for _ in $(seq 1 30); do kill -0 "$BOOT_PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$BOOT_PID" 2>/dev/null; wait "$BOOT_PID" 2>/dev/null; BOOT_PID=""

  python3 -I "$CHECKER" --mode "$mode" --log "$log" --secret svc=SMOKE_SVC_PASS --secret admin=SMOKE_ADMIN_PASS
  local rc=$?
  if [ "$rc" != "0" ]; then
    echo "  --- engine log: cited events and the last 40 lines, numbered (passwords replaced) ---"
    redacted_tail "$log"
    return "$rc"
  fi
  return 0
}

for mode in with-pg-background without-pg-background; do
  run_mode "$mode"
  rc=$?
  if [ "$rc" = "2" ]; then echo "NATIVE DATA-MOVE SMOKE: SETUP FAILED ($mode)"; exit 2; fi
  [ "$rc" = "0" ] || fail=1
done

if [ "$fail" = "0" ]; then echo "NATIVE DATA-MOVE SMOKE PASS"; exit 0; else echo "NATIVE DATA-MOVE SMOKE FAIL"; exit 1; fi
