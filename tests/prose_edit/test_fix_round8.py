# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 8 of the RDR-221 Phase 1 exit (nexus-ger02.7): `voice-card --from-stdin` takes the card as plain text too.

The live check at 858d959df failed its card row on one thing: the orchestrating model wrote the card to CARD= as
plain text, not as {"voice_card": "..."}, the save refused it, and the model recovered on a second write. A card is
one string, so plain text is an unambiguous input; accepting it removes that failure without loosening anything
else (an object of any other shape is still refused).
"""
from __future__ import annotations

from tests.prose_edit.conftest import REPO_PROJECT, Prose, t2_get, t2_json

DOC = "docs/x.md"
TITLE = "doc/docs/x.md"
CARD = "First person plural. Devices: the closing contrast line."


def test_a_plain_text_card_on_stdin_is_stored_as_written(prose: Prose) -> None:
    out = prose.ok("voice-card", DOC, "--from-stdin", stdin=f"  {CARD}\n")
    assert out["voice_card"]["text"] == CARD
    assert t2_json(REPO_PROJECT, TITLE)["voice_card"]["text"] == CARD


def test_a_json_string_card_is_stored_like_the_object_form(prose: Prose) -> None:
    out = prose.ok("voice-card", DOC, "--from-stdin", stdin='"' + CARD + '"')
    assert out["voice_card"]["text"] == CARD


def test_input_that_looks_like_json_but_does_not_parse_is_refused_not_stored(prose: Prose) -> None:
    # Test validation (nexus-ger02.7 F1): plain text is accepted, so broken JSON must not fall through as a card.
    for broken in ('{"voice_card": "First person. Devices: x', '{"voice_card": "abc",}',
                   '{"voice_card":"a"} trailing', '["x"', '"unterminated'):
        proc = prose.run("voice-card", DOC, "--from-stdin", stdin=broken)
        assert proc.returncode == 1, (broken, proc.stderr)
        assert proc.stdout == "" and t2_get(REPO_PROJECT, TITLE) is None


def test_a_fenced_plain_card_is_stored_without_the_fence(prose: Prose) -> None:
    out = prose.ok("voice-card", DOC, "--from-stdin", stdin=f"```\n{CARD}\n```\n")
    assert out["voice_card"]["text"] == CARD


def test_the_object_form_still_works_and_other_shapes_are_still_refused(prose: Prose) -> None:
    assert prose.ok("voice-card", DOC, "--from-stdin", stdin={"voice_card": CARD})["voice_card"]["text"] == CARD
    for bad in ({"card": CARD}, {"voice_card": CARD, "extra": 1}, {"voice_card": ""}, ["x"], "   \n"):
        proc = prose.run("voice-card", DOC, "--from-stdin", stdin=bad)
        assert proc.returncode == 1, (bad, proc.stderr)
        assert proc.stdout == ""
