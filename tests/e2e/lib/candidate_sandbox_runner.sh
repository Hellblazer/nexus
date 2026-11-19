#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# The sandbox runner candidate_sandbox.sh copies to <sandbox>/run. Its own environment is the whitelist; the
# secrets are read from files by the INNER shell, so no argv ever carries one. It resolves its sandbox from its own
# location, so it is copied unchanged.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
h="$here/home"
# shellcheck disable=SC2016  # the inner script is deliberately single-quoted: it must expand after the env -i
exec /usr/bin/env -i \
    HOME="$here/home" USER="${USER:-sandbox}" LANG="${LANG:-en_US.UTF-8}" TERM="${TERM:-xterm-256color}" \
    NEXUS_CONFIG_DIR="$here/home/.config/nexus" CLAUDE_CONFIG_DIR="$h/.claude" NX_NO_TELEMETRY=1 \
    TMPDIR="$here/tmp" UV_TOOL_DIR="$here/home/.local/share/uv/tools" UV_TOOL_BIN_DIR="$here/home/.local/bin" \
    PATH="$here/home/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    SBX_SECRETS="$here/secrets" \
    /bin/bash -c '
        for f in "$SBX_SECRETS"/*; do
            [ -f "$f" ] || continue
            IFS= read -r v <"$f" || true
            export "$(basename "$f")=$v"
        done
        unset SBX_SECRETS v f
        exec /usr/bin/env "$@"' _ "$@"
