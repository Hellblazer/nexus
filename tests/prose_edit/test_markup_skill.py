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
    for role in ("open_document", "get_document", "propose_edits", "list_suggestions", "undo"):
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
    for needle in ("open_document", "get_document", "`url`", "symbolic link"):
        assert needle in s6, needle
    before(s6, "open_document", "get_document", "`url`", "Go on to step 7")
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
    assert "`next`" in s10  # paged
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
    before(path_run, "list_suggestions", "`STATUSES=` path", "REVIEW decisions", "`undecided`", "Ask questions 1",
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


def clean_buffer() -> str:
    """The paragraph of step 12 that undoes the app's unsaved changes before the real apply writes the file."""
    s12 = step(12)
    return s12[s12.index("    Before the real apply"):s12.index("    On exit 1 with `--accept`")]


def test_step_12_uses_the_apply_args_cleans_the_app_buffer_then_applies_then_reopens() -> None:
    """Defect 3: a file written under the app's unsaved changes raises its changed-on-disk dialog, so undo comes first."""
    s12 = step(12)
    assert "`apply_args` that `REVIEW decisions` printed" in s12
    first = s12[:s12.index("    Before the real apply")]
    before(first, "--dry-run", "Apply these changes?", 'When the author picks "Apply"', "Before the real apply",
           "without `--dry-run`")
    before(s12, "Before the real apply", "MarkupEditor undo tool", "`hasChanges` is false", "Only then run the same command",
           "Don't Save", "open_document tool on the document again")
    cb = clean_buffer()
    before(cb, "decisions are already read", "get_document tool with `limit` 1", "MarkupEditor undo tool", "`hasChanges` is false",
           "Only then run the same command without `--dry-run`")
    assert "accepted + rejected + 3" in cb and "accepted and rejected in the app" in cb
    assert "get_document tool with `limit` 1 as in step 6b" in s12
    assert "after a successful apply, after \"Cancel\"" in s12


def test_the_clean_buffer_paragraph_says_why_and_keeps_the_dry_run_gate() -> None:
    cb = clean_buffer()
    for needle in ("changed on disk", "Reload from Disk", "Keep My Changes", "unsaved changes", "still answers",
                   "cannot see the dialog", "dry run still holds", "does not touch the file", "that sent suggestions", ONE_SERVER):
        assert needle in cb, needle


def test_a_buffer_that_does_not_clear_still_goes_on_to_the_apply_and_names_the_dialog() -> None:
    cb = clean_buffer()
    assert "did not clear" in cb and "go on all the same" in cb and "dialog" in cb
    # live run 3 (2026-10-31): undo restored the text but hasChanges stayed true; the author hears first
    warn = cb[cb.index("did not clear"):]
    assert "tell the author first" in warn and '"Reload from Disk"' in warn and 'never "Keep My Changes"' in warn
    assert warn.index("tell the author first") < warn.index("Only then run")


def test_the_author_hears_both_answers_before_the_reopen_and_a_dead_call_repeats_them() -> None:
    """The skill cannot see the app's dialogs, so a dirty buffer after apply is announced before open_document."""
    cp = closing()
    tell = cp[cp.index("Otherwise, when `hasChanges` is true"):cp.index("Then call the open_document tool on the document again")]
    for needle in ('"Reload from Disk"', 'never "Keep My Changes"', "holds every applied edit", '"Don\'t Save"', 'never "Save"'):
        assert needle in tell, needle
    assert "cannot see" in tell
    fallback = cp[cp.index("If a call fails or does not answer"):]
    assert "waiting on one of those dialogs" in fallback and "answer it as above" in fallback
    assert cp.index("open_document tool on the document again") < cp.index("If a call fails or does not answer")


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
    for needle in ("MarkupEditor", "Assistants", "suggestions", "Unlimited", "Evaluation", "stevengharris/MarkupEditorApp", "0.9.11",
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


def test_the_save_prompt_is_announced_before_the_open_when_the_buffer_is_dirty() -> None:
    """F1: a leftover accepted-suggestion buffer is what raises the prompt, and Save rewrites the whole file.

    Steve's released app has no app_state, and get_document does not answer while the prompt is up, so the skill reads
    hasChanges with get_document before open_document and tells the author then.
    """
    text = s6b()
    pre = text[text.index("Before opening, call the get_document tool"):text.index("Call the open_document tool")]
    before(pre, "`limit` 1", "`url` and `hasChanges`", "`hasChanges` true", "do not open the file", "Don't Save",
           'never "Save"', "the author's own work", "the author's to decide first", "run again; stop.")
    assert "earlier prose-edit review" in pre and "rewrites the whole file" in pre
    assert "rewrites the file's layout" in pre  # saving the author's own changes is a choice with a cost, named
    before(text, "Before opening, call the get_document tool", "Call the open_document tool")


def test_a_call_that_dies_after_the_open_names_both_causes_and_the_prompt_answer_before_the_retry_offer() -> None:
    """The prompt hangs get_document (measured 2026-11-06), so a failed poll means: not listening, or a dialog."""
    text = s6b()
    before(text, "Call the open_document tool", "up to 30 times", "If a call fails in any other way, or does not answer",
           "not listening", "waiting on a dialog", "Don't Save", "start the app", "and stop",
           "If the polls end with `url` still another file", "offer to retry")


def test_app_state_is_not_called_and_no_modal_window_is_read() -> None:
    """The released app does not serve app_state: one sentence says so, and nothing else names it."""
    assert TEXT.count("app_state") == 1 and "Do not call the app_state tool" in TEXT
    assert "modalWindow" not in TEXT
    assert "app_state" not in REVIEW.read_text(encoding="utf-8") and "modalWindow" not in REVIEW.read_text(encoding="utf-8")


def test_the_closing_url_check_treats_private_and_symlinks_as_the_same_file() -> None:
    """G2c gap 2: without 6b's rule a /var or symlinked path skips undo and tells the author a false thing."""
    check = clean_buffer()
    check = check[check.index("First call the get_document tool"):check.index("do not call the undo tool")]
    assert "`/private` prefix or a symbolic link is the same file" in check, check


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
    assert "the last `url` get_document reported" in s6b()  # a slow load that outlasts the polls is reported with the last url
    assert "retry" in s6b().lower()


def test_6b_says_the_authors_own_unsaved_changes_are_lost_by_dont_save() -> None:
    """S2a: the only dirty buffer may be the author's own work on this file, and the author hears so before the open."""
    pre = s6b()[s6b().index("Before opening, call the get_document tool"):s6b().index("Call the open_document tool")]
    for needle in ("the author's own", "Don't Save", "loses them"):
        assert needle in pre, needle


def test_a_resource_unavailable_error_means_a_dialog_and_the_document_tools_may_be_gone() -> None:
    """Measured 2026-11-06: the hung call ends with "read: Resource temporarily unavailable" once the dialog is answered,
    and the app's document tools then drop out of the session; only open_document stays listed."""
    text = s6b()
    err = text[text.index('A call that ends with the error "read: Resource temporarily unavailable"'):text.index("If a call fails in any other way")]
    before(err, "waiting on a dialog", "closed its connection", "only open_document left",
           "Ask the author which answer the dialog got", "After \"Don't Save\"", "ToolSearch",
           "call the open_document tool on the document again", "poll as above")
    cp = closing()
    fallback = cp[cp.index("If a call fails or does not answer"):]
    before(fallback, "Resource temporarily unavailable", "waiting on one of those dialogs", "answer it as above",
           "drop out of the session", "call the open_document tool on the document again", "before the next get_document call")


def test_the_resource_unavailable_branch_asks_which_answer_and_stops_after_cancel_or_save() -> None:
    """scmcs.4 review r1 blocker 2: the error comes back only once the dialog is answered, so the skill asks which
    answer it got: Don't Save re-opens, Cancel kept the author's changes, Save rewrote the file, so run again."""
    text = s6b()
    err = text[text.index('A call that ends with the error "read: Resource temporarily unavailable"'):text.index("If a call fails in any other way")]
    before(err, "has now been answered", "only once the dialog is gone", "Ask the author which answer the dialog got",
           "end your turn", "After \"Don't Save\"", "open_document tool on the document again",
           "After \"Cancel\"", "kept the app's changes", "delete WORK and stop",
           "After \"Save\"", "rewrote the whole file", "run again")


def test_both_failure_branches_after_the_open_keep_the_authors_own_work_caveat() -> None:
    """scmcs.4 review r1 blocker 1: an unsaved-changes prompt can come up after open_document whatever the pre-open
    read showed, and Don't Save loses the author's own work, so both failure branches say so."""
    text = s6b()
    err = text[text.index('A call that ends with the error "read: Resource temporarily unavailable"'):text.index("If a call fails in any other way")]
    other = text[text.index("If a call fails in any other way"):text.index("If the polls end with `url` still another file")]
    for branch in (err, other):
        before(branch, "only for changes an earlier prose-edit review left", "never \"Save\"",
               "loses changes that are the author's own work", "the author's to decide first")


def test_the_wait_for_the_dialog_answer_is_an_exception_to_every_stop_deleting_work() -> None:
    """scmcs.4 review r1 blocker 3: step 6b ends the turn to ask which answer a dialog got, with WORK in place, which
    the every-stop-deletes-WORK rule of step 5 must name."""
    step5 = TEXT[TEXT.index("5. WORK is the directory"):TEXT.index("6. If the brief contains")]
    before(step5, "On every stop after WORK exists", "are the exceptions", "step 6b's wait for the author to say which answer a dialog got")


def test_6b_tells_the_author_not_to_type_in_the_app_until_the_review_ends() -> None:
    """S2b: the editor runs for minutes with the file open and clean; typing then is discarded later."""
    assert "do not type in the app until the review ends" in s6b()


def test_6b_refuses_html_before_it_opens_anything() -> None:
    """S6: html review in the app is untested; refuse it with a message before the app is touched."""
    text = s6b()
    before(text, ".html", ".htm", "not supported", "rmtmp", "Call the open_document tool")


def test_the_closing_paragraph_reads_the_url_first_and_skips_undo_for_another_file() -> None:
    """S2c and S3: undo also undoes the author's own changes, and in another document."""
    cb = clean_buffer()
    before(cb, "get_document tool with `limit` 1", "`url`", "not the document", "do not call the undo tool", "undo tool")
    assert "accepted + rejected + 3" in cb
    assert "at most as many times as the author accepted in the app plus 3" not in cb
    assert "when `hasChanges` is already false" in cb or "`hasChanges` is already false" in cb
    cp = closing()
    before(cp, "get_document tool with `limit` 1", "`url`", "not the document", "do not call the open_document tool", "open_document tool on")
    assert "undo tool" not in cp.split("After any real apply")[1]  # the reopen only


def test_the_closing_paragraph_says_the_answers_before_the_reopen_where_the_dialog_appears() -> None:
    """S3: the dialog appears when open_document reloads the file, so the answers come first, then the call, then the polls."""
    cp = closing()
    before(cp, "tell the author before the reopen", "Don't Save", "Then call the open_document tool on the document again",
           "get_document tool with `limit` 1 as in step 6b")
    assert 'never "Save"' in cp
    assert ONE_SERVER in cp


def test_the_closing_paragraph_reopens_after_any_real_apply_but_undoes_only_what_was_sent() -> None:
    """F3: apply writes the file while the app shows the old text; that holds with sent 0 and for chat-only accepts."""
    cp = closing()
    assert "any real apply" in cp and "whether or not suggestions were sent" in cp
    assert "only when suggestions were sent" in cp
    assert "buffer is already clean" in cp  # a successful apply has nothing left to undo
    assert "`sent` 0" in cp


def test_a_stale_get_document_read_tells_the_model_to_open_the_file_again() -> None:
    """Review S3: a url mismatch must end in open_document, not in a stopped turn."""
    item = item1()
    assert "open_document" in item and "step 6b" in item


def item1() -> str:
    return step(10)[step(10).index("1. Call the MarkupEditor get_document"):step(10).index("2. Run `REVIEW translate")]


def test_get_document_is_called_with_no_limit_and_paged_by_next() -> None:
    """Defect 1: `limit` is the most characters of block Markdown a reply holds (default 100000), not a block count."""
    item = item1()
    assert "`limit` 40" not in TEXT and "`limit` 200" not in TEXT
    before(item, "`from` 0", "no `limit`", "characters", "`from` set to the reply's `next`", "until a reply has no `next`")
    assert "not blocks" in item or "not a count of blocks" in item


def test_every_page_goes_to_its_own_file_and_translate_takes_one_blocks_file_each() -> None:
    """Defect 2: no page is retyped or merged by the model; page 1 is the BLOCKS= path, the others sit beside it."""
    item = item1()
    assert "one JSON list" not in item and "read each saved page" not in item
    before(item, "`BLOCKS=` path", "`<work>/blocks-2.json`", "`<work>/blocks-3.json`")
    assert "`cp '<saved file>' '<blocks>'`" in item and "`cp '<saved file>' '<work>/blocks-2.json'`" in item
    assert "saves a reply to a file" in item and "a file copy" in item
    assert "Write" in item and "short" in item  # a reply that stays inline is Written, page by page
    s10 = step(10)
    translate = s10[s10.index("2. Run `REVIEW translate"):s10.index("3. When `sent` is 0")]
    before(translate, "--blocks-file '<blocks>'", "one `--blocks-file` for each further page", "in order",
           "--blocks-file '<work>/blocks-2.json'")


def test_the_page_files_are_named_as_an_exception_to_the_no_invented_paths_rule() -> None:
    s5 = step(5)
    assert "blocks-2.json" in s5 and "page" in s5 and "`BLOCKS=`" in s5


def test_the_script_takes_blocks_file_once_for_each_page() -> None:
    parser = REVIEW.read_text(encoding="utf-8")
    assert re.search(r'add_argument\("--blocks-file",[^)]*action="append"', parser, re.DOTALL)


def test_a_page_with_a_changed_buffer_or_another_url_ends_the_turn_for_any_page() -> None:
    item = item1()
    assert "If a reply says `hasChanges` is true" in item and "If its `url` is not the document" in item
