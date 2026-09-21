# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Local corrections to the upstream ``mcp`` SDK, applied at server startup.

nexus-rjmyk. Each patch here is a defect we hit in production, carried locally
because the fix is not coming from upstream on any timescale that helps us.
Same guard discipline as ``core.py``'s ``Settings.model_rebuild`` fix: these
touch UNEXPORTED SDK internals under an unbounded ``mcp>=1.0,<2`` pin, so a
future SDK refactor must degrade them to a logged warning and NEVER crash
``nx-mcp`` startup. A server that will not boot is worse than the bug.

Every patch checks the SHAPE it is correcting before applying, and reports that
it did nothing when the shape is absent. That is what makes the patch
self-retiring: when upstream fixes the defect, the shape check stops matching,
the patch becomes a no-op, and its test says so out loud instead of the patch
silently reimplementing behaviour the SDK now has.
"""

from __future__ import annotations

import inspect
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: The attribute name ``RequestResponder.cancel`` must CALL for the
#: cancellation patch to be correcting something. Checked rather than assumed:
#: see the module docstring on self-retiring.
_CANCEL_BUG_CALL = "_send_response"


def _calls_attribute(source: str, attribute: str) -> bool:
    """Whether *source* actually CALLS ``....<attribute>(...)``.

    Parsed, never grepped. A substring test reports a hit on any mention of the
    name -- including the one in this module's own replacement docstring, which
    says which call it removed. That is how the first version of this check
    reported the bug still present in code that no longer had it, and it is the
    same trap twice over: grep cannot tell an identifier from a sentence about
    one. Dedented first, because the method arrives indented from its class.
    """
    import ast  # noqa: PLC0415 — only on the patch path
    import textwrap  # noqa: PLC0415 — only on the patch path

    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        for node in ast.walk(tree)
    )


def _patch_cancellation_response() -> str:
    """Stop the SDK answering a request it has just cancelled.

    THE DEFECT, measured 2026-09-20 across 121 Claude Code client logs for this
    one server: ten teardowns of the stdio transport, each immediately preceded
    by ``Connection error: Received a response for an unknown message ID``.
    Five of them carried ``{"code":0,"message":"Request cancelled"}``.

    ``mcp/shared/session.py`` (1.27.1, and byte-identical in 1.30.0, the newest
    1.x -- so bumping inside our pin fixes nothing)::

        async def cancel(self) -> None:
            self._cancel_scope.cancel()
            self._completed = True
            await self._session._send_response(
                request_id=self.request_id,
                response=ErrorData(code=0, message="Request cancelled", ...),
            )

    The client cancelled, so it has already discarded that request id. The
    reply arrives as an id the client does not know, and the client treats that
    as fatal to the whole transport -- every tool on the server goes away at
    once, and at least once it did not come back without a human reconnecting.

    THIS IS NOT US INVENTING A POLICY. Two methods above, ``respond()`` guards
    with ``if not self.cancelled`` before sending. The SDK already holds that a
    cancelled request gets no response; ``cancel()`` simply disagrees with its
    own neighbour. This patch makes the two agree.

    Returns a short status string naming what happened, for the caller to log.
    """
    try:
        from mcp.shared.session import RequestResponder  # noqa: PLC0415 — SDK internal, probed
    except Exception as exc:  # noqa: BLE001 — boundary: a moved symbol must not kill startup
        return f"unavailable: {exc}"

    cancel = getattr(RequestResponder, "cancel", None)
    if cancel is None:
        return "unavailable: RequestResponder has no cancel()"

    if getattr(cancel, "_nx_patched", False):
        return "already applied"

    try:
        source = inspect.getsource(cancel)
    except (OSError, TypeError) as exc:
        # Cannot read it, so cannot prove it is the shape we mean to correct.
        # Refuse rather than patch blind: an unconditional override here would
        # silently replace whatever upstream does now.
        return f"skipped: cannot read cancel() source ({exc})"

    if not _calls_attribute(source, _CANCEL_BUG_CALL):
        # Upstream fixed it, or restructured past it. Nothing to correct.
        return "not needed: cancel() no longer sends a response"

    async def cancel_without_response(self: Any) -> None:
        """Cancel the request and complete it, sending NOTHING.

        Mirrors the upstream body with the ``_send_response`` call removed.
        The two guards are kept verbatim: they are the SDK's own invariants
        about being inside the context manager, and dropping them would turn a
        clear RuntimeError into an AttributeError somewhere further away.
        """
        if not self._entered:
            raise RuntimeError("RequestResponder must be used as a context manager")
        if not self._cancel_scope:
            raise RuntimeError("No active cancel scope")

        self._cancel_scope.cancel()
        # Marked complete so the responder leaves in_flight, exactly as
        # upstream does. Only the response is suppressed.
        self._completed = True

    cancel_without_response._nx_patched = True  # type: ignore[attr-defined]
    RequestResponder.cancel = cancel_without_response  # type: ignore[assignment]
    return "applied"


#: The pristine ``Tool.from_function`` as it was before the offload patch
#: replaced it, captured on first application. Exists for the non-vacuity
#: test: importing ``nexus.mcp`` applies the patches, so the unpatched
#: baseline is otherwise unreachable from inside the process.
_ORIGINAL_TOOL_FROM_FUNCTION: Any = None


def restore_sync_tool_offload() -> bool:
    """Put the SDK's own ``Tool.from_function`` back. Test support only.

    Returns True when something was restored. Not used by the server.
    """
    if _ORIGINAL_TOOL_FROM_FUNCTION is None:
        return False
    from mcp.server.fastmcp.tools.base import Tool  # noqa: PLC0415 — SDK internal, probed

    Tool.from_function = _ORIGINAL_TOOL_FROM_FUNCTION  # type: ignore[assignment]
    return True


def _offloaded(fn: Any) -> Any:
    """Wrap a blocking sync tool body so it runs off the event loop.

    ``functools.wraps`` keeps ``__wrapped__``, so ``inspect.signature`` still
    reports the ORIGINAL signature -- which is what FastMCP builds the tool's
    JSON schema from. The wire contract is therefore untouched; only where the
    body executes changes.
    """
    import asyncio  # noqa: PLC0415 — only on the patch path
    import functools  # noqa: PLC0415 — only on the patch path

    @functools.wraps(fn)
    async def _run_in_thread(*args: Any, **kwargs: Any) -> Any:
        # to_thread copies the current contextvars into the worker, so
        # structlog's bound context and anything else contextvar-scoped
        # survives the hop.
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    return _run_in_thread


def _patch_sync_tool_offload() -> str:
    """Run sync ``@mcp.tool()`` bodies in a thread instead of on the loop.

    THE DEFECT, measured against the installed SDK::

        slow_sync    slow finished at 2.00s, fast finished at 2.00s  -> BLOCKED
        slow_async   slow finished at 2.00s, fast finished at 0.05s  -> free

    ``mcp/server/fastmcp/utilities/func_metadata.py`` calls a sync tool body
    DIRECTLY from inside its async dispatch::

        if fn_is_async:
            return await fn(**arguments_parsed_dict)
        else:
            return fn(**arguments_parsed_dict)

    There is no thread offload anywhere on that path, so one sync tool body
    holds the whole server's event loop for its entire duration. Ours are not
    cheap: store_get 19s, search 14s, store_put 8-10s, all measured on a live
    connection. Meanwhile the conexus hooks are wired as ``mcp_tool`` entries
    with 5-10s timeouts on that SAME connection, so they do not lose a race --
    they never get to start.

    What that costs is not slowness, it is the transport. The client abandons
    the request id at its timeout, the loop eventually frees, the server
    answers anyway, and the late answer arrives as an id the client no longer
    knows -- the same unknown-id teardown ``_patch_cancellation_response``
    addresses from the other side.

    WHAT IS MEASURED AND WHAT IS INFERRED, kept apart deliberately. Measured:
    the blocking above, in this repo, against this SDK; and one teardown at
    2026-09-21T08:31:16Z whose two stray ids were late RESULTS, id 48
    carrying a ``hook_subagent_start`` response and id 49 an empty one.
    Inferred, and NOT established: that those two ids were starved by a
    slow tool body in particular. The client log records no outbound
    notifications and no ``Calling MCP tool: hook_*`` lines, so the path
    from "the loop was blocked" to "this teardown happened" is a hypothesis
    that fits, not an observation. This patch is justified by the measured
    blocking on its own; whether it ends the teardowns is settled by
    watching for new unknown-id lines in the client logs under load, which
    is nexus-rjmyk's own standing bar.

    Patched at the REGISTRATION boundary rather than at the 59 call sites,
    for one reason that matters: ``search``, ``store_put`` and the rest are
    imported and called synchronously by ``nexus.commands.doc`` and by a long
    tail of tests. Turning those module-level names into coroutines would
    break every one of them. Wrapping what FastMCP registers leaves the
    module-level names exactly as they are.

    Returns a short status string naming what happened, for the caller to log.
    """
    try:
        from mcp.server.fastmcp.tools.base import Tool  # noqa: PLC0415 — SDK internal, probed
        from mcp.server.fastmcp.utilities.func_metadata import (  # noqa: PLC0415 — SDK internal, probed
            FuncMetadata,
        )
    except Exception as exc:  # noqa: BLE001 — boundary: a moved symbol must not kill startup
        return f"unavailable: {exc}"

    try:
        from mcp.server.fastmcp.tools.base import _is_async_callable  # noqa: PLC0415 — SDK internal, probed
    except Exception:  # noqa: BLE001 — private helper; the stdlib check is equivalent here
        _is_async_callable = inspect.iscoroutinefunction  # type: ignore[assignment]

    original = Tool.__dict__.get("from_function")
    if original is None:
        return "unavailable: Tool has no from_function"
    if getattr(original, "_nx_patched", False):
        return "already applied"

    dispatch = getattr(FuncMetadata, "call_fn_with_arg_validation", None)
    if dispatch is None:
        return "unavailable: FuncMetadata has no call_fn_with_arg_validation"
    try:
        source = inspect.getsource(dispatch)
    except (OSError, TypeError) as exc:
        # Cannot read it, so cannot prove the SDK still runs sync bodies on the
        # loop. Refuse rather than wrap blind.
        return f"skipped: cannot read call_fn_with_arg_validation source ({exc})"

    if _calls_attribute(source, "to_thread"):
        # Upstream started offloading. Wrapping on top of that would put the
        # body in a thread inside a thread for no gain.
        return "not needed: the SDK already offloads sync tool bodies"

    underlying = original.__func__ if isinstance(original, classmethod) else original

    # Kept so a test can measure the UNPATCHED behaviour. Importing
    # nexus.mcp applies these patches as a side effect, so without this a
    # "without the patch" test silently measures the patched path and the
    # non-vacuity leg proves nothing.
    global _ORIGINAL_TOOL_FROM_FUNCTION
    if _ORIGINAL_TOOL_FROM_FUNCTION is None:
        _ORIGINAL_TOOL_FROM_FUNCTION = original

    def from_function_offloading_sync(cls: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_async_callable(fn):
            fn = _offloaded(fn)
        return underlying(cls, fn, *args, **kwargs)

    from_function_offloading_sync._nx_patched = True  # type: ignore[attr-defined]
    patched = classmethod(from_function_offloading_sync)
    patched._nx_patched = True  # type: ignore[attr-defined]
    Tool.from_function = patched  # type: ignore[assignment]
    return "applied"


def report_sdk_patches(log: Any, results: dict[str, str], **extra: Any) -> None:
    """Emit one line per patch status, at INFO.

    Separate from :func:`apply_sdk_patches` because of WHEN each has to run.
    Applying must happen at module import — the offload patch replaces
    ``Tool.from_function``, and the ``@mcp.tool()`` decorators that call it run
    at import as well — but ``configure_logging`` does not run until ``main()``.
    A status logged from inside the apply therefore reaches no handler and is
    dropped, which is what made "is the patch live in this process?" an audit
    instead of a grep. Moving the level from DEBUG to INFO did not fix that:
    the level was never the problem, the timing was (nexus-dgvsz).
    """
    for name, status in results.items():
        log.info(
            "mcp_sdk_patch_applied" if status == "applied"
            else "mcp_sdk_patch_not_applied",
            patch=name,
            status=status,
            **extra,
        )


def apply_sdk_patches() -> dict[str, str]:
    """Apply every local SDK correction. Never raises.

    Returns a mapping of patch name to status, which the caller logs. Startup
    continues whatever the statuses say -- see the module docstring.
    """
    results: dict[str, str] = {}
    try:
        results["cancellation_response"] = _patch_cancellation_response()
    except Exception as exc:  # noqa: BLE001 — boundary: no patch may kill startup
        results["cancellation_response"] = f"failed: {exc}"
    try:
        results["sync_tool_offload"] = _patch_sync_tool_offload()
    except Exception as exc:  # noqa: BLE001 — boundary: no patch may kill startup
        results["sync_tool_offload"] = f"failed: {exc}"

    # INFO, not DEBUG. These patches are the difference between a transport
    # that survives a fan-out and one that does not, and at DEBUG under an
    # INFO log they left no trace at all: 3.5 MB of mcp.log carried zero
    # occurrences, so "is the patch live in this process?" took an audit to
    # answer instead of a grep (nexus-dgvsz).
    # Deliberately SILENT. This runs at module import, before
    # configure_logging, so anything emitted here reaches no handler.
    # main() calls report_sdk_patches once logging exists (nexus-dgvsz).
    return results
