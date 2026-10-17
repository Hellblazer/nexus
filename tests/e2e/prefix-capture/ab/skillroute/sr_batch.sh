#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   sr_batch.sh <REPS> <ARM>... [-- <case>...]   ARM = S0|S1|S2|D|N|DT|NT; cases default to every conexus/evals case
# Results: ab/skillroute/out/<ARM>/<case>/r<rep>/
set -uo pipefail
REPS=${1:?REPS}; shift
ARMS=(); CASES=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do ARMS+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
HERE=$(cd "$(dirname "$0")" && pwd)
HARNESS=$(cd "$HERE/../.." && pwd)
WT=$(cd "$HARNESS/../../.." && pwd)
if [ $# -gt 0 ]; then CASES=("$@"); else
  for d in "$WT"/conexus/evals/*/; do CASES+=("$(basename "$d")"); done
fi
IMAGE=${IMAGE:-nexus-ab-seeded:2.1.300}
mkdir -p "$HERE/out"
for rep in $(seq 1 "$REPS"); do
  for arm in "${ARMS[@]}"; do
    for case in "${CASES[@]}"; do
      OUT="$HERE/out/$arm/$case/r$rep"
      if [ -s "$OUT/exit.txt" ]; then continue; fi
      mkdir -p "$OUT" && chmod 777 "$OUT"
      echo "$(date -u +%FT%TZ) start $arm $case r$rep" >> "$HERE/out/progress"
      docker run --rm --name "nxsr-$arm-$case-r$rep" \
        -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 -e ARM="$arm" -e CASE="$case" \
        -e ANTHROPIC_MODEL="${AB_MODEL:-claude-opus-5-5}" -e ENABLE_TOOL_SEARCH=true \
        -v "$WT:/src:ro" -v "$HARNESS:/harness:ro" -v "$HERE/plugins:/plugins:ro" -v "$OUT:/out" \
        --entrypoint /bin/bash "$IMAGE" /harness/ab/skillroute/sr_in_container.sh \
        > "$OUT/docker.log" 2>&1
      echo "$(date -u +%FT%TZ) done $arm $case r$rep rc=$?" >> "$HERE/out/progress"
    done
  done
done
echo "$(date -u +%FT%TZ) batch finished" >> "$HERE/out/progress"
