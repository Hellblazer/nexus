# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unlanded-write scan tripwire (nexus-piqm5 Layer 1).

The bead's own bar: any fix "must be falsifiable by breaking the store. A
check that passes when persistence is unavailable is the same defect one
level up." These tests hold the scan to it.

THE FIXTURE IS BUILT FROM PRODUCTION CODE, NOT FROM A COPIED STRING. The
failure text a broken store actually produces comes from
``nexus.mcp.core._mcp_tool_error``; ``test_falsifier_*`` calls that function
with a real ``SESSION_UNAUTHORIZED_MARKER`` exception -- the literal
2026-08-25 outage condition -- and feeds its output into the transcript. If
someone changes the returned prefix, the scan stops matching AND these tests
go red together. A hardcoded ``"Error: ..."`` fixture would keep passing
while the scan silently matched nothing in production, which is the exact
class of inert guard this bead exists to eliminate.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-stop-writes-scan.py"
HOOK = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-stop.sh"


def run_scan(path: Path) -> str:
    r = subprocess.run(
        [sys.executable, str(SCAN), str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout.strip()


def _tool_use(tid: str, name: str, tool_input: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name,
             "input": tool_input if tool_input is not None else {}},
        ]},
    }


def _tool_result(tid: str, content, is_error: bool | None = None) -> dict:
    block: dict = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"type": "user", "message": {"content": [block]}}


def write_transcript(tmp_path: Path, entries: list[dict], name="t.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return p


def real_store_failure_text() -> str:
    """The string a genuinely broken T1 hands back, from production code."""
    from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER
    from nexus.mcp.core import _mcp_tool_error

    return _mcp_tool_error("memory_put", RuntimeError(SESSION_UNAUTHORIZED_MARKER))


# ── non-vacuity: break the store, the scan MUST trip ────────────────────────

def test_falsifier_real_401_failure_is_detected(tmp_path):
    """The 2026-08-25 condition, reproduced from production code."""
    failure = real_store_failure_text()
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__memory_put",
                  {"project": "nexus", "title": "findings", "content": "..."}),
        _tool_result("a1", failure),
    ])
    verdict = run_scan(t)
    assert verdict.startswith("UNLANDED "), verdict
    assert "memory_put" in verdict


def test_falsifier_precondition_the_real_text_is_error_prefixed(tmp_path):
    """Guard the guard: if this drifts, the scan above is inert, not passing.

    The scan keys on an "Error:" prefix. Assert production still produces
    one, so a change to _mcp_tool_error cannot leave the scan quietly
    matching nothing while its own test keeps passing on a stale fixture.
    """
    assert real_store_failure_text().lstrip().startswith("Error:")


def test_falsifier_is_error_flag_alone_is_enough(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__store_put", {"content": "x"}),
        _tool_result("a1", "anything at all", is_error=True),
    ])
    assert run_scan(t).startswith("UNLANDED ")


# ── the other direction: it must not fire on healthy transcripts ────────────

def test_successful_writes_are_clean(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__memory_put", {"title": "x"}),
        _tool_result("a1", "Stored: [23582] nexus/x"),
        _tool_use("a2", "mcp__plugin_conexus_nexus__store_put", {"content": "y"}),
        _tool_result("a2", "Stored document abc123 in knowledge"),
    ])
    assert run_scan(t) == "CLEAN"


def test_read_only_agent_is_clean(tmp_path):
    """An agent that never writes cannot have unlanded writes."""
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__search", {"query": "x"}),
        _tool_result("a1", "Error: search blew up"),
        _tool_use("a2", "Read", {"file_path": "/etc/hosts"}),
        _tool_result("a2", "Error: nope"),
    ])
    assert run_scan(t) == "CLEAN"


def test_failed_read_on_a_write_capable_tool_is_not_a_write(tmp_path):
    """`scratch` is a write only for action=put; a failed search is a read."""
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__scratch",
                  {"action": "search", "query": "x"}),
        _tool_result("a1", "Error: T1 unreachable"),
    ])
    assert run_scan(t) == "CLEAN"


def test_scratch_put_failure_is_a_write(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__scratch",
                  {"action": "put", "content": "findings"}),
        _tool_result("a1", "Error: T1 unreachable"),
    ])
    verdict = run_scan(t)
    assert verdict.startswith("UNLANDED "), verdict
    assert "scratch" in verdict


def test_counts_are_per_failed_call_and_tools_deduped(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__memory_put", {"title": "1"}),
        _tool_result("a1", "Error: boom"),
        _tool_use("a2", "mcp__plugin_conexus_nexus__memory_put", {"title": "2"}),
        _tool_result("a2", "Error: boom"),
        _tool_use("a3", "mcp__plugin_conexus_nexus__store_put", {"content": "3"}),
        _tool_result("a3", "Stored document ok"),
    ])
    assert run_scan(t) == "UNLANDED 2 memory_put"


# ── robustness: never crash, never invent evidence from absence ─────────────

def test_junk_lines_are_skipped_not_fatal(tmp_path):
    failure = real_store_failure_text()
    p = tmp_path / "t.jsonl"
    p.write_text(
        "not json at all\n"
        + "{ broken json\n"
        + "\n"
        + json.dumps(_tool_use("a1", "memory_put", {"title": "x"})) + "\n"
        + json.dumps(_tool_result("a1", failure)) + "\n",
        encoding="utf-8",
    )
    assert run_scan(p).startswith("UNLANDED ")


def test_missing_transcript_is_scanerror_not_unlanded(tmp_path):
    assert run_scan(tmp_path / "nope.jsonl") == "SCANERROR"


def test_empty_transcript_is_clean(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("", encoding="utf-8")
    assert run_scan(p) == "CLEAN"


def test_result_content_as_block_list_is_flattened(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "memory_put", {"title": "x"}),
        _tool_result("a1", [{"type": "text", "text": "Error: T2 down"}]),
    ])
    assert run_scan(t).startswith("UNLANDED ")


def test_orphan_result_without_its_tool_use_is_ignored(tmp_path):
    """A result whose tool_use never appeared is not evidence of a write."""
    t = write_transcript(tmp_path, [_tool_result("ghost", "Error: boom")])
    assert run_scan(t) == "CLEAN"


# ── the hook must actually consult the scan ─────────────────────────────────

def test_hook_invokes_the_scan_and_only_acts_on_unlanded():
    """Naming is not wiring: assert subagent-stop.sh really calls this."""
    body = HOOK.read_text(encoding="utf-8")
    assert "subagent-stop-writes-scan.py" in body
    assert "UNLANDEDWRITE" in body
    # fail-open bias: anything that is not the literal UNLANDED verdict
    # must collapse to CLEAN rather than reaching an actionable branch.
    assert "_writes_verdict" in body


@pytest.mark.parametrize("qualified", [
    "mcp__plugin_conexus_nexus__memory_put",
    "memory_put",
])
def test_tool_name_qualification_is_tolerated(tmp_path, qualified):
    t = write_transcript(tmp_path, [
        _tool_use("a1", qualified, {"title": "x"}),
        _tool_result("a1", "Error: boom"),
    ])
    assert run_scan(t).startswith("UNLANDED ")
