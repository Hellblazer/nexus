# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-d76vc: ``mcp_infra.reset_t1_for_release`` — the production-safe,
T1-only singleton reset the MCP handoff watcher uses on a consumed marker.

Deliberately narrower than ``reset_singletons`` (test-only; also tears
down T3/catalog/plan-cache/search-trace state). These tests pin that
narrower contract: only ``_t1_instance``/``_t1_isolated`` are touched.
"""
from __future__ import annotations

import nexus.mcp_infra as mcp_infra


def test_reset_clears_injected_t1_singleton():
    sentinel = object()
    mcp_infra.inject_t1(sentinel, isolated=True)
    assert mcp_infra._t1_instance is sentinel
    assert mcp_infra._t1_isolated is True

    mcp_infra.reset_t1_for_release()

    assert mcp_infra._t1_instance is None
    assert mcp_infra._t1_isolated is False


def test_reset_is_a_noop_when_nothing_was_constructed():
    mcp_infra.reset_t1_for_release()  # must not raise
    mcp_infra.reset_t1_for_release()  # idempotent
    assert mcp_infra._t1_instance is None


def test_reset_does_not_touch_other_singletons():
    """The narrower-than-reset_singletons contract: T3/catalog/collections
    cache state must survive a T1-only release reset untouched."""
    t3_sentinel = object()
    mcp_infra._t3_instance = t3_sentinel
    try:
        mcp_infra.inject_t1(object())
        mcp_infra.reset_t1_for_release()
        assert mcp_infra._t3_instance is t3_sentinel
    finally:
        mcp_infra._t3_instance = None


def test_get_t1_reconstructs_after_reset(monkeypatch):
    """After a reset, the NEXT get_t1() call reconstructs rather than
    returning the stale cached instance."""
    mcp_infra.inject_t1(object())
    first, _ = mcp_infra.get_t1()

    mcp_infra.reset_t1_for_release()

    fresh_sentinel = object()

    def _fake_get_t1_database():
        return fresh_sentinel

    monkeypatch.setattr("nexus.db.t1.get_t1_database", _fake_get_t1_database)
    second, _ = mcp_infra.get_t1()

    assert first is not second
    assert second is fresh_sentinel
