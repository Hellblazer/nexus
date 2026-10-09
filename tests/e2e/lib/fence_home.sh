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

# Shadows applied whatever the caller names (twin of ALWAYS_SHADOWS in
# tests/_fence_home.py, kept in step by tests/test_fence_home_twins_agree.py).
#   autostart units (nexus-q81g7): a mirrored ~/.config/systemd/user or
#     ~/Library/LaunchAgents lets the upgrade-finish convergence path rewrite
#     and re-activate the operator's REAL unit against the real service manager
#     (destroyed qwentescence's unit 2026-09-30);
#   interim writers: ~/.local/state, ~/.claude/agents, ~/.claude/projects;
#   credential stores, never mirrored: .aws .gnupg .kube .ssh (whole) and
#     .config/gh .config/op.
# A shadow is <top>/<leaf> or a bare <top>.
FENCE_ALWAYS_SHADOWS=(
    ".config/systemd" "Library/LaunchAgents"
    ".local/state" ".claude/agents" ".claude/projects"
    ".aws" ".gnupg" ".kube" ".ssh" ".config/gh" ".config/op"
)

# fence_home <real_home> <gate_home> [shadow_relpath]
#
# Symlinks every top-level entry of <real_home> into <gate_home>, except the
# first component of each shadow. A bare <top> shadow becomes an empty real
# directory; for <top>/<leaf> the <top> is recreated as a real directory whose
# own entries are symlinked through except the shadowed leaves, which become
# fresh empty directories. The shadows are <shadow_relpath> (default
# .config/nexus) plus FENCE_ALWAYS_SHADOWS.
fence_home() {
    local real_home="$1" gate_home="$2" shadow="${3:-.config/nexus}"
    local -a shadows=("$shadow" "${FENCE_ALWAYS_SHADOWS[@]}")
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

    # Only <top>/<leaf> shadows mirror entries through; a bare <top> stays empty.
    for top in $(for rel in "${shadows[@]}"; do
                     case "$rel" in */*) echo "${rel%%/*}" ;; esac
                 done | sort -u); do
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

# fence_home_env <gate_home>
#
# Make the OS user service manager unreachable from this process tree, and mark
# the tree as fenced for the installer's own refusal (NX_FENCED_HOME, read by
# nexus.daemon.installer so a real `nx` child refuses mutating launchctl/systemctl
# verbs). Twin of fence_manager_env in tests/_fence_home.py -- see its docstring
# for why: empty XDG_RUNTIME_DIR + no DBUS_SESSION_BUS_ADDRESS (LINUX ONLY),
# DOCKER_HOST pinned from a rootless socket before the override, never cleared.
# NX_FENCE_UNAME overrides `uname -s` so the twins test can exercise both arms.
fence_home_env() {
    local gate_home="$1"
    local uname_s="${NX_FENCE_UNAME:-$(uname -s)}"
    export NX_FENCED_HOME="$gate_home"
    unset DBUS_SESSION_BUS_ADDRESS
    [ "$uname_s" = "Linux" ] || return 0
    if [ -z "${DOCKER_HOST:-}" ] && [ -n "${XDG_RUNTIME_DIR:-}" ] \
        && [ -S "$XDG_RUNTIME_DIR/docker.sock" ]; then
        export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
    fi
    mkdir -p "$gate_home/xdg-runtime"
    export XDG_RUNTIME_DIR="$gate_home/xdg-runtime"
}

# fence_uv_env <real_home>
#
# Pin uv's HOME-derived roots to the REAL home before any uv call runs under
# the fenced one. Call it after HOME is exported. Twin of fence_uv_env in
# tests/_fence_home.py. An explicit outer value wins.
#
# UV_PYTHON_INSTALL_DIR (nexus-t0pke): ~/.local is a real directory in the
# mirror (.local/state is shadowed), so uv finds its managed Pythons at
# <gate_home>/.local/share/uv/python, a path that runs through a symlink in the
# scratch dir. A `uv run` in a checkout with no .venv (a fresh worktree) builds
# .venv on that path: bin/python and pyvenv.cfg `home =` point into the scratch
# dir. The gate deletes the scratch dir on exit and the checkout's .venv
# dangles; a later `uv build` under that VIRTUAL_ENV fails with a broken
# interpreter path. Pinned here, the venv records the real path.
#
# UV_CACHE_DIR: uv resolves its cache off HOME at process start; pinned so the
# mirror is not the only thing between the gate and a cold resolve.
fence_uv_env() {
    local real_home="$1"
    export UV_CACHE_DIR="${UV_CACHE_DIR:-$real_home/.cache/uv}"
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${XDG_DATA_HOME:-$real_home/.local/share}/uv/python}"
}
