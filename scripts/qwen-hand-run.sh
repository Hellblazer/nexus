#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/qwen-hand-run.sh [pytest args...]   (nexus-0wp30)
#
# The only supported way to run a hand suite on qwentescence, and only once
# docs/contributing.md first-run item 10 (the overlap check) has been run green
# on the host. It runs
#   uv sync -q && scripts/build-gate-jar.sh && uv run pytest -n 8 -q [args]
# from this checkout under the box lock ($QWEN_SUITE_LEASE_ROOT/box.lock, default
# /var/lib/nx-suite-lease/box.lock) with NX_BUILD_LEASE_ROOT and
# NX_SUITE_LEASE_WAIT=1 set explicitly (the lease variables are passed here,
# never exported from a profile). A raw `flock ... box.lock` or a bare pytest is
# NOT a supported hand-run form: it takes the lock, or the suite lease, with no
# regard for a queued CI job.
#
# WHY A WRAPPER. CI's test-qwen job and a hand run take the SAME flock, and flock
# does not order its waiters. CI gives up after 1800 s, so a queue of hand runs
# starved it for about 24 minutes (2026-10-02). CI therefore has STRICT
# PRIORITY: the job posts a marker, `ci-waiting.<run>.<attempt>`, in the lease
# root while it waits for the lock (ci.yml, "Build the stamped service jar and run
# the full suite under the box lock"), and this script:
#   * refuses to START while a live marker exists (exit 76, retry later);
#   * after it takes the lock, looks again, and if CI arrived while it waited it
#     releases the lock and backs off (a pause with jitter, then tries again);
#   * gives up after QWEN_HAND_RUN_MAX_WAIT_SECONDS (exit 76);
#   * caps its own hold of the lock at QWEN_HAND_RUN_HOLD_SECONDS (exit 77), below
#     CI's 1800 s wait, so a hung or slow hand run cannot starve CI by itself.
# A hand run that already HOLDS the lock when CI arrives is not interrupted; CI
# waits for it, and the hold cap is what keeps that wait inside CI's 1800 s.
#
# LIVENESS IS A KERNEL LOCK, NOT A CLOCK. The CI step holds an advisory flock on
# its own marker for as long as it is queued. This script treats a marker as live
# only while it CANNOT take a shared lock on it. The kernel drops that lock when
# the CI process dies (SIGKILL included, which no trap survives), so a killed job
# stops blocking hand runs at once. An unlocked marker is dead whatever its mtime;
# a dead one older than 120 s is removed (the grace covers the instant between
# CI creating the file and locking it). mtime is a FALLBACK only, used where the
# lock cannot be tested (the file cannot be opened): then a marker younger than
# QWEN_CI_MARKER_STALE_SECONDS (2100 = CI's 1800 s wait plus a margin) counts as
# live, and one whose age cannot be read does not count at all (otherwise it would
# hold hand runs off until the whole wait ran out).
#
# The suite runs with fd 9 (the box lock) CLOSED for it, on purpose: this script
# holds the lock for exactly as long as it runs, and a daemonized Postgres or JVM
# that outlived the run would otherwise keep the lock until it died, starving CI
# for no reason it could see. The cost, a leaked orphan no longer blocks the next
# taker, is covered by CI's own report of processes left behind.
#
# Exit codes:
#   the suite's own, or the jar build's, once the run has started
#   76  CI has priority, or the box stayed busy: nothing was run, retry later
#   77  the run hit its hold cap and was stopped (see QWEN_HAND_RUN_HOLD_SECONDS)
#   69  host prerequisite missing (flock, timeout, or the lease directory)
# 75 is deliberately NOT used: tests/_suite_lease.py exits 75 for a held suite
# lease and the two must stay tellable apart. 69, 76 and 77 print a line saying
# they are not test failures.
#
# Tunables (seconds; the tests shrink them): QWEN_SUITE_LEASE_ROOT,
# QWEN_CI_MARKER_STALE_SECONDS (2100), QWEN_HAND_RUN_SLICE_SECONDS (20, one
# bounded wait on the lock before the marker is looked at again),
# QWEN_HAND_RUN_BACKOFF_SECONDS (30, plus up to half again of jitter),
# QWEN_HAND_RUN_MAX_WAIT_SECONDS (3600), QWEN_HAND_RUN_HOLD_SECONDS (1500, plus
# kill_s below before SIGKILL). The default hold plus the grace must stay under
# CI's wait (QWEN_BOX_LOCK_WAIT_SECONDS, 1800); a test pins it. A cold run, with
# the jar build, may not fit: raise the variable for that run knowing CI then
# waits that much longer.
set -uo pipefail

root="${QWEN_SUITE_LEASE_ROOT:-/var/lib/nx-suite-lease}"
stale_s="${QWEN_CI_MARKER_STALE_SECONDS:-2100}"
slice_s="${QWEN_HAND_RUN_SLICE_SECONDS:-20}"
backoff_s="${QWEN_HAND_RUN_BACKOFF_SECONDS:-30}"
max_wait_s="${QWEN_HAND_RUN_MAX_WAIT_SECONDS:-3600}"
hold_s="${QWEN_HAND_RUN_HOLD_SECONDS:-1500}"
kill_s=20
grace_s=120

say() { printf 'qwen-hand-run: %s\n' "$*" >&2; }

# Leave without running anything, naming the code and that it is not a test result.
refuse() {
  say "$2 (exit $1: nothing was run to completion; this is not a test failure)"
  exit "$1"
}

for pair in "QWEN_CI_MARKER_STALE_SECONDS=$stale_s" "QWEN_HAND_RUN_SLICE_SECONDS=$slice_s" \
            "QWEN_HAND_RUN_BACKOFF_SECONDS=$backoff_s" "QWEN_HAND_RUN_MAX_WAIT_SECONDS=$max_wait_s" \
            "QWEN_HAND_RUN_HOLD_SECONDS=$hold_s"; do
  case "${pair#*=}" in
    '' | *[!0-9]*) refuse 69 "${pair%%=*} must be a whole number of seconds, got '${pair#*=}'" ;;
  esac
done
[ "$hold_s" -gt 0 ] || refuse 69 "QWEN_HAND_RUN_HOLD_SECONDS must be above zero"

# Fail closed: without flock the run would be unserialized, the overlap that
# wedged the VM twice on 2026-10-01. Without timeout the hold cap cannot apply.
if ! command -v flock >/dev/null 2>&1; then
  refuse 69 "flock is not installed on this host: the box lock cannot be taken, so the suite would run unserialized against CI. Host prerequisite (util-linux)"
fi
if ! command -v timeout >/dev/null 2>&1; then
  refuse 69 "timeout is not installed on this host: the hold cap cannot be applied, so a hung run could hold the box lock past CI's wait. Host prerequisite (GNU coreutils)"
fi
if [ ! -d "$root" ]; then
  refuse 69 "the lease directory $root does not exist: the box lock and the CI priority marker live there. Host prerequisite (docs/contributing.md, First run of the qwen-linux route)"
fi
# Absolute from here on: the cd below would otherwise move a relative root.
root="$(cd "$root" 2>/dev/null && pwd)" || refuse 69 "cannot enter the lease directory"
lock="$root/box.lock"
if [ ! -e "$lock" ]; then
  # group-writable, like the directory, so both users of the box can open it
  (umask 002; : >> "$lock") 2>/dev/null
fi
if [ ! -r "$lock" ]; then
  refuse 69 "cannot read the box lock $lock"
fi

here="$(dirname "$0")"
cd "$here/.." || exit 69

# mtime of a file in epoch seconds: GNU stat first (the host), then BSD (a laptop).
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# Sets mstate for one marker file: live (its holder has the lock), dead (nobody
# does), gone (not there, or vanished while we looked), unknown (the lock could not
# be tested: the file cannot be opened). The file is opened read-only by fd, never
# by name through flock, which would CREATE a marker CI had just removed.
mstate=""
marker_state() {
  local f="$1" rc=0
  if [ ! -e "$f" ]; then
    mstate=gone
    return
  fi
  ( exec 7<"$f" && flock -n -s -E 200 7 ) 2>/dev/null || rc=$?
  case "$rc" in
    0) mstate=dead ;;
    200) mstate=live ;;
    *) if [ -e "$f" ]; then mstate=unknown; else mstate=gone; fi ;;
  esac
}

# Sets fresh_name and returns 0 when a live CI marker exists.
fresh_name=""
fresh_marker() {
  local f m now age
  now="$(date +%s)"
  for f in "$root"/ci-waiting.*; do
    [ -e "$f" ] || continue
    marker_state "$f"
    case "$mstate" in
      gone) continue ;;
      dead)
        # Unlocked: the corpse of a killed job, or the instant between CI's create
        # and its lock. Clear it only once it is clearly the former.
        m="$(mtime "$f")"
        case "$m" in
          '' | *[!0-9]*) ;;
          *) [ $((now - m)) -gt "$grace_s" ] && rm -f "$f" ;;
        esac
        continue
        ;;
      live) ;;
      *)
        # Fallback: mtime. A file that vanished while we read it is not a marker;
        # one whose age cannot be read does not count.
        m="$(mtime "$f")"
        case "$m" in
          '' | *[!0-9]*) continue ;;
        esac
        age=$((now - m))
        [ "$age" -lt "$stale_s" ] || continue
        ;;
    esac
    # The name is printed to a terminal and the lease root is writable by ghci.
    fresh_name="${f##*/}"
    fresh_name="${fresh_name//[^A-Za-z0-9._-]/?}"
    return 0
  done
  return 1
}

start="$(date +%s)"

# True when the bounded total wait has run out.
out_of_time() { [ $(($(date +%s) - start)) -ge "$max_wait_s" ]; }

backoff() { sleep $((backoff_s + RANDOM % (backoff_s / 2 + 1))); }

first=1
announced_wait=""
while :; do
  if fresh_marker; then
    if [ -n "$first" ]; then
      refuse 76 "CI has strict priority on this box: $fresh_name says a CI test-qwen run is queued for the box lock. Not starting; retry later, once that run has finished"
    fi
    if out_of_time; then
      refuse 76 "CI ($fresh_name) stayed queued for the whole ${max_wait_s}s wait; retry later"
    fi
    backoff
    continue
  fi
  first=""

  exec 9<"$lock" || refuse 69 "cannot open the box lock $lock"
  flock -w "$slice_s" -E 200 9
  rc=$?
  if [ "$rc" -eq 200 ]; then
    exec 9<&-
    if [ -z "$announced_wait" ]; then
      say "box lock $lock is held (a hand run or a CI job); waiting up to ${max_wait_s}s for it"
      announced_wait=1
    fi
    if out_of_time; then
      refuse 76 "the box lock $lock stayed held for the whole ${max_wait_s}s wait; retry later"
    fi
    continue
  fi
  if [ "$rc" -ne 0 ]; then
    exec 9<&-
    refuse 69 "flock failed on $lock (status $rc)"
  fi

  # We hold the lock. CI may have queued while we waited for it: look again,
  # and if so give the lock back rather than keep it from CI for a whole run.
  if fresh_marker; then
    exec 9<&-
    say "CI queued while this hand run waited ($fresh_name): yielding the box lock and backing off"
    if out_of_time; then
      refuse 76 "the ${max_wait_s}s wait ran out while yielding to CI; retry later"
    fi
    backoff
    continue
  fi
  break
done

say "holding the box lock $lock; running the suite at -n 8 (stopped after ${hold_s}s, the hold cap)"
export NX_BUILD_LEASE_ROOT="$root"
export NX_SUITE_LEASE_WAIT=1
ran_from="$(date +%s)"
# 9>&-: see the header, the suite does not inherit the box lock.
timeout -k "$kill_s" "$hold_s" bash -c 'uv sync -q && scripts/build-gate-jar.sh && uv run pytest -n 8 -q "$@"' qwen-hand-run "$@" 9>&-
rc=$?
# 124 is timeout's own code; 137 is its SIGKILL after the grace. A 137 that came
# sooner than the cap is something else killing the suite (the OOM killer), not us.
if [ "$rc" -eq 124 ] || { [ "$rc" -eq 137 ] && [ $(($(date +%s) - ran_from)) -ge "$hold_s" ]; }; then
  refuse 77 "the run hit its ${hold_s}s hold cap (QWEN_HAND_RUN_HOLD_SECONDS) and was stopped, so that CI is never kept waiting past its own wait; run a narrower selection, or raise the variable knowing CI then waits that much longer"
fi
exit "$rc"
