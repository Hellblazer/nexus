# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit coverage for RDR-191 Phase 1's ``*_serverside`` wrappers in
``nexus.catalog.chunk_quarantine`` — the route-availability decision logic
(``fn is None`` vs. a 404 ``VectorServiceError`` vs. a genuine server error
vs. success), isolated from the real-engine round trip
(``tests/test_rdr191_gc_serverside_prune.py`` covers the live path).
"""
from __future__ import annotations

import re

import pytest

from nexus.catalog.chunk_quarantine import (
    expire_quarantine_serverside,
    now_stamp,
    quarantine_orphans_serverside,
    restore_rereferenced_bounded_serverside,
    restore_rereferenced_serverside,
)
from nexus.db.http_vector_client import VectorServiceError


class _NoGcMethods:
    """A `db` with no HTTP GC capability at all (local/in-memory mode)."""


class _Sequence:
    """Returns one scripted response per call, in order; records call count
    (nexus-e8h5x: proves a bounded-loop caller actually looped, not just
    that its first call's result was interpreted correctly)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


class _Raises:
    def __init__(self, exc: Exception):
        self._exc = exc

    def __call__(self, *args, **kwargs):
        raise self._exc


class _Returns:
    def __init__(self, value):
        self._value = value

    def __call__(self, *args, **kwargs):
        return self._value


def test_now_stamp_matches_the_quarantined_at_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", now_stamp())


# ── quarantine_orphans_serverside ────────────────────────────────────────


def test_quarantine_serverside_noMethod_returnsNone():
    db = _NoGcMethods()
    assert quarantine_orphans_serverside(db, "code__x", "quarantine-code__x", now_stamp()) is None


def test_quarantine_serverside_404_now_RAISES_fallback_retired():
    """RETIRED at REQUIRED_ENGINE_VERSION (0,1,70): a 404 used to mean
    "pre-route engine, fall back client-side" and returned None. The route
    now ships in the pinned engine, so a 404 is a real failure and must
    propagate. Asserting the RAISE (not merely deleting the old test) is
    what stops the fallback quietly reappearing."""
    db = type("Db", (), {"gc_quarantine_orphans": _Raises(VectorServiceError("nope", code=404))})()
    with pytest.raises(VectorServiceError):
        quarantine_orphans_serverside(db, "code__x", "quarantine-code__x", now_stamp())


def test_quarantine_serverside_non404_reraises():
    db = type("Db", (), {"gc_quarantine_orphans": _Raises(VectorServiceError("boom", code=500))})()
    with pytest.raises(VectorServiceError):
        quarantine_orphans_serverside(db, "code__x", "quarantine-code__x", now_stamp())


def test_quarantine_serverside_success_returnsMovedAndSample():
    db = type("Db", (), {
        "gc_quarantine_orphans": _Returns({"moved": 3, "sample": [{"chash": "ab", "title": "t"}]}),
    })()
    result = quarantine_orphans_serverside(db, "code__x", "quarantine-code__x", now_stamp())
    assert result == (3, [{"chash": "ab", "title": "t"}])


def test_quarantine_serverside_success_missingSample_defaultsEmptyList():
    db = type("Db", (), {"gc_quarantine_orphans": _Returns({"moved": 0})})()
    result = quarantine_orphans_serverside(db, "code__x", "quarantine-code__x", now_stamp())
    assert result == (0, [])


# ── restore_rereferenced_serverside ──────────────────────────────────────


def test_restore_serverside_noMethod_returnsNone():
    db = _NoGcMethods()
    assert restore_rereferenced_serverside(db, "quarantine-code__x", "code__x") is None


def test_restore_serverside_404_now_RAISES_fallback_retired():
    """See test_quarantine_serverside_404_now_RAISES_fallback_retired."""
    db = type("Db", (), {"gc_restore_rereferenced": _Raises(VectorServiceError("nope", code=404))})()
    with pytest.raises(VectorServiceError):
        restore_rereferenced_serverside(db, "quarantine-code__x", "code__x")


def test_restore_serverside_non404_reraises():
    db = type("Db", (), {"gc_restore_rereferenced": _Raises(VectorServiceError("boom", code=500))})()
    with pytest.raises(VectorServiceError):
        restore_rereferenced_serverside(db, "quarantine-code__x", "code__x")


def test_restore_serverside_success_returnsCount():
    db = type("Db", (), {"gc_restore_rereferenced": _Returns(7)})()
    assert restore_rereferenced_serverside(db, "quarantine-code__x", "code__x") == 7


# ── restore_rereferenced_bounded_serverside (nexus-e8h5x) ────────────────


def test_restore_bounded_serverside_noMethod_returnsNone():
    db = _NoGcMethods()
    assert restore_rereferenced_bounded_serverside(db, "quarantine-code__x", "code__x") is None


def test_restore_bounded_serverside_loops_untilDrained():
    # 5 rows at a bound of 2: three batches, matching the engine-side
    # GcRestoreRereferencedBoundedTest fixture shape exactly. If the loop
    # were ever removed (a single call, no `while remaining > 0`), this
    # would fail on BOTH assertions: only 1 call would be made, and the
    # summed total would be 2, not 5.
    seq = _Sequence([
        {"restored": 2, "remaining": 3, "row_limit": 2},
        {"restored": 2, "remaining": 1, "row_limit": 2},
        {"restored": 1, "remaining": 0, "row_limit": 2},
    ])
    db = type("Db", (), {"gc_restore_rereferenced_bounded": seq})()
    total = restore_rereferenced_bounded_serverside(db, "quarantine-code__x", "code__x", row_limit=2)
    assert total == 5, "must sum every batch's restored count, not just the first"
    assert seq.calls == 3, "must loop until remaining == 0, not stop after one call"


def test_restore_bounded_serverside_noopFirstCall_stopsImmediately():
    seq = _Sequence([{"restored": 0, "remaining": 0, "row_limit": 10}])
    db = type("Db", (), {"gc_restore_rereferenced_bounded": seq})()
    total = restore_rereferenced_bounded_serverside(db, "quarantine-code__x", "code__x", row_limit=10)
    assert total == 0
    assert seq.calls == 1


def test_restore_bounded_serverside_olderEngineIgnoresRowLimit_stopsAfterOneCall():
    # An engine with the /gc/restore-rereferenced route but predating
    # gc_restore_rereferenced_bounded silently ignores the unrecognized
    # row_limit field and performs the UNBOUNDED restore -- its response has
    # no "remaining" key at all, not a zero one. The caller must detect this
    # from the response SHAPE and stop, since `restored` is already the
    # FULL count.
    seq = _Sequence([{"restored": 41032}])
    db = type("Db", (), {"gc_restore_rereferenced_bounded": seq})()
    total = restore_rereferenced_bounded_serverside(db, "quarantine-code__x", "code__x", row_limit=2000)
    assert total == 41032
    assert seq.calls == 1, "a response with no remaining key must not be looped on"


# ── expire_quarantine_serverside ─────────────────────────────────────────


def test_expire_serverside_noMethod_returnsNone():
    db = _NoGcMethods()
    assert expire_quarantine_serverside(
        db, "quarantine-code__x", "code__x", now_stamp(),
        floor_fraction=0.5, floor_min_chunks=100,
    ) is None


def test_expire_serverside_404_now_RAISES_fallback_retired():
    """See test_quarantine_serverside_404_now_RAISES_fallback_retired."""
    db = type("Db", (), {"gc_expire_quarantine": _Raises(VectorServiceError("nope", code=404))})()
    with pytest.raises(VectorServiceError):
        expire_quarantine_serverside(
            db, "quarantine-code__x", "code__x", now_stamp(),
            floor_fraction=0.5, floor_min_chunks=100,
        )


def test_expire_serverside_non404_reraises():
    db = type("Db", (), {"gc_expire_quarantine": _Raises(VectorServiceError("boom", code=500))})()
    with pytest.raises(VectorServiceError):
        expire_quarantine_serverside(
            db, "quarantine-code__x", "code__x", now_stamp(),
            floor_fraction=0.5, floor_min_chunks=100,
        )


def test_expire_serverside_success_returnsExpiredAndRefused():
    db = type("Db", (), {"gc_expire_quarantine": _Returns({"expired": 0, "refused": 12})})()
    result = expire_quarantine_serverside(
        db, "quarantine-code__x", "code__x", now_stamp(),
        floor_fraction=0.5, floor_min_chunks=5,
    )
    assert result == (0, 12)
