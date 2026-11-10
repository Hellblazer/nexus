# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interactive ``nx upgrade`` asks each rung a cheap "is it pending?" (nexus-mjmjl): a rung that cannot answer from
its record or a bounded count says PENDING without reading, and its converge makes the one scan, with progress per
batch. Before, detect ran a dry-run scan of every document and converge ran the same scan again."""

from __future__ import annotations

from nexus.commands.upgrade import _BatchProgress
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
    from types import SimpleNamespace

    from nexus.upgrade_ladder.rungs.manifest_span_fill import TITLE_RULE_MARK

    record = SimpleNamespace(detail=f"filled; {TITLE_RULE_MARK}", verified_at="2026-11-10")
    rung = ManifestSpanFillRung(reader_factory=_Walks(), writer_factory=lambda: None, t3_factory=lambda: None,
                                record_fn=lambda: record, managed_fn=lambda: False)
    with cheap_detect():
        status = rung.detect()
    assert status.converged


def test_batch_events_become_throttled_progress_lines(capsys) -> None:
    now = [0.0]
    progress = _BatchProgress(clock=lambda: now[0])
    progress.emit("manifest_span_fill_batch", documents=3200, filled=410)
    now[0] += 1
    progress.emit("manifest_span_fill_batch", documents=3500, filled=450)  # within EVERY_S: no line
    now[0] += progress.EVERY_S
    progress.emit("manifest_span_fill_batch", documents=6400, filled=800)
    progress.emit("ladder_rung_recorded", rung="x")  # not a batch event: no line
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "  manifest span fill: documents 3,200, filled 410",
        "  manifest span fill: documents 6,400, filled 800",
    ]


def test_under_auto_the_lines_go_to_stderr(capsys) -> None:
    _BatchProgress(to_stderr=True, clock=lambda: 0.0).emit("note_tags_batch", notes=10)
    out = capsys.readouterr()
    assert out.out == "" and out.err == "  note tags: notes 10\n"
