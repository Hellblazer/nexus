#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin-out-of-repo-gate.sh -- run the conexus Claude Code plugin the
# way a user does: from a project that is NOT a nexus checkout (nexus-z7ddd).
#
#   tests/e2e/plugin-out-of-repo-gate.sh [--plugin DIR] [--static-only]
#                                        [--only <name>[,<name>...]] [--jobs N]
#                                        [--model M] [--timeout SECONDS]
#                                        [--out DIR] [--keep]
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
#   1. STATIC. Every file reference in agents/, commands/, skills/ and
#      resources/ markdown is classified after substituting ${CLAUDE_PLUGIN_ROOT}
#      (RESOLVES / RELATIVE-NEAR / REPO-ONLY / RELATIVE-DEAD / MISSING / OUTSIDE /
#      URL / PLACEHOLDER / PROJECT; the table and the rules are in
#      tests/e2e/lib/plugin_out_of_repo.py). REPO-ONLY, RELATIVE-DEAD, MISSING
#      and OUTSIDE fail.
#   2. LIVE. One `claude -p --plugin-dir <plugin>` per agent (dispatched through
#      the Agent tool) and per skill (loaded through the Skill tool), run from a
#      fresh mktemp directory that has no git repository above it. Each model
#      quotes every file reference in the text it was handed and Reads it. The
#      verdict comes from the Read results in the stream-json event stream: a
#      path that does not open, a relative target in an agent, a path read from
#      a nexus checkout outside the plugin, or a run that quotes nothing where
#      the static pass expects references, all fail.
#
# Exit: 0 both passes clean; 1 any failure; 2 claude is not on PATH (never a
# pass) or no live run produced a result and nothing else failed; 64 usage.
# --static-only skips the live pass and does not need claude.
#
# Isolation. The live runs use the real HOME (the sign-in lives there) but a
# scratch NEXUS_CONFIG_DIR and a dead NX_SERVICE_URL, `--strict-mcp-config`
# (no plugin MCP server starts) and `--setting-sources ""` (the installed copy
# of the plugin does not load beside the one under test), so no run writes to a
# live store. NX_GATE_LIVE_STORE=1 drops the config and service isolation.
# Nothing here prints a token, an env dump or a config file.
#
# Cost, measured on the 7.95.0 release candidate: see the bead. One call per
# agent (two model sessions each: the outer one and the subagent) and one per
# skill, NX_GATE_JOBS (default 4) at a time.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"

PLUGIN="$REPO_ROOT/conexus"
STATIC_ONLY=0
ONLY=""
JOBS="${NX_GATE_JOBS:-4}"
MODEL="${NX_GATE_MODEL:-sonnet}"
TIMEOUT="${NX_GATE_TIMEOUT:-420}"
BUDGET="${NX_GATE_MAX_USD:-2}"
OUT=""
KEEP=0

usage() { sed -n '4,9p' "$0" >&2; exit 64; }
while [ $# -gt 0 ]; do
    case "$1" in
        --plugin) [ $# -ge 2 ] || usage; PLUGIN="$2"; shift 2 ;;
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
[ -f "$PLUGIN/.claude-plugin/plugin.json" ] || { echo "not a plugin directory: $PLUGIN" >&2; exit 64; }
PLUGIN="$(cd "$PLUGIN" && pwd -P)"

CLAUDE=""
if [ "$STATIC_ONLY" -eq 0 ]; then
    CLAUDE="$(command -v "${NX_GATE_CLAUDE:-claude}" 2>/dev/null)" || CLAUDE=""
    if [ -z "$CLAUDE" ]; then
        echo "PLUGIN OUT-OF-REPO GATE UNVERIFIED -- the claude CLI is not on PATH; the live pass cannot run (use --static-only for the static pass alone)" >&2
        exit 2
    fi
fi
e2e_python_resolve 10 || exit 2
PYMOD="$HERE/lib/plugin_out_of_repo.py"

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
"$E2E_PYTHON" -I "$PYMOD" static --plugin "$PLUGIN" || FAILED=1

CALLS=0
if [ "$STATIC_ONLY" -eq 0 ]; then
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
    echo "== live pass (claude $("$CLAUDE" --version 2>/dev/null | head -1), model $MODEL, $JOBS at a time, cwd $SCRATCH) =="
    while IFS=$'\t' read -r surface qual bare; do
        if [ -n "$ONLY" ]; then
            case ",$ONLY," in *",$bare,"*|*",$qual,"*|*",$surface/$bare,"*) ;; *) continue ;; esac
        fi
        while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$JOBS" ]; do sleep 1; done
        run_one "$surface" "$qual" "$bare" &
        CALLS=$((CALLS + 1))
    done < <("$E2E_PYTHON" -I "$PYMOD" targets --plugin "$PLUGIN")
    wait
    if [ "$CALLS" -eq 0 ]; then
        echo "live pass FAILED: no target selected (--only matched nothing)"
        FAILED=1
    else
        # with --only the run is partial: only the runs that were made are judged
        "$E2E_PYTHON" -I "$PYMOD" live --plugin "$PLUGIN" --events-dir "$EVENTS" ${ONLY:+--partial}
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
if [ "$STATIC_ONLY" -eq 1 ]; then
    echo "PLUGIN OUT-OF-REPO GATE PASSED (static pass only) -- $PLUGIN, ${WALL}s"
else
    echo "PLUGIN OUT-OF-REPO GATE PASSED -- $PLUGIN, $CALLS claude call(s), ${WALL}s"
fi
exit 0
