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
from structlog.testing import capture_logs

from nexus.catalog.chunk_quarantine import (
    _gc_loop_max_iterations,
    expire_quarantine_serverside,
    now_stamp,
    quarantine_orphans_bounded_serverside,
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


class _AlwaysReturns:
    """Returns the SAME scripted response on every call, forever; records
    call count. Used for the iteration-cap tests (nexus-e8h5x review round
    2): a server-side bug or persistent write pressure that never actually
    drains ``remaining`` must not spin a bounded loop forever."""

    def __init__(self, response):
        self._response = response
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return dict(self._response)


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


def test_restore_bounded_serverside_neverDrains_stopsAtIterationCap_logsWarning_notRaise():
    # nexus-e8h5x review round 2 (code-review SIGNIFICANT): a server-side
    # bug (or persistent concurrent-write pressure) that keeps `remaining`
    # positive forever must not spin this loop forever. Every call reports
    # 1 restored and `remaining` never drops -- the loop must give up at
    # _gc_loop_max_iterations, log a WARNING naming the collection and the
    # still-outstanding remaining, and return the partial total, never raise.
    row_limit = 2
    max_iterations = _gc_loop_max_iterations(row_limit)
    fake = _AlwaysReturns({"restored": 1, "remaining": 999, "row_limit": row_limit})
    db = type("Db", (), {"gc_restore_rereferenced_bounded": fake})()
    with capture_logs() as cap:
        total = restore_rereferenced_bounded_serverside(db, "quarantine-code__x", "code__x", row_limit=row_limit)
    assert fake.calls == max_iterations, "must stop at the iteration cap, not loop forever"
    assert total == max_iterations, "must return the partial total accumulated so far"
    warn_logs = [r for r in cap if r.get("event") == "gc_restore_bounded_loop_iteration_cap_reached"]
    assert warn_logs, f"must log a structured WARNING, never a silent give-up; captured: {cap}"
    assert warn_logs[0].get("log_level") == "warning"
    assert warn_logs[0]["collection"] == "code__x"
    assert warn_logs[0]["quarantine_collection"] == "quarantine-code__x"
    assert warn_logs[0]["remaining"] == 999


# ── quarantine_orphans_bounded_serverside (nexus-e8h5x review round 2) ───
#
# The engine route (catalog-037/nexus-a6mon, gc_quarantine_orphans_bounded,
# shipped v0.1.124/125) had NO client caller anywhere in this tree until
# this function -- found during nexus-e8h5x's review fold, since the
# original a6mon incident (a 41,032-row code__1-1 quarantine call cut
# mid-transaction at the ~30s edge deadline) was still fully reproducible
# end-to-end. Same test shape as restore_rereferenced_bounded_serverside's
# above, mirrored for this direction's response shape
# ({"moved", "sample", "remaining", "row_limit"}).


def test_quarantine_bounded_serverside_noMethod_returnsNone():
    db = _NoGcMethods()
    assert quarantine_orphans_bounded_serverside(db, "code__x", "quarantine-code__x", now_stamp()) is None


def test_quarantine_bounded_serverside_loops_untilDrained():
    # 5 rows at a bound of 2: three batches, same fixture shape as
    # GcQuarantineOrphansBoundedTest's engine-side test. If the loop were
    # ever removed (a single call, no `while remaining > 0`), this would
    # fail on BOTH assertions: only 1 call would be made, and the summed
    # total would be 2, not 5.
    seq = _Sequence([
        {"moved": 2, "sample": [{"chash": "aa", "title": "a"}], "remaining": 3, "row_limit": 2},
        {"moved": 2, "sample": [{"chash": "bb", "title": "b"}], "remaining": 1, "row_limit": 2},
        {"moved": 1, "sample": [{"chash": "cc", "title": "c"}], "remaining": 0, "row_limit": 2},
    ])
    db = type("Db", (), {"gc_quarantine_orphans_bounded": seq})()
    total_moved, sample = quarantine_orphans_bounded_serverside(
        db, "code__x", "quarantine-code__x", now_stamp(), sample_limit=20, row_limit=2,
    )
    assert total_moved == 5, "must sum every batch's moved count, not just the first"
    assert seq.calls == 3, "must loop until remaining == 0, not stop after one call"
    assert sample == [
        {"chash": "aa", "title": "a"}, {"chash": "bb", "title": "b"}, {"chash": "cc", "title": "c"},
    ], "must accumulate the sample across batches, capped at sample_limit"


def test_quarantine_bounded_serverside_sample_cappedAtSampleLimit():
    seq = _Sequence([
        {"moved": 2, "sample": [{"chash": "aa"}, {"chash": "bb"}], "remaining": 1, "row_limit": 2},
        {"moved": 1, "sample": [{"chash": "cc"}], "remaining": 0, "row_limit": 2},
    ])
    db = type("Db", (), {"gc_quarantine_orphans_bounded": seq})()
    _, sample = quarantine_orphans_bounded_serverside(
        db, "code__x", "quarantine-code__x", now_stamp(), sample_limit=2, row_limit=2,
    )
    assert sample == [{"chash": "aa"}, {"chash": "bb"}], "must stop accumulating once sample_limit is reached"


def test_quarantine_bounded_serverside_noopFirstCall_stopsImmediately():
    seq = _Sequence([{"moved": 0, "sample": [], "remaining": 0, "row_limit": 10}])
    db = type("Db", (), {"gc_quarantine_orphans_bounded": seq})()
    total_moved, sample = quarantine_orphans_bounded_serverside(
        db, "code__x", "quarantine-code__x", now_stamp(), sample_limit=10, row_limit=10,
    )
    assert total_moved == 0
    assert sample == []
    assert seq.calls == 1


def test_quarantine_bounded_serverside_olderEngineIgnoresRowLimit_stopsAfterOneCall():
    # An engine with the /gc/quarantine-orphans route but predating
    # gc_quarantine_orphans_bounded silently ignores the unrecognized
    # row_limit field and performs the UNBOUNDED quarantine -- its response
    # has no "remaining" key at all. The caller must detect this from the
    # response SHAPE and stop, since `moved` is already the FULL count.
    seq = _Sequence([{"moved": 41032, "sample": [{"chash": "dd"}]}])
    db = type("Db", (), {"gc_quarantine_orphans_bounded": seq})()
    total_moved, sample = quarantine_orphans_bounded_serverside(
        db, "code__x", "quarantine-code__x", now_stamp(), sample_limit=20, row_limit=2000,
    )
    assert total_moved == 41032
    assert sample == [{"chash": "dd"}]
    assert seq.calls == 1, "a response with no remaining key must not be looped on"


def test_quarantine_bounded_serverside_neverDrains_stopsAtIterationCap_logsWarning_notRaise():
    row_limit = 2
    max_iterations = _gc_loop_max_iterations(row_limit)
    fake = _AlwaysReturns({"moved": 1, "sample": [], "remaining": 999, "row_limit": row_limit})
    db = type("Db", (), {"gc_quarantine_orphans_bounded": fake})()
    with capture_logs() as cap:
        total_moved, _sample = quarantine_orphans_bounded_serverside(
            db, "code__x", "quarantine-code__x", now_stamp(), sample_limit=20, row_limit=row_limit,
        )
    assert fake.calls == max_iterations, "must stop at the iteration cap, not loop forever"
    assert total_moved == max_iterations, "must return the partial total accumulated so far"
    warn_logs = [r for r in cap if r.get("event") == "gc_quarantine_bounded_loop_iteration_cap_reached"]
    assert warn_logs, f"must log a structured WARNING, never a silent give-up; captured: {cap}"
    assert warn_logs[0].get("log_level") == "warning"
    assert warn_logs[0]["collection"] == "code__x"
    assert warn_logs[0]["quarantine_collection"] == "quarantine-code__x"
    assert warn_logs[0]["remaining"] == 999


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
