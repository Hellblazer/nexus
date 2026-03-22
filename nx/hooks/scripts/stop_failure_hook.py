#!/usr/bin/env python3
"""StopFailure hook — log API failure context to beads memory for observability.

Output and exit codes are ignored by Claude Code. This script exists purely
for side effects: bd remember (all failures) and bd create (rate_limit only).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


def _run(args: list[str], timeout: int = 5) -> bool:
    """Run a command, return True on success."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
        if DEBUG and result.stderr:
            _debug(f"stderr from {args[0]}: {result.stderr[:300]}")
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        _debug(f"{args[0]} failed: {exc}")
        return False


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
    error_details = data.get("error_details", "")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Normalize unknown error types
    if error_type not in KNOWN_TYPES:
        _debug(f"unknown error type: {error_type}, treating as 'unknown'")
        error_type = "unknown"

    if not shutil.which("bd"):
        _debug("bd not on PATH, skipping")
        return

    # Log failure to beads memory
    summary = f"stop-failure-{error_type}: {error_details[:200]} at {timestamp}"
    _run(["bd", "remember", summary])
    _debug(f"logged: {summary}")

    # Rate limit: create a blocker bead so next session sees it
    if error_type == "rate_limit":
        _run([
            "bd", "create",
            f"--title=Rate limit hit at {timestamp}",
            "--type=bug",
            "--priority=1",
            f"--description=API rate limit triggered. Details: {error_details[:200]}",
        ])
        _debug("created rate-limit blocker bead")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never raise — output is ignored anyway
        if DEBUG:
            print(f"[stop-failure-hook] unhandled: {exc}", file=sys.stderr)
    sys.exit(0)
