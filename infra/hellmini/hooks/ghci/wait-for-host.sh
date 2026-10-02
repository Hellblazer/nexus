#!/bin/bash
# Overlap guard for hellmini-ci (ghci), called by job-started.sh BEFORE ghci's colima VM starts.
# RAM (24 GB) is hellmini's binding limit: a full host `pytest -n auto` suite uses ~23 GB, so a CI
# job's 8 GiB VM on top of it thrashes. Wait while the box is busy, BOUNDED: after the cap the job
# proceeds anyway, so a stale lease or a hung job can delay CI but never block it.
# Busy = a ghrunner job (Runner.Worker) is running, OR a live holder owns the nexus suite or
# service (Maven/engine) lease in the hellmini clone's git common dir.
# Deliberately not a GitHub concurrency group: a newer pending job replaces an older one there,
# which could drop a release leg. (Sam 2026-09-30, from nexus_654's Service CI critique.)
set -uo pipefail
LEASE_ROOT="${GUARD_LEASE_ROOT:-/Volumes/Bulk/src/nexus/.git/nexus-build-lease}"
MAX_WAIT="${GUARD_MAX_WAIT_SECS:-1200}"
POLL="${GUARD_POLL_SECS:-15}"
OTHER_RUNNER_USER="${GUARD_OTHER_USER:-ghrunner}"

busy_reason() {
  local r=() res pid
  if pgrep -u "$OTHER_RUNNER_USER" -f Runner.Worker >/dev/null 2>&1; then
    r+=("$OTHER_RUNNER_USER job running")
  fi
  for res in suite service; do
    pid=$( { tr -dc '0-9' < "$LEASE_ROOT/$res/pid"; } 2>/dev/null ) || pid=""
    # ps, not kill -0: kill -0 on another user's pid fails with EPERM even when it is alive.
    if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
      r+=("$res lease held by pid $pid")
    fi
  done
  (( ${#r[@]} )) && { local IFS=';'; echo "${r[*]}"; }
  return 0
}

start=$SECONDS
reason=$(busy_reason)
if [[ -z "$reason" ]]; then
  echo "overlap guard: host idle, proceeding"
  exit 0
fi
echo "overlap guard: host busy ($reason); waiting up to $((MAX_WAIT / 60)) min"
while [[ -n "$reason" ]]; do
  if (( SECONDS - start >= MAX_WAIT )); then
    echo "overlap guard: still busy after $((SECONDS - start))s ($reason); proceeding anyway"
    exit 0
  fi
  sleep "$POLL"
  reason=$(busy_reason)
done
echo "overlap guard: host free after $((SECONDS - start))s, proceeding"
exit 0
