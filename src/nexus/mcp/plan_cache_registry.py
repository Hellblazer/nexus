"""Process-scoped registry for the T1 plan-session cache (nexus-sl69o).

Encapsulates the five module-level names that previously lived in
``nexus.mcp_infra``:
- ``_plan_cache_instance`` (the cache or sentinel)
- ``_plan_cache_lock``
- ``_plan_cache_populated``
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
import time
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


# Sentinel distinct from None: stored on the registry when init fails so
# subsequent get() calls short-circuit without re-attempting under the
# lock. Matches the prior _PLAN_CACHE_UNAVAILABLE semantics.
_UNAVAILABLE = object()


# nexus-ie7o8: bounded-staleness refresh interval for the plan library
# (HttpPlanLibrary — the only library since nexus-i711w; nexus-x1de2 (54)
# removed the SQLite file-mtime branch that once sat beside this).
#
# This is NOT change detection. It is the honest fallback the bead calls
# for: the Java plans service (``service/.../http/PlanHandler.java`` —
# save/get/delete/disable/enable/list_active/search/list/exists/metrics/
# import) exposes no ETag, Last-Modified header, or cheap version/counter
# on any plan-listing route, so there is nothing free to poll. Instead,
# the cache is guaranteed to repopulate no less often than once per this
# many seconds, converting UNBOUNDED staleness (populate-once-per-process,
# forever) into a BOUNDED window. A plan added or edited elsewhere is
# invisible for at most this long, not for the life of the MCP process.
#
# 90s: short enough that a plan edited mid-session becomes visible within
# a couple of tool calls in a typical interactive session, long enough
# that back-to-back MCP tool calls (the common case — many calls per
# minute) don't pay the populate cost (one ONNX embed per active plan,
# see PlanSessionCache.populate) on every single call. Tracked via
# time.monotonic() specifically (not wall-clock time.time()) so the
# window can't be perturbed by NTP adjustments or DST changes.
_HTTP_PLAN_LIBRARY_STALENESS_SECONDS: float = 90.0




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
    - When ``populate_from`` is supplied, the cache is repopulated no
      less often than
        every :data:`_HTTP_PLAN_LIBRARY_STALENESS_SECONDS` seconds
        (nexus-ie7o8 bounded-staleness fallback — NOT change detection;
        see that constant's docstring for why no cheaper signal exists).
    - The actual repopulate is single-flight and runs UNLOCKED (review
      finding on f3b02373): only one thread ever executes
      ``PlanSessionCache.populate()`` (one HTTP round-trip plus one ONNX
      embed per active plan) at a time; concurrent callers arriving
      mid-populate get the current, stale-but-usable cache immediately
      rather than blocking or each launching their own populate. See
      :meth:`get` for the full single-flight / failure-path / clear()
      coherence contract.

    Test isolation: :func:`reset_plan_cache_registry_for_tests` drops
    the module-level singleton so the next ``get_plan_cache_registry()``
    constructs a fresh registry.
    """

    def __init__(self) -> None:
        self._cache: Any = None  # PlanSessionCache | _UNAVAILABLE sentinel | None
        self._lock = threading.Lock()
        self._populated: bool = False
        # nexus-ie7o8: monotonic-clock timestamp of the most recent
        # successful populate, used ONLY by the bounded-staleness TTL
        # (HttpPlanLibrary is the only library; nexus-x1de2 (54)).
        # time.monotonic(), not time.time() — immune to wall-clock jumps.
        self._last_populate_monotonic: float = 0.0
        # Single-flight guard (review finding on f3b02373): True while some
        # thread is inside the unlocked ``populate()`` call below. Only
        # ``_lock``-holding sections may read/write this.
        self._populating: bool = False
        # Bumped by clear(). A populate that started before a clear() must
        # not stamp its (now-stale) success/failure bookkeeping onto the
        # post-clear state, and must not clobber a *different* populate's
        # ``_populating=True`` that the post-clear epoch may have already
        # set. See :meth:`get` and :meth:`clear`.
        self._epoch: int = 0

    def get(self, *, populate_from: Any = None) -> Any:
        """Return the T1 ``plans__session`` cache, lazy-populated on first call.

        When *populate_from* (a PlanLibrary) is supplied, the cache is
        populated from its rows on first call and repopulated no less
        often than every :data:`_HTTP_PLAN_LIBRARY_STALENESS_SECONDS`
        seconds (nexus-ie7o8). Bounded staleness, not change detection —
        see that constant's docstring for why no cheaper server-side
        signal exists today.

        nexus-x1de2 (54): the file-mtime branch (nexus-qgjr) that
        preceded the TTL is gone. It keyed on a ``.path`` attribute only
        the SQLite ``PlanLibrary`` had, and that class died with the
        store (nexus-i711w); ``HttpPlanLibrary`` is the only library
        left, so the branch was unreachable in production and kept alive
        solely by a test double.

        Per-call cost when NOT stale: one ``time.monotonic()`` call — no
        network round-trip, no re-embedding. The expensive part (one HTTP list call plus one
        ONNX embed per active plan, see ``PlanSessionCache.populate``)
        only runs when actually stale.

        **Lock granularity (review finding on f3b02373).** Before this
        method's staleness check and the actual repopulate shared one
        ``with self._lock`` block. That was harmless when populate ran
        once per process; once nexus-ie7o8 made it recurring (every
        :data:`_HTTP_PLAN_LIBRARY_STALENESS_SECONDS`, 90s, for the
        service-mode production default), every concurrent MCP tool
        call touching plan matching would queue up behind whichever
        thread happened to be repopulating, for the full duration of
        one HTTP round-trip plus N ONNX embeds. The staleness decision
        now takes the lock only briefly; the expensive ``populate()``
        call runs UNLOCKED, guarded instead by a ``_populating``
        single-flight flag:

        - The thread that finds the cache stale AND ``_populating`` is
          False wins the single-flight slot (sets ``_populating = True``
          under the lock) and calls ``populate()`` unlocked.
        - Any other thread arriving while ``_populating`` is True does
          NOT block and does NOT launch its own populate — it falls
          straight through to ``return self._cache`` and gets the
          stale-but-usable cache immediately. This is the deliberate
          choice: this is a CACHE under a bounded-staleness contract by
          design (nexus-ie7o8); a concurrent caller blocking on, or
          duplicating, an in-flight repopulate would either add tail
          latency this fix exists to remove, or turn one recurring
          populate into a thundering herd of N (one per waiter) — both
          strictly worse than serving the cache that is, at most, one
          TTL window old.
        - On both success and failure the populating thread reacquires
          the lock briefly to clear ``_populating`` — unconditionally,
          in a ``finally``, so a populate that raises can never
          deadlock the single-flight slot for the rest of the process.
          Bookkeeping (``_populated`` /
          ``_last_populate_monotonic``) is only stamped on success, and
          only if :meth:`clear` has not run since this populate started
          (see the ``_epoch`` check) — a failure (or a stale populate
          whose epoch was clear()'d away) must not silence the next
          call's retry.

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
                        from nexus.mcp_infra import get_t1  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
                        from nexus.plans.session_cache import PlanSessionCache  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
                        t1, _ = get_t1()
                        chroma_client = getattr(t1, "_client", None)
                        if not hasattr(chroma_client, "get_or_create_collection"):
                            # nexus-373jo: a SERVICE-backed T1 (HttpScratchStore,
                            # the default since nexus-rn3wo.1) carries an
                            # httpx.Client here — handing it to the chroma-shaped
                            # PlanSessionCache AttributeError'd and silently
                            # flipped EVERY production plan match to FTS5-only.
                            # The cache is session-scoped and in-memory by
                            # contract; the in-process InMemoryVectorClient
                            # (RDR-155 P4b P0a) restores the calibrated cosine
                            # gate with REAL per-instance isolation (unlike the
                            # retired EphemeralClient, whose SharedSystemClient
                            # shared process state by settings hash). The
                            # session_id row filter stays as a safety net.
                            from nexus.db.inmemory_vector_store import InMemoryVectorClient  # noqa: PLC0415 — deferred; rare init path

                            chroma_client = InMemoryVectorClient()
                            _log.info(
                                "plan_session_cache_ephemeral_substrate",
                                reason="service-backed T1 has no chroma client",
                            )
                        self._cache = PlanSessionCache(
                            client=chroma_client, session_id=t1.session_id,
                        )
                    except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
                        # nexus-373jo: settling UNAVAILABLE means the cosine
                        # gate is off for the process lifetime — never silent.
                        _log.warning(
                            "plan_session_cache_disabled_for_process",
                            consequence="plan match degrades to FTS5-only",
                            exc_info=True,
                        )
                        self._cache = _UNAVAILABLE
        if self._cache is _UNAVAILABLE:
            return None
        if populate_from is not None:
            # nexus-ie7o8: which staleness signal applies depends on
            # whether *populate_from* exposes a file mtime at all — NOT
            # on whether that mtime happens to be 0.0 (an OSError'd stat
            # on a SQLite library legitimately returns 0.0 too, and that
            # case should still use the mtime branch, not silently fall
            # back to the TTL).
            now_monotonic = time.monotonic()
            should_populate = False
            cache_obj: Any = None
            epoch_at_start = 0
            with self._lock:
                # Single-flight: someone else is already inside the
                # unlocked populate() call below. Don't queue up behind
                # it and don't launch a duplicate — fall through to
                # `return self._cache` and hand back the stale-but-
                # usable cache. See the class/method docstrings for why
                # this (not blocking, not a per-waiter populate) is the
                # right shape for a bounded-staleness cache.
                if not self._populating:
                    stale = (
                        not self._populated
                        or (now_monotonic - self._last_populate_monotonic)
                        >= _HTTP_PLAN_LIBRARY_STALENESS_SECONDS
                    )
                    if stale:
                        self._populating = True
                        should_populate = True
                        # Snapshot the cache object and epoch under the
                        # lock: if clear() runs while we're populating
                        # unlocked, we still finish populating THIS
                        # object (harmless — nothing references it once
                        # clear() has moved self._cache on) without
                        # mistaking a later epoch's state for our own.
                        cache_obj = self._cache
                        epoch_at_start = self._epoch
            if should_populate:
                # Only mark populated/mtime/monotonic-clock on success.
                # Pre-refactor code used `try/finally` which permanently
                # suppressed populate failures (a transient network blip
                # during plan-embedding would set `_populated = True`
                # and `_mtime = current_mtime`, so the next call saw
                # stale=False and never retried). The fix keeps the
                # cache instance available (we don't reset
                # `self._cache`) but leaves `_populated` / `_mtime` /
                # `_last_populate_monotonic` unchanged on failure so the
                # next call retries the populate.
                #
                # `_populating` is cleared in `finally` — unconditionally,
                # on success AND failure — so a populate that raises can
                # never leave the single-flight slot stuck forever (the
                # cache would otherwise deadlock: every future caller
                # would see `_populating=True` and never repopulate again).
                populate_ok = False
                try:
                    cache_obj.populate(populate_from)
                    populate_ok = True
                except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
                    _log.warning(
                        "plan_cache_populate_failed",
                        library=str(populate_from),
                        exc_info=True,
                    )
                finally:
                    with self._lock:
                        if self._epoch == epoch_at_start:
                            # UNCONDITIONAL, on success AND failure. A populate
                            # that raises must still free the single-flight
                            # slot, or every future caller sees
                            # `_populating=True`, believes a populate is in
                            # flight, and the cache never repopulates again --
                            # a permanent self-deadlock that no retry can clear.
                            self._populating = False
                            if populate_ok:
                                self._populated = True
                                self._last_populate_monotonic = now_monotonic
                        # else: clear() ran while we were populating
                        # unlocked. Our epoch's bookkeeping (and our
                        # `_populating` slot) are both moot — the post-
                        # clear epoch already reset `_populating` to
                        # False itself and may since have started its
                        # own populate; touching either field here
                        # would either resurrect stale success/failure
                        # state or clobber that newer populate's flag.
        return self._cache

    def clear(self) -> None:
        """Drop the cache state. Used by ``reset_plan_cache_registry_for_tests``.

        Coherent with an in-flight populate: bumping ``_epoch`` means a
        populate that started before this call (running unlocked, see
        :meth:`get`) will recognise on completion that its epoch is
        stale and skip touching ``_populating`` / ``_populated`` —
        it neither resurrects the state this call just
        cleared nor clobbers a fresh populate the new epoch may have
        already started. Resetting ``_populating`` here (rather than
        leaving it for the stale populate to clear) means a caller
        arriving right after ``clear()`` never gets wrongly told "a
        populate is already in flight" by a populate that, from this
        epoch's perspective, no longer exists.
        """
        with self._lock:
            self._cache = None
            self._populated = False
            self._last_populate_monotonic = 0.0
            self._populating = False
            self._epoch += 1


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
