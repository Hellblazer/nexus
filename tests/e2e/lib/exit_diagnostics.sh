#!/usr/bin/env bash
# tests/e2e/lib/exit_diagnostics.sh, silent-exit guard (nexus-f2g8u).
#
# tests/e2e/migration-rehearsal/run.sh was observed to exit 1 after a
# SUCCESSFUL wheel build with zero output on either stream (2026-09-02,
# NEXUS_TARGET_RELEASE=7.27.0 --package-upgrade, see the bead for the full
# incident). The wheel-build block's own failure path already names itself
# (run.sh's "uv build --wheel FAILED" echo before its `exit 1`); the gap is
# every OTHER command between there and the next step that logs: an
# ordinary command failing under `set -euo pipefail` with no explicit check
#, e.g. the disk-pressure preflight's `docker system df | awk ...`
# assignment failing because the docker daemon hiccuped, exits the whole
# harness with no diagnosis at all. run.sh's own header comment records the
# identical failure SHAPE recurring once already (the 2026-07-25 v0.1.55
# acquire-gate death, logging only its own banner).
#
# Usage (source once, as early as possible, right after `set -euo
# pipefail`, before any other trap is installed):
#
#   source "$SCRIPT_DIR/../lib/exit_diagnostics.sh"
#   diag_arm_err_trap
#
# Every EXIT trap the caller (re)installs later in its lifetime, this repo
# chains cleanup by re-issuing `trap '...' EXIT` wholesale rather than
# using bash's (nonexistent) additive trap semantics, must call
# diag_exit_guard FIRST in the new trap body, e.g.:
#
#   trap 'diag_exit_guard; existing_cleanup_1; existing_cleanup_2' EXIT
#
# so the diagnostic still fires no matter how far execution got before the
# reassignment, and existing cleanup is chained rather than clobbered.
#
# MECHANISM: an ERR trap, propagated into functions and command
# substitutions via `set -o errtrace` (diag_arm_err_trap sets it), records
# the line and command of the last command bash observed fail. Confirmed
# empirically (bash 5.x, 2026-09-05): this fires correctly inside a named
# function's body AND for a failing pipeline stage feeding a `var=$(...)`
# assignment under `pipefail`, precisely the two shapes the suspected
# 7.27.0 failure and its docker-preflight sibling take. diag_exit_guard
# runs from the caller's own EXIT trap; it prints that record to stderr
# whenever the script is exiting non-zero, so a silent non-zero exit is no
# longer possible: worst case (a direct `exit N` with no command failure
# observed since the last one, e.g. one of run.sh's own
# `echo "FATAL: ..."; exit 2` argument guards) it names "no failing command
# recorded" rather than staying silent; best case (an unguarded command
# failing under errexit) it names the exact line and command.
#
# diag_exit_guard never itself calls `exit`, bash preserves the original
# `exit N` (or errexit-triggered) status through an EXIT trap unless the
# trap explicitly calls `exit` with a different code, so it must not.

_DIAG_ERR_LINE=""
_DIAG_ERR_CMD=""

# Arms the ERR trap. Call once, as early as possible in the caller.
diag_arm_err_trap() {
  set -o errtrace
  # shellcheck disable=SC2064
  trap 'diag_record_err "$LINENO" "$BASH_COMMAND"' ERR
}

# Invoked by the armed ERR trap; not normally called directly.
diag_record_err() {
  _DIAG_ERR_LINE="$1"
  _DIAG_ERR_CMD="$2"
}

# Call FIRST in every EXIT trap the caller installs.
diag_exit_guard() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ -n "$_DIAG_ERR_LINE" ]; then
      echo "FATAL: run.sh exiting $rc: last command bash observed fail was line $_DIAG_ERR_LINE: $_DIAG_ERR_CMD" >&2
    else
      echo "FATAL: run.sh exiting $rc with no failing command recorded (a direct \`exit $rc\` with nothing tracked since the last recorded failure, if any)" >&2
    fi
  fi
  return "$rc"
}
