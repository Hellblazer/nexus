# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-qgjr: T1 plan-cache mtime-guarded refresh.

When the underlying SQLite ``plans`` table mutates (a new builtin
seeded, an existing description tweaked, a row deleted) the in-process
``plans__session`` cache must repopulate on next access without
requiring an MCP-server restart. Pattern mirrors the catalog
``_last_consistency_mtime`` trick at ``catalog.py:405``.

The fix:

  - Track the mtime of the SQLite file feeding the cache.
  - On every ``get_t1_plan_cache(populate_from=lib)`` call, compare
    the file's current mtime to the cached value; repopulate when
    advanced.

Two scenarios prove the contract:

  1. mtime advance triggers re-populate.
  2. mtime stable triggers no extra populate (steady-state cost).

These tests do not exercise the live T1 ChromaDB. They patch
``PlanSessionCache`` so the populate count is observable.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    from nexus.mcp_infra import reset_plan_cache_for_tests
    reset_plan_cache_for_tests()
    yield
    reset_plan_cache_for_tests()


def _stub_t1():
    """Return a (t1, lock) pair where t1 has the attributes
    ``PlanSessionCache.__init__`` reads (``_client``, ``session_id``).
    """
    t1 = MagicMock()
    t1._client = MagicMock()
    t1.session_id = "test-session"
    return (t1, MagicMock())


def _bump_mtime(path: Path, delta: float = 1.5) -> None:
    """Advance the mtime by *delta* seconds. Filesystems with second
    granularity need a >1s nudge to register the change."""
    st = path.stat()
    new_t = st.st_mtime + delta
    os.utime(str(path), (new_t, new_t))


def test_initial_populate_runs_once(tmp_path: Path) -> None:
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.mcp_infra import get_t1_plan_cache

    db_path = tmp_path / "plans.sqlite"
    lib = PlanLibrary(path=db_path)

    fake_cache = MagicMock()
    fake_cache.populate.return_value = 0
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        cache = get_t1_plan_cache(populate_from=lib)
        # Subsequent call with no mutation must NOT re-populate.
        get_t1_plan_cache(populate_from=lib)
        get_t1_plan_cache(populate_from=lib)

    assert cache is fake_cache
    assert fake_cache.populate.call_count == 1, (
        f"expected exactly one populate, got {fake_cache.populate.call_count}"
    )


def test_mtime_advance_triggers_repopulate(tmp_path: Path) -> None:
    """nexus-qgjr core contract: file mtime > cached mtime → populate again.

    Tests the path the symptom hit: re-seeding a builtin updates the
    SQLite file, the next ``get_t1_plan_cache`` call must rebuild
    without an MCP restart.
    """
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.mcp_infra import get_t1_plan_cache

    db_path = tmp_path / "plans.sqlite"
    lib = PlanLibrary(path=db_path)

    fake_cache = MagicMock()
    fake_cache.populate.return_value = 0
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 1

        _bump_mtime(db_path)

        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 2, (
            "mtime advanced — populate should have fired again"
        )

        # No further mtime change → no further populate.
        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 2


def test_no_populate_without_populate_from_arg(tmp_path: Path) -> None:
    """Sanity: callers that don't pass populate_from never trigger populate."""
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: F401
    from nexus.mcp_infra import get_t1_plan_cache

    fake_cache = MagicMock()
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache()
        get_t1_plan_cache()

    assert fake_cache.populate.call_count == 0


def test_reset_for_tests_clears_mtime(tmp_path: Path) -> None:
    """reset_plan_cache_for_tests must clear the mtime so the next
    populate_from call rebuilds against the fresh DB."""
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.mcp_infra import get_t1_plan_cache, reset_plan_cache_for_tests

    db_path = tmp_path / "plans.sqlite"
    lib = PlanLibrary(path=db_path)

    fake_cache = MagicMock()
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 1

        reset_plan_cache_for_tests()

        get_t1_plan_cache(populate_from=lib)
        assert fake_cache.populate.call_count == 2, (
            "post-reset populate should fire even without mtime change"
        )


def test_missing_path_attr_falls_back_safely(tmp_path: Path) -> None:
    """A library object without a ``path`` attribute (e.g. an in-memory
    fixture) falls back to single-populate semantics — no raise, no
    extra populate calls.
    """
    from nexus.mcp_infra import get_t1_plan_cache

    class _FakeLib:
        def list_active_plans(self, *, project=""):
            return []

    fake_cache = MagicMock()
    with patch("nexus.mcp_infra.get_t1", return_value=_stub_t1()), \
         patch("nexus.plans.session_cache.PlanSessionCache",
               return_value=fake_cache):
        get_t1_plan_cache(populate_from=_FakeLib())
        get_t1_plan_cache(populate_from=_FakeLib())

    # Without a path → no mtime tracking → behaves like the legacy
    # populate-once contract. Acceptable fallback.
    assert fake_cache.populate.call_count == 1
