#!/usr/bin/env bash
# Mirror a HOME, shadowing exactly one path (nexus-pfuns follow-up).
#
# Sourced by tests/e2e/local-service-gate.sh and exercised directly by
# tests/test_gate_fences_the_real_config_dir.py. It lives here rather than
# inline in the gate so the TEST DRIVES THE REAL IMPLEMENTATION instead of a
# copy of it — a fence verified against a reimplementation is not verified.
#
# WHY A DENYLIST. The first attempt symlinked a hand-picked set
# (.cache/.local/.claude) into a fresh HOME and broke on the second thing it
# touched: the Maven jar rebuild died because ~/.testcontainers.properties
# (carrying testcontainers.ryuk.disabled=true) and ~/.docker (holding the
# socket at ~/.docker/run/docker.sock) were absent, so testcontainers fell back
# to its ryuk-enabled default and could not reach the daemon. The set of
# "things $HOME is for" cannot be completed by enumeration. So: mirror
# everything, shadow one path.

# fence_home <real_home> <gate_home> [shadow_relpath]
#
# Symlinks every top-level entry of <real_home> into <gate_home>, except the
# first component of <shadow_relpath>; that component is recreated as a real
# directory whose own entries are symlinked through except the leaf, which
# becomes a fresh empty directory. Default shadow: .config/nexus
fence_home() {
    local real_home="$1" gate_home="$2" shadow="${3:-.config/nexus}"
    local shadow_top="${shadow%%/*}" shadow_leaf="${shadow#*/}"
    local entry base

    mkdir -p "$gate_home/$shadow_top"

    local had_dotglob had_nullglob
    shopt -q dotglob && had_dotglob=1 || had_dotglob=0
    shopt -q nullglob && had_nullglob=1 || had_nullglob=0
    shopt -s dotglob nullglob

    for entry in "$real_home"/*; do
        base="$(basename "$entry")"
        [ "$base" = "$shadow_top" ] && continue
        ln -sfn "$entry" "$gate_home/$base"
    done
    for entry in "$real_home/$shadow_top"/*; do
        base="$(basename "$entry")"
        [ "$base" = "$shadow_leaf" ] && continue
        ln -sfn "$entry" "$gate_home/$shadow_top/$base"
    done

    [ "$had_dotglob" = "1" ] || shopt -u dotglob
    [ "$had_nullglob" = "1" ] || shopt -u nullglob

    mkdir -p "$gate_home/$shadow"
}
