#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin-out-of-repo-gate.sh -- run the Claude Code plugins the way a
# user does: from a project that is NOT a nexus checkout (nexus-z7ddd).
#
#   tests/e2e/plugin-out-of-repo-gate.sh [--plugin DIR | --all | --installed X.Y.Z]
#                                        [--static-only] [--allowlist FILE]
#                                        [--only <name>[,<name>...]] [--jobs N]
#                                        [--model M] [--timeout SECONDS]
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
#                      not installed at that version FAILS the run; it is never
#                      skipped.
#   --allowlist FILE   reference allowlist (default: plugin-out-of-repo-allowlist.txt
#                      beside this script). Format and rules: lib/plugin_out_of_repo.py.
#
# Why it exists. On 2026-11-04 every agent's links to its relay template and
# context protocol (../resources/agent-shared/...) were found never to have
# resolved in any project, and nothing had caught it: every plugin check runs
# inside the nexus checkout, where those paths, `conexus/registry.yaml` and
# `scripts/...` all happen to open. 7.95.0 rewrote the agent links as
# ${CLAUDE_PLUGIN_ROOT}/...; Claude Code substitutes that in agent prompts.
#
# Two passes, both judged without trusting the model:
#
#   1. STATIC. Every file reference in agents/, commands/, skills/, resources/
#      and the markdown a hook injects (hooks/**/*.md) is classified after
#      substituting ${CLAUDE_PLUGIN_ROOT} (RESOLVES / RELATIVE-NEAR / REPO-ONLY /
#      RELATIVE-DEAD / MISSING / OUTSIDE / UNSUBSTITUTED / URL / PLACEHOLDER /
#      PROJECT; the table and the rules are in tests/e2e/lib/plugin_out_of_repo.py).
#      A directory that exists counts as a file. REPO-ONLY, RELATIVE-DEAD,
#      MISSING, OUTSIDE and UNSUBSTITUTED fail unless the allowlist carries the
#      reference with a written reason; a stale allowlist entry fails too.
#   2. LIVE. One `claude -p --plugin-dir <plugin>` per agent (dispatched through
#      the Agent tool) and per skill (loaded through the Skill tool), run from a
#      fresh mktemp directory that has no git repository above it. Each model
#      quotes every file reference in the text it was handed and Reads it. The
#      verdict comes from the Read results in the stream-json event stream: a
#      path that does not open, a relative target in an agent, a path read from
#      a nexus checkout outside the plugin, or a run that quotes nothing where
#      the static pass expects references, all fail. A skill the Skill tool will
#      not load (disable-model-invocation) is static-only and is named as such.
#
# A plugin with no agents, commands, skills or resource markdown (sn,
# conexus-tool-spill) has no census to take: it reports NOT-APPLICABLE, its hook
# markdown still gets the static pass, and no claude run is made for it. A
# multi-plugin run is PASSED only when at least one plugin was applicable and
# passed: a run over four plugins that examined nothing is not a pass.
#
# Exit: 0 clean; 1 any failure; 2 claude is not on PATH where a live pass is due
# (never a pass) or no live run produced a result and nothing else failed; 64
# usage. --static-only skips the live pass and does not need claude.
#
# Isolation. The live runs use the real HOME (the sign-in lives there) but a
# scratch NEXUS_CONFIG_DIR and a dead NX_SERVICE_URL, `--strict-mcp-config`
# (no plugin MCP server starts) and `--setting-sources ""` (the installed copy
# of the plugin does not load beside the one under test), so no run writes to a
# live store. NX_GATE_LIVE_STORE=1 drops the config and service isolation.
# Nothing here prints a token, an env dump or a config file.
#
# Cost: one call per agent (two model sessions each: the outer one and the
# subagent) and one per loadable skill, NX_GATE_JOBS (default 4) at a time.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"

PLUGIN="$REPO_ROOT/conexus"
MULTI=""            # "" | all | installed
INSTALLED_VERSION=""
STATIC_ONLY=0
ONLY=""
JOBS="${NX_GATE_JOBS:-4}"
MODEL="${NX_GATE_MODEL:-sonnet}"
TIMEOUT="${NX_GATE_TIMEOUT:-420}"
BUDGET="${NX_GATE_MAX_USD:-2}"
OUT=""
KEEP=0
ALLOWLIST=""
ALLOWLIST_EXPLICIT=0
CACHE="${NX_GATE_PLUGIN_CACHE:-$HOME/.claude/plugins/cache/nexus-plugins}"

usage() { sed -n '4,9p' "$0" >&2; exit 64; }
while [ $# -gt 0 ]; do
    case "$1" in
        --plugin) [ $# -ge 2 ] || usage; PLUGIN="$2"; shift 2 ;;
        --all) MULTI=all; shift ;;
        --installed) [ $# -ge 2 ] || usage; MULTI=installed; INSTALLED_VERSION="$2"; shift 2 ;;
        --allowlist) [ $# -ge 2 ] || usage; ALLOWLIST="$2"; ALLOWLIST_EXPLICIT=1; shift 2 ;;
        --static-only) STATIC_ONLY=1; shift ;;
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
if [ -z "$ALLOWLIST" ]; then ALLOWLIST="$HERE/plugin-out-of-repo-allowlist.txt"; fi
if [ "$ALLOWLIST_EXPLICIT" -eq 1 ] && [ ! -f "$ALLOWLIST" ]; then
    echo "--allowlist: no such file: $ALLOWLIST" >&2; exit 64
fi
[ -f "$ALLOWLIST" ] || ALLOWLIST=""
if [ -n "$MULTI" ] && [ -n "$ONLY" ]; then echo "--only selects within one plugin; it does not combine with --all or --installed" >&2; exit 64; fi
if [ "$MULTI" = installed ]; then
    case "$INSTALLED_VERSION" in [0-9]*.[0-9]*.[0-9]*) ;; *) echo "--installed wants a version like 7.95.0" >&2; exit 64 ;; esac
fi
e2e_python_resolve 10 || exit 2
PYMOD="$HERE/lib/plugin_out_of_repo.py"

# ---------------------------------------------------------------------------
# Multi-plugin mode: one single-plugin run per plugin, then one verdict.
# ---------------------------------------------------------------------------
if [ -n "$MULTI" ]; then
    LABELS=(); DIRS=()
    if [ "$MULTI" = all ]; then
        for n in conexus sn tool-spill prose-edit; do LABELS+=("$n"); DIRS+=("$REPO_ROOT/$n"); done
    else
        for n in conexus sn conexus-tool-spill conexus-prose-edit; do LABELS+=("$n"); DIRS+=("$CACHE/$n/$INSTALLED_VERSION"); done
    fi
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/plugin-out-of-repo-gate-all.XXXXXX")" || exit 2
    # shellcheck disable=SC2329  # invoked by the EXIT trap
    cleanup_multi() { [ "$KEEP" -eq 1 ] || rm -rf "$WORK"; }
    trap cleanup_multi EXIT
    trap 'exit 130' INT TERM
    START=$SECONDS
    ANY_FAILED=0; ANY_UNVERIFIED=0; PASSED_N=0; NA_N=0
    SUMMARY=()
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
        logdir="$WORK/$label"
        if [ -n "$OUT" ]; then logdir="$OUT/$label"; fi
        mkdir -p "$logdir"
        child=(bash "$HERE/plugin-out-of-repo-gate.sh" --plugin "$dir" --jobs "$JOBS" --model "$MODEL" --timeout "$TIMEOUT")
        [ "$STATIC_ONLY" -eq 0 ] || child+=(--static-only)
        [ -z "$ALLOWLIST" ] || child+=(--allowlist "$ALLOWLIST")
        [ -z "$OUT" ] || child+=(--out "$logdir")
        [ "$KEEP" -eq 0 ] || child+=(--keep)
        "${child[@]}" >"$logdir/gate.log" 2>&1
        rc=$?
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
    MODE_NOTE=""; [ "$STATIC_ONLY" -eq 0 ] || MODE_NOTE=" (static pass only)"
    if [ "$ANY_FAILED" -ne 0 ]; then
        echo "PLUGIN OUT-OF-REPO GATE FAILED -- $MULTI${INSTALLED_VERSION:+ $INSTALLED_VERSION}: $PASSED_N passed, $NA_N not applicable, ${WALL}s"
        exit 1
    fi
    if [ "$ANY_UNVERIFIED" -ne 0 ]; then
        echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- $MULTI${INSTALLED_VERSION:+ $INSTALLED_VERSION}: $PASSED_N passed, $NA_N not applicable, a live pass could not run, ${WALL}s"
        exit 2
    fi
    if [ "$PASSED_N" -eq 0 ]; then
        echo "PLUGIN OUT-OF-REPO GATE FAILED -- $MULTI${INSTALLED_VERSION:+ $INSTALLED_VERSION}: no plugin was applicable, so nothing was examined; a pass over nothing is not a pass"
        exit 1
    fi
    echo "PLUGIN OUT-OF-REPO GATE PASSED${MODE_NOTE} -- $MULTI${INSTALLED_VERSION:+ $INSTALLED_VERSION}: $PASSED_N passed, $NA_N not applicable, ${WALL}s"
    exit 0
fi

# ---------------------------------------------------------------------------
# One plugin.
# ---------------------------------------------------------------------------
[ -f "$PLUGIN/.claude-plugin/plugin.json" ] || { echo "not a plugin directory: $PLUGIN" >&2; exit 64; }
PLUGIN="$(cd "$PLUGIN" && pwd -P)"

IFS=$'\t' read -r KIND _ HOOK_FILES < <("$E2E_PYTHON" -I "$PYMOD" applicability --plugin "$PLUGIN")
TARGET_LINES="$("$E2E_PYTHON" -I "$PYMOD" targets --plugin "$PLUGIN")" || exit 2
NTARGETS=0
[ -z "$TARGET_LINES" ] || NTARGETS="$(printf '%s\n' "$TARGET_LINES" | wc -l | tr -d ' ')"

CLAUDE=""
if [ "$STATIC_ONLY" -eq 0 ] && [ "$NTARGETS" -gt 0 ]; then
    CLAUDE="$(command -v "${NX_GATE_CLAUDE:-claude}" 2>/dev/null)" || CLAUDE=""
    if [ -z "$CLAUDE" ]; then
        echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- the claude CLI is not on PATH; the live pass cannot run (use --static-only for the static pass alone)" >&2
        exit 2
    fi
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/plugin-out-of-repo-gate.XXXXXX")" || exit 2
# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
    local p
    for p in $(jobs -rp); do kill "$p" 2>/dev/null; done
    [ "$KEEP" -eq 1 ] || rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
START=$SECONDS
FAILED=0
UNVERIFIED=0

echo "== static pass =="
"$E2E_PYTHON" -I "$PYMOD" static --plugin "$PLUGIN" ${ALLOWLIST:+--allowlist "$ALLOWLIST"} || FAILED=1

CALLS=0
LIVE_NA=""
if [ "$STATIC_ONLY" -eq 0 ] && [ "$NTARGETS" -eq 0 ]; then
    LIVE_NA="no agent or loadable skill to run"
    echo
    echo "== live pass NOT-APPLICABLE: $PLUGIN has $LIVE_NA =="
fi
if [ "$STATIC_ONLY" -eq 0 ] && [ "$NTARGETS" -gt 0 ]; then
    # A project directory with no git repository above it. A TMPDIR that sits
    # inside a checkout would make the run an in-repo run, so refuse that.
    SCRATCH="$WORK/project"
    mkdir -p "$SCRATCH"
    d="$SCRATCH"
    while [ "$d" != "/" ]; do
        if [ -e "$d/.git" ]; then
            echo "the scratch project $SCRATCH sits inside a git repository ($d); set TMPDIR elsewhere" >&2
            exit 2
        fi
        d="$(dirname "$d")"
    done
    EVENTS="${OUT:-$WORK/events}"
    mkdir -p "$EVENTS"

    # shellcheck disable=SC2016  # the ${...} and $VAR are text for the model
    INNER_COMMON='List every file reference in the text that defines you: markdown link targets that name a file, ${...} paths, and any other path with a directory part, backticked or bare, absolute or relative. Skip URLs, globs, placeholders ($VAR, <name>, NNN), and paths that start with ~. Quote each target exactly as it is written in the text you were handed; do not resolve, rewrite or search for it. Read each one with the Read tool and use no other tool. Do not run any preflight or store anything.'
    agent_prompt() {  # <qualified agent name>
        printf '%s' "Dispatch ONE subagent of type $1 with the Agent tool, giving it this prompt verbatim: \"Look only at your own system prompt, the text that defines you. $INNER_COMMON A relative target is Read as written, against your working directory. Print one line per reference, tab separated: REF, the target as written, the absolute path you passed to Read. Print only those lines.\" Then print the subagent's reply verbatim and nothing else."
    }
    skill_prompt() {  # <qualified skill name>
        printf '%s' "Use the Skill tool to load the skill $1. Then look only at the text the skill delivered. $INNER_COMMON A relative target is resolved against the announced base directory. Print one line per reference, tab separated: REF, the target as written, the absolute path you passed to Read. Print only those lines."
    }

    ENVOPTS=(NX_NO_TELEMETRY=1)
    if [ "${NX_GATE_LIVE_STORE:-0}" != "1" ]; then
        mkdir -p "$WORK/nexus-config"
        ENVOPTS+=("NEXUS_CONFIG_DIR=$WORK/nexus-config" "NX_SERVICE_URL=http://127.0.0.1:9" "NX_SERVICE_TOKEN=plugin-gate-unused")
    fi

    run_one() {  # <surface> <qualified> <bare>
        local surface="$1" qual="$2" bare="$3" prompt pid wd
        local base="$EVENTS/${surface}__${bare}"
        if [ "$surface" = "agents" ]; then prompt="$(agent_prompt "$qual")"; else prompt="$(skill_prompt "$qual")"; fi
        (
            cd "$SCRATCH" || exit 1
            env "${ENVOPTS[@]}" "$CLAUDE" -p --plugin-dir "$PLUGIN" --setting-sources "" \
                --strict-mcp-config --tools "Read,Agent,Task,Skill" --allowedTools "Read,Agent,Task,Skill" \
                --output-format stream-json --verbose --model "$MODEL" --max-budget-usd "$BUDGET" \
                --no-session-persistence "$prompt" </dev/null >"$base.events.jsonl" 2>"$base.err" &
            pid=$!
            # watchdog: one-second sleeps so no sleep outlives the run, and no
            # inherited stdout is held open past it
            (
                exec >/dev/null 2>&1
                n=0
                while [ "$n" -lt "$TIMEOUT" ]; do
                    sleep 1
                    kill -0 "$pid" 2>/dev/null || exit 0
                    n=$((n + 1))
                done
                kill "$pid" 2>/dev/null
            ) &
            wd=$!
            wait "$pid"
            kill "$wd" 2>/dev/null
            wait "$wd" 2>/dev/null
        )
    }

    echo
    CLAUDE_VERSION="$("$CLAUDE" --version 2>/dev/null)"
    echo "== live pass (claude ${CLAUDE_VERSION%%$'\n'*}, model $MODEL, $JOBS at a time, cwd $SCRATCH) =="
    while IFS=$'\t' read -r surface qual bare; do
        if [ -n "$ONLY" ]; then
            case ",$ONLY," in *",$bare,"*|*",$qual,"*|*",$surface/$bare,"*) ;; *) continue ;; esac
        fi
        while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$JOBS" ]; do sleep 1; done
        run_one "$surface" "$qual" "$bare" &
        CALLS=$((CALLS + 1))
    done < <(printf '%s\n' "$TARGET_LINES")
    wait
    if [ "$CALLS" -eq 0 ]; then
        echo "live pass FAILED: no target selected (--only matched nothing)"
        FAILED=1
    else
        # with --only the run is partial: only the runs that were made are judged
        "$E2E_PYTHON" -I "$PYMOD" live --plugin "$PLUGIN" --events-dir "$EVENTS" ${ONLY:+--partial} ${ALLOWLIST:+--allowlist "$ALLOWLIST"}
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
    echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- no live run produced a result; $PLUGIN, $CALLS claude call(s), ${WALL}s"
    exit 2
fi
if [ "$KIND" != APPLICABLE ]; then
    echo "PLUGIN OUT-OF-REPO GATE NOT-APPLICABLE -- $PLUGIN has no agents, commands, skills or resource markdown (hook markdown files: $HOOK_FILES); the static pass over its hook markdown is clean, ${WALL}s"
    exit 0
fi
if [ "$STATIC_ONLY" -eq 1 ]; then
    echo "PLUGIN OUT-OF-REPO GATE PASSED (static pass only) -- $PLUGIN, ${WALL}s"
elif [ -n "$LIVE_NA" ]; then
    echo "PLUGIN OUT-OF-REPO GATE PASSED (static pass; live pass not applicable: $LIVE_NA) -- $PLUGIN, ${WALL}s"
else
    echo "PLUGIN OUT-OF-REPO GATE PASSED -- $PLUGIN, $CALLS claude call(s), ${WALL}s"
fi
exit 0
