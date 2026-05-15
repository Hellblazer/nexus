#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORB bridge: Stop + StopFailure -> hook_events/assistant_turn_ended (RDR-111 §Step 2).

Handles both Stop and StopFailure hook types (both map to assistant_turn_ended).
Output is ignored by Claude Code for Stop/StopFailure; the bridge writes its
tuple as a side effect.

The hook_type is inferred from the payload's ``hook_event_name`` field.

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

_SUPPORTED = frozenset({"Stop", "StopFailure"})


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[orb-bridge-stop] malformed JSON: {exc}\n")
        payload = {}

    # Prefer the payload's hook_event_name; fall back to argv[1] (which
    # hooks.json passes for Stop/StopFailure differentiation) so a payload
    # without hook_event_name does not silently get stored as "Stop" when
    # it was actually "StopFailure". Mirrors orb_bridge_session.py.
    hook_type = payload.get("hook_event_name", "")
    if hook_type not in _SUPPORTED and len(sys.argv) > 1:
        hook_type = sys.argv[1]
    if hook_type not in _SUPPORTED:
        hook_type = "Stop"

    try:
        from nexus.cockpit.hook_bridge import emit, output_for_hook
        emit(hook_type, payload)

        out = output_for_hook(hook_type)
        if out is not None:
            sys.stdout.write(out)
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"[orb-bridge-stop] error: {exc}\n")


if __name__ == "__main__":
    main()
