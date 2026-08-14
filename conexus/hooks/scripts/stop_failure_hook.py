#!/usr/bin/env python3
"""StopFailure hook — swallow API failure events without side effects.

Output and exit codes are ignored by Claude Code. Transient API failures are
infra events, not actionable bugs: it does NOT file issues (pollutes the
ready queue) and does NOT `bd remember` them (per-event keys accumulated
unboundedly and bd prime injected them into every session — nexus-0dj7e).
Debug tracing via NX_HOOK_DEBUG=1 only.
"""
from __future__ import annotations

import sys
if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

import json
import os
from datetime import datetime, timezone

DEBUG = os.environ.get("NX_HOOK_DEBUG", "0") == "1"

KNOWN_TYPES = frozenset({
    "rate_limit",
    "authentication_failed",
    "billing_error",
    "invalid_request",
    "server_error",
    "max_output_tokens",
    "unknown",
})


def _debug(msg: str) -> None:
    if DEBUG:
        print(f"[stop-failure-hook] {msg}", file=sys.stderr)


def main() -> None:
    # Parse stdin JSON — gracefully handle malformed input
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _debug("empty stdin")
            return
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        _debug(f"stdin parse error: {exc}")
        return

    error_type = data.get("error", "unknown")
    error_details = str(data.get("error_details") or "")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Normalize unknown error types
    if error_type not in KNOWN_TYPES:
        _debug(f"unknown error type: {error_type}, treating as 'unknown'")
        error_type = "unknown"

    # Only run side effects inside a real Claude Code session.
    # Tests invoke us via subprocess but don't set CLAUDECODE=1.
    if not os.environ.get("CLAUDECODE"):
        _debug("not in Claude Code session (CLAUDECODE not set), skipping side effects")
        return

    # No side effects. Transient API failures (rate limit, server error,
    # auth) are infra events: `bd create` would pollute `bd ready`, and
    # `bd remember` minted a permanent per-event key that bd prime injected
    # into every session's context (nexus-0dj7e). Debug trace only.
    summary = f"stop-failure-{error_type}: {error_details[:200]} at {timestamp}"
    _debug(f"observed: {summary}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never raise — output is ignored anyway
        if DEBUG:
            print(f"[stop-failure-hook] unhandled: {exc}", file=sys.stderr)
    sys.exit(0)
