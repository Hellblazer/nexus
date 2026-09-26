#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Exec `claude "$@"` with its OAuth token on fd 3 instead of in its environment
# (RDR-219, nexus-wauo1.36). Run it under `claude_credentials.py run --`, which
# sets CLAUDE_CODE_OAUTH_TOKEN; this script moves that value into a pipe on
# fd 3, removes the variable, and sets CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3,
# so the token is not in Claude's exec-time environment where a same-user
# `ps -E` or /proc/<pid>/environ could read it. Claude reads the fd once and
# removes the fd variable from its own children, as it does the token.
# Measured 2026-09-26 on Claude Code 2.1.283, interactive (tmux) and -p.
#
# The pipe is a process substitution written by `builtin printf`: the token is
# never an argv element and never touches disk.

t="${CLAUDE_CODE_OAUTH_TOKEN:-}"
if [ -z "$t" ]; then
    echo "claude_fd_exec: CLAUDE_CODE_OAUTH_TOKEN is not set -- run this harness through" \
         "'claude_credentials.py run -- ...' first (RDR-219)" >&2
    exit 1
fi
unset CLAUDE_CODE_OAUTH_TOKEN
export CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3
exec claude "$@" 3< <(builtin printf '%s' "$t")
