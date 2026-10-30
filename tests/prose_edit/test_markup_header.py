# SPDX-License-Identifier: AGPL-3.0-or-later
"""A path run's build header carries the work paths the MarkupEditor verbs read (nexus-scmcs.2.4, A1).

BLOCKS= is where the skill writes get_document's reply, PROPOSED= propose_edits' reply, STATUSES= list_suggestions'
reply. The skill copies these lines and never builds a path (nexus-ger02.7). A stdin run has no app: its
`tmpdir --ready` header is unchanged.
"""
from __future__ import annotations

from pathlib import Path

from tests.prose_edit.conftest import Prose
from tests.prose_edit.test_brief import brief_ok


def _fields(head: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in head.splitlines())


def test_a_path_run_header_lists_the_markup_paths_in_order_inside_the_work_directory(prose: Prose) -> None:
    out = brief_ok(prose, "build", "docs/x.md", "--work")
    fields = _fields(out.split("\n\n", 1)[0])
    work = Path(fields["WORK"])
    try:
        assert list(fields) == ["WORK", "DISPATCH", "REPLY", "FILTERED", "REASONS", "ANSWERS",
                                "BLOCKS", "PROPOSED", "STATUSES"]
        for key, name in (("BLOCKS", "blocks.json"), ("PROPOSED", "proposed.json"), ("STATUSES", "statuses.json")):
            assert Path(fields[key]) == work / name and Path(fields[key]).is_absolute(), key
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_a_stdin_runs_tmpdir_ready_header_is_unchanged(prose: Prose) -> None:
    fields = _fields(brief_ok(prose, "tmpdir", "--ready").strip())
    work = Path(fields["WORK"])
    try:
        assert list(fields) == ["WORK", "INPUT", "REPLY", "FILTERED", "REASONS", "ANSWERS", "ENTRY", "CARD"]
        assert not {"BLOCKS", "PROPOSED", "STATUSES"} & set(fields)
    finally:
        brief_ok(prose, "rmtmp", str(work))
