#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-2rs76: PINNED LOCAL-MODE BENCHMARK CORPUS.
#
# Why this exists (Sam, 2026-10-08): local-mode measurements indexed the whole
# repository, about 80,000 chunks and 2 h 54 min for the first index. That is
# too slow to repeat for a latency survey, a rerank page-quality check or an
# index-throughput run. This script builds a corpus that is representative and
# quick, and the same one every time:
#
#   * a snapshot of src/nexus and docs/ at ONE release commit (benchmark_corpus.py
#     PIN_SHA = the v7.77.1 tag), without tests/ and without the RDR archive
#     docs/rdr/, committed into a fresh git repo inside the sandbox and indexed
#     with `nx index repo` in local mode (bundled PG, default local embedder);
#   * a knowledge fixture of 30 short notes in 2 subject collections
#     (distributed-systems, vector-search), generated from fixed text.
#
# Everything deterministic (the pins, the path filter, the notes, the verdicts)
# lives in benchmark-corpus/benchmark_corpus.py, which tests/e2e/benchmark-corpus/
# test_benchmark_corpus.py drives. This file is the sandbox around it.
#
# SANDBOX. Same approach as fresh-install-mvv.sh --published: HOME is a directory
# under the sandbox, every nx and uv call runs under an `env -i` allowlist (an
# exported VOYAGE_API_KEY, UV_* or XDG_* must not reach the journey), and conexus
# is installed from PyPI with `uv tool install` into that HOME, never into the
# operator's tool directory. fresh-install-mvv.sh cannot be sourced (it runs its
# whole journey on source), so its sandbox helpers are repeated here in the same
# shape, and tests/test_e2e_gates_isolate_home.py and its siblings lint both.
#
# TMPDIR. The sandbox goes under $NX_BENCH_ROOT, else $TMPDIR, else /tmp. The
# unix-socket limit that nexus-0tqr7 worried about does not apply: the bundled
# Postgres is started with unix_socket_directories = '' (src/nexus/db/
# pg_provision.py) and listens on TCP only, so a long TMPDIR costs nothing.
#
# Usage: tests/e2e/benchmark-corpus.sh COMMAND [options]
#   run  [--keep] [--exec CMD] [--conexus-version X.Y.Z] [--root DIR]
#        Provision, index, assert, print the numbers. Tear the sandbox down at
#        the end unless --keep. --exec CMD runs `bash -c CMD` inside the sandbox
#        environment after the assertions and before teardown; a non-zero exit
#        fails the run.
#   up   [same options]     = run --keep. Leaves the engine and Postgres running.
#   exec SANDBOX -- CMD...  Run CMD inside a kept sandbox's environment.
#   env  SANDBOX            Print the sandbox environment as KEY=VALUE lines.
#   down SANDBOX            Stop the engine and Postgres, assert no process names
#                           the sandbox, delete it.
#   (no command) / -h       This text.
#
# The environment a follow-on command sees (exec, --exec, env): HOME, PATH (the
# sandbox's nx first), TERM, NX_LOCAL=1, NX_NO_TELEMETRY=1, plus
#   NX_BENCH_SANDBOX    the sandbox directory
#   NX_BENCH_SNAPSHOT   the indexed repository (the snapshot checkout)
#   NX_BENCH_PYTHON     the interpreter of the installed conexus
# Nothing else is passed through. A throughput run sets its own variables:
#   tests/e2e/benchmark-corpus.sh exec "$SB" -- bash -c 'NX_INDEX_CONCURRENCY=2 nx index repo --force "$NX_BENCH_SNAPSHOT"'
#
# Assertions (each fails the run, none can skip-pass):
#   * the installed conexus is the pinned version (or the --conexus-version given);
#   * the snapshot's file count and digest match the pins;
#   * code__ + docs__ chunks are inside the pinned ranges, per collection and in
#     total; exactly 2 knowledge collections (the 2 subjects) with 15 chunks each,
#     30 in all, every note title listed;
#   * a code phrase and a docs phrase from the snapshot each return a hit from the
#     indexed repo collection, and a phrase from one note per subject returns that
#     note in the first 3 hits;
#   * before teardown at least one process names the sandbox (so the leftover
#     check can see something), and after teardown none does.
#
# Cost: first install + engine and model downloads, then the first index of
# the snapshot (10 to 15 minutes at 10 to 14 chunks per second on a quiet box).
# Run it where the load is known; on chas, through `lab-run -c lab-m -- ...`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
SCRIPT_START="$(date +%s)"

# One interpreter >= 3.10, resolved once; never a bare python3 (nexus-u67ow).
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
e2e_python_resolve || exit 2

HELPER="$HERE/benchmark-corpus/benchmark_corpus.py"
corpus_py() { "$E2E_PYTHON" "$HELPER" "$@"; }

PIN_TAG="$(corpus_py pin PIN_TAG)"
PIN_SHA="$(corpus_py pin PIN_SHA)"
CONEXUS_VERSION="$(corpus_py pin CONEXUS_VERSION)"
NEXUS_CANONICAL_REPO_URL="https://github.com/Hellblazer/nexus.git"
SANDBOX_MARKER=".nx-benchmark-corpus-sandbox"

WORK=""
HOME_DIR=""
LOGS=""
BIN_DIR=""
PROBE_PYTHON=""
SNAP_DIR=""
KEEP=0
RUN_OK=0
SERVICE_UP=0
CONEXUS_INSTALLED=""

_fail() { echo "BENCHMARK CORPUS FAILED: $*" >&2; exit 1; }

_usage() { sed -n '/^# Usage:/,/^# Assertions/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }

# ── sandboxed command wrappers ──────────────────────────────────────────────
# env -i allowlist, the shape of fresh-install-mvv.sh's _nx / _uv_sandboxed.
# Deliberately absent: VOYAGE_API_KEY (an ambient key must not flip the engine
# voyage-only), NX_* steering variables, XDG overrides.
_nx() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$BIN_DIR:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_NO_TELEMETRY=1 \
        NX_LOCAL=1 \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$BIN_DIR/nx" "$@"
}

# Ambient PATH stays (this step has to find the real uv); everything UV_*, XDG_*
# and PIP_* is dropped, so the install resolves against the real index and lands
# in the sandbox HOME (nexus-enfoh).
_uv_sandboxed() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$PATH" \
        TERM="${TERM:-dumb}" \
        NX_NO_TELEMETRY=1 \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        uv "$@"
}

# The environment a follow-on command runs in.
_in_sandbox() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$BIN_DIR:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_NO_TELEMETRY=1 \
        NX_LOCAL=1 \
        NX_BENCH_SANDBOX="$WORK" \
        NX_BENCH_SNAPSHOT="$SNAP_DIR" \
        NX_BENCH_PYTHON="$PROBE_PYTHON" \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$@"
}

# ── sandbox bookkeeping ─────────────────────────────────────────────────────
_set_paths() {
    HOME_DIR="$WORK/home"
    LOGS="$WORK/logs"
    SNAP_DIR="$WORK/corpus/nexus-snapshot"
}

_write_state() {
    {
        echo "BIN_DIR=$BIN_DIR"
        echo "PROBE_PYTHON=$PROBE_PYTHON"
        echo "CONEXUS_INSTALLED=$CONEXUS_INSTALLED"
    } >"$WORK/state.txt"
}

# A kept sandbox is found again by its directory: it must carry the marker file
# and a nxbc- name, because `down` deletes the tree.
_load_sandbox() {
    local dir="$1"
    [ -n "$dir" ] && [ -d "$dir" ] || _fail "sandbox directory not found: ${dir:-<none given>}"
    WORK="$(cd "$dir" && pwd)"
    [ -f "$WORK/$SANDBOX_MARKER" ] || _fail "$WORK is not a benchmark-corpus sandbox (no $SANDBOX_MARKER)"
    case "$(basename "$WORK")" in
        nxbc-*) ;;
        *) _fail "$WORK is not named nxbc-*; refusing to treat it as a sandbox" ;;
    esac
    [ -f "$WORK/state.txt" ] || _fail "$WORK has no state.txt; the install never finished, remove it by hand"
    _set_paths
    BIN_DIR="$(sed -n 's/^BIN_DIR=//p' "$WORK/state.txt")"
    PROBE_PYTHON="$(sed -n 's/^PROBE_PYTHON=//p' "$WORK/state.txt")"
    [ -n "$BIN_DIR" ] && [ -x "$BIN_DIR/nx" ] || _fail "$WORK/state.txt names no usable nx; the install did not finish"
}

_stop_service() {
    [ -n "$BIN_DIR" ] && [ -x "$BIN_DIR/nx" ] || return 0
    _nx daemon service stop --with-pg >"$WORK/stop.log" 2>&1 || true
}

# Lines of the process table that name the sandbox path. Fixed-string match on
# the sandbox directory, never a broad pattern (benchmark_corpus.leftover_processes).
_sandbox_processes() {
    local ps_out
    ps_out="$(ps -axww -o pid= -o args= 2>/dev/null || true)"
    printf '%s\n' "$ps_out" | corpus_py leftover "$WORK" || true
}

# Stop the sandbox's service and prove nothing of it is left running.
_stop_and_verify_no_processes() {
    local left
    _stop_service
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        left="$(_sandbox_processes)"
        [ -z "$left" ] && return 0
        sleep 1
    done
    echo "processes still naming $WORK after nx daemon service stop --with-pg:" >&2
    printf '%s\n' "$left" >&2
    return 1
}

_teardown() {
    _stop_and_verify_no_processes || _fail "sandbox processes survived the stop; $WORK kept for inspection"
    SERVICE_UP=0
    rm -rf "$WORK"
    [ ! -e "$WORK" ] || _fail "$WORK still exists after removal"
    echo "  teardown: engine and Postgres stopped, no process names $WORK, directory removed"
}

cleanup() {
    local rc=$?
    if [ "$RUN_OK" = 1 ] && [ "$KEEP" = 1 ]; then
        return
    fi
    if [ "$RUN_OK" != 1 ] && [ -n "$WORK" ] && [ -d "$WORK" ]; then
        if [ "$SERVICE_UP" = 1 ]; then
            _stop_service
        fi
        echo "FAILURE EVIDENCE PRESERVED: $LOGS (remove with: $0 down $WORK)" >&2
    fi
    return "$rc"
}

# ── commands ────────────────────────────────────────────────────────────────
_cmd_run() {
    local exec_cmd="" conexus_version="$CONEXUS_VERSION" root="" arg
    while [ $# -gt 0 ]; do
        arg="$1"
        case "$arg" in
            --keep) KEEP=1; shift ;;
            --exec) [ $# -ge 2 ] || _fail "--exec needs a command"; exec_cmd="$2"; shift 2 ;;
            --conexus-version) [ $# -ge 2 ] || _fail "--conexus-version needs X.Y.Z"; conexus_version="$2"; shift 2 ;;
            --root) [ $# -ge 2 ] || _fail "--root needs a directory"; root="$2"; shift 2 ;;
            *) _fail "unknown option: $arg" ;;
        esac
    done
    root="${root:-${NX_BENCH_ROOT:-${TMPDIR:-/tmp}}}"
    [ -d "$root" ] || _fail "sandbox root $root does not exist"
    CONEXUS_INSTALLED="$conexus_version"

    echo "================================================================"
    echo " BENCHMARK CORPUS: $PIN_TAG snapshot ($PIN_SHA)"
    echo " conexus $conexus_version from PyPI, local mode, isolated HOME"
    echo "================================================================"

    WORK="$(mktemp -d "${root%/}/nxbc-XXXXXX")"
    : >"$WORK/$SANDBOX_MARKER"
    _set_paths
    mkdir -p "$HOME_DIR" "$LOGS" "$WORK/corpus"
    trap cleanup EXIT
    echo "  sandbox: $WORK"

    echo "-- 1/8 install conexus==$conexus_version from PyPI into the sandbox HOME --"
    # Linux gets --torch-backend cpu, exactly as the install page tells Linux
    # users (the PyPI default there is the 3.9 GB CUDA torch).
    local torch_flag=()
    [ "$(uname -s)" = "Linux" ] && torch_flag=(--torch-backend cpu)
    _uv_sandboxed tool install --python 3.12 --no-cache "conexus==$conexus_version" "${torch_flag[@]+"${torch_flag[@]}"}" \
        >"$LOGS/install.log" 2>&1 \
        || _fail "uv tool install conexus==$conexus_version failed (see $LOGS/install.log)"
    local tool_dir tool_bin
    tool_dir="$(_uv_sandboxed tool dir)"
    tool_bin="$(_uv_sandboxed tool dir --bin)"
    BIN_DIR="$tool_bin"
    PROBE_PYTHON="$tool_dir/conexus/bin/python"
    [ -x "$BIN_DIR/nx" ] || _fail "uv tool install did not expose $BIN_DIR/nx"
    [ -x "$PROBE_PYTHON" ] || _fail "no interpreter at $PROBE_PYTHON"
    _write_state
    local version_out
    version_out="$(_nx --version)"
    echo "  $version_out"
    [[ "$version_out" == *"$conexus_version"* ]] \
        || _fail "installed nx reports '$version_out', expected conexus $conexus_version"

    echo "-- 2/8 nx init (local mode) --"
    SERVICE_UP=1
    _nx init -y --no-autostart >"$LOGS/init.log" 2>&1 </dev/null || _fail "nx init failed (see $LOGS/init.log)"
    grep -Eq "the service backend is serving" "$LOGS/init.log" \
        || _fail "init did not confirm a serving backend (see $LOGS/init.log)"
    grep -q "converged and verified" "$LOGS/init.log" \
        || _fail "init did not converge the upgrade ladder (see $LOGS/init.log)"

    echo "-- 3/8 materialise the snapshot of $PIN_TAG --"
    if ! git -C "$REPO_ROOT" cat-file -e "$PIN_SHA^{commit}" 2>/dev/null; then
        echo "  $PIN_SHA is not in this checkout; fetching it from $NEXUS_CANONICAL_REPO_URL"
        git -C "$REPO_ROOT" fetch --quiet "$NEXUS_CANONICAL_REPO_URL" "$PIN_SHA" \
            || _fail "cannot get commit $PIN_SHA (the $PIN_TAG release commit) from $NEXUS_CANONICAL_REPO_URL"
    fi
    corpus_py snapshot "$REPO_ROOT" "$SNAP_DIR" >"$LOGS/snapshot.log" 2>&1 \
        || { cat "$LOGS/snapshot.log" >&2; _fail "snapshot failed (see $LOGS/snapshot.log)"; }
    cat "$LOGS/snapshot.log"

    echo "-- 4/8 knowledge fixture: 30 notes in 2 subjects --"
    corpus_py notes "$WORK/notes" >"$LOGS/notes.tsv"
    local subject title path note_count=0 tab
    tab="$(printf '\t')"
    while IFS="$tab" read -r subject title path; do
        _nx store put "$path" --collection "$subject" --title "$title" \
            >"$LOGS/store-$title.log" 2>&1 </dev/null \
            || _fail "store put of note $title failed (see $LOGS/store-$title.log)"
        grep -Eq "Stored: [0-9a-f]{64}" "$LOGS/store-$title.log" \
            || _fail "store put of note $title printed no full-digest id (see $LOGS/store-$title.log)"
        if grep -q "manifest_hook_batch_missing_doc_identity" "$LOGS/store-$title.log"; then
            _fail "note $title skipped its catalog manifest (nexus-f1itv class)"
        fi
        note_count=$((note_count + 1))
    done <"$LOGS/notes.tsv"
    [ "$note_count" = 30 ] || _fail "stored $note_count notes, expected 30"
    echo "  stored $note_count notes"

    echo "-- 5/8 first index of the snapshot (index repo) --"
    local t0 t1 index_seconds
    t0="$(date +%s)"
    _nx index repo "$SNAP_DIR" --debug-timing >"$LOGS/index.log" 2>&1 </dev/null \
        || { tail -20 "$LOGS/index.log" >&2; _fail "index repo failed (see $LOGS/index.log)"; }
    t1="$(date +%s)"
    index_seconds=$((t1 - t0))
    echo "  indexed in ${index_seconds}s (log: $LOGS/index.log)"

    echo "-- 6/8 chunk counts and note listings --"
    _nx collection list >"$LOGS/collections.txt" 2>&1 </dev/null || _fail "collection list failed (see $LOGS/collections.txt)"
    corpus_py check-counts "$index_seconds" <"$LOGS/collections.txt" | tee "$LOGS/report.txt" \
        || _fail "chunk counts are outside the pinned corpus (see $LOGS/collections.txt)"
    for subject in distributed-systems vector-search; do
        _nx store list --collection "$subject" --limit 300 >"$LOGS/store-list-$subject.txt" 2>&1 </dev/null \
            || _fail "store list of $subject failed (see $LOGS/store-list-$subject.txt)"
        corpus_py check-listing "$subject" <"$LOGS/store-list-$subject.txt" \
            || _fail "the $subject notes are not all listed (see $LOGS/store-list-$subject.txt)"
    done

    echo "-- 7/8 search probes --"
    local name corpus query probe_count=0 probe_expected
    corpus_py probes >"$LOGS/probes.tsv"
    probe_expected="$(wc -l <"$LOGS/probes.tsv" | tr -d ' ')"
    while IFS="$tab" read -r name corpus query; do
        _nx search "$query" --corpus "$corpus" --json --n 10 \
            >"$LOGS/search-$name.json" 2>"$LOGS/search-$name.err" </dev/null \
            || _fail "search probe $name failed (see $LOGS/search-$name.err)"
        corpus_py check-search "$name" <"$LOGS/search-$name.json" \
            || _fail "search probe $name did not return what it should (see $LOGS/search-$name.json)"
        probe_count=$((probe_count + 1))
    done <"$LOGS/probes.tsv"
    [ "$probe_count" -ge 4 ] && [ "$probe_count" = "$probe_expected" ] \
        || _fail "ran $probe_count search probes, expected $probe_expected (at least 4)"

    echo "-- 8/8 the process check can see the sandbox --"
    local live
    live="$(_sandbox_processes)"
    [ -n "$live" ] \
        || _fail "no process names $WORK while the service is up; the leftover check would pass vacuously"
    echo "  $(printf '%s\n' "$live" | wc -l | tr -d ' ') process(es) name the sandbox"

    if [ -n "$exec_cmd" ]; then
        echo "-- exec: $exec_cmd --"
        _in_sandbox bash -c "$exec_cmd" || _fail "--exec command exited non-zero"
    fi

    local total_label=""
    if [ "$KEEP" = 1 ]; then
        RUN_OK=1
        echo "SANDBOX UP: $WORK"
        echo "  follow-on: $0 exec $WORK -- <command>   |   $0 env $WORK   |   $0 down $WORK"
    else
        _teardown
        RUN_OK=1
    fi
    total_label="total script wall time: $(( $(date +%s) - SCRIPT_START ))s"
    echo "$total_label"
    local installed_note=""
    [ "$conexus_version" = "$CONEXUS_VERSION" ] || installed_note=" [conexus overridden from pinned $CONEXUS_VERSION]"
    echo "BENCHMARK CORPUS PASSED: conexus $conexus_version, snapshot $PIN_TAG ($PIN_SHA)$installed_note"
}

_cmd_exec() {
    [ $# -ge 1 ] || _fail "exec needs a sandbox directory"
    _load_sandbox "$1"
    shift
    [ "${1:-}" = "--" ] && shift
    [ $# -ge 1 ] || _fail "exec needs a command after the sandbox directory"
    _in_sandbox "$@"
}

_cmd_env() {
    [ $# -eq 1 ] || _fail "env needs exactly a sandbox directory"
    _load_sandbox "$1"
    echo "HOME=$HOME_DIR"
    echo "PATH=$BIN_DIR:/usr/bin:/bin"
    echo "NX_LOCAL=1"
    echo "NX_NO_TELEMETRY=1"
    echo "NX_BENCH_SANDBOX=$WORK"
    echo "NX_BENCH_SNAPSHOT=$SNAP_DIR"
    echo "NX_BENCH_PYTHON=$PROBE_PYTHON"
}

_cmd_down() {
    [ $# -eq 1 ] || _fail "down needs exactly a sandbox directory"
    _load_sandbox "$1"
    _teardown
    echo "BENCHMARK CORPUS SANDBOX REMOVED"
}

case "${1:-}" in
    run) shift; _cmd_run "$@" ;;
    up) shift; _cmd_run --keep "$@" ;;
    exec) shift; _cmd_exec "$@" ;;
    env) shift; _cmd_env "$@" ;;
    down) shift; _cmd_down "$@" ;;
    ""|-h|--help) _usage ;;
    *) echo "unknown command: $1" >&2; _usage >&2; exit 2 ;;
esac
