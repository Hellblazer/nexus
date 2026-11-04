#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin-out-of-repo-gate.sh -- run the Claude Code plugins the way a
# user does: from a project that is NOT a nexus checkout (nexus-z7ddd).
#
#   tests/e2e/plugin-out-of-repo-gate.sh [--plugin DIR | --all | --installed X.Y.Z]
#                                        [--static-only | --sweep] [--no-canary]
#                                        [--allowlist FILE] [--only <name>[,<name>...]]
#                                        [--jobs N] [--model M] [--timeout SECONDS]
#                                        [--out DIR] [--keep]
#
#   --plugin DIR       one plugin directory (default: the repo's conexus/)
#   --all              the four plugin directories of this checkout: conexus, sn,
#                      tool-spill, prose-edit (what the release battery runs)
#   --installed X.Y.Z  the four published plugins as the marketplace installed
#                      them: $NX_GATE_PLUGIN_CACHE (default
#                      ~/.claude/plugins/cache/nexus-plugins)/<plugin>/X.Y.Z for
#                      conexus, sn, conexus-tool-spill and conexus-prose-edit
#                      (the release skill's post-publish step). A plugin that is
#                      not installed at that version, whose plugin.json names
#                      another plugin or version, or that is hollow, FAILS the
#                      run; none is ever skipped.
#   --static-only      the static pass alone; needs no claude
#   --sweep            add the live sweep: one claude run per agent and skill
#                      (opt-in, ADVISORY; --only implies it and skips the canary)
#   --no-canary        skip the canary (the sweep alone, for an investigation)
#   --allowlist FILE   reference allowlist (default: plugin-out-of-repo-allowlist.txt
#                      beside this script). Format and rules: lib/plugin_out_of_repo.py.
#
# Why it exists. On 2026-11-04 every agent's links to its relay template and
# context protocol (../resources/agent-shared/...) were found never to have
# resolved in any project, and nothing had caught it: every plugin check runs
# inside the nexus checkout, where those paths, `conexus/registry.yaml` and
# `scripts/...` all happen to open.
#
# What a default run is, and why it is deterministic:
#
#   1. STATIC. Every file reference in agents/, commands/, skills/, resources/
#      and the markdown a hook injects (hooks/**/*.md) is classified
#      (tests/e2e/lib/plugin_out_of_repo.py: the classes, the extraction scope and
#      what Claude Code substitutes). REPO-ONLY, RELATIVE-DEAD, MISSING, OUTSIDE
#      and UNSUBSTITUTED fail unless the allowlist carries the reference with a
#      written reason; a stale allowlist entry fails too.
#   2. CANARY. The static classes rest on beliefs about Claude Code (the variable
#      is substituted in an agent, a skill and a command but NOT in a file the
#      model Reads; a skill's relative link resolves against the announced base
#      directory). The canary measures them: a fixture plugin the gate builds with
#      random nonces, one claude run per mechanism, judged on the Read events and
#      the text Claude Code delivered, never on what the model says (lib/
#      plugin_canary.py). It fails when a measurement contradicts the static
#      pass's pins. A model that does not follow three plain lines is retried
#      (at most 2) and then reported UNVERIFIED, never as a pass.
#
#   The SWEEP (--sweep) is the per-target live pass: one run per agent and per
#   skill of the real plugin, the model quoting and Reading each reference. It
#   is advisory (the release skill runs it after publish): it fails only on
#   definite evidence, counts a target it could not probe as UNPROBED, retries
#   noise at most twice, and is UNVERIFIED below a coverage floor.
#
# A plugin with no agents, commands, skills or resource markdown (sn,
# conexus-tool-spill) has no census to take: it reports NOT-APPLICABLE. In --all
# and --installed, conexus and conexus-prose-edit must be APPLICABLE and sn
# HOOKS-ONLY, so a hollow plugin is a failure, and a run in which nothing was
# applicable is not a pass.
#
# Exit: 0 clean; 1 any failure; 2 UNVERIFIED (claude is not on PATH where a
# live pass is due, the canary or sweep produced no evidence); 64 usage.
#
# Isolation (the live runs). The real HOME stays (the sign-in lives there), and
# nothing else of the real install does:
#   * the plugin under test is run from a scratch COPY with its hooks emptied and
#     its agents' `memory:` dropped (conexus's SessionStart hooks run
#     `nx upgrade --auto`, `nx self gc` and a version-lockstep upgrade; a
#     memory:user agent reads and writes ~/.claude/agent-memory). The first version of
#     this gate loaded the real hooks and left 13 nx tool generations behind;
#   * NX_UPGRADE_AUTO=0, the lockstep marker pre-written in scratch, a scratch
#     NEXUS_CONFIG_DIR, NX_TOOLS_DIR, UV_TOOL_DIR, UV_TOOL_BIN_DIR, an empty plugin
#     registry and marketplace list, and a dead NX_SERVICE_URL: should a hook run
#     anyway it has nothing real to act on;
#   * `--setting-sources ""` (no user or project settings, so no installed plugin
#     loads beside the copy), `--strict-mcp-config` (no plugin MCP server),
#     `--no-session-persistence`, a Read/Agent/Task/Skill-only tool set, and a
#     scratch project directory with no git repository above it;
#   * a watchdog that kills the claude process TREE at --timeout, and an
#     INT/TERM handler that kills every run's tree.
# Nothing here prints a token, an env dump or a config file.
#
# Cost: the canary is 4 short claude runs (plus at most 2 retries each); the
# sweep is one run per agent (two model sessions each) and per loadable skill,
# NX_GATE_JOBS (default 4) at a time.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"

PLUGIN="$REPO_ROOT/conexus"
MULTI=""            # "" | all | installed
INSTALLED_VERSION=""
STATIC_ONLY=0
SWEEP=0
NO_CANARY=0
ONLY=""
JOBS="${NX_GATE_JOBS:-4}"
MODEL="${NX_GATE_MODEL:-sonnet}"
TIMEOUT="${NX_GATE_TIMEOUT:-420}"
BUDGET="${NX_GATE_MAX_USD:-2}"
RETRIES="${NX_GATE_RETRIES:-2}"
OUT=""
KEEP=0
ALLOWLIST=""
ALLOWLIST_EXPLICIT=0
CACHE="${NX_GATE_PLUGIN_CACHE:-$HOME/.claude/plugins/cache/nexus-plugins}"

usage() { sed -n '4,10p' "$0" >&2; exit 64; }
while [ $# -gt 0 ]; do
    case "$1" in
        --plugin) [ $# -ge 2 ] || usage; PLUGIN="$2"; shift 2 ;;
        --all) MULTI=all; shift ;;
        --installed) [ $# -ge 2 ] || usage; MULTI=installed; INSTALLED_VERSION="$2"; shift 2 ;;
        --allowlist) [ $# -ge 2 ] || usage; ALLOWLIST="$2"; ALLOWLIST_EXPLICIT=1; shift 2 ;;
        --static-only) STATIC_ONLY=1; shift ;;
        --sweep) SWEEP=1; shift ;;
        --no-canary) NO_CANARY=1; shift ;;
        --only) [ $# -ge 2 ] || usage; ONLY="$2"; shift 2 ;;
        --jobs) [ $# -ge 2 ] || usage; JOBS="$2"; shift 2 ;;
        --model) [ $# -ge 2 ] || usage; MODEL="$2"; shift 2 ;;
        --timeout) [ $# -ge 2 ] || usage; TIMEOUT="$2"; shift 2 ;;
        --out) [ $# -ge 2 ] || usage; OUT="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done
case "$JOBS" in ''|*[!0-9]*|0) echo "--jobs must be a positive integer" >&2; exit 64 ;; esac
case "$TIMEOUT" in ''|*[!0-9]*|0) echo "--timeout must be a positive integer" >&2; exit 64 ;; esac
case "$RETRIES" in ''|*[!0-9]*) echo "NX_GATE_RETRIES must be a non-negative integer" >&2; exit 64 ;; esac
if [ -z "$ALLOWLIST" ]; then ALLOWLIST="$HERE/plugin-out-of-repo-allowlist.txt"; fi
if [ "$ALLOWLIST_EXPLICIT" -eq 1 ] && [ ! -f "$ALLOWLIST" ]; then
    echo "--allowlist: no such file: $ALLOWLIST" >&2; exit 64
fi
[ -f "$ALLOWLIST" ] || ALLOWLIST=""
if [ -n "$MULTI" ] && [ -n "$ONLY" ]; then echo "--only selects within one plugin; it does not combine with --all or --installed" >&2; exit 64; fi
if [ "$STATIC_ONLY" -eq 1 ] && { [ "$SWEEP" -eq 1 ] || [ -n "$ONLY" ]; }; then
    echo "--static-only runs no model; it does not combine with --sweep or --only" >&2; exit 64
fi
[ -z "$ONLY" ] || { SWEEP=1; NO_CANARY=1; }   # --only is a targeted investigation: no canary
if [ "$MULTI" = installed ]; then
    case "$INSTALLED_VERSION" in [0-9]*.[0-9]*.[0-9]*) ;; *) echo "--installed wants a version like 7.95.0" >&2; exit 64 ;; esac
fi
e2e_python_resolve 10 || exit 2
PYMOD="$HERE/lib/plugin_out_of_repo.py"
CANARY="$HERE/lib/plugin_canary.py"
GATE_CLAUDE="${NX_GATE_CLAUDE:-claude}"

# ---------------------------------------------------------------------------
# Shared: process-tree kill, the isolated environment, one claude session.
# ---------------------------------------------------------------------------
kill_tree() {  # <pid> [signal]: children first, then the process
    local pid="$1" sig="${2:-TERM}" c
    for c in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$c" "$sig"; done
    kill "-$sig" "$pid" 2>/dev/null
}
# shellcheck disable=SC2329  # invoked by cleanup, which the EXIT trap runs
reap_jobs() {
    local p
    for p in $(jobs -rp); do kill_tree "$p" TERM; done
    sleep 0.2
    for p in $(jobs -rp); do kill_tree "$p" KILL; done
}

CLAUDE=""
WORK=""
CHILD_PID=""
# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
    reap_jobs
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then kill_tree "$CHILD_PID" TERM; fi
    if [ -n "$WORK" ] && [ "$KEEP" -ne 1 ]; then rm -rf "$WORK"; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

mkwork() {
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/plugin-out-of-repo-gate.XXXXXX")" || exit 2
    WORK="$(cd "$WORK" && pwd -P)"   # resolved: /var/folders is /private/var/folders on macOS
}

ENVOPTS=()
SCRATCH=""
setup_isolation() {  # <plugin version for the lockstep marker>
    local iso="$WORK/iso" ver="${1:-0.0.0}"
    mkdir -p "$iso/nexus-config" "$iso/nx-tools" "$iso/uv-tools" "$iso/uv-bin"
    printf '%s\n' "$ver" >"$iso/lockstep-marker"
    printf '{"version": 2, "plugins": {}}\n' >"$iso/installed_plugins.json"
    printf '{}\n' >"$iso/known_marketplaces.json"
    ENVOPTS=(NX_NO_TELEMETRY=1 NX_UPGRADE_AUTO=0
        "NEXUS_CONFIG_DIR=$iso/nexus-config" "NX_TOOLS_DIR=$iso/nx-tools"
        "UV_TOOL_DIR=$iso/uv-tools" "UV_TOOL_BIN_DIR=$iso/uv-bin"
        "NX_LOCKSTEP_MARKER=$iso/lockstep-marker"
        "NX_PLUGIN_REGISTRY=$iso/installed_plugins.json" "NX_PLUGIN_MARKETPLACES=$iso/known_marketplaces.json"
        NX_SERVICE_URL=http://127.0.0.1:9 NX_SERVICE_TOKEN=plugin-gate-unused)
    # A project directory with no git repository above it. A TMPDIR that sits
    # inside a checkout would make the run an in-repo run, so refuse that.
    SCRATCH="$WORK/project"
    mkdir -p "$SCRATCH"
    echo "  scratch: $WORK"
    local d="$SCRATCH"
    while [ "$d" != "/" ]; do
        if [ -e "$d/.git" ]; then
            echo "the scratch project $SCRATCH sits inside a git repository ($d); set TMPDIR elsewhere" >&2
            exit 2
        fi
        d="$(dirname "$d")"
    done
}

claude_session() {  # <events base path> <plugin dir> <prompt> <budget usd>
    local base="$1" plugin="$2" prompt="$3" budget="$4" pid wd
    (
        cd "$SCRATCH" || exit 1
        env "${ENVOPTS[@]}" "$CLAUDE" -p --plugin-dir "$plugin" --setting-sources "" \
            --strict-mcp-config --tools "Read,Agent,Task,Skill" --allowedTools "Read,Agent,Task,Skill" \
            --output-format stream-json --verbose --model "$MODEL" --max-budget-usd "$budget" \
            --no-session-persistence "$prompt" </dev/null >"$base.events.jsonl" 2>"$base.err" &
        pid=$!
        # watchdog: one-second sleeps so no sleep outlives the run, and no inherited
        # stdout is held open past it; at the timeout it kills the whole process tree
        (
            exec >/dev/null 2>&1
            n=0
            while [ "$n" -lt "$TIMEOUT" ]; do
                sleep 1
                kill -0 "$pid" 2>/dev/null || exit 0
                n=$((n + 1))
            done
            kill_tree "$pid" TERM
            sleep 2
            kill_tree "$pid" KILL
        ) &
        wd=$!
        wait "$pid"
        kill "$wd" 2>/dev/null
        wait "$wd" 2>/dev/null
    )
}

throttle() { while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$JOBS" ]; do sleep 1; done; }

claude_version() { local v; v="$("$CLAUDE" --version 2>/dev/null)"; printf '%s' "${v%%$'\n'*}"; }
need_claude() {
    CLAUDE="$(command -v "$GATE_CLAUDE" 2>/dev/null)" || CLAUDE=""
    [ -n "$CLAUDE" ]
}

# ---------------------------------------------------------------------------
# The canary: four claude runs against a fixture plugin the gate builds.
# Sets CANARY_RC (0 clean, 1 broken or contradicted, 2 no evidence).
# ---------------------------------------------------------------------------
CANARY_RC=0
run_canary() {
    local cw="$WORK/canary" ev sessions s prompt attempt retry
    ev="$cw/events"; [ -z "$OUT" ] || ev="$OUT/canary"
    mkdir -p "$cw" "$ev"
    "$E2E_PYTHON" -I "$CANARY" build --dest "$cw/plugin" --manifest "$cw/manifest.json" || { CANARY_RC=2; return; }
    sessions="$("$E2E_PYTHON" -I "$CANARY" prompts)" || { CANARY_RC=2; return; }
    echo "== canary (claude $(claude_version), model $MODEL, cwd $SCRATCH) =="
    local line
    while IFS=$'\t' read -r s prompt; do
        throttle
        claude_session "$ev/canary__$s" "$cw/plugin" "$prompt" 0.5 &
    done < <(printf '%s\n' "$sessions")
    wait
    attempt=0
    while [ "$attempt" -lt "$RETRIES" ]; do
        retry="$("$E2E_PYTHON" -I "$CANARY" judge --plugin "$cw/plugin" --manifest "$cw/manifest.json" --events-dir "$ev" --retry-list)"
        [ -n "$retry" ] || break
        attempt=$((attempt + 1))
        echo "  canary retry $attempt: $(printf '%s' "$retry" | tr '\n' ' ')"
        while IFS= read -r s; do
            line="$(printf '%s\n' "$sessions" | awk -F'\t' -v s="$s" '$1 == s')"
            prompt="${line#*$'\t'}"
            throttle
            claude_session "$ev/canary__$s" "$cw/plugin" "$prompt" 0.5 &
        done < <(printf '%s\n' "$retry")
        wait
    done
    "$E2E_PYTHON" -I "$CANARY" judge --plugin "$cw/plugin" --manifest "$cw/manifest.json" --events-dir "$ev"
    CANARY_RC=$?
}

# ---------------------------------------------------------------------------
# Multi-plugin mode: one single-plugin run per plugin, then one verdict.
# ---------------------------------------------------------------------------
expect_kind() {  # <label> -> APPLICABLE | HOOKS-ONLY | ANY
    case "$1" in conexus|prose-edit|conexus-prose-edit) echo APPLICABLE ;; sn) echo HOOKS-ONLY ;; *) echo ANY ;; esac
}
expect_name() {  # <label> -> the "name" its plugin.json must carry
    case "$1" in prose-edit|conexus-prose-edit) echo conexus-prose-edit ;; tool-spill|conexus-tool-spill) echo conexus-tool-spill ;; *) echo "$1" ;; esac
}

if [ -n "$MULTI" ]; then
    LABELS=(); DIRS=()
    if [ "$MULTI" = all ]; then
        for n in conexus sn tool-spill prose-edit; do LABELS+=("$n"); DIRS+=("$REPO_ROOT/$n"); done
    else
        for n in conexus sn conexus-tool-spill conexus-prose-edit; do LABELS+=("$n"); DIRS+=("$CACHE/$n/$INSTALLED_VERSION"); done
    fi
    mkwork
    START=$SECONDS
    ANY_FAILED=0; ANY_UNVERIFIED=0; PASSED_N=0; NA_N=0
    SUMMARY=()
    CANARY_NOTE=""
    if [ "$STATIC_ONLY" -eq 0 ] && [ "$NO_CANARY" -eq 0 ]; then
        if ! need_claude; then
            echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- the claude CLI ($GATE_CLAUDE) is not on PATH; the canary cannot run (use --static-only for the static pass alone)"
            exit 2
        fi
        setup_isolation 0.0.0
        run_canary
        case "$CANARY_RC" in
            0) SUMMARY+=("$(printf '%-22s %s' canary PASSED)") ;;
            2) ANY_UNVERIFIED=1; SUMMARY+=("$(printf '%-22s %s' canary UNVERIFIED)") ;;
            *) ANY_FAILED=1; SUMMARY+=("$(printf '%-22s %s' canary FAILED)") ;;
        esac
        CANARY_NOTE=" + canary"
        echo
    fi
    i=0
    while [ "$i" -lt "${#LABELS[@]}" ]; do
        label="${LABELS[$i]}"; dir="${DIRS[$i]}"; i=$((i + 1))
        echo "######## plugin: $label ($dir)"
        if [ ! -f "$dir/.claude-plugin/plugin.json" ]; then
            echo "PLUGIN OUT-OF-REPO GATE FAILED -- $label is not at $dir (an absent plugin is a failure, never a skip)"
            SUMMARY+=("$(printf '%-22s %s' "$label" 'FAILED (not found)')")
            ANY_FAILED=1
            continue
        fi
        IFS=$'\t' read -r pname pver < <("$E2E_PYTHON" -I "$PYMOD" meta --plugin "$dir")
        want_name="$(expect_name "$label")"
        if [ "${pname:-}" != "$want_name" ]; then
            echo "PLUGIN OUT-OF-REPO GATE FAILED -- $dir carries plugin.json name '${pname:-}', not '$want_name'"
            SUMMARY+=("$(printf '%-22s %s' "$label" "FAILED (plugin.json name ${pname:-none})")")
            ANY_FAILED=1
            continue
        fi
        if [ "$MULTI" = installed ] && [ "${pver:-}" != "$INSTALLED_VERSION" ]; then
            echo "PLUGIN OUT-OF-REPO GATE FAILED -- $dir carries plugin.json version '${pver:-}', not '$INSTALLED_VERSION' (a stale or mislabelled install)"
            SUMMARY+=("$(printf '%-22s %s' "$label" "FAILED (plugin.json version ${pver:-none})")")
            ANY_FAILED=1
            continue
        fi
        want_kind="$(expect_kind "$label")"
        IFS=$'\t' read -r kind _ _ < <("$E2E_PYTHON" -I "$PYMOD" applicability --plugin "$dir")
        if [ "$want_kind" != ANY ] && [ "${kind:-}" != "$want_kind" ]; then
            echo "PLUGIN OUT-OF-REPO GATE FAILED -- $label is ${kind:-unknown}, expected $want_kind: a hollow plugin is not a pass (nothing in it was examined)"
            SUMMARY+=("$(printf '%-22s %s' "$label" "FAILED (hollow: ${kind:-unknown}, want $want_kind)")")
            ANY_FAILED=1
            continue
        fi
        logdir="$WORK/$label"
        if [ -n "$OUT" ]; then logdir="$OUT/$label"; fi
        mkdir -p "$logdir"
        child=(bash "$HERE/plugin-out-of-repo-gate.sh" --plugin "$dir" --jobs "$JOBS" --model "$MODEL" --timeout "$TIMEOUT" --no-canary)
        [ "$STATIC_ONLY" -eq 0 ] || child+=(--static-only)
        [ "$SWEEP" -eq 0 ] || child+=(--sweep)
        [ -z "$ALLOWLIST" ] || child+=(--allowlist "$ALLOWLIST")
        [ -z "$OUT" ] || child+=(--out "$logdir")
        [ "$KEEP" -eq 0 ] || child+=(--keep)
        "${child[@]}" >"$logdir/gate.log" 2>&1 &
        CHILD_PID=$!
        wait "$CHILD_PID"
        rc=$?
        CHILD_PID=""
        cat "$logdir/gate.log"
        verdict="$(grep -E '^PLUGIN OUT-OF-REPO GATE (PASSED|FAILED|UNVERIFIED|NOT-APPLICABLE)' "$logdir/gate.log" | tail -1)"
        word="$(printf '%s' "$verdict" | sed -E 's/^PLUGIN OUT-OF-REPO GATE ([A-Z-]+).*/\1/')"
        if [ "$rc" -eq 0 ] && [ "$word" = PASSED ]; then
            PASSED_N=$((PASSED_N + 1))
            SUMMARY+=("$(printf '%-22s %s' "$label" 'PASSED')")
        elif [ "$rc" -eq 0 ] && [ "$word" = NOT-APPLICABLE ]; then
            NA_N=$((NA_N + 1))
            SUMMARY+=("$(printf '%-22s %s' "$label" 'NOT-APPLICABLE')")
        elif [ "$rc" -eq 2 ]; then
            ANY_UNVERIFIED=1
            SUMMARY+=("$(printf '%-22s %s' "$label" 'UNVERIFIED')")
        else
            # exit 0 with no verdict line is not a pass (nexus-f2g8u)
            ANY_FAILED=1
            why="no verdict line"
            [ -z "$word" ] || why="verdict $word"
            SUMMARY+=("$(printf '%-22s %s' "$label" "FAILED (exit $rc; $why)")")
        fi
    done
    echo
    echo "== plugin out-of-repo gate: ${#LABELS[@]} plugin(s), ${MULTI}${INSTALLED_VERSION:+ $INSTALLED_VERSION} =="
    for line in "${SUMMARY[@]}"; do echo "  $line"; done
    WALL=$((SECONDS - START))
    if [ "$STATIC_ONLY" -eq 1 ]; then MODE_NOTE=" (static pass only)"
    elif [ "$SWEEP" -eq 1 ]; then MODE_NOTE=" (static${CANARY_NOTE} + sweep, advisory)"
    else MODE_NOTE=" (static${CANARY_NOTE})"; fi
    TAIL="${MULTI}${INSTALLED_VERSION:+ $INSTALLED_VERSION}"
    if [ "$ANY_FAILED" -ne 0 ]; then
        echo "PLUGIN OUT-OF-REPO GATE FAILED -- $TAIL: $PASSED_N passed, $NA_N not applicable, ${WALL}s"
        exit 1
    fi
    if [ "$ANY_UNVERIFIED" -ne 0 ]; then
        echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- $TAIL: $PASSED_N passed, $NA_N not applicable, a live pass could not run, ${WALL}s"
        exit 2
    fi
    if [ "$PASSED_N" -eq 0 ]; then
        echo "PLUGIN OUT-OF-REPO GATE FAILED -- $TAIL: no plugin was applicable, so nothing was examined; a pass over nothing is not a pass"
        exit 1
    fi
    echo "PLUGIN OUT-OF-REPO GATE PASSED${MODE_NOTE} -- $TAIL: $PASSED_N passed, $NA_N not applicable, ${WALL}s"
    exit 0
fi

# ---------------------------------------------------------------------------
# One plugin.
# ---------------------------------------------------------------------------
[ -f "$PLUGIN/.claude-plugin/plugin.json" ] || { echo "not a plugin directory: $PLUGIN" >&2; exit 64; }
PLUGIN="$(cd "$PLUGIN" && pwd -P)"

KIND=""; HOOK_FILES=0
IFS=$'\t' read -r KIND _ HOOK_FILES < <("$E2E_PYTHON" -I "$PYMOD" applicability --plugin "$PLUGIN")
case "$KIND" in
    APPLICABLE|HOOKS-ONLY|NO-MARKDOWN) ;;
    *) echo "PLUGIN OUT-OF-REPO GATE FAILED -- could not take the applicability of $PLUGIN (the census crashed); never read as NOT-APPLICABLE" >&2
       exit 1 ;;
esac
TARGET_LINES="$("$E2E_PYTHON" -I "$PYMOD" targets --plugin "$PLUGIN")" || exit 2
NTARGETS=0
[ -z "$TARGET_LINES" ] || NTARGETS="$(printf '%s\n' "$TARGET_LINES" | wc -l | tr -d ' ')"

DO_CANARY=0
[ "$STATIC_ONLY" -eq 1 ] || [ "$NO_CANARY" -eq 1 ] || [ "$KIND" != APPLICABLE ] || DO_CANARY=1
DO_SWEEP=0
[ "$STATIC_ONLY" -eq 1 ] || [ "$SWEEP" -eq 0 ] || [ "$NTARGETS" -eq 0 ] || DO_SWEEP=1

if [ "$DO_CANARY" -eq 1 ] || [ "$DO_SWEEP" -eq 1 ]; then
    if ! need_claude; then
        echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- the claude CLI ($GATE_CLAUDE) is not on PATH; the canary cannot run (use --static-only for the static pass alone)" >&2
        exit 2
    fi
fi

mkwork
START=$SECONDS
FAILED=0
UNVERIFIED=0
CALLS=0

echo "== static pass =="
"$E2E_PYTHON" -I "$PYMOD" static --plugin "$PLUGIN" ${ALLOWLIST:+--allowlist "$ALLOWLIST"} || FAILED=1

if [ "$DO_CANARY" -eq 1 ] || [ "$DO_SWEEP" -eq 1 ]; then
    META_VER="$("$E2E_PYTHON" -I "$PYMOD" meta --plugin "$PLUGIN" | cut -f2)"
    setup_isolation "${META_VER:-0.0.0}"
fi

CANARY_NOTE=""
if [ "$DO_CANARY" -eq 1 ]; then
    echo
    run_canary
    CALLS=$((CALLS + 4))
    CANARY_NOTE=" + canary"
    case "$CANARY_RC" in 0) ;; 2) UNVERIFIED=1 ;; *) FAILED=1 ;; esac
fi

LIVE_NA=""
if [ "$STATIC_ONLY" -eq 0 ] && [ "$SWEEP" -eq 1 ] && [ "$NTARGETS" -eq 0 ]; then
    LIVE_NA="no agent or loadable skill to sweep"
    echo
    echo "== live sweep NOT-APPLICABLE: $PLUGIN has $LIVE_NA =="
fi

if [ "$DO_SWEEP" -eq 1 ]; then
    # The plugin runs from a scratch copy: hooks emptied, agent memory dropped (see Isolation).
    ISOPLUGIN="$WORK/sweep-plugin/$(basename "$PLUGIN")"
    mkdir -p "$WORK/sweep-plugin"
    echo
    echo "== sweep isolation: copying $PLUGIN =="
    "$E2E_PYTHON" -I "$CANARY" isolate --src "$PLUGIN" --dest "$ISOPLUGIN" | sed 's/^/  /' || { echo "could not make the scratch copy" >&2; exit 2; }
    EVENTS="${OUT:-$WORK/events}"
    mkdir -p "$EVENTS"
    SWEEP_TARGETS="$("$E2E_PYTHON" -I "$PYMOD" targets --plugin "$ISOPLUGIN")" || exit 2

    # shellcheck disable=SC2016  # the ${...} and $VAR are text for the model
    INNER_COMMON='List every file reference in the text that defines you: markdown link targets that name a file, ${...} paths, and any other path with a directory part, backticked or bare, absolute or relative. Skip URLs, globs, placeholders ($VAR, <name>, NNN), and paths that start with ~. Quote each target exactly as it is written in the text you were handed; do not resolve, rewrite or search for it. Read each one with the Read tool and use no other tool. Do not run any preflight or store anything.'
    agent_prompt() {  # <qualified agent name>
        printf '%s' "Dispatch ONE subagent of type $1 with the Agent tool, giving it this prompt verbatim: \"Look only at your own system prompt, the text that defines you. $INNER_COMMON A relative target is Read as written, against your working directory. Print one line per reference, tab separated: REF, the target as written, the absolute path you passed to Read. Print only those lines.\" Then print the subagent's reply verbatim and nothing else."
    }
    skill_prompt() {  # <qualified skill name>
        printf '%s' "Use the Skill tool to load the skill $1. Then look only at the text the skill delivered. $INNER_COMMON A relative target is resolved against the announced base directory. Print one line per reference, tab separated: REF, the target as written, the absolute path you passed to Read. Print only those lines."
    }
    sweep_run() {  # <lines of surface<TAB>qualified<TAB>bare>
        local surface qual bare prompt
        while IFS=$'\t' read -r surface qual bare; do
            [ -n "$surface" ] || continue
            if [ -n "$ONLY" ]; then
                case ",$ONLY," in *",$bare,"*|*",$qual,"*|*",$surface/$bare,"*) ;; *) continue ;; esac
            fi
            if [ "$surface" = agents ]; then prompt="$(agent_prompt "$qual")"; else prompt="$(skill_prompt "$qual")"; fi
            throttle
            claude_session "$EVENTS/${surface}__${bare}" "$ISOPLUGIN" "$prompt" "$BUDGET" &
            CALLS=$((CALLS + 1))
        done < <(printf '%s\n' "$1")
        wait
    }
    sweep_judge() {  # extra args
        "$E2E_PYTHON" -I "$PYMOD" live --plugin "$ISOPLUGIN" --events-dir "$EVENTS" ${ONLY:+--partial} \
            ${ALLOWLIST:+--allowlist "$ALLOWLIST"} "$@"
    }

    echo
    echo "== live sweep (claude $(claude_version), model $MODEL, $JOBS at a time, cwd $SCRATCH) =="
    sweep_run "$SWEEP_TARGETS"
    if [ "$CALLS" -eq 0 ]; then
        echo "live sweep FAILED: no target selected (--only matched nothing)"
        FAILED=1
    else
        attempt=0
        while [ "$attempt" -lt "$RETRIES" ]; do
            retry="$(sweep_judge --retry-list)"
            [ -n "$retry" ] || break
            attempt=$((attempt + 1))
            echo "  sweep retry $attempt: $(printf '%s' "$retry" | tr '\n' ' ')"
            lines=""
            while IFS= read -r name; do
                s="${name%%__*}"; b="${name#*__}"
                lines="$lines$(printf '%s\n' "$SWEEP_TARGETS" | awk -F'\t' -v s="$s" -v b="$b" '$1 == s && $3 == b')"$'\n'
            done < <(printf '%s\n' "$retry")
            sweep_run "$lines"
        done
        sweep_judge
        rc=$?
        case "$rc" in 0) ;; 2) UNVERIFIED=1 ;; *) FAILED=1 ;; esac
    fi
fi

WALL=$((SECONDS - START))
echo
if [ "$FAILED" -ne 0 ]; then
    echo "PLUGIN OUT-OF-REPO GATE FAILED -- $PLUGIN, $CALLS claude call(s), ${WALL}s"
    exit 1
fi
if [ "$UNVERIFIED" -ne 0 ]; then
    echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- the canary or sweep produced no evidence; $PLUGIN, $CALLS claude call(s), ${WALL}s"
    exit 2
fi
if [ "$KIND" != APPLICABLE ]; then
    echo "PLUGIN OUT-OF-REPO GATE NOT-APPLICABLE -- $PLUGIN has no agents, commands, skills or resource markdown (hook markdown files: $HOOK_FILES); the static pass over its hook markdown is clean, ${WALL}s"
    exit 0
fi
if [ "$STATIC_ONLY" -eq 1 ]; then
    echo "PLUGIN OUT-OF-REPO GATE PASSED (static pass only) -- $PLUGIN, ${WALL}s"
elif [ "$DO_SWEEP" -eq 1 ]; then
    echo "PLUGIN OUT-OF-REPO GATE PASSED (static${CANARY_NOTE} + sweep, advisory) -- $PLUGIN, $CALLS claude call(s), ${WALL}s"
elif [ -n "$LIVE_NA" ]; then
    echo "PLUGIN OUT-OF-REPO GATE PASSED (static${CANARY_NOTE}; sweep not applicable: $LIVE_NA) -- $PLUGIN, $CALLS claude call(s), ${WALL}s"
else
    echo "PLUGIN OUT-OF-REPO GATE PASSED (static${CANARY_NOTE:- only}) -- $PLUGIN, $CALLS claude call(s), ${WALL}s"
fi
exit 0
