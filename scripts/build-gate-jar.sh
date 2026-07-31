#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build the shaded service JAR that the -m integration gates boot, with
# release_version STAMPED so the client's cloud version probe is satisfied
# legitimately.
#
# WHY THIS EXISTS (nexus-ao29z). release.properties ships release_version BLANK
# by design — it is stamped only at native-release time from the
# engine-service-vX.Y.Z tag — so a plain `mvn package` produces a JAR that
# reports release_version=null on /version. Since the fail-loud cloud probe
# landed (3cb14f96, 2026-07-09) the HttpVectorClient connection path fail-closes
# on exactly that, so every gate that boots a locally built JAR and drives the
# vector client errored at SETUP:
#
#   tests/db/test_http_combined_query_integration.py
#   tests/db/test_write_seam_gate_integration.py
#   tests/db/test_indexer_seam_b_integration.py
#   tests/db/test_frecency_enehl_integration.py
#
# CI's seam job hit this the same day and solved it by stamping (ci.yml, "Stamp
# release_version into the gate JAR"), deliberately NOT by bypassing the probe:
# the gate then exercises a CONFORMANT engine and the hardening stays intact.
# This script is that same step for a developer's machine, so a local run proves
# what CI proves.
#
# The stamp is taken from REQUIRED_ENGINE_VERSION in src/nexus/engine_version.py
# — the ONE floor constant — so it cannot drift when the pin bumps.
#
# The working tree is left CLEAN: release.properties is restored on exit,
# including on failure or interrupt.
#
#   usage: scripts/build-gate-jar.sh [extra mvn args...]

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
props="$repo_root/service/src/main/resources/META-INF/nexus/release.properties"

test -f "$props" || { echo "release.properties missing at $props" >&2; exit 1; }

ver=$(cd "$repo_root" && python3 -c "
import pathlib, re
s = pathlib.Path('src/nexus/engine_version.py').read_text()
m = re.search(r'REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)', s)
assert m, 'REQUIRED_ENGINE_VERSION not parseable — fix the regex before stamping'
print('.'.join(m.groups()))
")

backup="$(mktemp)"
cp "$props" "$backup"
# Restore on ANY exit path: a stamped release.properties left in the tree is a
# tracked-file modification that would follow the developer into their next
# commit, and it would make a subsequent `mvn package` silently produce a
# release-looking JAR.
trap 'cp "$backup" "$props"; rm -f "$backup"' EXIT

tmp="$(mktemp)"
grep -v '^release_version=' "$props" > "$tmp"
printf 'release_version=%s\n' "$ver" >> "$tmp"
mv "$tmp" "$props"
echo "stamped release_version=$ver"

cd "$repo_root/service"
./mvnw -q package -DskipTests "$@"

echo "built $(ls -1 "$repo_root"/service/target/nexus-service-*.jar | grep -v original)"
