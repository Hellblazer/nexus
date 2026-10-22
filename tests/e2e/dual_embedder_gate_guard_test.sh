#!/usr/bin/env bash
# tests/e2e/dual_embedder_gate_guard_test.sh -- guard test for
# tests/e2e/dual-embedder-gate.sh (RDR-210 Phase 5 Step 1, bead nexus-8x0tg).
#
# The gate boots a KEYED dual-embedder local engine, so it needs a real Voyage key
# and an engine. This test needs neither. It asserts the two properties that keep
# the gate from passing vacuously (the nexus-moht0 doctrine):
#
#   1. With the key absent from EVERY source in the credential chain (environment
#      NX_VOYAGE_API_KEY and VOYAGE_API_KEY, the repo .env, credentials.voyage_api_key
#      in config.yml) the gate exits non-zero with a message naming the key, BEFORE
#      it creates a scratch dir or runs uv, nx or a build. Run for real, with a
#      shimmed PATH whose uv and nx record every call: the shim log stays empty.
#      Positive controls: the key present in any ONE source gets past the check
#      (the shim is called), so the refusal is not a gate that always refuses; and
#      a decoy in the wrong place (another credential, or a voyage_api_key outside
#      the credentials block) does not count as a key.
#   2. The script has no path that exits 0 without having run both searches in both
#      passes: no `exit 0`, no skip vocabulary, the two passes straddle a restart,
#      every pass runs the bge search, the Voyage search and the combined search,
#      and a final tally refuses unless each ran once per pass. Static, in the style
#      of local_service_gate_guard_test.sh, with positive controls proving the
#      detectors fire on a script that does have a skip path.
#
# Run with: bash tests/e2e/dual_embedder_gate_guard_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
GATE="$REPO_ROOT/tests/e2e/dual-embedder-gate.sh"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/dual_embedder_gate_guard_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

fail_loud_and_exit() {
  bad "$1"
  echo
  echo "dual_embedder_gate_guard_test.sh: $PASS passed, $FAIL failed"
  exit 1
}

[[ -f "$GATE" ]] || fail_loud_and_exit "tests/e2e/dual-embedder-gate.sh does not exist"

# ── Hermetic harness ─────────────────────────────────────────────────────────
# Every run gets an empty HOME, an empty config dir, an empty TMPDIR and a PATH
# whose uv/nx/mvnw shims log and fail. Nothing real is reachable and no network
# call is possible. env -i, so no key leaks in from the caller's environment.
SHIMS="$WORKDIR/shims"
mkdir -p "$SHIMS"
for tool in uv nx mvnw; do
  cat > "$SHIMS/$tool" <<'SHIM'
#!/bin/sh
echo "$(basename "$0") $*" >> "$SHIM_LOG"
exit 97
SHIM
  chmod +x "$SHIMS/$tool"
done

FAKE_KEY="pa-guard-test-0000000000000000000000000000000000000000"
CASE=0

# run_gate <case-label> [VAR=value ...]   -> sets OUT, RC, LOG, T (the case's TMPDIR)
# Config dir content is whatever the caller wrote to "$CFG/config.yml" first.
run_gate() {
  CASE=$((CASE + 1))
  shift   # the label is for the reader of this test; it is not an env assignment
  local home="$WORKDIR/c$CASE/home" cfgdir="$WORKDIR/c$CASE/cfg"
  T="$WORKDIR/c$CASE/tmp"
  LOG="$WORKDIR/c$CASE/shim.log"
  mkdir -p "$home" "$T" "$cfgdir"
  if [[ -n "${CFG_YML:-}" ]]; then printf '%s\n' "$CFG_YML" > "$cfgdir/config.yml"; fi
  : > "$LOG"
  OUT="$(env -i PATH="$SHIMS:$PATH" HOME="$home" TMPDIR="$T" NEXUS_CONFIG_DIR="$cfgdir" \
        NX_DUAL_GATE_ENV_FILE="${ENV_FILE:-$WORKDIR/absent.env}" NX_NO_TELEMETRY=1 SHIM_LOG="$LOG" \
        "$@" bash "$GATE" </dev/null 2>&1)"
  RC=$?
}

assert_refused_before_anything() {  # <label>
  local label="$1"
  [[ $RC -ne 0 ]] && ok "$label: exits non-zero (rc $RC)" || bad "$label: exited 0 with no key"
  [[ "$OUT" == *VOYAGE_API_KEY* ]] && ok "$label: the message names the missing key" \
    || bad "$label: message does not name VOYAGE_API_KEY: $OUT"
  [[ "$OUT" == *FAIL* && "$OUT" != *SKIP* ]] && ok "$label: reads as a FAIL, not a skip" \
    || bad "$label: message is not a clear FAIL: $OUT"
  [[ ! -s "$LOG" ]] && ok "$label: no uv, nx or mvnw was run (no engine started)" \
    || bad "$label: tools were run before the key check: $(cat "$LOG")"
  [[ -z "$(ls -A "$T")" ]] && ok "$label: no scratch dir was created" \
    || bad "$label: scratch left behind in TMPDIR: $(ls -A "$T")"
}

assert_got_past_the_key_check() {  # <label>
  local label="$1"
  [[ -s "$LOG" ]] && ok "$label: reached the engine bring-up (uv was called)" \
    || bad "$label: never got past the key check: $OUT"
  [[ "$OUT" != *"no Voyage API key"* ]] && ok "$label: no missing-key message" \
    || bad "$label: refused for a missing key although one was supplied: $OUT"
  [[ "$OUT" != *"$FAKE_KEY"* ]] && ok "$label: the key value is never printed" \
    || bad "$label: the key value leaked into the output"
  [[ -z "$(ls -A "$T")" ]] && ok "$label: a failed run cleans its scratch dir" \
    || bad "$label: scratch left behind in TMPDIR: $(ls -A "$T")"
}

echo "Test 1: key absent from every source -> FAIL before any engine"
CFG_YML="" ENV_FILE="" run_gate "no key"
assert_refused_before_anything "no key"

echo "Test 1b: a blank key is no key"
CFG_YML=$'credentials:\n  voyage_api_key: ""' ENV_FILE="" run_gate "blank" VOYAGE_API_KEY="   " NX_VOYAGE_API_KEY=""
assert_refused_before_anything "blank key"

echo "Test 1c: decoys do not count"
printf 'OTHER_KEY=%s\n# VOYAGE_API_KEY=%s\n' "$FAKE_KEY" "$FAKE_KEY" > "$WORKDIR/decoy.env"
CFG_YML=$'credentials:\n  service_token: abc\nlocal:\n  voyage_api_key: '"$FAKE_KEY"$'\n  embed_model: bge-base-en-v15-768' \
  ENV_FILE="$WORKDIR/decoy.env" run_gate "decoys"
assert_refused_before_anything "decoys (wrong credential, commented line, key outside credentials:)"

echo "Test 2: positive controls -- the key in any ONE source gets past the check"
CFG_YML="" ENV_FILE="" run_gate "env VOYAGE_API_KEY" VOYAGE_API_KEY="$FAKE_KEY"
assert_got_past_the_key_check "VOYAGE_API_KEY env"
CFG_YML="" ENV_FILE="" run_gate "env NX_VOYAGE_API_KEY" NX_VOYAGE_API_KEY="$FAKE_KEY"
assert_got_past_the_key_check "NX_VOYAGE_API_KEY env"
printf 'export VOYAGE_API_KEY="%s"\n' "$FAKE_KEY" > "$WORKDIR/key.env"
CFG_YML="" ENV_FILE="$WORKDIR/key.env" run_gate "env file"
assert_got_past_the_key_check "repo .env file"
CFG_YML=$'credentials:\n  voyage_api_key: '"$FAKE_KEY" ENV_FILE="" run_gate "config.yml"
assert_got_past_the_key_check "config.yml credentials"

# ── Static: no skip path ─────────────────────────────────────────────────────
code_of() { sed -e 's/^[[:space:]]*#.*$//' "$1"; }              # drop whole-line comments
# First matching code line number. Command substitution then parameter expansion, not
# `| head -1`: under pipefail a head that closes early promotes its producer's SIGPIPE.
line_of() { local hits; hits="$(code_of "$1" | grep -nE -- "$2" | cut -d: -f1)"; printf '%s' "${hits%%$'\n'*}"; }

# Detectors, each returns 0 when the script is CLEAN. They take a file so the
# positive controls below can hand them a script that has a skip path. Counting
# (grep -c), never grep -q: a grep that quits on its first match closes the pipe
# early, and under pipefail the producer's SIGPIPE status would turn a "found" into
# a "clean" here.
SKIP_VOCAB='skip|passed_by_default|no_voyage|allow_no_key|allow_missing|optional_key'
EXIT_ZERO='(^|[;&|({[:space:]])exit[[:space:]]+0([[:space:];&|)}]|$)'
detect_exit_zero() { local n; n="$(code_of "$1" | grep -cE -- "$EXIT_ZERO")"; [[ "$n" -eq 0 ]]; }
detect_skip_vocab() { local n; n="$(code_of "$1" | grep -ciE -- "$SKIP_VOCAB")"; [[ "$n" -eq 0 ]]; }

echo "Test 3: the detectors fire on a script that has a skip path (non-vacuity of the static checks)"
printf '%s\n' '[ -z "$K" ] && { echo "no key, skipping"; exit 0; }' > "$WORKDIR/skippy.sh"
detect_exit_zero "$WORKDIR/skippy.sh" && bad "exit-0 detector missed 'exit 0'" || ok "exit-0 detector fires on 'exit 0'"
detect_skip_vocab "$WORKDIR/skippy.sh" && bad "skip-vocabulary detector missed 'skipping'" || ok "skip-vocabulary detector fires on 'skipping'"
printf '%s\n' '# exit 0 and skip are fine in a comment' 'echo hello' > "$WORKDIR/commented.sh"
detect_exit_zero "$WORKDIR/commented.sh" && detect_skip_vocab "$WORKDIR/commented.sh" \
  && ok "detectors ignore comments" || bad "detectors flag a comment"

echo "Test 4: dual-embedder-gate.sh has no skip path"
detect_exit_zero "$GATE" && ok "no 'exit 0' in the gate (success is falling off the end after the tally)" \
  || bad "the gate contains 'exit 0': $(code_of "$GATE" | grep -nE -- "$EXIT_ZERO")"
detect_skip_vocab "$GATE" && ok "no skip vocabulary or bypass switch in the gate's code" \
  || bad "the gate's code has skip vocabulary: $(code_of "$GATE" | grep -niE -- "$SKIP_VOCAB")"

key_line="$(line_of "$GATE" '^GATE_VOYAGE_KEY=')"
[[ -n "$key_line" ]] || fail_loud_and_exit "key-resolution anchor (GATE_VOYAGE_KEY=) matched nothing; this test needs updating, not a silent pass"
first_start_line="$(line_of "$GATE" 'mktemp -d|uv run|build-gate-jar|mvnw|source .*fence_home')"
[[ -n "$first_start_line" ]] || fail_loud_and_exit "engine-bring-up anchor matched nothing; this test needs updating"
[[ "$key_line" -lt "$first_start_line" ]] \
  && ok "the key is resolved (line $key_line) before the first scratch/uv/build line ($first_start_line)" \
  || bad "the key is resolved at line $key_line, AFTER an engine-side line ($first_start_line)"
refuse_line="$(line_of "$GATE" '^if[[:space:]]+\[[[:space:]]+-z[[:space:]]+"\$GATE_VOYAGE_KEY"')"
[[ -n "$refuse_line" && "$refuse_line" -lt "$first_start_line" ]] \
  && ok "the refusal on an empty key sits before any engine-side line" \
  || bad "no refusal on an empty GATE_VOYAGE_KEY found before the engine-side lines"

pass1_line="$(line_of "$GATE" '^dg_run_pass[[:space:]]+1[[:space:]]*$')"
restart_line="$(line_of "$GATE" '^dg_restart_engine[[:space:]]*$')"
pass2_line="$(line_of "$GATE" '^dg_run_pass[[:space:]]+2[[:space:]]*$')"
tally_line="$(line_of "$GATE" '^dg_require_tally[[:space:]]*$')"
banner_line="$(line_of "$GATE" 'DUAL-EMBEDDER GATE PASSED')"
for pair in "pass 1:$pass1_line" "restart:$restart_line" "pass 2:$pass2_line" "tally:$tally_line" "pass banner:$banner_line"; do
  [[ -n "${pair#*:}" ]] || fail_loud_and_exit "anchor for ${pair%%:*} matched nothing; this test needs updating, not a silent pass"
done
if [[ "$pass1_line" -lt "$restart_line" && "$restart_line" -lt "$pass2_line" && "$pass2_line" -lt "$tally_line" && "$tally_line" -lt "$banner_line" ]]; then
  ok "order: pass 1 ($pass1_line) < restart ($restart_line) < pass 2 ($pass2_line) < tally ($tally_line) < banner ($banner_line)"
else
  bad "order is wrong: pass1=$pass1_line restart=$restart_line pass2=$pass2_line tally=$tally_line banner=$banner_line"
fi
[[ "$(code_of "$GATE" | grep -cE 'DUAL-EMBEDDER GATE PASSED')" -eq 1 ]] \
  && ok "the success banner is printed in exactly one place" || bad "the success banner appears more than once"

# The pass body must run all three searches; the tally must demand each ran once per pass.
pass_body="$(sed -n '/^dg_run_pass()[[:space:]]*{/,/^}/p' "$GATE")"
[[ -n "$pass_body" ]] || fail_loud_and_exit "dg_run_pass() body anchor matched nothing; this test needs updating"
for needle in 'dg_search_one "\$DG_BGE_COLL"' 'dg_search_one "\$DG_VOY_COLL"' 'dg_combined_search'; do
  grep -qE -- "$needle" <<<"$pass_body" && ok "dg_run_pass runs: $needle" || bad "dg_run_pass does not run: $needle"
done
tally_body="$(sed -n '/^dg_require_tally()[[:space:]]*{/,/^}/p' "$GATE")"
[[ -n "$tally_body" ]] || fail_loud_and_exit "dg_require_tally() body anchor matched nothing; this test needs updating"
for counter in DG_BGE_SEARCHES DG_VOY_SEARCHES DG_COMBINED_BGE_HITS DG_COMBINED_VOY_HITS; do
  grep -qE -- "$counter" <<<"$tally_body" && ok "the tally demands $counter == the pass count" \
    || bad "the tally does not check $counter"
done
grep -qE 'exit[[:space:]]+1' <<<"$tally_body" && ok "a short tally exits 1" || bad "a short tally does not exit non-zero"
grep -qE '^DG_PASSES=2[[:space:]]*$' "$GATE" && ok "DG_PASSES is pinned at 2 (before and after the restart)" \
  || bad "DG_PASSES=2 not found"

echo
echo "dual_embedder_gate_guard_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
