#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   pe_detach.sh <arm> <label> [RUNS] [CASE_GLOB]   run claude plugin eval on plugins/<arm>/conexus
#   detached on chas; results in out/pe-<label>/ (nexus-dg7i6.7). Launch through
#   tests/e2e/lib/claude_credentials.py run --remote chas-test -- bash <this> ...
HERE=$(cd "$(dirname "$0")" && pwd)
ARM=${1:?arm}; LABEL=${2:?label}; RUNS=${3:-3}; GLOB=${4:-}
OUT="$HERE/out/pe-$LABEL"; mkdir -p "$OUT" && chmod 777 "$OUT"
HARNESS=$(cd "$HERE/../.." && pwd)
# Bash runs under Claude Code's bubblewrap sandbox inside the container: the image adds
# bubblewrap and socat (nexus-ab-seeded-sbx, FROM nexus-ab-seeded + apt), and bwrap needs
# user namespaces and a proc mount, which docker's default seccomp, AppArmor and /proc
# masking refuse.
setsid -f docker run --rm --name "nxpe-$LABEL" \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --security-opt systempaths=unconfined \
  -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 -e TREE=/plugins/"$ARM"/conexus \
  -e RUNS="$RUNS" -e CASE_GLOB="$GLOB" -e MAX_COST="${MAX_COST:-60}" -e CONC="${CONC:-2}" \
  -e ANTHROPIC_MODEL="${AB_MODEL:-claude-opus-5-5}" \
  -v "$HARNESS:/harness:ro" -v "$HERE/plugins:/plugins:ro" -v "$OUT:/out" \
  --entrypoint /bin/bash ${PE_IMAGE:-nexus-ab-seeded-sbx:2.1.300} /harness/ab/skillroute/pe_in_container.sh \
  > "$OUT/docker.log" 2>&1 < /dev/null
echo "detached pe-$LABEL"
