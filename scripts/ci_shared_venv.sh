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
#   key = sha256(uv.lock bytes, python full version, platform tag, dependency groups, recipe)[:32]
# A published entry is immutable (venv/ is chmod a-w) except `last-used`, which every reuse
# touches; labrun's weekly prune evicts on it (it must `chmod -R u+w` before `rm -rf`).
# Builders work in `.tmp.<key>.<host>.<pid>/` and rename it to `<key>/` in one step. BUILD_PATH
# (our addition, inside the entry) records where venv/ was built, for the script rewrite below.
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
# steps that need it (uv sync, docling warm, pytest) and nowhere else.
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
# FALLBACK: NX_LAB_CACHE_ROOT unset, missing or unwritable, no RUNNER_WORKSPACE, no uv, or any
# failure before the outputs: exit 0 with used=false and the fallback venv, so the workflow's
# normal `uv sync` path runs. A failure is announced with ::warning:: and never fails the job.
#
# USAGE: scripts/ci_shared_venv.sh [--group NAME]... [--smoke-import MODULE]   (run at the repo root)
#   --group defaults to `dev`. Test seams: NX_THIN_VENV_PARENT (instead of RUNNER_WORKSPACE),
#   NX_SHARED_VENV_WAIT (seconds to wait for another builder of the same key; default 600).
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
published=0

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

cleanup() {
  if [ "$published" -eq 0 ]; then remove_tree "$tmp"; fi
  [ -z "$thin" ] || remove_tree "$thin"
}
trap cleanup EXIT

# Fall back: leave nothing behind, change no env, let the workflow run its own uv sync.
fallback() {
  echo "::notice::shared venv not used: $1; the job runs its own uv sync"
  [ -z "$thin" ] || { remove_tree "$thin"; thin=""; }
  emit_output false "$(fallback_venv)"
  exit 0
}
warn_fallback() {
  echo "::warning::shared venv failed ($1); the job runs its own uv sync"
  [ -z "$thin" ] || { remove_tree "$thin"; thin=""; }
  emit_output false "$(fallback_venv)"
  exit 0
}

# ---- preconditions -------------------------------------------------------------------------
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

key="$("$py" -I - "$repo/uv.lock" "$pyver" "$platform" "$RECIPE" "${groups[@]}" <<'PY'
import hashlib, sys
lock, pyver, platform, recipe, *groups = sys.argv[1:]
h = hashlib.sha256()
with open(lock, "rb") as f:
    h.update(f.read())
for part in (pyver, platform, recipe, ",".join(sorted(groups))):
    h.update(b"\0" + part.encode())
print(h.hexdigest()[:32])
PY
)" || warn_fallback "cannot compute the key"
[ "${#key}" -eq 32 ] || warn_fallback "bad key '$key'"
entry="$venvs/$key"

entry_complete() {
  [ -f "$entry/COMPLETE" ] && [ "$(cat "$entry/COMPLETE" 2>/dev/null)" = "$key" ]
}

# ---- build (only on a miss) ----------------------------------------------------------------
build_entry() {
  local host
  host="$(hostname -s 2>/dev/null || echo host)"; host="${host//[^A-Za-z0-9-]/_}"
  tmp="$venvs/.tmp.$key.$host.$$"

  # Another job building this very key: wait for its entry instead of repeating a multi-GB write
  # (six shards of a python version start together after a lock change). Bounded, then build anyway.
  local waited=0 limit="${NX_SHARED_VENV_WAIT:-600}" other
  while [ "$waited" -lt "$limit" ]; do
    other="$(find "$venvs" -maxdepth 1 -type d -name ".tmp.$key.*" -mmin -30 ! -name "${tmp##*/}" 2>/dev/null || true)"
    [ -n "$other" ] || break
    entry_complete && return 0
    [ "$waited" -gt 0 ] || echo "another job is building shared venv $key; waiting up to ${limit}s"
    sleep 5; waited=$((waited + 5))
  done
  entry_complete && return 0

  # Stale builders' leftovers (crashed past their trap): this user's, untouched for 12 h.
  find "$venvs" -maxdepth 1 -type d -name '.tmp.*' -user "$(id -u)" -mmin +720 2>/dev/null |
    while read -r stale; do remove_tree "$stale"; done || true

  echo "building shared venv $key in $tmp (groups: ${groups[*]}, python $pyver, $platform)"
  mkdir -- "$tmp"
  local gargs=() g
  for g in "${groups[@]}"; do gargs+=(--group "$g"); done
  # Copy, never hardlink: chmod a-w below would otherwise strip write permission from the uv
  # wheel cache's own inodes.
  UV_PROJECT_ENVIRONMENT="$tmp/venv" UV_LINK_MODE=copy \
    uv sync --frozen --no-install-project --compile-bytecode "${gargs[@]}" --python "$py" \
    || warn_fallback "uv sync failed"
  "$tmp/venv/bin/python" -I -c 'pass' || warn_fallback "the built venv does not start"
  printf '%s\n' "$tmp/venv" > "$tmp/BUILD_PATH"
  chmod -R a-w "$tmp/venv"
  printf '%s\n' "$key" > "$tmp/COMPLETE"          # last: a dir with COMPLETE is a whole entry

  # Atomic publish. os.rename onto a non-empty directory fails, so a lost race cannot nest our
  # tmp inside the winner's entry (BSD `mv` would).
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

if entry_complete; then
  echo "shared venv $key: reusing the published entry"
else
  build_entry
fi
entry_complete || warn_fallback "entry $entry is not complete"
shared="$entry/venv"
"$shared/bin/python" -I -c 'pass' 2>/dev/null \
  || warn_fallback "the published entry's interpreter does not start (was its base python removed?)"
touch -- "$entry/last-used" 2>/dev/null || echo "::notice::could not touch $entry/last-used"

# ---- thin venv -----------------------------------------------------------------------------
thin="$thin_parent/nx-thin-venv"
remove_tree "$thin"
uv venv --no-project --quiet --python "$py" "$thin" || warn_fallback "uv venv failed"

"$thin/bin/python" -I - "$thin" "$shared" "$entry/BUILD_PATH" <<'PY' || warn_fallback "cannot layer the shared site-packages"
import os, shutil, subprocess, sys, sysconfig

thin, shared, build_path_file = sys.argv[1:]
thin_sp = sysconfig.get_paths()["purelib"]
out = subprocess.run(
    [os.path.join(shared, "bin", "python"), "-I", "-c",
     "import sysconfig; p = sysconfig.get_paths(); print(p['purelib']); print(p['platlib'])"],
    check=True, capture_output=True, text=True,
).stdout.split()
shared_sps = list(dict.fromkeys(out))
with open(os.path.join(thin_sp, "nx_shared_venv.pth"), "w") as f:
    f.write("import site; " + "; ".join(f"site.addsitedir({p!r})" for p in shared_sps) + "\n")

# The shared venv was built under a tmp path and renamed, so its console scripts carry a dead
# shebang. Give the thin venv its own copies pointing at the thin interpreter.
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
    with open(src, "rb") as f:
        data = f.read()
    if not data.startswith(b"#!"):
        continue
    dst = os.path.join(dst_bin, name)
    if os.path.exists(dst):
        continue
    with open(dst, "wb") as f:
        f.write(data.replace(build_venv.encode(), thin.encode()))
    os.chmod(dst, 0o755)
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
trap - EXIT          # the thin venv stays; the tmp dir is already published or removed
echo "thin venv $thin layered on shared venv $key"
