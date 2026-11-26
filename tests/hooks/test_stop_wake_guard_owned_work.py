# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Stop wake guard on owned work at the turn end (turn-end census, T2 nexus/turn-end-owned-idle-census-2026-11-19).

Split from test_stop_wake_guard.py, which is at its file-size ceiling. The fixtures are the census's real miss texts,
the corpus's false positives and the reviewers' probes; each guard of the present-tense promise and of the
peer-recipient strip has a fixture that fails without it.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "stop_wake_guard.py"
_spec = importlib.util.spec_from_file_location("stop_wake_guard_owned_work", SCRIPT)
swg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(swg)  # type: ignore[union-attr]


# --- owned work at the turn end (turn-end census, T2 nexus/turn-end-owned-idle-census-2026-11-19) ----------------
#
# The three real misses of 2026-11-12..19: each ended on the session's own next step with nothing armed, and the guard
# allowed it, for a present-tense promise ("I'm doing X", "Next I'm starting X") or for a peer named only as the
# recipient ("handing the draft to the peer") being read as the wake.

_OWNED_MISSES = [
    ("the peer has the CI result, and the RDR-230 thread is off the board. Next I'm starting RDR-171 Step 5 "
     "(nexus-qrg1r.2).", "Next I'm starting"),
    ("Nothing of mine is dispatched right now. I'm doing the `nexus-uwggq.1.5` switch onto the by-scheme route "
     "myself, in my own worktree, so the commit guard doesn't affect it.", "I'm doing"),
    ("One assumption still needs checking: that repo documents carry no content hash when they're registered. After "
     "that come the Trade-offs and handing the draft to the peer. Step 3 is still waiting on the peer's fixes and verify "
     "run, and the peer will message me when there's news.", "After that come"),
]


@pytest.mark.parametrize("text", [
    "So I'm doing the switch myself.",
    "Right, I'm starting step 5.",
    "Okay, I'm starting step 5.",
    "Meanwhile I'm writing the draft.",
])
def test_a_present_tense_own_step_after_a_lead_word_blocks(text: str) -> None:
    assert swg.evaluate_text(text).block is True, swg.evaluate_text(text)


@pytest.mark.parametrize(("text", "clause"), _OWNED_MISSES, ids=["f1b056f7", "82180959", "986abbe7"])
def test_a_present_tense_own_step_at_the_turn_end_blocks(text: str, clause: str) -> None:
    verdict = swg.evaluate_text(text)
    assert verdict.block is True, verdict
    assert clause in verdict.clause, verdict


@pytest.mark.parametrize("text", [
    "It's not mine, so I'm taking no action.",
    "Nothing depends on that artifact, so I'm doing nothing about it.",
    "My last push, c53aebe12, is already green, so I'm taking no action on it.",
    "I'm not starting anything else tonight.",
    # corpus false positive (f1b056f7 11-19 01:14): the object of a denial, not a step
    "This doesn't change anything I'm doing right now.",
    # critic probes: a state, a relative or subordinate clause, a reasoning report
    "Then I'm done.",
    "Next we're at the release gate.",
    "Next I'm blocked on the jar.",
    "Nothing I'm doing depends on it.",
    "Whatever I'm doing, it is not affected.",
    "Since I'm doing it in my own worktree, nothing else is affected.",
    "If I'm starting from develop, the rebase is clean.",
    "That is the part I'm working on, and it is done.",
    "I'm taking this to be a no-op.",
    "I'm checking whether the file exists; it does not.",
    # code-review probes: an idiom, a state, a restraint the old lookahead missed
    "I'm starting to think this is a race.",
    "I'm beginning to see the problem.",
    "I'm working on the assumption that it's green.",
    "I'm running low on context.",
    "I'm doing well, thanks.",
    "I'm doing none of it.",
    "The hold stands: I'm starting zero new work.",
    # one fixture per word of the lookahead and at a clause start, so each can fail (code review r2)
    "I'm starting zero new work.",
    "I'm doing no further work.",
    "I'm doing little else.",
    "I'm doing fine.",
    "I'm doing ok.",
    "I'm doing okay.",
    "I'm starting only what's already approved.",
    "After that is merged, the release proceeds.",
    "After that is done, we're finished.",
])
def test_a_present_tense_restraint_does_not_block(text: str) -> None:
    assert swg.evaluate_text(text).block is False, text


@pytest.mark.parametrize(("text", "blocks"), [
    # a peer only as the recipient of my step: the step is mine, nothing will wake me for it
    ("I'll write the draft and give it to the peer.", True),
    ("I'll write the summary for the peer next.", True),
    # a peer as the subject of the arrival, or the gate: their message is the wake
    ("Once the peer reports back, I'll send the result to the peer.", False),
    ("I'll push in the peer's window.", False),
    ("After the peer sends the window, I'll push.", False),
    # corpus false positive (1a877265 11-12): a report of a peer's request, no step of mine; the recipient
    # is stripped only from a clause whose actor is me
    ("marginalia then asked for one more test for the peer: timing how long dropping the search index takes while "
     "searches are running.", False),
    # a wait cue anywhere in the clause keeps the peer as the wake (code review r1)
    ("I'll run the test for the peer once she's back.", False),
    ("I'm doing the review for the peer after the diff arrives.", False),
    # the wait-word lookbehind and the possessive each keep a peer wake
    ("Waiting for the peer, I'll run the check.", False),
    ("I'll rebase for the peer's window.", False),
])
def test_a_peer_as_recipient_is_not_the_wake(text: str, blocks: bool) -> None:
    assert swg.evaluate_text(text).block is blocks, swg.evaluate_text(text)


_HELD_BACK = ("Never start work the user held back or asked you to wait on, work that needs the user's approval, or "
              "work another session owns.")


def test_the_continuation_never_licenses_held_back_work() -> None:
    """The same sentence as the post-compaction checkpoint directive (the peer, 1776ff7b7): one rule in both places."""
    for cls in (swg.CLASS_PROMISE, swg.CLASS_TIMED, swg.CLASS_CI_WAIT):
        assert _HELD_BACK in " ".join(swg.block_reason(cls, "I'm doing it.").split())
    tree = ast.parse(SCRIPT.with_name("compact_checkpoint.py").read_text(encoding="utf-8"))
    directive = next(ast.literal_eval(node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(getattr(t, "id", "") == "_RESUME_DIRECTIVE" for t in node.targets))
    assert _HELD_BACK in directive, "the checkpoint directive changed: keep the two sentences equal"
