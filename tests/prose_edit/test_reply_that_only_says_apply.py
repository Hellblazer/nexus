# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reply that only says to apply, and the voice card the offer must show (nexus-scmcs).

Two live stand-in runs failed on one gap. After a path run held a query, the skill asked question 1 and the voice-card
question in one call, and the author's next message said only "Yes, apply.". Review `b4c` stopped to ask which question
that answered (no dry run, no apply: held=None); teach `t2c` applied but pointed at "my earlier message" for the card,
so the offer never showed it. The skill now says how to read such a reply, and that the voice-card offer carries the
card text itself. Pure text tests over what a model follows literally, so each pins a condition.
"""
from __future__ import annotations

from tests.prose_edit.test_brief import route_sections
from tests.prose_edit.test_markup_skill import TEXT, before, step
from tests.prose_edit.test_one_call_decisions import _branch


def _reply_paragraph() -> str:
    section = route_sections()["Step 11 path run"]
    start = section.index("  A reply that only says to apply")
    return section[start:section.index("- Branch B,", start)]


def test_a_bare_apply_is_a_no_on_the_query_and_leaves_the_card_open() -> None:
    para = _reply_paragraph()
    before(para, "after you asked question 1, and the voice-card question with it", '(for example "Yes, apply.")',
           "do not ask again which question it answers", "It counts as No on question 1",
           "It does not answer the voice-card question", "step 13 asks it once more after the apply")
    assert "never as Keep it on the voice-card question" in para and "Never read such a reply as Yes on question 1" in para


def test_a_bare_apply_runs_the_dry_run_then_the_real_apply_in_the_same_turn() -> None:
    para = _reply_paragraph()
    before(para, "go to step 12 with the `apply_args` already printed", "run the dry run", "show it",
           "in the same turn", "Step 12 before the real apply", "without `--dry-run`", "no `Apply` question")


def test_a_bare_apply_still_asks_apply_when_the_dry_run_leaves_something_out() -> None:
    para = _reply_paragraph()
    before(para, "The one exception", "`unplaced` or `would_skip`", "ask `Apply` as step 12 of SKILL.md says")


def test_step_12_names_the_bare_apply_as_a_case_that_skips_the_apply_question() -> None:
    s12 = step(12)
    para = s12[s12.index("    Run `REVIEW apply --work"):s12.index("    App-decided run (a path run only):")]
    assert "nor after a reply that only says to apply" in para
    assert "ask the author to confirm unless the run is App-decided or follows a reply that only says to apply." in para
    assert "in the no-edits close of step 11, do not ask: run it at once; nor in an App-decided run, below" in para


def test_a_bare_apply_does_not_count_as_the_voice_card_answer_at_step_13() -> None:
    card = step(13)[step(13).index("    - The voice card"):]
    before(card, "already answered in step 11", "the author picked an option of it",
           "a reply that only said to apply", "did not answer it, so ask it here")


def test_the_card_offer_carries_the_card_text_in_the_message_that_asks() -> None:
    card = step(13)[step(13).index("    - The voice card"):]
    before(card, "the card text in the question", "Keep this voice card for this document?",
           "Put the card text whole in this reply", "even when an earlier message of the run showed it",
           "never \"the card is in my earlier message\"")
    assert TEXT.count("Keep this voice card for this document?") == 1


def test_the_trigger_is_a_reply_with_no_change_text_no_edit_number_and_no_other_instruction() -> None:
    para = _reply_paragraph()
    before(para, "tells you to apply and carries no change text, no edit number and no other instruction",
           "A message that carries any change text, edit number or other instruction is an ordinary answer",
           "ask what is unclear")
    assert "or to go on" not in para  # "go on" is not an instruction to apply


def test_the_paragraph_sits_under_branch_a_and_branch_b_keeps_its_question_flow() -> None:
    assert "A reply that only says to apply (Branch A only" in _branch("A")
    b = _branch("B")
    assert "A reply that only says to apply" not in b
    before(b, "Ask questions 1", "2 and 3 of step 11 in SKILL.md in that order", "each its own call")
    assert "In Branch B a bare apply decides none of the undecided edits: ask questions 2 and 3 as usual" in _branch("A")


def test_a_bare_apply_overrides_the_app_decided_hold_condition_on_purpose() -> None:
    para = _reply_paragraph()
    before(para, "overrides, on purpose, the hold condition of the App-decided run", "left pending in the app",
           "the app drops its pending suggestions when it reloads the file", "The one exception",
           "`unplaced` or `would_skip`", "ask `Apply`")


def test_a_stdin_run_takes_a_bare_apply_as_no_pick_and_asks_again() -> None:
    para = step(12)
    assert '"edit one", "no", "Yes, apply.")' in para
    before(para, "names no explicit numbers", "ask the author again for explicit numbers")


def test_a_bare_apply_runs_the_real_apply_even_when_every_edit_is_held() -> None:
    para = _reply_paragraph()
    before(para, "Run the real apply even when every edit is held and the dry run would apply nothing",
           "the apply also logs the session and deletes WORK",
           "\"nothing would change\" is never a reason to skip it", "The one exception")


def test_a_rejections_removal_is_run_once_and_its_renumbered_list_is_not_a_failed_removal() -> None:
    """The b2 runs of 2026-11-27 (r1, r2): the skill read the renumbered list `--remove 1` prints as 'rejection 1 is
    still stored', ran `--remove 1` again and deleted a second, wrong rejection (the verdict then saw two fewer)."""
    row = step(2)[step(2).index("| `rejections` |"):]
    row = row[:row.index("\n")]
    before(row, "Run `MEMORY <memory_argv...>` once", "deletes entry N and prints the list that is left, renumbered from 1",
           "the removal has happened", "Never run `--remove` again to check", "never read the script's source",
           "Show the numbered rejections")
