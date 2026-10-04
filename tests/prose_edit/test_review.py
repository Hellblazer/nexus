# SPDX-License-Identifier: AGPL-3.0-or-later
"""review.py (RDR-221 Steps 1.1 and 1.5, nexus-ger02.4): marked-up copy, accept by number, exact-match apply.

Pure pieces are imported and called. Everything that touches T2 runs brief.py and review.py as
subprocesses against the real engine substrate (the `prose` fixture), with the project prefix
override so the live prose projects are never touched.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, ROOT, Prose, git, t2_get, t2_json, t2_titles
from tests.prose_edit.test_brief import (
    NAMES_A_REPAIR,
    SKILL,
    brief_ok,
    edit,
    fenced,
    proposal,
    run_brief,
)

REVIEW = ROOT / ".claude" / "skills" / "prose-edit" / "scripts" / "review.py"


def _review() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_review", REVIEW)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _review_proc(prose: Prose, args: tuple[str, ...], env: dict[str, str] | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REVIEW), *args], capture_output=True, text=True,
        cwd=prose.cwd, env=env or prose.env, timeout=240,
    )


def run_review(prose: Prose, *args: str, env: dict[str, str] | None = None,
               dry_first: bool = True) -> subprocess.CompletedProcess[str]:
    """Run review.py. A real `apply` is first preceded by the dry run the skill always runs (the script
    refuses an apply without one); a dry run that itself fails is the result. `dry_first=False` runs the
    bare command, which is how the tests of the gate itself see the refusal."""
    if dry_first and args and args[0] == "apply" and "--dry-run" not in args:
        dry = _review_proc(prose, (*args, "--dry-run"), env)
        if dry.returncode != 0:
            return dry
    return _review_proc(prose, args, env)


def review_ok(prose: Prose, *args: str, dry_first: bool = True) -> dict:
    proc = run_review(prose, *args, dry_first=dry_first)
    assert proc.returncode == 0, f"{args}: rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


DOC = (
    "First paragraph is plain. It really is quite simple.\n\n"
    "Second paragraph says the queue drains in order. Basically it is ordered.\n\n"
    "Third paragraph repeats itself. Third paragraph repeats itself.\n"
)
E1 = edit(1, "It really is quite simple.", "It is simple.", "filler")
E2 = edit(2, "Basically it is ordered.", "It is ordered.", "filler opener")


def status(repo: Path) -> str:
    return git(repo, "status", "--porcelain", "-uall")


def start(prose: Prose, repo: Path, edits: list[dict], text: str = DOC, rel: str = "docs/s.md",
          target: str | None = None, genre: str = "reference-doc", **extra: object) -> tuple[Path, dict]:
    """A real session up to the author's turn: the file written, filter saved, copy rendered."""
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    target = target or rel
    proc = run_brief(prose, "filter", target, "--save", str(work / "filtered.json"),
                     stdin=fenced(proposal(edits, **extra)))
    assert proc.returncode == 0, proc.stderr
    return work, review_ok(prose, "render", target, "--work", str(work), "--genre", genre)


# ---------------------------------------------------------------------------
# The marked-up copy (Step 1.1)
# ---------------------------------------------------------------------------


def _plans(text: str, edits: list[dict], rng: dict | None = None) -> list[dict]:
    return _review().plan_edits(text, edits, rng, False)


def test_an_edit_is_inline_del_ins_then_a_superscript_number_and_its_reason_is_a_numbered_footnote() -> None:
    mod = _review()
    copy = mod.build_copy(DOC, proposal([E1, edit(2, "Basically ", "", "filler")]), label="docs/s.md",
                          genre="reference-doc", rng=None, html=False, stdin=False)
    assert "<del>It really is quite simple.</del><ins>It is simple.</ins><sup>1</sup>" in copy
    assert "<del>Basically </del><sup>2</sup>it is ordered." in copy  # a pure cut has no ins
    assert "<sup>1</sup> filler" in copy and "<sup>2</sup> filler" in copy
    body, notes = copy.split("\n---\n")[-2:]
    assert "<del>" in body and "<del>" not in notes


def test_the_note_is_a_blockquote_at_the_top_before_the_document_and_the_header_names_the_genre() -> None:
    mod = _review()
    copy = mod.build_copy(DOC, proposal([E1], note="Two paragraphs restate each other.", voice_card="Plain, first person."),
                          label="docs/s.md", genre="rdr", rng=None, html=False, stdin=False)
    lines = copy.splitlines()
    assert lines[0] == "# Line edit: docs/s.md"
    assert "Genre: rdr" in copy
    note = next(i for i, ln in enumerate(lines) if ln.startswith("> **Editor's note.**"))
    assert "Two paragraphs restate each other." in lines[note]
    assert note < next(i for i, ln in enumerate(lines) if "First paragraph is plain." in ln)
    assert any(ln.startswith("> **Voice card.**") and "Plain, first person." in ln for ln in lines)


def test_paragraph_proposals_sit_before_their_paragraph_and_queries_after_theirs() -> None:
    mod = _review()
    prop = proposal(
        [], paragraphs=[{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Third paragraph"',
                         "advice": "It restates itself."}],
        queries=[{"n": 1, "anchor": "queue drains in order", "text": "Which queue?"}],
    )
    copy = mod.build_copy(DOC, prop, label="docs/s.md", genre="rdr", rng=None, html=False, stdin=False)
    p = copy.index("> **[P1] cut.** It restates itself.")
    q = copy.index('> **[Q1]** "queue drains in order": Which queue?')
    assert copy.index("Second paragraph") < q < copy.index("Third paragraph repeats") and q < p
    assert p < copy.index("Third paragraph repeats")
    assert copy.index("Basically it is ordered.") < q  # after the whole paragraph, not after the anchor


def test_a_query_anchored_inside_a_code_fence_lands_after_the_fence_not_inside_it() -> None:
    mod = _review()
    text = "Intro sentence.\n\n```sh\nline one\n\nline two\n```\n\nTail sentence.\n"
    prop = proposal([], queries=[{"n": 1, "anchor": "line one", "text": "Why?"}])
    copy = mod.build_copy(text, prop, label="docs/s.md", genre="rdr", rng=None, html=False, stdin=False)
    assert copy.index("line two\n```") < copy.index("> **[Q1]**") < copy.index("Tail sentence.")


def test_an_edit_that_cannot_be_placed_is_listed_with_its_cause_instead_of_marked_inline() -> None:
    mod = _review()
    dup = edit(3, "Third paragraph repeats itself.", "Third repeats.")
    copy = mod.build_copy(DOC, proposal([E1, dup]), label="docs/s.md", genre="rdr", rng=None, html=False, stdin=False)
    assert "<del>Third paragraph repeats itself.</del>" not in copy
    assert "> **[E3]** Cannot be placed: the old string occurs 2 times. It is skipped if accepted." in copy
    assert "<del>It really is quite simple.</del>" in copy


def test_dropped_edits_keep_their_numbers_in_the_footnotes() -> None:
    mod = _review()
    copy = mod.build_copy(DOC, proposal([E2], dropped=[{"n": 1, "old": "x", "cause": "rejected"}]),
                          label="docs/s.md", genre="rdr", rng=None, html=False, stdin=False)
    assert "<sup>1</sup> dropped before this copy: rejected" in copy
    assert "<sup>2</sup> filler opener" in copy


def test_a_stdin_copy_says_nothing_is_written_to_a_file() -> None:
    mod = _review()
    copy = mod.build_copy("fix: a thing basically\n", proposal([edit(1, " basically", "", "filler")]),
                          label="stdin", genre="commit-message", rng=None, html=False, stdin=True)
    assert "Nothing is applied to a file" in copy and "<del> basically</del><sup>1</sup>" in copy


# ---------------------------------------------------------------------------
# Exact-match planning and apply
# ---------------------------------------------------------------------------


def test_an_old_string_that_occurs_exactly_once_is_placed_and_zero_or_many_are_not() -> None:
    plans = {p["n"]: p for p in _plans(DOC, [E1, edit(2, "not in the file", "x"),
                                              edit(3, "Third paragraph repeats itself.", "x")])}
    assert plans[1]["cause"] is None and DOC[plans[1]["span"][0]:plans[1]["span"][1]] == E1["old"]
    assert plans[2]["cause"] == "not-found"
    assert plans[3]["cause"] == "ambiguous" and "2 times" in plans[3]["detail"]


def test_two_overlapping_edits_are_both_skipped_and_touching_ones_are_not() -> None:
    text = "Alpha beta gamma delta.\n"
    plans = {p["n"]: p for p in _plans(text, [
        edit(1, "Alpha beta gamma", "A"), edit(2, "gamma delta.", "D"),   # overlap on "gamma"
        edit(3, "beta", "b"),                                              # inside edit 1
    ])}
    assert [plans[n]["cause"] for n in (1, 2, 3)] == ["overlap", "overlap", "overlap"]
    assert "edit 2" in plans[1]["detail"] and "edit 1" in plans[2]["detail"]
    touching = {p["n"]: p for p in _plans(text, [edit(1, "Alpha beta", "A"), edit(2, " gamma delta.", "G")])}
    assert touching[1]["cause"] is None and touching[2]["cause"] is None


def test_overlap_is_counted_only_among_the_edits_given_so_a_rejected_neighbour_frees_an_edit() -> None:
    text = "Alpha beta gamma delta.\n"
    both = [edit(1, "Alpha beta gamma", "A"), edit(2, "gamma delta.", "D")]
    assert all(p["cause"] == "overlap" for p in _plans(text, both))
    assert _plans(text, both[:1])[0]["cause"] is None


def test_a_range_run_counts_occurrences_inside_the_range_only() -> None:
    text = "Same words here.\n\nMiddle line.\n\nSame words here.\n"
    inside = _plans(text, [edit(1, "Same words here.", "S")], {"start": 5, "end": 5})
    assert inside[0]["cause"] is None and inside[0]["span"][0] == text.rindex("Same words here.")
    whole = _plans(text, [edit(1, "Same words here.", "S")], None)
    assert whole[0]["cause"] == "ambiguous"
    elsewhere = _plans(text, [edit(1, "Middle line.", "M")], {"start": 1, "end": 1})
    assert elsewhere[0]["cause"] == "outside-range"
    straddling = _plans(text, [edit(1, "here.\n\nMiddle", "x")], {"start": 1, "end": 3})
    assert straddling[0]["cause"] is None  # inside the range as a whole


def test_an_old_string_inside_a_protected_region_is_skipped_even_when_it_is_unique() -> None:
    text = "Plain sentence here.\n\n```sh\nsecret words\n```\n"
    plan = _plans(text, [edit(1, "secret words", "x")])[0]
    assert plan["cause"] == "protected-region"


def test_apply_replaces_in_file_order_from_the_original_spans() -> None:
    mod = _review()
    plans = mod.plan_edits(DOC, [E2, E1], None, False)  # given out of file order
    out = mod.apply_plan(DOC, plans)
    assert out == DOC.replace(E1["old"], E1["new"]).replace(E2["old"], E2["new"])
    # a replacement that is longer than its old string does not shift the others
    longer = [edit(1, "plain", "plain and long"), edit(2, "simple", "plain")]
    assert mod.apply_plan(DOC, mod.plan_edits(DOC, longer, None, False)) == (
        DOC.replace("plain", "plain and long", 1).replace("simple", "plain", 1))


def test_a_soft_wrapped_old_string_matches_across_the_line_break() -> None:
    text = "A sentence that wraps\nacross two lines here.\n"
    plans = _plans(text, [edit(1, "wraps\nacross two", "wraps across two")])
    assert plans[0]["cause"] is None


@pytest.mark.parametrize(("spec", "expected"), [
    ("1", {1}), ("1,3", {1, 3}), ("1, 3-5", {1, 3, 4, 5}), ("all", {1, 2, 3, 4, 5}), ("none", set()),
    ("ALL", {1, 2, 3, 4, 5}), ("2 4", {2, 4}), ("3-3", {3}),
])
def test_the_answer_grammar(spec: str, expected: set[int]) -> None:
    assert _review().parse_numbers(spec, {1, 2, 3, 4, 5}, "--accept") == expected


@pytest.mark.parametrize("spec", ["", "x", "1,,", "9", "0", "5-3", "1-9", "all,1", "-1", "1.5", "one"])
def test_a_bad_answer_is_refused_and_names_the_flag(spec: str) -> None:
    mod = _review()
    with pytest.raises(mod.UserError) as caught:
        mod.parse_numbers(spec, {1, 2, 3, 4, 5}, "--accept")
    assert "--accept" in str(caught.value)


def test_the_viewer_is_opened_only_on_macos_with_a_record_and_otherwise_the_path_is_printed(tmp_path: Path) -> None:
    mod = _review()
    copy = tmp_path / "c.md"
    copy.write_text("x")
    calls: list[list[str]] = []

    def run(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    assert mod.open_copy("Typora", copy, platform="darwin", override=None, run=run)["opened"] is True
    assert calls == [["open", "-a", "Typora", str(copy)]]
    for viewer, platform in ((None, "darwin"), ("", "darwin"), ("Typora", "linux")):
        calls.clear()
        got = mod.open_copy(viewer, copy, platform=platform, override=None, run=run)
        assert got["opened"] is False and calls == [] and got["reason"]
    assert mod.open_copy("Typora", copy, platform="darwin", override=None, run=lambda argv: 1)["opened"] is False
    calls.clear()
    assert mod.open_copy("Typora", copy, platform="linux", override="myopen --flag", run=run)["opened"] is True
    assert calls == [["myopen", "--flag", "Typora", str(copy)]]


# ---------------------------------------------------------------------------
# Counting, markup, links (mutations the first review's tests could not kill)
# ---------------------------------------------------------------------------


def test_an_old_string_that_overlaps_itself_counts_every_occurrence_so_it_is_ambiguous() -> None:
    plan = _plans("aaa\n", [edit(1, "aa", "b")])[0]
    assert plan["cause"] == "ambiguous" and "2 times" in plan["detail"]
    assert _plans("abab\n", [edit(1, "abab", "x")])[0]["cause"] is None  # one occurrence stays placeable
    assert _plans("ababa\n", [edit(1, "aba", "x")])[0]["cause"] == "ambiguous"


HTML = (
    '<!doctype html>\n<html><head><title>Page</title>\n<style>body { color: red; }</style>\n'
    '<script>var goat = "goatcounter";</script></head>\n<body>\n<h1>The queue</h1>\n'
    "<p>Intro paragraph is plain. It really is quite simple.</p>\n"
    '<p>Second paragraph links to <a href="https://example.com/really-quite-long">the long page</a> '
    "and says basically nothing.</p>\n"
    "<p>Unmarked paragraph stays out of the copy.</p>\n"
    "<p>Fourth paragraph closes the page.</p>\n</body></html>\n"
)


def test_an_html_edit_that_overlaps_markup_is_refused_but_text_between_tags_is_placed() -> None:
    mod = _review()
    plans = {p["n"]: p for p in mod.plan_edits(HTML, [
        edit(1, 'links to <a href="https://example.com/really-quite-long">', "x"),   # a whole tag
        edit(2, "example.com/really", "x"),                                         # inside an attribute value
        edit(3, "long\">the long", "x"),                                            # runs out of a tag into text
        edit(4, "and says basically nothing.", "and says nothing."),                 # plain text
    ], None, True)}
    assert [plans[n]["cause"] for n in (1, 2, 3, 4)] == ["markup", "markup", "markup", None]
    assert plans[1]["detail"] == "the old string overlaps HTML markup"


def test_a_markdown_link_destination_a_reference_definition_and_an_autolink_are_protected() -> None:
    text = ("See [the long link text](http://x/really-quite-long) for more.\n\n"
            "Or read <https://example.com/auto-link> today.\n\n"
            "A named [source][src] too.\n\n"
            "[src]: http://example.com/a-ref \"A title\"\n\n"
            'Inline <a href="https://example.com/some-page">html link</a> as well.\n')
    causes = {p["n"]: p["cause"] for p in _plans(text, [
        edit(1, "long link text](http://x/really-quite", "x"),   # spans the end of the link text into the URL
        edit(2, "http://x/really", "x"),                         # inside the URL
        edit(3, "auto-link", "x"),                               # inside an autolink
        edit(4, "a-ref", "x"),                                   # inside a reference definition
        edit(5, "some-page", "x"),                               # an attribute value in inline html
        edit(6, "the long link text", "the link"),               # the link text itself is prose
        edit(7, "html link", "link"),
    ])}
    assert causes == {1: "protected-region", 2: "protected-region", 3: "protected-region",
                      4: "protected-region", 5: "protected-region", 6: None, 7: None}


def test_a_reference_definition_protects_only_its_destination_and_title_never_its_label_or_a_footnote() -> None:
    text = ("[^1]: This footnote is really quite long and says basically nothing.\n\n"
            "[Sam]: Basically I said it was really quite late and nobody came.\n\n"
            "[the long label text]: http://example.com/a-ref \"A really quite long title\"\n\n"
            "[other]: <http://example.com/angle-ref> (A paren title)\n\n"
            "[^2]: Really.\n\n"                                   # a one-word footnote looks like a destination
            '[^3]: Intro "really quite long"\n')                  # a quoted tail looks like a title
    causes = {p["n"]: p["cause"] for p in _plans(text, [
        edit(1, "really quite long and says basically nothing", "long and says nothing"),  # footnote prose
        edit(2, "Basically I said it was really quite late", "I said it was late"),         # dialogue, not a definition
        edit(3, "the long label text", "the label"),                                        # a definition's label
        edit(4, "a-ref", "x"),                                                              # its destination
        edit(5, "really quite long title", "x"),                                            # its title
        edit(6, "angle-ref", "x"),                                                          # an <angle> destination
        edit(7, "A paren title", "x"),                                                      # a (paren) title
        edit(8, "Really.", "x"),                                                            # a footnote, never a definition
        edit(9, "really quite long\"", "x"),
    ])}
    assert causes == {1: None, 2: None, 3: None, 4: "protected-region", 5: "protected-region",
                      6: "protected-region", 7: "protected-region", 8: None, 9: None}


def test_the_filter_drops_an_edit_that_spans_a_link_destination(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text("See [the long link text](http://x/really-quite-long) for more.\n", encoding="utf-8")
    got = json.loads(brief_ok(prose, "filter", "docs/s.md", stdin=fenced(proposal([
        edit(1, "text](http://x/really", "x"), edit(2, "the long link text", "the link")]))))
    assert [e["n"] for e in got["edits"]] == [2]
    assert [(d["n"], d["cause"]) for d in got["dropped"]] == [(1, "protected-region")]


HTML_ENTITIES = (
    "<html><body><h1>Escapes</h1>\n"
    "<p>Use &lt;b&gt; tags &amp; say it really is quite simple. Fish &amp; chips are good.</p>\n"
    "</body></html>\n"
)


def test_an_html_copy_shows_entities_as_characters_but_marks_keep_the_source_text_they_matched() -> None:
    mod = _review()
    prop = proposal([edit(1, "it really is quite simple", "it is simple"),
                     edit(2, "Fish &amp; chips are good.", "Fish and chips are good.")])
    plans = {p["n"]: p for p in mod.plan_edits(HTML_ENTITIES, prop["edits"], None, True)}
    assert plans[2]["span"] is not None  # matching is against the raw source, entity and all
    copy = mod.build_copy(HTML_ENTITIES, prop, label="web/e.html", genre="exploration-essay", rng=None,
                          html=True, stdin=False)
    assert "Use <b> tags & say <del>it really is quite simple</del><ins>it is simple</ins><sup>1</sup>." in copy
    assert "<del>Fish &amp; chips are good.</del><ins>Fish and chips are good.</ins><sup>2</sup>" in copy
    assert "&lt;" not in copy and "tags &amp;" not in copy


def test_an_html_copy_is_flat_excerpts_with_marks_and_notes_beside_their_section() -> None:
    mod = _review()
    prop = proposal(
        [edit(1, "It really is quite simple.", "It is simple.", "filler"),
         edit(2, "basically nothing", "nothing", "filler")],
        paragraphs=[{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Second paragraph links"',
                     "advice": "It says nothing."}],
        queries=[{"n": 1, "anchor": "Fourth paragraph closes", "text": "Closes what?"}],
        note="Two paragraphs are padding.",
    )
    copy = mod.build_copy(HTML, prop, label="web/page.html", genre="exploration-essay", rng=None, html=True, stdin=False)
    for markup in ("<style", "<script", "<head", "<!doctype", "goatcounter", "color: red", "<p>", "<h1>", "<body>",
                   "href", "<a ", "example.com", "<title"):
        assert markup not in copy, markup
    assert "<del>It really is quite simple.</del><ins>It is simple.</ins><sup>1</sup>" in copy
    assert "<del>basically nothing</del><ins>nothing</ins><sup>2</sup>" in copy
    assert "Second paragraph links to the long page and says" in copy  # the link text stays, its URL does not
    assert "Unmarked paragraph" not in copy  # an excerpt holds only sections a mark or note belongs to
    assert "The queue" in copy  # the heading the excerpt sits under
    p = copy.index("> **[P1] cut.** It says nothing.")
    q = copy.index('> **[Q1]** "Fourth paragraph closes": Closes what?')
    assert copy.index("Intro paragraph") < p < copy.index("Second paragraph links")
    assert copy.index("Fourth paragraph closes the page.") < q
    assert "<sup>1</sup> filler" in copy and "<sup>2</sup> filler" in copy and "Two paragraphs are padding." in copy


def test_an_html_target_renders_a_flat_copy_and_applies_to_the_real_markup(prose: Prose, repo: Path) -> None:
    git_add = repo / "web" / "page.html"
    work, out = start(prose, repo, [edit(1, "It really is quite simple.", "It is simple.", "filler"),
                                    edit(2, "basically nothing", "nothing", "filler")],
                      text=HTML, rel="web/page.html", genre="exploration-essay")
    copy = Path(out["copy"]).read_text(encoding="utf-8")
    assert "<style" not in copy and "<script" not in copy and "<del>basically nothing</del>" in copy
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "all")
    assert [a["n"] for a in done["applied"]] == [1, 2]
    assert git_add.read_text(encoding="utf-8") == HTML.replace("It really is quite simple.", "It is simple.").replace(
        "basically nothing", "nothing")


def test_the_copy_header_example_is_in_the_answer_grammar_and_the_unplaceable_wording_matches_apply() -> None:
    mod = _review()
    dup = edit(3, "Third paragraph repeats itself.", "T")
    text = DOC + "Fourth paragraph says the queue drains in order and more.\n"
    edits = [dup, edit(4, "Fourth paragraph says the queue", "F"), edit(5, "the queue drains in order and", "Q")]
    for stdin in (False, True):
        copy = mod.build_copy(text, proposal(edits), label="docs/s.md", genre="rdr", rng=None, html=False, stdin=stdin)
        example = re.search(r"for example `([^`]+)`", copy)
        assert example, copy
        assert mod.parse_numbers(example.group(1), {1, 2, 3, 4, 5}, "--accept") == {1, 3, 4, 5}
    assert "> **[E3]** Cannot be placed: the old string occurs 2 times. It is skipped if accepted." in copy
    # two overlapping edits: accepting both skips both, accepting one applies it
    assert ("it overlaps edit 5. Accepted together they are both skipped; accepted alone it is applied."
            in copy)
    assert "skipped if accepted" not in copy.split("[E4]")[1].split("\n\n")[0]


def test_dropped_edits_queries_and_paragraphs_are_listed_with_their_text_and_cause() -> None:
    mod = _review()
    prop = proposal(
        [E2],
        dropped=[{"n": 1, "old": "a rejected span", "new": "its fix", "cause": "rejected"},
                 {"n": 3, "old": "not here", "new": "x", "cause": "not-found"}],
        dropped_queries=[{"n": 2, "anchor": "Gone anchor", "cause": "anchor-not-found"}],
        dropped_paragraphs=[{"n": 4, "paragraphs": 'the paragraph opening "Elsewhere"', "cause": "outside-range"}],
    )
    copy = mod.build_copy(DOC, prop, label="docs/s.md", genre="rdr", rng=None, html=False, stdin=False)
    assert '"a rejected span" -> "its fix" (rejected)' in copy
    assert '"not here" -> "x" (not-found)' in copy
    assert '[Q2] "Gone anchor" (anchor-not-found)' in copy
    assert '[P4] the paragraph opening "Elsewhere" (outside-range)' in copy
    assert "<sup>1</sup> dropped before this copy: rejected" in copy  # the footnotes keep their numbers


# ---------------------------------------------------------------------------
# End to end against the real substrate: scenarios from the RDR Test Plan
# ---------------------------------------------------------------------------


def test_copy_location_is_outside_the_repo_and_the_repo_status_is_unchanged(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text(DOC, encoding="utf-8")
    before = status(repo)
    work = Path(brief_ok(prose, "tmpdir").strip())
    assert run_brief(prose, "filter", "docs/s.md", "--save", str(work / "filtered.json"),
                     stdin=fenced(proposal([E1, E2]))).returncode == 0
    out = review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    copy = Path(out["copy"])
    try:
        assert copy.is_file() and work in copy.parents
        assert repo.resolve() not in copy.resolve().parents
        assert status(repo) == before
        assert "<del>It really is quite simple.</del>" in copy.read_text(encoding="utf-8")
        assert out["viewer"] is None and out["opened"] is False  # no viewer record: the path is printed
        assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == DOC  # render never edits the file
    finally:
        run_review(prose, "apply", "--work", str(work), "--accept", "none")
    assert not work.exists()
    assert status(repo) == before


def test_the_copy_opens_in_the_stored_viewer(prose: Prose, repo: Path, tmp_path: Path) -> None:
    prose.ok("viewer", "--set", "Typora")
    rec = tmp_path / "opener.py"
    log = tmp_path / "opened.json"
    rec.write_text(f"import json, sys\nopen({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    env = {**prose.env, "PROSE_EDIT_OPEN": f"{sys.executable} {rec}"}
    (repo / "docs" / "s.md").write_text(DOC, encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    run_brief(prose, "filter", "docs/s.md", "--save", str(work / "filtered.json"), stdin=fenced(proposal([E1])))
    proc = run_review(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["viewer"] == "Typora" and out["opened"] is True
    assert json.loads(log.read_text()) == ["Typora", out["copy"]]
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def test_prose_edit_open_runs_only_when_the_test_gate_is_set(prose: Prose, repo: Path, tmp_path: Path) -> None:
    prose.ok("viewer", "--set", "ZzNoSuchViewerApp")  # if the override were ignored this stays a harmless failed open
    rec = tmp_path / "opener.py"
    log = tmp_path / "opened.json"
    rec.write_text(f"import json, sys\nopen({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    env = {k: v for k, v in prose.env.items() if k != "PROSE_EDIT_TEST"}
    env["PROSE_EDIT_OPEN"] = f"{sys.executable} {rec}"
    (repo / "docs" / "s.md").write_text(DOC, encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    run_brief(prose, "filter", "docs/s.md", "--save", str(work / "filtered.json"), stdin=fenced(proposal([E1])))
    proc = run_review(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc", env=env)
    assert proc.returncode == 0, proc.stderr
    assert not log.exists(), "PROSE_EDIT_OPEN ran without PROSE_EDIT_TEST=1"
    assert json.loads(proc.stdout)["opened"] is False
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def test_render_reports_the_dropped_edits_queries_and_paragraphs_with_their_text(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text(DOC, encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    prop = proposal(
        [E1, edit(2, "not in the file at all", "x", "r")],
        queries=[{"n": 1, "anchor": "Gone anchor", "text": "Where?"}],
        paragraphs=[{"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Nothing like it"', "advice": "a"}],
    )
    run_brief(prose, "filter", "docs/s.md", "--save", str(work / "filtered.json"), stdin=fenced(prop))
    out = review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    assert out["dropped"] == [{"n": 2, "old": "not in the file at all", "new": "x", "cause": "not-found"}]
    assert out["dropped_queries"] == [{"n": 1, "anchor": "Gone anchor", "cause": "anchor-not-found"}]
    copy = Path(out["copy"]).read_text(encoding="utf-8")
    assert '"not in the file at all" -> "x" (not-found)' in copy and '[Q1] "Gone anchor" (anchor-not-found)' in copy
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def test_accepting_after_the_file_changed_under_one_edit_skips_and_reports_only_that_one(
    prose: Prose, repo: Path
) -> None:
    work, _ = start(prose, repo, [E1, E2])
    target = repo / "docs" / "s.md"
    target.write_text(DOC.replace("Basically it is ordered.", "Mostly it is ordered."), encoding="utf-8")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1,2")
    assert [a["n"] for a in out["applied"]] == [1]
    assert [(s["n"], s["cause"]) for s in out["skipped"]] == [(2, "not-found")]
    assert target.read_text(encoding="utf-8") == (
        DOC.replace("Basically it is ordered.", "Mostly it is ordered.").replace(E1["old"], E1["new"]))
    assert out["rejected"] == [] and not work.exists()


def test_a_twice_occurring_old_string_and_two_overlapping_edits_are_all_skipped_and_reported(
    prose: Prose, repo: Path
) -> None:
    text = DOC + "Fourth paragraph says the queue drains in order and more.\n"
    edits = [
        edit(1, "Third paragraph repeats itself.", "T"),            # twice
        edit(2, "Fourth paragraph says the queue", "F"),            # overlaps 3
        edit(3, "the queue drains in order and", "Q"),
        edit(4, "It really is quite simple.", "It is simple."),    # fine
    ]
    work, _ = start(prose, repo, edits, text=text)
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "all")
    assert {(s["n"], s["cause"]) for s in out["skipped"]} == {(1, "ambiguous"), (2, "overlap"), (3, "overlap")}
    assert [a["n"] for a in out["applied"]] == [4]
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == text.replace(
        "It really is quite simple.", "It is simple.")


def test_unaccepted_edits_are_stored_as_rejections_and_the_session_is_logged(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1, E2])
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2")
    assert out["rejected"] == [2] and out["held"] == []
    rec = t2_json(REPO_PROJECT, "doc/docs/s.md")
    assert [(r["old"], r["new"]) for r in rec["rejections"]] == [(E2["old"], E2["new"])]
    logs = [t for t in t2_titles(REPO_PROJECT) if t.startswith("log/docs/s.md/")]
    assert logs == [out["log"]["title"]]
    row = t2_get(REPO_PROJECT, logs[0])
    assert row is not None
    entry = json.loads(row["content"])
    assert entry["genre"] == "reference-doc" and entry["path"] == "docs/s.md"
    sess = entry["session"]
    assert sess["accepted"] == [1] and sess["applied"] == [1] and sess["rejected"] == [2]
    assert [e["n"] for e in sess["edits"]] == [1, 2] and sess["skipped"] == []


def test_a_held_edit_is_neither_applied_nor_rejected(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1, E2])
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--hold", "2")
    assert out["held"] == [2] and out["rejected"] == []
    assert [a["n"] for a in out["applied"]] == [1] and out["skipped"] == []
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"])
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None


def test_a_skipped_accepted_edit_is_not_stored_as_a_rejection(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1, edit(2, "Third paragraph repeats itself.", "T")])
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "all")
    assert [s["n"] for s in out["skipped"]] == [2] and out["rejected"] == []
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None


def test_reject_an_edit_and_run_again_the_same_edit_is_not_proposed_again(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1, E2])
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2")
    (repo / "docs" / "s.md").write_text(DOC, encoding="utf-8")  # the author reverts; same document again
    again = json.loads(brief_ok(prose, "filter", "docs/s.md", stdin=fenced(proposal([E1, E2]))))
    assert [e["n"] for e in again["edits"]] == [1]
    assert again["dropped"] == [{"n": 2, "old": E2["old"], "new": E2["new"], "cause": "rejected"}]


def test_list_and_remove_a_rejection_and_the_removed_edit_is_proposed_again(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1, E2])
    review_ok(prose, "apply", "--work", str(work), "--accept", "none", "--reject", "all")
    listed = prose.ok("rejections", "docs/s.md")["rejections"]
    assert [r["old"] for r in listed] == [E1["old"], E2["old"]]
    prose.ok("rejections", "docs/s.md", "--remove", "2")
    again = json.loads(brief_ok(prose, "filter", "docs/s.md", stdin=fenced(proposal([E1, E2]))))
    assert [e["n"] for e in again["edits"]] == [2]
    assert [d["n"] for d in again["dropped"]] == [1]


def test_a_stdin_run_keeps_no_document_record_applies_nothing_prints_the_text_and_logs_the_session(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    work = Path(brief_ok(prose, "tmpdir").strip())
    msg = "fix: make the queue drain in order basically\n\nThe loop is in fact ordered.\n"
    (work / "input.txt").write_text(msg, encoding="utf-8")
    edits = [edit(1, " basically", "", "filler"), edit(2, "in fact ", "", "filler")]
    saved = work / "filtered.json"
    proc = run_brief(prose, "filter", "-", "--file", str(work / "input.txt"), "--save", str(saved),
                     stdin=fenced(proposal(edits)))
    assert proc.returncode == 0, proc.stderr
    before = status(repo)
    out = review_ok(prose, "render", "-", "--work", str(work), "--file", str(work / "input.txt"),
                    "--genre", "commit-message")
    assert "Nothing is applied to a file" in Path(out["copy"]).read_text(encoding="utf-8")
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2")
    assert done["applied"] == [] and [a["n"] for a in done["accepted"]] == [1]
    assert done["text"] == "fix: make the queue drain in order\n\nThe loop is in fact ordered.\n"
    assert done["rejected"] == [2] and done["rejections_stored"] is False
    assert not any(t.startswith("doc/") for t in t2_titles(REPO_PROJECT))
    logs = [t for t in t2_titles(REPO_PROJECT) if t.startswith("log/stdin/")]
    assert logs == [done["log"]["title"]]
    row = t2_get(REPO_PROJECT, logs[0])
    assert row is not None and json.loads(row["content"])["session"]["accepted"] == [1]
    assert status(repo) == before and not work.exists()


def test_a_range_run_applies_inside_the_range_and_stores_the_rejection_under_the_bare_path(
    prose: Prose, repo: Path
) -> None:
    text = "Same words here.\n\nMiddle line.\n\nSame words here. Basically fine.\n"
    edits = [edit(1, "Same words here.", "S"), edit(2, "Basically fine.", "Fine.")]
    work, _ = start(prose, repo, edits, text=text, target="docs/s.md:5-5")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2")
    assert [a["n"] for a in out["applied"]] == [1] and out["rejected"] == [2]
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == (
        "Same words here.\n\nMiddle line.\n\nS Basically fine.\n")
    assert t2_json(REPO_PROJECT, "doc/docs/s.md")["rejections"][0]["old"] == "Basically fine."
    row = t2_get(REPO_PROJECT, out["log"]["title"])
    assert row is not None and json.loads(row["content"])["range"] == {"start": 5, "end": 5}


def test_an_unknown_number_stops_the_apply_before_anything_is_written_and_keeps_the_work_dir(
    prose: Prose, repo: Path
) -> None:
    work, _ = start(prose, repo, [E1], dropped=[{"n": 2, "old": "x", "cause": "rejected"}])
    for bad in ("2", "7", "1,9", "x"):
        proc = run_review(prose, "apply", "--work", str(work), "--accept", bad)
        assert proc.returncode == 1 and proc.stdout == "" and "--accept" in proc.stderr
    assert work.is_dir() and (repo / "docs" / "s.md").read_text(encoding="utf-8") == DOC
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def test_accept_and_hold_may_not_name_the_same_edit(prose: Prose, repo: Path) -> None:
    work, _ = start(prose, repo, [E1, E2])
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1,2", "--hold", "2")
    assert proc.returncode == 1 and "both" in proc.stderr and work.is_dir()
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def test_a_service_that_is_down_stops_the_apply_with_exit_three_and_leaves_file_and_work_alone(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    work, _ = start(prose, repo, [E1, E2])
    fake = Path(__file__).parent / "acceptance" / "fake_nx_unavailable.py"  # real nx wording, remedy included
    env = {**prose.env, "PROSE_EDIT_NX": f"{sys.executable} {fake}"}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 3 and "unavailable" in proc.stderr and proc.stdout == ""
    assert "Stop here and tell the author." in proc.stderr
    assert not NAMES_A_REPAIR.search(proc.stderr)
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == DOC and work.is_dir()
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def test_apply_refuses_a_directory_that_is_not_a_work_directory_and_a_work_dir_with_no_copy(
    prose: Prose, repo: Path
) -> None:
    proc = run_review(prose, "apply", "--work", str(repo), "--accept", "all")
    assert proc.returncode == 1 and "work directory" in proc.stderr and repo.is_dir()
    empty = Path(brief_ok(prose, "tmpdir").strip())
    try:
        proc = run_review(prose, "apply", "--work", str(empty), "--accept", "all")
        assert proc.returncode == 1 and "render" in proc.stderr and empty.is_dir()
    finally:
        brief_ok(prose, "rmtmp", str(empty))


def test_apply_through_a_symlinked_path_keeps_the_symlink_and_edits_its_target(
    prose: Prose, repo: Path
) -> None:
    (repo / "docs" / "real.md").write_text(DOC, encoding="utf-8")
    (repo / "docs" / "link.md").symlink_to("real.md")
    work = Path(brief_ok(prose, "tmpdir").strip())
    assert run_brief(prose, "filter", "docs/link.md", "--save", str(work / "filtered.json"),
                     stdin=fenced(proposal([E1]))).returncode == 0
    review_ok(prose, "render", "docs/link.md", "--work", str(work), "--genre", "reference-doc")
    review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert (repo / "docs" / "link.md").is_symlink()
    assert (repo / "docs" / "real.md").read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"])


def test_crlf_files_keep_their_line_endings_and_mixed_endings_are_refused(prose: Prose, repo: Path) -> None:
    crlf = "Wrapped sentence that\r\ncontinues here, basically.\r\n\r\nSecond.\r\n"
    target = repo / "docs" / "s.md"
    work = Path(brief_ok(prose, "tmpdir").strip())
    target.write_bytes(crlf.encode())
    run_brief(prose, "filter", "docs/s.md", "--save", str(work / "filtered.json"),
              stdin=fenced(proposal([edit(1, "that\ncontinues here, basically.", "that\ncontinues here.")])))
    review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert [a["n"] for a in out["applied"]] == [1]
    assert target.read_bytes() == b"Wrapped sentence that\r\ncontinues here.\r\n\r\nSecond.\r\n"
    mixed = "One sentence here, basically.\r\nAnother line.\nThird.\n"
    work2 = Path(brief_ok(prose, "tmpdir").strip())
    target.write_bytes(mixed.encode())
    run_brief(prose, "filter", "docs/s.md", "--save", str(work2 / "filtered.json"),
              stdin=fenced(proposal([edit(1, " basically", "")])))
    review_ok(prose, "render", "docs/s.md", "--work", str(work2), "--genre", "reference-doc")
    proc = run_review(prose, "apply", "--work", str(work2), "--accept", "1")
    assert proc.returncode == 1 and "line endings" in proc.stderr and target.read_bytes() == mixed.encode()
    brief_ok(prose, "rmtmp", str(work2))


def test_save_is_refused_outside_a_work_directory(prose: Prose, repo: Path, tmp_path: Path) -> None:
    (repo / "docs" / "s.md").write_text(DOC, encoding="utf-8")
    for bad in (tmp_path / "out.json", repo / "docs" / "out.json"):
        proc = run_brief(prose, "filter", "docs/s.md", "--save", str(bad), stdin=fenced(proposal([E1])))
        assert proc.returncode == 1 and "work directory" in proc.stderr and not bad.exists()
    work = Path(brief_ok(prose, "tmpdir").strip())
    try:
        proc = run_brief(prose, "filter", "docs/s.md", "--save", str(work / "sub" / "x.json"),
                         stdin=fenced(proposal([E1])))
        assert proc.returncode == 1 and "directly inside" in proc.stderr
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_the_saved_filter_output_is_the_machine_format_filter_prints(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text(DOC, encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    try:
        proc = run_brief(prose, "filter", "docs/s.md", "--save", str(work / "filtered.json"),
                         stdin=fenced(proposal([E1, E2])))
        assert proc.returncode == 0
        assert json.loads((work / "filtered.json").read_text(encoding="utf-8")) == json.loads(proc.stdout)
    finally:
        brief_ok(prose, "rmtmp", str(work))


# ---------------------------------------------------------------------------
# The skill and the script
# ---------------------------------------------------------------------------


def test_the_skill_runs_render_then_apply_and_deletes_work_only_after_the_answer() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "scripts/review.py" in text and "| REVIEW |" in text
    assert "`BRIEF filter <target> --budget <budget> --save '<filtered>' [--file '<input>'] < '<reply>'`" in text
    assert "REVIEW render <target> --work '<work>'" in text and "REVIEW apply --work '<work>' --accept" in text
    assert "A successful apply has already deleted WORK (unless it reports `log_error`, below); do not delete it again." in text
    assert "--work WORK < WORK/reply.txt" not in text  # filter no longer deletes the copy's directory
    for verb in re.findall(r"REVIEW ([\w-]+)", text):
        assert verb in {"render", "apply", "log-retry"}
    # a failed log keeps WORK and the skill says what the author does: log-retry, never a second apply
    assert "REVIEW log-retry --work '<work>'" in text and "Never run apply again for it" in text
    # the script refuses an apply with no matching dry run; the skill says so and never skips it
    assert "exit 1 with `dry run` in stderr" in text and "never skip it" in text
    # the author's answer comes from the author, never from a guess
    assert "Never choose the numbers yourself." in text
    step12 = text[text.index("\n12. "):text.index("\n13. ")]
    # an answer without explicit numbers, or one the grammar refuses, is asked again and never computed
    assert "all but 2" in step12 and "ask the author again" in step12 and "never compute, infer or choose the set" in step12
    # the interpreted sets are echoed by a dry run and confirmed before the real apply
    assert "--dry-run" in step12 and "confirm" in step12 and "without `--dry-run`" in step12
    # a failure that leaves WORK in place keeps it and asks for a retry (exit 3 included), matching the code
    assert "exit 3" in step12 and "run apply again" in step12 and "keep WORK" in step12
    assert step12.index("exit 3") < step12.index("On any other failure, delete WORK")  # the retry cases come first
    assert "dropped_queries" in text and "<old> -> <new> (<cause>)" in text
    # corrections and promotion are offered, written only after the author has read the text
    assert "has read the text" in text and "add-entry" in text and "promote" in text and "--dry-run" in text


def test_review_py_is_stdlib_only_and_has_no_type_ignore_and_no_print() -> None:
    src = REVIEW.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "nexus" not in imported and imported <= set(sys.stdlib_module_names) | {"__future__"}
    assert "type: ignore" not in src
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
                   for n in ast.walk(tree))
    assert os.access(REVIEW, os.R_OK)
