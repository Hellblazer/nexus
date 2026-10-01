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

# Paths ALWAYS shadowed with an empty real directory, whatever the caller's
# own shadow is (nexus-q81g7; twin of AUTOSTART_SHADOWS in tests/_fence_home.py,
# kept in step by tests/test_fence_home_twins_agree.py). They are the OS
# autostart-unit directories. Passed through, a gate's HOME resolves to the
# operator's REAL ~/.config/systemd/user and ~/Library/LaunchAgents, and the
# upgrade-finish convergence path rewrites and re-activates the real unit
# against the real service manager (destroyed qwentescence's unit 2026-09-30).
FENCE_AUTOSTART_SHADOWS=(".config/systemd" "Library/LaunchAgents")

# fence_home <real_home> <gate_home> [shadow_relpath]
#
# Symlinks every top-level entry of <real_home> into <gate_home>, except the
# first component of each shadow; that component is recreated as a real
# directory whose own entries are symlinked through except the shadowed
# leaves, which become fresh empty directories. The shadows are
# <shadow_relpath> (default .config/nexus) plus FENCE_AUTOSTART_SHADOWS.
fence_home() {
    local real_home="$1" gate_home="$2" shadow="${3:-.config/nexus}"
    local -a shadows=("$shadow" "${FENCE_AUTOSTART_SHADOWS[@]}")
    local rel top entry base is_top is_leaf

    mkdir -p "$gate_home"
    for rel in "${shadows[@]}"; do
        mkdir -p "$gate_home/${rel%%/*}"
    done

    local had_dotglob had_nullglob
    shopt -q dotglob && had_dotglob=1 || had_dotglob=0
    shopt -q nullglob && had_nullglob=1 || had_nullglob=0
    shopt -s dotglob nullglob

    for entry in "$real_home"/*; do
        base="$(basename "$entry")"
        is_top=0
        for rel in "${shadows[@]}"; do
            [ "$base" = "${rel%%/*}" ] && is_top=1
        done
        [ "$is_top" = "1" ] && continue
        ln -sfn "$entry" "$gate_home/$base"
    done

    for top in $(for rel in "${shadows[@]}"; do echo "${rel%%/*}"; done | sort -u); do
        for entry in "$real_home/$top"/*; do
            base="$(basename "$entry")"
            is_leaf=0
            for rel in "${shadows[@]}"; do
                if [ "${rel%%/*}" = "$top" ] && [ "${rel#*/}" = "$base" ]; then
                    is_leaf=1
                fi
            done
            [ "$is_leaf" = "1" ] && continue
            ln -sfn "$entry" "$gate_home/$top/$base"
        done
    done

    [ "$had_dotglob" = "1" ] || shopt -u dotglob
    [ "$had_nullglob" = "1" ] || shopt -u nullglob

    for rel in "${shadows[@]}"; do
        mkdir -p "$gate_home/$rel"
    done
}
