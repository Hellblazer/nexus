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


def target_url(repo: Path) -> str:
    """The `url` get_document reports for the run's target: the file `start` writes."""
    return str(repo / "docs" / "s.md")


def app_blocks(text: str, url: str) -> dict:
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
            "url": url, "view": "editor"}


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
    blocks = write_json(work / "blocks.json", app_blocks(text, target_url(repo)))
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
                               str(write_json(work / "blocks.json", app_blocks(TEXT, target_url(repo)))))
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
    outside = write_json(prose.tmp / "blocks.json", app_blocks(TEXT, target_url(repo)))
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(outside))
    assert proc.returncode == 1 and "work" in proc.stderr
    reply = write_json(prose.tmp / "proposed.json", app_reply(read(work / "propose.json")))
    proc = run_review(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    assert proc.returncode == 1 and "work" in proc.stderr and not (work / "suggestions.json").exists()


def test_a_blocks_file_that_is_empty_unreadable_or_the_source_view_is_refused(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    for bad in ({"blocks": []}, {"view": "source", "blocks": app_blocks(TEXT, target_url(repo))["blocks"]}, {"nothing": 1}, [], "text"):
        proc = run_review(prose, "translate", "--work", str(work), "--blocks-file",
                          str(write_json(work / "blocks.json", bad)))
        assert proc.returncode == 1 and "blocks" in proc.stderr, bad
        assert not (work / "propose.json").exists()
    (work / "blocks.json").write_text("{not json", encoding="utf-8")
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"))
    assert proc.returncode == 1 and "blocks" in proc.stderr


def test_pages_of_get_document_concatenate(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    whole = app_blocks(TEXT, target_url(repo))
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
                      str(write_json(work / "blocks.json", app_blocks(TEXT, target_url(repo)))))
    assert proc.returncode == 1 and "render" in proc.stderr
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None


# ---------------------------------------------------------------------------
# The blocks file must be the whole of the right document, unchanged (Gate 1 S2, F3)
# ---------------------------------------------------------------------------


def translate_proc(prose: Prose, work: Path, blocks: object):
    return run_review(prose, "translate", "--work", str(work), "--blocks-file",
                      str(write_json(work / "blocks.json", blocks)))


def refused_blocks(prose: Prose, repo: Path, mutate, needle: str) -> None:
    """A blocks file broken by `mutate(pages)` is refused, with a cause that names `needle`, and nothing is written."""
    work, _ = start(prose, repo, EDITS, text=TEXT)
    whole = app_blocks(TEXT, target_url(repo))
    pages = mutate(whole)
    proc = translate_proc(prose, work, pages)
    assert proc.returncode == 1, proc.stdout
    assert needle in proc.stderr, proc.stderr
    assert not (work / "propose.json").exists()


def test_a_last_page_that_still_has_next_is_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: {**w, "blocks": w["blocks"][:3], "next": 3}, "next")


def test_pages_whose_last_page_still_has_next_are_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: [{**w, "blocks": w["blocks"][:3], "next": 3},
                                           {**w, "blocks": w["blocks"][3:5], "next": 5}], "next")


def test_indexes_that_skip_a_block_are_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: {**w, "blocks": w["blocks"][:2] + w["blocks"][3:]}, "contiguous")


def test_a_read_that_starts_after_the_first_block_is_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: {**w, "blocks": w["blocks"][2:]}, "contiguous")


def test_pages_that_leave_a_gap_between_them_are_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: [{**w, "blocks": w["blocks"][:2], "next": 2},
                                           {**w, "blocks": w["blocks"][3:]}], "contiguous")


# ---------------------------------------------------------------------------
# One --blocks-file for each page of a long document (nexus-scmcs.2.17, defect 2)
# ---------------------------------------------------------------------------


def two_pages(repo: Path, cut: int = 3) -> tuple[dict, dict]:
    """get_document's two pages of TEXT: the first cut at block `cut`, which its `next` names."""
    whole = app_blocks(TEXT, target_url(repo))
    return {**whole, "blocks": whole["blocks"][:cut], "next": cut}, {**whole, "blocks": whole["blocks"][cut:]}


def translate_pages(prose: Prose, work: Path, *pages: object):
    """`translate` with one --blocks-file for each page, written inside WORK as blocks.json, blocks-2.json, ..."""
    argv: list[str] = []
    for i, page in enumerate(pages, start=1):
        name = "blocks.json" if i == 1 else f"blocks-{i}.json"
        argv += ["--blocks-file", str(write_json(work / name, page))]
    return run_review(prose, "translate", "--work", str(work), *argv)


def refused_pages(prose: Prose, repo: Path, mutate, needle: str) -> None:
    """Pages broken by `mutate(first, second)`, one file each, are refused naming `needle`; nothing is written."""
    work, _ = start(prose, repo, EDITS, text=TEXT)
    proc = translate_pages(prose, work, *mutate(*two_pages(repo)))
    assert proc.returncode == 1, proc.stdout
    assert needle in proc.stderr, proc.stderr
    assert not (work / "propose.json").exists()


def test_one_blocks_file_for_each_page_merges_them_in_order(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    proc = translate_pages(prose, work, *two_pages(repo))
    assert proc.returncode == 0, proc.stderr
    paged = json.loads(proc.stdout)
    whole = review_ok(prose, "translate", "--work", str(work), "--blocks-file",
                      str(write_json(work / "blocks.json", app_blocks(TEXT, target_url(repo)))))
    assert paged["sent"] == whole["sent"] == 4 and paged["edits"] == whole["edits"]  # non-vacuity: edits cross the cut
    assert paged["refused"] == whole["refused"]


def test_three_pages_in_three_files_merge(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    whole = app_blocks(TEXT, target_url(repo))
    pages = [{**whole, "blocks": whole["blocks"][:2], "next": 2}, {**whole, "blocks": whole["blocks"][2:5], "next": 5},
             {**whole, "blocks": whole["blocks"][5:]}]
    proc = translate_pages(prose, work, *pages)
    assert proc.returncode == 0 and json.loads(proc.stdout)["sent"] == 4, proc.stderr


def test_a_file_holding_a_list_of_replies_is_still_accepted_beside_another_file(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    whole = app_blocks(TEXT, target_url(repo))
    first = [{**whole, "blocks": whole["blocks"][:2], "next": 2}, {**whole, "blocks": whole["blocks"][2:4], "next": 4}]
    proc = translate_pages(prose, work, first, {**whole, "blocks": whole["blocks"][4:]})
    assert proc.returncode == 0 and json.loads(proc.stdout)["sent"] == 4, proc.stderr


def test_pages_given_out_of_order_are_refused(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: (b, a), "next")


def test_a_first_page_whose_next_is_not_the_second_pages_first_block_is_refused(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: ({**a, "next": 2}, b), "contiguous")  # overlap: block 2 comes twice
    refused_pages(prose, repo, lambda a, b: ({**a, "next": 4}, b), "contiguous")  # gap: block 3 is claimed missing


def test_a_second_page_that_starts_after_the_first_pages_next_is_refused(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: (a, {**b, "blocks": b["blocks"][1:]}), "contiguous")


def test_a_page_with_no_next_followed_by_another_page_is_refused(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: ({k: v for k, v in a.items() if k != "next"}, b), "next")


def test_the_same_page_given_twice_is_refused(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: (a, a, b), "contiguous")


def test_a_last_page_file_that_still_has_next_is_refused(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: (a, {**b, "blocks": b["blocks"][:1], "next": 4}), "next")


def test_the_second_page_is_checked_for_url_has_changes_and_the_source_view(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: (a, {**b, "url": str(repo / "docs" / "other.md")}), "url")
    refused_pages(prose, repo, lambda a, b: (a, {k: v for k, v in b.items() if k != "url"}), "url")
    refused_pages(prose, repo, lambda a, b: (a, {**b, "hasChanges": True}), "hasChanges")
    refused_pages(prose, repo, lambda a, b: (a, {**b, "view": "source"}), "source view")


def test_the_first_page_is_checked_when_the_second_is_clean(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: ({**a, "hasChanges": True}, b), "hasChanges")
    refused_pages(prose, repo, lambda a, b: ({**a, "url": str(repo / "docs" / "other.md")}, b), "url")


def test_a_second_page_with_a_block_that_has_no_hash_or_an_empty_list_is_refused(prose: Prose, repo: Path) -> None:
    refused_pages(prose, repo, lambda a, b: (a, {**b, "blocks": [{"index": 3, "markdown": "x"}, *b["blocks"][1:]]}), "hash")
    refused_pages(prose, repo, lambda a, b: (a, {**b, "blocks": []}), "holds no blocks")


def test_a_page_file_outside_the_work_directory_or_unreadable_is_refused(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    first, second = two_pages(repo)
    outside = write_json(prose.tmp / "blocks-2.json", second)
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(write_json(work / "blocks.json", first)),
                      "--blocks-file", str(outside))
    assert proc.returncode == 1 and "work" in proc.stderr and not (work / "propose.json").exists()
    (work / "blocks-2.json").write_text("{not json", encoding="utf-8")
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"),
                      "--blocks-file", str(work / "blocks-2.json"))
    assert proc.returncode == 1 and "blocks-2.json" in proc.stderr and not (work / "propose.json").exists()


def test_a_buffer_with_unsaved_changes_is_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: {**w, "hasChanges": True}, "hasChanges")


def test_a_second_page_with_unsaved_changes_is_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: [{**w, "blocks": w["blocks"][:3], "next": 3},
                                           {**w, "blocks": w["blocks"][3:], "hasChanges": True}], "hasChanges")


def test_a_reply_for_another_document_is_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: {**w, "url": str(repo / "docs" / "other.md")}, "url")


def test_a_reply_that_names_no_url_is_refused(prose: Prose, repo: Path) -> None:
    refused_blocks(prose, repo, lambda w: {k: v for k, v in w.items() if k != "url"}, "url")


def test_the_url_is_compared_by_realpath(prose: Prose, repo: Path) -> None:
    """macOS reports /private/tmp/... for a session whose target was given as /tmp/...; a symlinked path is the same file."""
    work, _ = start(prose, repo, EDITS, text=TEXT)
    link = prose.tmp / "alias"
    link.symlink_to(repo, target_is_directory=True)
    whole = app_blocks(TEXT, str(link / "docs" / "s.md"))
    proc = translate_proc(prose, work, whole)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["sent"] == 4


# ---------------------------------------------------------------------------
# The reply must be for the edits sent (Gate 1 F2)
# ---------------------------------------------------------------------------


def proposed_proc(prose: Prose, work: Path, reply: object):
    return run_review(prose, "proposed", "--work", str(work), "--from-file", str(write_json(work / "proposed.json", reply)))


def test_a_result_that_echoes_another_block_is_refused_and_nothing_is_written(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    before = (work / "propose.json").read_bytes()
    reply = app_reply(read(work / "propose.json"))
    reply["results"][2]["block"] += 1
    proc = proposed_proc(prose, work, reply)
    assert proc.returncode == 1 and "propose_edits reply" in proc.stderr and "block" in proc.stderr
    assert not (work / "suggestions.json").exists() and (work / "propose.json").read_bytes() == before


def test_a_proposed_result_that_echoes_no_block_is_refused(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    reply = app_reply(read(work / "propose.json"))
    del reply["results"][0]["block"]
    proc = proposed_proc(prose, work, reply)
    assert proc.returncode == 1 and "block" in proc.stderr
    assert not (work / "suggestions.json").exists()


def test_two_results_with_one_id_are_refused_and_nothing_is_written(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    before = (work / "propose.json").read_bytes()
    reply = app_reply(read(work / "propose.json"))
    reply["results"][1]["id"] = reply["results"][0]["id"]
    proc = proposed_proc(prose, work, reply)
    assert proc.returncode == 1 and "propose_edits reply" in proc.stderr and reply["results"][0]["id"] in proc.stderr
    assert not (work / "suggestions.json").exists() and (work / "propose.json").read_bytes() == before


def test_a_refused_result_needs_no_block(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    reply = app_reply(read(work / "propose.json"), refuse={1: "no"})
    assert "block" not in reply["results"][1]
    assert proposed_proc(prose, work, reply).returncode == 0


# ---------------------------------------------------------------------------
# Gate 1c survivors: each refusal is a named message, never a traceback that happens to exit 1 (nexus-scmcs.2.10)
# ---------------------------------------------------------------------------


def refused_by_name(prose: Prose, repo: Path, mutate, *needles: str) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT)
    proc = translate_proc(prose, work, mutate(app_blocks(TEXT, target_url(repo))))
    assert proc.returncode == 1, proc.stdout
    assert all(n in proc.stderr for n in needles) and "Traceback" not in proc.stderr, proc.stderr
    assert not (work / "propose.json").exists()


def test_the_source_view_is_refused_by_name_even_with_a_url(prose: Prose, repo: Path) -> None:
    refused_by_name(prose, repo, lambda w: {**w, "view": "source"}, "source view")


def test_a_reply_with_no_blocks_and_a_url_is_refused_by_name(prose: Prose, repo: Path) -> None:
    refused_by_name(prose, repo, lambda w: {**w, "blocks": []}, "holds no blocks")


def test_a_missing_url_is_refused_by_name(prose: Prose, repo: Path) -> None:
    refused_by_name(prose, repo, lambda w: {k: v for k, v in w.items() if k != "url"}, "names no `url`")


def test_a_block_without_its_fields_is_refused_by_name(prose: Prose, repo: Path) -> None:
    def broken(w: dict) -> dict:
        w["blocks"][1] = {"index": 1}
        return w
    refused_by_name(prose, repo, broken, "needs an integer index")


def test_a_block_index_that_appears_twice_is_refused_by_name(prose: Prose, repo: Path) -> None:
    def twice(w: dict) -> dict:
        w["blocks"][2] = dict(w["blocks"][1])
        return w
    refused_by_name(prose, repo, twice, "appears twice")


def test_a_read_that_starts_at_firstEditable_after_the_first_block_is_accepted(prose: Prose, repo: Path) -> None:
    """The app lists read-only blocks (a metadata block) before firstEditable; a read from there is a whole document."""
    work, _ = start(prose, repo, EDITS, text=TEXT)
    whole = app_blocks(TEXT, target_url(repo))
    proc = translate_proc(prose, work, {**whole, "blocks": whole["blocks"][1:], "firstEditable": 1})
    assert proc.returncode == 0, proc.stderr


def test_an_unknown_propose_status_is_refused_not_dropped(prose: Prose, repo: Path) -> None:
    work, _ = translated(prose, repo)
    reply = app_reply(read(work / "propose.json"))
    reply["results"][1]["status"] = "weird"
    proc = proposed_proc(prose, work, reply)
    assert proc.returncode == 1 and "status" in proc.stderr and "Traceback" not in proc.stderr
    assert not (work / "suggestions.json").exists()


def test_an_answer_the_author_wrote_is_not_sent_to_the_app(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, EDITS, text=TEXT, queries=[{"n": 1, "anchor": "wakes every ten", "text": "Every ten exactly?"}])
    answers = write_json(work / "answers.json", [{"answers": "Q1", "old": "picks the oldest job.",
                                                  "new": "picks the oldest job first."}])
    n = review_ok(prose, "answer", "--work", str(work), "--from-file", str(answers))["added"][0]["n"]
    out = review_ok(prose, "translate", "--work", str(work), "--blocks-file",
                    str(write_json(work / "blocks.json", app_blocks(TEXT, target_url(repo)))))
    assert n not in read(work / "propose.json")["order"] and out["sent"] == 4


def test_an_edit_render_could_not_place_is_not_sent_to_the_app(prose: Prose, repo: Path) -> None:
    twice = edit(6, "basically ", "", "occurs twice in the file")
    work, out = translated(prose, repo, edits=[*EDITS, twice])
    assert [int(u["n"]) for u in read(work / "session.json")["unplaced"]] == [6]  # setup: render could not place it
    assert 6 not in read(work / "propose.json")["order"] and out["sent"] == 4
    assert 6 not in [r["n"] for r in out["refused"]]
