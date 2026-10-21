# SPDX-License-Identifier: AGPL-3.0-or-later
"""The marked-up copy opens with an index of anchor links (nexus-ger02.27, Sam's decision).

A long document's copy is hard to move around in: Typora's rendered view shows no line numbers, so the copy
carries real Markdown anchors. Each proposal site (an edit mark, a paragraph note, a query note, or the block
listing a proposal that could not be placed) holds an explicit `<a id>`, and an index right after the header
links to each with a one-line gist. There is no changes-only copy.
"""
from __future__ import annotations

import re

from tests.prose_edit.test_brief import edit, proposal
from tests.prose_edit.test_review import DOC, E1, E2, HTML, _review

PARAS = [{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Third paragraph"',
          "advice": "It restates itself."}]
QUERIES = [{"n": 1, "anchor": "queue drains in order", "text": "Which queue?"}]


def _copy(prop: dict, text: str = DOC, **kw) -> str:
    kw = {"label": "docs/s.md", "genre": "rdr", "rng": None, "html": False, "stdin": False, **kw}
    return _review().build_copy(text, prop, **kw)


def _index(copy: str) -> list[str]:
    """The index's link lines: those after the `## Index` heading, up to the next blank-line break."""
    lines = copy.splitlines()
    start = lines.index("## Index")
    out: list[str] = []
    for ln in lines[start + 1:]:
        if ln.startswith("- "):
            out.append(ln)
        elif out:
            break
    return out


def _full() -> str:
    return _copy(proposal([E1, E2], paragraphs=PARAS, queries=QUERIES))


def test_the_copy_opens_with_an_index_before_the_note_and_the_document() -> None:
    copy = _full()
    lines = copy.splitlines()
    assert lines[0] == "# Line edit: docs/s.md"
    at = lines.index("## Index")
    answer = next(i for i, ln in enumerate(lines) if ln.startswith("Answer with the numbers to accept"))
    note = next(i for i, ln in enumerate(lines) if ln.startswith("> **Editor's note.**"))
    assert answer < at < note < next(i for i, ln in enumerate(lines) if "First paragraph is plain." in ln)


def test_the_index_has_one_link_per_proposal_each_with_a_one_line_gist() -> None:
    copy = _full()
    rows = _index(copy)
    assert [re.match(r"- \[([EPQ]\d+)\]\(#([epq]\d+)\) (.+)", r).group(1, 2) for r in rows] == [
        ("E1", "e1"), ("E2", "e2"), ("P1", "p1"), ("Q1", "q1")]
    gists = [re.match(r"- \[[^\]]+\]\(#[^)]+\) (.+)", r).group(1) for r in rows]
    assert "filler" in gists[0] and "filler opener" in gists[1]
    assert "cut" in gists[2] and "It restates itself." in gists[2]
    assert "Which queue?" in gists[3]
    assert all("\n" not in g and g.strip() for g in gists)


def test_every_index_link_has_exactly_one_matching_anchor_and_no_anchor_is_orphaned() -> None:
    copy = _full()
    targets = re.findall(r"\]\(#([a-z]\d+)\)", copy)
    anchors = re.findall(r'<a id="([a-z]\d+)"[^>]*></a>', copy)
    assert sorted(targets) == sorted(anchors) == ["e1", "e2", "p1", "q1"]


def test_each_anchor_sits_at_its_proposal_site() -> None:
    copy = _full()
    body = copy.split("\n---\n")[1]
    e1 = body.index('<a id="e1"')
    assert e1 < body.index("<sup>1</sup>") and body[e1:body.index("<sup>1</sup>")].count("<del>") == 1
    assert body.index('<a id="e2"') < body.index("<sup>2</sup>") and e1 < body.index('<a id="e2"')
    p = next(ln for ln in body.splitlines() if "[P1]" in ln)
    assert p.startswith("> ") and '<a id="p1"' in p  # inside the quoted note, so a viewer still shows the quote
    q = next(ln for ln in body.splitlines() if "[Q1]" in ln)
    assert q.startswith("> ") and '<a id="q1"' in q
    assert body.index("Second paragraph") < body.index('<a id="q1"') < body.index("Third paragraph repeats")


def test_a_proposal_that_cannot_be_placed_keeps_its_link_and_anchors_in_its_listing() -> None:
    dup = edit(3, "Third paragraph repeats itself.", "Third repeats.")
    copy = _copy(proposal([E1, dup]))
    assert any("(#e3)" in r for r in _index(copy))
    listing = next(ln for ln in copy.splitlines() if "[E3]" in ln and "Cannot be placed" in ln)
    assert listing.startswith("> ") and '<a id="e3"' in listing
    assert copy.count('<a id="e3"') == 1


def test_a_dropped_proposal_is_not_in_the_index_and_has_no_anchor() -> None:
    copy = _copy(proposal([E2], dropped=[{"n": 1, "old": "x", "cause": "rejected"}]))
    assert [r[:12] for r in _index(copy)] == ["- [E2](#e2) "]
    assert 'id="e1"' not in copy


def test_a_copy_with_no_proposal_has_no_index() -> None:
    copy = _copy(proposal([]))
    assert "## Index" not in copy and "<a id" not in copy


def test_a_long_gist_is_cut_to_one_short_line() -> None:
    long = edit(1, "It really is quite simple.", "It is simple.", "word " * 80 + "\nsecond line")
    row = _index(_copy(proposal([long])))[0]
    assert "\n" not in row and len(row) < 160 and row.endswith("…")


def test_the_html_copy_has_the_same_index_and_anchors_in_its_excerpts() -> None:
    prop = proposal(
        [edit(1, "It really is quite simple.", "It is simple.", "filler"),
         edit(2, "basically nothing", "nothing", "filler")],
        paragraphs=[{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Second paragraph links"',
                     "advice": "It says nothing."}],
        queries=[{"n": 1, "anchor": "Fourth paragraph closes", "text": "Closes what?"}])
    copy = _copy(prop, HTML, label="web/page.html", genre="exploration-essay", html=True)
    assert len(_index(copy)) == 4
    targets = re.findall(r"\]\(#([a-z]\d+)\)", copy)
    anchors = re.findall(r'<a id="([a-z]\d+)"[^>]*></a>', copy)
    assert sorted(targets) == sorted(anchors) == ["e1", "e2", "p1", "q1"]
    assert 'href' not in copy  # the page's own links are still flattened away; only id anchors remain


def _one_anchor_each(copy: str, ids: list[str]) -> None:
    for i in ids:
        assert f"(#{i})" in "\n".join(_index(copy)), i
        assert len(re.findall(rf'<a id="{i}"[^>]*></a>', copy)) == 1, i


def test_an_unplaced_paragraph_note_and_query_keep_their_links_and_anchors() -> None:
    """A paragraph proposal whose quoted phrase is not in the text, and a query whose anchor is not, are
    listed rather than placed; each listing carries the anchor its index link points to."""
    copy = _copy(proposal(
        [E1],
        paragraphs=[{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "No such opening"',
                     "advice": "Gone."}],
        queries=[{"n": 1, "anchor": "no such anchor text", "text": "Where?"}]))
    _one_anchor_each(copy, ["e1", "p1", "q1"])
    for tag in ("[P1]", "[Q1]"):
        listing = next(ln for ln in copy.splitlines() if tag in ln and ln.startswith("> "))
        assert listing.startswith("> ") and f'<a id="{tag[1:3].lower()}"' in listing


def test_an_html_note_outside_every_section_keeps_its_link_and_anchor() -> None:
    """A query anchored in text no section of the page holds (here, a script body) goes to the
    'Not in any section' listing, which still carries its anchor."""
    prop = proposal([edit(1, "It really is quite simple.", "It is simple.", "filler")],
                    queries=[{"n": 1, "anchor": "goatcounter", "text": "Why here?"}])
    copy = _copy(prop, HTML, label="web/page.html", genre="how-to", html=True)
    lost = next(ln for ln in copy.splitlines() if "Not in any section" in ln)
    assert "[Q1]" in lost and '<a id="q1"' in lost
    _one_anchor_each(copy, ["e1", "q1"])


def test_an_empty_gist_falls_back_to_the_proposal_kind() -> None:
    copy = _copy(proposal(
        [edit(1, "It really is quite simple.", "It is simple.", "   ")],
        paragraphs=[{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Third paragraph"', "advice": ""}],
        queries=[{"n": 1, "anchor": "queue drains in order", "text": ""}]))
    assert _index(copy) == ["- [E1](#e1) edit", "- [P1](#p1) cut", "- [Q1](#q1) query"]


def test_a_gist_shows_markup_in_the_reason_as_text() -> None:
    raw = edit(1, "It really is quite simple.", "It is simple.", "a [bracketed] `tick` <del>raw</del> *star*")
    row = _index(_copy(proposal([raw])))[0]
    assert row == r"- [E1](#e1) a \[bracketed\] \`tick\` &lt;del>raw&lt;/del> \*star\*"
