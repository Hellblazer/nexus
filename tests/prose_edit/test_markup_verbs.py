# SPDX-License-Identifier: AGPL-3.0-or-later
"""review.py translate and proposed (nexus-scmcs.2.5, A1).

`translate` turns the placed edits into one propose_edits payload (WORK/propose.json); `proposed` records the
app's reply (WORK/suggestions.json, the app's refusals). Neither writes the document or T2. The app is a stand-in
here: the helpers build get_document, propose_edits and list_suggestions replies in the real shapes
(tests/prose_edit/fixtures/markupeditor).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.prose_edit.conftest import REPO_PROJECT, Prose, t2_get, t2_titles
from tests.prose_edit.test_brief import edit
from tests.prose_edit.test_review import review_ok, run_review, start, status

TEXT = (
    "# Queue notes\n\n"
    "The queue is basically ordered, and entries leave in the order they arrived.\n\n"
    "The worker really quite simply retries a failed job once before it gives up.\n\n"
    "It should be noted that the retry path is unchanged from the previous\nrelease.\n\n"
    "The scheduler just wakes every ten seconds and picks the oldest job.\n\n"
    "The **retry budget is basically fixed** at one attempt per job.\n\n"
    "* The list item says it is “really” simple.\n"
)
E1 = edit(1, "is basically ordered,", "is ordered,", "hedge")
E2 = edit(2, "really quite simply ", "", "hedges")
E3 = edit(3, "It should be noted that the retry path is unchanged from the previous\nrelease.",
          "The retry path is unchanged from the previous\nrelease.", "opener")
E4 = edit(4, "retry budget is basically fixed", "retry budget is fixed", "hedge inside bold")
E5 = edit(5, "one attempt per job", "a single **attempt** per job", "adds formatting")
EDITS = [E1, E2, E3, E4, E5]


def app_blocks(text: str) -> dict:
    """get_document's reply for `text`: blocks unwrapped to one line, list markers as `*`."""
    blocks = []
    for i, raw in enumerate(b for b in text.split("\n\n") if b.strip()):
        raw = raw.strip("\n")
        kind = "heading" if raw.startswith("#") else "code" if raw.startswith("```") else (
            "bullet-list" if raw.startswith(("* ", "- ")) else "paragraph")
        md = raw if kind == "code" else " ".join(raw.split("\n"))
        blocks.append({"index": i, "kind": kind, "hash": hashlib.sha1(md.encode("utf-8")).hexdigest()[:14],
                       "markdown": md, "warnings": []})
    return {"blocks": blocks, "documentType": "md", "firstEditable": 0, "hasChanges": False,
            "url": "/tmp/stand-in.md", "view": "editor"}


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def app_reply(payload: dict, refuse: dict[int, str] | None = None, first_id: int = 12) -> dict:
    """propose_edits' reply for a propose.json payload; `refuse` maps an edit's index in the call to a reason."""
    results, sid = [], first_id
    for i, e in enumerate(payload["edits"]):
        if refuse and i in refuse:
            results.append({"index": i, "status": "refused", "reason": refuse[i]})
        else:
            results.append({"index": i, "status": "proposed", "id": f"s{sid}", "block": e["block"]})
            sid += 1
    return {"ok": True, "results": results}


def listing(suggestions: list[dict], statuses: dict[str, str], foreign: bool = True) -> dict:
    """list_suggestions' reply: this run's suggestions with the author's statuses, plus an earlier document's."""
    out = [{"block": 1, "find": "that taxonomy is built per.", "id": "s1", "note": "old", "replace": "x",
            "status": "accepted"}] if foreign else []
    out += [{"block": 1, "find": s["find"], "id": s["id"], "note": f"Edit {s['n']}", "replace": s["replace"],
             "status": statuses.get(s["id"], "pending")} for s in suggestions]
    return {"ok": True, "suggestions": out}


def translated(prose: Prose, repo: Path, edits: list[dict] = EDITS, text: str = TEXT,
               **extra: object) -> tuple[Path, dict]:
    work, _ = start(prose, repo, edits, text=text, **extra)
    blocks = write_json(work / "blocks.json", app_blocks(text))
    return work, review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(blocks))


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_translate_writes_the_propose_payload_and_lists_what_it_refused(prose: Prose, repo: Path) -> None:
    work, out = translated(prose, repo)
    assert out["sent"] == 4
    assert [(r["n"], r["cause"]) for r in out["refused"]] == [(5, "formatting")]
    saved = read(work / "propose.json")
    assert saved["order"] == [1, 2, 3, 4] and saved["refused"] == out["refused"]
    assert saved["edits"] == out["edits"]
    for e in saved["edits"]:
        assert set(e) == {"block", "expected", "find", "replace", "note"}
    by_note = {e["note"]: e for e in saved["edits"]}
    assert by_note["Edit 1: hedge"]["find"] == "basically " and by_note["Edit 1: hedge"]["replace"] == ""
    # the wrapped old text of edit 3 was located in the app's unwrapped paragraph
    e3 = by_note["Edit 3: opener"]
    assert (e3["block"], e3["find"], e3["replace"]) == (3, "It should be noted that the", "The")
    assert by_note["Edit 4: hedge inside bold"]["find"] == "basically "
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == TEXT


def test_translate_changes_neither_the_document_nor_t2_and_git_stays_clean(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    clean = status(repo)
    raw = (repo / "docs" / "s.md").read_bytes()
    translated_out = review_ok(prose, "translate", "--work", str(work), "--blocks-file",
                               str(write_json(work / "blocks.json", app_blocks(TEXT))))
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    assert translated_out["sent"] == 4
    assert (repo / "docs" / "s.md").read_bytes() == raw and status(repo) == clean
    assert not any(t.startswith(("log/", "doc/")) for t in t2_titles(REPO_PROJECT))


def test_proposed_records_ids_and_the_apps_own_refusals(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    payload = read(work / "propose.json")
    reply = write_json(work / "proposed.json", app_reply(payload, refuse={1: "find crosses a formatting boundary"}))
    out = review_ok(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    assert out["suggested"] == 3
    assert [(r["n"], r["cause"]) for r in out["refused"]] == [(2, "app-refused")]
    assert "formatting boundary" in out["refused"][0]["detail"]
    sugg = read_list(work / "suggestions.json")
    assert [(s["n"], s["id"]) for s in sugg] == [(1, "s12"), (3, "s13"), (4, "s14")]
    assert all(set(s) == {"n", "id", "find", "replace"} for s in sugg)
    assert [(r["n"], r["cause"]) for r in read(work / "propose.json")["refused"]] == [(5, "formatting"), (2, "app-refused")]


def read_list(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def test_proposed_run_twice_does_not_duplicate_the_apps_refusals(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    reply = write_json(work / "proposed.json", app_reply(read(work / "propose.json"), refuse={1: "no"}))
    for _ in range(2):
        review_ok(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    assert [r["n"] for r in read(work / "propose.json")["refused"]] == [5, 2]


def test_translate_run_again_replaces_the_payload_and_drops_the_old_suggestions(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    assert (work / "suggestions.json").is_file()
    review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"))
    assert not (work / "suggestions.json").exists()
    assert read(work / "propose.json")["order"] == [1, 2, 3, 4]


def test_a_reply_of_the_wrong_length_is_refused_and_nothing_is_written(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    before = (work / "propose.json").read_bytes()
    reply = app_reply(read(work / "propose.json"))
    for bad in ({**reply, "results": reply["results"][:-1]},
                {**reply, "results": [*reply["results"][:-1], {**reply["results"][-1], "index": 0}]},
                {"ok": False, "error": "no document"}, ["not", "a", "reply"]):
        proc = run_review(prose, "proposed", "--work", str(work), "--from-file",
                          str(write_json(work / "proposed.json", bad)))
        assert proc.returncode == 1 and "propose_edits reply" in proc.stderr, bad
        assert not (work / "suggestions.json").exists() and (work / "propose.json").read_bytes() == before


def test_a_proposed_result_without_an_id_is_refused(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    reply = app_reply(read(work / "propose.json"))
    del reply["results"][0]["id"]
    proc = run_review(prose, "proposed", "--work", str(work), "--from-file", str(write_json(work / "proposed.json", reply)))
    assert proc.returncode == 1 and "propose_edits reply" in proc.stderr


def test_files_outside_the_work_directory_are_refused(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    outside = write_json(prose.tmp / "blocks.json", app_blocks(TEXT))
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(outside))
    assert proc.returncode == 1 and "work" in proc.stderr
    reply = write_json(prose.tmp / "proposed.json", app_reply(read(work / "propose.json")))
    proc = run_review(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    assert proc.returncode == 1 and "work" in proc.stderr and not (work / "suggestions.json").exists()


def test_a_blocks_file_that_is_empty_unreadable_or_the_source_view_is_refused(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    for bad in ({"blocks": []}, {"view": "source", "blocks": app_blocks(TEXT)["blocks"]}, {"nothing": 1}, [], "text"):
        proc = run_review(prose, "translate", "--work", str(work), "--blocks-file",
                          str(write_json(work / "blocks.json", bad)))
        assert proc.returncode == 1 and "blocks" in proc.stderr, bad
        assert not (work / "propose.json").exists()
    (work / "blocks.json").write_text("{not json", encoding="utf-8")
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"))
    assert proc.returncode == 1 and "blocks" in proc.stderr


def test_pages_of_get_document_concatenate(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    whole = app_blocks(TEXT)
    pages = [{**whole, "blocks": whole["blocks"][:3], "next": 3}, {**whole, "blocks": whole["blocks"][3:]}]
    out = review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(write_json(work / "blocks.json", pages)))
    assert out["sent"] == 4


def test_nothing_to_send_is_not_an_error(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo, edits=[E5])
    assert read(work / "propose.json")["edits"] == []
    out = review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"))
    assert out["sent"] == 0 and [r["n"] for r in out["refused"]] == [5]


def test_translate_needs_a_rendered_session(prose: Prose, repo: Path) -> None:
    from tests.prose_edit.test_brief import brief_ok
    work = Path(brief_ok(prose, "tmpdir").strip())
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file",
                      str(write_json(work / "blocks.json", app_blocks(TEXT))))
    assert proc.returncode == 1 and "render" in proc.stderr
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
