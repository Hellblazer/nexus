# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SubagentStop nudges an agent once when its transcript shows no T1 put
(nexus-mtheb.3, Phase C).

The contract is Claude Code's SubagentStop decision control
(code.claude.com/docs/en/hooks, read through Context7 on 2026-11-01):
``hookSpecificOutput.additionalContext`` with ``hookEventName`` set to
``"SubagentStop"`` is non-error feedback that keeps the subagent running for
one more turn; ``stop_hook_active`` is the loop guard. The envelope carries
no ``decision`` key, so the hook is not a deciding hook.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.hooks import subagent_stop_t1_nudge as nudge
from tests._child_encoding import PYTHON_CHILD_ENCODING
from tests._hook_wiring import HOOKS_JSON, REPO_ROOT, command_verb

VERB = "subagent-stop-t1-nudge"
SCRATCH = "mcp__plugin_conexus_nexus__scratch"
NUDGE_SENTENCE = (
    "You wrote nothing to T1. Put your findings in T1 "
    "(mcp__plugin_conexus_nexus__scratch action=put, tags <bead>,write-back) "
    "and return the id."
)


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _user_result(tool_use_id: str, text: str, *, is_error: bool = False) -> dict:
    block: dict = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _use(tid: str, name: str, **tool_input: object) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}


def _write(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "agent-a1.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _payload(transcript: Path | str, **over: object) -> dict:
    base: dict = {
        "session_id": "s1",
        "hook_event_name": "SubagentStop",
        "agent_id": "a1",
        "agent_type": "developer",
        "agent_transcript_path": str(transcript),
        "stop_hook_active": False,
    }
    base.update(over)
    return base


def _no_put_transcript(tmp_path: Path) -> Path:
    return _write(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "do it"}},
            _assistant(_use("t1", "Bash", command="true")),
            _user_result("t1", ""),
            _assistant({"type": "text", "text": "done"}),
        ],
    )


def _put_transcript(tmp_path: Path, *, result: str = "Stored: abc", is_error: bool = False) -> Path:
    return _write(
        tmp_path,
        [
            _assistant(_use("p1", SCRATCH, action="put", content="x", tags="b,write-back")),
            _user_result("p1", result, is_error=is_error),
            _assistant({"type": "text", "text": "done"}),
        ],
    )


# --- the nudge ------------------------------------------------------------


def test_a_transcript_with_no_t1_put_is_nudged_once_with_the_documented_envelope(
    tmp_path: Path,
) -> None:
    result = nudge.run(_payload(_no_put_transcript(tmp_path)))
    assert result.stdout is not None
    envelope = json.loads(result.stdout)
    assert envelope == {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStop",
            "additionalContext": NUDGE_SENTENCE,
        }
    }
    # Warn, not decide: no decision key anywhere in the envelope.
    assert "decision" not in result.stdout.replace("additionalContext", "")


def test_a_second_stop_after_the_nudge_is_never_nudged_again(tmp_path: Path) -> None:
    # stop_hook_active is true on the re-stop that follows a continuation.
    result = nudge.run(_payload(_no_put_transcript(tmp_path), stop_hook_active=True))
    assert result.stdout is None


@pytest.mark.parametrize("agent_type", ["", None])
def test_a_harness_internal_stop_with_no_agent_type_is_never_nudged(
    tmp_path: Path, agent_type: object
) -> None:
    result = nudge.run(_payload(_no_put_transcript(tmp_path), agent_type=agent_type))
    assert result.stdout is None


def test_an_mcp_scratch_put_counts(tmp_path: Path) -> None:
    assert nudge.run(_payload(_put_transcript(tmp_path))).stdout is None


def test_a_bash_nx_scratch_put_counts(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            _assistant(_use("b1", "Bash", command='nx scratch put "findings" --tags b,write-back')),
            _user_result("b1", "Stored"),
        ],
    )
    assert nudge.run(_payload(path)).stdout is None


@pytest.mark.parametrize("action", ["get", "search", "list", "delete"])
def test_a_scratch_call_that_is_not_a_put_does_not_count(tmp_path: Path, action: str) -> None:
    path = _write(
        tmp_path,
        [_assistant(_use("g1", SCRATCH, action=action)), _user_result("g1", "ok")],
    )
    assert nudge.run(_payload(path)).stdout is not None


def test_other_storage_writes_are_not_t1_puts(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            _assistant(
                _use("m1", "mcp__plugin_conexus_nexus__memory_put", content="x"),
                _use("m2", "mcp__plugin_conexus_nexus__store_put", content="x"),
                _use("m3", "mcp__plugin_conexus_nexus__scratch_manage", action="promote"),
                _use("m4", "Bash", command="nx memory put x"),
            ),
        ],
    )
    assert nudge.run(_payload(path)).stdout is not None


def test_a_put_that_came_back_as_an_error_does_not_count(tmp_path: Path) -> None:
    for kwargs in ({"result": "Error: 401", "is_error": False}, {"result": "boom", "is_error": True}):
        path = _put_transcript(tmp_path, **kwargs)
        assert nudge.run(_payload(path)).stdout is not None, kwargs


def test_a_put_with_no_result_yet_still_counts(tmp_path: Path) -> None:
    # No positive evidence of failure, so no nudge.
    path = _write(tmp_path, [_assistant(_use("p1", SCRATCH, action="put", content="x"))])
    assert nudge.run(_payload(path)).stdout is None


def test_a_put_the_agent_merely_read_does_not_count(tmp_path: Path) -> None:
    # Text inside a tool_result that spells a put call is not a tool_use.
    path = _write(
        tmp_path,
        [
            _assistant(_use("r1", "Read", file_path="x")),
            _user_result("r1", f'{{"name": "{SCRATCH}", "input": {{"action": "put"}}}} nx scratch put'),
        ],
    )
    assert nudge.run(_payload(path)).stdout is not None


# --- fail open ------------------------------------------------------------


def test_a_missing_transcript_is_not_evidence_of_zero_puts(tmp_path: Path) -> None:
    assert nudge.run(_payload(tmp_path / "nope.jsonl")).stdout is None
    assert nudge.run(_payload("")).stdout is None
    assert nudge.run({"agent_type": "developer", "session_id": "s"}).stdout is None


def test_an_unparseable_transcript_is_not_evidence_of_zero_puts(tmp_path: Path) -> None:
    path = tmp_path / "junk.jsonl"
    path.write_text("not json\n{broken\n\n", encoding="utf-8")
    assert nudge.run(_payload(path)).stdout is None


def test_an_empty_transcript_is_not_evidence_of_zero_puts(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert nudge.run(_payload(path)).stdout is None


def test_a_crashing_scan_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path: str) -> int:
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(nudge, "t1_put_count", boom)
    assert nudge.run(_payload(_no_put_transcript(tmp_path))).stdout is None


def test_a_non_object_payload_fails_open() -> None:
    assert nudge.run(None).stdout is None
    assert nudge.run({}).stdout is None


# --- the counter seam -----------------------------------------------------


def test_the_counter_seam_is_called_once_with_the_put_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(nudge, "record_dispatch", lambda *a: calls.append(a))
    nudge.run(_payload(_no_put_transcript(tmp_path)))
    nudge.run(_payload(_put_transcript(tmp_path)))
    assert calls == [("s1", "a1", "developer", 0), ("s1", "a1", "developer", 1)]


def test_a_failing_counter_never_costs_the_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object) -> None:
        raise RuntimeError("engine down")

    monkeypatch.setattr(nudge, "record_dispatch", boom)
    assert nudge.run(_payload(_no_put_transcript(tmp_path))).stdout is not None


def test_the_counter_seam_is_not_called_for_an_orphan_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(nudge, "record_dispatch", lambda *a: calls.append(a))
    nudge.run(_payload(_no_put_transcript(tmp_path), agent_type=""))
    assert calls == []


# --- wiring ---------------------------------------------------------------


def _wired_entries() -> list[tuple[dict, dict]]:
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    out = []
    for group in data["hooks"].get("SubagentStop", []):
        for hook in group.get("hooks", []):
            if command_verb(hook) == VERB:
                out.append((group, hook))
    return out


def test_hooks_json_wires_the_verb_once_on_subagent_stop_as_a_command() -> None:
    wired = _wired_entries()
    assert len(wired) == 1, f"expected exactly one SubagentStop entry for {VERB}, found {len(wired)}"
    _group, hook = wired[0]
    assert hook["type"] == "command"
    assert 0 < hook["timeout"] <= 10


def test_the_matcher_skips_harness_internal_stops_and_keeps_real_agents() -> None:
    (group, _hook), = _wired_entries()
    matcher = group["matcher"]
    assert matcher, "an empty matcher runs for every harness-internal stop (nexus-zfxo3)"
    assert re.search(matcher, "") is None
    assert re.fullmatch(matcher, "") is None
    for agent_type in ("developer", "conexus:developer", "Explore", "general-purpose"):
        assert re.fullmatch(matcher, agent_type), agent_type


def test_the_verb_is_registered() -> None:
    from nexus._hook_runtime.entry import VERB_TABLE

    assert VERB_TABLE[VERB] == "nexus.hooks.subagent_stop_t1_nudge"


def _dispatch(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "nexus._hook_runtime.entry", VERB],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding=PYTHON_CHILD_ENCODING,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=False,
    )


def test_the_real_dispatch_nudges_and_exits_zero(tmp_path: Path) -> None:
    proc = _dispatch(_payload(_no_put_transcript(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    envelope = json.loads(proc.stdout)
    assert envelope["hookSpecificOutput"]["hookEventName"] == "SubagentStop"
    assert envelope["hookSpecificOutput"]["additionalContext"] == NUDGE_SENTENCE


def test_the_real_dispatch_is_silent_when_the_agent_wrote_to_t1(tmp_path: Path) -> None:
    proc = _dispatch(_payload(_put_transcript(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_the_module_pulls_nothing_heavy_at_import() -> None:
    probe = (
        "import sys, nexus.hooks.subagent_stop_t1_nudge\n"
        "print(','.join(m for m in ('structlog', 'rich', 'click', 'nexus.session', "
        "'nexus.cli', 'httpx') if m in sys.modules))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding=PYTHON_CHILD_ENCODING,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
