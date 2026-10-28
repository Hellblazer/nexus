# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.15 against the real engine: ``nx store expire`` judges each owning note by its own
catalog TTL and its own clock.

Two notes with identical text share one chunk row (RDR-108); the row's ``ttl_days`` and
``indexed_at`` are the last writer's. Before this change, if the permanent note wrote last the
other never expired, and if the TTL note wrote last the sweep saw two owners, resolved neither and
tombstoned nothing. Now each lapsed owner is tombstoned on its own ``meta.ttl_days`` counted from
its own ``meta.put_at``, and the chunk goes only when no live owner holds it (the engine's delete
anti-join).

A lapse needs time, so each test backdates the clock the code under test reads: the note's
``put_at`` in its catalog entry, and (for the cases that compare against the old behaviour) the
chunk row's own ``indexed_at``. The storage-free twin for the rules is
``tests/test_6pfgb15_note_ttl.py``.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog import note_ttl
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient, _post
from nexus.mcp.core import store_put
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_title
from tests._chunk_seed import seed_chunks_direct

pytestmark = pytest.mark.integration


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _chash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


class _World:
    """One knowledge subject on the real engine, with helpers to put, age and inspect notes."""

    def __init__(self, tenant: str, subject: str) -> None:
        self.client = HttpVectorClient(tenant=tenant)
        self.subject = subject
        self.collection: str | None = None

    def put(self, title: str, body: str, *, ttl: str | None = None) -> None:
        kwargs = {"ttl": ttl} if ttl else {}
        with patch("nexus.mcp.core._get_t3", return_value=self.client):
            store_put(content=body, collection=self.subject, title=title, **kwargs)
        self.collection = t3_collection_name(self.subject, t3=self.client)

    def _entry(self, title: str):
        reader = make_catalog_reader()
        assert reader is not None
        return reader.by_source_uri(uri_for(self.collection, title))

    def age_note(self, title: str, days: int) -> None:
        """Move the note's own clock back: its ``put_at`` in the catalog entry."""
        entry = self._entry(title)
        assert entry is not None, f"{title!r} was never registered"
        aged = _days_ago(days)
        writer = make_catalog_writer()
        writer.update(entry.tumbler, meta={"put_at": aged})
        # The note's own chunk rows were stamped by the same put: age them with it, or the sweep's
        # guard (nexus-6pfgb.30) rightly reads a row newer than the catalog's clock as a later put.
        reader = make_catalog_reader()
        assert reader is not None
        for manifest_row in reader.get_manifest(str(entry.tumbler)):
            row = self.client.get_by_id(self.collection, manifest_row.chash)
            if row is not None and row.get("title") == title:
                seed_chunks_direct(
                    self.collection, ids=[manifest_row.chash], documents=[row["content"]],
                    metadatas=[{"indexed_at": aged}], embed=True)

    def age_row(self, body: str, *, ttl_days: int | None, days: int, title: str | None = None) -> None:
        """Set the shared chunk row's own ``ttl_days`` and ``indexed_at`` (and its ``title``, when given):
        what the old sweep read."""
        meta: dict = {"ttl_days": ttl_days, "indexed_at": _days_ago(days)}
        if title is not None:
            meta["title"] = title
        seed_chunks_direct(
            self.collection, ids=[_chash(body)], documents=[body], metadatas=[meta], embed=True)

    def live(self, title: str) -> bool:
        return self._entry(title) is not None

    def chunk_present(self, body: str) -> bool:
        return _chash(body) in self.client.existing_ids(self.collection, [_chash(body)])

    def chunk_served(self, body: str) -> bool:
        """The chunk is visible to a read, which the engine allows only while a live document owns it."""
        return self.client.get_by_id(self.collection, _chash(body)) is not None


def test_a_ttl_note_expires_when_a_permanent_note_with_the_same_text_wrote_last(t2_service_env):
    """Bead pin (a). The row says permanent (B wrote last), so the old sweep found no candidate."""
    w = _World(t2_service_env, "6pfgb15-a")
    body = "6pfgb15 (a) identical text under a ttl note and a permanent note"
    w.put("a-ttl", body, ttl="1d")
    w.put("a-permanent", body)
    w.age_note("a-ttl", 2)
    assert w.live("a-ttl") and w.live("a-permanent"), "control: both live before the sweep"

    w.client.expire()

    assert not w.live("a-ttl"), "the lapsed note must be tombstoned although the shared row says permanent"
    assert w.live("a-permanent")
    assert w.chunk_served(body), "the chunk stays served for the permanent co-owner"


def test_a_ttl_note_expires_when_it_wrote_last_and_a_permanent_note_keeps_the_chunk(t2_service_env):
    """Bead pin (b). The row carries A's lapsed ttl, the sweep saw two owners and tombstoned nothing."""
    w = _World(t2_service_env, "6pfgb15-b")
    body = "6pfgb15 (b) identical text, the ttl note put after the permanent one"
    w.put("b-permanent", body)
    w.put("b-ttl", body, ttl="1d")
    w.age_note("b-ttl", 2)
    w.age_row(body, ttl_days=1, days=2)
    assert w.live("b-ttl") and w.live("b-permanent"), "control: both live before the sweep"

    w.client.expire()

    assert not w.live("b-ttl"), "the lapsed owner must be tombstoned although a co-owner holds the chunk"
    assert w.live("b-permanent")
    assert w.chunk_present(body) and w.chunk_served(body), "the permanent co-owner keeps the chunk served"


def test_a_note_whose_ttl_was_retracted_survives_a_stale_lapsed_row(t2_service_env):
    """A re-put without a TTL leaves ``ttl_days`` 0 in the catalog (the retraction value). A reader
    that took 0 for a TTL, or that kept trusting the row, would tombstone a permanent note."""
    w = _World(t2_service_env, "6pfgb15-zero")
    first = "6pfgb15 zero first body, put with a ttl"
    second = "6pfgb15 zero second body, put again without one"
    w.put("zero", first, ttl="1d")
    w.put("zero", second)
    entry = w._entry("zero")
    assert entry is not None and entry.meta.get("ttl_days") == 0, "control: the retraction value is in place"
    w.age_note("zero", 400)
    w.age_row(second, ttl_days=1, days=400)

    w.client.expire()

    assert w.live("zero"), "a 0 entry is permanent: it survives expire"
    assert w.chunk_served(second)


def test_each_owner_expires_on_its_own_ttl_and_clock(t2_service_env):
    w = _World(t2_service_env, "6pfgb15-own")
    body = "6pfgb15 own-clock text shared by a short and a long ttl note"
    w.put("own-short", body, ttl="1d")
    w.put("own-long", body, ttl="30d")
    w.age_note("own-short", 2)
    w.age_note("own-long", 2)

    w.client.expire()

    assert not w.live("own-short")
    assert w.live("own-long"), "30 days have not passed on its own clock"
    assert w.chunk_served(body)

    w.age_note("own-long", 31)
    w.client.expire()

    assert not w.live("own-long")
    assert not w.chunk_present(body), "no live owner is left: the chunk goes"


def test_a_note_put_before_the_catalog_held_its_ttl_still_expires_when_it_owns_the_chunk_alone(t2_service_env):
    """The legacy rule: no catalog TTL keys, one live owner, and a row that note wrote itself (its
    ``catalog_doc_id``, which the engine returns on /vectors/get), so the row's TTL is that note's."""
    w = _World(t2_service_env, "6pfgb15-legacy")
    w.put("legacy-seed", "6pfgb15 legacy seed, only to register the collection")
    body = "6pfgb15 legacy note text with a lapsed ttl on the chunk row only"
    cat = ActiveCatalog()
    owner = cat.register_owner("6pfgb15-legacy-owner", "curator")
    tumbler = cat.register(
        owner, "legacy-single", content_type="knowledge", physical_collection=w.collection,
        meta={"doc_id": _chash(body)})
    seed_chunks_direct(
        w.collection, ids=[_chash(body)], documents=[body], embed=True,
        metadatas=[{"ttl_days": 1, "indexed_at": _days_ago(5), "chunk_text_hash": _chash(body),
                    "catalog_doc_id": str(tumbler), "title": "legacy-single"}])
    cat.append_manifest_chunks(str(tumbler), [{"chash": _chash(body), "position": 0}], collection=w.collection)
    cat.resync_chunk_count_cache(str(tumbler))

    w.client.expire()

    assert not w.chunk_present(body)
    assert w.live("legacy-seed"), "an unrelated permanent note is untouched"


def test_a_chunk_shared_by_two_legacy_notes_is_left_alone(t2_service_env):
    """Neither owner has its own TTL on record and the row (written by the second) is whichever wrote
    last: nothing is tombstoned and the chunk stays. This pins the end state against the real engine,
    where the delete anti-join and the manifest reap would also refuse a shared chunk, so it does not
    isolate the classification; ``test_a_chunk_two_notes_without_keys_share_is_left_alone`` in
    ``tests/test_6pfgb15_note_ttl.py`` does."""
    w = _World(t2_service_env, "6pfgb15-legacy2")
    w.put("legacy2-seed", "6pfgb15 legacy2 seed, only to register the collection")
    body = "6pfgb15 legacy shared text, row ttl lapsed, two owners with no catalog ttl"
    cat = ActiveCatalog()
    owner = cat.register_owner("6pfgb15-legacy2-owner", "curator")
    seed_chunks_direct(
        w.collection, ids=[_chash(body)], documents=[body],
        metadatas=[{"ttl_days": 1, "indexed_at": _days_ago(5), "chunk_text_hash": _chash(body)}], embed=True)
    for title in ("legacy2-one", "legacy2-two"):
        tumbler = cat.register(
            owner, title, content_type="knowledge", physical_collection=w.collection,
            meta={"doc_id": _chash(body)})
        seed_chunks_direct(
            w.collection, ids=[_chash(body)], documents=[body], embed=True,
            metadatas=[{"catalog_doc_id": str(tumbler), "title": title}])  # the last writer's stamp ends up on the row
        cat.append_manifest_chunks(str(tumbler), [{"chash": _chash(body), "position": 0}], collection=w.collection)
        cat.resync_chunk_count_cache(str(tumbler))

    w.client.expire()

    assert len(documents_by_title("legacy2-one")) == 1 and len(documents_by_title("legacy2-two")) == 1
    assert w.chunk_present(body)


def test_a_legacy_permanent_note_survives_the_sweep_that_retires_its_ttl_co_owner_and_the_next(t2_service_env):
    """A note put before the catalog held TTLs (no keys, permanent) shares text with a TTL note put
    later. The row carries the TTL note's ttl_days and its ``catalog_doc_id``. Sweep one retires the TTL
    note and the engine keeps the chunk for the legacy note, but the row is not rewritten. Sweep two sees
    the legacy note as the chunk's sole owner with no keys of its own: it must not read the departed note's
    lapsed row as that note's TTL, which would tombstone a permanent note and delete its chunk."""
    w = _World(t2_service_env, "6pfgb15-twosweep")
    w.put("twosweep-seed", "6pfgb15 twosweep seed, only to register the collection")
    body = "6pfgb15 twosweep text shared by a legacy permanent note and a later ttl note"
    cat = ActiveCatalog()
    owner = cat.register_owner("6pfgb15-twosweep-owner", "curator")
    legacy = cat.register(
        owner, "twosweep-legacy", content_type="knowledge", physical_collection=w.collection,
        source_uri=uri_for(w.collection, "twosweep-legacy"), meta={"doc_id": _chash(body)})
    seed_chunks_direct(
        w.collection, ids=[_chash(body)], documents=[body], embed=True,
        metadatas=[{"indexed_at": _days_ago(500), "chunk_text_hash": _chash(body), "catalog_doc_id": str(legacy)}])
    cat.append_manifest_chunks(str(legacy), [{"chash": _chash(body), "position": 0}], collection=w.collection)
    cat.resync_chunk_count_cache(str(legacy))
    w.put("twosweep-ttl", body, ttl="1d")
    w.age_note("twosweep-ttl", 2)
    w.age_row(body, ttl_days=1, days=2)
    assert w.live("twosweep-ttl") and len(documents_by_title("twosweep-legacy")) == 1, "control: both live"

    w.client.expire()

    assert not w.live("twosweep-ttl"), "sweep one retires the lapsed note"
    assert len(documents_by_title("twosweep-legacy")) == 1 and w.chunk_served(body), "the permanent one is kept"
    row = _post("/v1/vectors/get", {"collection": w.collection, "ids": [_chash(body)], "include": ["metadatas"],
                                    "include_non_live": True}, tenant=w.client._tenant)["metadatas"][0]
    assert row["ttl_days"] == 1, "control: the row still carries the departed note's lapsed ttl"

    w.client.expire()

    assert len(documents_by_title("twosweep-legacy")) == 1, "sweep two must not tombstone the permanent note"
    assert w.chunk_present(body) and w.chunk_served(body)


def test_the_engine_returns_the_writers_catalog_doc_id_on_vectors_get(t2_service_env):
    """The sweep's attribution (``note_ttl.row_written_by``) reads ``catalog_doc_id`` off the metadata
    ``/v1/vectors/get`` returns. If the engine stopped returning it, every legacy row would read as
    unattributed and nothing would expire: this pins the read the sweep depends on."""
    w = _World(t2_service_env, "6pfgb15-stamp")
    body = "6pfgb15 stamp text, put through the note path"
    w.put("stamp-note", body)
    entry = w._entry("stamp-note")
    assert entry is not None
    row = _post("/v1/vectors/get", {"collection": w.collection, "ids": [_chash(body)], "include": ["metadatas"],
                                    "include_non_live": True}, tenant=w.client._tenant)["metadatas"][0]
    assert note_ttl.row_written_by(entry.tumbler, row, owner_title="stamp-note")


def test_a_stale_stamp_that_equals_a_legacy_notes_tumbler_does_not_make_the_row_its_own(t2_service_env):
    """An .nxexp import keeps the source box's ``catalog_doc_id`` and ``title`` on a row while the
    destination mints its own tumblers. Here the row's stamp equals the destination note's tumbler by
    chance and its title is the source writer's: the legacy note is permanent here and must survive."""
    w = _World(t2_service_env, "6pfgb15-stale")
    w.put("stale-seed", "6pfgb15 stale seed, only to register the collection")
    body = "6pfgb15 stale-stamp text, row ttl lapsed, written by a note of another box"
    cat = ActiveCatalog()
    owner = cat.register_owner("6pfgb15-stale-owner", "curator")
    tumbler = cat.register(
        owner, "stale-legacy", content_type="knowledge", physical_collection=w.collection,
        source_uri=uri_for(w.collection, "stale-legacy"), meta={"doc_id": _chash(body)})
    seed_chunks_direct(
        w.collection, ids=[_chash(body)], documents=[body], embed=True,
        metadatas=[{"ttl_days": 1, "indexed_at": _days_ago(5), "chunk_text_hash": _chash(body),
                    "catalog_doc_id": str(tumbler), "title": "the-note-of-the-source-box"}])
    cat.append_manifest_chunks(str(tumbler), [{"chash": _chash(body), "position": 0}], collection=w.collection)
    cat.resync_chunk_count_cache(str(tumbler))

    w.client.expire()

    assert len(documents_by_title("stale-legacy")) == 1 and w.chunk_served(body)


def test_a_note_re_put_permanent_by_a_writer_that_bypasses_the_catalog_hook_is_not_retired(t2_service_env):
    """nexus-6pfgb.30. An older client's re-put rewrites the chunk row (permanent, a fresh indexed_at)
    and leaves the catalog's ttl_days and put_at as they were, so the catalog says lapsed. The sweep must
    read the note's own row and leave the note alone, then retire it once the row agrees with the catalog."""
    w = _World(t2_service_env, "6pfgb30-stale")
    body = "6pfgb30 text re-put permanent by an older client"
    w.put("stale-ttl", body, ttl="1d")
    w.age_note("stale-ttl", 2)
    w.age_row(body, ttl_days=None, days=0)  # the older client's write: no TTL, indexed now, stamp and title kept
    entry = w._entry("stale-ttl")
    assert entry is not None and entry.meta.get("ttl_days") == 1, "control: the catalog still says 1 day, lapsed"
    rows = w.client._note_rows(w.collection, [_chash(body)])
    assert note_ttl.row_written_by(entry.tumbler, rows[_chash(body)], owner_title="stale-ttl"), (
        "control: the engine's store-get returns the stamp and title the guard attributes the row by")
    assert not rows[_chash(body)].get("ttl_days")

    report = w.client.expire_report()

    assert w.live("stale-ttl") and w.chunk_served(body), "a note its owner made permanent is not retired"
    assert [t for _, t in report.catalog_ttl_stale] == ["stale-ttl"]
    assert (report.notes_tombstoned, report.chunks_deleted, report.failures) == (0, 0, ())

    # remedy: a re-put with this client rewrites both the row and the catalog keys; it is permanent now
    w.put("stale-ttl", body)
    w.age_note("stale-ttl", 400)
    assert w.client.expire_report().notes_tombstoned == 0 and w.live("stale-ttl")


@pytest.mark.parametrize("older_client_title", ["", "some-other-title"], ids=["untitled-put", "put-under-another-title"])
def test_a_note_an_older_client_re_put_permanent_under_no_title_or_another_title_is_not_retired(
    t2_service_env, older_client_title,
):
    """nexus-6pfgb.30 round 2. An older client's untitled put, or put under another title, of the note's text
    stamps the chunk row with the note's own tumbler and the put's title (here "" or another), permanent,
    indexed now. The catalog still says 1 day, lapsed. The row names the note by its stamp, so the sweep
    holds it; before, only a row carrying the note's title counted and the note was retired."""
    w = _World(t2_service_env, f"6pfgb30-title-{len(older_client_title)}")
    body = f"6pfgb30 text an older client re-put permanent under title {older_client_title!r}"
    w.put("named-ttl", body, ttl="1d")
    w.age_note("named-ttl", 2)
    w.age_row(body, ttl_days=None, days=0, title=older_client_title)
    entry = w._entry("named-ttl")
    row = w.client._note_rows(w.collection, [_chash(body)])[_chash(body)]
    assert entry is not None and row.get("catalog_doc_id") == str(entry.tumbler) and row.get("title") == older_client_title
    assert not note_ttl.row_written_by(entry.tumbler, row, owner_title="named-ttl"), "control: the strict rule does not see it"

    report = w.client.expire_report()

    assert w.live("named-ttl") and w.chunk_served(body), "a note its owner made permanent is not retired"
    assert [t for _, t in report.catalog_ttl_stale] == ["named-ttl"]
    assert (report.notes_tombstoned, report.chunks_deleted, report.failures) == (0, 0, ())


def test_a_genuinely_lapsed_note_is_retired_with_its_own_rows_read(t2_service_env):
    w = _World(t2_service_env, "6pfgb30-lapsed")
    body = "6pfgb30 text of a note that simply lapsed"
    w.put("really-lapsed", body, ttl="1d")
    w.age_note("really-lapsed", 2)

    report = w.client.expire_report()

    assert not w.live("really-lapsed") and not w.chunk_present(body)
    assert (report.notes_tombstoned, report.catalog_ttl_stale, report.failures) == (1, (), ())


def test_a_lapsed_note_whose_rows_cannot_be_read_is_not_retired(t2_service_env):
    w = _World(t2_service_env, "6pfgb30-unread")
    body = "6pfgb30 text of a lapsed note whose rows cannot be read"
    w.put("unread", body, ttl="1d")
    w.age_note("unread", 2)

    with patch.object(HttpVectorClient, "_note_rows", side_effect=RuntimeError("engine unreachable")):
        report = w.client.expire_report()

    assert w.live("unread") and w.chunk_served(body)
    assert [t for t, _ in report.failures] and report.notes_tombstoned == 0
