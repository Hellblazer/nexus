#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-ly9c2: run one arm's task list on chas. Started through
# tests/e2e/lib/claude_credentials.py run --remote chas-test, so
# CLAUDE_CODE_OAUTH_TOKEN is in this process's environment (never printed).
#   ab_batch.sh <ARM> <REPS> <task-name>...   (task names = files in ab/tasks/ without .txt)
# AB_MODEL picks the main model (default claude-opus-5-5); ANTHROPIC_MODEL is
# ignored because the credentials helper sets it to haiku on the remote side.
# Results: ab/out/<ARM>/<task>/r<rep>/ ; progress in ab/out/<ARM>.progress
set -uo pipefail
ARM=${1:?ARM}; REPS=${2:?REPS}; shift 2
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lab_grant.sh"
lab_docker_flags 1   # one container at a time (nexus-kani0)
HARNESS=$(dirname "$HERE")
WT=$(cd "$HARNESS/../../.." && pwd)
IMAGE=${IMAGE:-nexus-ab-seeded:2.1.300}
mkdir -p "$HERE/out"
for rep in $(seq 1 "$REPS"); do
  for task in "$@"; do
    OUT=$HERE/out/$ARM/$task/r$rep
    if [ -s "$OUT/result.json" ]; then continue; fi
    mkdir -p "$OUT" && chmod 777 "$OUT"
    echo "$(date -u +%FT%TZ) start $ARM $task r$rep" >> "$HERE/out/$ARM.progress"
    docker run --rm "${LAB_DOCKER_FLAGS[@]+"${LAB_DOCKER_FLAGS[@]}"}" --name "nxab-$ARM-$task-r$rep" \
      -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 -e ARM="$ARM" -e TASK_FILE="/harness/ab/tasks/$task.txt" \
      -e ANTHROPIC_MODEL="${AB_MODEL:-claude-opus-5-5}" -e ENABLE_TOOL_SEARCH=true \
      -v "$WT:/src:ro" -v "$HARNESS:/harness:ro" -v "$OUT:/out" \
      --entrypoint /bin/bash "$IMAGE" /harness/ab/ab_in_container.sh \
      > "$OUT/docker.log" 2>&1
    echo "$(date -u +%FT%TZ) done $ARM $task r$rep rc=$?" >> "$HERE/out/$ARM.progress"
  done
done
echo "$(date -u +%FT%TZ) ARM $ARM finished" >> "$HERE/out/$ARM.progress"
