# SPDX-License-Identifier: AGPL-3.0-or-later
"""SKILL.md and README.md drive the MarkupEditor review (nexus-scmcs.2.12 and .2.13, A1).

Pure text tests. They pin the order of the A1 sequence for a path run, that the MarkupEditor tools are named by what
they do (the installer picks the server name, so a full tool name would be wrong on most machines), that no step
leaves the model to choose numbers, and that no word of the Typora viewer is left.
"""
from __future__ import annotations

import re

from tests.prose_edit.test_brief import ROOT, SKILL

README = ROOT / "prose-edit" / "README.md"
REVIEW = ROOT / "prose-edit" / "skills" / "prose-edit" / "scripts" / "review.py"
TEXT = SKILL.read_text(encoding="utf-8")


def step(n: int) -> str:
    start = TEXT.index(f"\n{n}. ")
    nxt = TEXT.find(f"\n{n + 1}. ", start)
    return TEXT[start:nxt if nxt != -1 else TEXT.index("\n## Rules")]


def before(text: str, *needles: str) -> None:
    """Each needle occurs in `text`, in this order."""
    at = -1
    for needle in needles:
        found = text.find(needle, at + 1)
        assert found > at, (needle, "is missing or out of order")
        at = found


# ---------------------------------------------------------------------------
# What is gone, and how the tools are named
# ---------------------------------------------------------------------------


def test_no_word_of_the_viewer_or_the_marked_up_copy_is_left() -> None:
    for word in ("Typora", "marked-up", "viewer", "opened", "review.md", "PROSE_EDIT_OPEN"):
        assert word.lower() not in TEXT.lower(), word


def test_the_markupeditor_tools_are_named_by_role_and_never_by_a_full_tool_name() -> None:
    assert not re.search(r"mcp__\w*markupeditor", TEXT, re.IGNORECASE)
    assert "mcp__" not in TEXT
    for role in ("open_document", "app_state", "get_document", "propose_edits", "list_suggestions", "undo"):
        assert re.search(rf"MarkupEditor {role}\b|the {role} tool|{role} tool", TEXT), role


def test_the_skill_names_the_verbs_the_script_has_and_none_it_lacks() -> None:
    verbs = set(re.findall(r"`REVIEW ([a-z-]+)", TEXT))
    parser = REVIEW.read_text(encoding="utf-8")
    assert verbs >= {"render", "translate", "proposed", "decisions", "answer", "apply", "log-retry"}, verbs
    for verb in verbs:
        assert f'"{verb}"' in parser, verb
    assert "record" not in verbs  # A1: no record verb, no saved-file verification


def test_arguments_still_appears_once() -> None:
    assert TEXT.count("$ARGUMENTS") == 1


# ---------------------------------------------------------------------------
# Step 4 and 5: the nine-line header and the paths
# ---------------------------------------------------------------------------


def test_a_path_runs_header_is_nine_lines_and_the_paths_have_names() -> None:
    s4, s5 = step(4), step(5)
    assert "The header is nine lines" in s4
    for key in ("BLOCKS=", "PROPOSED=", "STATUSES="):
        assert f"`{key}<path>`" in s4 and f"`{key}`" in s5, key
    for name in ("<blocks>", "<proposed>", "<statuses>"):
        assert name in s5


# ---------------------------------------------------------------------------
# Step 6b: open the document before the editor is paid for
# ---------------------------------------------------------------------------


def test_the_document_is_opened_and_confirmed_before_the_editor_is_dispatched() -> None:
    s6 = step(6)
    assert "6b." in s6 and TEXT.index("6b.") < TEXT.index("\n7. Dispatch")
    for needle in ("open_document", "app_state", "`url`", "`modalWindow`", "is empty", "symbolic link"):
        assert needle in s6, needle
    before(s6, "open_document", "app_state", "`modalWindow`", "Go on to step 7")
    # a missing tool or a silent app stops the run before the editor, with how to start the app
    assert "propose_edits tool is missing" in s6 and "rmtmp" in s6
    assert "Assistants" in s6 and "allow this program" in s6 and "start the app" in s6
    assert "`hasChanges`" in s6 and "Don't Save" in s6
    assert "Path run only" in s6


# ---------------------------------------------------------------------------
# Step 10: read, translate, propose, show, end the turn
# ---------------------------------------------------------------------------


def test_step_10_runs_the_app_sequence_in_order_for_a_path_run() -> None:
    s10 = step(10)
    before(s10, "REVIEW render", "get_document", "`from` 0", "`BLOCKS=` path", "REVIEW translate", "propose_edits",
           "`PROPOSED=` path", "REVIEW proposed", "Tell the author to accept or reject each suggestion in the app")
    assert "`next`" in s10 and "`limit`" in s10  # paged
    assert "every page in order as one JSON list" in s10  # the shape _load_blocks takes
    assert "--blocks-file '<blocks>'" in s10 and "--from-file '<proposed>'" in s10
    assert "When `sent` is 0 skip" in s10  # no propose_edits call and no `proposed`
    assert "copied whole and unchanged" in s10


def test_step_10_shows_the_edits_the_app_cannot_show_and_the_queries_and_paragraphs() -> None:
    s10 = step(10)
    assert "`refused`" in s10 and "[Q<n>]" in s10 and "[P<n>]" in s10
    assert "cause" in s10 and "detail" in s10
    for part in ("`note`", "`voice_card`", "`paragraphs`", "`queries`", "`dropped`"):
        assert part in s10, part


def test_the_author_is_told_not_to_type_not_to_undo_and_not_to_save_and_the_turn_ends() -> None:
    tail = step(10).split("Tell the author to accept or reject each suggestion in the app", 1)[1]
    for needle in ("must not type in the document", "must not use Undo", "must not save", "not kept",
                   "Never call the MarkupEditor save or replace tools", "End your turn"):
        assert needle in tail, needle
    assert "Undo" in tail and "counts as accepted" in tail  # why: an undone Accept stays accepted (F5)


def test_step_10_keeps_the_exit_3_retry_and_the_stdin_run_goes_straight_to_the_chat_review() -> None:
    s10 = step(10)
    assert "exit 3" in s10 and "keep WORK" in s10
    assert "A stdin run goes on to step 11" in s10


# ---------------------------------------------------------------------------
# Step 11 and 12: statuses, decisions, the author's picks, apply, undo, reopen
# ---------------------------------------------------------------------------


def test_step_11_reads_the_statuses_through_decisions_and_never_chooses_numbers() -> None:
    s11 = step(11)
    path_run = s11[s11.index("Path run, before any question"):]
    before(path_run, "list_suggestions", "`STATUSES=` path", "REVIEW decisions", "`undecided`", "ask the questions",
           "REVIEW decisions", "`apply_args`")
    assert "--from-file '<statuses>'" in s11
    assert "an edit the app holds is decided only there" in s11
    assert "Never choose the numbers yourself" in s11
    assert "the author's explicit numbers" in s11
    assert "the `undecided` ones" in s11  # the accept questions cover the leftovers only
    assert "When step 10 sent nothing, run it without `--from-file`" in s11


def test_step_11_keeps_the_stdin_runs_numbered_review() -> None:
    s11 = step(11)
    for label in ("Which edits do you accept?", "Leave undecided (Recommended)", "Reject them all", "Choose which to reject",
                  "Answer a query or paragraph proposal with a change?"):
        assert label in s11, label
    assert "groups of 2 to 4" in s11 and "more than 16" in s11


def test_step_12_uses_the_apply_args_then_returns_the_app_to_the_file() -> None:
    s12 = step(12)
    assert "`apply_args` that `REVIEW decisions` printed" in s12
    before(s12, "--dry-run", "Apply these changes?", "without `--dry-run`",
           "MarkupEditor undo tool", "`hasChanges` is false", "open_document tool on the document again", "Don't Save")
    assert "accepted + rejected + 3" in s12 and "accepted and rejected in the app" in s12
    assert "app_state tool as in step 6b" in s12
    assert "after a successful apply, after \"Cancel\"" in s12


def test_the_answer_route_does_not_read_the_app_again() -> None:
    s12 = step(12)
    assert "a path run: it only refreshes the session; do not read the app again" in s12


def test_rules_forbid_the_save_and_replace_tools() -> None:
    rules = TEXT[TEXT.index("## Rules"):TEXT.index("## Asking the author")]
    assert "Never call the MarkupEditor save or replace tools" in rules
    assert "Never change the document except through `REVIEW apply`." in rules


# ---------------------------------------------------------------------------
# The setup run
# ---------------------------------------------------------------------------


def test_the_setup_run_checks_for_the_propose_edits_tool_and_names_the_requirement() -> None:
    setup = TEXT[TEXT.index("## Setup run"):]
    check = setup[setup.index("3b."):setup.index("\n4. Genres")]
    for needle in ("propose_edits", "Assistants", "Unlimited", "keeps working without renewal", "build from source",
                   "Evaluation build stops after two weeks", "always allow", "ToolSearch", "stdin run does not need the app"):
        assert needle in check, needle
    assert "path-run requirement" in check or "requirement of path runs" in check
    assert "`ready` is unchanged" in setup  # the script cannot see the app's tools


# ---------------------------------------------------------------------------
# The README
# ---------------------------------------------------------------------------


def test_the_readme_describes_the_markupeditor_review_in_plain_words() -> None:
    text = README.read_text(encoding="utf-8")
    assert "viewer" not in text.lower() and "marked-up" not in text.lower() and "typora" not in text.lower()
    for needle in ("MarkupEditor", "Assistants", "suggestions", "Unlimited", "Evaluation", "stevengharris/MarkupEditorApp",
                   "do not save", "Always allow", "pasted text"):
        assert needle in text, needle
    assert "—" not in text  # no em dash


# ---------------------------------------------------------------------------
# Gate 2 fix round (nexus-scmcs.2.14): the stop paths and the app's edge cases
# ---------------------------------------------------------------------------

ONE_SERVER = "from the one MCP server that offers propose_edits"


def s6b() -> str:
    s6 = step(6)
    return s6[s6.index("6b."):]


def closing() -> str:
    """The paragraph of step 12 that returns the app to the file."""
    s12 = step(12)
    return s12[s12.index("    A path run returns the app to the file"):]


def test_the_save_prompt_branch_says_dont_save_never_save() -> None:
    """F1: a leftover accepted-suggestion buffer is what raises the prompt, and Save rewrites the whole file."""
    modal = s6b()[s6b().index("When `modalWindow` is not empty"):s6b().index("If `hasChanges` is true")]
    assert "Don't Save" in modal and 'never "Save"' in modal, modal
    assert "earlier prose-edit review" in modal and "rewrites the whole file" in modal
    assert "the author's own" in modal  # the other possible owner of those changes decides first


def test_the_expired_sentence_says_dont_save_never_save() -> None:
    s12 = step(12)
    expired = s12[s12.index("On exit 1 with `expired`"):s12.index("On exit 3, or exit 1 with `run apply again`")]
    assert "Don't Save" in expired and 'never "Save"' in expired, expired
    assert "returns the app to the file" in expired  # suggestions may still sit in the app


def test_every_stop_after_suggestions_were_sent_runs_the_return_to_the_file_first() -> None:
    s12 = step(12)
    other = s12[s12.index("On any other failure"):s12.index("A successful apply has already deleted WORK")]
    assert "returns the app to the file" in other, other
    cancel = s12[s12.index('On "Cancel"'):s12.index("The script refuses a real apply")]
    assert "returns the app to the file" in cancel, cancel


def test_all_the_markupeditor_tools_come_from_the_one_server_that_offers_propose_edits() -> None:
    """C1: two MarkupEditor servers can be registered; opening through one and polling through the other is a false
    'the app is not listening'."""
    assert ONE_SERVER in s6b() and "name that server to the author" in s6b().lower()
    assert ONE_SERVER in closing()
    rules = TEXT[TEXT.index("## Rules"):TEXT.index("## Asking the author")]
    assert ONE_SERVER in rules
    setup = TEXT[TEXT.index("3b."):TEXT.index("\n4. Genres")]
    assert ONE_SERVER in setup
    # the server's own README sentence
    assert "one server" in README.read_text(encoding="utf-8").lower()


def test_the_read_write_requirement_is_told_to_the_author_everywhere_the_app_is() -> None:
    """S1: propose_edits needs read-write access; a read-only app passes the 6b probes."""
    assert "read-write" in s6b()
    assert "read-write" in TEXT[TEXT.index("3b."):TEXT.index("\n4. Genres")]
    assert "read-write" in README.read_text(encoding="utf-8")
    assert "read-write" in step(10)  # the propose_edits failure at item 3 names what to change


def test_6b_polls_up_to_30_times_and_reports_the_last_state() -> None:
    assert "up to 30 times" in s6b() and "ten times" not in TEXT
    assert "last state" in s6b() and "retry" in s6b().lower()


def test_6b_says_the_authors_own_unsaved_changes_are_theirs_to_keep_or_discard() -> None:
    """S2a: after a fresh open the only hasChanges is the author's own work on this file."""
    branch = s6b()[s6b().index("If `hasChanges` is true"):s6b().index("Go on to step 7")]
    for needle in ("the author's own", "Don't Save", "lost", "save them first", "rewrites the file's layout"):
        assert needle in branch, needle


def test_6b_tells_the_author_not_to_type_in_the_app_until_the_review_ends() -> None:
    """S2b: the editor runs for minutes with the file open and clean; typing then is discarded later."""
    assert "do not type in the app until the review ends" in s6b()


def test_6b_refuses_html_before_it_opens_anything() -> None:
    """S6: html review in the app is untested; refuse it with a message before the app is touched."""
    text = s6b()
    before(text, ".html", ".htm", "not supported", "rmtmp", "Call the open_document tool")


def test_the_closing_paragraph_reads_app_state_first_and_skips_undo_for_another_file() -> None:
    """S2c and S3: undo also undoes the author's own changes, and in another document."""
    cp = closing()
    before(cp, "app_state", "`url`", "not the document", "do not call the undo tool", "undo tool")
    assert "accepted + rejected + 3" in cp
    assert "at most as many times as the author accepted in the app plus 3" not in cp
    assert "when `hasChanges` is already false" in cp or "`hasChanges` is already false" in cp


def test_the_closing_paragraph_says_dont_save_at_the_reopen_where_the_prompt_appears() -> None:
    """S3: the prompt appears when open_document reloads the file, so that is where Don't Save is said."""
    cp = closing()
    assert cp.index("call the open_document tool on the document again") < cp.index("Don't Save") \
        < cp.index("app_state tool as in step 6b")
    assert 'never "Save"' in cp
    assert ONE_SERVER in cp


def test_the_closing_paragraph_reopens_after_any_real_apply_but_undoes_only_what_was_sent() -> None:
    """F3: apply writes the file while the app shows the old text; that holds with sent 0 and for chat-only accepts."""
    cp = closing()
    assert "any real apply" in cp and "whether or not suggestions were sent" in cp
    assert "undo only when suggestions were sent" in cp or "Undo only when suggestions were sent" in cp
    assert "`sent` 0" in cp


def test_a_stale_get_document_read_tells_the_model_to_open_the_file_again() -> None:
    """Review S3: a url mismatch must end in open_document, not in a stopped turn."""
    item1 = step(10)[step(10).index("1. Call the MarkupEditor get_document"):step(10).index("2. Run `REVIEW translate")]
    assert "open_document" in item1 and "step 6b" in item1


def test_get_document_pages_are_about_40_blocks_and_a_saved_reply_is_copied_as_a_file() -> None:
    """S5: a 200-block page does not stay inline; the saved file is copied, never retyped."""
    item1 = step(10)[step(10).index("1. Call the MarkupEditor get_document"):step(10).index("2. Run `REVIEW translate")]
    assert "`limit` 40" in item1 and "`limit` 200" not in TEXT
    assert "saves a reply to a file" in item1 and "`cp '<saved file>' '<blocks>'`" in item1
    assert "a file copy" in item1
