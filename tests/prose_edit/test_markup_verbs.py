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
from tests.prose_edit.test_brief import brief_ok, edit
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


def target_url(repo: Path, rel: str = "docs/s.md") -> str:
    """The `url` get_document reports for the run's target: the file `start` writes."""
    return str(repo / rel)


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


def translated(prose: Prose, repo: Path, edits: list[dict] = EDITS, text: str = TEXT, rel: str = "docs/s.md",
               **extra: object) -> tuple[Path, dict]:
    work, _ = start(prose, repo, edits, text=text, rel=rel, **extra)
    blocks = write_json(work / "blocks.json", app_blocks(text, target_url(repo, rel)))
    return work, review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(blocks))


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))




def read_list(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def translate_proc(prose: Prose, work: Path, blocks: object):
    return run_review(prose, "translate", "--work", str(work), "--blocks-file",
                      str(write_json(work / "blocks.json", blocks)))


def translate_pages(prose: Prose, work: Path, *pages: object):
    """`translate` with one --blocks-file for each page, written inside WORK as blocks.json, blocks-2.json, ..."""
    argv: list[str] = []
    for i, page in enumerate(pages, start=1):
        name = "blocks.json" if i == 1 else f"blocks-{i}.json"
        argv += ["--blocks-file", str(write_json(work / name, page))]
    return run_review(prose, "translate", "--work", str(work), *argv)


def two_pages(repo: Path, cut: int = 3) -> tuple[dict, dict]:
    """get_document's two pages of TEXT: the first cut at block `cut`, which its `next` names."""
    whole = app_blocks(TEXT, target_url(repo))
    return {**whole, "blocks": whole["blocks"][:cut], "next": cut}, {**whole, "blocks": whole["blocks"][cut:]}


def proposed_proc(prose: Prose, work: Path, reply: object):
    return run_review(prose, "proposed", "--work", str(work), "--from-file", str(write_json(work / "proposed.json", reply)))


def translate_refusal(work: Path, proc, needles: tuple[str, ...], named: bool) -> str | None:
    """What is wrong with a `translate` that should have been refused (None when it was): exit 1, a message that
    names every needle (and shows no traceback when `named`), and no propose.json written."""
    problems = []
    if proc.returncode != 1:
        problems.append(f"exit {proc.returncode}, stdout {proc.stdout!r}")
    problems += [f"stderr lacks {n!r}: {proc.stderr!r}" for n in needles if n not in proc.stderr]
    if named and "Traceback" in proc.stderr:
        problems.append(f"a traceback, not a named refusal: {proc.stderr!r}")
    if (work / "propose.json").exists():
        problems.append("propose.json was written")
    return "; ".join(problems) or None


def test_translate_and_proposed_take_a_run_from_the_filtered_edits_to_the_apps_suggestions(prose: Prose, repo: Path) -> None:
    """One path run through translate, proposed (twice), translate again and proposed again: the payload, the ids,
    the app's own refusals recorded once, the document and T2 untouched throughout."""
    work, _ = start(prose, repo, EDITS, text=TEXT)
    clean, raw = status(repo), (repo / "docs" / "s.md").read_bytes()
    blocks = write_json(work / "blocks.json", app_blocks(TEXT, target_url(repo)))
    out = review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(blocks))
    # translate writes the propose payload and lists what it refused
    assert out["sent"] == 4, f"translate sent {out['sent']} edits, wanted 4"
    assert [(r["n"], r["cause"]) for r in out["refused"]] == [(5, "formatting")], out["refused"]
    saved = read(work / "propose.json")
    assert saved["order"] == [1, 2, 3, 4] and saved["refused"] == out["refused"], saved
    assert saved["edits"] == out["edits"], "propose.json holds other edits than translate printed"
    for e in saved["edits"]:
        assert set(e) == {"block", "expected", "find", "replace", "note"}, e
    by_note = {e["note"]: e for e in saved["edits"]}
    assert by_note["Edit 1: hedge"]["find"] == "basically " and by_note["Edit 1: hedge"]["replace"] == ""
    # the wrapped old text of edit 3 was located in the app's unwrapped paragraph
    e3 = by_note["Edit 3: opener"]
    assert (e3["block"], e3["find"], e3["replace"]) == (3, "It should be noted that the", "The"), e3
    assert by_note["Edit 4: hedge inside bold"]["find"] == "basically ", by_note["Edit 4: hedge inside bold"]
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == TEXT, "translate changed the document"

    # proposed records ids and the app's own refusals
    reply = write_json(work / "proposed.json",
                       app_reply(read(work / "propose.json"), refuse={1: "find crosses a formatting boundary"}))
    got = review_ok(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    assert got["suggested"] == 3, got
    assert [(r["n"], r["cause"]) for r in got["refused"]] == [(2, "app-refused")], got["refused"]
    assert "formatting boundary" in got["refused"][0]["detail"], got["refused"][0]
    sugg = read_list(work / "suggestions.json")
    assert [(s["n"], s["id"]) for s in sugg] == [(1, "s12"), (3, "s13"), (4, "s14")], sugg
    assert all(set(s) == {"n", "id", "find", "replace"} for s in sugg), sugg
    assert [(r["n"], r["cause"]) for r in read(work / "propose.json")["refused"]] == [(5, "formatting"), (2, "app-refused")]

    # proposed run twice does not duplicate the app's refusals
    review_ok(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    assert [r["n"] for r in read(work / "propose.json")["refused"]] == [5, 2], "a second `proposed` duplicated a refusal"

    # translate run again replaces the payload and drops the old suggestions
    assert (work / "suggestions.json").is_file()
    review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"))
    assert not (work / "suggestions.json").exists(), "a second translate kept the old suggestions"
    assert read(work / "propose.json")["order"] == [1, 2, 3, 4]

    # translate and proposed change neither the document nor T2, and git stays clean
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    assert (repo / "docs" / "s.md").read_bytes() == raw and status(repo) == clean, "the document or git changed"
    assert not any(t.startswith(("log/", "doc/")) for t in t2_titles(REPO_PROJECT)), "translate or proposed stored something"


def test_translate_for_runs_that_send_less_than_the_whole_proposal(prose: Prose, repo: Path) -> None:
    """Sessions that start from other places: not rendered, nothing to send, an author's answer, an unplaced edit."""
    # translate needs a rendered session
    bare_work = Path(brief_ok(prose, "tmpdir").strip())
    proc = run_review(prose, "translate", "--work", str(bare_work), "--blocks-file",
                      str(write_json(bare_work / "blocks.json", app_blocks(TEXT, target_url(repo)))))
    assert proc.returncode == 1 and "render" in proc.stderr, (proc.returncode, proc.stderr)
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None, "a refused translate stored a document record"

    # nothing to send is not an error
    work, _ = translated(prose, repo, edits=[E5])
    assert read(work / "propose.json")["edits"] == [], "the lone formatting edit was sent"
    out = review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"))
    assert out["sent"] == 0 and [r["n"] for r in out["refused"]] == [5], out

    # an answer the author wrote is not sent to the app
    work, _ = start(prose, repo, EDITS, text=TEXT, queries=[{"n": 1, "anchor": "wakes every ten", "text": "Every ten exactly?"}])
    answers = write_json(work / "answers.json", [{"answers": "Q1", "old": "picks the oldest job.",
                                                  "new": "picks the oldest job first."}])
    n = review_ok(prose, "answer", "--work", str(work), "--from-file", str(answers))["added"][0]["n"]
    out = review_ok(prose, "translate", "--work", str(work), "--blocks-file",
                    str(write_json(work / "blocks.json", app_blocks(TEXT, target_url(repo)))))
    assert n not in read(work / "propose.json")["order"] and out["sent"] == 4, (n, out["sent"])

    # an edit render could not place is not sent to the app
    twice = edit(6, "basically ", "", "occurs twice in the file")
    work, out = translated(prose, repo, edits=[*EDITS, twice])
    assert [int(u["n"]) for u in read(work / "session.json")["unplaced"]] == [6]  # setup: render could not place it
    assert 6 not in read(work / "propose.json")["order"] and out["sent"] == 4, out
    assert 6 not in [r["n"] for r in out["refused"]], out["refused"]


def test_a_blocks_reply_is_refused_unless_it_is_the_whole_right_document_unchanged(prose: Prose, repo: Path) -> None:
    """Every malformed get_document reply or page set is refused with a named cause and writes nothing (Gate 1 S2, F3,
    Gate 1c survivors nexus-scmcs.2.10, one --blocks-file for each page nexus-scmcs.2.17 defect 2); then the well-formed
    ones, from the same session, are accepted. The refusals all run first: they leave no propose.json behind."""
    work, _ = start(prose, repo, EDITS, text=TEXT)
    whole = app_blocks(TEXT, target_url(repo))
    blocks = whole["blocks"]
    first, second = two_pages(repo)
    other = str(repo / "docs" / "other.md")

    def without(page: dict, key: str) -> dict:
        return {k: v for k, v in page.items() if k != key}

    broken_block = [*blocks]
    broken_block[1] = {"index": 1}
    twice = [*blocks]
    twice[2] = dict(blocks[1])
    # (label, the file for each page, what stderr must name, whether a traceback is a defect)
    cases: list[tuple[str, list[object], tuple[str, ...], bool]] = [
        ("a last page that still has next", [{**whole, "blocks": blocks[:3], "next": 3}], ("next",), False),
        ("pages whose last page still has next",
         [[{**whole, "blocks": blocks[:3], "next": 3}, {**whole, "blocks": blocks[3:5], "next": 5}]], ("next",), False),
        ("indexes that skip a block", [{**whole, "blocks": blocks[:2] + blocks[3:]}], ("contiguous",), False),
        ("a read that starts after the first block", [{**whole, "blocks": blocks[2:]}], ("contiguous",), False),
        ("pages that leave a gap between them",
         [[{**whole, "blocks": blocks[:2], "next": 2}, {**whole, "blocks": blocks[3:]}]], ("contiguous",), False),
        ("a buffer with unsaved changes", [{**whole, "hasChanges": True}], ("hasChanges",), False),
        ("a second page with unsaved changes",
         [[{**whole, "blocks": blocks[:3], "next": 3}, {**whole, "blocks": blocks[3:], "hasChanges": True}]],
         ("hasChanges",), False),
        ("a reply for another document", [{**whole, "url": other}], ("url",), False),
        ("a reply that names no url", [without(whole, "url")], ("url",), False),
        ("a blocks file that is an empty block list", [{"blocks": []}], ("blocks",), False),
        ("a blocks file that is the source view", [{"view": "source", "blocks": blocks}], ("blocks",), False),
        ("a blocks file with no blocks key", [{"nothing": 1}], ("blocks",), False),
        ("a blocks file that is an empty list", [[]], ("blocks",), False),
        ("a blocks file that is a string", ["text"], ("blocks",), False),
        ("the source view is refused by name even with a url", [{**whole, "view": "source"}], ("source view",), True),
        ("a reply with no blocks and a url is refused by name", [{**whole, "blocks": []}], ("holds no blocks",), True),
        ("a missing url is refused by name", [without(whole, "url")], ("names no `url`",), True),
        ("a block without its fields is refused by name", [{**whole, "blocks": broken_block}], ("needs an integer index",), True),
        ("a block index that appears twice is refused by name", [{**whole, "blocks": twice}], ("appears twice",), True),
        ("pages given out of order", [second, first], ("next",), False),
        ("a first page whose next overlaps the second", [{**first, "next": 2}, second], ("contiguous",), False),
        ("a first page whose next leaves a gap", [{**first, "next": 4}, second], ("contiguous",), False),
        ("a second page that starts after the first page's next",
         [first, {**second, "blocks": second["blocks"][1:]}], ("contiguous",), False),
        ("a page with no next followed by another page", [without(first, "next"), second], ("next",), False),
        ("the same page given twice", [first, first, second], ("contiguous",), False),
        ("a last page file that still has next",
         [first, {**second, "blocks": second["blocks"][:1], "next": 4}], ("next",), False),
        ("the second page is checked for a url that is another document", [first, {**second, "url": other}], ("url",), False),
        ("the second page is checked for a missing url", [first, without(second, "url")], ("url",), False),
        ("the second page is checked for hasChanges", [first, {**second, "hasChanges": True}], ("hasChanges",), False),
        ("the second page is checked for the source view", [first, {**second, "view": "source"}], ("source view",), False),
        ("the first page is checked for hasChanges when the second is clean", [{**first, "hasChanges": True}, second],
         ("hasChanges",), False),
        ("the first page is checked for a url when the second is clean", [{**first, "url": other}, second], ("url",), False),
        ("a second page with a block that has no hash",
         [first, {**second, "blocks": [{"index": 3, "markdown": "x"}, *second["blocks"][1:]]}], ("hash",), False),
        ("a second page with an empty block list", [first, {**second, "blocks": []}], ("holds no blocks",), False),
    ]
    failures = []
    for label, files, needles, named in cases:
        problem = translate_refusal(work, translate_pages(prose, work, *files), needles, named)
        if problem:
            failures.append(f"{label}: {problem}")
    # a file that is not JSON, one outside the work directory, and a page file that is not JSON beside a good one
    (work / "blocks.json").write_text("{not json", encoding="utf-8")
    problem = translate_refusal(work, run_review(prose, "translate", "--work", str(work), "--blocks-file",
                                                 str(work / "blocks.json")), ("blocks",), False)
    if problem:
        failures.append(f"a blocks file that is not JSON: {problem}")
    outside = write_json(prose.tmp / "blocks.json", whole)
    problem = translate_refusal(work, run_review(prose, "translate", "--work", str(work), "--blocks-file", str(outside)),
                                ("work",), False)
    if problem:
        failures.append(f"a blocks file outside the work directory: {problem}")
    outside2 = write_json(prose.tmp / "blocks-2.json", second)
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(write_json(work / "blocks.json", first)),
                      "--blocks-file", str(outside2))
    problem = translate_refusal(work, proc, ("work",), False)
    if problem:
        failures.append(f"a page file outside the work directory: {problem}")
    write_json(work / "blocks.json", first)
    (work / "blocks-2.json").write_text("{not json", encoding="utf-8")
    proc = run_review(prose, "translate", "--work", str(work), "--blocks-file", str(work / "blocks.json"),
                      "--blocks-file", str(work / "blocks-2.json"))
    problem = translate_refusal(work, proc, ("blocks-2.json",), False)
    if problem:
        failures.append(f"an unreadable page file: {problem}")
    assert not failures, "refusals that did not hold:\n" + "\n".join(failures)

    # the well-formed ones, each of which writes propose.json (so they come after every refusal)
    proc = translate_proc(prose, work, [first, second])
    assert proc.returncode == 0 and json.loads(proc.stdout)["sent"] == 4, f"pages of get_document concatenate: {proc.stderr}"
    proc = translate_pages(prose, work, *two_pages(repo))
    assert proc.returncode == 0, f"one blocks file for each page: {proc.stderr}"
    paged = json.loads(proc.stdout)
    whole_out = review_ok(prose, "translate", "--work", str(work), "--blocks-file",
                          str(write_json(work / "blocks.json", whole)))
    assert paged["sent"] == whole_out["sent"] == 4 and paged["edits"] == whole_out["edits"], "paged vs whole"  # edits cross the cut
    assert paged["refused"] == whole_out["refused"], "paged vs whole refusals"
    pages3 = [{**whole, "blocks": blocks[:2], "next": 2}, {**whole, "blocks": blocks[2:5], "next": 5},
              {**whole, "blocks": blocks[5:]}]
    proc = translate_pages(prose, work, *pages3)
    assert proc.returncode == 0 and json.loads(proc.stdout)["sent"] == 4, f"three pages in three files: {proc.stderr}"
    listed = [{**whole, "blocks": blocks[:2], "next": 2}, {**whole, "blocks": blocks[2:4], "next": 4}]
    proc = translate_pages(prose, work, listed, {**whole, "blocks": blocks[4:]})
    assert proc.returncode == 0 and json.loads(proc.stdout)["sent"] == 4, \
        f"a file holding a list of replies beside another file: {proc.stderr}"
    # The app lists read-only blocks (a metadata block) before firstEditable; a read from there is a whole document.
    proc = translate_proc(prose, work, {**whole, "blocks": blocks[1:], "firstEditable": 1})
    assert proc.returncode == 0, f"a read that starts at firstEditable after the first block: {proc.stderr}"
    # macOS reports /private/tmp/... for a session whose target was given as /tmp/...; a symlinked path is the same file.
    link = prose.tmp / "alias"
    link.symlink_to(repo, target_is_directory=True)
    proc = translate_proc(prose, work, app_blocks(TEXT, str(link / "docs" / "s.md")))
    assert proc.returncode == 0, f"the url is compared by realpath: {proc.stderr}"
    assert json.loads(proc.stdout)["sent"] == 4


def test_a_propose_edits_reply_is_refused_unless_it_is_for_the_edits_sent(prose: Prose, repo: Path) -> None:
    """Every malformed propose_edits reply is refused as a named `propose_edits reply` cause and records nothing (Gate 1
    F2, Gate 1c survivors nexus-scmcs.2.10); then a refused result that needs no block is accepted."""
    work, _ = translated(prose, repo)
    before = (work / "propose.json").read_bytes()

    def reply(mutate=None, refuse: dict[int, str] | None = None) -> dict:
        r = app_reply(read(work / "propose.json"), refuse=refuse)
        if mutate:
            mutate(r)
        return r

    def length(r: dict) -> None:
        del r["results"][-1]

    def index0(r: dict) -> None:
        r["results"][-1]["index"] = 0

    def no_id(r: dict) -> None:
        del r["results"][0]["id"]

    def other_block(r: dict) -> None:
        r["results"][2]["block"] += 1

    def no_block(r: dict) -> None:
        del r["results"][0]["block"]

    def one_id(r: dict) -> None:
        r["results"][1]["id"] = r["results"][0]["id"]

    def weird(r: dict) -> None:
        r["results"][1]["status"] = "weird"

    one_id_reply = reply(one_id)
    shared_id = one_id_reply["results"][0]["id"]
    # (label, the reply, what stderr must name, whether propose.json must be byte for byte as it was, traceback is a defect)
    cases: list[tuple[str, object, tuple[str, ...], bool, bool]] = [
        ("a reply of the wrong length", reply(length), ("propose_edits reply",), True, False),
        ("a last result with the wrong index", reply(index0), ("propose_edits reply",), True, False),
        ("a reply that says ok: false", {"ok": False, "error": "no document"}, ("propose_edits reply",), True, False),
        ("a reply that is not an object", ["not", "a", "reply"], ("propose_edits reply",), True, False),
        ("a proposed result without an id", reply(no_id), ("propose_edits reply",), False, False),
        ("a result that echoes another block", reply(other_block), ("propose_edits reply", "block"), True, False),
        ("a proposed result that echoes no block", reply(no_block), ("block",), False, False),
        ("two results with one id", one_id_reply, ("propose_edits reply", shared_id), True, False),
        ("an unknown propose status is refused, not dropped", reply(weird), ("status",), False, True),
    ]
    failures = []
    for label, body, needles, keeps, named in cases:
        proc = proposed_proc(prose, work, body)
        problems = []
        if proc.returncode != 1:
            problems.append(f"exit {proc.returncode}")
        problems += [f"stderr lacks {n!r}: {proc.stderr!r}" for n in needles if n not in proc.stderr]
        if named and "Traceback" in proc.stderr:
            problems.append("a traceback, not a named refusal")
        if (work / "suggestions.json").exists():
            problems.append("suggestions.json was written")
        if keeps and (work / "propose.json").read_bytes() != before:
            problems.append("propose.json changed")
        if problems:
            failures.append(f"{label}: {'; '.join(problems)}")
    # a reply file outside the work directory
    outside = write_json(prose.tmp / "proposed.json", app_reply(read(work / "propose.json")))
    proc = run_review(prose, "proposed", "--work", str(work), "--from-file", str(outside))
    if proc.returncode != 1 or "work" not in proc.stderr or (work / "suggestions.json").exists():
        failures.append(f"a reply file outside the work directory: exit {proc.returncode}, {proc.stderr!r}")
    assert not failures, "refusals that did not hold:\n" + "\n".join(failures)

    refused_only = reply(refuse={1: "no"})
    assert "block" not in refused_only["results"][1], "setup: a refused result carries a block"
    assert proposed_proc(prose, work, refused_only).returncode == 0, "a refused result needs no block"
