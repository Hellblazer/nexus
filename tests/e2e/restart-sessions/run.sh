#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Sandbox gate for scripts/claude-restart-sessions --fresh (nexus-nuyju): real Claude Code
# sessions under a scratch HOME on a private tmux server, driven through the coordinated
# restart's continuation roll. See run.py for what it asserts. Authenticates with the
# automation token (RDR-219); nothing is written to the operator's ~/.claude, and no
# token, env dump or config file is printed.
#
#   tests/e2e/restart-sessions/run.sh [--model sonnet] [--keep]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
exec python3 "$repo/tests/e2e/lib/claude_credentials.py" run -- python3 -I "$here/run.py" "$@"
