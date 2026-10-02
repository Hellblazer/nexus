#!/bin/bash
# Runner job-completed hook: leave the internal disk as we found it. Never fails the job.
C=/Volumes/Bulk/ghrunner/build-caches
export COLIMA_HOME=$C/colima PATH=/opt/homebrew/bin:$PATH
sock="unix://$COLIMA_HOME/default/docker.sock"
if colima status >/dev/null 2>&1; then
  docker --host "$sock" system prune -af --volumes >/dev/null 2>&1 && echo "docker pruned"
  colima stop >/dev/null 2>&1 && echo "colima stopped"
fi
find "$C/tmp.noindex" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null && echo "tmp cleared"
UV_CACHE_DIR=$C/uv uv cache prune --ci >/dev/null 2>&1 && echo "uv cache pruned"
m2kb=$(du -sk "$C/m2" 2>/dev/null | cut -f1); m2kb=${m2kb:-0}
if (( m2kb > 5*1024*1024 )); then rm -rf "$C/m2" && mkdir -p "$C/m2" && echo "m2 reset (was $((m2kb/1024)) MB)"; fi
exit 0
