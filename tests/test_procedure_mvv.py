# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-240 Minimum Viable Validation (as rewritten under gate r2 Significant 2; nexus-qo7ie.31).

The JSON call through the MCP tool ``procedure_run`` and through ``nx run transcripts --args-file -``
returns identical JSON and no message body. Both doors call ``run_procedure``, which runs the real
run path: the shipped ``transcripts.lua`` in a ``python -I`` worker, its ``transcripts.sessions`` verb
answered in this process over fixture transcripts (``CLAUDE_CONFIG_DIR``). No store, no tenant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.mcp import core

DAY = "2026-11-03"
SECRET = "assistant prose that must never come back"


def _jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def _assistant(ts: str, sid: str, blocks: list, mid: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "sessionId": sid, "isSidechain": False,
            "message": {"id": mid, "model": "claude-opus-4-8", "role": "assistant", "content": blocks,
                        "usage": {"input_tokens": 3, "output_tokens": 5}}}


@pytest.fixture
def transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "cfg"
    p = root / "projects" / "-tmp-proj"
    for i in range(3):
        sid = f"s{i}"
        _jsonl(p / f"{sid}.jsonl", [
            {"type": "user", "timestamp": f"{DAY}T1{i}:00:00Z", "sessionId": sid,
             "message": {"role": "user", "content": "a prompt"}},
            _assistant(f"{DAY}T1{i}:00:01Z", sid, [
                {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "nx search foo"}},
                {"type": "tool_use", "id": f"u{i}", "name": "mcp__plugin_conexus_nexus__memory_get",
                 "input": {"project": "nexus", "title": "push-train"}}], f"m{i}"),
            _assistant(f"{DAY}T1{i}:00:02Z", sid, [{"type": "text", "text": SECRET}], f"n{i}"),
        ])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.chdir(tmp_path)
    return "-tmp-proj"


@pytest.mark.parametrize("query", ["tools", "memory", "violations", "agents"])
def test_both_front_doors_return_identical_json_and_no_body(transcripts: str, query: str) -> None:
    args = {"query": query, "since": DAY, "until": "2026-11-04", "project": transcripts}
    via_mcp = core.procedure_run(name="transcripts", args=args)
    cli = CliRunner().invoke(main, ["run", "transcripts", "--args-file", "-"], input=json.dumps(args))
    assert cli.exit_code == 0, cli.output
    assert json.loads(str(via_mcp)) == json.loads(cli.output)
    assert SECRET not in str(via_mcp) and SECRET not in cli.output
    assert json.loads(cli.output)["until"] == "2026-11-04"


def test_the_tools_rows_carry_what_the_fixture_holds(transcripts: str) -> None:
    args = {"query": "tools", "since": DAY, "until": "2026-11-04", "project": transcripts}
    rows = json.loads(str(core.procedure_run(name="transcripts", args=args)))["rows"]
    names = {(r.get("name"), r.get("session_id")) for r in rows}
    assert {("nx search", f"s{i}") for i in range(3)} <= names, sorted(names)
