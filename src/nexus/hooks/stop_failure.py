# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The StopFailure observer (RDR-215 bead nexus-q02nx.21).

Port of ``conexus/hooks/scripts/stop_failure_hook.py``. It swallows API
failure events and does nothing else. That is the whole hook, and it is
deliberate — the script's own docstring says so: transient API failures
are infra events, not actionable bugs, so it does NOT file issues (which
pollutes ``bd ready``) and does NOT ``bd remember`` them (per-event keys
accumulated unboundedly and ``bd prime`` injected them into every
session — nexus-0dj7e). Debug tracing under ``NX_HOOK_DEBUG=1`` only.

**Why this one is safe on the TOOL tier when its three siblings are not.**
Bead .21's tier resolution (T2 ``nexus_rdr/215-tier-resolution-bead-21``)
put ``mailbox_drain``, ``subagent_git_write_requires_orchestrator`` and
``phase_review_close_requires_gate`` on the command tier, because each
reaches ``_endpoint_resolve.py``, which cannot leave the plugin. This one
imports nothing but the standard library, so it has no closure to drag.

And it cannot be harmed by the tool tier's crash semantics. A raised
exception there returns empty text with ``isError`` false, and a
disconnected server is a non-blocking error — both read as allow. That
matters only for a hook whose answer is load-bearing. This hook has no
answer: the script says "Output and exit codes are ignored by Claude
Code", so an allow, a crash and a correct run are indistinguishable to
the caller by construction.

**The CLAUDECODE guard is carried verbatim, including its oddity.** The
script skips its side effects unless ``CLAUDECODE`` is set, with the
comment "Tests invoke us via subprocess but don't set CLAUDECODE=1". That
is a test-shaped condition in production code, and it guards side effects
that no longer exist — the body below it only builds a string and debug
logs it. Left exactly as found: "move, do not rewrite" (Approach item 9),
and removing it would be a behaviour change smuggled in under a port.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from nexus._hook_runtime._io import HookResult

__all__ = ["run"]

#: Carried verbatim from the script. Anything outside this set is
#: normalised to "unknown" rather than passed through.
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
    """Stderr, never stdout, and only under the env var.

    Read at call time rather than import time, unlike the script's
    module-level ``DEBUG``: the tool tier imports this module once per
    server, so a module-level read would freeze whatever the environment
    said at server start for the life of the process.
    """
    if os.environ.get("NX_HOOK_DEBUG", "0") == "1":
        # noqa rather than structlog: this is the debug trace of a hook
        # whose whole contract is to cost nothing, and reaching for a
        # logger here would re-import the 60 ms chain the package just
        # shed. stderr, never stdout — see the class below it in the tests.
        print(f"[stop-failure-hook] {msg}", file=sys.stderr)  # noqa: T201


def run(payload: dict | None) -> HookResult:
    """Observe a StopFailure event. Always allows, always silent."""
    if not isinstance(payload, dict) or not payload:
        _debug("empty stdin")
        return HookResult()

    error_type = payload.get("error", "unknown")
    error_details = str(payload.get("error_details") or "")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if error_type not in KNOWN_TYPES:
        _debug(f"unknown error type: {error_type}, treating as 'unknown'")
        error_type = "unknown"

    # Only run side effects inside a real Claude Code session.
    # Tests invoke us via subprocess but don't set CLAUDECODE=1.
    if not os.environ.get("CLAUDECODE"):
        _debug("not in Claude Code session (CLAUDECODE not set), skipping side effects")
        return HookResult()

    # No side effects. Transient API failures (rate limit, server error,
    # auth) are infra events: `bd create` would pollute `bd ready`, and
    # `bd remember` minted a permanent per-event key that bd prime injected
    # into every session's context (nexus-0dj7e). Debug trace only.
    summary = f"stop-failure-{error_type}: {error_details[:200]} at {timestamp}"
    _debug(f"observed: {summary}")
    return HookResult()
