# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sentinel pattern tests for get_t1_plan_cache (code-review S1).

When T1 initialisation fails, the singleton stores a sentinel object so
subsequent calls short-circuit to ``None`` without re-entering the lock
or re-attempting ``get_t1()``. This prevents lock contention when T1 is
sustainedly unreachable (e.g. the HTTP session server is down).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the singleton between tests."""
    from nexus.mcp_infra import reset_plan_cache_for_tests
    reset_plan_cache_for_tests()
    yield
    reset_plan_cache_for_tests()


def test_init_failure_returns_none():
    """get_t1() raising → get_t1_plan_cache returns None."""
    from nexus.mcp_infra import get_t1_plan_cache

    with patch("nexus.mcp_infra.get_t1", side_effect=RuntimeError("boom")):
        assert get_t1_plan_cache() is None


def test_init_failure_short_circuits_on_repeat_calls():
    """After a failed init, subsequent calls must not retry get_t1().

    Before the sentinel fix, every call re-acquired the lock and
    re-called get_t1() because instance == None on both the "never init"
    and "init failed" paths. Verify the sentinel breaks that retry loop.
    """
    from nexus.mcp_infra import get_t1_plan_cache

    call_count = [0]

    def tracking_get_t1():
        call_count[0] += 1
        raise RuntimeError("T1 unavailable")

    with patch("nexus.mcp_infra.get_t1", side_effect=tracking_get_t1):
        assert get_t1_plan_cache() is None
        assert get_t1_plan_cache() is None
        assert get_t1_plan_cache() is None

    assert call_count[0] == 1, (
        f"get_t1() called {call_count[0]} times — must be called exactly once; "
        "sentinel should short-circuit subsequent calls"
    )


def test_reset_restores_retry_eligibility():
    """reset_plan_cache_for_tests() must clear the sentinel so the next
    call is eligible for init retry — otherwise tests can't recover."""
    from nexus.mcp_infra import get_t1_plan_cache, reset_plan_cache_for_tests

    call_count = [0]

    def tracking_get_t1():
        call_count[0] += 1
        raise RuntimeError("T1 unavailable")

    with patch("nexus.mcp_infra.get_t1", side_effect=tracking_get_t1):
        assert get_t1_plan_cache() is None
        reset_plan_cache_for_tests()
        assert get_t1_plan_cache() is None

    assert call_count[0] == 2, "reset must allow init retry"
