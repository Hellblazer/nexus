#!/usr/bin/env bash
# Release battery driver (nexus-mfage fix B item 4, nexus-fp7ez item d).
#
#   tests/e2e/release-battery.sh [--max-parallel N] [--only a,b,c] [--skip-preflight] [--plan]
#
# Three legs and a preflight (cleanup step 11, nexus-0r1uz):
#   mvv    tests/e2e/fresh-install-mvv.sh                              the core install invariant
#   pkgup  tests/e2e/migration-rehearsal/run.sh --package-upgrade    package-upgrade convergence
#   lsg    tests/e2e/local-service-gate.sh                            the local-service gate
#
# Serial first: tests/e2e/release-preflight.sh (the ci-evidence required-context
# drift tests, the wire-contract ledger, the mandatory pins and the pin sweep).
# Then the three legs in ONE parallel group, MAX_PARALLEL wide (default 4), each
# leg in its own log, each verdict line captured verbatim with wall-clock
# seconds. Reds never stop the battery; one table at the end; exit 1 on any
# red. A leg that exits 0 without its verdict line is MISSING, never passed
# (nexus-f2g8u: a gate that fails with no text is the class a parallel group
# makes worse).
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
ONLY=""
SKIP_PREFLIGHT=0
PLAN_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
    --max-parallel=*) MAX_PARALLEL="${1#--max-parallel=}"; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --only=*) ONLY="${1#--only=}"; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --plan) PLAN_ONLY=1; shift ;;
    -h|--help) sed -n '2,/^set -uo pipefail/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$MAX_PARALLEL" =~ ^[1-9][0-9]*$ ]] || { echo "--max-parallel must be a positive integer" >&2; exit 2; }
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

# One interpreter, resolved once and exported to every leg (nexus-u67ow). Bare python3
# is /usr/bin/python3 3.9.6 on hellmini, and a leg that uses `match` aborts with a
# SyntaxError on it. Refuses here, by name and version, before anything is created.
# shellcheck source=lib/python.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2

# Short root on purpose: leg HOMEs nest .config/nexus/postgres under it
# and a long ${TMPDIR} would push a PG socket path past the platform limit.
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="/tmp/nxb-$STAMP-$$"
LOGS="$WORK/logs"
mkdir -p "$LOGS"
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

echo "RELEASE BATTERY: work=$WORK max-parallel=$MAX_PARALLEL tree=$(git rev-parse --short HEAD)$(git diff --quiet && git diff --cached --quiet || printf ' (dirty)')"

# ── leg table ────────────────────────────────────────────────────────────────
declare -A LEG_CMD LEG_VERDICT LEG_STATUS LEG_START LEG_END LEG_RC LEG_LINE LEG_PHASE
ORDER=()
define_leg() {  # define_leg <name> <phase> <verdict-regex> <command...>
  local name="$1" phase="$2" verdict="$3"; shift 3
  ORDER+=("$name"); LEG_PHASE[$name]="$phase"; LEG_VERDICT[$name]="$verdict"
  # %q-quoted so a path with a space survives the bash -c replay
  LEG_CMD[$name]="$(printf '%q ' "$@")"; LEG_STATUS[$name]="PENDING"; LEG_START[$name]=""; LEG_END[$name]=""; LEG_RC[$name]=""; LEG_LINE[$name]=""
}

[ "$SKIP_PREFLIGHT" = 1 ] || \
define_leg preflight  serial "PREFLIGHT (PASSED|FAILED)"                 tests/e2e/release-preflight.sh
# Group, longest first so the slots pack.
define_leg lsg        group  "LOCAL-SERVICE GATE (PASSED|FAILED)"       tests/e2e/local-service-gate.sh
define_leg pkgup      group  "PACKAGE-UPGRADE CONVERGENCE MVV (PASSED|FAILED)"   tests/e2e/migration-rehearsal/run.sh --package-upgrade
define_leg mvv        group  "FRESH-INSTALL MVV (PASSED|FAILED)"        tests/e2e/fresh-install-mvv.sh

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
# selection logic above (--only) is real code; this is how a test
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
# The closing lines, and the battery's exit status. Reads RED, ONLY_SKIPPED.
battery_verdict() {  # battery_verdict <red-count>
  local red="$1" why=() joined
  [ "${ONLY_SKIPPED:-0}" -eq 0 ] || why+=("${ONLY_SKIPPED} leg(s) skipped by --only")
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
  local leg="$1" rc="$2" line clean prop
  LEG_END[$leg]=$(date +%s); LEG_RC[$leg]="$rc"
  clean="$(sed -e 's/\x1b\[[0-9;]*m//g' "$LOGS/$leg.log")"
  # verbatim verdict line: last match after stripping ANSI colour
  line="$(printf '%s\n' "$clean" | grep -E "${LEG_VERDICT[$leg]}" | tail -1 || true)"
  LEG_LINE[$leg]="$line"
  if [ "$rc" -eq 0 ] && [ -n "$line" ] && [[ "$line" =~ PASSED ]]; then LEG_STATUS[$leg]="PASSED"
  elif [ "$rc" -eq 0 ] && [ -n "$line" ]; then LEG_STATUS[$leg]="FAILED"; line="(exit 0 but the verdict line says otherwise) $line"
  elif [ "$rc" -eq 0 ]; then LEG_STATUS[$leg]="MISSING"; line="(exit 0 but no verdict line matching /${LEG_VERDICT[$leg]}/ — not a pass)"
  else LEG_STATUS[$leg]="FAILED"; [ -n "$line" ] || line="(exit $rc, no verdict line; tail: $(tail -3 "$LOGS/$leg.log" | tr '\n' ' ' | cut -c1-200))"
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

SERIAL_LEGS=(); GROUP_LEGS=()
for leg in "${ORDER[@]}"; do
  [ "${LEG_STATUS[$leg]}" = PENDING ] || continue
  case "${LEG_PHASE[$leg]}" in
    serial) SERIAL_LEGS+=("$leg") ;;
    group)  GROUP_LEGS+=("$leg") ;;
  esac
done

echo "== preflight (serial): ${SERIAL_LEGS[*]-}"
# A preflight red is reported and the battery continues: every red reports.
for leg in "${SERIAL_LEGS[@]+"${SERIAL_LEGS[@]}"}"; do
  run_serial "$leg"
done

echo "== gate group (parallel, ${MAX_PARALLEL} wide): ${GROUP_LEGS[*]}"
GROUP_T0=$(date +%s)
run_group "${GROUP_LEGS[@]+"${GROUP_LEGS[@]}"}"
GROUP_T1=$(date +%s)

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
