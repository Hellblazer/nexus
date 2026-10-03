# SPDX-License-Identifier: AGPL-3.0-or-later
# Sourced by run-review.sh, run-teach.sh and run-memory-gate.sh (not run on its own). WT must be set first.
#
# Why it exists (nexus-ger02.7, the exit batch of 2026-10-03): the three runners each copy the scenario document
# into docs/ in the shared worktree and the editor Greps the genre's paths. Run at the same time, each runner's
# copy was a sibling document of the others', the editor found its own sentences in a "sibling" and called the
# filler lines house refrains, and a copy left behind by a killed runner did the same to the next batch.
#
#   runner_lock NAME      take the box-wide runner lock for the whole run, or exit 75 naming the holder. The lock is
#                         a directory with a holder record "<pid> <name>"; a lock whose holder is dead is reclaimed
#                         under a second lock (one reclaimer at a time, deciding again under it), one with no record
#                         is held until it is a minute old (its maker may be between mkdir and the write), and a
#                         holder that answers EPERM is live. Installs the exit, INT, TERM and HUP traps; the exit trap
#                         stops the runner's background jobs and their children before it removes copies and the lock.
#   runner_sweep_stale    remove untracked docs/zz-* left by an older runner (it holds the lock, so none is live)
#   runner_track REL...   paths relative to WT that the exit trap removes
#
# PROSE_EDIT_RUNNER_LOCK overrides the lock path (the tests use it); by default the lock sits in the git common
# directory, so every worktree of the box sees it.
RUNNER_COPIES=()
RUNNER_OWNS_LOCK=""
RUNNER_LOCK_PATH=""

_runner_lock_path() {
  if [ -n "${PROSE_EDIT_RUNNER_LOCK:-}" ]; then
    printf '%s\n' "$PROSE_EDIT_RUNNER_LOCK"
    return 0
  fi
  local common
  common="$(git -C "$WT" rev-parse --path-format=absolute --git-common-dir)" || return 1
  printf '%s\n' "$common/prose-edit-runner.lock"
}

# A pid is live when kill -0 reaches it or is refused for permission: a holder owned by another user answers
# EPERM, and it is as live as any. Only "no such process" means dead. LC_ALL=C keeps the message in English.
_runner_pid_alive() {
  local out
  out="$(LC_ALL=C kill -0 "$1" 2>&1)" && return 0
  case "$out" in
    *"not permitted"*|*"Operation not permitted"*) return 0 ;;
  esac
  return 1
}

# Every descendant pid of $1, one per line.
_runner_descendants() {
  ps -A -o pid= -o ppid= 2>/dev/null | awk -v root="$1" '
    { n++; pid[n] = $1; par[$1] = $2 }
    END {
      seen[root] = 1
      changed = 1
      while (changed) {
        changed = 0
        for (i = 1; i <= n; i++) {
          if (!(pid[i] in seen) && (par[pid[i]] in seen)) { seen[pid[i]] = 1; print pid[i]; changed = 1 }
        }
      }
    }'
}

# Stop the runner's background jobs and everything they started (a job is usually a subshell whose child is a
# headless session), so no session outlives the lock it ran under. TERM first, KILL for what ignores it.
_runner_stop_jobs() {
  local j d p i alive pids=""
  for j in $(jobs -p); do
    pids="$pids $j $(_runner_descendants "$j" | tr '\n' ' ')"
  done
  [ -n "${pids// /}" ] || return 0
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    alive=""
    for p in $pids; do _runner_pid_alive "$p" && alive="$alive $p"; done
    [ -z "$alive" ] && break
    sleep 0.1
  done
  if [ -n "$alive" ]; then
    # shellcheck disable=SC2086
    kill -KILL $alive 2>/dev/null
  fi
  wait 2>/dev/null
  return 0
}

runner_cleanup() {
  local rc=$? p
  trap - EXIT INT TERM HUP
  _runner_stop_jobs
  for p in ${RUNNER_COPIES[@]+"${RUNNER_COPIES[@]}"}; do
    rm -rf "${WT:?}/$p"
  done
  if [ -n "$RUNNER_OWNS_LOCK" ] && [ -n "$RUNNER_LOCK_PATH" ] \
     && [ "$(cut -d' ' -f1 "$RUNNER_LOCK_PATH/holder" 2>/dev/null)" = "$$" ]; then
    rm -rf "$RUNNER_LOCK_PATH"
  fi
  return "$rc"
}

# Reclaim a stale lock under a second lock, "$lock.reclaim", and decide again under it. A bare "read the holder,
# then rm -rf, then mkdir" lets two runners both read the dead pid: the second rm removes the first one's fresh
# lock and both hold it (measured, 37 of 60 simultaneous starts). Under the gate only one runner at a time looks,
# and it looks at the lock as it is NOW. Returns 0 when it removed a stale lock, 1 when the lock is not stale
# (a live holder, a young record-less lock, a fresh lock taken meanwhile), 2 when another runner holds the gate.
_runner_reclaim() {
  local lock="$1" gate="$1.reclaim" pid=""
  if ! mkdir "$gate" 2>/dev/null; then
    # a gate older than a minute belongs to a reclaimer that died
    [ -n "$(find "$gate" -maxdepth 0 -mmin +1 2>/dev/null)" ] && rmdir "$gate" 2>/dev/null
    return 2
  fi
  [ -s "$lock/holder" ] && pid="$(cut -d' ' -f1 "$lock/holder")"
  if [ -n "$pid" ]; then
    if ! _runner_pid_alive "$pid"; then
      rm -rf "$lock"
      rmdir "$gate"
      return 0
    fi
  elif [ -d "$lock" ] && [ -n "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
    rm -rf "$lock"   # a record that never came, and the directory is over a minute old
    rmdir "$gate"
    return 0
  fi
  rmdir "$gate"
  return 1
}

runner_lock() {
  local name="${1:?runner name}" lock pid holder_name tries=0 waits=0 rc
  lock="$(_runner_lock_path)" || { echo "$name: cannot find the git common directory for the runner lock" >&2; exit 1; }
  while ! mkdir "$lock" 2>/dev/null; do
    tries=$((tries + 1))
    pid=""
    holder_name="a runner that wrote no record"
    if [ -s "$lock/holder" ]; then
      pid="$(cut -d' ' -f1 "$lock/holder")"
      holder_name="$(cut -s -d' ' -f2- "$lock/holder")"
    fi
    if { [ -n "$pid" ] && ! _runner_pid_alive "$pid"; } \
       || { [ -z "$pid" ] && [ -n "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; }; then
      _runner_reclaim "$lock"
      rc=$?
      if [ "$rc" -eq 2 ] && [ "$waits" -lt 100 ]; then
        waits=$((waits + 1))
        sleep 0.05   # another runner is reclaiming: look again once it is done
        continue
      fi
      if [ "$rc" -le 1 ] && [ "$tries" -le 20 ]; then
        continue
      fi
    fi
    echo "$name: another prose-edit runner holds the lock ${holder_name:-unnamed} (pid ${pid:-unknown}) in $lock. Run one runner at a time: wait for it, or ask its owner. If no runner is live, remove that directory." >&2
    exit 75
  done
  RUNNER_LOCK_PATH="$lock"
  RUNNER_OWNS_LOCK=1
  printf '%s %s\n' "$$" "$name" > "$lock/holder"
  trap runner_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
}

runner_sweep_stale() {
  local f
  while IFS= read -r -d '' f; do
    echo "runner_guard: removing a leftover copy of an older runner: $f" >&2
    rm -f "${WT:?}/$f"
  done < <(git -C "$WT" ls-files -o --exclude-standard -z -- 'docs/zz-*')
  find "$WT/docs" -depth -type d -name 'zz-*' -empty -delete 2>/dev/null
  return 0
}

runner_track() {
  RUNNER_COPIES+=("$@")
}
