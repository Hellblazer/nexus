# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 (nexus-wauo1.40): the grant-mode stream reader reports a tool as ok
only when it was called AND returned a non-error, non-auth-failure result."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "e2e" / "migration-rehearsal" / "lib" / "stream_tool_calls.py"
OP = "mcp__nexus__operator_summarize"
EN = "mcp__nexus__nx_enrich_beads"


def _use(tid: str, name: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def _res(tid: str, body: object, is_error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": body, "is_error": is_error}]}}


def _run(events: list[dict], tmp_path: Path) -> tuple[dict[str, str], str]:
    out = tmp_path / "result.txt"
    stdin = "\n".join(json.dumps(e) for e in events) + "\nnot json\n"
    proc = subprocess.run(
        [sys.executable, str(HELPER), "--result", str(out), OP, EN],
        input=stdin, capture_output=True, text=True, check=True,
    )
    status = dict(line.split(" ", 1) for line in proc.stdout.splitlines())
    return status, out.read_text()


def test_called_and_returned_is_ok(tmp_path: Path) -> None:
    status, result = _run([
        _use("a", OP), _res("a", "A summary."),
        _use("b", EN), _res("b", [{"type": "text", "text": "Enriched: files x.py"}]),
        {"type": "result", "result": "done WORKLOADDONE"},
    ], tmp_path)
    assert status == {OP: "ok", EN: "ok"}
    assert result == "done WORKLOADDONE"


def test_marker_text_without_a_call_is_missing(tmp_path: Path) -> None:
    status, _ = _run([{"type": "result", "result": "SUMMARY: made up ENRICHED: made up"}], tmp_path)
    assert status == {OP: "missing", EN: "missing"}


def test_error_empty_and_auth_failure_are_errors(tmp_path: Path) -> None:
    status, _ = _run([
        _use("a", OP), _res("a", "boom", is_error=True),
        _use("b", EN), _res("b", "Not logged in · Please run /login"),
    ], tmp_path)
    assert status == {OP: "error", EN: "error"}
    status, _ = _run([_use("a", OP), _res("a", "  "), _use("b", EN)], tmp_path)
    assert status == {OP: "error", EN: "error"}


def test_a_later_good_result_wins(tmp_path: Path) -> None:
    status, _ = _run([_use("a", OP), _res("a", "x", is_error=True), _use("c", OP), _res("c", "fine")], tmp_path)
    assert status[OP] == "ok"
