# SPDX-License-Identifier: AGPL-3.0-or-later
# Sourced by run-review.sh, run-teach.sh and run-memory-gate.sh (not run on its own). WT must be set first.
#
# Why it exists (nexus-ger02.7, the exit batch of 2026-10-03): the three runners each copy the scenario document
# into docs/ in the shared worktree and the editor Greps the genre's paths. Run at the same time, each runner's
# copy was a sibling document of the others', the editor found its own sentences in a "sibling" and called the
# filler lines house refrains, and a copy left behind by a killed runner did the same to the next batch.
#
#   runner_lock NAME      take the box-wide runner lock for the whole run, or exit 75 naming the holder. The lock is
#                         a directory with a holder record "<pid> <name>"; a lock whose holder is dead is reclaimed,
#                         one with no record is held until it is a minute old (its maker may be between mkdir and
#                         the write). Installs the exit, INT, TERM and HUP traps.
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

runner_cleanup() {
  local rc=$? p
  trap - EXIT INT TERM HUP
  for p in ${RUNNER_COPIES[@]+"${RUNNER_COPIES[@]}"}; do
    rm -rf "${WT:?}/$p"
  done
  if [ -n "$RUNNER_OWNS_LOCK" ] && [ -n "$RUNNER_LOCK_PATH" ] \
     && [ "$(cut -d' ' -f1 "$RUNNER_LOCK_PATH/holder" 2>/dev/null)" = "$$" ]; then
    rm -rf "$RUNNER_LOCK_PATH"
  fi
  return "$rc"
}

runner_lock() {
  local name="${1:?runner name}" lock pid holder_name tries=0
  lock="$(_runner_lock_path)" || { echo "$name: cannot find the git common directory for the runner lock" >&2; exit 1; }
  while ! mkdir "$lock" 2>/dev/null; do
    tries=$((tries + 1))
    pid=""
    holder_name="a runner that wrote no record"
    if [ -s "$lock/holder" ]; then
      pid="$(cut -d' ' -f1 "$lock/holder")"
      holder_name="$(cut -s -d' ' -f2- "$lock/holder")"
    fi
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      if [ "$tries" -le 3 ]; then
        rm -rf "$lock"   # its holder is gone; try to take it
        continue
      fi
    elif [ -z "$pid" ] && [ -n "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ] && [ "$tries" -le 3 ]; then
      rm -rf "$lock"     # a record that never came, and the directory is over a minute old
      continue
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
