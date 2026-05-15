#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORB bridge: PostToolUse -> hook_events/tool_call_completed (RDR-111 §Step 2).

Observe-only. Output is ignored by Claude Code; the bridge writes its tuple
as a side effect.

RF-5: all tuplespace side-effects are skipped when CLAUDECODE is not set.
Errors are logged to stderr only; the script always exits 0.
"""
from __future__ import annotations

import json
import os
import sys

# Configure structlog to stderr BEFORE importing nexus.cockpit.hook_bridge so any
# module-level or transitive-import log records land on stderr, not stdout (which
# is reserved for the Claude Code hook protocol).
import structlog as _structlog
_structlog.configure(logger_factory=_structlog.PrintLoggerFactory(file=sys.stderr))

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: nx hook bridge requires Python 3.12+, got {sys.version.split()[0]}\n"
    )
    sys.exit(0)

_HOOK_TYPE = "PostToolUse"


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[orb-bridge-posttooluse] malformed JSON: {exc}\n")
        payload = {}

    try:
        from nexus.cockpit.hook_bridge import emit, output_for_hook
        emit(_HOOK_TYPE, payload)

        out = output_for_hook(_HOOK_TYPE)
        if out is not None:
            sys.stdout.write(out)
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"[orb-bridge-posttooluse] error: {exc}\n")


if __name__ == "__main__":
    main()
