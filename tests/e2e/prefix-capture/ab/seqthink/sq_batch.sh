#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   sq_batch.sh <REPS> <job>...   job = ARM:GUIDE:prompt, e.g. C:G2:p1_t1fallback
# Results: ab/seqthink/out/<ARM>-<GUIDE>/<prompt>/r<rep>/
set -uo pipefail
REPS=${1:?REPS}; shift
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/../lab_grant.sh"
lab_docker_flags 1   # one container at a time (nexus-kani0)
HARNESS=$(cd "$HERE/../.." && pwd)
WT=$(cd "$HARNESS/../../.." && pwd)
IMAGE=${IMAGE:-nexus-ab-seeded:2.1.300}
mkdir -p "$HERE/out"
for rep in $(seq 1 "$REPS"); do
  for job in "$@"; do
    IFS=: read -r ARM GUIDE NAME <<< "$job"
    RUN="r${rep}"
    OUT="$HERE/out/${ARM}-${GUIDE}/${NAME}/${RUN}"
    if [ -s "$OUT/exit.txt" ]; then continue; fi
    mkdir -p "$OUT" && chmod 777 "$OUT"
    echo "$(date -u +%FT%TZ) start $job $RUN" >> "$HERE/out/progress"
    docker run --rm "${LAB_DOCKER_FLAGS[@]+"${LAB_DOCKER_FLAGS[@]}"}" --name "nxsq-${ARM}-${GUIDE}-${NAME}-${RUN}" \
      -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 -e ARM="$ARM" -e GUIDE="$GUIDE" \
      -e PROMPT="/harness/ab/seqthink/prompts/${NAME}.txt" \
      -e ANTHROPIC_MODEL="${AB_MODEL:-claude-opus-5-5}" -e ENABLE_TOOL_SEARCH=true \
      -v "$WT:/src:ro" -v "$HARNESS:/harness:ro" -v "$OUT:/out" \
      --entrypoint /bin/bash "$IMAGE" /harness/ab/seqthink/sq_in_container.sh \
      > "$OUT/docker.log" 2>&1
    echo "$(date -u +%FT%TZ) done $job $RUN rc=$?" >> "$HERE/out/progress"
  done
done
echo "$(date -u +%FT%TZ) batch finished" >> "$HERE/out/progress"
