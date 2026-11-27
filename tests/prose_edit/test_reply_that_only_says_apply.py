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
    start = section.index("  Bare apply (Branch A only")
    return section[start:section.index("- Branch B,", start)]


def test_a_bare_apply_is_a_no_on_the_query_and_leaves_the_card_open() -> None:
    para = _reply_paragraph()
    before(para, "after question 1, with or without the voice-card question", '("Yes, apply.")',
           "It counts as No on question 1: do not ask which question it answers",
           "A voice-card question that went in the call stays open", "step 13 asks it after the apply")


def test_a_bare_apply_runs_the_dry_run_then_the_real_apply_in_the_same_turn() -> None:
    para = _reply_paragraph()
    before(para, "Go to step 12 with the printed `apply_args`", "dry run", "show it",
           "in the same turn", "Step 12 before the real apply", "with no `Apply` question")


def test_a_bare_apply_still_asks_apply_when_the_dry_run_leaves_something_out() -> None:
    para = _reply_paragraph()
    before(para, "The one exception", "`unplaced` or `would_skip`", "ask `Apply` as step 12 of SKILL.md says")


def test_step_12_names_the_bare_apply_as_a_case_that_skips_the_apply_question() -> None:
    s12 = step(12)
    para = s12[s12.index("    Run `REVIEW apply --work"):s12.index("    App-decided run (a path run only):")]
    assert "nor after a reply that only says to apply" in para
    assert ("ask the author to confirm unless the run is App-decided or follows a reply that only says to apply "
            "(and the dry run shows no `unplaced` or `would_skip`).") in para
    assert "in the no-edits close of step 11, do not ask: run it at once; nor in an App-decided run, below" in para


def test_the_card_offer_carries_the_card_text_in_the_message_that_asks() -> None:
    card = step(13)[step(13).index("    - The voice card"):]
    before(card, "the card text in the question", "Keep this voice card for this document?")
    assert TEXT.count("Keep this voice card for this document?") == 1


def test_the_trigger_is_a_reply_with_no_change_text_no_edit_number_and_no_other_instruction() -> None:
    para = _reply_paragraph()
    before(para, "tells you to apply and carries no change text, no edit number and no other instruction",
           "any of them makes it an ordinary answer")
    assert "or to go on" not in para  # "go on" is not an instruction to apply


def test_the_paragraph_sits_under_branch_a_and_branch_b_keeps_its_question_flow() -> None:
    assert "Bare apply (Branch A only" in _branch("A")
    b = _branch("B")
    assert "Bare apply" not in b
    before(b, "Ask questions 1", "2 and 3 of step 11 in SKILL.md in that order", "each its own call")
    assert "In Branch B it decides no undecided edit: ask questions 2 and 3 as usual" in _branch("A")


def test_a_bare_apply_overrides_the_app_decided_hold_condition_on_purpose() -> None:
    para = _reply_paragraph()
    before(para, "overrides the App-decided run's hold condition on purpose", "pending edits stay held",
           "The one exception", "`unplaced` or `would_skip`", "ask `Apply`")


def test_a_stdin_run_takes_a_bare_apply_as_no_pick_and_asks_again() -> None:
    para = step(12)
    assert '"edit one", "no", "Yes, apply.")' in para
    before(para, "names no explicit numbers", "ask the author again for explicit numbers")


def test_a_bare_apply_runs_the_real_apply_even_when_every_edit_is_held() -> None:
    para = _reply_paragraph()
    before(para, "Run the real apply even when every edit is held", "it also logs the session and deletes WORK",
           "\"nothing would change\" is no reason to skip it", "The one exception")


def test_a_rejections_removal_is_run_once_and_its_renumbered_list_is_not_a_failed_removal() -> None:
    """The b2 runs of 2026-11-27 (r1, r2): the skill read the renumbered list `--remove 1` prints as 'rejection 1 is
    still stored', ran `--remove 1` again and deleted a second, wrong rejection (the verdict then saw two fewer)."""
    row = step(2)[step(2).index("| `rejections` |"):]
    row = row[:row.index("\n")]
    before(row, "Run `MEMORY <memory_argv...>` once", "deletes entry N and prints the list that is left, renumbered from 1",
           "the removal has happened", "Never run `--remove` again to check", "never read the script's source",
           "Show the numbered rejections")


def test_a_bare_apply_also_covers_question_1_asked_alone() -> None:
    """A stored card, or no voice_card in the proposal, leaves question 1 alone in the call: the trigger and the
    voice-card sentences must not presume the card question was asked."""
    para = _reply_paragraph()
    before(para, "with or without the voice-card question", "A voice-card question that went in the call")
    assert "of either question" not in para
