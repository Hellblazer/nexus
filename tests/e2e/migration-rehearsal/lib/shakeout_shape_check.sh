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
# after run.sh's own native-build step, against the just-built candidate
# -- not inside rehearse_shakeout.sh.
#
# JVM-jar shim on a non-Linux host (first real --shakeout on a macOS host,
# 2026-09-13): run.sh's -Ob build runs inside a Linux container there, so
# the candidate is a Linux ELF this host cannot exec, and native-smoke.sh
# died at boot with "Exec format error" before phase (a) proved anything.
# Phase (a) tests the checkout/guard classification, which
# check_release_workflow_shape.py's own docstring calls orthogonal to
# native versus JVM, so when the host cannot run the candidate the check
# boots the same build's JVM jar through a shim instead. On a Linux host
# the real candidate still runs. Phase (b) never runs the binary.
#
# Extracted to its own function (same shape as lib/stage_artifacts.sh,
# lib/heartbeat_stall_note.sh) so a unit test can drive it directly against
# a stubbed `uv`, without paying for run.sh's real multi-minute native
# build.
#
#   shakeout_release_workflow_shape_check <native-binary-path> [<jvm-jar-path>]
#
# Runs scripts/check_release_workflow_shape.py against *native-binary-path*
# (or, when this host cannot execute it, a shim over *jvm-jar-path*; no jar
# there is a FAILED verdict, never a skip), prints exactly one verdict line
# either way so a reader
# scanning --shakeout's output sees this step ran, and returns the check's
# own exit code -- the caller gates --shakeout's exit code on that (`||
# exit 1`), never swallows it.
#   shakeout_shape_needs_jvm_shim <binary> <host-os>
# Returns 0 when *binary* is an ELF and *host-os* (uname -s) is not Linux.
shakeout_shape_needs_jvm_shim() {
  local bin="$1" host_os="$2" kind
  [ "$host_os" = Linux ] && return 1
  kind="$(file -b "$bin" 2>/dev/null)"
  [[ "$kind" == ELF* ]]
}

#   shakeout_shape_write_jvm_shim <jar> <dir>
# Writes an executable shim into *dir* that boots *jar* and prints its path.
# native-smoke.sh starts $BIN with -D system properties; java only honours
# them before -jar, so the shim moves every -D argument ahead of it.
shakeout_shape_write_jvm_shim() {
  local jar="$1" dir="$2" shim
  shim="$dir/nexus-service-jvm-shim"
  cat > "$shim" <<EOF
#!/usr/bin/env bash
jvm=(); app=()
for a in "\$@"; do
  case "\$a" in -D*) jvm+=("\$a") ;; *) app+=("\$a") ;; esac
done
exec java "\${jvm[@]}" -jar '$jar' "\${app[@]}"
EOF
  chmod +x "$shim"
  printf '%s\n' "$shim"
}

shakeout_release_workflow_shape_check() {
  local bin_path="$1" jar_path="${2:-}" check_bin="$1" shim_dir="" host_os rc
  host_os="$(uname -s)"
  if shakeout_shape_needs_jvm_shim "$bin_path" "$host_os"; then
    if [ -z "$jar_path" ] || [ ! -f "$jar_path" ]; then
      echo "[shakeout] release-workflow SHAPE check: FAILED -- the candidate $bin_path is a Linux binary this $host_os host cannot execute, and there is no JVM jar to boot instead (got '$jar_path')" >&2
      return 1
    fi
    shim_dir="$(mktemp -d "${TMPDIR:-/tmp}/nx-shape-shim.XXXXXX")"
    check_bin="$(shakeout_shape_write_jvm_shim "$jar_path" "$shim_dir")"
    echo "[shakeout] the candidate is a Linux binary this $host_os host cannot execute; phase (a) boots the same build's JVM jar ($jar_path) through $check_bin"
  fi
  echo "[shakeout] release-workflow SHAPE check (nexus-xihsm): checkout-shape native-smoke.sh + Docker-less Maven, against ${check_bin}…"
  uv run python scripts/check_release_workflow_shape.py --bin "$check_bin"
  rc=$?
  [ -n "$shim_dir" ] && rm -rf "$shim_dir"
  if [ "$rc" -eq 0 ]; then
    echo "[shakeout] release-workflow SHAPE check: PASSED"
    return 0
  fi
  echo "[shakeout] release-workflow SHAPE check: FAILED -- see output above" >&2
  return 1
}
