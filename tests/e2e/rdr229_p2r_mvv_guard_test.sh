#!/usr/bin/env bash
# tests/e2e/rdr229_p2r_mvv_guard_test.sh -- guard test for tests/e2e/rdr229-p2r-mvv.sh (RDR-229 P2.R, nexus-p2m1x.16).
#
# The gate needs a JDK, a PG bundle and an engine build, so this test needs none of them. It asserts the properties
# that keep the gate from passing vacuously (the nexus-moht0 doctrine):
#
#   1. An absent dependency (uv, java, git) exits 2 naming it, BEFORE a scratch dir is made or anything is built:
#      run for real with a PATH of shims, the shim log stays empty. Positive control: with all three present the
#      gate gets past preflight and reaches the jar build.
#   2. A jar build that fails exits 2, not 0.
#   3. The substrate test must have run: one test, none skipped, none errored, and the fixture and checks 1-4 must
#      have recorded. A skipped test, a missing record and an errored setup each exit 2 (a fake `uv` stands in for
#      pytest and writes what each case needs).
#   4. The script has no path that exits 0 other than the report's own status: no `exit 0`, no skip vocabulary,
#      and its last command is `exit "$REPORT_RC"`. Static, with positive controls that the detectors fire.
#   5. The report: all five checks PASS exits 0; any FAIL, a check that did not run, a census gate that ran no test,
#      or a census problem exits 1 and names the Phase 2 step the FAIL waits on.
#
# Run with: bash tests/e2e/rdr229_p2r_mvv_guard_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
GATE="$REPO_ROOT/tests/e2e/rdr229-p2r-mvv.sh"
REPORT="$REPO_ROOT/tests/e2e/lib/rdr229_p2r_report.py"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/rdr229_p2r_mvv_guard_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

fail_loud_and_exit() {
  bad "$1"
  echo
  echo "rdr229_p2r_mvv_guard_test.sh: $PASS passed, $FAIL failed"
  exit 1
}

[[ -f "$GATE" ]] || fail_loud_and_exit "tests/e2e/rdr229-p2r-mvv.sh does not exist"
[[ -f "$REPORT" ]] || fail_loud_and_exit "tests/e2e/lib/rdr229_p2r_report.py does not exist"
# shellcheck source=lib/python.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || fail_loud_and_exit "no Python 3.10+ for the harness (e2e_python_resolve, stderr above)"
PY="$E2E_PYTHON"

# -- A throwaway repo that holds a copy of the gate, so REPO_ROOT resolves into it ------------------------------------
make_repo() {  # <dir>
  local r="$1"
  mkdir -p "$r/tests/e2e/lib" "$r/scripts"
  cp "$GATE" "$r/tests/e2e/rdr229-p2r-mvv.sh"
  : > "$r/tests/e2e/rdr229_p2r_mvv_checks.py"
  : > "$r/tests/e2e/lib/rdr229_p2r_report.py"
  printf '#!/usr/bin/env bash\necho "build-gate-jar $*" >> "$SHIM_LOG"\nexit "${FAKE_BUILD_RC:-0}"\n' > "$r/scripts/build-gate-jar.sh"
  printf '#!/usr/bin/env bash\necho "mvnw-leased $*" >> "$SHIM_LOG"\nexit 0\n' > "$r/scripts/mvnw-leased.sh"
  chmod +x "$r/tests/e2e/rdr229-p2r-mvv.sh" "$r/scripts/build-gate-jar.sh" "$r/scripts/mvnw-leased.sh"
}

# Shim dir: java and git always; uv optionally. The fake uv logs every call, runs `uv run python` for real, and
# fakes `uv run pytest` by FAKE_MODE (writes the junit file and the results the gate then judges).
make_shims() {  # <dir> <with_uv: 1|0> <with_java: 1|0> <with_git: 1|0>
  local d="$1"
  mkdir -p "$d"
  if [[ "$3" == 1 ]]; then printf '#!/usr/bin/env bash\necho "java $*" >> "$SHIM_LOG"\n' > "$d/java"; chmod +x "$d/java"; fi
  if [[ "$4" == 1 ]]; then printf '#!/usr/bin/env bash\necho "git $*" >> "$SHIM_LOG"\necho abc1234\n' > "$d/git"; chmod +x "$d/git"; fi
  if [[ "$2" == 1 ]]; then
    cat > "$d/uv" <<SHIM
#!/usr/bin/env bash
echo "uv \$*" >> "\$SHIM_LOG"
[ "\$1" = run ] || exit 0
shift
case "\$1" in
  python) shift; exec "$PY" "\$@" ;;
  pytest)
    junit=""; for a in "\$@"; do case "\$a" in --junitxml=*) junit="\${a#--junitxml=}";; esac; done
    case "\${FAKE_MODE:-}" in
      skipped)  printf '<testsuites><testsuite tests="2" skipped="1" errors="0" failures="0"/></testsuites>' > "\$junit"; exit 0 ;;
      errored)  printf '<testsuites><testsuite tests="2" skipped="0" errors="1" failures="0"/></testsuites>' > "\$junit"; exit 1 ;;
      missing)  printf '<testsuites><testsuite tests="2" skipped="0" errors="0" failures="1"/></testsuites>' > "\$junit"
                printf '{"n": 0}\n{"n": 1}\n{"n": 2}\n' >> "\$P2R_RESULTS"; exit 1 ;;
      no6)      printf '<testsuites><testsuite tests="2" skipped="0" errors="0" failures="1"/></testsuites>' > "\$junit"
                printf '{"n": 0}\n{"n": 1}\n{"n": 2}\n{"n": 3}\n{"n": 4}\n' >> "\$P2R_RESULTS"; exit 1 ;;
      *)        printf '<testsuites><testsuite tests="2" skipped="0" errors="0" failures="0"/></testsuites>' > "\$junit"
                printf '{"n": 0}\n{"n": 1}\n{"n": 2}\n{"n": 3}\n{"n": 4}\n{"n": 6}\n' >> "\$P2R_RESULTS"; exit 0 ;;
    esac ;;
esac
exit 0
SHIM
    chmod +x "$d/uv"
  fi
}

run_gate() {  # <repo> <shim dir> [env assignments...]; stdout+stderr to $WORKDIR/out, status echoed
  local repo="$1" shims="$2"; shift 2
  : > "$WORKDIR/shim.log"
  env -i HOME="$WORKDIR/home" TMPDIR="$WORKDIR/tmp" PATH="$shims:$WORKDIR/sysbin" SHIM_LOG="$WORKDIR/shim.log" "$@" \
    /bin/bash "$repo/tests/e2e/rdr229-p2r-mvv.sh" > "$WORKDIR/out" 2>&1
  echo $?
}
mkdir -p "$WORKDIR/home" "$WORKDIR/tmp" "$WORKDIR/sysbin"
# The only system tools the gate calls, linked into a PATH of their own: the host's own java and VCS tool (macOS
# ships stubs of both under /usr/bin) must not satisfy a "missing" case.
for tool in bash env dirname mktemp rm mkdir tee tail cat head grep ls; do
  src="$(command -v "$tool")" || fail_loud_and_exit "the test host has no $tool"
  ln -s "$src" "$WORKDIR/sysbin/$tool"
done

echo "== 1. an absent dependency exits 2 before anything runs"
for missing in uv java git; do
  repo="$WORKDIR/repo-no-$missing"; make_repo "$repo"
  shims="$WORKDIR/shims-no-$missing"
  make_shims "$shims" "$([[ $missing == uv ]] && echo 0 || echo 1)" "$([[ $missing == java ]] && echo 0 || echo 1)" \
    "$([[ $missing == git ]] && echo 0 || echo 1)"
  rc="$(run_gate "$repo" "$shims")"
  if [[ "$rc" == 2 ]] && grep -q "GATE ERROR: $missing is not on PATH" "$WORKDIR/out" && [[ ! -s "$WORKDIR/shim.log" ]] \
     && ! compgen -G "$WORKDIR/tmp/nx-rdr229-p2r-mvv.*" >/dev/null; then
    ok "no $missing: exit 2 naming it, no shim called, no scratch dir made"
  else
    bad "no $missing: rc=$rc; out: $(head -3 "$WORKDIR/out" | tr '\n' ' '); shim log: $(cat "$WORKDIR/shim.log" 2>/dev/null | tr '\n' ' ')"
  fi
done

echo "== 2. positive control and the build failure"
repo="$WORKDIR/repo-ctl"; make_repo "$repo"; shims="$WORKDIR/shims-ctl"; make_shims "$shims" 1 1 1
rc="$(run_gate "$repo" "$shims" FAKE_BUILD_RC=1)"
if [[ "$rc" == 2 ]] && grep -q "build-gate-jar.sh failed" "$WORKDIR/out" && grep -q '^build-gate-jar' "$WORKDIR/shim.log"; then
  ok "all three present: preflight passes, the jar build runs, its failure exits 2 (not 0)"
else
  bad "build failure: rc=$rc; out: $(tail -3 "$WORKDIR/out" | tr '\n' ' ')"
fi

echo "== 3. the substrate test must have run to the end"
for mode in skipped errored missing no6; do
  repo="$WORKDIR/repo-$mode"; make_repo "$repo"
  rc="$(run_gate "$repo" "$shims" FAKE_MODE=$mode)"
  if [[ "$rc" == 2 ]] && grep -q "did not run to the end" "$WORKDIR/out" && ! grep -q '^mvnw-leased' "$WORKDIR/shim.log"; then
    ok "$mode: exit 2, and the census gates were never reached"
  else
    bad "$mode: rc=$rc; out: $(tail -3 "$WORKDIR/out" | tr '\n' ' ')"
  fi
done
repo="$WORKDIR/repo-complete"; make_repo "$repo"
rc="$(run_gate "$repo" "$shims" FAKE_MODE=complete)"
if grep -q '^mvnw-leased' "$WORKDIR/shim.log" && grep -q 'tests/test_collection_name_parse_census.py -m lint' "$WORKDIR/shim.log"; then
  ok "a complete substrate run reaches both census gates (client by -m lint, engine by Maven); rc=$rc is the report's"
else
  bad "complete run did not reach the census gates: rc=$rc; shim log: $(cat "$WORKDIR/shim.log" | tr '\n' ' ')"
fi

echo "== 4. no exit path but the report's"
code_lines() { grep -vE '^\s*#' "$1"; }
detect_bad_exit() { code_lines "$1" | grep -nE '(^|[;&|[:space:]])exit[[:space:]]+0\b|skip[-_ ]?pass|\|\|[[:space:]]*true|SKIP' ; }
if [[ -z "$(detect_bad_exit "$GATE")" ]]; then ok "the gate has no 'exit 0', no skip vocabulary, no '|| true'"; else bad "a skip path: $(detect_bad_exit "$GATE")"; fi
printf 'x\nexit 0\n' > "$WORKDIR/ctl1.sh"; printf 'y || true\n' > "$WORKDIR/ctl2.sh"; printf 'echo skip-pass\n' > "$WORKDIR/ctl3.sh"
if [[ -n "$(detect_bad_exit "$WORKDIR/ctl1.sh")" && -n "$(detect_bad_exit "$WORKDIR/ctl2.sh")" && -n "$(detect_bad_exit "$WORKDIR/ctl3.sh")" ]]; then
  ok "positive control: the detector fires on exit 0, '|| true' and skip-pass"
else
  bad "the skip-path detector is blind to one of its own controls"
fi
grep -qE '^export NX_ALLOW_PROD_WRITE' "$GATE" && bad "NX_ALLOW_PROD_WRITE is exported for the whole gate" \
  || ok "NX_ALLOW_PROD_WRITE is given to the substrate pytest run only, never exported"
last="$(code_lines "$GATE" | grep -v '^\s*$' | tail -1)"
[[ "$last" == 'exit "$REPORT_RC"' ]] && ok "the last command is exit \"\$REPORT_RC\"" || bad "the last command is: $last"
grep -q 'uv run python -I "\$REPO_ROOT/tests/e2e/lib/rdr229_p2r_report.py" report' "$GATE" \
  && ok "REPORT_RC comes from the report subcommand" || bad "REPORT_RC is not the report's status"

echo "== 5. the report"
mkdir -p "$WORKDIR/rep"
census_ok='{"client_live": {"holdover": 0}, "client_pin": {}, "client_pin_total": 0, "client_holdover_by_step": {}, "engine_pins": {"QUARANTINE_LITERAL_TOTAL_CEILING": 0, "COLLECTION_SPLIT_TOTAL_CEILING": 0, "COLLECTION_LITERAL_TOTAL_CEILING": 4, "QUARANTINE_READ_TOTAL_CEILING": 36}, "problems": []}'
census_bad='{"client_live": {"holdover": 23}, "client_pin": {}, "client_pin_total": 240, "client_holdover_by_step": {"P2.2": 19, "P2.4": 4}, "engine_pins": {"QUARANTINE_LITERAL_TOTAL_CEILING": 0, "COLLECTION_SPLIT_TOTAL_CEILING": 0, "COLLECTION_LITERAL_TOTAL_CEILING": 4, "QUARANTINE_READ_TOTAL_CEILING": 36}, "problems": [{"what": "client holdover class is 23, Phase 2 leaves 0", "waits_on": "P2.2 (nexus-p2m1x.7), P2.4 (nexus-p2m1x.9)"}]}'
mk_results() {  # <file> <status of 3> [status of 6]; the other checks pass
  "$PY" - "$1" "$2" "${3:-PASS}" <<'PYEOF'
import json, sys
path, three, six = sys.argv[1:4]
rows = [{"n": 0, "name": "fixture", "status": "PASS", "error": "", "evidence": {"name": "c", "row": {"owner_id": "o", "embedding_model": "m"}, "name_owner": "n", "name_model": "v"}}]
for n in (1, 2, 3, 4, 6):
    st = three if n == 3 else (six if n == 6 else "PASS")
    rows.append({"n": n, "name": f"check {n}", "status": st, "error": "boom\nline" if st != "PASS" else "", "evidence": {"examined": 2}})
open(path, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PYEOF
}
mk_results "$WORKDIR/rep/all-pass.jsonl" PASS
mk_results "$WORKDIR/rep/one-fail.jsonl" FAIL
printf '%s' "$census_ok" > "$WORKDIR/rep/census-ok.json"; printf '%s' "$census_bad" > "$WORKDIR/rep/census-bad.json"
report() { "$PY" "$REPORT" report "$@" > "$WORKDIR/rep/out" 2>&1; echo $?; }
rc="$(report "$WORKDIR/rep/all-pass.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 0 3)"
[[ "$rc" == 0 ]] && grep -q "RESULT: PASS" "$WORKDIR/rep/out" && ok "six PASS: exit 0" || bad "all pass gave rc=$rc"
rc="$(report "$WORKDIR/rep/one-fail.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 0 3)"
[[ "$rc" == 1 ]] && grep -q "check 3  FAIL" "$WORKDIR/rep/out" && grep -q "P2.5" "$WORKDIR/rep/out" \
  && ok "a FAIL: exit 1, named, with the step it waits on (P2.5)" || bad "one fail gave rc=$rc"
mk_results "$WORKDIR/rep/six-fail.jsonl" PASS FAIL
mk_results "$WORKDIR/rep/six-error.jsonl" PASS ERROR
head -3 "$WORKDIR/rep/all-pass.jsonl" > "$WORKDIR/rep/short.jsonl"
rc="$(report "$WORKDIR/rep/six-fail.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 0 3)"
[[ "$rc" == 1 ]] && grep -q "check 6  FAIL" "$WORKDIR/rep/out" && grep -q "P2.10" "$WORKDIR/rep/out" \
  && ok "check 6 FAIL: exit 1, naming P2.10" || bad "check 6 fail gave rc=$rc"
rc="$(report "$WORKDIR/rep/six-error.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 0 3)"
[[ "$rc" == 2 ]] && grep -q "GATE ERROR" "$WORKDIR/rep/out" && ok "check 6 ERROR (a gate fault): exit 2, not a FAIL" || bad "check 6 error gave rc=$rc"
rc="$(report "$WORKDIR/rep/short.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 0 3)"
[[ "$rc" == 1 ]] && grep -q "check 3  NOT RUN" "$WORKDIR/rep/out" && ok "a check that never ran: exit 1" || bad "missing check gave rc=$rc"
rc="$(report "$WORKDIR/rep/all-pass.jsonl" "$WORKDIR/rep/census-ok.json" 0 0 0 3)"
[[ "$rc" == 2 ]] && grep -q "GATE ERROR" "$WORKDIR/rep/out" && ok "a client census gate with no test count (an infrastructure fault): exit 2, not a FAIL" \
  || bad "client n=0 gave rc=$rc"
rc="$(report "$WORKDIR/rep/all-pass.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 1 0)"
[[ "$rc" == 2 ]] && ok "an engine census gate that failed with no test count (compile error, no report): exit 2" \
  || bad "engine n=0 gave rc=$rc"
rc="$(report "$WORKDIR/rep/all-pass.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 1 22)"
[[ "$rc" == 1 ]] && ok "an engine census gate that ran tests and is red: exit 1, a FAIL" || bad "engine red gave rc=$rc"
rc="$(report "$WORKDIR/rep/all-pass.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 0 3)"
labelled="$(grep -c "pre-satisfied guard" "$WORKDIR/rep/out")"
[[ "$labelled" == 2 ]] && ok "checks 2 and 4 are labelled pre-satisfied guards, and no other check is" \
  || bad "the pre-satisfied labels are wrong: $labelled"
"$PY" "$REPORT" report "$WORKDIR/rep/does-not-exist.jsonl" "$WORKDIR/rep/census-ok.json" 0 7 0 3 > /dev/null 2>&1
[[ "$?" == 2 ]] && ok "an uncaught exception in the report exits 2" || bad "the report's crash did not exit 2"
rc="$(report "$WORKDIR/rep/all-pass.jsonl" "$WORKDIR/rep/census-bad.json" 0 7 0 3)"
[[ "$rc" == 1 ]] && grep -q "P2.2" "$WORKDIR/rep/out" && grep -q "P2.4" "$WORKDIR/rep/out" \
  && ok "a census problem: exit 1, naming P2.2 and P2.4" || bad "census problem gave rc=$rc"

echo
echo "rdr229_p2r_mvv_guard_test.sh: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
