#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# drive_hook.sh <module> — run one ported hook from a shell: read the
# Claude Code payload on stdin, write the hook's decision to stdout, exit
# with the hook's exit code. <module> is a bare module name under
# nexus.hooks, e.g. `subagent_stop`.
#
# WHY THIS EXISTS. RDR-215 bead nexus-q02nx.21 deleted the plugin bash
# hooks these scenarios drove; production reaches the same code through
# `type: mcp_tool` entries in conexus/hooks/hooks.json. A cc-validation
# scenario that needs the hook behind its OWN wrapper (scenario 21's
# logging passthrough) or under its OWN pinned env (scenario 27's
# XDG_STATE_HOME) cannot use an mcp_tool entry for it, because both of
# those are per-command affordances an mcp_tool hook does not have. So
# those scenarios reach the ported module the only other way there is, by
# importing it.
#
# WHAT IS AND IS NOT PRESERVED. The decision logic and the ledger writes
# are the production ones — this imports the module production runs, it
# does not reimplement it. Env reaches it identically: the modules read
# NX_ORCH_STOP_GUARD and XDG_STATE_HOME out of this process's environment.
# What is NOT preserved is the TRANSPORT: production dispatches through
# FastMCP, this through a subprocess. Every scenario using this driver
# had already displaced the production transport with its own wiring, so
# no assertion loses its subject — but a scenario that wants to prove the
# mcp_tool path itself must wire `type: mcp_tool` against a real server,
# the way scenario 03 does, not use this.
#
# This checkout's own interpreter, never a bare `python3`: the module lives
# in the wheel, and an operator's system python3 cannot import it. The
# checkout venv is preferred over `uv run` because these run as Claude Code
# hooks under a timeout, and `uv run` adds a resolve step on every single
# firing; scenario 27 already reaches for `$REPO_ROOT/.venv/bin/python`
# directly for its SID capture, so this is that file's own idiom. `uv run`
# stays as the fallback for a checkout whose venv has not been created.
set -u
MODULE="${1:?usage: drive_hook.sh <nexus.hooks module name>}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

if [[ -x "$REPO/.venv/bin/python" ]]; then
    PY=("$REPO/.venv/bin/python")
else
    PY=(uv run --project "$REPO" python)
fi

exec "${PY[@]}" -c '
import importlib
import sys

from nexus._hook_runtime._io import read_payload

result = importlib.import_module("nexus.hooks." + sys.argv[1]).run(read_payload(sys.stdin))
if result.stdout:
    sys.stdout.write(result.stdout)
sys.exit(result.exit_code)
' "$MODULE"
