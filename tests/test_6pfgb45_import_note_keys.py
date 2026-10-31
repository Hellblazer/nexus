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
    def __init__(self, entries: dict[str, SimpleNamespace], *, fail_reads: int = 0) -> None:
        self.entries = entries
        self.resolve_calls = 0
        self.fail_reads = fail_reads

    def docs_for_chashes(self, chashes):
        return {}

    def resolve_many(self, tumblers):
        self.resolve_calls += 1
        if self.resolve_calls <= self.fail_reads:
            raise RuntimeError("catalog read down")
        return {t: self.entries[t] for t in tumblers if t in self.entries}


class _Writer:
    """Catalog writer double: ``update`` merges ``meta`` as the engine does, and logs the call order."""

    def __init__(self, entries, log: list, *, fail: bool = False, zero: frozenset = frozenset()) -> None:
        self.entries, self.log, self.fail, self.zero = entries, log, fail, zero
        self.many_calls: list[list[dict]] = []

    def update_many(self, updates):
        """The service writer's batch op: one count per entry, aligned (0 = nothing updated)."""
        self.many_calls.append(list(updates))
        if self.fail:
            raise RuntimeError("engine said no")
        counts = []
        for u in updates:
            if u["tumbler"] in self.zero:
                counts.append(0)
                continue
            self.log.append(("update", u["tumbler"], {"meta": u["meta"]}))
            self.entries[u["tumbler"]].meta.update(u["meta"])
            counts.append(1)
        return counts

    def update(self, tumbler, **fields):
        self.log.append(("update", str(tumbler), fields))
        if self.fail:
            raise RuntimeError("engine said no")
        self.entries[str(tumbler)].meta.update(fields.get("meta") or {})


class _LegacyWriter(_Writer):
    """A writer without the batch op (the SQLite/daemon-mode writer): per-document ``update``."""

    update_many = None


class _ImportWriter:
    rows_landed = 0
    sweep_skipped = 0

    def __init__(self, log: list) -> None:
        self.log = log
        self.landed: list[str] = []
        self.completed: list[str] = []

    def finish(self):
        """What the real writer says at the end: a landed document never completed is still awaiting."""
        from nexus.catalog.multi_document_write import FinishResult  # noqa: PLC0415

        return FinishResult(
            completed=list(self.completed),
            failed={d: "its last page landed and its completion stamp was never sent"
                    for d in self.landed if d not in self.completed})

    def claim_position(self, doc, position):
        return position

    def failure(self, doc):
        return None

    def write_page(self, rows_by_doc, chunks):
        self.landed.extend(rows_by_doc)
        return PageWriteResult(written=list(rows_by_doc), landed=list(rows_by_doc))

    def complete_documents(self, docs):
        self.log.append(("complete", list(docs)))
        self.completed.extend(docs)
        return PageWriteResult(finished=list(docs))


class _Hooks:
    def fire_store_chains(self, *a, **k):
        self.fired = True


def _flush(
    entries: dict[str, SimpleNamespace], *, fail: bool = False, collection: str = _COLL,
    fail_reads: int = 0, zero: frozenset = frozenset(), writer_cls=_Writer,
):
    log: list = []
    imp = _OwnerImport(
        db=None, collection_name=collection, hooks=_Hooks(), embedding_model="m", file_hash="h", skip_existing=False)
    imp._reader = _Reader(entries, fail_reads=fail_reads)
    imp._writer = writer_cls(entries, log, fail=fail, zero=zero)
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


# --- review round 1 (nexus-6pfgb.45) -----------------------------------------------------------


def test_a_file_routed_collection_makes_no_catalog_read_and_no_write():
    # code__, docs__ and rdr__ hold no notes: the page's documents complete with the read never made
    coll = "docs__art__bge-base-en-v15-768__v1"
    entries = {"1.1.6": _entry("1.1.6", source_uri="file:///repo/a.md", collection=coll)}
    imp, log = _flush(entries, collection=coll)
    assert imp._reader.resolve_calls == 0
    assert imp._writer.many_calls == [] and [e for e in log if e[0] == "update"] == []
    assert ("complete", ["1.1.6"]) in log and imp.failures == []


def test_a_read_that_fails_once_is_retried_and_the_note_is_stamped():
    entries = {"1.1.5": _entry("1.1.5")}
    imp, log = _flush(entries, fail_reads=1)
    assert imp._reader.resolve_calls == 2
    assert entries["1.1.5"].meta["ttl_days"] == 0
    assert ("complete", ["1.1.5"]) in log and imp.failures == []


def test_a_read_that_stays_down_holds_back_only_the_documents_that_could_be_notes():
    # a file-backed document of a knowledge collection cannot be a note: it completes; the note does not
    entries = {
        "1.1.5": _entry("1.1.5"),
        "1.1.6": _entry("1.1.6", source_uri="file:///repo/a.md"),
    }
    imp, log = _flush(entries, fail_reads=2)
    assert ("complete", ["1.1.6"]) in log
    assert all("1.1.5" not in e[1] for e in log if e[0] == "complete")
    assert [label for label, _ in imp.failures] == ["document 1.1.5"]
    assert "could not be read" in imp.failures[0][1]


def test_a_document_whose_stamp_failed_is_reported_once_not_again_by_finish():
    entries = {"1.1.5": _entry("1.1.5")}
    imp, _log = _flush(entries, fail=True)
    assert len(imp.failures) == 1
    imp.finish()
    assert [label for label, _ in imp.failures] == ["document 1.1.5"]


def test_the_pages_note_keys_go_in_one_batched_write():
    entries = {f"1.1.{i}": _entry(f"1.1.{i}") for i in range(5, 9)}
    imp, log = _flush(entries)
    assert len(imp._writer.many_calls) == 1
    assert [u["tumbler"] for u in imp._writer.many_calls[0]] == list(entries)
    assert all(e.meta["ttl_days"] == 0 for e in entries.values())
    assert ("complete", list(entries)) in log


def test_a_zero_count_for_one_entry_leaves_that_document_indexing_and_completes_the_rest():
    entries = {"1.1.5": _entry("1.1.5"), "1.1.6": _entry("1.1.6")}
    imp, log = _flush(entries, zero=frozenset({"1.1.5"}))
    assert ("complete", ["1.1.6"]) in log
    assert all("1.1.5" not in e[1] for e in log if e[0] == "complete")
    assert [label for label, _ in imp.failures] == ["document 1.1.5"]
    imp.finish()
    assert len(imp.failures) == 1


def test_a_writer_without_update_many_stamps_one_document_at_a_time():
    entries = {"1.1.5": _entry("1.1.5"), "1.1.6": _entry("1.1.6")}
    imp, log = _flush(entries, writer_cls=_LegacyWriter)
    assert len([e for e in log if e[0] == "update"]) == 2
    assert entries["1.1.5"].meta["ttl_days"] == 0 and entries["1.1.6"].meta["ttl_days"] == 0
