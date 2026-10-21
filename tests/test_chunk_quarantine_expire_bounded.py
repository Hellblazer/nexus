# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bounded, keyed client expiry loop (RDR-222 Amendment A1, nexus-1579m).

``expire_quarantine_bounded_serverside`` pages ``POST /v1/vectors/gc/expire-quarantine`` with ``row_limit``
and a per-page request key. These tests pin its outcome table (Technical Design 7.5) against a scripted
client object, one test per row of the table in each mode; ``tests/db/test_http_vector_client_keyed_sweeps.py``
drives the same loop over the real transport.

* Non-strict (the indexer's prune) never raises a page outcome, the first page's included, with two
  carve-outs below.
* Strict (``nx t3 gc``) re-raises the first page's error unchanged and relabels a later page's as
  :class:`BoundedDrainIncomplete`.
* A response-shape defect (a parsed body with wrong fields) and the production-write guard's refusal
  propagate in both modes. A 200 whose body is not JSON is NOT one: the client polls the ledger for it
  (``test_a_200_whose_body_does_not_parse_*`` in the keyed-sweeps tests).
"""
from __future__ import annotations

import http.client

import pytest
from structlog.testing import capture_logs

from nexus.catalog.chunk_quarantine import (
    GC_EXPIRE_ROW_LIMIT_DEFAULT,
    BoundedDrainIncomplete,
    _gc_loop_max_iterations,
    expire_quarantine_bounded_serverside,
    expire_quarantine_serverside,
)
from nexus.db.http_vector_client import CommittedWithUnknownCounts, VectorServiceError
from nexus.db.service_endpoint import ProductionWriteGuardError

SIBLING, ORIGIN, CUTOFF = "quarantine-code__x", "code__x", "2026-01-01T00:00:00Z"
BOTH_MODES = pytest.mark.parametrize("strict", [False, True], ids=["non-strict", "strict"])


def page(expired: int, *, more: bool, refused: int | None = None, floor_refused: bool = False) -> dict:
    return {"expired": expired, "refused": refused, "more": more, "row_limit": 100, "floor_refused": floor_refused}


class Pages:
    """A client object whose ``gc_expire_quarantine_bounded`` answers one scripted entry per call (an
    exception instance is raised), the last entry repeating, and records every call."""

    def __init__(self, *entries):
        self.entries, self.calls = list(entries), []

    def gc_expire_quarantine_bounded(self, quarantine, origin, cutoff, fraction, minimum, force, row_limit, *,
                                     count_refused=True):
        self.calls.append({
            "quarantine": quarantine, "origin": origin, "cutoff": cutoff, "fraction": fraction,
            "minimum": minimum, "force": force, "row_limit": row_limit, "count_refused": count_refused,
        })
        entry = self.entries.pop(0) if len(self.entries) > 1 else self.entries[0]
        if isinstance(entry, BaseException):
            raise entry
        return dict(entry)


def drain(db, *, strict=False, row_limit=100):
    return expire_quarantine_bounded_serverside(db, SIBLING, ORIGIN, CUTOFF, row_limit, strict=strict)


def events(cap, name):
    return [r for r in cap if r.get("event") == name]


# ── capability ───────────────────────────────────────────────────────────────


def test_a_client_without_the_bounded_method_is_none_and_expire_serverside_falls_back_to_the_unbounded_call():
    assert drain(object()) is None

    seen = []

    class Legacy:
        def gc_expire_quarantine(self, *args):
            seen.append(args)
            return {"expired": 5, "refused": 2}

    assert expire_quarantine_serverside(Legacy(), SIBLING, ORIGIN, CUTOFF) == (5, 2, True)
    assert seen == [(SIBLING, ORIGIN, CUTOFF, 1.0, 100, False)]


def test_expire_serverside_tries_the_bounded_loop_first_and_passes_strict_through():
    db = Pages(page(3, more=False, refused=1))
    assert expire_quarantine_serverside(db, SIBLING, ORIGIN, CUTOFF, strict=True) == (3, 1, True)
    assert db.calls[0]["row_limit"] == GC_EXPIRE_ROW_LIMIT_DEFAULT == 2000
    assert db.calls[0]["count_refused"] is True, "strict asks for the refused count (nx t3 gc prints it)"


# ── the wire request ─────────────────────────────────────────────────────────


@BOTH_MODES
def test_every_page_carries_the_inert_floor_the_same_cutoff_and_the_row_limit(strict):
    db = Pages(page(100, more=True, refused=0), page(100, more=True), page(1, more=False))
    drain(db, strict=strict)
    assert len(db.calls) == 3
    for call in db.calls:
        assert (call["quarantine"], call["origin"], call["cutoff"]) == (SIBLING, ORIGIN, CUTOFF)
        assert (call["fraction"], call["minimum"], call["force"], call["row_limit"]) == (1.0, 100, False, 100)


def test_count_refused_is_false_unless_strict_and_strict_stops_asking_once_a_page_answered_it():
    db = Pages(page(100, more=True), page(1, more=False))
    drain(db)
    assert [c["count_refused"] for c in db.calls] == [False, False]

    db = Pages(page(100, more=True, refused=None), page(100, more=True, refused=4), page(1, more=False))
    assert drain(db, strict=True) == (201, 4, True)
    assert [c["count_refused"] for c in db.calls] == [True, True, False]


# ── answers ──────────────────────────────────────────────────────────────────


@BOTH_MODES
def test_pages_sum_and_the_first_non_null_refused_is_kept_and_more_false_stops(strict):
    db = Pages(page(100, more=True, refused=7), page(100, more=True, refused=3), page(5, more=False, refused=3))
    assert drain(db, strict=strict) == (205, 7, True)
    assert len(db.calls) == 3


@BOTH_MODES
def test_an_answer_with_no_more_key_is_an_older_engines_complete_unbounded_answer(strict):
    db = Pages({"expired": 41, "refused": 2})
    assert drain(db, strict=strict) == (41, 2, True)
    assert len(db.calls) == 1


@BOTH_MODES
@pytest.mark.parametrize("body", [
    {"expired": 5, "refused": None, "row_limit": 100},
    {"expired": 5, "refused": None, "more": None, "row_limit": 100, "floor_refused": False},
], ids=["row_limit-no-more", "row_limit-null-more"])
def test_a_body_with_row_limit_but_no_more_is_a_shape_defect_not_an_old_engines_answer(strict, body):
    """A bounded engine always answers `row_limit`; an answer that carries it and lost `more` is not the
    unbounded older engine's complete total. Reading it as one would report a partial drain as done."""
    db = Pages(body)
    with pytest.raises(ValueError, match="more"):
        drain(db, strict=strict)
    assert len(db.calls) == 1


def test_no_progress_warns_with_the_totals_so_far_non_strict_and_raises_strict():
    db = Pages(page(100, more=True), page(0, more=True))
    with capture_logs() as cap:
        assert drain(db) == (100, None, True)
    assert events(cap, "gc_expire_bounded_no_progress"), cap
    assert len(db.calls) == 2

    db = Pages(page(100, more=True), page(0, more=True))
    with pytest.raises(BoundedDrainIncomplete) as exc:
        drain(db, strict=True)
    assert (exc.value.reason, exc.value.moved, exc.value.remaining, exc.value.batches, exc.value.kind) == (
        "no progress", 100, None, 2, "expire",
    )


@BOTH_MODES
@pytest.mark.parametrize("more", [True, False], ids=["more", "no-more"])
def test_a_floor_refusal_is_no_progress_too(strict, more):
    """`more=False, floor_refused=True` reaches the no-progress exit only through the floor_refused clause."""
    db = Pages(page(0, more=more, refused=900, floor_refused=True))
    if strict:
        with pytest.raises(BoundedDrainIncomplete) as exc:
            drain(db, strict=True)
        assert exc.value.reason == "no progress"
    else:
        with capture_logs() as cap:
            assert drain(db) == (0, 900, True)
        (warning,) = events(cap, "gc_expire_bounded_no_progress")
        assert warning["floor_refused"] is True
    assert len(db.calls) == 1


@BOTH_MODES
def test_a_page_committed_with_unknown_counts_continues_and_marks_the_total_a_lower_bound(strict):
    db = Pages(page(100, more=True), CommittedWithUnknownCounts(committed_unknown_counts=True), page(7, more=False))
    with capture_logs() as cap:
        assert drain(db, strict=strict) == (107, None, False)
    assert len(db.calls) == 3
    assert events(cap, "gc_expire_batch_committed_counts_unknown")


LOST = CommittedWithUnknownCounts(committed_unknown_counts=True)


def test_a_lost_count_page_then_no_progress_keeps_the_total_a_lower_bound():
    db = Pages(page(100, more=True), LOST, page(0, more=True))
    with capture_logs():
        assert drain(db) == (100, None, False)
    db = Pages(page(100, more=True), LOST, page(0, more=True))
    with pytest.raises(BoundedDrainIncomplete) as exc:
        drain(db, strict=True)
    assert exc.value.reason == "no progress" and exc.value.moved == 100


def test_a_lost_count_page_then_the_iteration_cap_keeps_the_total_a_lower_bound():
    row_limit = 1_000_000
    cap_pages = _gc_loop_max_iterations(row_limit)
    # page 1 is lost, the rest (the last entry repeats) always have more: the cap ends the drain.
    db = Pages(LOST, page(5, more=True))
    with capture_logs() as cap:
        assert drain(db, row_limit=row_limit) == (5 * (cap_pages - 1), None, False)
    assert events(cap, "gc_expire_bounded_loop_iteration_cap_reached")
    db = Pages(LOST, page(5, more=True))
    with pytest.raises(BoundedDrainIncomplete) as exc:
        drain(db, strict=True, row_limit=row_limit)
    assert exc.value.reason == "iteration cap"


@BOTH_MODES
def test_a_lost_count_page_then_an_older_engines_no_more_answer_keeps_the_total_a_lower_bound(strict):
    db = Pages(LOST, {"expired": 9, "refused": 1})
    assert drain(db, strict=strict) == (9, 1, False)


def test_the_iteration_cap_warns_non_strict_and_raises_strict():
    row_limit = 1_000_000
    cap_pages = _gc_loop_max_iterations(row_limit)
    db = Pages(page(5, more=True))
    with capture_logs() as cap:
        assert drain(db, row_limit=row_limit) == (5 * cap_pages, None, True)
    assert len(db.calls) == cap_pages
    assert events(cap, "gc_expire_bounded_loop_iteration_cap_reached")

    db = Pages(page(5, more=True))
    with pytest.raises(BoundedDrainIncomplete) as exc:
        drain(db, strict=True, row_limit=row_limit)
    assert exc.value.reason == "iteration cap" and exc.value.moved == 5 * cap_pages


# ── errors: the first page ───────────────────────────────────────────────────

TRANSPORT_ERRORS = [
    VectorServiceError("boom", code=500),
    VectorServiceError("refused key", code=400),
    TimeoutError("timed out"),
    http.client.IncompleteRead(b"x"),
]


@pytest.mark.parametrize("error", TRANSPORT_ERRORS, ids=lambda e: type(e).__name__)
def test_a_first_page_error_warns_and_returns_nothing_expired_non_strict(error):
    db = Pages(error)
    with capture_logs() as cap:
        assert drain(db) == (0, None, False)
    (warning,) = events(cap, "gc_expire_bounded_page_failed")
    assert warning["log_level"] == "warning" and warning["quarantine_collection"] == SIBLING


@pytest.mark.parametrize("error", TRANSPORT_ERRORS, ids=lambda e: type(e).__name__)
def test_a_first_page_error_is_re_raised_unchanged_strict(error):
    with pytest.raises(type(error)) as exc:
        drain(Pages(error), strict=True)
    assert exc.value is error


# ── errors: a later page ─────────────────────────────────────────────────────


def _unsettled(key: str) -> VectorServiceError:
    exc = VectorServiceError("HTTP 504", code=504)
    exc.add_note(f"request {key}: outcome unknown (the status poll budget ran out): the sweep may have committed")
    return exc


@pytest.mark.parametrize(
    "error", [_unsettled("11111111-1111-4111-8111-111111111111"), VectorServiceError("bad", code=400),
              http.client.IncompleteRead(b"x")],
    ids=["unsettled-with-key-note", "definite-400", "incomplete-read"],
)
def test_a_later_page_error_returns_partial_totals_marked_incomplete_non_strict(error):
    db = Pages(page(100, more=True, refused=4), error)
    with capture_logs() as cap:
        assert drain(db) == (100, 4, False)
    (warning,) = events(cap, "gc_expire_bounded_page_failed")
    assert warning["expired_before"] == 100 and warning["page"] == 2
    assert warning["error"] and isinstance(warning["notes"], list)
    if getattr(error, "__notes__", None):
        assert "11111111-1111-4111-8111-111111111111" in " ".join(warning["notes"])


@pytest.mark.parametrize(
    "error", [_unsettled("22222222-2222-4222-8222-222222222222"), VectorServiceError("bad", code=400),
              http.client.IncompleteRead(b"x")],
    ids=["unsettled-with-key-note", "definite-400", "incomplete-read"],
)
def test_a_later_page_error_is_a_bounded_drain_incomplete_with_the_error_as_cause_strict(error):
    db = Pages(page(100, more=True, refused=4), error)
    with pytest.raises(BoundedDrainIncomplete) as exc:
        drain(db, strict=True)
    assert (exc.value.reason, exc.value.moved, exc.value.batches, exc.value.kind) == ("page failed", 100, 1, "expire")
    assert exc.value.remaining is None and exc.value.__cause__ is error


# ── data defects propagate in both modes (F4) ────────────────────────────────

DEFECTS = [
    ProductionWriteGuardError("refused: no opt-in"),
    ValueError("malformed"),
    TypeError("malformed"),
]


@BOTH_MODES
@pytest.mark.parametrize("error", DEFECTS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("later", [False, True], ids=["first-page", "later-page"])
def test_a_data_defect_or_a_write_guard_refusal_propagates_in_both_modes(strict, error, later):
    entries = [page(10, more=True), error] if later else [error]
    with pytest.raises(type(error)) as exc:
        drain(Pages(*entries), strict=strict)
    assert exc.value is error


@BOTH_MODES
def test_a_page_whose_counts_are_not_numbers_propagates(strict):
    with pytest.raises((TypeError, ValueError)):
        drain(Pages({"expired": None, "more": False}), strict=strict)


# ── BoundedDrainIncomplete names the drain ───────────────────────────────────


def test_the_exception_names_the_drain_and_the_quarantine_message_is_unchanged():
    old = BoundedDrainIncomplete("no progress", 12, 3, 4)
    assert old.kind == "quarantine"
    assert str(old) == "bounded quarantine drain stopped (no progress) after 4 batch(es): 12 moved, remaining=3"

    new = BoundedDrainIncomplete("page failed", 12, None, 2, kind="expire")
    assert "expire" in str(new) and "12 expired" in str(new) and "moved" not in str(new)
    assert "quarantine drain" not in str(new)
    doc = (BoundedDrainIncomplete.__doc__ or "").lower()
    assert "quarantine drain" in doc and "expir" in doc and "page failed" in doc
