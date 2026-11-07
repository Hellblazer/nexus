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

import httpx
import pytest

from nexus.catalog.types import ManifestRow
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SPAN_FILL, RUNG_ORDER, default_registry
from nexus.upgrade_ladder.rungs.manifest_section_labels import TITLE_RULE_MARK
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
        self.before_append = None   # called with self, once, between the rung's re-read and the engine's compare

    def doc(self, tumbler: str, *, indexed_at: str = DOC_STAMP, title: str = "", file_path: str = "",
            meta: dict | None = None, collection: str = COLL) -> str:
        self.docs[tumbler] = SimpleNamespace(
            tumbler=tumbler, physical_collection=collection, indexed_at=indexed_at, alias_of="",
            title=title, file_path=file_path, meta=meta or {})
        self.manifests.setdefault(tumbler, [])
        return tumbler

    def row(self, tumbler: str, position: int, chash: str, *, written: bool = False, title: str | None = None,
            stype: str | None = None, page: int | None = None, collection: str = COLL, **span: int | None) -> None:
        cols = {c: span.get(c) for c in COLUMNS}
        self.manifests[tumbler].append(ManifestRow(
            position=position, chash=chash, chunk_index=position, collection=collection,
            section_title=title, section_type=stype, page_number=page, labels_written=written, **cols))

    def chunk(self, chash: str, collection: str = COLL, **meta) -> None:
        meta.setdefault("indexed_at", OLD)
        self.chunks[(collection, chash)] = {"chunk_text_hash": chash, **meta}

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
    def _guard(self, d: str, new: dict, refused: list) -> bool:
        """The engine's per-row ``expected`` compare (nexus-tj0wu): True when the row may be written."""
        expected = new.get("expected")
        if expected is None:
            return True
        old = next((r for r in self.manifests[d] if r.position == new["position"]), None)
        if old is None:
            reason = "position_absent"
        elif old.chash != expected["chash"]:
            reason = "chash_changed"
        elif any(k in expected and getattr(old, k) != expected[k]
                 for k in ("section_title", "section_type", "page_number", "labels_written")):
            reason = "labels_changed"
        elif any(k in expected and getattr(old, k) != expected[k]
                 for k in ("line_start", "line_end", "char_start", "char_end")):
            reason = "span_changed"
        else:
            return True
        refused.append({"doc_id": d, "position": new["position"], "reason": reason})
        return False

    def append_manifest_many(self, docs, *, collection, **kw) -> dict:
        assert not kw, kw
        self.appends.append((collection, [(d, [dict(r) for r in rows]) for d, rows in docs]))
        if self.before_append:
            hook, self.before_append = self.before_append, None
            hook(self)
        failed, refused = [], []
        for d, rows in docs:
            if d in self.fail_doc_ids:
                failed.append(d)
                continue
            for new in rows:
                if not self._guard(d, new, refused):
                    continue
                index = {r.position: i for i, r in enumerate(self.manifests[d])}
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
        return {"ok": True, "docs": len(docs), "failed_doc_ids": failed,
                "expected_refused": refused, "expected_refused_count": len(refused)}

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


def test_a_row_whose_stored_column_differs_from_the_chunk_row_is_not_filled_at_all(world: World) -> None:
    """nexus-l8o33 (review r1, C1). The chunk row says line_start 3, the manifest holds 77: the chunk row describes
    another position, so none of its values may join the row's lines (mixed provenance: lines of one writer, chars of
    another). The row stays as it was, in its own bucket, with its recorded span reported as unused."""
    d = world.doc("1.1.1")
    world.row(d, 0, h(1), line_start=77, line_end=None, char_start=None, char_end=None)
    world.chunk(h(1), **SPAN)
    rung = world.rung()
    assert rung.detect().converged, "nothing here is fillable, so nothing is pending"
    _, result = _fill(world, rung)
    assert world.spans(d) == [(0, 77, None, None, None)]
    assert world.appends == []
    c = rung._partial
    assert (c.span_disagrees, c.fillable, c.filled, c.counted) == (1, 0, 0, 1)
    assert c.reconciles()
    assert result.outcome is ConvergeOutcome.COMPLETED


def test_a_partial_row_that_agrees_with_the_chunk_row_takes_only_its_null_columns(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1), line_start=3, line_end=9, char_start=None, char_end=None)
    world.chunk(h(1), **SPAN)
    rung = world.rung()
    assert rung.detect().pending
    _fill(world, rung)
    assert world.spans(d) == [(0, 3, 9, 40, 190)]
    (_, requests), = world.appends
    sent = requests[0][1][0]
    assert "line_start" not in sent and "line_end" not in sent
    assert (sent["char_start"], sent["char_end"]) == (40, 190)
    assert rung._partial.span_disagrees == 0


def test_a_stored_column_the_chunk_row_does_not_record_is_not_a_disagreement(world: World) -> None:
    """The chunk row records 0 for line_start (not a span): nothing to contradict the stored 77, so the NULL columns fill."""
    d = world.doc("1.1.1")
    world.row(d, 0, h(1), line_start=77, line_end=None, char_start=None, char_end=None)
    world.chunk(h(1), line_start=0, line_end=9, chunk_start_char=40, chunk_end_char=190)
    rung, _ = _fill(world)
    assert world.spans(d) == [(0, 77, 9, 40, 190)]
    assert rung._partial.span_disagrees == 0


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
    status = world.rung(record_fn=lambda: SimpleNamespace(detail=f"0 filled this run; {TITLE_RULE_MARK}")).detect()
    assert status.converged and world.listings == 0


def test_a_record_from_before_the_title_rule_is_pending_once_and_the_walk_rewrites_it(world: World) -> None:
    """nexus-l8o33: a span-fill completion written under the old title rule (develop installs since 7e205178e) is
    scanned once more; the walk records it again with the mark, and the next detect reads it as current."""
    from tests.upgrade.conftest import InMemoryCompletionLedger
    from nexus.upgrade_ladder.registry import LadderRegistry
    from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome

    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    ledger = InMemoryCompletionLedger()
    ledger.record_verified(RUNG_MANIFEST_SPAN_FILL, package_version="7.97.0", detail="0 filled this run; ...")
    rung = world.rung(record_fn=lambda: ledger.completions().get(RUNG_MANIFEST_SPAN_FILL))
    runner = LadderRunner(LadderRegistry((rung,)), ledger, package_version_fn=lambda: "9.9.9")

    assert [r.outcome for r in runner.run().runs] == [RungOutcome.RECORDED]
    assert TITLE_RULE_MARK in ledger.completions()[RUNG_MANIFEST_SPAN_FILL].detail
    assert world.spans(d) == [(0, 3, 9, 40, 190)]
    listings = world.listings
    assert [r.outcome for r in runner.run().runs] == [RungOutcome.ALREADY_RECORDED]
    assert world.listings == listings


def test_a_pre_rule_record_is_not_scanned_unattended_on_a_managed_service(world: World) -> None:
    from nexus.upgrade_ladder.completion import CompletionRecord

    old = CompletionRecord(rung_name=RUNG_MANIFEST_SPAN_FILL, verified_at="t0", package_version="7.97.0")
    from nexus.upgrade_ladder.unattended import unattended_run

    with unattended_run():
        status = world.rung(managed_fn=lambda: True, record_fn=lambda: old).detect()
    assert (status.applicable, status.converged) == (False, False) and world.listings == 0


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


# ── the exactness conditions, each with a test that fails when its condition is removed (review r1) ────────────────────


def _listing(rows: list[dict]):
    """A ``chash_positions_with_tombstoned`` that answers *rows* whatever the chashes, with no deleted holder."""
    return lambda chashes: ([dict(r, collection=COLL, chunk_count=1, labels_written=False) for r in rows], [])


@pytest.mark.parametrize("held,case", [
    ([{"chash": h(1), "doc_id": "9.9.9", "position": 0}], "the one live row the listing names is another document's"),
    ([{"chash": h(1), "doc_id": "1.1.1", "position": 5}], "the one live row the listing names is another position"),
    ([], "the listing names no live holder at all"),
])
def test_a_listing_that_does_not_name_this_row_as_the_one_holder_leaves_it_null(world: World, held, case) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.chash_positions_with_tombstoned = _listing(held)  # type: ignore[method-assign]
    rung, _ = _fill(world)
    assert world.appends == [], case
    c = rung._partial
    assert (c.stale_listing, c.fillable, c.counted) == (1, 0, 1), case
    assert c.reconciles()


def test_a_document_without_indexed_at_cannot_attribute_the_chunk_row(world: World) -> None:
    d = world.doc("1.1.1", indexed_at="")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    rung, _ = _fill(world)
    assert world.appends == [] and rung._partial.chunk_without_indexed_at == 1


def test_a_position_that_changed_chash_before_the_write_is_not_reverted_to_the_scanned_chash(world: World) -> None:
    """The re-read sees another chash at the position (a concurrent re-index). The owners listing is held at the scanned
    state so that only the row's own chash comparison can catch it; the upsert would put the old chash back."""
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    snapshot = world.chash_positions_with_tombstoned([h(1)])

    def reindex(w: World) -> None:
        w.manifests[d][0] = replace(w.manifests[d][0], chash=h(99))
        w.chash_positions_with_tombstoned = lambda chashes: snapshot  # the listing still answers as at scan

    world.before_second_manifest_read = reindex
    rung, _ = _fill(world)
    assert world.appends == [] and rung._partial.raced == 1
    assert world.manifests[d][0].chash == h(99), "the new chash is not overwritten"


def test_a_deleted_document_that_started_holding_the_chash_before_the_write_stops_the_fill(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)

    def tombstone(w: World) -> None:
        w.tombstoned.append({"chash": h(1), "doc_id": "1.1.9", "position": 0, "collection": COLL})

    world.before_second_manifest_read = tombstone
    rung, _ = _fill(world)
    assert world.appends == [] and rung._partial.raced == 1


def test_a_column_filled_meanwhile_with_the_chunk_rows_value_is_not_sent_while_the_others_are(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)

    def fill_one(w: World) -> None:  # another writer wrote the same line_start the chunk row records
        w.manifests[d][0] = replace(w.manifests[d][0], line_start=3)

    world.before_second_manifest_read = fill_one
    rung, _ = _fill(world)
    (_, requests), = world.appends
    sent = requests[0][1][0]
    assert "line_start" not in sent and (sent["line_end"], sent["char_start"], sent["char_end"]) == (9, 40, 190)
    assert world.spans(d) == [(0, 3, 9, 40, 190)]
    assert rung._partial.raced == 0 and rung._partial.filled == 1


def test_a_column_filled_meanwhile_with_another_value_stops_the_fill_of_the_row(world: World) -> None:
    """The write-time twin of span_disagrees (review r2): a late value the chunk row contradicts means the chunk row
    may describe another position, so the other columns are not filled from it."""
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)

    def fill_one(w: World) -> None:  # another writer's value, not the chunk row's
        w.manifests[d][0] = replace(w.manifests[d][0], line_start=77)

    world.before_second_manifest_read = fill_one
    rung, _ = _fill(world)
    assert world.appends == []
    assert world.spans(d) == [(0, 77, None, None, None)]
    assert rung._partial.raced == 1 and rung._partial.filled == 0


# ── a request that raises ────────────────────────────────────────────────────────────────────────────────────────────


def _seven_rows(world: World) -> str:
    d = world.doc("1.1.1")
    for n in range(7):
        world.row(d, n, h(n + 1))
        world.chunk(h(n + 1), **SPAN)
    return d


def test_a_request_that_raises_after_an_earlier_one_wrote_still_restores_the_documents_stamp(world: World) -> None:
    """The engine restamps a document's indexed_at on every manifest write; a later request that raises must not skip
    the restore of the documents an earlier request wrote (review r1, S2)."""
    d = _seven_rows(world)
    world.restamp_on_append = "2026-11-06T00:00:00+00:00"
    real, calls = world.append_manifest_many, []

    def second_raises(request, *, collection, **kw):
        calls.append(request)
        if len(calls) == 2:
            raise RuntimeError("engine unavailable")
        return real(request, collection=collection, **kw)

    world.append_manifest_many = second_raises  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="engine unavailable"):
        _fill(world, world.rung(max_rows=3))
    assert len(calls) == 2
    assert world.docs[d].indexed_at == DOC_STAMP
    assert (d, {"indexed_at": DOC_STAMP}) in world.updates


def test_a_request_that_raises_after_the_engine_committed_it_restores_its_own_documents_stamp(world: World) -> None:
    """A timeout after the engine committed the request: its documents were restamped though no result came back
    (review r2), so they are restored with the ones earlier requests wrote."""
    d = _seven_rows(world)
    world.restamp_on_append = "2026-11-06T00:00:00+00:00"
    real = world.append_manifest_many

    def commits_then_times_out(request, *, collection, **kw):
        real(request, collection=collection, **kw)
        raise httpx.ReadTimeout("no answer")

    world.append_manifest_many = commits_then_times_out  # type: ignore[method-assign]
    with pytest.raises(httpx.ReadTimeout):
        _fill(world, world.rung(max_rows=3))
    assert world.docs[d].indexed_at == DOC_STAMP
    assert (d, {"indexed_at": DOC_STAMP}) in world.updates


def _superseded() -> Exception:
    """What the production client raises for the engine's 409 collection_superseded: the typed error built from the
    body by the same helper ``_raise_for_status`` calls, not a bare status error."""
    from nexus.collection_errors import superseded_write_error_from_body  # noqa: PLC0415

    err = superseded_write_error_from_body(409, {
        "error": "collection_superseded", "reason": "collection_superseded",
        "collection": "code__art__bge-base-en-v15-768__v2", "superseded_by": "code__art__bge-base-en-v15-768__v3",
    })
    assert err is not None
    return err


def _conflict(status: int = 409) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine.invalid/manifest/append_many")
    return httpx.HTTPStatusError("conflict", request=request, response=httpx.Response(status, request=request))


@pytest.mark.parametrize("refusal", [_superseded, _conflict], ids=["typed_superseded", "bare_409"])
def test_a_409_for_a_superseded_collection_defers_that_collection_and_the_run_goes_on(world: World, refusal) -> None:
    OTHER = "code__art__bge-base-en-v15-768__v2"
    a = world.doc("1.1.1", collection=OTHER)
    b = world.doc("1.1.2")
    world.row(a, 0, h(1), collection=OTHER)
    world.row(b, 0, h(2))
    world.chunk(h(1), collection=OTHER, **SPAN)
    world.chunk(h(2), **SPAN)
    real = world.append_manifest_many

    def superseded(request, *, collection, **kw):
        if collection == OTHER:
            raise refusal()
        return real(request, collection=collection, **kw)

    world.append_manifest_many = superseded  # type: ignore[method-assign]
    rung, result = _fill(world)
    assert world.spans(a) == [(0, None, None, None, None)] and world.spans(b) == [(0, 3, 9, 40, 190)]
    c = rung._partial
    assert (c.write_refused_409, c.filled, c.fillable) == (1, 1, 2)
    assert result.outcome is ConvergeOutcome.DEFERRED and "409" in result.detail


def test_a_status_other_than_409_still_aborts_the_run(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)

    def boom(request, *, collection, **kw):
        raise _conflict(500)

    world.append_manifest_many = boom  # type: ignore[method-assign]
    with pytest.raises(httpx.HTTPStatusError):
        _fill(world)


# ── nexus-tj0wu: a write racing the rung is refused by the engine, not reverted ────────────────────────────────────


def test_a_reindex_between_the_reread_and_the_post_is_refused_and_not_reverted(world: World) -> None:
    """The rung re-reads, then a re-index puts another chunk at the position, then the POST. Without `expected` the
    upsert takes the read chash and the read labels from the request and puts them back over the re-index."""
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.before_append = lambda w: w.manifests[d].__setitem__(0, replace(w.manifests[d][0], chash=h(2)))
    rung = world.rung()

    rung.converge(Report())

    assert world.manifests[d][0].chash == h(2), "the re-index stands"
    assert world.spans(d) == [(0, None, None, None, None)], "the old chunk's span is not written onto the new chunk"
    c = rung._partial
    assert (c.raced, c.filled, c.fillable) == (1, 0, 1), c.summary()
    assert rung._filled == 0


def test_a_label_written_between_the_reread_and_the_post_is_not_overwritten(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.before_append = lambda w: w.manifests[d].__setitem__(
        0, replace(w.manifests[d][0], section_title="Mine", section_type="body", page_number=2, labels_written=True))
    rung = world.rung()

    rung.converge(Report())

    assert world.labels(d) == [(0, "Mine", "body", 2, True)], "the concurrent label write stands"
    assert (rung._partial.raced, rung._partial.filled) == (1, 0)


def test_a_span_written_between_the_reread_and_the_post_is_not_overwritten(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.chunk(h(1), **SPAN)
    world.before_append = lambda w: w.manifests[d].__setitem__(0, replace(w.manifests[d][0], line_start=70, line_end=80))
    rung = world.rung()

    rung.converge(Report())

    assert world.spans(d) == [(0, 70, 80, None, None)], "the re-index's own span stands"
    assert (rung._partial.raced, rung._partial.filled) == (1, 0)


def test_the_request_carries_what_was_read_for_the_engine_to_compare(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1), title="T", stype="body", page=3, written=True, line_start=3)
    world.chunk(h(1), **SPAN)

    _fill(world)

    ((_coll, docs),) = world.appends
    assert docs[0][1][0]["expected"] == {
        "chash": h(1), "section_title": "T", "section_type": "body", "page_number": 3, "labels_written": True,
        "line_start": 3, "line_end": None, "char_start": None, "char_end": None}


# ── what the summary accounts for ────────────────────────────────────────────────────────────────────────────────────


def test_the_engine_comparable_count_is_by_recorded_value_and_the_rest_is_reported_apart(world: World) -> None:
    a, b, g = world.doc("1.1.1"), world.doc("1.1.2"), world.doc("1.1.3")
    world.row(a, 0, h(1))                        # shared, chunk records a span: counted, left for ownership
    world.row(b, 0, h(1))
    world.row(g, 0, h(2))                        # shared, chunk records 0s: nothing recorded, so not counted
    world.row(b, 1, h(2))
    world.row(g, 1, h(3))                        # chunk missing: not counted
    world.row(g, 2, h(4))                        # chunk without any span key: not counted
    world.chunk(h(1), **SPAN)
    world.chunk(h(2), line_start=0, line_end=0, chunk_start_char=0, chunk_end_char=0)
    world.chunk(h(4))
    rung, _ = _fill(world)
    c = rung._partial
    assert c.multi_row_cross_document == 2, "the 0-valued shared pair is not in an ownership bucket: the engine skips it"
    assert (c.chunk_missing, c.chunk_without_span_keys, c.chunk_span_unrecorded) == (1, 1, 2)
    assert (c.null_rows, c.counted) == (6, 2) and c.reconciles()
    text = c.summary()
    assert "= 0 fillable" in text and "each with a recorded span unused: " in text
    assert "Not counted by the engine (nothing recorded): 1 chunk missing, 1 chunk with no key" in text
    assert "2 chunk whose keys are 0" in text and "DOES NOT RECONCILE" not in text


def test_a_filled_row_whose_other_column_has_a_zero_key_leaves_the_engine_count_whole(world: World) -> None:
    d = world.doc("1.1.1")
    world.row(d, 0, h(1))
    world.row(d, 1, h(2))
    world.chunk(h(1), line_start=3, line_end=9, chunk_start_char=0, chunk_end_char=0)  # lines recorded, chars 0
    world.chunk(h(2), **SPAN)
    rung, _ = _fill(world)
    c = rung._partial
    assert world.spans(d) == [(0, 3, 9, None, None), (1, 3, 9, 40, 190)]
    assert (c.counted, c.fillable, c.filled) == (2, 2, 2)
    assert c.engine_count_after() == 0 and c.reconciles(), "the 0 chars are not counted by the engine, so zero is reached"
