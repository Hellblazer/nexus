# SPDX-License-Identifier: AGPL-3.0-or-later
"""suggest.py against the app's real serializer output (Gate 1 S1 and S3, nexus-scmcs.2.8).

fixtures/markupeditor/get_document_serializer.json is what MarkupEditor's get_document returned for
serializer-source.md, and propose_edits_serializer.json holds the edits the app was sent for it and its reply.
`old` and `new` are written in the file's own syntax, so a pair is producible only if translate finds `old` by the
text a reader sees and not by the file's Markdown.
"""
from __future__ import annotations

import json

import pytest

from tests.prose_edit.test_markup_translate import FIXTURE, _suggest

FIXTURES = FIXTURE.parent
SOURCE = (FIXTURES / "serializer-source.md").read_text(encoding="utf-8")
DOC = json.loads((FIXTURES / "get_document_serializer.json").read_text(encoding="utf-8"))
SENT = json.loads((FIXTURES / "propose_edits_serializer.json").read_text(encoding="utf-8"))
BLOCKS = DOC["blocks"]


def parsed(block: dict):
    return _suggest().Parsed(block["markdown"], block["kind"])


def shown(block: dict, find: str, replace: str) -> str:
    """The block's visible text after the edit; the find must occur exactly once, as the app requires."""
    vis = parsed(block).vis
    assert vis.count(find) == 1, (find, vis)
    return vis.replace(find, replace)


# request index in propose_edits_serializer.json -> (old, new) written in serializer-source.md's syntax
PAIRS: dict[int, tuple[str, str]] = {
    0: ("per [sic] the notes", "per the notes"),
    1: ("When a < b and c", "When a below b and c"),
    2: ("uses a ~tilde~ and", "uses a tilde and"),
    3: ("Tom &amp; Jerry use", "Tom and Jerry use"),
    4: ("use \\* as a bullet", "use + as a bullet"),
    5: ("Second item, plainly.", "Second item."),
    6: ("that is basically fine.", "that is fine."),
    7: ("basically first", "first"),
    8: ("at the end\\\nand its continuation", "at the end, and its continuation"),  # across the hard break
    9: ("See [the docs][d] and the", "See [the manual][d] and the"),
    10: ("is _really_ quite slow", "is _truly_ quite slow"),
}
ACCEPTED = [i for i, r in enumerate(SENT["reply"]["results"]) if r["status"] == "proposed"]


def edit(i: int) -> dict:
    return {"n": i + 1, "old": PAIRS[i][0], "new": PAIRS[i][1], "reason": "r"}


def test_the_pairs_are_written_in_the_files_syntax() -> None:
    for old, _new in PAIRS.values():
        assert old in SOURCE, old


@pytest.mark.parametrize("i", ACCEPTED)
def test_an_edit_the_app_accepted_is_producible_from_the_files_own_syntax(i: int) -> None:
    req = SENT["request"][i]
    proposals, refused = _suggest().translate_edits(BLOCKS, [edit(i)])
    assert refused == [], refused
    (p,) = proposals
    block = BLOCKS[req["block"]]
    assert (p["block"], p["expected"]) == (req["block"], req["expected"])
    # not necessarily the same find (widening differs), but the same visible text afterwards
    assert shown(block, p["find"], p["replace"]) == shown(block, req["find"], req["replace"])


def test_the_edit_the_app_refused_across_a_hard_break_is_refused_locally() -> None:
    i = next(i for i, r in enumerate(SENT["reply"]["results"]) if r["status"] == "refused")
    assert i == 8
    proposals, refused = _suggest().translate_edits(BLOCKS, [edit(i)])
    assert proposals == []
    assert [(r["n"], r["cause"]) for r in refused] == [(i + 1, "spans-lines")]
    assert "hard" in refused[0]["detail"]


def test_a_strong_run_written_with_underscores_is_found_by_its_text() -> None:
    proposals, refused = _suggest().translate_edits(BLOCKS, [{"n": 1, "old": "and __very__ old", "new": "and __rather__ old", "reason": "r"}])
    assert refused == []
    assert (proposals[0]["find"], proposals[0]["replace"]) == ("very", "rather")


def test_an_old_whose_changed_text_is_a_whole_entity_is_found_and_replaced() -> None:
    proposals, refused = _suggest().translate_edits(BLOCKS, [{"n": 1, "old": "Tom &amp; Jerry", "new": "Tom and Jerry", "reason": "r"}])
    assert refused == []
    assert (proposals[0]["find"], proposals[0]["replace"]) == ("&", "and")


def test_an_old_that_is_in_two_blocks_by_its_text_is_ambiguous() -> None:
    # "basically" is plain in the quote and in the table cell; the file's syntax does not tell them apart
    proposals, refused = _suggest().translate_edits(BLOCKS, [{"n": 1, "old": "basically", "new": "mostly", "reason": "r"}])
    assert proposals == [] and refused[0]["cause"] == "ambiguous"


# ---------------------------------------------------------------------------
# S3: the model of the app's find rules, over real output
# ---------------------------------------------------------------------------


def occurrences(text: str, needle: str) -> int:
    return sum(1 for i in range(len(text)) if text.startswith(needle, i))


def one_run(block: dict, find: str) -> bool:
    p = parsed(block)
    at = p.vis.find(find)
    return len({*p.ctx[at:at + len(find)]}) <= 1


def test_the_visible_text_agrees_with_every_find_the_app_accepted() -> None:
    """The model behind translate: each find the app took occurs once in visible(block) and lies in one run."""
    for i in ACCEPTED:
        req = SENT["request"][i]
        block = BLOCKS[req["block"]]
        assert occurrences(parsed(block).vis, req["find"]) == 1, req
        assert one_run(block, req["find"]), req
    refused = SENT["request"][8]
    assert occurrences(parsed(BLOCKS[refused["block"]]).vis, refused["find"]) == 0


def test_every_proposal_over_the_real_documents_finds_once_in_one_run() -> None:
    proposals, refused = _suggest().translate_edits(BLOCKS, [edit(i) for i in sorted(PAIRS)])
    assert sorted(p["n"] for p in proposals) == [i + 1 for i in ACCEPTED]  # non-vacuity: 10 sent, the break refused
    assert [r["n"] for r in refused] == [9]
    for p in proposals:
        block = BLOCKS[p["block"]]
        assert occurrences(parsed(block).vis, p["find"]) == 1, p
        assert one_run(block, p["find"]), p
    older = json.loads(FIXTURE.read_text(encoding="utf-8"))["blocks"]
    edits = [
        {"n": 1, "old": "The queue is basically ordered, and", "new": "The queue is ordered, and", "reason": "hedge"},
        {"n": 2, "old": "worker really quite simply retries", "new": "worker retries", "reason": "hedge"},
        {"n": 3, "old": "retry budget is basically fixed", "new": "retry budget is fixed", "reason": "inside bold"},
        {"n": 5, "old": "it is “really” simple", "new": "it is plainly simple", "reason": "curly"},
        {"n": 6, "old": "It should be noted that the retry path", "new": "The retry path", "reason": "opener"},
    ]
    props, _ = _suggest().translate_edits(older, edits)
    assert len(props) == 5
    for p in props:
        block = older[p["block"]]
        p2 = _suggest().Parsed(block["markdown"], block["kind"])
        at = p2.vis.find(p["find"])
        assert occurrences(p2.vis, p["find"]) == 1 and len({*p2.ctx[at:at + len(p["find"])]}) <= 1, p
