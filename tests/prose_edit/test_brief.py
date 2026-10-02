# SPDX-License-Identifier: AGPL-3.0-or-later
"""brief.py (RDR-221 Step 1.4, nexus-ger02.3): grammar, site-page layer, brief, agent output.

Pure pieces are imported and called; everything that touches T2 runs the script as a
subprocess against the real engine substrate (the `prose` fixture), with the project
prefix override so the live prose projects are never touched.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.prose_edit.conftest import ROOT, Prose

SKILLS = ROOT / ".claude" / "skills"
BRIEF = SKILLS / "prose-edit" / "scripts" / "brief.py"
MEMORY = SKILLS / "prose-edit" / "scripts" / "memory.py"
SITE_PAGE = SKILLS / "site-page" / "SKILL.md"
AGENT = ROOT / ".claude" / "agents" / "line-editor.md"
SKILL = SKILLS / "prose-edit" / "SKILL.md"
# What no instruction or message the model reads may name (nexus-ger02.15): naming the action primes it.
NAMES_A_REPAIR = re.compile(r"\bnx\b|start a service|repair|daemon|doctor", re.IGNORECASE)

# The six treatments the live repo style sheet holds, decoded exactly as `nx memory get` returns them.
IGNORED = [
    '"The terms table holds only words the lessons use..." (page structure)',
    '"Placeholders as <span class=\\"ph\\">..." (page structure)',
    '"Rendered output blocks come from real tool or CLI output..." (page structure)',
]
QUERY_ONLY = [
    '"No corpus counts, incident dates, or self-reference to this project\'s incidents on a web/ page..." (all three clauses)',
    '"No volume figures that drift with configuration..."',
]
NOTE_ONLY = ['"Motivate a new thing at the edge of what already exists and works..." (editor\'s note only)']
TREATMENTS = {
    "site_page_section3_ignored": IGNORED,
    "site_page_section3_query_only": QUERY_ONLY,
    "site_page_section3_note_only": NOTE_ONLY,
}


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_brief", BRIEF)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_brief(prose: Prose, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BRIEF), *args], input=stdin or "", capture_output=True, text=True,
        cwd=prose.cwd, env=prose.env, timeout=240,
    )


def brief_ok(prose: Prose, *args: str, stdin: str | None = None) -> str:
    proc = run_brief(prose, *args, stdin=stdin)
    assert proc.returncode == 0, f"{args}: rc={proc.returncode}\n{proc.stderr}"
    return proc.stdout


def seed(prose: Prose, level: str, scalars: dict | None = None, lists: dict | None = None,
         path: str | None = None) -> None:
    args = ["add-entry", "--level", level, "--from-stdin"]
    if path:
        args += ["--path", path]
    prose.ok(*args, stdin={"scalars": scalars or {}, "lists": lists or {}})


def seed_genre(prose: Prose, name: str, text: str, notes: list[str] | None = None) -> None:
    prose.ok("genre-put", name, stdin={
        "exemplars": [{"text": text, "path": "docs/x.md", "start": 2, "end": 3}],
        "notes": notes or [],
    })


def proposal(edits: list[dict], **extra: object) -> dict:
    return {"voice_card": "vc", "note": "n", "paragraphs": [], "edits": edits, "queries": [], **extra}


def edit(n: int, old: str, new: str = "", reason: str = "r") -> dict:
    return {"n": n, "old": old, "new": new, "reason": reason}


def fenced(obj: object, before: str = "Here is my pass.\n", after: str = "") -> str:
    return f"{before}```json\n{json.dumps(obj, indent=1)}\n```\n{after}"


# ---------------------------------------------------------------------------
# Invocation grammar
# ---------------------------------------------------------------------------


def parse(*tokens: str) -> dict:
    return _module().parse_invocation(list(tokens))


def test_a_bare_path_is_an_edit_run_with_the_default_budget() -> None:
    got = parse("docs/x.md")
    assert got == {
        "mode": "edit", "path": "docs/x.md", "range": None, "stdin": False, "genre": None,
        "budget": 10, "target": "docs/x.md",
    }


def test_a_range_genre_and_budget_parse_in_either_order() -> None:
    a = parse("CHANGELOG.md:44-60", "--genre", "changelog", "--budget", "3")
    b = parse("--budget=3", "--genre=changelog", "CHANGELOG.md:44-60")
    assert a == b
    assert a["range"] == {"start": 44, "end": 60} and a["path"] == "CHANGELOG.md"
    assert a["target"] == "CHANGELOG.md:44-60" and a["budget"] == 3 and a["genre"] == "changelog"
    assert parse("docs/x.md:7")["range"] == {"start": 7, "end": 7}


def test_dash_is_the_stdin_channel() -> None:
    got = parse("-", "--genre", "commit-message")
    assert got["stdin"] is True and got["path"] is None and got["target"] == "-"
    assert got["genre"] == "commit-message" and got["range"] is None


def test_rejections_and_exemplar_map_onto_memory_verbs() -> None:
    assert parse("rejections", "docs/x.md") == {
        "mode": "rejections", "path": "docs/x.md", "remove": None,
        "memory_argv": ["rejections", "docs/x.md"],
    }
    got = parse("rejections", "docs/x.md", "--remove", "2")
    assert got["remove"] == 2 and got["memory_argv"] == ["rejections", "docs/x.md", "--remove", "2"]
    assert parse("rejections", "--remove", "2", "docs/x.md") == got
    ex = parse("exemplar", "changelog", "CHANGELOG.md:44-44")
    assert ex == {
        "mode": "exemplar", "genre": "changelog", "where": "CHANGELOG.md:44-44",
        "memory_argv": ["exemplar-add", "changelog", "CHANGELOG.md:44-44"],
    }


def test_a_file_named_rejections_or_exemplar_is_reached_with_dash_dash_or_dot_slash() -> None:
    assert parse("--", "rejections")["path"] == "rejections"
    assert parse("./exemplar")["path"] == "./exemplar"
    assert parse("rejections", "--", "rejections")["path"] == "rejections"
    assert parse("--", "-")["path"] == "-" and parse("--", "-")["stdin"] is False
    assert parse("--", "--genre")["path"] == "--genre"


@pytest.mark.parametrize(
    ("tokens", "fragment"),
    [
        ([], "usage"),
        (["a.md", "b.md"], "one path"),
        (["--genre", "poetry", "a.md"], "genre"),
        (["--genre"], "needs a value"),
        (["a.md", "--budget", "zero"], "budget"),
        (["a.md", "--budget", "0"], "budget"),
        (["a.md", "--budget", "-2"], "budget"),
        (["a.md:9-3"], "range"),
        (["a.md:0-3"], "range"),
        (["a.md", "--bogus"], "unknown flag"),
        (["a.md", "--remove", "1"], "--remove"),
        (["--genre", "rdr"], "path"),
        (["rejections"], "path"),
        (["rejections", "a.md", "--remove", "0"], "--remove"),
        (["rejections", "a.md", "--remove", "x"], "--remove"),
        (["rejections", "a.md:3-4"], "bare path"),
        (["rejections", "-"], "stdin"),
        (["rejections", "a.md", "b.md"], "one path"),
        (["exemplar"], "usage"),
        (["exemplar", "changelog"], "usage"),
        (["exemplar", "poetry", "a.md:1-2"], "genre"),
        (["exemplar", "changelog", "a.md"], "range"),
        (["exemplar", "changelog", "a.md:5-2"], "range"),
        (["exemplar", "changelog", "a.md:1-2", "extra"], "usage"),
        (["a.md", "--mode", "cut-only"], "not built"),
        (["a.md", "--voice-card", "f"], "not built"),
        (["a.md", "--word-budget", "5"], "not built"),
    ],
)
def test_malformed_invocations_are_refused_with_a_message(tokens: list[str], fragment: str) -> None:
    mod = _module()
    with pytest.raises(mod.UserError) as caught:
        mod.parse_invocation(tokens)
    assert fragment in str(caught.value)


def test_cli_parse_prints_json_and_exits_one_on_a_malformed_invocation() -> None:
    good = subprocess.run([sys.executable, str(BRIEF), "parse", "a.md:1-2", "--budget", "4"],
                          capture_output=True, text=True)
    assert good.returncode == 0 and json.loads(good.stdout)["budget"] == 4
    bad = subprocess.run([sys.executable, str(BRIEF), "parse", "a.md", "--budget", "x"],
                         capture_output=True, text=True)
    assert bad.returncode == 1 and bad.stdout == "" and "budget" in bad.stderr


# ---------------------------------------------------------------------------
# site-page section 3 -> memory.py's --site-layer
# ---------------------------------------------------------------------------

SKILL_TEXT = """\
# Site page

## 2. Before drafting

- Not a section 3 bullet.

## 3. Register (both genres)

- Plain technical English. Active voice.
- The terms table holds only words the lessons use. More than six means internals.
- Placeholders as `<span class="ph">&lt;ORANGE&gt;</span>`. Prompts are examples.
- No corpus counts, incident dates, or self-reference to this project's incidents on a `web/` page.
- Motivate a new thing at the edge of what already exists.
  Name the existing mechanism (continuation line).

## 4. House template

- Not a section 3 bullet either.
"""

TREAT_SMALL = {
    "ignored": ['"The terms table holds only words the lessons use..." (structure)',
                '"Placeholders as <span class=\\"ph\\">..." (structure)'],
    "query_only": ['"No corpus counts, incident dates, or self-reference..." (x)'],
    "note_only": ['"Motivate a new thing at the edge..." (x)'],
}


def test_section3_bullets_are_only_those_under_the_section_and_keep_continuations() -> None:
    mod = _module()
    bullets = mod.section3_bullets(SKILL_TEXT)
    assert len(bullets) == 5
    assert bullets[0] == "Plain technical English. Active voice."
    assert bullets[4].endswith("(continuation line).") and "Name the existing" in bullets[4]
    with pytest.raises(mod.UserError, match=r"section 3"):
        mod.section3_bullets("# nothing\n\n## 4. House\n\n- x\n")


def test_the_site_layer_drops_ignored_marks_query_and_note_and_keeps_the_rest() -> None:
    mod = _module()
    layer = mod.build_site_layer(mod.section3_bullets(SKILL_TEXT), TREAT_SMALL)
    assert layer["scalars"] == {}
    lists = layer["lists"]
    assert lists["site_page_rules"] == ["Plain technical English. Active voice."]
    assert [q.startswith("No corpus counts") for q in lists["site_page_queries"]] == [True]
    assert lists["site_page_editors_note"][0].startswith("Motivate a new thing")
    joined = json.dumps(layer)
    assert "terms table" not in joined and "Placeholders" not in joined  # dropped outright


def test_a_listed_treatment_that_matches_no_bullet_fails_loud() -> None:
    mod = _module()
    bullets = mod.section3_bullets(SKILL_TEXT)
    stale = {**TREAT_SMALL, "ignored": TREAT_SMALL["ignored"] + ['"A rule someone deleted..." (gone)']}
    with pytest.raises(mod.UserError, match=r"A rule someone deleted"):
        mod.build_site_layer(bullets, stale)
    edited = SKILL_TEXT.replace("The terms table holds", "The glossary holds")
    with pytest.raises(mod.UserError, match=r"terms table"):
        mod.build_site_layer(mod.section3_bullets(edited), TREAT_SMALL)


def test_an_entry_without_an_opening_quote_is_refused_and_ambiguity_is_refused() -> None:
    mod = _module()
    bullets = mod.section3_bullets(SKILL_TEXT)
    with pytest.raises(mod.UserError, match=r"treatment entry"):
        mod.build_site_layer(bullets, {**TREAT_SMALL, "ignored": ["no quotes here"]})
    both = {**TREAT_SMALL, "ignored": ['"Plain..." (x)'], "note_only": ['"Plain tech..." (y)']}
    with pytest.raises(mod.UserError, match=r"more than one"):
        mod.build_site_layer(bullets, both)


def test_the_live_style_sheet_treatments_all_match_the_real_site_page_skill() -> None:
    """Drift guard: editing a section 3 bullet's opening words must break this before it silently
    changes what the editor applies."""
    mod = _module()
    bullets = mod.section3_bullets(SITE_PAGE.read_text(encoding="utf-8"))
    layer = mod.build_site_layer(bullets, mod.treatments_from_lists(TREATMENTS))
    lists = layer["lists"]
    assert len(lists["site_page_queries"]) == 2 and len(lists["site_page_editors_note"]) == 1
    assert len(lists["site_page_rules"]) == len(bullets) - 6 > 0
    assert not any("terms table" in r or "Placeholders as" in r or "Rendered output" in r
                   for r in lists["site_page_rules"])


# ---------------------------------------------------------------------------
# brief assembly (real T2)
# ---------------------------------------------------------------------------

ORDER = ["## 1. Exemplars", "## 2. Voice card", "## 3. Style sheet", "## 4. Not a defect",
         "## 5. Budget", "## 6. Prefer cutting"]


def test_the_brief_carries_each_layer_in_the_rdr_order_with_the_later_layer_winning(
    prose: Prose, repo: Path
) -> None:
    seed(prose, "user", {"tone": "user-tone"}, {"banned": ["user-banned"]})
    seed(prose, "repo", {"tone": "repo-tone", "audience": "repo-audience"},
         {"diagnostics": ["Is the actor the subject? Leave it for a refrain."],
          "genre_map": ["notes/=how-to"]})
    seed(prose, "doc", {"tone": "doc-tone"}, {"banned": ["doc-banned"]}, path="docs/x.md")
    seed_genre(prose, "reference-doc", "EXEMPLAR-PASSAGE-TEXT", ["genre-note-one"])
    prose.ok("reject", "docs/x.md", "--old", "OLD-REJECTED", "--new", "NEW-REJECTED")
    prose.ok("promote", "docs/x.md", "1", "--level", "repo")

    text = brief_ok(prose, "build", "docs/x.md", "--budget", "7")
    positions = [text.index(h) for h in ORDER]
    assert positions == sorted(positions)
    exemplars = text[positions[0]:positions[1]]
    assert "EXEMPLAR-PASSAGE-TEXT" in exemplars and "docs/x.md" in exemplars
    assert "genre-note-one" in exemplars
    sheet = text[positions[2]:positions[3]]
    assert "tone: doc-tone" in sheet and "user-tone" not in sheet and "repo-tone" not in sheet
    assert "repo-audience" in sheet and "user-banned" in sheet and "doc-banned" in sheet
    assert "Is the actor the subject?" in sheet
    assert "genre_map" not in sheet and "notes/=how-to" not in sheet  # routing data, not guidance
    assert "OLD-REJECTED" in text[positions[3]:positions[4]]
    assert "7" in text[positions[4]:positions[5]]
    assert "Prefer cutting to rewriting" in text[positions[5]:]
    assert "voice_card" in text[positions[1]:positions[2]]


def test_a_known_genre_with_no_record_runs_without_exemplars_and_says_so(
    prose: Prose, tmp_path: Path
) -> None:
    src = tmp_path / "msg.txt"
    src.write_text("fix: make the queue drain in order\n\nThe body text of the message.\n")
    text = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(src))
    exemplars = text[text.index(ORDER[0]):text.index(ORDER[1])]
    assert "No exemplars are stored for genre commit-message" in exemplars
    assert "editor's note" in exemplars
    assert "stdin" in text.lower() and str(src) in text
    tail = text[text.index("## Text to edit"):]  # the text rides in the brief: the agent reads no temp file
    assert "<document>\nfix: make the queue drain in order" in tail
    assert tail.rstrip().endswith("</document>")
    missing = run_brief(prose, "build", "-", "--genre", "commit-message", "--file", str(tmp_path / "nope"))
    assert missing.returncode == 1 and "--file" in missing.stderr


def test_an_unmapped_path_with_no_genre_is_refused_so_the_skill_asks(prose: Prose, repo: Path) -> None:
    (repo / "notes.txt").write_text("hello\n")
    proc = run_brief(prose, "build", "notes.txt")
    assert proc.returncode == 1 and proc.stdout == ""
    assert "no genre" in proc.stderr and "ask the author" in proc.stderr
    proc = run_brief(prose, "build", "-", "--file", str(repo / "notes.txt"))
    assert proc.returncode == 1 and "no genre" in proc.stderr and "ask the author" in proc.stderr
    # The same path with a genre given builds.
    assert ORDER[0] in brief_ok(prose, "build", "notes.txt", "--genre", "reference-doc")


def test_a_stdin_build_needs_the_input_file_and_reads_no_document_record(prose: Prose) -> None:
    proc = run_brief(prose, "build", "-", "--genre", "commit-message")
    assert proc.returncode == 1 and "--file" in proc.stderr
    proc = run_brief(prose, "build", "docs/x.md", "--file", "/tmp/y")
    assert proc.returncode == 1 and "--file" in proc.stderr


def test_a_range_run_names_the_range_and_says_the_voice_card_is_the_whole_files(
    prose: Prose,
) -> None:
    text = brief_ok(prose, "build", "docs/x.md:2-3", "--genre", "reference-doc")
    assert "lines 2-3" in text
    assert "whole file" in text
    assert "docs/x.md" in text


def test_a_how_to_brief_gets_section3_with_ignored_dropped_and_query_and_note_marked(
    prose: Prose, repo: Path
) -> None:
    seed(prose, "repo", lists={**TREATMENTS, "diagnostics": ["Q?"]})
    (repo / "web").mkdir()
    (repo / "web" / "page.html").write_text("<p>hi</p>\n")
    text = brief_ok(prose, "build", "web/page.html")
    sheet = text[text.index(ORDER[2]):text.index(ORDER[3])]
    assert "Active voice, no semicolons" in sheet  # an applied rule, read from the real skill
    assert "The terms table holds" not in sheet and "Placeholders as" not in sheet
    assert "Rendered output blocks" not in sheet
    assert "QUERY" in sheet and "No corpus counts" in sheet
    assert "Motivate a new thing" in sheet and "editor's note" in sheet.lower()
    assert "site_page_section3" not in sheet  # the treatment lists are the helper's, not the editor's


def test_a_reference_doc_brief_gets_no_section3(prose: Prose) -> None:
    seed(prose, "repo", lists={**TREATMENTS, "diagnostics": ["Q?"]})
    text = brief_ok(prose, "build", "docs/x.md")
    assert "Active voice, no semicolons" not in text and "Motivate a new thing" not in text


def test_a_stale_treatment_stops_the_build_for_a_site_genre_only(prose: Prose) -> None:
    stale = {**TREATMENTS, "site_page_section3_ignored": IGNORED + ['"A deleted rule..." (gone)']}
    seed(prose, "repo", lists=stale)
    proc = run_brief(prose, "build", "docs/exploration/e.md", "--genre", "exploration-essay")
    assert proc.returncode == 1 and "A deleted rule" in proc.stderr
    assert run_brief(prose, "build", "docs/x.md").returncode == 0  # reference-doc never reads it


def test_an_edited_site_page_skill_stops_the_build(prose: Prose, tmp_path: Path) -> None:
    seed(prose, "repo", lists=TREATMENTS)
    edited = tmp_path / "site.md"
    edited.write_text(SITE_PAGE.read_text().replace("The terms table holds", "The glossary holds"))
    proc = run_brief(prose, "build", "docs/exploration/e.md", "--genre", "exploration-essay",
                     "--site-page", str(edited))
    assert proc.returncode == 1 and "terms table" in proc.stderr


def test_t2_being_down_is_not_answered_with_an_empty_brief(prose: Prose) -> None:
    prose.env["PROSE_EDIT_NX"] = f"{sys.executable} -c 'import sys; sys.exit(9)'"
    proc = run_brief(prose, "build", "docs/x.md")
    assert proc.returncode != 0 and proc.stdout == ""


def test_tmpdir_is_outside_the_repo_and_exists(prose: Prose, repo: Path) -> None:
    out = Path(brief_ok(prose, "tmpdir").strip())
    try:
        assert out.is_dir() and repo not in out.parents and out != repo
    finally:
        shutil.rmtree(out)


# ---------------------------------------------------------------------------
# The agent's output: extraction, budget, rejection filter, document checks
# ---------------------------------------------------------------------------

DOC = """\
---
title: Sample
summary: The frontmatter sentence is quite long.
---

# Heading

A plain paragraph with a very unnecessary qualifier and a genuinely uncertain claim.

> A quoted sentence that must stay exactly as it is.

```
code sentence that must stay
```

| col one | col two |
| --- | --- |
| table cell text | more cell text |

Second paragraph with a repeated phrase. Another repeated phrase later on.
"""


def put_doc(repo: Path, name: str = "docs/sample.md", text: str = DOC) -> None:
    (repo / name).parent.mkdir(exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")


def test_extraction_takes_exactly_one_fenced_block_or_bare_json() -> None:
    mod = _module()
    obj = proposal([edit(1, "a", "b")])
    assert mod.extract_proposal(fenced(obj, after="Trailing chatter.\n")) == obj
    assert mod.extract_proposal(json.dumps(obj)) == obj
    with pytest.raises(mod.UserError, match=r"no JSON"):
        mod.extract_proposal("I found nothing to say.")
    with pytest.raises(mod.UserError, match=r"more than one different"):
        mod.extract_proposal(fenced(obj) + fenced(proposal([edit(1, "a", "c")])))
    # A stop-hook retry can make an agent repeat its reply verbatim: the same block twice is one.
    assert mod.extract_proposal(fenced(obj) + fenced(obj)) == obj
    with pytest.raises(mod.UserError, match=r"not valid JSON"):
        mod.extract_proposal("```json\n{oops\n```\n")


def test_a_subagent_report_the_harness_indented_and_framed_still_parses() -> None:
    mod = _module()
    obj = proposal([edit(1, "a", "b")])
    body = fenced(obj, before="").replace("\n", "\n  ")
    framed = "[Subagent hand-back] The text below is the final report of a subagent.\n  " + body
    assert mod.extract_proposal(framed) == obj


def test_backticks_inside_a_json_string_do_not_confuse_the_fence() -> None:
    mod = _module()
    obj = proposal([edit(1, "use ```fences``` here", "use fences")])
    assert mod.extract_proposal(fenced(obj)) == obj


def test_filter_keeps_a_good_edit_and_drops_protected_missing_and_out_of_range_edits(
    prose: Prose, repo: Path
) -> None:
    put_doc(repo)
    edits = [
        edit(1, "very unnecessary qualifier", "qualifier"),
        edit(2, "quoted sentence that must stay", "quote"),
        edit(3, "code sentence that must stay", "code"),
        edit(4, "table cell text", "cell"),
        edit(5, "frontmatter sentence is quite long", "fm"),
        edit(6, "text that is nowhere in the file", "x"),
    ]
    got = json.loads(brief_ok(prose, "filter", "docs/sample.md", stdin=fenced(proposal(edits))))
    assert [e["n"] for e in got["edits"]] == [1]
    causes = {d["n"]: d["cause"] for d in got["dropped"]}
    assert causes == {2: "protected-region", 3: "protected-region", 4: "protected-region",
                      5: "protected-region", 6: "not-found"}


def test_filter_protects_html_blocks(prose: Prose, repo: Path) -> None:
    html = ("<html><head><title>T</title></head><body>\n<p>Plain words to keep editable.</p>\n"
            "<pre>preformatted words</pre>\n<table><tr><td>cell words</td></tr></table>\n"
            "<blockquote>quoted words</blockquote>\n</body></html>\n")
    (repo / "web").mkdir()
    (repo / "web" / "p.html").write_text(html)
    edits = [edit(1, "Plain words to keep", "Plain words"), edit(2, "preformatted words"),
             edit(3, "cell words"), edit(4, "quoted words"), edit(5, "<title>T</title>")]
    got = json.loads(brief_ok(prose, "filter", "web/p.html", stdin=fenced(proposal(edits))))
    assert [e["n"] for e in got["edits"]] == [1]
    assert {d["cause"] for d in got["dropped"]} == {"protected-region"} and len(got["dropped"]) == 4


def test_filter_range_run_keeps_only_edits_inside_the_range(prose: Prose, repo: Path) -> None:
    lines = [f"Entry {i} has a word to trim and stay." for i in range(1, 9)]
    (repo / "CHANGELOG.md").write_text("\n".join(lines) + "\n")
    edits = [edit(1, "Entry 2 has a word to trim", "Entry 2 has a word"),
             edit(2, "Entry 5 has a word to trim", "Entry 5 has a word"),
             edit(3, "Entry 7 has a word to trim", "Entry 7 has a word")]
    got = json.loads(brief_ok(prose, "filter", "CHANGELOG.md:4-6", stdin=fenced(proposal(edits))))
    assert [e["n"] for e in got["edits"]] == [2]
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {1: "outside-range", 3: "outside-range"}


def test_an_old_string_found_inside_and_outside_a_protected_region_is_kept(
    prose: Prose, repo: Path
) -> None:
    put_doc(repo, text="A repeated phrase here.\n\n> A repeated phrase here.\n")
    got = json.loads(brief_ok(prose, "filter", "docs/sample.md",
                              stdin=fenced(proposal([edit(1, "A repeated phrase here.")]))))
    assert [e["n"] for e in got["edits"]] == [1]  # uniqueness is the apply step's call


def test_filter_clamps_to_the_budget_and_reports_the_excess(prose: Prose, repo: Path) -> None:
    put_doc(repo, text="".join(f"Sentence number {i} has fluff words.\n\n" for i in range(1, 6)))
    edits = [edit(i, f"Sentence number {i} has fluff words.", f"Sentence {i}.") for i in range(1, 6)]
    got = json.loads(brief_ok(prose, "filter", "docs/sample.md", "--budget", "3",
                              stdin=fenced(proposal(edits))))
    assert [e["n"] for e in got["edits"]] == [1, 2, 3]
    assert {d["n"]: d["cause"] for d in got["dropped"]} == {4: "over-budget", 5: "over-budget"}
    assert got["warnings"] and "budget" in got["warnings"][0]
    within = json.loads(brief_ok(prose, "filter", "docs/sample.md", "--budget", "9",
                                 stdin=fenced(proposal(edits))))
    assert len(within["edits"]) == 5 and within["warnings"] == []


def test_filter_applies_a_stored_rejection_through_memory_py(prose: Prose, repo: Path) -> None:
    put_doc(repo)
    prose.ok("reject", "docs/sample.md", "--old", "very unnecessary qualifier", "--new", "qualifier")
    edits = [edit(1, "very unnecessary qualifier", "qualifier"),
             edit(2, "genuinely uncertain claim", "uncertain claim")]
    got = json.loads(brief_ok(prose, "filter", "docs/sample.md", stdin=fenced(proposal(edits))))
    assert [e["n"] for e in got["edits"]] == [2]
    assert got["dropped"] == [{"n": 1, "old": "very unnecessary qualifier", "new": "qualifier", "cause": "rejected"}]


def test_filter_passes_memory_pys_validation_failure_on_and_prints_nothing(
    prose: Prose, repo: Path
) -> None:
    put_doc(repo)
    bad = proposal([{"n": 1, "old": "x", "new": "x", "reason": "r"}])  # new equals old
    proc = run_brief(prose, "filter", "docs/sample.md", stdin=fenced(bad))
    assert proc.returncode == 1 and proc.stdout == "" and "equals" in proc.stderr
    proc = run_brief(prose, "filter", "docs/sample.md", stdin="no json at all")
    assert proc.returncode == 1 and "no JSON" in proc.stderr


def test_a_stdin_run_filters_against_the_input_file_with_no_document_record(
    prose: Prose, tmp_path: Path
) -> None:
    src = tmp_path / "msg.txt"
    src.write_text("fix: make the thing work really quite well\n\nBody line stays.\n")
    edits = [edit(1, "really quite well", "well"), edit(2, "not in the input", "x")]
    got = json.loads(brief_ok(prose, "filter", "-", "--file", str(src), stdin=fenced(proposal(edits))))
    assert [e["n"] for e in got["edits"]] == [1]
    assert got["dropped"] == [{"n": 2, "old": "not in the input", "new": "x", "cause": "not-found"}]
    assert run_brief(prose, "filter", "-", stdin=fenced(proposal(edits))).returncode == 1


# ---------------------------------------------------------------------------
# End to end through the memory.py verbs the grammar maps onto
# ---------------------------------------------------------------------------


def test_rejections_and_exemplar_argv_drive_memory_py(prose: Prose, repo: Path) -> None:
    prose.ok("reject", "docs/x.md", "--old", "two", "--new", "2")
    argv = json.loads(brief_ok(prose, "parse", "rejections", "docs/x.md"))["memory_argv"]
    listed = prose.ok(*argv)
    assert [r["old"] for r in listed["rejections"]] == ["two"]
    argv = json.loads(brief_ok(prose, "parse", "rejections", "docs/x.md", "--remove", "1"))["memory_argv"]
    assert prose.ok(*argv)["rejections"] == []
    argv = json.loads(brief_ok(prose, "parse", "exemplar", "reference-doc", "docs/x.md:2-3"))["memory_argv"]
    got = prose.ok(*argv)
    assert got["exemplar"]["text"] == "two\nthree" and got["exemplar"]["path"] == "docs/x.md"


# ---------------------------------------------------------------------------
# The agent and the skill files
# ---------------------------------------------------------------------------


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, f"{path} has no frontmatter"
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_the_agent_is_read_only_and_names_its_model() -> None:
    fm = _frontmatter(AGENT)
    assert fm["name"] == "line-editor"
    assert fm["tools"] == "[Read, Grep, Glob]"
    assert fm["model"] in {"opus", "sonnet", "haiku"} and fm["description"]


def test_the_agents_example_output_is_a_valid_proposal_in_memory_pys_format() -> None:
    mem_spec = importlib.util.spec_from_file_location("mem_for_agent_test", MEMORY)
    assert mem_spec and mem_spec.loader
    mem = importlib.util.module_from_spec(mem_spec)
    sys.modules[mem_spec.name] = mem
    mem_spec.loader.exec_module(mem)
    blocks = re.findall(r"```json\n(.*?)\n```", AGENT.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) == 1, "the agent file shows the output format exactly once"
    prop = mem.validate_proposal(json.loads(blocks[0]))
    assert set(prop) == {"voice_card", "note", "paragraphs", "edits", "queries"}


def test_the_agent_body_carries_the_diagnostic_questions_and_the_protections() -> None:
    body = AGENT.read_text(encoding="utf-8").lower()
    for needle in ("open with its claim", "actor", "end on what matters", "reader new to the project",
                   "qualifier", "voice card", "refrain", "tricolon", "never rewrite a whole section",
                   "budget", "prefer cutting"):
        assert needle in body, needle


def test_the_skill_has_frontmatter_and_names_only_real_helper_verbs() -> None:
    fm = _frontmatter(SKILL)
    assert fm["name"] == "prose-edit" and fm["description"].lower().startswith("use when")
    text = SKILL.read_text(encoding="utf-8")
    assert "scripts/brief.py" in text and "scripts/memory.py" in text
    helper_verbs = set(re.findall(r"BRIEF (parse|build|filter|tmpdir|site-layer)\b", text))
    assert {"parse", "build", "filter", "tmpdir"} <= helper_verbs
    tree = ast.parse(BRIEF.read_text())
    commands = {c.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name) and t.id == "COMMANDS"
                for c in ast.walk(node.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert helper_verbs <= commands
    assert "line-editor" in text and "$ARGUMENTS" in text
    assert "memory.py" in text


def test_brief_py_is_stdlib_only_and_never_imports_nexus() -> None:
    tree = ast.parse(BRIEF.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "nexus" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
