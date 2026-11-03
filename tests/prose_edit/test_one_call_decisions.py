# SPDX-License-Identifier: AGPL-3.0-or-later
"""After the author decides in MarkupEditor, the skill asks as little as it can (nexus-h72g3).

Design of record: T2 nexus/design-prose-edit-decision-flow-h72g3.md. A path run whose edits were all decided in the
app applies without the "Apply these changes?" question; the query question and the voice-card offer go in one
AskUserQuestion call; with neither, nothing is asked after the author says done. Pure text tests.
"""
from __future__ import annotations

from tests.prose_edit.test_markup_skill import TEXT, before, step


def _path_run_paragraph() -> str:
    s11 = step(11)
    return s11[s11.index("    Path run, before any question"):s11.index("    First say, in two sentences")]


def test_an_app_decided_run_applies_without_the_apply_question() -> None:
    s12 = step(12)
    para = s12[s12.index("    App-decided run"):s12.index("    Before the real apply")]
    assert "do not ask `Apply`" in para
    before(para, "dry run", "show", "Before the real apply", "without `--dry-run`")
    # The dry run itself is never skipped: the script refuses a real apply without one.
    assert "never skip the dry run" in s12


def test_a_run_with_chat_decisions_still_asks_apply() -> None:
    s12 = step(12)
    para = s12[s12.index("    App-decided run"):s12.index("    Before the real apply")]
    assert "any edit was decided in chat" in para and "asks `Apply` as above" in para
    assert "Apply these changes?" in s12


def test_nothing_is_asked_after_done_when_the_app_decided_everything() -> None:
    para = _path_run_paragraph()
    before(para, "When `undecided` is empty", "App-decided run")
    assert "asks nothing" in para


def test_query_question_and_voice_card_offer_share_one_call() -> None:
    para = _path_run_paragraph()
    assert "one `AskUserQuestion` call" in para and "header `Voice card`" in para and "header `Answer`" in para
    card = step(13)[step(13).index("    - The voice card"):]
    assert "already answered in step 11" in card and "do not ask it again" in card


def test_the_voice_card_offer_alone_is_still_asked_after_the_apply() -> None:
    card = step(13)[step(13).index("    - The voice card"):]
    assert "End your turn with that question." in card
    assert TEXT.count("Keep this voice card for this document?") == 1
