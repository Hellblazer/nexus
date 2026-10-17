#!/usr/bin/env bash
# Shadow a HOME: an empty directory plus a short allowlist of toolchain caches
# (nexus-ld3aa; supersedes the nexus-pfuns mirror-everything design).
#
# Sourced by tests/e2e/local-service-gate.sh and exercised directly by
# tests/test_gate_fences_the_real_config_dir.py. It lives here rather than
# inline in the gate so the TEST DRIVES THE REAL IMPLEMENTATION instead of a
# copy of it — a fence verified against a reimplementation is not verified.
#
# WHY SHADOW-EVERYTHING. The fence used to symlink every entry of the real home
# and shadow a denylist. The failure modes are asymmetric: a path the fence
# fails to mirror breaks LOUDLY on the first run (2026-08-24: the Maven jar
# rebuild died because ~/.testcontainers.properties and ~/.docker were absent),
# while a path it mirrors by mistake fails SILENTLY and destructively
# (2026-09-12 a test overwrote the operator's global identity file; 2026-09-30 a
# test replaced a live nexus-service autostart unit on qwentescence). A denylist
# is never finished: a dotfile the operator installs next week is mirrored by
# default. So the home starts EMPTY and only FENCE_MIRRORED_ENTRIES are linked
# through, each a read-mostly toolchain cache measured as needed. Anything else
# a leg needs it is given explicitly (an env pin: fence_uv_env, fence_cache_env;
# or a fixture file), never by widening the mirror. Credential stores
# (.ssh .aws .gnupg .kube .netrc .npmrc .gitconfig .claude .config/gh .config/op
# ...) are never in the list.

# The only top-level entries of the real home linked into a fenced home. Twin of
# MIRRORED_ENTRIES in tests/_fence_home.py, kept in step by
# tests/test_fence_home_twins_agree.py. An entry the real home lacks is skipped.
FENCE_MIRRORED_ENTRIES=(
    ".m2" ".gradle" ".docker" ".cargo" ".rustup" ".testcontainers.properties"
)

# fence_home <real_home> <gate_home>
#
# Creates <gate_home> empty, then symlinks each FENCE_MIRRORED_ENTRIES entry that
# exists in <real_home> into it. Nothing else of the real home is visible from
# the result.
fence_home() {
    local real_home="$1" gate_home="$2" name
    mkdir -p "$gate_home"
    for name in "${FENCE_MIRRORED_ENTRIES[@]}"; do
        if [ -e "$real_home/$name" ] || [ -L "$real_home/$name" ]; then
            ln -sfn "$real_home/$name" "$gate_home/$name"
        fi
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
# UV_PYTHON_INSTALL_DIR (nexus-t0pke): the fenced home has no ~/.local, so
# without the pin uv looks for managed Pythons under the scratch dir. A `uv run`
# in a checkout with no .venv (a fresh worktree) would build .venv on that
# path; the gate deletes the scratch dir on exit and the checkout's .venv
# dangles, and a later `uv build` under that VIRTUAL_ENV fails with a broken
# interpreter path. Pinned here, the venv records the real path.
#
# UV_CACHE_DIR: uv resolves its cache off HOME at process start; pinned so a
# cold resolve is not the price of the fence.
fence_uv_env() {
    local real_home="$1"
    export UV_CACHE_DIR="${UV_CACHE_DIR:-$real_home/.cache/uv}"
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${XDG_DATA_HOME:-$real_home/.local/share}/uv/python}"
}

# fence_cache_env <real_home>
#
# Pin the ONNX model roots to the real home's read-mostly caches. The engine
# resolves model files from NX_ONNX_MODEL_DIR or $HOME/.cache/nexus/onnx_models
# (nexus.db.onnx_model_root); under the empty fenced home the default has no
# model in it and the gate's vector leg would find nothing (or download ~500 MB).
# Shared by env, not by mirroring .cache, which holds far more than models. Twin
# of fence_cache_env in tests/_fence_home.py; an explicit outer value wins and an
# EMPTY one counts as unset.
fence_cache_env() {
    local real_home="$1"
    export NX_ONNX_MODEL_DIR="${NX_ONNX_MODEL_DIR:-$real_home/.cache/nexus/onnx_models}"
    # The MiniLM direct-path cache (nexus.db.minilm_direct.CACHE_DIR_ENV), the
    # Chroma-compatible root. tests/conftest.py also pins it, but from
    # NX_REAL_HOME, which is the EMPTY gate home when the suite runs in the gate.
    export NX_MINILM_CACHE_DIR="${NX_MINILM_CACHE_DIR:-$real_home/.cache/chroma/onnx_models}"
    # The ONNX Runtime native-library extraction root (nexus-8ool4): write-once per
    # <version>-<os-arch>, reused across engine starts, so the gate shares the real
    # cache instead of extracting ~16 MB again into the empty home.
    export NX_ORT_NATIVE_DIR="${NX_ORT_NATIVE_DIR:-$real_home/.cache/nexus/ort_native}"
}
