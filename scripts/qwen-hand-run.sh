#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/qwen-hand-run.sh [pytest args...]   (nexus-0wp30)
#
# The only documented way to run a hand suite on qwentescence. It runs
#   uv sync -q && scripts/build-gate-jar.sh && uv run pytest -n 8 -q [args]
# from this checkout under the box lock ($QWEN_SUITE_LEASE_ROOT/box.lock, default
# /var/lib/nx-suite-lease/box.lock) with NX_BUILD_LEASE_ROOT and
# NX_SUITE_LEASE_WAIT=1 set explicitly (the lease variables are passed here,
# never exported from a profile).
#
# WHY A WRAPPER. CI's test-qwen job and a hand run take the SAME flock, and flock
# does not order its waiters. CI gives up after 1800 s and a hand run takes about
# 11 minutes, so a queue of hand runs starved CI for about 24 minutes
# (2026-10-02). CI therefore has STRICT PRIORITY: the job posts a marker,
# `ci-waiting.<run>.<attempt>`, in the lease root while it waits for the lock
# (ci.yml, "Build the stamped service jar and run the full suite under the box
# lock"), and this script:
#   * refuses to START while a fresh marker exists (exit 76, retry later);
#   * after it takes the lock, looks again, and if CI arrived while it waited it
#     releases the lock and backs off (a pause with jitter, then tries again);
#   * gives up after QWEN_HAND_RUN_MAX_WAIT_SECONDS (exit 76).
# A marker older than QWEN_CI_MARKER_STALE_SECONDS (2100 = CI's 1800 s wait plus a
# margin) is the leftover of a job that was killed, and is ignored.
# A hand run that already HOLDS the lock when CI arrives is not interrupted; CI
# waits for it, which is inside its 1800 s bound.
#
# Exit codes:
#   the suite's own, or the jar build's, once the run has started
#   76  CI has priority, or the box stayed busy: nothing was run, retry later
#   69  host prerequisite missing (flock, or the lease directory): nothing was run
# 75 is deliberately NOT used: tests/_suite_lease.py exits 75 for a held suite
# lease and the two must stay tellable apart.
#
# Tunables (seconds; the tests shrink them): QWEN_SUITE_LEASE_ROOT,
# QWEN_CI_MARKER_STALE_SECONDS (2100), QWEN_HAND_RUN_SLICE_SECONDS (20, one
# bounded wait on the lock before the marker is looked at again),
# QWEN_HAND_RUN_BACKOFF_SECONDS (30, plus up to half again of jitter),
# QWEN_HAND_RUN_MAX_WAIT_SECONDS (3600).
set -uo pipefail

root="${QWEN_SUITE_LEASE_ROOT:-/var/lib/nx-suite-lease}"
lock="$root/box.lock"
stale_s="${QWEN_CI_MARKER_STALE_SECONDS:-2100}"
slice_s="${QWEN_HAND_RUN_SLICE_SECONDS:-20}"
backoff_s="${QWEN_HAND_RUN_BACKOFF_SECONDS:-30}"
max_wait_s="${QWEN_HAND_RUN_MAX_WAIT_SECONDS:-3600}"

say() { printf 'qwen-hand-run: %s\n' "$*" >&2; }

for pair in "QWEN_CI_MARKER_STALE_SECONDS=$stale_s" "QWEN_HAND_RUN_SLICE_SECONDS=$slice_s" \
            "QWEN_HAND_RUN_BACKOFF_SECONDS=$backoff_s" "QWEN_HAND_RUN_MAX_WAIT_SECONDS=$max_wait_s"; do
  case "${pair#*=}" in
    '' | *[!0-9]*) say "${pair%%=*} must be a whole number of seconds, got '${pair#*=}'. Nothing was run."; exit 69 ;;
  esac
done

# Fail closed: without flock the run would be unserialized, the overlap that
# wedged the VM twice on 2026-10-01.
if ! command -v flock >/dev/null 2>&1; then
  say "flock is not installed on this host: the box lock cannot be taken, so the suite would run unserialized against CI. Host prerequisite (util-linux). Nothing was run."
  exit 69
fi
if [ ! -d "$root" ]; then
  say "the lease directory $root does not exist: the box lock and the CI priority marker live there. Host prerequisite (docs/contributing.md, First run of the qwen-linux route). Nothing was run."
  exit 69
fi
if [ ! -e "$lock" ]; then
  # group-writable, like the directory, so both users of the box can open it
  (umask 002; : >> "$lock") 2>/dev/null
fi
if [ ! -r "$lock" ]; then
  say "cannot read the box lock $lock. Nothing was run."
  exit 69
fi

here="$(dirname "$0")"
cd "$here/.." || exit 69

# mtime of a file in epoch seconds: GNU stat first (the host), then BSD (a laptop).
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# Sets fresh_name and fresh_age and returns 0 when a non-stale CI marker exists.
# A marker whose age cannot be read counts as fresh: that errs toward CI.
fresh_name=""
fresh_age=0
fresh_marker() {
  local f m now age
  now="$(date +%s)"
  for f in "$root"/ci-waiting.*; do
    [ -e "$f" ] || continue
    m="$(mtime "$f")"
    case "$m" in
      '' | *[!0-9]*) age=0 ;;
      *) age=$((now - m)) ;;
    esac
    if [ "$age" -lt "$stale_s" ]; then
      fresh_name="${f##*/}"
      fresh_age="$age"
      return 0
    fi
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
      say "CI has strict priority on this box: $fresh_name (${fresh_age}s old) says a CI test-qwen run is queued for the box lock. Not starting; retry later, once that run has finished."
      exit 76
    fi
    if out_of_time; then
      say "CI ($fresh_name) stayed queued for the whole ${max_wait_s}s wait. Nothing was run; retry later."
      exit 76
    fi
    backoff
    continue
  fi
  first=""

  exec 9<"$lock" || { say "cannot open the box lock $lock. Nothing was run."; exit 69; }
  flock -w "$slice_s" -E 200 9
  rc=$?
  if [ "$rc" -eq 200 ]; then
    exec 9<&-
    if [ -z "$announced_wait" ]; then
      say "box lock $lock is held (a hand run or a CI job); waiting up to ${max_wait_s}s for it"
      announced_wait=1
    fi
    if out_of_time; then
      say "the box lock $lock stayed held for the whole ${max_wait_s}s wait. Nothing was run; retry later."
      exit 76
    fi
    continue
  fi
  if [ "$rc" -ne 0 ]; then
    exec 9<&-
    say "flock failed on $lock (status $rc). Nothing was run."
    exit 69
  fi

  # We hold the lock. CI may have queued while we waited for it: look again,
  # and if so give the lock back rather than keep it from CI for a whole run.
  if fresh_marker; then
    exec 9<&-
    say "CI queued while this hand run waited ($fresh_name): yielding the box lock and backing off"
    if out_of_time; then
      say "the ${max_wait_s}s wait ran out while yielding to CI. Nothing was run; retry later."
      exit 76
    fi
    backoff
    continue
  fi
  break
done

say "holding the box lock $lock; running the suite at -n 8"
export NX_BUILD_LEASE_ROOT="$root"
export NX_SUITE_LEASE_WAIT=1
uv sync -q && scripts/build-gate-jar.sh && uv run pytest -n 8 -q "$@"
exit $?
