# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared I/O for ported Claude Code hooks (RDR-215).

Both tiers — the ``mcp_tool`` hooks registered on ``nx-mcp`` and the exec-form
``nx-hook`` verbs — call the same ``run(payload) -> HookResult``. This module
holds the three pieces they share: the stdin payload reader, the decision
envelope writers, and the never-fail boundary.

**The envelopes are bytes, not objects.** The writers reproduce the exact text
the bash layer emits today (inventoried in T2 ``nexus_rdr/215-hook-contract-map``),
key order included. Claude Code parses the JSON, but it is not the only reader:
tests and the routing framework match on rendered text, so a reformatted
equivalent is a contract break.

**Why an explicit boundary.** None of the 16 retired scripts set ``set -e``, and
several say so in a comment: a hook must never fail. Bash gets that by default;
Python does not, so a ported hook is wrapped in :func:`never_fail`, which turns
a crash into the same thing a hook that decided to stay silent produces.

**One hook must not use this.** ``phase_review_close_requires_gate`` is the
routing framework's only ``fail_closed: true`` rule
(``conexus/hooks/scripts/routing/registry.yaml``), and failing open is exactly
the wrong answer there — a crash would let a phase close without its gate. It
needs a deny-emitting counterpart, not this boundary. Bead nexus-q02nx.21 owns
that decision; do not reach for :func:`never_fail` on that hook by habit.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "HookResult",
    "additional_context",
    "never_fail",
    "permission_decision",
    "permission_request",
    "read_payload",
    "stop_decision",
]


@dataclass(frozen=True)
class HookResult:
    """What a hook decided.

    ``stdout`` is the rendered envelope line, or ``None`` for the many hooks
    that are stdout-silent by contract (``subagent-start-stamp.sh`` and
    ``agent-dispatch-expect.sh`` never write to real stdout at all).
    ``exit_code`` is 0 for every hook verb; the ledger verbs propagate their own
    codes, which callers branch on.
    """

    stdout: str | None = None
    exit_code: int = 0


def read_payload(stream: IO[str]) -> dict | None:
    """Read and parse a Claude Code hook JSON payload from *stream*.

    Returns the parsed dict, or ``None`` when no usable payload is available.
    Carried from ``nexus.commands.hook._read_stdin_payload`` (nexus-rv2x), and
    safe against the three inputs that produced that bead:

    * **TTY stdin** — reading would block until EOF, so a verb typed
      interactively would hang. Detected via ``isatty()`` and skipped without
      calling ``read()`` at all.
    * **Empty or closed pipe** — ``read()`` returns ``""`` promptly.
    * **Malformed JSON**, or valid JSON that is not an object.
    """
    try:
        if stream.isatty():
            return None
        raw = stream.read()
    except Exception as exc:  # noqa: BLE001 — a hook must never fail on its own stdin
        _log.debug("hook_stdin_read_failed", error=str(exc))
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — same swallow as an empty read
        _log.debug("hook_stdin_parse_failed", error=str(exc))
        return None
    if not isinstance(data, dict):
        return None
    return data


def _render(obj: dict) -> str:
    """Render one envelope as a single line, in the bash layer's spacing.

    ``json.dumps`` defaults (``", "`` and ``": "``) already match what the
    scripts' ``printf`` formats produce, and ``dict`` preserves insertion order,
    so key order here is the key order on the wire.
    """
    return json.dumps(obj)


def permission_decision(
    event: str,
    decision: str,
    *,
    permission_decision_reason: str | None = None,
    reason: str | None = None,
    additional_context: str | None = None,  # noqa: A002 — mirrors the wire field name
    system_message: str | None = None,
) -> str:
    """The ``hookSpecificOutput``/``permissionDecision`` envelope.

    Field order follows ``pre_close_verification_hook.sh``'s shared ``deny()``
    helper (line 38), which is the widest form: reason fields before
    ``additionalContext``, and ``systemMessage`` at the top level beside
    ``hookSpecificOutput`` rather than inside it.
    """
    specific: dict[str, object] = {"hookEventName": event, "permissionDecision": decision}
    if permission_decision_reason is not None:
        specific["permissionDecisionReason"] = permission_decision_reason
    if reason is not None:
        specific["reason"] = reason
    if additional_context is not None:
        specific["additionalContext"] = additional_context
    envelope: dict[str, object] = {"hookSpecificOutput": specific}
    if system_message is not None:
        envelope["systemMessage"] = system_message
    return _render(envelope)


def additional_context(event: str, text: str) -> str:  # noqa: F811 — distinct name at module scope
    """The context-injection envelope, with no ``permissionDecision`` field.

    Used by the ``SubagentStart`` injectors, which in bash accumulate body text
    through an ``EXIT`` trap and an fd-3 buffer; in Python the body is just a
    string, so the trap machinery disappears.
    """
    return _render({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def permission_request(behavior: str) -> str:
    """The ``PermissionRequest`` envelope (``auto-approve-nx-mcp.sh``:89-96).

    A different shape from :func:`permission_decision`: the decision is a nested
    object keyed ``behavior``, not a string keyed ``permissionDecision``.
    """
    return _render(
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": behavior},
            }
        }
    )


def stop_decision(decision: str, *, reason: str | None = None) -> str:
    """The top-level ``decision`` envelope the Stop and SubagentStop hooks use.

    Note this is deliberately *not* a ``hookSpecificOutput`` shape — those two
    events take the older top-level form, and mixing them silently drops the
    decision.
    """
    envelope: dict[str, object] = {"decision": decision}
    if reason is not None:
        envelope["reason"] = reason
    return _render(envelope)


def never_fail(body: Callable[[], HookResult], hook: str) -> HookResult:
    """Run *body*, turning any crash into a silent, successful result.

    The bash layer gets this for free by never setting ``set -e``; Python has no
    equivalent, so this is the explicit replacement and every hook goes through
    it. A crash becomes exactly what a hook that chose to say nothing produces,
    which for a PreToolUse gate means the event proceeds.

    Interpreter-shutdown signals pass through. Swallowing ``KeyboardInterrupt``
    or ``SystemExit`` would turn the harness killing a hook into a hang, which
    is the opposite of failing open. Cancellation passes through for the same
    reason: ``asyncio.CancelledError`` is a ``BaseException``, the tool tier
    calls ``run()`` from async handlers, and a swallowed cancellation breaks the
    caller's own timeout rather than the hook's.
    """
    try:
        return body()
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as exc:  # noqa: BLE001 — the whole point: a hook must never fail
        import asyncio  # noqa: PLC0415 — deferred import; only the crash path pays it, and nx-hook's cold start is budgeted

        if isinstance(exc, asyncio.CancelledError):
            raise
        _log.warning("hook_boundary_swallowed_exception", hook=hook, error=str(exc))
        return HookResult()
