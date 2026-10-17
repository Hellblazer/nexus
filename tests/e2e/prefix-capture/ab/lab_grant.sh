# SPDX-License-Identifier: AGPL-3.0-or-later
# Sourced by the A/B batch scripts (nexus-kani0). A container is started by
# dockerd, outside the lab-run grant's cgroup, so the scheduler cannot see or
# cap it. Under lab-run (LAB_MEM_MB exported) lab_docker_flags N sets
# LAB_DOCKER_FLAGS to pin each of N concurrent containers to the granted cores
# and an equal share of the memory cap, with no swap. Outside lab-run it sets
# nothing, so a plain docker run is unchanged. Expand it with the set -u safe
# form: "${LAB_DOCKER_FLAGS[@]+"${LAB_DOCKER_FLAGS[@]}"}".
lab_docker_flags() {
  LAB_DOCKER_FLAGS=()
  [ -n "${LAB_MEM_MB:-}" ] || return 0
  local n=${1:-1} cores mem
  cores=$(cat "/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup 2>/dev/null)/cpuset.cpus.effective" 2>/dev/null) || cores=""
  mem=$(( LAB_MEM_MB / n ))
  if [ -n "$cores" ]; then
    LAB_DOCKER_FLAGS+=("--cpuset-cpus=$cores")
  fi
  LAB_DOCKER_FLAGS+=("--memory=${mem}m" "--memory-swap=${mem}m")
}
