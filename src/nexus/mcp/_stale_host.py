# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-g6vb4 (GH #1414): MCP-host staleness self-detection — decorate + warn.

An in-place ``uv tool upgrade conexus`` replaces site-packages under a live
nx-mcp process. The already-imported module graph stays OLD; the first
deferred import after the upgrade reads a NEW module off disk, which may
reference names the cached old modules don't export — an ImportError for a
name that demonstrably exists on disk and imports fine in a fresh
interpreter (the GH #1414 incident chain, verified). Nothing in the failing
tool call pointed at staleness: ``detect_stale_processes()`` skips
``pid == me`` by construction, so the host must self-detect.

This hook wraps the CallToolRequest handler (the same FastMCP-internals
patch as ``_first_run.install_banner_dispatch_hook``, verified against mcp
1.27.1) and, per Hal's g6vb4 decision (2026-07-21):

- **warns once** (structlog) the first time a per-call check finds the
  install newer than this process's startup baseline;
- **decorates import-shaped failures** — raised ``ImportError`` /
  ``AttributeError`` AND FastMCP ``isError`` results whose text carries an
  import-failure signature — with an actionable "stale MCP host … restart"
  message, ONLY when actually stale (a fresh host's import bug surfaces
  undecorated);
- **never refuses a call**: a stale host mostly works (cached modules keep
  serving); refusing would brick every live session on every upgrade.
"""
from __future__ import annotations

import structlog

from nexus import upgrade_finish

_log = structlog.get_logger(__name__)

#: Substrings identifying an import-shaped failure text (FastMCP renders the
#: exception's ``str`` into the isError content block).
_IMPORT_MARKERS: tuple[str, ...] = (
    "ImportError",
    "ModuleNotFoundError",
    "cannot import name",
    "No module named",
    "AttributeError",
    "has no attribute",
)


def _warn(**kw: object) -> None:
    """Seam for tests; emits the one-shot staleness warning."""
    _log.warning("mcp_host_stale", **kw)


def _stale_note(st: "upgrade_finish.SelfStaleness") -> str:
    return (
        f"[stale MCP host: this nx-mcp process started under conexus "
        f"{st.started_version}; the installed distribution is now "
        f"{st.installed_version}. The running process is executing old "
        f"code and cannot safely import newly-installed modules — this "
        f"error is almost certainly upgrade skew, not a code defect. "
        f"Restart the MCP host to clear it (restart your Claude "
        f"session, or `nx daemon restart-stale` from a fresh shell)."
    )


def install_stale_host_hook(server: object) -> bool:
    """Wrap the MCP server's CallToolRequest handler with the staleness
    check. Returns False (logged at debug) when the baseline cannot be
    captured (source checkout without dist-info) or the FastMCP internals
    moved — MCP boot is never blocked.

    FRAGILE COUPLING (shared with ``install_banner_dispatch_hook``): reaches
    into ``server._mcp_server`` and patches
    ``request_handlers[CallToolRequest]`` — private FastMCP internals,
    verified against mcp 1.27.1. ``tests/test_stale_host.py`` exercises the
    real FastMCP path and goes red if they move.
    """
    try:
        baseline_mtime, baseline_version, dist_info = (
            upgrade_finish.install_dist_info()
        )
        baseline = (baseline_mtime, baseline_version)
    except Exception as exc:  # noqa: BLE001 — no dist-info: nothing to compare against
        _log.debug(
            "stale_host_hook_no_baseline", error=f"{type(exc).__name__}: {exc}"
        )
        return False
    try:
        from mcp import types  # type: ignore[import-not-found]  # noqa: PLC0415 — deferred heavy dep; mcp SDK loaded only when patching handler

        low = server._mcp_server  # type: ignore[attr-defined]
        key = types.CallToolRequest
        original = low.request_handlers.get(key)
        if original is None:
            return False

        state = {"stale": None, "warned": False}  # per-install, not module-global

        def _check() -> "upgrade_finish.SelfStaleness | None":
            """Evaluate (or replay) staleness; never raises.

            Fast path (the common, never-upgraded case): a single ``stat``
            of the startup dist-info path — an upgrade either bumps its
            mtime (same-version reinstall) or removes the directory
            (version change renames it), so an unchanged stat proves
            fresh with no importlib.metadata resolution (critic MEDIUM-2).
            Once stale, the verdict is cached — the disk cannot move back
            under this process in a way that makes its module graph young
            again.
            """
            try:
                cached = state["stale"]
                if cached is not None:
                    return cached
                try:
                    if dist_info.stat().st_mtime <= baseline_mtime:
                        return upgrade_finish.SelfStaleness(
                            stale=False,
                            started_version=baseline_version,
                            installed_version=baseline_version,
                        )
                except OSError:
                    pass  # dist-info gone/renamed: resolve fully below
                st = upgrade_finish.self_staleness(baseline)
                if st.stale:
                    state["stale"] = st
                return st
            except Exception:  # noqa: BLE001 — the check must never break a tool call
                return None

        def _fresh_verdict() -> "upgrade_finish.SelfStaleness | None":
            """Re-resolve at decoration time (rare, error path) so the note
            names the CURRENT installed version even after a second in-place
            upgrade in the same long-lived process (critic LOW-1)."""
            try:
                st = upgrade_finish.self_staleness(baseline)
                return st if st.stale else None
            except Exception:  # noqa: BLE001 — decoration must never break the error path
                return None

        async def _wrapped(req: object) -> object:
            st = _check()
            if st is not None and st.stale and not state["warned"]:
                state["warned"] = True
                _warn(
                    started_version=st.started_version,
                    installed_version=st.installed_version,
                )
            try:
                result = await original(req)
            except (ImportError, AttributeError) as exc:
                # Handler-level import failure (rare; tool-body failures are
                # normally converted to isError results by FastMCP below).
                fresh = _fresh_verdict()
                if fresh is not None:
                    raise type(exc)(f"{exc} {_stale_note(fresh)}") from exc
                raise
            if st is not None and st.stale:
                try:
                    inner = getattr(result, "root", result)
                    if getattr(inner, "isError", False):
                        content = getattr(inner, "content", None) or []
                        first = content[0] if content else None
                        text = getattr(first, "text", None)
                        if isinstance(text, str) and any(
                            m in text for m in _IMPORT_MARKERS
                        ):
                            fresh = _fresh_verdict() or st
                            first.text = f"{text}\n\n{_stale_note(fresh)}"
                except Exception as exc:  # noqa: BLE001 — never corrupt a tool result
                    _log.debug(
                        "stale_host_decorate_failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
            return result

        low.request_handlers[key] = _wrapped
        return True
    except Exception as exc:  # noqa: BLE001 — never block boot on the staleness hook
        _log.debug(
            "stale_host_hook_install_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return False
