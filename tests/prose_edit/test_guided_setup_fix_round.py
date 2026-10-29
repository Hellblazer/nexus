# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-scmcs fix round (two reviewers on 0c92d345a and d660a306b): a beta tester starting cold in a repository that
is not laid out like nexus. These pin the skill text the choice prompts depend on (the tool's limits, the grouping of
the accept question, the genre check) and the script behaviour behind setup (the skip list, root files, try_path)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.prose_edit.conftest import Prose, git
from tests.prose_edit.test_brief import _module
from tests.prose_edit.test_guided_setup import (
    ALL_GENRES,
    README,
    SKILL_TEXT,
    _candidates,
    _put,
    _setup,
    _setup_run,
    _step,
)


def _drop_seed_doc(bare: Prose) -> None:
    """The fixture repository tracks docs/x.md, a built-in reference-doc; setup ignores a tracked file that is gone."""
    (bare.cwd / "docs" / "x.md").unlink()


def _stage(bare: Prose) -> None:
    git(bare.cwd, "add", "-A")


# ---------------------------------------------------------------------------
# the tool's limits, read off the skill text
# ---------------------------------------------------------------------------

_HEADER_LIST = re.compile(r"[Hh]eaders?\s+((?:`[^`]+`(?:,\s*|\s+and\s+)?)+)")
_OPTION_LIST = re.compile(
    r"(?:\bwith|\boptions(?:\s+are)?)\s+((?:\"[^\"]+\"(?:\s*\([^()]*\))?\s*(?:,\s*|\band\b\s*|\bor\b\s*)?)+)"
)


def test_every_header_chip_the_skill_names_is_at_most_twelve_characters() -> None:
    chips = [c for m in _HEADER_LIST.finditer(SKILL_TEXT) for c in re.findall(r"`([^`]+)`", m.group(1))]
    assert len(chips) >= 12, chips  # non-vacuity: the skill names a dozen or more
    assert {"Genre", "Remember", "Answer", "Others", "Apply", "Genres", "Rules in?", "Voice card"} <= set(chips)
    assert [c for c in chips if len(c) > 12] == []


def test_the_rule_for_a_candidate_header_covers_a_folder_and_a_file_name_cut_to_twelve() -> None:
    run = _setup_run()
    assert "last folder name of the key or the file name, cut to 12 characters" in run


def test_every_option_list_the_skill_spells_out_has_two_to_four_options() -> None:
    lists = [re.findall(r"\"([^\"]+)\"", m.group(1)) for m in _OPTION_LIST.finditer(SKILL_TEXT)]
    assert len(lists) >= 11, lists  # non-vacuity
    assert [opts for opts in lists if not 2 <= len(opts) <= 4] == []
    treatment = re.search(r"header `Treatment`: ((?:\"[^\"]+\"(?:, | or )?)+)\)", SKILL_TEXT)
    assert treatment and len(re.findall(r"\"[^\"]+\"", treatment.group(1))) == 3
    flat = {o for opts in lists for o in opts}
    assert {"Accept it", "Leave it undecided", "Reject it", "Use my suggestions (Recommended)",
            "Rules are in (Recommended)"} <= flat


def test_the_setup_run_option_tables_have_two_to_four_rows() -> None:
    rows = [ln for ln in _step_3_table() if ln.strip().startswith("| ") and not ln.strip().startswith(("| Option", "| ---"))]
    assert 2 <= len(rows) <= 4


def _step_3_table() -> list[str]:
    run = _setup_run()
    start = run.index("| Option | Description | Then |")
    return run[start : run.index("\n\n", start)].splitlines()


# ---------------------------------------------------------------------------
# 2, 3, 4: the accept, reject and query questions
# ---------------------------------------------------------------------------


def test_the_accept_question_splits_into_balanced_groups_of_two_to_four() -> None:
    step11 = _step(11)
    assert "groups of 2 to 4" in step11
    for split in ("5 edits are 3 + 2", "9 are 3 + 3 + 3", "13 are 4 + 3 + 3 + 3"):
        assert split in step11, split
    assert "every four placed edits" not in step11
    assert "`Edits 1-4`, `Edits 5-8`" not in step11


def test_a_single_placed_edit_is_a_single_select_accept_it_or_leave_it_undecided() -> None:
    step11 = _step(11)
    assert "A single placed edit is one single-select question" in step11
    assert 'the options "Accept it" and "Leave it undecided"' in step11


def test_the_accept_header_names_the_groups_own_edit_numbers_and_an_empty_tick_means_none() -> None:
    step11 = _step(11)
    assert "names the group's own edit numbers" in step11 and "gaps" in step11
    assert "a submission with nothing ticked means none accepted from that group" in step11


def test_the_accept_option_leads_with_the_reason_and_the_changed_words_not_a_cut_sentence() -> None:
    step11 = _step(11)
    assert "`<reason>: <old words> -> <new words>`" in step11
    assert "cut to about 120 characters" not in step11
    assert "shorten the reason before you shorten the change" in step11


def test_reject_them_all_covers_only_the_offered_edits_and_claims_no_storage_on_a_stdin_run() -> None:
    step11 = _step(11)
    assert "covers only the editor's own edits the accept questions offered" in step11
    assert "never an overflow edit" in step11
    assert "on a stdin run none is stored" in step11
    assert '"Reject them all" (each is stored as a rejection' not in step11
    assert "every edit the accept questions offered and the author did not tick" in _step(12)


def test_the_query_question_shows_what_it_asks_about() -> None:
    step11 = _step(11)
    assert "Put what it asks about in the question text" in step11
    assert "the text of the query" in step11 and "the count and the first one" in step11


# ---------------------------------------------------------------------------
# 5: a genre is one of the six names
# ---------------------------------------------------------------------------


def test_the_skill_never_stores_or_passes_an_other_answer_that_is_not_a_genre_name_at_both_questions() -> None:
    names = "`rdr`, `reference-doc`, `how-to`, `exploration-essay`, `changelog` or `commit-message`"
    row = next(ln for ln in _step(4).splitlines() if "stderr contains `no genre`" in ln)
    assert f"not exactly one of {names}" in row
    assert "never passed as `--genre` or stored" in row and "ask the genre question again" in row
    assert f"not exactly one of {names}" in _setup_run()
    assert "is never stored; say so and ask that question again" in _setup_run()


def _genre_map_entry(bare: Prose, *items: str):
    return bare.run("add-entry", "--level", "repo", "--from-stdin", stdin={"scalars": {}, "lists": {"genre_map": list(items)}})


# add-entry refuses before it reaches T2: the `bare` fixture's nx cannot start, so a script that reached for T2 first
# would end with exit 3 ("T2 unavailable") and not the refusal. That ordering is what keeps the valid entry beside
# the bad one from being stored; the T2-backed writes of valid entries are test_memory.py's and
# test_plugin_packaging.py's.
@pytest.mark.parametrize("entry", ["docs/guides/=howto", "docs/guides/=How-To", "docs/guides/=", "docs/guides/", "=how-to", "docs/guides/=a genre"])
def test_add_entry_refuses_a_genre_map_value_that_is_not_one_of_the_six(bare: Prose, entry: str) -> None:
    proc = _genre_map_entry(bare, "notes/=how-to", entry)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "genre_map entry" in proc.stderr and "rdr" in proc.stderr and "commit-message" in proc.stderr
    assert "T2 unavailable" not in proc.stderr  # refused before any read of T2, so nothing was stored


def test_add_entry_refuses_a_bad_genre_map_value_given_by_key_and_value_too(bare: Prose) -> None:
    proc = bare.run("add-entry", "--level", "repo", "--key", "genre_map", "--value", "docs/=howto", "--list")
    assert proc.returncode == 1 and "genre_map entry" in proc.stderr


def test_the_genre_map_check_names_the_bad_entry_and_passes_each_of_the_six_genres() -> None:
    memory = _module()._MEM  # one module load: its UserError is the one its check raises
    check = memory._check_genre_map_items
    check([f"area{i}/={g}" for i, g in enumerate(ALL_GENRES)])
    with pytest.raises(memory.UserError, match="docs/guides/=howto"):
        check(["notes/=how-to", "docs/guides/=howto"])


# ---------------------------------------------------------------------------
# 6: the skip list, try_path and the Try line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("segment", ["src", "data", "examples", "resources", "commands", "out", "service"])
def test_a_generic_folder_name_that_holds_real_docs_elsewhere_is_offered(bare: Prose, segment: str) -> None:
    _put(bare.cwd, f"{segment}/content/guide.md")
    _put(bare.cwd, f"{segment}/content/intro.md")
    _stage(bare)
    report = _setup(bare)
    assert f"{segment}/content/" in _candidates(report)
    assert _module().is_skipped(f"{segment}/content/guide.md") is False


def test_a_repository_with_docs_only_under_src_content_gets_candidates_and_a_try_path(bare: Prose) -> None:
    _drop_seed_doc(bare)
    _put(bare.cwd, "src/content/guide.md")
    _put(bare.cwd, "src/content/intro.md")
    _stage(bare)
    report = _setup(bare)
    found = _candidates(report)
    assert set(found) == {"src/content/"} and found["src/content/"]["files"] == 2
    assert report["try_path"] in {"src/content/guide.md", "src/content/intro.md"}


@pytest.mark.parametrize(
    "segment", ["dist", "build", "node_modules", "vendor", "site-packages", "__pycache__", "coverage", "target"]
)
def test_build_and_dependency_folders_are_never_offered_and_never_tried(bare: Prose, segment: str) -> None:
    _drop_seed_doc(bare)
    _put(bare.cwd, f"{segment}/pkg/readme.md")
    _stage(bare)
    report = _setup(bare)
    assert report["genre_candidates"] == [] and report["try_path"] is None
    assert report["unmapped_other"] == 2  # the file and the .claude site-page sheet


@pytest.mark.parametrize("rel", ["CONTRIBUTING.md", "tests/fixtures/f.md", "scripts/run.md"])
def test_try_path_is_never_null_while_a_visible_document_exists(bare: Prose, rel: str) -> None:
    _drop_seed_doc(bare)
    _put(bare.cwd, rel)
    _stage(bare)
    report = _setup(bare)
    assert report["genre_candidates"] == []
    assert report["try_path"] == rel


def test_the_try_line_is_the_absolute_path_in_single_quotes_and_unmapped_other_is_generic() -> None:
    run = _setup_run()
    assert "`Try: /conexus-prose-edit:prose-edit '<root>/<try_path>'`" in run
    assert "with `<root>` the `root` of the report" in run
    assert "tests, fixtures, scripts" not in run
    assert "that many other Markdown and HTML files were not offered, they still work, and a run on one asks for a genre" in run


def test_setup_reports_the_root_the_try_line_is_built_from(bare: Prose) -> None:
    report = _setup(bare)
    assert Path(report["root"]).resolve() == bare.cwd.resolve()
    assert (Path(report["root"]) / report["try_path"]).is_file()


# ---------------------------------------------------------------------------
# 7: one question first; root boilerplate is not a candidate; notes is not an essay
# ---------------------------------------------------------------------------


def test_root_files_other_than_the_readme_are_counted_not_offered(bare: Prose) -> None:
    for rel in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "docs/runbooks/a.md"):
        _put(bare.cwd, rel)
    _stage(bare)
    report = _setup(bare)
    assert set(_candidates(report)) == {"README.md", "docs/runbooks/"}
    assert report["unmapped_other"] == 3 + 1  # the three root files and the .claude site-page sheet


def test_a_notes_folder_is_a_reference_doc_suggestion_not_an_essay() -> None:
    mod = _module()
    assert mod.suggest_genre_for_key("notes/") == "reference-doc"
    assert mod.suggest_genre_for_key("docs/release-notes/") == "reference-doc"
    assert "notes" not in dict(mod.GENRE_KEYWORDS)["exploration-essay"]


def test_setup_opens_the_genre_step_with_one_question_before_any_per_folder_one() -> None:
    run = _setup_run()
    step4 = run[run.index("\n4. Genres.") : run.index("\n5. Run `BRIEF setup` again")]
    assert "ask ONE question first, before any per-folder question, header `Genres`" in step4
    for label in ('"Use my suggestions (Recommended)"', '"Go one by one"', '"Ask me when I edit"'):
        assert label in step4, label
    assert step4.index("Use my suggestions") < step4.index("What kind of writing is")
    assert "lists every candidate as `<key> (<files> files) -> <suggested>`" in step4
    assert 'map every candidate to its `suggested` genre in a single `add-entry`' in step4
    assert '"Ask me when I edit": store nothing' in step4


# ---------------------------------------------------------------------------
# 9, 10, 11: cross-references, the JSON choice, the voice card sentence
# ---------------------------------------------------------------------------


def test_no_reference_to_step_three_leaves_out_which_list() -> None:
    assert "as step 3 does" not in SKILL_TEXT
    assert "step 3 of the Steps list does" in _setup_run()
    assert "redirect of step 9 of the Steps list" in _setup_run()


def test_show_me_the_json_ends_with_a_choice_question_not_a_plain_text_wait() -> None:
    row = next(ln for ln in _setup_run().splitlines() if ln.lstrip().startswith("| Show me the JSON"))
    assert 'header `Rules in?`' in row and '"Rules are in (Recommended)" and "Skip for now"' in row
    assert "Ask the author to say when the rules are in" not in row


def test_the_voice_card_offer_says_in_one_sentence_what_a_voice_card_is() -> None:
    assert "a plain sentence saying what a voice card is" in SKILL_TEXT
    assert "how this document sounds" in SKILL_TEXT


# ---------------------------------------------------------------------------
# 1: the README can be followed cold
# ---------------------------------------------------------------------------


def test_the_readme_quick_start_gives_every_command_in_order() -> None:
    text = README.read_text(encoding="utf-8")
    block = text[text.index("## Quick start") : text.index("\n## Genres")]
    commands = [
        "/plugin marketplace add Hellblazer/nexus",
        "/plugin install conexus@nexus-plugins",
        "/conexus:setup",
        "/plugin install conexus-prose-edit@nexus-plugins",
        "/reload-plugins",
        "/conexus-prose-edit:prose-edit setup",
    ]
    at = [block.index(c) for c in commands]
    assert at == sorted(at), at
    assert "you can skip" not in text and "by number" not in text
    assert "ends with \"Ready.\"" in block


# confirmation round on 8153fd54e


def test_the_genres_question_says_what_a_genre_is_and_one_candidate_skips_it() -> None:
    step = _setup_run()
    assert '"A genre is the kind of writing a document is; it sets what the editor looks for' in step
    assert "When it holds one candidate, skip the `Genres` question" in step


def test_reject_them_all_never_covers_an_edit_the_author_added() -> None:
    # review.py refuses --reject of an author's own edit, so the option must leave those held
    assert "never an edit the author added through the answer route" in SKILL_TEXT
    assert "When any edit offered in the accept questions went unticked" in SKILL_TEXT


def test_a_stdin_run_without_a_genre_asks_instead_of_stopping() -> None:
    assert "except a stdin run whose stderr says `needs --genre`" in SKILL_TEXT
