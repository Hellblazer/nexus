#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   pe_detach.sh <arm> <label> [RUNS] [CASE_GLOB]   run claude plugin eval on plugins/<arm>/conexus
#   detached on chas; results in out/pe-<label>/ (nexus-dg7i6.7). Launch through
#   tests/e2e/lib/claude_credentials.py run --remote chas-test -- bash <this> ...
#   What detaches is the launcher, never the container (nexus-kani0): with lab-run on
#   PATH the detached process is `lab-run -c ${PE_CLASS:-lab-s} -- <this> --foreground`,
#   which queues, holds the grant while docker run runs in the foreground, and returns
#   it when the container exits; lab_grant.sh sizes the container to that grant. A
#   detached container (docker run -d, or setsid around docker run) would outlive it.
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "${1:-}" != "--foreground" ]; then
  ARM=${1:?arm}; LABEL=${2:?label}
  OUT="$HERE/out/pe-$LABEL"; mkdir -p "$OUT" && chmod 777 "$OUT"
  if command -v lab-run >/dev/null 2>&1; then
    setsid nohup lab-run -c "${PE_CLASS:-lab-s}" -- bash "$0" --foreground "$@" > "$OUT/lab-run.log" 2>&1 < /dev/null &
  else
    setsid nohup bash "$0" --foreground "$@" > "$OUT/lab-run.log" 2>&1 < /dev/null &
  fi
  echo "detached pe-$LABEL"
  exit 0
fi
shift
ARM=${1:?arm}; LABEL=${2:?label}; RUNS=${3:-3}; GLOB=${4:-}
OUT="$HERE/out/pe-$LABEL"
HARNESS=$(cd "$HERE/../.." && pwd)
# shellcheck source=/dev/null
. "$HERE/../lab_grant.sh"
lab_docker_flags 1   # one container (nexus-kani0)
# Bash runs under Claude Code's bubblewrap sandbox inside the container: the image adds
# bubblewrap and socat (nexus-ab-seeded-sbx, FROM nexus-ab-seeded + apt), and bwrap needs
# user namespaces and a proc mount, which docker's default seccomp, AppArmor and /proc
# masking refuse.
docker run --rm "${LAB_DOCKER_FLAGS[@]+"${LAB_DOCKER_FLAGS[@]}"}" --name "nxpe-$LABEL" \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --security-opt systempaths=unconfined \
  -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 -e TREE=/plugins/"$ARM"/conexus \
  -e RUNS="$RUNS" -e CASE_GLOB="$GLOB" -e KEEP_TEMP="${KEEP_TEMP:-}" -e MAX_COST="${MAX_COST:-100}" -e CONC="${CONC:-2}" \
  -e ANTHROPIC_MODEL="${AB_MODEL:-claude-opus-5-5}" \
  -v "$HARNESS:/harness:ro" -v "$HERE/plugins:/plugins:ro" -v "$OUT:/out" \
  --entrypoint /bin/bash ${PE_IMAGE:-nexus-ab-seeded-sbx:2.1.300} /harness/ab/skillroute/pe_in_container.sh \
  > "$OUT/docker.log" 2>&1 < /dev/null
