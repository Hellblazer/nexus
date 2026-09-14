# SPDX-License-Identifier: AGPL-3.0-or-later
# shellcheck shell=bash
#
# nexus-mfage: the two artifact-staging seams every migration-rehearsal leg
# uses. Extracted to its own sourced file (nexus-og52j) so a unit test can
# drive stage_native directly against a fixture directory instead of only
# through the full run.sh entrypoint, which needs Docker plus a leg
# selection to reach this code.
#
# Callers set $ARTIFACTS / $ARTIFACT_WHEEL before sourcing this file when
# running in build-artifacts.sh manifest mode; both are optional (empty in
# the default working-tree mode).

# stage_wheel <dest-dir>
#   Copies the wheel under test -- build-artifacts.sh's manifest wheel in
#   ARTIFACTS mode, else the freshest dist/conexus-*.whl -- preserving its
#   real PEP 427 name.
stage_wheel() {
  if [ -n "$ARTIFACTS" ]; then cp "$ARTIFACT_WHEEL" "$1/"
  else cp "$(ls -t dist/conexus-*.whl | head -1)" "$1/"; fi   # keep real PEP 427 name
}

# stage_native <dest-dir>
#   Copies the native candidate binary -- and ONLY the binary.
#
#   nexus-og52j: prior to nexus-223oj, a LOCAL -Pnative -Ob quick build also
#   emitted native-image .so siblings (libawt/libawt_headless/libawt_xawt/
#   libjavajpeg/liblcms, plus libjava/libjvm) that this function silently
#   staged alongside the binary. Every rehearsal then ran the candidate WITH
#   libraries the release never ships -- engine-service-release.yml's "Stage
#   artifact + sha256" step copies ONLY dist/$ASSET (the bare executable) to
#   the release, no .so is ever uploaded -- so no gate could observe the
#   "release binary depends on a .so it doesn't upload" failure mode by
#   construction; the harness proved the binary worked with libraries that
#   reach no user. nexus-223oj cut the AWT reachability roots that produced
#   those siblings (measured: 7 .so files emitted in the control build, 0 in
#   the fixed build) -- a correct build today ships the executable alone.
#
#   So this now FAILS LOUD on any .so found next to the binary instead of
#   copying it in: if native-image ever starts emitting one again (a
#   reachability regression, a GraalVM/library upgrade), that must break
#   this staging step -- pre-tag, locally -- not silently diverge from what
#   the published release actually ships.
stage_native() {
  local src="service/target"
  [ -n "$ARTIFACTS" ] && src="$ARTIFACTS/native"
  mkdir -p "$1"
  cp "$src/nexus-service" "$1/"
  if compgen -G "$src"/*.so > /dev/null; then
    echo "FATAL: $src holds .so sibling(s) alongside nexus-service, but the published release ships the executable ALONE (engine-service-release.yml stages only dist/\$ASSET -- no .so is uploaded there). Staging one here would run the rehearsal candidate with a library the release never ships, exactly the gap nexus-og52j closed. Offending file(s): $(ls "$src"/*.so)" >&2
    exit 1
  fi
}
