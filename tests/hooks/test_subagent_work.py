# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A dispatched agent is background work its identity owns (RDR-253 Step 6 part 1, nexus-hczdo): SubagentStart registers
``kind=agent`` with ref ``agent:<agent id>``, SubagentStop marks it ``done``. Register and done only, once each: no
per-step write. A hook never blocks the dispatch or the stop (a failed label is a False and a log line)."""
from __future__ import annotations

import pytest

from nexus.hooks import subagent_start, subagent_stop_t1_nudge as nudge, subagent_work

START = {"session_id": "s1", "agent_id": "a220cf773a02bf4d7", "agent_type": "conexus:developer", "task": "x", "cwd": "/w"}
REF = "agent:a220cf773a02bf4d7"


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr("nexus.identity.work.register_own",
                        lambda kind, ref, state="running", **k: seen.append((kind, ref, state)) or True)
    return seen


@pytest.fixture(autouse=True)
def _no_recorder_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.hooks import agent_dispatch_record  # noqa: PLC0415

    monkeypatch.setattr(agent_dispatch_record, "spawn", lambda *a: True)


def test_start_registers_the_agent_running(labels: list) -> None:
    assert subagent_work.register_start(START) is True
    assert labels == [("agent", REF, "running")]


def test_stop_marks_the_agent_done(labels: list) -> None:
    assert subagent_work.mark_done(START) is True
    assert labels == [("agent", REF, "done")]


@pytest.mark.parametrize("payload", [None, {}, {"agent_id": "a1"}, {"agent_type": "conexus:developer"},
                                     {"agent_id": "", "agent_type": "x"}, {"agent_id": 7, "agent_type": "x"}])
def test_an_event_with_no_agent_id_or_no_agent_type_writes_nothing(labels: list, payload: object) -> None:
    """About 95% of SubagentStop events are harness-internal and carry no agent_type (nexus-zfxo3); hooks.json never
    spawns the stop verb for them, so a start without a type would leave a running row nothing ends."""
    assert subagent_work.register_start(payload) is False and subagent_work.mark_done(payload) is False
    assert labels == []


def test_a_label_that_fails_is_a_false_never_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr("nexus.identity.work.register_own", boom)
    assert subagent_work.register_start(START) is False
    assert subagent_work.mark_done(START) is False


@pytest.fixture
def start_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_t2_memory_section", "_linked_rdrs_section", "_knowledge_map_section", "_t1_scratch_section"):
        monkeypatch.setattr(subagent_start, name, lambda *a, **k: "")
    monkeypatch.setattr(subagent_start, "_nx_env", lambda sid: {})
    monkeypatch.setattr(subagent_start, "_repo_root", lambda env: "/r")
    monkeypatch.setattr(subagent_start, "_worktree_preflight_row", lambda *a, **k: "")


def test_the_subagent_start_hook_registers_the_dispatch(labels: list, start_sections: None) -> None:
    result = subagent_start.run(START)
    assert labels == [("agent", REF, "running")]
    assert result.stdout and "Claimant id: a220cf773a02bf4d7" in result.stdout


def test_a_failed_registration_never_blocks_the_dispatch(monkeypatch: pytest.MonkeyPatch, start_sections: None) -> None:
    def boom(*a, **k):
        raise RuntimeError("no engine")

    monkeypatch.setattr(subagent_work, "register_start", boom)
    result = subagent_start.run(START)
    assert result.stdout and "Claimant id: a220cf773a02bf4d7" in result.stdout


def test_the_stop_verb_marks_done_for_an_agent_that_gets_no_t1_nudge(labels: list) -> None:
    """web-fetch and the other NO_T1_AGENTS are never nudged, but SubagentStart registered them: their row must end."""
    stop = {**START, "agent_type": "web-fetch", "agent_transcript_path": "/no/such/transcript"}
    assert nudge.run(stop).stdout in (None, "")
    assert labels == [("agent", REF, "done")]


def test_the_stop_verb_marks_done_even_when_the_transcript_is_unreadable(labels: list) -> None:
    nudge.run({**START, "agent_transcript_path": "/no/such/transcript"})
    assert labels == [("agent", REF, "done")]


def test_a_nudged_agent_runs_one_more_turn_so_its_row_ends_at_the_stop_after_the_nudge(labels: list, tmp_path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}\n')
    out = nudge.run({**START, "agent_transcript_path": str(transcript)})
    assert out.stdout and "You wrote nothing to T1" in out.stdout
    assert labels == [], "the nudge keeps the agent running: not done yet"
    nudge.run({**START, "agent_transcript_path": str(transcript), "stop_hook_active": True})
    assert labels == [("agent", REF, "done")]


def test_an_orphan_stop_with_no_agent_type_writes_nothing(labels: list) -> None:
    nudge.run({"session_id": "s1", "agent_id": "a1"})
    assert labels == []
