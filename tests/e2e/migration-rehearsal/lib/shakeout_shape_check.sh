# SPDX-License-Identifier: AGPL-3.0-or-later
# shellcheck shell=bash
#
# nexus-xihsm (critic follow-up on the nexus-vpl9c riders, 2026-09-13):
# scripts/check_release_workflow_shape.py was run by nobody -- the exact
# "release-only procedure rots silently" class the check itself exists to
# close. --shakeout is the PRE-TAG candidate gate (nexus-mbeke) and,
# uniquely among the migration-rehearsal legs, runs its native build on the
# HOST -- the one place phase (a)'s checkout-shape non-vacuity assert can
# genuinely be True (this repo IS a checkout with .git + pyproject.toml)
# and phase (b) has service/mvnw + a JDK on hand at all.
#
# The --shakeout CONTAINER itself is the WRONG place to run this: it is a
# uv-tool-installed wheel with no .git/pyproject.toml ancestor by design
# (that absence is what nexus-xihsm's checkout-vs-installed-wheel class is
# ABOUT -- see service/native-smoke.sh's own header and
# scripts/check_release_workflow_shape.py's module docstring). Running
# phase (a) inside that container could only ever hit the non-vacuity
# refusal, never a real pass, and phase (b) has no service/ Java sources or
# Maven wrapper in there to run at all. So this runs on the HOST, right
# after run.sh's own native-build step, against the REAL just-built
# candidate (never a JVM-jar shim) -- not inside rehearse_shakeout.sh.
#
# Extracted to its own function (same shape as lib/stage_artifacts.sh,
# lib/heartbeat_stall_note.sh) so a unit test can drive it directly against
# a stubbed `uv`, without paying for run.sh's real multi-minute native
# build.
#
#   shakeout_release_workflow_shape_check <native-binary-path>
#
# Runs scripts/check_release_workflow_shape.py against *native-binary-path*
# (no JVM-jar shim), prints exactly one verdict line either way so a reader
# scanning --shakeout's output sees this step ran, and returns the check's
# own exit code -- the caller gates --shakeout's exit code on that (`||
# exit 1`), never swallows it.
shakeout_release_workflow_shape_check() {
  local bin_path="$1"
  echo "[shakeout] release-workflow SHAPE check (nexus-xihsm): checkout-shape native-smoke.sh + Docker-less Maven, against the just-built native candidate ($bin_path)…"
  if uv run python scripts/check_release_workflow_shape.py --bin "$bin_path"; then
    echo "[shakeout] release-workflow SHAPE check: PASSED"
    return 0
  fi
  echo "[shakeout] release-workflow SHAPE check: FAILED -- see output above" >&2
  return 1
}
