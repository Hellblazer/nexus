# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-ie7o8: bounded-staleness refresh for HTTP-backed PlanLibrary.

``PlanCacheRegistry.get``'s mtime-guarded refresh (nexus-qgjr) only ever
fires for the SQLite :class:`~nexus.db.t2.plan_library.PlanLibrary`,
which exposes a ``.path`` attribute the registry stats. In service mode
(the PRODUCTION DEFAULT) ``populate_from`` is ``db.plans``, which is
:class:`~nexus.db.t2.http_plan_library.HttpPlanLibrary` — it has no
``.path``, so the old mtime probe always returned 0.0 and the staleness
comparison could never fire. The T1 plan-session cache was populated
exactly once per MCP process and never refreshed for its lifetime; a
plan added or edited by any other process stayed invisible forever
(UNBOUNDED staleness).

The fix converts this into BOUNDED staleness for libraries with no file
mtime to read: repopulate no less often than every
``_HTTP_PLAN_LIBRARY_STALENESS_SECONDS``, tracked via a monotonic clock
(immune to wall-clock adjustments) rather than inventing a fake mtime.
This is deliberately NOT change detection — a write between refreshes
is still invisible until the TTL elapses — but it bounds the window
instead of leaving it open for the life of the process.

These tests use a bare double shaped like ``HttpPlanLibrary`` (a
``list_active_plans`` method, deliberately NO ``.path`` attribute) so
they exercise the actual production shape, not the SQLite one that hid
this bug in the first place.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.mcp_infra import get_t1_plan_cache


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
    """Shaped like ``HttpPlanLibrary``: exposes ``list_active_plans`` but
    carries NO ``.path`` attribute at all — the production service-mode
    shape (``db.plans`` in ``mcp/core.py``), not the SQLite shape.
    """

    def list_active_plans(self, *, project=""):
        return []


def test_http_shaped_library_has_no_path_attr():
    """Sanity guard: if HttpPlanLibrary ever grows a ``.path`` attribute,
    this fixture (and the bug it reproduces) silently stops being
    representative. Fail loudly instead."""
    from nexus.db.t2.http_plan_library import HttpPlanLibrary

    assert "path" not in vars(HttpPlanLibrary) or not hasattr(
        HttpPlanLibrary, "path",
    ), "HttpPlanLibrary now exposes .path — re-check this test's fixture shape"
    assert not hasattr(_FakeHttpPlanLibrary(), "path")


def test_immediate_repeat_call_within_ttl_does_not_repopulate():
    """Steady-state cost assertion: two calls in quick succession (well
    inside the TTL) must not pay the populate cost twice."""
    from nexus.mcp_infra import get_t1_plan_cache

    fake_cache = MagicMock()
    fake_cache.populate.return_value = 0
    lib = _FakeHttpPlanLibrary()
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache(populate_from=lib)
        get_t1_plan_cache(populate_from=lib)
        get_t1_plan_cache(populate_from=lib)

    assert fake_cache.populate.call_count == 1, (
        f"expected exactly one populate inside the TTL window, "
        f"got {fake_cache.populate.call_count}"
    )


def test_http_shaped_library_repopulates_after_ttl_elapses():
    """THE CORE FIX. An HttpPlanLibrary-shaped double (no ``.path``) must
    still repopulate once the bounded-staleness TTL elapses — this is
    the scenario that was DEAD in production: nexus-qgjr's repopulate
    tier never fired for any service-mode plan library because the old
    mtime probe returned 0.0 unconditionally for anything without
    ``.path``, so ``current_mtime > 0.0`` was always False and the
    staleness comparison could never trigger a second populate.

    MUST FAIL against the pre-fix code: the old comparison has no
    TTL/monotonic-clock fallback at all, so no amount of elapsed time
    ever triggers a second populate for a path-less library.
    """
    from nexus.mcp import plan_cache_registry as pcr
    from nexus.mcp_infra import get_t1_plan_cache

    fake_cache = MagicMock()
    fake_cache.populate.return_value = 0
    lib = _FakeHttpPlanLibrary()

    real_monotonic = pcr.time.monotonic
    t0 = real_monotonic()
    # Elapsed-time trapdoor: first call sees t0, second call sees
    # t0 + TTL + 1s, i.e. "TTL has just elapsed."
    ttl = getattr(pcr, "_HTTP_PLAN_LIBRARY_STALENESS_SECONDS", None)
    assert ttl is not None and ttl > 0, (
        "expected a named, positive _HTTP_PLAN_LIBRARY_STALENESS_SECONDS "
        "constant on nexus.mcp.plan_cache_registry"
    )
    fake_clock = {"t": t0}

    def _fake_monotonic():
        return fake_clock["t"]

    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache), \
         patch.object(pcr.time, "monotonic", _fake_monotonic):
        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 1

        # Well within the TTL: no repopulate.
        fake_clock["t"] = t0 + (ttl / 2.0)
        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 1, (
            "repopulated before the TTL elapsed"
        )

        # TTL elapsed: must repopulate.
        fake_clock["t"] = t0 + ttl + 1.0
        get_t1_plan_cache(populate_from=lib)

    assert fake_cache.populate.call_count == 2, (
        "TTL elapsed for a path-less (HttpPlanLibrary-shaped) library — "
        "the bounded-staleness tier must repopulate (nexus-ie7o8). "
        f"populate call_count={fake_cache.populate.call_count}"
    )


def test_reset_clears_bounded_staleness_clock():
    """reset_plan_cache_for_tests must clear the monotonic-clock state too,
    not just the SQLite-mtime state, so a fresh registry doesn't inherit a
    stale 'last populated at T' timestamp from a previous test/process."""
    from nexus.mcp_infra import get_t1_plan_cache, reset_plan_cache_for_tests

    fake_cache = MagicMock()
    fake_cache.populate.return_value = 0
    lib = _FakeHttpPlanLibrary()
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 1

        reset_plan_cache_for_tests()

        get_t1_plan_cache(populate_from=lib)

    assert fake_cache.populate.call_count == 2, (
        "post-reset populate should fire even though the TTL has not elapsed"
    )


# ── ported from test_plan_cache_mtime_invalidation.py (nexus-x1de2 (54)) ──
# The SQLite file-mtime branch those tests exercised is deleted; the two
# contracts below never depended on it and keep their coverage here.


def test_no_populate_without_populate_from_arg():
    """Callers that don't pass populate_from never trigger populate."""
    fake_cache = MagicMock()
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache()
        get_t1_plan_cache()

    assert fake_cache.populate.call_count == 0


def test_populate_failure_does_not_suppress_retry():
    """A transient populate failure must NOT permanently mark the cache
    populated (code-review finding 2 on PR #881): bookkeeping is stamped
    only on success, so the next call retries inside the same TTL window."""
    lib = _FakeHttpPlanLibrary()
    fake_cache = MagicMock()
    fake_cache.populate.side_effect = [RuntimeError("transient"), 0]
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache(populate_from=lib)
        get_t1_plan_cache(populate_from=lib)

    assert fake_cache.populate.call_count == 2, (
        f"expected retry on the second call after first failure, "
        f"got {fake_cache.populate.call_count}"
    )
