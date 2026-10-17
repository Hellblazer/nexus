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
# or a fixture file), never by widening the mirror. No credential store is
# mirrored BY NAME (.ssh .aws .gnupg .kube .netrc .npmrc .gitconfig .claude
# .config/gh .config/op ... are never in the list). That is a claim about
# top-level names, not content: .docker can hold config.json (registry auths) and
# .m2 can hold settings.xml (server passwords), and both are linked whole. A JVM
# reads them regardless, since the JDK's user.home ignores $HOME. Narrowing them
# is Sam's call (nexus-u73rf).

# The only top-level entries of the real home linked into a fenced home. Twin of
# MIRRORED_ENTRIES in tests/_fence_home.py, kept in step by
# tests/test_fence_home_twins_agree.py. An entry the real home lacks is skipped.
FENCE_MIRRORED_ENTRIES=(
    ".m2" ".docker" ".testcontainers.properties"
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
# Pin the HOME-derived caches the empty fenced home would leave cold. The old
# whole-.cache mirror shared all of these; the inverted fence shares exactly the
# ones named here, by env, never by mirroring .cache (which holds far more than
# caches). An explicit outer value wins and an EMPTY one counts as unset. The
# pins are EXPORTED, so a nested fence (the Python twin inside this gate, whose
# "real home" is the empty gate home) keeps the outer value. Twin of
# fence_cache_env in tests/_fence_home.py; read that docstring for each key.
#
#   NX_ONNX_MODEL_DIR, NX_MINILM_CACHE_DIR, NX_ORT_NATIVE_DIR: model and native roots.
#   HF_HUB_CACHE: the HuggingFace hub cache. NOT HF_HOME, the parent of token.
#     <HF_HOME>/hub when the operator set HF_HOME, else
#     <XDG_CACHE_HOME>/huggingface/hub, else <real>/.cache/huggingface/hub.
#   NX_TEST_SUBSTRATE_CACHE: the engine-substrate cache root, read first by
#     tests/db/_service_fixture.py substrate_cache_root(). NOT XDG_CACHE_HOME,
#     which would re-expose all of .cache.
fence_cache_env() {
    local real_home="$1" cache_home hf_home
    export NX_ONNX_MODEL_DIR="${NX_ONNX_MODEL_DIR:-$real_home/.cache/nexus/onnx_models}"
    export NX_MINILM_CACHE_DIR="${NX_MINILM_CACHE_DIR:-$real_home/.cache/chroma/onnx_models}"
    export NX_ORT_NATIVE_DIR="${NX_ORT_NATIVE_DIR:-$real_home/.cache/nexus/ort_native}"
    cache_home="${XDG_CACHE_HOME:-$real_home/.cache}"
    hf_home="${HF_HOME:-$cache_home/huggingface}"
    export HF_HUB_CACHE="${HF_HUB_CACHE:-$hf_home/hub}"
    export NX_TEST_SUBSTRATE_CACHE="${NX_TEST_SUBSTRATE_CACHE:-$cache_home/nexus-test-substrate}"
}

# fence_keep_home <exitstatus>
#
# True (0) when a fenced home must be KEPT: only after a FAILED run, and only
# when NX_KEEP_FENCED_HOME is set (nexus-7df8e). Twin of the keep rule in
# remove_fenced_home in tests/_fence_home.py.
fence_keep_home() {
    [ -n "${NX_KEEP_FENCED_HOME:-}" ] && [ "${1:-0}" != "0" ]
}

# fence_remove_home <gate_home> <exitstatus>
#
# Remove the fenced home at the end of a run (nexus-7df8e). Returns 0 when it is
# gone, 1 when it was kept or refused. Refuses unless <gate_home> is the
# NX_FENCED_HOME this tree exported, so a wrong argument can never delete a
# directory the fence did not make. The mirror links are removed as links first,
# so nothing can follow one into the operator's real .m2 or .docker.
fence_remove_home() {
    local gate_home="$1" status="${2:-0}" name
    [ -n "$gate_home" ] && [ -d "$gate_home" ] && [ ! -L "$gate_home" ] || return 1
    [ "$gate_home" = "${NX_FENCED_HOME:-}" ] || return 1
    fence_keep_home "$status" && return 1
    for name in "${FENCE_MIRRORED_ENTRIES[@]}"; do
        if [ -L "$gate_home/$name" ]; then
            rm -f "$gate_home/$name" || return 1
        fi
    done
    rm -rf "$gate_home"
    [ ! -e "$gate_home" ]
}
