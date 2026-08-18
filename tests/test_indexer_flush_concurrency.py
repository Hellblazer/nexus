# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dimrz: FLUSH_CONCURRENCY must be derived from the quota ceiling.

``nexus.indexer`` used to hard-code ``flush_concurrency=3`` at the
ChunkBatcher call site with a comment claiming it stays inside the
10-concurrent-writes-per-collection service quota
(``QUOTAS.MAX_CONCURRENT_WRITES``, ``nexus.db.limits``). The literal had no
mechanical tie to that quota, so a future tightening of the quota could
silently invalidate the choice. ``FLUSH_CONCURRENCY`` replaces the literal
with ``min(3, QUOTAS.MAX_CONCURRENT_WRITES)`` so this test fails loud instead.
"""
from __future__ import annotations

from nexus.db.limits import QUOTAS
from nexus.indexer import FLUSH_CONCURRENCY


def test_flush_concurrency_stays_inside_quota():
    """FLUSH_CONCURRENCY must never exceed the per-collection write quota."""
    assert FLUSH_CONCURRENCY <= QUOTAS.MAX_CONCURRENT_WRITES


def test_flush_concurrency_effective_value_unchanged():
    """Today's empirical choice (3, from the 3midv sweep) must not silently drift."""
    assert FLUSH_CONCURRENCY == 3


def test_flush_concurrency_is_derived_not_a_bare_literal():
    """The bound is min(empirical, quota) so quota movement is caught, not silent."""
    assert FLUSH_CONCURRENCY == min(3, QUOTAS.MAX_CONCURRENT_WRITES)
