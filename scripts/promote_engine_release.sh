#!/usr/bin/env bash
# nexus-cl14i: publish an engine-service GitHub release only when every
# expected asset is attached. Called by engine-service-release.yml's
# promote-release job; kept as a file so the failure path can be driven
# locally with a stub `gh` (tests/scripts/test_promote_engine_release_sh.py).
#
# Two blocking checks before the draft flag flips (nexus-ujbz8 added the second):
#   1. every expected asset is attached;
#   2. every native binary is under its size ceiling
#      (scripts/check_engine_cut_riders.py, the one place the ceilings live).
# An oversized binary leaves a DRAFT here, where a re-run is cheap, instead of
# publishing an immutable tag that can only be fixed by a re-cut. To publish
# deliberately over a ceiling, raise it in that script with a reviewed change;
# publishing the draft by hand (gh release edit, draft flag off) is the human's
# override.
#
# Windows (RDR-224 P0.4, nexus-f9bgu.14): the Windows assets BLOCK promotion, but
# only while the repo variable NX_WINDOWS_RELEASE_LEGS is on, because with it off
# no Windows leg runs and waiting for its assets would hold every release a draft
# forever. The workflow normalises the variable to the literal "on" or "off" and
# passes it as the optional third argument; this script reads no GitHub state and
# no environment, so both states are driven by tests/scripts/. Omitted means off.
# Any other value is a wiring bug and exits 2 before gh is called, never "off".
#
# Usage: promote_engine_release.sh <tag> <owner/repo> [on|off]
# Exit 0: all assets present and sized, release promoted out of draft.
# Exit 1: assets missing or a binary over its ceiling; release left a DRAFT
#         (no consumer resolves it).
# Exit 2: the third argument is neither "on" nor "off"; nothing was read or changed.
set -euo pipefail
tag="${1:?tag}"
repo="${2:?owner/repo}"
windows="${3-off}"
case "$windows" in
  on|off) ;;
  *)
    echo "::error::third argument must be 'on' or 'off', got '$windows'"
    exit 2
    ;;
esac
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
expected=""
for arch in linux-amd64 linux-arm64 mac-arm64; do
  b="nexus-service-$arch"
  expected="$expected $b $b.sha256 $b.cosign.bundle $b.sigstore.json"
  p="nexus-pg-$arch.txz"
  expected="$expected $p $p.sha256 $p.sigstore.json"
done
if [ "$windows" = "on" ]; then
  # P0.4 layout: one archive and one .sha256 and one sigstore bundle per artifact.
  # The PG bundle (P2.2) is the only Windows asset so far; the engine archive
  # (P1.2, nexus-f9bgu.9) joins here, behind the same switch.
  p="nexus-pg-windows-x64.txz"
  expected="$expected $p $p.sha256 $p.sigstore.json"
fi
present="$(gh release view "$tag" --repo "$repo" --json assets --jq '.assets[].name')"
missing=""
# Pipe-free exact-line match: under pipefail, `printf | grep -q` can report
# the producer's SIGPIPE over a successful match (nexus-i66g4 class).
for a in $expected; do
  if [[ $'\n'"$present"$'\n' != *$'\n'"$a"$'\n'* ]]; then
    missing="$missing $a"
  fi
done
if [ -n "$missing" ]; then
  echo "::error::release $tag is missing assets:$missing -- leaving it a DRAFT (nexus-cl14i)"
  exit 1
fi
count="$(printf '%s\n' $expected | wc -l | tr -d ' ')"
echo "all $count expected assets present on $tag"
assets_json="$(mktemp "${TMPDIR:-/tmp}/promote-assets.XXXXXX")"
trap 'rm -f "$assets_json"' EXIT
gh release view "$tag" --repo "$repo" --json assets > "$assets_json"
if ! python3 "$HERE/check_engine_cut_riders.py" sizes "$tag" --assets-json "$assets_json"; then
  echo "::error::release $tag has a native binary over its size ceiling (or the sizes could not be read) -- leaving it a DRAFT (nexus-ujbz8)"
  exit 1
fi
gh release edit "$tag" --repo "$repo" --draft=false
echo "published $tag"
