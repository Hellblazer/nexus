#!/usr/bin/env bash
# Dual-embedder functional gate (RDR-210 Phase 5 Step 1, bead nexus-8x0tg; Phase 6 scenarios, nexus-s08oe).
#
# WHY THIS EXISTS. No other gate boots a local engine KEYED (RDR-210 Gap 6): the
# local-service gate runs keyless and bge-768 only, by design. The engine from
# RDR-210 Phase 2 serves bge-768 and Voyage side by side when a Voyage key reaches
# it, and until this script nothing proved that end to end: a bge collection and a
# Voyage collection in one database, both written, both searched, both still
# searchable after a restart, and one search over both returning hits from both.
# With the Phase 6 scenarios (step 6 below) it runs the whole Minimum Viable
# Validation.
#
# AN ABSENT KEY IS A FAILURE, NEVER A SKIP (the nexus-moht0 vacuous-gate doctrine).
# A gate that skip-passes when its dependency is missing proves nothing about the
# dual engine, and this one has no max-skip assertion to fall back on because it
# has no skip path at all: no key -> exit 1 with a message naming the key, before
# any scratch dir, build or engine. tests/e2e/dual_embedder_gate_guard_test.sh
# pins that, and pins that no `exit 0` or skip vocabulary exists in this file.
#
# THE KEY comes from the operator's credential chain, resolved BEFORE the home is
# fenced (the fence would hide the real config.yml): NX_VOYAGE_API_KEY, then
# VOYAGE_API_KEY (environment), then the repo .env (override: NX_DUAL_GATE_ENV_FILE),
# then credentials.voyage_api_key in $NEXUS_CONFIG_DIR/config.yml (default
# ~/.config/nexus). The key is held in a shell variable only. It reaches the engine
# through the environment of the daemon-start subshell (never argv, so it is not in
# `ps`), it is never written to the scratch config (which stays keyless, so the
# install's profile is bge), and it is never printed. The supervisor passes an
# explicit NX_VOYAGE_API_KEY through whatever the engine's install receipt says.
#
# WHAT IT DOES, fenced from ~/.config/nexus exactly as the local-service gate is
# (empty HOME plus the toolchain-cache allowlist, a PATH shim that refuses a bare
# `nx`):
#   1. Scratch config dir; `nx init --service` provisions a throwaway PG (bge-768
#      profile, no key in the scratch config), then stops the pinned-release
#      engine init auto-started so the engine under test is the one started below.
#   2. Builds the stamped dev jar with scripts/build-gate-jar.sh (or uses
#      NEXUS_SERVICE_BIN, a native binary), which carries the tree's dual engine.
#   3. Starts the engine under the supervisor with the key in its environment.
#   4. PASS 1 and PASS 2, with an engine restart between them (stop, start, same
#      PG). Each pass:
#        - GET /health and GET /version: embedding_mode is `dual`, the served
#          models include bge-768 and voyage-context-3, and the artifact is the
#          jar this run built (build_ref);
#        - registers two documents and writes one chunk to a bge-768 collection
#          and one to a Voyage collection (write_many, the client's combined write);
#        - searches the bge collection alone and the Voyage collection alone and
#          requires EVERY chunk written so far (so pass 2 proves pass 1 survived
#          the restart);
#        - runs ONE search over both collections through the real client
#          (`nx search --corpus bge,voyage`, which splits per model and merges) and
#          requires hits from each.
#      Pass 2 also requires the engine's process_start_time to differ from pass 1.
#   5. A tally refuses unless each of the four searches ran once per pass.
#   6. The Phase 6 scenarios: seeds source-backed documents (one with an aspect row) and
#      a `nx store put` note on the bge side, switches the install to the Voyage profile,
#      and runs `nx collection re-embed --all --wait`: the same documents come back from a
#      search before and after, each job ends done and its source is deleted, the aspect
#      row survives re-homed to the target, the target has a taxonomy_meta row and topic
#      assignments from --wait's discover, and a second --all plans nothing.
#   7. Runs the three keyed_voyage tests in tests/test_integration.py with the key and
#      the Voyage profile in their environment, on the suite's own keyed engine
#      substrate (nexus-8errq); exactly three must pass. Then prints the pass banner.
#   8. Tears everything down (engine, PG, scratch), even on failure.
#
# The Voyage collections make real Voyage API calls (a handful of embeddings, and the
# re-embed moves about 20 chunks: under a thousand tokens a run, capped at 200,000), so the gate costs a fraction of a cent and needs the network. It is run on
# demand and by the release battery leg (nexus-oc2bk), not on every push.
#
# Usage:
#   tests/e2e/dual-embedder-gate.sh
#   NEXUS_SERVICE_BIN=<native binary> tests/e2e/dual-embedder-gate.sh
#   NX_DUAL_GATE_LAB_RUN_CLASS=lab-m tests/e2e/dual-embedder-gate.sh   # build inside a lab-run grant
#
# Exit: 0 = both passes complete and the tally holds; 1 = a check failed or the key
# is absent; 2 = the environment could not be set up (interpreter, build, artifact).
set -euo pipefail
# Sandbox installs are throwaways, never users (tests/test_e2e_no_telemetry_lint.py).
export NX_NO_TELEMETRY=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ── The credential chain (resolved FIRST, before any scratch, build or engine) ──
dg_trim() {  # strip surrounding whitespace, one pair of quotes, and a YAML null
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  v="${v#[\"\']}"
  v="${v%[\"\']}"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  case "$v" in null | '~') v="" ;; esac
  printf '%s' "$v"
}

dg_env_file_value() {  # <file> <NAME>: NAME=value or export NAME=value; commented lines never match
  [ -f "$1" ] || return 0
  awk -v want="$2" '
    { line = $0; sub(/^[ \t]*export[ \t]+/, "", line) }
    index(line, want "=") == 1 { v = substr(line, length(want) + 2); sub(/[ \t]+#.*$/, "", v); print v; exit }
  ' "$1"
}

dg_yml_credential() {  # <config.yml>: credentials.voyage_api_key (only inside the top-level credentials: block)
  [ -f "$1" ] || return 0
  awk '
    /^credentials:/ { inb = 1; next }
    /^[^ \t#]/ { inb = 0 }
    inb && $1 == "voyage_api_key:" { v = $0; sub(/^[ \t]*voyage_api_key:[ \t]*/, "", v); sub(/[ \t]+#.*$/, "", v); print v; exit }
  ' "$1"
}

dg_resolve_voyage_key() {  # prints the key, or nothing; never fails
  local v name envfile cfgdir
  for v in "${NX_VOYAGE_API_KEY:-}" "${VOYAGE_API_KEY:-}"; do
    v="$(dg_trim "$v")"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  done
  envfile="${NX_DUAL_GATE_ENV_FILE:-$REPO_ROOT/.env}"
  for name in NX_VOYAGE_API_KEY VOYAGE_API_KEY; do
    v="$(dg_trim "$(dg_env_file_value "$envfile" "$name")")"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  done
  cfgdir="${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}"
  v="$(dg_trim "$(dg_yml_credential "$cfgdir/config.yml")")"
  if [ -n "$v" ]; then printf '%s' "$v"; fi
  return 0
}

GATE_VOYAGE_KEY="$(dg_resolve_voyage_key)"
# The key lives in the shell variable from here on; nothing else may inherit it.
unset VOYAGE_API_KEY NX_VOYAGE_API_KEY
if [ -z "$GATE_VOYAGE_KEY" ]; then
  {
    echo "[dual-gate] FAIL: no Voyage API key (VOYAGE_API_KEY / NX_VOYAGE_API_KEY)."
    echo "[dual-gate]   This gate boots a KEYED dual-embedder engine. An absent key is a failure, never a pass-by-default."
    echo "[dual-gate]   Looked, in order: NX_VOYAGE_API_KEY and VOYAGE_API_KEY in the environment;"
    echo "[dual-gate]   the same names in ${NX_DUAL_GATE_ENV_FILE:-$REPO_ROOT/.env};"
    echo "[dual-gate]   credentials.voyage_api_key in ${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}/config.yml."
    echo "[dual-gate]   Export VOYAGE_API_KEY, or persist one in that config.yml, and run again."
  } >&2
  exit 1
fi

# shellcheck source=lib/python.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2
cd "$REPO_ROOT"

# The scratch name has the shape fence_remove_home accepts (lib/fence_home.sh).
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/nx-local-service-gate.dual.XXXXXX")"
echo "[dual-gate] scratch config: $SCRATCH"

# ── Fence the REAL config dir (same layers as local-service-gate.sh) ─────────
REAL_HOME="$HOME"
# shellcheck source=tests/e2e/lib/fence_home.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/fence_home.sh"
GATE_HOME="$SCRATCH/home"
fence_home "$REAL_HOME" "$GATE_HOME"
export HOME="$GATE_HOME"
fence_home_env "$GATE_HOME"
fence_uv_env "$REAL_HOME"
fence_cache_env "$REAL_HOME"

GATE_SHIM="$SCRATCH/shim"
mkdir -p "$GATE_SHIM"
cat > "$GATE_SHIM/nx" <<'SHIM'
#!/bin/sh
echo "FATAL: bare 'nx' called inside dual-embedder-gate (PPID=$PPID)." >&2
echo "  It resolves to the INSTALLED tool, not the tree under test, and it" >&2
echo "  writes the real ~/.config/nexus. Use 'uv run nx'." >&2
exit 1
SHIM
chmod +x "$GATE_SHIM/nx"
export PATH="$GATE_SHIM:$PATH"
echo "[dual-gate] fenced: HOME is an empty scratch dir at $HOME (toolchain caches only); PATH shim active"

# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() {
  local rc=$?
  set +e
  # --with-pg: a plain stop leaves the supervisor's PostgreSQL running by design.
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop --with-pg >/dev/null 2>&1
  if [ -f "$SCRATCH/pg_credentials" ]; then
    # shellcheck disable=SC1090,SC1091
    source "$SCRATCH/pg_credentials" >/dev/null 2>&1
    pg_ctl -D "$SCRATCH/postgres" stop -m fast >/dev/null 2>&1
  fi
  if ! fence_remove_home "$GATE_HOME" "$rc" && fence_keep_home "$rc" && [ -d "$GATE_HOME" ]; then
    KEPT_HOME="${TMPDIR:-/tmp}/nx-kept-gate-home.$$"
    if mv "$GATE_HOME" "$KEPT_HOME" 2>/dev/null; then
      echo "[dual-gate] kept the fenced home (NX_KEEP_FENCED_HOME): $KEPT_HOME"
    fi
  fi
  rm -rf "$SCRATCH"
  echo "[dual-gate] cleaned up"
}
trap cleanup EXIT

# ── 1. Provision the throwaway PG, bge-768 profile, keyless scratch config ───
# --no-autostart: a gate never registers a login unit, and without it nx init prompts
# whenever stdin is a TTY (nexus-wvyvn). The key is not in this process's environment.
NEXUS_CONFIG_DIR="$SCRATCH" uv run nx init --service --no-autostart </dev/null

# nx init also installs and starts the PINNED RELEASE engine, and daemon start is
# idempotent on a live lease, so left running it would be the engine under test
# (nexus-4e96a). Stop it; PG stays up.
echo "[dual-gate] stopping the pinned-release engine nx init auto-started (nexus-4e96a)"
NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop

# ── 2. The engine artifact ──────────────────────────────────────────────────
DG_JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
DG_BUILD_REF=""
if [ -n "${NEXUS_SERVICE_BIN:-}" ]; then
  echo "[dual-gate] native binary: $NEXUS_SERVICE_BIN"
else
  DG_BUILD_ARGS=()
  if [ -n "${NX_DUAL_GATE_LAB_RUN_CLASS:-}" ]; then
    DG_BUILD_ARGS=(--lab-run "$NX_DUAL_GATE_LAB_RUN_CLASS")
  fi
  # The stamped jar (release_version + a build_ref nonce), shared lease, gate-jar cache.
  # An assignment, then the parse: build_ref is how the version check proves the served
  # process is the jar this run was given and not the pinned release (nexus-308ph).
  if ! DG_BUILD_OUT="$("$REPO_ROOT/scripts/build-gate-jar.sh" ${DG_BUILD_ARGS[@]+"${DG_BUILD_ARGS[@]}"} 2>&1)"; then
    printf '%s\n' "$DG_BUILD_OUT" >&2
    echo "[dual-gate] ERROR: scripts/build-gate-jar.sh failed -- fix the Maven build and run again" >&2
    exit 2
  fi
  printf '%s\n' "$DG_BUILD_OUT" | sed 's/^/[dual-gate]   build: /'
  DG_BUILD_REF="$(awk -F= '/^stamped build_ref=/ { v = $2 } END { print v }' <<<"$DG_BUILD_OUT")"
  if [ -z "$DG_BUILD_REF" ] || [ ! -f "$DG_JAR" ]; then
    echo "[dual-gate] ERROR: no build_ref in the build output, or $DG_JAR is missing" >&2
    exit 2
  fi
  # Boot from a private copy. The battery's gate group runs this leg beside local-service-gate.sh in one
  # worktree, and that leg rebuilds service/target/ between this leg's two boots (nexus-i5mbq, 7.84.0 battery:
  # the second boot found its NEXUS_SERVICE_JAR missing). The copy carries the stamp the version check reads.
  cp "$DG_JAR" "$SCRATCH/engine.jar"
  DG_JAR="$SCRATCH/engine.jar"
fi

# ── Engine start, stop and lease ─────────────────────────────────────────────
# dg_with_key CMD...: run CMD with the key in its environment. The key is an exported variable in this
# subshell, not an argument, so `ps` never shows it. The supervisor and the keyed_voyage pytest run both
# get it here and nowhere else: NX_VOYAGE_API_KEY for an engine, VOYAGE_API_KEY for the client's
# credential lookup (nexus.config.get_credential).
dg_with_key() {
  (
    export NX_VOYAGE_API_KEY="$GATE_VOYAGE_KEY"
    export VOYAGE_API_KEY="$NX_VOYAGE_API_KEY"
    exec "$@"
  )
}

dg_start_engine() {
  (
    export NX_LOCAL=1
    export NEXUS_CONFIG_DIR="$SCRATCH"
    if [ -n "${NEXUS_SERVICE_BIN:-}" ]; then
      export NEXUS_SERVICE_BIN
    else
      export NEXUS_SERVICE_JAR="$DG_JAR"
    fi
    # --config-dir is explicit so a sibling leg's stop cannot read this process as its own (nexus-oacd3).
    dg_with_key uv run nx daemon service start --config-dir "$SCRATCH"
  )
}

dg_read_lease() {
  local lease
  lease="$(cat "$SCRATCH"/storage_service_addr.*)"
  DG_PORT="$(printf '%s' "$lease" | "$E2E_PYTHON" -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['port'])")"
  DG_TOKEN="$(printf '%s' "$lease" | "$E2E_PYTHON" -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['token'])")"
  DG_BASE="http://127.0.0.1:$DG_PORT"
  DG_AUTH=(-H "Authorization: Bearer $DG_TOKEN")
  echo "[dual-gate] engine on 127.0.0.1:$DG_PORT"
}

dg_restart_engine() {
  echo "[dual-gate] restarting the engine (stop, then start against the same PG)"
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop
  dg_start_engine
  dg_read_lease
}

# ── Counted, fail-loud checks (same discipline as the local-service gate's smoke leg) ──
DG_DIR="$SCRATCH/probe"
mkdir -p "$DG_DIR"
DG_PASSED=0
DG_PASSES=2
DG_BGE_SEARCHES=0
DG_VOY_SEARCHES=0
DG_COMBINED_BGE_HITS=0
DG_COMBINED_VOY_HITS=0
DG_CODE=""
DG_UID="$$"

dg_fail() {
  echo "[dual-gate] CHECK FAILED: $1 (code=${DG_CODE:-n/a})" >&2
  if [ "${DG_CODE:-}" = "429" ]; then
    echo "[dual-gate]   429 after $DG_REQUEST_TRIES tries. The body above says whose limit it was; the engine answers 429 when Voyage rate-limits the key. Voyage's published rate limits are for usage tier 1 and up, and tier 1 needs a payment method on the account (docs.voyageai.com/docs/rate-limits). Wait and run again, or use a key from an account with a payment method." >&2
  fi
  if [ -f "$DG_DIR/resp.json" ]; then echo "[dual-gate]   body: $(cat "$DG_DIR/resp.json")" >&2; fi
  if [ -s "$DG_DIR/curl.err" ]; then echo "[dual-gate]   curl: $(cat "$DG_DIR/curl.err" 2>/dev/null)" >&2; fi
  local elog
  for elog in "$SCRATCH/logs/storage_service_jar.log" "$SCRATCH/logs/storage_service_native.log"; do
    [ -s "$elog" ] || continue
    echo "[dual-gate]   engine log ($elog), last WARN/ERROR lines:" >&2
    grep -E ' (WARN|ERROR) ' "$elog" | tail -n 20 | sed 's/^/[dual-gate]     /' >&2
  done
  exit 1
}

# dg_request METHOD PATH [JSON_BODY]: body to $DG_DIR/resp.json, status to DG_CODE ("000" when
# the connection failed). 429 and 5xx from the Voyage-bound routes are retried up to
# DG_REQUEST_TRIES tries in all, because the gate talks to a real remote API; a dead key or a
# refused model keeps failing and fails. Each wait is the response's Retry-After in seconds (the
# engine sets it on a rate-limited answer), at most DG_RETRY_AFTER_MAX_S, or else
# DG_RETRY_SLEEP_S times the try number.
DG_REQUEST_TRIES=5
DG_RETRY_SLEEP_S=3
DG_RETRY_AFTER_MAX_S=60
dg_retry_wait() {  # <try>: seconds to wait before the next try
  local ra
  ra="$(awk 'tolower($1) == "retry-after:" { gsub(/[^0-9]/, "", $2); print $2 }' "$DG_DIR/headers" 2>/dev/null | tail -n 1)"
  if [ -n "$ra" ]; then
    [ "$ra" -le "$DG_RETRY_AFTER_MAX_S" ] || ra="$DG_RETRY_AFTER_MAX_S"
    printf '%s' "$ra"
  else
    printf '%s' "$((DG_RETRY_SLEEP_S * $1))"
  fi
}

dg_request() {
  local method="$1" path="$2" body="${3:-}" try=1
  if [ "$#" -ge 3 ] && [ -z "$body" ]; then
    DG_CODE="n/a"
    rm -f "$DG_DIR/resp.json"
    dg_fail "$method $path: the request body is empty (its builder produced no output)"
  fi
  local args=(-sS -o "$DG_DIR/resp.json" -D "$DG_DIR/headers" -w '%{http_code}' -X "$method" "${DG_AUTH[@]}")
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' -d "$body")
  fi
  while :; do
    DG_CODE="$(curl "${args[@]}" "$DG_BASE$path" 2>"$DG_DIR/curl.err")" || true
    [ -n "$DG_CODE" ] || DG_CODE="000"
    case "$DG_CODE" in
      429 | 502 | 503 | 504)
        if [ "$try" -lt "$DG_REQUEST_TRIES" ]; then
          echo "[dual-gate] $method $path -> $DG_CODE, retrying ($try/$DG_REQUEST_TRIES)"
          sleep "$(dg_retry_wait "$try")"
          try=$((try + 1))
          continue
        fi
        ;;
    esac
    break
  done
}

# dg_holds EXPR: EXPR is a python expression over `d`, the JSON in $DG_DIR/resp.json. Every
# value spliced into EXPR is script-controlled and shell-safe (alphanumerics, dashes, dots).
dg_holds() {
  "$E2E_PYTHON" -c "
import json, sys
d = json.load(open('$DG_DIR/resp.json'))
sys.exit(0 if bool($1) else 1)
"
}

dg_ok() {
  DG_PASSED=$((DG_PASSED + 1))
  echo "[dual-gate] ok ($DG_PASSED): $1"
}

# dg_check DESC EXPR: fail the gate unless EXPR holds.
dg_check() {
  dg_holds "$2" || dg_fail "$1"
  dg_ok "$1"
}

dg_expect_200() { [ "$DG_CODE" = "200" ] || dg_fail "$1"; }

DG_OWNER_PREFIX="9.$DG_UID"
DG_BGE_COLL="knowledge__gate-dual__bge-base-en-v15-768__v1"
DG_VOY_COLL="knowledge__gate-dual__voyage-context-3__v1"
DG_BGE_CHASHES=""
DG_VOY_CHASHES=""
DG_QUERY="dual embedder gate probe $DG_UID"
DG_START_TIME_1=""

# dg_version_check: the engine is the DUAL one, serving both families, and is the artifact this run built.
dg_version_check() {
  dg_request GET /health
  dg_expect_200 "GET /health"
  dg_check "GET /health -> status=ok db=up" "d.get('status')=='ok' and d.get('db')=='up'"
  dg_request GET /version
  dg_expect_200 "GET /version"
  dg_check "GET /version -> embedding_mode==dual (a keyed engine boots dual; onnx-local means the key did not arrive, or the artifact predates the dual router)" \
    "d.get('embedding_mode')=='dual'"
  dg_check "GET /version -> embedding_models carry bge-base-en-v15-768 and voyage-context-3" \
    "{'bge-base-en-v15-768','voyage-context-3'} <= set(d.get('embedding_models', []))"
  if [ -n "$DG_BUILD_REF" ]; then
    dg_check "GET /version -> build_ref==$DG_BUILD_REF (the served process is the jar this run built, not the pinned release)" \
      "d.get('build_ref')=='$DG_BUILD_REF'"
  else
    dg_check "GET /version -> build_ref ABSENT (a native binary never stamps it; a present field means a stray jar is serving)" \
      "d.get('build_ref') is None"
  fi
  DG_THIS_START="$("$E2E_PYTHON" -c "import json;print(json.load(open('$DG_DIR/resp.json')).get('process_start_time',''))")"
  [ -n "$DG_THIS_START" ] || dg_fail "GET /version carries no process_start_time (needed to prove the restart)"
}

# dg_setup: once, before pass 1. Both collections registered under one owner. Registered the way the
# client does (RDR-204: the engine no longer auto-registers), each with its own family's model.
dg_setup() {
  local body
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'tumbler_prefix':'$DG_OWNER_PREFIX','name':'gate-dual-owner','owner_type':'gate_dual'}))")"
  dg_request POST /v1/catalog/owners/upsert "$body"
  dg_expect_200 "POST /v1/catalog/owners/upsert"
  dg_check "POST /v1/catalog/owners/upsert -> ok" "d.get('ok') is True"
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'name':'$DG_BGE_COLL','content_type':'knowledge','owner_id':'gate-dual','embedding_model':'bge-base-en-v15-768'}))")"
  dg_request POST /v1/catalog/collections/upsert "$body"
  dg_expect_200 "POST /v1/catalog/collections/upsert (bge-768)"
  dg_check "register the bge-768 collection -> ok" "d.get('ok') is True"
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'name':'$DG_VOY_COLL','content_type':'knowledge','owner_id':'gate-dual','embedding_model':'voyage-context-3'}))")"
  dg_request POST /v1/catalog/collections/upsert "$body"
  dg_expect_200 "POST /v1/catalog/collections/upsert (voyage-context-3)"
  dg_check "register the Voyage collection -> ok" "d.get('ok') is True"
}

# dg_write_chunk FAMILY(bge|voy) PASS: one document and one chunk into the family's collection, in ONE
# combined write (the chunk and its owner land together; a chunk is searchable only with a manifest row).
dg_write_chunk() {
  local fam="$1" pass="$2" coll text chash doc body title
  if [ "$fam" = "bge" ]; then coll="$DG_BGE_COLL"; else coll="$DG_VOY_COLL"; fi
  title="gate-dual-$fam-p$pass-$DG_UID"
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'owner_prefix':'$DG_OWNER_PREFIX','title':'$title','content_type':'knowledge','file_path':'/gate-dual/$title.md'}))")"
  dg_request POST /v1/catalog/doc/register "$body"
  dg_expect_200 "POST /v1/catalog/doc/register ($fam pass $pass)"
  dg_check "register the $fam document of pass $pass -> a tumbler under $DG_OWNER_PREFIX" \
    "isinstance(d.get('tumbler'), str) and d['tumbler'].startswith('$DG_OWNER_PREFIX.')"
  doc="$("$E2E_PYTHON" -c "import json;print(json.load(open('$DG_DIR/resp.json'))['tumbler'])")"
  text="dual embedder gate probe $DG_UID $fam pass $pass"
  chash="$("$E2E_PYTHON" -c "import hashlib;print(hashlib.sha256(b'$text').hexdigest())")"
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'collection':'$coll','docs':[{'doc_id':'$doc','rows':[{'position':0,'chash':'$chash'}]}],'chunks':[{'chash':'$chash','text':'$text','metadata':{'source':'gate-dual'}}]}))")"
  dg_request POST /v1/catalog/manifest/write_many "$body"
  dg_expect_200 "POST /v1/catalog/manifest/write_many ($fam pass $pass)"
  dg_check "write one $fam chunk in pass $pass -> chunks_written==1" "d.get('chunks_written')==1"
  if [ "$fam" = "bge" ]; then DG_BGE_CHASHES="$DG_BGE_CHASHES $chash"; else DG_VOY_CHASHES="$DG_VOY_CHASHES $chash"; fi
}

# dg_search_one COLLECTION: search ONE collection (the engine refuses a mixed-model request) and require
# EVERY chunk written to it so far, so a pass-2 call also proves the pass-1 chunk survived the restart.
dg_search_one() {
  local coll="$1" want body
  if [ "$coll" = "$DG_BGE_COLL" ]; then want="$DG_BGE_CHASHES"; else want="$DG_VOY_CHASHES"; fi
  # An empty want-set is a subset of every result, so it would pass vacuously.
  [ -n "${want// /}" ] || dg_fail "no chunk was written to $coll before its search (an empty expectation passes against anything)"
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'query':'$DG_QUERY','collections':['$coll'],'n_results':10}))")"
  dg_request POST /v1/vectors/search "$body"
  dg_expect_200 "POST /v1/vectors/search ($coll)"
  dg_check "search $coll alone -> returns every chunk written to it so far ($(printf '%s' "$want" | wc -w | tr -d ' '))" \
    "set('$want'.split()) <= {r.get('id') for r in d}"
}

# dg_combined_search PASS: ONE search over both collections through the real client, which splits per
# model and merges. Requires a hit from each collection carrying this pass's chunk text.
dg_combined_search() {
  local pass="$1"
  if ! NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx search "$DG_QUERY" \
        --corpus "$DG_BGE_COLL,$DG_VOY_COLL" --json --no-rerank --no-threshold --n 20 \
        </dev/null >"$DG_DIR/resp.json" 2>"$DG_DIR/combined.err"; then
    DG_CODE="n/a"
    echo "[dual-gate]   client stderr: $(cat "$DG_DIR/combined.err")" >&2
    dg_fail "nx search over both collections exited non-zero"
  fi
  dg_combined_hit "ONE search over both collections returns a hit from the bge collection (pass $pass)" \
    "any(r.get('collection')=='$DG_BGE_COLL' and 'dual embedder gate probe $DG_UID bge pass $pass' in (r.get('content') or '') for r in d)"
  DG_COMBINED_BGE_HITS=$((DG_COMBINED_BGE_HITS + 1))
  dg_combined_hit "...and a hit from the Voyage collection (pass $pass)" \
    "any(r.get('collection')=='$DG_VOY_COLL' and 'dual embedder gate probe $DG_UID voy pass $pass' in (r.get('content') or '') for r in d)"
  DG_COMBINED_VOY_HITS=$((DG_COMBINED_VOY_HITS + 1))
}

# dg_combined_hit DESC EXPR: dg_check for the combined search. A missing hit prints the client's
# stderr first: the client logs a model group it dropped there, and the JSON on stdout says nothing.
dg_combined_hit() {
  if ! dg_holds "$2"; then
    DG_CODE="n/a"
    echo "[dual-gate]   client stderr: $(cat "$DG_DIR/combined.err")" >&2
    dg_fail "$1"
  fi
  dg_ok "$1"
}

# dg_require_restart: pass 2's engine has a different process_start_time from pass 1's.
dg_require_restart() {
  [ "$DG_THIS_START" != "$DG_START_TIME_1" ] \
    || dg_fail "process_start_time is unchanged across the restart ($DG_THIS_START): the engine did not restart"
  echo "[dual-gate] ok: process_start_time moved ($DG_START_TIME_1 -> $DG_THIS_START)"
}

dg_run_pass() {
  local pass="$1"
  echo "[dual-gate] ── pass $pass ──"
  dg_version_check
  if [ "$pass" -eq 1 ]; then
    DG_START_TIME_1="$DG_THIS_START"
  else
    dg_require_restart
  fi
  dg_write_chunk bge "$pass"
  dg_write_chunk voy "$pass"
  dg_search_one "$DG_BGE_COLL"
  DG_BGE_SEARCHES=$((DG_BGE_SEARCHES + 1))
  dg_search_one "$DG_VOY_COLL"
  DG_VOY_SEARCHES=$((DG_VOY_SEARCHES + 1))
  dg_combined_search "$pass"
}

# The tally: the four searches each ran once per pass. A pass that returned early, a loop that never ran
# or a counter nobody incremented is a short tally and fails here rather than reading as a green run.
dg_require_tally() {
  local name val short=0
  for name in DG_BGE_SEARCHES DG_VOY_SEARCHES DG_COMBINED_BGE_HITS DG_COMBINED_VOY_HITS; do
    val="${!name}"
    if [ "$val" -ne "$DG_PASSES" ]; then
      echo "[dual-gate] TALLY SHORT: $name=$val, expected $DG_PASSES" >&2
      short=1
    fi
  done
  if [ "$short" -ne 0 ]; then
    exit 1
  fi
  echo "[dual-gate] tally: bge searches=$DG_BGE_SEARCHES voyage searches=$DG_VOY_SEARCHES combined hits (bge/voyage)=$DG_COMBINED_BGE_HITS/$DG_COMBINED_VOY_HITS over $DG_PASSES passes; $DG_PASSED checks passed"
}

# dg_set_embed_choice MODEL: record the install's embedder choice in the scratch config (no key is
# written; the supervisor derives the engine's NX_EMBED_PROFILE from this choice and ignores a caller's
# environment). The engine seeds its profile rows from the profile at EVERY boot and refuses to register a
# new collection of any other family, so a bge-profile install can hold a Voyage collection only when that
# collection was registered while the profile was Voyage (first live run, 2026-10-22: the registration of a
# Voyage collection under the bge profile answered 422).
dg_set_embed_choice() {
  NEXUS_CONFIG_DIR="$SCRATCH" uv run python -c "import pathlib, sys, yaml; p = pathlib.Path('$SCRATCH/config.yml'); d = (yaml.safe_load(p.read_text()) if p.exists() else None) or {}; d.setdefault('local', {})['embed_model'] = sys.argv[1]; p.write_text(yaml.safe_dump(d))" "$1"
}

# dg_seed_voyage_collection: the realistic route to a mixed install. Boot under the Voyage profile and
# register the Voyage collection; the switch to the bge profile follows.
dg_seed_voyage_collection() {
  local body
  echo "[dual-gate] seeding: a Voyage-profile boot registers the Voyage collection"
  dg_set_embed_choice voyage-context-3
  dg_start_engine
  dg_read_lease
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'tumbler_prefix':'$DG_OWNER_PREFIX','name':'gate-dual-owner','owner_type':'gate_dual'}))")"
  dg_request POST /v1/catalog/owners/upsert "$body"
  dg_expect_200 "POST /v1/catalog/owners/upsert (seed boot)"
  body="$("$E2E_PYTHON" -c "import json;print(json.dumps({'name':'$DG_VOY_COLL','content_type':'knowledge','owner_id':'gate-dual','embedding_model':'voyage-context-3'}))")"
  dg_request POST /v1/catalog/collections/upsert "$body"
  dg_expect_200 "POST /v1/catalog/collections/upsert (voyage-context-3, Voyage profile)"
  dg_check "register the Voyage collection under the Voyage profile -> ok" "d.get('ok') is True"
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop
  dg_set_embed_choice bge-base-en-v15-768
}

# dg_run_keyed_voyage_tests: the three tests tests/test_integration.py marks keyed_voyage (nexus-8errq):
# voyage-code-3 index and query, a voyage-context-3 markdown index through index_markdown, and a
# voyage-context-3 note write (their embedding_model asserts echo client-side metadata; what they prove is
# that the engine embeds and serves those collections). local-service-gate.sh runs keyless and deselects them, so this is
# their only gate. They are a keyed local install: their keyed_local_voyage fixture gives the client a
# Voyage local.embed_model, and, like every integration test, they run on the suite's own engine
# substrate (built from this tree's jar), which inherits the key and NX_EMBED_PROFILE=voyage from this
# environment; under the bge profile the engine refuses a new Voyage collection. NX_SUITE_LEASE_WAIT=1
# queues behind another substrate run on the box instead of refusing (the first live run met one).
# NX_BUILD_LEASE_WAIT does the same for the build lease: the release battery runs this leg beside
# local-service-gate.sh, which holds that lease while it stamps release.properties, and without the
# wait the substrate refused at once and the leg went red with no verdict (7.85.0 battery, 2026-10-23).
# EXACTLY three must pass: a test that did not run (no key reaching pytest, a deselect, a collection
# error) is a short count and a FAIL.
DG_KEYED_VOYAGE_EXPECTED=3
dg_run_keyed_voyage_tests() {
  local xml="$SCRATCH/keyed_voyage.xml" rc=0 counts passed total
  echo "[dual-gate] ── keyed_voyage tests: -m 'integration and keyed_voyage' on a keyed Voyage-profile substrate ──"
  NX_EMBED_PROFILE=voyage NX_SUITE_LEASE_WAIT=1 NX_BUILD_LEASE_WAIT="${NX_BUILD_LEASE_WAIT:-3600}" \
    dg_with_key uv run pytest tests/test_integration.py -o addopts="" -m "integration and keyed_voyage" \
    -q --color=no -p no:xdist --junit-xml="$xml" || rc=$?
  # A passed case has no failure, error or other outcome element under it.
  counts="$("$E2E_PYTHON" -c "import sys, xml.etree.ElementTree as ET
cases = list(ET.parse(sys.argv[1]).getroot().iter('testcase'))
print(sum(all(k.tag in ('system-out', 'system-err', 'properties') for k in c) for c in cases), len(cases))" "$xml" 2>/dev/null || echo "0 0")"
  read -r passed total <<<"$counts"
  if [ "$rc" -ne 0 ] || [ "$passed" -ne "$DG_KEYED_VOYAGE_EXPECTED" ] || [ "$total" -ne "$DG_KEYED_VOYAGE_EXPECTED" ]; then
    echo "[dual-gate] CHECK FAILED: keyed_voyage tests: pytest rc=$rc, $passed of $total passed, expected exactly $DG_KEYED_VOYAGE_EXPECTED" >&2
    exit 1
  fi
  DG_PASSED=$((DG_PASSED + 1))
  echo "[dual-gate] ok ($DG_PASSED): keyed_voyage tests: $passed of $DG_KEYED_VOYAGE_EXPECTED passed on the keyed substrate"
}

# ── The Phase 6 scenarios (RDR-210 Minimum Viable Validation, nexus-s08oe) ──
# The install switches to the Voyage profile and `nx collection re-embed --all --wait` moves every bge
# collection to its Voyage sibling: the gate's bge collection (its pass chunks plus a theme set of
# source-backed documents, one of them carrying an aspect row) and a `nx store put` note's collection.
# Checks: the same documents come back from a search before and after; the job ends done and the source
# is deleted; the aspect row survives, re-homed to the target; --wait's taxonomy discover gave the target
# a taxonomy_meta row and topic assignments (the real-engine check the verb tests only mock); and a
# second --all plans nothing. The theme documents are two sets of seven near-duplicates on unrelated
# subjects, so HDBSCAN (min_cluster_size 5) forms topics out of them: sklearn's HDBSCAN never selects the
# root cluster (allow_single_cluster is off), so ONE tight group comes back as all noise and discover
# writes no topics and no taxonomy_meta row (first live run, 2026-10-25: ten near-duplicates, 0 topics).
DG_MVV_THEME_DOCS=14
DG_MVV_QUERY="gate mvv re-embedding moves documents between embedding families $DG_UID"
DG_MVV_TOKEN_CAP=200000
DG_MVV_TUMBLERS=""
DG_MVV_CHASHES=""
DG_MVV_ASPECT_DOC=""
DG_MVV_NOTE_COLL=""
DG_MVV_RANK_TEXT=""
DG_MVV_RANK_CHASH=""

# dg_mvv_client ARGS...: the real client against the scratch install, stdout to $DG_DIR/client.out and
# stderr to $DG_DIR/client.err; returns the client's exit status. The client's write guard refuses a write
# from a dev checkout without a stated reason (nexus-a2qhz), and these writes go to this run's own engine.
# Once the install is on the Voyage profile (DG_MVV_CLIENT_KEYED=1) the client gets the key too, as a
# Voyage-profile install's client has it from config.yml: --wait's discover refuses a Voyage local.embed_model
# with no key (live run, 2026-10-25). Through dg_with_key, so the key is in the environment, never argv.
DG_MVV_WRITE_REASON="dual-embedder-gate Phase 6: self-provisioned throwaway engine in a scratch NEXUS_CONFIG_DIR (nexus-s08oe)"
DG_MVV_CLIENT_KEYED=0
dg_mvv_client() {
  local run=()
  [ "$DG_MVV_CLIENT_KEYED" -eq 1 ] && run=(dg_with_key)
  NX_ALLOW_PROD_WRITE="$DG_MVV_WRITE_REASON" NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" \
    ${run[@]+"${run[@]}"} uv run nx "$@" </dev/null >"$DG_DIR/client.out" 2>"$DG_DIR/client.err"
}

dg_mvv_client_fail() {
  DG_CODE="n/a"
  echo "[dual-gate]   client stdout: $(cat "$DG_DIR/client.out")" >&2
  echo "[dual-gate]   client stderr: $(cat "$DG_DIR/client.err")" >&2
  dg_fail "$1"
}

# dg_mvv_search CORPUS [QUERY]: one search over CORPUS through the real client (QUERY defaults to the MVV query).
# $DG_DIR/resp.json gets the chunk ids it returned (content-addressed, so a moved chunk keeps its id), the
# doc_ids, the chunk ids per collection and the top hit's id; the raw client output is kept in
# $DG_DIR/search.json and printed when a check on it fails.
dg_mvv_search() {
  dg_mvv_client search "${2:-$DG_MVV_QUERY}" --corpus "$1" --json --no-rerank --no-threshold --n 50 \
    || dg_mvv_client_fail "nx search over $1 exited non-zero"
  cp "$DG_DIR/client.out" "$DG_DIR/search.json"
  "$E2E_PYTHON" -c "import json, sys
rows = json.load(open(sys.argv[1]))
by_coll = {}
for r in rows:
    if r.get('id'):
        by_coll.setdefault(r.get('collection') or '', set()).add(r['id'])
json.dump({'ids': sorted({r.get('id') for r in rows if r.get('id')}),
           'doc_ids': sorted({r.get('doc_id') for r in rows if r.get('doc_id')}),
           'by_coll': {c: sorted(v) for c, v in by_coll.items()},
           'top': rows[0].get('id') if rows else None}, open(sys.argv[2], 'w'))" \
    "$DG_DIR/search.json" "$DG_DIR/resp.json" || dg_fail "could not read the search results of $1"
}

# dg_mvv_search_check DESC EXPR: dg_check on a dg_mvv_search result; a failure prints the raw results first.
dg_mvv_search_check() {
  if ! dg_holds "$2"; then
    DG_CODE="n/a"
    echo "[dual-gate]   search results: $("$E2E_PYTHON" -c "import json
print(json.dumps([{k: r.get(k) for k in ('id', 'doc_id', 'collection', 'title')} for r in json.load(open('$DG_DIR/search.json'))]))")" >&2
    dg_fail "$1"
  fi
  dg_ok "$1"
}

# dg_mvv_require_gone COLLECTION: the catalog has no row for COLLECTION (404), where the same route answers 200
# for the live Voyage collection, so the 404 is not a wrong path or tenant. A superseded source that a job kept
# (kept_tombstoned, kept_by_flag, kept_residue) still has its row and fails here.
dg_mvv_require_gone() {
  dg_request GET "/v1/catalog/collections/get?name=$DG_VOY_COLL"
  dg_expect_200 "GET /v1/catalog/collections/get ($DG_VOY_COLL, the control)"
  dg_request GET "/v1/catalog/collections/get?name=$1"
  [ "$DG_CODE" = "404" ] || dg_fail "the source collection $1 still has a catalog row (deleted means 404)"
  dg_ok "the source collection $1 is deleted (404; the live Voyage collection answers 200 on the same route)"
}

# dg_mvv_seed: under the bge profile, the theme documents (each a real file, registered by its path) in one
# combined write to the bge collection, an aspect row on the first, and one `nx store put` note. Two stand-ins,
# both fair for the move because the job re-embeds the stored chunk text and joins satellite rows by doc_id
# (RDR-210 Technical Design 4): the documents are registered and written over HTTP the way the client writes
# a manifest, not through `nx index`; and the aspect row is upserted directly, not by the extractor.
dg_mvv_seed() {
  local i path title doc text chash body docs_json="" chunks_json=""
  mkdir -p "$SCRATCH/mvv"
  for i in $(seq 1 "$DG_MVV_THEME_DOCS"); do
    path="$SCRATCH/mvv/theme-$i.md"
    if [ $((i % 2)) -eq 1 ]; then
      text="gate mvv $DG_UID theme document $i: re-embedding moves documents between embedding families"
    else
      text="gate mvv $DG_UID theme document $i: autumn apple harvest and cider pressing in the orchard"
    fi
    printf '%s\n' "$text" > "$path"
    title="gate-mvv-theme-$i-$DG_UID"
    body="$("$E2E_PYTHON" -c "import json, sys;print(json.dumps({'owner_prefix':'$DG_OWNER_PREFIX','title':sys.argv[1],'content_type':'knowledge','file_path':sys.argv[2]}))" "$title" "$path")"
    dg_request POST /v1/catalog/doc/register "$body"
    dg_expect_200 "POST /v1/catalog/doc/register (mvv theme $i)"
    doc="$("$E2E_PYTHON" -c "import json;print(json.load(open('$DG_DIR/resp.json')).get('tumbler',''))")"
    [ -n "$doc" ] || dg_fail "register mvv theme document $i returned no tumbler"
    chash="$("$E2E_PYTHON" -c "import hashlib, sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$text")"
    docs_json="$docs_json$("$E2E_PYTHON" -c "import json, sys;print(json.dumps({'doc_id':sys.argv[1],'rows':[{'position':0,'chash':sys.argv[2]}]}))" "$doc" "$chash")"$'\n'
    chunks_json="$chunks_json$("$E2E_PYTHON" -c "import json, sys;print(json.dumps({'chash':sys.argv[1],'text':sys.argv[2],'metadata':{'source':'gate-mvv','chunk_text_hash':sys.argv[1]}}))" "$chash" "$text")"$'\n'
    DG_MVV_TUMBLERS="$DG_MVV_TUMBLERS $doc"
    if [ -z "$DG_MVV_RANK_TEXT" ]; then DG_MVV_RANK_TEXT="$text"; DG_MVV_RANK_CHASH="$chash"; fi
    DG_MVV_CHASHES="$DG_MVV_CHASHES $chash"
    [ -n "$DG_MVV_ASPECT_DOC" ] || DG_MVV_ASPECT_DOC="$doc $path"
  done
  printf '%s' "$docs_json" > "$DG_DIR/mvv_docs.jsonl"
  printf '%s' "$chunks_json" > "$DG_DIR/mvv_chunks.jsonl"
  body="$("$E2E_PYTHON" -c "import json, sys
docs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
chunks = [json.loads(l) for l in open(sys.argv[2]) if l.strip()]
print(json.dumps({'collection': sys.argv[3], 'docs': docs, 'chunks': chunks}))" \
    "$DG_DIR/mvv_docs.jsonl" "$DG_DIR/mvv_chunks.jsonl" "$DG_BGE_COLL")"
  dg_request POST /v1/catalog/manifest/write_many "$body"
  dg_expect_200 "POST /v1/catalog/manifest/write_many (mvv theme set)"
  dg_check "write the $DG_MVV_THEME_DOCS mvv theme chunks to the bge collection -> chunks_written==$DG_MVV_THEME_DOCS" \
    "d.get('chunks_written')==$DG_MVV_THEME_DOCS"

  local adoc apath
  read -r adoc apath <<<"$DG_MVV_ASPECT_DOC"
  body="$("$E2E_PYTHON" -c "import json, sys
print(json.dumps({'collection': sys.argv[1], 'source_path': sys.argv[2], 'doc_id': sys.argv[3],
  'source_uri': 'file://' + sys.argv[2], 'problem_formulation': 'gate mvv aspect ' + sys.argv[4],
  'proposed_method': 'move the document', 'experimental_datasets': [], 'experimental_baselines': [],
  'experimental_results': '', 'extras': {}, 'confidence': 0.9, 'extracted_at': '2026-10-25T00:00:00Z',
  'model_version': 'gate', 'extractor_name': 'gate-dual', 'salient_sentences': []}))" \
    "$DG_BGE_COLL" "$apath" "$adoc" "$DG_UID")"
  dg_request POST /v1/aspects/upsert "$body"
  dg_expect_200 "POST /v1/aspects/upsert (mvv aspect)"
  dg_check "write an aspect row for the first mvv theme document -> written" "d.get('written') is True"

  printf 'gate mvv %s note: re-embedding moves documents between embedding families\n' "$DG_UID" > "$SCRATCH/mvv/note.txt"
  NX_ALLOW_PROD_WRITE="$DG_MVV_WRITE_REASON" NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx store put - --collection gate-mvv-notes \
    --title "gate-mvv-note-$DG_UID" <"$SCRATCH/mvv/note.txt" >"$DG_DIR/client.out" 2>"$DG_DIR/client.err" \
    || dg_mvv_client_fail "nx store put of the mvv note exited non-zero"
  DG_MVV_NOTE_COLL="$(awk '/^Stored: / { print $NF }' "$DG_DIR/client.out")"
  case "$DG_MVV_NOTE_COLL" in
    knowledge__*__bge-base-en-v15-768__v*) dg_ok "nx store put wrote the note to a bge collection ($DG_MVV_NOTE_COLL)" ;;
    *) dg_mvv_client_fail "nx store put did not report a bge-768 knowledge collection (got '$DG_MVV_NOTE_COLL')" ;;
  esac
}

# dg_run_mvv: the scenario, from the bge-profile engine of pass 2 to a second, empty --all.
dg_run_mvv() {
  local target body adoc apath
  echo "[dual-gate] ── Phase 6: re-embed --all (Minimum Viable Validation) ──"
  dg_mvv_seed
  dg_mvv_search "$DG_BGE_COLL,$DG_MVV_NOTE_COLL"
  dg_mvv_search_check "before: one search returns every mvv theme chunk and document from the bge collection" \
    "set('$DG_MVV_CHASHES'.split()) <= set(d['by_coll'].get('$DG_BGE_COLL', [])) and set('$DG_MVV_TUMBLERS'.split()) <= set(d['doc_ids'])"
  dg_mvv_search_check "before: ...and the note from the note's bge collection" \
    "len(d['by_coll'].get('$DG_MVV_NOTE_COLL', [])) == 1"
  cp "$DG_DIR/resp.json" "$DG_DIR/mvv_before.json"

  echo "[dual-gate] switching the install to the Voyage profile and restarting the engine"
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop
  dg_set_embed_choice voyage-context-3
  DG_MVV_CLIENT_KEYED=1
  dg_start_engine
  dg_read_lease
  dg_version_check

  dg_mvv_client collection re-embed --all || dg_mvv_client_fail "re-embed --all (dry run) exited non-zero"
  grep -qF "dry-run: 2 job(s) would start" "$DG_DIR/client.out" \
    || dg_mvv_client_fail "re-embed --all plans exactly the two bge collections"
  { grep -qF "$DG_BGE_COLL" "$DG_DIR/client.out" && grep -qF "$DG_MVV_NOTE_COLL" "$DG_DIR/client.out"; } \
    || dg_mvv_client_fail "re-embed --all's plan names both bge collections"
  dg_ok "re-embed --all (dry run) plans the two bge collections"

  dg_mvv_client collection re-embed --all --no-dry-run --yes --wait --token-cap "$DG_MVV_TOKEN_CAP" \
    || dg_mvv_client_fail "re-embed --all --no-dry-run --yes --wait exited non-zero"
  sed 's/^/[dual-gate]   re-embed: /' "$DG_DIR/client.out"
  echo "[dual-gate] re-embed --all --wait exited 0 (every job finished and discover ran without error)"

  dg_request GET /v1/vectors/reembed/status
  dg_expect_200 "GET /v1/vectors/reembed/status"
  dg_check "the job on the bge collection ended done with the Voyage collection as its target" \
    "any(j.get('source_collection')=='$DG_BGE_COLL' and j.get('state')=='done' and j.get('target_collection')=='$DG_VOY_COLL' for j in (d.get('jobs', d) if isinstance(d, dict) else d))"
  target="$("$E2E_PYTHON" -c "import json
d = json.load(open('$DG_DIR/resp.json'))
jobs = d.get('jobs', d) if isinstance(d, dict) else d
print(next((j.get('target_collection') or '' for j in jobs if j.get('source_collection')=='$DG_MVV_NOTE_COLL' and j.get('state')=='done'), ''))")" \
    || dg_fail "could not read the note's job from GET /v1/vectors/reembed/status"
  case "$target" in
    knowledge__*__voyage-context-3__v*) dg_ok "the job on the note's collection ended done, into $target" ;;
    *) dg_fail "no done job moved $DG_MVV_NOTE_COLL to a voyage-context-3 sibling (target '$target')" ;;
  esac

  dg_mvv_require_gone "$DG_BGE_COLL"
  dg_mvv_require_gone "$DG_MVV_NOTE_COLL"

  dg_mvv_search "$DG_VOY_COLL,$target"
  dg_mvv_search_check "after: the same search over the Voyage targets returns every chunk and document it returned before" \
    "set(json.load(open('$DG_DIR/mvv_before.json'))['ids']) <= set(d['ids']) and set(json.load(open('$DG_DIR/mvv_before.json'))['doc_ids']) <= set(d['doc_ids'])"
  dg_mvv_search_check "after: ...the theme chunks from the Voyage collection and the note from its Voyage sibling" \
    "set('$DG_MVV_CHASHES'.split()) <= set(d['by_coll'].get('$DG_VOY_COLL', [])) and json.load(open('$DG_DIR/mvv_before.json'))['by_coll']['$DG_MVV_NOTE_COLL'] == d['by_coll'].get('$target')"
  # The vectors are real, not just present: a document's own text finds it first after the move.
  dg_mvv_search "$DG_VOY_COLL" "$DG_MVV_RANK_TEXT"
  dg_mvv_search_check "after: the first theme document's own text ranks it first in the Voyage collection" \
    "d['top'] == '$DG_MVV_RANK_CHASH'"

  read -r adoc apath <<<"$DG_MVV_ASPECT_DOC"
  dg_request GET "/v1/aspects/get_by_doc_id?doc_id=$adoc"
  dg_expect_200 "GET /v1/aspects/get_by_doc_id ($adoc)"
  dg_check "the moved document's aspect row survived the source delete, re-homed to the Voyage collection" \
    "d.get('collection')=='$DG_VOY_COLL' and d.get('problem_formulation')=='gate mvv aspect $DG_UID'"

  dg_request GET "/v1/taxonomy/meta/last_count?collection=$DG_VOY_COLL"
  dg_expect_200 "GET /v1/taxonomy/meta/last_count ($DG_VOY_COLL; 404 means discover wrote no taxonomy_meta row)"
  dg_check "--wait's discover wrote a taxonomy_meta row for the Voyage target" \
    "d.get('count', 0) >= $DG_MVV_THEME_DOCS"
  body="$("$E2E_PYTHON" -c "import json, sys;print(json.dumps({'doc_ids': sys.argv[1].split()}))" "$DG_MVV_CHASHES")"
  dg_request POST /v1/taxonomy/assignments/for_docs "$body"
  dg_expect_200 "POST /v1/taxonomy/assignments/for_docs (mvv theme chunks)"
  dg_check "--wait's discover assigned topics to at least 10 of the $DG_MVV_THEME_DOCS moved theme chunks" \
    "len({r.get('doc_id') for r in d}) >= 10"

  dg_mvv_client collection re-embed --all || dg_mvv_client_fail "the second re-embed --all exited non-zero"
  grep -qF "No collection is off the profile." "$DG_DIR/client.out" \
    || dg_mvv_client_fail "a second re-embed --all plans nothing"
  dg_ok "a second re-embed --all plans nothing (No collection is off the profile.)"
}

# ── 3-5. Seed the Voyage collection, boot the bge-profile keyed engine, then run both passes around a restart ──
dg_seed_voyage_collection
dg_start_engine
dg_read_lease
dg_version_check
dg_setup
dg_run_pass 1
dg_restart_engine
dg_run_pass 2
dg_require_tally
dg_run_mvv
dg_run_keyed_voyage_tests
echo "[dual-gate] DUAL-EMBEDDER GATE PASSED"
