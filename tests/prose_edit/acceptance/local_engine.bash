# SPDX-License-Identifier: AGPL-3.0-or-later
# Sourced by run-review.sh, run-teach.sh and run-memory-gate.sh after runner_guard.bash and runner_lock (not run on
# its own). WT, OUT and TMPDIR must be set first. nexus-scmcs.3.
#
# Why it exists: the runners WRITE to T2 (zzprose-prefixed projects). On the operator's machine `nx` is whatever is
# configured, usually the real, cloud-mode install, so a test run wrote to the operator's real store. Decision of
# 2026-11-11: a test run makes no cloud writes. The runners now start a throwaway local-mode engine in a sandbox HOME
# and point the skill's scripts at it through PROSE_EDIT_NX (memory.py and brief.py read it; the sessions themselves
# may not call nx: --disallowedTools Bash(nx:*)). The pattern is tests/e2e/fresh-install-mvv.sh: `env -i` with an
# allowlist (a bare `env HOME=...` leaks UV_TOOL_DIR, XDG_* and the rest into the live install, nexus-enfoh), and
# `nx init -y --no-autostart`, which never registers a launchd or systemd unit.
#
#   local_engine_start NAME
#       Which nx the run uses, in this order:
#         PROSE_EDIT_LOCAL_ENGINE=0   opt out. Nothing here changes: PROSE_EDIT_NX keeps whatever the caller set, or
#                                     stays unset, so the skill's scripts use the `nx` the shell resolves (the
#                                     operator's configured install, cloud or not).
#         PROSE_EDIT_NX non-empty     the caller named a command on purpose; it wins and no engine starts.
#         otherwise                   the checkout's own nx ($WT/.venv/bin/nx) initialises a sandbox under TMPDIR
#                                     (prose-engine-XXXXXX/home) and PROSE_EDIT_NX is exported as the `env -i ...`
#                                     wrapper aimed at it. A missing .venv/bin/nx stops the run with the fix named
#                                     (uv sync, or one of the two switches above): the runner never falls back to
#                                     the operator's nx on its own. A failed init stops the run and names the log,
#                                     $OUT/local-engine-init.log.
#       The decision is recorded in $OUT/local-engine.txt and on stderr.
#   local_engine_stop
#       Stops the aspect worker (by its lease in the sandbox config dir), then the service and its PG
#       (`nx daemon service stop --with-pg`), and removes the sandbox. Registered with runner_at_exit, so the guard's
#       exit trap runs it after the sessions are stopped, on success, failure and signal alike. It never stops a
#       process by pattern: if a process whose command line names the sandbox survives the stop, the sandbox is
#       kept (removing a data directory under a live postmaster orphans its shared memory), the pids are printed,
#       and the operator stops them.
#
# The checkout's nx refuses every write ("STOP: refusing a WRITE ... presumed to target the OPERATOR'S REAL, LIVE
# STORE", src/nexus/db/service_endpoint.py guard_production_write) unless NX_ALLOW_PROD_WRITE names a reason. The
# wrapper carries one, and only the wrapper: the target is the sandbox engine, which the `env -i` allowlist (NX_LOCAL=1,
# NEXUS_CONFIG_DIR in the sandbox, no NX_SERVICE_*) makes the only endpoint the call can resolve. The shell the
# runner runs in never exports it.
#
# PROSE_EDIT_ENGINE_STOP_WAIT=<seconds> (default 10) is how long the teardown waits for the stopped processes to go.
# PROSE_EDIT_ENGINE_CACHE=<dir> keeps the ~416 MB bge model between runs, like FRESH_MVV_CACHE: it seeds the
# sandbox's ~/.cache/nexus from <dir>/nexus and saves the sandbox's copy there when the cache holds none yet.
LOCAL_ENGINE_DIR=""
LOCAL_ENGINE_CONFIG=""
LOCAL_ENGINE_ENV=()

_local_engine_note() {
  mkdir -p "$OUT" && printf '%s\n' "$1" >> "$OUT/local-engine.txt"
  printf 'local engine: %s\n' "$1" >&2
}

# One line per process, "<pid> <command line>", for every process whose command line contains $1 (a fixed string,
# not a pattern). The directory goes in through the environment so the awk running here does not list itself.
_local_engine_survivors() {
  ps -A -o pid= -o command= 2>/dev/null | LE_DIR="$1" LE_SELF="$$" awk '
    BEGIN { d = ENVIRON["LE_DIR"]; me = ENVIRON["LE_SELF"] }
    $1 != me && index($0, d) > 0 { print }'
}

local_engine_start() {
  local name="${1:?runner name}" nxbin sandbox base log cache
  if [ "${PROSE_EDIT_LOCAL_ENGINE:-1}" = 0 ]; then
    _local_engine_note "off (PROSE_EDIT_LOCAL_ENGINE=0): T2 goes to ${PROSE_EDIT_NX:-nx}, which may be the operator's real install"
    return 0
  fi
  if [ -n "${PROSE_EDIT_NX:-}" ]; then
    _local_engine_note "not started: PROSE_EDIT_NX is set and wins, T2 goes to ${PROSE_EDIT_NX}"
    return 0
  fi
  nxbin="$WT/.venv/bin/nx"
  if [ ! -x "$nxbin" ]; then
    echo "$name: no $nxbin. The local engine runs the checkout's own nx: run 'uv sync' in $WT. To skip the engine, set PROSE_EDIT_LOCAL_ENGINE=0 (T2 goes to the operator's configured nx) or PROSE_EDIT_NX=<command>." >&2
    exit 1
  fi
  base="${TMPDIR:?set TMPDIR}"
  base="${base%/}"
  mkdir -p "$base" || exit 1
  sandbox="$(mktemp -d "$base/prose-engine-XXXXXX")" || { echo "$name: cannot make a sandbox under $base" >&2; exit 1; }
  LOCAL_ENGINE_DIR="$sandbox"
  LOCAL_ENGINE_CONFIG="$sandbox/home/.config/nexus"
  runner_at_exit local_engine_stop   # before anything can fail, so a half-built sandbox is removed too
  mkdir -p "$sandbox/home" || exit 1
  LOCAL_ENGINE_ENV=(env -i
    "HOME=$sandbox/home"
    "NEXUS_CONFIG_DIR=$LOCAL_ENGINE_CONFIG"
    "PATH=$WT/.venv/bin:/usr/bin:/bin"
    "TERM=${TERM:-dumb}"
    NX_LOCAL=1
    NX_NO_TELEMETRY=1
    "NX_ALLOW_PROD_WRITE=prose-edit acceptance runner: these writes go to a throwaway local engine in $sandbox, never the operator's store"
    PYTHONIOENCODING=utf-8   # memory.py sets these for its child, and env -i would drop them
    PYTHONUTF8=1)
  [ -z "${HTTPS_PROXY:-}" ] || LOCAL_ENGINE_ENV+=("HTTPS_PROXY=$HTTPS_PROXY")
  [ -z "${HTTP_PROXY:-}" ] || LOCAL_ENGINE_ENV+=("HTTP_PROXY=$HTTP_PROXY")
  LOCAL_ENGINE_ENV+=("$nxbin")
  cache="${PROSE_EDIT_ENGINE_CACHE:-}"
  if [ -n "$cache" ] && [ -d "$cache/nexus" ]; then
    mkdir -p "$sandbox/home/.cache" && cp -R "$cache/nexus" "$sandbox/home/.cache/nexus"
    echo "local engine: seeded the model cache from $cache" >&2
  fi
  log="$OUT/local-engine-init.log"
  mkdir -p "$OUT" || exit 1
  echo "local engine: initialising a throwaway local-mode install in $sandbox (the first run downloads the model; PROSE_EDIT_ENGINE_CACHE keeps it)" >&2
  if ! "${LOCAL_ENGINE_ENV[@]}" init -y --no-autostart > "$log" 2>&1; then
    echo "$name: the local engine did not initialise (nx init failed); its log is $log. Nothing was written to any T2. PROSE_EDIT_LOCAL_ENGINE=0 runs against the operator's nx instead." >&2
    exit 1
  fi
  if ! grep -q "the service backend is serving" "$log"; then
    echo "$name: nx init exited 0 but did not say the service backend is serving; its log is $log." >&2
    exit 1
  fi
  if [ -n "$cache" ] && [ -d "$sandbox/home/.cache/nexus" ] && [ ! -d "$cache/nexus" ]; then
    mkdir -p "$cache" && cp -R "$sandbox/home/.cache/nexus" "$cache/nexus" 2>/dev/null
  fi
  PROSE_EDIT_NX="$(printf '%q ' "${LOCAL_ENGINE_ENV[@]}")"
  export PROSE_EDIT_NX
  _local_engine_note "started: $sandbox (T2 goes to this throwaway engine through PROSE_EDIT_NX; it is removed on exit)"
}

local_engine_stop() {
  local dir="$LOCAL_ENGINE_DIR" i left
  [ -n "$dir" ] || return 0
  LOCAL_ENGINE_DIR=""
  if [ "${#LOCAL_ENGINE_ENV[@]}" -gt 0 ]; then
    # The aspect worker an nx call spawned outlives the service by design (nexus-wl9pk): stop it by its lease first.
    "${LOCAL_ENGINE_ENV[@]}" daemon aspect-worker stop --config-dir "$LOCAL_ENGINE_CONFIG" >/dev/null 2>&1 || true
    "${LOCAL_ENGINE_ENV[@]}" daemon service stop --with-pg >/dev/null 2>&1 || true
  fi
  left="$(_local_engine_survivors "$dir")"
  i=0
  while [ -n "$left" ] && [ "$i" -lt "${PROSE_EDIT_ENGINE_STOP_WAIT:-10}" ]; do   # seconds a stopping engine gets to exit
    sleep 1
    i=$((i + 1))
    left="$(_local_engine_survivors "$dir")"
  done
  if [ -n "$left" ]; then
    echo "local engine: $dir KEPT. These processes still name it after the stop; stop each by its pid, then remove the directory:" >&2
    printf '%s\n' "$left" >&2
    return 0
  fi
  rm -rf "$dir"
  return 0
}
