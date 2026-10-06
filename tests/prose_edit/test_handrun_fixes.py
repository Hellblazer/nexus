# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixes from the RDR-221 interactive hand runs of 2026-10-04 (nexus-ger02.17, .18, .20, .24, .25).

.17 a path typed with Claude Code's @ file mention reaches parse with the @; .18 the brief showed the editor an
exemplar taken from the document it was editing; .20 the author could not tell that reject keeps the text; .24 the
line-editor dispatch ran in the background and the reply was copied with a heredoc; .25 a run with no sentence edits
had no way to close and was left out of the accept-rate record.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, Prose, t2_titles
from tests.prose_edit.test_brief import ORDER, SKILL, _module, brief_ok, fenced, proposal, run_brief
from tests.prose_edit.test_review import DOC, review_ok, start, status


# --- .17 ---------------------------------------------------------------------


@pytest.mark.parametrize(("tokens", "key", "want"), [
    (["@docs/x.md"], "path", "docs/x.md"),
    (["@docs/x.md:2-3", "--genre", "rdr"], "target", "docs/x.md:2-3"),
    (["rejections", "@docs/x.md"], "path", "docs/x.md"),
    (["exemplar", "rdr", "@docs/x.md:2-3"], "where", "docs/x.md:2-3"),
])
def test_a_leading_at_from_a_file_mention_is_dropped(tokens: list[str], key: str, want: str) -> None:
    assert _module().parse_invocation(tokens)[key] == want


def test_a_file_whose_name_starts_with_at_is_reached_after_dash_dash() -> None:
    mod = _module()
    assert mod.parse_invocation(["--", "@notes.md"])["path"] == "@notes.md"
    assert mod.parse_invocation(["rejections", "--", "@notes.md"])["path"] == "@notes.md"
    assert mod.parse_invocation(["@"])["path"] == "@"  # a bare @ is not a mention of anything


def test_a_mentioned_path_builds_with_its_genre_inferred(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "rdr").mkdir()
    (repo / "docs" / "rdr" / "rdr-001-x.md").write_text("# RDR-001\n\nBody.\n")
    parsed = json.loads(brief_ok(prose, "parse", "@docs/rdr/rdr-001-x.md"))
    assert "Genre: rdr" in brief_ok(prose, "build", parsed["target"])


# --- .18 ---------------------------------------------------------------------


def _genre_with(prose: Prose, *paths: str) -> None:
    prose.ok("genre-put", "reference-doc", stdin={
        "exemplars": [{"text": f"EXEMPLAR FROM {p}", "path": p, "start": 1, "end": 2} for p in paths],
        "notes": [],
    })


def _exemplars(text: str) -> str:
    return text[text.index(ORDER[0]):text.index(ORDER[1])]


def test_an_exemplar_from_the_document_under_edit_is_not_shown(prose: Prose) -> None:
    _genre_with(prose, "docs/x.md", "docs/y.md")
    shown = _exemplars(brief_ok(prose, "build", "docs/x.md", "--genre", "reference-doc"))
    assert "EXEMPLAR FROM docs/y.md" in shown and "EXEMPLAR FROM docs/x.md" not in shown


def test_when_every_exemplar_is_from_the_document_the_brief_says_so(prose: Prose) -> None:
    _genre_with(prose, "docs/x.md")
    shown = _exemplars(brief_ok(prose, "build", "docs/x.md", "--genre", "reference-doc"))
    assert "EXEMPLAR FROM" not in shown
    assert "all come from this document" in shown and "No exemplars are stored" not in shown
    assert "all come from this document" in SKILL.read_text(encoding="utf-8")  # step 6 tells the author


def test_a_range_run_on_the_exemplar_document_also_leaves_it_out(prose: Prose) -> None:
    _genre_with(prose, "docs/x.md")
    assert "EXEMPLAR FROM" not in _exemplars(brief_ok(prose, "build", "docs/x.md:2-3", "--genre", "reference-doc"))


def test_a_stdin_run_keeps_every_exemplar(prose: Prose, tmp_path: Path) -> None:
    _genre_with(prose, "docs/x.md")
    src = tmp_path / "in.txt"
    src.write_text("Some text.\n")
    shown = _exemplars(brief_ok(prose, "build", "-", "--genre", "reference-doc", "--file", str(src)))
    assert "EXEMPLAR FROM docs/x.md" in shown


# --- .20, .24, .25: the skill text -------------------------------------------


def _step(n: int) -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index(f"\n{n}. ")
    return text[start:text.index(f"\n{n + 1}. ", start)]


def test_step_eleven_says_reject_keeps_the_text_and_hold_stores_nothing() -> None:
    step = _step(11)
    assert "Reject keeps the document's text as it is" in step
    assert "the text stays as it is and nothing is stored" in step
    assert "(the text stays as it is, and the edit is stored)" in _step(12)


def test_steps_seven_and_eight_take_a_background_reply_and_forbid_a_shell_copy() -> None:
    assert "as a message from the agent" in _step(7)
    step = _step(8)
    assert "report text of the agent's message" in step and "Never copy the reply with Bash" in step


def test_step_eleven_closes_a_run_with_no_edits_without_asking_for_numbers() -> None:
    step = _step(11)
    assert "has no `edits`" in step and "do not ask for numbers" in step and "`--accept none`" in step


# --- .25: the close path the skill text relies on ----------------------------


def test_a_session_with_no_edits_closes_with_accept_none_and_is_logged(prose: Prose, repo: Path) -> None:
    work, rendered = start(prose, repo, [], queries=[{"n": 1, "anchor": "queue", "text": "q"}])
    assert rendered["counts"]["edits"] == 0
    before = status(repo)
    dry = review_ok(prose, "apply", "--work", str(work), "--accept", "none", "--dry-run")
    assert dry["accept"] == dry["hold"] == dry["reject"] == [] and dry["stores_rejections"] is False
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "none")
    assert out["applied"] == [] and out["rejected"] == [] and out["held"] == []
    assert out["log"] and str(out["log"]["title"]).startswith("log/docs/s.md/")
    assert any(t.startswith("log/docs/s.md/") for t in t2_titles(REPO_PROJECT))
    assert not work.exists() and status(repo) == before
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == DOC


def test_genre_notes_are_shown_when_every_exemplar_is_left_out(prose: Prose) -> None:
    prose.ok("genre-put", "reference-doc", stdin={
        "exemplars": [{"text": "EXEMPLAR FROM docs/x.md", "path": "docs/x.md", "start": 1, "end": 2}],
        "notes": ["GENRE-NOTE-KEPT"],
    })
    shown = _exemplars(brief_ok(prose, "build", "docs/x.md", "--genre", "reference-doc"))
    assert "all come from this document" in shown and "GENRE-NOTE-KEPT" in shown


def test_the_no_edits_close_shows_dropped_edits_and_step_twelve_does_not_ask_for_it() -> None:
    assert "each `dropped` edit with its cause" in _step(11) and "`warnings` entry" in _step(11)
    assert "in the no-edits close of step 11, do not ask: run it at once" in _step(12)


def test_a_stdin_session_with_no_edits_also_closes_and_is_logged(prose: Prose) -> None:
    fields = dict(line.split("=", 1) for line in brief_ok(prose, "tmpdir", "--ready").strip().splitlines())
    work, given = Path(fields["WORK"]), Path(fields["INPUT"])
    given.write_text("fix: drain the queue in order\n\nBody.\n", encoding="utf-8")
    reply = fenced(proposal([], queries=[{"n": 1, "anchor": "queue", "text": "q"}]))
    proc = run_brief(prose, "filter", "-", "--file", str(given), "--save", fields["FILTERED"], stdin=reply)
    assert proc.returncode == 0, proc.stderr
    review_ok(prose, "render", "-", "--work", str(work), "--file", str(given), "--genre", "commit-message")
    review_ok(prose, "apply", "--work", str(work), "--accept", "none", "--dry-run")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "none")
    assert out["mode"] == "stdin" and out["applied"] == [] and out["log"]
    assert not work.exists()
