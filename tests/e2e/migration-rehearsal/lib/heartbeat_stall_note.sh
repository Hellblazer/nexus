# SPDX-License-Identifier: AGPL-3.0-or-later
# shellcheck shell=bash
#
# nexus-wo6sc: name a heartbeat stall when one is in the window, instead of
# sending the reader after supervisor setup.
#
# Measured 2026-09-12 (release battery, --max-parallel 4). Two heartbeat
# ticks blew the 15 s lease TTL at 19.098 s and 31.622 s, so every client
# resolved "endpoint not resolvable" while the service was alive and well.
# The package-upgrade gate reported that as:
#
#   Error: nexus-service endpoint is not resolvable ... start the supervisor
#   with 'nx daemon service start'
#
# The supervisor was already running. The gate then printed the missed-TTL
# ERROR lines itself, thirty lines further down, inside its own diag dump --
# it reads the supervisor log competently one stage later than the failure
# it misattributes. So this is not new plumbing; it is doing at the failure
# site what the harness already does at the next stage.
#
# Emits nothing when no missed-TTL tick is present, so a genuine
# supervisor-setup failure keeps its own (correct) message.

# Print a one-line attribution when the supervisor log shows a missed-TTL
# heartbeat. Silent (rc 0, no output) when the log is unreadable or clean.
#
#   $1  optional: supervisor log path
#       (default "$HOME/.config/nexus/logs/storage_service.log")
heartbeat_stall_note() {
  local log="${1:-$HOME/.config/nexus/logs/storage_service.log}"
  [ -r "$log" ] || return 0

  local hits
  hits="$(grep -c 'storage_service_heartbeat_missed_ttl' "$log" 2>/dev/null || true)"
  hits="${hits//[!0-9]/}"
  [ -n "$hits" ] && [ "$hits" -gt 0 ] || return 0

  local last
  last="$(grep 'storage_service_heartbeat_missed_ttl' "$log" 2>/dev/null | tail -1)"

  printf 'LIKELY CAUSE — heartbeat stall, NOT supervisor setup: %s missed-TTL tick(s) in %s. The supervisor was alive; its own lease stamp overran the TTL, so every client read the endpoint as absent until the next stamp. Ignore the "start the supervisor" remedy above. Last tick: %s\n' \
    "$hits" "$log" "$last"

  # nexus-wo6sc: the sub-phase breakdown is what says WHICH of the stamp's
  # four parts stalled -- a slow syscall phase is a filesystem stall, while
  # time in "stamp.unaccounted" is the supervisor losing the CPU. Print it
  # when the tick carried one; older engines log a bare "stamp".
  case "$last" in
    *stamp.unaccounted*)
      printf 'Read stamp.* in that line: time inside a syscall phase is a filesystem stall; time in stamp.unaccounted is descheduling.\n'
      ;;
    *)
      printf 'That tick predates stamp sub-phasing (nexus-wo6sc), so it names no syscall — cause not determinable from this line alone.\n'
      ;;
  esac
}
