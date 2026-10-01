#!/usr/bin/env bash
# Release battery driver (nexus-mfage fix B item 4, nexus-fp7ez item d).
#
#   tests/e2e/release-battery.sh [--artifacts DIR] [--max-parallel N] [--only a,b,c] [--skip-preflight]
#                                [--cut [--candidate-engine PATH] [--accept-candidate-mismatch]]
#                                [--expected-engine-lag <bead>@<engine-version>] [--plan]
#
# CUT MODE (--cut, or NX_CUT_MODE=1; nexus-0kmat): this battery gates an ENGINE
# cut, so the gates that provision their own engine must run the CANDIDATE, not
# the pinned published one (which predates the change and passes vacuously).
# The candidate is --candidate-engine PATH (a *.jar or a native binary; an
# error without --cut), else the stamped dev jar the artifacts leg just built.
# It reaches mvv, smoke, shakedown and dtok (the data-token CLI gate, a leg that
# runs in cut mode, or when named in --only) as NX_CANDIDATE_ENGINE, which each
# leg puts inside its own env scrub. lsg runs the artifacts jar, shakeout and
# candmig the artifacts native binary. Every one of those legs prints an
# `ENGINE IDENTITY` line and, at the end of its journey, an `ENGINE OWNERLESS
# REFUSALS` line, and in cut mode a leg whose log lacks either one naming the
# candidate (by sha256) with zero refusals is FAILED even when its own checks
# went green. pkgup is not an engine leg: it converges to the PUBLISHED engine.
# A candidate whose sha256 is not the artifacts manifest's jar or native binary
# would make lsg/shakeout/candmig gate a different engine from mvv/smoke/
# shakedown/dtok: that is refused unless --accept-candidate-mismatch, which also
# makes the verdict PARTIAL, as does NX_CANDIDATE_EXPECT_OWNERLESS_MODE other
# than enforce (the escape is for a candidate run in log-only mode).
# See tests/e2e/lib/candidate_engine.py.
#
# EXPECTED LAG (non-cut only): develop's client carries the metadata_merge write
# mode, the pinned engine does not echo it, so mvv/smoke/shakedown/dtok go red
# against the pinned engine with EngineOlderThanClientError until the pin moves.
# --expected-engine-lag <bead>@<REQUIRED_ENGINE_VERSION> (or NX_EXPECTED_ENGINE_LAG)
# names that state: a red engine-bearing leg whose FAILING STEP (the failed verdict
# line, the log files it names, the stretch of the leg log that ends at the failure)
# carries the EngineOlderThanClientError signature reads EXPECTED-LAG(<bead>) instead
# of FAILED, the verdict is PARTIAL, and the ack REFUSES to run once the pin has
# moved off the version it names. Any other red stays red, including one that follows
# a tolerated mention of the error in a step that passed.
#
# Leg 0, serial: build every artifact ONCE (tests/e2e/migration-rehearsal/
# build-artifacts.sh — wheel, stamped dev jar, linux native candidate, plus
# the manifest every consuming leg verifies against this tree), then the pin
# sweep (scripts/pins-preflight.sh; its lint bucket runs on the jar leg 0
# just built, so scripts/build-gate-jar.sh is not a separate step).
# Then every gate in ONE parallel group, MAX_PARALLEL wide (default 4), each
# leg in its own log, each verdict line captured verbatim with wall-clock
# seconds; then the throughput-baselined leg (run.sh --shakeout, Phase C,
# nexus-98zsp) ALONE so contention cannot misread its 2x ceiling. Reds never
# stop the battery; one table at the end; exit 1 on any red. A leg that
# exits 0 without its verdict line is MISSING, never passed (nexus-f2g8u:
# a gate that fails with no text is the class a parallel group makes worse).
#
# Concurrency is asserted, not assumed (nexus-mfage AC5): the group's
# start/end stamps must overlap, or the run is red with CONCURRENCY NOT PROVEN.
set -uo pipefail
(( BASH_VERSINFO[0] >= 4 )) || { echo "bash >= 4 required (found $BASH_VERSION)" >&2; exit 2; }
export NO_COLOR=1
unset FORCE_COLOR CLICOLOR_FORCE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
# Relative paths on the command line (and in NX_CANDIDATE_ENGINE) mean relative to
# where the operator ran this, not to the repo root the next line moves into.
INVOKE_DIR="$PWD"
cd "$REPO_ROOT" || exit 2

MAX_PARALLEL="${MAX_PARALLEL:-4}"
ARTIFACTS=""
ONLY=""
SKIP_PREFLIGHT=0
CUT_MODE="${NX_CUT_MODE:-0}"
CANDIDATE_ENGINE="${NX_CANDIDATE_ENGINE:-}"
ACCEPT_CANDIDATE_MISMATCH=0
PLAN_ONLY=0
EXPECTED_ENGINE_LAG="${NX_EXPECTED_ENGINE_LAG:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --artifacts) ARTIFACTS="$2"; shift 2 ;;
    --artifacts=*) ARTIFACTS="${1#--artifacts=}"; shift ;;
    --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
    --max-parallel=*) MAX_PARALLEL="${1#--max-parallel=}"; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --only=*) ONLY="${1#--only=}"; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --cut) CUT_MODE=1; shift ;;
    --candidate-engine) CANDIDATE_ENGINE="$2"; shift 2 ;;
    --candidate-engine=*) CANDIDATE_ENGINE="${1#--candidate-engine=}"; shift ;;
    --accept-candidate-mismatch) ACCEPT_CANDIDATE_MISMATCH=1; shift ;;
    --plan) PLAN_ONLY=1; shift ;;
    --expected-engine-lag) EXPECTED_ENGINE_LAG="$2"; shift 2 ;;
    --expected-engine-lag=*) EXPECTED_ENGINE_LAG="${1#--expected-engine-lag=}"; shift ;;
    -h|--help) sed -n '2,/^set -uo pipefail/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$MAX_PARALLEL" =~ ^[1-9][0-9]*$ ]] || { echo "--max-parallel must be a positive integer" >&2; exit 2; }
[ "$CUT_MODE" = 1 ] || [ "$CUT_MODE" = 0 ] || { echo "NX_CUT_MODE must be 0 or 1 (got '$CUT_MODE')" >&2; exit 2; }
_abs() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$INVOKE_DIR" "$1" ;; esac; }
[ -z "$ARTIFACTS" ] || ARTIFACTS="$(_abs "$ARTIFACTS")"
[ -z "$CANDIDATE_ENGINE" ] || CANDIDATE_ENGINE="$(_abs "$CANDIDATE_ENGINE")"
# nexus-0kmat: legs inherit the exported environment through `bash -c`.
export NX_CUT_MODE="$CUT_MODE"
if [ "$CUT_MODE" != 1 ]; then
  # An engine candidate with no cut mode exports the file and enforces nothing:
  # every leg would run it and none would be required to have served it.
  if [ -n "$CANDIDATE_ENGINE" ] || [ "$ACCEPT_CANDIDATE_MISMATCH" = 1 ]; then
    echo "--candidate-engine / NX_CANDIDATE_ENGINE / --accept-candidate-mismatch need --cut: without cut mode nothing asserts the candidate was served (nexus-0kmat)" >&2
    exit 2
  fi
  unset NX_CANDIDATE_ENGINE
else
  # nexus-0kmat critique S1: lsg is the one leg that sends its engine a deliberate ownerless write, so
  # it carries the positive control every other leg's zero reading rests on. This knob drops it.
  [ -z "${NEXUS_GATE_NO_VECTOR_SMOKE:-}" ] || { echo "NEXUS_GATE_NO_VECTOR_SMOKE is set: it drops local-service-gate.sh's vector leg, which carries the cut battery's only positive control (the deliberate ownerless write). Cut mode refuses it; unset it." >&2; exit 2; }
  [ -z "$EXPECTED_ENGINE_LAG" ] || { echo "--expected-engine-lag is for the PINNED engine; cut mode gates the candidate and a lag ack there would hide the red it exists to find" >&2; exit 2; }
  if [ -n "$CANDIDATE_ENGINE" ]; then
    [ -f "$CANDIDATE_ENGINE" ] || { echo "--candidate-engine: $CANDIDATE_ENGINE is not a file" >&2; exit 2; }
    export NX_CANDIDATE_ENGINE="$CANDIDATE_ENGINE"
  else
    unset NX_CANDIDATE_ENGINE
  fi
fi
# The mode the engine must report (the module reads NX_CANDIDATE_EXPECT_OWNERLESS_MODE;
# default enforce). Anything else is a deliberate non-final run: banner now, PARTIAL at the end.
CUT_NON_ENFORCE=""
if [ "$CUT_MODE" = 1 ] && [ "${NX_CANDIDATE_EXPECT_OWNERLESS_MODE:-enforce}" != enforce ]; then
  CUT_NON_ENFORCE="${NX_CANDIDATE_EXPECT_OWNERLESS_MODE}"
  echo "CUT MODE WARNING: NX_CANDIDATE_EXPECT_OWNERLESS_MODE=$CUT_NON_ENFORCE, so no leg asserts the engine runs the ownerless-write check in enforce mode. This run is PARTIAL, never the final cut." >&2
fi
CUT_ABORT_REASON=""
CUT_MISMATCH_ACCEPTED=0
CUT_JAR=""
CUT_NATIVE=""
LAG_BEAD=""
LAG_COUNT=0

# >>> BEGIN moving-tree guard (nexus-57cvk) -- extracted verbatim by
# tests/test_release_battery_refuses_moving_tree.py; keep both markers.
#
# AGENTS.md § Worktrees: one session, one worktree -- rule 4 says the
# battery runs in the RELEASE WORKTREE, never the primary. That rule was advice, and this repo's own
# history says advice decays -- so this is the enforceable half the bead
# asked for.
#
# WHAT IT REFUSES, and why this exact condition. The hazard is not "you are
# in the primary", it is "the branch this checkout holds can be moved
# underneath a 70-minute run". Rule 9 obliges whoever pushes to develop to
# fast-forward the primary in the same breath, so a battery on `develop`
# with peers on the box is a collision waiting to be discovered on leg 12 --
# which is what happened to 7.57.0, eleven legs green, artifact identity
# mismatch, nothing wrong with the code.
#
# The branch test is rule 2's own test, deliberately, rather than a path
# heuristic: git refuses to check out a branch that is already checked out
# elsewhere, so "this checkout holds develop" IS "this is the primary".
# A path heuristic would false-positive on a renamed directory, and a false
# positive here blocks a release.
#
# The worktree-count half keeps it from firing on a box where nobody can
# move anything: a lone checkout with no peers has no rule-9 pusher, so a
# battery on develop there is merely unusual, not hazardous.
BATTERY_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo DETACHED)"
BATTERY_WORKTREES="$(git worktree list 2>/dev/null | grep -c . || echo 1)"
if [ "$BATTERY_BRANCH" = "develop" ] && [ "$BATTERY_WORKTREES" -gt 1 ] \
   && [ "${NX_BATTERY_ALLOW_DEVELOP:-0}" != "1" ]; then
  cat >&2 <<'REFUSED'
BATTERY REFUSED: this checkout holds `develop` and is not the only worktree
on this box.

Another session that pushes to develop is obliged to fast-forward this
checkout (AGENTS.md § Worktrees: one session, one worktree, rule 9),
which moves the tree the battery
keys its artifacts on. That ends the run on a tree-identity mismatch after
the legs have already been paid for -- measured on 7.57.0, twelfth leg,
eleven legs green.

Run it from the RELEASE WORKTREE instead (rule 4). A release worktree holds
the release branch, which a develop push cannot move at all, and it gates
the tree that actually ships -- version bump included -- rather than an
unbumped ancestor of it.

    git worktree add ../nexus-wt/release-vX.Y.Z -b release/vX.Y.Z develop

For a deliberate non-release sweep on develop, set NX_BATTERY_ALLOW_DEVELOP=1
and accept that a peer's push can red the run.
REFUSED
  exit 2
fi
# <<< END moving-tree guard (nexus-57cvk)

# The pinned engine, parsed before anything is created so a bad --expected-engine-lag
# leaves no work dir behind.
REQUIRED_ENGINE="$(python3 -c '
import re, pathlib
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", pathlib.Path("src/nexus/engine_version.py").read_text())
print(".".join(m.groups()) if m else "")')"
[ -n "$REQUIRED_ENGINE" ] || { echo "could not parse REQUIRED_ENGINE_VERSION" >&2; exit 2; }
# nexus-0kmat: the expected-lag ack names the engine it was written for and dies with it.
if [ -n "$EXPECTED_ENGINE_LAG" ]; then
  if [[ "$EXPECTED_ENGINE_LAG" =~ ^(nexus-[a-z0-9.]+)@([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    LAG_BEAD="${BASH_REMATCH[1]}"; LAG_ENGINE="${BASH_REMATCH[2]}"
  else
    echo "--expected-engine-lag must read <bead>@<engine-version>, e.g. nexus-z0o2p.9@$REQUIRED_ENGINE (got '$EXPECTED_ENGINE_LAG')" >&2; exit 2
  fi
  [ "$LAG_ENGINE" = "$REQUIRED_ENGINE" ] || { echo "--expected-engine-lag $EXPECTED_ENGINE_LAG is for engine $LAG_ENGINE but REQUIRED_ENGINE_VERSION is $REQUIRED_ENGINE: the pin moved, so the lag it acknowledged is over and a red engine leg is a real red. Drop the ack." >&2; exit 2; }
fi

# Short root on purpose: sandbox HOMEs nest .config/nexus/postgres under it
# and a long ${TMPDIR} would push a PG socket path past the platform limit.
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="/tmp/nxb-$STAMP-$$"
LOGS="$WORK/logs"
mkdir -p "$LOGS"
[ -n "$ARTIFACTS" ] || ARTIFACTS="$WORK/artifacts"
BATTERY_T0=$(date +%s)

# Reap our own Postgres clusters on ANY exit, including SIGINT/SIGTERM
# (nexus-6qp25). Every leg's sandbox boots its own cluster under $WORK; when
# this driver is killed mid-flight the legs die but their postmasters are
# re-parented to init and keep running, holding one SysV shared-memory
# segment each against kern.sysv.shmmni. Measured 2026-09-12: a battery
# stopped mid-run left a local-service-gate cluster and a shakedown cluster
# alive, and a third from an earlier run had been orphaned nearly three days
# -- consuming budget that a peer's test run then could not get, where
# exhaustion surfaces as thousands of SETUP errors that read as a code
# regression rather than as contention.
#
# Matches on the postmaster's -D data directory being under OUR $WORK, so it
# can never touch a peer's cluster, a gate this battery did not start, or the
# operator's real service. SIGINT is Postgres's fast-shutdown signal.
_reap_our_clusters() {
  local rc=$?
  local pids
  pids="$(pgrep -f "postgres -D $WORK" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "[battery] reaping $(printf '%s\n' "$pids" | grep -c .) cluster(s) under $WORK" >&2
    # shellcheck disable=SC2086
    kill -INT $pids 2>/dev/null || true
    sleep 2
    pids="$(pgrep -f "postgres -D $WORK" 2>/dev/null || true)"
    # shellcheck disable=SC2086
    [ -n "$pids" ] && kill -KILL $pids 2>/dev/null || true
  fi
  return $rc
}
trap _reap_our_clusters EXIT INT TERM

echo "RELEASE BATTERY: work=$WORK artifacts=$ARTIFACTS max-parallel=$MAX_PARALLEL tree=$(git rev-parse --short HEAD)$(git diff --quiet && git diff --cached --quiet || printf ' (dirty)')"

# ── leg table ────────────────────────────────────────────────────────────────
declare -A LEG_CMD LEG_VERDICT LEG_STATUS LEG_START LEG_END LEG_RC LEG_LINE LEG_PHASE
ORDER=()
define_leg() {  # define_leg <name> <phase> <verdict-regex> <command...>
  local name="$1" phase="$2" verdict="$3"; shift 3
  ORDER+=("$name"); LEG_PHASE[$name]="$phase"; LEG_VERDICT[$name]="$verdict"
  # %q-quoted so a path with a space survives the bash -c replay
  LEG_CMD[$name]="$(printf '%q ' "$@")"; LEG_STATUS[$name]="PENDING"; LEG_START[$name]=""; LEG_END[$name]=""; LEG_RC[$name]=""; LEG_LINE[$name]=""
}

CHANGESET_DELTA=0
if git rev-parse -q --verify "engine-service-v$REQUIRED_ENGINE" >/dev/null 2>&1; then
  git diff --quiet "engine-service-v$REQUIRED_ENGINE" HEAD -- service/src/main/resources/db/changelog || CHANGESET_DELTA=1
else
  echo "  (engine-service-v$REQUIRED_ENGINE is not a local tag; cannot tell whether the tree carries a changeset — running --candidate-migration to be safe)"
  CHANGESET_DELTA=1
fi

define_leg artifacts  serial "ARTIFACTS BUILT"                          tests/e2e/migration-rehearsal/build-artifacts.sh "$ARTIFACTS"
[ "$SKIP_PREFLIGHT" = 1 ] || \
define_leg preflight  serial "PINS PREFLIGHT (PASSED|FAILED)"           scripts/pins-preflight.sh
# Group, longest first so the slots pack: shakedown is max(leg) on this box.
define_leg shakedown  group  "SHAKEDOWN (PASSED|FAILED)"                env "NEXUS_SANDBOX_HOME=$WORK/sb-shakedown" tests/e2e/release-sandbox.sh shakedown
define_leg lsg        group  "LOCAL-SERVICE GATE (PASSED|FAILED)"       env "NX_GATE_ARTIFACTS=$ARTIFACTS" tests/e2e/local-service-gate.sh
define_leg pkgup      group  "PACKAGE-UPGRADE CONVERGENCE MVV (PASSED|FAILED)"   tests/e2e/migration-rehearsal/run.sh --artifacts "$ARTIFACTS" --package-upgrade
if [ "$CHANGESET_DELTA" = 1 ]; then
define_leg candmig    group  "CANDIDATE-MIGRATION REHEARSAL (PASSED|FAILED)"     tests/e2e/migration-rehearsal/run.sh --artifacts "$ARTIFACTS" --candidate-migration
fi
define_leg mvv        group  "FRESH-INSTALL MVV (PASSED|FAILED)"        tests/e2e/fresh-install-mvv.sh
# nexus-0kmat: the data-token CLI gate drives the real CLI through a full
# local-engine journey. It runs in cut mode (its engine must be the candidate) and
# when named in --only; an ordinary client release does not pay its ~10 min.
define_leg dtok       group  "DATA-TOKEN CLI GATE (PASSED|FAILED)"      tests/e2e/data-token-cli-gate.sh
define_leg smoke      group  "SMOKE (PASSED|FAILED)"                    env "NEXUS_SANDBOX_HOME=$WORK/sb-smoke" tests/e2e/release-sandbox.sh smoke
define_leg upshakeout group  "UPGRADE-SHAKEOUT PASSED"                  tests/e2e/upgrade-shakeout.sh run
define_leg genflip    group  "GEN-FLIP LIVE-HOLDER (PASSED|FAILED)"     tests/e2e/gen-flip-live-holder.sh
define_leg pluginls   group  "PLUGIN-LOCKSTEP GATE (PASSED|FAILED|UNVERIFIED)"  tests/e2e/plugin-lockstep-gate.sh
# nexus-rcoze: this checkout's hooks.json fired against every published CLI a
# user may still have (7.55.0 on), this wheel, and none -- no entry may block.
define_leg hookskew   group  "HOOK-CLI SKEW GATE (PASSED|FAILED|UNVERIFIED)"   tests/e2e/hook-cli-skew/run.sh
# RDR-219 Phase 3 Step 2b (nexus-wauo1.24): no credential-shaped file left
# under a harness-owned root. Fails on any find; the token/expiry status
# check it also runs only ever warns, never fails this leg.
define_leg janitor    group  "CREDENTIAL JANITOR (PASSED|FAILED)"        python3 scripts/credential_janitor.py
# nexus-z0o2p.41: the GitHub-backed mandatory_regression_pin tests, moved out of lsg (whose fenced HOME
# has no gh auth) to run here under the operator's real HOME at a zero skip budget. Not an engine leg.
define_leg pins       group  "MANDATORY PINS GATE (PASSED|FAILED)"     tests/e2e/mandatory-pins-gate.sh
define_leg shakeout   alone  "CANDIDATE SHAKEOUT (PASSED|FAILED)"                tests/e2e/migration-rehearsal/run.sh --artifacts "$ARTIFACTS" --shakeout

if [ "$CUT_MODE" != 1 ] && [[ ",$ONLY," != *",dtok,"* ]]; then LEG_STATUS[dtok]="SKIPPED(cut mode only)"; fi
ONLY_SKIPPED=0
if [ -n "$ONLY" ]; then
  # Every name must be a real leg: a typo would otherwise skip the whole
  # gate group and still print PASSED (code-review-expert, T2 [24758]).
  IFS=',' read -r -a _only_names <<< "$ONLY"
  for name in "${_only_names[@]}"; do
    [ -n "${LEG_PHASE[$name]:-}" ] || { echo "--only: unknown leg '$name' (legs: ${ORDER[*]})" >&2; exit 2; }
  done
  keep=",$ONLY,"
  for leg in "${ORDER[@]}"; do
    [[ "$keep" == *",$leg,"* ]] || [ "${LEG_PHASE[$leg]}" = serial ] || [ "${LEG_STATUS[$leg]}" != PENDING ] || { LEG_STATUS[$leg]="SKIPPED(--only)"; ONLY_SKIPPED=$((ONLY_SKIPPED+1)); }
  done
fi

# --plan: print the legs this invocation would run (phase, status) and stop. The
# selection logic above (cut mode, --only, dtok) is real code; this is how a test
# or an operator reads its outcome without paying for a leg.
if [ "$PLAN_ONLY" = 1 ]; then
  for leg in "${ORDER[@]}"; do printf 'PLAN %s %s %s\n' "$leg" "${LEG_PHASE[$leg]}" "${LEG_STATUS[$leg]}"; done
  rm -rf "$WORK"
  exit 0
fi

# ── execution ────────────────────────────────────────────────────────────────
start_leg() {
  local leg="$1"
  LEG_START[$leg]=$(date +%s); LEG_STATUS[$leg]="RUNNING"
  echo "[$(date +%H:%M:%S)] START $leg: ${LEG_CMD[$leg]}"
  ( bash -c "${LEG_CMD[$leg]}" ) > "$LOGS/$leg.log" 2>&1 &
  LEG_PID[$leg]=$!
}
declare -A LEG_PID
# nexus-0kmat: legs that run an engine and must name which one, and read its
# ownerless-write counters and log at the end of the journey. mvv/smoke/shakedown/
# dtok provision their own (the candidate, via NX_CANDIDATE_ENGINE); lsg runs the
# artifacts jar, shakeout and candmig the artifacts native binary. pkgup is NOT
# here: it package-upgrades an old install and converges to the PUBLISHED engine,
# so it never runs the candidate and has no ownerless-write reading to give.
CUT_ENGINE_LEGS=" mvv smoke shakedown dtok lsg shakeout candmig "
# How many DELIBERATE ownerless writes each engine leg may declare (candidate_engine.py
# --min-controls / --max-controls), held by the battery rather than by the count a leg prints about
# itself. Only lsg sends one (its smoke leg's negative control): it must declare at least 1, or nothing
# in the battery has shown the counter and the log can see a refusal and every other leg's 0/0/0 rests
# on nothing. Every other engine leg declares exactly 0: a leg that bumped its own declared count would
# turn a stray-writer red green (round 3 review M1).
cut_leg_controls_args() {  # cut_leg_controls_args <leg>
  case "$1" in
    lsg) printf '%s' "--min-controls 1" ;;
    *)   printf '%s' "--max-controls 0" ;;
  esac
}
# The engine FILE a leg is expected to have served (judged by sha256).
cut_leg_candidate() {  # cut_leg_candidate <leg>
  case "$1" in
    lsg) printf '%s' "${CUT_JAR:-}" ;;
    shakeout|candmig) printf '%s' "${CUT_NATIVE:-}" ;;
    *) printf '%s' "${NX_CANDIDATE_ENGINE:-}" ;;
  esac
}
# In cut mode a green leg that ran against the pinned published engine, never said
# which engine it ran against, or never read the refusal counters, is not a pass.
# Prints the reason; empty = ok. The reader's own exit status decides: a crash
# with empty output is a failure, never a quiet pass.
cut_mode_vacuity() {  # cut_mode_vacuity <leg>
  [ "${CUT_MODE:-0}" = 1 ] || return 0
  [[ "${CUT_ENGINE_LEGS:-}" == *" $1 "* ]] || return 0
  local out rc cand cargs
  cand="$(cut_leg_candidate "$1")"
  cargs="$(cut_leg_controls_args "$1")"
  [ -n "$cand" ] || { printf '%s: cut mode, but no candidate engine file is known for this leg' "$1"; return 0; }
  out="$(python3 "$REPO_ROOT/tests/e2e/lib/candidate_engine.py" cut-assert-log "$LOGS/$1.log" "$1" --candidate "$cand" $cargs 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] || return 0
  [ -n "$out" ] || out="$1: cut-assert-log exited $rc with no output"
  printf '%s' "${out%%$'\n'*}"
}
# Resolve the candidate in cut mode, once the artifacts leg PASSED. The artifacts
# manifest (verified against THIS tree) names the jar and native binary every
# $ARTIFACTS leg uses; the candidate is --candidate-engine, else that jar. A
# candidate that is neither manifest artifact by sha256 would put two engines in
# one green battery. Returns 1 with CUT_ABORT_REASON set when it cannot proceed.
cut_resolve_candidate() {
  local which
  python3 "$REPO_ROOT/tests/e2e/lib/artifact_manifest.py" verify "$ARTIFACTS" "$REPO_ROOT" >/dev/null 2>"$LOGS/candidate-manifest.err" \
    || { CUT_ABORT_REASON="no candidate engine: the artifacts manifest does not verify against this tree ($(tr '\n' ' ' <"$LOGS/candidate-manifest.err" | cut -c1-200))"; return 1; }
  CUT_JAR="$(python3 "$REPO_ROOT/tests/e2e/lib/candidate_engine.py" manifest-artifact "$ARTIFACTS" jar 2>"$LOGS/candidate-manifest.err")" \
    || { CUT_ABORT_REASON="no candidate engine: the artifacts manifest names no jar ($(tr '\n' ' ' <"$LOGS/candidate-manifest.err" | cut -c1-200))"; return 1; }
  CUT_NATIVE="$(python3 "$REPO_ROOT/tests/e2e/lib/candidate_engine.py" manifest-artifact "$ARTIFACTS" native 2>"$LOGS/candidate-manifest.err")" \
    || { CUT_ABORT_REASON="no candidate engine: the artifacts manifest names no native binary ($(tr '\n' ' ' <"$LOGS/candidate-manifest.err" | cut -c1-200))"; return 1; }
  if [ -z "${NX_CANDIDATE_ENGINE:-}" ]; then
    export NX_CANDIDATE_ENGINE="$CUT_JAR"
    return 0
  fi
  which="$(python3 "$REPO_ROOT/tests/e2e/lib/candidate_engine.py" candidate-in-manifest "$ARTIFACTS" "$NX_CANDIDATE_ENGINE" 2>/dev/null)" || which=none
  if [ "$which" = none ]; then
    if [ "$ACCEPT_CANDIDATE_MISMATCH" = 1 ]; then
      CUT_MISMATCH_ACCEPTED=1
      echo "CUT MODE WARNING: the candidate engine $NX_CANDIDATE_ENGINE is NOT the artifacts manifest's engine. mvv/smoke/shakedown/dtok run it; lsg, shakeout and candmig run the artifacts' own. Two engines in one battery: the verdict is PARTIAL." >&2
      return 0
    fi
    CUT_ABORT_REASON="the candidate engine $NX_CANDIDATE_ENGINE is not the engine in the artifacts manifest (sha256 matches neither its jar nor its native binary), so mvv/smoke/shakedown/dtok would gate it while lsg/shakeout/candmig gate another. Build the candidate into the artifacts, or pass --accept-candidate-mismatch to run mixed (PARTIAL)"
    return 1
  fi
  return 0
}
# The pinned engine predates develop's client (nexus-0kmat): the named, acknowledged
# lag. Prints the reason when EVERY failing step of a FAILED engine-bearing leg carries the
# signature; empty = a real red (candidate_engine.py failed-step-lag). A failing step is each
# [FAIL] marker's block (the sandbox legs print one per failed step), or, where the log has no
# marker, the FAILED verdict line's own evidence: the log files that line names
# (data-token-cli-gate fails at "store put ... failed (see <dir>/store-put.log / .stderr.log)"
# and the error text lives only in the stderr file) and the stretch of the leg log that ends
# at the failure and begins after the previous step boundary. Not the last N lines of the log,
# and not the newest file in some evidence directory: a tolerated early mention followed by an
# unrelated red stays red, and so does a leg with one lag step and one unrelated red step.
ENGINE_LAG_LEGS=" mvv smoke shakedown dtok "
ENGINE_LAG_SIGNATURE='EngineOlderThanClientError|The engine is older than this client'
engine_lag_verdict() {  # engine_lag_verdict <leg> <failed-verdict-line>
  [ -n "${LAG_BEAD:-}" ] || return 0
  [[ "${ENGINE_LAG_LEGS}" == *" $1 "* ]] || return 0
  # Every failing step must carry the signature, so one lag step beside an unrelated red stays red
  # (round 3 review M2); the reader's own exit status decides, a crash is no ack.
  python3 "$REPO_ROOT/tests/e2e/lib/candidate_engine.py" failed-step-lag "$LOGS/$1.log" "${2:-}" "$ENGINE_LAG_SIGNATURE" >/dev/null 2>&1 || return 0
  printf 'EXPECTED-LAG(%s): the pinned engine %s predates this client'"'"'s metadata_merge write mode (EngineOlderThanClientError)' "$LAG_BEAD" "${LAG_ENGINE:-?}"
}
# The closing lines, and the battery's exit status. Reads RED, ONLY_SKIPPED,
# CUT_ABORT_REASON, CUT_MISMATCH_ACCEPTED, CUT_NON_ENFORCE, LAG_COUNT.
battery_verdict() {  # battery_verdict <red-count>
  local red="$1" why=() joined
  if [ -n "${CUT_ABORT_REASON:-}" ]; then
    echo "CUT MODE: ${CUT_ABORT_REASON}, so no engine-bearing leg ran (nexus-0kmat): RED"; red=$((red+1))
  fi
  [ "${ONLY_SKIPPED:-0}" -eq 0 ] || why+=("${ONLY_SKIPPED} leg(s) skipped by --only")
  [ "${CUT_MISMATCH_ACCEPTED:-0}" != 1 ] || why+=("candidate engine differs from the artifacts manifest's (--accept-candidate-mismatch)")
  [ -z "${CUT_NON_ENFORCE:-}" ] || why+=("NX_CANDIDATE_EXPECT_OWNERLESS_MODE=${CUT_NON_ENFORCE}: ownerless_write_mode not asserted to be enforce")
  [ "${LAG_COUNT:-0}" -eq 0 ] || why+=("${LAG_COUNT} leg(s) EXPECTED-LAG(${LAG_BEAD:-?}) against the pinned engine")
  if [ "$red" -eq 0 ]; then
    if [ "${#why[@]}" -gt 0 ]; then
      joined="$(printf '%s; ' "${why[@]}")"
      echo "RELEASE BATTERY PASSED (PARTIAL: ${joined%; } — not a release verdict)"
    else
      echo "RELEASE BATTERY PASSED"
    fi
    return 0
  fi
  echo "RELEASE BATTERY FAILED: $red red leg(s)"; return 1
}
finish_leg() {  # finish_leg <leg> <rc>
  local leg="$1" rc="$2" line clean prop vac
  LEG_END[$leg]=$(date +%s); LEG_RC[$leg]="$rc"
  clean="$(sed -e 's/\x1b\[[0-9;]*m//g' "$LOGS/$leg.log")"
  # verbatim verdict line: last match after stripping ANSI colour
  line="$(printf '%s\n' "$clean" | grep -E "${LEG_VERDICT[$leg]}" | tail -1 || true)"
  LEG_LINE[$leg]="$line"
  if [ "$rc" -eq 0 ] && [ -n "$line" ] && [[ "$line" =~ PASSED|BUILT ]]; then LEG_STATUS[$leg]="PASSED"
  elif [ "$rc" -eq 0 ] && [ -n "$line" ]; then LEG_STATUS[$leg]="FAILED"; line="(exit 0 but the verdict line says otherwise) $line"
  elif [ "$rc" -eq 0 ]; then LEG_STATUS[$leg]="MISSING"; line="(exit 0 but no verdict line matching /${LEG_VERDICT[$leg]}/ — not a pass)"
  else LEG_STATUS[$leg]="FAILED"; [ -n "$line" ] || line="(exit $rc, no verdict line; tail: $(tail -3 "$LOGS/$leg.log" | tr '\n' ' ' | cut -c1-200))"
  fi
  # nexus-0kmat: cut-mode non-vacuity. Only a leg that otherwise PASSED can be
  # downgraded here; a leg that is already red stays red with its own reason.
  if [ "${LEG_STATUS[$leg]}" = PASSED ] && [ "${CUT_MODE:-0}" = 1 ]; then
    vac="$(cut_mode_vacuity "$leg")"
    if [ -n "$vac" ]; then LEG_STATUS[$leg]="FAILED"; line="(VACUOUS in cut mode) $vac"; fi
  fi
  # nexus-0kmat: a red against the PINNED engine that is the acknowledged lag is
  # named, counted and PARTIAL; it is never a pass and never an unexplained red.
  if [ "${LEG_STATUS[$leg]}" = FAILED ] && [ "${CUT_MODE:-0}" != 1 ]; then
    vac="$(engine_lag_verdict "$leg" "$line")"
    if [ -n "$vac" ]; then LEG_STATUS[$leg]="EXPECTED-LAG"; line="$vac | $line"; LAG_COUNT=$((LAG_COUNT+1)); fi
  fi
  # nexus-tt5vm review round 2 (Sam's data-point goal): fresh-install-mvv's
  # propagation wait, when it fires, folds PROPAGATION_WAIT_S=<n> into its
  # OWN final verdict line (the same line `line` above already captured
  # via LEG_VERDICT's regex) -- but that is a property of that child
  # script's output shape, not something this battery should depend on
  # staying true forever. Independently grep the FULL log for the literal
  # (this leg's own $LOGS/$leg.log lives under THIS script's $WORK, never
  # deleted by the child's cleanup trap) and fold it in here too, skipping
  # only when `line` already carries it verbatim -- belt and suspenders,
  # not duplication. A no-op for every other leg; the literal never
  # appears in their output.
  prop="$(printf '%s\n' "$clean" | grep -oE 'PROPAGATION_WAIT_S=[0-9]+' | tail -1 || true)"
  if [ -n "$prop" ] && [[ "$line" != *"$prop"* ]]; then
    line="$line  [$prop]"
  fi
  LEG_LINE[$leg]="$line"
  echo "[$(date +%H:%M:%S)] END   $leg: ${LEG_STATUS[$leg]} rc=$rc wall=$(( LEG_END[$leg] - LEG_START[$leg] ))s — $line"
}
run_serial() {
  local leg="$1"
  start_leg "$leg"; wait "${LEG_PID[$leg]}"; finish_leg "$leg" $?
}
run_group() {  # run_group <leg...>: MAX_PARALLEL wide, reds do not stop the rest
  local queue=("$@") running=() leg pid rc i
  while [ ${#queue[@]} -gt 0 ] || [ ${#running[@]} -gt 0 ]; do
    while [ ${#queue[@]} -gt 0 ] && [ ${#running[@]} -lt "$MAX_PARALLEL" ]; do
      leg="${queue[0]}"; queue=("${queue[@]:1}")
      start_leg "$leg"; running+=("$leg")
    done
    sleep 2
    local still=()
    for leg in "${running[@]}"; do
      pid="${LEG_PID[$leg]}"
      if kill -0 "$pid" 2>/dev/null; then still+=("$leg")
      else wait "$pid"; rc=$?; finish_leg "$leg" "$rc"; fi
    done
    running=("${still[@]+"${still[@]}"}")
  done
}

SERIAL_LEGS=(); GROUP_LEGS=(); ALONE_LEGS=()
for leg in "${ORDER[@]}"; do
  [ "${LEG_STATUS[$leg]}" = PENDING ] || continue
  case "${LEG_PHASE[$leg]}" in
    serial) SERIAL_LEGS+=("$leg") ;;
    group)  GROUP_LEGS+=("$leg") ;;
    alone)  ALONE_LEGS+=("$leg") ;;
  esac
done

echo "== leg 0 (serial): ${SERIAL_LEGS[*]}"
LEG0_ABORT=0
for leg in "${SERIAL_LEGS[@]}"; do
  run_serial "$leg"
  # An artifacts red aborts: every group leg consumes them. A preflight red
  # is reported and the battery continues (item e: sandbox reds all report).
  if [ "$leg" = artifacts ] && [ "${LEG_STATUS[$leg]}" != PASSED ]; then LEG0_ABORT=1; break; fi
done

# nexus-0kmat: in cut mode the candidate engine is --candidate-engine, else the
# stamped dev jar the artifacts leg just built, verified against this tree by the
# manifest (cut_resolve_candidate). No candidate, or one that is not the manifest's
# engine, aborts: the legs below would otherwise provision the pinned published
# engine, or gate two engines, and pass.
if [ "$LEG0_ABORT" = 0 ] && [ "$CUT_MODE" = 1 ]; then
  cut_resolve_candidate || { echo "CUT MODE: $CUT_ABORT_REASON" >&2; LEG0_ABORT=1; }
fi
[ "$CUT_MODE" != 1 ] || echo "CUT MODE: candidate engine = ${NX_CANDIDATE_ENGINE:-<none>}"

GROUP_T0=""; GROUP_T1=""
if [ "$LEG0_ABORT" = 0 ]; then
  echo "== gate group (parallel, ${MAX_PARALLEL} wide): ${GROUP_LEGS[*]}"
  GROUP_T0=$(date +%s)
  run_group "${GROUP_LEGS[@]+"${GROUP_LEGS[@]}"}"
  GROUP_T1=$(date +%s)
  echo "== alone: ${ALONE_LEGS[*]}"
  for leg in "${ALONE_LEGS[@]+"${ALONE_LEGS[@]}"}"; do run_serial "$leg"; done
else
  for leg in "${GROUP_LEGS[@]}" "${ALONE_LEGS[@]}"; do
    if [ -n "$CUT_ABORT_REASON" ]; then LEG_STATUS[$leg]="NOT RUN (cut-mode abort)"; else LEG_STATUS[$leg]="NOT RUN (artifacts red)"; fi
  done
fi

# ── report ───────────────────────────────────────────────────────────────────
BATTERY_T1=$(date +%s)
echo
echo "RELEASE BATTERY REPORT (logs: $LOGS)"
printf '%-11s %-22s %8s  %s\n' LEG STATUS WALL_S VERDICT
RED=0; SERIAL_SUM=0; MAX_OVERLAP=0
for leg in "${ORDER[@]}"; do
  wall=""
  if [ -n "${LEG_START[$leg]}" ] && [ -n "${LEG_END[$leg]}" ]; then
    wall=$(( LEG_END[$leg] - LEG_START[$leg] ))
    [ "${LEG_PHASE[$leg]}" = group ] && SERIAL_SUM=$(( SERIAL_SUM + wall ))
  fi
  printf '%-11s %-22s %8s  %s\n' "$leg" "${LEG_STATUS[$leg]}" "$wall" "${LEG_LINE[$leg]}"
  case "${LEG_STATUS[$leg]}" in PASSED|SKIPPED*|"NOT RUN"*|EXPECTED-LAG) ;; *) RED=$((RED+1)) ;; esac
done
[ "$CHANGESET_DELTA" = 1 ] || echo "candmig     NOT RUN (no changeset in service/src/main/resources/db/changelog since engine-service-v$REQUIRED_ENGINE)"
# max overlap of the group's [start,end] intervals: the AC5 proof
if [ -n "$GROUP_T0" ]; then
  MAX_OVERLAP="$(for leg in "${GROUP_LEGS[@]}"; do [ -n "${LEG_START[$leg]}" ] && printf '%s S\n%s E\n' "${LEG_START[$leg]}" "${LEG_END[$leg]}"; done \
    | sort -k1,1n -k2,2 | awk '$2=="S"{c++; if(c>m)m=c} $2=="E"{c--} END{print m+0}')"
  echo
  echo "gate group wall: $(( GROUP_T1 - GROUP_T0 ))s vs serial sum ${SERIAL_SUM}s (${#GROUP_LEGS[@]} legs, max ${MAX_OVERLAP} concurrent)"
  if [ ${#GROUP_LEGS[@]} -ge 2 ] && [ "$MAX_OVERLAP" -lt 2 ]; then
    echo "CONCURRENCY NOT PROVEN: no two group legs overlapped (nexus-mfage AC5)"; RED=$((RED+1))
  fi
fi
echo "battery wall: $(( BATTERY_T1 - BATTERY_T0 ))s"
battery_verdict "$RED"
exit $?
