# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 nexus-6pfgb.45 (the second half of nexus-6pfgb.18): an ``.nxexp`` import stamps ``put_at`` = now
and ``ttl_days`` = 0 (permanent) on a note's catalog entry that holds no note keys, so an imported note
is no longer keyless (``ttl unknown``) and its TTL is not silently dropped.

Fill-only: an entry that holds any note key keeps them all; an indexed (file-backed) document gets none.
The stamp goes through the writer put uses (``update`` with ``meta``), after the page's chains fired and
before the document's completion stamp, so a kill between the two leaves the document ``indexing`` and the
next run of the same file resumes it (the stamp is fill-only, so doing it twice is the same).

The tests drive ``_OwnerImport.flush`` against fakes for the catalog and the import writer; the engine
journey is ``tests/test_z0o2p19_nxexp_import_combined_write.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from nexus.catalog.multi_document_write import PageWriteResult
from nexus.catalog.note_ttl import has_own_keys, note_ttl_days
from nexus.exporter import _OwnerImport, _PageRec

_COLL = "knowledge__art__bge-base-en-v15-768__v1"
_NOTE_URI = f"nx-note://{_COLL}/a-note"


def _entry(tumbler: str, *, source_uri: str = _NOTE_URI, collection: str = _COLL, meta: dict | None = None):
    return SimpleNamespace(
        tumbler=tumbler, file_path="", source_uri=source_uri, physical_collection=collection,
        meta=dict(meta or {}), index_state="indexing", index_content_hash="h")


class _Reader:
    def __init__(self, entries: dict[str, SimpleNamespace]) -> None:
        self.entries = entries

    def docs_for_chashes(self, chashes):
        return {}

    def resolve_many(self, tumblers):
        return {t: self.entries[t] for t in tumblers if t in self.entries}


class _Writer:
    """Catalog writer double: ``update`` merges ``meta`` as the engine does, and logs the call order."""

    def __init__(self, entries, log: list, *, fail: bool = False) -> None:
        self.entries, self.log, self.fail = entries, log, fail

    def update(self, tumbler, **fields):
        self.log.append(("update", str(tumbler), fields))
        if self.fail:
            raise RuntimeError("engine said no")
        self.entries[str(tumbler)].meta.update(fields.get("meta") or {})


class _ImportWriter:
    def __init__(self, log: list) -> None:
        self.log = log

    def claim_position(self, doc, position):
        return position

    def failure(self, doc):
        return None

    def write_page(self, rows_by_doc, chunks):
        return PageWriteResult(written=list(rows_by_doc), landed=list(rows_by_doc))

    def complete_documents(self, docs):
        self.log.append(("complete", list(docs)))
        return PageWriteResult(finished=list(docs))


class _Hooks:
    def fire_store_chains(self, *a, **k):
        self.fired = True


def _flush(entries: dict[str, SimpleNamespace], *, fail: bool = False):
    log: list = []
    imp = _OwnerImport(
        db=None, collection_name=_COLL, hooks=_Hooks(), embedding_model="m", file_hash="h", skip_existing=False)
    imp._reader = _Reader(entries)
    imp._writer = _Writer(entries, log, fail=fail)
    imp._import_writer = _ImportWriter(log)
    page = []
    for i, doc in enumerate(entries):
        imp._state[doc] = "write"
        page.append(_PageRec(
            rec_id=f"{i:064x}", doc="text", meta={}, emb=[0.0],
            group={"doc_id": doc, "key": doc, "source_uri": entries[doc].source_uri}, position=0))
    imp.flush(page)
    return imp, log


def test_a_keyless_imported_note_is_stamped_put_at_now_and_permanent():
    before = datetime.now(UTC)
    entries = {"1.1.5": _entry("1.1.5")}
    assert not has_own_keys(entries["1.1.5"].meta)
    _imp, log = _flush(entries)
    after = datetime.now(UTC)
    updates = [e for e in log if e[0] == "update"]
    assert len(updates) == 1, log
    meta = entries["1.1.5"].meta
    assert meta["ttl_days"] == 0
    stamped = datetime.fromisoformat(meta["put_at"])
    assert before - timedelta(seconds=1) <= stamped <= after + timedelta(seconds=1)
    assert has_own_keys(meta) and note_ttl_days(meta) == 0     # shown as permanent, not "ttl unknown"
    # the stamp comes before the document's completion stamp
    assert [e[0] for e in log] == ["update", "complete"]


def test_a_note_that_already_has_note_keys_keeps_them():
    held = {"put_at": "2026-10-01T00:00:00+00:00", "ttl_days": 7, "tags": ["keep"]}
    entries = {"1.1.5": _entry("1.1.5", meta=held)}
    _imp, log = _flush(entries)
    assert [e for e in log if e[0] == "update"] == []
    assert entries["1.1.5"].meta == held


def test_an_entry_holding_only_ttl_days_is_not_given_a_put_at_clock():
    # fill-only is per entry: a note with a ttl_days and no put_at is a note written between the two
    # changes; a fresh put_at would restart a 7 day clock the note never agreed to
    entries = {"1.1.5": _entry("1.1.5", meta={"ttl_days": 7})}
    _flush(entries)
    assert entries["1.1.5"].meta == {"ttl_days": 7}


def test_an_indexed_document_gets_no_note_keys():
    coll = "docs__art__bge-base-en-v15-768__v1"
    entries = {
        "1.1.6": _entry("1.1.6", source_uri="file:///repo/a.md", collection=_COLL),
        "1.1.7": _entry("1.1.7", source_uri=f"nx-note://{coll}/x", collection=coll),
    }
    _flush(entries)
    assert entries["1.1.6"].meta == {} and entries["1.1.7"].meta == {}


def test_a_note_copied_under_an_nxexp_qualified_uri_is_still_a_note():
    entries = {"1.1.5": _entry("1.1.5", source_uri=f"nxexp://{_COLL}/{_NOTE_URI}")}
    _flush(entries)
    assert entries["1.1.5"].meta["ttl_days"] == 0 and entries["1.1.5"].meta["put_at"]


def test_a_failed_stamp_is_reported_and_the_document_is_left_for_the_next_run():
    entries = {"1.1.5": _entry("1.1.5")}
    imp, log = _flush(entries, fail=True)
    assert ("complete", ["1.1.5"]) not in log
    assert imp.failures and imp.failures[0][0] == "document 1.1.5"
    assert "engine said no" in imp.failures[0][1]
