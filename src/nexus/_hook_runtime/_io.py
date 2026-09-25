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
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO


def _emit(level: str, event: str, **fields: object) -> None:
    """Log a hook diagnostic without ever touching stdout.

    **stdout is the decision channel.** Claude Code parses this process's stdout
    as the hook's JSON decision. An unconfigured structlog logger writes there
    by default — ``PrintLoggerFactory``, with no level filtering — so a debug
    line about malformed stdin is enough to corrupt a decision and read as a
    hook malfunction. That is the nexus D9 defect class, and
    :func:`nexus.logging_setup.emit_import_time_warning` exists because of it.
    Bridging at each entry point works only while every caller remembers; this
    module is the spine both tiers call, so the guarantee belongs here.

    **The import is deferred for a second reason.** ``import structlog`` costs
    about 0.06 s against about 0.01 s of interpreter startup (25.5.0 pulls
    ``structlog.dev`` -> ``rich.traceback`` -> ``pygments`` -> an entry-point
    scan). Every command-tier hook loads this module on every invocation, and
    the close gate runs on every Bash call, so only a hook that actually logs —
    a malformed payload or a swallowed crash, both rare — should pay it.
    """
    try:
        from nexus.logging_setup import (  # noqa: PLC0415 — deferred; see above
            active_log_file,
            emit_import_time_warning,
        )

        if active_log_file() is None:
            # No sink yet. Make one, rather than drop the line.
            #
            # This is reached on the command tier, where nothing configures
            # logging up front any more: nx-hook's main() used to call the
            # bridge unconditionally before reading stdin, which is what made
            # every stdlib-only verb pay for structlog (nexus-br31l). Removing
            # that call alone would have silently retired read_payload's own
            # parse-failure diagnostics, because main() reads the payload
            # BEFORE any verb runs, so no verb -- however diligent about
            # calling configure_hook_logging() itself -- could ever get a sink
            # in place in time. Measured: hook.log created, 0 bytes, the
            # malformed-payload line gone. Configuring HERE keeps the cost on
            # the path that actually logs, which is the point of the whole
            # deferral.
            configure_hook_logging()
            if active_log_file() is None:
                # Genuinely no sink available (unwritable config dir, or a
                # logging_setup that declined). A debug line has nowhere safe
                # to go, so it is dropped rather than risked on stdout; a
                # warning goes to the stderr-bound logger that never reads
                # global structlog state.
                if level == "warning":
                    emit_import_time_warning(event, **fields)
                return
        import structlog  # noqa: PLC0415 — deferred; see above

        getattr(structlog.get_logger(__name__), level)(event, **fields)
    except Exception:  # noqa: BLE001 — a hook must never fail, least of all on its own logging
        return


def configure_hook_logging() -> None:
    """Point structlog at stderr + ``<config>/logs/hook.log``, for verbs that log.

    :func:`_emit` needs no help: it resolves a sink itself and drops a debug
    line rather than risk stdout. This exists for the other case -- a verb
    whose own implementation logs through an ambient
    ``structlog.get_logger()`` it does not own, which is every verb reaching
    into :mod:`nexus.hooks` (its module-scope ``_log``). Without a configured
    sink those lines are not lost to a file, they are written to **stdout** by
    structlog's default ``PrintLoggerFactory`` -- the hook's decision channel.

    **Call this only from a verb that already pays for structlog anyway.** It
    imports ``nexus.logging_setup``, which costs about 0.06 s against about
    0.01 s of interpreter startup, and that was the entire reason nexus-br31l
    existed: ``nx-hook`` used to call this from its own ``main()`` on every
    dispatch, so a stdlib-only verb paid the whole ``structlog`` ->
    ``rich`` -> ``pygments`` chain to set up a sink it would never write to.
    A verb that does not log must not call this.

    The envelope is safe either way -- :func:`nexus._hook_runtime.entry.main`
    routes stray stdout to stderr for the whole dispatch -- so a verb that
    forgets this loses its log file, not its correctness.

    Best-effort: an interpreter missing ``nexus.logging_setup``, or a bug in
    the logging setup itself, must never turn into a hook failure.
    """
    try:
        from nexus.logging_setup import configure_logging  # noqa: PLC0415 — deferred; see above

        configure_logging(mode="hook")
    except Exception:  # noqa: BLE001 — best-effort; must never break the calling hook
        return


#: Where :func:`stream` writes, or ``None`` when nothing installed one.
#: :func:`nexus._hook_runtime.entry.main` installs a sink bound to the REAL
#: stdout for the length of one dispatch; outside a dispatch (the tool tier,
#: a test calling ``run()`` in-process) there is none.
_stream_sink: Callable[[str], None] | None = None


def install_stream_sink(sink: Callable[[str], None] | None) -> None:
    """Install (or, with ``None``, remove) the sink :func:`stream` writes to."""
    global _stream_sink  # noqa: PLW0603 — one dispatch per process; entry.main owns the lifetime
    _stream_sink = sink


def stream(text: str) -> bool:
    """Write *text* to the real stdout NOW, flushed, and return ``True``; or
    return ``False`` when no sink is installed, leaving the caller to put the
    text in its :class:`HookResult` instead.

    For the one verb whose stdout is plain injected context rather than a
    decision envelope, and whose correctness depends on the write happening
    before the next step: ``mailbox-drain`` consumes a row at the engine and
    must have shown it before dropping its own recovery record. A
    ``HookResult`` is written only after ``run()`` returns, so a harness
    timeout (a kill) in between would lose a message the engine already
    consumed. A decision hook must not use this: its envelope is one JSON
    object and belongs in ``HookResult.stdout``.
    """
    sink = _stream_sink
    if sink is None:
        return False
    sink(text)
    return True


__all__ = [
    "HookResult",
    "additional_context",
    "configure_hook_logging",
    "install_stream_sink",
    "never_fail",
    "permission_decision",
    "permission_request",
    "read_payload",
    "stop_decision",
    "stream",
]


@dataclass(frozen=True)
class HookResult:
    """What a hook decided.

    ``stdout`` is the rendered envelope line, or ``None`` for the many hooks
    that are stdout-silent by contract (``subagent-start-stamp.sh`` and
    ``agent-dispatch-expect.sh`` never write to real stdout at all).
    ``exit_code`` is 0 for every hook verb; the ledger verbs propagate their own
    codes, which callers branch on.

    ``crashed`` marks a result produced by :func:`never_fail`'s swallow rather
    than by the verb returning. It exists because a ledger verb's exit code IS
    its contract, so "the verb crashed" and "the verb legitimately returned 0"
    must not be the same answer. Measured before the fix (bead nexus-q02nx.9):
    a ledger verb that raised exited 0, indistinguishable from a clean
    ``reconcile``, which bead ``.13`` would read as "nothing stranded" -- a
    silent miss in the subsystem built to catch silent misses. Non-ledger verbs
    ignore it and still exit 0, because for them a crash IS a hook choosing to
    say nothing, which is what failing open means.
    """

    stdout: str | None = None
    exit_code: int = 0
    crashed: bool = False


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
        _emit("debug", "hook_stdin_read_failed", error=str(exc))
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — same swallow as an empty read
        _emit("debug", "hook_stdin_parse_failed", error=str(exc))
        return None
    if not isinstance(data, dict):
        return None
    return data


def structured_field(data: dict, name: str) -> dict:
    """Read a payload field that is contractually an OBJECT, whatever shape it arrives in.

    Returns the field as a dict, or ``{}`` when it is absent, null, or
    something that is not an object and cannot be read as one.

    This exists because the same field reaches a hook by two routes with
    two shapes, and getting it wrong is SILENT. On the command tier the
    payload is the harness's own stdin JSON, so ``tool_input`` is a real
    dict. On the tool tier it arrives through a ``hooks.json`` ``input``
    map (``"tool_input": "${tool_input}"``) whose substitution was measured
    at bead nexus-q02nx.6 to deliver a structure -- but the tool's
    parameter is typed ``Any``, so a caller that hands it the JSON *text*
    is accepted just as readily, and the port's own ``isinstance(x, dict)``
    checks then fell through to a default.

    The damage was not a crash in any of the three affected hooks; it was
    a plausible wrong answer. ``pre_close_verification`` read the whole
    JSON blob as the command string, found no ``bd`` verb inside the JSON
    quoting and allowed the close. ``agent_dispatch_expect`` recorded the
    dispatch as ``general-purpose`` rather than its real subagent type,
    which is worse than recording nothing: the RDR-184 ledger matches N
    EXPECT rows of a type against N STARTs of that type, so a wrong type
    both grants a phantom credit and manufactures an undeclared start.
    ``divergence_language_guard`` read an empty ``file_path`` and scanned
    nothing. Three hooks, one boundary, and in every case the failure
    looked exactly like the hook having nothing to say.

    So: parse a string that holds an object, and be loud about anything
    else. A field that arrives as a non-empty string which is NOT JSON, or
    is JSON but not an object, is a shape this code does not understand,
    and it logs at warning rather than returning ``{}`` quietly -- the
    caller still gets ``{}`` and still fails open, because a hook must
    never take down the event it observes, but the log line names the
    field and the hook.
    """
    value = data.get(name)
    if isinstance(value, dict):
        return value
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:  # noqa: BLE001 — a hook never fails on its own payload
            _emit("warning", "hook_structured_field_not_json", field=name)
            return {}
        if isinstance(parsed, dict):
            return parsed
        _emit("warning", "hook_structured_field_not_an_object", field=name, kind=type(parsed).__name__)
        return {}
    _emit("warning", "hook_structured_field_unexpected_type", field=name, kind=type(value).__name__)
    return {}


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
        # A cancellation can arrive WRAPPED. asyncio.TaskGroup (3.11+) raises a
        # BaseExceptionGroup, and isinstance(group, CancelledError) is False
        # however many CancelledErrors it carries -- so the check above alone
        # would swallow exactly the cancellation it exists to let through. The
        # tool tier calls run() from async handlers, and Phase 2's two async
        # tuple projectors are a plausible source of a group, so this is the
        # sibling of 52919bb20's fix rather than a hypothetical.
        if isinstance(exc, BaseExceptionGroup) and exc.subgroup(asyncio.CancelledError):
            raise
        _emit("warning", "hook_boundary_swallowed_exception", hook=hook, error=str(exc))
        # Also one line on stderr (nexus-3lc5s). The log file alone made a
        # swallowed crash indistinguishable, to anyone holding only the
        # process's streams, from a verb that ran and had nothing to say:
        # exit 0, both streams empty. stderr is never a decision channel.
        try:
            sys.stderr.write(f"[nx-hook] {hook}: swallowed {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001, S110 — a diagnostic must not become the crash it reports
            pass
        return HookResult(crashed=True)
