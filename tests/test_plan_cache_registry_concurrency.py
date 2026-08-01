# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Concurrency contract for ``PlanCacheRegistry.get`` (review finding on f3b02373).

Before f3b02373, ``PlanSessionCache.populate()`` — one HTTP round-trip
plus one ONNX embed per active plan — ran at most once per MCP process.
nexus-ie7o8 made that recurring: at least once every
``_HTTP_PLAN_LIBRARY_STALENESS_SECONDS`` (90s) for the service-mode
production-default ``HttpPlanLibrary``. Both stacked reviewers flagged
that ``PlanCacheRegistry.get`` held its lock across BOTH the cheap
staleness check AND that now-recurring expensive populate — meaning
every concurrent MCP tool call touching plan matching would queue up
behind whichever thread was repopulating, for the full duration, once
every 90s on a shared server.

The fix (``src/nexus/mcp/plan_cache_registry.py``) makes the populate
single-flight and unlocked: one thread wins the ``_populating`` slot and
calls ``populate()`` outside the lock; concurrent arrivals get the
current (possibly stale) cache immediately rather than blocking or each
running their own populate.

Every test here uses ``threading.Event`` / ``threading.Barrier`` as the
synchronization primitive — no sleep is used to coordinate threads (a
bounded ``Event.wait(timeout=...)`` is only a safety net against a
genuine test hang, never a substitute for synchronization).
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    from nexus.mcp_infra import reset_plan_cache_for_tests
    reset_plan_cache_for_tests()
    yield
    reset_plan_cache_for_tests()


def _stub_t1():
    """(t1, lock) pair with the attributes PlanSessionCache.__init__ reads."""
    t1 = MagicMock()
    t1._client = MagicMock()
    t1.session_id = "test-session"
    return (t1, MagicMock())


class _FakeHttpPlanLibrary:
    """Shaped like ``HttpPlanLibrary``: no ``.path`` attribute at all —
    the production service-mode shape that drives the bounded-staleness
    (recurring) populate path this fix targets."""

    def list_active_plans(self, *, project=""):
        return []


def test_concurrent_get_calls_singleflight_populate_without_blocking():
    """N callers arriving while a populate is in flight must not queue up
    behind it (lock-too-broad regression) and must not each trigger their
    own populate (thundering herd). They get the stale-but-usable cache
    back immediately instead.

    Proof of "did not block": the concurrent callers are joined with a
    bounded timeout WHILE the in-flight populate is still deliberately
    held open (blocked on ``release_populate``, not yet set). If a
    caller's thread is still alive at that point, it was waiting on the
    lock/populate rather than falling through to ``return self._cache``.
    """
    from nexus.mcp.plan_cache_registry import PlanCacheRegistry

    registry = PlanCacheRegistry()
    lib = _FakeHttpPlanLibrary()

    populate_started = threading.Event()
    release_populate = threading.Event()
    populate_calls: list[int] = []
    populate_calls_lock = threading.Lock()

    class _SlowCache:
        def populate(self, library, *, project=""):
            with populate_calls_lock:
                populate_calls.append(1)
            populate_started.set()
            assert release_populate.wait(timeout=5), (
                "test setup bug: release_populate was never signalled"
            )
            return 0

    fake_cache = _SlowCache()

    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache", return_value=fake_cache):
        driver_result: list = []

        def _driver():
            driver_result.append(registry.get(populate_from=lib))

        driver = threading.Thread(target=_driver)
        driver.start()
        assert populate_started.wait(timeout=5), "populate never started"

        # Populate is now blocked in-flight (holding no lock). Spawn N
        # concurrent arrivals.
        n = 8
        concurrent_results: list = [None] * n
        barrier = threading.Barrier(n + 1)

        def _caller(i: int) -> None:
            barrier.wait(timeout=5)
            concurrent_results[i] = registry.get(populate_from=lib)

        callers = [threading.Thread(target=_caller, args=(i,)) for i in range(n)]
        for t in callers:
            t.start()
        barrier.wait(timeout=5)  # release all callers at once

        for t in callers:
            t.join(timeout=5)
            assert not t.is_alive(), (
                "a concurrent get() call blocked on the in-flight populate "
                "instead of returning the stale-but-usable cache — lock "
                "granularity regression"
            )

        # The in-flight populate is STILL held open at this point (we have
        # not released it) — every caller above genuinely returned while
        # populate was still running, not merely by lucky timing.
        assert not release_populate.is_set()

        release_populate.set()
        driver.join(timeout=5)

    assert populate_calls == [1], (
        f"expected exactly ONE populate (single-flight), got {len(populate_calls)} "
        "— concurrent arrivals must not each launch their own populate "
        "(thundering herd)"
    )
    assert driver_result == [fake_cache]
    for i, result in enumerate(concurrent_results):
        assert result is fake_cache, (
            f"caller {i} did not get the cache back immediately (got {result!r})"
        )
    assert registry._populating is False, (
        "single-flight slot must be released once the populate completes"
    )
    assert registry._populated is True
    assert registry._epoch == 0


def test_populate_failure_clears_populating_flag_so_next_call_retries():
    """A populate that raises must clear ``_populating`` (both success and
    failure paths) or the cache deadlocks itself forever — every future
    caller would see ``_populating=True`` and never repopulate again.
    """
    from nexus.mcp.plan_cache_registry import PlanCacheRegistry

    registry = PlanCacheRegistry()
    lib = _FakeHttpPlanLibrary()
    calls: list[int] = []

    class _FailOnceCache:
        def populate(self, library, *, project=""):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient populate failure")
            return 0

    fake_cache = _FailOnceCache()

    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache", return_value=fake_cache):
        result1 = registry.get(populate_from=lib)

        assert result1 is fake_cache, "cache instance must remain usable after a failed populate"
        assert len(calls) == 1
        assert registry._populating is False, (
            "populate failure left _populating=True — the cache would deadlock: "
            "every subsequent call would believe a populate is already in "
            "flight and never retry"
        )
        assert registry._populated is False, (
            "a failed populate must not be marked as a successful populate "
            "(regression: the pre-refactor try/finally bug)"
        )

        result2 = registry.get(populate_from=lib)

        assert len(calls) == 2, (
            "the next call after a populate failure must retry the populate, "
            "not skip it because _populating (or _populated) was stuck"
        )
        assert result2 is fake_cache
        assert registry._populating is False
        assert registry._populated is True


def test_clear_during_inflight_populate_does_not_leak_populating_flag():
    """``clear()`` must remain coherent with an in-flight populate: a
    populate started in a pre-clear() epoch must not, upon completing
    later, clobber a *different* populate's ``_populating=True`` that a
    post-clear epoch has since set. Without the epoch guard, the
    orphaned pre-clear populate's completion would unconditionally clear
    ``_populating``, letting a third caller wrongly conclude no populate
    is running and launch a duplicate (reintroducing the thundering herd
    the single-flight design exists to prevent).
    """
    from nexus.mcp.plan_cache_registry import PlanCacheRegistry

    registry = PlanCacheRegistry()
    lib = _FakeHttpPlanLibrary()

    call_started = [threading.Event(), threading.Event()]
    call_release = [threading.Event(), threading.Event()]
    call_index = {"n": 0}
    call_index_lock = threading.Lock()

    class _RoutedCache:
        def populate(self, library, *, project=""):
            with call_index_lock:
                i = call_index["n"]
                call_index["n"] += 1
            call_started[i].set()
            assert call_release[i].wait(timeout=5), (
                "test setup bug: call_release was never signalled"
            )
            return 0

    fake_cache = _RoutedCache()

    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache", return_value=fake_cache):
        # Epoch 0: kick off a populate that blocks on call_release[0].
        t_epoch0 = threading.Thread(target=lambda: registry.get(populate_from=lib))
        t_epoch0.start()
        assert call_started[0].wait(timeout=5), "epoch-0 populate never started"

        # clear() while epoch-0's populate is still in flight (unlocked).
        registry.clear()
        assert registry._epoch == 1
        assert registry._populating is False
        assert registry._cache is None

        # Epoch 1: a fresh caller (post-clear) finds the cache stale and
        # claims the single-flight slot itself, blocking on call_release[1].
        t_epoch1 = threading.Thread(target=lambda: registry.get(populate_from=lib))
        t_epoch1.start()
        assert call_started[1].wait(timeout=5), "epoch-1 populate never started"
        assert registry._populating is True, (
            "epoch-1's populate should have claimed the single-flight slot"
        )

        # Release the ORPHANED epoch-0 populate. Its completion must NOT
        # clear _populating (that would falsely announce no populate is
        # running) nor stamp epoch-1's bookkeeping with epoch-0's values.
        call_release[0].set()
        t_epoch0.join(timeout=5)
        assert not t_epoch0.is_alive()

        assert registry._populating is True, (
            "an orphaned pre-clear() populate clobbered a live post-clear "
            "populate's _populating flag — clear() is not coherent with an "
            "in-flight populate"
        )
        assert registry._epoch == 1, "epoch must not be touched by the orphaned populate"

        # Now let epoch-1's populate finish normally.
        call_release[1].set()
        t_epoch1.join(timeout=5)
        assert not t_epoch1.is_alive()

    assert registry._populating is False
    assert registry._populated is True
    assert registry._epoch == 1
    assert call_index["n"] == 2
