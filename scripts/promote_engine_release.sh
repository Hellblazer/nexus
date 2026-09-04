#!/usr/bin/env bash
# nexus-cl14i: publish an engine-service GitHub release only when every
# expected asset is attached. Called by engine-service-release.yml's
# promote-release job; kept as a file so the failure path can be driven
# locally with a stub `gh` (tests/scripts/test_promote_engine_release_sh.py).
#
# Usage: promote_engine_release.sh <tag> <owner/repo>
# Exit 0: all assets present, release promoted out of draft.
# Exit 1: assets missing; release left a DRAFT (no consumer resolves it).
set -euo pipefail
tag="${1:?tag}"
repo="${2:?owner/repo}"
expected=""
for arch in linux-amd64 linux-arm64 mac-arm64; do
  b="nexus-service-$arch"
  expected="$expected $b $b.sha256 $b.cosign.bundle $b.sigstore.json"
  p="nexus-pg-$arch.txz"
  expected="$expected $p $p.sha256 $p.sigstore.json"
done
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
gh release edit "$tag" --repo "$repo" --draft=false
echo "published $tag"
