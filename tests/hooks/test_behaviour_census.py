# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart behaviour census hook (conexus/hooks/scripts/behaviour_census.py).

Every case runs the REAL script as a subprocess against a real transcript
directory. Deliberately not a reimplementation of its counting: a gate whose
domain excludes the shipped artifact proves only that the gate's own copy
works (nexus-01, 2026-09-19, on
``test_a_stdlib_only_verb_dispatch_never_loads_structlog`` passing green while
every real verb regressed, because it dispatched a synthetic verb written into
tmp_path).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "behaviour_census.py"


def _tool_use(name: str, command: str | None = None) -> str:
    block: dict = {"type": "tool_use", "name": name, "input": {}}
    if command is not None:
        block["input"]["command"] = command
    return json.dumps({"isSidechain": False, "message": {"role": "assistant", "content": [block]}})


def _run(transcript_path: Path, session_id: str = "current") -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"session_id": session_id, "transcript_path": str(transcript_path)})
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload, capture_output=True, text=True, timeout=60, check=False,
    )


def test_the_script_ships_where_the_hook_declares_it() -> None:
    assert SCRIPT.is_file(), f"hooks.json declares {SCRIPT.name}; it must exist"


def test_no_prior_session_is_silent(tmp_path: Path) -> None:
    """A first-ever session has nothing to report and must not say so loudly."""
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_a_prior_transcript_with_no_tool_calls_says_skipped_not_zero(tmp_path: Path) -> None:
    """The non-vacuity assert. A parse that saw nothing is not a session that
    did nothing, and 0% would read identically to one that was measured."""
    (tmp_path / "prior.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    )
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0
    assert "skipped, not zero" in r.stdout
    assert "0.0%" not in r.stdout


def test_it_reports_delegation_and_deliberation_from_a_real_transcript(tmp_path: Path) -> None:
    lines = [
        _tool_use("mcp__plugin_conexus_sequential-thinking__sequentialthinking"),
        _tool_use("Edit"),                       # decision, thought precedes it
        _tool_use("Agent"),                      # decision + dispatch
        _tool_use("Bash", "ls -la"),             # hands-on, NOT a decision
        _tool_use("Bash", "git commit -m x"),    # hands-on AND a decision
    ]
    (tmp_path / "prior.jsonl").write_text("\n".join(lines) + "\n")
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    # hands-on is Edit + both Bash calls = 3 (Edit counts as hands-on AND as a
    # decision; different denominators, not double counting). 1 dispatch of 4.
    assert "delegation **25.0%**" in r.stdout
    assert "1 dispatched, 3 by hand" in r.stdout
    # 3 decisions (Edit, Agent, git commit), all inside the 6-call window
    assert "of 3 decisions" in r.stdout


def test_subagent_calls_are_excluded(tmp_path: Path) -> None:
    """A subagent's own calls are not the orchestrator's refusal to delegate;
    counting them would make delegation look worse the more you delegate."""
    own = json.loads(_tool_use("Edit"))
    sub = json.loads(_tool_use("Edit"))
    sub["isSidechain"] = True
    (tmp_path / "prior.jsonl").write_text(
        json.dumps(own) + "\n" + (json.dumps(sub) + "\n") * 20
    )
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert "of 1 decisions" in r.stdout, "sidechain entries must not be counted"


def test_the_current_session_is_never_its_own_previous(tmp_path: Path) -> None:
    """Excluded by session_id, not by mtime: the current transcript is the
    newest file on disk precisely because it is being written now."""
    cur = tmp_path / "mysession.jsonl"
    cur.write_text("\n".join(_tool_use("Edit") for _ in range(3)) + "\n")
    r = _run(cur, session_id="mysession")
    assert r.stdout.strip() == "", "the session must not report itself"


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null"])
def test_a_malformed_payload_never_blocks_a_session(payload: str) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload, capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode == 0, "a census must never fail a session start"
