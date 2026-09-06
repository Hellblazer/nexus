#!/usr/bin/env bash
# Release battery driver (nexus-mfage fix B item 4, nexus-fp7ez item d).
#
#   tests/e2e/release-battery.sh [--artifacts DIR] [--max-parallel N] [--only a,b,c] [--skip-preflight]
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
cd "$REPO_ROOT" || exit 2

MAX_PARALLEL="${MAX_PARALLEL:-4}"
ARTIFACTS=""
ONLY=""
SKIP_PREFLIGHT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --artifacts) ARTIFACTS="$2"; shift 2 ;;
    --artifacts=*) ARTIFACTS="${1#--artifacts=}"; shift ;;
    --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
    --max-parallel=*) MAX_PARALLEL="${1#--max-parallel=}"; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --only=*) ONLY="${1#--only=}"; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$MAX_PARALLEL" =~ ^[1-9][0-9]*$ ]] || { echo "--max-parallel must be a positive integer" >&2; exit 2; }

# Short root on purpose: sandbox HOMEs nest .config/nexus/postgres under it
# and a long ${TMPDIR} would push a PG socket path past the platform limit.
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="/tmp/nxb-$STAMP-$$"
LOGS="$WORK/logs"
mkdir -p "$LOGS"
[ -n "$ARTIFACTS" ] || ARTIFACTS="$WORK/artifacts"
BATTERY_T0=$(date +%s)
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

REQUIRED_ENGINE="$(python3 -c '
import re, pathlib
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", pathlib.Path("src/nexus/engine_version.py").read_text())
print(".".join(m.groups()) if m else "")')"
[ -n "$REQUIRED_ENGINE" ] || { echo "could not parse REQUIRED_ENGINE_VERSION" >&2; exit 2; }
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
define_leg smoke      group  "SMOKE (PASSED|FAILED)"                    env "NEXUS_SANDBOX_HOME=$WORK/sb-smoke" tests/e2e/release-sandbox.sh smoke
define_leg upshakeout group  "UPGRADE-SHAKEOUT PASSED"                  tests/e2e/upgrade-shakeout.sh run
define_leg genflip    group  "GEN-FLIP LIVE-HOLDER (PASSED|FAILED)"     tests/e2e/gen-flip-live-holder.sh
define_leg shakeout   alone  "CANDIDATE SHAKEOUT (PASSED|FAILED)"                tests/e2e/migration-rehearsal/run.sh --artifacts "$ARTIFACTS" --shakeout

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
    [[ "$keep" == *",$leg,"* ]] || [ "${LEG_PHASE[$leg]}" = serial ] || { LEG_STATUS[$leg]="SKIPPED(--only)"; ONLY_SKIPPED=$((ONLY_SKIPPED+1)); }
  done
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
finish_leg() {  # finish_leg <leg> <rc>
  local leg="$1" rc="$2" line
  LEG_END[$leg]=$(date +%s); LEG_RC[$leg]="$rc"
  # verbatim verdict line: last match after stripping ANSI colour
  line="$(sed -e 's/\x1b\[[0-9;]*m//g' "$LOGS/$leg.log" | grep -E "${LEG_VERDICT[$leg]}" | tail -1 || true)"
  LEG_LINE[$leg]="$line"
  if [ "$rc" -eq 0 ] && [ -n "$line" ] && [[ "$line" =~ PASSED|BUILT ]]; then LEG_STATUS[$leg]="PASSED"
  elif [ "$rc" -eq 0 ] && [ -n "$line" ]; then LEG_STATUS[$leg]="FAILED"; line="(exit 0 but the verdict line says otherwise) $line"
  elif [ "$rc" -eq 0 ]; then LEG_STATUS[$leg]="MISSING"; line="(exit 0 but no verdict line matching /${LEG_VERDICT[$leg]}/ — not a pass)"
  else LEG_STATUS[$leg]="FAILED"; [ -n "$line" ] || line="(exit $rc, no verdict line; tail: $(tail -3 "$LOGS/$leg.log" | tr '\n' ' ' | cut -c1-200))"
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

GROUP_T0=""; GROUP_T1=""
if [ "$LEG0_ABORT" = 0 ]; then
  echo "== gate group (parallel, ${MAX_PARALLEL} wide): ${GROUP_LEGS[*]}"
  GROUP_T0=$(date +%s)
  run_group "${GROUP_LEGS[@]+"${GROUP_LEGS[@]}"}"
  GROUP_T1=$(date +%s)
  echo "== alone: ${ALONE_LEGS[*]}"
  for leg in "${ALONE_LEGS[@]+"${ALONE_LEGS[@]}"}"; do run_serial "$leg"; done
else
  for leg in "${GROUP_LEGS[@]}" "${ALONE_LEGS[@]}"; do LEG_STATUS[$leg]="NOT RUN (artifacts red)"; done
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
  case "${LEG_STATUS[$leg]}" in PASSED|SKIPPED*|"NOT RUN"*) ;; *) RED=$((RED+1)) ;; esac
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
if [ "$RED" -eq 0 ]; then
  if [ "$ONLY_SKIPPED" -gt 0 ]; then echo "RELEASE BATTERY PASSED (PARTIAL: $ONLY_SKIPPED leg(s) skipped by --only — not a release verdict)"; else echo "RELEASE BATTERY PASSED"; fi
  exit 0
fi
echo "RELEASE BATTERY FAILED: $RED red leg(s)"; exit 1
