#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# ripgrep for a CI job, without depending on apt.
#
# On 2026-11-11 GitHub-hosted ubuntu-latest could not resolve archive.ubuntu.com or
# security.ubuntu.com, and a bare `sudo apt-get install -y ripgrep` failed the integration and
# docs-reader jobs, and pytest-gate with them (CI 43033435294). In order:
#   1. an rg already on PATH (the lab pool has one and no sudo) is used as is;
#   2. the pinned GitHub release is downloaded, its SHA-256 checked against the value below, and
#      its directory added to GITHUB_PATH; a mismatch is never installed;
#   3. apt, tried three times; if that fails too, the step fails naming all three.
# Pinned: ripgrep 14.1.1 (published 2024-09-09). Checksums from the release's own .sha256 files.
set -uo pipefail

RG_VERSION="14.1.1"
SHA_X86_64="${NX_RG_SHA256_X86_64:-4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e}"
SHA_AARCH64="${NX_RG_SHA256_AARCH64:-c827481c4ff4ea10c9dc7a4022c8de5db34a5737cb74484d62eb94a95841ab2f}"

if command -v rg >/dev/null 2>&1; then
  echo "ripgrep already on PATH: $(command -v rg)"
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64) target="x86_64-unknown-linux-musl"; want="$SHA_X86_64" ;;
  aarch64|arm64) target="aarch64-unknown-linux-gnu"; want="$SHA_AARCH64" ;;
  *) target=""; want="" ;;
esac

if [ -n "$target" ]; then
  name="ripgrep-${RG_VERSION}-${target}"
  dest="${RUNNER_TEMP:-/tmp}/ripgrep"
  mkdir -p "$dest"
  if curl -fsSL --retry 3 --connect-timeout 10 -o "$dest/$name.tar.gz" \
      "https://github.com/BurntSushi/ripgrep/releases/download/${RG_VERSION}/${name}.tar.gz"; then
    got="$(sha256sum "$dest/$name.tar.gz" 2>/dev/null | cut -d' ' -f1)"
    [ -n "$got" ] || got="$(shasum -a 256 "$dest/$name.tar.gz" | cut -d' ' -f1)"
    if [ "$got" = "$want" ]; then
      tar -xzf "$dest/$name.tar.gz" -C "$dest"
      echo "$dest/$name" >> "$GITHUB_PATH"
      echo "ripgrep $RG_VERSION installed from the pinned GitHub release ($target)"
      exit 0
    fi
    echo "::warning::ripgrep release checksum mismatch (got $got, want $want); not installing it"
  else
    echo "::warning::could not download ripgrep $RG_VERSION from GitHub; trying apt"
  fi
fi

for attempt in 1 2 3; do
  if sudo apt-get install -y ripgrep; then
    echo "ripgrep installed from apt (attempt $attempt)"
    exit 0
  fi
  [ "$attempt" -lt 3 ] && sleep "${NX_RG_APT_RETRY_SLEEP:-10}"
done
echo "::error::ripgrep could not be installed: not on PATH, the pinned GitHub release failed, and apt failed three times"
exit 1
