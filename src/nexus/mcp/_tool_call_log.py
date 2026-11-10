# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""One event per ``tools/call``: the tool name and the keys of ``_meta``.

nexus-2nxoo (shakeout F18). nx-mcp logged no tool calls, so nobody could say
whether a client (the Claude Desktop remote-devices bridge) sends per-request
``_meta`` such as a session id. This answers that from ``mcp.log``.

KEYS ONLY. ``_meta`` and the call arguments can carry tokens, so neither a
value nor an argument is ever read into the event. That holds for this event
only: under ``NEXUS_LOG_LEVEL=DEBUG`` the mcp SDK itself logs full request
messages (``lowlevel/server.py``, "Received message"), arguments and ``_meta``
values included.

Level: INFO the FIRST time a given key-set is seen in this process, DEBUG
after that. The ``mcp`` entry point logs at INFO by default
(``logging_setup._resolve_level``), and the server Claude Desktop starts has no
editable environment, so the case F18 asks about (a client forwarding a session
id or any other key) must reach ``mcp.log`` without ``NEXUS_LOG_LEVEL=DEBUG``.
Claude Code 2.1.x sends ``claudecode/toolUseId`` in ``_meta`` on every
``tools/call``, so a rule keyed on "any key besides progressToken" would log
every call (about 1 MB/day); keyed on the key-set, a steady client adds one
INFO line per distinct key-set per process. The remembered set is bounded
(``_MAX_KEYSETS``); once it is full, new key-sets log at DEBUG and one
``mcp_tool_call_keyset_limit`` INFO line says so.

Every event carries ``pid``, because ``mcp.log`` is shared by many nx-mcp
processes. A probe finds pid N from ``mcp_server_starting``, checks
``mcp_sdk_patch_applied patch=tool_call_log pid=N`` (the hook is live in that
process), then greps ``mcp_tool_call pid=N`` after that start line's timestamp
(pids are reused across a long ``mcp.log``, so bound the match by the
``mcp_server_starting`` time). The listed keys are capped
(``_MAX_KEYS`` keys, each cut to ``_MAX_KEY_LEN`` characters, with
``meta_keys_truncated`` set when anything was cut) so a hostile client cannot
grow a line.

FAILURES (nexus-3ceda.7). The key-set rule means ``mcp_tool_call`` is not one
line per call, so it cannot say how often an agent retries after an error. Two
more INFO events can: ``mcp_tool_error`` for every ``tools/call`` whose result
is ``isError`` or that raised (a marked failure, a ``ToolError``, a crash, an
unknown tool), with ``streak`` (consecutive failures of that tool), ``calls``
(the process's tools/call count so far) and ``superseded`` (it replaced a
failure no call had followed yet); and ``mcp_tool_after_error`` for the next
call that process makes, with ``failed_tool``, ``same_tool``, ``same_args``
(compared through a per-process keyed digest held in memory, never logged) and
``seconds`` since the failure. Both carry ``schema``. A blind retry is
``same_tool`` and ``same_args``; a corrected one is ``same_tool`` without
``same_args``. How the rate is computed (time bounds, denominator, windows) is
in the bead. The volume is one or two lines per failure. Like
``mcp_tool_call``, neither line carries an argument, a result or the error text.

HOOK. A ``Server.middleware`` entry (mcp 2.x; the 1.x ``request_handlers``
wrap is gone with the decorator registry, nexus-sgmyl). The chain runs ahead of
every inbound message, so the entry acts only on ``ctx.method == "tools/call"``
and passes everything else straight through. Middleware runs BEFORE params
validation, so the keys are read from the raw wire params: ``name`` and the
keys of ``_meta``, never a value. The middleware list is documented as
provisional by the SDK ("may change in a 2.x minor release"), so the install
degrades to a status string, never an exception: an unverifiable SDK shape must
not break startup (nexus-vc5yb / nexus-3hc9z).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import structlog

#: Distinct key-sets remembered per process; past it, new ones log at DEBUG.
_MAX_KEYSETS = 64
#: Most keys one event lists, and the longest a listed key may be.
_MAX_KEYS = 32
_MAX_KEY_LEN = 128

#: Key-sets already logged at INFO in this process. Module state: one process
#: serves one MCP server. Tests clear it with ``_reset_state``. No lock: the
#: wrapper is an async request handler and ``_emit`` has no ``await``, so it
#: runs to completion on the event-loop thread and the check-then-add below
#: cannot interleave. Calling ``_emit`` off that thread would need a lock.
_seen_keysets: set[tuple[tuple[str, ...], bool]] = set()
_limit_noted = False

#: The failure lines' format. Step 3 of nexus-3ceda.7 bumps it, so an analysis
#: can tell the code that wrote a line without a version in the start lines.
FAILURE_SCHEMA = 1

#: The call that last failed in this process: its tool, when (``_clock``), and a
#: keyed digest of its arguments, until the next ``tools/call`` logs what the
#: agent did after it (nexus-3ceda.7). One slot: a failure while another is
#: still pending replaces it, and its error line says ``superseded=True``. Same
#: single-thread reasoning as ``_seen_keysets``.
_last_failure: tuple[str | None, float, bytes] | None = None
#: Consecutive failures per tool in this process, bounded like ``_seen_keysets``.
_streaks: dict[str, int] = {}
#: ``tools/call`` requests this process has seen, for errors per call.
_calls = 0
_clock: Callable[[], float] = time.monotonic
#: Per-process key for the argument digest: the digest never leaves memory, and
#: the key keeps it from being a stable fingerprint of the arguments even there.
_ARGS_KEY: bytes = os.urandom(16)


def _reset_state() -> None:
    """Forget the remembered key-sets (tests; a process never needs it)."""
    global _limit_noted, _cwd_healed, _last_failure, _calls
    _seen_keysets.clear()
    _limit_noted = False
    _cwd_healed = False
    _last_failure = None
    _streaks.clear()
    _calls = 0


def _meta_keys(params: Any) -> list[str]:
    """Sorted keys of the request's ``_meta``, ``[]`` when it has none.

    Read off the raw wire params, so these are exactly the keys the client sent:
    the declared ``progressToken`` when present plus every extra.
    """
    if not isinstance(params, Mapping):
        return []
    meta = params.get("_meta")
    if not isinstance(meta, Mapping):
        return []
    return sorted(str(k) for k in meta)


def _emit(params: Any) -> None:
    global _limit_noted
    # Fetched per call, not at import: this module loads before
    # configure_logging runs, and a logger bound earlier would miss the config.
    log = structlog.get_logger(__name__)
    all_keys = _meta_keys(params)
    kept = all_keys[:_MAX_KEYS]
    keys = [k[:_MAX_KEY_LEN] for k in kept]
    truncated = len(all_keys) > _MAX_KEYS or any(len(k) > _MAX_KEY_LEN for k in kept)
    shape = (tuple(keys), truncated)
    if shape in _seen_keysets:
        emit = log.debug
    elif len(_seen_keysets) < _MAX_KEYSETS:
        _seen_keysets.add(shape)
        emit = log.info
    else:
        emit = log.debug
        if not _limit_noted:
            _limit_noted = True
            log.info("mcp_tool_call_keyset_limit", limit=_MAX_KEYSETS, pid=os.getpid())
    extra: dict[str, Any] = (
        {"meta_keys_truncated": True, "meta_keys_total": len(all_keys)} if truncated else {}
    )
    name = params.get("name") if isinstance(params, Mapping) else None
    emit("mcp_tool_call", tool=name, meta_keys=keys, pid=os.getpid(), **extra)


def _tool_name(params: Any) -> str | None:
    name = params.get("name") if isinstance(params, Mapping) else None
    return name[:_MAX_KEY_LEN] if isinstance(name, str) else None


def _args_digest(params: Any) -> bytes:
    """A keyed digest of the call's arguments, compared in memory and never logged."""
    args = params.get("arguments") if isinstance(params, Mapping) else None
    try:
        text = json.dumps(args, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        text = repr(args)
    return hashlib.blake2b(text.encode("utf-8", "replace"), key=_ARGS_KEY, digest_size=16).digest()


def _note_next_call(params: Any) -> None:
    """Count the call; after a failure, log what the agent did next, once (nexus-3ceda.7).

    ``same_args`` separates a blind retry (same tool, same arguments) from a
    corrected one (same tool, other arguments)."""
    global _last_failure, _calls
    _calls += 1
    if _last_failure is None:
        return
    failed_tool, at, failed_args = _last_failure
    _last_failure = None
    tool = _tool_name(params)
    same_tool = tool is not None and tool == failed_tool
    structlog.get_logger(__name__).info(
        "mcp_tool_after_error", tool=tool, failed_tool=failed_tool, same_tool=same_tool,
        same_args=same_tool and _args_digest(params) == failed_args,
        seconds=round(_clock() - at, 1), schema=FAILURE_SCHEMA, pid=os.getpid(),
    )


def _is_error(result: Any) -> bool:
    if isinstance(result, Mapping):
        return result.get("isError") is True
    return getattr(result, "is_error", False) is True


def _note_outcome(params: Any, failed: bool) -> None:
    """Log a failed call and remember it for the next one. Tool name and outcome only:
    never an argument, a result or the error text, which can carry tokens."""
    global _last_failure
    tool = _tool_name(params)
    if not failed:
        if tool is not None:
            _streaks.pop(tool, None)
        return
    streak = _streaks.get(tool, 0) + 1 if tool is not None else 1
    if tool is not None and (tool in _streaks or len(_streaks) < _MAX_KEYSETS):
        _streaks[tool] = streak
    superseded = _last_failure is not None
    _last_failure = (tool, _clock(), _args_digest(params))
    structlog.get_logger(__name__).info(
        "mcp_tool_error", tool=tool, streak=streak, calls=_calls, superseded=superseded,
        schema=FAILURE_SCHEMA, pid=os.getpid(),
    )


_cwd_healed = False


def _heal_cwd() -> None:
    """nexus-x6qc7 (shakeout F19): this wrap already runs ahead of every tool call
    on both servers, so it also moves the process off a working directory its host
    deleted (Claude Desktop removes the plugin's install directory on update while
    the servers run there). Without it every subprocess a tool starts, ``claude -p``
    included, inherits the deleted directory. Logs once per process."""
    global _cwd_healed
    from nexus.util.stable_cwd import heal_deleted_cwd  # noqa: PLC0415 — stdlib-only, deferred like the SDK import

    if heal_deleted_cwd() and not _cwd_healed:
        _cwd_healed = True
        structlog.get_logger(__name__).warning("mcp_cwd_deleted_healed", cwd=os.getcwd(), pid=os.getpid())


def install_tool_call_log(server: object) -> str:
    """Add the per-call event to the server's middleware chain.

    Returns a short status for the caller to log; never raises.
    """
    try:
        middleware = server.middleware  # type: ignore[attr-defined]
        if any(getattr(m, "_nx_tool_call_log", False) for m in middleware):
            return "already applied"

        async def _logged(ctx: Any, call_next: Any) -> Any:
            if ctx.method == "tools/call":
                try:
                    _emit(ctx.params)
                except Exception:  # noqa: BLE001 — observability must never break a tool call
                    pass
                try:
                    _heal_cwd()
                except Exception:  # noqa: BLE001 — a failed chdir must never break a tool call
                    pass
                try:
                    _note_next_call(ctx.params)
                except Exception:  # noqa: BLE001 — observability must never break a tool call
                    pass
                try:
                    result = await call_next(ctx)
                except Exception:
                    try:
                        _note_outcome(ctx.params, True)
                    except Exception:  # noqa: BLE001 — observability must never break a tool call
                        pass
                    raise
                try:
                    _note_outcome(ctx.params, _is_error(result))
                except Exception:  # noqa: BLE001 — observability must never break a tool call
                    pass
                return result
            return await call_next(ctx)

        _logged._nx_tool_call_log = True  # type: ignore[attr-defined]
        middleware.append(_logged)
        return "applied"
    except Exception as exc:  # noqa: BLE001 — boundary: an unverifiable SDK shape must not kill startup
        return f"unavailable: {type(exc).__name__}: {exc}"
