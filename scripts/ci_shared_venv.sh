#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Shared dependencies-only venv per uv.lock, plus a thin per-job venv (nexus-dvk18, CI "F5").
#
# WHY: every pool shard ran `uv sync --group dev` into a venv on the host's disk: ~83 MB of
# writes per shard (71 MB of it ext4 metadata outside the job's cgroup), plus ~35 MB at
# teardown (replay on nexus-aca0q). The dependencies are identical across shards, so build
# them once per key under the labrun cache root and let each job layer a thin venv on top.
#
# CONTRACT v1 with labrun (bead nexus-dvk18 comments; labrun owns root creation and prune):
#   $NX_LAB_CACHE_ROOT/nexus/venvs/<key>/{venv/, COMPLETE, BUILD_PATH, last-used}
#   key = sha256(uv.lock bytes with the root project's own version blanked, python full version,
#         platform tag, dependency groups, recipe)[:32]. A release bump (the root package's
#         `version = ...` line in uv.lock, once a day or so) therefore keeps the key.
# A published entry is immutable (venv/ is chmod a-w) except `last-used`, which every reuse
# touches BEFORE it validates the entry; labrun's weekly prune evicts on it (it must
# `chmod -R u+w` before `rm -rf`, and should rename the entry aside first).
# Builders work in `.tmp.<key>.<host>.<random>/` and rename it to `<key>/` in one step. BUILD_PATH
# (our addition, inside the entry) records where venv/ was built, for the script rewrite below.
# Other names under venvs/ that belong to this script (labrun's prune must leave them alone while
# they are young): `.lock.<key>/` (the build lock, see below), `.lock-dead.*` (a taken-over lock),
# `.gone.<key>.<ts>/` (an entry that failed validation, renamed aside once). Anything of the
# `.tmp.*`, `.gone.*`, `.lock.*` and `.lock-dead.*` families older than 12 h is debris; the next
# cold build removes it.
#
# BUILD LOCK: one builder per key. `mkdir .lock.<key>` plus the exclusive (O_EXCL) creation of its
# `owner` file is the atomic claim (mkdir(1) alone is not: see claim_lock); the holder refreshes
# `heartbeat` every NX_SHARED_VENV_HEARTBEAT seconds (15) from a background loop that exits when the
# script does, SIGKILL included. Waiters judge liveness by the heartbeat age: older than
# NX_SHARED_VENV_DEAD seconds (60) the holder is dead, and a waiter renames the lock aside, removes
# it and the dead builder's tmp dir, and takes the lock itself. After every wait the waiter re-checks
# the entry and re-takes the lock, so a failed builder costs one rebuild, never N.
# The wait is bounded by NX_SHARED_VENV_WAIT (600 s); past it the job falls back to its own sync.
#
# THE SHARED INTERPRETER IS NEVER STARTED. The entry's bin/python is a symlink to the builder's
# base interpreter, which setup-uv installs under a per-job RUNNER_TEMP (UV_PYTHON_INSTALL_DIR is
# always set), so for a later job it may be gone. The thin venv is made with THIS job's interpreter;
# the shared site-packages path is the thin venv's own purelib/platlib path (relative to its prefix)
# applied to the entry's venv/, and the entry is validated through the thin venv's python.
#
# PER JOB: a thin venv at $RUNNER_WORKSPACE/nx-thin-venv (labrun mounts tmpfs at $work/_work;
# RUNNER_TEMP and TMPDIR stay on disk). It has the same interpreter as the shared entry, a .pth
# line `import site; site.addsitedir(<shared site-packages>)` (the shape `uv run --with` uses:
# addsitedir, not a bare path, so .pth files inside the shared tree still run), and the project
# installed editable with --no-deps, so nothing is re-resolved. The dependencies' console scripts
# are copied into the thin venv's bin/ with the build path in their shebang rewritten, so
# `uv run pytest` finds them.
#
# OUTPUTS (GITHUB_OUTPUT; printed to stdout when it is unset). Nothing goes to GITHUB_ENV: a
# job-wide UV_PROJECT_ENVIRONMENT would make every other project's `uv run` in the job (the mcpb
# project, anything a test spawns) sync into this venv, so the workflow sets it in the `env:` of the
# steps that need it (uv sync, pytest) and nowhere else.
#   used=true|false   true: the thin venv is ready; the workflow skips its own `uv sync` and the F3
#                     bytecode-prefix step (the shared entry is precompiled; the project's pyc
#                     lands in tmpfs).
#   venv=<path>       the value for UV_PROJECT_ENVIRONMENT, never empty (uv ignores an empty one on
#                     some versions and errors on others). used=true: the thin venv. Otherwise the
#                     per-job venv location on disk: $LAB_DISK_DIR/venv when the job's workspace is
#                     tmpfs (LAB_WS=tmpfs; a venv synced into tmpfs would copy ~1.3 GB into RAM
#                     because the uv cache is on disk), else uv's default <repo>/.venv.
#   key=<key>         the shared entry's key, when used=true.
# With used=true the steps also set UV_NO_SYNC=1: the shared packages are invisible to uv's
# site-packages scan, so a bare `uv run` would otherwise install every dependency into the thin venv.
#
# F1 GATE: F5 is used only when LAB_WS=tmpfs (labrun F1). Its saving is the thin venv and the
# project's bytecode landing in RAM; on a class without a tmpfs workspace the thin venv would sit
# on NVMe and the workflow's F3 bytecode prefix (skipped whenever F5 is used) would be lost, so the
# job gets used=false and F3 runs.
#
# FALLBACK: LAB_WS is not tmpfs, NX_LAB_CACHE_ROOT unset, missing or unwritable, no
# RUNNER_WORKSPACE, no uv, or ANY failure before the outputs (an EXIT trap writes used=false and
# the fallback venv when nothing was written yet): exit 0, so the workflow's normal `uv sync` path
# runs. A failure is announced with ::warning:: and never fails the job (a signal keeps its status).
#
# USAGE: scripts/ci_shared_venv.sh [--group NAME]... [--smoke-import MODULE]   (run at the repo root)
#   --group defaults to `dev`. Test seams: NX_THIN_VENV_PARENT (instead of RUNNER_WORKSPACE),
#   NX_SHARED_VENV_WAIT (seconds to wait for another builder of the same key; default 600),
#   NX_SHARED_VENV_HEARTBEAT (15), NX_SHARED_VENV_DEAD (60), NX_SHARED_VENV_POLL (5).
set -euo pipefail

# Everything that decides the shared venv's content, besides uv.lock and the interpreter.
# Change it when the build command changes, so old entries are never mistaken for new ones.
RECIPE="v1 sync --frozen --no-install-project --compile-bytecode link-mode=copy"

groups=()
smoke_import=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --group) groups+=("${2:?--group needs a name}"); shift 2 ;;
    --smoke-import) smoke_import="${2:?--smoke-import needs a module}"; shift 2 ;;
    *) echo "ci_shared_venv.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "${#groups[@]}" -gt 0 ] || groups=(dev)


repo="$(pwd -P)"
tmp=""        # this process's build directory, until it is published or removed
thin=""       # this job's thin venv, removed again on any failure before the outputs
keep_thin=0   # set once the outputs say used=true
published=0
emitted=0     # set by emit_output: the EXIT trap writes the fallback outputs while it is still 0
held=0        # this process holds the build lock
hb_pid=""     # the lock's heartbeat loop
lockdir=""
me=""
entry=""
hb_interval="${NX_SHARED_VENV_HEARTBEAT:-15}"
dead_after="${NX_SHARED_VENV_DEAD:-60}"
poll="${NX_SHARED_VENV_POLL:-5}"
wait_limit="${NX_SHARED_VENV_WAIT:-600}"

# Where a job that does NOT use the shared venv syncs its own (see OUTPUTS above).
fallback_venv() {
  if [ "${LAB_WS:-}" = tmpfs ]; then
    if [ -n "${LAB_DISK_DIR:-}" ] && [ -d "$LAB_DISK_DIR" ] && [ -w "$LAB_DISK_DIR" ]; then
      echo "$LAB_DISK_DIR/venv"; return 0
    fi
    echo "::warning::LAB_WS=tmpfs but LAB_DISK_DIR is unusable; the venv is synced into the tmpfs workspace" >&2
  fi
  echo "$repo/.venv"
}

# emit_output used venv [key]
emit_output() {
  emitted=1
  local lines=("used=$1" "venv=$2")
  [ -z "${3:-}" ] || lines+=("key=$3")
  if [ -n "${GITHUB_OUTPUT:-}" ]; then printf '%s\n' "${lines[@]}" >> "$GITHUB_OUTPUT" || true
  else printf '%s\n' "${lines[@]}"; fi
}

# Make a (possibly read-only) tree removable, then remove it. Never aborts the caller.
remove_tree() {
  [ -n "$1" ] && [ -e "$1" ] || return 0
  chmod -R u+w "$1" 2>/dev/null || true
  rm -rf -- "$1" 2>/dev/null || echo "::notice::ci_shared_venv: could not remove $1"
}

release_lock() {
  [ "$held" -eq 1 ] || return 0
  held=0
  if [ -n "$hb_pid" ]; then
    kill "$hb_pid" 2>/dev/null || true
    wait "$hb_pid" 2>/dev/null || true
    hb_pid=""
  fi
  # Only our own lock: after a take-over the path may belong to someone else.
  if [ "$(cat "$lockdir/owner" 2>/dev/null || true)" = "$me" ]; then remove_tree "$lockdir"; fi
}

# The catch-all. Every path out of the script runs this: it drops the lock, the half-built tmp dir
# and (unless the outputs said used=true) the thin venv, and if no output was written yet it writes
# used=false plus the fallback venv, so no failure in the lines above can fail the shard. A job
# cancelled by a signal (status >= 128) keeps its status.
cleanup() {
  local rc=$?
  trap - EXIT
  release_lock
  if [ "$published" -eq 0 ]; then remove_tree "$tmp"; fi
  if [ "$keep_thin" -ne 1 ]; then remove_tree "$thin"; fi
  if [ "$emitted" -eq 0 ]; then
    echo "::warning::shared venv step ended early (status $rc); the job runs its own uv sync"
    emit_output false "$(fallback_venv)"
    if [ "$rc" -lt 128 ]; then exit 0; fi
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT HUP

# Fall back: leave nothing behind (the EXIT trap removes it), change no env, let the workflow run
# its own uv sync.
fallback() {
  echo "::notice::shared venv not used: $1; the job runs its own uv sync"
  emit_output false "$(fallback_venv)"
  exit 0
}
warn_fallback() {
  echo "::warning::shared venv failed ($1); the job runs its own uv sync"
  emit_output false "$(fallback_venv)"
  exit 0
}

# ---- preconditions -------------------------------------------------------------------------
[ "${LAB_WS:-}" = tmpfs ] || fallback "LAB_WS is not tmpfs (F5 needs the tmpfs workspace of labrun F1; F3 bytecode applies instead)"
root="${NX_LAB_CACHE_ROOT:-}"
[ -n "$root" ] || fallback "NX_LAB_CACHE_ROOT is unset"
[ -d "$root" ] || fallback "NX_LAB_CACHE_ROOT=$root is not a directory"
[ -w "$root" ] || fallback "NX_LAB_CACHE_ROOT=$root is not writable"
venvs="$root/nexus/venvs"
mkdir -p -- "$venvs" 2>/dev/null || fallback "cannot create $venvs"
[ -w "$venvs" ] || fallback "$venvs is not writable"
thin_parent="${NX_THIN_VENV_PARENT:-${RUNNER_WORKSPACE:-}}"
[ -n "$thin_parent" ] || fallback "RUNNER_WORKSPACE is unset (no tmpfs workspace for the thin venv)"
[ -d "$thin_parent" ] && [ -w "$thin_parent" ] || fallback "$thin_parent is not a writable directory"
command -v uv >/dev/null 2>&1 || fallback "uv is not on PATH"
[ -f "$repo/uv.lock" ] || fallback "no uv.lock in $repo"

# ---- interpreter and key -------------------------------------------------------------------
# `uv run --no-project` honours UV_PYTHON (setup-uv sets it from python-version) and fetches the
# interpreter when absent; `uv python find` ignores UV_PYTHON.
probe="$(uv run --no-project --no-sync python -I -c '
import platform, sys, sysconfig
print(sys._base_executable)
print(platform.python_version())
print(sysconfig.get_platform())
' 2>/dev/null)" || warn_fallback "cannot resolve the interpreter"
{ read -r py && read -r pyver && read -r platform; } <<<"$probe" || warn_fallback "unreadable interpreter probe"
[ -x "$py" ] || warn_fallback "interpreter $py is not executable"

# The root project's own `version = ...` line is blanked first: the build uses
# --no-install-project, so a release bump does not change the entry, and it must not change the key.
key="$("$py" -I - "$repo/uv.lock" "$pyver" "$platform" "$RECIPE" "${groups[@]}" <<'PY'
import hashlib, re, sys

lock, pyver, platform, recipe, *groups = sys.argv[1:]
with open(lock, encoding="utf-8") as f:
    text = f.read()
root_source = re.compile(r'(?m)^source = \{ (?:editable|virtual) = "\." \}$')
blocks = re.split(r"(?m)^(?=\[\[package\]\]$)", text)
blocks = [
    re.sub(r'(?m)^version = ".*"$', 'version = ""', b, count=1)
    if b.startswith("[[package]]") and root_source.search(b) else b
    for b in blocks
]
h = hashlib.sha256()
h.update("".join(blocks).encode("utf-8"))
for part in (pyver, platform, recipe, ",".join(sorted(groups))):
    h.update(b"\0" + part.encode())
print(h.hexdigest()[:32])
PY
)" || warn_fallback "cannot compute the key"
[ "${#key}" -eq 32 ] || warn_fallback "bad key '$key'"
entry="$venvs/$key"
shared="$entry/venv"
lockdir="$venvs/.lock.$key"
host="$(hostname -s 2>/dev/null || echo host)"; host="${host//[^A-Za-z0-9-]/_}"
me="$host.$$.$RANDOM$RANDOM"

entry_complete() {
  [ -f "$entry/COMPLETE" ] && [ "$(cat "$entry/COMPLETE" 2>/dev/null)" = "$key" ]
}

# ---- build lock ----------------------------------------------------------------------------
# Seconds since the lock was last refreshed (its heartbeat file, or the directory itself in the
# moment between mkdir and the first heartbeat); status 3 when it is gone.
lock_age() {
  "$py" -I -c '
import os, sys, time
newest = None
for p in (os.path.join(sys.argv[1], "heartbeat"), sys.argv[1]):
    try:
        m = os.stat(p).st_mtime
    except OSError:
        continue
    newest = m if newest is None else max(newest, m)
if newest is None:
    sys.exit(3)
print(max(0, int(time.time() - newest)))
' "$1"
}

hold_lock() {
  # claim_lock created `owner` exclusively; it is never rewritten here, because a plain `>` is a
  # path-based open that, during a take-over race, could land in a lock now made by someone else.
  # held is set only once the heartbeat is written: release_lock removes a lock only when its owner
  # names us, so a lock we made and could not write into is removed here, not left behind.
  if ! touch -- "$lockdir/heartbeat" 2>/dev/null; then
    # Only what we wrote, then rmdir, which fails on a lock another job has made its own meanwhile.
    rm -f -- "$lockdir/heartbeat" "$lockdir/owner" 2>/dev/null || true
    rmdir -- "$lockdir" 2>/dev/null || true
    warn_fallback "cannot write into the build lock $lockdir"
  fi
  held=1
  # The heartbeat loop dies with this script (kill -0 on its pid), so a SIGKILLed builder's lock
  # goes stale instead of being refreshed for ever. One-second sleeps: a killed loop leaves at most
  # one behind. It stops only when the lock names another owner: a lock that is momentarily gone
  # is a waiter's take_over_lock moving it aside to measure it, and the waiter puts a live lock
  # back, which must go on being refreshed (nexus-6xlce).
  (
    n=0
    while kill -0 "$$" 2>/dev/null; do
      sleep 1
      n=$((n + 1))
      if [ "$n" -ge "$hb_interval" ]; then
        n=0
        owner="$(cat "$lockdir/owner" 2>/dev/null || true)"
        [ -z "$owner" ] || [ "$owner" = "$me" ] || exit 0
        [ -z "$owner" ] || touch -- "$lockdir/heartbeat" 2>/dev/null || true
      fi
    done
  ) </dev/null >/dev/null 2>&1 &
  hb_pid=$!
}

# The holder stopped refreshing the lock: move it aside (atomic; one waiter wins), remove it and the
# dead builder's tmp dir. The caller loops and races for the fresh lock with everyone else.
take_over_lock() {
  local gone="$venvs/.lock-dead.$key.$me" owner age deadtmp
  owner="$(cat "$lockdir/owner" 2>/dev/null || echo unknown)"
  mv -- "$lockdir" "$gone" 2>/dev/null || return 0      # someone else was first
  age="$(lock_age "$gone" 2>/dev/null || echo 0)"
  if [ "$age" -le "$dead_after" ]; then
    # We caught a lock that was refreshed or re-taken between our look and the move: put it back.
    # If a fresh lock took its place meanwhile, the one we hold is no one's any more: drop it.
    # os.rename, not mv: mv onto a directory created after the check would nest ours inside it.
    if [ -e "$lockdir" ] || ! "$py" -I -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$gone" "$lockdir" 2>/dev/null; then
      remove_tree "$gone"
    fi
    return 0
  fi
  echo "::notice::build lock for $key is dead (no heartbeat for ${age}s, owner $owner); taking it over"
  deadtmp="$(cat "$gone/tmp" 2>/dev/null || true)"
  case "$deadtmp" in "$venvs"/.tmp."$key".*) remove_tree "$deadtmp" ;; esac
  remove_tree "$gone"
}

# claim_lock: 0 when THIS process now holds the lock directory, 1 otherwise.
#
# MKDIR(1) IS NOT A MUTEX EVERYWHERE (nexus-x8sk8, same class as nexus-bo01z). uutils coreutils 0.8.0,
# /usr/bin/mkdir on Ubuntu 26.04 (chas), checks for the path and then creates it, and reports the
# EEXIST of a lost race as success: 4 of 200 racing pairs both exited 0 there (GNU: 0 of 200). So the
# claim is the EXCLUSIVE creation of `owner` inside the directory: noclobber makes bash open it
# O_EXCL, a syscall no userland tool can soften, and mkdir only makes the directory exist. Of any
# number of racers whose mkdir "succeeded", one creates `owner`; the rest see it exist and lose.
# `owner` is written exactly once, here: hold_lock does not rewrite it (a path-based `>` could land
# in a lock a take-over has handed to someone else). A mkdir that made the directory but cannot
# write into it (no `owner` anywhere) is a filesystem error, not contention: the directory is
# dropped and the job falls back, as hold_lock does for its heartbeat.
claim_lock() {
  if ! mkdir -- "$lockdir" 2>/dev/null; then
    [ -d "$lockdir" ] || warn_fallback "cannot create the build lock $lockdir"   # not a held lock: a real fault
    return 1
  fi
  (set -C; printf '%s\n' "$me" > "$lockdir/owner") 2>/dev/null && return 0
  # A mkdir that "succeeded" for a lost race can find the directory already released and gone (the
  # winner built and removed it first): that is a lost claim, not a fault, and the caller re-checks.
  if [ ! -e "$lockdir/owner" ] && [ -d "$lockdir" ]; then
    rmdir -- "$lockdir" 2>/dev/null || true   # fails, correctly, on a lock another job has made its own
    warn_fallback "cannot write into the build lock $lockdir"
  fi
  return 1
}

# 0: we hold the lock. 1: someone else does (or just took it over); the caller waits and re-checks.
acquire_lock() {
  if claim_lock; then hold_lock; return 0; fi
  local age
  age="$(lock_age "$lockdir" 2>/dev/null)" || return 1  # released since the mkdir
  if [ "$age" -gt "$dead_after" ]; then take_over_lock; fi
  return 1
}

# Debris of crashed builders and failed entries, this user's, untouched for 12 h.
sweep_stale() {
  find "$venvs" -maxdepth 1 -type d \
    \( -name '.tmp.*' -o -name '.gone.*' -o -name '.lock.*' -o -name '.lock-dead.*' \) \
    -user "$(id -u)" -mmin +720 2>/dev/null |
    while read -r stale; do remove_tree "$stale"; done || true
}

# ---- build (only on a miss) ----------------------------------------------------------------
do_build() {
  local t g gargs=()
  t="$(mktemp -d "$venvs/.tmp.$key.$host.XXXXXX" 2>/dev/null)" || warn_fallback "cannot create a build directory under $venvs"
  tmp="$t"
  chmod 755 "$tmp" 2>/dev/null || true
  printf '%s\n' "$tmp" > "$lockdir/tmp" 2>/dev/null || true   # a taker-over removes a dead builder's tmp
  echo "building shared venv $key in $tmp (groups: ${groups[*]}, python $pyver, $platform)"
  for g in "${groups[@]}"; do gargs+=(--group "$g"); done
  # Copy, never hardlink: chmod a-w below would otherwise strip write permission from the uv
  # wheel cache's own inodes.
  UV_PROJECT_ENVIRONMENT="$tmp/venv" UV_LINK_MODE=copy \
    uv sync --frozen --no-install-project --compile-bytecode "${gargs[@]}" --python "$py" \
    || warn_fallback "uv sync failed"
  "$tmp/venv/bin/python" -I -c 'pass' || warn_fallback "the built venv does not start"
  printf '%s\n' "$tmp/venv" > "$tmp/BUILD_PATH" || warn_fallback "cannot write $tmp/BUILD_PATH"
  chmod -R a-w "$tmp/venv" || warn_fallback "cannot make $tmp/venv read-only"
  printf '%s\n' "$key" > "$tmp/COMPLETE" || warn_fallback "cannot write $tmp/COMPLETE"   # last: COMPLETE marks a whole entry

  # Atomic publish. os.rename onto a non-empty directory fails, so a lost race (only possible
  # after a take-over of a lock that was merely slow) cannot nest our tmp inside the winner's entry
  # (BSD `mv` would).
  if "$py" -I -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$tmp" "$entry" 2>/dev/null; then
    published=1; tmp=""
    echo "published shared venv $key"
  elif entry_complete; then
    echo "lost the publish race for $key; using the published entry"
    remove_tree "$tmp"; tmp=""
  else
    warn_fallback "cannot publish $entry"
  fi
}

build_entry() {
  local start=$SECONDS announced=0
  while :; do
    entry_complete && return 0
    if acquire_lock; then
      # Published between our check and the lock?
      if entry_complete; then release_lock; return 0; fi
      if [ -e "$entry" ]; then   # a <key> dir without COMPLETE (interrupted prune): debris
        echo "::notice::removing incomplete entry $entry"
        remove_tree "$entry"
      fi
      sweep_stale
      do_build
      release_lock
      return 0
    fi
    if [ $((SECONDS - start)) -ge "$wait_limit" ]; then
      fallback "another job holds the build lock for $key and ${wait_limit}s passed"
    fi
    if [ "$announced" -eq 0 ]; then
      echo "another job is building shared venv $key; waiting up to ${wait_limit}s"
      announced=1
    fi
    sleep "$poll"
  done
}

# ---- thin venv (made first: its layout tells where the shared site-packages are) -------------
thin="$thin_parent/nx-thin-venv"
remove_tree "$thin"
uv venv --no-project --quiet --python "$py" "$thin" || warn_fallback "uv venv failed"
layout="$("$thin/bin/python" -I -c '
import os, sys, sysconfig
p = sysconfig.get_paths()
for k in ("purelib", "platlib"):
    print(os.path.relpath(p[k], sys.prefix))
')" || warn_fallback "cannot read the thin venv layout"
{ read -r purelib_rel && read -r platlib_rel; } <<<"$layout" || warn_fallback "unreadable thin venv layout"

# Entry health that does not depend on the project or on any interpreter: the recorded build path
# and a populated site-packages. (The entry's own bin/python is NOT started; see the header.)
entry_healthy() {
  [ -s "$entry/BUILD_PATH" ] && [ -d "$shared/$purelib_rel" ] && compgen -G "$shared/$purelib_rel/*.dist-info" >/dev/null
}

attempt=1
while :; do
  if entry_complete; then
    echo "shared venv $key: reusing the published entry"
  else
    build_entry
  fi
  entry_complete || warn_fallback "entry $entry is not complete"
  # The entry's identity as validated: the rename below moves it aside only if it is still that
  # directory, never one another job republished in the meantime (nexus-6xlce).
  ident="$("$py" -I -c 'import os, sys; s = os.stat(sys.argv[1]); print(f"{s.st_dev}:{s.st_ino}")' "$entry" 2>/dev/null)" \
    || ident=""
  # Before validating, so a concurrent prune sees a young entry.
  touch -- "$entry/last-used" 2>/dev/null || echo "::notice::could not touch $entry/last-used"
  if entry_healthy; then break; fi
  [ "$attempt" -eq 1 ] || warn_fallback "the rebuilt entry $entry is unhealthy as well"
  attempt=2
  aside="$venvs/.gone.$key.$(date +%s)"
  echo "::warning::shared venv entry $key failed validation; moving it to ${aside##*/} and rebuilding once"
  if "$py" -I -c '
import os, sys
entry, aside, ident = sys.argv[1:]
s = os.stat(entry)
if f"{s.st_dev}:{s.st_ino}" != ident:
    sys.exit(3)
os.rename(entry, aside)
' "$entry" "$aside" "$ident" 2>/dev/null; then
    touch -- "$aside" 2>/dev/null || true     # young, so the 12 h sweep leaves it to its readers
  else
    echo "::notice::did not move $entry aside: it was moved or replaced since it was validated"
  fi
done

"$thin/bin/python" -I - "$thin" "$shared" "$entry/BUILD_PATH" "$purelib_rel" "$platlib_rel" <<'PY' || warn_fallback "cannot layer the shared site-packages"
import os, stat, sys, sysconfig

thin, shared, build_path_file, *rels = sys.argv[1:]
thin_sp = sysconfig.get_paths()["purelib"]
shared_sps = [p for p in dict.fromkeys(os.path.join(shared, r) for r in rels) if os.path.isdir(p)]
assert shared_sps, rels
with open(os.path.join(thin_sp, "nx_shared_venv.pth"), "w") as f:
    f.write("import site; " + "; ".join(f"site.addsitedir({p!r})" for p in shared_sps) + "\n")

# The shared venv was built under a tmp path and renamed, so its console scripts carry a dead
# shebang. Give the thin venv its own copies pointing at the thin interpreter. Executables that are
# not scripts (ruff is a native binary) have nothing to rewrite: link them.
with open(build_path_file) as f:
    build_venv = f.read().strip()
src_bin = os.path.join(shared, "bin")
dst_bin = os.path.join(thin, "bin")
for name in sorted(os.listdir(src_bin)):
    src = os.path.join(src_bin, name)
    if os.path.islink(src) or not os.path.isfile(src):
        continue
    if name.lower().startswith(("activate", "python", "deactivate")):
        continue
    dst = os.path.join(dst_bin, name)
    if os.path.lexists(dst):
        continue
    with open(src, "rb") as f:
        is_script = f.read(2) == b"#!"
        data = f.read() if is_script else b""
    if is_script:
        with open(dst, "wb") as f:
            f.write((b"#!" + data).replace(build_venv.encode(), thin.encode()))
        os.chmod(dst, 0o755)
    elif os.stat(src).st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        os.symlink(src, dst)
PY

# The project, editable, with no dependency resolution: the shared tree already satisfies them.
uv pip install --python "$thin/bin/python" --no-deps --editable "$repo" || warn_fallback "editable install failed"
"$thin/bin/python" -I -c '
import sys
assert any("site-packages" in p for p in sys.path), sys.path
' || warn_fallback "the thin venv does not start"
if [ -n "$smoke_import" ]; then
  "$thin/bin/python" -I -c "import $smoke_import" || warn_fallback "import $smoke_import fails in the thin venv"
fi

# ---- outputs (the point of no return: nothing below can fail the fallback) ----------------
emit_output true "$thin" "$key"
keep_thin=1          # the thin venv stays; the tmp dir is already published or removed
echo "thin venv $thin layered on shared venv $key"
