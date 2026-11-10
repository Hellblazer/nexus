# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interactive ``nx upgrade`` asks each rung a cheap "is it pending?" (nexus-mjmjl): a rung that cannot answer from
its record or a bounded count says PENDING without reading, converge makes the scan, with progress per batch, and
verify reuses converge's scan when it found nothing to fill. Before, detect ran a dry-run scan of every document,
converge ran it again, and verify a third time."""

from __future__ import annotations

from types import SimpleNamespace

from nexus.commands.upgrade import _BatchProgress
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from nexus.upgrade_ladder.scan_reuse import VERIFY_REUSE_S, ScanReuse
from nexus.upgrade_ladder.rungs.manifest_span_fill import TITLE_RULE_MARK
from nexus.upgrade_ladder.auto_guard import CHEAP_PENDING
from nexus.upgrade_ladder.rungs.manifest_section_labels import ManifestSectionLabelsRung
from nexus.upgrade_ladder.rungs.manifest_span_fill import ManifestSpanFillRung
from nexus.upgrade_ladder.rungs.note_tags_array import NoteTagsArrayRung
from nexus.upgrade_ladder.unattended import bounded_reads, cheap_detect, unattended_run


class _Walks:
    def __init__(self) -> None:
        self.walks: list[str] = []

    def __call__(self):
        owner = self

        class _R:
            def manifest_section_label_fillable(self, _limit):
                return None

            def close(self):
                pass

            def __getattr__(self, name):
                owner.walks.append(name)
                raise AssertionError(f"detect walked the catalog ({name})")

        return _R()


def _rungs(reader):
    return [
        ManifestSpanFillRung(reader_factory=reader, writer_factory=lambda: None, t3_factory=lambda: None,
                             record_fn=lambda: None, managed_fn=lambda: False),
        ManifestSectionLabelsRung(reader_factory=reader, writer_factory=lambda: None, t3_factory=lambda: None,
                                  record_fn=lambda: None, managed_fn=lambda: False, stale_zero_fn=lambda: False),
        NoteTagsArrayRung(reader_factory=reader, writer_factory=lambda: None, managed_fn=lambda: False,
                          record_fn=lambda: None),
    ]


def test_interactive_detect_reads_nothing_and_says_pending() -> None:
    reader = _Walks()
    with cheap_detect():
        statuses = [rung.detect() for rung in _rungs(reader)]
    assert reader.walks == []
    assert all(s.applicable and not s.converged and s.pending_detail == CHEAP_PENDING for s in statuses)


def test_doctor_and_the_callout_still_report_not_checked_even_inside_an_interactive_run() -> None:
    reader = _Walks()
    with cheap_detect(), unattended_run(), bounded_reads():
        statuses = [rung.detect() for rung in _rungs(reader)]
    assert reader.walks == []
    assert all(not s.applicable for s in statuses)


def test_a_recorded_rung_is_still_converged_from_its_record() -> None:
    record = SimpleNamespace(detail=f"filled; {TITLE_RULE_MARK}", verified_at="2026-11-10")
    rung = ManifestSpanFillRung(reader_factory=_Walks(), writer_factory=lambda: None, t3_factory=lambda: None,
                                record_fn=lambda: record, managed_fn=lambda: False)
    with cheap_detect():
        status = rung.detect()
    assert status.converged


def test_batch_events_become_throttled_progress_lines(capsys) -> None:
    now = [0.0]
    progress = _BatchProgress(clock=lambda: now[0])
    counters = {"rows": 9870, "span_complete": 9000, "chunk_missing": 0, "raced": 0}  # the unshown ones stay in the log
    progress.emit("manifest_span_fill_batch", documents=3200, fillable=410, filled=410, **counters)
    now[0] += 1
    progress.emit("manifest_span_fill_batch", documents=3500, fillable=450, filled=450, **counters)  # within EVERY_S
    now[0] += progress.EVERY_S
    progress.emit("manifest_span_fill_batch", documents=6400, fillable=800, filled=800, **counters)
    progress.emit("ladder_rung_recorded", rung="x")  # not a batch event: no line
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "  manifest span fill: documents 3,200, rows 9,870, fillable 410, filled 410",
        "  manifest span fill: documents 6,400, rows 9,870, fillable 800, filled 800",
    ]


def test_under_auto_the_lines_go_to_stderr(capsys) -> None:
    _BatchProgress(to_stderr=True, clock=lambda: 0.0).emit(
        "note_tags_rung_batch", converted=3, skipped=0, remaining=0, examined=10,
    )
    out = capsys.readouterr()
    assert out.out == "" and out.err == "  note tags: examined 10, converted 3, remaining 0\n"


def test_the_rdr192_backfill_prints_a_line_per_collection(capsys) -> None:
    _BatchProgress(clock=lambda: 0.0).emit(
        "rdr192_backfill_collection_done", collection="docs__1-1__bge__v1", pass_number=1, legacy_before=40,
        manifest_rows_written=40, skipped={},
    )
    assert capsys.readouterr().out == "  rdr192 backfill: collection docs__1-1__bge__v1, manifest rows written 40\n"


class _Notes:
    """A catalog of notes for the note-tags rung; counts every full listing."""

    def __init__(self, tags: dict[str, object]) -> None:
        self.docs = {t: SimpleNamespace(tumbler=t, title=t, content_type="knowledge", file_path="",
                                        physical_collection="knowledge__notes__bge__v1", meta={"tags": v})
                     for t, v in tags.items()}
        self.listings = 0

    def distinct_doc_collections(self):
        self.listings += 1
        return ["knowledge__notes__bge__v1"]

    def list_by_collection(self, _collection):
        return list(self.docs.values())

    def resolve(self, tumbler):
        return self.docs.get(tumbler)

    def update(self, tumbler, *, meta):
        self.docs[tumbler].meta.update(meta)

    def close(self):
        pass


def _note_rung(world: _Notes, record=None) -> NoteTagsArrayRung:
    return NoteTagsArrayRung(reader_factory=lambda: world, writer_factory=lambda: world,
                             managed_fn=lambda: False, record_fn=lambda: record)


def test_a_recorded_note_tags_rung_still_lists_the_notes_in_an_interactive_run() -> None:
    """A record does not stop an interactive run from finding strings an older client wrote since."""
    world = _Notes({"1.1.1": "a,b"})
    with cheap_detect():
        status = _note_rung(world, record=SimpleNamespace(verified_at="2026-11-01")).detect()
    assert world.listings == 1 and not status.converged and status.pending_detail != CHEAP_PENDING


def _walk(world: _Notes):
    from tests.upgrade.conftest import InMemoryCompletionLedger  # noqa: PLC0415 — the suite's shared fake

    ledger = InMemoryCompletionLedger()
    runner = LadderRunner(LadderRegistry((_note_rung(world),)), ledger, package_version_fn=lambda: "9.9.9")
    with cheap_detect():
        report = runner.run()
    return report, ledger


def test_an_interactive_walk_with_nothing_to_fill_reads_once_and_records() -> None:
    world = _Notes({"1.1.1": ["a", "b"]})
    report, ledger = _walk(world)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED], [(r.outcome, r.detail) for r in report.runs]
    assert world.listings == 1  # converge's listing; verify reused it
    assert ledger.verified_rungs()


def test_an_interactive_walk_still_fills_and_verify_reads_afresh() -> None:
    world = _Notes({"1.1.1": "a,b", "1.1.2": ["c"]})
    report, ledger = _walk(world)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED], [(r.outcome, r.detail) for r in report.runs]
    assert world.docs["1.1.1"].meta["tags"] == ["a", "b"]
    assert world.listings == 2  # converge's listing, then verify confirms the fill
    assert ledger.verified_rungs()


def test_a_held_scan_is_taken_once_and_expires() -> None:
    now = [0.0]
    reuse: ScanReuse[str] = ScanReuse(clock=lambda: now[0])
    reuse.keep("clean")
    assert reuse.take() == "clean" and reuse.take() is None
    reuse.keep("clean")
    now[0] += VERIFY_REUSE_S + 1
    assert reuse.take() is None
    reuse.keep("clean")
    reuse.clear()
    assert reuse.take() is None
