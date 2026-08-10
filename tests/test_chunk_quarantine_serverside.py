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
    restore_rereferenced_serverside,
)
from nexus.db.http_vector_client import VectorServiceError


class _NoGcMethods:
    """A `db` with no HTTP GC capability at all (local/in-memory mode)."""


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


def test_quarantine_serverside_404_returnsNone():
    db = type("Db", (), {"gc_quarantine_orphans": _Raises(VectorServiceError("nope", code=404))})()
    assert quarantine_orphans_serverside(db, "code__x", "quarantine-code__x", now_stamp()) is None


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


def test_restore_serverside_404_returnsNone():
    db = type("Db", (), {"gc_restore_rereferenced": _Raises(VectorServiceError("nope", code=404))})()
    assert restore_rereferenced_serverside(db, "quarantine-code__x", "code__x") is None


def test_restore_serverside_non404_reraises():
    db = type("Db", (), {"gc_restore_rereferenced": _Raises(VectorServiceError("boom", code=500))})()
    with pytest.raises(VectorServiceError):
        restore_rereferenced_serverside(db, "quarantine-code__x", "code__x")


def test_restore_serverside_success_returnsCount():
    db = type("Db", (), {"gc_restore_rereferenced": _Returns(7)})()
    assert restore_rereferenced_serverside(db, "quarantine-code__x", "code__x") == 7


# ── expire_quarantine_serverside ─────────────────────────────────────────


def test_expire_serverside_noMethod_returnsNone():
    db = _NoGcMethods()
    assert expire_quarantine_serverside(
        db, "quarantine-code__x", "code__x", now_stamp(),
        floor_fraction=0.5, floor_min_chunks=100,
    ) is None


def test_expire_serverside_404_returnsNone():
    db = type("Db", (), {"gc_expire_quarantine": _Raises(VectorServiceError("nope", code=404))})()
    assert expire_quarantine_serverside(
        db, "quarantine-code__x", "code__x", now_stamp(),
        floor_fraction=0.5, floor_min_chunks=100,
    ) is None


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
