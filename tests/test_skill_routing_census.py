"""Tests for scripts/skill_routing_census.py (bead nexus-dg7i6.1).

The census is the baseline instrument for the skill-routing epic
(nexus-dg7i6): the listing changes in .2/.3/.4 are judged by how its
numbers move. Every count below is known by construction from the
synthetic fixture, never read back from the live corpus.

Traps the fixture is built against:
- isSidechain records replayed into a top-level session file must not be
  counted as top-level Skill calls (they are subagent traffic);
- a user typing /conexus:x is a slash command, not a model-initiated
  Skill call, and must land in its own bucket;
- runs whose FIRST timestamp falls outside the window are excluded whole.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "skill_routing_census.py"

START = "2026-10-01T00:00:00+00:00"
END = "2026-11-01T00:00:00+00:00"
IN_TS = "2026-10-10T12:00:00.000Z"
OUT_TS = "2026-09-10T12:00:00.000Z"


def _tool(name: str, ts: str, *, sidechain: bool = False, **inp) -> dict:
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
    }
    if sidechain:
        rec["isSidechain"] = True
    return rec


def _text(ts: str) -> dict:
    return {"type": "assistant", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": "ok — ✓"}]}}


def _slash(name: str, ts: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {
        "role": "user",
        "content": f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"}}


def _write(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n")


def _fixture(root: Path) -> None:
    # Session A (in window): 2 conexus:debugging, 1 release, 1 Agent dispatch,
    # 1 user slash command, plus one sidechain Skill record that must be ignored.
    _write(root / "aaaa.jsonl", [
        _slash("conexus:rdr-gate", IN_TS),
        _tool("Skill", IN_TS, skill="conexus:debugging"),
        _tool("Skill", IN_TS, skill="conexus:debugging"),
        _tool("Skill", IN_TS, skill="release"),
        _tool("Agent", IN_TS, subagent_type="conexus:code-review-expert"),
        _tool("Skill", IN_TS, sidechain=True, skill="conexus:query"),
        _text(IN_TS),
    ])
    # Subagent of A: one Skill call, one Task dispatch with no subagent_type.
    _write(root / "aaaa" / "subagents" / "agent-1.jsonl", [
        _tool("Skill", IN_TS, sidechain=True, skill="site-page"),
        _tool("Task", IN_TS, sidechain=True),
    ])
    # Session B (in window): 60 assistant turns, zero Skill calls.
    _write(root / "bbbb.jsonl", [_tool("Bash", IN_TS, command="ls") for _ in range(60)])
    # Session C (out of window): would add a Skill call if counted.
    _write(root / "cccc.jsonl", [_tool("Skill", OUT_TS, skill="conexus:code-review")])


def _run(root: Path) -> dict:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--start", START,
         "--end", END, "--json"],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_counts_known_by_construction(tmp_path: Path) -> None:
    _fixture(tmp_path)
    r = _run(tmp_path)
    main, sub = r["main"], r["subagent"]
    assert main["runs"] == 2
    assert main["runs_with_skill"] == 1
    assert main["skill_calls"] == {"conexus:debugging": 2, "release": 1}
    assert main["slash_commands"] == {"conexus:rdr-gate": 1}
    assert main["agent_dispatches"] == {"conexus:code-review-expert": 1}
    assert main["long_runs"] == 1
    assert main["long_runs_without_skill"] == 1
    assert sub["runs"] == 1
    assert sub["runs_with_skill"] == 1
    assert sub["skill_calls"] == {"site-page": 1}
    assert sub["agent_dispatches"] == {"(default)": 1}


def test_sidechain_records_in_a_session_file_are_not_main_calls(tmp_path: Path) -> None:
    _fixture(tmp_path)
    assert "conexus:query" not in _run(tmp_path)["main"]["skill_calls"]


def test_out_of_window_run_is_excluded_whole(tmp_path: Path) -> None:
    _fixture(tmp_path)
    r = _run(tmp_path)
    assert "conexus:code-review" not in r["main"]["skill_calls"]
    assert r["main"]["runs"] == 2


def test_empty_window_fails_loud(tmp_path: Path) -> None:
    _fixture(tmp_path)
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path),
         "--start", "2025-01-01T00:00:00+00:00", "--end", "2025-02-01T00:00:00+00:00"],
        capture_output=True, text=True)
    assert out.returncode != 0
    assert "no runs" in out.stderr.lower()
