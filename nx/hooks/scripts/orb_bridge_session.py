#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORB bridge: SessionStart + SessionEnd -> hook_events/session_lifecycle (RDR-111 §Step 2).

Handles both SessionStart and SessionEnd hook types (both map to
session_lifecycle, per RDR-111 §Step 2 rationale: both represent a session
turn boundary).

The hook_type is inferred from the payload's ``hook_event_name`` field,
with a fallback to the argv[1] (hook-type arg from hooks.json).

SessionEnd emits no stdout (output ignored by Claude Code).
SessionStart also emits no stdout (observe-only).

RF-5: all tuplespace side-effects are skipped when CLAUDECODE is not set.
Errors are logged to stderr only; the script always exits 0.
"""
from __future__ import annotations

import json
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

_SUPPORTED = frozenset({"SessionStart", "SessionEnd"})


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[orb-bridge-session] malformed JSON: {exc}\n")
        payload = {}

    # Infer hook type from payload or argv[1]
    hook_type = payload.get("hook_event_name", "")
    if hook_type not in _SUPPORTED and len(sys.argv) > 1:
        hook_type = sys.argv[1]
    if hook_type not in _SUPPORTED:
        hook_type = "SessionStart"

    try:
        from nexus.cockpit.hook_bridge import emit, output_for_hook
        emit(hook_type, payload)

        out = output_for_hook(hook_type)
        if out is not None:
            sys.stdout.write(out)
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"[orb-bridge-session] error: {exc}\n")


if __name__ == "__main__":
    main()
