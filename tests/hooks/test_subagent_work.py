# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A dispatched agent is background work its identity owns (RDR-253 Step 6 part 1, nexus-hczdo): SubagentStart registers
``kind=agent`` with ref ``agent:<agent id>``, SubagentStop removes the row. Register and remove only, once each: no
per-step write. A hook never blocks the dispatch or the stop (a failed label is a False and a log line)."""
from __future__ import annotations

import pytest

from nexus.hooks import subagent_start, subagent_stop_t1_nudge as nudge, subagent_work

START = {"session_id": "s1", "agent_id": "a220cf773a02bf4d7", "agent_type": "conexus:developer", "task": "x", "cwd": "/w"}
REF = "agent:a220cf773a02bf4d7"


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """What the hooks wrote, as ``(kind, ref, what)``: ``running`` for a registration, ``forget`` for the stop's removal."""
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr("nexus.identity.work.register_own",
                        lambda kind, ref, state="running", **k: seen.append((kind, ref, state)) or True)
    monkeypatch.setattr("nexus.identity.work.forget_own", lambda kind, ref, **k: seen.append((kind, ref, "forget")) or True)
    return seen


@pytest.fixture(autouse=True)
def _no_recorder_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.hooks import agent_dispatch_record  # noqa: PLC0415

    monkeypatch.setattr(agent_dispatch_record, "spawn", lambda *a: True)


def test_start_registers_the_agent_running(labels: list) -> None:
    assert subagent_work.register_start(START) is True
    assert labels == [("agent", REF, "running")]


def test_stop_removes_the_agents_row(labels: list) -> None:
    """Review ship-blocker 2: every dispatch used to leave a done row, so a busy identity reached the 200-row limit by agents
    alone; the ready check ignores done rows anyway. The stop forgets the row."""
    assert subagent_work.register_stop(START) is True
    assert labels == [("agent", REF, "forget")]


@pytest.mark.parametrize("payload", [None, {}, {"agent_id": "a1"}, {"agent_type": "conexus:developer"},
                                     {"agent_id": "", "agent_type": "x"}, {"agent_id": 7, "agent_type": "x"}])
def test_an_event_with_no_agent_id_or_no_agent_type_writes_nothing(labels: list, payload: object) -> None:
    """About 95% of SubagentStop events are harness-internal and carry no agent_type (nexus-zfxo3); hooks.json never
    spawns the stop verb for them, so a start without a type would leave a running row nothing ends."""
    assert subagent_work.register_start(payload) is False and subagent_work.register_stop(payload) is False
    assert labels == []


def test_a_label_that_fails_is_a_false_never_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr("nexus.identity.work.register_own", boom)
    monkeypatch.setattr("nexus.identity.work.forget_own", boom)
    assert subagent_work.register_start(START) is False
    assert subagent_work.register_stop(START) is False


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


def test_the_stop_verb_removes_the_row_of_an_agent_that_gets_no_t1_nudge(labels: list) -> None:
    """web-fetch and the other NO_T1_AGENTS are never nudged, but SubagentStart registered them: their row must end."""
    stop = {**START, "agent_type": "web-fetch", "agent_transcript_path": "/no/such/transcript"}
    assert nudge.run(stop).stdout in (None, "")
    assert labels == [("agent", REF, "forget")]


def test_the_stop_verb_removes_the_row_even_when_the_transcript_is_unreadable(labels: list) -> None:
    nudge.run({**START, "agent_transcript_path": "/no/such/transcript"})
    assert labels == [("agent", REF, "forget")]


_NO_PUT_TRANSCRIPT = '{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}\n'


def test_a_nudged_agent_loses_its_row_at_the_stop_that_nudged_it(labels: list, tmp_path) -> None:
    """gh#1680: the nudge used to skip the removal and wait for a second stop, which a read-only agent that ends with its
    hand-back often never makes, so its row stayed running while the session lived. The row ends at every stop."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_NO_PUT_TRANSCRIPT, encoding="utf-8")
    out = nudge.run({**START, "agent_transcript_path": str(transcript)})
    assert out.stdout and "You wrote nothing to T1" in out.stdout
    assert labels == [("agent", REF, "forget")], "the nudging stop removes the row; no second stop is needed"


def test_a_second_stop_after_the_nudge_forgets_again_and_stays_idempotent(labels: list, tmp_path) -> None:
    """``forget_own`` answers True for a row already gone, so the re-stop (``stop_hook_active``) is a no-op that must
    neither raise nor nudge again."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_NO_PUT_TRANSCRIPT, encoding="utf-8")
    nudge.run({**START, "agent_transcript_path": str(transcript)})
    again = nudge.run({**START, "agent_transcript_path": str(transcript), "stop_hook_active": True})
    assert again.stdout in (None, ""), "no second nudge"
    assert labels and all(label == ("agent", REF, "forget") for label in labels), "every recorded call is a forget"


def test_a_forget_that_raises_during_a_nudging_stop_still_returns_the_exact_nudge(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The removal is best effort and runs after the verdict: a failing engine must neither cost the nudge nor change it."""
    from nexus._hook_runtime._io import additional_context  # noqa: PLC0415

    def boom(kind, ref, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr("nexus.identity.work.forget_own", boom)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_NO_PUT_TRANSCRIPT, encoding="utf-8")
    out = nudge.run({**START, "agent_transcript_path": str(transcript)})
    assert out.stdout == additional_context("SubagentStop", nudge.NUDGE)


def test_an_orphan_stop_with_no_agent_type_writes_nothing(labels: list) -> None:
    nudge.run({"session_id": "s1", "agent_id": "a1"})
    assert labels == []


def test_a_hooks_registration_never_purges_and_a_stop_that_raises_never_costs_the_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review ship-blocker 2 (the hook has 10 s; a purge is up to ~200 sequential calls) and the stop-side try/except that no
    test could fail before: ``_label`` swallows everything, so the guard in ``run`` is only reached by a raising stop."""
    seen: list[dict] = []
    monkeypatch.setattr("nexus.identity.work.register_own", lambda kind, ref, state="running", **k: seen.append(k) or True)
    subagent_work.register_start(START)
    assert seen and seen[0].get("purge") is False

    def boom(payload):
        raise RuntimeError("stop label bug")

    monkeypatch.setattr(subagent_work, "register_stop", boom)
    out = nudge.run({**START, "agent_transcript_path": "/no/such/transcript"})
    assert out.stdout in (None, "")


def test_a_registration_that_succeeds_leaves_one_structured_log_line(labels: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review item 7 (the measure): the hooks logged only failures, so the number of registered agents was unobservable."""
    events: list[tuple[str, dict]] = []

    class Recorder:
        def info(self, event: str, **kw) -> None:
            events.append((event, kw))

        def warning(self, event: str, **kw) -> None:
            events.append((event, kw))

    monkeypatch.setattr(subagent_work, "_log", Recorder())
    subagent_work.register_start(START)
    subagent_work.register_stop(START)
    assert events == [
        ("subagent_work_registered", {"agent_id": "a220cf773a02bf4d7", "agent_type": "conexus:developer"}),
        ("subagent_work_forgotten", {"agent_id": "a220cf773a02bf4d7", "agent_type": "conexus:developer"}),
    ]
