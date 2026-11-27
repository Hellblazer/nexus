# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TTL sweep names each note it retires, and ``--dry-run`` judges by the same code (nexus-6pfgb.15, 8.5.8 shakeout).

Storage-free, on the fake catalog and fake ``/v1/vectors`` wire of ``tests/test_6pfgb15_note_ttl.py``; the same claims
against the real engine are ``tests/integration/test_nx_store_expire_audit.py``.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.db.http_vector_client import HttpVectorClient
from nexus.metadata_schema import chunk_row_expires_at
from tests.test_6pfgb15_note_ttl import _COLLECTION, NOW, _ago, _entry, _FakeCatalog, _lapsed_row, _World


def test_the_report_names_each_retired_note_with_its_collection_and_expiry(monkeypatch):
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2), "doc_id": "c1"}, title="probe-one"),
         _entry("1.1.2", meta={"ttl_days": 30, "put_at": _ago(2), "doc_id": "c2"}, title="long-lived")],
        {"1.1.1": ["c1"], "1.1.2": ["c2"]})
    w = _World(monkeypatch, cat, {})

    report = w.sweep()

    assert [(n.collection, n.tumbler, n.title, n.basis) for n in report.retired] == [
        (_COLLECTION, "1.1.1", "probe-one", "catalog")]
    assert report.retired[0].expires_at == (NOW - timedelta(days=2) + timedelta(days=1)).isoformat()


def test_dry_run_judges_by_the_catalog_ttl_and_writes_nothing(monkeypatch):
    """The shakeout's pre-list read chunk-row expiry; the sweep retired a fourth note by its catalog TTL. The row here
    carries no TTL at all, and the dry run still names the note."""
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2), "doc_id": "c1"}, title="catalog-only")],
        {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {"c1": {"indexed_at": _ago(2)}})  # no ttl_days on the row

    dry = HttpVectorClient().expire_report(dry_run=True)

    assert cat.tombstoned == [] and cat.manifest_writes == [] and w.deleted == [] and w.reaped_chashes == []
    assert dry.dry_run and (dry.chunks_deleted, dry.notes_tombstoned) == (0, 0)
    assert [(n.title, n.tumbler) for n in dry.retired] == [("catalog-only", "1.1.1")]
    assert dry.candidate_chunks == 1

    real = w.sweep()

    assert [(n.collection, n.tumbler, n.title, n.expires_at) for n in real.retired] == [
        (n.collection, n.tumbler, n.title, n.expires_at) for n in dry.retired]
    assert cat.tombstoned == ["1.1.1"] and w.deleted == [["c1"]]


def test_dry_run_names_a_keyless_legacy_note_the_row_judges_and_an_orphan_chunk(monkeypatch):
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"doc_id": "c1"})], {"1.1.1": ["c1"]})
    rows = {"c1": _lapsed_row(1, written_by="1.1.1"), "c-orphan": _lapsed_row(1)}
    w = _World(monkeypatch, cat, rows)

    dry = HttpVectorClient().expire_report(dry_run=True)

    assert [(n.title, n.basis) for n in dry.retired] == [("note-1.1.1", "chunk row")]
    assert dry.retired[0].expires_at == (NOW - timedelta(days=99)).isoformat(), "indexed 100 days ago with a 1 day TTL"
    assert dry.orphan_chunks == 1 and dry.candidate_chunks == 2
    assert w.deleted == [] and w.reaped_chashes == [] and cat.tombstoned == []


def test_dry_run_holds_a_note_the_guard_holds_and_reports_a_note_it_cannot_judge(monkeypatch):
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2), "doc_id": "c1"}, title="re-put-permanent"),
         _entry("1.1.2", meta={"ttl_days": 1, "put_at": _ago(2), "doc_id": "c2"}, title="unreadable")],
        {"1.1.1": ["c1"], "1.1.2": ["c2"]})
    rows = {"c1": {"indexed_at": _ago(0), "catalog_doc_id": "1.1.1", "title": "re-put-permanent"}}
    w = _World(monkeypatch, cat, rows)

    def store_get(path, body, **kw):
        if path == "/v1/vectors/store-get" and "c2" in body["ids"]:
            raise RuntimeError("engine unreachable")
        return original(path, body, **kw)

    original = w._post(rows)
    monkeypatch.setattr("nexus.db.http_vector_client._post", store_get)

    dry = HttpVectorClient().expire_report(dry_run=True)

    assert dry.retired == ()
    assert [t for _, t in dry.catalog_ttl_stale] == ["re-put-permanent"]
    assert [t for t, _ in dry.failures] == ["1.1.2"]
    assert cat.tombstoned == [] and w.deleted == []


@pytest.mark.parametrize(("meta", "expected"), [
    ({"ttl_days": 2, "indexed_at": "2026-10-24T12:00:00+00:00"}, "2026-10-26T12:00:00+00:00"),
    ({"ttl_days": 1, "indexed_at": "2026-10-25T12:00:00"}, "2026-10-26T12:00:00"),
    ({"ttl_days": None, "indexed_at": "2026-10-24T12:00:00+00:00"}, None),
    ({"ttl_days": 0, "indexed_at": "2026-10-24T12:00:00+00:00"}, None),
    ({"ttl_days": 2}, None),
    ({"ttl_days": 2, "indexed_at": "not a date"}, None),
    ({"ttl_days": 10**12, "indexed_at": "2026-10-24T12:00:00+00:00"}, None),
    ({}, None),
])
def test_a_chunk_rows_expiry_instant_is_its_stamp_plus_its_ttl(meta, expected):
    got = chunk_row_expires_at(meta)
    assert (got.isoformat() if got is not None else None) == expected
