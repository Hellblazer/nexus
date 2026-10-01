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
python3 "$HERE/candidate_engine_stub.py" "$WORKDIR" >"$WORKDIR/stub.out" 2>&1 &
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

candidate_engine_refusals "$CFG" t8 0 >"$WORKDIR/o8d" 2>/dev/null; rc=$?
expect_rc "refusals line prints without cut mode" 0 "$rc"
expect_has "refusals line carries the counters" "$WORKDIR/o8d" "refused_total=0 would_refuse_total=0"

echo "Test 9: refusals read the log the lease's launch kind names, in cut mode, through an ambient proxy"
# A jar launch writes storage_service_jar.log (storage_service_daemon.py _svc_log_name); the
# lease carries launch_kind. Writing the NATIVE name beside a jar lease is the bug this pins.
write_lease_kind() {  # <artifact> <launch_kind>
    printf '{"endpoint":{"host":"127.0.0.1","port":%s,"artifact":"%s","launch_kind":"%s"}}' "$PORT" "$1" "$2" >"$CFG/storage_service_addr.501"
}
write_lease_kind "$JAR" jar
mkdir -p "$CFG/logs"
printf 'INFO boot\nWARN event=ownerless_chunk_write_refused source_path=/x\n' >"$CFG/logs/storage_service_jar.log"
NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_refusals "$CFG" t9 0 >"$WORKDIR/o9" 2>"$WORKDIR/e9"; rc=$?
expect_rc "cut mode: a refusal in storage_service_jar.log is a failure" 1 "$rc"
expect_has "the refusals line counts it" "$WORKDIR/o9" "log_lines=1 log=storage_service_jar.log"
printf 'INFO boot\nWARN event=ownerless_chunk_write_would_refuse source_path=/x\n' >"$CFG/logs/storage_service_jar.log"
NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_refusals "$CFG" t9 0 >"$WORKDIR/o9b" 2>/dev/null; rc=$?
expect_rc "cut mode: a would-refuse line (log-only) is a failure too" 1 "$rc"
printf 'INFO boot\n' >"$CFG/logs/storage_service_jar.log"
NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_refusals "$CFG" t9 0 >"$WORKDIR/o9c" 2>/dev/null; rc=$?
expect_rc "cut mode: a clean jar log passes" 0 "$rc"
rm -f "$CFG/logs/storage_service_jar.log"
NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_refusals "$CFG" t9 0 >"$WORKDIR/o9d" 2>"$WORKDIR/e9d"; rc=$?
expect_rc "cut mode: a MISSING engine log is a failure" 1 "$rc"
expect_has "the refusals line says there was no log" "$WORKDIR/o9d" "log=none"
printf 'INFO boot\n' >"$CFG/logs/storage_service_jar.log"
HTTP_PROXY="http://127.0.0.1:1" http_proxy="http://127.0.0.1:1" ALL_PROXY="http://127.0.0.1:1" \
    NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_refusals "$CFG" t9 0 >"$WORKDIR/o9e" 2>/dev/null; rc=$?
expect_rc "an ambient proxy does not break the loopback probe" 0 "$rc"
expect_has "the counters were read through the proxy setting" "$WORKDIR/o9e" "refused_total=0 would_refuse_total=0 log_lines=0"

echo "Test 10: a gate's own control must read EXACTLY (enforce: refused, log-only: would-refuse); anything else fails"
set_status() {  # <mode> <refused> <would>
    printf '{"ownerless_write_mode":"%s","ownerless_writes_refused_total":%s,"ownerless_writes_would_refuse_total":%s}' "$1" "$2" "$3" >"$WORKDIR/stub.status.json"
}
control_log() {  # <event> -- one engine log line carrying it
    printf 'INFO boot\nWARN event=%s source_path=/x\n' "$1" >"$CFG/logs/storage_service_jar.log"
}
cut_refusals() {  # <controls> -> rc of the end-of-journey read in cut mode
    NX_CUT_MODE=1 NX_CANDIDATE_ENGINE="$JAR" candidate_engine_refusals "$CFG" t10 "$1" >"$WORKDIR/o10" 2>"$WORKDIR/e10"
}
set_status enforce 1 0; control_log ownerless_chunk_write_refused
cut_refusals 1; expect_rc "enforce, one control, 1 refused 0 would 1 log line" 0 "$?"
expect_has "the line declares the control" "$WORKDIR/o10" "controls=1 mode=enforce"
cut_refusals 0; expect_rc "enforce, no control declared but one refusal read" 1 "$?"
expect_has "...is a writer nobody intended" "$WORKDIR/e10" "fix the writer before tagging"
set_status enforce 0 0; control_log ownerless_chunk_write_refused
cut_refusals 1; expect_rc "enforce, control declared but the counter reads 0 (dead counter)" 1 "$?"
expect_has "...says the counter did not see its own control" "$WORKDIR/e10" "did not see the gate's own control"
set_status enforce 1 0; printf 'INFO boot\n' >"$CFG/logs/storage_service_jar.log"
cut_refusals 1; expect_rc "enforce, counter 1 but no log line" 1 "$?"
set_status enforce 2 0; control_log ownerless_chunk_write_refused
cut_refusals 1; expect_rc "enforce, a second refusal beyond the control" 1 "$?"
set_status enforce 1 1; control_log ownerless_chunk_write_refused
cut_refusals 1; expect_rc "enforce, a would-refuse beside the control" 1 "$?"
set_status log-only 0 1; control_log ownerless_chunk_write_would_refuse
NX_CANDIDATE_EXPECT_OWNERLESS_MODE=log-only cut_refusals 1; expect_rc "log-only, one control, 0 refused 1 would 1 log line" 0 "$?"
set_status log-only 1 0; control_log ownerless_chunk_write_would_refuse
NX_CANDIDATE_EXPECT_OWNERLESS_MODE=log-only cut_refusals 1; expect_rc "log-only, the control counted as refused" 1 "$?"
rm -f "$WORKDIR/stub.status.json"

echo
echo "candidate_engine_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
