# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.15: the TTL sweep judges each owning note by its own catalog entry.

Storage-free. The rules in :mod:`nexus.catalog.note_ttl` are pure; the sweep
(``HttpVectorClient.expire_report``) runs against a fake catalog and a fake ``/v1/vectors`` wire, so
every branch is reachable: a lapsed note, a permanent one, the retraction value 0, an orphan chunk,
a note put before the catalog held its TTL, a chunk several such notes share, and a retirement that
fails. The same claims against the real engine (including the engine's delete anti-join, which a
fake cannot show) are ``tests/integration/test_6pfgb15_expire_per_owner.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nexus.catalog import note_ttl
from nexus.catalog.tumbler import Tumbler
from nexus.db.http_vector_client import ExpireIncompleteError, HttpVectorClient
from nexus.metadata_schema import is_expired

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
_COLLECTION = "knowledge__6pfgb15__bge-base-en-v15-768__v1"


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


# ── the rules ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("meta", [
    {}, {"ttl_days": 0}, {"ttl_days": None}, {"ttl_days": -3}, {"ttl_days": "n/a"}, {"ttl_days": True},
    None, "not a dict",
    # past timedelta's range, or not an int at all: one such note must not abort every later sweep
    {"ttl_days": 10**12}, {"ttl_days": float("inf")}, {"ttl_days": float("nan")}, {"ttl_days": 1e300},
    {"ttl_days": "1" + "0" * 30},
])
def test_absent_zero_and_unreadable_ttl_are_permanent(meta):
    assert note_ttl.note_ttl_days(meta) == 0
    assert not note_ttl.lapsed(meta, _ago(400), now=NOW)


@pytest.mark.parametrize("meta", [
    {"ttl_days": 10**12, "put_at": _ago(400)}, {"ttl_days": float("inf"), "put_at": _ago(400)},
    {"ttl_days": 1e300, "put_at": _ago(400)},
])
def test_a_ttl_past_the_range_of_a_timedelta_never_lapses_and_never_raises(meta):
    assert not note_ttl.lapsed(meta, _ago(400), now=NOW)


def test_a_garbage_ttl_does_not_abort_the_sweep_for_the_notes_after_it(monkeypatch):
    cat = _FakeCatalog([
        _entry("1.1.1", meta={"ttl_days": float("inf"), "put_at": _ago(400)}),
        _entry("1.1.2", meta={"ttl_days": 10**12, "put_at": _ago(400)}),
        _entry("1.1.3", meta={"ttl_days": 1, "put_at": _ago(2)}),
    ], {"1.1.1": ["c1"], "1.1.2": ["c2"], "1.1.3": ["c3"]})
    w = _World(monkeypatch, cat, {})

    report = w.sweep()

    assert cat.tombstoned == ["1.1.3"] and w.deleted == [["c3"]]
    assert (report.notes_tombstoned, report.failures) == (1, ())


def test_a_positive_ttl_lapses_on_its_own_clock_to_the_second():
    meta = {"ttl_days": 7, "put_at": _ago(7)}
    assert note_ttl.lapsed(meta, _ago(0), now=NOW), "exactly 7 days after the put"
    assert not note_ttl.lapsed({"ttl_days": 7, "put_at": _ago(6.99)}, _ago(400), now=NOW)


def test_the_put_time_is_the_clock_not_the_entrys_indexed_at():
    """The engine refreshes indexed_at on every manifest write, a heal or a backfill included, so a
    TTL counted from it drifts later each time. put_at is written once per put."""
    meta = {"ttl_days": 7, "put_at": _ago(10)}
    refreshed = _ago(0.01)  # a heal ten minutes ago
    assert note_ttl.clock_start(meta, refreshed) == _ago(10)
    assert note_ttl.lapsed(meta, refreshed, now=NOW)


def test_an_entry_with_a_ttl_but_no_put_time_counts_from_indexed_at():
    meta = {"ttl_days": 7}
    assert note_ttl.clock_start(meta, _ago(8)) == _ago(8)
    assert note_ttl.lapsed(meta, _ago(8), now=NOW)
    assert not note_ttl.lapsed(meta, _ago(6), now=NOW)


def test_no_readable_clock_never_lapses():
    assert not note_ttl.lapsed({"ttl_days": 7}, "", now=NOW)
    assert not note_ttl.lapsed({"ttl_days": 7, "put_at": "yesterday"}, "also not a date", now=NOW)


def test_naive_and_z_stamps_read_as_utc():
    assert note_ttl.lapsed({"ttl_days": 1, "put_at": "2026-10-24T12:00:00"}, "", now=NOW)
    assert note_ttl.lapsed({"ttl_days": 1, "put_at": "2026-10-24T12:00:00Z"}, "", now=NOW)


@pytest.mark.parametrize(("meta", "expected"), [
    ({}, False), ({"doc_id": "x"}, False), ({"ttl_days": 0}, True), ({"ttl_days": 5}, True),
    ({"put_at": _ago(1)}, True), ({"put_at": ""}, False), (None, False),
])
def test_an_entry_has_its_own_keys_when_it_carries_a_put_time_or_a_ttl_key(meta, expected):
    assert note_ttl.has_own_keys(meta) is expected


@pytest.mark.parametrize(("owner", "row", "expected"), [
    ("1.1.1", {"catalog_doc_id": "1.1.1", "title": "N"}, True),
    (Tumbler.parse("1.1.1"), {"catalog_doc_id": "1.1.1", "title": "N"}, True),
    ("1.1.1", {"catalog_doc_id": "1.1.2", "title": "N"}, False),
    ("1.1.1", {"catalog_doc_id": "1.1.11", "title": "N"}, False),
    ("1.1.1", {"catalog_doc_id": "", "title": "N"}, False),
    ("1.1.1", {"title": "N"}, False),
    ("1.1.1", {"catalog_doc_id": None, "title": "N"}, False),
    ("1.1.1", {"catalog_doc_id": 111, "title": "N"}, False),
    ("1.1.1", None, False),
    ("1.1.1", "not a dict", False),
    # an .nxexp import keeps the source box's stamp: the tumbler can match by chance, the title does not
    ("1.1.1", {"catalog_doc_id": "1.1.1", "title": "another note"}, False),
    ("1.1.1", {"catalog_doc_id": "1.1.1", "title": ""}, False),
    ("1.1.1", {"catalog_doc_id": "1.1.1"}, False),
    ("1.1.1", {"catalog_doc_id": "1.1.1", "title": None}, False),
])
def test_a_row_is_an_owners_only_when_its_catalog_doc_id_and_title_are_that_owners(owner, row, expected):
    assert note_ttl.row_written_by(owner, row, owner_title="N") is expected


def test_an_owner_with_no_title_is_never_the_writer_of_a_row():
    assert not note_ttl.row_written_by("1.1.1", {"catalog_doc_id": "1.1.1", "title": ""}, owner_title="")


# ── the sweep ────────────────────────────────────────────────────────────────


def _entry(tumbler, *, meta, title="", content_type="knowledge", file_path="", collection=_COLLECTION,
           indexed_at=None):
    return SimpleNamespace(
        tumbler=Tumbler.parse(tumbler), title=title or f"note-{tumbler}", content_type=content_type,
        file_path=file_path, physical_collection=collection, indexed_at=indexed_at or _ago(0), meta=meta)


class _FakeCatalog:
    """Reader and writer in one. ``owners`` maps chash to tumblers; ``manifests`` tumbler to chashes."""

    def __init__(self, entries, manifests):
        self.docs = {str(e.tumbler): e for e in entries}
        self.manifests = {t: list(c) for t, c in manifests.items()}
        self.tombstoned: list[str] = []
        self.manifest_writes: list[tuple[str, list[str]]] = []
        self.fail_tombstone: set[str] = set()

    def list_by_collection(self, collection, *, limit=None):
        return [e for t, e in self.docs.items() if e.physical_collection == collection and t not in self.tombstoned]

    def docs_for_chashes(self, chashes):
        return {c: [t for t, cs in self.manifests.items() if c in cs] for c in chashes}

    def resolve_many(self, tumblers):
        return {t: self.docs[t] for t in tumblers if t in self.docs and t not in self.tombstoned}

    def get_manifest(self, tumbler):
        return [SimpleNamespace(chash=c, position=i) for i, c in enumerate(self.manifests.get(str(tumbler), []))]

    def write_manifest(self, tumbler, rows, *, collection):
        self.manifest_writes.append((str(tumbler), [r["chash"] for r in rows]))
        self.manifests[str(tumbler)] = [r["chash"] for r in rows]

    def delete_document(self, tumbler):
        if str(tumbler) in self.fail_tombstone:
            raise RuntimeError("catalog refused")
        self.tombstoned.append(str(tumbler))

    def close(self):
        pass


class _World:
    """A sweep over one knowledge collection: rows the wire returns, and a catalog."""

    def __init__(self, monkeypatch, catalog, rows):
        self.deleted: list[list[str]] = []
        self.get_wheres: list[dict] = []
        self.reaped_chashes: list[str] = []
        monkeypatch.setattr(HttpVectorClient, "list_collections", lambda self: [
            {"name": _COLLECTION, "count": 1, "content_type": "knowledge"}])
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._post(rows))
        monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: catalog)
        monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: catalog)
        # the legacy single-owner reap resolves through the same fake; a real resolve needs more of
        # the reader than the fake carries, so it is recorded here and its effect asserted separately
        monkeypatch.setattr(
            "nexus.catalog.store_hook.reap_catalog_manifest_for_chashes",
            lambda chashes, *, expected_collection: self.reaped_chashes.extend(chashes))
        monkeypatch.setattr("nexus.db.http_vector_client.datetime", _FrozenDatetime)
        self.catalog = catalog

    def _post(self, rows):
        def post(path, body, **kw):
            if path == "/v1/vectors/get":
                self.get_wheres.append(body["where"])
                if body["offset"] > 0:
                    return {"ids": [], "metadatas": []}
                return {"ids": list(rows), "metadatas": list(rows.values())}
            if path == "/v1/vectors/store-delete":
                self.deleted.append(list(body["ids"]))
                return {"deleted": len(body["ids"])}
            raise AssertionError(path)
        return post

    def sweep(self):
        return HttpVectorClient().expire_report()


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def _lapsed_row(ttl=1, *, written_by=None):
    """A chunk row whose ttl has lapsed. *written_by* is the tumbler the row's ``catalog_doc_id`` names
    (``note_write`` stamps the writer's on every chunk); None is a row with no stamp."""
    row = {"ttl_days": ttl, "indexed_at": _ago(100)}
    if written_by is not None:
        row["catalog_doc_id"] = written_by
        row["title"] = f"note-{written_by}"  # _entry's default title: the writer's title travels with its stamp
    return row


def test_a_lapsed_note_is_tombstoned_through_the_note_delete_path_and_its_chunk_goes(monkeypatch):
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2), "doc_id": "c1"})], {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {"c1": _lapsed_row()})

    report = w.sweep()

    assert cat.tombstoned == ["1.1.1"]
    assert cat.manifest_writes == [("1.1.1", [])], "the manifest is retracted before the tombstone"
    assert w.deleted == [["c1"]]
    assert (report.chunks_deleted, report.notes_tombstoned, report.failures) == (1, 1, ())


def test_a_zero_ttl_note_and_a_note_with_no_ttl_never_expire_whatever_the_row_says(monkeypatch):
    """The catalog spells permanent as 0 or absent; the row (the last writer's) says lapsed. Neither
    note is judged by the row, and the catalog's 0 never reaches is_expired (which would log a legacy
    warning for it on every call)."""
    cat = _FakeCatalog([
        _entry("1.1.1", meta={"ttl_days": 0, "put_at": _ago(400), "doc_id": "c1"}),
        _entry("1.1.2", meta={"put_at": _ago(400), "doc_id": "c2"}),
    ], {"1.1.1": ["c1"], "1.1.2": ["c2"]})
    w = _World(monkeypatch, cat, {"c1": _lapsed_row(), "c2": _lapsed_row()})
    judged: list[dict] = []
    real = is_expired
    monkeypatch.setattr(
        "nexus.metadata_schema.is_expired", lambda meta, *, now_iso: judged.append(meta) or real(meta, now_iso=now_iso))

    report = w.sweep()

    assert cat.tombstoned == [] and w.deleted == []
    assert (report.chunks_deleted, report.notes_tombstoned) == (0, 0)
    assert judged and all("put_at" not in m for m in judged), (
        "is_expired reads chunk rows only: a catalog entry's 0 would log its legacy warning per note")


def test_each_owner_is_judged_by_its_own_ttl_and_clock(monkeypatch):
    cat = _FakeCatalog([
        _entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2)}),
        _entry("1.1.2", meta={"ttl_days": 30, "put_at": _ago(2)}),
        _entry("1.1.3", meta={"ttl_days": 1, "put_at": _ago(0.5)}),
    ], {"1.1.1": ["shared"], "1.1.2": ["shared"], "1.1.3": ["other"]})
    w = _World(monkeypatch, cat, {})

    report = w.sweep()

    assert cat.tombstoned == ["1.1.1"]
    assert w.deleted == [["shared"]], "sent to the engine, whose anti-join keeps it while 1.1.2 holds it"
    assert report.notes_tombstoned == 1


def test_a_lapsed_note_is_retired_whatever_its_chunk_row_says(monkeypatch):
    """B (permanent) wrote last, so the row says permanent: the old sweep found no candidate."""
    cat = _FakeCatalog([
        _entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2)}),
        _entry("1.1.2", meta={"ttl_days": 0, "put_at": _ago(1)}),
    ], {"1.1.1": ["c1"], "1.1.2": ["c1"]})
    w = _World(monkeypatch, cat, {})

    w.sweep()

    assert cat.tombstoned == ["1.1.1"] and "1.1.2" not in cat.tombstoned


def test_a_lapse_from_a_heal_refreshed_indexed_at_still_counts_from_the_put(monkeypatch):
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"ttl_days": 7, "put_at": _ago(10)}, indexed_at=_ago(0.001))], {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {})

    w.sweep()

    assert cat.tombstoned == ["1.1.1"]


def test_a_chunk_with_no_live_owner_goes_as_before(monkeypatch):
    cat = _FakeCatalog([], {})
    w = _World(monkeypatch, cat, {"orphan": _lapsed_row()})

    report = w.sweep()

    assert w.deleted == [["orphan"]] and report.chunks_deleted == 1 and report.notes_tombstoned == 0


def test_a_note_with_no_ttl_keys_of_its_own_is_judged_by_the_row_when_it_wrote_the_row_and_owns_it_alone(monkeypatch):
    cat = _FakeCatalog([_entry("1.1.1", meta={"doc_id": "c1"})], {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {"c1": _lapsed_row(written_by="1.1.1")})

    report = w.sweep()

    assert w.deleted == [["c1"]] and w.reaped_chashes == ["c1"]
    assert report.row_ttl_unattributed == 0


def test_a_sole_legacy_owner_is_left_alone_when_the_row_is_not_its_own_write(monkeypatch):
    """Another note wrote the row (and has gone, or never held the chunk now), or nobody stamped it:
    the row's ttl is not this note's, so it is counted and left, not read as the sole owner's."""
    for row in (_lapsed_row(written_by="1.1.2"), _lapsed_row()):
        cat = _FakeCatalog([_entry("1.1.1", meta={"doc_id": "c1"})], {"1.1.1": ["c1"]})
        w = _World(monkeypatch, cat, {"c1": row})

        report = w.sweep()

        assert w.deleted == [] and w.reaped_chashes == [] and cat.tombstoned == []
        assert report.row_ttl_unattributed == 1


def test_a_stale_stamp_from_an_import_that_equals_the_owners_tumbler_does_not_attribute_the_row(monkeypatch):
    """An .nxexp import keeps the SOURCE box's catalog_doc_id on each row while the destination mints its
    own tumblers, so the stamp can equal an unrelated destination note's tumbler. The row carries the
    source writer's title, which is not this note's, so the row is not read as this note's TTL."""
    cat = _FakeCatalog([_entry("1.1.1", meta={"doc_id": "c1"}, title="permanent-here")], {"1.1.1": ["c1"]})
    row = {"ttl_days": 1, "indexed_at": _ago(100), "catalog_doc_id": "1.1.1", "title": "the-source-writer"}
    w = _World(monkeypatch, cat, {"c1": row})

    report = w.sweep()

    assert w.deleted == [] and w.reaped_chashes == [] and cat.tombstoned == []
    assert report.row_ttl_unattributed == 1


def test_a_legacy_permanent_note_survives_the_sweep_that_retires_its_keyed_co_owner_and_the_next(monkeypatch):
    """Legacy A (no keys, permanent: every note put before nexus-6pfgb.5) shares text with keyed B.
    B wrote last, so the row carries B's ttl_days. Sweep 1 retires B and the engine keeps the chunk for A,
    but the row is not rewritten. Sweep 2 sees A as the sole live owner with no keys: it must not read
    B's lapsed row as A's TTL. Two sweeps on one catalog."""
    cat = _FakeCatalog([
        _entry("1.1.1", meta={"doc_id": "c1"}, title="A-legacy-permanent"),
        _entry("1.1.2", meta={"ttl_days": 1, "put_at": _ago(2), "doc_id": "c1"}, title="B-ttl"),
    ], {"1.1.1": ["c1"], "1.1.2": ["c1"]})
    rows = {"c1": _lapsed_row(written_by="1.1.2")}

    first = _World(monkeypatch, cat, rows).sweep()
    assert cat.tombstoned == ["1.1.2"] and first.notes_tombstoned == 1

    second_world = _World(monkeypatch, cat, rows)
    second = second_world.sweep()

    assert cat.tombstoned == ["1.1.2"], "A is still live after the second sweep"
    assert second_world.reaped_chashes == [] and second_world.deleted == []
    assert (second.chunks_deleted, second.notes_tombstoned, second.row_ttl_unattributed) == (0, 0, 1)


def test_a_chunk_two_notes_without_keys_share_is_left_alone(monkeypatch):
    cat = _FakeCatalog([
        _entry("1.1.1", meta={"doc_id": "c1"}), _entry("1.1.2", meta={"doc_id": "c1"}),
    ], {"1.1.1": ["c1"], "1.1.2": ["c1"]})
    w = _World(monkeypatch, cat, {"c1": _lapsed_row(written_by="1.1.1")})

    report = w.sweep()

    assert w.deleted == [] and w.reaped_chashes == [] and cat.tombstoned == []
    assert report.row_ttl_unattributed == 1


def test_a_note_with_keys_is_never_judged_by_the_row(monkeypatch):
    """Its own entry says permanent (or not yet lapsed); the row's lapsed ttl is another note's."""
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"ttl_days": 30, "put_at": _ago(1), "doc_id": "c1"})], {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {"c1": _lapsed_row()})

    report = w.sweep()

    assert w.deleted == [] and cat.tombstoned == [] and report.row_ttl_unattributed == 0


def test_a_file_indexed_document_owning_the_chunk_is_never_judged_by_the_row(monkeypatch):
    cat = _FakeCatalog([_entry("1.1.1", meta={}, content_type="code", file_path="a.py")], {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {"c1": _lapsed_row()})

    report = w.sweep()

    assert w.deleted == [] and report.row_ttl_unattributed == 1


def test_a_note_in_another_collection_is_not_a_lapse_here(monkeypatch):
    cat = _FakeCatalog(
        [_entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(5)}, collection="knowledge__other__bge-base-en-v15-768__v1")],
        {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {})

    w.sweep()

    assert cat.tombstoned == [] and w.deleted == []


def test_a_note_re_put_after_the_listing_is_not_retired_from_the_stale_entry(monkeypatch):
    """The sweep lists the lapsed notes once and retires each later. A re-put between the two (made
    permanent, or its clock restarted) is read again just before the retraction and left alone."""
    class _Racing(_FakeCatalog):
        def list_by_collection(self, collection, *, limit=None):
            listed = super().list_by_collection(collection, limit=limit)
            # the owner re-puts both notes after the sweep has read the listing
            self.docs["1.1.1"] = _entry("1.1.1", meta={"ttl_days": 0, "put_at": _ago(0.01)})
            self.docs["1.1.2"] = _entry("1.1.2", meta={"ttl_days": 1, "put_at": _ago(0.01)})
            return listed

    cat = _Racing([
        _entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2)}),
        _entry("1.1.2", meta={"ttl_days": 1, "put_at": _ago(2)}),
        _entry("1.1.3", meta={"ttl_days": 1, "put_at": _ago(2)}),
    ], {"1.1.1": ["c1"], "1.1.2": ["c2"], "1.1.3": ["c3"]})
    w = _World(monkeypatch, cat, {})

    report = w.sweep()

    assert cat.tombstoned == ["1.1.3"], "only the note still lapsed at its turn is retired"
    assert cat.manifests["1.1.1"] == ["c1"] and cat.manifests["1.1.2"] == ["c2"], "no manifest retracted"
    assert w.deleted == [["c3"]]
    assert (report.notes_tombstoned, report.chunks_deleted, report.failures) == (1, 1, ())


def test_a_note_that_cannot_be_retired_stays_whole_and_is_reported(monkeypatch):
    cat = _FakeCatalog([
        _entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2)}),
        _entry("1.1.2", meta={"ttl_days": 1, "put_at": _ago(2)}),
    ], {"1.1.1": ["c1"], "1.1.2": ["c2"]})
    cat.fail_tombstone.add("1.1.1")
    w = _World(monkeypatch, cat, {})

    report = w.sweep()

    assert cat.tombstoned == ["1.1.2"]
    assert cat.manifests["1.1.1"] == ["c1"], "the failed note's manifest is written back"
    assert w.deleted == [["c2"]], "its chunk is held back"
    assert [t for t, _ in report.failures] == ["1.1.1"]
    assert report.chunks_deleted == 1 and report.notes_tombstoned == 1


def test_expire_raises_when_a_lapsed_note_could_not_be_retired(monkeypatch):
    cat = _FakeCatalog([_entry("1.1.1", meta={"ttl_days": 1, "put_at": _ago(2)})], {"1.1.1": ["c1"]})
    cat.fail_tombstone.add("1.1.1")
    _World(monkeypatch, cat, {})

    with pytest.raises(ExpireIncompleteError) as err:
        HttpVectorClient().expire()

    assert err.value.report.failures and "1.1.1" in str(err.value)


def test_a_catalog_read_failure_propagates_rather_than_reading_as_no_owner(monkeypatch):
    cat = _FakeCatalog([], {})

    def boom(chashes):
        raise RuntimeError("catalog down")

    cat.docs_for_chashes = boom
    w = _World(monkeypatch, cat, {"c1": _lapsed_row()})

    with pytest.raises(RuntimeError, match="catalog down"):
        w.sweep()
    assert w.deleted == []


def test_the_sweep_still_asks_the_wire_only_for_positive_row_ttls(monkeypatch):
    cat = _FakeCatalog([], {})
    w = _World(monkeypatch, cat, {})

    w.sweep()

    assert w.get_wheres == [{"ttl_days": {"$gt": 0}}]
