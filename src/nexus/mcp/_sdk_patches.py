# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Local corrections to the upstream ``mcp`` SDK, applied at server startup.

nexus-rjmyk. Each patch here is a defect we hit in production, carried locally
because the fix is not coming from upstream on any timescale that helps us.
These touch UNEXPORTED SDK internals under the ``mcp>=2.3,<3`` pin, so a
future SDK refactor must degrade them to a logged warning and NEVER crash
``nx-mcp`` startup. A server that will not boot is worse than the bug.

Every patch checks the SHAPE it is correcting before applying, and reports that
it did nothing when the shape is absent. That is what makes the patch
self-retiring: when upstream fixes the defect, the shape check stops matching,
the patch becomes a no-op, and its test says so out loud instead of the patch
silently reimplementing behaviour the SDK now has.

RETIRED BY THE mcp 2.x MIGRATION (nexus-sgmyl). Two of the three patches this
module carried fixed defects of mcp 1.x's session layer, and 2.3.0 fixed both
structurally rather than by a small edit, so the patches are deleted, not
kept as no-ops:

- ``cancellation_response``: 1.x ``RequestResponder.cancel()`` answered a
  request the client had just cancelled, and the client dropped the transport
  on the unknown id. ``mcp.shared.session`` no longer exists; the 2.x
  ``JSONRPCDispatcher`` never answers a request whose ``cancel_requested`` is
  set. ``tests/test_mcp_sdk_patches.py`` pins that guard in the SDK source.
- ``sync_tool_offload``: 1.x ran a sync tool body on the event loop. 2.x
  ``FuncMetadata.call_fn`` runs it with ``anyio.to_thread.run_sync``. The
  behavioural test in ``tests/test_mcp_sync_tool_offload.py`` stays as the pin.

What remains is ``tool_failure_is_error``, which is our contract (nexus-6b6hq),
not an SDK defect.
"""

from __future__ import annotations

import inspect
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


def _patch_tool_failure_is_error() -> str:
    """Turn a tool's marked failure return into an ``isError`` result.

    nexus-6b6hq. Not an SDK defect: the SDK reports a RAISED exception as
    ``is_error``, but rewords it as ``Error executing tool <name>: ...``, and
    our tools return their failures instead of raising them, because their
    module-level names are also called in-process. Wrapping at the
    ``Tool.from_function`` registration boundary leaves the module-level names
    exactly as they are; only what MCPServer registers changes (``search``,
    ``store_put`` and the rest are called synchronously by
    ``nexus.commands.doc`` and a long tail of tests, so they cannot become
    coroutines). The wrapped body returns a ``CallToolResult`` built by the
    SDK's own ``_convert_to_content`` from the unmarked value, which the SDK
    passes through untouched (``FuncMetadata.convert_result``), so the content
    reaches the client byte-identical and only ``is_error`` changes. What
    counts as a failure is in ``_tool_failure``.

    The wrapper keeps a sync body sync; the SDK then runs it on a worker
    thread (``FuncMetadata.call_fn``), so a slow tool never holds the loop.

    Returns a short status string naming what happened, for the caller to log.
    """
    try:
        from mcp.server.mcpserver.tools.base import Tool  # noqa: PLC0415 — SDK internal, probed
        from mcp.server.mcpserver.utilities.func_metadata import (  # noqa: PLC0415 — SDK internal, probed
            FuncMetadata,
        )
    except Exception as exc:  # noqa: BLE001 — boundary: a moved symbol must not kill startup
        return f"unavailable: {exc}"

    from nexus.mcp._tool_failure import (  # noqa: PLC0415 — only on the patch path
        failures_as_error_results,
        is_exempt,
    )

    current = Tool.__dict__.get("from_function")
    if current is None:
        return "unavailable: Tool has no from_function"
    if getattr(current, "_nx_failure_patched", False):
        return "already applied"

    convert = getattr(FuncMetadata, "convert_result", None)
    if convert is None:
        return "unavailable: FuncMetadata has no convert_result"
    try:
        source = inspect.getsource(convert)
    except (OSError, TypeError) as exc:
        return f"skipped: cannot read convert_result source ({exc})"
    try:
        from mcp.server.mcpserver.utilities.func_metadata import (  # noqa: PLC0415, F401 — SDK internal, probed
            _convert_to_content,
        )
    except Exception as exc:  # noqa: BLE001 — boundary: a moved symbol must not kill startup
        return f"unavailable: {exc}"
    if "CallToolResult" not in source:
        # The SDK no longer passes a returned CallToolResult through, so a
        # wrapped failure would be serialized as an ordinary success.
        return "unavailable: convert_result does not pass a CallToolResult through"

    underlying = current.__func__ if isinstance(current, classmethod) else current

    def from_function_failures_as_errors(cls: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
        name = kwargs.get("name") or getattr(fn, "__name__", "")
        if not is_exempt(name):
            fn = failures_as_error_results(fn)
        return underlying(cls, fn, *args, **kwargs)

    from_function_failures_as_errors._nx_failure_patched = True  # type: ignore[attr-defined]
    patched = classmethod(from_function_failures_as_errors)
    patched._nx_failure_patched = True  # type: ignore[attr-defined]
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
        results["tool_failure_is_error"] = _patch_tool_failure_is_error()
    except Exception as exc:  # noqa: BLE001 — boundary: no patch may kill startup
        results["tool_failure_is_error"] = f"failed: {exc}"

    # Deliberately SILENT. This runs at module import, before
    # configure_logging, so anything emitted here reaches no handler.
    # main() calls report_sdk_patches once logging exists (nexus-dgvsz).
    return results
