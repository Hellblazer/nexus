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
patch pattern the now-deleted RDR-126 §3 first-run banner dispatch hook
used, verified against mcp 1.27.1) and, per Hal's g6vb4 decision
(2026-07-21):

- **warns once** (structlog) the first time a per-call check finds the
  install newer than this process's startup baseline;
- **decorates import-shaped failures** — raised ``ImportError`` /
  ``AttributeError`` AND FastMCP ``isError`` results whose text carries an
  import-failure signature — with an actionable "stale MCP host … restart"
  message, ONLY when actually stale (a fresh host's import bug surfaces
  undecorated);
- **never refuses a call**: a stale host mostly works (cached modules keep
  serving); refusing would brick every live session on every upgrade.

TWO LAYOUTS, TWO REGIMES (nexus-utpuw.12)
-----------------------------------------
Everything above describes IN-PLACE replacement, and under the side-by-side
generation layout that premise dies: the running generation's files are never
touched by a flip, so the dist-info mtime detector reports ``stale=False``
forever on every migrated box. The replacement is exact --
``sys.prefix != readlink(<tools>/current)`` -- and it carries a DIFFERENT
verdict, so the two regimes are kept apart rather than blended:

* **Generation host.** Skew is real but BENIGN: the tree is intact, the module
  graph is coherent, and the host converges at its next spawn. One
  informational note, and the GH #1414 decoration is SUPPRESSED -- there, an
  ImportError is an ordinary defect, and "almost certainly upgrade skew, not a
  code defect" would send someone chasing a restart for a real bug.
* **Legacy uv tree** (an un-migrated box, or a migrated one where a stray
  ``uv tool upgrade conexus`` rebuilt the tree under a live holder --
  nexus-utpuw.7's recorded accepted risk). In-place replacement still really
  happens, so the mtime detector and the decoration below are still correct
  and are kept verbatim.

Observably, this ADDS the note on a generation host and changes nothing else:
that host previously read fresh forever, so it was never decorated either.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from nexus import install_layout, upgrade_finish

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


def _note_skew(**kw: object) -> None:
    """Seam for tests; emits the one-shot generation-skew note.

    INFO, not WARNING, and deliberately: under generations a skewed holder
    is consistent, and the nexus-utpuw acceptance criterion says the only
    output about live holders is informational.
    """
    _log.info("mcp_host_generation_skew", **kw)


def _stale_note(st: "upgrade_finish.SelfStaleness") -> str:
    return (
        f"[stale MCP host: this nx-mcp process started under conexus "
        f"{st.started_version}; the installed distribution is now "
        f"{st.installed_version}. The running process is executing old "
        f"code and cannot safely import newly-installed modules — this "
        f"error is almost certainly upgrade skew, not a code defect. "
        f"Restart your Claude session to clear it — MCP hosts are "
        f"session-bound, so `nx daemon restart-stale` reports them but "
        f"cannot cycle them; only the session owner can."
    )


def install_stale_host_hook(server: object) -> bool:
    """Wrap the MCP server's CallToolRequest handler with the staleness
    check, in whichever regime this host's layout calls for (module
    docstring). Returns False (logged at debug) when the LEGACY regime's
    baseline cannot be captured (source checkout without dist-info) or the
    FastMCP internals moved — MCP boot is never blocked. A generation host
    needs no dist-info and so installs regardless.

    FRAGILE COUPLING: reaches into ``server._mcp_server`` and patches
    ``request_handlers[CallToolRequest]`` — private FastMCP internals,
    verified against mcp 1.27.1. ``tests/test_stale_host.py`` exercises the
    real FastMCP path and goes red if they move.
    """
    generation = upgrade_finish.running_generation()
    baseline_mtime, baseline_version = 0.0, ""
    dist_info = None
    if generation is None:
        # LEGACY regime only. The generation regime compares sys.prefix
        # against the pointer and needs no dist-info at all; gating it behind
        # this baseline would leave the reframe disabled on exactly the
        # layout it was written for — the silent-rot shape this arc is about.
        try:
            baseline_mtime, baseline_version, dist_info = (
                upgrade_finish.install_dist_info()
            )
        except Exception as exc:  # noqa: BLE001 — no dist-info: nothing to compare against
            _log.debug(
                "stale_host_hook_no_baseline", error=f"{type(exc).__name__}: {exc}"
            )
            return False
    baseline = (baseline_mtime, baseline_version)
    try:
        from mcp import types  # type: ignore[import-not-found]  # noqa: PLC0415 — deferred heavy dep; mcp SDK loaded only when patching handler

        low = server._mcp_server  # type: ignore[attr-defined]
        key = types.CallToolRequest
        original = low.request_handlers.get(key)
        if original is None:
            return False

        state = {  # per-install, not module-global
            "stale": None, "warned": False, "noted": False,
        }

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

        def _check_skew() -> Path | None:
            """The CURRENT generation when this host is no longer running it;
            None when it matches, or when the layout cannot answer.

            NOT cached, unlike the legacy verdict beside it. The obvious
            rationale for caching — "the pointer only ever moves forward, so
            a host that has fallen behind never catches up" — is FALSE in
            this repo: ``nx_rollback_current`` in ``_install/flip.sh`` (.3)
            repoints ``current`` at ``previous``, and a rolled-back pointer
            can land back on this host's own generation. A cache would then
            hold a verdict the disk has retracted. Recomputing costs ONE
            ``readlink``, which is also why this regime needs no equivalent
            of the legacy fast path (critic MEDIUM-2): what that path exists
            to avoid is an ``importlib.metadata`` resolution per tool call,
            and there is none here to avoid. Substantive-critic, 2026-08-26.

            An ABSENT ``current`` (``is_stale`` raises) is not a verdict this
            formula can make, and a tool call is not the place to surface it
            — it reads as "no skew known", never as an error.
            """
            try:
                if not install_layout.is_stale(generation):
                    return None
                return install_layout.current_generation()
            except Exception:  # noqa: BLE001 — the check must never break a tool call
                return None

        async def _wrapped_generation(req: object) -> object:
            """Generation regime: note once, decorate NEVER, refuse never.

            No decoration here is the whole point (module docstring): this
            host's tree was not touched by the flip, so an import-shaped
            failure is a real defect and labelling it upgrade skew would
            misdirect the reader.

            The note DOES promise convergence, which ``spawn_tripwire``
            deliberately does not, and the difference is real rather than
            sloppiness: an MCP host is long-lived and launched by BARE
            COMMAND NAME (``conexus/.mcp.json`` runs ``nx-mcp`` /
            ``nx-mcp-catalog``), so it resolves through the shim and its skew
            comes from a flip landing after startup — which the next respawn
            does clear. A hand-configured client pointing at an absolute
            generation path would not converge; that is outside how this
            project launches its servers.
            """
            current = _check_skew()
            if current is not None and not state["noted"]:
                state["noted"] = True
                _note_skew(
                    running=str(generation),
                    current=str(current),
                    detail=(
                        f"this MCP host is running generation "
                        f"{generation.name}; current is {current.name}. Its "
                        f"tree is intact — it is serving coherent code and "
                        f"converges when the session next respawns it."
                    ),
                )
            return await original(req)

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

        low.request_handlers[key] = (
            _wrapped_generation if generation is not None else _wrapped
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never block boot on the staleness hook
        _log.debug(
            "stale_host_hook_install_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return False
