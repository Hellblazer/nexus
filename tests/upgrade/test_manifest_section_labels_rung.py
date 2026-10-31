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
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from tests.upgrade.conftest import InMemoryCompletionLedger
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


class _PositionProbe:
    """A manifest row that counts how often its position is read."""

    def __init__(self, row: ManifestRow, world: "World") -> None:
        self._row, self._world = row, world

    @property
    def position(self) -> int:
        self._world.position_reads += 1
        return self._row.position

    def __getattr__(self, name: str):
        return getattr(self._row, name)


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
        self.restamp_on_append: str | None = None  # the engine stamps the document's indexed_at on every manifest write
        self.fail_update_ids: set[str] = set()
        self.updates: list[tuple[str, dict]] = []
        self.probe_positions = False
        self.listings = 0
        self.position_reads = 0
        self.before_second_manifest_read = None  # called with self, once, before the re-read
        self._manifest_reads = 0
        self.tombstoned: list[dict] = []  # manifest rows of deleted documents (nexus-6pfgb.58)
        self.tombstoned_reported = True  # False: an engine before include_tombstoned, which sends no list
        self.tombstoned_reads: list[list[str]] = []

    def doc(self, tumbler: str, *, collection: str = COLL, indexed_at: str = DOC_STAMP,
            title: str = "", file_path: str = "", meta: dict | None = None) -> str:
        self.docs[tumbler] = SimpleNamespace(
            tumbler=tumbler, physical_collection=collection, indexed_at=indexed_at, alias_of="",
            title=title, file_path=file_path, meta=meta or {})
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
        self.listings += 1
        return [SimpleNamespace(**vars(d)) for d in self.docs.values()]  # fresh entries, as the HTTP reader returns

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[ManifestRow]]:
        self._manifest_reads += 1
        if self._manifest_reads == 2 and self.before_second_manifest_read:
            hook, self.before_second_manifest_read = self.before_second_manifest_read, None
            hook(self)
        if self.probe_positions:
            return {d: [_PositionProbe(r, self) for r in self.manifests[d]] for d in doc_ids if self.manifests.get(d)}
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

    def deleted(self, tumbler: str, position: int, chash: str, *, collection: str = COLL) -> None:
        """A tombstoned document holding *chash*: invisible to chash_positions, named by the tombstoned read."""
        self.tombstoned.append({"chash": chash, "doc_id": tumbler, "position": position, "collection": collection})

    def chash_positions_with_tombstoned(self, chashes: list[str]):
        rows = self.chash_positions(chashes)  # a test may replace chash_positions; this follows it
        if rows is None:
            return None
        self.tombstoned_reads.append(list(chashes))
        gone = [t for t in self.tombstoned if t["chash"] in set(chashes)] if self.tombstoned_reported else None
        return rows, gone

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
            if self.restamp_on_append is not None:
                self.docs[d].indexed_at = self.restamp_on_append
        return {"ok": True, "docs": len(docs), "failed_doc_ids": failed}

    def update(self, tumbler: str, **fields) -> None:
        self.updates.append((tumbler, dict(fields)))
        if tumbler in self.fail_update_ids:
            raise RuntimeError("engine refused the update")
        assert set(fields) == {"indexed_at"}, fields
        self.docs[tumbler].indexed_at = fields["indexed_at"]

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
        kw.setdefault("record_fn", lambda: None)  # no completion on file: the default would ask the engine's ledger
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


RUN_TIME = "2026-10-30T12:00:00+00:00"
WITHIN_SKEW = "2026-02-01T00:02:00+00:00"  # two minutes after DOC_STAMP: inside the five-minute guard


def _one_fillable_and_one_newer_row(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))                      # fillable
    world.row("1.1.1", 1, h(2))                      # its chunk was last written by a departed co-owner
    _labelled(world, h(1))
    world.chunk(h(2), section_title="Departed", section_type="references", page_number=3, indexed_at=FUTURE)


def test_the_documents_indexed_at_is_restored_after_the_write_so_a_second_pass_changes_nothing(world: World) -> None:
    """nexus-1nrcu (code review): the engine restamps a document's ``indexed_at`` on every manifest write.
    Left alone, the chunk-newer guard (+5 min over the DOCUMENT's stamp) is off for the document's other
    rows on the next pass, and a departed co-owner's label is copied."""
    _one_fillable_and_one_newer_row(world)
    world.restamp_on_append = RUN_TIME

    first = world.rung()
    assert first.converge(Report()).outcome is ConvergeOutcome.COMPLETED
    assert world.labels("1.1.1") == [(0, "Refs", "references", 9, True), (1, None, None, None, False)]
    assert world.updates == [("1.1.1", {"indexed_at": DOC_STAMP})], "the stamp read at scan, written back"
    assert world.docs["1.1.1"].indexed_at == DOC_STAMP
    writes = len(world.appends)

    second = world.rung()
    assert second.detect().converged is True
    assert second.converge(Report()).outcome is ConvergeOutcome.COMPLETED
    assert len(world.appends) == writes, "the second pass sends no write"
    assert world.labels("1.1.1")[1] == (1, None, None, None, False), "the departed co-owner's label is never copied"
    assert second.verify() is True


def test_a_document_that_was_not_written_is_not_restamped(world: World) -> None:
    _one_fillable_and_one_newer_row(world)
    world.fail_doc_ids = {"1.1.1"}

    world.rung().converge(Report())

    assert world.updates == [], "the engine rolled the document back: its stamp is untouched"


def test_a_failed_restore_defers_and_names_the_document(world: World) -> None:
    _one_fillable_and_one_newer_row(world)
    world.restamp_on_append = RUN_TIME
    world.fail_update_ids = {"1.1.1"}
    rung = world.rung()

    result = rung.converge(Report())

    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "1.1.1" in result.detail and "indexed_at" in result.detail


# ── documents the orphan backfill minted ─────────────────────────────────────


@pytest.mark.parametrize("mode", ["dt_link", "synthetic", "synthetic_chash", "csv_curated"])
def test_a_document_minted_by_the_orphan_backfill_is_left_legacy(world: World, mode: str) -> None:
    """nexus-jz86a, decision (a): an orphan row stays legacy because the chunk row's label is a FORMER
    owner's, not the row's own answer. The document is registered at backfill time, so its ``indexed_at`` is
    later than any chunk it holds and the date guard always passes for it: only the marker keeps the rung
    from copying exactly that label."""
    world.doc("1.1.1", meta={"backfill_from": "t3_orphan", "backfill_mode": mode})
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    world.doc("1.1.2", meta={"tags": "x"})   # control: any other document with the same shape is filled
    world.row("1.1.2", 0, h(2))
    _labelled(world, h(2))
    rung = world.rung()

    result = rung.converge(Report())

    assert world.labels("1.1.1") == [(0, None, None, None, False)]
    assert world.labels("1.1.2") == [(0, "Refs", "references", 9, True)]
    assert "1 in documents the orphan backfill minted" in result.detail
    assert rung.verify() is True


# ── the recorded completion short-circuits the scan ─────────────────────────


def test_a_walk_after_a_recorded_completion_makes_no_catalog_listing_call(world: World) -> None:
    """nexus-1nrcu (critic): ``nx doctor``, ``nx upgrade --auto`` (every session start) and every walk call
    ``detect()``; a full-corpus scan each time, for rows that stay legacy by design, is not cheap. Once the
    completion is on file the rung answers from it."""
    _one_fillable_and_one_newer_row(world)
    ledger = InMemoryCompletionLedger()
    rung = world.rung(record_fn=lambda: ledger.completions().get(ManifestSectionLabelsRung.name))
    runner = LadderRunner(LadderRegistry((rung,)), ledger, package_version_fn=lambda: "9.9.9")

    first = runner.run()
    assert [r.outcome for r in first.runs] == [RungOutcome.RECORDED]
    listings, chunk_reads = world.listings, len(world.chunk_reads)
    assert listings > 0 and ledger.verified_rungs() == frozenset({RUNG_MANIFEST_SECTION_LABELS})

    second = runner.run()

    assert [r.outcome for r in second.runs] == [RungOutcome.ALREADY_RECORDED]
    assert (world.listings, len(world.chunk_reads)) == (listings, chunk_reads), "no listing, no chunk read"
    assert rung.detect().converged is True and world.listings == listings


def test_a_completion_recorded_by_an_earlier_package_version_still_short_circuits(world: World) -> None:
    """The rung is a one-time transition, not a per-release one: rows that stay legacy by design stay legacy
    (a forced re-index is the only later path), and a rescan per release would copy what the guards refused
    once a co-owner left."""
    from nexus.upgrade_ladder.completion import CompletionRecord

    _one_fillable_and_one_newer_row(world)
    old = CompletionRecord(rung_name=RUNG_MANIFEST_SECTION_LABELS, verified_at="t0", package_version="1.0.0")

    status = world.rung(record_fn=lambda: old).detect()

    assert (status.applicable, status.converged) == (True, True) and world.listings == 0


def test_an_unreadable_ledger_is_not_a_record_and_the_scan_runs(world: World) -> None:
    _one_fillable_and_one_newer_row(world)

    def broken():
        raise RuntimeError("ledger down")

    status = world.rung(record_fn=broken).detect()

    assert (status.applicable, status.converged) == (True, False) and world.listings == 1


# ── attribution by title ─────────────────────────────────────────────────────


def test_a_chunk_row_written_by_another_document_within_the_skew_is_not_copied(world: World) -> None:
    """nexus-1nrcu (critic): B is indexed, A (sharing a chunk) is indexed two minutes later and then
    deleted. The ``indexed_at`` guard (+5 min) lets A's row through; A is tombstoned, so the live ownership
    read counts one owner. The chunk row's title names A's file, not B's."""
    world.doc("1.1.1", title="b.py", file_path="src/b.py")
    world.row("1.1.1", 0, h(1))
    world.chunk(h(1), section_title="A's heading", section_type="references", indexed_at=WITHIN_SKEW,
                title="src/a.py:3-9")
    rung = world.rung()

    result = rung.converge(Report())

    assert world.appends == [] and world.labels("1.1.1") == [(0, None, None, None, False)]
    assert "1 chunk row not attributable" in result.detail
    assert rung.verify() is True, "left legacy by design: not pending"


@pytest.mark.parametrize("title", [
    "src/b.py:3-9",          # code chunk: <relative path>:<line range>
    "src/b.py:chunk-4",      # prose chunk: <relative path>:chunk-<n>
    "src/b.py:page-2",       # page chunk
    "b.py",                  # the document's own title
    "",                      # the chunk row carries none
    None,                    # no title key at all
])
def test_a_chunk_row_titled_for_the_document_or_untitled_is_copied(world: World, title) -> None:
    world.doc("1.1.1", title="b.py", file_path="src/b.py")
    world.row("1.1.1", 0, h(1))
    meta = {} if title is None else {"title": title}
    world.chunk(h(1), section_title="Refs", section_type="references", indexed_at=WITHIN_SKEW, **meta)

    world.rung().converge(Report())

    assert world.labels("1.1.1") == [(0, "Refs", "references", None, True)]


def test_a_paper_chunk_titled_with_the_documents_title_is_copied(world: World) -> None:
    world.doc("1.1.1", title="Attention Is All You Need", file_path="")
    world.row("1.1.1", 0, h(1))
    world.chunk(h(1), section_title="Refs", section_type="references", indexed_at=OLD, title="Attention Is All You Need")

    world.rung().converge(Report())

    assert world.labels("1.1.1") == [(0, "Refs", "references", None, True)]


@pytest.mark.parametrize("listed", [("1.1.2", 0), ("1.1.1", 7)], ids=["other-document", "other-position"])
def test_a_single_listing_that_does_not_name_this_row_is_not_ownership(world: World, listed: tuple[str, int]) -> None:
    """The ownership read answers with exactly ONE row for the chash, but it is not this row (the listing and
    the manifest read disagree: a document moved, a position renumbered between the two reads). One row is
    not enough; it has to be this one. Mutating the guard to ``len(held) == 1`` fills this row."""
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    doc_id, position = listed
    world.chash_positions = lambda chashes: [  # type: ignore[method-assign]
        {"chash": c, "doc_id": doc_id, "position": position, "chunk_count": 1, "collection": COLL,
         "labels_written": False} for c in chashes]

    result = world.rung().converge(Report())

    assert world.appends == [] and world.labels("1.1.1") == [(0, None, None, None, False)]
    assert "1 changed meanwhile" in result.detail


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


# ── nexus-6pfgb.58: a tombstoned co-owner ─────────────────────────────────────


def test_a_chash_also_held_by_a_tombstoned_document_is_not_filled(world: World) -> None:
    """The bead's pin. 1.1.1 and 1.1.2 each hold a chash alone among LIVE documents and the chunk row carries a
    label for both; a deleted document also held 1.1.1's chash, so that label may be its. Only 1.1.2 is filled.
    Mutating the rung to ignore the tombstoned holders fills both."""
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    world.doc("1.1.2")
    world.row("1.1.2", 0, h(2))
    _labelled(world, h(1))
    _labelled(world, h(2))
    world.deleted("9.9.9", 4, h(1))
    rung = world.rung()

    result = rung.converge(Report())

    assert result.outcome is ConvergeOutcome.COMPLETED
    assert world.labels("1.1.1") == [(0, None, None, None, False)], "the shared-with-a-deleted-document row stays legacy"
    assert world.labels("1.1.2") == [(0, "Refs", "references", 9, True)]
    assert "1 also held by a deleted document" in result.detail
    assert [a[1] for a in world.appends] == [[("1.1.2", [{
        "chash": h(2), "position": 0, "section_title": "Refs", "section_type": "references",
        "page_number": 9, "labels_written": True}])]]


def test_a_tombstoned_holder_of_another_chash_does_not_stop_this_fill(world: World) -> None:
    """The deleted holder is counted for a chash this live row holds alone, not for a chash it does not hold."""
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    world.deleted("9.9.9", 0, h(7))  # an unrelated chash
    result = world.rung().converge(Report())

    assert world.labels("1.1.1") == [(0, "Refs", "references", 9, True)]
    assert "0 also held by a deleted document" in result.detail


def test_dry_run_and_verify_do_not_count_a_row_with_a_tombstoned_holder_as_fillable(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    world.deleted("9.9.9", 0, h(1))
    rung = world.rung()

    assert rung.detect().converged is True
    assert rung.verify() is True


def test_a_tombstoned_holder_that_appears_between_the_scan_and_the_write_stops_the_fill(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    world.before_second_manifest_read = lambda w: w.deleted("9.9.9", 0, h(1))  # a co-owner is deleted meanwhile

    result = world.rung().converge(Report())

    assert world.appends == [] and world.labels("1.1.1") == [(0, None, None, None, False)]
    assert "1 changed meanwhile" in result.detail
    assert len(world.tombstoned_reads) == 2, "the batch is read for tombstoned holders at the scan and again before the write"


def test_an_engine_that_cannot_list_tombstoned_holders_is_not_applicable_and_writes_nothing(world: World) -> None:
    """Filling without the read is the gap: an engine before include_tombstoned sends no list, which is not 'none'."""
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    world.tombstoned_reported = False
    rung = world.rung()

    status = rung.detect()

    assert status.applicable is False and "tombstoned" in status.pending_detail
    result = world.rung().converge(Report())
    assert result.outcome is ConvergeOutcome.DEFERRED and "tombstoned" in result.detail
    assert world.appends == [] and world.labels("1.1.1") == [(0, None, None, None, False)]


def test_a_reader_with_no_tombstoned_read_is_not_applicable(world: World) -> None:
    world.doc("1.1.1")
    world.row("1.1.1", 0, h(1))
    _labelled(world, h(1))
    reader = SimpleNamespace(all_documents=world.all_documents, get_manifests=world.get_manifests,
                             chash_positions=world.chash_positions, close=lambda: None)
    rung = ManifestSectionLabelsRung(
        reader_factory=lambda: reader, writer_factory=lambda: world, t3_factory=lambda: world,
        record_fn=lambda: None)

    assert rung.detect().applicable is False
    assert rung.converge(Report()).outcome is ConvergeOutcome.DEFERRED
    assert world.appends == []


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


def test_a_request_is_bounded_by_rows_as_well_as_by_documents(world: World) -> None:
    """nexus-1nrcu (code review): 50 documents of thousands of fillable rows each was one request, every
    document in its own engine transaction. A request now carries at most ``max_rows`` rows, a document's
    rows spilling into the next request."""
    n = 0
    for t in ("1.1.1", "1.1.2", "1.1.3"):
        world.doc(t)
        for pos in range(3):
            n += 1
            world.row(t, pos, h(n))
            _labelled(world, h(n))

    world.rung(max_rows=4).converge(Report())

    sizes = [sum(len(rows) for _d, rows in docs) for _c, docs in world.appends]
    assert sizes == [4, 4, 1], "9 rows, cut at 4 per request"
    assert all(r.labels_written for rows in world.manifests.values() for r in rows)
    assert sorted((d, r["position"]) for _c, docs in world.appends for d, rows in docs for r in rows) == [
        (t, p) for t in ("1.1.1", "1.1.2", "1.1.3") for p in range(3)], "every row once"
    assert [u[0] for u in world.updates] == ["1.1.1", "1.1.2", "1.1.3"], "a document is restamped once, not per request"


def test_the_write_reads_each_rows_position_a_bounded_number_of_times(world: World) -> None:
    """The re-read index is built once per batch, not once per planned row (quadratic in a big document)."""
    world.doc("1.1.1")
    for pos in range(200):
        world.row("1.1.1", pos, h(pos + 1))
        _labelled(world, h(pos + 1))
    world.probe_positions = True

    world.rung().converge(Report())

    assert all(r.labels_written for r in world.manifests["1.1.1"])
    assert world.position_reads < 200 * 10, f"{world.position_reads} position reads for 200 rows"


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
