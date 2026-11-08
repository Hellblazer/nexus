# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-alsq3 (RDR-192 residue): the client's quarantine retention rides the move, client half.

``NX_GC_QUARANTINE_DAYS`` used to be honoured by the client's own expiry only: the engine's expiry of client-moved
knowledge quarantine ran on its own 14 days whatever the user set. The client now sends ``quarantine_expires_at``
(``quarantined_at`` plus ``quarantine_days()``, an absolute fixed-width UTC instant) on every move of a KNOWLEDGE
origin (the engine's expiry reads it nowhere else), the engine (vectors-056) writes it on the rows that call moved,
and its expiry waits for the later of its retention and the stamp. The engine half is pinned in ``GcQuarantineExpiresAtRouteTest`` (service/).

What each test pins is named by what its revert would do: a wrapper that dropped the field leaves the request
without it; an HTTP method that dropped it never puts it in the body; a stamp computed from the wrong clock, shape
or variable fails the arithmetic test.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog.testing

import nexus.catalog.chunk_quarantine as cq
from nexus.catalog.chunk_quarantine import GcFloor, quarantine_expires_at

_COLL = "knowledge__nexus-1-1__voyage-context-3__v1"
_QUAR = "quarantine-" + _COLL
_STAMP = "2026-10-04T00:00:00Z"


@pytest.fixture(autouse=True)
def _default_days(monkeypatch):
    monkeypatch.delenv("NX_GC_QUARANTINE_DAYS", raising=False)


# ── the stamp ─────────────────────────────────────────────────────────────────


def test_the_stamp_is_quarantined_at_plus_the_users_days_in_the_same_shape(monkeypatch) -> None:
    assert quarantine_expires_at(_STAMP) == "2026-10-18T00:00:00Z", "the default is 14 days"
    monkeypatch.setenv("NX_GC_QUARANTINE_DAYS", "60")
    assert quarantine_expires_at(_STAMP) == "2026-12-03T00:00:00Z"
    monkeypatch.setenv("NX_GC_QUARANTINE_DAYS", "0")
    assert quarantine_expires_at(_STAMP) == _STAMP, "0 days keeps nothing past the move"
    assert quarantine_expires_at("2026-12-25T23:59:59Z", days=7) == "2027-01-01T23:59:59Z", "carries over a year end"


@pytest.mark.parametrize("bad", ["", "2026-10-04", "2026-10-04T00:00:00+00:00", "2026-10-04T00:00:00.5Z", "yesterday"])
def test_a_moved_at_that_is_not_the_stamp_shape_gets_no_stamp(bad) -> None:
    """No stamp is better than a guessed one: the engine keeps its own retention for the row."""
    assert quarantine_expires_at(bad) is None


# ── the wrappers ──────────────────────────────────────────────────────────────


class _Db:
    """A handle that records the keyword fields each move call carried and answers from a script. It resolves the
    origin's catalog row the way ``HttpVectorClient`` does, answering ``content_type`` (knowledge unless told)."""

    content_type: str | None = "knowledge"

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def _resolve_collection_row(self, collection, *, strict=False):
        return None if self.content_type is None else {"content_type": self.content_type}

    def gc_quarantine_orphans_bounded(self, c, q, at, sample, row_limit, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)

    def gc_quarantine_orphans(self, c, q, at, sample, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _batch(moved: int, remaining: int) -> dict:
    return {"moved": moved, "sample": [], "remaining": remaining, "row_limit": 2000}


def test_the_bounded_wrapper_sends_the_stamp_on_every_batch_floor_or_not() -> None:
    db = _Db(_batch(2, 2), _batch(2, 0))
    assert cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP) == (4, [])
    assert db.calls == [{"quarantine_expires_at": "2026-10-18T00:00:00Z"}] * 2, "each batch stamps its own rows"


def test_the_floor_stays_on_the_first_batch_while_the_stamp_rides_all_of_them() -> None:
    echo = {"floor": {"given": True, "fraction": 0.25, "min_chunks": 100, "force": False}}
    db = _Db({**_batch(2, 2), **echo}, _batch(1, 0))
    cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP, floor=GcFloor(0.25, 100))
    stamp = {"quarantine_expires_at": "2026-10-18T00:00:00Z"}
    assert db.calls == [
        {"floor_fraction": 0.25, "floor_min_chunks": 100, "force": False, **stamp},
        stamp,
    ]


def test_the_unbounded_wrapper_sends_the_stamp_with_and_without_a_floor() -> None:
    plain = _Db({"moved": 1, "sample": []})
    cq.quarantine_orphans_serverside(plain, _COLL, _QUAR, _STAMP)
    assert plain.calls == [{"quarantine_expires_at": "2026-10-18T00:00:00Z"}]

    echo = {"floor": {"given": True, "fraction": 0.25, "min_chunks": 100, "force": False}}
    floored = _Db({"moved": 1, "sample": [], **echo})
    cq.quarantine_orphans_serverside(floored, _COLL, _QUAR, _STAMP, floor=GcFloor(0.25, 100))
    assert floored.calls[0]["quarantine_expires_at"] == "2026-10-18T00:00:00Z"
    assert floored.calls[0]["floor_fraction"] == 0.25


def test_the_users_days_reach_the_request(monkeypatch) -> None:
    """The point of the bead: a user who sets 60 days sends a stamp 60 days out, which the engine now honours."""
    monkeypatch.setenv("NX_GC_QUARANTINE_DAYS", "60")
    db = _Db(_batch(1, 0))
    cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP)
    assert db.calls == [{"quarantine_expires_at": "2026-12-03T00:00:00Z"}]


def test_an_unusable_moved_at_sends_no_stamp_rather_than_a_guess() -> None:
    db = _Db(_batch(1, 0))
    cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, "not a stamp")
    assert db.calls == [{}]


# ── knowledge only: the engine's expiry reads the stamp nowhere else ──────────


@pytest.mark.parametrize("kind", ["code", "docs", "rdr"])
def test_a_non_knowledge_origin_sends_no_stamp(kind) -> None:
    """The engine's expiry touches a registered live KNOWLEDGE origin only; a stamp on a code, docs or rdr move
    would be a write nothing reads."""
    bounded = _Db(_batch(1, 0))
    bounded.content_type = kind
    cq.quarantine_orphans_bounded_serverside(bounded, _COLL, _QUAR, _STAMP)
    unbounded = _Db({"moved": 1, "sample": []})
    unbounded.content_type = kind
    cq.quarantine_orphans_serverside(unbounded, _COLL, _QUAR, _STAMP)
    assert bounded.calls == [{}] and unbounded.calls == [{}]


def test_an_origin_whose_row_cannot_be_read_sends_no_stamp_and_the_move_still_runs() -> None:
    class _Unreadable(_Db):
        # As HttpVectorClient: a failed listing raises only under strict, and is a None otherwise.
        def _resolve_collection_row(self, collection, *, strict=False):
            if strict:
                raise RuntimeError("listing failed")
            return None

    no_row = _Db(_batch(1, 0))
    no_row.content_type = None
    unreadable = _Unreadable(_batch(1, 0))
    assert cq.quarantine_orphans_bounded_serverside(no_row, _COLL, _QUAR, _STAMP) == (1, [])
    assert cq.quarantine_orphans_bounded_serverside(unreadable, _COLL, _QUAR, _STAMP) == (1, [])
    assert no_row.calls == [{}] and unreadable.calls == [{}]


def test_a_failed_listing_is_read_strictly_and_warned() -> None:
    class _Unreadable(_Db):
        def __init__(self, *responses: dict) -> None:
            super().__init__(*responses)
            self.strict_seen: list[bool] = []

        def _resolve_collection_row(self, collection, *, strict=False):
            self.strict_seen.append(strict)
            if strict:
                raise RuntimeError("listing failed")
            return None

    db = _Unreadable(_batch(1, 0))
    with structlog.testing.capture_logs() as logs:
        assert cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP) == (1, [])
    assert db.strict_seen == [True]
    assert [e["event"] for e in logs if e.get("log_level") == "warning"] == ["quarantine_retention_stamp_row_unreadable"]


def test_a_handle_that_cannot_resolve_a_row_sends_no_stamp() -> None:
    class _Bare:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def gc_quarantine_orphans_bounded(self, c, q, at, sample, row_limit, **kw):
            self.calls.append(kw)
            return _batch(1, 0)

    bare = _Bare()
    cq.quarantine_orphans_bounded_serverside(bare, _COLL, _QUAR, _STAMP)
    assert bare.calls == [{}]


# ── the HTTP client ───────────────────────────────────────────────────────────


def _http_client(content_type: str = "knowledge"):
    from nexus.db.http_vector_client import HttpVectorClient  # noqa: PLC0415 — deferred so the module-level doubles need no client import

    client = HttpVectorClient.__new__(HttpVectorClient)
    client._tenant = None
    client._row_resolver = lambda name: {"content_type": content_type}
    return client


def test_the_http_client_puts_the_stamp_in_the_unbounded_body_only_when_given_one() -> None:
    sent: list[dict] = []

    def fake_post(path, body, **_kw):
        sent.append(body)
        return {"moved": 0, "sample": []}

    client = _http_client()
    with patch("nexus.db.http_vector_client._post", fake_post):
        client.gc_quarantine_orphans(_COLL, _QUAR, _STAMP)
        client.gc_quarantine_orphans(_COLL, _QUAR, _STAMP, quarantine_expires_at="2026-10-18T00:00:00Z")
    assert "quarantine_expires_at" not in sent[0], "a caller without a stamp sends the request this method always sent"
    assert sent[1]["quarantine_expires_at"] == "2026-10-18T00:00:00Z"
    assert sent[1]["quarantined_at"] == _STAMP


def test_the_http_client_puts_the_stamp_in_the_bounded_body_only_when_given_one() -> None:
    sent: list[dict] = []

    def fake_keyed(path, body, **_kw):
        sent.append(body)
        return {"moved": 0, "sample": [], "remaining": 0}

    client = _http_client()
    with patch("nexus.db.http_vector_client._keyed_sweep_post", fake_keyed):
        client.gc_quarantine_orphans_bounded(_COLL, _QUAR, _STAMP, 20, 2000)
        client.gc_quarantine_orphans_bounded(
            _COLL, _QUAR, _STAMP, 20, 2000, floor_fraction=0.5, quarantine_expires_at="2026-10-18T00:00:00Z")
    assert "quarantine_expires_at" not in sent[0]
    assert sent[1]["quarantine_expires_at"] == "2026-10-18T00:00:00Z"
    assert sent[1]["row_limit"] == 2000 and sent[1]["floor_fraction"] == 0.5


def test_a_wrapper_over_the_http_client_puts_the_stamp_in_the_real_request_body() -> None:
    """End to end through the real wrappers and the real client methods, faking only the transport."""
    sent: list[dict] = []

    def fake_keyed(path, body, **_kw):
        sent.append(body)
        return {"moved": 1, "sample": [], "remaining": 0}

    def fake_post(path, body, **_kw):
        sent.append(body)
        return {"moved": 1, "sample": []}

    client = _http_client()
    with (
        patch("nexus.db.http_vector_client._keyed_sweep_post", fake_keyed),
        patch("nexus.db.http_vector_client._post", fake_post),
    ):
        cq.quarantine_orphans_bounded_serverside(client, _COLL, _QUAR, _STAMP)
        cq.quarantine_orphans_serverside(client, _COLL, _QUAR, _STAMP)
    assert [b["quarantine_expires_at"] for b in sent] == ["2026-10-18T00:00:00Z"] * 2
