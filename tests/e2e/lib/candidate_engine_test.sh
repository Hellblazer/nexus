#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/lib/candidate_engine_test.sh: shell-level tests for candidate_engine.sh
# (nexus-0kmat). Self-provisioning: a throwaway tmpdir, a fake jar, a fake lease and a
# stub engine on an ephemeral port; no engine, no network, no nexus install.
# Run directly: `bash tests/e2e/lib/candidate_engine_test.sh`.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=./candidate_engine.sh disable=SC1091
source "$HERE/candidate_engine.sh"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/candidate_engine_test.XXXXXX")"
STUB_PID=""
trap '[ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null; rm -rf "$WORKDIR"' EXIT

PASS=0
FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
expect_rc() {  # <label> <want-rc> <got-rc>
    if [ "$2" = "$3" ]; then ok "$1 (rc=$3)"; else bad "$1: want rc=$2 got rc=$3"; fi
}
expect_has() {  # <label> <file> <needle>
    if grep -qF -- "$3" "$2"; then ok "$1"; else bad "$1: missing [$3] in: $(head -c 400 "$2")"; fi
}

JAR="$WORKDIR/engine candidate.jar"   # a space in the path on purpose
: >"$JAR"
BIN="$WORKDIR/nexus-service"
printf '#!/bin/sh\nexit 0\n' >"$BIN"
chmod +x "$BIN"
PINNED="$WORKDIR/pinned/nexus-service"
mkdir -p "$(dirname "$PINNED")"
printf '#!/bin/sh\nexit 0\n' >"$PINNED"
chmod +x "$PINNED"

run_load() {  # env assignments..., runs candidate_engine_load in a clean subshell
    ( env -i PATH="$PATH" HOME="$WORKDIR" "$@" bash -c '
        source "'"$HERE"'/candidate_engine.sh"
        candidate_engine_load || exit $?
        printf "%s\n" "${CAND_ENV_ARGS[@]+"${CAND_ENV_ARGS[@]}"}"
    ' )
}

echo "Test 1: no candidate, cut mode off -> pinned engine path unchanged, empty args"
run_load >"$WORKDIR/o1" 2>"$WORKDIR/e1"; rc=$?
expect_rc "load with nothing set" 0 "$rc"
expect_has "says it is the pinned published engine" "$WORKDIR/o1" "PINNED PUBLISHED"
if [ "$(grep -c '=' "$WORKDIR/o1")" = 0 ]; then ok "no env assignments"; else bad "unexpected assignments: $(cat "$WORKDIR/o1")"; fi

echo "Test 2: cut mode, no candidate -> refused loudly"
run_load NX_CUT_MODE=1 >"$WORKDIR/o2" 2>"$WORKDIR/e2"; rc=$?
expect_rc "cut mode without NX_CANDIDATE_ENGINE" 2 "$rc"
expect_has "names the vacuity" "$WORKDIR/e2" "PINNED PUBLISHED engine"

echo "Test 3: candidate set but missing -> refused, no fall-back"
run_load NX_CANDIDATE_ENGINE="$WORKDIR/absent.jar" >"$WORKDIR/o3" 2>"$WORKDIR/e3"; rc=$?
expect_rc "missing candidate" 2 "$rc"
expect_has "says it will not fall back" "$WORKDIR/e3" "Refusing to fall back"
run_load NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$WORKDIR/absent.jar" >/dev/null 2>&1; rc=$?
expect_rc "missing candidate in cut mode" 2 "$rc"

echo "Test 4: jar candidate -> NEXUS_SERVICE_JAR (+ JAVA_HOME), survives a path with a space"
run_load NX_CANDIDATE_ENGINE="$JAR" JAVA_HOME="$WORKDIR" >"$WORKDIR/o4" 2>"$WORKDIR/e4"; rc=$?
expect_rc "jar candidate" 0 "$rc"
expect_has "jar goes to NEXUS_SERVICE_JAR" "$WORKDIR/o4" "NEXUS_SERVICE_JAR="
expect_has "JAVA_HOME travels with a jar" "$WORKDIR/o4" "JAVA_HOME=$WORKDIR"
expect_has "path with a space is intact" "$WORKDIR/o4" "engine candidate.jar"

echo "Test 5: native candidate -> NEXUS_SERVICE_BIN; not executable -> refused"
run_load NX_CANDIDATE_ENGINE="$BIN" >"$WORKDIR/o5" 2>/dev/null; rc=$?
expect_rc "native candidate" 0 "$rc"
expect_has "native goes to NEXUS_SERVICE_BIN" "$WORKDIR/o5" "NEXUS_SERVICE_BIN="
NOEXEC="$WORKDIR/noexec"; : >"$NOEXEC"
run_load NX_CANDIDATE_ENGINE="$NOEXEC" >/dev/null 2>"$WORKDIR/e5"; rc=$?
expect_rc "non-executable native candidate" 2 "$rc"

echo "Test 6: an ambient launch variable naming a different artifact is a contradiction"
run_load NX_CANDIDATE_ENGINE="$JAR" JAVA_HOME="$WORKDIR" NEXUS_SERVICE_BIN="$BIN" >/dev/null 2>"$WORKDIR/e6"; rc=$?
expect_rc "jar candidate + ambient NEXUS_SERVICE_BIN" 2 "$rc"
run_load NX_CANDIDATE_ENGINE="$BIN" NEXUS_SERVICE_BIN="$BIN" >/dev/null 2>&1; rc=$?
expect_rc "same artifact named twice is fine" 0 "$rc"

echo "Test 7: the resolved variables survive an env -i scrub (the allowlist pattern the gates use)"
(
    # shellcheck disable=SC2030
    export NX_CANDIDATE_ENGINE="$JAR" JAVA_HOME="$WORKDIR"
    candidate_engine_load >/dev/null || exit 9
    env -i HOME="$WORKDIR" PATH="/usr/bin:/bin" \
        ${CAND_ENV_ARGS[@]+"${CAND_ENV_ARGS[@]}"} \
        /usr/bin/env >"$WORKDIR/o7"
)
expect_has "NEXUS_SERVICE_JAR present inside env -i" "$WORKDIR/o7" "NEXUS_SERVICE_JAR="
if grep -q '^NX_CANDIDATE_ENGINE=' "$WORKDIR/o7"; then bad "NX_CANDIDATE_ENGINE leaked into the scrubbed env"; else ok "the scrub still drops NX_* itself"; fi

echo "Test 7b: a stage dir gives each gate a private copy (the supervisor matches engines by argv)"
python3 "$HERE/candidate_engine.py" env --stage "$WORKDIR/stage-a" >/dev/null 2>&1; rc=$?
expect_rc "env --stage without a candidate set is the unchanged default" 0 "$rc"
NX_CANDIDATE_ENGINE="$JAR" JAVA_HOME="$WORKDIR" python3 "$HERE/candidate_engine.py" env --stage "$WORKDIR/stage-a" >"$WORKDIR/o7a" 2>&1
NX_CANDIDATE_ENGINE="$JAR" JAVA_HOME="$WORKDIR" python3 "$HERE/candidate_engine.py" env --stage "$WORKDIR/stage-b" >"$WORKDIR/o7b" 2>&1
expect_has "gate a's variables name its own copy" "$WORKDIR/o7a" "stage-a"
expect_has "gate b's variables name its own copy" "$WORKDIR/o7b" "stage-b"
if [ -f "$WORKDIR/stage-a/engine candidate.jar" ] && [ -f "$WORKDIR/stage-b/engine candidate.jar" ]; then ok "both copies exist"; else bad "a staged copy is missing"; fi

echo "Test 8: identity + refusals against a stub engine (candidate, then the pinned binary)"
python3 - "$WORKDIR" >"$WORKDIR/stub.out" 2>&1 <<'PY' &
import http.server, json, sys, threading
workdir = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/version":
            body = {"release_version": "0.1.142", "build_ref": "abc1234+99"}
        elif self.path == "/v1/status":
            body = {"ownerless_write_mode": "enforce", "ownerless_writes_refused_total": 0,
                    "ownerless_writes_would_refuse_total": 0}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
open(workdir + "/stub.port", "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY
STUB_PID=$!
for _ in $(seq 1 50); do [ -s "$WORKDIR/stub.port" ] && break; sleep 0.1; done
if [ -s "$WORKDIR/stub.port" ]; then ok "stub engine up on an ephemeral port"; else bad "stub engine did not start: $(cat "$WORKDIR/stub.out")"; fi
PORT="$(cat "$WORKDIR/stub.port" 2>/dev/null || echo 0)"
CFG="$WORKDIR/cfg"; mkdir -p "$CFG"
write_lease() {  # <artifact>
    printf '{"endpoint":{"host":"127.0.0.1","port":%s,"artifact":"%s"}}' "$PORT" "$1" >"$CFG/storage_service_addr.501"
}

write_lease "$JAR"
NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_identity "$CFG" t8 >"$WORKDIR/o8" 2>"$WORKDIR/e8"; rc=$?
expect_rc "cut mode, lease artifact IS the candidate" 0 "$rc"
expect_has "identity line says candidate=yes" "$WORKDIR/o8" "candidate=yes"
expect_has "identity line names the artifact" "$WORKDIR/o8" "engine candidate.jar"
expect_has "identity line carries release_version" "$WORKDIR/o8" "release_version=0.1.142"
expect_has "identity line carries build_ref" "$WORKDIR/o8" "build_ref=abc1234+99"
expect_has "identity line carries the ownerless mode" "$WORKDIR/o8" "ownerless_write_mode=enforce"

write_lease "$PINNED"
NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_identity "$CFG" t8 >"$WORKDIR/o8b" 2>"$WORKDIR/e8b"; rc=$?
expect_rc "cut mode, lease artifact is the PINNED engine -> failure" 1 "$rc"
expect_has "identity line says candidate=no" "$WORKDIR/o8b" "candidate=no"
expect_has "failure names the vacuity" "$WORKDIR/e8b" "ran against the pinned published engine"

NX_CANDIDATE_ENGINE="$JAR" candidate_engine_identity "$CFG" t8 >"$WORKDIR/o8c" 2>/dev/null; rc=$?
expect_rc "not cut mode: a pinned engine is reported, not failed" 0 "$rc"

candidate_engine_refusals "$CFG" t8 >"$WORKDIR/o8d" 2>/dev/null; rc=$?
expect_rc "refusals line prints without cut mode" 0 "$rc"
expect_has "refusals line carries the counters" "$WORKDIR/o8d" "refused_total=0 would_refuse_total=0"

echo "Test 9: the gates are wired (static)"
for gate in fresh-install-mvv.sh data-token-cli-gate.sh release-sandbox.sh; do
    f="$REPO_ROOT/tests/e2e/$gate"
    for needle in "candidate_engine.sh" "candidate_engine_load" "candidate_engine_identity" "candidate_engine_refusals"; do
        if grep -qF "$needle" "$f"; then ok "$gate uses $needle"; else bad "$gate does not use $needle"; fi
    done
done
for gate in fresh-install-mvv.sh data-token-cli-gate.sh; do
    f="$REPO_ROOT/tests/e2e/$gate"
    n_scrub="$(grep -c 'env -i' "$f")"
    n_pass="$(grep -c 'CAND_ENV_ARGS\[@\]+' "$f")"
    if [ "$n_pass" -ge 1 ]; then ok "$gate puts CAND_ENV_ARGS inside an env -i allowlist ($n_pass of $n_scrub scrubs)"; else bad "$gate never passes CAND_ENV_ARGS to an env -i"; fi
done

echo
echo "candidate_engine_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
