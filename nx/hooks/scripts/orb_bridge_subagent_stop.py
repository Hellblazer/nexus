#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORB bridge: SubagentStop -> hook_events/agent_completed (RDR-111 §Step 2).

CA-6 spike (2026-05-14): SubagentStop payload is significantly richer than
the original RF-1 inference -- includes agent_id, agent_type, effort.level,
last_assistant_message, agent_transcript_path. The bridge uses
last_assistant_message as match_text for semantic search.

RF-5: all tuplespace side-effects are skipped when CLAUDECODE is not set.
Errors are logged to stderr only; the script always exits 0.
"""
from __future__ import annotations

import json
import sys

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: nx hook bridge requires Python 3.12+, got {sys.version.split()[0]}\n"
    )
    sys.exit(0)

_HOOK_TYPE = "SubagentStop"

# Plugin/wheel compat protocol (nexus-yeu8). See orb_bridge_pretooluse.py.
EXPECTED_BRIDGE_API_VERSION = 1


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[orb-bridge-subagent-stop] malformed JSON: {exc}\n")
        payload = {}

    try:
        from nexus.cockpit.hook_bridge import (
            check_bridge_api_version,
            configure_logging_to_stderr,
            emit,
            output_for_hook,
        )

        configure_logging_to_stderr()
        if not check_bridge_api_version(EXPECTED_BRIDGE_API_VERSION):
            return
        emit(_HOOK_TYPE, payload)

        out = output_for_hook(_HOOK_TYPE)
        if out is not None:
            sys.stdout.write(out)
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"[orb-bridge-subagent-stop] error: {exc}\n")


if __name__ == "__main__":
    main()
