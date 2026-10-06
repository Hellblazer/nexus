# SPDX-License-Identifier: AGPL-3.0-or-later
"""brief.py after the two reviews of nexus-ger02.3: protected shapes, scoping, hygiene."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.prose_edit.conftest import Prose, git
from tests.prose_edit.test_brief import (
    AGENT,
    BRIEF,
    NAMES_A_REPAIR,
    ORDER,
    SKILL,
    _frontmatter,
    _module,
    brief_ok,
    edit,
    fenced,
    proposal,
    run_brief,
    seed,
)

REVIEW = BRIEF.with_name("review.py")
PLAIN = "Plain editable sentence stays."


def _filter(prose: Prose, path: str, edits: list[dict], *extra: str, **extra_prop: object) -> dict:
    return json.loads(brief_ok(prose, "filter", path, *extra, stdin=fenced(proposal(edits, **extra_prop))))


# ---------------------------------------------------------------------------
# Protected shapes (code review I1): each binds to one construct
# ---------------------------------------------------------------------------

SHAPES: dict[str, tuple[str, str, str]] = {
    # name: (file name, document text, protected old string)
    "fence indented inside a list item": (
        "docs/s.md",
        f"- item\n\n    ```sh\n    nx run secret words\n    ```\n\n{PLAIN}\n", "nx run secret words"),
    "indented code block outside a list": (
        "docs/s.md", f"Intro sentence here.\n\n    indented code words\n    more code lines\n\n{PLAIN}\n",
        "indented code words"),
    "pipeless gfm table": (
        "docs/s.md", f"Intro sentence here.\n\nx | y\n--- | ---\ncell one words | cell two\n\n{PLAIN}\n",
        "cell one words"),
    "lazy blockquote continuation": (
        "docs/s.md", f"> quoted first line\nlazy continuation words\n\n{PLAIN}\n", "lazy continuation words"),
    "html comment in markdown": (
        "docs/s.md", f"{PLAIN}\n\n<!-- hidden comment\nwords across lines -->\n", "words across lines"),
    "inline html kbd in html": (
        "web/s.html", f"<p>{PLAIN} Press <kbd>Ctrl words</kbd> now.</p>\n", "Ctrl words"),
    "inline html code in html": (
        "web/s.html", f"<p>{PLAIN} Run <code>code words</code> now.</p>\n", "code words"),
    "inline svg in html": (
        "web/s.html", f"<p>{PLAIN}</p><svg><text>svg label words</text></svg>\n", "svg label words"),
    "html tag attribute": (
        "web/s.html", f'<p>{PLAIN} <a href="x.html">link text</a> end.</p>\n', 'href="x.html"'),
    "nested same-tag blockquote in html": (
        "web/s.html",
        f"<p>{PLAIN}</p><blockquote>outer <blockquote>inner</blockquote> after inner words</blockquote>\n",
        "after inner words"),
    "nested same-tag table in html": (
        "web/s.html",
        f"<p>{PLAIN}</p><table><tr><td><table><tr><td>x</td></tr></table> after nested cell</td></tr></table>\n",
        "after nested cell"),
    "bom before frontmatter": (
        "docs/s.md", f"﻿---\ntitle: front words\n---\n\n{PLAIN}\n", "front words"),
    "toml frontmatter": (
        "docs/s.md", f"+++\ntitle = \"toml front words\"\n+++\n\n{PLAIN}\n", "toml front words"),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_each_protected_shape_is_dropped_and_the_plain_sentence_stays(
    prose: Prose, repo: Path, shape: str
) -> None:
    name, text, protected = SHAPES[shape]
    (repo / name).parent.mkdir(exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    assert text.count(protected) == 1
    got = _filter(prose, name, [edit(1, protected, "x"), edit(2, PLAIN, "Plain sentence stays.")])
    assert [e["n"] for e in got["edits"]] == [2], shape
    cause = "markup" if shape == "html tag attribute" else "protected-region"
    assert got["dropped"] == [{"n": 1, "old": protected, "new": "x", "cause": cause}], shape


def test_indentation_that_is_not_code_stays_editable(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text(
        "- item one\n\n    continued list paragraph words\n\nLast plain sentence.\n", encoding="utf-8")
    got = _filter(prose, "docs/s.md", [edit(1, "continued list paragraph words", "continued")])
    assert [e["n"] for e in got["edits"]] == [1]
    (repo / "web").mkdir(exist_ok=True)
    (repo / "web" / "i.html").write_text(
        "<html><body>\n    <p>\n        Indented html words stay editable.\n    </p>\n</body></html>\n")
    got = _filter(prose, "web/i.html", [edit(1, "Indented html words stay editable.", "Words stay.")])
    assert [e["n"] for e in got["edits"]] == [1]


def test_an_unclosed_inline_html_tag_in_markdown_does_not_swallow_the_rest(
    prose: Prose, repo: Path
) -> None:
    (repo / "docs" / "s.md").write_text(f"Write a <code> tag here.\n\n{PLAIN}\n", encoding="utf-8")
    got = _filter(prose, "docs/s.md", [edit(1, PLAIN, "Plain.")])
    assert [e["n"] for e in got["edits"]] == [1]


# ---------------------------------------------------------------------------
# One sentence per edit (critic 7)
# ---------------------------------------------------------------------------


def test_an_edit_spanning_two_clear_sentences_is_dropped_as_multi_sentence(
    prose: Prose, repo: Path
) -> None:
    (repo / "docs" / "s.md").write_text(
        "The queue drains first. The retry path stays put. See e.g. Foo for more. One sentence here.\n\n"
        "Second paragraph starts here.\n", encoding="utf-8")
    edits = [
        edit(1, "The queue drains first. The retry path stays put.", "The queue drains."),
        edit(2, "See e.g. Foo for more.", "See Foo."),
        edit(3, "One sentence here.\n\nSecond paragraph starts here.", "Merged."),
        edit(4, "The retry path stays put.", ""),
    ]
    got = _filter(prose, "docs/s.md", edits)
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {1: "multi-sentence", 3: "multi-sentence"}
    assert [e["n"] for e in got["edits"]] == [2, 4]  # "e.g. Foo" is not a clear break
    assert not got["warnings"]


def test_an_ambiguous_period_is_kept_with_a_warning(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text("Use v1.2. Then wait for it to finish.\n", encoding="utf-8")
    got = _filter(prose, "docs/s.md", [edit(1, "Use v1.2. Then wait for it", "Use v1.2 and wait")])
    assert [e["n"] for e in got["edits"]] == [1]
    assert any("edit 1" in w and "sentence" in w for w in got["warnings"])


# ---------------------------------------------------------------------------
# Queries and paragraph proposals in range runs (code review M3)
# ---------------------------------------------------------------------------


def test_a_range_run_drops_queries_and_paragraphs_outside_the_range(prose: Prose, repo: Path) -> None:
    lines = [f"Entry {i} has a plain sentence." for i in range(1, 9)]
    (repo / "CHANGELOG.md").write_text("\n".join(lines) + "\n")
    queries = [
        {"n": 1, "anchor": "Entry 2 has a plain sentence.", "text": "outside"},
        {"n": 2, "anchor": "Entry 5 has a plain sentence.", "text": "inside"},
        {"n": 3, "anchor": "Entry 99 does not exist.", "text": "missing"},
    ]
    paras = [
        {"n": 1, "action": "cut", "paragraphs": 'the paragraph opening "Entry 7 has"', "advice": "a"},
        {"n": 2, "action": "cut", "paragraphs": 'the paragraph opening "Entry 5 has"', "advice": "b"},
        {"n": 3, "action": "cut", "paragraphs": "no quoted words here", "advice": "c"},
    ]
    got = _filter(prose, "CHANGELOG.md:4-6", [edit(1, "Entry 5 has a plain", "Entry 5 has a")],
                  queries=queries, paragraphs=paras)
    assert [q["n"] for q in got["queries"]] == [2]
    assert {d["n"]: d["cause"] for d in got["dropped_queries"]} == {1: "outside-range", 3: "anchor-not-found"}
    assert [p["n"] for p in got["paragraphs"]] == [2, 3]
    assert {d["n"]: d["cause"] for d in got["dropped_paragraphs"]} == {1: "outside-range"}
    assert any("paragraph proposal 3" in w for w in got["warnings"])


def test_a_whole_file_run_drops_only_queries_whose_anchor_is_missing(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text("An anchor sentence lives here.\n", encoding="utf-8")
    queries = [{"n": 1, "anchor": "An anchor sentence", "text": "ok"},
               {"n": 2, "anchor": "Not in the file at all", "text": "gone"}]
    got = _filter(prose, "docs/s.md", [], queries=queries)
    assert [q["n"] for q in got["queries"]] == [1]
    assert got["dropped_queries"] == [{"n": 2, "anchor": "Not in the file at all", "cause": "anchor-not-found"}]


# ---------------------------------------------------------------------------
# Budget validation (code review M4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-3", "x", "1.5", "１２"])
def test_budget_must_be_a_positive_ascii_integer_on_build_and_filter(prose: Prose, bad: str) -> None:
    for args in (("build", "docs/x.md", "--budget", bad), ("filter", "docs/x.md", "--budget", bad)):
        proc = run_brief(prose, *args, stdin=fenced(proposal([])))
        assert proc.returncode == 1 and "--budget" in proc.stderr and proc.stdout == "", args


def test_filter_defaults_to_the_same_budget_as_build(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text("".join(f"Sentence number {i} is here.\n\n" for i in range(1, 14)))
    edits = [edit(i, f"Sentence number {i} is here.", "") for i in range(1, 13)]
    got = _filter(prose, "docs/s.md", edits)
    assert len(got["edits"]) == 10 and len(got["dropped"]) == 2
    assert "at most 10 sentence edits" in brief_ok(prose, "build", "docs/x.md")


def test_every_dropped_edit_carries_its_new_text_whatever_dropped_it(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "s.md").write_text("Sentence one is here. Sentence two is here. Sentence three is here.\n")
    prose.ok("reject", "docs/s.md", "--old", "Sentence two is here.", "--new", "Two.")
    edits = [edit(1, "Sentence one is here.", "One."), edit(2, "Sentence two is here.", "Two."),
             edit(3, "not in the file", "Three."), edit(4, "Sentence three is here.", "Four.")]
    proc = run_brief(prose, "filter", "docs/s.md", "--budget", "3", stdin=fenced(proposal(edits)))
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert {d["n"]: (d["cause"], d["new"]) for d in got["dropped"]} == {
        2: ("rejected", "Two."), 3: ("not-found", "Three."), 4: ("over-budget", "Four.")}


# ---------------------------------------------------------------------------
# Temp directory hygiene (code review M1, M5)
# ---------------------------------------------------------------------------


def test_rmtmp_removes_only_a_prose_edit_directory_directly_under_the_temp_dir(
    prose: Prose, tmp_path: Path
) -> None:
    work = Path(brief_ok(prose, "tmpdir").strip())
    (work / "input.txt").write_text("secret pasted text")
    assert work.is_dir()
    brief_ok(prose, "rmtmp", str(work))
    assert not work.exists()

    tmp_root = prose.tmp
    victim = tmp_root / f"not-prose-edit-{os.getpid()}"
    victim.mkdir()
    nested = Path(brief_ok(prose, "tmpdir").strip())
    (nested / "sub").mkdir()
    try:
        assert run_brief(prose, "rmtmp", str(victim)).returncode == 1 and victim.exists()
        assert run_brief(prose, "rmtmp", str(nested / "sub")).returncode == 1 and (nested / "sub").exists()
        (nested / ".prose-edit-work").unlink()  # the sentinel is what marks a work directory
        assert run_brief(prose, "rmtmp", str(nested)).returncode == 1 and nested.exists()
        (nested / ".prose-edit-work").write_text("made by brief.py tmpdir\n")
        assert run_brief(prose, "rmtmp", str(tmp_path)).returncode == 1 and tmp_path.exists()
        assert run_brief(prose, "rmtmp", "/").returncode == 1
        link = tmp_root / f"prose-edit-link-{os.getpid()}"
        link.symlink_to(victim)
        try:
            assert run_brief(prose, "rmtmp", str(link)).returncode == 1 and victim.exists()
        finally:
            link.unlink()
        gone = run_brief(prose, "rmtmp", str(nested.parent / "prose-edit-nonexistent"))
        assert gone.returncode == 1 and "prose-edit" in gone.stderr
    finally:
        victim.rmdir()
        brief_ok(prose, "rmtmp", str(nested))


def test_tmpdir_refuses_a_location_inside_the_repository_and_leaves_nothing(
    prose: Prose, repo: Path
) -> None:
    inside = repo / "scratch"
    inside.mkdir()
    env = {**prose.env, "TMPDIR": str(inside)}
    proc = subprocess.run([sys.executable, str(BRIEF), "tmpdir"], capture_output=True, text=True,
                          cwd=repo, env=env)
    assert proc.returncode == 1 and "inside the repository" in proc.stderr and proc.stdout == ""
    assert list(inside.iterdir()) == []  # the directory the guard created is gone


def test_a_service_that_is_down_exits_three_from_build_and_filter(prose: Prose, tmp_path: Path) -> None:
    fake = Path(__file__).parent / "acceptance" / "fake_nx_unavailable.py"  # real nx wording, remedy included
    prose.env["PROSE_EDIT_NX"] = f"{sys.executable} {fake}"
    built = run_brief(prose, "build", "docs/x.md")
    assert built.returncode == 3 and "unavailable" in built.stderr and built.stdout == ""
    assert "Stop here and tell the author." in built.stderr  # said where the model reads it
    assert not NAMES_A_REPAIR.search(built.stderr)
    filtered = run_brief(prose, "filter", "docs/x.md", stdin=fenced(proposal([])))
    assert filtered.returncode == 3 and "unavailable" in filtered.stderr and filtered.stdout == ""


# ---------------------------------------------------------------------------
# Genre paths and new prose in the brief (critic 2, 4)
# ---------------------------------------------------------------------------


def _files(repo: Path, names: list[str]) -> None:
    for n in names:
        (repo / n).parent.mkdir(parents=True, exist_ok=True)
        (repo / n).write_text(f"{n} words.\n")


def _line(text: str, prefix: str) -> str:
    hits = [ln for ln in text.splitlines() if ln.startswith(prefix)]
    assert len(hits) == 1, (prefix, hits)
    return hits[0]


def test_genre_paths_follow_the_genre_map_and_exclude_rdr_and_the_target(
    prose: Prose, repo: Path
) -> None:
    _files(repo, ["docs/exploration/a.md", "docs/exploration/b.md", "docs/rdr/rdr-1-x.md",
                  "docs/rdr/rdr-2-y.md", "docs/guide.md", "docs/plans/p.md", "web/page.html", "CHANGELOG.md", "web/other.html"])
    essay = brief_ok(prose, "build", "docs/exploration/a.md")
    assert _line(essay, "Genre paths:") == "Genre paths: docs/exploration/*.md"
    assert _line(essay, "Exclude from the search:") == "Exclude from the search: docs/exploration/a.md"
    assert "docs/rdr" not in _line(essay, "Genre paths:")
    ref = brief_ok(prose, "build", "docs/guide.md")
    assert _line(ref, "Genre paths:") == "Genre paths: docs/*.md"  # top level only, not docs/rdr or docs/plans
    rdr = brief_ok(prose, "build", "docs/rdr/rdr-1-x.md")
    assert _line(rdr, "Genre paths:") == "Genre paths: docs/rdr/*.md"
    how = brief_ok(prose, "build", "web/page.html")
    assert _line(how, "Genre paths:") == "Genre paths: web/*.html"


def test_genre_paths_list_files_when_a_glob_would_reach_other_genres(prose: Prose, repo: Path) -> None:
    _files(repo, ["notes/one.md", "notes/two.md", "notes/three.md"])
    seed(prose, "repo", lists={"genre_map": ["notes/one.md=changelog", "notes/two.md=changelog"]})
    text = brief_ok(prose, "build", "notes/one.md")
    assert _line(text, "Genre paths:") == "Genre paths: notes/two.md"  # notes/*.md would include three.md


def test_genre_paths_are_none_when_no_other_document_has_the_genre(prose: Prose, repo: Path, tmp_path: Path) -> None:
    src = tmp_path / "m.txt"
    src.write_text("fix: a thing\n")
    text = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(src))
    assert _line(text, "Genre paths:") == "Genre paths: none"
    assert "Exclude from the search" not in text
    _files(repo, ["docs/exploration/only.md"])
    alone = brief_ok(prose, "build", "docs/exploration/only.md")
    assert _line(alone, "Genre paths:") == "Genre paths: none"


def test_new_prose_is_the_targets_changed_lines_from_the_working_tree_and_the_index(
    prose: Prose, repo: Path
) -> None:
    assert _line(brief_ok(prose, "build", "docs/x.md"), "New prose:") == "New prose: none (no changed lines)"
    (repo / "docs" / "x.md").write_text("one\nTWO\nthree\nfour\nfive\n")
    git(repo, "add", "docs/x.md")  # staged change
    (repo / "docs" / "x.md").write_text("one\nTWO\nthree\nFOUR\nfive\nsix\n")  # plus an unstaged one
    line = _line(brief_ok(prose, "build", "docs/x.md"), "New prose:")
    assert line == "New prose: lines 2-2, 4-4, 6-6. An em dash on any other line is a query, never an edit."


def test_new_prose_for_an_untracked_file_is_all_of_it_and_for_stdin_all_text(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    (repo / "docs" / "fresh.md").write_text("brand new\n")
    assert _line(brief_ok(prose, "build", "docs/fresh.md"), "New prose:").startswith(
        "New prose: all lines (the file is not tracked)")
    src = tmp_path / "m.txt"
    src.write_text("fix: a thing\n")
    text = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(src))
    assert _line(text, "New prose:").startswith("New prose: all text is new")


def test_new_prose_for_the_changelog_adds_the_unreleased_section(prose: Prose, repo: Path) -> None:
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- new one\n\n## [1.0.0] - 2026-01-01\n\n- old one\n")
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "changelog")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- new one\n\n## [1.0.0] - 2026-01-01\n\n- older one\n")
    line = _line(brief_ok(prose, "build", "CHANGELOG.md:1-20"), "New prose:")
    assert line == "New prose: lines 3-7, 11-11. An em dash on any other line is a query, never an edit."


# ---------------------------------------------------------------------------
# Section 6 device exception, and text that the reviews pinned
# ---------------------------------------------------------------------------


def test_section_six_carries_the_device_exception(prose: Prose) -> None:
    text = brief_ok(prose, "build", "docs/x.md")
    six = text[text.index(ORDER[5]):]
    assert "Prefer cutting to rewriting" in six
    assert "Never cut a voice-card device, however this section or a diagnostic reads." in six


def test_brief_py_has_no_type_ignore_and_parse_is_in_the_argparse_tree() -> None:
    assert "type: ignore" not in BRIEF.read_text(encoding="utf-8")
    proc = subprocess.run([sys.executable, str(BRIEF), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0 and "parse" in proc.stdout and "rmtmp" in proc.stdout


def test_section3_bullets_refuses_an_empty_section() -> None:
    mod = _module()
    with pytest.raises(mod.UserError, match="no bullets"):
        mod.section3_bullets("# t\n\n## 3. Register\n\nNo bullets here.\n\n## 4. Next\n")


def test_duplicate_flags_and_non_ascii_digits_are_refused() -> None:
    mod = _module()
    for tokens in (["a.md", "--budget", "3", "--budget", "4"], ["a.md", "--genre", "rdr", "--genre=rdr"],
                   ["rejections", "a.md", "--remove", "1", "--remove", "2"]):
        with pytest.raises(mod.UserError, match="more than once"):
            mod.parse_invocation(tokens)
    for tokens in (["a.md", "--budget=１２"], ["a.md:１-２"], ["exemplar", "rdr", "a.md:１-２"]):
        with pytest.raises(mod.UserError):
            mod.parse_invocation(tokens)


def test_a_literal_path_after_dash_dash_is_seen_even_when_its_text_repeats_an_earlier_token() -> None:
    got = _module().parse_invocation(["--genre", "rdr", "--", "rdr"])
    assert got["path"] == "rdr" and got["genre"] == "rdr" and got["stdin"] is False


# ---------------------------------------------------------------------------
# The agent and skill files: each assertion binds to the text it names
# ---------------------------------------------------------------------------


def _agent() -> str:
    return AGENT.read_text(encoding="utf-8")


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_protected_region_bullets_are_the_ones_the_filter_enforces() -> None:
    text = _agent()
    section = text[text.index("## Protected regions"):text.index("## Diagnostic questions")]
    for bullet in ("- a block quote;", "- a code block", "- a table;", "- frontmatter;", "- an HTML comment"):
        assert bullet in section, bullet


def test_the_agent_scopes_the_recurrence_search_to_the_brief_and_guards_devices() -> None:
    text = _agent()
    assert "Grep only the paths on the brief's \"Genre paths:\" line" in text
    assert "never the document itself" in text
    assert "Never cut a voice-card device, however section 6 of the brief or a diagnostic reads." in text
    assert "turn it into a query" in text  # a closing line with no twin becomes a query
    # Sam 2026-09-30: restructuring a three-item construction may be proposed, unless it is a voice-card device.
    assert "You may propose restructuring a three-item construction" in text
    assert "unless it is a device on the voice card; a device stays as it is." in text
    assert "is a query, not an edit or a paragraph proposal" not in text


def test_the_agent_treats_only_pure_filler_as_a_default_cut() -> None:
    text = _agent()
    assert '"basically", "really", "quite" and "just" used as filler' in text
    assert "Every other qualifier or intensifier is a query" in text
    rule = next(ln for ln in text.splitlines() if ln.startswith("- Cut only the filler words"))
    assert "unless" not in rule and rule.endswith("is a query.")
    assert "the rule in this file wins" in text  # the guards beat a style-sheet diagnostic


def test_the_agent_has_the_large_file_and_sentence_and_query_rules() -> None:
    text = _agent()
    assert "too large to read whole" in text and "opening, middle and end" in text
    assert "at most one sentence" in text
    assert "A query never carries replacement wording." in text
    assert "outside the brief's \"New prose\" lines is a query" in text
    row = next(ln for ln in text.splitlines() if ln.startswith("| Would a reader new to the project"))
    assert "ask a query" in row and "propose" not in row.lower()


def test_the_skill_is_explicit_only_deletes_work_on_every_stop_and_splits_stdin_at_the_first_line() -> None:
    text = _skill()
    assert _frontmatter(SKILL)["disable-model-invocation"] == "true"
    assert "On every stop after WORK exists and before step 12, delete WORK first with `BRIEF rmtmp '<work>'`." in text
    # a path run never calls tmpdir: build --work makes WORK only once the brief exists
    assert text.count("BRIEF tmpdir") == 1
    tmp_line = next(ln for ln in text.splitlines() if "BRIEF tmpdir" in ln)
    assert tmp_line.strip().startswith("| true |")
    assert "`BRIEF build <target> --budget <budget> [--genre <genre>] --work`" in text
    assert "A successful apply has already deleted WORK" in text  # the copy lives in WORK until the author answers
    assert "The flags are the first line. The text is everything after the first newline." in text
    assert "For a stdin run pass only `-` and the flags to `parse`." in text
    assert "| `-- <path>` or `./<path>` | A file named `rejections` or `exemplar`. |" in text


FIXTURES = Path(__file__).parent / "fixtures"


def test_the_acceptance_fixtures_exist_and_the_protected_one_has_four_protected_regions() -> None:
    names = {p.name for p in FIXTURES.iterdir()}
    assert {"protected.md", "qualifiers-a.md", "qualifiers-b.md", "qualifiers-c.md",
            "refrain-no-twin.md", "unmapped-notes.txt"} <= names
    mod = _module()
    text = (FIXTURES / "protected.md").read_text(encoding="utf-8")
    spans = mod.protected_spans(text)

    def covered(needle: str) -> bool:
        i = text.index(needle)
        return any(a <= i and i + len(needle) <= b for a, b in spans)

    for inside in ("This very basically explains", "Basically, the original design note", "return queue.pop()",
                   "A very basically important expiry column"):
        assert covered(inside), inside
    assert not covered("The scheduler basically hands each job")
    assert not covered("The retry path is in fact identical")


def test_the_skill_never_lets_the_model_pick_a_genre_and_the_agent_greps_by_glob() -> None:
    skill = _skill()
    row = next(ln for ln in skill.splitlines() if ln.strip().startswith("| exit 1, stderr contains `no genre`"))
    assert "Never choose a genre yourself." in row and "End your turn with that question" in row
    agent = _agent()
    assert "Pass each entry as the Grep `glob`, with `path` left at the repository root." in agent


def test_a_failed_helper_stops_the_model_without_naming_an_action_to_avoid() -> None:
    skill = _skill()
    line = "On any failure, show stderr and stop."
    assert skill.count(line) >= 4  # steps 1, 4 and 9 and the Rules table
    assert "If a `MEMORY` command fails, show stderr and stop." in skill
    assert line in skill[skill.index("## Rules"):]
    assert "Use Grep for nothing else." in _agent()


def test_no_instruction_the_model_reads_names_nx_a_service_or_a_repair() -> None:
    """Sam, nexus-ger02.15: naming the forbidden action primes the model to do it. A five-times-repeated
    prohibition did not stop `uv run nx daemon service start` after a T2 failure, so the wording is gone
    and the instruction is the neutral one: show stderr and stop."""
    skill = _skill()
    assert not NAMES_A_REPAIR.search(skill), NAMES_A_REPAIR.search(skill)
    assert not NAMES_A_REPAIR.search(_agent())
    for script in (BRIEF, REVIEW):
        text = script.read_text(encoding="utf-8")
        assert "Stop here and tell the author." in text
        assert "start a service" not in text and "repair anything" not in text


def test_a_null_genre_from_parse_is_not_a_reason_to_ask() -> None:
    assert "A null `genre` in the output only means the flag was not given for a path run." in _skill()
    assert "step 4 finds the genre from the path and reports when none exists" in _skill()


# ---------------------------------------------------------------------------
# Delta review: stdin needs a genre, strict rmtmp, markup, root globs, renames
# ---------------------------------------------------------------------------


def test_a_stdin_run_without_a_genre_is_refused_at_parse() -> None:
    mod = _module()
    with pytest.raises(mod.UserError, match="stdin run needs --genre"):
        mod.parse_invocation(["-"])
    with pytest.raises(mod.UserError, match="stdin run needs --genre"):
        mod.parse_invocation(["-", "--budget", "3"])
    assert mod.parse_invocation(["-", "--genre", "commit-message"])["stdin"] is True
    assert mod.parse_invocation(["--", "-"])["stdin"] is False  # a file named "-"


def test_tmpdir_writes_the_sentinel_and_rmtmp_refuses_lookalikes(prose: Prose, tmp_path: Path) -> None:
    work = Path(brief_ok(prose, "tmpdir").strip())
    other = Path(brief_ok(prose, "tmpdir").strip())
    base = prose.tmp
    locks = base / f"prose-edit-locks-{os.getuid()}"
    made = not locks.exists()
    locks.mkdir(exist_ok=True)
    shaped = base / "prose-edit-abcd1234"  # right shape, no sentinel
    shaped.mkdir()
    try:
        assert (work / ".prose-edit-work").is_file()
        assert run_brief(prose, "rmtmp", str(locks)).returncode == 1 and locks.exists()
        (locks / ".prose-edit-work").write_text("planted")  # even a planted sentinel: the name shape fails
        assert run_brief(prose, "rmtmp", str(locks)).returncode == 1 and locks.exists()
        assert run_brief(prose, "rmtmp", str(shaped)).returncode == 1 and shaped.exists()
        dotdot = run_brief(prose, "rmtmp", f"{work}/../{other.name}")
        assert dotdot.returncode == 1 and "'..'" in dotdot.stderr and other.exists() and work.exists()
        assert run_brief(prose, "rmtmp", f"{work}/..").returncode == 1
    finally:
        (locks / ".prose-edit-work").unlink(missing_ok=True)
        if made:
            locks.rmdir()
        shaped.rmdir()
        brief_ok(prose, "rmtmp", str(work))
        brief_ok(prose, "rmtmp", str(other))


def test_html_markup_is_not_editable_but_the_text_between_tags_is(prose: Prose, repo: Path) -> None:
    (repo / "web").mkdir(exist_ok=True)
    (repo / "web" / "m.html").write_text(
        '<p class="lede">Plain words here. <a href="x.html">link text</a> and <em>more</em> words.</p>\n')
    edits = [edit(1, 'class="lede"', ""), edit(2, "<em>more</em>", "more"), edit(3, "Plain words here.", "Plain."),
             edit(4, 'href="x.html">link text</a> and', "link text and")]
    got = _filter(prose, "web/m.html", edits)
    assert [e["n"] for e in got["edits"]] == [3]
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {1: "markup", 2: "markup", 4: "markup"}


def test_a_root_level_glob_is_anchored_and_does_not_reach_subdirectories(prose: Prose, repo: Path) -> None:
    (repo / "CHANGELOG.md").write_text("# c\n")
    (repo / "notes.md").write_text("n\n")
    seed(prose, "repo", lists={"genre_map": ["notes.md=changelog"]})
    text = brief_ok(prose, "build", "CHANGELOG.md")
    assert _line(text, "Genre paths:") == "Genre paths: /*.md"
    assert "docs/x.md" not in _line(text, "Genre paths:")


def test_a_staged_rename_counts_only_its_changed_lines_and_crlf_lines_count_once(
    prose: Prose, repo: Path
) -> None:
    (repo / "docs" / "d.md").write_text("one\ntwo\nthree\n")
    (repo / "docs" / "crlf.md").write_bytes(b"a\r\nb\r\nc\r\n")
    git(repo, "add", "docs/d.md", "docs/crlf.md")
    git(repo, "commit", "-q", "-m", "more")
    git(repo, "mv", "docs/d.md", "docs/e.md")
    (repo / "docs" / "e.md").write_text("one\nTWO\nthree\n")
    git(repo, "add", "docs/e.md")
    line = _line(brief_ok(prose, "build", "docs/e.md"), "New prose:")
    assert line == "New prose: lines 2-2. An em dash on any other line is a query, never an edit."
    (repo / "docs" / "p.md").write_text("alpha\nbeta\n")
    git(repo, "add", "docs/p.md")
    git(repo, "commit", "-q", "-m", "p")
    git(repo, "mv", "docs/p.md", "docs/q.md")  # a pure rename adds no new prose
    assert _line(brief_ok(prose, "build", "docs/q.md"), "New prose:") == "New prose: none (no changed lines)"
    (repo / "docs" / "crlf.md").write_bytes(b"a\r\nB\r\nc\r\n")
    crlf = _line(brief_ok(prose, "build", "docs/crlf.md"), "New prose:")
    assert crlf == "New prose: lines 2-2. An em dash on any other line is a query, never an edit."


def test_section_six_says_only_filler_is_cut_and_the_agent_finds_near_twins() -> None:
    mod = _module()
    read = {"genre": "reference-doc", "path": "docs/x.md", "range": None, "merged": {"scalars": {}, "lists": {}},
            "genre_record": None}
    six = mod.render_brief(read, 10, None)
    six = six[six.index("## 6. Prefer cutting"):]
    assert "Cut only filler words; every other qualifier or intensifier is a query." in six
    agent = _agent()
    assert "When the fifth word is a name or term, use the first three words instead." in agent
    assert 'write "no exact twin found", never "no twin"' in agent


# ---------------------------------------------------------------------------
# Work directories made structurally: build --work, filter --work, the stale sweep
# ---------------------------------------------------------------------------


def _work_dirs(prose: Prose) -> set[str]:
    return {p.name for p in prose.tmp.glob("prose-edit-*") if (p / ".prose-edit-work").exists()}


def test_build_work_makes_the_directory_only_after_the_brief_exists(prose: Prose, repo: Path) -> None:
    before = _work_dirs(prose)
    (repo / "notes.txt").write_text("hello\n")
    failed = run_brief(prose, "build", "notes.txt", "--work")  # no genre: nothing may be created
    assert failed.returncode == 1 and _work_dirs(prose) == before
    out = brief_ok(prose, "build", "docs/x.md", "--work")
    first, dispatch, *ready, blank, rest = out.split("\n", 7)
    assert first.startswith("WORK=") and dispatch.startswith("DISPATCH=") and blank == ""
    assert [r.split("=", 1)[0] for r in ready] == ["REPLY", "FILTERED", "REASONS", "ANSWERS"]
    assert rest.startswith("# Editing brief") and "BRIEF_SHA" not in out
    work = Path(first[len("WORK="):])
    try:
        assert (work / ".prose-edit-work").is_file() and _work_dirs(prose) - before == {work.name}
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_the_prose_fixture_gives_each_test_a_private_temp_directory_outside_the_repo(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    # without the fixture's TMPDIR line, prose.tmp is the shared temp dir and every check below still holds
    # but this one: the private directory must sit under this test's own tmp_path
    assert tmp_path.resolve() in prose.tmp.resolve().parents
    work = Path(brief_ok(prose, "tmpdir").strip())
    try:
        assert tmp_path.resolve() in work.resolve().parents
        assert work.parent == prose.tmp.resolve() and repo.resolve() not in prose.tmp.resolve().parents
        assert prose.tmp.resolve() != repo.resolve()
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_a_work_directory_never_has_a_work_directory_name_before_its_sentinel_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _module()
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setattr(mod.tempfile, "tempdir", str(base))
    seen: list[str] = []
    real = mod.tempfile.mkdtemp

    def spy(*args: object, **kw: object) -> str:
        path = real(*args, **kw)
        seen.append(Path(path).name)
        return path

    monkeypatch.setattr(mod.tempfile, "mkdtemp", spy)
    monkeypatch.chdir(tmp_path)  # not a git repository: no containment check
    work = Path(mod.cmd_tmpdir())
    assert (work / ".prose-edit-work").is_file() and work.parent == base.resolve()
    assert mod._WORK_NAME.fullmatch(work.name)
    # the directory mkdtemp made, which has no sentinel yet, is not shaped like a work directory
    assert seen and not any(mod._WORK_NAME.fullmatch(name) for name in seen)
    assert sorted(p.name for p in base.iterdir()) == [work.name]


def test_build_work_is_refused_for_a_stdin_run(prose: Prose, tmp_path: Path) -> None:
    src = tmp_path / "m.txt"
    src.write_text("fix: a thing\n")
    proc = run_brief(prose, "build", "-", "--genre", "commit-message", "--file", str(src), "--work")
    assert proc.returncode == 1 and "--work is for a path run" in proc.stderr and proc.stdout == ""


def test_a_successful_filter_deletes_its_work_directory_and_a_failed_one_keeps_it(
    prose: Prose, repo: Path
) -> None:
    (repo / "docs" / "s.md").write_text("An editable sentence here.\n", encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    bad = run_brief(prose, "filter", "docs/s.md", "--work", str(work), stdin="no json here")
    assert bad.returncode == 1 and work.is_dir()
    good = run_brief(prose, "filter", "docs/s.md", "--work", str(work),
                     stdin=fenced(proposal([edit(1, "An editable sentence here.", "")])))
    assert good.returncode == 0 and json.loads(good.stdout)["edits"][0]["n"] == 1
    assert not work.exists()
    refused = run_brief(prose, "filter", "docs/s.md", "--work", str(repo),
                        stdin=fenced(proposal([edit(1, "An editable sentence here.", "")])))
    assert refused.returncode == 1 and refused.stdout == "" and repo.is_dir()


def test_tmpdir_sweeps_work_directories_idle_for_two_hours_but_not_active_or_lookalike_ones(prose: Prose) -> None:
    """The key is the sentinel's mtime, which review.py renews on render, apply and log-retry (see
    test_fix_round1.py): a directory is swept for being idle, not for being old."""
    base = prose.tmp
    old = base / "prose-edit-oldwork1"
    young = base / "prose-edit-younger1"
    bare = base / "prose-edit-nosentin"
    for d in (old, young, bare):
        d.mkdir()
    for d in (old, young):
        (d / ".prose-edit-work").write_text("x")
    os.utime(old / ".prose-edit-work", (1, 1))
    os.utime(bare, (1, 1))
    building = base / ".prose-edit-building-zz9"  # what a `tmpdir` killed before its rename leaves
    building.mkdir()
    os.utime(building, (1, 1))
    fresh = Path(brief_ok(prose, "tmpdir").strip())
    try:
        assert not old.exists() and not building.exists()
        assert young.exists() and bare.exists() and fresh.exists()
    finally:
        for d in (young, bare):
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
        brief_ok(prose, "rmtmp", str(fresh))
