#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORB bridge: Notification -> hook_events/notification (RDR-111 §Step 2).

CA-6 spike (2026-05-14): verified payload -- message, notification_type,
session_id, cwd, hook_event_name. Reliable trigger is idle-wait (~60-90s),
NOT permission prompts (CC 2.1.x auto-allows safe tools). The bridge uses
the message text as match_text for semantic search.

Output is ignored by Claude Code for Notification hooks.

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

_HOOK_TYPE = "Notification"


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[orb-bridge-notification] malformed JSON: {exc}\n")
        payload = {}

    try:
        from nexus.cockpit.hook_bridge import configure_logging_to_stderr, emit, output_for_hook

        configure_logging_to_stderr()
        emit(_HOOK_TYPE, payload)

        out = output_for_hook(_HOOK_TYPE)
        if out is not None:
            sys.stdout.write(out)
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"[orb-bridge-notification] error: {exc}\n")


if __name__ == "__main__":
    main()
