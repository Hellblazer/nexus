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

    for name, status in results.items():
        if status == "applied":
            _log.debug("mcp_sdk_patch_applied", patch=name)
        else:
            _log.debug("mcp_sdk_patch_not_applied", patch=name, status=status)
    return results
