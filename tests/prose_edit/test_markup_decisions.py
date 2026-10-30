# SPDX-License-Identifier: AGPL-3.0-or-later
"""review.py decisions (nexus-scmcs.2.6, A1): the app's statuses as the flags the existing `apply` takes.

The verb writes nothing and logs nothing: `apply` stays the only writer of the document, the only store of
rejections and the only session logger.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, Prose, t2_titles
from tests.prose_edit.test_brief import brief_ok, edit
from tests.prose_edit.test_markup_verbs import (
    E5,
    EDITS,
    TEXT,
    app_reply,
    listing,
    read,
    read_list,
    run_review,
    review_ok,
    translated,
    write_json,
)


def proposed(prose: Prose, repo: Path, refuse: dict[int, str] | None = None, **kw) -> tuple[Path, list[dict]]:
    """A run up to the author's turn in the app: translate (edits 1-4 sent, 5 refused locally) and proposed."""
    work, _ = translated(prose, repo, **kw)
    reply = write_json(work / "proposed.json", app_reply(read(work / "propose.json"), refuse=refuse))
    review_ok(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    return work, read_list(work / "suggestions.json")


def decide(prose: Prose, work: Path, statuses: dict | list, *flags: str, name: str = "statuses.json"):
    return run_review(prose, "decisions", "--work", str(work), "--from-file",
                      str(write_json(work / name, statuses)), *flags)


def test_the_apps_statuses_become_apply_flags_for_this_runs_ids_only(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)  # ids s12..s15 for edits 1..4
    ids = {s["n"]: s["id"] for s in sugg}
    reply = listing(sugg, {ids[1]: "accepted", ids[2]: "rejected", ids[3]: "accepted", ids[4]: "pending"})
    proc = decide(prose, work, reply)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["accept"] == [1, 3] and out["reject"] == [2] and out["hold"] == [4]
    assert out["apply_args"] == ["--accept", "1,3", "--reject", "2", "--hold", "4"]
    assert out["warnings"] == []
    assert out["undecided"] == [5]  # refused locally: nobody has decided it, so apply holds it unless the chat names it
    assert [(r["n"], r["cause"]) for r in out["leftover"]] == [(5, "formatting")]


def test_stale_pending_and_missing_are_held_with_a_warning_for_the_missing_one(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    ids = {s["n"]: s["id"] for s in sugg}
    reply = listing([s for s in sugg if s["n"] != 4], {ids[1]: "stale", ids[2]: "accepted"})
    out = json.loads(decide(prose, work, reply).stdout)
    assert out["accept"] == [2] and out["hold"] == [1, 3, 4] and out["reject"] == []
    assert len(out["warnings"]) == 1 and "edit 4" in out["warnings"][0]


def test_chat_decisions_for_the_leftovers_join_the_apps(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo, refuse={1: "find crosses a formatting boundary"})  # the app refuses edit 2
    ids = {s["n"]: s["id"] for s in sugg}
    reply = listing(sugg, {ids[1]: "rejected", ids[3]: "accepted", ids[4]: "accepted"})
    proc = decide(prose, work, reply, "--accept", "2", "--reject", "5")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["accept"] == [2, 3, 4] and out["reject"] == [1, 5] and out["hold"] == []
    assert out["app"] == {"accept": [3, 4], "reject": [1], "hold": []}
    assert out["chat"] == {"accept": [2], "reject": [5], "hold": []}
    assert out["apply_args"] == ["--accept", "2,3,4", "--reject", "1,5"]
    assert out["undecided"] == []


def test_the_chat_cannot_decide_an_edit_the_app_holds(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    proc = decide(prose, work, listing(sugg, {}), "--accept", "1")
    assert proc.returncode == 1 and "1" in proc.stderr and "app" in proc.stderr


def test_reject_rest_means_the_leftovers_nobody_named(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo, refuse={0: "no"})  # the app refuses edit 1
    out = json.loads(decide(prose, work, listing(sugg, {}), "--accept", "1", "--reject", "rest").stdout)
    assert out["accept"] == [1] and out["reject"] == [5]


def test_both_flags_on_one_edit_are_refused(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    proc = decide(prose, work, listing(sugg, {}), "--accept", "5", "--hold", "5")
    assert proc.returncode == 1 and "both" in proc.stderr


def test_the_author_answers_join_the_leftovers(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo, queries=[{"n": 1, "anchor": "wakes every ten", "text": "Every ten exactly?"}])
    answers = write_json(work / "answers.json", [{"answers": "Q1", "old": "picks the oldest job.",
                                                  "new": "picks the oldest job first."}])
    added = review_ok(prose, "answer", "--work", str(work), "--from-file", str(answers))["added"]
    n = added[0]["n"]
    assert n == 6
    out = json.loads(decide(prose, work, listing(sugg, {}), "--accept", str(n)).stdout)
    assert out["accept"] == [n]


def test_a_missing_or_foreign_statuses_file_is_refused(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    outside = write_json(prose.tmp / "statuses.json", listing(sugg, {}))
    proc = run_review(prose, "decisions", "--work", str(work), "--from-file", str(outside))
    assert proc.returncode == 1 and "work" in proc.stderr
    proc = run_review(prose, "decisions", "--work", str(work))  # edits are with the app: the statuses are needed
    assert proc.returncode == 1 and "--from-file" in proc.stderr
    for bad in ({"ok": False}, "text", 3):
        proc = decide(prose, work, bad)
        assert proc.returncode == 1 and "list_suggestions" in proc.stderr, bad


def test_decisions_before_proposed_is_refused(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    proc = decide(prose, work, listing([], {}))
    assert proc.returncode == 1 and "proposed" in proc.stderr


def test_a_run_that_sent_nothing_needs_no_statuses(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo, edits=[E5])
    proc = run_review(prose, "decisions", "--work", str(work), "--accept", "5")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["apply_args"] == ["--accept", "5"]


def test_decisions_writes_nothing_and_stores_nothing(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    before = {p.name: p.read_bytes() for p in work.iterdir() if p.is_file() and p.name not in (".prose-edit-work",)}
    doc = (repo / "docs" / "s.md").read_bytes()
    assert decide(prose, work, listing(sugg, {sugg[0]["id"]: "rejected"})).returncode == 0
    after = {p.name: p.read_bytes() for p in work.iterdir() if p.is_file() and p.name not in (".prose-edit-work",)}
    assert {k: v for k, v in after.items() if k != "statuses.json"} == before and (repo / "docs" / "s.md").read_bytes() == doc
    assert doc.decode("utf-8") == TEXT
    assert not any(t.startswith(("log/", "doc/")) for t in t2_titles(REPO_PROJECT))


@pytest.mark.parametrize("bad", ["9", "x"])
def test_an_unknown_chat_number_is_refused(prose: Prose, repo: Path, bad: str) -> None:
    work, sugg = proposed(prose, repo)
    proc = decide(prose, work, listing(sugg, {}), "--accept", bad)
    assert proc.returncode == 1 and "--accept" in proc.stderr


# ---------------------------------------------------------------------------
# Gate 1c survivors (nexus-scmcs.2.10)
# ---------------------------------------------------------------------------


def test_the_suggestions_file_records_what_was_sent_not_what_the_listing_says(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    sent = read(work / "propose.json")
    by_n = dict(zip(sent["order"], sent["edits"]))
    assert [(s["n"], s["find"], s["replace"]) for s in sugg] == [(n, by_n[n]["find"], by_n[n]["replace"]) for n in sent["order"]]


def test_reject_rest_leaves_the_authors_answer_alone(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo, queries=[{"n": 1, "anchor": "wakes every ten", "text": "Every ten exactly?"}])
    answers = write_json(work / "answers.json", [{"answers": "Q1", "old": "picks the oldest job.",
                                                  "new": "picks the oldest job first."}])
    n = review_ok(prose, "answer", "--work", str(work), "--from-file", str(answers))["added"][0]["n"]
    out = json.loads(decide(prose, work, listing(sugg, {}), "--reject", "rest").stdout)
    assert out["reject"] == [5] and n not in out["reject"]


def test_reject_rest_leaves_an_unplaced_edit_alone(prose: Prose, repo: Path) -> None:
    work, out = translated(prose, repo, edits=[*EDITS, edit(6, "basically ", "", "occurs twice in the file")])
    assert [int(u["n"]) for u in read(work / "session.json")["unplaced"]] == [6]
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    d = json.loads(decide(prose, work, listing(read_list(work / "suggestions.json"), {}), "--reject", "rest").stdout)
    assert d["reject"] == [5] and 6 not in d["reject"]


def test_decisions_before_proposed_names_the_verb_and_has_no_traceback(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    proc = decide(prose, work, listing([], {}))
    assert proc.returncode == 1 and "run `review.py proposed` first" in proc.stderr and "Traceback" not in proc.stderr


def test_decisions_without_a_statuses_file_names_the_flag_and_has_no_traceback(prose: Prose, repo: Path) -> None:
    work, _ = proposed(prose, repo)
    proc = run_review(prose, "decisions", "--work", str(work))
    assert proc.returncode == 1 and "--from-file is required" in proc.stderr and "Traceback" not in proc.stderr


def test_a_statuses_file_in_another_runs_work_directory_is_refused(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    other = Path(brief_ok(prose, "tmpdir").strip())
    foreign = write_json(other / "statuses.json", listing(sugg, {}))
    proc = run_review(prose, "decisions", "--work", str(work), "--from-file", str(foreign))
    assert proc.returncode == 1 and "directly inside this work directory" in proc.stderr


# ---------------------------------------------------------------------------
# Gate 1a S2: the numbers in suggestions.json belong to the filtered.json translate read
# ---------------------------------------------------------------------------


def test_translate_records_the_sha256_of_the_filtered_proposal(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    want = hashlib.sha256((work / "filtered.json").read_bytes()).hexdigest()
    assert read(work / "propose.json")["filtered_sha"] == want
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    assert read(work / "propose.json")["filtered_sha"] == want  # `proposed` rewrites propose.json and keeps it


def test_decisions_refuses_when_the_filtered_proposal_changed_since_translate(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo)
    filtered = read(work / "filtered.json")
    filtered["edits"] = filtered["edits"][::-1]  # a re-filter that renumbered or reordered the edits
    write_json(work / "filtered.json", filtered)
    proc = decide(prose, work, listing(sugg, {sugg[0]["id"]: "accepted"}))
    assert proc.returncode == 1 and "filtered.json changed since translate" in proc.stderr
    assert "run `review.py translate` again" in proc.stderr and "Traceback" not in proc.stderr
    assert proc.stdout.strip() == ""


def test_an_answer_added_after_translate_does_not_trip_the_check(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo, queries=[{"n": 1, "anchor": "wakes every ten", "text": "Every ten exactly?"}])
    answers = write_json(work / "answers.json", [{"answers": "Q1", "old": "picks the oldest job.",
                                                  "new": "picks the oldest job first."}])
    n = review_ok(prose, "answer", "--work", str(work), "--from-file", str(answers))["added"][0]["n"]
    proc = decide(prose, work, listing(sugg, {}), "--accept", str(n))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["accept"] == [n]


def test_a_second_answer_after_a_changed_filtered_proposal_does_not_launder_the_change(prose: Prose, repo: Path) -> None:
    work, sugg = proposed(prose, repo, queries=[{"n": 1, "anchor": "wakes every ten", "text": "Every ten exactly?"}])
    filtered = read(work / "filtered.json")
    filtered["edits"] = filtered["edits"][::-1]
    write_json(work / "filtered.json", filtered)
    answers = write_json(work / "answers.json", [{"answers": "Q1", "old": "picks the oldest job.",
                                                  "new": "picks the oldest job first."}])
    review_ok(prose, "answer", "--work", str(work), "--from-file", str(answers))
    proc = decide(prose, work, listing(sugg, {}))
    assert proc.returncode == 1 and "filtered.json changed since translate" in proc.stderr
