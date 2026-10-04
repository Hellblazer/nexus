# SPDX-License-Identifier: AGPL-3.0-or-later
"""The copy marks only the part of an edit that changes (nexus-ger02.19).

In the 2026-10-04 hand run a semicolon-to-colon edit was struck and re-inserted as a whole sentence, and the
author read it as "exactly the same as the edit". The copy now strikes the changed words only, and a change of
punctuation or whitespace alone is also named in its footnote. Apply still matches the edit's whole old text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.prose_edit.conftest import Prose
from tests.prose_edit.test_brief import edit
from tests.prose_edit.test_review import _review, review_ok, start

SEMI = "Nelson's docuverse was explicitly append-only; bytes are never truly deleted."
COLON = "Nelson's docuverse was explicitly append-only: bytes are never truly deleted."


@pytest.mark.parametrize(("old", "new", "want"), [
    (SEMI, COLON, ("Nelson's docuverse was explicitly append-only", ";", ":", " bytes are never truly deleted.")),
    ("The colour is red.", "The color is red.", ("The ", "colour", "color", " is red.")),  # never split a word
    ("Basically it is ordered.", "It is ordered.", ("", "Basically it", "It", " is ordered.")),
    ("Cut all of this.", "", ("", "Cut all of this.", "", "")),
    ("a  b", "a b", ("a ", " ", "", "b")),
])
def test_the_changed_part_is_cut_at_word_edges(old: str, new: str, want: tuple) -> None:
    assert _review().change_core(old, new) == want


@pytest.mark.parametrize(("old", "new", "note"), [
    (SEMI, COLON, " (punctuation only: `;` → `:`)"),
    ("a  b", "a b", " (whitespace only: a space → nothing)"),
    ("The colour is red.", "The color is red.", ""),
    ("It is done.", "It is done", " (punctuation only: `.` → nothing)"),
])
def test_a_change_no_reader_would_spot_is_named(old: str, new: str, note: str) -> None:
    assert _review().change_note(old, new) == note


def test_the_copy_marks_one_character_and_apply_still_changes_it(prose: Prose, repo: Path) -> None:
    text = f"Intro line.\n\n{SEMI}\n"
    work, rendered = start(prose, repo, [edit(1, SEMI, COLON, "no semicolons")], text=text)
    copy = Path(rendered["copy"]).read_text(encoding="utf-8")
    assert "append-only<del>;</del><ins>:</ins> bytes are never truly deleted.<sup>1</sup>" in copy
    assert f"<del>{SEMI}</del>" not in copy
    assert "<sup>1</sup> no semicolons (punctuation only: `;` → `:`)" in copy
    review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == f"Intro line.\n\n{COLON}\n"
