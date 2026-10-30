# SPDX-License-Identifier: AGPL-3.0-or-later
"""suggest.py: Markdown edits to MarkupEditor propose_edits entries (nexus-scmcs.2.2, A1).

Pure functions, no substrate. The block shapes are the real get_document ones (fixtures/markupeditor).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUGGEST = ROOT / "prose-edit" / "skills" / "prose-edit" / "scripts" / "suggest.py"
FIXTURE = Path(__file__).parent / "fixtures" / "markupeditor" / "get_document.json"


def _suggest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_suggest", SUGGEST)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def block(index: int, markdown: str, kind: str = "paragraph") -> dict:
    return {"index": index, "kind": kind, "hash": f"h{index}", "markdown": markdown, "warnings": []}


def one(blocks: list[dict], old: str, new: str, reason: str = "r", n: int = 1):
    proposals, refused = _suggest().translate_edits(blocks, [{"n": n, "old": old, "new": new, "reason": reason}])
    return proposals, refused


# ---------------------------------------------------------------------------
# visible(): the rendered text of inline Markdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("md, want", [
    ("The **retry budget is basically fixed** at one", "The retry budget is basically fixed at one"),
    ("__strong__ and *em* and _em2_ and ~~gone~~", "strong and em and em2 and gone"),
    ("snake_case_name stays", "snake_case_name stays"),
    ("use `queue --fast` here", "use queue --fast here"),
    ("see [the docs](http://x.example/a_b) now", "see the docs now"),
    ("an ![alt text](pic.png) image", "an alt text image"),
    ("escaped \\*star\\* and \\_under\\_", "escaped *star* and _under_"),
    ("# Queue notes", "Queue notes"),
    ("## Two", "Two"),
    ("* one\n* two", "one\ntwo"),
    ("1. one\n2. two", "one\ntwo"),
    ("> quoted", "quoted"),
    ("a &amp; b &lt;c&gt;", "a & b <c>"),
    ("a <b>bold</b> tag", "a bold tag"),
    ("2 * 3 * 4 is not emphasis", "2 * 3 * 4 is not emphasis"),
    ("it is “really” simple", "it is “really” simple"),
])
def test_visible_is_the_rendered_text(md: str, want: str) -> None:
    assert _suggest().visible(md) == want


# ---------------------------------------------------------------------------
# translate_edits(): the cases, one per rule
# ---------------------------------------------------------------------------

WRAPPED = block(1, "The queue is basically ordered, and entries leave in the order they arrived.")
BOLD = block(5, "The **retry budget is basically fixed** at one attempt per job.")


@pytest.mark.parametrize("blocks, old, new, find, replace", [
    # a cut: only the changed words are struck, not the sentence
    ([WRAPPED], "is basically ordered, and", "is ordered, and", "basically ", ""),
    # a plain replacement; "order" is also inside "ordered", so the find widens by a whole word
    ([WRAPPED], "entries leave in the order", "entries leave in the sequence", "the order", "the sequence"),
    # a cut inside bold (one run of formatting)
    ([BOLD], "retry budget is basically fixed", "retry budget is fixed", "basically ", ""),
    # curly quotes stay exactly as the document has them in find; replace passes through
    ([block(7, "* The list item says it is “really” simple.", "bullet-list")],
     "it is “really” simple", "it is plain", "“really” simple", "plain"),
    # a table cell: pipes never enter find
    ([block(2, "| a | b |\n| --- | --- |\n| basically ok | x |", "table")],
     "basically ok", "ok", "basically ", ""),
    # a list item inside a whole-list block: markers never enter find
    ([block(3, "* one thing\n* another thing", "bullet-list")], "another thing", "other thing", "another", "other"),
    # a heading
    ([block(0, "# Queue notes", "heading")], "Queue notes", "Queue facts", "notes", "facts"),
    # block Markdown is unwrapped, the old text is wrapped as in the file: whitespace collapses on both sides
    ([WRAPPED], "queue is basically\nordered, and", "queue is\nordered, and", "basically ", ""),
])
def test_translation_cases(blocks: list[dict], old: str, new: str, find: str, replace: str) -> None:
    proposals, refused = one(blocks, old, new, reason="hedge", n=4)
    assert refused == []
    assert len(proposals) == 1
    p = proposals[0]
    assert (p["find"], p["replace"]) == (find, replace)
    assert p["n"] == 4 and p["note"] == "Edit 4: hedge"
    located = next(b for b in blocks if find.replace("“", "").split(" ")[0] in b["markdown"])
    assert p["block"] == located["index"] and p["expected"] == located["hash"]


def test_widening_goes_by_whole_words_until_the_find_is_unique() -> None:
    b = block(1, "It is fine. It is fine, really.")
    proposals, refused = one([b], "It is fine, really.", "It is great, really.")
    assert refused == []
    assert (proposals[0]["find"], proposals[0]["replace"]) == ("is fine,", "is great,")
    assert proposals[0]["find"] in b["markdown"] and b["markdown"].count(proposals[0]["find"]) == 1


def test_a_pure_insertion_anchors_on_a_neighbouring_word() -> None:
    b = block(4, "The scheduler just wakes every ten seconds and picks the oldest job.")
    proposals, refused = one([b], "wakes every ten", "wakes about every ten")
    assert refused == []
    p = proposals[0]
    assert b["markdown"].count(p["find"]) == 1
    assert b["markdown"].replace(p["find"], p["replace"]) == (
        "The scheduler just wakes about every ten seconds and picks the oldest job.")


def test_a_pure_deletion_replaces_with_nothing() -> None:
    proposals, _ = one([WRAPPED], "is basically ordered", "is ordered")
    assert proposals[0]["replace"] == ""


@pytest.mark.parametrize("blocks, old, new, cause", [
    ([WRAPPED], "this text is nowhere", "that text", "not-found"),
    ([block(1, "Same words here."), block(2, "Same words here, again.")], "Same words here", "Other words here", "ambiguous"),
    ([block(1, "It is. It is.")], "It is.", "It was.", "ambiguous"),
    # the new text carries Markdown formatting: the app cannot add formatting
    ([WRAPPED], "is basically ordered", "is **ordered**", "formatting"),
    ([WRAPPED], "is basically ordered", "is `ordered`", "formatting"),
    ([WRAPPED], "is basically ordered", "is [ordered](x)", "formatting"),
    # the changed text includes Markdown syntax (it crosses the end of a bold run)
    ([BOLD], "is basically fixed** at one attempt", "is basically fixed. One attempt", "formatting"),
    ([block(6, "```bash\nworker --retries 1\n```", "code")], "worker --retries 1", "worker --retries 2", "code-block"),
    ([block(0, "title: x", "metadata")], "title: x", "title: y", "metadata"),
    ([WRAPPED], "ordered,\n\nand", "ordered, and", "spans-lines"),
    ([WRAPPED], "is basically ordered", "is basically  ordered", "no-change"),
])
def test_refusals_never_guess(blocks: list[dict], old: str, new: str, cause: str) -> None:
    proposals, refused = one(blocks, old, new, n=9)
    assert proposals == []
    assert [(r["n"], r["cause"]) for r in refused] == [(9, cause)]
    assert refused[0]["detail"]


def test_a_find_that_would_have_to_cross_a_formatting_run_is_refused() -> None:
    # "job" is in plain text and inside bold: the only unique find lies across the run boundary
    b = block(1, "Retry the job and **retry the job** again.")
    proposals, refused = one([b], "the job and **retry the job** again", "the job and **retry the task** again")
    # the core "job" -> "task" sits in the bold run; "the job" occurs twice, "retry the job" once and stays in bold
    assert refused == [] and proposals[0]["find"] == "retry the job" and proposals[0]["replace"] == "retry the task"
    # plain-text twin of the bold text: no widening inside the run makes it unique
    b2 = block(2, "Fix **a** or a.")
    proposals2, refused2 = one([b2], "**a** or a.", "**b** or a.")
    assert proposals2 == [] and refused2[0]["cause"] in ("formatting", "ambiguous")


def test_several_edits_keep_call_order_and_refusals_are_listed_separately() -> None:
    blocks = [WRAPPED, BOLD]
    edits = [
        {"n": 2, "old": "retry budget is basically fixed", "new": "retry budget is fixed", "reason": "b"},
        {"n": 1, "old": "nowhere to be found", "new": "x", "reason": "a"},
        {"n": 3, "old": "is basically ordered", "new": "is ordered", "reason": "c"},
    ]
    proposals, refused = _suggest().translate_edits(blocks, edits)
    assert [p["n"] for p in proposals] == [2, 3]
    assert [r["n"] for r in refused] == [1]


def test_the_real_fixture_blocks_translate() -> None:
    blocks = json.loads(FIXTURE.read_text(encoding="utf-8"))["blocks"]
    edits = [
        {"n": 1, "old": "The queue is basically ordered, and", "new": "The queue is ordered, and", "reason": "hedge"},
        {"n": 2, "old": "worker really quite simply retries", "new": "worker retries", "reason": "hedge"},
        {"n": 3, "old": "retry budget is basically fixed", "new": "retry budget is fixed", "reason": "inside bold"},
        {"n": 4, "old": "worker --retries 1", "new": "worker --retries 2", "reason": "code"},
        {"n": 5, "old": "it is “really” simple", "new": "it is plainly simple", "reason": "curly"},
        {"n": 6, "old": "It should be noted that the retry path", "new": "The retry path", "reason": "opener"},
    ]
    proposals, refused = _suggest().translate_edits(blocks, edits)
    by_n = {p["n"]: p for p in proposals}
    assert sorted(by_n) == [1, 2, 3, 5, 6]
    assert [(r["n"], r["cause"]) for r in refused] == [(4, "code-block")]
    assert by_n[1]["block"] == 1 and by_n[1]["expected"] == blocks[1]["hash"]
    assert by_n[3]["block"] == 5 and by_n[3]["find"] == "basically "
    assert by_n[5]["find"].startswith("“") and by_n[5]["block"] == 7
    for p in proposals:
        vis = _suggest().visible(blocks[p["block"]]["markdown"])
        assert vis.count(p["find"]) == 1, p
        assert set(p) == {"n", "block", "expected", "find", "replace", "note"}
