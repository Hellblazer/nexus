#!/bin/bash
# Runner job-started hook (hellmini-ci): bring up ghcis own Docker VM only for the jobs duration.
# --disk 40 caps the VM image; it only takes effect when the VM is (re)created.
set -euo pipefail
export COLIMA_HOME=/Volumes/Bulk/ghci/build-caches/colima PATH=/opt/homebrew/bin:$PATH
# Diagnostic (2026-10-02, non-fatal): host TCP sockets in the colima published-port band 32768-49151.
# A forward leaked by another colima VM pins a port here; see T2 nexus/hellmini-second-test-host-howto.
{ pinned=$(/usr/sbin/netstat -anv -p tcp 2>/dev/null | awk '{n=split($4,a,"."); p=a[n]+0; if (p>=32768 && p<=49151) print}'); echo "host ports 32768-49151 in use: $(printf '%s' "$pinned" | grep -c . || true)"; [ -n "$pinned" ] && printf '%s\n' "$pinned"; } || true
# Overlap guard: wait (bounded, 20 min) while ghrunner has a job or a nexus suite/build lease is held.
bash "$(dirname "$0")/wait-for-host.sh" || true
if ! colima status >/dev/null 2>&1; then
  colima start --cpu 8 --memory 8 --disk 40 --vm-type vz --mount-type virtiofs
fi
docker --host "unix://$COLIMA_HOME/default/docker.sock" info --format "docker ready: {{.ServerVersion}}"
