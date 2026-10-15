#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Arm C validation batch on chas. Started via claude_credentials.py run --remote.
#   ts_batch.sh <REPS> <job>...   job = ARM:MODE:name, e.g. C:single:subagent
#   (single mode prompt = prompts/<name>.txt; tuple mode name is free).
# Results: ab/tuplespace/out/<ARM>/<name>/r<rep>/
set -uo pipefail
REPS=${1:?REPS}; shift
HERE=$(cd "$(dirname "$0")" && pwd)
HARNESS=$(cd "$HERE/../.." && pwd)
WT=$(cd "$HARNESS/../../.." && pwd)
IMAGE=${IMAGE:-nexus-ab-seeded:2.1.300}
mkdir -p "$HERE/out"
for rep in $(seq 1 "$REPS"); do
  for job in "$@"; do
    IFS=: read -r ARM MODE NAME <<< "$job"
    OUT=$HERE/out/$ARM/$NAME/r$rep
    if [ -s "$OUT/exit.txt" ]; then continue; fi
    mkdir -p "$OUT" && chmod 777 "$OUT"
    echo "$(date -u +%FT%TZ) start $job r$rep" >> "$HERE/out/progress"
    docker run --rm --name "nxts-$ARM-$NAME-r$rep" \
      -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 -e ARM="$ARM" -e MODE="$MODE" \
      -e PROMPT="/harness/ab/tuplespace/prompts/$NAME.txt" \
      -e ANTHROPIC_MODEL="${AB_MODEL:-claude-opus-5-5}" -e ENABLE_TOOL_SEARCH=true \
      -v "$WT:/src:ro" -v "$HARNESS:/harness:ro" -v "$OUT:/out" \
      --entrypoint /bin/bash "$IMAGE" /harness/ab/tuplespace/ts_in_container.sh \
      > "$OUT/docker.log" 2>&1
    echo "$(date -u +%FT%TZ) done $job r$rep rc=$?" >> "$HERE/out/progress"
  done
done
echo "$(date -u +%FT%TZ) batch finished" >> "$HERE/out/progress"
