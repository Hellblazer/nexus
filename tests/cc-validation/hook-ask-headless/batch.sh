#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-mcfaj: run the headless hook-ask matrix on chas, one container per run.
# Started by run.sh through
#   tests/e2e/lib/claude_credentials.py run --remote chas-test -- ...
# so CLAUDE_CODE_OAUTH_TOKEN is in this process's environment (never printed).
#   batch.sh [run-id ...]      (default: every row of RUNS)
# Evidence: <this dir>/out/<run-id>/{stream.jsonl,hook.log,rc,elapsed,...}
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-nexus-cc-val:local}"
OUTROOT="${OUTROOT:-$HERE/out}"

[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || { echo "batch.sh: no automation token in the environment" >&2; exit 2; }

# id | permission flag (- = default mode) | hook verdict | style | allow rule (rule|norule)
# c_* rows are the controls: the SAME hook fires but emits no decision, so the
# command must run (an allow rule pre-approves it; the *_norule pair leaves
# only the auto-mode classifier to approve it). An a_* row differs from its
# control only by the ask verdict.
RUNS=(
    "c_default|-|none|direct|rule"
    "a_default|-|ask|direct|rule"
    "c_dontask|dontAsk|none|direct|rule"
    "a_dontask|dontAsk|ask|direct|rule"
    "c_auto|auto|none|direct|rule"
    "a_auto|auto|ask|direct|rule"
    "c_auto_norule|auto|none|direct|norule"
    "a_auto_norule|auto|ask|direct|norule"
    "a_bgsub|-|ask|bgsub|rule"
    "a_real|-|real|direct|rule"
)

want=("$@")
for row in "${RUNS[@]}"; do
    IFS='|' read -r id perm hook style rule <<< "$row"
    if [ "${#want[@]}" -gt 0 ]; then
        hit=0
        for w in "${want[@]}"; do [ "$w" = "$id" ] && hit=1; done
        [ "$hit" = 1 ] || continue
    fi
    out="$OUTROOT/$id"
    rm -rf "$out"
    mkdir -p "$out"
    chmod 777 "$out"
    echo "$(date -u +%FT%TZ) start $id"
    docker run --rm --name "nxmcfaj-$id" \
        -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 \
        -e ANTHROPIC_MODEL="${MCFAJ_MODEL:-haiku}" \
        -e CLAUDE_CODE_SUBAGENT_MODEL="${MCFAJ_MODEL:-haiku}" \
        -e RUN_TIMEOUT="${RUN_TIMEOUT:-240}" \
        -v "$HERE:/harness:ro" -v "$out:/out" \
        --entrypoint /bin/bash "$IMAGE" \
        /harness/in_container.sh "$id" "$perm" "$hook" "$style" "$rule" \
        > "$out/docker.log" 2>&1
    echo "$(date -u +%FT%TZ) done $id docker_rc=$?"
done
