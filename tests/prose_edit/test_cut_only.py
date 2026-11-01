# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cut-only mode (RDR-221 Step 3.1, nexus-ger02.10): grammar, brief, filter, review, and the skills that carry it.

Pure pieces are imported and called. What touches T2 runs brief.py and review.py as subprocesses against the real
engine substrate (the `prose` fixture), with the project prefix override.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, ROOT, Prose, t2_json
from tests.prose_edit.test_brief import (
    AGENT,
    PLUGIN_SKILL,
    SKILL,
    _frontmatter,
    _module,
    brief_ok,
    edit,
    fenced,
    parse,
    proposal,
    run_brief,
)
from tests.prose_edit.test_review import _review, review_ok, run_review

CUT_SKILL = ROOT / "prose-edit" / "skills" / "prose-cut" / "SKILL.md"
README = ROOT / "prose-edit" / "README.md"
SITE_PAGE = ROOT / ".claude" / "skills" / "site-page" / "SKILL.md"
PAGE_FIXTURE = Path(__file__).parent / "fixtures" / "cut-only-page.html"
CARD = "First person plural, plain register. Refrain: the closing line of each section."


def _card(tmp_path: Path, text: str = CARD) -> Path:
    path = tmp_path / "card.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _cut_args(card: Path, words: int = 300, *more: str) -> list[str]:
    return ["--mode", "cut-only", "--voice-card", str(card), "--word-budget", str(words), *more]


# ---------------------------------------------------------------------------
# Invocation grammar
# ---------------------------------------------------------------------------


def test_a_cut_only_invocation_parses_with_its_card_and_word_budget(tmp_path: Path) -> None:
    card = _card(tmp_path)
    got = parse("web/p.html", *_cut_args(card, 300, "--genre", "how-to", "--budget", "20"))
    assert got == {
        "mode": "cut-only", "path": "web/p.html", "range": None, "stdin": False, "genre": "how-to",
        "budget": 20, "word_budget": 300, "voice_card_file": str(card), "target": "web/p.html",
    }
    assert parse("web/p.html", *_cut_args(card))["budget"] == 10  # the edit-run default


def test_cut_only_flags_take_either_form_in_any_order_and_a_range(tmp_path: Path) -> None:
    card = _card(tmp_path)
    a = parse("web/p.html:5-40", "--word-budget=250", f"--voice-card={card}", "--mode=cut-only", "--genre=how-to")
    b = parse("--genre", "how-to", "--mode", "cut-only", "--voice-card", str(card), "--word-budget", "250",
              "web/p.html:5-40")
    assert a == b
    assert a["range"] == {"start": 5, "end": 40} and a["target"] == "web/p.html:5-40" and a["word_budget"] == 250


def test_a_relative_card_path_is_made_absolute_against_the_session_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _card(tmp_path)
    monkeypatch.chdir(tmp_path)
    got = parse("p.md", *_cut_args(Path("card.txt")))
    assert os.path.isabs(got["voice_card_file"]) and Path(got["voice_card_file"]).samefile(tmp_path / "card.txt")


@pytest.mark.parametrize(
    ("make", "fragment"),
    [
        (lambda c: ["p.md", "--mode", "rewrite", "--voice-card", str(c), "--word-budget", "9"], "cut-only"),
        (lambda c: ["p.md", "--voice-card", str(c)], "--mode cut-only"),
        (lambda c: ["p.md", "--word-budget", "9"], "--mode cut-only"),
        (lambda c: ["p.md", "--mode", "cut-only", "--word-budget", "9"], "--voice-card"),
        (lambda c: ["p.md", "--mode", "cut-only", "--voice-card", str(c)], "--word-budget"),
        (lambda c: ["p.md", "--mode", "cut-only", "--voice-card", str(c), "--word-budget", "0"], "--word-budget"),
        (lambda c: ["p.md", "--mode", "cut-only", "--voice-card", str(c), "--word-budget", "x"], "--word-budget"),
        (lambda c: ["p.md", "--mode", "cut-only", "--voice-card", str(c), "--word-budget", "-3"], "--word-budget"),
        (lambda c: ["p.md", "--mode", "cut-only", "--voice-card", str(c), "--word-budget", "9", "--mode", "cut-only"],
         "more than once"),
        (lambda c: ["-", "--mode", "cut-only", "--voice-card", str(c), "--word-budget", "9", "--genre", "how-to"],
         "stdin"),
        (lambda c: ["p.md", "--mode", "cut-only", "--voice-card", str(c.parent / "absent.txt"), "--word-budget", "9"],
         "voice card"),
        (lambda c: ["p.md", "--mode", "cut-only", "--voice-card", str(c.parent), "--word-budget", "9"],
         "voice card"),
    ],
)
def test_a_malformed_cut_only_invocation_is_refused_with_a_message(
    tmp_path: Path, make: object, fragment: str
) -> None:
    mod = _module()
    tokens = make(_card(tmp_path))  # type: ignore[operator]
    with pytest.raises(mod.UserError) as caught:
        mod.parse_invocation(tokens)
    assert fragment in str(caught.value)


def test_a_card_that_is_empty_not_utf8_or_over_the_limit_is_refused(tmp_path: Path) -> None:
    mod = _module()
    blank = tmp_path / "blank.txt"
    blank.write_text("  \n \n", encoding="utf-8")
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe\x00 not utf-8 \xc3\x28")
    long = tmp_path / "long.txt"
    long.write_text("x" * (mod._MEM.VOICE_CARD_MAX + 1), encoding="utf-8")
    exact = tmp_path / "exact.txt"
    exact.write_text("x" * mod._MEM.VOICE_CARD_MAX, encoding="utf-8")
    for card, fragment in ((blank, "empty"), (bad, "UTF-8"), (long, str(mod._MEM.VOICE_CARD_MAX))):
        with pytest.raises(mod.UserError) as caught:
            mod.parse_invocation(["p.md", *_cut_args(card)])
        assert fragment in str(caught.value), card.name
    assert mod.parse_invocation(["p.md", *_cut_args(exact)])["mode"] == "cut-only"


@pytest.mark.parametrize("tokens", [
    ["rejections", "a.md", "--mode", "cut-only"],
    ["rejections", "a.md", "--word-budget", "5"],
    ["exemplar", "changelog", "a.md:1-2", "--voice-card", "f"],
])
def test_the_other_verbs_take_no_cut_only_flag(tokens: list[str]) -> None:
    mod = _module()
    with pytest.raises(mod.UserError) as caught:
        mod.parse_invocation(tokens)
    assert "unknown flag" in str(caught.value)


def test_decide_parses_the_callers_decisions_into_apply_arguments() -> None:
    got = parse("decide", "/tmp/prose-edit-ab12cd34", "--accept", "1, 3-5", "--reject", "2", "--hold", "6",
                "--reasons-file", "/tmp/prose-edit-ab12cd34/reasons.json")
    assert got == {
        "mode": "decide", "work": "/tmp/prose-edit-ab12cd34",
        "apply_argv": ["--work", "/tmp/prose-edit-ab12cd34", "--accept", "1, 3-5", "--reject", "2", "--hold", "6",
                       "--reasons-file", "/tmp/prose-edit-ab12cd34/reasons.json"],
    }
    none = parse("decide", "/tmp/prose-edit-ab12cd34", "--accept", "none")
    assert none["apply_argv"] == ["--work", "/tmp/prose-edit-ab12cd34", "--accept", "none"]


@pytest.mark.parametrize(("tokens", "fragment"), [
    (["decide"], "work"),
    (["decide", "/tmp/w"], "--accept"),  # decide is mandatory: --accept none says nothing was accepted
    (["decide", "/tmp/w", "--reject", "1"], "--accept"),
    (["decide", "/tmp/w", "/tmp/v", "--accept", "1"], "one work"),
    (["decide", "/tmp/w", "--accept", "1", "--bogus", "x"], "unknown flag"),
    (["decide", "/tmp/w", "--accept", "1", "--budget", "3"], "unknown flag"),
])
def test_a_malformed_decide_is_refused(tokens: list[str], fragment: str) -> None:
    mod = _module()
    with pytest.raises(mod.UserError) as caught:
        mod.parse_invocation(tokens)
    assert fragment in str(caught.value)


def test_a_file_named_decide_is_reached_with_dot_slash() -> None:
    assert parse("./decide")["mode"] == "edit" and parse("--", "decide")["path"] == "decide"


# ---------------------------------------------------------------------------
# removes_words_only: the one rule the filter and apply share
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("old", "new"), [
    ("The queue really drains.", "The queue drains."),
    ("A whole sentence goes.", ""),
    ("A whole sentence goes.", "  \n "),
    ("It is basically, in fact, ordered.", "It is ordered."),     # punctuation may change
    ("The Queue really drains.", "the queue drains."),             # so may capitalisation
    ("Tom &amp; Jerry really run.", "Tom &amp; Jerry run."),       # an entity is not a word
    ("well-known tools really help", "well-known tools help"),
    ("they don’t really go", "they don’t go"),
])
def test_a_cut_removes_words_and_adds_none(old: str, new: str) -> None:
    assert _module().removes_words_only(old, new) is True


@pytest.mark.parametrize(("old", "new"), [
    ("The queue drains.", "The queue drains."),                    # nothing removed
    ("The queue drains.", "the queue drains"),                     # only case and punctuation
    ("It is ordered.", "It is, ordered."),
    ("The queue drains.", "The queue always drains."),             # a word added
    ("The queue drains.", "The queue empties."),                   # a word replaced
    ("one two three", "three two"),                                # reordered
    ("The queue drains.", "The queues drain."),                    # inflected
    ("don't go", "dont go"),
    ("Run it now", "Run it now please"),
    ("x", "y"),
])
def test_an_edit_that_adds_replaces_or_reorders_a_word_is_not_a_cut(old: str, new: str) -> None:
    assert _module().removes_words_only(old, new) is False


# ---------------------------------------------------------------------------
# The prose word count
# ---------------------------------------------------------------------------


def test_the_word_count_skips_protected_spans_and_markup_in_markdown() -> None:
    mod = _module()
    text = ("Intro words here.\n\n```\ncode words here\n```\n\n> quoted words\n\n"
            "A [link text](https://example.invalid/some/path) sits here.\n\nLast one.\n")
    assert mod.prose_word_count(text, html=False) == 3 + 5 + 2  # intro; "A link text sits here"; last one
    assert mod.prose_word_count("Before <b>bold</b> after &amp; more", html=False) == 4  # tags and entities


def test_the_word_count_of_an_html_page_skips_every_excluded_element() -> None:
    mod = _module()
    text = PAGE_FIXTURE.read_text(encoding="utf-8")
    assert mod.prose_word_count(text, html=True) == 45
    # the same text read as Markdown protects fewer elements (no nav, headings, button or samp), so it counts more
    assert mod.prose_word_count(text, html=False) > 45


def test_the_word_count_of_a_range_counts_that_window_only() -> None:
    mod = _module()
    text = "one two\n\nthree four five\n\nsix\n"
    assert mod.prose_word_count(text, html=False) == 6
    assert mod.prose_word_count(text, html=False, window=mod._range_span(text, {"start": 3, "end": 3})) == 3


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------

_READ = {"genre": "how-to", "merged": {}, "path": "web/p.html", "root": "/r", "layers": {}, "genre_record": None,
         "range": None}


def _brief(cut: dict | None = None, genre: str = "how-to", budget: int = 20) -> str:
    return _module().render_brief({**_READ, "genre": genre}, budget, None, None,
                                  ["New prose: all lines (the file is not tracked)."], cut)


CUT = {"card": CARD, "word_budget": 300, "prose_words": 412, "html": True}


def test_the_normal_brief_never_says_cut_only() -> None:
    for genre in ("how-to", "reference-doc", "changelog"):
        assert "cut-only" not in _brief(None, genre).lower(), genre
    assert "## 6. Cut redundancy, unpack compression" in _brief(None)


def test_a_cut_only_brief_names_the_mode_and_turns_section_6_into_removal_only() -> None:
    brief = _brief(CUT)
    lines = brief.splitlines()
    assert lines[:4] == ["# Editing brief", "", "Genre: how-to", "Mode: cut-only"]
    section6 = brief[brief.index("## 6. "):]
    assert section6.startswith("## 6. Cut-only")
    for needle in ("Every edit only removes words", "No edit adds markup", "Paragraph proposals are cuts only",
                   "Raise no queries", "Never cut a voice-card device"):
        assert needle in section6, needle
    assert "unpack" not in section6.lower() and "add words" not in section6.lower()
    assert "may add words" not in brief  # the lengthening clause is off whatever the genre
    assert "Cut redundancy, unpack compression" not in brief
    assert "may add words when that makes a sentence parse" not in _brief(CUT, "rdr")


def test_a_cut_only_brief_states_the_prose_word_count_and_the_target() -> None:
    section5 = _brief(CUT)
    section5 = section5[section5.index("## 5. Budget"):section5.index("## 6. ")]
    assert "Propose at most 20 sentence edits" in section5
    assert "412 words" in section5 and "300 words" in section5
    assert "target" in section5 and "not a limit" in section5
    assert "earned" in section5


def test_the_given_voice_card_is_shown_between_the_card_tags_as_the_anchor() -> None:
    brief = _brief(CUT)
    section2 = brief[brief.index("## 2. Voice card"):brief.index("## 3. Style sheet")]
    assert f"\n<voice_card>\n{CARD}\n</voice_card>\n" in section2
    assert "Return it unchanged" in section2
    assert "approved this voice card for this document on" not in section2  # the stored-card sentence
    assert "Before proposing any edit, write the voice card" not in section2


def test_an_html_target_gets_the_raw_source_paragraph_and_a_markdown_one_does_not() -> None:
    html = _brief(CUT)
    assert "raw source" in html and "entities as written" in html and "never contains or touches a tag" in html
    assert "raw source" not in _brief({**CUT, "html": False})


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------

# The elements a how-to page must not be cut in, each with a sentence that holds one cuttable word. The first
# three sit in the head; the rest are the elements brief.py protects for html, the new ones (nav, headings, button,
# samp) beside the old (pre, code, table, blockquote, svg, comment, kbd).
EXCLUDED = {
    "title": "Head title words really stay", "style": "style words really stay",
    "script": "script words really stay", "nav": "Nav link words really stay",
    "h1": "Heading one words really stay", "h2": "Heading two words really stay",
    "pre": "shell command words really stay", "code": "inline code words really stay",
    "table": "Table cell words really stay", "blockquote": "Quoted words really stay",
    "svg": "Svg label words really stay", "comment": "Comment words really stay",
    "button": "Button label words really stay", "kbd": "Ctrl words really stay",
    "samp": "Samp output words really stay",
}
KEPT = [
    ("This plain sentence really holds the first cut.", "This plain sentence holds the first cut."),
    ("The caption below the command really holds the second cut.",
     "The caption below the command holds the second cut."),
    ("and this tail really holds the third cut.", "and this tail holds the third cut."),
    ("Tom &amp; Jerry really run the tests quickly.", "Tom &amp; Jerry run the tests quickly."),
]


def _page(prose: Prose, name: str = "web/p.html") -> str:
    (prose.cwd / name).parent.mkdir(exist_ok=True)
    shutil.copy(PAGE_FIXTURE, prose.cwd / name)
    return (prose.cwd / name).read_text(encoding="utf-8")


def _cut_filter(prose: Prose, path: str, edits: list[dict], *extra: str, words: int = 30, budget: int = 30,
                **prop: object) -> dict:
    args = ["filter", path, "--budget", str(budget), "--mode", "cut-only", "--word-budget", str(words), *extra]
    return json.loads(brief_ok(prose, *args, stdin=fenced(proposal(edits, **prop))))


def test_an_edit_in_each_excluded_element_is_dropped_and_the_plain_cuts_stay(prose: Prose) -> None:
    text = _page(prose)
    for phrase in EXCLUDED.values():
        assert text.count(phrase) == 1, phrase
    seeded = [edit(i, phrase, phrase.replace(" really", "")) for i, phrase in enumerate(EXCLUDED.values(), 1)]
    base = len(seeded)
    plain = [edit(base + i, old, new) for i, (old, new) in enumerate(KEPT, 1)]
    got = _cut_filter(prose, "web/p.html", [*seeded, *plain])
    assert [e["n"] for e in got["edits"]] == [base + 1, base + 2, base + 3, base + 4]
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {i: "protected-region" for i in range(1, base + 1)}
    assert got["mode"] == "cut-only"


def test_an_edit_that_touches_a_tag_is_dropped_as_markup_and_one_across_code_as_protected(prose: Prose) -> None:
    _page(prose)
    got = _cut_filter(prose, "web/p.html", [
        edit(1, "The <em>emphasised</em> word sits between tags.", "The <em>emphasised</em> word sits."),
        edit(2, "Run <code>inline code words really stay</code> first", "Run <code>inline code words really stay</code>"),
        edit(3, "word sits between tags.", "word sits tags."),
    ])
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {1: "markup", 2: "protected-region"}
    assert [e["n"] for e in got["edits"]] == [3]


def test_review_plan_edits_agrees_with_the_filter_on_the_new_html_elements(prose: Prose) -> None:
    text = _page(prose)
    edits = [edit(i, phrase, phrase.replace(" really", "")) for i, phrase in enumerate(EXCLUDED.values(), 1)]
    edits += [edit(100 + i, old, new) for i, (old, new) in enumerate(KEPT, 1)]
    plans = {p["n"]: p for p in _review().plan_edits(text, edits, None, True, True)}
    assert {n for n, p in plans.items() if p["span"] is not None} == {101, 102, 103, 104}
    assert {p["cause"] for n, p in plans.items() if n <= len(EXCLUDED)} == {"protected-region"}
    # the same elements in a markdown file are prose again: the extra tags are protected for html only
    md = "Intro words here.\n\nBefore <h2>Inline heading words really stay</h2> after.\n"
    plans = _review().plan_edits(md, [edit(1, "Inline heading words really stay", "Inline heading words stay")], None, False)
    assert plans[0]["span"] is not None


def test_markdown_with_an_inline_heading_is_not_newly_protected(prose: Prose) -> None:
    md = "Intro words here.\n\nBefore <h2>Inline heading words really stay</h2> after.\n"
    (prose.cwd / "docs" / "m.md").write_text(md, encoding="utf-8")
    (prose.cwd / "docs" / "m.html").write_text(md, encoding="utf-8")
    cut = [edit(1, "Inline heading words really stay", "Inline heading words stay")]
    assert [e["n"] for e in _cut_filter(prose, "docs/m.md", cut)["edits"]] == [1]
    as_html = _cut_filter(prose, "docs/m.html", cut)
    assert as_html["edits"] == [] and [d["cause"] for d in as_html["dropped"]] == ["protected-region"]


def test_an_edit_that_is_not_a_cut_is_dropped_whatever_else_is_true_of_it(prose: Prose) -> None:
    (prose.cwd / "docs" / "c.md").write_text(
        "The queue is basically ordered. The worker retries once.\n\nThe scheduler wakes every ten seconds.\n",
        encoding="utf-8")
    got = _cut_filter(prose, "docs/c.md", [
        edit(1, "The queue is basically ordered.", "The queue is ordered."),                  # a cut
        edit(2, "The worker retries once.", "The worker retries one time."),                    # a replacement
        edit(3, "The scheduler wakes every ten seconds.", "The scheduler wakes every ten seconds, always."),
    ])
    assert [e["n"] for e in got["edits"]] == [1]
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {2: "not-a-cut", 3: "not-a-cut"}
    assert got["dropped"][0]["new"] == "The worker retries one time."  # the author can audit what was dropped


def test_a_normal_filter_run_is_unchanged_and_carries_no_cut_only_keys(prose: Prose) -> None:
    (prose.cwd / "docs" / "c.md").write_text("The worker retries once.\n", encoding="utf-8")
    got = json.loads(brief_ok(prose, "filter", "docs/c.md", stdin=fenced(
        proposal([edit(1, "The worker retries once.", "The worker retries one time.")]))))
    assert [e["n"] for e in got["edits"]] == [1] and "mode" not in got and "words" not in got


def test_only_paragraph_cuts_stay_and_one_in_a_protected_region_is_dropped(prose: Prose) -> None:
    _page(prose)
    paragraphs = [
        {"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "This plain sentence"', "advice": "repeats"},
        {"n": 2, "action": "merge", "paragraphs": 'the paragraphs opening "This plain" and "Tom"', "advice": "join"},
        {"n": 3, "action": "cut", "paragraphs": 'the blockquote opening "Quoted words really stay"', "advice": "x"},
        {"n": 4, "action": "split", "paragraphs": 'the paragraph opening "Tom"', "advice": "split"},
    ]
    got = _cut_filter(prose, "web/p.html", [], paragraphs=paragraphs)
    assert [p["n"] for p in got["paragraphs"]] == [1]
    assert {p["n"]: p["cause"] for p in got["dropped_paragraphs"]} == {2: "not-a-cut", 3: "protected-region", 4: "not-a-cut"}


def test_every_query_moves_to_dropped_queries_with_the_cut_only_cause(prose: Prose) -> None:
    _page(prose)
    queries = [{"n": 1, "anchor": "This plain sentence", "text": "Is it needed?"},
               {"n": 2, "anchor": "absent anchor text", "text": "Where?"}]
    got = _cut_filter(prose, "web/p.html", [], queries=queries)
    assert got["queries"] == []
    assert [(q["n"], q["anchor"], q["cause"]) for q in got["dropped_queries"]] == [
        (1, "This plain sentence", "cut-only"), (2, "absent anchor text", "cut-only")]


def test_the_word_budget_is_a_target_the_filter_reports_and_never_a_cap(prose: Prose) -> None:
    _page(prose)
    edits = [edit(i, old, new) for i, (old, new) in enumerate(KEPT, 1)]
    over = _cut_filter(prose, "web/p.html", edits, words=30)
    assert [e["n"] for e in over["edits"]] == [1, 2, 3, 4] and over["dropped"] == []  # nothing dropped for the number
    assert over["words"] == {"document": 45, "budget": 30, "removed": 4, "after": 41, "counts": "sentence edits only"}
    assert any("word budget" in w and "41" in w for w in over["warnings"])
    within = _cut_filter(prose, "web/p.html", edits, words=41)
    assert within["words"]["after"] == 41 and not any("word budget" in w for w in within["warnings"])


def test_the_sentence_edit_budget_still_caps_a_cut_only_run(prose: Prose) -> None:
    _page(prose)
    edits = [edit(i, old, new) for i, (old, new) in enumerate(KEPT, 1)]
    got = _cut_filter(prose, "web/p.html", edits, budget=2)
    assert [e["n"] for e in got["edits"]] == [1, 2]
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {3: "over-budget", 4: "over-budget"}
    assert got["words"]["removed"] == 2


def test_the_filter_refuses_a_cut_only_flag_it_cannot_use(prose: Prose) -> None:
    _page(prose)
    for args in (["--word-budget", "30"], ["--mode", "cut-only"], ["--mode", "other", "--word-budget", "3"]):
        proc = run_brief(prose, "filter", "web/p.html", *args, stdin=fenced(proposal([])))
        assert proc.returncode != 0, args
    (prose.cwd / "in.txt").write_text("text\n", encoding="utf-8")
    stdin = run_brief(prose, "filter", "-", "--file", str(prose.cwd / "in.txt"), "--mode", "cut-only",
                      "--word-budget", "3", stdin=fenced(proposal([])))
    assert stdin.returncode == 1 and "stdin" in stdin.stderr


# ---------------------------------------------------------------------------
# A session end to end: build, filter, render, decide (T2 round trip)
# ---------------------------------------------------------------------------

DOC = (
    "Cut notes\n\n"
    "The queue is basically ordered, and entries leave in the order they arrived.\n\n"
    "The worker really quite simply retries a failed job once before it gives up.\n\n"
    "It should be noted that the retry path is unchanged from the previous release.\n\n"
    "The scheduler just wakes every ten seconds and picks the oldest job.\n"
)
CUT1 = edit(1, "The queue is basically ordered, and entries leave in the order they arrived.",
            "The queue is ordered, and entries leave in the order they arrived.", "filler")
CUT2 = edit(2, "The worker really quite simply retries a failed job once before it gives up.",
            "The worker retries a failed job once before it gives up.", "filler")
CUT3 = edit(3, "The scheduler just wakes every ten seconds and picks the oldest job.",
            "The scheduler wakes every ten seconds and picks the oldest job.", "filler")
REWRITE = edit(4, "It should be noted that the retry path is unchanged from the previous release.",
               "The retry path is the same as before.", "plainer")
MERGE = {"n": 1, "action": "merge", "paragraphs": 'the paragraphs opening "The worker" and "It should"', "advice": "join"}
DROP = {"n": 2, "action": "cut", "paragraphs": 'the paragraph opening "It should be noted"', "advice": "restates"}
QUERY = {"n": 1, "anchor": "The scheduler", "text": "Is ten seconds right?"}


def _build(prose: Prose, card: Path, words: int = 40) -> tuple[Path, str]:
    out = brief_ok(prose, "build", "docs/cut.md", "--genre", "reference-doc", "--budget", "20",
                   *_cut_args(card, words), "--work")
    head, brief = out.split("\n\n", 1)
    return Path(dict(line.split("=", 1) for line in head.splitlines())["WORK"]), brief


def _session(prose: Prose, card: Path, edits: list[dict], words: int = 40) -> tuple[Path, dict]:
    work, brief = _build(prose, card, words)
    sha = (work / "brief.md").read_text(encoding="utf-8").rsplit("Brief id: ", 1)[1].strip()
    reply = fenced(proposal(edits, voice_card=card.read_text(encoding="utf-8"), brief_sha=sha,
                            paragraphs=[MERGE, DROP], queries=[QUERY]))
    filtered = brief_ok(prose, "filter", "docs/cut.md", "--budget", "20", "--mode", "cut-only", "--word-budget",
                        str(words), "--save", str(work / "filtered.json"), stdin=reply)
    review_ok(prose, "render", "docs/cut.md", "--work", str(work), "--genre", "reference-doc")
    return work, json.loads(filtered)


def test_build_copies_the_given_card_into_the_work_directory_and_the_brief(prose: Prose, tmp_path: Path) -> None:
    (prose.cwd / "docs" / "cut.md").write_text(DOC, encoding="utf-8")
    work, brief = _build(prose, _card(tmp_path))
    assert (work / "card.txt").read_text(encoding="utf-8") == CARD
    assert "Mode: cut-only" in brief and f"<voice_card>\n{CARD}\n</voice_card>" in brief
    assert (work / "brief.md").read_text(encoding="utf-8").startswith(brief.rstrip("\n"))
    brief_ok(prose, "rmtmp", str(work))


def test_a_given_card_wins_over_a_stored_card_and_is_never_stored(prose: Prose, tmp_path: Path) -> None:
    (prose.cwd / "docs" / "cut.md").write_text(DOC, encoding="utf-8")
    prose.ok("voice-card", "docs/cut.md", "--from-stdin", stdin={"voice_card": "The stored card, unlike the given."})
    work, brief = _build(prose, _card(tmp_path))
    assert CARD in brief and "The stored card, unlike the given." not in brief
    assert prose.ok("voice-card", "docs/cut.md")["voice_card"]["text"] == "The stored card, unlike the given."
    brief_ok(prose, "rmtmp", str(work))


def test_an_unchanged_given_card_raises_no_filter_warning_and_a_rewritten_one_does(prose: Prose, tmp_path: Path) -> None:
    (prose.cwd / "docs" / "cut.md").write_text(DOC, encoding="utf-8")
    work, brief = _build(prose, _card(tmp_path))
    sha = (work / "brief.md").read_text(encoding="utf-8").rsplit("Brief id: ", 1)[1].strip()
    for card, warned in ((CARD, False), ("A card the editor wrote itself.", True)):
        out = json.loads(brief_ok(prose, "filter", "docs/cut.md", "--budget", "20", "--mode", "cut-only",
                                  "--word-budget", "40", "--save", str(work / "filtered.json"),
                                  stdin=fenced(proposal([CUT1], voice_card=card, brief_sha=sha))))
        assert any("voice_card" in w for w in out["warnings"]) is warned
    brief_ok(prose, "rmtmp", str(work))


def test_a_cut_only_session_is_decided_applied_stored_and_logged(prose: Prose, tmp_path: Path) -> None:
    doc = prose.cwd / "docs" / "cut.md"
    doc.write_text(DOC, encoding="utf-8")
    card = _card(tmp_path)
    work, filtered = _session(prose, card, [CUT1, CUT2, CUT3, REWRITE])
    assert [e["n"] for e in filtered["edits"]] == [1, 2, 3]
    assert {d["n"]: d["cause"] for d in filtered["dropped"]} == {4: "not-a-cut"}
    assert [p["n"] for p in filtered["paragraphs"]] == [2] and filtered["dropped_paragraphs"][0]["cause"] == "not-a-cut"
    assert filtered["queries"] == [] and filtered["dropped_queries"][0]["cause"] == "cut-only"
    assert filtered["words"]["budget"] == 40 and filtered["words"]["removed"] == 5
    assert json.loads((work / "session.json").read_text(encoding="utf-8"))["cut_only"] is True
    dry = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2", "--hold", "3", "--dry-run")
    assert dry["dry_run"] is True and [e["n"] for e in dry["accept"]] == [1]
    assert [e["n"] for e in dry["reject"]] == [2] and [e["n"] for e in dry["hold"]] == [3]
    assert doc.read_text(encoding="utf-8") == DOC  # a dry run writes nothing
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2", "--hold", "3")
    assert [a["n"] for a in done["applied"]] == [1] and done["rejected"] == [2] and done["held"] == [3]
    assert doc.read_text(encoding="utf-8") == DOC.replace("basically ", "")
    stored = prose.ok("rejections", "docs/cut.md")["rejections"]
    assert [r["old"] for r in stored] == [CUT2["old"]]  # the declined cut is stored, the held one is not
    session = t2_json(REPO_PROJECT, done["log"]["title"])["session"]
    assert session["cut_only"] is True and session["word_budget"] == 40
    assert session["words"]["budget"] == 40 and session["accepted"] == [1] and session["rejected"] == [2]
    assert session["words"]["removed"] == 1 and session["words"]["proposed"] == 5  # the applied cut, not the proposed ones
    assert not work.exists()


def test_the_next_cut_only_run_drops_the_rejected_cut_and_lists_it_in_section_4(prose: Prose, tmp_path: Path) -> None:
    doc = prose.cwd / "docs" / "cut.md"
    doc.write_text(DOC, encoding="utf-8")
    card = _card(tmp_path)
    work, _ = _session(prose, card, [CUT1, CUT2, CUT3])
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2", "--hold", "3")
    work2, brief = _build(prose, card)
    section4 = brief[brief.index("## 4. Not a defect"):brief.index("## 5. Budget")]
    assert "The author rejected these edits for THIS document" in section4
    assert "really quite simply" in section4
    sha = (work2 / "brief.md").read_text(encoding="utf-8").rsplit("Brief id: ", 1)[1].strip()
    again = json.loads(brief_ok(prose, "filter", "docs/cut.md", "--budget", "20", "--mode", "cut-only",
                                "--word-budget", "40", "--save", str(work2 / "filtered.json"),
                                stdin=fenced(proposal([CUT2, CUT3], voice_card=CARD, brief_sha=sha))))
    assert [e["n"] for e in again["edits"]] == [3]  # the held cut is proposed again; the rejected one is not
    assert {d["n"]: d["cause"] for d in again["dropped"]} == {2: "rejected"}
    brief_ok(prose, "rmtmp", str(work2))


def test_apply_refuses_a_hand_edited_edit_that_is_not_a_cut(prose: Prose, tmp_path: Path) -> None:
    doc = prose.cwd / "docs" / "cut.md"
    doc.write_text(DOC, encoding="utf-8")
    work, _ = _session(prose, _card(tmp_path), [CUT1])
    path = work / "filtered.json"
    prop = json.loads(path.read_text(encoding="utf-8"))
    prop["edits"].append(edit(9, "The scheduler just wakes every ten seconds", "The scheduler sleeps ten seconds"))
    path.write_text(json.dumps(prop), encoding="utf-8")
    review_ok(prose, "render", "docs/cut.md", "--work", str(work), "--genre", "reference-doc")
    for args in (["--accept", "9", "--dry-run"], ["--accept", "1, 9", "--dry-run"]):
        proc = run_review(prose, "apply", "--work", str(work), *args)
        assert proc.returncode == 1 and "edit 9" in proc.stderr and "not a cut" in proc.stderr, args
    assert doc.read_text(encoding="utf-8") == DOC
    ok = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    assert ok.returncode == 0  # the cut beside it is still decidable
    brief_ok(prose, "rmtmp", str(work))


def test_a_normal_session_still_applies_an_edit_that_adds_words(prose: Prose) -> None:
    doc = prose.cwd / "docs" / "n.md"
    doc.write_text("The worker retries once.\n", encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    brief_ok(prose, "filter", "docs/n.md", "--save", str(work / "filtered.json"), stdin=fenced(
        proposal([edit(1, "The worker retries once.", "The worker retries one time.")])))
    review_ok(prose, "render", "docs/n.md", "--work", str(work), "--genre", "reference-doc")
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert [a["n"] for a in done["applied"]] == [1]
    assert "cut_only" not in t2_json(REPO_PROJECT, done["log"]["title"])["session"]


# ---------------------------------------------------------------------------
# The skills, the agent and the README
# ---------------------------------------------------------------------------

CUT_TEXT = CUT_SKILL.read_text(encoding="utf-8") if CUT_SKILL.exists() else ""
CUT_ALLOWED = ("Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/prose-edit/scripts/brief.py *) "
               "Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/prose-edit/scripts/review.py *)")
EDIT_TEXT = SKILL.read_text(encoding="utf-8")


def test_prose_cut_is_a_model_invocable_skill_with_the_same_script_permission() -> None:
    fm = _frontmatter(CUT_SKILL)
    assert fm["name"] == "prose-cut"
    assert fm["description"].startswith("Use only from the site-page review gate")
    assert "disable-model-invocation" not in fm
    assert fm["allowed-tools"] == CUT_ALLOWED  # brief.py and review.py only: it never reaches memory.py
    assert "memory.py" not in fm["allowed-tools"] and "scripts/*" not in fm["allowed-tools"]
    assert _frontmatter(SKILL)["disable-model-invocation"] == "true"  # /prose-edit stays the author's own command
    assert CUT_TEXT.count("$ARGUMENTS") == 1


def test_prose_cut_names_the_same_script_prefixes_as_prose_edit() -> None:
    row = re.compile(r"^\| (BRIEF|REVIEW) \| `([^`]+)` \|$", re.MULTILINE)
    cut, edit_rows = dict(row.findall(CUT_TEXT)), dict(row.findall(EDIT_TEXT))
    assert set(cut) == {"BRIEF", "REVIEW"} and cut == edit_rows


def test_prose_cut_has_no_app_and_asks_no_question() -> None:
    for word in ("MarkupEditor", "AskUserQuestion", "propose_edits", "open_document", "Typora"):
        assert word not in CUT_TEXT, word
    assert "mcp__" not in CUT_TEXT
    assert "Never ask" in CUT_TEXT


def test_prose_cut_runs_the_cut_only_pipeline_in_order() -> None:
    order = ["BRIEF parse", "BRIEF build", "--mode cut-only", "--voice-card", "--word-budget", "--work",
             "line-editor", "BRIEF filter", "REVIEW render"]
    pos = CUT_TEXT.index("## Cut run")
    for needle in order:  # each appears after the one before it, in the steps
        found = CUT_TEXT.find(needle, pos)
        assert found >= 0, needle
        pos = found + 1
    assert "conexus-prose-edit:line-editor" in CUT_TEXT
    assert "WORK=" in CUT_TEXT and "BRIEF rmtmp" in CUT_TEXT


def test_prose_cut_stops_on_no_genre_without_asking() -> None:
    row = next(line for line in CUT_TEXT.splitlines() if "no genre" in line)
    assert "stop" in row.lower() and "never choose a genre" in CUT_TEXT.lower()
    assert "suggest-genre" not in CUT_TEXT


def test_prose_cut_decide_runs_the_dry_run_then_the_real_apply_with_no_question() -> None:
    decide = CUT_TEXT[CUT_TEXT.index("## Decide"):]
    dry = decide.index("REVIEW apply <apply_argv...> --dry-run")
    real = decide.index("REVIEW apply <apply_argv...>`", dry + 1)
    assert dry < real
    assert "without asking" in decide and "apply_argv" in decide
    assert "REVIEW log-retry" in decide and "run apply again" in decide


def test_prose_edit_points_cut_only_and_decide_to_prose_cut() -> None:
    inv = EDIT_TEXT[EDIT_TEXT.index("## Invocation"):EDIT_TEXT.index("## Steps")]
    assert "--mode cut-only --voice-card" in inv and "prose-cut" in inv
    step2 = EDIT_TEXT[EDIT_TEXT.index("2. Branch on `mode`"):EDIT_TEXT.index("3. Fix the input.")]
    for mode in ("`cut-only`", "`decide`, `count`"):
        row = next(line for line in step2.splitlines() if line.lstrip().startswith(f"| {mode}"))
        assert "conexus-prose-edit:prose-cut" in row and "stop" in row.lower(), mode


def _gate() -> str:
    text = SITE_PAGE.read_text(encoding="utf-8")
    return text[text.index("## 5. Review gate before publish"):text.index("## 6. Publish")]


def test_the_site_page_gate_dispatches_prose_cut_and_decides_after_its_own_fixes() -> None:
    gate = _gate()
    for needle in ("conexus-prose-edit:prose-cut", "--mode cut-only", "--voice-card", "--word-budget",
                   "--budget 20", "--genre how-to", "exploration-essay", "prose-cut decide"):
        assert needle in gate, needle
    for word in ("accept", "reject", "hold"):
        assert re.search(rf"\b{word}\b", gate), word
    # the fixes land first, so a fix that touched a cut makes apply skip it (no cause to name, nothing stored)
    assert gate.index("make the fact and style passes' fixes first") < gate.index("prose-cut decide '<work>'")
    assert "skipped" in gate and "not found" in gate and "nothing is stored" in gate
    assert "lost to a fact fix" not in gate  # the gate no longer predicts the overlap itself
    assert "--accept none" in gate and "mandatory" in gate


def test_prose_cut_and_the_gate_say_where_and_how_the_reasons_are_written() -> None:
    for text in (CUT_TEXT[CUT_TEXT.index("## Decide"):], _gate()):
        assert '{"<n>": "<reason>"}' in text
        assert "<work>/reasons.json" in text
        assert "--reasons-file '<work>/reasons.json'" in text
        assert "directly in" in text


def test_the_count_verb_is_named_in_prose_cuts_invocation_table_and_as_how_the_gate_picks_n() -> None:
    inv = CUT_TEXT[CUT_TEXT.index("## Invocation"):CUT_TEXT.index("## Cut run")]
    assert "`count <path>[:<start>-<end>]`" in inv and '{"words": N}' in inv
    assert "BRIEF count" in CUT_TEXT and "## Count" in CUT_TEXT
    gate = _gate()
    assert "prose-cut count" in gate
    unit = "the visible words outside protected regions and markup, an entity counted as the character it stands for"
    assert unit in gate and unit in CUT_TEXT
    assert "words.document" in gate and "final count" in gate


def test_both_texts_say_paragraph_cuts_are_advice_with_no_decide_number() -> None:
    for text in (CUT_TEXT, _gate()):
        assert "takes no decide number" in text and "nothing is stored" in text


def test_both_texts_say_a_cut_named_in_no_set_is_held_and_how_a_rejection_is_removed() -> None:
    for text in (CUT_TEXT[CUT_TEXT.index("## Decide"):], _gate()):
        assert "named in no set is held" in text and "rejections '<page>' --remove N" in text


def test_the_gate_says_the_check_is_structural_and_each_cut_is_read_for_meaning() -> None:
    gate = _gate()
    assert "only removals reach you" not in gate
    assert "structural" in gate and "negation" in gate and "read for meaning" in gate


def test_the_gate_maps_all_three_genres_and_runs_the_other_passes_in_the_background_first() -> None:
    gate = _gate()
    item = gate[gate.index("3. Voice pass"):gate.index("The voice pass exists because")]
    for needle in ("--genre how-to", "--genre reference-doc", "Overview", "--genre exploration-essay", "docs/exploration"):
        assert needle in item, needle
    assert "in the background" in item and "before" in item and "waits for its editor" in item


def test_the_line_editor_protects_the_extra_html_elements() -> None:
    body = AGENT.read_text(encoding="utf-8")
    protected = body[body.index("## Protected regions"):body.index("## Diagnostic questions")]
    line = next(ln for ln in protected.splitlines() if "nav" in ln)
    for element in ("nav", "h1", "h6", "button", "samp"):
        assert element in line, element
    assert "cut-only" in line  # a normal run's editor may edit them: the script protects them for a cut-only run only
    assert "cut-only" in body  # the rule that keys on the brief's mode stays


def test_the_readme_mentions_prose_cut() -> None:
    text = README.read_text(encoding="utf-8")
    assert "prose-cut" in text and "cut-only" in text and "site-page" in text


def test_the_scripts_describe_the_new_flags_in_their_docstrings() -> None:
    brief = (PLUGIN_SKILL / "scripts" / "brief.py").read_text(encoding="utf-8")
    assert "PHASE3_FLAGS" not in brief and "is not built yet" not in brief
    for needle in ("--mode cut-only", "--voice-card", "--word-budget", "decide"):
        assert needle in brief, needle


# ---------------------------------------------------------------------------
# Fix round for the reviews of 8d8bfa46a (nexus-ger02.11, .12)
# ---------------------------------------------------------------------------

CUT_ONLY_TAGS = ("nav", "h1", "h2", "button", "samp")


def _cut_session(prose: Prose, card: Path, path: str, edits: list[dict], genre: str = "how-to", words: int = 40,
                 **prop: object) -> tuple[Path, dict]:
    """build --work, filter --save and render for any page, as the prose-cut skill runs them."""
    out = brief_ok(prose, "build", path, "--genre", genre, "--budget", "20", *_cut_args(card, words), "--work")
    work = Path(dict(line.split("=", 1) for line in out.split("\n\n", 1)[0].splitlines())["WORK"])
    filtered = brief_ok(prose, "filter", path, "--budget", "20", "--mode", "cut-only", "--word-budget", str(words),
                        "--save", str(work / "filtered.json"), stdin=fenced(proposal(edits, **prop)))
    review_ok(prose, "render", path, "--work", str(work), "--genre", genre)
    return work, json.loads(filtered)


# 1. The html-only protected tags belong to a cut-only run


def test_a_normal_html_run_still_edits_inside_nav_headings_buttons_and_samp(prose: Prose) -> None:
    text = _page(prose)
    edits = [edit(i, EXCLUDED[k], EXCLUDED[k].replace(" really", "")) for i, k in enumerate(CUT_ONLY_TAGS, 1)]
    got = json.loads(brief_ok(prose, "filter", "web/p.html", "--budget", "30", stdin=fenced(proposal(edits))))
    assert [e["n"] for e in got["edits"]] == [1, 2, 3, 4, 5] and got["dropped"] == []
    plans = {p["n"]: p for p in _review().plan_edits(text, edits, None, True)}
    assert all(p["span"] is not None for p in plans.values()), plans
    marked = {p["n"]: p for p in _review().plan_edits(text, edits, None, True, True)}  # the cut-only set
    assert {p["cause"] for p in marked.values()} == {"protected-region"}


def test_a_normal_html_session_applies_an_edit_in_an_h1(prose: Prose) -> None:
    _page(prose)
    work = Path(brief_ok(prose, "tmpdir").strip())
    brief_ok(prose, "filter", "web/p.html", "--save", str(work / "filtered.json"), stdin=fenced(proposal(
        [edit(1, EXCLUDED["h1"], "Heading one words stay")])))
    review_ok(prose, "render", "web/p.html", "--work", str(work), "--genre", "how-to")
    assert "cut_only" not in json.loads((work / "session.json").read_text(encoding="utf-8"))
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert [a["n"] for a in done["applied"]] == [1] and done["skipped"] == []
    assert "<h1>Heading one words stay</h1>" in (prose.cwd / "web" / "p.html").read_text(encoding="utf-8")


def test_a_cut_only_html_session_leaves_an_h1_edit_unplaced(prose: Prose, tmp_path: Path) -> None:
    _page(prose)
    work, filtered = _cut_session(prose, _card(tmp_path), "web/p.html", [])
    prop = json.loads((work / "filtered.json").read_text(encoding="utf-8"))
    prop["edits"] = [edit(1, EXCLUDED["h1"], "Heading one words stay")]  # a hand-edited proposal
    (work / "filtered.json").write_text(json.dumps(prop), encoding="utf-8")
    out = review_ok(prose, "render", "web/p.html", "--work", str(work), "--genre", "how-to")
    assert [(u["n"], u["cause"]) for u in out["unplaced"]] == [(1, "protected-region")]
    brief_ok(prose, "rmtmp", str(work))


# 2. A cut-only work directory keeps its enforcement


def test_filter_in_a_cut_only_work_directory_refuses_to_run_without_the_cut_flags(prose: Prose, tmp_path: Path) -> None:
    (prose.cwd / "docs" / "cut.md").write_text(DOC, encoding="utf-8")
    work, _ = _build(prose, _card(tmp_path))
    args = ["filter", "docs/cut.md", "--budget", "20", "--save", str(work / "filtered.json")]
    proc = run_brief(prose, *args, stdin=fenced(proposal([CUT1])))
    assert proc.returncode == 1 and "cut-only" in proc.stderr and "--word-budget" in proc.stderr, proc.stderr
    assert not (work / "filtered.json").exists()
    (work / "brief.md").unlink()  # the card file alone marks the directory
    assert (work / "card.txt").is_file()
    assert run_brief(prose, *args, stdin=fenced(proposal([CUT1]))).returncode == 1
    ok = run_brief(prose, *args, "--mode", "cut-only", "--word-budget", "40", stdin=fenced(proposal([CUT1])))
    assert ok.returncode == 0, ok.stderr
    brief_ok(prose, "rmtmp", str(work))


def test_render_sets_cut_only_from_the_work_directory_when_the_proposal_lacks_the_mode(prose: Prose, tmp_path: Path) -> None:
    doc = prose.cwd / "docs" / "cut.md"
    doc.write_text(DOC, encoding="utf-8")
    work, _ = _session(prose, _card(tmp_path), [CUT1])
    path = work / "filtered.json"
    prop = json.loads(path.read_text(encoding="utf-8"))
    for key in ("mode", "word_budget", "words"):
        prop.pop(key)
    prop["edits"].append(edit(9, "The scheduler just wakes every ten seconds", "The scheduler sleeps ten seconds"))
    path.write_text(json.dumps(prop), encoding="utf-8")
    review_ok(prose, "render", "docs/cut.md", "--work", str(work), "--genre", "reference-doc")
    assert json.loads((work / "session.json").read_text(encoding="utf-8"))["cut_only"] is True
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "9", "--dry-run")
    assert proc.returncode == 1 and "not a cut" in proc.stderr and doc.read_text(encoding="utf-8") == DOC
    brief_ok(prose, "rmtmp", str(work))


def test_a_cut_only_render_opens_no_viewer_while_an_ordinary_render_does(prose: Prose, tmp_path: Path) -> None:
    """The copy has no reader in a prose-cut run, so a stored viewer is neither asked for nor started; an ordinary
    run's Typora review is unchanged."""
    prose.ok("viewer", "--set", "Typora")
    opener = tmp_path / "opener.py"
    log = tmp_path / "opened.json"
    opener.write_text(f"import json, sys\nopen({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    env = {**prose.env, "PROSE_EDIT_OPEN": f"{sys.executable} {opener}"}
    (prose.cwd / "docs" / "cut.md").write_text(DOC, encoding="utf-8")
    work, _ = _session(prose, _card(tmp_path), [CUT1])
    proc = run_review(prose, "render", "docs/cut.md", "--work", str(work), "--genre", "reference-doc", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["viewer"] is None and out["opened"] is False and not log.exists()
    assert json.loads((work / "session.json").read_text(encoding="utf-8"))["cut_only"] is True
    brief_ok(prose, "rmtmp", str(work))
    plain = Path(brief_ok(prose, "tmpdir").strip())
    brief_ok(prose, "filter", "docs/cut.md", "--save", str(plain / "filtered.json"), stdin=fenced(proposal([CUT1])))
    proc = run_review(prose, "render", "docs/cut.md", "--work", str(plain), "--genre", "reference-doc", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["viewer"] == "Typora" and out["opened"] is True and json.loads(log.read_text()) == ["Typora", out["copy"]]
    assert "cut_only" not in json.loads((plain / "session.json").read_text(encoding="utf-8"))
    brief_ok(prose, "rmtmp", str(plain))


# 5. Markup and meaning in cuts

MARKUP = [
    ("html", "Use a very good plan.", "Use <a> good plan."),
    ("html", "Use a very good plan.", "Use a &amp; good plan."),
    ("html", "Tom &amp; Jerry really run.", "Tom &amp; &amp; Jerry run."),
    ("html", "Use a very good plan.", "Use a > good plan."),
    ("md", "A really big thing helps.", "A **big** thing helps."),
    ("md", "Keep foo really bar here.", "Keep foo ~~bar~~ here."),
    ("md", "Run the really long command.", "Run the `long` command."),
    ("md", "A really big thing helps.", "A _big_ thing helps."),
    # a symbol standing in for a word, or markup built from the words that stay
    ("md", "Use a very good plan.", "Use [a good](plan)."),
    ("md", "Feed cats or dogs very often.", "Feed cats/dogs often."),
    ("md", "Bring A and B very soon.", "Bring A & B soon."),
    ("md", "Use a good plan here really.", "# Use a good plan here."),
    ("html", "Keep A &mdash; very B here.", "Keep A &amp; B here."),
]


@pytest.mark.parametrize(("kind", "old", "new"), MARKUP)
def test_a_cut_that_adds_markup_is_dropped_as_not_a_cut(prose: Prose, kind: str, old: str, new: str) -> None:
    name = "docs/m.html" if kind == "html" else "docs/m.md"
    body = f"<p>Intro words here.</p>\n<p>{old}</p>\n" if kind == "html" else f"Intro words here.\n\n{old}\n"
    (prose.cwd / name).write_text(body, encoding="utf-8")
    got = _cut_filter(prose, name, [edit(1, old, new)])
    assert got["edits"] == [] and [(d["n"], d["cause"]) for d in got["dropped"]] == [(1, "not-a-cut")], got


def test_a_cut_that_keeps_its_markup_stays(prose: Prose) -> None:
    (prose.cwd / "docs" / "m.md").write_text("A **really** big thing helps, and `x` stays.\n", encoding="utf-8")
    got = _cut_filter(prose, "docs/m.md", [edit(1, "A **really** big thing helps", "A big thing helps")])
    assert [e["n"] for e in got["edits"]] == [1]
    _page(prose)  # Tom &amp; Jerry keeps its entity in KEPT


def test_apply_refuses_an_accepted_cut_that_adds_markup(prose: Prose, tmp_path: Path) -> None:
    doc = prose.cwd / "docs" / "cut.md"
    doc.write_text(DOC, encoding="utf-8")
    work, _ = _session(prose, _card(tmp_path), [CUT1])
    path = work / "filtered.json"
    prop = json.loads(path.read_text(encoding="utf-8"))
    old = "It should be noted that the retry path is unchanged from the previous release."
    prop["edits"].append(edit(9, old, "It should be **noted** that the retry path is unchanged."))
    path.write_text(json.dumps(prop), encoding="utf-8")
    review_ok(prose, "render", "docs/cut.md", "--work", str(work), "--genre", "reference-doc")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "9", "--dry-run")
    assert proc.returncode == 1 and "edit 9" in proc.stderr and "not a cut" in proc.stderr, proc.stderr
    assert doc.read_text(encoding="utf-8") == DOC
    brief_ok(prose, "rmtmp", str(work))


def test_apply_refuses_an_accepted_html_cut_that_adds_a_tag(prose: Prose, tmp_path: Path) -> None:
    page = prose.cwd / "web" / "q.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text("<html><body>\n<p>Intro words here.</p>\n<p>Use a very good plan.</p>\n</body></html>\n", encoding="utf-8")
    work, _ = _cut_session(prose, _card(tmp_path), "web/q.html", [edit(1, "Intro words here.", "Intro words.")])
    path = work / "filtered.json"
    prop = json.loads(path.read_text(encoding="utf-8"))
    prop["edits"].append(edit(9, "Use a very good plan.", "Use <a> good plan."))
    path.write_text(json.dumps(prop), encoding="utf-8")
    review_ok(prose, "render", "web/q.html", "--work", str(work), "--genre", "how-to")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "9", "--dry-run")
    assert proc.returncode == 1 and "edit 9" in proc.stderr and "not a cut" in proc.stderr, proc.stderr
    brief_ok(prose, "rmtmp", str(work))


def test_a_kept_cut_that_removes_a_negation_or_modal_is_warned_never_dropped(prose: Prose) -> None:
    (prose.cwd / "docs" / "neg.md").write_text(
        "The job does not retry after a failure. The queue is basically ordered. Only the leader may write.\n"
        "The worker cannot run without a lock. A reader may read the log.\n", encoding="utf-8")
    got = _cut_filter(prose, "docs/neg.md", [
        edit(1, "The job does not retry after a failure.", "The job does retry after a failure."),
        edit(2, "The queue is basically ordered.", "The queue is ordered."),
        edit(3, "Only the leader may write.", "The leader may write."),
        edit(4, "The worker cannot run without a lock.", "The worker run a lock."),
        edit(5, "A reader may read the log.", "A reader read the log."),
    ])
    assert [e["n"] for e in got["edits"]] == [1, 2, 3, 4, 5] and got["dropped"] == []
    said = {n: [w for w in got["warnings"] if f"edit {n} " in w and "meaning" in w] for n in (1, 2, 3, 4, 5)}
    assert said[2] == [] and all(len(said[n]) == 1 for n in (1, 3, 4, 5)), got["warnings"]
    assert '"not"' in said[1][0] and '"only"' in said[3][0] and '"may"' not in said[3][0]
    assert '"cannot"' in said[4][0] and '"without"' in said[4][0] and '"may"' in said[5][0]



def test_a_cut_that_removes_a_contraction_or_leaves_emphasis_unpaired_is_warned(prose: Prose) -> None:
    (prose.cwd / "docs" / "neg.md").write_text(
        "We don't ship the build. You can\u2019t skip it. Run it unless told not to. This is **very good** stuff.\n",
        encoding="utf-8")
    got = _cut_filter(prose, "docs/neg.md", [
        edit(1, "We don't ship the build.", "We ship the build."),
        edit(2, "You can\u2019t skip it.", "You skip it."),
        edit(3, "Run it unless told not to.", "Run it told not to."),
        edit(4, "This is **very good** stuff.", "This is good** stuff."),
    ])
    assert [e["n"] for e in got["edits"]] == [1, 2, 3, 4], got
    meaning = {n: [w for w in got["warnings"] if f"edit {n} " in w and "meaning" in w] for n in (1, 2, 3, 4)}
    assert all(len(meaning[n]) == 1 for n in (1, 2, 3)) and meaning[4] == [], got["warnings"]
    assert '"don\'t"' in meaning[1][0] and '"unless"' in meaning[3][0]
    assert any("edit 4 " in w and '"**"' in w and "pair" in w for w in got["warnings"]), got["warnings"]



def test_both_skills_forbid_joining_anything_after_a_command() -> None:
    # live cutonly-1 (2026-10-31): the model ran `brief.py filter ... ; echo "FILTER_EXIT=$?"`, which the
    # allowlist refused in don't-ask mode, and the run stopped before the filter
    for rel in ("prose-edit/skills/prose-cut/SKILL.md", "prose-edit/skills/prose-edit/SKILL.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "nothing after it" in text and "`echo`" in text and "`;`" in text, rel


# 4. The count verb


def test_count_prints_the_words_document_of_a_cut_run_for_markdown_html_and_a_range(prose: Prose) -> None:
    _page(prose)
    assert json.loads(brief_ok(prose, "count", "web/p.html")) == {"words": 45}
    filtered = _cut_filter(prose, "web/p.html", [], words=30)
    assert filtered["words"]["document"] == 45  # one unit: the count the filter and the brief call the document's
    (prose.cwd / "docs" / "c.md").write_text("one two\n\nthree four five\n\nsix\n", encoding="utf-8")
    assert json.loads(brief_ok(prose, "count", "docs/c.md")) == {"words": 6}
    assert json.loads(brief_ok(prose, "count", "docs/c.md:3-3")) == {"words": 3}


def test_count_refuses_a_missing_file_and_takes_no_flag(prose: Prose) -> None:
    gone = run_brief(prose, "count", "docs/absent.md")
    assert gone.returncode == 1 and "absent.md" in gone.stderr
    assert run_brief(prose, "count", "docs/x.md", "--genre", "how-to").returncode != 0


def test_count_is_a_parsed_invocation_and_a_file_named_count_is_reached_with_dot_slash() -> None:
    mod = _module()
    assert mod.parse_invocation(["count", "web/p.html:2-9"]) == {
        "mode": "count", "path": "web/p.html", "range": {"start": 2, "end": 9}, "target": "web/p.html:2-9"}
    assert mod.parse_invocation(["count", "web/p.html"])["range"] is None
    for tokens in (["count"], ["count", "a.md", "b.md"], ["count", "-"], ["count", "a.md", "--genre", "how-to"]):
        with pytest.raises(mod.UserError):
            mod.parse_invocation(tokens)
    assert mod.parse_invocation(["./count"])["mode"] == "edit"


# 6. Order: the gate's own fixes land first, and a cut one of them touched skips by itself


def test_a_fix_that_touched_a_cuts_sentence_makes_the_cut_skip_and_nothing_is_stored(prose: Prose, tmp_path: Path) -> None:
    doc = prose.cwd / "docs" / "cut.md"
    doc.write_text(DOC, encoding="utf-8")
    work, _ = _session(prose, _card(tmp_path), [CUT1, CUT2, CUT3])
    fixed = DOC.replace("retries a failed job once", "retries a failed job twice")  # the gate's fact fix lands first
    doc.write_text(fixed, encoding="utf-8")
    dry = review_ok(prose, "apply", "--work", str(work), "--accept", "1,2,3", "--dry-run")
    assert [(s["n"], s["cause"]) for s in dry["would_skip"]] == [(2, "not-found")] and dry["stores_rejections"] is False
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1,2,3")
    assert [a["n"] for a in done["applied"]] == [1, 3] and [s["n"] for s in done["skipped"]] == [2]
    assert done["rejected"] == [] and done["rejections_stored"] is False
    assert doc.read_text(encoding="utf-8") == fixed.replace("basically ", "").replace("just ", "")
    assert prose.ok("rejections", "docs/cut.md")["rejections"] == []
    session = t2_json(REPO_PROJECT, done["log"]["title"])["session"]
    assert session["accepted"] == [1, 2, 3] and session["applied"] == [1, 3] and session["rejected"] == []


def test_a_cut_named_in_no_set_is_held_and_nothing_is_stored(prose: Prose, tmp_path: Path) -> None:
    doc = prose.cwd / "docs" / "cut.md"
    doc.write_text(DOC, encoding="utf-8")
    work, _ = _session(prose, _card(tmp_path), [CUT1, CUT2, CUT3])
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert done["held"] == [2, 3] and done["rejected"] == [] and done["rejections_stored"] is False
    assert prose.ok("rejections", "docs/cut.md")["rejections"] == []


# 7. Paragraph cuts and the word numbers


def test_the_filter_states_that_words_removed_counts_sentence_edits_only(prose: Prose) -> None:
    _page(prose)
    paragraphs = [{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "This plain sentence"', "advice": "x"}]
    got = _cut_filter(prose, "web/p.html", [edit(1, *KEPT[0])], paragraphs=paragraphs)
    assert [p["n"] for p in got["paragraphs"]] == [1]
    assert got["words"]["removed"] == 1 and got["words"]["counts"] == "sentence edits only"


def test_the_session_log_records_the_words_the_applied_cuts_removed_not_the_proposed_ones(
    prose: Prose, tmp_path: Path
) -> None:
    (prose.cwd / "docs" / "cut.md").write_text(DOC, encoding="utf-8")
    work, filtered = _session(prose, _card(tmp_path), [CUT1, CUT2, CUT3])
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "2")
    words = t2_json(REPO_PROJECT, done["log"]["title"])["session"]["words"]
    assert filtered["words"]["removed"] == 5
    assert words["removed"] == 3 and words["proposed"] == 5
    assert "after" not in words and words["budget"] == 40  # the gate's fixes land first: `count` is the final count
