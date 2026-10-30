# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1nrcu (RDR-236): the ``manifest-section-labels`` rung copies a chunk row's section label onto
the legacy manifest rows it is exact for.

Storage-free: a catalog fake that answers the engine's reads and applies ``append_manifest_many`` as
the engine's ``UPSERT_APPEND`` does (labels, ``labels_written`` and the chash change; nothing else) stands
in for the reader and the writer, injected through the rung's factories. The same fill against the real
engine is a step of ``tests/upgrade/test_rdr192_manifest_backfill_substrate.py``.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nexus.catalog.types import ManifestRow
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SECTION_LABELS, RUNG_ORDER, default_registry
from nexus.upgrade_ladder.rungs.manifest_section_labels import ManifestSectionLabelsRung

COLL = "docs__art__bge-base-en-v15-768__v1"
OTHER = "docs__other__bge-base-en-v15-768__v1"
OLD = "2026-01-01T00:00:00+00:00"
DOC_STAMP = "2026-02-01T00:00:00+00:00"
FUTURE = "2026-03-01T00:00:00+00:00"


def h(n: int) -> str:
    return f"{n:064x}"


class Report:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class World:
    """Live documents, their manifests, and the T3 chunk rows; the reader, the writer and the T3 stub."""

    def __init__(self) -> None:
        self.docs: dict[str, SimpleNamespace] = {}
        self.manifests: dict[str, list[ManifestRow]] = {}
        self.chunks: dict[tuple[str, str], dict] = {}
        self.appends: list[tuple[str, list[tuple[str, list[dict]]]]] = []
        self.chunk_reads: list[tuple[str, int]] = []
        self.no_catalog = False
        self.route = True
        self.fail_doc_ids: set[str] = set()
        self.before_second_manifest_read = None  # called with self, once, before the re-read
        self._manifest_reads = 0

    def doc(self, tumbler: str, *, collection: str = COLL, indexed_at: str = DOC_STAMP) -> str:
        self.docs[tumbler] = SimpleNamespace(
            tumbler=tumbler, physical_collection=collection, indexed_at=indexed_at, alias_of="")
        self.manifests.setdefault(tumbler, [])
        return tumbler

    def row(self, tumbler: str, position: int, chash: str, *, written: bool = False,
            title: str | None = None, stype: str | None = None, collection: str = COLL) -> None:
        self.manifests[tumbler].append(ManifestRow(
            position=position, chash=chash, chunk_index=position, line_start=position * 10, line_end=position * 10 + 9,
            collection=collection, section_title=title, section_type=stype, labels_written=written))

    def chunk(self, chash: str, collection: str = COLL, **meta) -> None:
        self.chunks[(collection, chash)] = {"chunk_text_hash": chash, **meta}

    # reader ---------------------------------------------------------------
    def all_documents(self) -> list[SimpleNamespace]:
        return list(self.docs.values())

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[ManifestRow]]:
        self._manifest_reads += 1
        if self._manifest_reads == 2 and self.before_second_manifest_read:
            hook, self.before_second_manifest_read = self.before_second_manifest_read, None
            hook(self)
        return {d: list(self.manifests[d]) for d in doc_ids if self.manifests.get(d)}

    def chash_positions(self, chashes: list[str]) -> list[dict] | None:
        if not self.route:
            return None
        wanted = set(chashes)
        return [
            {"chash": r.chash, "doc_id": d, "position": r.position, "chunk_count": len(rows),
             "collection": r.collection, "labels_written": r.labels_written}
            for d, rows in sorted(self.manifests.items()) for r in rows if r.chash in wanted
        ]

    # writer ---------------------------------------------------------------
    def append_manifest_many(self, docs, *, collection, **kw) -> dict:
        assert not kw, kw
        self.appends.append((collection, [(d, [dict(r) for r in rows]) for d, rows in docs]))
        failed = []
        for d, rows in docs:
            if d in self.fail_doc_ids:
                failed.append(d)
                continue
            rows_by_pos = {r.position: i for i, r in enumerate(self.manifests[d])}
            for new in rows:
                i = rows_by_pos[new["position"]]
                self.manifests[d][i] = replace(
                    self.manifests[d][i], chash=new["chash"], section_title=new["section_title"],
                    section_type=new["section_type"], page_number=new["page_number"],
                    labels_written=new["labels_written"])
        return {"ok": True, "docs": len(docs), "failed_doc_ids": failed}

    # t3 -------------------------------------------------------------------
    def get_collection(self, name: str):
        world = self

        class _Col:
            def get(self, ids=None, include=None, **kw):
                world.chunk_reads.append((name, len(ids)))
                found = [c for c in ids if (name, c) in world.chunks]
                return {"ids": found, "metadatas": [world.chunks[(name, c)] for c in found]}

        return _Col()

    def close(self) -> None:
        pass

    def rung(self, **kw) -> ManifestSectionLabelsRung:
        return ManifestSectionLabelsRung(
            reader_factory=lambda: None if self.no_catalog else self,
            writer_factory=lambda: self, t3_factory=lambda: self, **kw)

    def labels(self, tumbler: str) -> list[tuple]:
        return [(r.position, r.section_title, r.section_type, r.page_number, r.labels_written)
                for r in self.manifests[tumbler]]


@pytest.fixture
def world() -> World:
    return World()


def _labelled(world: World, chash: str, **meta) -> None:
    world.chunk(chash, section_title="Refs", section_type="references", page_number=9, indexed_at=OLD, **meta)


# ── the rung's shape ─────────────────────────────────────────────────────────


def test_the_rung_is_registered_and_conforms() -> None:
    assert RUNG_MANIFEST_SECTION_LABELS == "manifest-section-labels"
    assert RUNG_MANIFEST_SECTION_LABELS in RUNG_ORDER
    assert RUNG_MANIFEST_SECTION_LABELS in [r.name for r in default_registry()]
    assert isinstance(ManifestSectionLabelsRung(), Rung)


# ── the rule ─────────────────────────────────────────────────────────────────


def test_a_single_row_chash_takes_the_chunk_rows_label_and_is_marked_written(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    rung = world.rung()

    status = rung.detect()
    assert (status.applicable, status.converged) == (True, False)
    assert "1 legacy manifest row" in status.pending_detail
    assert world.appends == [], "detect is read-only"

    result = rung.converge(Report())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert world.labels("1.1.1") == [(0, "Refs", "references", 9, True)]
    ((collection, docs),) = world.appends
    assert collection == COLL
    assert docs == [("1.1.1", [{
        "chash": h(1), "position": 0, "section_title": "Refs", "section_type": "references",
        "page_number": 9, "labels_written": True}])], "chash, position, the three labels and the flag; nothing else"
    assert rung.verify() is True
    assert "1 filled this run" in rung.record_detail()


def test_a_chash_at_two_positions_of_one_document_is_not_filled(world: World) -> None:
    """One owner, two rows: the chunk row holds one label, so it would be stamped on both positions."""
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    world.row("1.1.1", 5, h(1))
    _labelled(world, h(1))

    result = world.rung().converge(Report())

    assert world.appends == [] and world.labels("1.1.1") == [(0, None, None, None, False), (5, None, None, None, False)]
    assert "2 repeated inside the document" in result.detail


def test_a_chash_shared_with_another_document_is_not_filled(world: World) -> None:
    for t in ("1.1.1", "1.1.2"):
        world.doc(t)
        world.row(t, 0, h(1))
    _labelled(world, h(1))

    result = world.rung().converge(Report())

    assert world.appends == []
    assert "2 shared with another document" in result.detail


def test_a_row_already_written_is_untouched_even_when_its_labels_are_empty(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1), written=True, title="", stype="")
    world.row("1.1.1", 1, h(2), written=True, title="Mine", stype="body")
    _labelled(world, h(1))
    _labelled(world, h(2))
    rung = world.rung()

    assert rung.detect().converged is True
    assert rung.converge(Report()).outcome is ConvergeOutcome.COMPLETED
    assert world.appends == []
    assert world.labels("1.1.1") == [(0, "", "", None, True), (1, "Mine", "body", None, True)]


def test_a_chunk_row_with_none_of_the_label_keys_leaves_the_row_legacy(world: World) -> None:
    """A writer that predates labels omitted them: copying "" would say "unlabelled for good"."""
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    world.chunk(h(1), indexed_at=OLD)  # no section_* keys at all
    rung = world.rung()

    result = rung.converge(Report())

    assert world.appends == []
    assert "1 chunk with no label keys" in result.detail
    assert rung.verify() is True, "nothing fillable: the row stays legacy, which is not pending"


def test_a_chunk_row_with_empty_label_keys_is_copied_as_written_empty(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    world.chunk(h(1), section_title="", section_type="", indexed_at=OLD)

    world.rung().converge(Report())

    assert world.labels("1.1.1") == [(0, "", "", None, True)]


@pytest.mark.parametrize("meta,bucket", [
    ({"indexed_at": FUTURE}, "chunk row not attributable"),
    ({}, "chunk row not attributable"),
])
def test_a_chunk_row_newer_than_the_document_or_undated_is_not_copied(world: World, meta: dict, bucket: str) -> None:
    """A later chunk row was written by someone else (a departed co-owner is invisible to the live
    ownership read); an undated one cannot be attributed."""
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    world.chunk(h(1), section_type="references", **meta)

    result = world.rung().converge(Report())

    assert world.appends == [] and bucket in result.detail


def test_a_chunk_that_is_missing_is_counted_and_the_row_stays_legacy(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))

    result = world.rung().converge(Report())

    assert world.appends == [] and "1 chunk missing" in result.detail


def test_the_chunk_is_read_in_the_rows_own_collection(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1), collection=OTHER)
    world.chunk(h(1), COLL, section_type="wrong-collection", indexed_at=OLD)
    world.chunk(h(1), OTHER, section_type="methods", indexed_at=OLD)

    world.rung().converge(Report())

    assert world.labels("1.1.1") == [(0, "", "methods", None, True)]
    ((collection, _docs),) = world.appends
    assert collection == OTHER


# ── convergence, partition and idempotence ───────────────────────────────────


def _mixed(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))                     # fillable
    world.row("1.1.1", 1, h(2))                     # shared
    world.row("1.1.1", 2, h(3), written=True, title="", stype="")  # written
    world.row("1.1.1", 3, h(4))                     # no label keys
    world.doc("1.1.2")
    world.row("1.1.2", 0, h(2))
    world.row("1.1.2", 1, h(5))                     # fillable
    for n in (1, 2, 3, 5):
        _labelled(world, h(n))
    world.chunk(h(4), indexed_at=OLD)


def test_a_dry_run_reports_the_partition_a_real_run_touches_and_writes_nothing(world: World) -> None:
    _mixed(world)
    rung = world.rung()

    dry = rung.detect()
    assert dry.pending_detail.startswith("2 legacy manifest row(s) of 5")
    assert world.appends == []

    result = rung.converge(Report())
    assert result.detail.startswith("2 document(s), 6 row(s): 1 already written, 5 legacy (2 fillable, 2 filled")
    assert sum(len(rows) for _c, docs in world.appends for _d, rows in docs) == 2


def test_a_second_run_changes_nothing(world: World) -> None:
    _mixed(world)
    first = world.rung()
    first.converge(Report())
    before = {t: world.labels(t) for t in world.docs}
    writes = len(world.appends)

    again = world.rung()
    assert again.detect().converged is True
    result = again.converge(Report())

    assert result.outcome is ConvergeOutcome.COMPLETED
    assert len(world.appends) == writes, "a second run sends no write"
    assert {t: world.labels(t) for t in world.docs} == before
    assert again.verify() is True


def test_an_empty_catalog_is_converged_and_says_it_examined_nothing(world: World) -> None:
    rung = world.rung()

    assert rung.detect().converged is True
    assert rung.converge(Report()).detail.startswith("0 document(s), 0 row(s)")
    assert rung.verify() is True and "0 document(s)" in rung.record_detail()


# ── races and failures ───────────────────────────────────────────────────────


def test_a_row_that_became_written_before_the_write_is_skipped_and_counted(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))

    def reindex(w: World) -> None:  # a re-index writes the document's own labels between scan and write
        w.manifests["1.1.1"][0] = replace(
            w.manifests["1.1.1"][0], section_title="Own", section_type="body", labels_written=True)

    world.before_second_manifest_read = reindex

    result = world.rung().converge(Report())

    assert world.appends == []
    assert world.labels("1.1.1") == [(0, "Own", "body", None, True)], "the fresh labels survive"
    assert "1 changed meanwhile" in result.detail


def test_a_chash_that_gained_a_second_row_before_the_write_is_skipped(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    world.doc("1.1.2")
    _labelled(world, h(1))
    world.before_second_manifest_read = lambda w: w.row("1.1.2", 0, h(1), written=True, title="", stype="")

    result = world.rung().converge(Report())

    assert world.appends == []
    assert "1 changed meanwhile" in result.detail


def test_a_write_the_engine_refuses_defers_and_the_row_stays_fillable(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    world.fail_doc_ids = {"1.1.1"}
    rung = world.rung()

    result = rung.converge(Report())

    assert result.outcome is ConvergeOutcome.DEFERRED and "1 row(s)" in result.detail
    assert rung.verify() is False and "1 legacy manifest row" in rung.verify_detail()


def test_an_engine_without_the_route_is_not_applicable(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    world.route = False

    status = world.rung().detect()

    assert status.applicable is False and "chash_positions" in status.pending_detail
    assert world.rung().converge(Report()).outcome is ConvergeOutcome.DEFERRED


def test_no_catalog_is_not_applicable_and_does_not_verify(world: World) -> None:
    world.no_catalog = True
    rung = world.rung()

    assert rung.detect().applicable is False
    assert rung.verify() is False and "no catalog" in rung.verify_detail()


# ── batching bound ───────────────────────────────────────────────────────────


def test_writes_are_one_request_per_collection_per_batch_of_documents(world: World) -> None:
    for i in range(1, 8):
        world.doc(f"1.1.{i}")
        world.row(f"1.1.{i}", 0, h(i))
        _labelled(world, h(i))

    world.rung(doc_batch=3).converge(Report())

    assert [len(docs) for _c, docs in world.appends] == [3, 3, 1], "ceil(7 / 3) requests, not 7"
    assert all(r.labels_written for rows in world.manifests.values() for r in rows)
    assert max(n for _c, n in world.chunk_reads) <= 3, "chunk reads follow the batch"


def test_the_auto_time_limit_defers_between_batches_and_the_rest_resumes(world: World, monkeypatch) -> None:
    for i in range(1, 7):
        world.doc(f"1.1.{i}")
        world.row(f"1.1.{i}", 0, h(i))
        _labelled(world, h(i))
    calls = iter([False, True])  # the limit passes before the third batch
    monkeypatch.setattr(
        "nexus.upgrade_ladder.rungs.manifest_section_labels.active_expired", lambda: next(calls, True))
    rung = world.rung(doc_batch=2)

    first = rung.converge(Report())
    assert first.outcome is ConvergeOutcome.DEFERRED and "time limit" in first.detail
    done = [t for t in world.docs if world.labels(t)[0][-1]]
    assert done == ["1.1.1", "1.1.2", "1.1.3", "1.1.4"], "two batches written, the third not started"

    monkeypatch.setattr("nexus.upgrade_ladder.rungs.manifest_section_labels.active_expired", lambda: False)
    assert rung.converge(Report()).outcome is ConvergeOutcome.COMPLETED
    assert all(world.labels(t)[0][-1] for t in world.docs)
