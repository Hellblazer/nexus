#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORB bridge: PreToolUse -> hook_events/tool_call_intent (RDR-111 §Step 2).

Observe-only per CA-8 spike (2026-05-14): both hooks fire on every invocation
regardless of registration order; allow-wins. The bridge writes its tuple as
a side effect and emits no stdout, leaving permission decisions to the user's
allowlist and other installed hooks.

RF-5: all tuplespace side-effects are skipped when CLAUDECODE is not set.
Errors are logged to stderr only; the script always exits 0.
"""
from __future__ import annotations

import json
import os
import sys

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: nx hook bridge requires Python 3.12+, got {sys.version.split()[0]}\n"
    )
    sys.exit(0)  # always exit 0

_HOOK_TYPE = "PreToolUse"


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[orb-bridge-pretooluse] malformed JSON: {exc}\n")
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
        sys.stderr.write(f"[orb-bridge-pretooluse] error: {exc}\n")


if __name__ == "__main__":
    main()
