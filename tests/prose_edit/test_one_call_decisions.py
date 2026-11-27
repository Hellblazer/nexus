# SPDX-License-Identifier: AGPL-3.0-or-later
"""After the author decides in MarkupEditor, the skill asks as little as it safely can (nexus-h72g3).

Design of record: T2 nexus/design-prose-edit-decision-flow-h72g3.md. A path run whose edits were all accepted or
rejected in the app applies without the "Apply these changes?" question; anything held, warned, unplaced or skipped
keeps the question. The query question and the voice-card offer go in one AskUserQuestion call. Pure text tests over
the skill a model follows literally, so each pins a condition, not only a phrase.
"""
from __future__ import annotations

from tests.prose_edit.test_markup_skill import TEXT, before, step


def _path_run_paragraph() -> str:
    s11 = step(11)
    return s11[s11.index("    Path run, before any question"):s11.index("    First say, in two sentences")]


def _branch(letter: str) -> str:
    para = _path_run_paragraph()
    start = para.index(f"    - Branch {letter},")
    nxt = para.find("    - Branch ", start + 1)
    return para[start:nxt if nxt != -1 else len(para)]


def _app_decided() -> str:
    s12 = step(12)
    return s12[s12.index("    App-decided run (a path run only):"):s12.index("    Before the real apply")]


def _apply_paragraph() -> str:
    s12 = step(12)
    return s12[s12.index("    Run `REVIEW apply --work"):s12.index("    App-decided run (a path run only):")]


def test_exactly_two_branches_split_on_undecided() -> None:
    para = _path_run_paragraph()
    assert "take exactly one of these two branches" in para
    assert "Branch A, `undecided` is empty." in _branch("A")
    assert "Branch B, `undecided` is not empty." in _branch("B")
    # The old free-floating "asks nothing here" sentence, which a run with undecided edits could match, is gone.
    assert "the run asks nothing here" not in TEXT


def test_undecided_edits_always_reach_the_accept_and_others_questions() -> None:
    b = _branch("B")
    before(b, "Ask questions 1", "2 and 3", "then run `REVIEW decisions")
    assert "only with a query or a paragraph proposal" in b  # question 1 is the only optional one
    assert "go to step 12" not in b


def test_branch_a_asks_only_the_query_call_and_leaves_the_apply_decision_to_step_12() -> None:
    a = _branch("A")
    before(a, "query or a paragraph proposal", "ask question 1", "end your turn", "go to step 12")
    assert "Step 12 decides there whether the run is App-decided" in a
    assert "Branch B" in a  # a query answer that adds edits becomes undecided work


def test_the_no_ask_apply_needs_every_set_but_accept_and_reject_empty() -> None:
    para = _app_decided()
    assert "`undecided`, `hold` and `warnings` all empty" in para
    assert "`hold`, `unplaced` and `would_skip` all empty" in para
    before(para, "is not App-decided", "ask `Apply` as in step 12 of SKILL.md")
    before(para, "do not ask `Apply`", "run the dry run", "show it", "Step 12 before the real apply", "without `--dry-run`")
    assert "never skip the dry run" in para


def test_the_apply_paragraph_names_the_app_decided_exception() -> None:
    para = _apply_paragraph()
    assert "Apply these changes?" in para
    assert "in the no-edits close of step 11, do not ask: run it at once; nor in an App-decided run, below" in para


def test_a_reject_in_the_app_is_disclosed_before_the_author_decides() -> None:
    item = step(10)[step(10).index("    6. Tell the author to accept or reject"):]
    assert "a Reject in the app is stored for this document" in item
    assert "left pending is held" in item


def test_query_question_carries_the_voice_card_after_the_lookup() -> None:
    s11 = step(11)
    q1 = s11[s11.index("    1. Queries and paragraph proposals"):s11.index("    2. Accept.")]
    before(q1, "`MEMORY voice-card <path>`", "finds no card stored", "header `Voice card`", "same call",
           "two questions in that call and nothing else")
    # the no-edits close routes through the same combined question
    assert "in the query question below (with the voice card, as that question says)" in s11


def test_step_13_takes_the_combined_answer_and_handles_other() -> None:
    card = step(13)[step(13).index("    - The voice card"):]
    before(card, "already answered in step 11", "do not ask it again", '"Keep it"', '"Not now"',
           'typed in "Other"', "show the card with that change", "once more")
    assert "End your turn with that question." in card
    assert TEXT.count("Keep this voice card for this document?") == 1


def test_a_run_with_chat_decisions_still_asks_apply() -> None:
    """Branch B can end with every set empty (the author ticked each undecided edit in chat); only the Branch A
    condition and the chat sentence keep it on the Apply question, as the design's 'chat-decided edits keep the
    Apply confirm' requires."""
    para = _app_decided()
    assert "took Branch A of step 11 (no edit question was asked in chat)" in para
    assert "When any edit was decided in chat, the run asks `Apply` as in step 12 of SKILL.md." in para
    assert "ask the author to confirm unless the run is App-decided or follows a reply that only says to apply" in _apply_paragraph()


def test_a_no_to_the_query_question_goes_to_step_12_and_branch_b_uses_explicit_numbers() -> None:
    assert 'on "No", or with no query and no paragraph proposal' in _branch("A")
    b = _branch("B")
    assert "again with the author's explicit numbers" in b
    assert "only when it was not already asked in this step" in b
