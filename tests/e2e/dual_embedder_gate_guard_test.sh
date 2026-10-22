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
#      it creates a scratch dir or runs uv, a build or the supervisor. Run for real, with a
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
#   3. The checks themselves fail when a family is missing (nexus-clvut). The
#      gate's check functions are cut out of the script and run against a canned
#      engine (lib/dual_gate_fake_engine.py, port 0) and a canned client: the
#      version check refuses an engine that is not dual or lacks a family, the
#      per-collection search refuses a missing chunk or an empty expectation, the
#      combined search refuses a missing hit from either family and prints the
#      client's stderr, the restart check refuses an unchanged start time, the
#      tally refuses a count other than the pass count, and a 429 is retried with
#      the engine's Retry-After (or a growing wait), then explained.
#
# Run with: bash tests/e2e/dual_embedder_gate_guard_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
GATE="$REPO_ROOT/tests/e2e/dual-embedder-gate.sh"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/dual_embedder_gate_guard_test.XXXXXX")"
ENGINE_PID=""
trap '[[ -n "$ENGINE_PID" ]] && { kill "$ENGINE_PID" 2>/dev/null; wait "$ENGINE_PID" 2>/dev/null; }; rm -rf "$WORKDIR"' EXIT
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
for tool in uv mvnw nx; do
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
  [[ ! -s "$LOG" ]] && ok "$label: no shimmed tool was run (no engine started)" \
    || bad "$label: tools were run before the key check: $(cat "$LOG")"
  [[ -z "$(ls -A "$T")" ]] && ok "$label: no scratch dir was created" \
    || bad "$label: scratch left behind in TMPDIR: $(ls -A "$T")"
}

assert_got_past_the_key_check() {  # <label>
  local label="$1"
  grep -q -- '--service' "$LOG" && ok "$label: reached the engine bring-up (the init call ran)" \
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

# The key stays out of argv, logs and files: in code it appears only in its assignment, the empty-key test
# and the one export inside the engine-start subshell (nexus-8x0tg review).
key_uses="$(code_of "$GATE" | grep -n 'GATE_VOYAGE_KEY' | grep -vE '^[0-9]+:(GATE_VOYAGE_KEY="\$\(dg_resolve_voyage_key\)"|if \[ -z "\$GATE_VOYAGE_KEY" \]; then|[[:space:]]*export NX_VOYAGE_API_KEY="\$GATE_VOYAGE_KEY")[[:space:]]*$' || true)"
[[ -z "$key_uses" ]] \
  && ok "GATE_VOYAGE_KEY appears only in its assignment, the empty-key test and the one export" \
  || bad "GATE_VOYAGE_KEY is used elsewhere: $key_uses"

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

# The pass body must run all three searches; the tally must demand each ran once per pass. Both are
# read through code_of, so a call that is commented out does not count as run.
PASS_NEEDLES=('dg_search_one "\$DG_BGE_COLL"' 'dg_search_one "\$DG_VOY_COLL"' 'dg_combined_search' 'dg_require_restart')
pass_body_of() { code_of "$1" | sed -n '/^dg_run_pass()[[:space:]]*{/,/^}/p'; }
missing_pass_needles() {  # <script>: prints each search the pass body does not run
  local body needle
  body="$(pass_body_of "$1")"
  for needle in "${PASS_NEEDLES[@]}"; do
    [[ "$(grep -cE -- "$needle" <<<"$body")" -gt 0 ]] || printf '%s\n' "$needle"
  done
}
pass_body="$(pass_body_of "$GATE")"
[[ -n "$pass_body" ]] || fail_loud_and_exit "dg_run_pass() body anchor matched nothing; this test needs updating"
[[ -z "$(missing_pass_needles "$GATE")" ]] && ok "dg_run_pass runs the bge search, the Voyage search, the combined search and the restart check" \
  || bad "dg_run_pass does not run: $(missing_pass_needles "$GATE")"
sed 's/^\([[:space:]]*\)dg_combined_search "\$pass"/\1# dg_combined_search "$pass"/' "$GATE" > "$WORKDIR/commented-search.sh"
[[ "$(missing_pass_needles "$WORKDIR/commented-search.sh")" == dg_combined_search ]] \
  && ok "a commented-out combined search reads as not run (positive control)" \
  || bad "the pass-body check counts a commented-out call as run"
tally_body="$(code_of "$GATE" | sed -n '/^dg_require_tally()[[:space:]]*{/,/^}/p')"
[[ -n "$tally_body" ]] || fail_loud_and_exit "dg_require_tally() body anchor matched nothing; this test needs updating"
for counter in DG_BGE_SEARCHES DG_VOY_SEARCHES DG_COMBINED_BGE_HITS DG_COMBINED_VOY_HITS; do
  grep -qE -- "$counter" <<<"$tally_body" && ok "the tally demands $counter == the pass count" \
    || bad "the tally does not check $counter"
done
grep -qE 'exit[[:space:]]+1' <<<"$tally_body" && ok "a short tally exits 1" || bad "a short tally does not exit non-zero"
grep -qE '^DG_PASSES=2[[:space:]]*$' "$GATE" && ok "DG_PASSES is pinned at 2 (before and after the restart)" \
  || bad "DG_PASSES=2 not found"

# ── Behaviour: the checks fail when a family is missing (nexus-clvut) ────────
echo "Test 5: the gate's checks against a canned engine"
FNS="$WORKDIR/gate_checks.sh"
sed -n '/^# ── Counted, fail-loud checks/,/^# dg_set_embed_choice MODEL/p' "$GATE" > "$FNS"
for fn in dg_request dg_check dg_version_check dg_search_one dg_combined_search dg_require_restart dg_require_tally; do
  grep -qE "^$fn\(\)" "$FNS" || fail_loud_and_exit "the extracted check block has no $fn(); its sed anchors need updating"
done
# A lost end anchor would run sed to the end of the file and source the gate's main flow.
[[ "$(grep -cE '^(dg_seed_voyage_collection|dg_run_pass [12]|dg_require_tally)[[:space:]]*$' "$FNS")" -eq 0 ]] \
  || fail_loud_and_exit "the extracted check block reaches the gate's main flow; its end anchor needs updating"
# The model names come from the gate's own collection names, so this file spells neither.
BGE_MODEL="$(sed -n 's/^DG_BGE_COLL="knowledge__gate-dual__\(.*\)__v1"$/\1/p' "$FNS")"
VOY_MODEL="$(sed -n 's/^DG_VOY_COLL="knowledge__gate-dual__\(.*\)__v1"$/\1/p' "$FNS")"
[[ -n "$BGE_MODEL" && -n "$VOY_MODEL" ]] || fail_loud_and_exit "the collection-name anchors matched nothing; this test needs updating"

SCEN="$WORKDIR/scenario"
REQS="$WORKDIR/requests.log"
mkdir -p "$SCEN" "$WORKDIR/fn-shims"
: > "$REQS"
python3 "$HERE/lib/dual_gate_fake_engine.py" "$SCEN" "$WORKDIR/engine.port" "$REQS" &
ENGINE_PID=$!
for _ in $(seq 1 100); do [[ -s "$WORKDIR/engine.port" ]] && break; sleep 0.1; done
[[ -s "$WORKDIR/engine.port" ]] || fail_loud_and_exit "the canned engine never wrote its port"
ENGINE_PORT="$(cat "$WORKDIR/engine.port")"
# The combined search runs the client as `uv run nx search ...`; this uv prints the canned result.
cat > "$WORKDIR/fn-shims/uv" <<'SHIM'
#!/bin/sh
echo "canned client stderr: model group dropped" >&2
cat "$SCEN/combined.json"
exit "$(cat "$SCEN/combined.rc")"
SHIM
# Every retry wait is recorded, not slept, so the waits are pinned and the test stays fast.
cat > "$WORKDIR/fn-shims/sleep" <<'SHIM'
#!/bin/sh
printf '%s ' "$*" >> "$SLEEPS"
SHIM
chmod +x "$WORKDIR/fn-shims/uv" "$WORKDIR/fn-shims/sleep"
SLEEPS="$WORKDIR/sleeps.log"

# fn_run <shell>: source the gate's checks into a fresh bash and run <shell> there; sets OUT and RC.
fn_run() {
  rm -rf "$WORKDIR/fn-scratch"
  mkdir -p "$WORKDIR/fn-scratch"
  : > "$SLEEPS"
  OUT="$(SCRATCH="$WORKDIR/fn-scratch" SCEN="$SCEN" SLEEPS="$SLEEPS" E2E_PYTHON=python3 \
        NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 \
        PATH="$WORKDIR/fn-shims:$PATH" bash -c '
    set -euo pipefail
    source "$1"
    DG_UID=4242
    DG_BASE="http://127.0.0.1:$2"
    DG_AUTH=(-H "Authorization: Bearer canned")
    DG_BUILD_REF=ref-under-test
    eval "$3"' _ "$FNS" "$ENGINE_PORT" "$1" 2>&1)"
  RC=$?
}
expect_pass() { [[ $RC -eq 0 ]] && ok "$1" || bad "$1: rc $RC: $OUT"; }
expect_fail() {  # <label> <text the failure must carry, on a line that is not a passed check>
  if [[ $RC -ne 0 && -n "$(grep -F -- "$2" <<<"$OUT" | grep -vF '] ok (' || true)" ]]; then ok "$1"
  else bad "$1: rc $RC, wanted a failure naming '$2': $OUT"; fi
}
canned() { printf '%s\n' "$2" > "$SCEN/$1.json"; }
version_body() {  # <mode> <models as a JSON list> <build_ref>
  canned GET_version "{\"embedding_mode\": \"$1\", \"embedding_models\": $2, \"build_ref\": \"$3\", \"process_start_time\": \"t1\"}"
}
canned GET_health '{"status": "ok", "db": "up"}'

version_body dual "[\"$BGE_MODEL\", \"$VOY_MODEL\"]" ref-under-test
fn_run 'dg_version_check; echo "passed=$DG_PASSED"'
expect_pass "version check passes a dual engine serving both families"
[[ "$OUT" == *"passed=4"* ]] && ok "each passed check is counted (4 for the version check)" || bad "passed-check counter: $OUT"
version_body onnx-local "[\"$BGE_MODEL\", \"$VOY_MODEL\"]" ref-under-test
fn_run dg_version_check
expect_fail "version check refuses an engine that is not dual" "embedding_mode==dual"
version_body dual "[\"$BGE_MODEL\"]" ref-under-test
fn_run dg_version_check
expect_fail "version check refuses an engine missing the Voyage family" "embedding_models carry"
version_body dual "[\"$VOY_MODEL\"]" ref-under-test
fn_run dg_version_check
expect_fail "version check refuses an engine missing the bge family" "embedding_models carry"
version_body dual "[\"$BGE_MODEL\", \"$VOY_MODEL\"]" ref-of-the-pinned-release
fn_run dg_version_check
expect_fail "version check refuses an engine that is not the jar under test" "build_ref==ref-under-test"
fn_run 'DG_BUILD_REF=""; dg_version_check'
expect_fail "with a native binary, version check refuses a served build_ref (a stray jar)" "build_ref ABSENT"
canned GET_version "{\"embedding_mode\": \"dual\", \"embedding_models\": [\"$BGE_MODEL\", \"$VOY_MODEL\"], \"process_start_time\": \"t1\"}"
fn_run 'DG_BUILD_REF=""; dg_version_check'
expect_pass "with a native binary, version check passes an engine that stamps no build_ref"

canned POST_v1_vectors_search '[{"id": "aa"}, {"id": "bb"}, {"id": "cc"}]'
fn_run 'DG_BGE_CHASHES=" aa bb"; dg_search_one "$DG_BGE_COLL"'
expect_pass "per-collection search passes when every written chunk comes back"
fn_run 'DG_VOY_CHASHES=" aa dd"; dg_search_one "$DG_VOY_COLL"'
expect_fail "per-collection search refuses a missing chunk" "returns every chunk written to it"
searches_before="$(grep -c 'vectors/search' "$REQS")"
fn_run 'DG_VOY_CHASHES=""; dg_search_one "$DG_VOY_COLL"'
expect_fail "per-collection search refuses an empty expectation" "an empty expectation passes against anything"
[[ "$(grep -c 'vectors/search' "$REQS")" -eq "$searches_before" ]] \
  && ok "an empty expectation fails before any search is sent" || bad "an empty expectation still sent a search"

combined() {  # <bge hit pass> <voyage hit pass> <client rc> [collection of the voy hit's model]
  printf '[{"collection": "knowledge__gate-dual__%s__v1", "content": "dual embedder gate probe 4242 bge pass %s"}, {"collection": "knowledge__gate-dual__%s__v1", "content": "dual embedder gate probe 4242 voy pass %s"}]\n' \
    "$BGE_MODEL" "$1" "${4:-$VOY_MODEL}" "$2" > "$SCEN/combined.json"
  printf '%s\n' "$3" > "$SCEN/combined.rc"
}
combined 1 1 0
fn_run 'dg_combined_search 1; echo "hits $DG_COMBINED_BGE_HITS/$DG_COMBINED_VOY_HITS"'
expect_pass "combined search passes with a hit from each family"
[[ "$OUT" == *"hits 1/1"* ]] && ok "combined search counts one hit per family" || bad "combined search hit counters: $OUT"
combined 1 0 0
fn_run 'dg_combined_search 1'
expect_fail "combined search refuses a missing Voyage hit" "a hit from the Voyage collection"
[[ "$OUT" == *"canned client stderr: model group dropped"* ]] && ok "a missing hit prints the client's stderr" \
  || bad "a missing hit does not print the client's stderr: $OUT"
combined 0 1 0
fn_run 'dg_combined_search 1'
expect_fail "combined search refuses a missing bge hit" "a hit from the bge collection"
combined 1 1 0 "$BGE_MODEL"
fn_run 'dg_combined_search 1'
expect_fail "combined search refuses the Voyage text coming back from the bge collection" "a hit from the Voyage collection"

fn_run 'DG_START_TIME_1=t1 DG_THIS_START=t2; dg_require_restart'
expect_pass "the restart check passes when process_start_time moved"
fn_run 'DG_START_TIME_1=t1 DG_THIS_START=t1; dg_require_restart'
expect_fail "the restart check refuses an unchanged process_start_time" "the engine did not restart"
combined 1 1 3
fn_run 'dg_combined_search 1'
expect_fail "combined search refuses a client that exits non-zero" "exited non-zero"

fn_run 'DG_BGE_SEARCHES=2 DG_VOY_SEARCHES=2 DG_COMBINED_BGE_HITS=2 DG_COMBINED_VOY_HITS=2; dg_require_tally'
expect_pass "the tally passes when each search ran once per pass"
fn_run 'DG_BGE_SEARCHES=2 DG_VOY_SEARCHES=1 DG_COMBINED_BGE_HITS=2 DG_COMBINED_VOY_HITS=2; dg_require_tally'
expect_fail "the tally refuses a search that ran short" "TALLY SHORT: DG_VOY_SEARCHES=1"
fn_run 'DG_BGE_SEARCHES=2 DG_VOY_SEARCHES=2 DG_COMBINED_BGE_HITS=2 DG_COMBINED_VOY_HITS=3; dg_require_tally'
expect_fail "the tally refuses a count above the pass count" "TALLY SHORT: DG_COMBINED_VOY_HITS=3"

printf '429\n' > "$SCEN/GET_health.code"
health_before="$(grep -c 'GET /health' "$REQS")"
fn_run 'dg_request GET /health; dg_expect_200 "GET /health"'
expect_fail "a 429 that never clears fails" "429 after 5 tries"
[[ "$OUT" == *"tier 1 needs a payment method"* ]] && ok "the 429 failure explains Voyage's usage tiers" \
  || bad "the 429 failure does not explain the limit: $OUT"
[[ "$(( $(grep -c 'GET /health' "$REQS") - health_before ))" -eq 5 ]] \
  && ok "a 429 is retried to five tries in all" || bad "a 429 was sent $(( $(grep -c 'GET /health' "$REQS") - health_before )) times, not 5"
[[ "$(cat "$SLEEPS")" == "3 6 9 12 " ]] && ok "with no Retry-After the waits grow (3 6 9 12 s)" \
  || bad "waits without Retry-After: '$(cat "$SLEEPS")'"
printf '7\n' > "$SCEN/GET_health.retry_after"
fn_run 'dg_request GET /health; dg_expect_200 "GET /health"'
[[ "$(cat "$SLEEPS")" == "7 7 7 7 " ]] && ok "each wait is the engine's Retry-After" \
  || bad "waits with Retry-After 7: '$(cat "$SLEEPS")'"
printf '600\n' > "$SCEN/GET_health.retry_after"
fn_run 'dg_request GET /health; dg_expect_200 "GET /health"'
[[ "$(cat "$SLEEPS")" == "60 60 60 60 " ]] && ok "a long Retry-After is capped at 60 s" \
  || bad "waits with Retry-After 600: '$(cat "$SLEEPS")'"
rm -f "$SCEN/GET_health.retry_after"
printf '503\n' > "$SCEN/GET_health.code"
health_before="$(grep -c 'GET /health' "$REQS")"
fn_run 'dg_request GET /health; dg_expect_200 "GET /health"'
expect_fail "a 503 that never clears fails" "CHECK FAILED: GET /health (code=503)"
[[ "$(( $(grep -c 'GET /health' "$REQS") - health_before ))" -eq 5 ]] \
  && ok "a 503 is retried to five tries in all" || bad "a 503 was sent $(( $(grep -c 'GET /health' "$REQS") - health_before )) times, not 5"
rm -f "$SCEN/GET_health.code"

echo
echo "dual_embedder_gate_guard_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
