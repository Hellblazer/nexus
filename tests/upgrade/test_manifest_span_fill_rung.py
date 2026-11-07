# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``manifest-span-fill`` rung (RDR-236, nexus-l8o33), rung level over a fake catalog and T3.

The fake engine's ``append_manifest_many`` follows ``CatalogRepository.insertManifestChunkRows`` (UPSERT_APPEND):
the three labels and ``labels_written`` are rewritten FROM THE REQUEST, and a span column takes a sent value and keeps
the stored one against a sent NULL (nexus-mzpsh). So a test that sends no labels would see them wiped, and the
labels tests below fail if the rung stops sending them. The engine twin is ``test_legacy_rows_walk_substrate.py``.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.catalog.types import ManifestRow
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SPAN_FILL, RUNG_ORDER, default_registry
from nexus.upgrade_ladder.rungs.manifest_span_fill import ManifestSpanFillRung

COLL = "code__art__bge-base-en-v15-768__v1"
OLD = "2026-01-01T00:00:00+00:00"
DOC_STAMP = "2026-02-01T00:00:00+00:00"
FUTURE = "2026-03-01T00:00:00+00:00"
SPAN = {"line_start": 3, "line_end": 9, "chunk_start_char": 40, "chunk_end_char": 190}
COLUMNS = ("line_start", "line_end", "char_start", "char_end")


def h(n: int) -> str:
    return f"{n:064x}"


class Report:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class World:
    """Live documents, their manifests and the T3 chunk rows; the reader, the writer and the T3 stub in one."""

    def __init__(self) -> None:
        self.docs: dict[str, SimpleNamespace] = {}
        self.manifests: dict[str, list[ManifestRow]] = {}
        self.chunks: dict[tuple[str, str], dict] = {}
        self.appends: list[tuple[str, list[tuple[str, list[dict]]]]] = []
        self.tombstoned: list[dict] = []
        self.listings = 0
        self.no_catalog = False
        self.fail_doc_ids: set[str] = set()
        self.before_second_manifest_read = None
        self._manifest_reads = 0
        self.updates: list[tuple[str, dict]] = []
        self.restamp_on_append: str | None = None

    def doc(self, tumbler: str, *, indexed_at: str = DOC_STAMP, title: str = "", file_path: str = "",
            meta: dict | None = None) -> str:
        self.docs[tumbler] = SimpleNamespace(
            tumbler=tumbler, physical_collection=COLL, indexed_at=indexed_at, alias_of="",
            title=title, file_path=file_path, meta=meta or {})
        self.manifests.setdefault(tumbler, [])
        return tumbler

    def row(self, tumbler: str, position: int, chash: str, *, written: bool = False, title: str | None = None,
            stype: str | None = None, page: int | None = None, **span: int | None) -> None:
        cols = {c: span.get(c) for c in COLUMNS}
        self.manifests[tumbler].append(ManifestRow(
            position=position, chash=chash, chunk_index=position, collection=COLL,
            section_title=title, section_type=stype, page_number=page, labels_written=written, **cols))

    def chunk(self, chash: str, **meta) -> None:
        meta.setdefault("indexed_at", OLD)
        self.chunks[(COLL, chash)] = {"chunk_text_hash": chash, **meta}

    def spans(self, tumbler: str) -> list[tuple]:
        return [(r.position, *(getattr(r, c) for c in COLUMNS)) for r in self.manifests[tumbler]]

    def labels(self, tumbler: str) -> list[tuple]:
        return [(r.position, r.section_title, r.section_type, r.page_number, r.labels_written)
                for r in self.manifests[tumbler]]

    # reader ---------------------------------------------------------------
    def all_documents(self) -> list[SimpleNamespace]:
        self.listings += 1
        return [SimpleNamespace(**vars(d)) for d in self.docs.values()]

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[ManifestRow]]:
        self._manifest_reads += 1
        if self._manifest_reads == 2 and self.before_second_manifest_read:
            hook, self.before_second_manifest_read = self.before_second_manifest_read, None
            hook(self)
        return {d: list(self.manifests[d]) for d in doc_ids if self.manifests.get(d)}

    def chash_positions_with_tombstoned(self, chashes: list[str]):
        wanted = set(chashes)
        rows = [
            {"chash": r.chash, "doc_id": d, "position": r.position, "chunk_count": len(rs),
             "collection": r.collection, "labels_written": r.labels_written}
            for d, rs in sorted(self.manifests.items()) for r in rs if r.chash in wanted
        ]
        return rows, [t for t in self.tombstoned if t["chash"] in wanted]

    # writer ---------------------------------------------------------------
    def append_manifest_many(self, docs, *, collection, **kw) -> dict:
        assert not kw, kw
        self.appends.append((collection, [(d, [dict(r) for r in rows]) for d, rows in docs]))
        failed = []
        for d, rows in docs:
            if d in self.fail_doc_ids:
                failed.append(d)
                continue
            index = {r.position: i for i, r in enumerate(self.manifests[d])}
            for new in rows:
                i = index[new["position"]]
                old = self.manifests[d][i]
                same_chunk = old.chash == new["chash"] and old.collection == collection
                spans = {}
                for c in COLUMNS:
                    sent = new.get(c)
                    spans[c] = (sent if sent is not None else getattr(old, c)) if same_chunk else sent
                self.manifests[d][i] = replace(
                    old, chash=new["chash"], collection=collection, **spans,
                    section_title=new.get("section_title"), section_type=new.get("section_type"),
                    page_number=new.get("page_number"),
                    labels_written=bool(new["labels_written"]) if isinstance(new.get("labels_written"), bool)
                    else any(k in new for k in ("section_title", "section_type", "page_number")))
            if self.restamp_on_append is not None:
                self.docs[d].indexed_at = self.restamp_on_append
        return {"ok": True, "docs": len(docs), "failed_doc_ids": failed}

    def update(self, tumbler: str, **fields) -> None:
        self.updates.append((tumbler, dict(fields)))
        self.docs[tumbler].indexed_at = fields["indexed_at"]

    def update_many(self, updates: list[dict]) -> list[int]:
        for u in updates:
            self.updates.append((u["tumbler"], {k: v for k, v in u.items() if k != "tumbler"}))
            self.docs[u["tumbler"]].indexed_at = u["indexed_at"]
        return [1] * len(updates)

    # t3 -------------------------------------------------------------------
    def get_collection(self, name: str):
        world = self

        class _Col:
            def get(self, ids=None, include=None, **kw):
                found = [c for c in ids if (name, c) in world.chunks]
                return {"ids": found, "metadatas": [world.chunks[(name, c)] for c in found]}

        return _Col()

    def close(self) -> None:
        pass

    def rung(self, **kw) -> ManifestSpanFillRung:
        kw.setdefault("record_fn", lambda: None)
        kw.setdefault("managed_fn", lambda: False)
        return ManifestSpanFillRung(
            reader_factory=lambda: None if self.no_catalog else self,
            writer_factory=lambda: self, t3_factory=lambda: self, **kw)


@pytest.fixture
def world() -> World:
    return World()


def _fill(world: World, rung: ManifestSpanFillRung | None = None):
    rung = rung or world.rung()
    result = rung.converge(Report())
    return rung, result


# ── shape ────────────────────────────────────────────────────────────────────


def test_the_rung_is_registered_after_its_backfill_and_conforms() -> None:
    assert RUNG_MANIFEST_SPAN_FILL == "manifest-span-fill"
    assert RUNG_MANIFEST_SPAN_FILL in RUNG_ORDER
    assert RUNG_MANIFEST_SPAN_FILL in [r.name for r in default_registry()]
    assert isinstance(ManifestSpanFillRung(), Rung)


# ── the rule ─────────────────────────────────────────────────────────────────


def test_a_single_row_chash_takes_the_chunk_rows_span_on_every_null_column(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    rung, result = _fill(world)
    assert result.outcome is ConvergeOutcome.COMPLETED, result.detail
    assert world.spans(d) == [(0, 3, 9, 40, 190)]
    assert rung.verify() is True


def test_only_the_null_columns_are_sent_and_a_stored_column_is_never_overwritten(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1), line_start=77, line_end=None, char_start=None, char_end=None)
    world.chunk(h(1), **SPAN)  # the chunk row says line_start 3; the manifest holds 77
    rung = world.rung()
    assert rung.detect().pending
    _fill(world, rung)
    assert world.spans(d) == [(0, 77, 9, 40, 190)], "the stored column stays; the NULL ones are filled"
    (_, requests), = world.appends
    sent = requests[0][1][0]
    assert "line_start" not in sent and sent["line_end"] == 9
    assert rung._partial.span_disagrees == 1


def test_a_row_with_every_column_set_is_never_read_against_the_chunk_or_written(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1), line_start=1, line_end=2, char_start=3, char_end=4)
    world.chunk(h(1), **SPAN)
    rung = world.rung()
    assert rung.detect().converged
    _fill(world, rung)
    assert world.appends == []


@pytest.mark.parametrize("written", [False, True])
def test_the_rows_labels_survive_the_write_unchanged(world: World, written: bool) -> None:
    """The append rewrites the labels from the request: the rung must send the row's own back."""
    d = world.doc("1.1.1")
    if written:
        world.row(d, 0, h(1), written=True, title="Intro", stype="body", page=4)
    else:
        world.row(d, 0, h(1))
    world.chunk(h(1), section_title="CHUNK-LABEL", section_type="CHUNK-TYPE", page_number=99, **SPAN)
    _fill(world)
    expect = (0, "Intro", "body", 4, True) if written else (0, None, None, None, False)
    assert world.labels(d) == [expect]
    assert world.spans(d) == [(0, 3, 9, 40, 190)]


def test_an_empty_written_label_is_sent_back_empty(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1), written=True, title="", stype="", page=None)
    world.chunk(h(1), **SPAN)
    _fill(world)
    assert world.labels(d) == [(0, "", "", None, True)]


def test_a_chash_shared_with_another_document_or_repeated_in_one_stays_null(world: World) -> None:
    a, b = world.doc("1.1.1"), world.doc("1.1.2")
    world.row(a, 0, h(1))
    world.row(b, 0, h(1))
    world.row(a, 1, h(2))
    world.row(a, 2, h(2))
    world.chunk(h(1), **SPAN)
    world.chunk(h(2), **SPAN)
    rung, _ = _fill(world)
    assert world.appends == []
    c = rung._partial
    assert (c.multi_row_cross_document, c.multi_row_in_document, c.fillable) == (2, 2, 0)


def test_a_chash_also_held_by_a_tombstoned_document_stays_null(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.tombstoned.append({"chash": h(1), "doc_id": "1.1.9", "position": 0, "collection": COLL})
    rung, _ = _fill(world)
    assert world.appends == [] and rung._partial.tombstoned_co_owner == 1
    assert rung.detect().converged, "a row left NULL by design is not pending"


@pytest.mark.parametrize("mode", ["dt_link", "synthetic", "synthetic_chash", "csv_curated"])
def test_a_document_minted_by_the_orphan_backfill_is_left_null(world: World, mode: str) -> None:
    d = world.doc("1.1.1", meta={"backfill_from": "t3_orphan", "mode": mode})
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    rung, _ = _fill(world)
    assert world.appends == [] and rung._partial.orphan_minted == 1


@pytest.mark.parametrize("meta,bucket", [
    ({}, "chunk_without_span_keys"),
    ({"line_start": 0, "line_end": 0, "chunk_start_char": 0, "chunk_end_char": 0}, "chunk_span_unrecorded"),
    ({"line_start": "3", "line_end": None}, "chunk_span_unrecorded"),
    ({**SPAN, "indexed_at": FUTURE}, "chunk_newer_than_document"),
    ({**SPAN, "indexed_at": ""}, "chunk_without_indexed_at"),
    ({**SPAN, "title": "other/file.py:1-5"}, "chunk_titled_for_another"),
])
def test_a_chunk_row_that_cannot_give_a_span_leaves_the_row_null_and_is_counted(world: World, meta, bucket) -> None:
    d = world.doc("1.1.1", file_path="src/a.py", title="a.py")
    world.row(d, 0, h(1))
    world.chunk(h(1), **meta)
    rung, _ = _fill(world)
    assert world.appends == []
    counts = rung._partial
    assert getattr(counts, bucket) == 1 and counts.fillable == 0, counts.summary()


def test_a_chunk_titled_for_this_document_with_a_line_suffix_is_copied(world: World) -> None:
    d = world.doc("1.1.1", file_path="src/a.py", title="a.py")
    world.row(d, 0, h(1))
    world.chunk(h(1), title="src/a.py:3-9", **SPAN)
    _fill(world)
    assert world.spans(d) == [(0, 3, 9, 40, 190)]


def test_a_missing_chunk_is_counted(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    rung, _ = _fill(world)
    assert rung._partial.chunk_missing == 1 and world.appends == []


def test_a_char_start_of_zero_is_a_span_only_beside_a_recorded_end(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.row(d, 1, h(2))
    world.chunk(h(1), line_start=0, line_end=0, chunk_start_char=0, chunk_end_char=120)
    world.chunk(h(2), line_start=5, line_end=6, chunk_start_char=0, chunk_end_char=0)
    _fill(world)
    assert world.spans(d) == [(0, None, None, 0, 120), (1, 5, 6, None, None)]


# ── lifecycle ────────────────────────────────────────────────────────────────


def _mixed(world: World) -> None:
    a, b = world.doc("1.1.1"), world.doc("1.1.2")
    world.row(a, 0, h(1))              # exact
    world.row(a, 1, h(2))              # shared with b
    world.row(b, 0, h(2))
    world.row(b, 1, h(3))              # exact
    for n in (1, 2, 3):
        world.chunk(h(n), **SPAN)


def test_a_dry_run_reports_the_partition_a_real_run_touches_and_writes_nothing(world: World) -> None:
    _mixed(world)
    rung = world.rung()
    status = rung.detect()
    assert status.pending and "2 manifest row(s) of 4" in status.pending_detail, status.pending_detail
    assert world.appends == [] and world.updates == []
    _, result = _fill(world, rung)
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert rung._partial.filled == 2 and rung._partial.fillable == 2


def test_a_second_run_changes_nothing(world: World) -> None:
    _mixed(world)
    _fill(world)
    before = [dict(world.manifests), len(world.appends)]
    rung = world.rung()
    assert rung.detect().converged
    _, result = _fill(world, rung)
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert len(world.appends) == before[1], "no second write"
    assert rung._partial.filled == 0 and rung._partial.fillable == 0


def test_the_documents_indexed_at_is_restored_after_the_write(world: World) -> None:
    d = world.doc("1.1.1", indexed_at=DOC_STAMP)
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.restamp_on_append = "2026-11-06T00:00:00+00:00"
    _fill(world)
    assert world.docs[d].indexed_at == DOC_STAMP
    assert world.updates == [(d, {"indexed_at": DOC_STAMP})]


def test_a_failed_restore_defers_and_names_the_document(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)

    def refuse(updates):
        return [0] * len(updates)

    world.update_many = refuse  # type: ignore[method-assign]

    def boom(tumbler, **fields):
        raise RuntimeError("engine refused the update")

    world.update = boom  # type: ignore[method-assign]
    _, result = _fill(world)
    assert result.outcome is ConvergeOutcome.DEFERRED and d in result.detail


def test_a_write_the_engine_refuses_defers_and_the_row_stays_fillable(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.fail_doc_ids.add(d)
    rung, result = _fill(world)
    assert result.outcome is ConvergeOutcome.DEFERRED and "refused the write for 1 row" in result.detail
    assert world.spans(d) == [(0, None, None, None, None)]
    assert rung.verify() is False and rung.detect().pending


def test_a_row_filled_between_the_scan_and_the_write_is_skipped(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)

    def fill_it(w: World) -> None:
        w.manifests[d][0] = replace(w.manifests[d][0], line_start=1, line_end=2, char_start=3, char_end=4)

    world.before_second_manifest_read = fill_it
    rung, _ = _fill(world)
    assert world.appends == [] and rung._partial.raced == 1
    assert world.spans(d) == [(0, 1, 2, 3, 4)]


def test_labels_written_between_the_scan_and_the_write_are_not_reverted(world: World) -> None:
    """The label rung ran in the window: the span write carries the labels as they are by then, not as scanned."""
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)

    def label_it(w: World) -> None:
        w.manifests[d][0] = replace(
            w.manifests[d][0], section_title="Late", section_type="body", page_number=2, labels_written=True)

    world.before_second_manifest_read = label_it
    _fill(world)
    assert world.labels(d) == [(0, "Late", "body", 2, True)]
    assert world.spans(d) == [(0, 3, 9, 40, 190)]


def test_a_chash_that_gained_a_second_row_before_the_write_is_skipped(world: World) -> None:
    a, b = world.doc("1.1.1"), world.doc("1.1.2")
    world.row(a, 0, h(1))
    world.chunk(h(1), **SPAN)

    def add_holder(w: World) -> None:
        w.row(b, 0, h(1))

    world.before_second_manifest_read = add_holder
    rung, _ = _fill(world)
    assert world.appends == [] and rung._partial.raced == 1


def test_writes_are_bounded_by_rows_and_documents(world: World) -> None:
    d = world.doc("1.1.1")
    for n in range(7):
        world.row(d, n, h(n + 1))
        world.chunk(h(n + 1), **SPAN)
    _fill(world, world.rung(max_rows=3))
    assert [len(rows) for _c, req in world.appends for _d, rows in req] == [3, 3, 1]


# ── detect / converge / verify gates ─────────────────────────────────────────


def test_a_recorded_completion_is_converged_with_no_catalog_read(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    status = world.rung(record_fn=lambda: object()).detect()
    assert status.converged and world.listings == 0


def test_an_unreadable_ledger_is_not_a_record_and_the_scan_runs(world: World) -> None:
    def broken():
        raise RuntimeError("ledger down")

    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    assert world.rung(record_fn=broken).detect().pending


def test_unattended_on_a_managed_service_reads_nothing(world: World) -> None:
    from nexus.upgrade_ladder.unattended import unattended_run  # noqa: PLC0415

    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    rung = world.rung(managed_fn=lambda: True)
    with unattended_run():
        status = rung.detect()
        result = rung.converge(Report())
        assert rung.verify() is False and rung.verify_deferred()
    assert not status.applicable and result.outcome is ConvergeOutcome.DEFERRED
    assert world.listings == 0 and world.appends == []


def test_interactive_on_a_managed_service_keeps_the_scan(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    rung = world.rung(managed_fn=lambda: True)
    assert rung.detect().pending
    _, result = _fill(world, rung)
    assert result.outcome is ConvergeOutcome.COMPLETED


def test_no_catalog_is_not_applicable_and_does_not_verify(world: World) -> None:
    world.no_catalog = True
    rung = world.rung()
    assert not rung.detect().applicable
    assert rung.converge(Report()).outcome is ConvergeOutcome.DEFERRED
    assert rung.verify() is False


def test_an_engine_that_cannot_list_tombstoned_holders_is_not_applicable_and_writes_nothing(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.chash_positions_with_tombstoned = lambda chashes: ([], None)  # type: ignore[method-assign]
    rung = world.rung()
    assert not rung.detect().applicable
    assert rung.converge(Report()).outcome is ConvergeOutcome.DEFERRED
    assert world.appends == []


def test_an_engine_without_the_route_is_not_applicable(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chash_positions_with_tombstoned = lambda chashes: None  # type: ignore[method-assign]
    assert not world.rung().detect().applicable


def test_an_empty_catalog_is_converged_and_says_it_examined_nothing(world: World) -> None:
    rung = world.rung()
    assert rung.detect().converged
    assert rung.verify() is True
    assert "0 document(s), 0 row(s)" in rung.record_detail()


def test_the_auto_time_limit_defers_between_batches_and_the_rest_resumes(world: World, monkeypatch) -> None:
    for i in range(3):
        d = world.doc(f"1.1.{i + 1}")
        world.row(d, 0, h(i + 1))
        world.chunk(h(i + 1), **SPAN)
    # the first batch always runs; the limit has passed by the time the second is due
    monkeypatch.setattr("nexus.upgrade_ladder.rungs.manifest_span_fill.active_expired", lambda: bool(world.appends))
    rung = world.rung(doc_batch=1)
    result = rung.converge(Report())
    assert result.outcome is ConvergeOutcome.DEFERRED and "time limit" in result.detail
    assert [world.spans(f"1.1.{i}")[0][1] for i in (1, 2, 3)] == [3, None, None]
    monkeypatch.setattr("nexus.upgrade_ladder.rungs.manifest_span_fill.active_expired", lambda: False)
    again = world.rung(doc_batch=1)
    assert again.converge(Report()).outcome is ConvergeOutcome.COMPLETED
    assert [world.spans(f"1.1.{i}")[0][1] for i in (1, 2, 3)] == [3, 3, 3]
