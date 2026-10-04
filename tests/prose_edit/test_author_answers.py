# SPDX-License-Identifier: AGPL-3.0-or-later
"""The author's answers become edits (nexus-ger02.21); a multi-sentence edit narrows to its sentence (.22).

Both come from the 2026-10-04 interactive hand runs. The author answered queries and a paragraph proposal with
changes, and the orchestrator made them by hand outside `REVIEW apply`, which the skill's rule forbids. And the
filter dropped a sound cut because the editor quoted the preceding sentence as context.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, Prose, t2_json, t2_titles
from tests.prose_edit.test_brief import SKILL, _module, edit, fenced, proposal, run_brief
from tests.prose_edit.test_review import E1, review_ok, run_review, start

Q1 = {"n": 1, "anchor": "queue drains", "text": "Is 'in order' exact?"}
P1 = {"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Third paragraph"', "advice": "repeats"}


# --- .22 ---------------------------------------------------------------------


@pytest.mark.parametrize(("old", "new", "want"), [
    ("flags spans that may have gone stale. Existing collections can be backfilled without re-embedding.",
     "flags spans that may have gone stale.",
     (" Existing collections can be backfilled without re-embedding.", "")),
    ("The cat sat. The dog really ran fast.", "The cat sat. The dog ran fast.",
     ("The dog really ran fast.", "The dog ran fast.")),
    ("Cut this one. Keep this one.", "Keep this one.", ("Cut this one. ", "")),
    ("The cat sat here. The dog ran.", "The cat sat there. The dog walked.", None),  # two sentences change
    ("One para.\n\nTwo para.", "One para.", None),  # a paragraph break is never narrowed
])
def test_a_multi_sentence_edit_narrows_to_the_sentences_it_changes(old: str, new: str, want: object) -> None:
    assert _module().narrow_edit(old, new) == want


def _filter(prose: Prose, repo: Path, text: str, edits: list[dict]) -> dict:
    (repo / "docs" / "n.md").write_text(text, encoding="utf-8")
    proc = run_brief(prose, "filter", "docs/n.md", stdin=fenced(proposal(edits)))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_filter_keeps_a_context_quoting_cut_as_the_one_sentence_cut(prose: Prose, repo: Path) -> None:
    text = "Audit flags spans that may have gone stale. Collections can be backfilled later.\n"
    out = _filter(prose, repo, text, [edit(1, "flags spans that may have gone stale. Collections can be "
                                             "backfilled later.", "flags spans that may have gone stale.")])
    assert out["dropped"] == []
    assert out["edits"][0]["old"] == " Collections can be backfilled later." and out["edits"][0]["new"] == ""
    assert any("narrowed" in w for w in out["warnings"])


def test_an_edit_that_changes_two_sentences_is_still_dropped(prose: Prose, repo: Path) -> None:
    out = _filter(prose, repo, "The cat sat here. The dog ran.\n",
                  [edit(1, "The cat sat here. The dog ran.", "The cat sat there. The dog walked.")])
    assert out["edits"] == [] and out["dropped"][0]["cause"] == "multi-sentence"


def test_a_narrowed_cut_applies_cleanly(prose: Prose, repo: Path) -> None:
    text = "Keep the first sentence. Drop the second sentence.\n"
    work, _ = start(prose, repo, [edit(1, "Keep the first sentence. Drop the second sentence.",
                                       "Keep the first sentence.")], text=text)
    review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == "Keep the first sentence.\n"


# --- .21 ---------------------------------------------------------------------


def _answer(prose: Prose, work: Path, entries: object) -> subprocess.CompletedProcess[str]:
    (work / "answers.json").write_text(json.dumps(entries), encoding="utf-8")
    return run_review(prose, "answer", "--work", str(work), "--from-file", str(work / "answers.json"))


def test_an_answer_becomes_the_next_numbered_edit_and_applies(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1], paragraphs=[P1])
    proc = _answer(prose, work, [
        {"answers": "Q1", "old": "the queue drains in order", "new": "the queue drains first in, first out"},
        {"answers": "P1", "old": "Third paragraph repeats itself. Third paragraph repeats itself.\n",
         "new": "Third paragraph says it once.\n"},
    ])
    assert proc.returncode == 0, proc.stderr
    added = json.loads(proc.stdout)["added"]
    assert [a["n"] for a in added] == [2, 3] and [a["answers"] for a in added] == ["Q1", "P1"]
    edits = json.loads((work / "filtered.json").read_text())["edits"]
    assert [e.get("by") for e in edits] == [None, "author", "author"]
    review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "2,3")
    body = (repo / "docs" / "s.md").read_text(encoding="utf-8")
    assert "first in, first out" in body and "Third paragraph says it once." in body
    assert "It really is quite simple." in body  # edit 1 was not accepted: held
    assert out["held"] == [1] and any(t.startswith("log/docs/s.md/") for t in t2_titles(REPO_PROJECT))


@pytest.mark.parametrize(("entry", "fragment"), [
    ({"answers": "Q9", "old": "the queue drains in order", "new": "x"}, "Q9 is not a query"),
    ({"answers": "Q1", "old": "no such text", "new": "x"}, "not-found"),
    ({"answers": "Q1", "old": "Third paragraph repeats itself.", "new": "x"}, "ambiguous"),
    ({"answers": "Q1", "old": "the queue", "new": "the queue", "extra": 1}, "exactly the keys"),
    ({"answers": "first", "old": "the queue", "new": "a queue"}, "like Q4 or P1"),
    ({"answers": "Q1", "old": "the queue", "new": "the queue"}, "must differ"),
])
def test_an_answer_that_cannot_be_placed_adds_nothing(prose: Prose, repo: Path, entry: dict, fragment: str) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1])
    before = (work / "filtered.json").read_bytes()
    proc = _answer(prose, work, [{"answers": "Q1", "old": "the queue drains in order", "new": "ok"}, entry])
    assert proc.returncode == 1 and fragment in proc.stderr, proc.stderr
    assert (work / "filtered.json").read_bytes() == before


def test_an_answers_file_outside_the_work_directory_is_refused(prose: Prose, repo: Path, tmp_path: Path) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1])
    outside = tmp_path / "answers.json"
    outside.write_text(json.dumps([{"answers": "Q1", "old": "the queue drains in order", "new": "x"}]))
    proc = run_review(prose, "answer", "--work", str(work), "--from-file", str(outside))
    assert proc.returncode == 1 and "--from-file" in proc.stderr


def test_the_authors_own_edit_is_never_stored_as_a_rejection(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1])
    assert _answer(prose, work, [{"answers": "Q1", "old": "the queue drains in order", "new": "x"}]).returncode == 0
    review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    proc = run_review(prose, "apply", "--work", str(work), "--reject", "2", "--dry-run")
    assert proc.returncode == 1 and "author's own answer" in proc.stderr
    dry = review_ok(prose, "apply", "--work", str(work), "--reject", "rest", "--dry-run")
    assert [r["n"] for r in dry["reject"]] == [1] and [h["n"] for h in dry["hold"]] == [2]


def test_the_skill_routes_answers_through_review_answer_and_prints_the_answers_path() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "`REVIEW answer --work '<work>' --from-file '<answers>'`" in text
    assert "never edit the document yourself" in text and "never rejected" in text
    assert "`REASONS=<path>` and `ANSWERS=<path>`" in text and "`ANSWERS=`, `ENTRY=`" in text


def test_the_log_keeps_the_authors_edits_out_of_the_editors_accept_rate(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1])
    assert _answer(prose, work, [{"answers": "Q1", "old": "the queue drains in order", "new": "x"}]).returncode == 0
    review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "2")
    log = t2_json(REPO_PROJECT, str(out["log"]["title"]))["session"]
    assert [e["n"] for e in log["edits"]] == [1] and [e["n"] for e in log["author_edits"]] == [2]
    assert log["accepted"] == [] and log["author_accepted"] == [2] and log["held"] == [1]


@pytest.mark.parametrize("second", [
    [{"answers": "Q1", "old": "the queue drains in order", "new": "y"}],  # the same text answered again
    [{"answers": "Q1", "old": "queue drains", "new": "y"}],  # inside the first answer's text
])
def test_an_answer_over_text_an_earlier_answer_changes_is_refused(prose: Prose, repo: Path, second: list) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1])
    assert _answer(prose, work, [{"answers": "Q1", "old": "the queue drains in order", "new": "x"}]).returncode == 0
    before = (work / "filtered.json").read_bytes()
    proc = _answer(prose, work, second)
    assert proc.returncode == 1 and "overlaps edit 2" in proc.stderr, proc.stderr
    assert (work / "filtered.json").read_bytes() == before


def test_two_answers_in_one_call_that_overlap_add_nothing(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1])
    before = (work / "filtered.json").read_bytes()
    proc = _answer(prose, work, [{"answers": "Q1", "old": "the queue drains in order", "new": "x"},
                                 {"answers": "Q1", "old": "drains in order. Basically", "new": "y"}])
    assert proc.returncode == 1 and "overlaps entry 1" in proc.stderr, proc.stderr
    assert (work / "filtered.json").read_bytes() == before


def test_an_answer_after_a_dry_run_needs_a_new_dry_run(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1], queries=[Q1])
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    assert _answer(prose, work, [{"answers": "Q1", "old": "the queue drains in order", "new": "x"}]).returncode == 0
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert proc.returncode == 1 and "dry run" in proc.stderr
