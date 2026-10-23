#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Regenerate src/nexus/_install/constraints.txt from uv.lock (nexus-2k66t.4.1).
#
# That file ships INSIDE the wheel and every install path hands it to
# `uv pip install --constraints`, so an install resolves the versions the
# release's lock resolved instead of whatever PyPI serves today (the
# nexus-l2ku5 class: mcp 2.0.0 killed both MCP servers for four days).
#
# Run it after ANY change to uv.lock (a dependency bump, `uv sync`, the
# release's Step 5 lock refresh) and commit the result.
# tests/test_install_constraints_pin.py fails the default suite when the two
# disagree, so a stale file cannot reach a tag.
#
# What the file keeps and drops, and why:
#   * `--frozen`: never re-resolves; the lock as committed is the source.
#   * `--no-emit-project`: conexus itself is not constrained (the installer
#     names the version it installs), so a version bump does not change the
#     file and a release does not need to regenerate it for that alone.
#   * `--no-dev --all-extras`: runtime set plus every optional extra, so an
#     install that adds `[local]` is constrained too. A constraint never adds
#     a package; it only bounds one that something else requests.
#   * markers stay (`; sys_platform == 'linux'`), so one file serves every
#     platform and Python in the lock.
#   * the `+cpu` local label is dropped from pins (`torch==2.8.0+cpu` becomes
#     `torch==2.8.0`). PEP 440 lets `==2.8.0` match `2.8.0+cpu` and
#     `2.8.0+cu130`, so the default CPU backend and an `NX_TORCH_BACKEND=auto`
#     GPU opt-in both still resolve.
#   * `--no-annotate --no-header`: no `# via` comments and no command echo, so
#     the file does not churn between uv versions.
set -euo pipefail

_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_out="$_root/src/nexus/_install/constraints.txt"
_tmp="$(mktemp "${TMPDIR:-/tmp}/nx-constraints-regen.XXXXXX")"
trap 'rm -f "$_tmp"' EXIT

uv export --frozen --no-hashes --no-annotate --no-header --no-emit-project \
    --no-dev --all-extras --directory "$_root" > "$_tmp"

# Non-vacuity: an export that produced nothing must not overwrite the file.
_pins="$(grep -c '==' "$_tmp" || true)"
if [ "${_pins:-0}" -lt 100 ]; then
    echo "regen-install-constraints: uv export produced only ${_pins:-0} pins from $_root/uv.lock; refusing to write" >&2
    exit 1
fi

{
    echo "# Generated from uv.lock by scripts/regen-install-constraints.sh (nexus-2k66t.4.1). Do not edit."
    echo "# install_generation.sh and generation_core.py pass this to uv pip install --constraints."
    sed -E 's/^([A-Za-z0-9._-]+==[0-9][^ ;+]*)\+cpu/\1/' "$_tmp"
} > "$_out"

echo "wrote $_out ($_pins pins)"
