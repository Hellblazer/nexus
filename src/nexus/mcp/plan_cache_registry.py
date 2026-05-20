"""Process-scoped registry for the T1 plan-session cache (nexus-sl69o).

Encapsulates the five module-level names that previously lived in
``nexus.mcp_infra``:
- ``_plan_cache_instance`` (the cache or sentinel)
- ``_plan_cache_lock``
- ``_plan_cache_populated``
- ``_plan_cache_mtime``
- ``_PLAN_CACHE_UNAVAILABLE`` sentinel

into a single :class:`PlanCacheRegistry` held as one module-level
singleton via :func:`get_plan_cache_registry`. Public API contracts are
preserved at ``nexus.mcp_infra.get_t1_plan_cache`` and
``nexus.mcp_infra.reset_plan_cache_for_tests``; this module is the
implementation backing store.

**Scope honesty**: this is encapsulation, not "DI top-down" in the
strict sense. The cache must be process-scoped (its whole purpose is
to amortize PlanLibrary-population cost across MCP tool calls within
one server process). Nexus's MCP handlers are top-level functions
registered with FastMCP, not methods on a server class, so there's no
server-instance to attach the registry to via constructor injection.
Strict DI would require restructuring the MCP handler surface into a
class; out of scope for nexus-sl69o.

What IS achieved here:
- One named module-level singleton (the registry) instead of five
- Cache state cohesive in a class with proper methods
- Test substitution via :func:`reset_plan_cache_registry_for_tests`
  (same shape as the prior ``reset_plan_cache_for_tests``, cleaner
  internals)
- The nexus-qgjr mtime-guarded refresh contract preserved as
  :meth:`PlanCacheRegistry.get`'s populate-when-stale logic

See ``nx memory get nexus/design-singleton-elimination-voyage-and-plan-cache.md``
for the approved design and the honest scoping note.
"""
from __future__ import annotations

import threading
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


# Sentinel distinct from None: stored on the registry when init fails so
# subsequent get() calls short-circuit without re-attempting under the
# lock. Matches the prior _PLAN_CACHE_UNAVAILABLE semantics.
_UNAVAILABLE = object()


def _plan_library_mtime(library: Any) -> float:
    """Return the SQLite file mtime for *library*, or 0.0 when unknown.

    Falls back to 0.0 when the library does not expose a ``path``
    attribute (in-memory or test-stub libraries) or when the file is
    missing; both produce a stable repopulate-never-runs fallback that
    matches the legacy single-populate contract.
    """
    path = getattr(library, "path", None)
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class PlanCacheRegistry:
    """Process-scoped registry around the T1 plan-session cache.

    Lifecycle:
    - Constructed once per MCP server process via
      :func:`get_plan_cache_registry`.
    - First :meth:`get` lazy-initialises the underlying
      :class:`nexus.plans.session_cache.PlanSessionCache` from the
      live T1 client (via ``nexus.mcp_infra.get_t1``).
    - On init failure, the registry's ``cache`` slot is set to the
      ``_UNAVAILABLE`` sentinel; subsequent :meth:`get` calls
      short-circuit and return None without re-entering the lock.
    - When ``populate_from`` is supplied, the cache is repopulated
      whenever the underlying PlanLibrary SQLite file mtime advances
      (nexus-qgjr mtime-guarded refresh).

    Test isolation: :func:`reset_plan_cache_registry_for_tests` drops
    the module-level singleton so the next ``get_plan_cache_registry()``
    constructs a fresh registry.
    """

    def __init__(self) -> None:
        self._cache: Any = None  # PlanSessionCache | _UNAVAILABLE sentinel | None
        self._lock = threading.Lock()
        self._populated: bool = False
        self._mtime: float = 0.0  # SQLite file mtime captured at most recent populate

    def get(self, *, populate_from: Any = None) -> Any:
        """Return the T1 ``plans__session`` cache, lazy-populated on first call.

        When *populate_from* (a PlanLibrary) is supplied, the cache is
        populated from its rows on first call and repopulated whenever
        the underlying SQLite file mtime advances (nexus-qgjr). The
        mtime check mirrors the catalog's ``_last_consistency_mtime``
        pattern at ``catalog.py:405``: cheap when nothing changed,
        rebuilds when a write moves the file's stat-time.

        Libraries without a ``path`` attribute fall back to populate-
        once semantics; the mtime tier costs them nothing.

        Returns ``None`` when no T1 client is reachable; the matcher
        falls back to FTS5 in that case. Subsequent calls after an
        init failure return ``None`` immediately without re-entering
        the lock (see ``_UNAVAILABLE`` sentinel).
        """
        if self._cache is _UNAVAILABLE:
            return None
        if self._cache is None:
            with self._lock:
                if self._cache is None:
                    try:
                        from nexus.mcp_infra import get_t1
                        from nexus.plans.session_cache import PlanSessionCache
                        t1, _ = get_t1()
                        self._cache = PlanSessionCache(
                            client=t1._client, session_id=t1.session_id,
                        )
                    except Exception:
                        self._cache = _UNAVAILABLE
        if self._cache is _UNAVAILABLE:
            return None
        if populate_from is not None:
            current_mtime = _plan_library_mtime(populate_from)
            with self._lock:
                stale = (
                    not self._populated
                    or (current_mtime > 0.0 and current_mtime > self._mtime)
                )
                if stale:
                    # Only mark populated/mtime on success. Pre-refactor
                    # code used `try/finally` which permanently suppressed
                    # populate failures (a transient network blip during
                    # plan-embedding would set `_populated = True` and
                    # `_mtime = current_mtime`, so the next call saw
                    # stale=False and never retried). The fix keeps the
                    # cache instance available (we don't reset
                    # `self._cache`) but leaves `_populated` / `_mtime`
                    # unchanged so the next call retries the populate.
                    try:
                        self._cache.populate(populate_from)
                    except Exception:
                        _log.warning(
                            "plan_cache_populate_failed",
                            library=str(populate_from),
                            exc_info=True,
                        )
                    else:
                        self._populated = True
                        self._mtime = current_mtime
        return self._cache

    def clear(self) -> None:
        """Drop the cache state. Used by ``reset_plan_cache_registry_for_tests``."""
        with self._lock:
            self._cache = None
            self._populated = False
            self._mtime = 0.0


# Module-level singleton. The cache MUST be process-scoped (the whole
# point is to amortize PlanLibrary population across MCP tool calls),
# so a module-level holder is unavoidable in nexus's current MCP
# architecture. See module docstring for the honest scoping note.
_registry: PlanCacheRegistry | None = None
_registry_lock = threading.Lock()


def get_plan_cache_registry() -> PlanCacheRegistry:
    """Return the process-scoped :class:`PlanCacheRegistry`, lazy-creating on first call."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = PlanCacheRegistry()
    return _registry


def reset_plan_cache_registry_for_tests() -> None:
    """Test helper: drop the registry singleton so the next call constructs fresh.

    Public surface ``nexus.mcp_infra.reset_plan_cache_for_tests`` delegates here.
    """
    global _registry
    with _registry_lock:
        _registry = None
