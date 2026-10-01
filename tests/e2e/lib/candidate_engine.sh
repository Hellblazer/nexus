#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source this; it defines functions only and runs nothing (nexus-0kmat).
#
# Thin bash face of tests/e2e/lib/candidate_engine.py, which carries the logic
# and the rationale. A gate that launches nx under `env -i` does:
#
#     source "$REPO_ROOT/tests/e2e/lib/candidate_engine.sh"
#     candidate_engine_load || exit 1            # before provisioning
#     env -i HOME=... PATH=... \
#         ${CAND_ENV_ARGS[@]+"${CAND_ENV_ARGS[@]}"} \
#         nx init ...
#     candidate_engine_identity "$HOME_DIR/.config/nexus" mvv || _fail ...
#     ...journey...
#     candidate_engine_refusals "$HOME_DIR/.config/nexus" mvv || _fail ...
#
# CAND_ENV_ARGS is empty when no candidate is set and cut mode is off, so the
# default (pinned published engine) path is unchanged.
#
# Inputs: NX_CANDIDATE_ENGINE (a *.jar or a native binary), NX_CUT_MODE=1,
# NX_CANDIDATE_EXPECT_OWNERLESS_MODE (default enforce; "none" drops that assert).
#
# A native binary is not required: every gate here takes the JVM jar that
# scripts/build-gate-jar.sh produces through NEXUS_SERVICE_JAR. A leg that can
# only take a native binary (release-sandbox.sh `service` mode requires
# NEXUS_SERVICE_BIN) gets a JVM-jar shim, as AGENTS.md's release-workflow shape
# check does: an executable `#!/bin/sh` file whose body is
# `exec java -jar <abs jar> "$@"`, named by NX_CANDIDATE_ENGINE.

CAND_ENGINE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAND_ENV_ARGS=()

# Resolve the candidate into CAND_ENV_ARGS. Returns 2 with the reason on stderr
# when the candidate cannot be honoured (set but missing, cut mode with none).
# <stage-dir> (optional) gets a private COPY of the candidate that the variables
# then name: gates run in parallel in the battery, and the supervisor finds its
# engine processes by argv, so two gates on one jar path stop each other's
# engines. Every gate passes a directory of its own.
candidate_engine_load() {  # [stage-dir]
    local out line
    CAND_ENV_ARGS=()
    if [ -n "${1:-}" ]; then
        out="$(python3 "$CAND_ENGINE_LIB_DIR/candidate_engine.py" env --stage "$1")" || return 2
    else
        out="$(python3 "$CAND_ENGINE_LIB_DIR/candidate_engine.py" env)" || return 2
    fi
    while IFS= read -r line; do
        [ -n "$line" ] && CAND_ENV_ARGS+=("$line")
    done <<<"$out"
    if [ "${#CAND_ENV_ARGS[@]}" -gt 0 ]; then
        echo "  candidate engine: ${CAND_ENV_ARGS[0]}"
    elif [ "${NX_CUT_MODE:-0}" != 1 ]; then
        echo "  engine: PINNED PUBLISHED (no NX_CANDIDATE_ENGINE; not a cut-mode run)"
    fi
    return 0
}

# Print which engine is actually serving; in cut mode fail unless it is the candidate.
candidate_engine_identity() {  # <config-dir> <label>
    python3 "$CAND_ENGINE_LIB_DIR/candidate_engine.py" identity "$1" --label "$2"
}

# End of journey: ownerless-write counters and engine log; in cut mode fail on any refusal.
candidate_engine_refusals() {  # <config-dir> <label>
    python3 "$CAND_ENGINE_LIB_DIR/candidate_engine.py" refusals "$1" --label "$2"
}
