# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart sandbox reads the reply to the prompt it sent, never an earlier turn's (nexus-xwijj).

The 2026-11-02 roll failed rs_alpha with "THAT'S THE HANDOFF ..." although the session answered
HELIOTROPE: the handoff turn's reply landed after the reply count was taken, so the wait ended on it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"
ASK = "What is the project code word for this task? Reply with only the word."


def _run_module():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_reply", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _user(text) -> dict:
    return {"type": "user", "message": {"content": text}}


def _said(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool() -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1"}]}}


def test_the_handoff_turn_s_reply_is_not_the_answer() -> None:
    """The failing roll's order: the handoff reply, then the question, then the answer."""
    events = [_user("cat /tmp/h.md"), _tool(), _user([{"type": "tool_result"}]),
              _said("That's the handoff for the restarted rs_alpha session."), _user(ASK), _said("HELIOTROPE")]
    assert _run_module().reply_after(events, ASK) == "HELIOTROPE"


def test_no_answer_yet_is_none() -> None:
    events = [_user("cat /tmp/h.md"), _said("That's the handoff."), _user(ASK), _tool()]
    assert _run_module().reply_after(events, ASK) is None


def test_a_prompt_not_yet_in_the_transcript_is_none() -> None:
    assert _run_module().reply_after([_user("cat /tmp/h.md"), _said("That's the handoff.")], ASK) is None


def test_the_last_asking_counts_when_a_prompt_repeats() -> None:
    events = [_user(ASK), _said("UNKNOWN"), _user(ASK), _said("HELIOTROPE")]
    assert _run_module().reply_after(events, ASK) == "HELIOTROPE"


def test_text_parts_in_a_list_prompt_match() -> None:
    events = [_user([{"type": "text", "text": ASK}]), _said("HELIOTROPE")]
    assert _run_module().reply_after(events, ASK) == "HELIOTROPE"


def test_a_preamble_before_a_tool_call_is_not_the_answer() -> None:
    """Text, a tool call, its result, then the answer: the answer is the turn's last text."""
    events = [_user(ASK), _said("Let me check the handoff."), _tool(), _user([{"type": "tool_result"}]),
              _said("HELIOTROPE")]
    assert _run_module().reply_after(events, ASK) == "HELIOTROPE"


def test_a_turn_still_in_its_tool_call_is_none() -> None:
    """A preamble then a pending tool call or result: the turn is not over, whatever the status says."""
    run = _run_module()
    assert run.reply_after([_user(ASK), _said("Let me check the handoff."), _tool()], ASK) is None
    assert run.reply_after([_user(ASK), _said("Let me check."), _tool(), _user([{"type": "tool_result"}])], ASK) is None


def test_the_next_prompt_ends_the_turn() -> None:
    events = [_user(ASK), _said("HELIOTROPE"), _user("something else"), _said("OTHER")]
    assert _run_module().reply_after(events, ASK) == "HELIOTROPE"
